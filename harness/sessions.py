"""Session management with persistent database storage."""

import uuid
from typing import Optional
from google.adk.sessions import DatabaseSessionService
from .config import config

_session_service: Optional[DatabaseSessionService] = None


def _enable_sqlite_wal(db_url: str) -> None:
    """Ensure SQLite database uses WAL journal mode for multi-process concurrency."""
    if "sqlite" not in db_url:
        return
    import sqlite3
    from pathlib import Path
    path_part = db_url.split(":///", 1)[-1]
    db_path = Path(path_part).resolve()
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
    except Exception:
        pass


def get_session_service() -> DatabaseSessionService:
    """Return the global DatabaseSessionService instance."""
    global _session_service
    if _session_service is None:
        _enable_sqlite_wal(config.database_url)
        _session_service = DatabaseSessionService(db_url=config.database_url)
    return _session_service


def generate_session_id() -> str:
    """Generate a unique UUID-based session identifier."""
    return f"ses_{uuid.uuid4().hex}"


def resolve_user_id(
    header_val: Optional[str] = None,
    service_name: Optional[str] = None,
) -> str:
    """Resolve user identity cleanly without hardcoding."""
    if service_name:
        return f"service:{service_name}"
    if header_val and header_val.strip():
        return header_val.strip()
    return "operator"
