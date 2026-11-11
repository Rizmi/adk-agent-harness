"""Workflow Skills MCP Server.

Provides procedural workflow instructions and skill discovery from skills/:
- Listing registered workflow skills.
- Loading skill instructions (SKILL.md) into execution context.
"""

import argparse
import os
from pathlib import Path
from typing import List

from mcp.server.mcpserver import MCPServer

mcp = MCPServer(
    name="skills",
    title="Workflow Skills MCP Server",
    description="Discovers and loads procedural workflow instructions from skills/.",
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


def _get_skills_dir() -> Path:
    return _resolve_config_path("ADK_SKILLS_DIR", "skills")


# -----------------------------------------------------------------------------
# Skills Tools
# -----------------------------------------------------------------------------
@mcp.tool(meta={"summary": "List available procedural skills defined in skills/"})
def skill_list() -> List[str]:
    """List available procedural skills defined in skills/.

    Returns folder names of all discovered skills.
    """
    skills_dir = _get_skills_dir()
    if not skills_dir.exists() or not skills_dir.is_dir():
        return []
    return [
        d.name
        for d in sorted(skills_dir.iterdir())
        if d.is_dir() and not d.name.startswith((".", "__")) and (d / "SKILL.md").exists()
    ]


@mcp.tool(meta={"summary": "Load step-by-step workflow instructions from skills/"})
def skill_load(skill_name: str) -> str:
    """Load a skill markdown file (SKILL.md) into context.

    Args:
        skill_name: The folder name or identifier of the skill in skills/.
    """
    skills_dir = _get_skills_dir()
    clean = skill_name.strip().lower()
    if not skills_dir.exists():
        return f"Skills directory not found: {skills_dir}"

    target_file = None
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        name_low = skill_dir.name.lower()
        if name_low == clean or name_low.endswith(f"-{clean}") or clean.endswith(f"-{name_low}"):
            sf = skill_dir / "SKILL.md"
            if sf.exists():
                target_file = sf
                break

    if not target_file:
        return f"Skill '{skill_name}' not found in {skills_dir}."

    try:
        return target_file.read_text(encoding="utf-8")
    except Exception as e:
        return f"Error reading skill '{skill_name}': {e}"


# -----------------------------------------------------------------------------
# Main Runner
# -----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Workflow Skills MCP Server")
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
        default=int(os.getenv("MCP_PORT_SKILLS", os.getenv("MCP_PORT", "8102"))),
        help="Port for SSE transport",
    )
    args = parser.parse_args()

    if args.transport == "sse":
        mcp.run("sse", host=args.host, port=args.port)
    else:
        mcp.run("stdio")


if __name__ == "__main__":
    main()
