"""Zero-Trust Permission Guardrail.

Two independent layers, both default-deny:

1. Tool layer - which tools an agent may call at all.
   deny  : explicit hard block (wins over everything)
   ask   : requires human approval through the confirmations API
   allow : may run immediately
   Anything not matched by allow or ask is DENIED. There is no implicit allow,
   so no agent ever needs to write a "*" deny entry.

2. Path layer - which filesystem paths a builtin tool may touch.
   Applies ONLY to builtin filesystem tools, which the loader tags with
   ``_mcp_server_name = "filesystem"``. MCP tools and skills are never
   inspected or restricted here; they enforce their own boundaries.
   Per-tool rules:
   deny  : explicit hard block for a path
   ask   : requires human approval
   allow : permitted path patterns
   A tool with no rule for that path is DENIED.

Policies are entirely data-driven from the agent definition. This module holds
no project-specific paths, tool names, or client knowledge.
"""

import fnmatch
import logging
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Dict, List, Optional
import uuid

from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from .config import config

logger = logging.getLogger("harness.permissions")

# Tool arguments that may carry a filesystem target.
_PATH_KEYS = (
    "file_path",
    "path",
    "filepath",
    "target_path",
    "base_dir",
    "directory",
    "dir_path",
    "folder",
    "filename",
)

APPROVAL_TTL_SECONDS = 300.0

# Server names used for the harness's own tools (see loader.py).
BUILTIN_SERVERS = ("filesystem", "skills", "memory")


def normalize_tool_alias(tool_name: str) -> str:
    tk = tool_name.strip()
    if tk.startswith("basic."):
        suffix = tk.split(".", 1)[1]
        if suffix.startswith("file_"):
            return f"filesystem.{suffix}"
        if suffix.startswith("skill_"):
            return f"skills.{suffix}"
        if suffix.startswith("memory_"):
            return f"memory.{suffix}"
    return tk


class ApprovalRegistry:
    """Holds pending human-approval requests, keyed by request id."""

    def __init__(self) -> None:
        self._pending: Dict[str, "PendingApproval"] = {}

    def create_request(
        self,
        session_id: str,
        user_id: str,
        agent_name: str,
        tool_name: str,
        args: Dict[str, Any],
        hint: str,
        model_override: Optional[str] = None,
    ) -> "PendingApproval":
        self._cleanup_expired()
        record = PendingApproval(
            request_id=f"appr_{uuid.uuid4().hex[:12]}",
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            tool_name=tool_name,
            args=args,
            hint=hint,
            created_at=time.time(),
            model_override=model_override,
        )
        self._pending[record.request_id] = record
        return record

    def get_pending(self, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        self._cleanup_expired()
        res = []
        for req in self._pending.values():
            if req.decision is not None or req.consumed or req.is_expired():
                continue
            if session_id and req.session_id != session_id:
                continue
            resources = []
            if isinstance(req.args, dict):
                resources = [f"{k}: {v}" for k, v in req.args.items() if k != "_approval_token"]
            res.append({
                "requestId": req.request_id,
                "sessionId": req.session_id,
                "agentName": req.agent_name,
                "tool": req.tool_name,
                "action": req.tool_name,
                "hint": req.hint,
                "args": req.args,
                "resources": resources,
                "createdAt": req.created_at,
            })
        return res

    def resolve_request(self, request_id: str, decision: str) -> Optional["PendingApproval"]:
        self._cleanup_expired()
        record = self._pending.get(request_id)
        if record is None or record.is_expired() or record.decision is not None:
            return None
        record.decision = decision
        return record

    def consume(
        self,
        request_id: str,
        tool_name: Optional[str] = None,
        args: Optional[Dict[str, Any]] = None,
        agent_name: Optional[str] = None,
    ) -> bool:
        """Mark an approved request as used. A token is valid exactly once and must match tool/agent."""
        record = self._pending.get(request_id)
        if record is None or record.decision != "approved" or record.consumed or record.is_expired():
            return False
        if tool_name is not None and normalize_tool_alias(record.tool_name) != normalize_tool_alias(tool_name):
            logger.warning("Approval request %s tool mismatch: %s != %s", request_id, record.tool_name, tool_name)
            return False
        if agent_name is not None and record.agent_name != agent_name:
            logger.warning("Approval request %s agent mismatch: %s != %s", request_id, record.agent_name, agent_name)
            return False
        if args is not None and record.args != args:
            logger.warning("Approval request %s args mismatch", request_id)
            return False
        record.consumed = True
        return True

    def _cleanup_expired(self) -> None:
        now = time.time()
        for key in [k for k, v in self._pending.items() if v.is_expired() or v.consumed]:
            self._pending.pop(key, None)


@dataclass
class PendingApproval:
    """A tool invocation paused for human confirmation."""

    request_id: str
    session_id: str
    user_id: str
    agent_name: str
    tool_name: str
    args: Dict[str, Any]
    hint: str
    created_at: float
    decision: Optional[str] = None
    consumed: bool = False
    model_override: Optional[str] = None

    def is_expired(self, ttl_seconds: float = APPROVAL_TTL_SECONDS) -> bool:
        return (time.time() - self.created_at) > ttl_seconds


approval_registry = ApprovalRegistry()


# -----------------------------------------------------------------------------
# Tool identity
# -----------------------------------------------------------------------------
def resolve_tool_identity(tool: BaseTool) -> tuple:
    """Return (bare_name, server_name, qualified_name) for any tool.

    server_name is resolved from, in order:
      1. the ``_mcp_server_name`` tag the loader sets on builtin tools,
      2. the module of the wrapped function, for untagged builtin tools,
      3. the MCP session's working directory for stdio MCP tools.
    Identity comes from provenance, never from tool-name guessing, so an MCP
    tool can never be mistaken for a builtin filesystem tool.
    """
    bare_name = (getattr(tool, "name", None) or str(tool)).lower()
    server_name = getattr(tool, "_mcp_server_name", None)

    if not server_name:
        wrapped = getattr(tool, "func", None)
        module = getattr(wrapped, "__module__", "") or ""
        if "filesystem" in module:
            server_name = "filesystem"
        elif "skills" in module:
            server_name = "skills"
        elif "memory" in module:
            server_name = "memory"

    if not server_name:
        session_manager = getattr(tool, "_mcp_session_manager", None)
        params = getattr(session_manager, "_connection_params", None)
        cwd = getattr(getattr(params, "server_params", None), "cwd", None)
        if cwd:
            server_name = Path(cwd).name.lower()

    server_name = (server_name or "tool").lower()
    return bare_name, server_name, f"{server_name}.{bare_name}"


def _matches_tool(bare_name: str, qualified_name: str, patterns: List[str]) -> bool:
    """Match a tool against policy patterns.

    Supported forms: '*', 'database.*', '*.update_commission', 'update_commission',
    'database:update_commission', and 'builtin:filesystem' / 'builtin:*' for the
    harness's own tools, so the policy reads the same as the agent's tools list.
    """
    for raw in patterns:
        pattern = raw.strip().lower()
        if not pattern:
            continue

        if pattern.startswith("builtin:"):
            target = pattern[len("builtin:"):].rstrip("*").rstrip(":")
            if target in ("", "*"):
                if qualified_name.startswith(tuple(f"{s}." for s in BUILTIN_SERVERS)):
                    return True
            elif qualified_name.startswith(f"{target}."):
                return True
            continue

        if pattern.startswith("mcp:"):
            pattern = pattern[4:]
        pattern = pattern.replace(":", ".")

        if pattern == "*":
            return True
        if "." in pattern:
            if fnmatch.fnmatchcase(qualified_name, pattern):
                return True
        elif fnmatch.fnmatchcase(bare_name, pattern) or fnmatch.fnmatchcase(qualified_name, f"*.{pattern}"):
            return True
    return False


# -----------------------------------------------------------------------------
# Path policy
# -----------------------------------------------------------------------------
def _extract_path(args: Dict[str, Any]) -> Optional[str]:
    """Return the filesystem target of a tool call, or None when it has none."""
    for key in _PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _resolve(raw_path: str) -> Optional[tuple]:
    """Resolve a raw path to (path, posix_str, root_relative_posix_or_None)."""
    try:
        path = Path(raw_path)
        if not path.is_absolute():
            path = config.root_dir / path
        resolved = path.resolve()
    except (OSError, ValueError) as exc:
        logger.warning("Could not resolve path %r: %s", raw_path, exc)
        return None

    try:
        relative = resolved.relative_to(config.root_dir.resolve()).as_posix()
    except ValueError:
        relative = None
    return resolved, resolved.as_posix(), relative


def _matches_path(resolved: Path, posix: str, relative: Optional[str], patterns: List[str]) -> bool:
    """Match a resolved path against absolute or root-relative patterns.

    A pattern such as ``/data/tmp/*`` also matches ``/data/tmp`` itself, so a
    directory-scoped tool (file_list, file_glob) can target the directory it was
    granted without needing a second entry.
    """
    for raw in patterns:
        pattern = raw.strip().rstrip("/")
        if not pattern:
            continue
        if pattern == "*":
            return True

        targets = [pattern]
        if pattern.endswith("/*"):
            targets.append(pattern[:-2])

        for target in targets:
            if target.startswith("/"):
                if fnmatch.fnmatchcase(posix, target):
                    return True
            elif relative is not None:
                clean = target.lstrip("/")
                if fnmatch.fnmatchcase(relative, clean):
                    return True

    return False


def normalize_rules(raw: Any) -> Dict[str, List[str]]:
    """Normalize a rule set to {'deny': [], 'ask': [], 'allow': []}.

    Accepts a mapping or the shorthand list form [deny, ask, allow].
    """
    if isinstance(raw, dict):
        return {
            "deny": list(raw.get("deny") or []),
            "ask": list(raw.get("ask") or []),
            "allow": list(raw.get("allow") or []),
        }
    if isinstance(raw, (list, tuple)):
        def _group(index: int) -> List[str]:
            item = raw[index] if len(raw) > index else None
            return list(item) if isinstance(item, (list, tuple)) else []

        deny, ask, allow = _group(0), _group(1), _group(2)
        if len(raw) == 2 and ask and not allow:
            ask, allow = [], ask
        return {"deny": deny, "ask": ask, "allow": allow}
    return {"deny": [], "ask": [], "allow": []}


def rules_for_tool(bare_name: str, qualified_name: str, path_policy: Optional[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Look up the path rule set for a tool. Absent rules mean deny (caller enforces)."""
    if not path_policy:
        return {"deny": [], "ask": [], "allow": []}

    for key in (bare_name, qualified_name):
        if key in path_policy:
            return normalize_rules(path_policy[key])

    for key, value in path_policy.items():
        if fnmatch.fnmatchcase(bare_name, key) or fnmatch.fnmatchcase(qualified_name, key):
            return normalize_rules(value)

    if "*" in path_policy:
        return normalize_rules(path_policy["*"])

    return {"deny": [], "ask": [], "allow": []}


# -----------------------------------------------------------------------------
# Guardrail factory
# -----------------------------------------------------------------------------
def _denied(qualified_name: str, message: str, **extra: Any) -> Dict[str, Any]:
    logger.warning("DENY %s: %s", qualified_name, message)
    return {"status": "permission_denied", "tool": qualified_name, "message": message, **extra}


def _consume_confirmation(
    tool_context: ToolContext,
    qualified_name: Optional[str] = None,
    args: Optional[Dict[str, Any]] = None,
    agent_name: Optional[str] = None,
) -> bool:
    """Return True when this call carries an unused approval for the request.

    ADK delivers an approval as ``ToolConfirmation`` on the tool context, with
    the original request id inside ``payload``. The token is single-use and
    bound to the specific tool invocation.
    """
    confirmation = getattr(tool_context, "tool_confirmation", None)
    if confirmation is None or not getattr(confirmation, "confirmed", False):
        return False
    payload = getattr(confirmation, "payload", None)
    if not isinstance(payload, dict):
        return False
    request_id = payload.get("request_id")
    return bool(
        request_id
        and approval_registry.consume(
            request_id,
            tool_name=qualified_name,
            args=args,
            agent_name=agent_name,
        )
    )


def create_agent_guardrail(
    agent_name: str,
    policy: Dict[str, Any],
    path_policy: Optional[Dict[str, Any]] = None,
    model_override: Optional[str] = None,
):
    """Build (before_tool_callback, after_tool_callback) for one agent."""
    deny_patterns = list(policy.get("deny") or [])
    ask_patterns = list(policy.get("ask") or [])
    allow_patterns = list(policy.get("allow") or [])

    def request_approval(
        tool_context: ToolContext,
        session_id: str,
        user_id: str,
        qualified_name: str,
        args: Dict[str, Any],
        hint: str,
        **extra: Any,
    ) -> Dict[str, Any]:
        record = approval_registry.create_request(
            session_id=session_id,
            user_id=user_id,
            agent_name=agent_name,
            tool_name=qualified_name,
            args=dict(args),
            hint=hint,
            model_override=model_override,
        )
        tool_context.request_confirmation(
            hint=hint,
            payload={"request_id": record.request_id, "tool": qualified_name, **extra, "args": args},
        )
        return {
            "status": "confirmation_required",
            "request_id": record.request_id,
            "tool": qualified_name,
            **extra,
            "message": hint,
        }

    def before_tool_guard(
        tool: BaseTool, args: Dict[str, Any], tool_context: ToolContext
    ) -> Optional[Dict[str, Any]]:
        bare_name, server_name, qualified_name = resolve_tool_identity(tool)
        session = tool_context.session
        session_id = session.id
        user_id = session.user_id

        # --- Layer 1: tool permission -------------------------------------
        if _matches_tool(bare_name, qualified_name, deny_patterns):
            return _denied(qualified_name, f"Tool '{qualified_name}' is blocked by policy for agent '{agent_name}'.")

        if _matches_tool(bare_name, qualified_name, ask_patterns):
            confirmation = getattr(tool_context, "tool_confirmation", None)
            if confirmation is not None:
                if not getattr(confirmation, "confirmed", False):
                    return _denied(qualified_name, f"Action '{qualified_name}' was rejected by operator.")
                if not _consume_confirmation(tool_context, qualified_name, args, agent_name):
                    return _denied(
                        qualified_name,
                        f"Action '{qualified_name}' approval token is invalid, expired, or does not match call parameters.",
                    )
            else:
                hint = f"Action '{qualified_name}' requires human confirmation."
                return request_approval(tool_context, session_id, user_id, qualified_name, args, hint)
        elif not _matches_tool(bare_name, qualified_name, allow_patterns):
            return _denied(
                qualified_name,
                f"Tool '{qualified_name}' is not allowed for agent '{agent_name}'. Default deny.",
            )

        # --- Layer 2: path permission (builtin filesystem tools only) ------
        if server_name != "filesystem":
            return None

        raw_path = _extract_path(args)
        if raw_path is None:
            return _denied(
                qualified_name,
                f"Tool '{bare_name}' requires an explicit path argument. Default deny.",
            )

        resolved = _resolve(raw_path)
        if resolved is None:
            return _denied(qualified_name, f"Path '{raw_path}' could not be resolved to a safe location.")

        resolved_path, posix, relative = resolved
        display = relative or posix

        rules = rules_for_tool(bare_name, qualified_name, path_policy)

        if _matches_path(resolved_path, posix, relative, rules["deny"]):
            return _denied(qualified_name, f"Path '{display}' is blocked for tool '{bare_name}'.", path=display)

        if _matches_path(resolved_path, posix, relative, rules["ask"]):
            confirmation = getattr(tool_context, "tool_confirmation", None)
            if confirmation is not None:
                if not getattr(confirmation, "confirmed", False):
                    return _denied(
                        qualified_name,
                        f"Action '{bare_name}' on '{display}' was rejected by operator.",
                        path=display,
                    )
                if not _consume_confirmation(tool_context, qualified_name, args, agent_name):
                    return _denied(
                        qualified_name,
                        f"Action '{bare_name}' on '{display}' approval token is invalid, expired, or does not match call parameters.",
                        path=display,
                    )
            else:
                hint = f"Access to '{display}' via '{bare_name}' requires human confirmation."
                return request_approval(
                    tool_context, session_id, user_id, qualified_name, args, hint, path=display
                )
        elif not _matches_path(resolved_path, posix, relative, rules["allow"]):
            return _denied(
                qualified_name,
                f"Path '{display}' is not permitted for tool '{bare_name}'. Default deny.",
                path=display,
            )

        return None

    def after_tool_guard(
        tool: BaseTool, args: Dict[str, Any], tool_context: ToolContext, result: Any
    ) -> Optional[Any]:
        return None

    return before_tool_guard, after_tool_guard
