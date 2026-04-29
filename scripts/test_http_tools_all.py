#!/usr/bin/env python3
"""Exercise all safe NVDA bridge tools via HTTP endpoints."""

import json
import urllib.request
from urllib.error import HTTPError, URLError

BASE_URL = "http://127.0.0.1:8765"
TOKEN_HEADER = "X-NVDA-Bridge-Token"
BRIDGE_TOKEN = "nvda-bridge-secret-change-me"
HEADERS = {
    TOKEN_HEADER: BRIDGE_TOKEN,
    "Content-Type": "application/json",
}

SAFE_DEFAULTS = {
    "direction": "next",
    "text": "test",
    "message": "test",
    "gesture": "down",
    "repeat": 1,
    "delay_ms": 0,
    "target": "focus",
    "include_relations": False,
    "max_depth": 1,
    "max_children": 1,
    "max_ancestors": 1,
    "source": "focus",
    "scope": "running",
    "include_logs": False,
    "log_lines": 1,
    "include_tree": False,
    "tree_depth": 1,
    "tree_max_children": 1,
    "addon_scope": "running",
    "disable_addons": False,
    "debug_logging": False,
    "max_lines": 1,
    "filter_level": "INFO",
    "use_tail": True,
    "code": "print('ok')",
    "snapshot_vars": False,
    "capture_vars": [],
}

SKIP_CATEGORIES = {"developer", "system"}
SKIP_TOOLS = {"send_keyboard"}
TOOL_ALTERNATIVES = {
    "move_focus": [
        {"direction": "next"},
        {"direction": "previous"},
    ],
}


def request(path, method="GET", body=None):
    url = BASE_URL + path
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in HEADERS.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(text)
            except json.JSONDecodeError:
                return resp.status, text
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, {"error": body}
    except URLError as exc:
        return None, {"error": str(exc)}
    except Exception as exc:
        return None, {"error": str(exc)}


def build_params(input_schema):
    if not isinstance(input_schema, dict):
        return None
    required = set(input_schema.get("required", []))
    if not required:
        return {}
    params = {}
    for field in required:
        if field in SAFE_DEFAULTS:
            params[field] = SAFE_DEFAULTS[field]
        else:
            # skip tools with unknown required fields
            return None
    return params


def main():
    status, tools_payload = request("/tools")
    if status != 200:
        print("Failed to fetch /tools", status, tools_payload)
        return 1

    tools = tools_payload.get("tools", [])
    errors = []
    successes = []
    skipped = []

    for tool in tools:
        name = tool.get("name")
        category = tool.get("category")
        if category in SKIP_CATEGORIES or name in SKIP_TOOLS:
            skipped.append((name, category, "skipped category or unsafe tool"))
            continue

        input_schema = tool.get("inputSchema") or {}
        params = build_params(input_schema)
        if params is None:
            skipped.append((name, category, input_schema.get("required", [])))
            continue

        if name in TOOL_ALTERNATIVES:
            result = None
            for alt_params in TOOL_ALTERNATIVES[name]:
                payload = {
                    "request_id": f"http-test-{name}",
                    "action": name,
                    "params": alt_params,
                }
                status, body = request("/action", method="POST", body=payload)
                if status == 200 and isinstance(body, dict) and body.get("success"):
                    successes.append(name)
                    print(f"OK: {name} with {alt_params}")
                    result = True
                    break
                if isinstance(body, dict) and body.get("error") and "No next object available" in body["error"]:
                    continue
                result = False
                break
            if result is False:
                errors.append((name, status, body))
                print(f"FAIL: {name} -> {status}")
            elif result is None:
                skipped.append((name, category, "no valid direction available"))
            continue

        payload = {
            "request_id": f"http-test-{name}",
            "action": name,
            "params": params,
        }
        status, body = request("/action", method="POST", body=payload)
        if status == 200 and isinstance(body, dict) and body.get("success"):
            successes.append(name)
            print(f"OK: {name}")
        else:
            errors.append((name, status, body))
            print(f"FAIL: {name} -> {status}")

    print("\nSummary")
    print("======")
    print(f"Successes: {len(successes)}")
    print(f"Errors: {len(errors)}")
    print(f"Skipped: {len(skipped)}")
    if errors:
        print("\nErrors detail:")
        for name, status, body in errors:
            print(name, status)
            print(body)
    if skipped:
        print("\nSkipped tools:")
        for item in skipped:
            print(item)

    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
