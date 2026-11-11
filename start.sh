#!/usr/bin/env bash

set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ADK_ENV_FILE:-$ROOT_DIR/.env}"
cd "$ROOT_DIR"

AGENT_PID=""
MCP_PID=""
SHUTTING_DOWN=0

die() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

dotenv_value() {
  local key="$1"
  python3 - "$ENV_FILE" "$key" <<'PY'
import sys
path, target = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == target:
                print(v.strip().strip('"').strip("'"), end="")
                break
except FileNotFoundError:
    pass
PY
}

port_available() {
  python3 - "$1" <<'PY'
import socket, sys
port = int(sys.argv[1])
for family, addr in ((socket.AF_INET, "127.0.0.1"),):
    s = socket.socket(family, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((addr, port))
    except OSError:
        sys.exit(1)
    finally:
        s.close()
sys.exit(0)
PY
}

free_port() {
  local port="$1"
  if ! port_available "$port"; then
    printf '[Setup] Port %s is busy. Releasing process holding it...\n' "$port"
    if command -v fuser >/dev/null 2>&1; then
      fuser -k "${port}/tcp" >/dev/null 2>&1 || true
    elif command -v lsof >/dev/null 2>&1; then
      local pids
      pids=$(lsof -ti :"$port" 2>/dev/null || true)
      [[ -n "$pids" ]] && kill -9 $pids 2>/dev/null || true
    fi
    sleep 0.5
  fi
}

wait_for_http() {
  local url="$1"
  local label="$2"
  local pid="$3"

  "$ROOT_DIR/.venv/bin/python" - "$url" "$label" "$pid" <<'PY'
import base64, os, sys, time, urllib.error, urllib.request

url, label, pid = sys.argv[1], sys.argv[2], int(sys.argv[3])
username = os.environ.get("AGENT_SERVER_USERNAME", "operator")
password = os.environ.get("AGENT_SERVER_PASSWORD", "")
headers = {}
if password:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    headers["Authorization"] = f"Basic {token}"

for _ in range(40):
    try:
        os.kill(pid, 0)
    except OSError:
        print(f"{label} process exited prematurely.", file=sys.stderr)
        sys.exit(1)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            if 200 <= resp.status < 400:
                print(f"[Ready] {label} is online ({url})")
                sys.exit(0)
    except urllib.error.HTTPError as e:
        if "sse" in url and e.code in (200, 400, 405):
            print(f"[Ready] {label} is online ({url})")
            sys.exit(0)
    except Exception:
        pass
    time.sleep(0.4)

print(f"{label} did not become ready in time.", file=sys.stderr)
sys.exit(1)
PY
}

stop_proc() {
  local pid="$1"
  [[ -n "$pid" ]] || return 0
  if command -v setsid >/dev/null 2>&1; then
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  else
    kill -TERM "$pid" 2>/dev/null || true
  fi
}

cleanup() {
  local exit_code=$?
  (( SHUTTING_DOWN )) && return
  SHUTTING_DOWN=1
  trap - EXIT INT TERM HUP

  printf '\nStopping ADK Agent Harness...\n'
  stop_proc "$AGENT_PID"
  stop_proc "$MCP_PID"

  for _ in {1..20}; do
    local running=0
    for pid in "$AGENT_PID" "$MCP_PID"; do
      [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null && running=1
    done
    (( running == 0 )) && break
    sleep 0.2
  done

  for pid in "$AGENT_PID" "$MCP_PID"; do
    [[ -n "$pid" ]] || continue
    kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  done

  printf 'ADK Agent Harness stopped.\n'
  exit "$exit_code"
}

[[ -f "$ENV_FILE" ]] || die "Missing $ENV_FILE. Run: cp .env.example .env and set your API key."
command -v python3 >/dev/null 2>&1 || die "Python 3.11+ is required."

# Auto-detect uv if installed
if ! command -v uv >/dev/null 2>&1; then
  for candidate in "$HOME/.cargo/bin" "$HOME/.local/bin" "/usr/local/bin"; do
    if [[ -x "$candidate/uv" ]]; then
      export PATH="$candidate:$PATH"
      break
    fi
  done
fi

# 1. Provision root Python virtual environment if missing
if [[ ! -x "$ROOT_DIR/.venv/bin/python" ]]; then
  printf '[Setup] Creating Python virtual environment in .venv...\n'
  if command -v uv >/dev/null 2>&1; then
    uv venv "$ROOT_DIR/.venv"
    uv pip install -r "$ROOT_DIR/requirements.txt" --python "$ROOT_DIR/.venv/bin/python"
  else
    python3 -m venv "$ROOT_DIR/.venv"
    "$ROOT_DIR/.venv/bin/pip" install --upgrade pip -q
    "$ROOT_DIR/.venv/bin/pip" install -r "$ROOT_DIR/requirements.txt" -q
  fi
fi

# 2. Sync any local MCP server virtual environments if uv is available
if command -v uv >/dev/null 2>&1; then
  for pyproject in "$ROOT_DIR"/mcp/*/pyproject.toml; do
    if [[ -f "$pyproject" ]]; then
      mcp_dir="$(dirname -- "$pyproject")"
      if [[ ! -d "$mcp_dir/.venv" ]]; then
        mcp_name="$(basename -- "$mcp_dir")"
        printf '[Setup] Syncing local MCP server (%s)...\n' "$mcp_name"
        uv sync --directory "$mcp_dir" --quiet || true
      fi
    fi
  done
fi

# Export all variables from .env to environment so child processes inherit all provider API keys
if [[ -f "$ENV_FILE" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    if [[ -n "$line" && ! "$line" =~ ^# && "$line" =~ = ]]; then
      k="${line%%=*}"
      k="${k// /}"
      v="${line#*=}"
      v="${v%\"}"
      v="${v#\"}"
      v="${v%\'}"
      v="${v#\'}"
      export "$k=$v"
    fi
  done < "$ENV_FILE"
fi

AGENT_HOST="${AGENT_HOST:-$(dotenv_value AGENT_HOST)}"
AGENT_HOST="${AGENT_HOST:-127.0.0.1}"
AGENT_PORT="${AGENT_PORT:-$(dotenv_value AGENT_PORT)}"
AGENT_PORT="${AGENT_PORT:-4096}"
MCP_HOST="${MCP_HOST:-$(dotenv_value MCP_HOST)}"
MCP_HOST="${MCP_HOST:-127.0.0.1}"
MCP_BASE_PORT="${MCP_BASE_PORT:-$(dotenv_value MCP_BASE_PORT)}"
MCP_BASE_PORT="${MCP_BASE_PORT:-8101}"
ADK_MODEL="${ADK_MODEL:-$(dotenv_value ADK_MODEL)}"
ADK_ENABLE_WEB_UI="${ADK_ENABLE_WEB_UI:-$(dotenv_value ADK_ENABLE_WEB_UI)}"
ADK_ENABLE_WEB_UI="${ADK_ENABLE_WEB_UI:-true}"
export ADK_ENABLE_WEB_UI

AGENT_SERVER_USERNAME="${AGENT_SERVER_USERNAME:-$(dotenv_value AGENT_SERVER_USERNAME)}"
AGENT_SERVER_PASSWORD="${AGENT_SERVER_PASSWORD:-$(dotenv_value AGENT_SERVER_PASSWORD)}"
[[ -n "$AGENT_SERVER_USERNAME" ]] && export AGENT_SERVER_USERNAME
[[ -n "$AGENT_SERVER_PASSWORD" ]] && export AGENT_SERVER_PASSWORD

export AGENT_HOST AGENT_PORT MCP_HOST MCP_BASE_PORT

mkdir -p "$ROOT_DIR/workspace" "$ROOT_DIR/artifacts" "$ROOT_DIR/tmp"

free_port "$AGENT_PORT"
free_port "$MCP_BASE_PORT"

printf '\n======================================================\n'
printf '  ADK Headless Agent Harness\n'
printf '======================================================\n'
printf '  Workspace:     %s\n' "$ROOT_DIR"
printf '  API Endpoint:  http://%s:%s\n' "$AGENT_HOST" "$AGENT_PORT"
if [[ "$ADK_ENABLE_WEB_UI" =~ ^(true|1|yes)$ ]]; then
  printf '  Web Chat UI:   http://%s:%s/\n' "$AGENT_HOST" "$AGENT_PORT"
else
  printf '  Web Chat UI:   Disabled (headless mode)\n'
fi
printf '  Active Model:  %s\n' "${ADK_MODEL:-(from .env)}"
printf '  Local MCPs:    SSE starting at http://%s:%s/sse\n' "$MCP_HOST" "$MCP_BASE_PORT"
printf '======================================================\n\n'

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

if command -v setsid >/dev/null 2>&1; then
  setsid "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/mcp/run_all.py" --host "$MCP_HOST" --base-port "$MCP_BASE_PORT" &
else
  "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/mcp/run_all.py" --host "$MCP_HOST" --base-port "$MCP_BASE_PORT" &
fi
MCP_PID=$!
wait_for_http "http://$MCP_HOST:$MCP_BASE_PORT/sse" "Local MCP Runner" "$MCP_PID"

if command -v setsid >/dev/null 2>&1; then
  setsid "$ROOT_DIR/.venv/bin/python" -m uvicorn harness.server:app --host "$AGENT_HOST" --port "$AGENT_PORT" &
else
  "$ROOT_DIR/.venv/bin/python" -m uvicorn harness.server:app --host "$AGENT_HOST" --port "$AGENT_PORT" &
fi
AGENT_PID=$!
wait_for_http "http://$AGENT_HOST:$AGENT_PORT/api/info" "ADK Agent Harness" "$AGENT_PID"

printf '\nHarness is live. Press Ctrl+C to stop.\n'

set +e
if [[ "${BASH_VERSINFO[0]}" -gt 4 || ( "${BASH_VERSINFO[0]}" -eq 4 && "${BASH_VERSINFO[1]}" -ge 3 ) ]]; then
  wait -n "$MCP_PID" "$AGENT_PID"
  service_status=$?
else
  wait "$AGENT_PID" "$MCP_PID"
  service_status=$?
fi
set -e

exit "$service_status"
