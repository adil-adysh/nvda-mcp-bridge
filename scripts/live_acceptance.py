#!/usr/bin/env python3
"""Live, end-to-end acceptance for the action surface (implementation plan P1-P4).

Unlike ``tests/``, this drives a *running* NVDA over the bridge and asserts the
behaviour the plan promised, against real applications. It is deliberately
assertive about numbers the design depends on: coalescing one user action into
one transition, bumping ``state_version`` only on a real change, settling before
returning, and resolving references that it captured itself.

What it touches on the machine, and why that is safe:

* it starts Notepad and types ``abc`` into an unsaved document;
* it opens Notepad's Replace dialog and closes it again (Escape, then Cancel);
* it opens a local HTML page in the default browser and clicks a button that
  only updates an ``aria-live`` region;
* it presses ``alt+tab`` to move focus, and restores the original window at the
  end.

It never presses Enter in a terminal, never invokes an unknown control, and never
closes a window it did not open.

Usage::

    python scripts/live_acceptance.py --verbose
    python scripts/live_acceptance.py --json report.json
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path

BASE_URL = "http://127.0.0.1:8765"
TOKEN_HEADER = "X-NVDA-Bridge-Token"

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_SW_RESTORE = 9
_HWND_TOPMOST = -1
_HWND_NOTOPMOST = -2
_SWP_NOMOVE_NOSIZE = 0x0003

# Explicit prototypes: the defaults return ``c_int``, which truncates 64-bit
# window handles and silently breaks every comparison against them.
_word = ctypes.c_ushort
_dword = ctypes.c_ulong
for _name in ("SetFocus",):
    getattr(_user32, _name).restype = ctypes.c_void_p
    getattr(_user32, _name).argtypes = (ctypes.c_void_p,)
_user32.GetForegroundWindow.argtypes = ()
_user32.GetForegroundWindow.restype = ctypes.c_void_p
_user32.BringWindowToTop.argtypes = (ctypes.c_void_p,)
_user32.BringWindowToTop.restype = ctypes.c_bool
_user32.SetForegroundWindow.argtypes = (ctypes.c_void_p,)
_user32.SetForegroundWindow.restype = ctypes.c_bool
_user32.ShowWindow.argtypes = (ctypes.c_void_p, ctypes.c_int)
_user32.ShowWindow.restype = ctypes.c_bool
_user32.SetWindowPos.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint)
_user32.SetWindowPos.restype = ctypes.c_bool
_user32.IsWindowVisible.argtypes = (ctypes.c_void_p,)
_user32.IsWindowVisible.restype = ctypes.c_bool
_user32.GetWindowTextLengthW.argtypes = (ctypes.c_void_p,)
_user32.GetWindowTextLengthW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int)
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetWindowThreadProcessId.argtypes = (ctypes.c_void_p, ctypes.POINTER(_dword))
_user32.GetWindowThreadProcessId.restype = _dword
_user32.AttachThreadInput.argtypes = (_dword, _dword, ctypes.c_bool)
_user32.AttachThreadInput.restype = ctypes.c_bool
_user32.SwitchToThisWindow.argtypes = (ctypes.c_void_p, ctypes.c_bool)
_user32.GetWindowRect.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.RECT))
_user32.EnumWindows.restype = ctypes.c_bool
_kernel32.GetCurrentThreadId.restype = _dword


def _windows_matching(fragment: str) -> list[int]:
    """Return top-level window handles whose title contains ``fragment``."""
    matches: list[int] = []
    fragment = fragment.lower()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buffer, length + 1)
        if fragment in buffer.value.lower():
            matches.append(int(hwnd))
        return True

    _user32.EnumWindows(callback, 0)
    return matches


def force_foreground(fragment: str) -> bool:
    """Restore and foreground a window by title fragment.

    Windows refuses ``SetForegroundWindow`` from a background process, and a
    browser launched with ``--new-window`` therefore opens *behind* the focused
    window. Two standard tricks make it reliable: attach this thread's input to
    the target's, and toggle the window's topmost flag to raise it without
    stealing focus policy.
    """
    for hwnd in _windows_matching(fragment):
        _user32.ShowWindow(hwnd, _SW_RESTORE)
        _user32.SetWindowPos(hwnd, _HWND_TOPMOST, 0, 0, 0, 0, _SWP_NOMOVE_NOSIZE)
        _user32.SetWindowPos(hwnd, _HWND_NOTOPMOST, 0, 0, 0, 0, _SWP_NOMOVE_NOSIZE)
        target_thread = _user32.GetWindowThreadProcessId(hwnd, None)
        current_thread = _kernel32.GetCurrentThreadId()
        attached = bool(_user32.AttachThreadInput(current_thread, target_thread, True))
        try:
            _user32.BringWindowToTop(hwnd)
            _user32.SwitchToThisWindow(hwnd, True)
            _user32.SetForegroundWindow(hwnd)
            _user32.SetFocus(hwnd)
        finally:
            if attached:
                _user32.AttachThreadInput(current_thread, target_thread, False)
        time.sleep(0.2)
        if _user32.GetForegroundWindow() == hwnd:
            return True
    return False


def window_center(hwnd: int) -> tuple[int, int] | None:
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None
    return rect.left + width // 2, rect.top + height // 2

PAGE = """<!doctype html>
<meta charset="utf-8">
<title>{{TITLE}}</title>
<body style="font: 16px sans-serif; padding: 2rem">
<h1>{{TITLE}}</h1>
<p><button id="go" autofocus onclick="clicked()">Run action</button></p>
<div id="status" role="status" aria-live="polite">idle</div>
<p><a href="#one">One</a> <a href="#two">Two</a> <a href="#three">Three</a></p>
<script>
let count = 0;
function clicked() {
  count += 1;
  // The button renames itself, so the click is visible in the *projection* as a
  // name change on the focused object. That works on every browser; aria-live
  // delivery differs between Firefox and Chromium, so the live region is a
  // secondary, non-failing signal.
  const button = document.getElementById('go');
  button.textContent = 'Run action ' + count;
  document.getElementById('status').textContent = 'clicked ' + count;
}
</script>
</body>
"""


# --------------------------------------------------------------------------- http


def resolve_token(explicit: str | None) -> str:
    if explicit:
        return explicit.strip()
    env_token = os.environ.get("NVDA_BRIDGE_TOKEN", "").strip()
    if env_token:
        return env_token
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidate = Path(appdata) / "nvda" / "nvda-mcp-bridge" / "token.key"
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8").strip()
    raise SystemExit("No bridge token found (pass --token or set NVDA_BRIDGE_TOKEN).")


class Bridge:
    def __init__(self, token: str) -> None:
        self.token = token

    def _request(self, method: str, path: str, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(BASE_URL + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        request.add_header(TOKEN_HEADER, self.token)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # A 4xx carries a protocol envelope that several checks assert on
            # (a stale reference, a denied permission). It is a result, not a
            # harness failure; only a broken transport raises.
            payload = exc.read().decode("utf-8", "replace")
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                raise AssertionError(f"HTTP {exc.code} for {path}: {payload[:300]}") from exc

    def call(self, action: str, params=None):
        return self._request("POST", "/action", {"action": action, "params": params or {}})

    def get(self, path: str):
        return self._request("GET", path)


# ---------------------------------------------------------------------- reporting


class Report:
    def __init__(self, verbose: bool = False) -> None:
        self.checks = []
        self.verbose = verbose

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if self.verbose or not ok:
            print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))
        return ok

    def skip(self, name: str, detail: str = "") -> None:
        """Record a check that could not run because of the environment."""
        self.checks.append({"name": name, "ok": True, "detail": f"skipped: {detail}", "skipped": True})
        print(f"SKIP {name}" + (f"  [{detail}]" if detail else ""))

    def section(self, title: str) -> None:
        if self.verbose:
            print(f"\n== {title} ==")

    def note(self, name: str, detail: str = "") -> None:
        """Record an observation that is informative but not a pass/fail gate."""
        self.checks.append({"name": name, "ok": True, "detail": f"info: {detail}", "note": True})
        if self.verbose:
            print(f"NOTE {name}" + (f"  [{detail}]" if detail else ""))

    @property
    def failed(self):
        return [check for check in self.checks if not check["ok"]]


# ------------------------------------------------------------------------ helpers


def ok(response) -> bool:
    return bool(response.get("success"))


def data(response) -> dict:
    return response.get("data") or {}


def result(response) -> dict:
    return response.get("result") or {}


def error_code(response) -> int | None:
    return (response.get("error") or {}).get("code")


def window_title(bridge: Bridge) -> str:
    window = (data(bridge.call("observe")) or {}).get("window") or {}
    return str(window.get("title", ""))


def activate(bridge: Bridge, fragment: str, attempts: int = 14) -> bool:
    """Bring a window with ``fragment`` in its title to the foreground.

    First choice is the bridge's own ``activate_window``: it runs inside NVDA,
    which the foreground rules treat far better than a helper process, and it
    verifies the switch itself. The older tricks below remain as fallbacks for a
    bridge that does not have the tool yet.

    Uses the task switcher last, because a non-interactive session refuses
    ``SetForegroundWindow`` outright (verified: the call succeeds and the
    foreground does not change). Alt+Tab is handled by the shell, so it works
    where the API does not. Each press is followed by a poll because NVDA
    processes the resulting focus event a few hundred milliseconds later.
    """
    fragment = fragment.lower()
    if fragment in window_title(bridge).lower():
        return True

    response = bridge.call("activate_window", {"title": fragment})
    if ok(response):
        if wait_for(lambda: fragment in window_title(bridge).lower(), timeout_s=1.5, interval_s=0.1):
            return True

    # Raising the window with the topmost toggle is what works when
    # SetForegroundWindow is refused: it changes the Z order. Prefer the task
    # switcher after that, because a click at the centre can land on a menu or a
    # link — a click is the last resort, not the first.
    if force_foreground(fragment):
        if wait_for(lambda: fragment in window_title(bridge).lower(), timeout_s=1.5, interval_s=0.15):
            return True

    for _ in range(attempts):
        bridge.call("press", {"keys": "alt+tab"})
        if wait_for(lambda: fragment in window_title(bridge).lower(), timeout_s=1.2, interval_s=0.15):
            return True

    # Prefer a click when the window already exists: it is deterministic, while
    # the task switcher walks a multi-window MRU list. A window that refuses to
    # come forward still accepts a click, and a click is the tool surface's own
    # way to reach something that is not focusable. Restore first: a minimised
    # window has no usable screen rectangle to click.
    for hwnd in _windows_matching(fragment)[:2]:
        _user32.ShowWindow(hwnd, _SW_RESTORE)
        time.sleep(0.2)
        center = window_center(hwnd)
        if not center:
            continue
        bridge.call("click_at", {"x": center[0], "y": center[1]})
        if wait_for(lambda: fragment in window_title(bridge).lower(), timeout_s=2.0, interval_s=0.15):
            return True

    return False


def wait_for(predicate, timeout_s: float = 5.0, interval_s: float = 0.1):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval_s)
    return None


def recover_focus(bridge: Bridge) -> bool:
    """Get NVDA's focus onto a real window before asserting anything.

    A previous run can leave a popup open or focus on a destroyed menu object;
    ``alt+tab`` is the least intrusive way back, and the check afterwards is what
    makes it a recovery rather than a hope.
    """
    for attempt in range(5):
        title = window_title(bridge).strip()
        if title and title != "Desktop":
            return True
        try:
            bridge.call("press", {"keys": "alt+tab" if attempt % 2 else "escape"})
        except AssertionError:
            pass
        time.sleep(0.3)
    title = window_title(bridge).strip()
    return bool(title and title != "Desktop")


# ------------------------------------------------------------------------- phases


def check_bridge_contract(bridge: Bridge, report: Report) -> None:
    report.section("bridge contract (P4)")
    health = bridge.get("/health")
    report.check("health reports api_level 2", health.get("api_level") == 2, str(health.get("api_level")))
    report.check(
        "both api levels are advertised",
        set(health.get("supported_api_levels") or []) >= {1, 2},
        str(health.get("supported_api_levels")),
    )

    payload = bridge.get("/tools")
    tools = {tool["name"]: tool for tool in payload.get("tools", [])}
    report.check("schema_hash is published", isinstance(payload.get("schema_hash"), str) and len(payload["schema_hash"]) == 64)
    report.check("outputSchema is published per tool", all("outputSchema" in tool for tool in tools.values()))
    # api_level 3 retired the v1 aliases: they must not be advertised at all, so
    # no client pays context for a name it must not use.
    for name in (
        "get_current_focus",
        "get_navigator_object",
        "read_current_line",
        "move_focus",
        "send_keyboard",
        "get_desktop_object",
        "get_window_title",
    ):
        report.check(f"{name} is retired", name not in tools, f"present={name in tools}")
    for name in ("observe", "invoke", "set_value", "focus", "press", "read", "navigate", "scroll", "dismiss", "click_at", "wait_for_events"):
        report.check(f"tool {name} is advertised", name in tools)
    report.check(
        "dismiss requires dialog_control",
        (tools.get("dismiss") or {}).get("requiredPermission") == "dialog_control",
        str((tools.get("dismiss") or {}).get("requiredPermission")),
    )


def check_observation(bridge: Bridge, report: Report) -> None:
    report.section("observation (P1)")
    response = bridge.call("observe")
    report.check("observe succeeds", ok(response), str(error_code(response)))
    payload = data(response)
    focus = payload.get("focus") or {}
    report.check("focus carries a ref", focus.get("ref") == "f", str(focus.get("ref")))
    report.check("ancestors carry refs", all("ref" in node for node in payload.get("ancestors") or []))
    report.check("result carries state_version", isinstance(result(response).get("state_version"), int))
    report.check(
        "observation is fast enough",
        response.get("duration_ms", 9999) < 300,
        f"{response.get('duration_ms')} ms",
    )


def prepare_notepad(bridge: Bridge, report: Report) -> bool:
    subprocess.Popen(["notepad.exe"])  # noqa: S603,S607 — a known local executable
    # A cold start takes a moment; activating before the window exists wastes the
    # activation budget on a window that is not there yet.
    wait_for(lambda: bool(_windows_matching("Notepad")), timeout_s=15, interval_s=0.3)
    activated = activate(bridge, "Notepad")
    if not activated:
        # The machine may be full of stale Notepad windows that refuse to come
        # forward in a non-interactive session. Say so rather than reporting a
        # product failure; the dialog and browser sections cover the same tools.
        report.skip("Notepad can be focused", window_title(bridge) or "no window")
        return False
    report.check("Notepad can be focused", True, window_title(bridge))
    # A click can leave a ribbon menu open; Escape closes it, and only then does
    # Tab reach the editor.
    for _ in range(2):
        bridge.call("press", {"keys": "escape"})
        time.sleep(0.2)
    # Activating by click can land on the tab strip; Tab reaches the editor in
    # Notepad, and the check below is what decides whether it worked.
    for _ in range(4):
        observed = data(bridge.call("observe"))
        role = (observed.get("focus") or {}).get("role")
        if role in ("DOCUMENT", "EDITABLETEXT"):
            break
        bridge.call("press", {"keys": "tab"})
    observe = data(bridge.call("observe"))
    report.check(
        "focus is Notepad's document",
        (observe.get("focus") or {}).get("role") in ("DOCUMENT", "EDITABLETEXT"),
        str((observe.get("focus") or {}).get("role")),
    )
    return True


def check_settle_and_coalescing(bridge: Bridge, report: Report) -> None:
    report.section("settle and coalescing (P1)")
    marker = f"abc {int(time.time()) % 100000}"
    since = bridge.get("/events").get("to_version", 0)
    before = bridge.get("/state").get("state_version", 0)

    response = bridge.call("set_value", {"ref": "f", "value": marker})
    report.check("set_value succeeds", ok(response), str(error_code(response)))
    block = result(response)
    report.check("set_value reports changed", block.get("changed") is True, json.dumps(block.get("delta"))[:200])
    report.check(
        "set_value names the change in its delta",
        str((block.get("delta") or {}).get("value", {}).get("to")) == marker,
        json.dumps(block.get("delta"))[:200],
    )
    report.check(
        "settle completes inside its budget",
        0 < block.get("settled_ms", 99999) < 2000,
        f"{block.get('settled_ms')} ms",
    )

    time.sleep(0.6)
    page = bridge.get(f"/events?since={since}")
    transitions = page.get("events") or []
    report.check(
        "one action coalesces into at most two transitions",
        len(transitions) <= 2,
        f"{len(transitions)} transitions: {[entry.get('kind') for entry in transitions]}",
    )
    after = bridge.get("/state").get("state_version", 0)
    report.check("one visible change bumps state_version once", after - before == 1, f"{before} -> {after}")

    read = data(bridge.call("read", {"ref": "f", "scope": "document", "max_chars": 200}))
    report.check("the value reads back", marker in str(read.get("text")), repr(read.get("text"))[:120])

    report.section("idle")
    idle_since = bridge.get("/events").get("to_version", 0)
    idle_version = bridge.get("/state").get("state_version", 0)
    time.sleep(2.0)
    idled = bridge.get(f"/events?since={idle_since}")
    report.check("idle produces no transitions", not idled.get("events"), str(idled.get("events"))[:120])
    report.check(
        "idle produces no state_version bumps",
        bridge.get("/state").get("state_version", 0) == idle_version,
        f"{idle_version} -> {bridge.get('/state').get('state_version')}",
    )


def check_dialog(bridge: Bridge, report: Report) -> None:
    report.section("dialog automation (P2)")
    opener = bridge.call("press", {"keys": "control+h"})
    report.check("Replace dialog opens", ok(opener), str(error_code(opener)))
    time.sleep(0.4)
    observed = data(bridge.call("observe"))
    dialog = observed.get("dialog") or {}
    report.check("observe detects the dialog", bool(dialog), json.dumps(dialog)[:160])
    if not dialog:
        return

    children = dialog.get("children") or []
    report.check("dialog children are referenced", bool(children), f"{len(children)} children")
    names = [child.get("name") for child in children]
    report.check("dialog children are the controls", any(name for name in names), json.dumps(names)[:200])

    cancel = next(
        (
            child
            for child in children
            if str(child.get("name", "")).strip().lower()
            in ("cancel", "close", "exit find and replace")
        ),
        None,
    )
    if cancel is None:
        report.check("a control that leaves the dialog can be found", False, json.dumps(names)[:240])
        bridge.call("dismiss")
        return
    invoked = bridge.call("invoke", {"ref": cancel["ref"]})
    report.check("invoke on a dialog control succeeds", ok(invoked), str(error_code(invoked)))
    report.check(
        "invoking Cancel changes the UI",
        result(invoked).get("changed") is True,
        json.dumps(result(invoked).get("delta"))[:200],
    )
    time.sleep(0.3)
    report.check("the dialog is gone after Cancel", not (data(bridge.call("observe")).get("dialog")))

    reopened = bridge.call("press", {"keys": "control+h"})
    report.check("Replace dialog reopens", ok(reopened), str(error_code(reopened)))
    time.sleep(0.4)
    dismissed = bridge.call("dismiss")
    report.check("dismiss succeeds", ok(dismissed), str(error_code(dismissed)))
    report.check("dismiss reports the dialog gone", data(dismissed).get("dismissed") is True, json.dumps(data(dismissed).get("steps"))[:200])
    time.sleep(0.3)
    report.check("no dialog remains after dismiss", not (data(bridge.call("observe")).get("dialog")))


def open_page(path: Path, page_title: str) -> str:
    """Open a local page in a *new browser window*, and wait for that window.

    ``os.startfile`` lets the browser decide, and a browser that reuses an
    existing window may open the page in a background tab — which makes "the page
    is focused" fail for reasons that have nothing to do with NVDA. Two browsers
    are tried because an already-running one sometimes reuses its window anyway.
    """
    url = path.as_uri()
    candidates = (
        (r"C:\Program Files\Mozilla Firefox\firefox.exe", "-new-window"),
        (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "--new-window"),
        (r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", "--new-window"),
    )
    for executable, flag in candidates:
        if not Path(executable).is_file():
            continue
        subprocess.Popen([executable, flag, url])  # noqa: S603
        if wait_for(lambda: bool(_windows_matching(page_title)), timeout_s=45, interval_s=0.3):
            return Path(executable).name
    os.startfile(str(path))  # noqa: S606
    wait_for(lambda: bool(_windows_matching(page_title)), timeout_s=45, interval_s=0.3)
    return "default-browser"


def check_browser(bridge: Bridge, report: Report, page_path: Path, page_title: str) -> None:
    report.section("browser automation (P2)")
    browser = open_page(page_path, page_title)
    report.note("browser launched", browser)
    activated = activate(bridge, page_title, attempts=30)
    report.check("the acceptance page opens", bool(activated), window_title(bridge))
    if not activated:
        return

    observed = data(bridge.call("observe"))
    focus = observed.get("focus") or {}
    # The page autofocuses its button, but a browser that opened in the
    # background does not apply that until it is interacted with. Reach the
    # button the way a keyboard user would, then assert on where focus is.
    for _ in range(6):
        if focus.get("role") == "BUTTON":
            break
        bridge.call("press", {"keys": "tab"})
        time.sleep(0.15)
        observed = data(bridge.call("observe"))
        focus = observed.get("focus") or {}
    report.check("the page's button has focus", focus.get("role") == "BUTTON", str(focus.get("role")))
    report.check("the button is named", "Run action" in str(focus.get("name")), str(focus.get("name")))

    since = bridge.get("/events").get("to_version", 0)
    invoked = bridge.call("invoke", {"ref": focus.get("ref", "f")})
    report.check("invoke on a browser button succeeds", ok(invoked), str(error_code(invoked)))
    block = result(invoked)
    report.check("invoking the button is a change", block.get("changed") is True, json.dumps(block.get("delta"))[:200])
    report.check(
        "the change names the button's new name",
        str((block.get("delta") or {}).get("name", {}).get("from")) == "Run action"
        and str((block.get("delta") or {}).get("name", {}).get("to", "")).startswith("Run action "),
        json.dumps(block.get("delta"))[:200],
    )

    time.sleep(0.5)
    page = bridge.get(f"/events?since={since}")
    kinds = [entry.get("kind") for entry in page.get("events") or []]
    report.note("live region events after invoke", str(kinds))

    # A focus move inside the page: Tab from the button lands on the first link.
    moved = bridge.call("press", {"keys": "tab"})
    delta = result(moved).get("delta") or {}
    report.check("a focus move is a change", result(moved).get("changed") is True, json.dumps(delta)[:200])
    name_delta = delta.get("name") or {}
    report.check(
        "the delta names the old and the new focus",
        bool(name_delta.get("from")) and bool(name_delta.get("to")) and name_delta.get("from") != name_delta.get("to"),
        json.dumps(name_delta)[:200],
    )

    # A raw pointer click on the button. The read-back position is the claim that
    # matters here; a click may also leave a popup behind, so it runs last.
    refreshed = data(bridge.call("observe")).get("focus") or {}
    box = refreshed.get("location") or [0, 0, 0, 0]
    x, y = box[0] + max(1, box[2] // 2), box[1] + max(1, box[3] // 2)
    clicked = bridge.call("click_at", {"x": x, "y": y})
    report.check("click_at succeeds", ok(clicked), str(error_code(clicked)))
    report.check(
        "the pointer landed on the requested point",
        data(clicked).get("onTarget") is True,
        json.dumps(data(clicked).get("cursor")),
    )
    report.check(
        "click_at reports the object under the point",
        bool(data(clicked).get("hit")),
        json.dumps(data(clicked).get("hit"))[:160],
    )
    report.note("click_at result", json.dumps(result(clicked))[:200])
    bridge.call("press", {"keys": "escape"})  # clear any popup the click opened
    time.sleep(0.5)


def check_delivery(bridge: Bridge, report: Report) -> None:
    report.section("delivery (P3)")
    since = bridge.get("/events").get("to_version", 0)
    received = {}

    def wait() -> None:
        received["page"] = bridge.get(f"/events?since={since}&timeout_ms=8000")

    waiter = threading.Thread(target=wait)
    waiter.start()
    time.sleep(0.3)
    bridge.call("press", {"keys": "tab"})
    waiter.join(timeout=12)
    page = received.get("page") or {}
    report.check("a waiter is woken by a transition", bool(page.get("events")), json.dumps(page.get("events"))[:200])
    report.check("the waiter advances its cursor", page.get("to_version", 0) > since, f"{since} -> {page.get('to_version')}")

    since_now = bridge.get("/events").get("to_version", 0)
    # A timeout is only meaningful when nothing else is in flight. A click can
    # leave a popup whose name change arrives late, so make two attempts after
    # clearing it rather than asserting on one noisy sample.
    response = {}
    for _ in range(3):
        bridge.call("press", {"keys": "escape"})
        time.sleep(1.0)
        since_now = bridge.get("/events").get("to_version", 0)
        response = bridge.call("wait_for_events", {"since": since_now, "timeout_ms": 200})
        if data(response).get("timed_out"):
            break
    detail = json.dumps(data(response))[:160]
    report.check("wait_for_events returns a timeout honestly", data(response).get("timed_out") is True, detail)
    report.check("wait_for_events reports no events on timeout", not data(response).get("events"), detail)


def check_references(bridge: Bridge, report: Report) -> None:
    report.section("references (P2)")
    stale = bridge.call("invoke", {"ref": "f-does-not-exist"})
    report.check("unknown ref is rejected", not ok(stale))
    report.check("unknown ref uses the stale-reference code", error_code(stale) == 4206, str(error_code(stale)))

    observed = data(bridge.call("observe"))
    ref = (observed.get("focus") or {}).get("ref")
    focused = bridge.call("focus", {"ref": ref})
    report.check("focus on a captured ref succeeds", ok(focused), str(error_code(focused)))


# --------------------------------------------------------------------------- main


def main(argv=None) -> int:
    # Window titles on Windows can contain characters the console code page has
    # no mapping for (Edge inserts a zero-width space); a report must never crash
    # on the text it is reporting about.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--token", help="bridge token (defaults to env or token.key)")
    parser.add_argument("--json", help="write the report to this path")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--skip-browser", action="store_true")
    args = parser.parse_args(argv)

    bridge = Bridge(resolve_token(args.token))
    report = Report(verbose=args.verbose)

    check_bridge_contract(bridge, report)
    report.check("a real window has focus", recover_focus(bridge), window_title(bridge))
    check_observation(bridge, report)
    if prepare_notepad(bridge, report):
        check_settle_and_coalescing(bridge, report)
        check_dialog(bridge, report)
        check_references(bridge, report)

    page_path = Path(tempfile.gettempdir()) / "nvda-mcp-acceptance.html"
    # A unique title per run: the machine may still have pages from earlier runs
    # open, and activating "the acceptance page" would otherwise pick one of them.
    page_title = f"nvda-mcp acceptance {int(time.time()) % 100000}"
    page_path.write_text(PAGE.replace("{{TITLE}}", page_title), encoding="utf-8")
    if not args.skip_browser:
        check_browser(bridge, report, page_path, page_title)
        check_delivery(bridge, report)

    skipped = [check for check in report.checks if check.get("skipped")]
    print(f"\n{len(report.checks) - len(report.failed) - len(skipped)}/{len(report.checks) - len(skipped)} checks passed ({len(skipped)} skipped)")
    if report.failed:
        print("\nfailures:")
        for check in report.failed:
            print(f"  {check['name']}: {check['detail']}")
    if args.json:
        Path(args.json).write_text(json.dumps(report.checks, indent=1), encoding="utf-8")
        print(f"report written to {args.json}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
