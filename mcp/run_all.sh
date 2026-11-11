#!/usr/bin/env bash
# run_all.sh — Standalone SSE launcher for all discovered local MCP servers in mcp/*/server.py

set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$DIR"

chmod +x run_all.py 2>/dev/null || true
python3 run_all.py "$@"
