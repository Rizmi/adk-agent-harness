"""Harness Configuration Manager.

Loads runtime parameters (models, database paths, compaction, MCP servers)
dynamically from the environment (.env) without hardcoding domain-specific rules.

Provider details (base URL, API key, model id) are NOT read from .env. The .env
file only names which catalog entry to use; ``models.json`` holds the rest.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .models import ModelCatalog, ModelSpec

ROOT_DIR = Path(__file__).resolve().parent.parent
_env_path_raw = os.environ.get("ADK_ENV_FILE", "").strip() or os.environ.get("ENV_FILE", "").strip()
ENV_PATH = Path(_env_path_raw).resolve() if _env_path_raw else ROOT_DIR / ".env"


def _load_dotenv(override: bool = False):
    """Load .env file into os.environ if available."""
    if ENV_PATH.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(ENV_PATH, override=override)
        except ImportError:
            with open(ENV_PATH, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if override or k not in os.environ:
                            os.environ[k] = v


def reload_env():
    """Reload .env file into os.environ so runtime changes to keys take effect immediately."""
    _load_dotenv(override=True)


_load_dotenv()


def _required(name: str) -> str:
    """Read a required env var. Missing or blank is a hard error - never defaulted."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required in .env and must not be blank.")
    return value


def _optional_path(name: str, fallback: Path) -> Path:
    """Directory override that defaults to a project-relative path."""
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser().resolve() if raw else fallback


@dataclass
class HarnessConfig:
    """Runtime configuration for the generic ADK agent harness."""

    # Active model name (a key in models.json) and the catalog it resolves in.
    model: str
    models_file: Path
    model_catalog: ModelCatalog

    # Paths
    root_dir: Path
    agents_dir: Path
    skills_dir: Path
    memory_file: Path
    mcp_dir: Path
    mcp_servers_file: Path

    # Session database URL
    database_url: str

    # Context compaction retention (keeps the last N events uncompacted)
    compaction_retention_size: int

    # Server Authentication
    server_username: Optional[str] = None
    server_password: Optional[str] = None

    # Timezone (default UTC)
    timezone: str = "UTC"

    # ------------------------------------------------------------------ models
    @property
    def model_spec(self) -> ModelSpec:
        """The resolved spec for the active model."""
        return self.model_catalog.get(self.model)

    @property
    def available_models(self) -> List[str]:
        """All catalog names, for client dropdowns."""
        return self.model_catalog.names()

    def spec_for(self, name: Optional[str]) -> ModelSpec:
        """Resolve any catalog name; empty name means the active model."""
        return self.model_catalog.get(name or self.model)

    def use_model(self, name: str) -> ModelSpec:
        """Switch the active model. Unknown names raise."""
        reload_env()
        self.model_catalog.invalidate(name)
        spec = self.model_catalog.get(name)
        self.model = spec.name
        return spec

    def get_context_limit(self, model_name: Optional[str] = None) -> int:
        """Return the validated context limit for a model name or the active model.

        Fails fast if context_limit is not defined.
        """
        spec = self.spec_for(model_name)
        if not spec.context_limit or spec.context_limit <= 0:
            raise ValueError(f"Model '{spec.name}' has no valid context_limit defined in models.json.")
        return spec.context_limit

    @classmethod
    def load(cls) -> "HarnessConfig":
        _load_dotenv()

        # The catalog must exist before anything can resolve a model.
        models_file_str = os.environ.get("ADK_MODELS_FILE", "").strip()
        models_file = Path(models_file_str).expanduser().resolve() if models_file_str else ROOT_DIR / "models.json"
        model_catalog = ModelCatalog(models_file)

        # The env var only names a catalog entry; it does not describe a provider.
        model = _required("ADK_MODEL")
        model_catalog.get(model)  # fail fast on an unknown name

        agents_dir = _optional_path("ADK_AGENTS_DIR", ROOT_DIR / "agents")
        skills_dir = _optional_path("ADK_SKILLS_DIR", ROOT_DIR / "skills")
        memory_file = _optional_path("ADK_MEMORY_FILE", ROOT_DIR / "MEMORY.md")
        mcp_dir = _optional_path("ADK_MCP_DIR", ROOT_DIR / "mcp")
        mcp_servers_file = _optional_path("ADK_MCP_SERVERS_FILE", mcp_dir / "servers.json")

        # Database URL for persistent sessions (saved in project root by default)
        db_url = os.environ.get("ADK_DATABASE_URL", "").strip()
        if not db_url or db_url in ("sessions.db", "sqlite+aiosqlite:///sessions.db", "sqlite:///sessions.db"):
            session_db_path = ROOT_DIR / "sessions.db"
            db_url = f"sqlite+aiosqlite:///{session_db_path.resolve()}"
        elif db_url.startswith("sqlite+aiosqlite:///") and not db_url.startswith("sqlite+aiosqlite:////"):
            rel_path = db_url.replace("sqlite+aiosqlite:///", "")
            session_db_path = (ROOT_DIR / rel_path).resolve()
            db_url = f"sqlite+aiosqlite:///{session_db_path}"
        elif db_url.startswith("sqlite:///") and not db_url.startswith("sqlite:////"):
            rel_path = db_url.replace("sqlite:///", "")
            session_db_path = (ROOT_DIR / rel_path).resolve()
            db_url = f"sqlite+aiosqlite:///{session_db_path}"

        server_username = os.environ.get("AGENT_SERVER_USERNAME", "").strip() or None
        server_password = os.environ.get("AGENT_SERVER_PASSWORD", "").strip() or None
        timezone = os.environ.get("ADK_TIMEZONE", "").strip() or os.environ.get("TIMEZONE", "").strip() or "UTC"

        return cls(
            model=model,
            models_file=models_file,
            model_catalog=model_catalog,
            root_dir=ROOT_DIR,
            agents_dir=agents_dir,
            skills_dir=skills_dir,
            memory_file=memory_file,
            mcp_dir=mcp_dir,
            mcp_servers_file=mcp_servers_file,
            database_url=db_url,
            compaction_retention_size=int(os.environ.get("ADK_COMPACTION_RETENTION_SIZE", "15")),
            server_username=server_username,
            server_password=server_password,
            timezone=timezone,
        )


config = HarnessConfig.load()
