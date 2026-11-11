#!/usr/bin/env python3
"""Probe what the UIA search surface actually offers inside NVDA.

`locate` wants to search with the platform rather than by walking NVDA objects,
because every `children` call crosses into the application's process (~9 ms on
Chromium). This answers, against the running NVDA:

1. Is there a `CUIAutomation` object to create conditions with?
2. Which condition/property APIs and constants exist at module level?
3. Does a real focused object expose `UIAElement`, and is it a raw
   `IUIAutomationElement`?
4. How long does `FindFirst`/`FindAll` take on the current window?

Requires developer mode. Read-only: creates conditions, never activates them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from action_surface_probe import BridgeError, _post, jload, py  # noqa: E402

INVENTORY = r'''
import json
import api

out = {}

try:
    import UIAHandler
    out["uiahandler"] = True
except Exception as exc:
    UIAHandler = None
    out["uiahandler"] = False
    out["uiahandler_error"] = f"{type(exc).__name__}: {exc}"

if UIAHandler is not None:
    names = [n for n in dir(UIAHandler) if not n.startswith("__")]
    out["condition_api"] = [n for n in names if "ondition" in n]
    out["handler"] = hasattr(UIAHandler, "handler")
    handler = getattr(UIAHandler, "handler", None)
    out["handler_type"] = type(handler).__name__ if handler is not None else None
    if handler is not None:
        out["handler_public"] = [n for n in dir(handler) if not n.startswith("_")][:40]
        client = getattr(handler, "clientObject", None)
        out["client_type"] = type(client).__name__ if client is not None else None
        if client is not None:
            out["client_methods"] = [
                n for n in dir(client) if n.startswith("Create") or n.startswith("Find")
            ]
    out["property_ids"] = sorted(n for n in names if n.endswith("PropertyId"))[:40]
    out["property_id_count"] = len([n for n in names if n.endswith("PropertyId")])
    out["control_types"] = sorted(n for n in names if n.startswith("UIA_") and "ControlType" in n)
    out["tree_scopes"] = sorted(n for n in names if "TreeScope" in n)
    out["name_ids"] = sorted(n for n in names if n in ("UIA_NamePropertyId", "UIA_AutomationIdPropertyId", "UIA_ControlTypePropertyId"))

f = api.getFocusObject()
element = getattr(f, "UIAElement", None)
out["focus_role"] = str(getattr(f, "role", None))
out["focus_name"] = getattr(f, "name", None)
out["focus_has_uia"] = element is not None
if element is not None:
    out["element_type"] = type(element).__name__
    out["element_methods"] = [n for n in dir(element) if n.startswith("Find")]
    out["is_raw_uia"] = element.__class__.__module__

print("JSON" + json.dumps(out, default=str))
'''

TIMING = r'''
import json, time
import api
import UIAHandler

handler = UIAHandler.handler
client = getattr(handler, "clientObject", None)
out = {}
f = api.getFocusObject()
element = getattr(f, "UIAElement", None)
if client is None or element is None:
    out["skip"] = "no client or no UIAElement"
else:
    try:
        true_condition = client.CreateTrueCondition()
        start = time.perf_counter()
        first = element.FindFirst(UIAHandler.TreeScope_Descendants, true_condition)
        out["findfirst_ms"] = round((time.perf_counter() - start) * 1000, 1)
        out["findfirst_found"] = first is not None
    except Exception as exc:
        out["findfirst_error"] = f"{type(exc).__name__}: {exc}"
    try:
        start = time.perf_counter()
        all_nodes = element.FindAll(UIAHandler.TreeScope_Descendants, true_condition)
        out["findall_ms"] = round((time.perf_counter() - start) * 1000, 1)
        out["findall_count"] = int(all_nodes.Length) if all_nodes is not None else -1
    except Exception as exc:
        out["findall_error"] = f"{type(exc).__name__}: {exc}"
    try:
        condition = client.CreatePropertyCondition(UIAHandler.UIA_ControlTypePropertyId, UIAHandler.UIA_ButtonControlTypeId)
        start = time.perf_counter()
        buttons = element.FindAll(UIAHandler.TreeScope_Descendants, condition)
        out["buttons_ms"] = round((time.perf_counter() - start) * 1000, 1)
        out["button_count"] = int(buttons.Length) if buttons is not None else -1
    except Exception as exc:
        out["buttons_error"] = f"{type(exc).__name__}: {exc}"

print("JSON" + json.dumps(out, default=str))
'''


def main() -> int:
    try:
        inventory = jload(py(INVENTORY))
        timing = jload(py(TIMING))
    except BridgeError as err:
        print(f"probe failed: {err}")
        return 1
    print(json.dumps({"inventory": inventory, "timing": timing}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
