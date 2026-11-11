"""MCP Connection and Lifecycle Manager (SSE & Stdio).

Dynamically discovers local MCP servers from ``mcp/*/server.py`` and external
MCP servers configured in ``mcp/servers.json``. Supports both HTTP/SSE and
subprocess stdio transports without hardcoded server names or ports.
"""

import asyncio
import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.types import Tool

from ..config import config

logger = logging.getLogger("harness.mcp")

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env_vars(value: str) -> str:
    """Expand ${VAR} references from os.environ."""
    def replace(match: re.Match) -> str:
        var = match.group(1)
        return os.environ.get(var, "")
    return _ENV_REF.sub(replace, value)


@dataclass
class McpServerEntry:
    """Configuration for a single local or external MCP server."""

    name: str
    transport: str  # "sse" | "stdio"
    url: Optional[str] = None
    port: Optional[int] = None
    command: Optional[str] = None
    args: List[str] = field(default_factory=list)
    env: Optional[Dict[str, str]] = None
    cwd: Optional[str] = None
    description: str = ""
    local_dir: Optional[Path] = None


class McpManager:
    """Manages SSE and stdio connections, tool inspection, and tool dispatch for MCP servers."""

    def __init__(self, host: Optional[str] = None, base_port: Optional[int] = None):
        self.host = host or os.environ.get("MCP_HOST", "127.0.0.1")
        self.base_port = base_port or int(os.environ.get("MCP_BASE_PORT", "8101"))
        self._servers: Dict[str, McpServerEntry] = {}
        self._tools: Dict[str, Dict[str, Tool]] = {}
        self._lock = asyncio.Lock()
        self.reload_servers()

    def reload_servers(self) -> Dict[str, McpServerEntry]:
        """Discover local mcp/*/server.py directories and merge mcp/servers.json."""
        servers: Dict[str, McpServerEntry] = {}
        mcp_dir = config.mcp_dir

        # 1. Auto-discover local MCP servers in mcp/*/server.py
        if mcp_dir.exists():
            idx = 0
            for srv_dir in sorted(mcp_dir.iterdir()):
                if not srv_dir.is_dir() or srv_dir.name.startswith((".", "__")):
                    continue
                srv_file = srv_dir / "server.py"
                if not srv_file.exists():
                    continue
                name = srv_dir.name.lower()
                port_env = os.environ.get(f"MCP_PORT_{name.upper()}", "").strip()
                port = int(port_env) if port_env.isdigit() else (self.base_port + idx)
                idx += 1
                servers[name] = McpServerEntry(
                    name=name,
                    transport="sse",
                    port=port,
                    url=f"http://{self.host}:{port}/sse",
                    local_dir=srv_dir,
                    description=f"Local MCP server ({name})",
                )

        # 2. Load external or overridden MCP servers from mcp/servers.json (if present)
        raw_servers_file = os.environ.get("ADK_MCP_SERVERS_FILE", "").strip()
        servers_file = Path(raw_servers_file).expanduser().resolve() if raw_servers_file else config.mcp_servers_file
        if servers_file.exists():
            try:
                raw = json.loads(servers_file.read_text(encoding="utf-8"))
                entries = raw.get("mcpServers") if isinstance(raw, dict) and "mcpServers" in raw else raw
                if isinstance(entries, dict):
                    for raw_name, cfg in entries.items():
                        if raw_name.startswith("_") or not isinstance(cfg, dict):
                            continue
                        if cfg.get("disabled") is True:
                            servers.pop(raw_name.lower(), None)
                            continue
                        name = raw_name.strip().lower()
                        transport = str(cfg.get("transport") or cfg.get("type") or "").strip().lower()
                        url = cfg.get("url")
                        command = cfg.get("command")

                        if not transport:
                            transport = "sse" if url else "stdio"

                        if transport == "sse":
                            port = cfg.get("port")
                            resolved_url = _expand_env_vars(str(url)) if url else f"http://{self.host}:{port}/sse"
                            servers[name] = McpServerEntry(
                                name=name,
                                transport="sse",
                                url=resolved_url,
                                port=int(port) if port else None,
                                description=str(cfg.get("description", "")),
                                local_dir=servers[name].local_dir if name in servers else None,
                            )
                        elif transport == "stdio" and command:
                            raw_args = cfg.get("args") or []
                            args = [_expand_env_vars(str(a)) for a in raw_args]
                            raw_env = cfg.get("env")
                            env_map = None
                            if isinstance(raw_env, dict):
                                env_map = {k: _expand_env_vars(str(v)) for k, v in raw_env.items()}
                            cwd = _expand_env_vars(str(cfg["cwd"])) if cfg.get("cwd") else None
                            servers[name] = McpServerEntry(
                                name=name,
                                transport="stdio",
                                command=_expand_env_vars(str(command)),
                                args=args,
                                env=env_map,
                                cwd=cwd,
                                description=str(cfg.get("description", "")),
                            )
            except Exception as exc:
                logger.warning("Failed to parse MCP servers file %s: %s", servers_file, exc)

        self._servers = servers
        return servers

    def list_available_servers(self) -> List[str]:
        """List distinct available server names."""
        self.reload_servers()
        return sorted(list(self._servers.keys()))

    def get_server_entry(self, name: str) -> McpServerEntry:
        """Return the resolved McpServerEntry for a server name."""
        clean = name.strip().lower()
        if clean.startswith("mcp:"):
            clean = clean[4:]
        if clean not in self._servers:
            self.reload_servers()
        if clean not in self._servers:
            raise ValueError(f"Unknown MCP server: '{name}'. Available: {self.list_available_servers()}")
        return self._servers[clean]

    def get_server_url(self, name: str) -> str:
        """Return the SSE endpoint URL for a given SSE server name."""
        entry = self.get_server_entry(name)
        if entry.transport != "sse" or not entry.url:
            raise ValueError(f"MCP server '{name}' does not use SSE transport.")
        return entry.url

    def is_connected(self, name: str) -> bool:
        """Check if an MCP server is reachable (SSE) or configured (stdio)."""
        try:
            entry = self.get_server_entry(name)
        except ValueError:
            return False

        if entry.transport == "stdio":
            return bool(entry.command)

        if not entry.url:
            return False

        req = urllib.request.Request(entry.url, headers={"Accept": "text/event-stream"})
        try:
            with urllib.request.urlopen(req, timeout=0.5) as resp:
                return resp.status in (200, 204)
        except urllib.error.HTTPError:
            return True
        except Exception:
            return False

    async def fetch_tools_for_server(self, name: str) -> Dict[str, Tool]:
        """Query an MCP server over SSE or stdio and cache its tools."""
        entry = self.get_server_entry(name)
        try:
            if entry.transport == "sse":
                if not entry.url:
                    raise RuntimeError(f"Missing URL for SSE MCP server '{name}'")
                async with sse_client(entry.url) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        result = await session.list_tools()
                        tools_map = {t.name: t for t in result.tools}
                        self._tools[entry.name] = tools_map
                        return tools_map
            else:
                if not entry.command:
                    raise RuntimeError(f"Missing command for stdio MCP server '{name}'")
                params = StdioServerParameters(
                    command=entry.command,
                    args=entry.args,
                    env=entry.env,
                    cwd=entry.cwd,
                )
                async with stdio_client(params) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        result = await session.list_tools()
                        tools_map = {t.name: t for t in result.tools}
                        self._tools[entry.name] = tools_map
                        return tools_map
        except Exception as e:
            raise RuntimeError(f"Failed to connect to MCP server '{name}': {e}") from e

    async def get_tool(self, server_name: str, tool_name: str) -> Optional[Tool]:
        """Retrieve schema/metadata for a specific tool on a server."""
        clean_server = server_name.strip().lower()
        if clean_server not in self._tools:
            await self.fetch_tools_for_server(clean_server)
        return self._tools.get(clean_server, {}).get(tool_name)

    async def load_all_tools(self) -> Dict[str, Dict[str, Tool]]:
        """Load tools from all configured MCP servers concurrently."""
        self.reload_servers()
        async with self._lock:
            names = list(self._servers.keys())
            tasks = [self.fetch_tools_for_server(name) for name in names]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for name, res in zip(names, results):
                if isinstance(res, Exception):
                    logger.warning("Failed to load tools for server '%s': %s", name, res)
            return self._tools

    async def _dispatch_session_call(self, session: ClientSession, tool_name: str, arguments: Dict[str, Any]) -> Any:
        await session.initialize()
        res = await session.call_tool(tool_name, arguments or {})

        if not res:
            return None

        texts: List[str] = []
        for c in getattr(res, "content", []):
            if hasattr(c, "text"):
                texts.append(c.text)

        if texts:
            combined = "\n".join(texts)
            try:
                return json.loads(combined)
            except Exception:
                return combined

        if getattr(res, "is_error", False):
            return {"error": str(res)}

        return str(res)

    async def call_tool(self, server_name: str, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Execute a tool call against an MCP server over SSE or stdio."""
        entry = self.get_server_entry(server_name)
        if entry.transport == "sse":
            if not entry.url:
                raise RuntimeError(f"Missing URL for SSE MCP server '{server_name}'")
            async with sse_client(entry.url) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    return await self._dispatch_session_call(session, tool_name, arguments)
        else:
            if not entry.command:
                raise RuntimeError(f"Missing command for stdio MCP server '{server_name}'")
            params = StdioServerParameters(
                command=entry.command,
                args=entry.args,
                env=entry.env,
                cwd=entry.cwd,
            )
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    return await self._dispatch_session_call(session, tool_name, arguments)

    async def close_all(self):
        """Clean up MCP manager resources."""
        self._tools.clear()


mcp_manager = McpManager()
