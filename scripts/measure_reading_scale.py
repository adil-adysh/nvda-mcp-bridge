#!/usr/bin/env python3
"""Measure what reading tools cost on a document that is actually large.

The review that prompted this says `navigate_reading(direction="last")` scans up
to `MAX_SCAN` (2000) positions inside one main-thread dispatch, and that a page
full of links can therefore freeze NVDA. That is a code-level reading of the
implementation, not a measurement, and the difference matters:

* `list_reading_targets` stops after `limit + 1` advances, so its cost is
  bounded by the request, not by the document;
* `navigate_reading(direction="last")` scans to the end of the document (up to
  `MAX_SCAN`), and nothing bounds its elapsed time.

This script generates pages with a known number of links and headings, opens
them in a real browser, and times each call from the client. Because the whole
operation runs in one `context.dispatch` hop on NVDA's main thread, the
client-measured wall time is the time NVDA was unable to do anything else — so
the number is a stall measurement, not just a latency measurement.

Usage:
    python scripts/measure_reading_scale.py --links 2000 --headings 400
    python scripts/measure_reading_scale.py --links 2000 --browser firefox
    python scripts/measure_reading_scale.py --quick      # 200 links, sanity only
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

for _stream in (sys.stdout, sys.stderr):
	try:
		_stream.reconfigure(encoding="utf-8", errors="replace")
	except (AttributeError, ValueError):
		pass

from live_acceptance import (  # noqa: E402
	Bridge,
	data,
	error_code,
	ok,
	resolve_token,
	wait_for,
	window_title,
	_windows_matching,
)
from locate_acceptance import close_titled_window  # noqa: E402

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
EDGE_ALT = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
FIREFOX = r"C:\Program Files\Mozilla Firefox\firefox.exe"

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{{TITLE}}</title>
<style>body{font:14px/1.5 system-ui,sans-serif;margin:1rem}li{margin:0}</style>
</head><body>
<h1>{{TITLE}}</h1>
{{HEADINGS}}
<ul>
{{LINKS}}
</ul>
</body></html>
"""


def build_page(links: int, headings: int, title: str, token: str) -> Path:
	path = REPO / ".probe" / f"reading-scale-{token}.html"
	path.parent.mkdir(parents=True, exist_ok=True)
	heading_html = "\n".join(f"<h2>Section {index:04d}</h2>" for index in range(1, headings + 1))
	link_html = "\n".join(
		f'<li><a href="#l{index:04d}">Link {index:04d}</a></li>' for index in range(1, links + 1)
	)
	path.write_text(
		PAGE.replace("{{TITLE}}", title)
		.replace("{{HEADINGS}}", heading_html)
		.replace("{{LINKS}}", link_html),
		encoding="utf-8",
	)
	return path


def open_page(browser: str, path: Path) -> None:
	executable = {
		"edge": EDGE if Path(EDGE).exists() else EDGE_ALT,
		"firefox": FIREFOX,
	}[browser]
	subprocess.Popen([executable, str(path)])


def focus_page(bridge: Bridge, token: str, title: str, attempts: int = 12) -> bool:
	"""Make the page the document NVDA reads, the way a user would.

	A raised window does not keep the foreground on its own, and a browser keeps
	keyboard focus in its chrome — address bar, toolbars — until something puts
	it in the page. ``activate_window`` is retried until the window in front is
	the page, and only then is F6 pressed, because F6 sent to the wrong window
	cycles the wrong toolbar.
	"""
	for attempt in range(attempts):
		if token.lower() not in window_title(bridge).lower():
			bridge.call("activate_window", {"title": token})
			time.sleep(1.5)
			if attempt and attempt % 3 == 2:
				# Slow launch: the OS can hand the window the foreground before
				# NVDA's event-driven model notices it. A real switch — Alt+Tab
				# twice — is what makes NVDA process a foreground event.
				bridge.call("press", {"keys": "alt+tab"})
				time.sleep(1.0)
				bridge.call("press", {"keys": "alt+tab"})
				time.sleep(1.5)
			continue
		if attempt:
			bridge.call("press", {"keys": "f6"})
			time.sleep(1.2)
		response = bridge.call("list_reading_targets", {"item": "heading", "limit": 1})
		if ok(response) and str(data(response).get("document", "")).lower().startswith(title.lower()):
			return True
	return False


def timed(bridge: Bridge, action: str, params: dict) -> dict:
	"""Run one bridge call and report client wall time beside server elapsed."""
	started = time.monotonic()
	response = bridge.call(action, params)
	elapsed = round((time.monotonic() - started) * 1000.0, 1)
	block = data(response) or {}
	return {
		"action": action,
		"params": params,
		"ok": ok(response),
		"error": (response.get("error") or {}).get("message") if not ok(response) else None,
		"code": error_code(response) if not ok(response) else None,
		"wall_ms": elapsed,
		"server_ms": block.get("elapsed_ms"),
		"count": len(block.get("targets") or []) if block.get("targets") is not None else None,
		"truncated": block.get("truncated"),
		"stopped": block.get("stopped"),
		"reached": block.get("reached"),
	}


def main() -> int:
	parser = argparse.ArgumentParser()
	parser.add_argument("--links", type=int, default=2000)
	parser.add_argument("--headings", type=int, default=400)
	parser.add_argument("--browser", choices=("edge", "firefox"), default="edge")
	parser.add_argument("--quick", action="store_true", help="200 links, 40 headings")
	parser.add_argument("--json", action="store_true", help="print one JSON object")
	parser.add_argument("--keep", action="store_true", help="leave the page open")
	args = parser.parse_args()
	if args.quick:
		args.links, args.headings = 200, 40

	bridge = Bridge(resolve_token(None))
	health = bridge.get("/health")
	if not health or health.get("status") != "ok":
		print("bridge is not answering; start NVDA first")
		return 2

	# A window is titled with the page title, so the fragment has to be unique
	# per run: an earlier tab with the same title is otherwise indistinguishable
	# from this run's.
	token = uuid.uuid4().hex[:6]
	title = f"Reading scale {args.links} links {token}"
	path = build_page(args.links, args.headings, title, token)
	print(f"page: {path}  ({args.links} links, {args.headings} headings)")

	try:
		open_page(args.browser, path)
		appeared = wait_for(lambda: bool(_windows_matching(token)), timeout_s=40, interval_s=0.5)
		if not appeared:
			print("the page never appeared as a window")
			return 2
		time.sleep(1.5)
		if not focus_page(bridge, token, title):
			print("the page did not become the readable document in front")
			return 2

		# Baseline: what one bounded enumeration costs, which is what
		# `list_reading_targets` spends regardless of document size.
		results = [
			timed(bridge, "list_reading_targets", {"item": "link", "limit": 1}),
			timed(bridge, "list_reading_targets", {"item": "link", "limit": 50}),
			timed(bridge, "list_reading_targets", {"item": "heading", "limit": 50}),
			# The unbounded one. This is the claim under test.
			timed(bridge, "navigate_reading", {"item": "link", "direction": "last"}),
			timed(bridge, "navigate_reading", {"item": "heading", "direction": "last"}),
			timed(bridge, "navigate_reading", {"item": "link", "direction": "first"}),
			# Cost of `steps`: each step re-searches from the cursor, so this
			# scales with the request rather than with the document.
			timed(bridge, "navigate_reading", {"item": "link", "direction": "next", "steps": 1}),
			timed(bridge, "navigate_reading", {"item": "link", "direction": "next", "steps": 5}),
			timed(bridge, "navigate_reading", {"item": "link", "direction": "next", "steps": 10}),
			timed(bridge, "navigate_reading", {"item": "link", "direction": "next", "steps": 20}),
			# The same calls with a budget too small to finish: the answer has to
			# say where it stopped, and a list cut this way is never "complete".
			timed(bridge, "list_reading_targets", {"item": "link", "limit": 50, "timeout_ms": 100}),
			timed(
				bridge,
				"navigate_reading",
				{"item": "link", "direction": "next", "steps": 20, "timeout_ms": 250},
			),
		]
	finally:
		if not args.keep:
			close_titled_window(token)
			path.unlink(missing_ok=True)

	payload = {
		"links": args.links,
		"headings": args.headings,
		"browser": args.browser,
		"results": results,
	}
	for row in results:
		label = f"{row['params'].get('item')} {row['params'].get('direction') or 'list'}"
		if row["params"].get("limit"):
			label += f" limit={row['params']['limit']}"
		if row["params"].get("steps"):
			label += f" steps={row['params']['steps']}"
		if row["ok"]:
			print(
				f"{label:<34} wall={row['wall_ms']:>8.1f} ms  server={row['server_ms']!s:>7} ms"
				f"  n={row['count']} truncated={row['truncated']} "
				f"stopped={row['stopped']} reached={row['reached']}"
			)
		else:
			print(f"{label:<34} wall={row['wall_ms']:>8.1f} ms  error {row['code']}: {row['error']}")
	if args.json:
		print(json.dumps(payload))
	return 0


if __name__ == "__main__":
	sys.exit(main())
