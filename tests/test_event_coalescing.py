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

	def test_each_journal_has_an_identity_of_its_own(self):
		"""A version number only means something inside the journal that issued it.

		NVDA restarts begin again at zero, so a client has to be able to tell
		"nothing happened" apart from "the journal I was reading is gone".
		"""
		first = events.EventJournal()
		second = events.EventJournal()

		self.assertTrue(first.journal_id)
		self.assertNotEqual(first.journal_id, second.journal_id)
		self.assertEqual(first.snapshot()["journal_id"], first.journal_id)

	def test_paging_returns_the_oldest_unseen_entries(self):
		journal = events.EventJournal()
		for index in range(5):
			journal.record_transition("focus", name=f"e{index}")

		page = journal.snapshot(since=0, limit=2)

		self.assertEqual([entry["name"] for entry in page["events"]], ["e0", "e1"])
		self.assertEqual(page["to_version"], page["events"][-1]["version"])
		self.assertEqual(page["state_version"], 5)
		self.assertTrue(page["has_more"])

	def test_paging_to_the_end_says_there_is_no_more(self):
		journal = events.EventJournal()
		for index in range(4):
			journal.record_transition("focus", name=f"e{index}")

		first = journal.snapshot(since=0, limit=2)
		second = journal.snapshot(since=first["to_version"], limit=2)

		self.assertEqual([entry["name"] for entry in second["events"]], ["e2", "e3"])
		self.assertFalse(second["has_more"])
		self.assertEqual(second["to_version"], 4)

	def test_a_cursor_never_advances_past_an_entry_it_was_not_given(self):
		"""The old contract returned the newest page and a cursor at the head.

		Following that cursor skipped everything between, with no record that it
		happened. Paging now walks the journal, one page at a time.
		"""
		journal = events.EventJournal()
		for index in range(7):
			journal.record_transition("focus", name=f"e{index}")

		seen = []
		cursor = 0
		for _ in range(10):
			page = journal.snapshot(since=cursor, limit=3)
			seen.extend(entry["name"] for entry in page["events"])
			cursor = page["to_version"]
			if not page["has_more"]:
				break

		self.assertEqual(seen, [f"e{index}" for index in range(7)])

	def test_an_empty_page_does_not_move_the_cursor(self):
		journal = events.EventJournal()
		page = journal.snapshot(since=42)

		self.assertEqual(page["events"], [])
		self.assertEqual(page["to_version"], 42)
		self.assertFalse(page["has_more"])


class PaginationTests(unittest.TestCase):
	"""Cursor semantics the Rust event pump depends on.

	The pump used to advance to the journal's head (`state_version`) instead of
	the last event it was given (`to_version`), which skipped every event between
	the page and the head. These pin the contract that makes that avoidable.
	"""

	def test_one_page_reports_caught_up(self):
		journal = events.EventJournal()
		for index in range(3):
			journal.record_transition("focus", name=f"e{index}")

		page = journal.snapshot(since=0, limit=10)

		self.assertEqual(page["to_version"], 3)
		self.assertEqual(page["event_version"], 3)
		self.assertEqual(page["state_version"], 3)
		self.assertFalse(page["has_more"])
		self.assertTrue(page["complete"])
		self.assertTrue(page["caught_up"])
		self.assertEqual(page["cursor_status"], "ok")

	def test_a_full_page_reports_paged_not_caught_up(self):
		journal = events.EventJournal()
		for index in range(10):
			journal.record_transition("focus")

		page = journal.snapshot(since=0, limit=4)

		self.assertEqual(page["to_version"], 4)
		self.assertEqual(page["event_version"], 10)
		self.assertTrue(page["has_more"])
		self.assertTrue(page["complete"], "this page lost nothing")
		self.assertFalse(page["caught_up"], "the caller has not seen everything")
		self.assertEqual(page["cursor_status"], "paged")
		self.assertEqual(page["next_cursor"], 4)

	def test_more_events_than_the_page_limit_are_never_skipped(self):
		journal = events.EventJournal()
		total = 250
		for index in range(total):
			journal.record_transition("focus", name=f"e{index}")

		seen = []
		cursor = 0
		requests = 0
		while requests < 100:
			requests += 1
			page = journal.snapshot(since=cursor, limit=100)
			seen.extend(entry["name"] for entry in page["events"])
			cursor = page["to_version"]
			if not page["has_more"]:
				break

		self.assertEqual(seen, [f"e{index}" for index in range(total)])
		self.assertEqual(cursor, total)
		self.assertEqual(requests, 3)

	def test_an_event_burst_is_one_transition_at_one_version(self):
		journal = events.EventJournal()
		coalescer = events.TransitionCoalescer(journal, quiet_ms=10)
		coalescer.start()
		try:
			for _ in range(5):
				coalescer.record("valueChange", _FakeObject(name="abc"))
			self.assertTrue(journal.wait(since=0, timeout_ms=1000)["events"])

			page = journal.snapshot(since=0, limit=10)

			self.assertEqual(page["to_version"], 1, "one burst is one cursor step")
			self.assertEqual(page["events"][0]["count"], 5)
		finally:
			coalescer.stop()

	def test_a_cursor_older_than_retained_history_is_reported_as_stale(self):
		journal = events.EventJournal(capacity=2)
		for _ in range(5):
			journal.record_transition("focus")

		page = journal.snapshot(since=0, limit=10)

		self.assertEqual(page["cursor_status"], "stale")
		self.assertTrue(page["truncated"])
		self.assertFalse(page["complete"])
		self.assertFalse(page["caught_up"])
		self.assertEqual(page["oldest_retained_version"], 4)
		self.assertEqual(page["dropped"], 3)

	def test_a_cursor_from_another_journal_is_reported_as_future(self):
		journal = events.EventJournal()
		journal.record_transition("focus")

		page = journal.snapshot(since=900, limit=10)

		self.assertEqual(page["events"], [])
		self.assertEqual(page["cursor_status"], "future_cursor")
		self.assertEqual(page["to_version"], 900, "the cursor must not move backwards")
		self.assertFalse(page["has_more"])

	def test_a_restarted_journal_has_a_new_identity(self):
		before = events.EventJournal()
		before.record_transition("focus")
		after = events.EventJournal()

		self.assertNotEqual(before.journal_id, after.journal_id)
		# The old cursor is a future cursor here, which is how the consumer
		# knows to reset rather than wait forever.
		self.assertEqual(after.snapshot(since=before.version)["cursor_status"], "future_cursor")

	def test_an_empty_history_is_caught_up_not_stale(self):
		journal = events.EventJournal()

		page = journal.snapshot(since=0)

		self.assertEqual(page["events"], [])
		self.assertEqual(page["to_version"], 0)
		self.assertEqual(page["oldest_retained_version"], 0)
		self.assertEqual(page["cursor_status"], "ok")
		self.assertTrue(page["complete"])
		self.assertTrue(page["caught_up"])

	def test_repeated_polling_without_new_events_does_not_move_the_cursor(self):
		journal = events.EventJournal()
		journal.record_transition("focus")

		first = journal.snapshot(since=0)
		second = journal.snapshot(since=first["to_version"])
		third = journal.wait(since=first["to_version"], timeout_ms=10)

		for page in (second, third):
			self.assertEqual(page["events"], [])
			self.assertEqual(page["to_version"], first["to_version"])
			self.assertTrue(page["caught_up"])

	def test_events_arriving_during_catch_up_are_still_ahead(self):
		journal = events.EventJournal()
		for index in range(150):
			journal.record_transition("focus", name=f"e{index}")

		first = journal.snapshot(since=0, limit=100)
		for index in range(150, 160):
			journal.record_transition("focus", name=f"e{index}")
		second = journal.snapshot(since=first["to_version"], limit=100)

		self.assertEqual(first["to_version"], 100)
		self.assertEqual(second["to_version"], 160)
		self.assertEqual(len(second["events"]), 60, "the events that arrived mid-catch-up are delivered")
		self.assertFalse(second["has_more"])

	def test_the_version_vocabulary_is_reported_separately(self):
		journal = events.EventJournal()
		for _ in range(10):
			journal.record_transition("focus")

		page = journal.snapshot(since=0, limit=3)

		self.assertEqual(page["event_version"], page["state_version"])
		self.assertNotEqual(page["to_version"], page["event_version"])
		self.assertEqual(page["next_cursor"], page["to_version"])


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
