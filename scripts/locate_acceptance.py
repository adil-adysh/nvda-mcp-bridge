#!/usr/bin/env python3
"""End-to-end acceptance for `locate` (implementation plan P5).

What this proves, against a live NVDA and a real browser:

1. a control outside the observed neighbourhood can be found by role and name
   over a page holding hundreds of elements, in well under a second;
2. the found reference is actionable — `invoke` activates it, `set_value` writes
   its value, `focus` moves to it;
3. it works for an element that is scrolled out of view, which is the case a
   screenshot agent cannot click at all;
4. a name-only search reports honestly that it is truncated, and a role with no
   UIA equivalent falls back to the bounded walk and says so;
5. a control inside a native application's dialog is found, and its ref closes
   the dialog with a verified postcondition;
6. a reference to an element whose window has gone is reported stale (4206),
   not acted on.

The page is generated per run with a unique title, because an earlier run's tab
with the same title is otherwise indistinguishable from this run's.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

# Window titles carry zero-width spaces, which the default console code page
# cannot encode; a failed print must not end an acceptance run.
for _stream in (sys.stdout, sys.stderr):
	try:
		_stream.reconfigure(encoding="utf-8", errors="replace")
	except (AttributeError, ValueError):
		pass

from action_surface_probe import close_window  # noqa: E402
from live_acceptance import (  # noqa: E402
    Bridge,
    Report,
    activate,
    data,
    error_code,
    force_foreground,
    ok,
    result,
    resolve_token,
    wait_for,
    window_title,
    _user32,
    _windows_matching,
)

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
EDGE_ALT = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{{TITLE}}</title>
<style>body{font:15px/1.6 system-ui,sans-serif;margin:1.5rem}
input{width:16rem;padding:.3rem}#links div{padding:0}
#status{margin-top:1rem;color:#333}</style></head><body>
<h1>{{TITLE}}</h1>
<p><label for="q">Search field</label><br><input id="q" type="text" value="start"></p>
<p><button id="beta" onclick="hit('beta')">Beta action</button></p>
<div id="links">{{LINKS}}</div>
<p><a id="two" href="#two" onclick="hit('two'); return false">Two</a></p>
<div id="status" role="status" aria-live="polite">idle</div>
<script>
function hit(which){
  document.getElementById('status').textContent = 'clicked ' + which;
  document.title = {{BASE_TITLE}} + ' clicked ' + which;
}
</script></body></html>
"""


def write_page(directory: Path, title: str) -> Path:
	links = "\n".join(
		f'<div><a href="#item{index}">Item {index}</a></div>' for index in range(200)
	)
	page = directory / "locate_acceptance.html"
	page.write_text(
		PAGE.replace("{{TITLE}}", title)
		.replace("{{LINKS}}", links)
		.replace("{{BASE_TITLE}}", json.dumps(title)),
		encoding="utf-8",
	)
	return page


def launch_browser(page: Path) -> bool:
	executable = next((path for path in (EDGE, EDGE_ALT) if Path(path).is_file()), None)
	if executable is None:
		return False
	subprocess.Popen([executable, "--new-window", page.as_uri()])  # noqa: S603
	return True


def close_titled_window(fragment: str) -> None:
	"""Close top-level windows whose title contains ``fragment`` (best effort)."""
	WM_CLOSE = 0x0010

	@ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
	def callback(hwnd, _param):
		length = _user32.GetWindowTextLengthW(hwnd)
		if length:
			buffer = ctypes.create_unicode_buffer(length + 1)
			_user32.GetWindowTextW(hwnd, buffer, length + 1)
			if fragment.lower() in buffer.value.lower():
				_user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
		return True

	_user32.EnumWindows(callback, 0)


def observed_window_title(bridge: Bridge) -> str:
	window = data(bridge.call("observe")).get("window") or {}
	return str(window.get("title", ""))


def check_browser(bridge: Bridge, report: Report, title: str) -> dict:
	report.section("locate in a browser (P5)")
	appeared = wait_for(lambda: bool(_windows_matching(title)), timeout_s=25, interval_s=0.3)
	report.check("the acceptance page opens", appeared, title)
	if not appeared:
		return {}
	time.sleep(1.0)
	# `locate` searches the foreground window when no scope is given, so the page
	# must be the foreground window — that is the product contract, not a harness
	# detail.
	report.check("the acceptance page is foreground", activate(bridge, title), window_title(bridge))
	if title.lower() not in observed_window_title(bridge).lower():
		report.skip("browser locate checks", "the page did not take the foreground")
		return {}

	found = bridge.call("locate", {"role": "BUTTON", "name": "Beta action"})
	block = data(found)
	matches = block.get("matches") or []
	report.check("locate finds a button by name", ok(found), str(error_code(found)))
	report.check("the search used the platform", block.get("method") == "uia", str(block.get("method")))
	report.check("exactly one button matched", len(matches) == 1, str(len(matches)))
	report.check(
		"the search over 200+ links stays fast",
		float(block.get("elapsed_ms") or 9999) < 300,
		f"{block.get('elapsed_ms')} ms",
	)
	match = matches[0] if matches else {}
	report.check("the match carries a ref", bool(match.get("ref")), str(match.get("ref")))
	report.check("the match carries a target kind", match.get("target") == "uia", str(match.get("target")))
	report.check("the match carries its role and name", match.get("role") == "BUTTON", json.dumps(match)[:160])
	report.check("the match carries a screen rectangle", len(match.get("location") or []) == 4, str(match.get("location")))

	invoked = bridge.call("invoke", {"ref": match.get("ref")})
	report.check("a located button can be invoked", ok(invoked), str(error_code(invoked)))
	clicked = wait_for(lambda: "clicked beta" in observed_window_title(bridge), timeout_s=4, interval_s=0.2)
	report.check("invoking the located button reaches the page", bool(clicked), observed_window_title(bridge))

	# The link is at the bottom of 200 rows, so it is scrolled out of view. A
	# coordinate click could not reach it; a UIA Invoke can.
	two = data(bridge.call("locate", {"role": "LINK", "name": "Two", "exact": True}))
	two_matches = two.get("matches") or []
	report.check("an off-screen link is found by exact name", len(two_matches) == 1, str(len(two_matches)))
	if two_matches:
		report.note("the off-screen link's rectangle", json.dumps(two_matches[0].get("location")))
		report.check(
			"the link is genuinely out of view",
			bool(two_matches[0].get("states") and "OFFSCREEN" in two_matches[0]["states"]),
			json.dumps(two_matches[0].get("states")),
		)
		invoked_two = bridge.call("invoke", {"ref": two_matches[0].get("ref")})
		report.check("an off-screen link can still be invoked", ok(invoked_two), str(error_code(invoked_two)))
		report.check(
			"the off-screen invoke reaches the page",
			bool(wait_for(lambda: "clicked two" in observed_window_title(bridge), timeout_s=4, interval_s=0.2)),
			observed_window_title(bridge),
		)

	# A located edit can be written and read back without a keystroke.
	field = data(bridge.call("locate", {"role": "EDITABLETEXT", "name": "Search field"}))
	field_matches = field.get("matches") or []
	report.check("the search field is located", len(field_matches) == 1, str(len(field_matches)))
	if field_matches:
		ref = field_matches[0].get("ref")
		wrote = bridge.call("set_value", {"ref": ref, "value": "located text"})
		report.check("set_value writes a located field", ok(wrote), json.dumps(data(wrote))[:160])
		report.check("the write used the platform", data(wrote).get("mode") == "platform", str(data(wrote).get("mode")))
		read_back = bridge.call("read", {"ref": ref, "scope": "document"})
		report.check(
			"the written value reads back",
			"located text" in str(data(read_back).get("text", "")),
			str(data(read_back).get("text"))[:80],
		)

	# A name-only search reports truncation, because "the" is everywhere.
	partial = data(bridge.call("locate", {"name": "Item 1", "limit": 5}))
	report.check("a broad search returns the requested number", len(partial.get("matches") or []) == 5, str(len(partial.get("matches") or [])))
	report.check("a broad search reports truncation", partial.get("truncated") is True, str(partial.get("truncated")))

	# A role with no UIA control type cannot use the provider; the reply must say
	# it walked instead of pretending it found nothing quickly.
	walked = bridge.call("locate", {"role": "TABLECELL", "name": "nothing", "timeout_ms": 300})
	report.check("an unmapped role succeeds honestly", ok(walked), str(error_code(walked)))
	report.check("an unmapped role reports the walk", data(walked).get("method") == "walk", str(data(walked).get("method")))
	report.check("the walk is bounded by its budget", float(data(walked).get("elapsed_ms") or 9999) < 2000, f"{data(walked).get('elapsed_ms')} ms")

	missing = bridge.call("locate", {"role": "BUTTON", "name": "No such control here"})
	report.check("no match is an empty list, not an error", ok(missing) and (data(missing).get("matches") or []) == [], json.dumps(data(missing))[:160])

	# within: the scope is reported, and a window-wide fallback is declared.
	observed = data(bridge.call("observe", {"include": ["focus"]}))
	focus_ref = (observed.get("focus") or {}).get("ref", "f")
	scoped = bridge.call("locate", {"role": "BUTTON", "name": "Beta", "within": focus_ref})
	report.check("a search within a ref succeeds", ok(scoped), str(error_code(scoped)))
	report.check(
		"the scope of the search is reported",
		bool(data(scoped).get("scope")),
		str(data(scoped).get("scope"))[:120],
	)
	report.note("within-search backend", str(data(scoped).get("method")))
	return two_matches[0] if two_matches else {}


def check_notepad_dialog(bridge: Bridge, report: Report) -> None:
	report.section("locate in a native dialog (P5)")
	subprocess.Popen(["notepad.exe"])  # noqa: S603
	appeared = wait_for(lambda: bool(_windows_matching("Notepad")), timeout_s=15, interval_s=0.3)
	report.check("Notepad opens", appeared)
	if not appeared:
		return
	time.sleep(1.8)

	# Raise the window above the browser before clicking it: a click at its centre
	# otherwise lands on whatever is on top, which is how an "activate" can
	# silently type into the wrong application.
	force_foreground("Notepad")
	focused = activate(bridge, "Notepad")
	if not focused:
		report.skip("Notepad can be focused", window_title(bridge) or "no window")
		close_titled_window("Notepad")
		return

	# Close any ribbon menu a previous click opened, so the shortcut reaches the
	# application rather than an open menu.
	for _ in range(3):
		bridge.call("press", {"keys": "escape"})
		time.sleep(0.2)

	# Ctrl+Shift+S opens the Save As dialog: a separate top-level window with a
	# real Cancel button, and opening it changes nothing on disk.
	opened = bridge.call("press", {"keys": "control+shift+s"})
	report.check("the Save As dialog opens", ok(opened), str(error_code(opened)))

	# A dialog appears when the application has drawn it, which is not when the
	# keystroke returned: poll instead of sleeping for a guess, or the next check
	# searches a window that does not exist yet.
	def dialog_cancel():
		response = bridge.call("locate", {"role": "BUTTON", "name": "Cancel"})
		return response if (data(response).get("matches") or []) else None

	found = wait_for(dialog_cancel, timeout_s=10, interval_s=0.4) or bridge.call(
		"locate", {"role": "BUTTON", "name": "Cancel"}
	)
	block = data(found)
	matches = block.get("matches") or []
	report.check("the dialog's Cancel button is located", ok(found) and len(matches) == 1, json.dumps(matches)[:200])
	report.check("the search used the platform", block.get("method") == "uia", str(block.get("method")))
	report.note("the dialog's search scope", str(block.get("scope")))
	report.note("the dialog's Cancel automation id", str((matches[0].get("automationId") if matches else "")))

	if matches:
		closed = bridge.call("invoke", {"ref": matches[0]["ref"]})
		report.check("invoking Cancel succeeds", ok(closed), str(error_code(closed)))
		report.check(
			"closing the dialog is a verified change",
			result(closed).get("changed") is True,
			json.dumps(result(closed).get("delta"))[:220],
		)
		report.check(
			"the dialog's window is gone afterwards",
			bool(
				wait_for(
					lambda: "Notepad" in str((data(bridge.call("observe")).get("window") or {}).get("title")),
					timeout_s=4,
					interval_s=0.2,
				)
			),
			str((data(bridge.call("observe")).get("window") or {}).get("title")),
		)
		after = data(bridge.call("locate", {"role": "BUTTON", "name": "Cancel"}))
		report.check("the Cancel button is no longer found", (after.get("matches") or []) == [], json.dumps(after.get("matches"))[:160])

	close_titled_window("Notepad")
	time.sleep(1.0)
	_dismiss_save_prompt(bridge)
	close_titled_window("Notepad")
	time.sleep(0.5)


def _dismiss_save_prompt(bridge: Bridge) -> None:
	"""Notepad may ask to save; decline so the next run starts clean."""
	dialog = data(bridge.call("observe")).get("dialog") or {}
	if dialog.get("name"):
		answer = bridge.call("locate", {"role": "BUTTON", "name": "Don't save"})
		matches = data(answer).get("matches") or []
		if matches:
			bridge.call("invoke", {"ref": matches[0]["ref"]})
			return
		bridge.call("dismiss")


def check_stale(bridge: Bridge, report: Report, page_title: str, ref: str) -> None:
	report.section("a dead located reference is stale (P5)")
	if not ref:
		report.skip("stale located reference", "no location survived the browser checks")
		return
	close_titled_window(page_title)
	report.check(
		"the page window is closed",
		bool(wait_for(lambda: not _windows_matching(page_title), timeout_s=8, interval_s=0.2)),
	)
	invoked = bridge.call("invoke", {"ref": ref})
	report.check("invoking a dead element fails", not ok(invoked))
	report.check("the failure is the stale-reference code", error_code(invoked) == 4206, str(error_code(invoked)))


def main(argv=None) -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--token", default=None)
	parser.add_argument("--verbose", action="store_true")
	parser.add_argument("--keep", action="store_true", help="leave the browser window open")
	args = parser.parse_args(argv)

	bridge = Bridge(resolve_token(args.token))
	report = Report(verbose=args.verbose)
	title = f"Locate Acceptance {uuid.uuid4().hex[:6]}"
	scratch = REPO / ".probe"
	scratch.mkdir(exist_ok=True)
	page = write_page(scratch, title)
	ref = ""
	try:
		# A leftover Notepad or probe window would be the foreground window, and
		# therefore the search scope, for the whole browser section.
		close_titled_window("Notepad")
		close_titled_window("NVDA Locate Probe")
		close_titled_window("NVDA Action Probe")
		time.sleep(0.6)
		if not launch_browser(page):
			print("no Chromium browser found", file=sys.stderr)
			return 2
		two = check_browser(bridge, report, title)
		ref = str(two.get("ref", ""))
		check_notepad_dialog(bridge, report)
		check_stale(bridge, report, title, ref)
	finally:
		if not args.keep:
			close_titled_window(title)
			try:
				page.unlink()
				scratch.rmdir()
			except OSError:
				pass

	failures = report.failed
	total = len([check for check in report.checks if not check.get("note")])
	print(f"\n{total - len(failures)}/{total} checks passed")
	for failure in failures:
		print(f"FAILED {failure['name']}: {failure['detail']}")
	return 0 if not failures else 1


if __name__ == "__main__":
	try:
		sys.exit(main())
	except Exception as error:  # noqa: BLE001
		print(f"acceptance harness error: {type(error).__name__}: {error}", file=sys.stderr)
		sys.exit(1)
