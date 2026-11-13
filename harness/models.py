"""Model catalog loaded from ``models.json``.

The ``.env`` file only names which model to use (``ADK_MODEL`` /
``ADK_TRIAGE_MODEL``); every provider detail - provider id, model id, base URL,
API key, temperature - lives in ``models.json`` under that name.

Resolution is strict: an unknown name or a missing secret is an error. Nothing
falls back, so a typo can never silently run on a different model.
"""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# ${VAR} references are resolved from the environment (.env).
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
# {file:path} references are resolved from local or opencode secrets.
_FILE_REF = re.compile(r"^\{file:(.+)\}$")

# How the harness talks to a model.
#   litellm     - OpenAI-compatible provider through LiteLLM (default)
#   gemini-rest - Google Generative Language REST API
VALID_TRANSPORTS = {"litellm", "gemini-rest"}


class ModelConfigError(RuntimeError):
    """Raised when models.json is missing, malformed, or a name cannot be resolved."""


@dataclass(frozen=True)
class ModelSpec:
    """One fully-resolved model entry."""

    name: str
    provider: str
    model: str
    base_url: str
    api_key: str
    temperature: float
    transport: str
    label: str = ""
    context_limit: Optional[int] = None
    max_output_tokens: Optional[int] = None
    multimodal: bool = False

    @property
    def display_name(self) -> str:
        """Label shown in client model pickers."""
        return self.label or self.model


class ModelCatalog:
    """Reads models.json and resolves names to ModelSpec objects."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._raw_entries: Dict[str, dict] = {}
        self._models: Dict[str, ModelSpec] = {}
        self.reload()

    def reload(self) -> None:
        """(Re)read the catalog from disk. Raises ModelConfigError on any problem."""
        if not self.path.exists():
            raise ModelConfigError(
                f"Model catalog not found: {self.path}. Create it, or set ADK_MODELS_FILE to its location."
            )

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ModelConfigError(f"Model catalog {self.path} is not valid JSON: {exc}") from exc

        entries = raw.get("models") if isinstance(raw, dict) else None
        if not isinstance(entries, dict) or not entries:
            raise ModelConfigError(
                f"Model catalog {self.path} must contain a non-empty 'models' object."
            )

        for name, data in entries.items():
            if not isinstance(data, dict):
                raise ModelConfigError(f"Model '{name}' in {self.path} must be an object.")
            missing = [
                k for k in ("provider", "model", "base_url", "api_key", "transport")
                if not str(data.get(k, "")).strip()
            ]
            if missing:
                raise ModelConfigError(
                    f"Model '{name}' in {self.path} is missing required field(s): {', '.join(missing)}."
                )
            transport = str(data["transport"]).strip().lower()
            if transport not in VALID_TRANSPORTS:
                raise ModelConfigError(
                    f"Model '{name}' in {self.path} has transport '{transport}'; "
                    f"expected one of: {', '.join(sorted(VALID_TRANSPORTS))}."
                )
            temperature = data.get("temperature", 0.2)
            try:
                float(temperature)
            except (TypeError, ValueError) as exc:
                raise ModelConfigError(
                    f"Model '{name}' in {self.path} has a non-numeric temperature: {temperature!r}."
                ) from exc

        self._raw_entries = entries
        self._models.clear()

    def _build_spec(self, name: str, data: dict) -> ModelSpec:
        """Validate one catalog entry and resolve its environment references."""
        temperature = float(data.get("temperature", 0.2))
        multimodal = bool(data.get("multimodal", False))
        if not multimodal:
            caps = data.get("capabilities") or {}
            inputs = caps.get("input") or []
            if "image" in inputs:
                multimodal = True
        return ModelSpec(
            name=name,
            provider=str(data["provider"]).strip(),
            model=str(data["model"]).strip(),
            base_url=self._resolve_env(str(data["base_url"]), name, "base_url"),
            api_key=self._resolve_env(str(data["api_key"]), name, "api_key"),
            temperature=temperature,
            transport=str(data["transport"]).strip().lower(),
            label=str(data.get("label", "")).strip(),
            context_limit=data.get("context_limit"),
            max_output_tokens=data.get("max_output_tokens"),
            multimodal=multimodal,
        )

    def _resolve_env(self, value: str, model_name: str, field_name: str) -> str:
        """Expand ``${VAR}`` and ``{file:...}`` references; missing values are errors."""
        val_trimmed = value.strip()
        file_match = _FILE_REF.match(val_trimmed)
        if file_match:
            rel_path = file_match.group(1).strip()
            candidates = [
                self.path.parent / rel_path,
                Path.home() / ".config" / "opencode" / rel_path,
            ]
            for candidate in candidates:
                if candidate.exists() and candidate.is_file():
                    content = candidate.read_text(encoding="utf-8").strip()
                    if content:
                        return content
            raise ModelConfigError(
                f"Model '{model_name}' references file '{rel_path}' for {field_name}, "
                f"but file was not found."
            )

        def replace(match: re.Match) -> str:
            var = match.group(1)
            resolved = os.environ.get(var, "").strip()
            if not resolved:
                raise ModelConfigError(
                    f"Model '{model_name}' references ${{{var}}} for {field_name}, "
                    f"but {var} is not set in the environment."
                )
            return resolved

        resolved_value = _ENV_REF.sub(replace, value).strip()
        if not resolved_value:
            raise ModelConfigError(
                f"Model '{model_name}' has an empty {field_name} in {self.path}."
            )
        return resolved_value

    def names(self) -> List[str]:
        """All catalog names, in file order."""
        return list(self._raw_entries.keys())

    def invalidate(self, name: Optional[str] = None) -> None:
        """Invalidate cached ModelSpec entries so they re-resolve from environment."""
        if name:
            self._models.pop(name, None)
        else:
            self._models.clear()

    def list_entries(self) -> List[dict]:
        """Metadata for all catalog entries, suitable for dropdown listing."""
        items = []
        for name, data in self._raw_entries.items():
            configured = True
            missing_vars: List[str] = []

            for field in ("api_key", "base_url"):
                val = str(data.get(field, "")).strip()
                for match in _ENV_REF.finditer(val):
                    var_name = match.group(1)
                    if not os.environ.get(var_name, "").strip():
                        configured = False
                        if var_name not in missing_vars:
                            missing_vars.append(var_name)

            multimodal = bool(data.get("multimodal", False))
            if not multimodal:
                caps = data.get("capabilities") or {}
                inputs = caps.get("input") or []
                if "image" in inputs:
                    multimodal = True

            items.append({
                "name": name,
                "provider": str(data.get("provider", "")).strip(),
                "model": str(data.get("model", "")).strip(),
                "display_name": str(data.get("label", "")).strip() or str(data.get("model", "")).strip() or name,
                "configured": configured,
                "missing_vars": missing_vars,
                "multimodal": multimodal,
            })
        return items

    def specs(self) -> List[ModelSpec]:
        """All resolved specs that can currently be resolved."""
        res: List[ModelSpec] = []
        for name in self.names():
            try:
                res.append(self.get(name))
            except ModelConfigError:
                pass
        return res

    def get(self, name: str) -> ModelSpec:
        """Resolve a model name. Unknown names raise - there is no fallback."""
        key = (name or "").strip()
        if not key:
            raise ModelConfigError(
                "Model name is empty. Set ADK_MODEL in .env."
            )
        if key not in self._raw_entries:
            for entry_name, data in self._raw_entries.items():
                m = str(data.get("model", "")).strip()
                if key == m or (m.startswith("openai/") and key == m[7:]):
                    key = entry_name
                    break
        if key not in self._raw_entries:
            raise ModelConfigError(
                f"Unknown model '{key}'. Available in {self.path.name}: {', '.join(self.names())}."
            )
        if key not in self._models:
            self._models[key] = self._build_spec(key, self._raw_entries[key])
        return self._models[key]


def load_model_spec(name: str, models_file: Optional[Path] = None) -> ModelSpec:
    """Resolve one model name from models.json without importing harness config.

    Used by standalone tools or external scripts that must resolve a model spec
    without depending on the harness configuration module being importable.
    """
    path = models_file
    if path is None:
        raw = os.environ.get("ADK_MODELS_FILE", "").strip()
        path = Path(raw).resolve() if raw else Path(__file__).resolve().parent.parent / "models.json"
    return ModelCatalog(path).get(name)
