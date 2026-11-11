"""Verification suite for no-fallback behavior in harness configuration and models.

Tests that missing or blank required values raise hard errors, never default.
Run: .venv/bin/python tests/test_fallback.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASES = []


def run_case(label, env_overrides, expect_error, needle="", pop_vars=None):
    env = {**os.environ, **env_overrides}
    for v in (pop_vars or []):
        env.pop(v, None)
    py_bin = str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else sys.executable
    r = subprocess.run(
        [
            py_bin,
            "-c",
            "from harness.config import config; print('OK', config.model)",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    is_error = r.returncode != 0
    combined = (r.stdout + r.stderr).strip().splitlines()
    last = combined[-1] if combined else ""
    passed = (is_error == expect_error) and (needle.lower() in last.lower() if needle else True)
    CASES.append((label, passed, last[:110]))
    status = "PASS " if passed else "FAIL "
    print(f"{status} {label} -> {last[:110]}")
    assert passed, f"{label} failed: {last}"


def test_missing_adk_model():
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f_env:
        f_env.write("# Empty env file\n")
        empty_env = f_env.name

    try:
        run_case(
            "missing ADK_MODEL",
            {"ADK_ENV_FILE": empty_env},
            True,
            "ADK_MODEL is required in .env",
            pop_vars=["ADK_MODEL"],
        )
    finally:
        if os.path.exists(empty_env):
            os.remove(empty_env)


def test_blank_adk_model():
    run_case(
        "blank ADK_MODEL",
        {"ADK_MODEL": "   "},
        True,
        "ADK_MODEL is required in .env",
    )


def test_unknown_model_name():
    run_case(
        "unknown model name",
        {"ADK_MODEL": "no-such-model-alias"},
        True,
        "Unknown model",
    )


def test_missing_secret_for_active_model():
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f_cat:
        json.dump(
            {
                "models": {
                    "test-missing-secret": {
                        "provider": "test",
                        "model": "test-llm",
                        "base_url": "http://127.0.0.1:9999",
                        "api_key": "${NONEXISTENT_SECRET_FOR_TEST}",
                        "context_limit": 128000,
                        "transport": "litellm",
                    }
                }
            },
            f_cat,
        )
        cat_file = f_cat.name

    try:
        run_case(
            "missing secret var for active model",
            {"ADK_MODELS_FILE": cat_file, "ADK_MODEL": "test-missing-secret"},
            True,
            "NONEXISTENT_SECRET_FOR_TEST is not set in the environment",
        )
    finally:
        if os.path.exists(cat_file):
            os.remove(cat_file)


def test_missing_models_json():
    run_case(
        "missing models.json",
        {"ADK_MODELS_FILE": "/tmp/nonexistent_models_catalog.json"},
        True,
        "not found",
    )


def test_valid_default_config():
    run_case(
        "valid default config",
        {"ADK_MODEL": "openai-gpt4o", "OPENAI_API_KEY": "mock-test-key"},
        False,
        "OK",
    )


def test_lazy_secret_resolution():
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f_lazy:
        json.dump(
            {
                "models": {
                    "active-model": {
                        "provider": "openai",
                        "model": "test-active",
                        "base_url": "http://127.0.0.1",
                        "api_key": "${OPENAI_API_KEY}",
                        "context_limit": 128000,
                        "transport": "litellm",
                    },
                    "unused-unconfigured-model": {
                        "provider": "anthropic",
                        "model": "claude-test",
                        "base_url": "http://127.0.0.1",
                        "api_key": "${MISSING_UNCONFIGURED_KEY}",
                        "context_limit": 128000,
                        "transport": "litellm",
                    },
                }
            },
            f_lazy,
        )
        lazy_cat = f_lazy.name

    try:
        run_case(
            "lazy resolution of unused model secrets",
            {"ADK_MODELS_FILE": lazy_cat, "ADK_MODEL": "active-model", "OPENAI_API_KEY": "mock-test-key"},
            False,
            "OK active-model",
        )
    finally:
        if os.path.exists(lazy_cat):
            os.remove(lazy_cat)


def main():
    test_missing_adk_model()
    test_blank_adk_model()
    test_unknown_model_name()
    test_missing_secret_for_active_model()
    test_missing_models_json()
    test_valid_default_config()
    test_lazy_secret_resolution()

    print()
    failed = [c for c in CASES if not c[1]]
    total = len(CASES)
    passed = total - len(failed)
    print(f"Results: {passed}/{total} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
