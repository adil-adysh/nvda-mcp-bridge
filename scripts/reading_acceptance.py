#!/usr/bin/env python3
"""End-to-end acceptance for document-order reading (implementation plan P5.2).

What this proves, against a live NVDA and real browsers:

1. one call enumerates a page's headings in reading order, with levels, where
   `locate` would need a search per level and would return tree order;
2. the list is bounded and says when it was cut;
3. a document that cannot answer a type reports what it *can* answer — the
   Word-only `reference` type is refused by both browsers with the supported
   list attached — and that list is probed from the implementation rather than
   read out of a table someone typed by hand;
4. navigation lands where the screen reader would, with `reached` telling a
   partial move from a complete one;
5. a position backed by a control hands back a ref that reads back the control;
6. an application without a document says "not in browse mode" instead of
   failing obscurely.

The page is generated per run with a unique title, because an earlier run's tab
with the same title is otherwise indistinguishable from this run's.
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

# Window titles carry characters the default console code page cannot encode; a
# failed print must not end an acceptance run.
for _stream in (sys.stdout, sys.stderr):
	try:
		_stream.reconfigure(encoding="utf-8", errors="replace")
	except (AttributeError, ValueError):
		pass

from locate_acceptance import close_titled_window  # noqa: E402
from live_acceptance import (  # noqa: E402
	Bridge,
	Report,
	activate,
	data,
	error_code,
	result,
	ok,
	resolve_token,
	wait_for,
	window_title,
	_windows_matching,
)

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
EDGE_ALT = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
FIREFOX = r"C:\Program Files\Mozilla Firefox\firefox.exe"
WORD = r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE"

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{{TITLE}}</title>
<style>body{font:15px/1.6 system-ui,sans-serif;margin:2rem;max-width:44rem}
nav a{margin-right:1rem}</style></head><body>
<h1>{{TITLE}}</h1>
<h2 id="intro">Introduction</h2>
<p>This paragraph exists so the document has text between its headings.</p>
<h3 id="background">Background</h3>
<p>Another paragraph, so the browser has to build a real document.</p>
<h2 id="methods">Methods</h2>
<nav aria-label="On this page">
<a href="#intro">Intro</a><a href="#methods">Methods</a>
</nav>
<h2 id="results">Results</h2>
<p><a id="report" href="#report">Download report</a></p>
<p><label for="q">Search field</label><br><input id="q" type="text"></p>
<button type="button">Run</button>
</body></html>
"""

#: What the page holds, in document order. The h1 carries the unique title.
HEADINGS = ["{{TITLE}}", "Introduction", "Background", "Methods", "Results"]
LEVELS = [1, 2, 3, 2, 2]
LINKS = ["Intro", "Methods", "Download report"]


def write_page(directory: Path, title: str) -> Path:
	page = directory / "reading_acceptance.html"
	page.write_text(PAGE.replace("{{TITLE}}", title), encoding="utf-8")
	return page


def launch(executable: str, page: Path, new_window_flag: str = "--new-window") -> bool:
	if not Path(executable).is_file():
		return False
	subprocess.Popen([executable, new_window_flag, page.as_uri()])  # noqa: S603
	return True


def error_details(response) -> dict:
	return (response.get("error") or {}).get("details") or {}


def read_document(bridge: Bridge, title: str, attempts: int = 5):
	"""Read the headings of the window in front, putting focus in its page.

	A raised window does not necessarily keep the foreground, and NVDA builds a
	document model only once something in it is focused. The tool refuses when
	the focus and the window in front disagree, rather than reading a document
	behind it, and says so; the harness then does what a user does — activate
	again and press F6.
	"""
	response = None
	for attempt in range(attempts):
		if title.lower() not in window_title(bridge).lower():
			bridge.call("activate_window", {"title": title})
			time.sleep(1.2)
		if attempt:
			# A browser keeps focus in its chrome — address bar, toolbars — until
			# something puts it in the page. F6 cycles; two presses are usually
			# what it takes from the address bar.
			bridge.call("press", {"keys": "f6"})
			time.sleep(1.2)
		response = bridge.call("list_reading_targets", {"item": "heading", "limit": 20})
		if ok(response) and str(data(response).get("document", "")).lower().startswith(title.lower()):
			return response
		if not ok(response) and error_code(response) != 4205:
			return response
	if response is not None and not ok(response):
		print(f"  {title}: reading was refused — {error_details(response).get('reason') or (response.get('error') or {}).get('message')}")
	return response


def labels(block: dict) -> list:
	return [target.get("label") for target in block.get("targets") or []]


def check_document(bridge: Bridge, report: Report, title: str, name: str, landmarks: bool, implementations: tuple) -> None:
	"""The checks every browse mode document should pass."""
	report.section(f"reading a document in {name}")
	appeared = wait_for(lambda: bool(_windows_matching(title)), timeout_s=30, interval_s=0.3)
	report.check(f"{name}: the acceptance page opens", appeared, title)
	if not appeared:
		return
	time.sleep(1.0)
	outline = read_document(bridge, title)
	block = data(outline)
	report.check(
		f"{name}: the document in front is the requested one",
		str(block.get("document", "")).lower().startswith(title.lower()),
		str(block.get("document")),
	)
	expected = [title if item == "{{TITLE}}" else item for item in HEADINGS]
	report.check(f"{name}: one call lists every heading", ok(outline) and len(block.get("targets") or []) == 5, json.dumps(labels(block))[:220])
	report.check(f"{name}: headings come in reading order", labels(block) == expected, json.dumps(labels(block))[:220])
	report.check(
		f"{name}: heading levels are reported",
		[target.get("level") for target in block.get("targets") or []] == LEVELS,
		json.dumps([target.get("level") for target in block.get("targets") or []]),
	)
	report.check(f"{name}: the document is named", str(block.get("document", "")).startswith(title), str(block.get("document")))
	report.check(f"{name}: the answering interface is reported", bool(block.get("interface")), str(block.get("interface")))
	report.check(
		f"{name}: the expected browse mode implementation answered",
		any(name in str(block.get("interface")) for name in implementations),
		str(block.get("interface")),
	)
	report.note(f"{name}: browse mode interface", f"{block.get('interface')} (types {block.get('source')}, {block.get('elapsed_ms')} ms)")

	cut = bridge.call("list_reading_targets", {"item": "heading", "limit": 2})
	report.check(
		f"{name}: a bounded list is cut and says so",
		len(data(cut).get("targets") or []) == 2 and data(cut).get("truncated") is True,
		json.dumps({"n": len(data(cut).get("targets") or []), "truncated": data(cut).get("truncated")}),
	)

	links = bridge.call("list_reading_targets", {"item": "link", "limit": 20})
	# `text` is the position's own text; `label` is what NVDA would announce,
	# which adds state for links ("Intro; same page").
	report.check(
		f"{name}: the links are listed",
		ok(links) and [target.get("text") for target in data(links).get("targets") or []] == LINKS,
		json.dumps([target.get("text") for target in data(links).get("targets") or []])[:220],
	)

	landmark = bridge.call("list_reading_targets", {"item": "landmark", "limit": 10})
	if landmarks:
		report.check(
			f"{name}: landmarks are enumerated",
			ok(landmark) and len(data(landmark).get("targets") or []) >= 1,
			json.dumps(labels(data(landmark)))[:220],
		)
	else:
		report.check(
			f"{name}: landmarks are reported unsupported",
			error_code(landmark) == 4205,
			json.dumps(error_details(landmark).get("available"))[:200],
		)

	# `reference` is a Word-only type: no browser implements it, which is the
	# case the capability probe exists for. Everything else in that reply has to
	# be true of this document.
	unsupported = bridge.call("list_reading_targets", {"item": "reference"})
	details = error_details(unsupported)
	report.check(f"{name}: an unsupported type is a domain error", error_code(unsupported) == 4205, str(error_code(unsupported)))
	report.check(
		f"{name}: the refusal lists what the document can do",
		"heading" in (details.get("available") or []) and "reference" not in (details.get("available") or []),
		json.dumps(details.get("available"))[:200],
	)
	report.check(
		f"{name}: the supported list was measured, not declared",
		details.get("source") in ("probed", "attempted"),
		str(details.get("source")),
	)

	if name != "Edge":
		return

	# --- navigation, on one backend, so a failure names the move that broke ---
	# Nothing here activates a window, so a page that lost the focus to another
	# window would make every move land somewhere else. One check that the
	# document in front is still the acceptance page is worth more than a dozen
	# confusing label mismatches.
	current = bridge.call("list_reading_targets", {"item": "heading", "limit": 1})
	focused = str(data(current).get("document", ""))
	if not focused.lower().startswith(title.lower()):
		report.check(
			f"{name}: the document in front is still the acceptance page",
			False,
			focused or "no document",
		)
		return

	first = bridge.call("navigate_reading", {"item": "heading", "direction": "first"})
	report.check(f"{name}: 'first' lands on the first heading", data(first).get("label") == title, str(data(first).get("label")))
	report.check(f"{name}: the move reports how far it went", data(first).get("reached") == 1, str(data(first).get("reached")))
	report.check(f"{name}: the move reports a settle outcome", "changed" in result(first), json.dumps(result(first))[:160])
	report.check(
		f"{name}: a completed move says it made every step",
		data(first).get("stopped") == "steps",
		str(data(first).get("stopped")),
	)

	steps = bridge.call("navigate_reading", {"item": "heading", "direction": "next", "steps": 2})
	report.check(f"{name}: two steps skip two headings", data(steps).get("label") == "Background", str(data(steps).get("label")))
	report.check(f"{name}: skipped moves are counted", data(steps).get("reached") == 2, str(data(steps).get("reached")))

	previous = bridge.call("navigate_reading", {"item": "heading", "direction": "previous", "steps": 1})
	report.check(f"{name}: 'previous' walks back", data(previous).get("label") == "Introduction", str(data(previous).get("label")))

	last = bridge.call("navigate_reading", {"item": "heading", "direction": "last"})
	report.check(f"{name}: 'last' lands on the final heading", data(last).get("label") == "Results", str(data(last).get("label")))
	report.check(
		f"{name}: 'last' says the document ended",
		data(last).get("stopped") == "end",
		str(data(last).get("stopped")),
	)

	link_first = bridge.call("navigate_reading", {"item": "link", "direction": "first"})
	link_ref = str(data(link_first).get("ref") or "")
	report.check(f"{name}: a link position carries a reference", bool(link_ref), f"{data(link_first).get('role')} {link_ref}")
	report.check(f"{name}: the landing is the first link", data(link_first).get("text") == LINKS[0], str(data(link_first).get("text")))
	if link_ref:
		read = bridge.call("read", {"ref": link_ref, "scope": "line"})
		report.check(
			f"{name}: the landing reference reads the link back",
			ok(read) and LINKS[0] in str(data(read).get("text", "")),
			f"{(data(read).get('text') or '')[:60]} ({data(read).get('source')})",
		)

	link_last = bridge.call("navigate_reading", {"item": "link", "direction": "last"})
	report.check(f"{name}: the last link is the last one on the page", data(link_last).get("text") == LINKS[-1], str(data(link_last).get("text")))

	absent = bridge.call("navigate_reading", {"item": "table", "direction": "next"})
	report.check(f"{name}: moving to a missing type is a domain error", error_code(absent) == 4205, str(error_code(absent)))
	report.check(
		f"{name}: the error names the move that failed",
		"table" in str((absent.get("error") or {}).get("message", "")),
		str((absent.get("error") or {}).get("message", "")),
	)

	bad_item = bridge.call("list_reading_targets", {"item": "paragraph"})
	report.check(f"{name}: an unknown item is a input error", error_code(bad_item) == 4103, str(error_code(bad_item)))
	report.check(
		f"{name}: the input error names the valid set",
		"heading" in str((bad_item.get("error") or {}).get("message", "")),
		str((bad_item.get("error") or {}).get("message", ""))[:160],
	)

	bad_steps = bridge.call("navigate_reading", {"item": "heading", "direction": "last", "steps": 2})
	report.check(f"{name}: 'last' refuses a step count", error_code(bad_steps) == 4103, str(error_code(bad_steps)))


def check_without_a_document(bridge: Bridge, report: Report) -> None:
	"""An application with no document must say so, not fail obscurely."""
	report.section("an application with no document")
	existing = bool(_windows_matching("Notepad"))
	if not existing:
		subprocess.Popen(["notepad.exe"])  # noqa: S603
	if not wait_for(lambda: bool(_windows_matching("Notepad")), timeout_s=15, interval_s=0.3):
		report.skip("no-document checks", "Notepad did not open")
		return
	time.sleep(1.0)
	if not activate(bridge, "Notepad"):
		report.skip("no-document checks", f"Notepad did not take the foreground ({window_title(bridge)})")
		close_titled_window("Notepad")
		return

	response = bridge.call("list_reading_targets", {"item": "heading"})
	details = error_details(response)
	report.check("a plain application has no document", error_code(response) == 4205, str(error_code(response)))
	report.check("it says browse mode is off", details.get("browse_mode") is False, json.dumps(details)[:200])
	report.check(
		"it says why",
		"browse mode" in str(details.get("reason", "")),
		str(details.get("reason")),
	)

	move = bridge.call("navigate_reading", {"item": "heading", "direction": "next"})
	report.check("moving in it fails the same way", error_code(move) == 4205, str(error_code(move)))

	close_titled_window("Notepad")
	time.sleep(0.8)


def check_word(bridge: Bridge, report: Report, word: str) -> None:
	"""A word processor is not a page: NVDA reads it in focus mode until asked.

	`start_browse_mode` is the opt-in that turns browse mode on, the way
	NVDA+space does. Without it the document is honestly reported as having no
	browse mode, and it is a separate action because it changes the session.
	"""
	report.section("reading a Word document")
	fixture = REPO / ".probe" / "word_reading.docx"
	generator = REPO / "scripts" / "make_word_fixture.ps1"

	def powershell(*extra: str):
		return subprocess.run(  # noqa: S603
			[
				"powershell",
				"-NoProfile",
				"-ExecutionPolicy",
				"Bypass",
				"-File",
				str(generator),
				"-Path",
				str(fixture),
				*extra,
			],
			capture_output=True,
			text=True,
		)

	if not fixture.is_file():
		generated = powershell()
		if not fixture.is_file():
			report.skip("Word reading checks", f"no fixture and no Word ({generated.stderr.strip()[:120]})")
			return

	# Opened through Word itself: file association lands in Protected View,
	# which is read-only and has no browse mode.
	opened = powershell("-Open")
	if not opened.returncode == 0:
		report.skip("Word reading checks", f"Word would not open the document ({opened.stderr.strip()[:120]})")
		return
	if not wait_for(lambda: bool(_windows_matching("word_reading")), timeout_s=40, interval_s=0.5):
		report.skip("Word reading checks", "the document window did not appear")
		close_titled_window("word_reading")
		return
	time.sleep(1.5)
	report.check("the document window is raised", activate(bridge, "word_reading"), window_title(bridge))
	time.sleep(1.0)

	# Focus mode is the default, and the tool must say so rather than guess.
	without = bridge.call("list_reading_targets", {"item": "heading", "limit": 5})
	report.check(
		"a Word document reports no browse mode until asked",
		error_code(without) in (4205, None),
		f"{error_code(without)} {error_details(without).get('reason', '')}"[:200],
	)

	started = bridge.call("start_browse_mode")
	report.check(
		"browse mode can be started for a focus-mode document",
		ok(started) and data(started).get("created") is True,
		f"{error_code(started)} {json.dumps(data(started))[:160]}",
	)
	again = bridge.call("start_browse_mode")
	report.check(
		"asking twice does not create a second document",
		ok(again) and data(again).get("created") is False,
		json.dumps(data(again))[:160],
	)

	response = None
	for attempt in range(4):
		response = bridge.call("list_reading_targets", {"item": "heading", "limit": 10})
		if ok(response):
			break
		bridge.call("press", {"keys": "control+home"})
		time.sleep(1.0)
	block = data(response)
	report.check("a Word document can be read after starting browse mode", ok(response), str(error_code(response)))
	if not ok(response):
		close_titled_window("word_reading")
		return

	report.check(
		"Word answers through its browse mode document",
		"Word" in str(block.get("interface")),
		str(block.get("interface")),
	)
	report.check(
		"the headings are in order with their levels",
		labels(block) == ["Reading Acceptance Word", "Introduction", "Background", "Results"]
		and [target.get("level") for target in block.get("targets") or []] == [1, 2, 3, 2],
		f"{json.dumps(labels(block))} {json.dumps([t.get('level') for t in block.get('targets') or []])}"[:240],
	)
	report.check("the document is named after its file", "word_reading" in str(block.get("document")), str(block.get("document")))

	move = bridge.call("navigate_reading", {"item": "heading", "direction": "next", "steps": 2})
	report.check("moving in a Word document works", data(move).get("label") == "Background", str(data(move).get("label")))

	reference = bridge.call("list_reading_targets", {"item": "reference", "limit": 3})
	report.check(
		"Word offers the types a word processor has",
		ok(reference) and "reference" in (data(reference).get("available") or []),
		json.dumps(data(reference).get("available"))[:200],
	)

	close_titled_window("word_reading")
	time.sleep(0.6)


def main(argv=None) -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--token", default=None)
	parser.add_argument("--verbose", action="store_true")
	parser.add_argument("--keep", action="store_true", help="leave the browser windows open")
	parser.add_argument("--skip-firefox", action="store_true")
	parser.add_argument("--word", action="store_true", help="also check a Word document (needs Word)")
	parser.add_argument(
		"--only",
		choices=("all", "edge", "firefox", "nodoc", "word"),
		default="all",
		help="run one section only, for working on one backend at a time",
	)
	args = parser.parse_args(argv)

	bridge = Bridge(resolve_token(args.token))
	report = Report(verbose=args.verbose)
	title = f"Reading Acceptance {uuid.uuid4().hex[:6]}"
	scratch = REPO / ".probe"
	scratch.mkdir(exist_ok=True)
	page = write_page(scratch, title)
	edge = next((path for path in (EDGE, EDGE_ALT) if Path(path).is_file()), None)
	try:
		# A leftover probe window would be the foreground window for the whole run.
		close_titled_window("Reading Acceptance")
		close_titled_window("Notepad")
		time.sleep(0.6)

		if args.only in ("all", "edge") and edge and launch(edge, page):
			check_document(
			bridge,
			report,
			title,
			"Edge",
			landmarks=True,
			# NVDA can read a Chromium page through two implementations: the
			# virtual buffer, or the UIA tree. Both must answer.
			implementations=("ChromeVBuf", "ChromiumUIATreeInterceptor", "UIAWebTreeInterceptor"),
		)
		elif args.only in ("all", "edge"):
			report.skip("Edge reading checks", "no Chromium browser found")
		if not args.keep:
			close_titled_window(title)
			time.sleep(1.0)

		if args.skip_firefox or args.only not in ("all", "firefox"):
			report.skip("Firefox reading checks", "not requested")
		elif launch(FIREFOX, page):
			check_document(
			bridge,
			report,
			title,
			"Firefox",
			landmarks=True,
			# `Gecko_ia2` is the object class NVDA reports for a Firefox document.
			implementations=("Gecko",),
		)
		else:
			report.skip("Firefox reading checks", "Firefox is not installed")
		if not args.keep:
			close_titled_window(title)
			time.sleep(0.8)

		if args.only in ("all", "nodoc"):
			check_without_a_document(bridge, report)

		if args.word or args.only == "word":
			check_word(bridge, report, WORD)
	finally:
		if not args.keep:
			close_titled_window(title)
			close_titled_window("Notepad")
			try:
				page.unlink()
				scratch.rmdir()
			except OSError:
				pass
		# Leave the user where they were, ignoring the outcome: this is polite,
		# not a check.
		activate(bridge, "dsh", attempts=4)

	failures = report.failed
	total = len([check for check in report.checks if not check.get("note") and not check.get("skipped")])
	skipped = len([check for check in report.checks if check.get("skipped")])
	print(f"\n{total - len(failures)}/{total} checks passed" + (f" ({skipped} skipped)" if skipped else ""))
	for failure in failures:
		print(f"FAILED {failure['name']}: {failure['detail']}")
	return 0 if not failures else 1


if __name__ == "__main__":
	try:
		sys.exit(main())
	except Exception as error:  # noqa: BLE001
		print(f"acceptance harness error: {type(error).__name__}: {error}", file=sys.stderr)
		sys.exit(2)
