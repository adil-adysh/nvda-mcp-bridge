# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Contracts for the independent, bounded speech observation journal."""

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
		log=types.SimpleNamespace(debug=lambda *a, **k: None, info=lambda *a, **k: None,
			warning=lambda *a, **k: None, error=lambda *a, **k: None)
	)

_PACKAGE_NAME = "nvdaMcpBridge"
if _PACKAGE_NAME not in sys.modules:
	_package = types.ModuleType(_PACKAGE_NAME)
	_package.__path__ = [_ADDON_DIR]
	sys.modules[_PACKAGE_NAME] = _package

speech = importlib.import_module(f"{_PACKAGE_NAME}.server.speech")
events = importlib.import_module(f"{_PACKAGE_NAME}.server.events")


class SpeechExtractionTests(unittest.TestCase):
	def test_extract_speech_text_joins_only_string_chunks(self):
		command = object()
		self.assertEqual(speech.extract_speech_text(["Save", command, " complete"]), ("Save complete", False))

	def test_extract_speech_text_truncates_at_limit(self):
		text, truncated = speech.extract_speech_text(["x" * 1030], limit=1024)
		self.assertEqual(len(text), 1024)
		self.assertTrue(truncated)

	def test_empty_speech_sequence_is_not_recorded(self):
		journal = speech.SpeechJournal()
		for sequence in ([], [object(), 4, None]):
			text, truncated = speech.extract_speech_text(sequence)
			self.assertEqual((text, truncated), ("", False))
			self.assertIsNone(journal.record_speech(text, "queued", truncated))
		self.assertEqual(journal.version, 0)


class SpeechJournalTests(unittest.TestCase):
	def test_speech_journal_versions_are_independent_and_ordered(self):
		ui_journal = events.EventJournal()
		speech_journal = speech.SpeechJournal()
		ui_version = ui_journal.version
		self.assertEqual(speech_journal.record_speech("one", "queued"), 1)
		self.assertEqual(speech_journal.record_control("speech_canceled"), 2)
		page = speech_journal.snapshot()
		self.assertEqual([item["version"] for item in page["events"]], [1, 2])
		self.assertLess(page["events"][0]["at_ms"], page["events"][1]["at_ms"])
		self.assertEqual(ui_journal.version, ui_version)

	def test_speech_journal_retains_256_and_reports_expired_cursor(self):
		journal = speech.SpeechJournal()
		for index in range(257):
			journal.record_speech(str(index), "queued")
		page = journal.snapshot(since=0)
		self.assertEqual(page["earliest_available_version"], 2)
		self.assertEqual(page["dropped"], 1)
		self.assertTrue(page["expired"])
		self.assertTrue(page["resync_required"])
		self.assertEqual(page["resync_reason"], "cursor_expired")

	def test_speech_journal_pagination_has_no_gaps(self):
		journal = speech.SpeechJournal(capacity=5)
		for index in range(7):
			journal.record_speech(str(index), "queued")
		versions = []
		since = 2
		while True:
			page = journal.snapshot(since=since, limit=2, journal_id=journal.journal_id)
			versions.extend(item["version"] for item in page["events"])
			since = page["to_version"]
			if not page["has_more"]:
				break
		self.assertEqual(versions, [3, 4, 5, 6, 7])

	def test_speech_journal_reset_changes_id_and_rejects_old_cursor(self):
		journal = speech.SpeechJournal()
		journal.record_speech("private text", "queued")
		old_id = journal.journal_id
		journal.reset("permission_revoked")
		page = journal.snapshot(since=1, journal_id=old_id)
		self.assertNotEqual(old_id, journal.journal_id)
		self.assertEqual(page["cursor_status"], "journal_mismatch")
		self.assertTrue(page["resync_required"])
		self.assertEqual(page["events"], [])
		self.assertEqual(journal.version, 0)

	def test_speech_journal_wait_handles_timeout_event_and_cancellation(self):
		journal = speech.SpeechJournal()
		timeout_page = journal.wait(timeout_ms=5)
		self.assertTrue(timeout_page["timed_out"])
		result = {}
		waiter = threading.Thread(target=lambda: result.update(journal.wait(since=0, timeout_ms=1000)))
		waiter.start()
		time.sleep(0.02)
		journal.record_speech("ready", "queued")
		waiter.join(timeout=2)
		self.assertFalse(waiter.is_alive())
		self.assertEqual([event["text"] for event in result["events"]], ["ready"])
		cancel = threading.Event()
		cancel.set()
		cancelled_page = journal.wait(since=journal.version, timeout_ms=500, cancellation_event=cancel)
		self.assertTrue(cancelled_page["cancelled"])
		self.assertFalse(cancelled_page["timed_out"])


if __name__ == "__main__":
	unittest.main()
