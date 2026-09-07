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
**The low-level `Server` API, not `FastMCP`.** FastMCP derives each tool's schema from a
decorated Python function's type hints. That would mean a second definition of every
tool, alongside `TOOL_REGISTRY`, and two definitions drift. The low-level API lets this
module *read* the registry at request time, so there is exactly one statement of what
the agent may do -- which is the property the registry's own docstring promises.

**Every tool is annotated `readOnlyHint=True`.** Nothing in CostProof modifies a cloud
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

**Every call is audited.** One JSON line per call, appended to
``outputs/audit/mcp-calls.jsonl`` -- the same discipline as the in-process review, so an
MCP-driven session is as reconstructable as a CLI one.

**Resources expose the evidence, not just the tools.** The knowledge base, the latest
report and the estimator scorecard are readable directly, so a client can quote the
runbook rather than only search it.

**One prompt, `cost_review`, carries the operating rules.** The "model narrates, it never
calculates" contract is part of the protocol surface, not something the client has to
know in advance.

Running it
----------
    python -m costproof.agent.mcp_server              # stdio transport, for a client
    python -m costproof.agent.mcp_server --self-test  # print what a client would see

Claude Desktop configuration (claude_desktop_config.json):

    {"mcpServers": {"costproof": {
        "command": "python",
        "args": ["-m", "costproof.agent.mcp_server"]
    }}}

The server changes its working directory to the repository root at startup, so the
relative data paths in `tools.py` resolve wherever the client launched it from.
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

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from costproof.agent import llm, tools
from costproof.agent.review import MATERIALITY_THRESHOLD_USD

SERVER_NAME = "costproof"
SERVER_VERSION = "0.1.0"

#: src/costproof/agent/mcp_server.py -> parents[3] is the repository root.
ROOT = Path(__file__).resolve().parents[3]
KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"
AUDIT_LOG = ROOT / "outputs" / "audit" / "mcp-calls.jsonl"

# =======================================================================================
# Serialisation
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
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(obj, (np.bool_,)):
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


# =======================================================================================
# Resources
# =======================================================================================

#: Static resources. URI -> (path, mime, description). Paths relative to ROOT.
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
# Server
# =======================================================================================

server = Server(
    SERVER_NAME,
    version=SERVER_VERSION,
    # Delivered to the client at initialisation, before it sees a single tool. This is
    # the protocol's designated place for operating rules the client should know.
    instructions=(
        "CostProof measures cloud cost and the causal effect of optimisations. Every "
        "tool is read-only. Every figure you report must come verbatim from a tool "
        "result; never compute, round or combine numbers yourself. Reproduce each "
        "result's caveats. A result whose governance block says "
        "requires_human_approval=true must not be acted on without saying so."
    ),
)


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """Advertise exactly what TOOL_REGISTRY contains -- nothing more, nothing less."""
    out = []
    for name, entry in tools.TOOL_REGISTRY.items():
        out.append(types.Tool(
            name=name,
            description=entry["description"],
            inputSchema=entry["parameters"],
            annotations=types.ToolAnnotations(
                title=name.replace("_", " "),
                readOnlyHint=True,      # nothing in CostProof mutates an estate
                destructiveHint=False,
                idempotentHint=True,    # same inputs, same data -> same answer
                openWorldHint=False,    # reads local Parquet, calls no external service
            ),
        ))
    return out


#: The `method` string `_timed` stamps on a tool that raised. It is the one reliable
#: signal that a tool crashed rather than returned a considered "no".
_CRASHED = "failed before producing a value"


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    """Run a registry tool and return its full provenance.

    Two different kinds of "not ok", and the protocol must tell them apart:

    * **isError = true** -- the request itself failed. The tool does not exist, or it
      raised. The client should treat the call as having produced nothing.
    * **isError = false, ok = false** -- the tool ran and its considered answer is "no":
      the parallel-trends check failed, no control group could be built, no passage
      cleared the relevance floor. That is an analytical outcome, not a failure, and it
      is often the most valuable thing the tool can say.

    Collapsing the second into the first would teach a client that a refused savings
    claim is a malfunction to retry, which is precisely backwards.

    The SDK has already validated `arguments` against the tool's inputSchema.
    """
    started = datetime.now(timezone.utc).isoformat()
    t0 = time.perf_counter()

    # Some tools are CPU-heavy (find_waste trains a model). Run them off the event
    # loop so the server keeps answering list/ping requests meanwhile.
    result: tools.ToolResult = await asyncio.to_thread(tools.call, name, **arguments)

    value = _jsonable(result.value)
    is_error = name not in tools.TOOL_REGISTRY or result.method == _CRASHED
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
        structuredContent=payload,
        isError=is_error,
    )


@server.list_resources()
async def list_resources() -> list[types.Resource]:
    """Only advertise files that exist. A resource that 404s is worse than none."""
    out = []
    for uri, (path, mime, desc) in RESOURCES.items():
        if path.exists():
            out.append(types.Resource(
                uri=uri, name=uri.split("//", 1)[1], description=desc,
                mimeType=mime, size=path.stat().st_size,
            ))
    return out


@server.read_resource()
async def read_resource(uri) -> str:
    key = str(uri)
    if key not in RESOURCES:
        raise ValueError(f"unknown resource {key!r}; available: {sorted(RESOURCES)}")
    path, _, _ = RESOURCES[key]
    if not path.exists():
        raise FileNotFoundError(f"{key} is registered but {path} has not been generated "
                                f"yet -- run `python -m costproof.cli review` or `study`")
    return path.read_text()


@server.list_prompts()
async def list_prompts() -> list[types.Prompt]:
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


@server.get_prompt()
async def get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
    if name != "cost_review":
        raise ValueError(f"unknown prompt {name!r}")
    focus = (arguments or {}).get("focus", "").strip()
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
        messages=[
            types.PromptMessage(role="user", content=types.TextContent(
                type="text", text=llm.SYSTEM_PROMPT + "\n\n---\n\n" + task)),
        ],
    )


# =======================================================================================
# Entry points
# =======================================================================================


async def _serve() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def self_test() -> int:
    """Print what a client would see. No transport, no client needed."""
    async def _run():
        tl = await list_tools()
        rl = await list_resources()
        pl = await list_prompts()
        return tl, rl, pl

    tl, rl, pl = asyncio.run(_run())
    print(f"\n{SERVER_NAME} MCP server v{SERVER_VERSION}  (root: {ROOT})\n")
    print(f"TOOLS ({len(tl)})")
    for t in tl:
        req = t.inputSchema.get("required", [])
        props = t.inputSchema.get("properties", {})
        sig = ", ".join(f"{p}{'' if p in req else '?'}" for p in props) or "—"
        print(f"  {t.name:24s} ({sig})")
        print(f"  {'':24s} {t.description}")
    print(f"\nRESOURCES ({len(rl)} available of {len(RESOURCES)} registered)")
    for r in rl:
        print(f"  {str(r.uri):46s} {r.mimeType:14s} {r.size or 0:>7,} bytes")
    missing = [u for u, (p, _, _) in RESOURCES.items() if not p.exists()]
    for u in missing:
        print(f"  {u:46s} (not generated yet)")
    print(f"\nPROMPTS ({len(pl)})")
    for p in pl:
        args = ", ".join(a.name + ("" if a.required else "?") for a in (p.arguments or []))
        print(f"  {p.name:24s} ({args or '—'})")
    print(f"\nAUDIT LOG  {AUDIT_LOG.relative_to(ROOT)}")
    print(f"GATE       human approval at >= ${MATERIALITY_THRESHOLD_USD:,.0f}/yr annualised impact\n")
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
