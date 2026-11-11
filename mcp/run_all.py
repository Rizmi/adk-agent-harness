#!/usr/bin/env python3
"""Unified Local MCP Server Runner.

Automatically discovers all local MCP server packages inside ``mcp/*/server.py``
and runs them concurrently over HTTP/SSE, or exports ready-to-use client
configuration files for external MCP clients.

Usage:
    python3 mcp/run_all.py                  # Start all discovered local MCP servers on SSE
    python3 mcp/run_all.py --host 0.0.0.0   # Bind to all interfaces
    python3 mcp/run_all.py --export-config  # Generate mcp_config_stdio.json and mcp_config_sse.json
    python3 mcp/run_all.py --status         # Check health of running SSE servers
"""

import argparse
import ast
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from subprocess import Popen
from typing import Dict, List

MCP_DIR = Path(__file__).resolve().parent
ROOT_DIR = MCP_DIR.parent

BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RED = "\033[31m"
RESET = "\033[0m"
DIM = "\033[2m"


def discover_local_servers(base_port: int = 8101) -> List[Dict]:
    """Auto-discover local MCP servers in mcp/*/server.py."""
    servers: List[Dict] = []
    idx = 0
    for srv_dir in sorted(MCP_DIR.iterdir()):
        if not srv_dir.is_dir() or srv_dir.name.startswith((".", "__")):
            continue
        srv_file = srv_dir / "server.py"
        if not srv_file.exists():
            continue

        name = srv_dir.name.lower()
        port_env = os.environ.get(f"MCP_PORT_{name.upper()}", "").strip()
        port = int(port_env) if port_env.isdigit() else (base_port + idx)
        idx += 1

        desc = f"Local MCP server ({name})"
        try:
            tree = ast.parse(srv_file.read_text(encoding="utf-8"))
            doc = ast.get_docstring(tree)
            if doc:
                desc = doc.strip().splitlines()[0].strip()
        except Exception:
            pass

        servers.append({
            "name": name,
            "dir": srv_dir.name,
            "port": port,
            "desc": desc,
        })
    return servers


def get_server_python(sub_dir: str) -> Path:
    """Resolve virtual environment Python interpreter for a server or root venv."""
    sub_venv = MCP_DIR / sub_dir / ".venv" / "bin" / "python"
    if sub_venv.exists():
        return sub_venv
    root_venv = ROOT_DIR / ".venv" / "bin" / "python"
    if root_venv.exists():
        return root_venv
    return Path(sys.executable)


def generate_configs(servers: List[Dict], host: str = "127.0.0.1") -> None:
    """Generate ready-to-use configuration files for external MCP clients."""
    stdio_config = {"mcpServers": {}}
    sse_config = {"mcpServers": {}}

    for s in servers:
        s_dir = MCP_DIR / s["dir"]
        py_bin = get_server_python(s["dir"])
        stdio_config["mcpServers"][s["name"]] = {
            "command": str(py_bin),
            "args": ["server.py", "--transport", "stdio"],
            "cwd": str(s_dir),
            "description": s["desc"],
        }
        sse_config["mcpServers"][s["name"]] = {
            "url": f"http://{host}:{s['port']}/sse",
            "type": "sse",
            "description": s["desc"],
        }

    stdio_file = MCP_DIR / "mcp_config_stdio.json"
    sse_file = MCP_DIR / "mcp_config_sse.json"

    stdio_file.write_text(json.dumps(stdio_config, indent=2) + "\n", encoding="utf-8")
    sse_file.write_text(json.dumps(sse_config, indent=2) + "\n", encoding="utf-8")

    print(f"{GREEN}✓{RESET} Generated client configurations for host '{host}':")
    print(f"  • Stdio mode: {CYAN}{stdio_file}{RESET}")
    print(f"  • SSE mode:   {CYAN}{sse_file}{RESET}\n")


def check_health(host: str, port: int, timeout: float = 1.0) -> bool:
    """Check if an MCP server's SSE endpoint is responding."""
    url = f"http://{host}:{port}/sse"
    req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status in (200, 204)
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def status_cmd(servers: List[Dict], host: str) -> None:
    """Check health of all discovered local servers."""
    print(f"\n{BOLD}Local MCP Servers Status ({len(servers)} discovered, Host: {host}){RESET}\n")
    print(f"{'Server':<16} {'Port':<8} {'SSE Endpoint':<34} {'Health':<10}")
    print("-" * 70)
    for s in servers:
        endpoint = f"http://{host}:{s['port']}/sse"
        alive = check_health(host, s["port"])
        status_str = f"{GREEN}ONLINE{RESET}" if alive else f"{RED}OFFLINE{RESET}"
        print(f"{s['name']:<16} {s['port']:<8} {endpoint:<34} {status_str:<10}")
    print("")


def run_all(servers: List[Dict], host: str = "127.0.0.1") -> None:
    """Launch and monitor all discovered local MCP servers concurrently."""
    if not servers:
        print(f"{YELLOW}No local MCP servers discovered in {MCP_DIR}.{RESET}")
        return

    generate_configs(servers, host)

    print(f"{BOLD}{CYAN}══════════════════════════════════════════════════════════════════════{RESET}")
    print(f"{BOLD}  ADK Agent Harness — Local MCP Runner ({len(servers)} Server(s)){RESET}")
    print(f"{BOLD}{CYAN}══════════════════════════════════════════════════════════════════════{RESET}\n")

    processes: List[tuple[Dict, Popen]] = []

    def cleanup(signum=None, frame=None):
        print(f"\n\n{YELLOW}Stopping all local MCP servers...{RESET}")
        for _, proc in processes:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        time.sleep(0.5)
        for _, proc in processes:
            if proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
        print(f"{GREEN}✓ All MCP servers stopped cleanly.{RESET}")
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    for s in servers:
        s_dir = MCP_DIR / s["dir"]
        py_bin = get_server_python(s["dir"])
        cmd = [
            str(py_bin),
            "server.py",
            "--transport",
            "sse",
            "--host",
            host,
            "--port",
            str(s["port"]),
        ]
        proc = Popen(
            cmd,
            cwd=str(s_dir),
            env=os.environ.copy(),
            stdout=open(s_dir / "server.log", "w", encoding="utf-8"),
            stderr=open(s_dir / "server.err.log", "w", encoding="utf-8"),
        )
        processes.append((s, proc))

    ports = ", ".join(str(s["port"]) for s in servers)
    print(f"{DIM}Waiting for {len(servers)} server(s) to initialize on port(s) {ports}...{RESET}")
    for _ in range(15):
        all_ready = True
        for s, proc in processes:
            if proc.poll() is not None:
                all_ready = True
                break
            if not check_health(host, s["port"], timeout=0.2):
                all_ready = False
        if all_ready:
            break
        time.sleep(0.4)

    print(f"\n{'Server':<16} {'Port':<8} {'SSE Endpoint':<34} {'Status':<10}")
    print(f"{'─'*16} {'─'*8} {'─'*34} {'─'*10}")

    for s, proc in processes:
        endpoint = f"http://{host}:{s['port']}/sse"
        if proc.poll() is not None:
            status_str = f"{RED}FAILED (exit {proc.returncode}){RESET}"
        else:
            alive = check_health(host, s["port"], timeout=0.8)
            status_str = f"{GREEN}ONLINE{RESET}" if alive else f"{YELLOW}STARTING{RESET}"
        print(f"{s['name']:<16} {s['port']:<8} {endpoint:<34} {status_str}")

    print(f"\n{BOLD}Managing {len(servers)} MCP server(s).{RESET} {DIM}Press Ctrl+C to stop.{RESET}\n")

    try:
        while True:
            time.sleep(2.0)
            for s, proc in processes:
                if proc.poll() is not None:
                    print(f"{RED}[ALERT] MCP server '{s['name']}' exited with code {proc.returncode}.{RESET}")
                    print(f"       Check logs at: {MCP_DIR / s['dir'] / 'server.err.log'}")
                    sys.exit(proc.returncode or 1)
    except KeyboardInterrupt:
        cleanup()


def main():
    parser = argparse.ArgumentParser(description="ADK Agent Harness Local MCP Runner")
    parser.add_argument("--host", default=os.environ.get("MCP_HOST", "127.0.0.1"), help="Host interface to bind")
    parser.add_argument("--base-port", type=int, default=int(os.environ.get("MCP_BASE_PORT", "8101")), help="Starting port")
    parser.add_argument("--export-config", action="store_true", help="Generate MCP client config files and exit")
    parser.add_argument("--status", action="store_true", help="Check health of running servers and exit")
    args = parser.parse_args()

    servers = discover_local_servers(base_port=args.base_port)

    if args.export_config:
        generate_configs(servers, args.host)
        return

    if args.status:
        status_cmd(servers, args.host)
        return

    run_all(servers, args.host)


if __name__ == "__main__":
    main()
