"""Agent Specification Loader.

Discovers and imports standalone agent specifications from the agents/ directory
and builds fully scoped ADK LlmAgent instances with minimal prompt overhead.
Only exposes tool_describe and tool_run to the model.
"""

import ast
import importlib.util
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from google.adk.agents import LlmAgent
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.tool_context import ToolContext

from .config import config
from .mcp.manager import mcp_manager
from .permissions import approval_registry
from .providers.model import create_llm
from .schema import AgentDefinition


def _extract_mcp_tools(mcp_dir: Path) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Parse local MCP servers via AST to discover all available tools and docstrings without importing."""
    tools: Dict[str, Dict[str, Dict[str, Any]]] = {}
    if not mcp_dir.exists():
        return tools

    for srv_dir in sorted(mcp_dir.iterdir()):
        if not srv_dir.is_dir() or srv_dir.name.startswith((".", "__")):
            continue
        srv_file = srv_dir / "server.py"
        if not srv_file.exists():
            continue

        try:
            tree = ast.parse(srv_file.read_text(encoding="utf-8"))
        except Exception:
            continue

        srv_tools: Dict[str, Dict[str, Any]] = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                is_tool = False
                summary = None
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call):
                        func = dec.func
                        if (isinstance(func, ast.Attribute) and func.attr == "tool") or (
                            isinstance(func, ast.Name) and func.id == "tool"
                        ):
                            is_tool = True
                            for kw in dec.keywords:
                                if kw.arg == "meta" and isinstance(kw.value, ast.Dict):
                                    for k, v in zip(kw.value.keys, kw.value.values):
                                        if (
                                            isinstance(k, ast.Constant)
                                            and k.value == "summary"
                                            and isinstance(v, ast.Constant)
                                        ):
                                            summary = str(v.value)
                    elif isinstance(dec, ast.Attribute) and dec.attr == "tool":
                        is_tool = True

                if is_tool:
                    doc = ast.get_docstring(node) or ""
                    arg_names = [a.arg for a in node.args.args if a.arg not in ("self", "cls")]
                    srv_tools[node.name] = {
                        "summary": summary,
                        "doc": doc,
                        "args": arg_names,
                    }

        tools[srv_dir.name.lower()] = srv_tools

    return tools


async def _discover_all_mcp_tools(mcp_dir: Path, required_servers: Set[str]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Combine local AST-extracted MCP tools with live-inspected external MCP servers."""
    tools = _extract_mcp_tools(mcp_dir)
    mcp_manager.reload_servers()

    for srv_name in required_servers:
        if srv_name in tools:
            continue
        if srv_name in mcp_manager.list_available_servers():
            live_tools = await mcp_manager.fetch_tools_for_server(srv_name)
            srv_map: Dict[str, Dict[str, Any]] = {}
            for t_name, tool_obj in live_tools.items():
                meta = getattr(tool_obj, "meta", None) or {}
                doc = (tool_obj.description or "").strip()
                first_line = doc.splitlines()[0].strip() if doc else ""
                summary = meta.get("summary") or first_line or f"Tool {t_name} on {srv_name}"
                props = (tool_obj.inputSchema or {}).get("properties") or {}
                srv_map[t_name] = {
                    "summary": summary,
                    "doc": doc,
                    "args": list(props.keys()),
                }
            tools[srv_name] = srv_map

    return tools


LEGACY_TOOL_ALIASES: Dict[str, str] = {
    # Filesystem tools
    "basic.file_read": "filesystem.file_read",
    "basic.file_write": "filesystem.file_write",
    "basic.file_edit": "filesystem.file_edit",
    "basic.file_delete": "filesystem.file_delete",
    "basic.file_list": "filesystem.file_list",
    "basic.file_glob": "filesystem.file_glob",
    # Skills tools
    "basic.skill_list": "skills.skill_list",
    "basic.skill_load": "skills.skill_load",
    # Memory tools
    "basic.memory_read": "memory.memory_read",
    "basic.memory_write": "memory.memory_write",
    "basic.memory_edit": "memory.memory_edit",
}


def normalize_tool_name(tool_name: str) -> str:
    """Normalize legacy or alias tool identifiers to canonical server.tool names."""
    clean = tool_name.strip()
    return LEGACY_TOOL_ALIASES.get(clean, clean)


def _extract_skills(skills_dir: Path) -> Dict[str, str]:
    """Discover available skills and extract summaries from YAML frontmatter."""
    skills: Dict[str, str] = {}
    if not skills_dir.exists():
        return skills

    for sk_dir in sorted(skills_dir.iterdir()):
        if not sk_dir.is_dir() or sk_dir.name.startswith((".", "__")):
            continue
        sf = sk_dir / "SKILL.md"
        if sf.exists():
            try:
                for line in sf.read_text(encoding="utf-8").splitlines():
                    if line.startswith("description:"):
                        skills[sk_dir.name] = line.split(":", 1)[1].strip()
                        break
                if sk_dir.name not in skills:
                    skills[sk_dir.name] = "Operational workflow procedure"
            except Exception:
                continue

    return skills


class DynamicInstruction:
    """Dynamic instruction provider that injects live current date and time on every turn.

    Accepts ctx from ADK canonical_instruction and evaluates fresh.
    Also implements __contains__, __len__, __iter__, __str__ for transparent testing & logging.
    """

    def __init__(self, base_instructions: str, timezone_name: str = "UTC"):
        self.base_instructions = base_instructions
        self.timezone_name = timezone_name

    def __call__(self, ctx: Any = None) -> str:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        tz_label = self.timezone_name or "UTC"
        try:
            tz = ZoneInfo(tz_label)
        except Exception:
            tz_label = "UTC"
            tz = ZoneInfo("UTC")

        now = datetime.now(tz)
        offset = now.strftime("%z")
        formatted_offset = f"UTC{offset[:3]}:{offset[3:]}" if len(offset) == 5 else offset
        time_str = now.strftime("%A, %d %B %Y, %I:%M %p") + f" ({formatted_offset})"
        header = f"Current Date and Time ({tz_label}): {time_str}\n\n"
        return header + self.base_instructions

    def __str__(self) -> str:
        return self(None)

    def __contains__(self, item: Any) -> bool:
        return item in str(self)

    def __len__(self) -> int:
        return len(str(self))

    def __iter__(self):
        return iter(str(self))


class AgentCatalog:
    """Discovers, loads, and instantiates pluggable agents."""

    def __init__(self, agents_dir: Optional[Path] = None):
        self.agents_dir = agents_dir or config.agents_dir
        self._definitions: Dict[str, AgentDefinition] = {}

    def discover_and_load_all(self) -> Dict[str, AgentDefinition]:
        """Scan agents directory and load all agent definition files."""
        self._definitions.clear()
        if not self.agents_dir.exists():
            return self._definitions

        for py_file in sorted(self.agents_dir.glob("*.py")):
            if py_file.name.startswith("__"):
                continue
            try:
                agent_def = self._load_file(py_file)
                if agent_def:
                    self._definitions[agent_def.name] = agent_def
            except Exception as e:
                print(f"[AgentCatalog] Error loading agent from {py_file}: {e}")

        return self._definitions

    def _load_file(self, file_path: Path) -> Optional[AgentDefinition]:
        """Dynamically import a Python file and extract AgentDefinition."""
        spec = importlib.util.spec_from_file_location(f"agent_module_{file_path.stem}", file_path)
        if not spec or not spec.loader:
            return None

        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        if hasattr(mod, "agent") and isinstance(mod.agent, AgentDefinition):
            mod.agent.validate()
            return mod.agent

        name = getattr(mod, "NAME", file_path.stem)
        desc = getattr(mod, "DESCRIPTION", f"{name} agent")
        instructions = getattr(mod, "INSTRUCTIONS", None)
        if not instructions:
            inst_file = getattr(mod, "INSTRUCTIONS_FILE", None)
            if inst_file and Path(inst_file).exists():
                instructions = Path(inst_file).read_text(encoding="utf-8").strip()

        if instructions:
            agent_def = AgentDefinition(
                name=name,
                description=desc,
                instructions=instructions,
                model=getattr(mod, "MODEL", None),
                skills=getattr(mod, "SKILLS", []),
                permissions=getattr(mod, "PERMISSIONS", {"allow": [], "ask": []}),
            )
            agent_def.validate()
            return agent_def

        return None

    def get_definition(self, name: str) -> Optional[AgentDefinition]:
        """Get an agent definition by name."""
        if not self._definitions:
            self.discover_and_load_all()
        return self._definitions.get(name)

    def list_agent_names(self) -> List[str]:
        """List all loaded agent names."""
        if not self._definitions:
            self.discover_and_load_all()
        return list(self._definitions.keys())

    async def build_agent(self, name: str, model_override: Optional[str] = None) -> LlmAgent:
        """Construct an instantiated LlmAgent according to its AgentDefinition.

        Validates all permissions and skills strictly with zero fallbacks.
        Generates dynamic prompt documentation for allowed tools and skills.
        Registers only tool_describe and tool_run to eliminate token bloat.
        """
        agent_def = self.get_definition(name)
        if not agent_def:
            available = self.list_agent_names()
            raise ValueError(f"Agent '{name}' not found. Available agents: {available}")

        chosen_model = (model_override or "").strip() or agent_def.model
        model = create_llm(config.spec_for(chosen_model))

        allow_list = [normalize_tool_name(tk) for tk in (agent_def.permissions.get("allow") or [])]
        ask_list = [normalize_tool_name(tk) for tk in (agent_def.permissions.get("ask") or [])]

        allowed_tools_set: Set[str] = set(allow_list)
        ask_tools_set: Set[str] = set(ask_list)
        all_requested_tools = allow_list + ask_list

        required_servers = {
            tk.split(".", 1)[0].strip().lower()
            for tk in all_requested_tools
            if "." in tk
        }

        # 1. Discover all tools (local AST + external MCP servers) and skills
        all_mcp_tools = await _discover_all_mcp_tools(config.mcp_dir, required_servers)
        all_skills = _extract_skills(config.skills_dir)

        # 2. Strict Zero-Fallback Validation
        tool_summaries: List[str] = []
        for tool_key in all_requested_tools:
            if "." not in tool_key:
                raise ValueError(
                    f"Agent '{agent_def.name}' has invalid tool permission '{tool_key}'. "
                    f"Must be formatted as 'server.tool_name'."
                )
            server_name, bare_tool = tool_key.split(".", 1)
            server_name = server_name.lower()
            if server_name not in all_mcp_tools:
                available_srvs = sorted(set(list(all_mcp_tools.keys()) + mcp_manager.list_available_servers()))
                raise ValueError(
                    f"Agent '{agent_def.name}' references unknown MCP server '{server_name}' "
                    f"in permission '{tool_key}'. Available servers: {available_srvs}"
                )
            if bare_tool not in all_mcp_tools[server_name]:
                available = sorted(list(all_mcp_tools[server_name].keys()))
                raise ValueError(
                    f"Agent '{agent_def.name}' references unknown tool '{bare_tool}' on MCP server "
                    f"'{server_name}' in permission '{tool_key}'. Available tools: {available}"
                )
            tool_meta = all_mcp_tools[server_name][bare_tool]
            summary = tool_meta.get("summary")
            if not summary or not summary.strip():
                raise ValueError(
                    f"Agent '{agent_def.name}' references tool '{tool_key}', but it has no meta summary description. "
                    f"@mcp.tool(meta={{'summary': '...'}}) is mandatory for all local tools in agent permissions."
                )
            tier_marker = " (requires confirmation)" if tool_key in ask_tools_set else ""
            tool_summaries.append(f"- `{tool_key}`: {summary.strip()}{tier_marker}")

        skill_summaries: List[str] = []
        for sk in agent_def.skills:
            if sk not in all_skills:
                raise ValueError(
                    f"Agent '{agent_def.name}' references unknown skill '{sk}'. "
                    f"File 'skills/{sk}/SKILL.md' does not exist."
                )
            skill_summaries.append(f"- `{sk}`: {all_skills[sk]}")

        # 3. Dynamic Prompt Injection
        instruction_parts = [agent_def.instructions]

        if tool_summaries:
            instruction_parts.append(
                "\n\n## Available Tools\n" + "\n".join(tool_summaries)
            )

        if skill_summaries:
            instruction_parts.append(
                "\n\n## Available Skills\n" + "\n".join(skill_summaries)
            )

        composed_instructions = "".join(instruction_parts).strip()

        # 4. Builtin Minimal Tools: tool_describe and tool_run
        all_permitted = allowed_tools_set | ask_tools_set

        async def tool_describe(tool_name: str) -> dict:
            """Inspect schema, parameters, and documentation for an allowed tool.

            Args:
                tool_name: Qualified tool identifier in 'server.tool' format (e.g. 'filesystem.file_read').
            """
            clean = normalize_tool_name(tool_name)
            if clean not in all_permitted:
                return {"error": f"Tool '{tool_name}' is not permitted for agent '{agent_def.name}'."}

            server, bare_name = clean.split(".", 1)
            server = server.lower()
            # Try live inspection over MCP if available
            try:
                if mcp_manager.is_connected(server):
                    live_tool = await mcp_manager.get_tool(server, bare_name)
                    if live_tool:
                        return {
                            "name": clean,
                            "description": live_tool.description,
                            "parameters": live_tool.inputSchema,
                        }
            except Exception:
                pass

            ast_tool = all_mcp_tools.get(server, {}).get(bare_name, {})
            return {
                "name": clean,
                "description": ast_tool.get("doc") or ast_tool.get("summary", ""),
                "parameters": {
                    "type": "object",
                    "properties": {a: {"type": "string"} for a in ast_tool.get("args", [])},
                },
            }

        async def tool_run(
            tool_name: str,
            arguments: Optional[Dict[str, Any]] = None,
            tool_context: Optional[ToolContext] = None,
        ) -> Any:
            """Execute an allowed tool with input arguments.

            Args:
                tool_name: Qualified tool identifier in 'server.tool' format (e.g. 'filesystem.file_read').
                arguments: Dictionary of arguments to pass to the tool.
            """
            clean = normalize_tool_name(tool_name)
            args = arguments or {}

            # A. Access Check
            if clean not in all_permitted:
                return {"error": f"Tool '{tool_name}' is not allowed for agent '{agent_def.name}'. Default deny."}

            # B. Ask Tier Check
            if clean in ask_tools_set:
                confirmation = (
                    getattr(tool_context, "tool_confirmation", None) if tool_context else None
                )
                if confirmation is not None:
                    if not getattr(confirmation, "confirmed", False):
                        return {"error": f"Action '{clean}' was rejected by operator."}
                    payload = getattr(confirmation, "payload", None) or {}
                    req_id = payload.get("request_id")
                    if not req_id or not approval_registry.consume(
                        req_id, tool_name=clean, args=args, agent_name=agent_def.name
                    ):
                        return {
                            "error": f"Action '{clean}' approval token is invalid, expired, or parameters mismatched."
                        }
                else:
                    session = tool_context.session if tool_context else None
                    session_id = session.id if session else "ses_unknown"
                    user_id = session.user_id if session else "operator"
                    hint = f"Action '{clean}' requires human confirmation."
                    record = approval_registry.create_request(
                        session_id=session_id,
                        user_id=user_id,
                        agent_name=agent_def.name,
                        tool_name=clean,
                        args=args,
                        hint=hint,
                        model_override=chosen_model,
                    )
                    if tool_context:
                        tool_context.request_confirmation(
                            hint=hint,
                            payload={"request_id": record.request_id, "tool": clean, "args": args},
                        )
                    return {
                        "status": "confirmation_required",
                        "request_id": record.request_id,
                        "tool": clean,
                        "message": hint,
                    }

            # C. Execution Dispatch over SSE / Stdio
            server, bare_name = clean.split(".", 1)
            try:
                return await mcp_manager.call_tool(server.lower(), bare_name, args)
            except Exception as e:
                return {"error": f"Execution failed for '{clean}': {e}"}

        ft_desc = FunctionTool(tool_describe)
        ft_run = FunctionTool(tool_run)

        dynamic_instruction = DynamicInstruction(
            base_instructions=composed_instructions,
            timezone_name=config.timezone,
        )

        return LlmAgent(
            name=agent_def.name,
            description=agent_def.description,
            model=model,
            instruction=dynamic_instruction,
            tools=[ft_desc, ft_run],
        )


agent_catalog = AgentCatalog()
