#!/usr/bin/env python3
"""Ask an installed MCP companion which tools it advertises, over stdio.

The bridge's ``/tools`` catalog and the companion's MCP surface can disagree:
the companion caches the catalog it fetched at startup and only re-reads it when
the bridge's ``schema_hash`` changes. When that happens it sends
``notifications/tools/list_changed`` and the client re-lists — but a client that
ignores the notification keeps the tool set it saw at connect time, which looks
exactly like "the new tool is missing from the deployment".

This probe answers the deployment question directly, without a client in the
loop: perform the MCP handshake, print the server's advertised capabilities and
its ``tools/list``, and exit.

Usage::

    python scripts/mcp_tool_catalog_probe.py
    python scripts/mcp_tool_catalog_probe.py --exe path/to/nvda-mcp-server.exe --json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_EXE = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Programs"
    / "NVDA MCP Bridge"
    / "bin"
    / "nvda-mcp-server.exe"
)


def probe(exe: Path, timeout: float) -> dict:
    """Return the create/initialize result and the tool list the server reports."""
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "catalog-probe", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    payload = "".join(json.dumps(message) + "\n" for message in requests)

    process = subprocess.Popen(
        [str(exe)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    assert process.stdin is not None
    process.stdin.write(payload)
    process.stdin.flush()

    result: dict = {"capabilities": None, "instructions": None, "tools": [], "server": None}
    deadline = time.time() + timeout
    while time.time() < deadline and not result["tools"]:
        line = process.stdout.readline() if process.stdout else ""
        if not line:
            break
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("id") == 1:
            handshake = message.get("result", {})
            result["capabilities"] = handshake.get("capabilities")
            result["instructions"] = handshake.get("instructions")
            result["server"] = handshake.get("serverInfo")
        elif message.get("id") == 2:
            result["tools"] = [tool["name"] for tool in message["result"]["tools"]]

    process.stdin.close()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--exe", type=Path, default=DEFAULT_EXE, help="companion binary to probe")
    parser.add_argument("--timeout", type=float, default=25.0, help="seconds to wait for a reply")
    parser.add_argument("--json", action="store_true", help="print one JSON object instead of lines")
    args = parser.parse_args(argv)

    if not args.exe.is_file():
        print(f"companion binary not found: {args.exe}", file=sys.stderr)
        return 2

    result = probe(args.exe, args.timeout)
    if not result["tools"]:
        print("the server answered no tools/list — is the bridge running?", file=sys.stderr)

    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result["tools"] else 1

    print(f"binary:      {args.exe}")
    print(f"server:      {result['server']}")
    print(f"instructions: {result['instructions']}")
    print(f"capabilities: {json.dumps(result['capabilities'])}")
    print(f"tool count:   {len(result['tools'])}")
    for name in result["tools"]:
        print(f" - {name}")
    return 0 if result["tools"] else 1


if __name__ == "__main__":
    sys.exit(main())
