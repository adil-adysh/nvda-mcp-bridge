#!/usr/bin/env python3
"""Probe the assumptions the action layer rests on, in a real browser.

Three things the plan asserts but has not measured:

1. `invoke` -- can an object be activated with `doAction()` without moving
   focus, and does the page actually react?
2. `ref` -- how expensive is walking from the document to a node, and how often
   are NVDA's objects replaced for the same node?
3. event rate -- what does a page that updates itself do to the event path, and
   what does a full projection cost while that happens?

Opens and closes its own window. Requires developer mode.
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
WINDOW_MATCH = "NVDA Action Probe"
ZWSP = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)

FORM_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>NVDA Action Probe</title>
<style>body{font:16px/1.7 system-ui,sans-serif;margin:2rem}
input[type=text]{width:16rem;padding:.3rem}#log{margin-top:1rem;color:#222}
#ticker,#live{font-family:ui-monospace,monospace;color:#666}</style></head><body>
<h1>NVDA Action Probe</h1>
<label for="q">Text field</label>
<input id="q" type="text" value="hello" autofocus>
<label><input id="c" type="checkbox"> Checkbox</label>
<p><button id="b" onclick="activated()">Run action</button></p>
<div id="log">idle</div>
<div id="ticker">tick 0</div>
<div id="live" aria-live="polite"></div>
<script>
function activated(){document.getElementById('log').textContent='activated';
document.title='NVDA Action Probe - activated';}
document.getElementById('c').addEventListener('change',function(){
document.getElementById('log').textContent='checkbox '+this.checked;});
var n=0;setInterval(function(){n++;document.getElementById('ticker').textContent='tick '+n;},25);
var m=0;setInterval(function(){m++;document.getElementById('live').textContent='update '+m;},400);
</script></body></html>
"""

# --- inside-NVDA probes -------------------------------------------------------------

FIND_NODE = r'''
import json, time
import api, controlTypes
import NVDAObjects

WANT = {"button", "checkbox", "edit"}
start = time.perf_counter()
focus = api.getFocusObject()
# Start from the containing document/window rather than the focused control.
node = focus
for _ in range(6):
    if node.parent is None:
        break
    node = node.parent
    if str(node.role) in ("52", "1"):
        break
root = node

found = {}
visited = 0
stack = [(root, 0)]
while stack and visited < 350:
    obj, depth = stack.pop(0)
    visited += 1
    role = str(obj.role)
    try:
        name = obj.name
    except Exception:
        name = None
    key = None
    if role == "9":
        key = "button"
    elif role == "5":
        key = "checkbox"
    elif role == "8":
        key = "edit"
    if key and key not in found:
        found[key] = obj
    if depth < 14:
        try:
            children = obj.children
        except Exception:
            children = []
        for child in children:
            stack.append((child, depth + 1))

elapsed = (time.perf_counter() - start) * 1000
print("JSON" + json.dumps({
    "walk_ms": round(elapsed, 1),
    "visited": visited,
    "root": [str(root.role), root.name],
    "found": {k: v.name for k, v in found.items()},
}, default=str))
'''


def invoke_probe(target: str) -> str:
    return (
        "import json, time\n"
        "import api\n"
        "from NVDAObjects import NVDAObject\n"
        "\n"
        "focus = api.getFocusObject()\n"
        "node = focus\n"
        "for _ in range(6):\n"
        "    if node.parent is None:\n"
        "        break\n"
        "    node = node.parent\n"
        "    if str(node.role) in ('52', '1'):\n"
        "        break\n"
        "root = node\n"
        f"target_role = {target!r}\n"
        "found = None\n"
        "visited = 0\n"
        "stack = [(root, 0)]\n"
        "while stack and visited < 350 and found is None:\n"
        "    obj, depth = stack.pop(0)\n"
        "    visited += 1\n"
        "    if str(obj.role) == target_role and obj is not focus:\n"
        "        found = obj\n"
        "        break\n"
        "    if depth < 14:\n"
        "        try:\n"
        "            children = obj.children\n"
        "        except Exception:\n"
        "            children = []\n"
        "        for child in children:\n"
        "            stack.append((child, depth + 1))\n"
        "\n"
        "out = {'visited': visited}\n"
        "if found is None:\n"
        "    out['error'] = 'no target found'\n"
        "else:\n"
        "    out['name'] = found.name\n"
        "    out['role'] = str(found.role)\n"
        "    out['states_before'] = sorted(str(s) for s in found.states)\n"
        "    try:\n"
        "        out['action_count'] = found.actionCount\n"
        "        out['default_action_index'] = found.defaultActionIndex\n"
        "        out['action_name'] = found.getActionName(found.defaultActionIndex)\n"
        "    except Exception as err:\n"
        "        out['action_error'] = f'{type(err).__name__}: {err}'\n"
        "    t0 = time.perf_counter()\n"
        "    try:\n"
        "        found.doAction()\n"
        "        out['do_action'] = 'ok'\n"
        "    except Exception as err:\n"
        "        out['do_action'] = f'{type(err).__name__}: {err}'\n"
        "    out['do_action_ms'] = round((time.perf_counter() - t0) * 1000, 1)\n"
        "    time.sleep(0.4)\n"
        "    try:\n"
        "        out['states_after'] = sorted(str(s) for s in found.states)\n"
        "        out['alive'] = getattr(found, 'isAlive', 'no isAlive attribute')\n"
        "    except Exception as err:\n"
        "        out['after_error'] = f'{type(err).__name__}: {err}'\n"
        "print('JSON' + json.dumps(out, default=str))\n"
    )


SAMPLE_FOCUS = r'''
import json
import api
f = api.getFocusObject()
print("JSON" + json.dumps({
    "instance": id(f),
    "alive": getattr(f, "isAlive", None),
    "role": str(f.role),
    "name": f.name,
}))
'''

PROJECTION_COST = r'''
import json, time
import api

def project(invalidate=False):
    f = api.getFocusObject()
    ancestors = _ancestors(f)
    if invalidate:
        try:
            f.invalidateCache()
        except Exception:
            pass
        for a in ancestors:
            try:
                a.invalidateCache()
            except Exception:
                pass
    return {
        "role": str(f.role),
        "name": f.name,
        "states": sorted(str(s) for s in f.states),
        "value": f.value if hasattr(f, "value") else None,
        "location": tuple(f.location) if f.location else None,
        "ancestors": [(str(o.role), o.name) for o in ancestors],
    }

def _ancestors(f):
    out = []
    o = f
    for _ in range(5):
        o = o.parent
        if o is None:
            break
        out.append(o)
    return out

def sample(invalidate):
    times = []
    for _ in range(30):
        t0 = time.perf_counter()
        project(invalidate)
        times.append(round((time.perf_counter() - t0) * 1000, 2))
    times.sort()
    return {"min": times[0], "median": times[len(times) // 2],
            "p90": times[int(len(times) * 0.9)], "max": times[-1]}

print("JSON" + json.dumps({
    "cached": sample(False),
    "invalidated": sample(True),
}))
'''

SET_VALUE_SURFACE = r'''
import json
import api, textInfos
f = api.getFocusObject()
candidates = [n for n in dir(f) if "value" in n.lower() or n.lower().startswith("set")]
out = {
    "role": str(f.role),
    "name": f.name,
    "value": f.value if hasattr(f, "value") else None,
    "candidates": candidates,
}
info = None
try:
    info = f.makeTextInfo(textInfos.POSITION_ALL)
    out["text"] = info.text[:80]
    out["text_methods"] = [n for n in dir(info) if n.lower().startswith("set") or "update" in n.lower()]
except Exception as err:
    out["text_error"] = f"{type(err).__name__}: {err}"

# Can the value be written at all? Try every mechanism NVDA exposes.
mechanisms = {}

uia_element = getattr(f, "UIAElement", None)
mechanisms["has_UIAElement"] = uia_element is not None
if uia_element is not None:
    try:
        import UIAHandler
        pattern = uia_element.GetCurrentPattern(UIAHandler.UIA_ValuePatternId)
        value_pattern = pattern.QueryInterface(UIAHandler.IUIAutomationValuePattern)
        mechanisms["uia_value_pattern"] = "available"
        mechanisms["uia_is_read_only"] = value_pattern.CurrentIsReadOnly
    except Exception as err:
        mechanisms["uia_value_pattern"] = f"{type(err).__name__}: {err}"
else:
    mechanisms["uia_value_pattern"] = "no UIA element"

ia2_object = getattr(f, "IAccessibleObject", None)
mechanisms["has_IAccessibleObject"] = ia2_object is not None
if ia2_object is not None:
    try:
        from comInterfaces import IAccessible2Lib as IA2
        editable = ia2_object.QueryInterface(IA2.IAccessibleEditableText)
        mechanisms["ia2_editable_text"] = "available"
        mechanisms["ia2_editable_methods"] = [
            n for n in dir(editable) if "eplace" in n or "etText" in n or "nsert" in n
        ]
    except Exception as err:
        mechanisms["ia2_editable_text"] = f"{type(err).__name__}: {err}"

out["mechanisms"] = mechanisms
out["liveness"] = {
    "isCurrent": getattr(f, "isCurrent", "missing"),
    "isDefunct": getattr(f, "isDefunct", "missing"),
    "isAlive": getattr(f, "isAlive", "missing"),
    "has_invalidateCache": hasattr(f, "invalidateCache"),
}
print("JSON" + json.dumps(out, default=str))
'''

REC_INSTALL = r'''
import builtins, eventHandler, time
state = {"events": [], "t0": time.perf_counter()}
if not getattr(builtins, "_NVDA_ORIG_EXECUTE_EVENT", None):
    builtins._NVDA_ORIG_EXECUTE_EVENT = eventHandler.executeEvent
    def recording_execute_event(eventName, obj, **kwargs):
        if len(state["events"]) < 8000:
            state["events"].append((eventName, type(obj).__name__))
        return builtins._NVDA_ORIG_EXECUTE_EVENT(eventName, obj, **kwargs)
    eventHandler.executeEvent = recording_execute_event
    builtins._NVDA_EVENT_STATE = state
builtins._NVDA_EVENT_STATE["events"].clear()
builtins._NVDA_EVENT_STATE["t0"] = time.perf_counter()
print("installed")
'''

REC_RATE = r'''
import builtins, collections, json, time
state = builtins._NVDA_EVENT_STATE
elapsed = time.perf_counter() - state["t0"]
counts = collections.Counter(e[0] for e in state["events"])
print("JSON" + json.dumps({
    "seconds": round(elapsed, 2),
    "events": len(state["events"]),
    "per_second": round(len(state["events"]) / elapsed, 1),
    "kinds": dict(counts.most_common(10)),
}))
state["events"].clear()
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


# --- plumbing -----------------------------------------------------------------------


class BridgeError(RuntimeError):
    pass


def _post(action: str, params: dict | None = None, timeout: float = 120) -> dict:
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
        raise BridgeError(f"execute_python failed: {error.get('name')} {error.get('message')}")
    return (result.get("data") or {}).get("stdout", "").strip()


def jload(output: str) -> dict:
    for line in reversed(output.splitlines()):
        if line.startswith("JSON"):
            return json.loads(line[4:])
    return {"raw": output}


def window_title() -> str:
    return ((_post("get_window_title").get("data")) or {}).get("title", "").translate(ZWSP)


def log_text() -> str:
    """Read the page's own log div through NVDA, so the check is the user's view."""
    return py(
        "import api\n"
        "f = api.getFocusObject()\n"
        "root = f\n"
        "for _ in range(6):\n"
        "    if root.parent is None or str(root.role) in ('52','1'):\n"
        "        break\n"
        "    root = root.parent\n"
        "import json\n"
        "hits = []\n"
        "stack = [(root, 0)]\n"
        "visited = 0\n"
        "while stack and visited < 350:\n"
        "    o, d = stack.pop(0)\n"
        "    visited += 1\n"
        "    if str(o.role) == '7':\n"
        "        try: hits.append(o.name)\n"
        "        except Exception: pass\n"
        "    if d < 14:\n"
        "        try: ch = o.children\n"
        "        except Exception: ch = []\n"
        "        for c in ch: stack.append((c, d+1))\n"
        "print('JSON' + json.dumps(hits[:8], default=str))\n"
    )


def find_browser_exe(name: str) -> str:
    for candidate in BROWSERS[name]:
        if Path(candidate).is_file():
            return candidate
    raise BridgeError(f"{name} not found")


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


def close_window() -> None:
    user32 = ctypes.windll.user32
    WM_CLOSE = 0x0010

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def callback(hwnd, _param):
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if WINDOW_MATCH in buffer.value:
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return True

    user32.EnumWindows(callback, 0)
    time.sleep(1.0)


def main() -> int:
    scratch = REPO / ".probe"
    scratch.mkdir(exist_ok=True)
    form = scratch / "action_probe.html"
    form.write_text(FORM_HTML, encoding="utf-8")
    url = "file:///" + quote(form.as_posix())

    try:
        py(REC_INSTALL)
    except BridgeError as err:
        print(f"cannot run the probe: {err}", file=sys.stderr)
        print("Enable developer mode in NVDA Settings > NVDA MCP Bridge.", file=sys.stderr)
        return 2

    try:
        title = open_window(url)
        print(f"window: {title}")
        if WINDOW_MATCH not in title:
            print("probe window did not take focus; aborting", file=sys.stderr)
            return 1
        time.sleep(0.5)

        print("\n== 1. walking the tree to find a node (what a ref resolver must do)")
        print(jload(py(FIND_NODE)))

        print("\n== 2. set_value surface on the focused text field")
        print(json.dumps(jload(py(SET_VALUE_SURFACE)), indent=1))

        print("\n== 3. doAction on nodes that do NOT have focus")
        for label, role in (("button", "9"), ("checkbox", "5")):
            before_log = log_text()
            before_title = window_title()
            result = jload(py(invoke_probe(role)))
            time.sleep(0.3)
            after_log = log_text()
            after_title = window_title()
            print(f"  {label}: {result}")
            print(f"    log {before_log} -> {after_log}")
            print(f"    title changed: {before_title != after_title}")

        print("\n== 4. object churn for the same DOM node")
        samples = []
        for _ in range(12):
            samples.append(jload(py(SAMPLE_FOCUS)))
            time.sleep(0.15)
        instances = {s.get("instance") for s in samples}
        print(
            {
                "samples": len(samples),
                "distinct_instances": len(instances),
                "names": sorted({str(s.get("name")) for s in samples}),
                "keys": sorted(samples[0]),
            }
        )

        print("\n== 5. projection cost while the page updates itself")
        print(jload(py(PROJECTION_COST)))

        print("\n== 6. event rate from a self-updating page (25 ms text, 400 ms live region)")
        py(REC_INSTALL)  # reset the counter
        time.sleep(3.0)
        print(jload(py(REC_RATE)))
    finally:
        close_window()
        py(REC_REMOVE)
        try:
            form.unlink()
            scratch.rmdir()
        except OSError:
            pass

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", choices=sorted(BROWSERS), default="edge")
    args = parser.parse_args()
    try:
        sys.exit(main())
    except BridgeError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
