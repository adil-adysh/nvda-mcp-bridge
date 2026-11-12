# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Parser tests for native NVDA log records and embedded tracebacks."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _log_support import FIXTURE, log_diagnostics  # noqa: E402


class RedactionTests(unittest.TestCase):
	def test_known_token_and_credential_assignment_are_redacted(self):
		from nvdaMcpBridge.server.log_diagnostics import redaction

		redaction.register_secret("bridge-secret-123")
		with tempfile.TemporaryDirectory() as directory:
			path = os.path.join(directory, "secret.log")
			with open(path, "w", encoding="utf-8") as handle:
				handle.write(
					"INFO - core.main (00:00:01.000) - MainThread (1):\n"
					"started with bridge-secret-123 and token=plain-secret\n"
				)
			record = next(log_diagnostics.LogReader(log_diagnostics.snapshot_log(path)).iter_records())
			public = record.public(include_raw=True)
		self.assertNotIn("bridge-secret-123", public["message"])
		self.assertNotIn("plain-secret", public["message"])
		self.assertIn(redaction.REDACTED, public["message"])
		self.assertNotIn("bridge-secret-123", public["raw"])


class NativeLogParserTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.snapshot = log_diagnostics.snapshot_log(FIXTURE)
		cls.records = list(log_diagnostics.LogReader(cls.snapshot).iter_records())

	def find(self, predicate):
		return next(record for record in self.records if predicate(record))

	def test_multiline_records_are_single_logical_entries(self):
		foreground = self.find(lambda record: "Foreground took too long" in record.message)
		self.assertEqual(foreground.parse_status, "parsed")
		self.assertEqual(foreground.level, "DEBUGWARNING")
		self.assertEqual(foreground.thread_name, "MainThread")
		self.assertEqual(foreground.thread_id, 35940)
		self.assertEqual(foreground.line_end, foreground.line_start + 1)

		traceback = self.find(lambda record: record.exception and record.exception["type"] == "KeyError")
		self.assertEqual(traceback.line_end, traceback.line_start + 5)
		self.assertIn("Traceback (most recent call last):", traceback.raw)
		self.assertIn("Malformed add-on scan results", traceback.message)

	def test_debugwarning_and_io_are_preserved_not_folded(self):
		levels = {record.level for record in self.records}
		self.assertIn("DEBUGWARNING", levels)
		self.assertIn("IO", levels)
		io_record = self.find(lambda record: record.level == "IO")
		self.assertEqual(io_record.logger, "speech.speech.speak")
		self.assertEqual(io_record.subsystem, "speech")

	def test_unknown_level_is_preserved_with_a_derived_note(self):
		unknown = self.find(lambda record: record.level == "TRACEBACK")
		self.assertEqual(unknown.parse_status, "parsed")
		self.assertIn("unknown_level_preserved", unknown.parse_notes)
		self.assertEqual(unknown.logger, "globalPlugins.example")

	def test_unicode_is_preserved(self):
		unicode_record = self.find(lambda record: "café" in record.message)
		self.assertIn("日本語", unicode_record.message)
		self.assertIn("Ελληνικά", self.find(lambda record: "Ελληνικά" in record.message).message)

	def test_empty_message_is_not_dropped(self):
		empty = self.find(lambda record: record.timestamp == "23:59:59.900")
		self.assertEqual(empty.message.strip(), "")
		self.assertEqual(empty.parse_status, "parsed")

	def test_malformed_header_becomes_a_malformed_record(self):
		malformed = self.find(lambda record: record.parse_status == "malformed")
		self.assertIn("broken header without timestamp", malformed.raw)
		self.assertIn("malformed header message kept", malformed.raw)
		self.assertEqual(malformed.logger, "")
		self.assertIn("header_not_parsed", malformed.parse_notes)

	def test_parsed_fields_and_derived_fields_are_distinguished(self):
		foreground = self.find(lambda record: "Foreground took too long" in record.message)
		self.assertIn("module", foreground.derived_fields)
		self.assertEqual(foreground.module, "IAccessibleHandler")
		self.assertEqual(foreground.function, "_shouldGetEvents")
		self.assertEqual(foreground.timestamp_date_source, "unknown")
		self.assertIsNone(foreground.timestamp_date)
		self.assertIsNone(foreground.timestamp_absolute)
		self.assertEqual(foreground.timestamp_timezone, "unknown")

	def test_midnight_rollover_is_visible_in_millisecond_values(self):
		before = self.find(lambda record: record.timestamp == "23:59:59.900")
		after = self.find(lambda record: record.timestamp == "00:00:01.100")
		self.assertGreater(before.timestamp_ms, after.timestamp_ms)
		self.assertEqual(after.timestamp_ms, 1100)

	def test_foreground_facts_are_extracted(self):
		foreground = self.find(lambda record: "Foreground took too long" in record.message)
		self.assertEqual(foreground.enrichment["focus"]["still_handle"], 67678)
		self.assertEqual(foreground.enrichment["focus"]["expected_class"], "XamlExplorerHostIslandWindow")
		self.assertEqual(foreground.subsystem, "focus")

	def test_addon_identity_uses_explicit_nvda_module_naming(self):
		record = self.find(lambda record: record.logger.startswith("external:globalPlugins.browserNav"))
		self.assertEqual(record.addon, "browserNav")
		app_module = self.find(lambda record: record.logger.startswith("external:appModules"))
		self.assertEqual(app_module.addon, "notepad")
		core = self.find(lambda record: record.logger == "core.main")
		self.assertIsNone(core.addon)

	def test_comments_are_not_misclassified_as_exceptions(self):
		com = self.find(lambda record: record.logger.startswith("comtypes"))
		self.assertIsNone(com.exception)
		self.assertEqual(com.subsystem, "com")

	def test_partial_traceback_is_marked_partial_and_keeps_raw_text(self):
		content = (
			"INFO - core.main (02:00:00.000) - MainThread (1):\n"
			"Something failed\n"
			"Traceback (most recent call last):\n"
			'  File "core.py", line 1, in run\n'
			"    raise ValueError('unfinished')\n"
		)
		with tempfile.TemporaryDirectory() as directory:
			path = os.path.join(directory, "partial.log")
			with open(path, "w", encoding="utf-8") as handle:
				handle.write(content)
			record = next(log_diagnostics.LogReader(log_diagnostics.snapshot_log(path)).iter_records())
		self.assertIsNotNone(record.exception)
		self.assertEqual(record.exception["parse_status"], "partial")
		self.assertEqual(record.exception["frames"][0]["function"], "run")
		self.assertIn("Traceback", record.exception["raw_traceback"])
		self.assertIn("unfinished", record.raw)

	def test_chained_tracebacks_preserve_primary_and_chain(self):
		record = self.find(lambda record: record.exception and record.exception["type"] == "AttributeError")
		exception = record.exception
		self.assertEqual(exception["parse_status"], "complete")
		self.assertEqual(exception["type"], "AttributeError")
		self.assertEqual(exception["message"], "Example error")
		self.assertEqual(exception["frames"][0]["function"], "event_gainFocus")
		self.assertEqual(len(exception["chain"]), 1)
		self.assertEqual(exception["chain"][0]["type"], "ZeroDivisionError")
		self.assertEqual(exception["chain"][0]["relation"], "direct_cause")

	def test_entry_id_addresses_the_exact_byte_offset_and_survives_append(self):
		record = self.records[4]
		generation, offset = log_diagnostics.parse_entry_id(record.entry_id)
		self.assertEqual(generation, self.snapshot.generation)
		self.assertEqual(offset, record.byte_start)
		self.assertIsNotNone(log_diagnostics.parse_entry_hash(record.entry_id))
		with tempfile.TemporaryDirectory() as directory:
			copy = os.path.join(directory, "growing.log")
			with open(FIXTURE, "rb") as source, open(copy, "wb") as target:
				target.write(source.read())
			original = log_diagnostics.snapshot_log(copy)
			original_record = list(log_diagnostics.LogReader(original).iter_records())[4]
			with open(copy, "a", encoding="utf-8") as handle:
				handle.write("INFO - core.main (02:00:00.000) - MainThread (1):\nappended\n")
			refreshed = log_diagnostics.snapshot_log(copy)
			self.assertEqual(original.generation, refreshed.generation)
			found = next(
				item
				for item in log_diagnostics.LogReader(refreshed).iter_records()
				if item.entry_id == original_record.entry_id
			)
			self.assertEqual(found.raw, original_record.raw)


if __name__ == "__main__":
	_ = unittest.main()
