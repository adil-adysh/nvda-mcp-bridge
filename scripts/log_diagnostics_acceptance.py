#!/usr/bin/env python3
"""Live acceptance for the native-log diagnostic tools against running NVDA.

Read-only apart from one ``observe`` action, which captures the screen without
changing it. Verifies the HTTP envelope, pagination, context expansion, summary
determinism, trace-id correlation, authentication, and that an expensive log
search does not block an unrelated action.

    python scripts/log_diagnostics_acceptance.py
    python scripts/log_diagnostics_acceptance.py --json report.json
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge_auth import TOKEN_HEADER, resolve_token  # noqa: E402

BASE_URL = "http://127.0.0.1:8765"


def call(token: str, action: str, params: dict | None = None, timeout: float = 30.0) -> tuple[int, dict]:
	payload = json.dumps({"action": action, "params": params or {}}).encode("utf-8")
	request = urllib.request.Request(
		f"{BASE_URL}/action",
		data=payload,
		headers={TOKEN_HEADER: token, "Content-Type": "application/json"},
		method="POST",
	)
	try:
		with urllib.request.urlopen(request, timeout=timeout) as response:
			return response.status, json.loads(response.read().decode("utf-8"))
	except urllib.error.HTTPError as error:
		raw = error.read().decode("utf-8", "replace")
		try:
			return error.code, json.loads(raw)
		except json.JSONDecodeError:
			return error.code, {"_raw": raw}


def tools(token: str) -> dict:
	request = urllib.request.Request(f"{BASE_URL}/tools", headers={TOKEN_HEADER: token})
	with urllib.request.urlopen(request, timeout=10) as response:
		return json.loads(response.read().decode("utf-8"))


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--json", type=Path, default=None)
	args = parser.parse_args()
	token = resolve_token()
	report: dict = {"checks": [], "measurements": {}, "tools": {}}

	def check(label: str, ok: bool, detail=None) -> None:
		report["checks"].append({"label": label, "ok": bool(ok), "detail": detail})

	discovery = tools(token)
	report["tools"]["count"] = discovery["count"]
	report["tools"]["api_level"] = discovery["api_level"]
	names = {tool["name"] for tool in discovery["tools"]}
	for name in ("search_logs", "get_log_context", "summarize_logs", "get_logs", "get_action_trace"):
		check(f"tool advertised: {name}", name in names)

	started = time.perf_counter()
	status, search = call(token, "search_logs", {"levels": ["INFO"], "message": {"contains": "Bridge listening"}, "limit": 5})
	report["measurements"]["search_ms"] = round((time.perf_counter() - started) * 1000, 1)
	check("search_logs returns HTTP 200 success", status == 200 and search.get("success") is True, status)
	entries = (search.get("data") or {}).get("entries") or []
	check("search found the bridge startup record", bool(entries), entries[:1])
	if entries:
		entry_id = entries[0]["entry_id"]
		started = time.perf_counter()
		status, context = call(token, "get_log_context", {"entry_id": entry_id, "before": 3, "after": 5})
		report["measurements"]["context_ms"] = round((time.perf_counter() - started) * 1000, 1)
		check("get_log_context returns the anchor", status == 200 and any(
			item["entry_id"] == entry_id for item in (context.get("data") or {}).get("entries", [])
		), status)

	status, warnings = call(token, "search_logs", {"levels": ["DEBUGWARNING"], "limit": 1, "order": "oldest_first"})
	warning_page = (warnings.get("data") or {}).get("page") or {}
	warning_entries = (warnings.get("data") or {}).get("entries") or []
	check("DEBUGWARNING is searchable", status == 200 and all(item["level"] == "DEBUGWARNING" for item in warning_entries))
	check(
		"pagination exposes a cursor exactly when has_more is set",
		bool(warning_page.get("next_cursor")) == bool(warning_page.get("has_more")),
		warning_page,
	)

	started = time.perf_counter()
	status, summary = call(token, "summarize_logs", {"include": ["counts", "warnings", "exceptions", "noise"], "limit": 5})
	report["measurements"]["summary_ms"] = round((time.perf_counter() - started) * 1000, 1)
	summary_data = summary.get("data") or {}
	check("summarize_logs returns counts", status == 200 and (summary_data.get("summary") or {}).get("total_records", 0) > 0)
	check("summary reports parse failures explicitly", "parse_failures" in (summary_data.get("coverage") or {}))

	status, legacy = call(token, "get_logs", {"max_lines": 3, "use_tail": True})
	legacy_data = legacy.get("data") or {}
	check(
		"legacy get_logs keeps its shape",
		status == 200 and {"log_file", "returned_lines", "max_lines", "lines", "file_size"} <= set(legacy_data),
	)
	check("legacy get_logs returns bounded lines", len(legacy_data.get("lines") or []) <= 3)

	status, observed = call(token, "observe", {"include": ["focus"]})
	trace_id = observed.get("trace_id")
	status_trace, trace = call(token, "get_action_trace", {"trace_id": trace_id})
	check("action carries a trace id", bool(trace_id))
	check(
		"get_action_trace resolves it",
		status_trace == 200 and (trace.get("data") or {}).get("found") is True,
		trace_id,
	)
	status_trace_search, related = call(token, "search_logs", {"trace_id": trace_id, "limit": 5})
	related_data = related.get("data") or {}
	check(
		"search_logs resolves native records carrying the trace id",
		status_trace_search == 200 and bool(related_data.get("entries")),
		(trace_id, related_data.get("diagnostics")),
	)

	# An expensive search must not block an unrelated action. The summary scans
	# the real log on the bridge worker, while observe runs on NVDA's main thread.
	summary_result: dict = {}

	def run_summary() -> None:
		summary_result["started"] = time.perf_counter()
		summary_result["result"] = call(token, "summarize_logs", {"limit": 10}, timeout=60)
		summary_result["elapsed_ms"] = round((time.perf_counter() - summary_result["started"]) * 1000, 1)

	thread = threading.Thread(target=run_summary)
	thread.start()
	time.sleep(0.05)
	started = time.perf_counter()
	status, observe_during = call(token, "observe", {"include": ["focus"]}, timeout=15)
	action_ms = round((time.perf_counter() - started) * 1000, 1)
	thread.join()
	report["measurements"]["observe_during_summary_ms"] = action_ms
	report["measurements"]["concurrent_summary_ms"] = summary_result.get("elapsed_ms")
	check("observe succeeds while summary scans", status == 200 and observe_during.get("success") is True, action_ms)
	check("observe remains responsive while summary scans", action_ms < 3000, action_ms)

	# Authentication and validation remain the transport's job.
	bad = urllib.request.Request(f"{BASE_URL}/tools", headers={TOKEN_HEADER: "wrong"})
	try:
		with urllib.request.urlopen(bad, timeout=5):
			bad_status = 200
	except urllib.error.HTTPError as error:
		bad_status = error.code
	check("invalid token is rejected", bad_status == 401, bad_status)
	status, invalid = call(token, "search_logs", {"message": {"regex": "("}})
	check(
		"invalid regex is a structured caller error",
		status == 400 and (invalid.get("error") or {}).get("code") == 4103,
		invalid.get("error"),
	)

	# Concurrent independent searches: each cursor must stay independent.
	with ThreadPoolExecutor(max_workers=2) as pool:
		futures = [
			pool.submit(call, token, "search_logs", {"levels": ["DEBUGWARNING"], "limit": 1, "order": "oldest_first"})
			for _ in range(2)
		]
		pages = [future.result() for future in futures]
	first_ids = [(page[1].get("data") or {}).get("entries", [{}])[0].get("entry_id") for page in pages]
	check("concurrent searches are independent", first_ids[0] == first_ids[1] and first_ids[0] is not None, first_ids)

	failed = [item for item in report["checks"] if not item["ok"]]
	print(f"tools advertised: {discovery['count']} (api_level {discovery['api_level']})")
	print(f"checks: {len(report['checks']) - len(failed)}/{len(report['checks'])} passed")
	for label, value in report["measurements"].items():
		print(f"  {label}: {value}")
	for item in failed:
		print(f"FAILED: {item['label']} :: {item['detail']}")
	if args.json:
		args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
	return 1 if failed else 0


if __name__ == "__main__":
	raise SystemExit(main())
