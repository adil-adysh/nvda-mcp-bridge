# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Deterministic summary tests for native NVDA log diagnostics."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _log_support import FIXTURE, log_diagnostics  # noqa: E402

from nvdaMcpBridge.server.log_diagnostics import filters as log_filters  # noqa: E402


class LogSummaryTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.snapshot = log_diagnostics.snapshot_log(FIXTURE)
		cls.result = log_diagnostics.summarize_logs(cls.snapshot, log_diagnostics.compile_filter(), limit=20)
		cls.summary = cls.result["summary"]

	def test_severity_distribution_counts_every_level(self):
		counts = self.summary["severity_counts"]
		self.assertEqual(self.summary["total_records"], 20)
		self.assertEqual(counts["DEBUGWARNING"], 2)
		self.assertEqual(counts["IO"], 1)
		self.assertEqual(counts["TRACEBACK"], 1)
		self.assertEqual(sum(counts.values()), 20)

	def test_top_sources_are_sorted_and_carry_shares(self):
		rows = self.summary["top_sources"]
		self.assertEqual(rows[0]["count"], 2)
		self.assertEqual(rows[0]["share"], 0.1)
		self.assertEqual([row["count"] for row in rows[:3]], [2, 2, 2])
		self.assertEqual(
			rows,
			sorted(rows, key=lambda row: (-row["count"], row["source"])),
		)

	def test_volatile_com_pointers_are_normalized_and_preserve_examples(self):
		pattern = next(row for row in self.summary["repeated_patterns"] if row["pattern"].startswith("GetBestInterface"))
		self.assertEqual(pattern["count"], 2)
		self.assertIn("GetBestInterface", pattern["example"])
		self.assertIn("0x", pattern["example"])
		self.assertEqual(len(pattern["entry_ids"]), 2)

	def test_distinct_errors_are_not_merged(self):
		with tempfile.TemporaryDirectory() as directory:
			path = os.path.join(directory, "distinct.log")
			with open(path, "w", encoding="utf-8") as handle:
				handle.write(
					"ERROR - example.one (00:00:01.000) - MainThread (1):\nFailed to open document 12345\n"
					"ERROR - example.one (00:00:02.000) - MainThread (1):\nFailed to save document 12345\n"
				)
			snapshot = log_diagnostics.snapshot_log(path)
			summary = log_diagnostics.summarize_logs(snapshot, log_diagnostics.compile_filter())["summary"]
		patterns = {row["pattern"] for row in summary["repeated_patterns"]}
		self.assertEqual(patterns, {"Failed to open document <number>", "Failed to save document <number>"})

	def test_exception_groups_carry_type_source_top_frame_and_parse_status(self):
		rows = self.summary["exceptions"]
		types = {row["type"] for row in rows}
		self.assertIn("KeyError", types)
		self.assertIn("AttributeError", types)
		key_error = next(row for row in rows if row["type"] == "KeyError")
		self.assertEqual(key_error["source"], "addonStore")
		self.assertIn("fromDict", key_error["top_frame"])
		self.assertEqual(key_error["parse_status"], "complete")

	def test_warning_groups_include_debugwarning_and_normalize_window_handles(self):
		rows = self.summary["warnings"]
		foreground = [row for row in rows if "Foreground" in row["pattern"]]
		self.assertEqual(sum(row["count"] for row in foreground), 2)
		self.assertTrue(all("<number>" in row["pattern"] for row in foreground))
		self.assertEqual({row["level"] for row in foreground}, {"DEBUGWARNING"})

	def test_noise_is_reported_not_removed(self):
		noise = self.summary["noise"]
		self.assertGreater(noise["com_records"], 0)
		self.assertGreater(noise["com_share"], 0)
		self.assertTrue(any("not by itself an error" in noise["note"] for _ in [0]))
		self.assertIn("DEBUG", self.summary["severity_counts"])

	def test_time_buckets_are_deterministic_and_mark_candidates(self):
		buckets = self.summary["time_buckets"]
		self.assertTrue(buckets)
		self.assertTrue(all("start" in bucket and "count" in bucket for bucket in buckets))
		self.assertEqual(
			buckets,
			log_diagnostics.summarize_logs(self.snapshot, log_diagnostics.compile_filter(), limit=20)["summary"][
				"time_buckets"
			],
		)

	def test_empty_log_is_a_complete_empty_summary(self):
		with tempfile.TemporaryDirectory() as directory:
			path = os.path.join(directory, "empty.log")
			open(path, "w", encoding="utf-8").close()
			snapshot = log_diagnostics.snapshot_log(path)
			result = log_diagnostics.summarize_logs(snapshot, log_diagnostics.compile_filter())
		self.assertEqual(result["summary"]["total_records"], 0)
		self.assertEqual(result["summary"]["severity_counts"], {})
		self.assertTrue(result["coverage"]["complete"])

	def test_incomplete_scan_is_reported(self):
		with tempfile.TemporaryDirectory() as directory:
			path = os.path.join(directory, "many.log")
			with open(path, "w", encoding="utf-8") as handle:
				for index in range(1200):
					handle.write(f"INFO - core.main (00:00:01.000) - MainThread (1):\nmessage {index}\n")
			snapshot = log_diagnostics.snapshot_log(path)
			result = log_diagnostics.summarize_logs(
				snapshot,
				log_diagnostics.compile_filter(),
				max_scan_records=1000,
			)
		self.assertFalse(result["coverage"]["complete"])
		self.assertLessEqual(result["coverage"]["records_examined"], 1000)
		self.assertTrue(any("lower bounds" in note for note in result["coverage"]["limitations"]))

	def test_time_budget_is_reported_not_hidden(self):
		from unittest import mock

		from nvdaMcpBridge.server.log_diagnostics import summary as summary_module

		with mock.patch.object(summary_module, "MAX_SCAN_SECONDS", -1.0):
			result = log_diagnostics.summarize_logs(self.snapshot, log_diagnostics.compile_filter())
		self.assertFalse(result["coverage"]["complete"])
		self.assertTrue(result["coverage"]["time_budget_exhausted"])
		self.assertTrue(any("summary budget" in note for note in result["coverage"]["limitations"]))

	def test_deterministic_ordering_repeats(self):
		first = log_diagnostics.summarize_logs(self.snapshot, log_diagnostics.compile_filter(), limit=10)
		second = log_diagnostics.summarize_logs(self.snapshot, log_diagnostics.compile_filter(), limit=10)
		self.assertEqual(first["summary"], second["summary"])

	def test_invalid_group_and_include_values_are_caller_errors(self):
		with self.assertRaises(log_filters.InvalidFilterError):
			log_diagnostics.summarize_logs(self.snapshot, log_diagnostics.compile_filter(), group_by=["everything"])
		with self.assertRaises(log_filters.InvalidFilterError):
			log_diagnostics.summarize_logs(self.snapshot, log_diagnostics.compile_filter(), include=["magic"])

	def test_section_selection_omits_unrequested_sections(self):
		result = log_diagnostics.summarize_logs(
			self.snapshot,
			log_diagnostics.compile_filter(),
			include=["counts"],
		)
		self.assertIn("severity_counts", result["summary"])
		self.assertNotIn("exceptions", result["summary"])
		self.assertNotIn("repeated_patterns", result["summary"])


if __name__ == "__main__":
	_ = unittest.main()
