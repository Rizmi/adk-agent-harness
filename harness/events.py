"""SSE event serialization for HTTP clients.

ADK events are translated into the small set of chunk types a client UI
renders. The contract is:

  {"type": "delta", "field": "text"|"reasoning", "text": "..."}
  {"type": "tool", "callId": "...", "tool": "...", "state": "running"|"complete", ...}
  {"type": "permission", "requestId": "...", "action": "...", "hint": "...", ...}
"""

from typing import Any, Dict, List

from google.adk.events import Event
from google.adk.utils._function_call_names import REQUEST_CONFIRMATION_FUNCTION_CALL_NAME

CONFIRMATION_FUNCTION_NAME = REQUEST_CONFIRMATION_FUNCTION_CALL_NAME


def _permission_event(
    request_id: Any,
    tool: Any,
    hint: str,
    args: Any = None,
    path: Any = None,
) -> Dict[str, Any]:
    """Build a permission card payload."""
    resources = []
    if isinstance(args, dict):
        resources = [f"{k}: {v}" for k, v in args.items() if k != "_approval_token"]
    if path:
        resources.insert(0, f"path: {path}")

    return {
        "type": "permission",
        "requestId": request_id,
        "action": tool or "restricted_action",
        "hint": hint,
        "resources": resources,
    }


def _usage_event(usage: Any, context_limit: int) -> Dict[str, Any]:
    """Build a usage chunk from ADK event usage metadata."""
    prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
    return {
        "type": "usage",
        "promptTokens": prompt_tokens,
        "candidatesTokens": getattr(usage, "candidates_token_count", 0) or 0,
        "totalTokens": getattr(usage, "total_token_count", 0) or 0,
        "cachedTokens": getattr(usage, "cached_content_token_count", 0) or 0,
        "thoughtsTokens": getattr(usage, "thoughts_token_count", 0) or 0,
        "contextTokens": prompt_tokens,
        "contextLimit": context_limit,
    }


_active_call_tools: Dict[str, str] = {}


def serialize_adk_event(event: Event, context_limit: int) -> List[Dict[str, Any]]:
    """Convert one ADK event into SSE chunks for HTTP clients."""
    chunks: List[Dict[str, Any]] = []

    usage = getattr(event, "usage_metadata", None)
    if usage:
        chunks.append(_usage_event(usage, context_limit=context_limit))

    if not event.content or not event.content.parts:
        return chunks

    for part in event.content.parts:
        function_call = getattr(part, "function_call", None)
        function_response = getattr(part, "function_response", None)

        if function_call:
            args = function_call.args or {}
            call_id = function_call.id or function_call.name

            if function_call.name == CONFIRMATION_FUNCTION_NAME:
                confirmation = args.get("toolConfirmation") or {}
                original = args.get("originalFunctionCall") or {}
                payload = confirmation.get("payload") or {}
                chunks.append(_permission_event(
                    request_id=payload.get("request_id") or function_call.id,
                    tool=payload.get("tool") or original.get("name"),
                    hint=confirmation.get("hint") or "Confirmation required.",
                    args=payload.get("args") or original.get("args"),
                    path=payload.get("path"),
                ))
            else:
                tool_label = function_call.name
                tool_input = args
                if function_call.name == "tool_run" and "tool_name" in args:
                    tool_label = args["tool_name"]
                    tool_input = args.get("arguments") or {}
                elif function_call.name == "tool_describe" and "tool_name" in args:
                    tool_label = f"describe({args['tool_name']})"

                _active_call_tools[call_id] = tool_label

                chunks.append({
                    "type": "tool",
                    "callId": call_id,
                    "tool": tool_label,
                    "state": "running",
                    "input": tool_input,
                })
            continue

        if function_response:
            output = function_response.response or {}
            call_id = function_response.id or function_response.name
            tool_label = _active_call_tools.pop(call_id, function_response.name)

            if isinstance(output, dict) and output.get("status") == "confirmation_required":
                chunks.append(_permission_event(
                    request_id=output.get("request_id"),
                    tool=output.get("tool") or tool_label,
                    hint=output.get("message", "Confirmation required."),
                    path=output.get("path"),
                ))
            chunks.append({
                "type": "tool",
                "callId": call_id,
                "tool": tool_label,
                "state": "complete",
                "output": output,
            })
            continue

        text = getattr(part, "text", None)
        if not text:
            continue

        # A thought part carries reasoning only; never emit it as answer text.
        if getattr(part, "thought", False):
            chunks.append({"type": "delta", "field": "reasoning", "text": text})
        else:
            chunks.append({"type": "delta", "field": "text", "text": text})

    return chunks
