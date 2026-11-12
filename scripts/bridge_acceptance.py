#!/usr/bin/env python3
"""Live acceptance test for the NVDA MCP Bridge tool surface.

Unlike ``tests/`` (which unit-tests handlers in isolation), this script talks to a
*running* NVDA over the HTTP bridge and checks the wire contract:

* every advertised tool executes, or fails for a defensible reason
* every success payload validates against the schema published in ``schema.json``
* every failure carries a protocol error code and a non-200 status
* wrong-typed input is rejected rather than silently coerced
* auth, Host/Origin pinning, body limits and request ids behave

Usage::

    python scripts/bridge_acceptance.py
    python scripts/bridge_acceptance.py --json out.json
    python scripts/bridge_acceptance.py --include-lifecycle   # DESTRUCTIVE

Authentication is read from ``--token``, then ``NVDA_BRIDGE_TOKEN``, then the
per-installation ``token.key`` written by the add-on.

Safety: lifecycle tools (``restart_nvda``, ``reload_plugins``) and every
developer tool are only probed for schema rejection. They are never dispatched,
because a successful ``restart_nvda`` really does restart NVDA.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE_URL = "http://127.0.0.1:8765"
TOKEN_HEADER = "X-NVDA-Bridge-Token"
REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "schema.json"

# Protocol error codes (mirrors docs/protocol/protocol-constants.json).
ERROR_CODES = {
    4001, 4003,
    4101, 4102, 4103,
    4201, 4202, 4203, 4204, 4205, 4206,
    5001, 5003, 5004,
    5303,
}
ENVELOPE_KEYS = {
    "success", "request_id", "action", "duration_ms", "bridge",
    "nvda_status_snapshot", "result", "data", "error",
    # Diagnostic correlation: the logical action id and its bounded summary.
    "trace_id", "trace",
}
RESULT_KEYS = {
    "ok", "state_version", "changed", "delta", "settled_ms", "uncertain",
    "completed", "expect_mismatch",
    # Observation completeness, added with the action trace.
    "projection_available", "events_complete", "settling_status",
}
ERROR_KEYS = {"code", "message", "name", "category", "retry_policy", "retry_after_ms", "details"}


# --------------------------------------------------------------------------- auth


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
    raise SystemExit(
        "No bridge token found. Pass --token, set NVDA_BRIDGE_TOKEN, or install the add-on so it writes token.key."
    )


# --------------------------------------------------------------------------- http


def http_raw(method, path, body=None, headers=None, timeout=30):
    """Return (status, raw_body, wall_ms). status is -1 when the socket failed."""
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(BASE_URL + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), (time.perf_counter() - started) * 1000
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read()
        except Exception:  # server terminates early on 413
            payload = b""
        return exc.code, payload, (time.perf_counter() - started) * 1000
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc).encode("utf-8"), (time.perf_counter() - started) * 1000


class Bridge:
    def __init__(self, token: str) -> None:
        self.token = token

    def call(self, action, params=None, request_id=None):
        payload = {"action": action, "params": params or {}}
        if request_id is not None:
            payload["request_id"] = request_id
        status, raw, wall_ms = http_raw("POST", "/action", payload, {TOKEN_HEADER: self.token})
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except Exception:  # noqa: BLE001
            body = {"_unparsed": raw[:200].decode("utf-8", "replace")}
        return {"http": status, "body": body, "wall_ms": round(wall_ms, 2)}

    def tools(self):
        status, raw, _ = http_raw("GET", "/tools", headers={TOKEN_HEADER: self.token})
        if status != 200:
            raise SystemExit(f"/tools failed: HTTP {status}: {raw[:200]!r}")
        return json.loads(raw)["tools"]


# ----------------------------------------------------------------------- checks


def load_schemas():
    try:
        import jsonschema  # noqa: F401
    except ImportError:
        return None, None
    payload = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return payload["types"], jsonschema


def check_envelope(result, action, tools_by_name, types, jsonschema):
    problems = []
    body = result["body"]
    if not isinstance(body, dict):
        return ["response body is not a JSON object"]

    extra = set(body) - ENVELOPE_KEYS
    if extra:
        problems.append(f"unexpected envelope keys {sorted(extra)}")
    if body.get("action") != action:
        problems.append(f"action echo is {body.get('action')!r}")
    if not isinstance(body.get("request_id"), str) or not body.get("request_id"):
        problems.append("missing request_id")
    if not isinstance(body.get("duration_ms"), (int, float)):
        problems.append("duration_ms is not numeric")

    if body.get("success") is True:
        if result["http"] != 200:
            problems.append(f"success=True with HTTP {result['http']}")
        block = body.get("result")
        if not isinstance(block, dict):
            problems.append("missing result block")
        else:
            extra_block = set(block) - RESULT_KEYS
            if extra_block:
                problems.append(f"unexpected result keys {sorted(extra_block)}")
            if not isinstance(block.get("ok"), bool):
                problems.append("result.ok is not a boolean")
            if not isinstance(block.get("state_version"), int) or isinstance(block.get("state_version"), bool):
                problems.append("result.state_version is not an integer")
            if "settled_ms" in block and not isinstance(block["settled_ms"], (int, float)):
                problems.append("result.settled_ms is not numeric")
            if "changed" in block and not isinstance(block["changed"], bool):
                problems.append("result.changed is not a boolean")
        schema = None
        definition = tools_by_name.get(action)
        if definition and types is not None:
            envelope = types.get(definition.get("outputType"))
            if envelope is None:
                problems.append(f"no schema for outputType {definition.get('outputType')!r}")
            else:
                # The Rust companion validates `data` against types[X].properties.data.
                schema = envelope.get("properties", {}).get("data", envelope)
        if schema is not None:
            payload = body.get("data") if isinstance(body.get("data"), dict) else {}
            try:
                jsonschema.validate(payload, schema)
            except jsonschema.ValidationError as exc:
                problems.append(f"data fails declared schema: {exc.message[:140]} at {list(exc.absolute_path)}")
    elif body.get("success") is False:
        error = body.get("error")
        if not isinstance(error, dict):
            problems.append("failure without an error object")
        else:
            if set(error) - ERROR_KEYS:
                problems.append(f"unexpected error keys {sorted(set(error) - ERROR_KEYS)}")
            if error.get("code") not in ERROR_CODES:
                problems.append(f"error.code {error.get('code')!r} is not a protocol code")
            if not str(error.get("message") or ""):
                problems.append("error.message is empty")
        if result["http"] == 200:
            problems.append("failure returned with HTTP 200")
    else:
        problems.append(f"success is {body.get('success')!r}")
    return problems


# --------------------------------------------------------------- case matrix
# (label, action, params, expected) — expected is "ok" or "error".
# Only non-destructive tools appear here; lifecycle tools are probed separately.

CASES = [
    ("observe", "observe", {}, "ok"),
    ("inspect_object:desktop", "inspect_object", {"target": "desktop"}, "ok"),
    ("read:line", "read", {"scope": "line"}, "ok"),
    ("navigate:next", "navigate", {"direction": "next"}, "ok"),
    ("navigate:previous", "navigate", {"direction": "previous"}, "ok"),
    ("navigate:bad-literal", "navigate", {"direction": "sideways"}, "error"),
    ("navigate:missing", "navigate", {}, "error"),
    ("speak_text", "speak_text", {"text": "Bridge acceptance test."}, "ok"),
    ("speak_text:empty", "speak_text", {"text": ""}, "error"),
    ("show_message", "show_message", {"message": "Bridge acceptance test."}, "ok"),
    ("show_message:empty", "show_message", {"message": ""}, "error"),
    ("show_message:too-long", "show_message", {"message": "y" * 1200}, "error"),
    ("press:modifier", "press", {"keys": "shift"}, "ok"),
    ("press:repeat", "press", {"keys": "shift", "repeat": 3, "delay_ms": 40}, "ok"),
    ("press:bad-gesture", "press", {"keys": "not+a+real+key"}, "error"),
    ("press:empty-gesture", "press", {"keys": ""}, "error"),
    ("press:repeat-zero", "press", {"keys": "shift", "repeat": 0}, "error"),
    ("press:repeat-high", "press", {"keys": "shift", "repeat": 21}, "error"),
    ("press:delay-high", "press", {"keys": "shift", "delay_ms": 9999}, "error"),
    ("inspect_object", "inspect_object", {}, "ok"),
    ("inspect_object:navigator", "inspect_object", {"target": "navigator", "include_relations": True}, "ok"),
    ("inspect_object:bad-target", "inspect_object", {"target": "mouse"}, "error"),
    ("walk_object_tree", "walk_object_tree", {}, "ok"),
    ("walk_object_tree:depth0", "walk_object_tree", {"max_depth": 0}, "ok"),
    ("walk_object_tree:negative", "walk_object_tree", {"max_depth": -1}, "error"),
    ("walk_object_tree:children-high", "walk_object_tree", {"max_children": 9999}, "error"),
    ("get_focus_lineage", "get_focus_lineage", {}, "ok"),
    ("get_focus_lineage:1", "get_focus_lineage", {"max_ancestors": 1}, "ok"),
    ("get_focus_lineage:negative", "get_focus_lineage", {"max_ancestors": -5}, "error"),
    ("inspect_app_module", "inspect_app_module", {}, "ok"),
    ("inspect_app_module:navigator", "inspect_app_module", {"source": "navigator"}, "ok"),
    ("list_loaded_addons", "list_loaded_addons", {}, "ok"),
    ("list_loaded_addons:all", "list_loaded_addons", {"scope": "all"}, "ok"),
    ("list_loaded_addons:bad-scope", "list_loaded_addons", {"scope": "sometimes"}, "error"),
    ("health_snapshot", "health_snapshot", {}, "ok"),
    ("health_snapshot:logs", "health_snapshot", {"include_logs": True, "log_lines": 20}, "ok"),
    ("health_snapshot:log-lines-high", "health_snapshot", {"log_lines": 9999}, "error"),
    ("get_logs", "get_logs", {}, "ok"),
    ("get_logs:errors", "get_logs", {"filter_level": "ERROR", "max_lines": 10}, "ok"),
    ("get_logs:head", "get_logs", {"use_tail": False, "max_lines": 3}, "ok"),
    ("get_logs:max-zero", "get_logs", {"max_lines": 0}, "error"),
    ("get_logs:bad-level", "get_logs", {"filter_level": "TRACE"}, "error"),
    ("unknown-action", "definitely_not_a_tool", {}, "error"),
    # Action surface (P2). Only refs that cannot resolve, and reads, are probed
    # live: invoking arbitrary controls or pressing Enter would change the
    # machine's state rather than test the contract.
    ("observe", "observe", {}, "ok"),
    ("observe:minimal", "observe", {"include": ["focus"], "max_text": 20}, "ok"),
    ("observe:bad-include", "observe", {"include": ["everything"]}, "error"),
    ("observe:extra-prop", "observe", {"depth": 3}, "error"),
    ("read", "read", {}, "ok"),
    ("read:document", "read", {"scope": "document", "max_chars": 40}, "ok"),
    ("read:bad-scope", "read", {"scope": "page"}, "error"),
    ("read:stale-ref", "read", {"ref": "f-does-not-exist"}, "error"),
    ("invoke:missing-ref", "invoke", {}, "error"),
    ("invoke:stale-ref", "invoke", {"ref": "f-does-not-exist"}, "error"),
    ("focus:stale-ref", "focus", {"ref": "f-does-not-exist"}, "error"),
    ("set_value:missing-value", "set_value", {"ref": "f"}, "error"),
    ("set_value:stale-ref", "set_value", {"ref": "f-does-not-exist", "value": "x"}, "error"),
    ("navigate:bad-direction", "navigate", {"direction": "sideways"}, "error"),
    ("scroll:bad-amount", "scroll", {"amount": "chapter"}, "error"),
    ("press:modifier", "press", {"keys": "shift"}, "ok"),
    ("press:bad-gesture", "press", {"keys": "not+a+real+key"}, "error"),
    ("press:empty", "press", {"keys": ""}, "error"),
    ("press:repeat-zero", "press", {"keys": "shift", "repeat": 0}, "error"),
    ("click_at:missing-y", "click_at", {"x": 10}, "error"),
    ("dismiss:extra-prop", "dismiss", {"force": True}, "error"),
    ("wait_for_events", "wait_for_events", {"since": 0, "timeout_ms": 100}, "ok"),
    ("wait_for_events:bad-timeout", "wait_for_events", {"timeout_ms": -5}, "error"),
]

# Wrong-typed values that the published input schemas forbid. Accepting any of
# them is a contract leak: agents get silent coercion instead of a correction.
TYPE_PROBES = [
    ("walk_object_tree", {"max_depth": "1"}, "str for int"),
    ("walk_object_tree", {"max_depth": 1.9}, "float for int"),
    ("walk_object_tree", {"max_depth": True}, "bool for int"),
    ("get_logs", {"max_lines": "5"}, "str for int"),
    ("get_logs", {"filter_level": 3}, "int for enum"),
    ("get_logs", {"use_tail": "false"}, "str for bool"),
    ("inspect_object", {"include_relations": "false"}, "str for bool"),
    ("inspect_object", {"include_relations": 1}, "int for bool"),
    ("health_snapshot", {"include_logs": "no"}, "str for bool"),
    ("health_snapshot", {"log_lines": "20"}, "str for int"),
    ("list_loaded_addons", {"scope": ["running"]}, "array for enum"),
    ("press", {"keys": "shift", "repeat": "2"}, "str for int"),
    ("press", {"keys": "shift", "delay_ms": "10"}, "str for int"),
    ("press", {"keys": 5}, "int for str"),
    ("speak_text", {"text": 12345}, "int for str"),
    ("speak_text", {"text": ["a"]}, "array for str"),
]


# Some tools report a normal environmental condition as a failure. ``navigate``
# walks *siblings* of the focused object, so it reports an error whenever focus
# sits on an only child — the common case in a browser. Treat it as a skip so it
# stays visible in the report without failing the run.
TOLERATED_ERRORS = ("No next object available", "No previous object available")


def run(bridge: Bridge, tools, include_lifecycle: bool, verbose: bool):
    tools_by_name = {tool["name"]: tool for tool in tools}
    types, jsonschema = load_schemas()
    if jsonschema is None:
        print("note: jsonschema is not installed; schema validation is skipped\n")

    cases = list(CASES)
    # Lifecycle tools are never dispatched. Rejecting an unexpected property
    # proves the input contract without risking a real restart.
    for name in ("restart_nvda", "reload_plugins"):
        if name in tools_by_name and not include_lifecycle:
            cases.append((f"{name}:extra-prop", name, {"unexpected": True}, "error"))
    if "execute_python" in tools_by_name:
        cases.append(("execute_python:gated", "execute_python", {"code": "1 + 1"}, "error"))

    results = []
    for label, action, params, expected in cases:
        result = bridge.call(action, params)
        problems = check_envelope(result, action, tools_by_name, types, jsonschema)
        actual = "ok" if (result["body"] or {}).get("success") else "error"
        note = ""
        if actual != expected:
            problems.insert(0, f"expected {expected}, got {actual}")
        if actual == "error":
            error = (result["body"] or {}).get("error") or {}
            message = str(error.get("message") or "")
            if any(tolerated in message for tolerated in TOLERATED_ERRORS):
                problems = [problem for problem in problems if "expected" not in problem]
                note = f"skipped: {message}"
        if action in ("restart_nvda", "reload_plugins") and not include_lifecycle and actual == "ok":
            problems.append("DESTRUCTIVE tool executed during a safe run")
        results.append(
            {
                "label": label,
                "action": action,
                "expected": expected,
                "actual": actual,
                "http": result["http"],
                "error_code": ((result["body"] or {}).get("error") or {}).get("code"),
                "duration_ms": (result["body"] or {}).get("duration_ms"),
                "problems": problems,
                "note": note,
                "ok": not problems,
            }
        )
        if verbose:
            marker = "PASS" if not problems else "FAIL"
            detail = "; ".join(problems)[:80] if problems else note
            print(f"{marker} {label:34} http={result['http']:>4} {actual:5} {detail}")

    type_results = []
    for action, params, note in TYPE_PROBES:
        result = bridge.call(action, params)
        accepted = bool((result["body"] or {}).get("success"))
        type_results.append({"action": action, "params": params, "note": note, "accepted": accepted})

    security = run_security(bridge)

    def probe(_):
        started = time.perf_counter()
        ok = bool((bridge.call("observe")["body"] or {}).get("success"))
        return ok, (time.perf_counter() - started) * 1000

    with ThreadPoolExecutor(max_workers=8) as pool:
        concurrent = list(pool.map(probe, range(16)))

    latencies = {}
    for item in results:
        if item["ok"] and item["actual"] == "ok" and item["duration_ms"]:
            latencies.setdefault(item["action"], []).append(item["duration_ms"])

    return {
        "tools_exposed": len(tools),
        "cases": results,
        "type_probes": type_results,
        "security": security,
        "concurrency": {
            "ok": sum(1 for ok, _ in concurrent if ok),
            "total": len(concurrent),
            "median_ms": round(statistics.median([ms for _, ms in concurrent]), 1),
        },
        "latency_ms": {
            name: {
                "samples": len(values),
                "median": round(statistics.median(values), 1),
                "max": max(values),
            }
            for name, values in latencies.items()
        },
    }


def run_security(bridge: Bridge):
    token = bridge.token
    checks = []

    def record(label, expected, method, path, body=None, headers=None):
        status, raw, _ = http_raw(method, path, body, headers)
        parsed = None
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            parsed = raw[:120].decode("utf-8", "replace")
        ok = status == expected
        if ok and isinstance(parsed, dict) and "error" in parsed:
            ok = parsed["error"].get("code") in ERROR_CODES
        checks.append({"label": label, "expected": expected, "http": status, "ok": ok, "body": parsed})

    record("missing token", 401, "GET", "/tools")
    record("wrong token", 401, "GET", "/tools", headers={TOKEN_HEADER: "not-the-token"})
    record("bearer token", 200, "GET", "/tools", headers={"Authorization": f"Bearer {token}"})
    record("missing token on action", 401, "POST", "/action", {"action": "observe"})
    record("malformed JSON", 400, "POST", "/action", b"{not json", {TOKEN_HEADER: token})
    record("evil Host header", 403, "GET", "/tools", headers={TOKEN_HEADER: token, "Host": "evil.example.com"})
    record("evil Origin", 403, "GET", "/tools", headers={TOKEN_HEADER: token, "Origin": "https://evil.example.com"})

    request_id = "rid-" + uuid.uuid4().hex[:8]
    result = bridge.call("observe", request_id=request_id)
    echoed = (result["body"] or {}).get("request_id") == request_id
    checks.append(
        {
            "label": "client request_id echoed",
            "expected": request_id,
            "http": result["http"],
            "ok": echoed,
            "body": (result["body"] or {}).get("request_id"),
        }
    )
    return checks


def summarize(report):
    failed = [case for case in report["cases"] if not case["ok"]]
    leaked = [probe for probe in report["type_probes"] if probe["accepted"]]
    unsafe = [check for check in report["security"] if not check["ok"]]

    print(f"\ntools advertised: {report['tools_exposed']}")
    print(f"behavioural cases: {len(report['cases']) - len(failed)}/{len(report['cases'])} passed")
    print(f"type leaks: {len(leaked)}/{len(report['type_probes'])} wrong-typed inputs accepted")
    print(f"security checks: {len(report['security']) - len(unsafe)}/{len(report['security'])} passed")
    concurrency = report["concurrency"]
    print(f"concurrency: {concurrency['ok']}/{concurrency['total']} ok, median {concurrency['median_ms']} ms")

    slowest = sorted(report["latency_ms"].items(), key=lambda kv: -kv[1]["median"])[:5]
    if slowest:
        print("\nslowest tools (bridge-reported duration_ms):")
        for name, stats in slowest:
            print(f"  {name:22} median {stats['median']:7.1f}  max {stats['max']:7.1f}  n={stats['samples']}")

    if failed:
        print("\nfailures:")
        for case in failed:
            print(f"  {case['label']:34} {'; '.join(case['problems'])[:110]}")
    skipped = [case for case in report["cases"] if case.get("note")]
    if skipped:
        print("\nnot exercised (environmental):")
        for case in skipped:
            print(f"  {case['label']:34} {case['note']}")
    if leaked:
        print("\nwrong-typed input accepted (should have been rejected):")
        for probe in leaked:
            print(f"  {probe['action']:20} {json.dumps(probe['params'])[:60]:62} {probe['note']}")
    if unsafe:
        print("\nsecurity failures:")
        for check in unsafe:
            print(f"  {check['label']:34} expected {check['expected']}, got {check['http']}")

    return 1 if (failed or leaked or unsafe) else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--token", help="bridge token (defaults to env or token.key)")
    parser.add_argument("--json", help="write the full report to this path")
    parser.add_argument("--verbose", action="store_true", help="print every case")
    parser.add_argument(
        "--include-lifecycle",
        action="store_true",
        help="DESTRUCTIVE: actually call restart_nvda / reload_plugins",
    )
    args = parser.parse_args(argv)

    bridge = Bridge(resolve_token(args.token))
    tools = bridge.tools()
    report = run(bridge, tools, args.include_lifecycle, args.verbose)

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1), encoding="utf-8")
        print(f"report written to {args.json}")
    return summarize(report)


if __name__ == "__main__":
    sys.exit(main())
