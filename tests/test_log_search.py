# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Search, filtering, cursor and generation tests for native log diagnostics."""

from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _log_support import FIXTURE, log_diagnostics  # noqa: E402

from nvdaMcpBridge.server.log_diagnostics import filters as log_filters  # noqa: E402
from nvdaMcpBridge.server.log_diagnostics import search as log_search  # noqa: E402


class NativeLogSearchTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.snapshot = log_diagnostics.snapshot_log(FIXTURE)

	def search(self, **kwargs):
		include = kwargs.pop("include", None)
		exclude = kwargs.pop("exclude", None)
		addon = kwargs.pop("addon", None)
		contains = kwargs.pop("contains", None)
		regex = kwargs.pop("regex", None)
		case_sensitive = kwargs.pop("case_sensitive", False)
		threads = kwargs.pop("threads", None)
		trace_id = kwargs.pop("trace_id", None)
		last_seconds = kwargs.pop("last_seconds", None)
		start = kwargs.pop("start", None)
		end = kwargs.pop("end", None)
		now = kwargs.pop("now", None)
		filters = log_diagnostics.compile_filter(
			levels=kwargs.pop("levels", None),
			min_level=kwargs.pop("min_level", None),
			source_include=include,
			source_exclude=exclude,
			addon=addon,
			message_contains=contains,
			message_regex=regex,
			case_sensitive=case_sensitive,
			threads=threads,
			trace_id=trace_id,
			last_seconds=last_seconds,
			start=start,
			end=end,
			now=now,
		)
		return log_diagnostics.search_logs(self.snapshot, filters, **kwargs)

	def test_exact_and_multiple_level_filters(self):
		one = self.search(levels=["ERROR"], limit=10)
		self.assertEqual([entry["level"] for entry in one["entries"]], ["ERROR"])
		many = self.search(levels=["ERROR", "CRITICAL", "DEBUGWARNING"], limit=20)
		self.assertEqual(
			{entry["level"] for entry in many["entries"]},
			{"ERROR", "CRITICAL", "DEBUGWARNING"},
		)

	def test_min_level_uses_documented_ordering(self):
		result = self.search(min_level="WARNING", limit=50)
		self.assertEqual({entry["level"] for entry in result["entries"]}, {"WARNING", "ERROR", "CRITICAL"})
		self.assertNotIn("DEBUGWARNING", {entry["level"] for entry in result["entries"]})

	def test_source_prefix_glob_and_exclusion(self):
		prefix = self.search(include=["speech.*"], limit=20)
		self.assertTrue(prefix["entries"])
		self.assertTrue(all(entry["logger"].startswith("speech.") for entry in prefix["entries"]))
		globbed = self.search(include=["external:globalPlugins.browser*"], limit=20)
		self.assertTrue(all("globalPlugins.browser" in entry["logger"] for entry in globbed["entries"]))
		without_com = self.search(min_level="DEBUG", exclude=["comtypes.*"], limit=200)
		self.assertTrue(without_com["entries"])
		self.assertFalse(any(entry["logger"].startswith("comtypes") for entry in without_com["entries"]))

	def test_addon_filter(self):
		result = self.search(addon="browserNav", limit=20)
		self.assertTrue(result["entries"])
		self.assertTrue(all(entry["addon"] == "browserNav" for entry in result["entries"]))
		self.assertEqual(self.search(addon="doesNotExist", limit=20)["entries"], [])

	def test_case_sensitive_and_insensitive_substring(self):
		insensitive = self.search(contains="foreground", limit=20)
		exact = self.search(contains="foreground", case_sensitive=True, limit=20)
		self.assertTrue(insensitive["entries"])
		self.assertEqual(exact["entries"], [])
		self.assertTrue(self.search(contains="Foreground", case_sensitive=True, limit=20)["entries"])

	def test_regex_and_invalid_regex(self):
		result = self.search(regex=r"Foreground still \d+", limit=20)
		self.assertGreaterEqual(len(result["entries"]), 2)
		with self.assertRaises(log_filters.InvalidFilterError):
			self.search(regex="[")
		with self.assertRaises(log_filters.InvalidFilterError):
			self.search(regex="(a+)+$")

	def test_thread_filter_by_name_and_id(self):
		by_name = self.search(threads=["Dummy-88"], limit=20)
		self.assertEqual([entry["thread"]["id"] for entry in by_name["entries"]], [15972])
		by_id = self.search(threads=["16976"], limit=20)
		self.assertEqual(by_id["entries"][0]["thread"]["name"], "_checkForUpdatableAddons")

	def test_trace_id_filter_is_explicit_substring_evidence(self):
		self.assertEqual(self.search(trace_id="at-doesnotexist", limit=10)["entries"], [])
		with tempfile.TemporaryDirectory() as directory:
			path = os.path.join(directory, "trace.log")
			with open(path, "w", encoding="utf-8") as handle:
				handle.write(
					"INFO - external:globalPlugins.nvdaMcp-bridge.server.http_bridge (00:00:01.000) - Worker (7):\n"
					"bridge.action.success action=press trace_id=at-abc123\n"
				)
			snapshot = log_diagnostics.snapshot_log(path)
			entry = log_diagnostics.search_logs(
				snapshot,
				log_diagnostics.compile_filter(trace_id="at-abc123"),
			)["entries"][0]
			self.assertEqual(entry["enrichment"]["bridge"]["trace_id"], "at-abc123")

	def test_time_of_day_range_and_midnight_wrap(self):
		result = self.search(start="23:59:00", end="00:00:02", limit=20)
		timestamps = [entry["timestamp"] for entry in result["entries"]]
		self.assertIn("23:59:59.900", timestamps)
		self.assertIn("00:00:01.100", timestamps)
		self.assertEqual(result["diagnostics"]["time_filter_precision"], "time_of_day")
		self.assertTrue(any("time-of-day" in note for note in result["diagnostics"]["limitations"]))

	def test_relative_time_window_wraps_midnight(self):
		result = self.search(last_seconds=10, now=datetime(1970, 1, 1, 0, 0, 5), limit=20)
		timestamps = [entry["timestamp"] for entry in result["entries"]]
		self.assertIn("23:59:59.900", timestamps)
		self.assertIn("00:00:01.100", timestamps)

	def test_empty_result_is_a_success_not_an_error(self):
		result = self.search(contains="never-present-in-this-log", limit=10)
		self.assertEqual(result["entries"], [])
		self.assertEqual(result["page"]["returned"], 0)
		self.assertFalse(result["page"]["has_more"])
		self.assertEqual(result["diagnostics"]["matched_records"], 0)

	def test_pagination_oldest_first_has_no_gaps_or_duplicates(self):
		seen = []
		cursor = None
		for _ in range(20):
			result = self.search(levels=["DEBUGWARNING", "ERROR"], limit=2, order="oldest_first", cursor=cursor)
			seen.extend(entry["entry_id"] for entry in result["entries"])
			cursor = result["page"]["next_cursor"]
			if not cursor:
				break
		self.assertEqual(len(seen), len(set(seen)))
		self.assertGreaterEqual(len(seen), 3)
		self.assertEqual(seen, sorted(seen, key=lambda value: int(value.split(":")[1], 16)))

	def test_pagination_newest_first_has_no_gaps_or_duplicates(self):
		seen = []
		cursor = None
		for _ in range(20):
			result = self.search(levels=["DEBUGWARNING", "ERROR"], limit=2, order="newest_first", cursor=cursor)
			seen.extend(entry["entry_id"] for entry in result["entries"])
			cursor = result["page"]["next_cursor"]
			if not cursor:
				break
		self.assertEqual(len(seen), len(set(seen)))
		offsets = [int(value.split(":")[1], 16) for value in seen]
		self.assertEqual(offsets, sorted(offsets, reverse=True))
		self.assertGreaterEqual(len(seen), 3)

	def test_cursor_rejects_different_filters_and_order(self):
		first = self.search(levels=["WARNING"], limit=1)
		cursor = first["page"]["next_cursor"]
		with self.assertRaises(log_filters.InvalidFilterError):
			self.search(levels=["ERROR"], limit=1, cursor=cursor)
		with self.assertRaises(log_filters.InvalidFilterError):
			self.search(levels=["WARNING"], limit=1, order="oldest_first", cursor=cursor)

	def test_cursor_expires(self):
		with mock.patch.object(log_search.time, "time", return_value=1_000_000):
			first = self.search(levels=["DEBUGWARNING"], limit=1)
			cursor = first["page"]["next_cursor"]
		self.assertIsNotNone(cursor)
		with mock.patch.object(log_search.time, "time", return_value=1_000_000 + 31 * 60):
			with self.assertRaises(log_diagnostics.StaleLogError) as raised:
				self.search(levels=["DEBUGWARNING"], limit=1, cursor=cursor)
		self.assertEqual(raised.exception.reason, "cursor_expired")

	def test_file_growth_does_not_duplicate_an_existing_cursor(self):
		with tempfile.TemporaryDirectory() as directory:
			path = os.path.join(directory, "grow.log")
			shutil.copyfile(FIXTURE, path)
			snapshot = log_diagnostics.snapshot_log(path)
			first = log_diagnostics.search_logs(
				snapshot,
				log_diagnostics.compile_filter(levels=["DEBUGWARNING"]),
				limit=1,
				order="oldest_first",
			)
			with open(path, "a", encoding="utf-8") as handle:
				handle.write(
					"DEBUGWARNING - IAccessibleHandler.internalWinEventHandler._shouldGetEvents (00:10:00.000) - MainThread (35940):\n"
					"New warning\n"
				)
			new_snapshot = log_diagnostics.snapshot_log(path)
			self.assertEqual(snapshot.generation, new_snapshot.generation)
			second = log_diagnostics.search_logs(
				new_snapshot,
				log_diagnostics.compile_filter(levels=["DEBUGWARNING"]),
				limit=1,
				order="oldest_first",
				cursor=first["page"]["next_cursor"],
			)
			self.assertNotEqual(first["entries"][0]["entry_id"], second["entries"][0]["entry_id"])

	def test_file_replacement_makes_a_cursor_stale(self):
		with tempfile.TemporaryDirectory() as directory:
			path = os.path.join(directory, "replace.log")
			shutil.copyfile(FIXTURE, path)
			snapshot = log_diagnostics.snapshot_log(path)
			first = log_diagnostics.search_logs(
				snapshot,
				log_diagnostics.compile_filter(levels=["DEBUGWARNING"]),
				limit=1,
				order="oldest_first",
			)
			replacement = os.path.join(directory, "replacement.log")
			shutil.copyfile(FIXTURE, replacement)
			os.replace(replacement, path)
			new_snapshot = log_diagnostics.snapshot_log(path)
			with self.assertRaises(log_diagnostics.StaleLogError):
				log_diagnostics.search_logs(
					new_snapshot,
					log_diagnostics.compile_filter(levels=["DEBUGWARNING"]),
					limit=1,
					order="oldest_first",
					cursor=first["page"]["next_cursor"],
				)

	def test_file_truncation_makes_a_cursor_stale(self):
		with tempfile.TemporaryDirectory() as directory:
			path = os.path.join(directory, "truncate.log")
			shutil.copyfile(FIXTURE, path)
			snapshot = log_diagnostics.snapshot_log(path)
			first = log_diagnostics.search_logs(
				snapshot,
				log_diagnostics.compile_filter(levels=["DEBUGWARNING"]),
				limit=1,
				order="oldest_first",
			)
			with open(path, "r+b") as handle:
				handle.truncate(200)
			new_snapshot = log_diagnostics.snapshot_log(path)
			self.assertEqual(snapshot.generation, new_snapshot.generation)
			with self.assertRaises(log_diagnostics.StaleLogError) as raised:
				log_diagnostics.search_logs(
					new_snapshot,
					log_diagnostics.compile_filter(levels=["DEBUGWARNING"]),
					limit=1,
					order="oldest_first",
					cursor=first["page"]["next_cursor"],
				)
			self.assertEqual(raised.exception.reason, "file_truncated")

	def test_scan_budget_is_reported_as_incomplete(self):
		with tempfile.TemporaryDirectory() as directory:
			path = os.path.join(directory, "many.log")
			with open(path, "w", encoding="utf-8") as handle:
				for index in range(1200):
					handle.write(
						f"INFO - core.main (00:00:{index % 60:02d}.000) - MainThread (1):\nmessage {index}\n"
					)
			snapshot = log_diagnostics.snapshot_log(path)
			result = log_diagnostics.search_logs(
				snapshot,
				log_diagnostics.compile_filter(),
				limit=10,
				max_scan_records=1000,
			)
			self.assertTrue(result["diagnostics"]["scan_budget_exhausted"])
			self.assertTrue(result["diagnostics"]["truncated"])

	def test_multiple_clients_share_no_cursor_state(self):
		first = self.search(levels=["DEBUGWARNING"], limit=1)
		second = self.search(levels=["DEBUGWARNING"], limit=1)
		self.assertEqual(first["entries"][0]["entry_id"], second["entries"][0]["entry_id"])
		page_a = self.search(levels=["DEBUGWARNING"], limit=1, cursor=first["page"]["next_cursor"])
		page_b = self.search(levels=["DEBUGWARNING"], limit=1, cursor=second["page"]["next_cursor"])
		self.assertEqual(page_a["entries"][0]["entry_id"], page_b["entries"][0]["entry_id"])


class FilterValidationTests(unittest.TestCase):
	def test_source_glob_is_not_a_regex(self):
		self.assertTrue(log_filters._source_matches("speech.*", "speech.speech.speak"))
		self.assertFalse(log_filters._source_matches("speech.*", "speechless.module"))

	def test_invalid_time_range_is_rejected(self):
		with self.assertRaises(log_filters.InvalidFilterError):
			log_diagnostics.compile_filter(
				start="2026-01-02T10:00:00",
				end="2026-01-01T09:00:00",
				now=datetime(1970, 1, 1, 0, 0, 1),
			)

	def test_date_and_time_only_endpoints_cannot_be_mixed(self):
		with self.assertRaises(log_filters.InvalidFilterError):
			log_diagnostics.compile_filter(start="2026-01-01T10:00:00", end="12:00:00")

	def test_last_seconds_cannot_combine_with_start(self):
		with self.assertRaises(log_filters.InvalidFilterError):
			log_diagnostics.compile_filter(last_seconds=10, start="00:00:00")

	def test_cursor_rejects_tampering(self):
		raw = base64.urlsafe_b64encode(json.dumps({"v": 1, "g": "x"}).encode()).decode()
		with self.assertRaises(log_filters.InvalidFilterError):
			log_diagnostics.decode_cursor(raw)


if __name__ == "__main__":
	_ = unittest.main()
