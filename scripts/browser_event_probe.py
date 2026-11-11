#!/usr/bin/env python3
"""Measure what a browser actually produces on NVDA's event path.

The script opens its own Edge or Chrome window on a scratch HTML form, records
every event NVDA dispatches for a few controlled actions, measures how long a
focus change takes to become observable, and closes the window it opened.

It answers two questions that the computer-use design depends on:

* how many raw events one user-level action produces (i.e. how much coalescing
  the observation layer needs), and
* how stale an immediate read after a keystroke is, and how long settlement
  really takes in a browser.

Requires developer mode, because the recorder is installed through
``execute_python``. Nothing is installed persistently: the window is closed and
the recorder removed even on failure.

    python scripts/browser_event_probe.py [--browser edge|chrome] [--keep-window]
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from bridge_auth import resolve_token  # noqa: E402

BRIDGE = "http://127.0.0.1:8765"
BROWSERS = {
    "edge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
}
WINDOW_MATCH = "NVDA Event Probe"
ZWSP = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)

FORM_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>NVDA Event Probe</title>
<style>body{font:16px/1.6 system-ui,sans-serif;margin:2rem}
label{display:block;margin:.75rem 0 .25rem}input[type=text]{width:18rem;padding:.25rem}
#log{margin-top:1rem;color:#333}</style></head><body>
<h1>NVDA Event Probe</h1>
<label for="q">Text field</label>
<input id="q" type="text" autofocus oninput="mirror()">
<label><input id="c" type="checkbox" onchange="clicked('checkbox')"> Checkbox</label>
<p><button id="b" onclick="clicked('button')">Run action</button></p>
<div id="log" aria-live="polite">nothing yet</div>
<script>
function mirror(){document.getElementById('log').textContent='value: '+document.getElementById('q').value;}
function clicked(w){document.getElementById('log').textContent=w+' activated';
document.title='NVDA Event Probe - '+w;}
</script></body></html>
"""

# --- snippets executed inside NVDA ------------------------------------------------

REC_INSTALL = r'''
import builtins, eventHandler, logHandler, time
state = {"events": [], "limit": 4000, "t0": time.perf_counter()}
if not getattr(builtins, "_NVDA_ORIG_EXECUTE_EVENT", None):
    builtins._NVDA_ORIG_EXECUTE_EVENT = eventHandler.executeEvent
    def recording_execute_event(eventName, obj, **kwargs):
        if len(state["events"]) < state["limit"]:
            def probe(fn):
                try: return str(fn())
                except Exception: return None
            state["events"].append((round((time.perf_counter() - state["t0"]) * 1000, 1),
                eventName, type(obj).__name__, probe(lambda: obj.name),
                probe(lambda: obj.role), sorted(kwargs)))
        return builtins._NVDA_ORIG_EXECUTE_EVENT(eventName, obj, **kwargs)
    eventHandler.executeEvent = recording_execute_event
    builtins._NVDA_EVENT_STATE = state
builtins._NVDA_EVENT_STATE["events"].clear()
builtins._NVDA_EVENT_STATE["t0"] = time.perf_counter()
print("installed")
'''

REC_CUT = r'''
import builtins, collections, json, time
state = getattr(builtins, "_NVDA_EVENT_STATE", None)
if state is None:
    print("null")
else:
    ev = state["events"]
    payload = {
        "total": len(ev),
        "kinds": dict(collections.Counter(e[1] for e in ev).most_common()),
        "span_ms": round(ev[-1][0] - ev[0][0], 1) if ev else 0.0,
        "events": [{"at_ms": e[0], "kind": e[1], "class": e[2], "name": e[3],
                    "role": e[4], "kwargs": e[5]} for e in ev[:60]],
    }
    print("JSON" + json.dumps(payload))
    ev.clear()
    state["t0"] = time.perf_counter()
'''

REC_REMOVE = r'''
import builtins, eventHandler
orig = getattr(builtins, "_NVDA_ORIG_EXECUTE_EVENT", None)
if orig is not None:
    eventHandler.executeEvent = orig
    del builtins._NVDA_ORIG_EXECUTE_EVENT
builtins.__dict__.pop("_NVDA_EVENT_STATE", None)
print("removed")
'''


# --- bridge plumbing ---------------------------------------------------------------


class BridgeError(RuntimeError):
    pass


def _post(action: str, params: dict | None = None, timeout: float = 60) -> dict:
    request = urllib.request.Request(
        BRIDGE + "/action",
        data=json.dumps({"action": action, "params": params or {}}).encode(),
        headers={"X-NVDA-Bridge-Token": resolve_token(), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.URLError as err:
        raise BridgeError(f"bridge unreachable at {BRIDGE}: {err}") from err


def py(code: str) -> str:
    result = _post("execute_python", {"code": code})
    if not result.get("success"):
        error = result.get("error") or {}
        if error.get("code") in (4201, 4202, 4203, 4204):
            raise BridgeError(f"execute_python blocked: {error.get('name')} - {error.get('message')}")
        raise BridgeError(f"execute_python failed: {json.dumps(result)[:300]}")
    return (result.get("data") or {}).get("stdout", "").strip()


def press(gesture: str) -> None:
    _post("send_keyboard", {"gesture": gesture})


def focus_name() -> str:
    data = (_post("get_current_focus").get("data")) or {}
    return (data.get("name") or "").translate(ZWSP)


def window_title() -> str:
    return ((_post("get_window_title").get("data")) or {}).get("title", "").translate(ZWSP)


def cut() -> dict:
    out = py(REC_CUT)
    if not out.startswith("JSON"):
        return {}
    return json.loads(out[4:])


# --- window lifecycle ---------------------------------------------------------------


def find_browser_exe(name: str) -> str:
    for candidate in BROWSERS[name]:
        if Path(candidate).is_file():
            return candidate
    raise BridgeError(f"{name} not found in the usual install locations")


def open_window(url: str) -> str:
    exe = find_browser_exe(args.browser)
    py(
        "import subprocess, time\n"
        f"subprocess.Popen([{exe!r}, '--new-window', {url!r}])\n"
        "time.sleep(2.5)\n"
        "print('launched')"
    )
    time.sleep(1.0)
    return window_title()


def close_window() -> bool:
    """Close only the window this script opened, by title, without sending keys."""
    user32 = ctypes.windll.user32
    WM_CLOSE = 0x0010
    closed = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd, _param):
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if WINDOW_MATCH in buffer.value:
                closed.append((hwnd, buffer.value))
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return True

    user32.EnumWindows(callback, 0)
    time.sleep(1.0)
    for hwnd, title in closed:
        print(f"  closed hwnd={hwnd} {title.split(' - ')[0]!r}")
    return bool(closed)


# --- reporting ----------------------------------------------------------------------

KINDS_OF_INTEREST = {
    "gainFocus",
    "loseFocus",
    "focusEntered",
    "focusExited",
    "foreground",
    "valueChange",
    "stateChange",
    "nameChange",
    "caret",
    "selection",
    "typedCharacter",
    "liveRegionChange",
    "alert",
    "UIA_notification",
    "documentLoadComplete",
}


def report(label: str, bucket: dict) -> None:
    if not bucket:
        print(f"  {label}: no events")
        return
    kinds = ", ".join(f"{kind}x{count}" for kind, count in bucket["kinds"].items())
    print(f"  {label}: {bucket['total']} events over {bucket['span_ms']} ms")
    print(f"    {kinds}")
    novel = sorted(set(bucket["kinds"]) - KINDS_OF_INTEREST)
    if novel:
        print(f"    not in the planned recorded set: {', '.join(novel)}")


def measure_settle(rounds: int = 3) -> None:
    print("\n-- focus settle after one Tab")
    for index in range(rounds):
        before = focus_name()
        started = time.perf_counter()
        press("tab")
        first = focus_name()
        first_ms = (time.perf_counter() - started) * 1000
        settled_at = None
        deadline = started + 1.0
        while time.perf_counter() < deadline:
            if focus_name() != before:
                settled_at = (time.perf_counter() - started) * 1000
                break
            time.sleep(0.005)
        verdict = "RACED" if first == before else "fresh"
        settle = f"{settled_at:.0f} ms" if settled_at else "no change within 1 s"
        print(
            f"  {index + 1}. {before!r} -> {first!r} after {first_ms:.0f} ms "
            f"({verdict}); settled in {settle}"
        )


def main() -> int:
    scratch = REPO / ".probe"
    scratch.mkdir(exist_ok=True)
    form = scratch / "event_probe.html"
    form.write_text(FORM_HTML, encoding="utf-8")
    url = "file:///" + quote(form.as_posix())

    print(f"bridge: {BRIDGE}")
    try:
        py(REC_INSTALL)
    except BridgeError as err:
        print(f"cannot run the probe: {err}", file=sys.stderr)
        print(
            "Enable developer mode in NVDA Settings > NVDA MCP Bridge, then retry. "
            "The probe needs it to install the recorder inside NVDA.",
            file=sys.stderr,
        )
        return 2

    try:
        print("\n-- opening an isolated browser window")
        title = open_window(url)
        print(f"  foreground: {title}")
        if WINDOW_MATCH not in title:
            print("  the probe window did not take focus; not sending keys", file=sys.stderr)
            return 1

        time.sleep(0.5)
        print("\n-- a page load")
        report("load", cut())

        print("\n-- typing 'abc' into the autofocused field")
        for char in ("a", "b", "c"):
            press(char)
            time.sleep(0.3)
        time.sleep(0.8)
        report("typing", cut())

        print("\n-- Tab to the checkbox, Space to toggle it")
        press("tab")
        time.sleep(0.4)
        press("space")
        time.sleep(0.8)
        report("checkbox", cut())

        print("\n-- Tab to the button, Enter to activate it")
        press("tab")
        time.sleep(0.4)
        press("enter")
        time.sleep(1.0)
        report("button", cut())
        print(f"  title now: {window_title()}")

        measure_settle()
    finally:
        if not args.keep_window:
            print("\n-- cleanup")
            close_window()
        py(REC_REMOVE)
        try:
            form.unlink()
            scratch.rmdir()
        except OSError:
            pass

    print("\npass: the probe ran; see the counts and settle latencies above")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", choices=sorted(BROWSERS), default="edge")
    parser.add_argument("--keep-window", action="store_true", help="leave the probe window open")
    args = parser.parse_args()
    try:
        sys.exit(main())
    except BridgeError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
