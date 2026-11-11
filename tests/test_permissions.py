"""Permission and scoping verification tests for ADK Agent Harness agents.

Verifies:
  * Strict fail-fast validation against typos in agent permissions.
  * Strict fail-fast validation against unknown MCP servers in agent definitions.
  * Strict fail-fast validation against unknown skills.
  * Strict fail-fast validation against local tools missing meta summaries.
  * Default-deny behavior in tool_run for unpermitted tools.
  * Confirmation required for ask-tier tools in tool_run.
  * Valid approval resolution and single-use consumption.
  * Correct dynamic prompt scoping without token bloat.

Run: .venv/bin/python tests/test_permissions.py
"""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Safe defaults for testing in environments without a populated .env
os.environ.setdefault("ADK_MODEL", "openai-gpt4o")
os.environ.setdefault("OPENAI_API_KEY", "mock-test-key")

from harness.config import config
from harness.loader import agent_catalog, _extract_mcp_tools
from harness.permissions import approval_registry
from harness.schema import AgentDefinition


class FakeContext:
    tool_confirmation = None

    class _Session:
        id = "ses_test"
        user_id = "operator"

    session = _Session()

    def __init__(self):
        self.requested = []

    def request_confirmation(self, *, hint, payload):
        self.requested.append({"hint": hint, "payload": payload})


class FakeConfirmedContext:
    def __init__(self, request_id: str):
        class _Confirmation:
            confirmed = True
            payload = {"request_id": request_id}

        class _Session:
            id = "ses_test"
            user_id = "operator"

        self.tool_confirmation = _Confirmation()
        self.session = _Session()


async def run_tests():
    print("\n--- [Test 1: Agent Permissions Typo Rejection (Fail-Fast)] ---")
    bad_agent = AgentDefinition(
        name="typo_agent",
        description="agent with typo",
        instructions="do work",
        skills=[],
        permissions={"allow": ["basic.file_readdddd"], "ask": []},
    )
    agent_catalog._definitions["typo_agent"] = bad_agent
    try:
        await agent_catalog.build_agent("typo_agent")
        assert False, "Should have raised ValueError for typo 'file_readdddd'"
    except ValueError as e:
        assert "file_readdddd" in str(e)
        print("✓ Typo in tool name caught and refused startup loudly")

    print("\n--- [Test 2: Unknown MCP Server Rejection] ---")
    bad_server_agent = AgentDefinition(
        name="bad_server",
        description="agent with bad server",
        instructions="do work",
        skills=[],
        permissions={"allow": ["nonexistent_server.tool"], "ask": []},
    )
    agent_catalog._definitions["bad_server"] = bad_server_agent
    try:
        await agent_catalog.build_agent("bad_server")
        assert False, "Should have raised ValueError for unknown server"
    except ValueError as e:
        assert "nonexistent_server" in str(e)
        print("✓ Unknown server in permissions rejected loudly")

    print("\n--- [Test 3: Unknown Skill Rejection] ---")
    bad_skill_agent = AgentDefinition(
        name="bad_skill",
        description="agent with bad skill",
        instructions="do work",
        skills=["unknown-skill-xyz"],
        permissions={"allow": [], "ask": []},
    )
    agent_catalog._definitions["bad_skill"] = bad_skill_agent
    try:
        await agent_catalog.build_agent("bad_skill")
        assert False, "Should have raised ValueError for unknown skill"
    except ValueError as e:
        assert "unknown-skill-xyz" in str(e)
        print("✓ Unknown skill rejected loudly")

    print("\n--- [Test 3b: Missing Tool Meta Summary Rejection] ---")
    original_extract = _extract_mcp_tools

    def mock_extract(mcp_dir):
        res = original_extract(mcp_dir)
        res["filesystem"]["no_summary_tool"] = {"summary": None, "doc": "doc", "args": []}
        return res

    bad_meta_agent = AgentDefinition(
        name="bad_meta_agent",
        description="agent with tool lacking meta",
        instructions="do work",
        skills=[],
        permissions={"allow": ["filesystem.no_summary_tool"], "ask": []},
    )
    agent_catalog._definitions["bad_meta_agent"] = bad_meta_agent
    with patch("harness.loader._extract_mcp_tools", side_effect=mock_extract):
        try:
            await agent_catalog.build_agent("bad_meta_agent")
            assert False, "Should have raised ValueError for tool lacking meta summary"
        except ValueError as e:
            assert "no meta summary description" in str(e)
            print("✓ Tool without meta summary rejected loudly")

    print("\n--- [Test 4: Default-Deny in tool_run] ---")
    test_agent = AgentDefinition(
        name="scoped_agent",
        description="scoped agent",
        instructions="instructions",
        skills=[],
        permissions={
            "allow": ["basic.file_read"],
            "ask": ["basic.file_delete"],
        },
    )
    agent_catalog._definitions["scoped_agent"] = test_agent
    agent = await agent_catalog.build_agent("scoped_agent")

    tools = {t.name: t for t in agent.tools}
    assert "tool_describe" in tools and "tool_run" in tools
    assert len(tools) == 2, f"Agent must have strictly 2 tools, got {len(tools)}"

    tool_run = tools["tool_run"]
    tool_describe = tools["tool_describe"]

    res = await tool_run.run_async(args={"tool_name": "basic.memory_read"}, tool_context=FakeContext())
    assert "error" in res and "not allowed" in res["error"]
    print("✓ Unpermitted tool 'basic.memory_read' denied by default")

    desc_res = await tool_describe.run_async(args={"tool_name": "basic.memory_read"}, tool_context=None)
    assert "error" in desc_res
    print("✓ tool_describe on unpermitted tool denied")

    print("\n--- [Test 5: Ask Tier Confirmation Flow] ---")
    ctx = FakeContext()
    ask_res = await tool_run.run_async(
        args={"tool_name": "basic.file_delete", "arguments": {"file_path": "tmp/obsolete.txt"}},
        tool_context=ctx,
    )
    assert ask_res.get("status") == "confirmation_required", f"Expected confirmation_required, got {ask_res}"
    req_id = ask_res["request_id"]
    assert len(ctx.requested) == 1
    assert ctx.requested[0]["payload"]["request_id"] == req_id
    print("✓ Ask tier tool paused and triggered confirmation request")

    resolved = approval_registry.resolve_request(req_id, "approved")
    assert resolved is not None and resolved.decision == "approved"
    print("✓ Operator approved request in registry")

    assert approval_registry.consume(req_id, tool_name="basic.file_delete", agent_name="scoped_agent")
    print("✓ Confirmation token successfully consumed")

    assert not approval_registry.consume(req_id, tool_name="basic.file_delete", agent_name="scoped_agent")
    print("✓ Second consumption correctly rejected (replay protection)")

    print("\n--- [Test 6: Valid Agent Loading & Dynamic Scoping] ---")
    agent_catalog.discover_and_load_all()
    assistant_agent = await agent_catalog.build_agent("assistant")
    assert len(assistant_agent.tools) == 2
    assert "filesystem.file_read" in assistant_agent.instruction
    assert "workspace-basics" in assistant_agent.instruction
    assert "memory.memory_read" in assistant_agent.instruction
    print(f"✓ Assistant agent instruction built with scoped tools ({len(assistant_agent.instruction)} chars)")

    print("\n--- [Test 7: Modular Agent Scoping Without Memory] ---")
    no_mem_agent = AgentDefinition(
        name="no_mem_agent",
        description="Agent configured without memory tools",
        instructions="Do coding work only.",
        skills=["workspace-basics"],
        permissions={
            "allow": [
                "filesystem.file_read",
                "filesystem.file_write",
                "skills.skill_load",
            ],
            "ask": [],
        },
    )
    agent_catalog._definitions["no_mem_agent"] = no_mem_agent
    built_no_mem = await agent_catalog.build_agent("no_mem_agent")
    instruction_str = str(built_no_mem.instruction)
    assert "filesystem.file_read" in instruction_str
    assert "memory.memory_read" not in instruction_str
    assert "memory.memory_write" not in instruction_str

    t_desc = built_no_mem.tools[0].func
    t_run = built_no_mem.tools[1].func
    desc_res = await t_desc("memory.memory_read")
    assert "error" in desc_res and "not permitted" in desc_res["error"]
    run_res = await t_run("memory.memory_read", {})
    assert "error" in run_res and "not allowed" in run_res["error"]
    print("✓ Agent configured without memory tools cleanly rejects memory operations by default")

    print("\n--- [Test 8: Backwards Compatibility Tool Aliases] ---")
    alias_agent = AgentDefinition(
        name="alias_agent",
        description="Agent using legacy basic.* tool names",
        instructions="Run tasks",
        skills=[],
        permissions={"allow": ["basic.file_read", "basic.memory_read"], "ask": []},
    )
    agent_catalog._definitions["alias_agent"] = alias_agent
    built_alias = await agent_catalog.build_agent("alias_agent")
    assert "filesystem.file_read" in built_alias.instruction
    assert "memory.memory_read" in built_alias.instruction
    print("✓ Legacy basic.* permissions seamlessly mapped to modular filesystem.* and memory.*")

    print("\n=======================================================")
    print(" ALL PERMISSION & SCOPING TESTS PASSED CLEANLY!")
    print("=======================================================\n")


def test_agent_permissions():
    """Pytest entrypoint for the permission & scoping verification suite."""
    asyncio.run(run_tests())


if __name__ == "__main__":
    asyncio.run(run_tests())
