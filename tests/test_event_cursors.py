# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Typed event-cursor contract (API level 1).

The journal is a bounded ring buffer, so a numeric version is not a portable
cursor: it only means something inside the generation that issued it. These
tests pin the three independent concepts the contract distinguishes —
projection ``state_version``, journal ``event_version`` and ``journal_id`` —
and the rule that an explicit catch-up call can never present evicted history
as complete.
"""

import importlib
import os
import sys
import threading
import time
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
handlers = importlib.import_module(f"{_PACKAGE_NAME}.tools.handlers")


class _Context:
	"""Minimal coordinator context: only the event surface is exercised."""

	def __init__(self, journal):
		self.journal = journal
		self.cancellation_event = None
		self.calls = []

	def event_version(self):
		return self.journal.version

	def state_version(self):
		return 123456

	def wait_events(self, since, timeout_ms, kinds, limit=100, journal_id=None, cancellation_event=None):
		self.calls.append(("wait_events", since, timeout_ms, journal_id))
		return self.journal.wait(
			since=since,
			timeout_ms=timeout_ms,
			kinds=kinds,
			limit=limit,
			journal_id=journal_id,
			cancellation_event=cancellation_event,
		)


def _fill(journal, count, start=0):
	for index in range(start, start + count):
		journal.record_transition("focus", name=f"e{index}")


class JournalIdentityTests(unittest.TestCase):
	def test_a_journal_id_is_unpredictable_and_regenerated_on_reset(self):
		journal = events.EventJournal()
		first = journal.journal_id
		self.assertRegex(first, r"^[0-9a-f]{16}$")
		journal.record_transition("focus")
		second = journal.reset("test")
		self.assertNotEqual(first, second)
		self.assertEqual(journal.version, 0, "a new generation starts its own sequence")
		self.assertEqual(journal.snapshot()["journal_id"], second)

	def test_state_version_does_not_travel_with_the_event_cursor(self):
		journal = events.EventJournal()
		_fill(journal, 3)
		page = journal.snapshot(since=0)
		# Three independent numeric meanings in one response.
		self.assertEqual(page["event_version"], 3)
		self.assertEqual(page["to_version"], 3)
		self.assertEqual(page["state_version"], page["event_version"])


class NoCursorTests(unittest.TestCase):
	def test_no_cursor_starts_at_the_head_and_does_not_replay_history(self):
		journal = events.EventJournal()
		_fill(journal, 5)
		context = _Context(journal)
		result = handlers.wait_for_events(since=None, timeout_ms=0, kinds=None, context=context)
		self.assertEqual(context.calls[0][1], 5, "default is the current head")
		self.assertEqual(result["events"], [])
		self.assertFalse(result["resync_required"])

	def test_an_event_after_the_wait_begins_is_returned(self):
		journal = events.EventJournal()
		_fill(journal, 2)
		result_box = {}

		def wait():
			result_box.update(
				handlers.wait_for_events(since=None, timeout_ms=2000, kinds=None, context=_Context(journal))
			)

		thread = threading.Thread(target=wait)
		thread.start()
		time.sleep(0.05)
		journal.record_transition("value", name="typed")
		thread.join(timeout=3)
		self.assertFalse(thread.is_alive())
		self.assertEqual([entry["name"] for entry in result_box["events"]], ["typed"])
		self.assertEqual(result_box["journal_id"], journal.journal_id)


class ExplicitCursorTests(unittest.TestCase):
	def test_a_valid_cursor_returns_only_retained_events_after_it(self):
		journal = events.EventJournal()
		_fill(journal, 4)
		page = journal.snapshot(since=1, limit=10, journal_id=journal.journal_id)
		self.assertEqual([entry["name"] for entry in page["events"]], ["e1", "e2", "e3"])
		self.assertFalse(page["expired"])
		self.assertTrue(page["history_complete"])

	def test_the_earliest_retained_boundary_is_usable(self):
		journal = events.EventJournal(capacity=3)
		_fill(journal, 5)
		earliest = journal.snapshot()["earliest_available_version"]
		page = journal.snapshot(since=earliest - 1, journal_id=journal.journal_id)
		self.assertEqual(earliest, 3)
		self.assertFalse(page["expired"], "the cursor still reaches every retained event")
		self.assertEqual(len(page["events"]), 3)

	def test_an_expired_cursor_is_explicit_and_carries_a_resync_cursor(self):
		journal = events.EventJournal(capacity=2)
		_fill(journal, 5)
		page = journal.snapshot(since=0, journal_id=journal.journal_id)
		self.assertTrue(page["expired"])
		self.assertTrue(page["resync_required"])
		self.assertEqual(page["resync_reason"], events.RESYNC_EXPIRED)
		self.assertFalse(page["history_complete"])
		self.assertEqual(page["earliest_available_version"], 4)
		self.assertEqual(page["resync_from"], {"journal_id": journal.journal_id, "version": 3})
		self.assertEqual([entry["name"] for entry in page["events"]], ["e3", "e4"])
		self.assertIn("resync", page["recovery"]["action"])

	def test_an_expired_cursor_with_no_matching_kind_is_still_expired(self):
		journal = events.EventJournal(capacity=2)
		_fill(journal, 5)
		# Nothing named "caret" is retained, but history before the cursor was
		# still lost. Reporting `expired: false` here would claim completeness
		# for a kind whose events may have been evicted.
		page = journal.snapshot(since=0, kinds=["caret"], journal_id=journal.journal_id)
		self.assertTrue(page["expired"])
		self.assertTrue(page["resync_required"])
		self.assertFalse(page["history_complete"])
		self.assertEqual(page["events"], [])

	def test_a_cursor_from_another_journal_is_not_interpreted(self):
		old = events.EventJournal()
		_fill(old, 10)
		new = events.EventJournal()
		_fill(new, 2)
		page = new.snapshot(since=7, journal_id=old.journal_id)
		self.assertEqual(page["cursor_status"], "journal_mismatch")
		self.assertTrue(page["resync_required"])
		self.assertEqual(page["resync_reason"], events.RESYNC_JOURNAL_MISMATCH)
		self.assertEqual(page["events"], [], "the old version must not select new events")
		self.assertEqual(page["journal_id"], new.journal_id)
		self.assertEqual(page["resync_from"], {"journal_id": new.journal_id, "version": 2})

	def test_pagination_has_no_gaps_or_duplicates(self):
		journal = events.EventJournal()
		_fill(journal, 250)
		seen = []
		cursor = 0
		for _ in range(100):
			page = journal.snapshot(since=cursor, limit=100, journal_id=journal.journal_id)
			seen.extend(entry["name"] for entry in page["events"])
			cursor = page["to_version"]
			if not page["has_more"]:
				break
		self.assertEqual(seen, [f"e{index}" for index in range(250)])
		self.assertEqual(len(seen), len(set(seen)))

	def test_an_empty_journal_is_caught_up_not_expired(self):
		journal = events.EventJournal()
		page = journal.snapshot(since=0, journal_id=journal.journal_id)
		self.assertFalse(page["expired"])
		self.assertFalse(page["resync_required"])
		self.assertEqual(page["earliest_available_version"], 0)


class WaitLifecycleTests(unittest.TestCase):
	def test_a_journal_reset_during_a_wait_ends_the_wait_with_a_mismatch(self):
		journal = events.EventJournal()
		old_id = journal.journal_id
		result_box = {}

		def wait():
			result_box.update(
				handlers.wait_for_events(
					since=0,
					journal_id=old_id,
					timeout_ms=5000,
					kinds=None,
					context=_Context(journal),
				)
			)

		thread = threading.Thread(target=wait)
		thread.start()
		time.sleep(0.05)
		journal.reset("test-reset")
		thread.join(timeout=2)
		self.assertFalse(thread.is_alive(), "a reset must not leave the waiter blocked")
		self.assertTrue(result_box["resync_required"])
		self.assertEqual(result_box["cursor_status"], "journal_mismatch")
		self.assertEqual(result_box["journal_id"], journal.journal_id)

	def test_cancellation_ends_a_wait_without_claiming_events(self):
		journal = events.EventJournal()
		context = _Context(journal)
		context.cancellation_event = threading.Event()
		result_box = {}

		def wait():
			result_box.update(
				handlers.wait_for_events(since=0, timeout_ms=5000, kinds=None, context=context)
			)

		thread = threading.Thread(target=wait)
		thread.start()
		time.sleep(0.05)
		context.cancellation_event.set()
		thread.join(timeout=2)
		self.assertFalse(thread.is_alive())
		self.assertTrue(result_box["cancelled"])
		self.assertEqual(result_box["events"], [])

	def test_shutdown_during_a_wait_is_a_cancellation_not_a_hang(self):
		journal = events.EventJournal()
		shutdown = threading.Event()
		result_box = {}

		def wait():
			result_box.update(
				journal.wait(since=0, timeout_ms=5000, cancellation_event=shutdown)
			)

		thread = threading.Thread(target=wait)
		thread.start()
		time.sleep(0.05)
		shutdown.set()
		thread.join(timeout=2)
		self.assertFalse(thread.is_alive())
		self.assertTrue(result_box.get("cancelled"))

	def test_concurrent_append_and_eviction_stay_internally_consistent(self):
		journal = events.EventJournal(capacity=8)
		stop = threading.Event()

		def writer():
			index = 0
			while not stop.is_set() and index < 400:
				journal.record_transition("focus", name=f"e{index}")
				index += 1
			stop.set()

		thread = threading.Thread(target=writer)
		thread.start()
		last_to = 0
		pages = 0
		try:
			while not stop.is_set() or pages < 20:
				page = journal.snapshot(since=last_to, limit=3, journal_id=journal.journal_id)
				self.assertEqual(page["journal_id"], journal.journal_id)
				self.assertGreaterEqual(page["to_version"], last_to)
				if page["expired"]:
					self.assertTrue(page["resync_required"])
				last_to = page["to_version"]
				pages += 1
		finally:
			stop.set()
			thread.join(timeout=3)
		self.assertGreater(pages, 0)


if __name__ == "__main__":
	unittest.main()
