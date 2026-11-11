"""Runner and stream orchestration.

Builds and caches one ADK ``Runner`` per agent and exposes streaming helpers for
prompt turns, aborts, and tool-confirmation resumptions.
"""

import asyncio
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional

from google.adk.apps import App
from google.adk.events import Event
from google.adk.runners import Runner
from google.genai import types

from .compaction import create_compaction_config
from .config import config
from .loader import agent_catalog
from .sessions import get_session_service

from google.adk.utils._function_call_names import REQUEST_CONFIRMATION_FUNCTION_CALL_NAME as CONFIRMATION_FUNCTION_NAME

logger = logging.getLogger("harness.runner")

# One cached runner per (agent_name, model).
_runners: Dict[str, Runner] = {}
_runner_lock = asyncio.Lock()


async def invalidate_runners() -> None:
    """Drop cached runners so the next turn rebuilds agents against current config.

    A Runner holds the LlmAgent (and therefore its model) it was built with, so a
    global model switch via /api/model clears the cache.
    """
    async with _runner_lock:
        _runners.clear()


async def get_agent_runner(agent_name: str, model_override: Optional[str] = None) -> Runner:
    """Return the cached Runner for an agent and model, building it on first use."""
    name = (agent_name or "").strip()
    if not name:
        raise ValueError("agent_name is required.")

    chosen_model = (model_override or "").strip() or config.model
    # Validate the model alias exists in catalog (raises fast if unknown)
    config.model_catalog.get(chosen_model)

    cache_key = f"{name}:{chosen_model}"

    async with _runner_lock:
        runner = _runners.get(cache_key)
        if runner is not None:
            return runner

        agent = await agent_catalog.build_agent(name, model_override=chosen_model)
        runner = Runner(
            app=App(
                name=name,
                root_agent=agent,
                events_compaction_config=create_compaction_config(chosen_model),
            ),
            session_service=get_session_service(),
            auto_create_session=True,
        )
        _runners[cache_key] = runner
        return runner


def _text_message(parts: List[types.Part]) -> types.Content:
    return types.Content(role="user", parts=parts)


async def run_prompt_stream(
    session_id: str,
    prompt: str,
    agent_name: str,
    user_id: str,
    attachments: Optional[List[Dict[str, Any]]] = None,
    abort_signal: Optional[asyncio.Event] = None,
    model_override: Optional[str] = None,
) -> AsyncGenerator[Event, None]:
    """Run one prompt turn and yield ADK events."""
    runner = await get_agent_runner(agent_name, model_override=model_override)

    parts: List[types.Part] = []
    for attachment in attachments or []:
        path = attachment.get("path")
        if path:
            parts.append(types.Part.from_text(
                text=f"[Attached File: {attachment.get('name', 'file')}]\nURI: {path}"
            ))
    parts.append(types.Part.from_text(text=prompt))

    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=_text_message(parts),
    ):
        if abort_signal is not None and abort_signal.is_set():
            raise asyncio.CancelledError("Prompt execution aborted by user.")
        yield event


async def _find_confirmation_id(session_id: str, agent_name: str, user_id: str) -> str:
    """Return the id of the outstanding adk_request_confirmation function call."""
    session = await get_session_service().get_session(
        app_name=agent_name, user_id=user_id, session_id=session_id
    )
    if session is None:
        raise ValueError(f"Session '{session_id}' not found for agent '{agent_name}'.")

    for event in reversed(session.events):
        for call in event.get_function_calls() or []:
            if call.name == CONFIRMATION_FUNCTION_NAME and call.id:
                return call.id
    raise ValueError(f"No pending confirmation in session '{session_id}'.")


async def resume_confirmation_stream(
    session_id: str,
    agent_name: str,
    user_id: str,
    confirmed: bool,
    payload: Optional[Dict[str, Any]] = None,
    abort_signal: Optional[asyncio.Event] = None,
    model_override: Optional[str] = None,
) -> AsyncGenerator[Event, None]:
    """Answer a paused confirmation, then yield the resumed events."""
    runner = await get_agent_runner(agent_name, model_override=model_override)
    confirmation_id = await _find_confirmation_id(session_id, agent_name, user_id)

    # ADK validates this as a ToolConfirmation model: only 'confirmed' and
    # 'payload' are accepted, and the payload carries our request id.
    response = types.Part.from_function_response(
        name=CONFIRMATION_FUNCTION_NAME,
        response={
            "id": confirmation_id,
            "name": CONFIRMATION_FUNCTION_NAME,
            "response": {"confirmed": confirmed, "payload": payload or {}},
        },
    )
    response.function_response.id = confirmation_id

    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=_text_message([response]),
    ):
        if abort_signal is not None and abort_signal.is_set():
            raise asyncio.CancelledError("Resume aborted by user.")
        yield event
