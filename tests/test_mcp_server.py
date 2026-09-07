"""Protocol-level tests for the MCP server.

These launch the real server as a subprocess and speak MCP to it over stdio, exactly
as Claude Desktop or any other client would. They are not unit tests of the handler
functions -- those would pass even if the transport, the handshake or the schema
validation were broken. What is being tested is the contract a client depends on.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="pip install 'costproof[agent]' to test the MCP server")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from costproof.agent import tools  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SILVER = ROOT / "data" / "silver" / "billing.parquet"

needs_data = pytest.mark.skipif(
    not SILVER.exists(),
    reason="run `python -m costproof.cli data` and the silver SQL first",
)


def _params() -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "costproof.agent.mcp_server"],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )


@pytest.mark.asyncio
async def test_handshake_advertises_all_three_capabilities():
    async with stdio_client(_params()) as (r, w), ClientSession(r, w) as s:
        init = await s.initialize()
        assert init.serverInfo.name == "costproof"
        assert init.capabilities.tools is not None
        assert init.capabilities.resources is not None
        assert init.capabilities.prompts is not None
        # Operating rules are delivered before the client sees a single tool.
        assert "never compute" in (init.instructions or "")


@pytest.mark.asyncio
async def test_tools_are_exactly_the_registry_and_all_read_only():
    """One definition of what the agent may do. The server must not add or drop any."""
    async with stdio_client(_params()) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        listed = await s.list_tools()
        assert {t.name for t in listed.tools} == set(tools.TOOL_REGISTRY)
        for t in listed.tools:
            assert t.inputSchema == tools.TOOL_REGISTRY[t.name]["parameters"]
            assert t.annotations.readOnlyHint is True
            assert t.annotations.destructiveHint is False


@pytest.mark.asyncio
async def test_schema_violation_is_rejected_before_the_tool_runs():
    """A wrong argument type must fail at the protocol layer, not inside the tool."""
    async with stdio_client(_params()) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        res = await s.call_tool("find_waste", {"top_n": "ten"})
        assert res.isError is True
        assert "not of type 'integer'" in res.content[0].text


@needs_data
@pytest.mark.asyncio
async def test_result_carries_provenance_and_governance():
    """The client gets method, caveats, sources and a gate decision -- not a bare number."""
    async with stdio_client(_params()) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        res = await s.call_tool("get_spend_summary", {})
        assert res.isError is False
        sc = res.structuredContent
        assert sc["ok"] is True
        for key in ("value", "method", "caveats", "sources", "governance"):
            assert key in sc, f"missing {key}"
        assert "EffectiveCost" in sc["method"]
        gov = sc["governance"]
        # Untagged spend on this estate is ~$970k/yr: well over the $1k gate.
        assert gov["requires_human_approval"] is True
        assert any("materiality" in reason for reason in gov["reasons"])


@pytest.mark.asyncio
async def test_unknown_tool_is_a_protocol_error():
    """Asking for a tool that does not exist is the client's mistake: isError=true."""
    async with stdio_client(_params()) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        res = await s.call_tool("delete_everything", {})
        assert res.isError is True
        assert res.structuredContent["ok"] is False
        assert "not in the registry" in res.structuredContent["error"]


@pytest.mark.asyncio
async def test_a_considered_no_is_not_an_error():
    """A tool that ran and found nothing has succeeded. isError must stay false.

    This is the distinction the whole governance model rests on: a refused savings
    claim or an empty retrieval is information, not a malfunction to retry.
    """
    async with stdio_client(_params()) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        res = await s.call_tool("search_knowledge",
                                {"query": "xylophone quarterback marmalade"})
        assert res.isError is False
        sc = res.structuredContent
        assert sc["ok"] is False
        assert "relevance floor" in sc["error"]
        assert sc["governance"]["requires_human_approval"] is False


@pytest.mark.asyncio
async def test_knowledge_resources_are_readable():
    async with stdio_client(_params()) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        listed = await s.list_resources()
        uris = {str(x.uri) for x in listed.resources}
        assert "costproof://knowledge/measurement-policy" in uris
        body = await s.read_resource("costproof://knowledge/measurement-policy")
        text = body.contents[0].text
        assert "approval" in text.lower()


@pytest.mark.asyncio
async def test_cost_review_prompt_embeds_the_no_arithmetic_rule():
    async with stdio_client(_params()) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        gp = await s.get_prompt("cost_review", {"focus": "bu-datasci"})
        text = gp.messages[0].content.text
        assert "must appear verbatim" in text   # rule 1 of the system prompt
        assert "bu-datasci" in text             # the argument was injected
