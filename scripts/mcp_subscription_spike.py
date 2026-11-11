"""Feasibility check for the event-delivery design, spoken over real stdio.

Spawns the companion as an MCP client would and exercises, in order:

  server/discover              -> does it advertise the modern protocol?
  resources/list               -> is nvda://ui/state exposed?
  resources/read               -> does the bridge /state round-trip?
  subscriptions/listen         -> ack, then a resources/updated notification when
                                  the UI actually changes

It talks raw JSON-RPC so the result does not depend on any MCP client support.

Usage: python scripts/mcp_subscription_spike.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(r"D:\nvda-addons\nvda-mcp")
_BINARY_CANDIDATES = [
    REPO / "rust_mcp_bridge" / "target" / "x86_64-pc-windows-msvc" / "release" / "nvda-mcp-server.exe",
    REPO / "rust_mcp_bridge" / "target" / "release" / "nvda-mcp-server.exe",
]
BINARY = next((path for path in _BINARY_CANDIDATES if path.is_file()), _BINARY_CANDIDATES[0])
BRIDGE = "http://127.0.0.1:8765"
STATE_URI = "nvda://ui/state"
PROTOCOL = "2026-07-28"


def bridge_token() -> str:
    path = Path(os.environ["APPDATA"]) / "nvda" / "nvda-mcp-bridge" / "token.key"
    return path.read_text(encoding="utf-8").strip()


def bridge_call(action: str, params: dict | None = None) -> dict:
    payload = json.dumps({"action": action, "params": params or {}}).encode()
    request = urllib.request.Request(f"{BRIDGE}/action", data=payload, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("X-NVDA-Bridge-Token", bridge_token())
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


class StdioClient:
    def __init__(self, process: subprocess.Popen):
        self.process = process
        self._next_id = 0
        self._lock = threading.Lock()
        self.inbox: list[dict] = []
        self.wake = threading.Event()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.inbox.append(message)
            self.wake.set()

    def send(self, message: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def request(self, method: str, params: dict, timeout: float = 10.0) -> dict:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
        self.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": PROTOCOL,
                        "io.modelcontextprotocol/clientInfo": {"name": "spike", "version": "1"},
                        "io.modelcontextprotocol/clientCapabilities": {},
                    },
                    **params,
                },
            }
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for message in list(self.inbox):
                if message.get("id") == request_id:
                    return message
            time.sleep(0.02)
        raise TimeoutError(f"no response to {method}")

    def wait_for_notification(self, method: str, timeout: float) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for message in list(self.inbox):
                if message.get("method") == method:
                    return message
            time.sleep(0.02)
        return None

    def close(self) -> None:
        try:
            self.process.terminate()
            self.process.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self.process.kill()


def main() -> int:
    if not BINARY.is_file():
        print(f"binary missing: {BINARY} (run cargo build --release)")
        return 2

    env = dict(os.environ)
    env["NVDA_BRIDGE_URL"] = BRIDGE
    env["LOG_LEVEL"] = "warn"

    print(f"spawning {BINARY.name}")
    process = subprocess.Popen(
        [str(BINARY)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    client = StdioClient(process)
    failures = 0

    try:
        # 1. Discovery: a modern server must answer server/discover.
        discover = client.request("server/discover", {})
        result = discover.get("result") or {}
        versions = result.get("supportedVersions") or result.get("supportedProtocolVersions") or []
        print(f"server/discover -> protocols={versions}")
        modern_ok = PROTOCOL in versions
        print(f"  modern protocol advertised: {modern_ok}")
        failures += 0 if modern_ok else 1

        # 2. Resources are advertised and listable.
        listing = client.request("resources/list", {})
        resources = (listing.get("result") or {}).get("resources") or []
        uris = [resource.get("uri") for resource in resources]
        print(f"resources/list -> {uris}")
        failures += 0 if STATE_URI in uris else 1

        # 3. Reading the resource round-trips through the bridge.
        read = client.request("resources/read", {"uri": STATE_URI})
        contents = (read.get("result") or {}).get("contents") or []
        text = contents[0].get("text", "") if contents else ""
        focus_name = ""
        try:
            focus_name = json.loads(text).get("focus", {}).get("name", "")
        except Exception:  # noqa: BLE001
            pass
        print(f"resources/read -> {len(text)} bytes, focus={focus_name[:48]!r}")
        failures += 0 if focus_name else 1

        # 4. Subscribe, then cause a real UI change and wait for the signal.
        with client._lock:
            client._next_id += 1
            listen_id = client._next_id
        client.send(
            {
                "jsonrpc": "2.0",
                "id": listen_id,
                "method": "subscriptions/listen",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": PROTOCOL,
                        "io.modelcontextprotocol/clientInfo": {"name": "spike", "version": "1"},
                        "io.modelcontextprotocol/clientCapabilities": {},
                    },
                    "notifications": {"resourceSubscriptions": [STATE_URI]},
                },
            }
        )

        ack = client.wait_for_notification("notifications/subscriptions/acknowledged", 5.0)
        acknowledged = (ack or {}).get("params", {}).get("notifications") or {}
        print(f"subscriptions/listen ack -> {json.dumps(acknowledged)}")
        failures += 0 if acknowledged.get("resourceSubscriptions") else 1

        print("causing a focus change via the bridge...")
        started = time.monotonic()
        bridge_call("send_keyboard", {"gesture": "tab"})
        time.sleep(0.3)
        bridge_call("send_keyboard", {"gesture": "shift+tab"})

        notification = client.wait_for_notification("notifications/resources/updated", 8.0)
        if notification:
            latency = (time.monotonic() - started) * 1000
            params = notification.get("params") or {}
            meta = params.get("_meta") or {}
            print(
                f"notifications/resources/updated -> uri={params.get('uri')} "
                f"subscriptionId={meta.get('io.modelcontextprotocol/subscriptionId')} "
                f"after {latency:.0f}ms"
            )
        else:
            print("notifications/resources/updated -> NONE (subscription did not fire)")
            failures += 1

        # 5. Cancellation should end the stream cleanly.
        client.send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": listen_id, "reason": "spike complete"},
            }
        )
        deadline = time.monotonic() + 3.0
        ended = False
        while time.monotonic() < deadline and not ended:
            ended = any(
                message.get("id") == listen_id and "result" in message
                for message in client.inbox
            )
            time.sleep(0.05)
        # Cancellation teardown depends on the negotiated peer lifecycle, and this
        # client deliberately skips the handshake (an agent may do the same), so it
        # is reported rather than asserted.
        print(f"subscription closed gracefully on cancel: {ended} (informational)")

    finally:
        client.close()

    print(f"\nresult: {'PASS' if failures == 0 else f'{failures} check(s) failed'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
