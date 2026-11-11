"""FastAPI Server exposing the Generic Agent Harness to HTTP/SSE Clients.

Completely project-agnostic. Supports multiple pluggable agents, SSE streaming,
session persistence, cancellation signals, and human confirmation workflows.
"""

import asyncio
import base64
import json
import os
import secrets
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import config, reload_env
from .events import serialize_adk_event
from .loader import agent_catalog
from .mcp.manager import mcp_manager
from .permissions import approval_registry
from .runner import invalidate_runners, resume_confirmation_stream, run_prompt_stream
from .sessions import (
    generate_session_id,
    get_session_service,
    resolve_user_id,
)

# Registry of active session cancellation signals: session_id -> asyncio.Event
_active_abort_signals: Dict[str, asyncio.Event] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager: initializes persistent MCP connections and cleans up on shutdown."""
    print("[Harness] Starting ADK Agent runtime...")
    # Load all agent definitions
    agents = agent_catalog.discover_and_load_all()
    print(f"[Harness] Discovered {len(agents)} agents: {list(agents.keys())}")

    # Check available MCP servers
    available_mcps = mcp_manager.list_available_servers()
    connected = [m for m in available_mcps if mcp_manager.is_connected(m)]
    print(f"[Harness] MCP servers online ({len(connected)}/{len(available_mcps)}): {connected}")
    yield
    print("[Harness] Shutting down...")
    await mcp_manager.close_all()
    print("[Harness] Shutdown complete.")


app = FastAPI(
    title="ADK Agent Harness",
    description="Generic, pluggable agent runtime container powered by Google ADK",
    version="2.0.0",
    lifespan=lifespan,
)

# Configure secure CORS: support ADK_ALLOWED_ORIGINS or safe local/private network origins
allowed_origins_env = os.environ.get("ADK_ALLOWED_ORIGINS", "").strip()
if allowed_origins_env and allowed_origins_env != "*":
    origins = [o.strip() for o in allowed_origins_env.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
elif allowed_origins_env == "*":
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|100\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+)(:\d+)?$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    """Enforce Basic Authentication on all endpoints when AGENT_SERVER_PASSWORD is set."""
    # Allow CORS preflight requests
    if request.method == "OPTIONS":
        return await call_next(request)

    if config.server_password:
        auth_hdr = request.headers.get("authorization")
        if not auth_hdr or not auth_hdr.startswith("Basic "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication required"},
                headers={"WWW-Authenticate": "Basic"},
            )
        try:
            encoded = auth_hdr.split(" ", 1)[1]
            decoded = base64.b64decode(encoded).decode("utf-8")
            username, password = decoded.split(":", 1)
        except Exception:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid authorization header format"},
                headers={"WWW-Authenticate": "Basic"},
            )

        user_ok = True
        if config.server_username:
            user_ok = secrets.compare_digest(username, config.server_username)
        pass_ok = secrets.compare_digest(password, config.server_password)

        if not (user_ok and pass_ok):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid credentials"},
                headers={"WWW-Authenticate": "Basic"},
            )

    return await call_next(request)


# -----------------------------------------------------------------------------
# Schemas
# -----------------------------------------------------------------------------
class SessionCreateRequest(BaseModel):
    title: Optional[str] = "Chat"
    user_id: Optional[str] = None
    agent: Optional[str] = None


class PromptRequest(BaseModel):
    text: Optional[str] = ""
    files: Optional[List[Dict[str, Any]]] = None
    user_id: Optional[str] = None
    agent: Optional[str] = None
    model: Optional[str] = None


class ApprovalReplyRequest(BaseModel):
    requestId: str
    decision: str  # "approved" | "rejected" | "once"
    sessionId: Optional[str] = None
    model: Optional[str] = None


class ModelSwitchRequest(BaseModel):
    model: str


# -----------------------------------------------------------------------------
# Web Chat UI (Optional / Toggleable)
# -----------------------------------------------------------------------------
ENABLE_WEB_UI = os.environ.get("ADK_ENABLE_WEB_UI", "true").strip().lower() in ("true", "1", "yes")
WEB_DIR = config.root_dir / "web"

if ENABLE_WEB_UI and WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    @app.get("/chat", include_in_schema=False)
    async def get_web_ui():
        """Serve the standalone browser chat UI."""
        index_file = WEB_DIR / "index.html"
        if index_file.exists():
            return FileResponse(str(index_file))
        return JSONResponse({"status": "ready", "message": "Web UI directory present but index.html missing"})
else:
    @app.get("/", include_in_schema=False)
    async def get_headless_root():
        """Headless root response."""
        return {
            "name": "ADK Agent Harness",
            "version": "1.0.0",
            "status": "ready",
            "mode": "headless",
            "endpoints": ["/api/info", "/api/agents", "/api/mcp", "/api/model", "/api/session"],
        }


# -----------------------------------------------------------------------------
# Info & Catalog Endpoints
# -----------------------------------------------------------------------------
@app.get("/api/info")
async def get_info():
    """Harness health and active configuration."""
    return {
        "name": "ADK Agent Harness",
        "version": "1.0.0",
        "status": "ready",
        "model": config.model,
        "agents": agent_catalog.list_agent_names(),
    }


@app.get("/api/agents")
async def list_agents():
    """Lists loaded agent definitions."""
    catalog = agent_catalog.discover_and_load_all()
    return {
        "agents": [
            {
                "name": a.name,
                "description": a.description,
                "tools": list(a.permissions.get("allow") or []) + list(a.permissions.get("ask") or []),
                "skills": a.skills,
            }
            for a in catalog.values()
        ]
    }


@app.get("/api/mcp")
async def get_mcp_status():
    """Reports status of discovered MCP servers."""
    servers = mcp_manager.list_available_servers()
    return {
        "data": [
            {
                "name": s,
                "transport": mcp_manager.get_server_entry(s).transport,
                "status": {"status": "connected" if mcp_manager.is_connected(s) else "discovered"},
            }
            for s in servers
        ]
    }


@app.get("/api/model")
@app.get("/api/model/default")
async def get_models():
    """Lists every model in the catalog, for client model pickers."""
    reload_env()
    config.model_catalog.invalidate()
    entries = config.model_catalog.list_entries()
    groups: dict = {}
    for entry in entries:
        groups.setdefault(entry["provider"], []).append(
            {
                "id": entry["name"],
                "name": entry["display_name"],
                "configured": entry.get("configured", True),
                "missing_vars": entry.get("missing_vars", []),
            }
        )
    return {
        "current": config.model,
        "groups": [
            {"provider": provider, "models": models}
            for provider, models in groups.items()
        ],
    }


@app.post("/api/model")
async def switch_model(req: ModelSwitchRequest):
    """Switch the active model to a catalog entry."""
    requested = req.model.strip()
    if not requested:
        raise HTTPException(status_code=400, detail="Model identifier is required.")
    try:
        spec = config.use_model(requested)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Cached runners are bound to the model they were built with, so drop them.
    await invalidate_runners()
    return {"success": True, "model": spec.name, "label": spec.display_name}


# -----------------------------------------------------------------------------
# Session Management
# -----------------------------------------------------------------------------
@app.post("/api/session")
async def create_session(
    req: Optional[SessionCreateRequest] = None,
    x_user_id: Optional[str] = Header(None),
    x_agent: Optional[str] = Header(None),
    agent: Optional[str] = Query(None),
):
    """Create a persistent session with a UUID identifier."""
    target_agent = (req.agent if req and req.agent else None) or agent or x_agent
    if not target_agent or not target_agent.strip():
        available = agent_catalog.list_agent_names()
        raise HTTPException(
            status_code=400,
            detail=f"Missing required 'agent' parameter. You must explicitly specify an agent. Available: {available}",
        )
    target_agent = target_agent.strip()
    available = agent_catalog.list_agent_names()
    if target_agent not in available:
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{target_agent}' not found. Available: {available}",
        )

    session_svc = get_session_service()
    uid = resolve_user_id(x_user_id or (req.user_id if req else None))
    sid = generate_session_id()

    session = await session_svc.create_session(
        app_name=target_agent,
        user_id=uid,
        session_id=sid,
    )
    return {
        "id": session.id,
        "data": {
            "id": session.id,
            "title": req.title if req else "Chat",
            "agent": target_agent,
            "createdAt": session.last_update_time,
        },
    }


@app.delete("/api/session/{session_id}")
async def delete_session(
    session_id: str,
    x_user_id: Optional[str] = Header(None),
    x_agent: Optional[str] = Header(None),
    agent: Optional[str] = Query(None),
):
    """Delete a persistent session."""
    target_agent = agent or x_agent
    if not target_agent or not target_agent.strip():
        raise HTTPException(
            status_code=400,
            detail="Missing required 'agent' parameter. Specify 'agent' via query ?agent= or X-Agent header.",
        )
    target_agent = target_agent.strip()

    session_svc = get_session_service()
    uid = resolve_user_id(x_user_id)
    try:
        await session_svc.delete_session(app_name=target_agent, user_id=uid, session_id=session_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete session: {e}")
    return {"success": True}


@app.get("/api/session/active")
async def get_active_sessions():
    """List running session IDs."""
    active_map = {sid: True for sid in _active_abort_signals}
    return {"data": active_map}


@app.get("/api/session/{session_id}/message")
async def get_session_messages(
    session_id: str,
    x_user_id: Optional[str] = Header(None),
    x_agent: Optional[str] = Header(None),
    agent: Optional[str] = Query(None),
):
    """Retrieve message history for a session."""
    target_agent = agent or x_agent
    if not target_agent or not target_agent.strip():
        raise HTTPException(
            status_code=400,
            detail="Missing required 'agent' parameter. Specify 'agent' via query ?agent= or X-Agent header.",
        )
    target_agent = target_agent.strip()

    session_svc = get_session_service()
    uid = resolve_user_id(x_user_id)
    try:
        session = await session_svc.get_session(app_name=target_agent, user_id=uid, session_id=session_id)
        if not session:
            return {"data": []}

        # Index all function responses by call id for pairing
        responses: Dict[str, Any] = {}
        for event in session.events:
            if event.content and event.content.parts:
                for p in event.content.parts:
                    fr = getattr(p, "function_response", None)
                    if fr:
                        cid = getattr(fr, "id", None) or fr.name
                        responses[cid] = getattr(fr, "response", {})

        is_active = session_id in _active_abort_signals
        entries = []
        total_prompt_tokens = 0
        total_candidates_tokens = 0
        total_tokens = 0
        cached_tokens = 0
        latest_prompt_tokens = 0

        for event in session.events:
            um = getattr(event, "usage_metadata", None)
            if um:
                p_cnt = getattr(um, "prompt_token_count", 0) or 0
                c_cnt = getattr(um, "candidates_token_count", 0) or 0
                t_cnt = getattr(um, "total_token_count", 0) or 0
                cache_cnt = getattr(um, "cached_content_token_count", 0) or 0
                total_prompt_tokens += p_cnt
                total_candidates_tokens += c_cnt
                total_tokens += t_cnt
                cached_tokens += cache_cnt
                if p_cnt > 0:
                    latest_prompt_tokens = p_cnt

            # ADK stamps events authored by the runner with the user_id (e.g. "operator")
            # and its own turns with the agent name, so anything not authored by this
            # agent is a user message.
            role = "assistant" if event.author == target_agent else "user"
            parts = []
            if event.content and event.content.parts:
                for p in event.content.parts:
                    # Thought parts also carry .text, so this check must come
                    # before the plain-text branch or reasoning renders as answer.
                    if getattr(p, "thought", False):
                        parts.append({"type": "reasoning", "text": p.text or ""})
                    elif getattr(p, "text", None):
                        parts.append({"type": "text", "text": p.text})
                    elif getattr(p, "function_call", None):
                        fc = p.function_call
                        cid = getattr(fc, "id", None) or fc.name
                        has_resp = cid in responses
                        # If completed, show complete with output; if still streaming, running; else aborted/incomplete
                        state = "complete" if has_resp else ("running" if is_active else "incomplete")
                        part_dict: Dict[str, Any] = {
                            "type": "tool",
                            "callId": cid,
                            "tool": fc.name,
                            "state": state,
                            "input": getattr(fc, "args", {}),
                        }
                        if has_resp:
                            part_dict["output"] = responses[cid]
                        parts.append(part_dict)
                    elif getattr(p, "function_response", None):
                        fr = p.function_response
                        cid = getattr(fr, "id", None) or fr.name
                        # Only append if not already paired with a call in this turn
                        if not any(part.get("callId") == cid for part in parts):
                            parts.append({
                                "type": "tool",
                                "callId": cid,
                                "tool": fr.name,
                                "state": "complete",
                                "output": getattr(fr, "response", {}),
                            })

            if parts:
                entries.append({
                    "id": event.id,
                    "type": role,
                    "info": {"role": role, "id": event.id},
                    "content": parts,
                })

        context_limit = config.get_context_limit()

        usage_summary = {
            "promptTokens": total_prompt_tokens,
            "candidatesTokens": total_candidates_tokens,
            "totalTokens": total_tokens,
            "cachedTokens": cached_tokens,
            "contextTokens": latest_prompt_tokens,
            "contextLimit": context_limit,
        }

        return {"data": entries, "usage": usage_summary}
    except Exception as e:
        return {"data": [], "error": str(e)}


@app.get("/api/session/{session_id}/usage")
async def get_session_usage(
    session_id: str,
    x_user_id: Optional[str] = Header(None),
    x_agent: Optional[str] = Header(None),
    agent: Optional[str] = Query(None),
):
    """Retrieve cumulative and latest turn token usage for a session."""
    target_agent = agent or x_agent
    if not target_agent or not target_agent.strip():
        raise HTTPException(
            status_code=400,
            detail="Missing required 'agent' parameter. Specify 'agent' via query ?agent= or X-Agent header.",
        )
    target_agent = target_agent.strip()
    available = agent_catalog.list_agent_names()
    if target_agent not in available:
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{target_agent}' not found. Available: {available}",
        )

    session_svc = get_session_service()
    uid = resolve_user_id(x_user_id)
    try:
        session = await session_svc.get_session(app_name=target_agent, user_id=uid, session_id=session_id)
        if not session:
            raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found.")

        total_prompt_tokens = 0
        total_candidates_tokens = 0
        total_tokens = 0
        cached_tokens = 0
        latest_prompt_tokens = 0

        for event in session.events:
            um = getattr(event, "usage_metadata", None)
            if um:
                p_cnt = getattr(um, "prompt_token_count", 0) or 0
                c_cnt = getattr(um, "candidates_token_count", 0) or 0
                t_cnt = getattr(um, "total_token_count", 0) or 0
                cache_cnt = getattr(um, "cached_content_token_count", 0) or 0
                total_prompt_tokens += p_cnt
                total_candidates_tokens += c_cnt
                total_tokens += t_cnt
                cached_tokens += cache_cnt
                if p_cnt > 0:
                    latest_prompt_tokens = p_cnt

        context_limit = config.get_context_limit()

        return {
            "sessionId": session_id,
            "promptTokens": total_prompt_tokens,
            "candidatesTokens": total_candidates_tokens,
            "totalTokens": total_tokens,
            "cachedTokens": cached_tokens,
            "contextTokens": latest_prompt_tokens,
            "contextLimit": context_limit,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -----------------------------------------------------------------------------
# Streaming Prompt Execution
# -----------------------------------------------------------------------------
@app.post("/api/session/{session_id}/prompt")
async def prompt_session(
    session_id: str,
    req: PromptRequest,
    x_user_id: Optional[str] = Header(None),
    agent: Optional[str] = Query(None),
):
    """Execute a prompt turn and stream response events via SSE."""
    target_agent = (req.agent or agent or "").strip()
    if not target_agent:
        available = agent_catalog.list_agent_names()
        raise HTTPException(
            status_code=400,
            detail=f"Missing required 'agent' parameter. Available: {available}",
        )
    if target_agent not in agent_catalog.list_agent_names():
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{target_agent}' not found. Available: {agent_catalog.list_agent_names()}",
        )

    uid = resolve_user_id(x_user_id or req.user_id)
    session_svc = get_session_service()
    existing_session = await session_svc.get_session(app_name=target_agent, user_id=uid, session_id=session_id)
    if existing_session is None:
        # Auto-create the session if it does not exist yet (e.g. client provided id or restored from local storage)
        existing_session = await session_svc.create_session(
            app_name=target_agent,
            user_id=uid,
            session_id=session_id,
        )

    abort_event = asyncio.Event()
    _active_abort_signals[session_id] = abort_event

    context_limit = config.get_context_limit(req.model)

    async def event_generator():
        try:
            attachments = []
            for f in req.files or []:
                attachments.append({
                    "path": f.get("uri", "").replace("file://", ""),
                    "name": f.get("name", "attachment"),
                })

            async for adk_event in run_prompt_stream(
                session_id=session_id,
                prompt=req.text or "",
                agent_name=target_agent,
                user_id=uid,
                attachments=attachments,
                abort_signal=abort_event,
                model_override=req.model,
            ):
                for item in serialize_adk_event(adk_event, context_limit=context_limit):
                    yield f"data: {json.dumps(item)}\n\n"

        except asyncio.CancelledError:
            yield f"data: {json.dumps({'type': 'delta', 'field': 'text', 'text': '\n[Request aborted by user]'})}\n\n"
        except Exception as err:
            yield f"data: {json.dumps({'type': 'error', 'error': str(err)})}\n\n"
        finally:
            _active_abort_signals.pop(session_id, None)
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/session/{session_id}/abort")
async def abort_session(session_id: str):
    """Signal abortion of an active run."""
    signal = _active_abort_signals.get(session_id)
    if signal:
        signal.set()
        return {"success": True, "aborted": True}
    return {"success": True, "aborted": False}


# -----------------------------------------------------------------------------
# Approvals (Ask Tier)
# -----------------------------------------------------------------------------
@app.get("/api/permissions")
@app.get("/api/agent/permissions")
async def list_permissions(sessionId: Optional[str] = None):
    """List pending tool confirmation requests."""
    pending = approval_registry.get_pending(sessionId)
    return {"pending": pending}


@app.post("/api/session/{session_id}/approval")
async def reply_permission(session_id: str, req: ApprovalReplyRequest):
    """Resolve an approval request and, if approved or rejected, resume the paused turn."""
    approved = req.decision in ("approved", "once")
    record = approval_registry.resolve_request(
        req.requestId, "approved" if approved else "rejected"
    )
    if record is None:
        raise HTTPException(status_code=410, detail="Approval request expired or not found.")

    if record.session_id != session_id:
        raise HTTPException(status_code=400, detail="Session ID does not match approval request.")

    abort_event = asyncio.Event()
    _active_abort_signals[record.session_id] = abort_event

    model_override = (req.model or "").strip() or record.model_override
    context_limit = config.get_context_limit(model_override)

    async def resume_generator():
        try:
            async for adk_event in resume_confirmation_stream(
                session_id=record.session_id,
                agent_name=record.agent_name,
                user_id=record.user_id,
                confirmed=approved,
                payload={"request_id": record.request_id},
                abort_signal=abort_event,
                model_override=model_override,
            ):
                for item in serialize_adk_event(adk_event, context_limit=context_limit):
                    yield f"data: {json.dumps(item)}\n\n"
        except asyncio.CancelledError:
            yield f"data: {json.dumps({'type': 'delta', 'field': 'text', 'text': '\n[Resume aborted by user]'})}\n\n"
        except Exception as err:
            yield f"data: {json.dumps({'type': 'error', 'error': str(err)})}\n\n"
        finally:
            _active_abort_signals.pop(record.session_id, None)
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        resume_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
