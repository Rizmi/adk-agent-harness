"""Persistent Memory MCP Server.

Provides persistent, self-updating memory notes for ADK agents:
- Reading, writing, and surgical editing of MEMORY.md.
"""

import argparse
import os
import tempfile
from pathlib import Path
from typing import Optional

from mcp.server.mcpserver import MCPServer

mcp = MCPServer(
    name="memory",
    title="Persistent Memory MCP Server",
    description="Persistent agent memory storage, atomic overwrite, and surgical editing (MEMORY.md).",
    version="1.0.0",
)

ROOT_DIR = Path(__file__).resolve().parent.parent.parent


def _load_dotenv_if_needed() -> None:
    """Load root .env if variables are not already populated in os.environ."""
    env_path_raw = os.environ.get("ADK_ENV_FILE", "").strip() or os.environ.get("ENV_FILE", "").strip()
    env_path = Path(env_path_raw).expanduser().resolve() if env_path_raw else ROOT_DIR / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path, override=False)
    except ImportError:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k not in os.environ:
                        os.environ[k] = v


_load_dotenv_if_needed()


def _resolve_config_path(env_key: str, default_rel: str) -> Path:
    raw = os.environ.get(env_key, "").strip()
    if not raw:
        return (ROOT_DIR / default_rel).resolve()
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = ROOT_DIR / p
    return p.resolve()


def _get_memory_file() -> Path:
    return _resolve_config_path("ADK_MEMORY_FILE", "MEMORY.md")


def _guard_memory(path: Path) -> Optional[str]:
    """Reject a memory path that is a symlink or resolves somewhere unexpected."""
    if path.is_symlink():
        return f"Access denied: memory file '{path}' is a symlink. Refusing to follow it."
    try:
        resolved = path.resolve()
    except OSError as exc:
        return f"Error: memory path could not be resolved: {exc}"

    expected = _get_memory_file().resolve()
    if resolved != expected:
        return f"Access denied: memory file resolves to '{resolved}', expected '{expected}'."
    return None


# -----------------------------------------------------------------------------
# Memory Tools
# -----------------------------------------------------------------------------
@mcp.tool(meta={"summary": "Read persistent agent memory (MEMORY.md)"})
def memory_read() -> str:
    """Read the agent's persistent memory file (MEMORY.md).

    Returns the entire file as text. Read before editing so changes are based on current content.
    """
    path = _get_memory_file()
    denied = _guard_memory(path)
    if denied:
        return denied
    if not path.exists():
        return f"Memory file does not exist yet: {path}"
    if not path.is_file():
        return f"Memory path is not a regular file: {path}"

    try:
        return path.read_text(encoding="utf-8")
    except Exception as exc:
        return f"Error reading memory file: {exc}"


@mcp.tool(meta={"summary": "Overwrite persistent agent memory (MEMORY.md) atomically"})
def memory_write(content: str) -> str:
    """Overwrite the agent's memory file with new content.

    Use this to restructure or compact memory. Writes are atomic.

    Args:
        content: The full new text of the memory file.
    """
    path = _get_memory_file()
    denied = _guard_memory(path)
    if denied:
        return denied

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".memory.", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp_name, path)
        except Exception:
            Path(temp_name).unlink(missing_ok=True)
            raise
        return f"Memory file written ({len(content)} bytes)."
    except Exception as exc:
        return f"Error writing memory file: {exc}"


@mcp.tool(meta={"summary": "Replace an exact text snippet in persistent memory (MEMORY.md)"})
def memory_edit(old_string: str, new_string: str) -> str:
    """Replace an exact snippet in the agent's memory file.

    Args:
        old_string: Exact text to find. Must occur exactly once.
        new_string: Replacement text.
    """
    path = _get_memory_file()
    denied = _guard_memory(path)
    if denied:
        return denied
    if not path.exists():
        return f"Memory file does not exist yet: {path}"

    try:
        content = path.read_text(encoding="utf-8")
        if old_string not in content:
            return "Error: old_string not found in memory file."
        count = content.count(old_string)
        if count > 1:
            return (
                f"Error: old_string matches {count} occurrences in memory. "
                f"Provide more surrounding context so the match is unique."
            )
        updated = content.replace(old_string, new_string, 1)
        path.write_text(updated, encoding="utf-8")
        return f"Memory file updated successfully (replaced 1 occurrence)."
    except Exception as exc:
        return f"Error updating memory file: {exc}"


# -----------------------------------------------------------------------------
# Main Runner
# -----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Persistent Memory MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default=os.getenv("MCP_TRANSPORT", "stdio"),
        help="Transport type (stdio or sse)",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("MCP_HOST", "127.0.0.1"),
        help="Host for SSE transport",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("MCP_PORT_MEMORY", os.getenv("MCP_PORT", "8103"))),
        help="Port for SSE transport",
    )
    args = parser.parse_args()

    if args.transport == "sse":
        mcp.run("sse", host=args.host, port=args.port)
    else:
        mcp.run("stdio")


if __name__ == "__main__":
    main()
