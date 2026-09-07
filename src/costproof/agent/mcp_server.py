"""CostProof as an MCP server.

What MCP is, in one paragraph
-----------------------------
The Model Context Protocol is an open standard for connecting a language model to
tools, data and prompt templates over a uniform interface. A server advertises what it
offers; any MCP-aware client -- Claude Desktop, an IDE, an agent framework -- can
discover and use it without custom integration code. It is to model-tool integration
what ODBC was to databases: one adapter instead of N x M.

Design decisions, and why
-------------------------
**The low-level `Server`, not `MCPServer`.** The high-level class derives each tool's
schema from a decorated Python function's type hints. That would mean a second
definition of every tool, alongside `TOOL_REGISTRY`, and two definitions drift. The
low-level API lets this module *read* the registry at request time, so there is exactly
one statement of what the agent may do -- the property the registry's docstring promises.

**The server validates every call against the tool's JSON Schema before dispatch.** The
v2 SDK leaves input validation to the low-level handler. A wrong argument type is
rejected here with a protocol-level error, and the tool never runs -- so a rejected call
never reaches the audit log, because nothing happened.

**Every tool is annotated `read_only_hint=True`.** Nothing in CostProof modifies a cloud
estate. The annotation is a machine-readable version of that fact, and it is what lets a
client decide a call is safe to auto-approve. Declaring it honestly matters more than
declaring it at all: a server that marks a destructive tool read-only has broken the
protocol's trust model.

**Provenance travels with every result.** Each response carries the tool's `method`,
its `caveats` and its `sources`, exactly as the in-process agent receives them. The
client's model may summarise; it cannot claim it was never told.

**Governance is made machine-readable.** A server cannot force a human approval step
inside a client it does not control. What it can do is attach a `governance` block to
any result whose annualised impact clears the materiality threshold, stating that the
finding requires human sign-off before action. The gate travels with the data.

**Two kinds of "no", kept distinct at the protocol level.** `is_error=True` means the
request itself failed: the tool does not exist, the arguments were invalid, or the tool
raised. `is_error=False` with `ok=False` means the tool ran and its considered answer is
"no": the parallel-trends check failed, no control group could be built, no passage
cleared the relevance floor. Collapsing the second into the first would teach a client
that a refused savings claim is a malfunction to retry -- precisely backwards.

**Every call is audited.** One JSON line per call, appended to
``outputs/audit/mcp-calls.jsonl`` -- the same discipline as the in-process review.

**Resources expose the evidence, not just the tools.** The knowledge base, the latest
report and the estimator scorecard are readable directly, so a client can quote the
runbook rather than only search it.

**One prompt, `cost_review`, carries the operating rules,** and the same rules are sent
as server `instructions` during the handshake -- before the client sees a single tool.

Running it
----------
    python -m costproof.agent.mcp_server              # stdio transport, for a client
    python -m costproof.agent.mcp_server --self-test  # print what a client would see

Claude Desktop (claude_desktop_config.json):

    {"mcpServers": {"costproof": {
        "command": "/path/to/python",
        "args": ["-m", "costproof.agent.mcp_server"]
    }}}

The server changes its working directory to the repository root at startup, so the
relative data paths in `tools.py` resolve wherever the client launched it from.

Requires ``mcp>=2.0``. The 1.x SDK had a different handler API (decorators on the
server object); this module targets the 2.x constructor-injected handlers.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jsonschema
import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError

from costproof.agent import llm, tools
from costproof.agent.review import MATERIALITY_THRESHOLD_USD

SERVER_NAME = "costproof"
SERVER_VERSION = "0.2.0"

#: src/costproof/agent/mcp_server.py -> parents[3] is the repository root.
ROOT = Path(__file__).resolve().parents[3]
KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"
AUDIT_LOG = ROOT / "outputs" / "audit" / "mcp-calls.jsonl"

INSTRUCTIONS = (
    "CostProof measures cloud cost and the causal effect of optimisations. Every tool is "
    "read-only. Every figure you report must come verbatim from a tool result; never "
    "compute, round or combine numbers yourself. Reproduce each result's caveats. A "
    "result whose governance block says requires_human_approval=true must not be acted "
    "on without saying so."
)

#: The `method` string `_timed` stamps on a tool that raised. It is the one reliable
#: signal that a tool crashed rather than returned a considered "no".
_CRASHED = "failed before producing a value"


# =======================================================================================
# Serialisation and governance
# =======================================================================================


def _jsonable(obj: Any) -> Any:
    """Convert tool output into something json.dumps accepts.

    Tool values arrive as DataFrames, numpy scalars, Timestamps and NaNs. JSON has none
    of those. NaN in particular is not valid JSON at all -- Python will emit it, most
    parsers will reject it -- so it becomes null explicitly.
    """
    import numpy as np
    import pandas as pd

    if isinstance(obj, pd.DataFrame):
        return [_jsonable(r) for r in obj.to_dict(orient="records")]
    if isinstance(obj, pd.Series):
        return _jsonable(obj.to_dict())
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, (pd.Timestamp, datetime)):
        return obj.isoformat()
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return obj


def _annualised_impact(name: str, value: Any) -> float | None:
    """Pull the annualised dollar figure out of a tool result, if it has one.

    Each tool reports its impact under a different key because each measures a
    different thing. This is the one place that knowledge lives.
    """
    try:
        if name == "check_commitment_waste":
            return float(value["annualised"])
        if name == "estimate_savings":
            return float(value["annualised_saving"])
        if name == "find_waste":
            return float(sum(r.get("annualised_cost") or 0 for r in value))
        if name == "get_spend_summary":
            rows = [r for r in value if r.get("business_unit") == "unallocated"]
            return float(rows[0]["annualised"]) if rows else None
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    return None


def _governance(name: str, result: tools.ToolResult, value: Any) -> dict:
    """The same gate the in-process review applies, expressed as data."""
    impact = _annualised_impact(name, value)
    reasons: list[str] = []
    if impact is not None and impact >= MATERIALITY_THRESHOLD_USD:
        reasons.append(f"annualised impact ${impact:,.0f} >= "
                       f"${MATERIALITY_THRESHOLD_USD:,.0f} materiality threshold")
    if any("PARALLEL-TRENDS CHECK FAILED" in c for c in result.caveats):
        reasons.append("estimate failed its validity check and is not causal")
    if any("untagged" in c.lower() or "no business_unit" in c.lower()
           for c in result.caveats):
        reasons.append("owner cannot be established from the billing feed")
    return {
        "requires_human_approval": bool(reasons),
        "reasons": reasons or ["below all gates"],
        "annualised_impact_usd": impact,
        "note": ("No action has been taken. This server is read-only; it measures and "
                 "recommends, and a human decides."),
    }


def _audit(entry: dict) -> None:
    """Append one line. Never raise -- an audit failure must not fail the call."""
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except OSError as exc:  # pragma: no cover
        print(f"audit write failed: {exc}", file=sys.stderr)


def _error_result(payload: dict) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, indent=2))],
        structured_content=payload,
        is_error=True,
    )


# =======================================================================================
# Resources
# =======================================================================================

#: Static resources. URI -> (path, mime, description).
RESOURCES: dict[str, tuple[Path, str, str]] = {
    "costproof://knowledge/remediation-runbook": (
        KNOWLEDGE_DIR / "01-remediation-runbook.md", "text/markdown",
        "How each class of cloud waste is recognised and fixed, with the risk of each fix."),
    "costproof://knowledge/measurement-policy": (
        KNOWLEDGE_DIR / "02-measurement-policy.md", "text/markdown",
        "Which cost measure to use, the design hierarchy for savings claims, and the "
        "human approval gate."),
    "costproof://knowledge/pricing-reference": (
        KNOWLEDGE_DIR / "03-pricing-reference.md", "text/markdown",
        "List prices for the SKUs in the estate and the economics of a commitment."),
    "costproof://report/latest": (
        ROOT / "reports" / "cost-review.md", "text/markdown",
        "The most recent governed cost review: findings, methods, caveats, tool log."),
    "costproof://validation/scorecard": (
        ROOT / "outputs" / "tables" / "scorecard.csv", "text/csv",
        "Four estimators scored against known ground truth: bias, RMSE, coverage, "
        "false-positive rate, power."),
    "costproof://validation/dollar-impact": (
        ROOT / "outputs" / "tables" / "dollar_impact.csv", "text/csv",
        "Annualised dollar misstatement of each estimator against true savings."),
}


# =======================================================================================
# Handlers  --  each is  async (ctx, params) -> Result
# =======================================================================================


def tool_definitions() -> list[types.Tool]:
    """Exactly what TOOL_REGISTRY contains -- nothing more, nothing less."""
    return [
        types.Tool(
            name=name,
            description=entry["description"],
            input_schema=entry["parameters"],
            annotations=types.ToolAnnotations(
                title=name.replace("_", " "),
                read_only_hint=True,      # nothing in CostProof mutates an estate
                destructive_hint=False,
                idempotent_hint=True,     # same inputs, same data -> same answer
                open_world_hint=False,    # reads local Parquet, calls no external service
            ),
        )
        for name, entry in tools.TOOL_REGISTRY.items()
    ]


async def on_list_tools(ctx, params) -> types.ListToolsResult:
    return types.ListToolsResult(tools=tool_definitions())


async def on_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    """Validate, run, wrap with provenance and governance, audit."""
    name = params.name
    arguments: dict[str, Any] = dict(params.arguments or {})
    started = datetime.now(timezone.utc).isoformat()
    t0 = time.perf_counter()

    # --- 1. does it exist? -------------------------------------------------------------
    entry = tools.TOOL_REGISTRY.get(name)
    if entry is None:
        return _error_result({
            "tool": name, "ok": False, "value": None,
            "method": "unknown tool",
            "error": f"{name!r} is not in the registry. Available: "
                     f"{sorted(tools.TOOL_REGISTRY)}",
            "caveats": [], "sources": [],
            "governance": {"requires_human_approval": False, "reasons": ["no tool ran"],
                           "annualised_impact_usd": None, "note": ""},
        })

    # --- 2. do the arguments match the published schema? --------------------------------
    # The 2.x low-level server leaves this to the handler. Rejecting here means a bad
    # call never runs and never reaches the audit log -- nothing happened.
    try:
        jsonschema.validate(instance=arguments, schema=entry["parameters"])
    except jsonschema.ValidationError as exc:
        return _error_result({
            "tool": name, "ok": False, "value": None,
            "method": "rejected before dispatch",
            "error": f"Input validation error: {exc.message}",
            "caveats": [], "sources": [],
            "governance": {"requires_human_approval": False, "reasons": ["no tool ran"],
                           "annualised_impact_usd": None, "note": ""},
        })

    # --- 3. run it, off the event loop ---------------------------------------------------
    # Some tools are CPU-heavy (find_waste trains a model). to_thread keeps the server
    # answering list/ping requests meanwhile.
    result: tools.ToolResult = await asyncio.to_thread(tools.call, name, **arguments)

    value = _jsonable(result.value)
    is_error = result.method == _CRASHED
    payload = {
        "tool": name,
        "ok": result.ok,
        "value": value,
        "method": result.method,
        "caveats": list(result.caveats),
        "sources": list(result.sources),
        "governance": _governance(name, result, value),
        "error": result.error,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }
    _audit({
        "at": started, "transport": "mcp", "tool": name, "arguments": arguments,
        "ok": result.ok, "is_error": is_error, "elapsed_ms": payload["elapsed_ms"],
        "error": result.error,
        "requires_human_approval": payload["governance"]["requires_human_approval"],
    })
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, indent=2))],
        structured_content=payload,
        is_error=is_error,
    )


def resource_definitions() -> list[types.Resource]:
    """Only advertise files that exist. A resource that 404s is worse than none."""
    return [
        types.Resource(
            uri=uri, name=uri.split("//", 1)[1], description=desc,
            mime_type=mime, size=path.stat().st_size,
        )
        for uri, (path, mime, desc) in RESOURCES.items()
        if path.exists()
    ]


async def on_list_resources(ctx, params) -> types.ListResourcesResult:
    return types.ListResourcesResult(resources=resource_definitions())


async def on_read_resource(ctx, params: types.ReadResourceRequestParams
                           ) -> types.ReadResourceResult:
    key = str(params.uri)
    if key not in RESOURCES:
        raise MCPError(types.INVALID_PARAMS,
                       f"unknown resource {key!r}; available: {sorted(RESOURCES)}")
    path, mime, _ = RESOURCES[key]
    if not path.exists():
        raise MCPError(types.INVALID_PARAMS,
                       f"{key} is registered but {path.name} has not been generated yet "
                       f"-- run `python -m costproof.cli review` or `study`")
    return types.ReadResourceResult(contents=[
        types.TextResourceContents(uri=key, mime_type=mime, text=path.read_text()),
    ])


def prompt_definitions() -> list[types.Prompt]:
    return [types.Prompt(
        name="cost_review",
        description=("Run a governed cloud cost review: spend, unit economics, "
                     "commitments, waste queue. Enforces the rule that the model "
                     "narrates tool output and never computes a figure itself."),
        arguments=[types.PromptArgument(
            name="focus",
            description="Optional business unit or topic to concentrate on, e.g. bu-datasci",
            required=False,
        )],
    )]


async def on_list_prompts(ctx, params) -> types.ListPromptsResult:
    return types.ListPromptsResult(prompts=prompt_definitions())


async def on_get_prompt(ctx, params: types.GetPromptRequestParams) -> types.GetPromptResult:
    if params.name != "cost_review":
        raise MCPError(types.INVALID_PARAMS, f"unknown prompt {params.name!r}")
    focus = (params.arguments or {}).get("focus", "").strip()
    task = (
        "Run a cloud cost review using the costproof tools. Call, in order: "
        "get_spend_summary, get_unit_economics, check_commitment_waste, then find_waste "
        "with top_n=10. For each finding, call search_knowledge to retrieve the "
        "remediation guidance and cite the section returned.\n\n"
        + (f"Concentrate on: {focus}.\n\n" if focus else "")
        + "Report each finding with its annualised impact, the method the tool used, its "
        "caveats verbatim, and whether the governance block says it requires human "
        "approval. Do not propose any action on a gated finding without saying that "
        "approval is required first."
    )
    return types.GetPromptResult(
        description="Governed cost review with grounded remediation guidance",
        messages=[types.PromptMessage(
            role="user",
            content=types.TextContent(type="text",
                                      text=llm.SYSTEM_PROMPT + "\n\n---\n\n" + task),
        )],
    )


# =======================================================================================
# Server
# =======================================================================================


def build_server() -> Server:
    """Construct the server. Handlers are injected, which is the 2.x idiom."""
    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        instructions=INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        on_list_resources=on_list_resources,
        on_read_resource=on_read_resource,
        on_list_prompts=on_list_prompts,
        on_get_prompt=on_get_prompt,
    )


async def _serve() -> None:
    server = build_server()
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def self_test() -> int:
    """Print what a client would see. No transport, no client needed."""
    tl, rl, pl = tool_definitions(), resource_definitions(), prompt_definitions()
    print(f"\n{SERVER_NAME} MCP server v{SERVER_VERSION}  (root: {ROOT})\n")
    print(f"TOOLS ({len(tl)})")
    for t in tl:
        req = t.input_schema.get("required", [])
        props = t.input_schema.get("properties", {})
        sig = ", ".join(f"{p}{'' if p in req else '?'}" for p in props) or "—"
        print(f"  {t.name:24s} ({sig})")
        print(f"  {'':24s} {t.description}")
    print(f"\nRESOURCES ({len(rl)} available of {len(RESOURCES)} registered)")
    for r in rl:
        print(f"  {str(r.uri):46s} {r.mime_type:14s} {r.size or 0:>7,} bytes")
    for u, (p, _, _) in RESOURCES.items():
        if not p.exists():
            print(f"  {u:46s} (not generated yet)")
    print(f"\nPROMPTS ({len(pl)})")
    for p in pl:
        args = ", ".join(a.name + ("" if a.required else "?") for a in (p.arguments or []))
        print(f"  {p.name:24s} ({args or '—'})")
    print(f"\nAUDIT LOG  {AUDIT_LOG.relative_to(ROOT)}")
    print(f"GATE       human approval at >= ${MATERIALITY_THRESHOLD_USD:,.0f}/yr "
          f"annualised impact\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # Relative data paths in tools.py assume the repo root. Make that true regardless
    # of where the client launched us from.
    os.chdir(ROOT)
    if "--self-test" in argv:
        return self_test()
    asyncio.run(_serve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
