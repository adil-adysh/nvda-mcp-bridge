# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Context-window tests for stable native log entry references."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _log_support import FIXTURE, log_diagnostics  # noqa: E402

from nvdaMcpBridge.server.log_diagnostics import filters as log_filters  # noqa: E402


class LogContextTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.snapshot = log_diagnostics.snapshot_log(FIXTURE)
		cls.records = list(log_diagnostics.LogReader(cls.snapshot).iter_records())

	def context(self, record, **kwargs):
		return log_diagnostics.get_log_context(self.snapshot, entry_id=record.entry_id, **kwargs)

	def test_first_record_has_no_before_context(self):
		result = self.context(self.records[0], before=5, after=2)
		self.assertEqual(result["before"], 0)
		self.assertTrue(result["complete_before"])
		self.assertEqual(result["after"], 2)
		self.assertFalse(result["complete_after"])
		self.assertEqual(result["entries"][0]["entry_id"], self.records[0].entry_id)
		self.assertEqual([entry["line_start"] for entry in result["entries"]], [1, 3, 5])

	def test_last_record_has_no_after_context(self):
		last = self.records[-1]
		result = self.context(last, before=2, after=5)
		self.assertEqual(result["after"], 0)
		self.assertTrue(result["complete_after"])
		self.assertEqual(result["entries"][-1]["entry_id"], last.entry_id)
		self.assertFalse(result["complete_before"])

	def test_middle_context_is_chronological_and_preserves_line_ranges(self):
		anchor = next(record for record in self.records if "BrowserNav focus handler" in record.message)
		result = self.context(anchor, before=2, after=2)
		self.assertEqual(
			[entry["line_start"] for entry in result["entries"]],
			sorted(entry["line_start"] for entry in result["entries"]),
		)
		self.assertEqual(result["entries"][2]["entry_id"], anchor.entry_id)
		for entry in result["entries"]:
			self.assertLessEqual(entry["line_start"], entry["line_end"])

	def test_thread_filter_keeps_only_the_anchor_thread(self):
		anchor = next(record for record in self.records if record.thread_name == "Dummy-88")
		result = self.context(anchor, before=10, after=10, include_related_threads=False)
		self.assertTrue(result["entries"])
		self.assertEqual({entry["thread"]["id"] for entry in result["entries"]}, {anchor.thread_id})
		self.assertTrue(any("Only the anchor entry's thread" in note for note in result["diagnostics"]["limitations"]))

	def test_all_threads_are_included_by_default(self):
		anchor = next(record for record in self.records if record.thread_name == "_checkForUpdatableAddons")
		result = self.context(anchor, before=3, after=3)
		thread_ids = {entry["thread"]["id"] for entry in result["entries"]}
		self.assertGreater(len(thread_ids), 1)

	def test_bounded_counts_are_enforced(self):
		with self.assertRaises(log_filters.InvalidFilterError):
			self.context(self.records[1], before=51)
		with self.assertRaises(log_filters.InvalidFilterError):
			self.context(self.records[1], after=-1)

	def test_stale_generation_is_reported(self):
		with tempfile.TemporaryDirectory() as directory:
			path = os.path.join(directory, "replace.log")
			shutil.copyfile(FIXTURE, path)
			snapshot = log_diagnostics.snapshot_log(path)
			anchor = list(log_diagnostics.LogReader(snapshot).iter_records())[2]
			replacement = os.path.join(directory, "replacement.log")
			shutil.copyfile(FIXTURE, replacement)
			os.replace(replacement, path)
			new_snapshot = log_diagnostics.snapshot_log(path)
			with self.assertRaises(log_diagnostics.StaleLogError):
				log_diagnostics.get_log_context(new_snapshot, entry_id=anchor.entry_id)

	def test_malformed_entry_reference_is_a_caller_error(self):
		with self.assertRaises(log_filters.InvalidFilterError):
			log_diagnostics.get_log_context(self.snapshot, entry_id="not-a-reference")

	def test_changed_bytes_at_the_same_offset_are_a_stale_reference(self):
		record = self.records[2]
		forged = f"{self.snapshot.generation}:{record.byte_start:x}:deadbeef"
		with self.assertRaises(log_diagnostics.StaleLogError) as raised:
			log_diagnostics.get_log_context(self.snapshot, entry_id=forged)
		self.assertEqual(raised.exception.reason, "entry_changed")

	def test_multiline_traceback_is_context_in_one_piece(self):
		anchor = next(record for record in self.records if record.exception and record.exception["type"] == "KeyError")
		result = self.context(anchor, before=1, after=1, include_raw=True)
		entry = result["entries"][1]
		self.assertIn("Traceback (most recent call last):", entry["raw"])
		self.assertIn("KeyError", entry["raw"])
		self.assertEqual(entry["line_end"], entry["line_start"] + 5)
		self.assertEqual(entry["exception"]["type"], "KeyError")

	def test_time_window_around_foreground_event(self):
		anchor = next(record for record in self.records if "Foreground took too long" in record.message)
		result = self.context(anchor, before_ms=1500, after_ms=1500)
		self.assertIn("foreground", result["anchor"]["message"].lower())
		timestamps = [entry["timestamp_ms"] for entry in result["entries"] if entry["timestamp_ms"] is not None]
		self.assertEqual(timestamps, sorted(timestamps))
		self.assertTrue(any("Time windows use time-of-day" in note for note in result["diagnostics"]["limitations"]))

	def test_after_context_stops_after_requested_count(self):
		anchor = self.records[0]
		result = self.context(anchor, before=0, after=1)
		self.assertEqual(result["returned"], 2)
		self.assertEqual(result["after"], 1)
		self.assertTrue(result["complete_after"] is False)


if __name__ == "__main__":
	_ = unittest.main()
