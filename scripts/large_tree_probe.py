#!/usr/bin/env python3
"""Measure the two things that could still invalidate the action layer.

1. Ref resolution on a large tree: how long does it take to reach a node deep in
   a 500-link page, by walking, compared with a retained reference?
2. Whether a UIA control exposes ValuePattern, i.e. whether `set_value` has a
   real platform path outside Chromium's IA2 objects.

Both run in one browser session. Requires developer mode.
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
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
WINDOW_MATCH = "NVDA Tree Probe"
ZWSP = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)

ITEMS = 500
FORM_HTML = (
    "<!doctype html><html lang=en><head><meta charset=utf-8>"
    "<title>NVDA Tree Probe</title></head><body>"
    "<h1>NVDA Tree Probe</h1>"
    '<label for=q>Filter</label><input id=q type=text autofocus>'
    "<ul>"
    + "".join(f'<li><a href="#{i}">Item {i}</a></li>' for i in range(ITEMS))
    + "</ul></body></html>"
)

# BFS from the containing document to a node whose name is `target`, counting
# what a resolver would have to touch.
WALK = r'''
import json, time
import api

TARGET = __TARGET__

start = time.perf_counter()
f = api.getFocusObject()
root = f
for _ in range(6):
    if root.parent is None or str(root.role) in ("52", "1"):
        break
    root = root.parent

visited = 0
found_at = None
MAX_NODES = 250
stack = [(root, 0)]
while stack and visited < MAX_NODES:
    obj, depth = stack.pop(0)
    visited += 1
    try:
        name = obj.name
    except Exception:
        name = None
    if name == TARGET:
        found_at = (visited, depth)
        break
    if depth < 20:
        try:
            children = obj.children
        except Exception:
            children = []
        for child in children:
            stack.append((child, depth + 1))

elapsed = round((time.perf_counter() - start) * 1000, 1)
print("JSON" + json.dumps({
    "target": TARGET,
    "found": found_at is not None,
    "visited": visited,
    "budget": MAX_NODES,
    "depth": found_at[1] if found_at else None,
    "walk_ms": elapsed,
    "ms_per_node": round(elapsed / visited, 2) if visited else None,
    "remaining_nodes": len(stack),
}))
'''

# The other half of the comparison: reaching the same node through a reference
# that was captured earlier costs a property read.
BY_REFERENCE = r'''
import json, time
import api

TARGET = __TARGET__

f = api.getFocusObject()
root = f
for _ in range(6):
    if root.parent is None or str(root.role) in ("52", "1"):
        break
    root = root.parent

# capture once
target_obj = None
stack = [(root, 0)]
visited = 0
while stack and target_obj is None and visited < 4000:
    obj, depth = stack.pop(0)
    visited += 1
    try:
        if obj.name == TARGET:
            target_obj = obj
            break
    except Exception:
        pass
    if depth < 20:
        try:
            children = obj.children
        except Exception:
            children = []
        for child in children:
            stack.append((child, depth + 1))

timings = []
if target_obj is None:
    print("JSON" + json.dumps({"error": "not found"}))
else:
    for _ in range(20):
        t0 = time.perf_counter()
        try:
            target_obj.invalidateCache()
        except Exception:
            pass
        name = target_obj.name
        role = str(target_obj.role)
        timings.append(round((time.perf_counter() - t0) * 1000, 3))
    timings.sort()
    print("JSON" + json.dumps({
        "name": name,
        "role": role,
        "median_ms": timings[len(timings) // 2],
        "max_ms": timings[-1],
    }))
'''

VALUE_PATTERN = r'''
import json
import api

f = api.getFocusObject()
out = {"role": str(f.role), "name": f.name, "type": type(f).__name__}
element = getattr(f, "UIAElement", None)
out["has_UIAElement"] = element is not None
if element is not None:
    try:
        import UIAHandler
        pattern = element.GetCurrentPattern(UIAHandler.UIA_ValuePatternId)
        value = pattern.QueryInterface(UIAHandler.IUIAutomationValuePattern)
        out["value_pattern"] = "available"
        out["current_value"] = value.CurrentValue[:40]
        out["is_read_only"] = bool(value.CurrentIsReadOnly)
    except Exception as err:
        out["value_pattern"] = f"{type(err).__name__}: {err}"
print("JSON" + json.dumps(out, default=str))
'''


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
    except urllib.error.HTTPError as err:
        body = err.read().decode(errors="replace")[:400]
        raise BridgeError(f"{action}: HTTP {err.code} {body}") from err
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
    page = scratch / "tree_probe.html"
    page.write_text(FORM_HTML, encoding="utf-8")
    url = "file:///" + quote(page.as_posix())

    try:
        py("print('ok')")
    except BridgeError as err:
        print(f"cannot run the probe: {err}", file=sys.stderr)
        return 2

    try:
        py(
            "import subprocess, time\n"
            f"subprocess.Popen([{EDGE!r}, '--new-window', {url!r}])\n"
            "time.sleep(2.5)\n"
            "print('launched')"
        )
        time.sleep(1.0)
        title = window_title()
        print(f"window: {title}")
        if WINDOW_MATCH not in title:
            print("probe window did not take focus; aborting", file=sys.stderr)
            return 1
        time.sleep(0.5)

        print(f"\n== walking a {ITEMS}-item page to the last item")
        print(jload(py(WALK.replace("__TARGET__", repr(f"Item {ITEMS - 1}")))))

        print("\n== reaching the same node through a captured reference")
        print(jload(py(BY_REFERENCE.replace("__TARGET__", repr(f"Item {ITEMS - 1}")))))

        print("\n== doAction on that node, found only by walking")
        print(
            jload(
                py(
                    "import json\n"
                    "import api\n"
                    f"target = {f'Item {ITEMS - 1}'!r}\n"
                    "f = api.getFocusObject()\n"
                    "root = f\n"
                    "for _ in range(6):\n"
                    "    if root.parent is None or str(root.role) in ('52','1'):\n"
                    "        break\n"
                    "    root = root.parent\n"
                    "stack = [(root, 0)]\n"
                    "found = None\n"
                    "visited = 0\n"
                    "while stack and found is None and visited < 4000:\n"
                    "    o, d = stack.pop(0)\n"
                    "    visited += 1\n"
                    "    try:\n"
                    "        if o.name == target and str(o.role) == '30':\n"
                    "            found = o; break\n"
                    "    except Exception: pass\n"
                    "    if d < 20:\n"
                    "        try: ch = o.children\n"
                    "        except Exception: ch = []\n"
                    "        for c in ch: stack.append((c, d+1))\n"
                    "out = {'visited': visited, 'found': found is not None}\n"
                    "if found is not None:\n"
                    "    out['action_count'] = found.actionCount\n"
                    "    out['action_name'] = found.getActionName(found.defaultActionIndex)\n"
                    "print('JSON' + json.dumps(out, default=str))\n"
                )
            )
        )

        print("\n== UIA ValuePattern on the address bar (Ctrl+L), a UIA-only control")
        _post("send_keyboard", {"gesture": "control+l"})
        time.sleep(0.8)
        print(jload(py(VALUE_PATTERN)))
    finally:
        close_window()
        try:
            page.unlink()
            scratch.rmdir()
        except OSError:
            pass

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", default="edge")
    parser.parse_args()
    try:
        sys.exit(main())
    except BridgeError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
