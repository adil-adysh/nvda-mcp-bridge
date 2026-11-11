#!/usr/bin/env python3
"""Probe five: can a located UIA element be acted on directly?

Probes two and three established the search side: `FindAll(Descendants,
condition)` over an Edge window is one provider-side call (65 ms for 2,346
elements) where NVDA's own walk costs ~4.8 ms *per node*. Part four established
that a UIA hit cannot be turned into an `NVDAObjects.UIA.UIA` object, and that
hit-testing only works for elements that are on screen. So the remaining
question is whether the element itself is actionable.

This opens its own page and, using only UIA (`InvokePattern`,
`ValuePattern.SetValue`, `SetFocus`), operates controls by name -- then checks
the page's own log, which is the user's view.

The window is never focused: `ElementFromHandle` needs an HWND, not focus, and
that is the point -- an agent should be able to act on a window it is not
driving keystrokes into.

Requires developer mode.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from urllib.parse import quote

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from action_surface_probe import (  # noqa: E402
    BROWSERS,
    BridgeError,
    _post,
    close_window,
    find_browser_exe,
    jload,
    py,
)

TITLE = "NVDA Locate Probe"

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>NVDA Locate Probe</title>
<style>body{font:16px/1.8 system-ui,sans-serif;margin:2rem}
input{width:16rem;padding:.3rem}button{margin-right:.5rem}</style></head><body>
<h1>NVDA Locate Probe</h1>
<label for="q">Search field</label><input id="q" type="text" value="start">
<p><button id="alpha" onclick="hit('alpha')">Alpha action</button>
<button id="beta" onclick="hit('beta')">Beta action</button>
<a id="go" href="about:blank" onclick="hit('link'); return false">Go somewhere</a></p>
<div id="log" role="status">idle</div>
<script>
function hit(which){document.getElementById('log').textContent='clicked '+which;}
</script></body></html>
"""

# Runs inside NVDA. Everything here is UIA-only.
SEARCH_AND_ACT = r'''
import json, time
import UIAHandler

TITLE = "NVDA Locate Probe"
client = UIAHandler.handler.clientObject

import api
import ctypes
found = []
desktop = api.getDesktopObject()
try:
    windows = desktop.children
except Exception:
    windows = []
for window in windows:
    try:
        name = window.name or ""
    except Exception:
        continue
    if TITLE in name:
        found.append((window.windowHandle, name))
out = {"windows": found}
if not found:
    raise RuntimeError("probe window not found")
hwnd = found[0][0]

root = client.ElementFromHandle(hwnd)
out["root_name"] = root.CurrentName
out["root_control_type"] = root.CurrentControlType


def find(control_type, name):
    condition = client.CreateAndCondition(
        client.CreatePropertyCondition(UIAHandler.UIA_ControlTypePropertyId, control_type),
        client.CreatePropertyConditionEx(
            UIAHandler.UIA_NamePropertyId,
            name,
            UIAHandler.PropertyConditionFlags_IgnoreCase | UIAHandler.PropertyConditionFlags_MatchSubstring,
        ),
    )
    start = time.perf_counter()
    matches = root.FindAll(UIAHandler.TreeScope_Descendants, condition)
    return matches, round((time.perf_counter() - start) * 1000, 1)


def value_of(element):
    try:
        pattern = element.GetCurrentPattern(UIAHandler.UIA_ValuePatternId)
        value_pattern = pattern.QueryInterface(UIAHandler.IUIAutomationValuePattern)
        return value_pattern.CurrentValue
    except Exception as exc:
        return f"<{type(exc).__name__}>"


out["steps"] = []

# 1. Invoke a button by name, without focusing anything.
matches, ms = find(UIAHandler.UIA_ButtonControlTypeId, "alpha")
out["alpha_search_ms"] = ms
out["alpha_count"] = int(matches.Length)
if matches.Length:
    element = matches.GetElement(0)
    out["alpha_name"] = element.CurrentName
    out["alpha_automation_id"] = element.CurrentAutomationId
    start = time.perf_counter()
    try:
        pattern = element.GetCurrentPattern(UIAHandler.UIA_InvokePatternId)
        invoke = pattern.QueryInterface(UIAHandler.IUIAutomationInvokePattern)
        invoke.Invoke()
        out["alpha_invoke_ms"] = round((time.perf_counter() - start) * 1000, 1)
        out["alpha_invoked"] = True
    except Exception as exc:
        out["alpha_invoke_error"] = f"{type(exc).__name__}: {exc}"
    time.sleep(0.6)

# 2. Read the page's own log through UIA (a Text/StatusBar element named "clicked alpha").
try:
    condition = client.CreatePropertyConditionEx(
        UIAHandler.UIA_NamePropertyId,
        "clicked",
        UIAHandler.PropertyConditionFlags_IgnoreCase | UIAHandler.PropertyConditionFlags_MatchSubstring,
    )
    start = time.perf_counter()
    hits = root.FindAll(UIAHandler.TreeScope_Descendants, condition)
    out["log_search_ms"] = round((time.perf_counter() - start) * 1000, 1)
    out["log_hits"] = [hits.GetElement(i).CurrentName for i in range(min(5, int(hits.Length)))]
except Exception as exc:
    out["log_error"] = f"{type(exc).__name__}: {exc}"

# 3. Set the value of the edit field by name, then read it back.
matches, ms = find(UIAHandler.UIA_EditControlTypeId, "Search field")
out["edit_search_ms"] = ms
out["edit_count"] = int(matches.Length)
if matches.Length:
    field = matches.GetElement(0)
    out["edit_value_before"] = value_of(field)
    start = time.perf_counter()
    try:
        pattern = field.GetCurrentPattern(UIAHandler.UIA_ValuePatternId)
        value_pattern = pattern.QueryInterface(UIAHandler.IUIAutomationValuePattern)
        value_pattern.SetValue("located text")
        out["edit_set_ms"] = round((time.perf_counter() - start) * 1000, 1)
        time.sleep(0.4)
        out["edit_value_after"] = value_of(field)
    except Exception as exc:
        out["edit_set_error"] = f"{type(exc).__name__}: {exc}"

# 4. Focus the second button by name, without touching the keyboard.
matches, ms = find(UIAHandler.UIA_ButtonControlTypeId, "beta")
out["beta_search_ms"] = ms
if matches.Length:
    element = matches.GetElement(0)
    try:
        element.SetFocus()
        out["beta_focused"] = True
    except Exception as exc:
        out["beta_focus_error"] = f"{type(exc).__name__}: {exc}"

# 5. Does the element survive and re-read after all that?
if matches.Length:
    try:
        out["beta_name_after"] = element.CurrentName
        out["beta_runtime_id"] = str(element.GetRuntimeId())
    except Exception as exc:
        out["beta_reread_error"] = f"{type(exc).__name__}: {exc}"

# 6. Cost of a dead element: close the window later, but read a bogus element now.
try:
    not_there = client.CreatePropertyConditionEx(
        UIAHandler.UIA_NamePropertyId,
        "definitely not on this page at all",
        UIAHandler.PropertyConditionFlags_IgnoreCase | UIAHandler.PropertyConditionFlags_MatchSubstring,
    )
    matches = root.FindAll(UIAHandler.TreeScope_Descendants, not_there)
    out["empty_count"] = int(matches.Length)
except Exception as exc:
    out["empty_error"] = f"{type(exc).__name__}: {exc}"

print("JSON" + json.dumps(out, default=str))
'''


def main() -> int:
    scratch = REPO / ".probe"
    scratch.mkdir(exist_ok=True)
    page = scratch / "locate_probe.html"
    page.write_text(PAGE, encoding="utf-8")
    url = "file:///" + quote(page.as_posix())

    exe = find_browser_exe("edge")
    try:
        py(
            "import subprocess, time\n"
            f"subprocess.Popen([{exe!r}, '--new-window', {url!r}])\n"
            "time.sleep(3.0)\n"
            "print('launched')"
        )
        time.sleep(1.0)
        result = jload(py(SEARCH_AND_ACT))
    except BridgeError as err:
        print(f"probe failed: {err}")
        return 1
    finally:
        close_window()
        try:
            page.unlink()
            scratch.rmdir()
        except OSError:
            pass

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
