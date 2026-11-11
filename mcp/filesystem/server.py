"""Filesystem Operations MCP Server.

Provides sandboxed, safe filesystem access for ADK agents:
- Reading, writing, editing, deleting, globbing, and directory listing.
"""

import argparse
import os
from pathlib import Path
from typing import List

from mcp.server.mcpserver import MCPServer

mcp = MCPServer(
    name="filesystem",
    title="Filesystem Operations MCP Server",
    description="Sandboxed filesystem tools for safe reading, writing, editing, globbing, and listing workspace files.",
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


def _parse_dir_list(env_key: str, default_paths: List[Path]) -> List[Path]:
    raw = os.environ.get(env_key, "").strip()
    if not raw:
        return [p.resolve() for p in default_paths]
    dirs: List[Path] = []
    for part in raw.split(","):
        clean = part.strip()
        if not clean:
            continue
        p = Path(clean).expanduser()
        if not p.is_absolute():
            p = ROOT_DIR / p
        dirs.append(p.resolve())
    return dirs


def _get_authorized_dirs() -> tuple[List[Path], List[Path]]:
    workspace_dir = _resolve_config_path("ADK_WORKSPACE_DIR", "workspace")
    artifacts_dir = _resolve_config_path("ADK_ARTIFACTS_DIR", "artifacts")
    tmp_dir = _resolve_config_path("ADK_TMP_DIR", "tmp")

    read_dirs = _parse_dir_list(
        "ADK_ALLOWED_READ_DIRS",
        [workspace_dir, artifacts_dir, tmp_dir],
    )
    write_dirs = _parse_dir_list(
        "ADK_ALLOWED_WRITE_DIRS",
        [workspace_dir, artifacts_dir, tmp_dir],
    )
    return read_dirs, write_dirs


def _resolve_path(file_path: str) -> Path:
    p = Path(file_path).expanduser()
    if not p.is_absolute():
        p = ROOT_DIR / p
    return p.resolve()


def _is_allowed(path: Path, allowed_roots: List[Path]) -> bool:
    try:
        resolved = path.resolve()
        for root in allowed_roots:
            try:
                resolved.relative_to(root.resolve())
                return True
            except ValueError:
                continue
    except Exception:
        pass
    return False


# -----------------------------------------------------------------------------
# Filesystem Tools
# -----------------------------------------------------------------------------
@mcp.tool(meta={"summary": "Read lines from a file inside an authorized workspace directory"})
def file_read(file_path: str, max_lines: int = 200, start_line: int = 1) -> str:
    """Read lines from an authorized file safely.

    Args:
        file_path: Relative (to project root) or absolute path to the file.
        max_lines: Maximum number of lines to read (capped at 500).
        start_line: 1-indexed line number to start reading from.
    """
    read_dirs, _ = _get_authorized_dirs()
    path = _resolve_path(file_path)
    if not _is_allowed(path, read_dirs):
        return f"Access denied: '{file_path}' is outside authorized read directories."
    if not path.exists():
        return f"File not found: {file_path}"
    if not path.is_file():
        return f"Path is not a regular file: {file_path}"

    max_lines = min(max_lines, 500)
    lines = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for idx, line in enumerate(f, start=1):
                if idx < start_line:
                    continue
                if idx >= start_line + max_lines:
                    break
                lines.append(f"{idx}: {line}")
        return "".join(lines) if lines else "(Empty file or beyond line count)"
    except Exception as e:
        return f"Error reading file: {e}"


@mcp.tool(meta={"summary": "Find files matching a glob pattern inside an authorized directory"})
def file_glob(pattern: str, base_dir: str = "workspace") -> List[str]:
    """Find files matching a glob pattern inside an authorized directory.

    Args:
        pattern: Standard glob pattern (e.g. '*.json', '**/*.md').
        base_dir: Directory to search, relative to the project root or absolute.
    """
    read_dirs, _ = _get_authorized_dirs()
    target = _resolve_path(base_dir)
    if not _is_allowed(target, read_dirs):
        return [f"Access denied: '{base_dir}' is outside authorized read directories."]
    if not target.exists() or not target.is_dir():
        return []

    matches = []
    for p in target.glob(pattern):
        if any(ignored in p.parts for ignored in (".git", ".venv", "node_modules", "__pycache__")):
            continue
        try:
            rel = p.relative_to(ROOT_DIR)
            matches.append(str(rel))
        except ValueError:
            matches.append(str(p))
        if len(matches) >= 100:
            break
    return matches


@mcp.tool(meta={"summary": "List files and folders inside an authorized directory"})
def file_list(directory: str = "workspace") -> List[str]:
    """List files and folders inside an authorized directory.

    Args:
        directory: Directory path, relative to the project root or absolute.
    """
    read_dirs, _ = _get_authorized_dirs()
    target = _resolve_path(directory)
    if not _is_allowed(target, read_dirs):
        return [f"Access denied: '{directory}' is outside authorized read directories."]
    if not target.exists() or not target.is_dir():
        return [f"Directory not found: {directory}"]

    results = []
    for item in sorted(target.iterdir()):
        if any(ignored in item.parts for ignored in (".git", ".venv", "node_modules", "__pycache__")):
            continue
        try:
            rel = item.relative_to(ROOT_DIR).as_posix()
            results.append(f"{rel}/" if item.is_dir() else rel)
        except ValueError:
            results.append(f"{item.as_posix()}/" if item.is_dir() else item.as_posix())
        if len(results) >= 100:
            break
    return results


@mcp.tool(meta={"summary": "Write text content to a file inside an authorized write directory"})
def file_write(file_path: str, content: str, overwrite: bool = False) -> str:
    """Write text content to a file in an authorized directory (workspace, tmp, or artifacts).

    Args:
        file_path: Relative or absolute path to the file.
        content: Text content to write into the file.
        overwrite: Set to True to overwrite existing file content.
    """
    _, write_dirs = _get_authorized_dirs()
    path = _resolve_path(file_path)
    if not _is_allowed(path, write_dirs):
        return f"Access denied: '{file_path}' is outside authorized write directories."
    if path.exists() and not overwrite:
        return f"Error: File '{file_path}' already exists. Pass overwrite=True to replace it."

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"File '{file_path}' written successfully ({len(content)} bytes)."
    except Exception as e:
        return f"Error writing file: {e}"


@mcp.tool(meta={"summary": "Replace an exact text snippet inside an authorized file"})
def file_edit(file_path: str, old_string: str, new_string: str) -> str:
    """Replace occurrences of old_string with new_string in an authorized file.

    Args:
        file_path: Relative or absolute path to the file.
        old_string: Exact text snippet to find and replace.
        new_string: Exact replacement text.
    """
    _, write_dirs = _get_authorized_dirs()
    path = _resolve_path(file_path)
    if not _is_allowed(path, write_dirs):
        return f"Access denied: '{file_path}' is outside authorized write directories."
    if not path.exists():
        return f"File not found: {file_path}"
    if not path.is_file():
        return f"Path is not a regular file: {file_path}"

    try:
        content = path.read_text(encoding="utf-8")
        if old_string not in content:
            return f"Error: old_string not found in '{file_path}'."
        count = content.count(old_string)
        updated = content.replace(old_string, new_string)
        path.write_text(updated, encoding="utf-8")
        return f"File '{file_path}' edited successfully ({count} replacement(s))."
    except Exception as e:
        return f"Error editing file: {e}"


@mcp.tool(meta={"summary": "Delete a file inside an authorized write directory"})
def file_delete(file_path: str) -> str:
    """Delete a regular file inside an authorized write directory.

    Args:
        file_path: Relative or absolute path to the file to remove.
    """
    _, write_dirs = _get_authorized_dirs()
    raw = Path(file_path).expanduser()
    if not raw.is_absolute():
        raw = ROOT_DIR / raw
    if raw.is_symlink():
        return f"Access denied: refusing to delete symlink '{file_path}'."

    path = raw.resolve()
    if not _is_allowed(path, write_dirs):
        return f"Access denied: '{file_path}' is outside authorized write directories."
    if not path.exists():
        return f"File not found: {file_path}"
    if not path.is_file():
        return f"Error: '{file_path}' is a directory or special file; only regular files can be deleted."

    try:
        path.unlink()
        return f"File '{file_path}' deleted successfully."
    except Exception as e:
        return f"Error deleting file: {e}"


# -----------------------------------------------------------------------------
# Main Runner
# -----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Filesystem Operations MCP Server")
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
        default=int(os.getenv("MCP_PORT_FILESYSTEM", os.getenv("MCP_PORT", "8101"))),
        help="Port for SSE transport",
    )
    args = parser.parse_args()

    if args.transport == "sse":
        mcp.run("sse", host=args.host, port=args.port)
    else:
        mcp.run("stdio")


if __name__ == "__main__":
    main()
