#!/usr/bin/env python3
"""Exercise an installed NVDA MCP companion on an idle Windows desktop."""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from uuid import uuid4

from mcp_tool_catalog_probe import DEFAULT_EXE


class SmokeFailure(RuntimeError):
    """A smoke-test assertion or installed companion call failed."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


@contextmanager
def child_process(args: list[str], **kwargs):
    process = subprocess.Popen(args, creationflags=subprocess.CREATE_NO_WINDOW, **kwargs)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()


class MCPClient:
    def __init__(self, process: subprocess.Popen, timeout: float):
        self.process = process
        self.timeout = timeout
        self.request_id = 0
        self.responses: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        try:
            for line in self.process.stdout:
                self.responses.put(line)
        finally:
            self.responses.put(None)

    def _send(self, method: str, params: dict, request_id: int | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            message["id"] = request_id
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict) -> dict:
        self.request_id += 1
        self._send(method, params, self.request_id)
        deadline = time.monotonic() + self.timeout
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                line = self.responses.get(timeout=remaining)
            except queue.Empty:
                break
            if line is None:
                raise SmokeFailure(f"Companion exited while handling {method}")
            response = json.loads(line)
            if response.get("id") != self.request_id:
                continue
            if "error" in response:
                raise SmokeFailure(f"{method}: {response['error']}")
            return response["result"]
        raise SmokeFailure(f"Timed out waiting for {method}")

    def initialize(self) -> None:
        self.request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "nvda-ui-smoke", "version": "1"},
        })
        self._send("notifications/initialized", {})

    def tool(self, name: str, /, **arguments) -> dict:
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        if response.get("isError"):
            raise SmokeFailure(f"{name}: {response.get('content')}")
        payload = response.get("structuredContent")
        require(isinstance(payload, dict), f"{name} returned no structured content")
        return payload


@contextmanager
def dialog_fixture():
    title = f"NVDA MCP smoke {uuid4().hex[:12]}"
    command = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        f"[Windows.Forms.MessageBox]::Show('Temporary smoke test dialog', '{title}', 'OKCancel')"
    )
    with child_process(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        stdout=subprocess.PIPE, text=True,
    ) as process:
        yield title, process


def activate_fixture(client: MCPClient, user32, title: str, process: subprocess.Popen) -> dict:
    deadline = time.monotonic() + client.timeout
    while time.monotonic() < deadline and process.poll() is None:
        handle = user32.FindWindowW(None, title)
        if handle:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
            require(pid.value == process.pid, "Fixture window belongs to another process")
            result = client.tool("activate_window", handle=handle)
            require(result.get("activated") and result.get("handle") == handle, "Fixture activation failed")
            return result
        time.sleep(0.05)
    raise SmokeFailure(f"Fixture did not open: {title}")


def verify_dialog(client: MCPClient, foreground: dict, background: dict, title: str) -> tuple[int, int]:
    deadline = time.monotonic() + client.timeout
    while True:
        observation = client.tool("observe", include=["focus", "dialog"], max_bytes=0)
        if observation.get("focus_window_handle") == foreground["handle"] or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    require(observation.get("foreground_window_handle") == foreground["handle"], "Fixture lost foreground")
    require(observation.get("focus_window_handle") == foreground["handle"], "NVDA focus did not follow")
    dialog = observation.get("dialog") or {}
    require(dialog.get("name") == title, f"Wrong dialog: {dialog.get('name')!r}")
    require({"OK", "Cancel"} <= {child.get("name") for child in dialog.get("children", [])},
            "The dialog did not expose both OK and Cancel")

    arguments = {"name": "Cancel", "role": "button", "exact": True}
    other = client.tool("locate", within=background["ref"], **arguments)
    require(bool(other.get("matches")), "Cancel was not found in the background fixture")
    require((other.get("search_window") or {}).get("handle") == background["handle"],
            "locate did not identify the background search window")
    require((other.get("focus_window") or {}).get("handle") == foreground["handle"],
            "locate did not identify the foreground fixture as NVDA's focused window")

    current = client.tool("locate", within=foreground["ref"], **arguments)
    matches = current.get("matches") or []
    require(len(matches) == 1 and bool(matches[0].get("location")), "Cancel has no unique clickable bounds")
    bounds = matches[0]["location"]
    require(bounds["width"] > 0 and bounds["height"] > 0, "Cancel bounds are empty")
    return bounds["left"] + bounds["width"] // 2, bounds["top"] + bounds["height"] // 2


def run_smoke(exe: Path, timeout: float) -> None:
    user32 = ctypes.WinDLL("user32")
    user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    user32.FindWindowW.restype = wintypes.HWND
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.WindowFromPhysicalPoint.argtypes = (wintypes.POINT,)
    user32.WindowFromPhysicalPoint.restype = wintypes.HWND
    user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
    user32.GetAncestor.restype = wintypes.HWND
    user32.SwapMouseButton.argtypes = (wintypes.BOOL,)
    user32.SwapMouseButton.restype = wintypes.BOOL
    original_swap = bool(user32.GetSystemMetrics(23))  # SM_SWAPBUTTON

    with child_process(
        [str(exe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
    ) as companion:
        client = MCPClient(companion, timeout)
        client.initialize()
        for name, field in (("click_at", "pointer_on_target"), ("locate", "search_window")):
            schema = client.tool("get_tool_schema", name=name, include_output_schema=True)
            require(field in (schema.get("output_schema") or {}).get("properties", {}),
                    f"Installed {name} schema is missing {field}; install the updated add-on and companion")

        try:
            with dialog_fixture() as (background_title, background_process):
                background = activate_fixture(client, user32, background_title, background_process)
                for swapped in (False, True):
                    user32.SwapMouseButton(swapped)
                    require(bool(user32.GetSystemMetrics(23)) == swapped, "Windows did not change button mapping")
                    with dialog_fixture() as (title, process):
                        foreground = activate_fixture(client, user32, title, process)
                        x, y = verify_dialog(client, foreground, background, title)
                        require(user32.GetForegroundWindow() == foreground["handle"], "Fixture lost foreground before click")
                        hit = user32.WindowFromPhysicalPoint(wintypes.POINT(x, y))
                        require(hit and user32.GetAncestor(hit, 2) == foreground["handle"],  # GA_ROOT
                                "Another window is covering the click target")
                        click = client.tool("click_at", x=x, y=y)
                        require(click.get("pointer_on_target") is True and click.get("on_target") is True,
                                "The pointer did not reach Cancel")
                        output, _ = process.communicate(timeout=timeout)
                        require(process.returncode == 0 and output.strip() == "Cancel",
                                f"The dialog did not return Cancel: {output.strip()!r}")
                        observation = client.tool("observe", include=["focus"])
                        require(observation.get("foreground_window_handle") != foreground["handle"],
                                "NVDA still observed the closed fixture")
                    print(f"PASS {'swapped' if swapped else 'standard'} mouse buttons")
        finally:
            user32.SwapMouseButton(original_swap)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, default=DEFAULT_EXE, help="installed companion executable")
    parser.add_argument("--timeout", type=float, default=15, help="timeout in seconds for each operation")
    args = parser.parse_args()
    try:
        require(sys.platform == "win32", "The UI smoke test requires Windows")
        require(args.exe.is_file(), f"Companion binary not found: {args.exe}")
        require(args.timeout > 0, "--timeout must be positive")
        run_smoke(args.exe.resolve(), args.timeout)
    except (SmokeFailure, OSError, subprocess.TimeoutExpired) as error:
        print(f"FAIL {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
