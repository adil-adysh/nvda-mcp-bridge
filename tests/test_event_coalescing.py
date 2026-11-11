# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Tests for the coalescing event journal (P1).

The behaviour these pin is the one the browser measurements demanded: one user
action produces a burst of events, and a client must be told about the burst, not
about each event in it.
"""

import importlib
import os
import sys
import types
import unittest

_ADDON_DIR = os.path.normpath(
	os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "addon", "globalPlugins", "nvdaMcp-bridge")
)
if _ADDON_DIR not in sys.path:
	sys.path.insert(0, _ADDON_DIR)

if "logHandler" not in sys.modules:
	sys.modules["logHandler"] = types.SimpleNamespace(
		log=types.SimpleNamespace(
			debug=lambda *args, **kwargs: None,
			info=lambda *args, **kwargs: None,
			warning=lambda *args, **kwargs: None,
			error=lambda *args, **kwargs: None,
		)
	)

_PACKAGE_NAME = "nvdaMcpBridge"
if _PACKAGE_NAME not in sys.modules:
	_package = types.ModuleType(_PACKAGE_NAME)
	_package.__path__ = [_ADDON_DIR]
	sys.modules[_PACKAGE_NAME] = _package

events = importlib.import_module(f"{_PACKAGE_NAME}.server.events")


class _FakeObject:
	def __init__(self, role=42, name="field"):
		self.role = role
		self.name = name


class CoarseKindTests(unittest.TestCase):
	def test_structural_event_wins_over_property_change(self):
		# One focus move also carries name/value changes; "focus" is the headline.
		self.assertEqual(events.coarse_kind(["nameChange", "gainFocus", "valueChange"]), "focus")

	def test_value_beats_name(self):
		self.assertEqual(events.coarse_kind(["nameChange", "valueChange"]), "value")

	def test_unknown_events_are_other(self):
		self.assertEqual(events.coarse_kind(["somethingNew"]), "other")

	def test_live_region_is_kept(self):
		self.assertEqual(events.coarse_kind(["liveRegionChange", "nameChange"]), "liveRegion")


class JournalTests(unittest.TestCase):
	def test_raw_history_is_off_by_default(self):
		journal = events.EventJournal()
		self.assertIsNone(journal.record_raw("caret"))
		self.assertEqual(journal.version, 0)

	def test_raw_history_records_when_enabled(self):
		journal = events.EventJournal()
		journal.raw_enabled = True
		journal.record_raw("caret", _FakeObject())
		page = journal.snapshot(raw=True)
		self.assertEqual(len(page["events"]), 1)
		self.assertEqual(page["events"][0]["kind"], "caret")

	def test_transitions_are_the_default_surface(self):
		journal = events.EventJournal()
		journal.record_transition("focus", kinds=["gainFocus"], count=4)
		page = journal.snapshot()
		self.assertEqual(page["source"], "transitions")
		self.assertEqual(len(page["events"]), 1)
		self.assertEqual(page["events"][0]["count"], 4)

	def test_kind_filter_matches_any_member_of_a_burst(self):
		journal = events.EventJournal()
		journal.record_transition("focus", kinds={"gainFocus", "valueChange"})
		# Filters accept the coarse kind, the NVDA event name, or a co-member.
		self.assertEqual(len(journal.snapshot(kinds=["value"])["events"]), 1)
		self.assertEqual(len(journal.snapshot(kinds=["valueChange"])["events"]), 1)
		self.assertEqual(len(journal.snapshot(kinds=["name"])["events"]), 0)

	def test_truncated_is_reported_when_the_cursor_is_too_old(self):
		journal = events.EventJournal(capacity=2)
		for _ in range(4):
			journal.record_transition("focus")
		page = journal.snapshot(since=0)
		self.assertTrue(page["truncated"])
		self.assertEqual(page["dropped"], 2)


class CoalescerTests(unittest.TestCase):
	def test_a_burst_becomes_one_transition(self):
		journal = events.EventJournal()
		coalescer = events.TransitionCoalescer(journal, quiet_ms=20)
		coalescer.start()
		try:
			for name in ("valueChange", "caret", "caret", "nameChange", "valueChange"):
				coalescer.record(name, _FakeObject(name="abc"))
			self.assertTrue(journal.wait(since=0, timeout_ms=1000)["events"])
			page = journal.snapshot()
			self.assertEqual(len(page["events"]), 1)
			entry = page["events"][0]
			self.assertEqual(entry["count"], 5)
			self.assertEqual(entry["kind"], "value")
			self.assertIn("caret", entry["kinds"])
			self.assertEqual(entry["name"], "abc")
		finally:
			coalescer.stop()

	def test_two_bursts_are_two_transitions(self):
		journal = events.EventJournal()
		coalescer = events.TransitionCoalescer(journal, quiet_ms=20)
		coalescer.start()
		try:
			coalescer.record("gainFocus", _FakeObject(name="first"))
			self.assertTrue(journal.wait(since=0, timeout_ms=1000)["events"])
			coalescer.record("gainFocus", _FakeObject(name="second"))
			self.assertTrue(journal.wait(since=1, timeout_ms=1000)["events"])
			names = [entry["name"] for entry in journal.snapshot()["events"]]
			self.assertEqual(names, ["first", "second"])
		finally:
			coalescer.stop()

	def test_flush_emits_without_waiting(self):
		journal = events.EventJournal()
		coalescer = events.TransitionCoalescer(journal, quiet_ms=10000)
		coalescer.record("gainFocus", _FakeObject())
		self.assertEqual(journal.version, 0)
		coalescer.flush()
		self.assertEqual(journal.version, 1)

	def test_every_event_bumps_the_revision_callback(self):
		journal = events.EventJournal()
		coalescer = events.TransitionCoalescer(journal, quiet_ms=20)
		bumps = []
		coalescer.on_event = lambda: bumps.append(1)
		for name in ("caret", "caret", "caret"):
			coalescer.record(name)
		self.assertEqual(len(bumps), 3)

	def test_recorder_never_raises_on_a_dead_object(self):
		class Exploding:
			@property
			def role(self):
				raise RuntimeError("dead object")

			@property
			def name(self):
				raise RuntimeError("dead object")

		journal = events.EventJournal()
		recorder = events.EventRecorder(journal)
		recorder.record("gainFocus", Exploding())
		self.assertEqual(journal.version, 0)

	def test_stop_flushes_a_pending_burst(self):
		journal = events.EventJournal()
		coalescer = events.TransitionCoalescer(journal, quiet_ms=10000)
		coalescer.start()
		coalescer.record("gainFocus", _FakeObject())
		coalescer.stop()
		self.assertEqual(journal.version, 1)


if __name__ == "__main__":
	unittest.main()
