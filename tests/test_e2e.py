"""End-to-End Verification Test for the ADK Agent Harness API and Dynamic Agent Configuration.

Run: .venv/bin/python tests/test_e2e.py
"""

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Safe defaults for testing in environments without a populated .env
os.environ.setdefault("ADK_MODEL", "openai-gpt4o")
os.environ.setdefault("OPENAI_API_KEY", "mock-test-key")

from starlette.testclient import TestClient
from harness.server import app
from harness.loader import agent_catalog


def test_api_endpoints():
    with TestClient(app) as client:
        print("\n--- [Test 1: Info & Agent Listing] ---")
        res = client.get("/api/info")
        assert res.status_code == 200, res.text
        data = res.json()
        print("Info response:", data)
        assert "assistant" in data.get("agents", [])

        res_agents = client.get("/api/agents")
        assert res_agents.status_code == 200
        agents_list = res_agents.json().get("agents", [])
        agent_names = [a["name"] for a in agents_list]
        print("Agents list:", agent_names)
        assert "assistant" in agent_names

        res_mcp = client.get("/api/mcp")
        assert res_mcp.status_code == 200
        mcp_data = res_mcp.json().get("data", [])
        assert len(mcp_data) > 0, "Discovered MCP servers should be reported"
        for s in mcp_data:
            assert "name" in s and "status" in s and "transport" in s
        print(f"✓ /api/mcp returned statuses for {len(mcp_data)} server(s)")

        print("\n--- [Test 2: Missing Agent Rejection] ---")
        res = client.post("/api/session", json={})
        assert res.status_code == 400, f"Expected 400, got {res.status_code}: {res.text}"
        print("✓ Missing agent correctly returned HTTP 400")

        print("\n--- [Test 3: Invalid Agent Rejection] ---")
        res = client.post("/api/session", json={"agent": "nonexistent_agent"})
        assert res.status_code == 404, f"Expected 404, got {res.status_code}: {res.text}"
        print("✓ Non-existent agent correctly returned HTTP 404")

        print("\n--- [Test 4: Valid Session Creation & Cleanup] ---")
        res = client.post("/api/session", json={"agent": "assistant"})
        assert res.status_code == 200, f"Expected 200, got {res.status_code}: {res.text}"
        session_id = res.json()["id"]
        print(f"✓ Session created: {session_id}")

        active_res = client.get("/api/session/active")
        assert active_res.status_code == 200
        print("✓ /api/session/active returned 200 OK")

        del_res = client.delete(f"/api/session/{session_id}?agent=assistant")
        assert del_res.status_code == 200, f"Expected 200 on delete, got {del_res.status_code}"
        print("✓ Created session cleaned up successfully")


def test_mcp_discovery_and_config():
    print("\n--- [Test 5: Local MCP Auto-Discovery + servers.json Registry] ---")
    from harness.mcp.manager import mcp_manager

    servers = mcp_manager.list_available_servers()
    assert "filesystem" in servers, f"Local 'filesystem' MCP server must be auto-discovered, got: {servers}"
    assert "skills" in servers, f"Local 'skills' MCP server must be auto-discovered, got: {servers}"
    assert "memory" in servers, f"Local 'memory' MCP server must be auto-discovered, got: {servers}"
    entry = mcp_manager.get_server_entry("filesystem")
    assert entry.transport == "sse"
    print(f"✓ Auto-discovered local servers: {servers}")

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f_ext:
        json.dump(
            {
                "mcpServers": {
                    "filesystem": {"disabled": True},
                    "external-tool": {
                        "transport": "stdio",
                        "command": "/usr/bin/env",
                        "args": ["true"],
                        "description": "dummy",
                    },
                }
            },
            f_ext,
        )
        ext_file = f_ext.name

    try:
        os.environ["ADK_MCP_SERVERS_FILE"] = ext_file
        servers = mcp_manager.reload_servers()
        assert "filesystem" not in servers, "Disabled local server should be removed"
        assert "external-tool" in servers
        ext = mcp_manager.get_server_entry("external-tool")
        assert ext.transport == "stdio" and ext.command == "/usr/bin/env"
        print("✓ servers.json disable + external stdio registration works")
    finally:
        os.environ.pop("ADK_MCP_SERVERS_FILE", None)
        mcp_manager.reload_servers()
        if os.path.exists(ext_file):
            os.remove(ext_file)


def test_agent_scoping():
    print("\n--- [Test 6: Agent Scoping & Definition Isolation] ---")
    assistant_def = agent_catalog.get_definition("assistant")
    assert "workspace-basics" in assistant_def.skills
    assert "filesystem.file_delete" in assistant_def.permissions.get("ask", [])
    assert "filesystem.file_read" in assistant_def.permissions.get("allow", [])
    print("✓ Assistant agent definition isolation verified")


def test_minimal_agent_tools_and_scoping():
    print("\n--- [Test 7: Minimal Tools (tool_describe + tool_run) & Prompt Scoping] ---")

    async def _run():
        assistant = await agent_catalog.build_agent("assistant")
        assert len(assistant.tools) == 2, f"Expected 2 tools, got {len(assistant.tools)}"
        tool_names = {t.name for t in assistant.tools}
        assert tool_names == {"tool_describe", "tool_run"}
        print("✓ LlmAgent has strictly 2 tools: tool_describe, tool_run (token bloat eliminated)")

        assert "## Available Tools" in assistant.instruction
        assert "filesystem.file_read" in assistant.instruction
        assert "## Available Skills" in assistant.instruction
        print(f"✓ Injected instruction contains allowed tool & skill summaries ({len(assistant.instruction)} chars)")

    asyncio.run(_run())


def test_basic_mcp_path_security():
    print("\n--- [Test 8: MCP Filesystem Internal Path Security] ---")
    spec = importlib.util.spec_from_file_location("filesystem_server", ROOT / "mcp" / "filesystem" / "server.py")
    fs_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fs_mod)
    file_read = fs_mod.file_read
    file_write = fs_mod.file_write
    file_delete = fs_mod.file_delete

    # 1. file_read outside authorized dirs -> denied
    res_env = file_read(".env")
    assert "Access denied" in res_env or "outside authorized" in res_env
    print("✓ file_read('.env') -> Access denied")

    res_passwd = file_read("/etc/passwd")
    assert "Access denied" in res_passwd or "outside authorized" in res_passwd
    print("✓ file_read('/etc/passwd') -> Access denied")

    # 2. file_write outside authorized dirs -> denied
    res_w_code = file_write("harness/server.py", "malicious content")
    assert "Access denied" in res_w_code
    print("✓ file_write('harness/server.py') -> Access denied")

    # 3. file_write inside tmp -> allowed
    tmp_dir = fs_mod._resolve_config_path("ADK_TMP_DIR", "tmp")
    tmp_path = str(tmp_dir / "test_e2e_write.txt")
    res_w_tmp = file_write(tmp_path, "hello world", overwrite=True)
    assert "written successfully" in res_w_tmp
    print("✓ file_write(tmp/...) -> ALLOWED")

    # 4. file_delete inside tmp -> allowed
    res_del = file_delete(tmp_path)
    assert "deleted successfully" in res_del
    print("✓ file_delete(tmp/...) -> ALLOWED")

    # 5. deletion outside -> denied
    res_del_bad = file_delete("harness/server.py")
    assert "Access denied" in res_del_bad
    print("✓ file_delete('harness/server.py') -> Access denied")


def test_server_basic_auth():
    print("\n--- [Test 9: Server HTTP Basic Authentication Middleware] ---")
    import base64
    from harness.config import config

    orig_user = config.server_username
    orig_pass = config.server_password
    try:
        with TestClient(app) as client:
            config.server_username = "operator"
            config.server_password = "secure_password_test"

            r_no_auth = client.get("/api/info")
            assert r_no_auth.status_code == 401, f"Expected 401, got {r_no_auth.status_code}"
            print("✓ Request without auth correctly returns HTTP 401")

            bad_hdr = {"Authorization": "Basic " + base64.b64encode(b"operator:wrong").decode()}
            r_bad = client.get("/api/info", headers=bad_hdr)
            assert r_bad.status_code == 401, f"Expected 401, got {r_bad.status_code}"
            print("✓ Request with bad credentials correctly returns HTTP 401")

            good_hdr = {"Authorization": "Basic " + base64.b64encode(b"operator:secure_password_test").decode()}
            r_good = client.get("/api/info", headers=good_hdr)
            assert r_good.status_code == 200, f"Expected 200, got {r_good.status_code}"
            print("✓ Request with valid credentials succeeds with HTTP 200")
    finally:
        config.server_username = orig_user
        config.server_password = orig_pass


def test_session_token_usage():
    """Verify live token usage serialization and session usage endpoints."""
    from google.adk.events import Event
    from google.genai.types import GenerateContentResponseUsageMetadata, Content, Part
    from harness.events import serialize_adk_event

    print("\n--- [Test 10: Token Usage Serialization] ---")
    mock_usage = GenerateContentResponseUsageMetadata(
        prompt_token_count=1520,
        candidates_token_count=180,
        total_token_count=1700,
        cached_content_token_count=1200,
        thoughts_token_count=85,
    )
    event_with_usage = Event(
        author="assistant",
        usage_metadata=mock_usage,
        content=Content(role="model", parts=[Part.from_text(text="Hello world")]),
    )
    chunks = serialize_adk_event(event_with_usage, context_limit=128000)
    usage_chunks = [c for c in chunks if c.get("type") == "usage"]
    assert len(usage_chunks) == 1, f"Expected 1 usage chunk, got {len(usage_chunks)}"
    u = usage_chunks[0]
    assert u["promptTokens"] == 1520
    assert u["totalTokens"] == 1700
    assert u["contextLimit"] == 128000
    print("✓ serialize_adk_event correctly emits usage chunks with full metadata")

    with TestClient(app) as client:
        r_create = client.post("/api/session", json={"agent": "assistant"})
        assert r_create.status_code == 200
        sid = r_create.json()["id"]

        try:
            r_no_agent = client.get(f"/api/session/{sid}/usage")
            assert r_no_agent.status_code == 400

            r_usage = client.get(f"/api/session/{sid}/usage?agent=assistant")
            assert r_usage.status_code == 200
            usage_data = r_usage.json()
            assert usage_data["sessionId"] == sid
            assert "promptTokens" in usage_data
            assert "contextLimit" in usage_data
            print("✓ /api/session/{id}/usage returns valid token metadata")
        finally:
            client.delete(f"/api/session/{sid}?agent=assistant")


def test_dynamic_instruction_timezone():
    print("\n--- [Test 11: Dynamic Instruction Timezone Injection] ---")
    from harness.loader import DynamicInstruction

    di = DynamicInstruction("Base instructions.", timezone_name="UTC")
    text = str(di)
    assert text.startswith("Current Date and Time (UTC):")
    assert text.endswith("Base instructions.")
    print("✓ DynamicInstruction injects a live UTC timestamp header")


if __name__ == "__main__":
    test_api_endpoints()
    test_mcp_discovery_and_config()
    test_agent_scoping()
    asyncio.run(test_minimal_agent_tools_and_scoping())
    test_basic_mcp_path_security()
    test_server_basic_auth()
    test_session_token_usage()
    test_dynamic_instruction_timezone()
    print("\n==========================================")
    print("  ALL END-TO-END VERIFICATION CHECKS PASSED")
    print("==========================================")
