# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Registry, handler and backward-compatibility tests for log diagnostic tools."""

from __future__ import annotations

import importlib
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _log_support import FIXTURE, PACKAGE_NAME  # noqa: E402

if "globalVars" not in sys.modules:
	globalVars = types.ModuleType("globalVars")
	globalVars.appArgs = types.SimpleNamespace(logFileName=FIXTURE)
	sys.modules["globalVars"] = globalVars
else:
	sys.modules["globalVars"].appArgs = types.SimpleNamespace(logFileName=FIXTURE)

models = importlib.import_module(f"{PACKAGE_NAME}.tools.models")
handlers = importlib.import_module(f"{PACKAGE_NAME}.tools.handlers")
tools_registry = importlib.import_module(f"{PACKAGE_NAME}.tools.registry").NVDA_TOOLS_REGISTRY
executor_module = importlib.import_module(f"{PACKAGE_NAME}.core.executor")


class LogToolRegistryTests(unittest.TestCase):
	def test_new_tools_are_registered_as_coordinator_diagnostics(self):
		for name in ("search_logs", "get_log_context", "summarize_logs"):
			metadata = tools_registry.get(name, "1.0.0").get_metadata()
			self.assertEqual(metadata.category, "diagnostics")
			self.assertEqual(metadata.required_permission, "diagnostics")
			self.assertTrue(metadata.read_only)
			self.assertFalse(metadata.side_effects)
			self.assertEqual(metadata.execution_mode.value, "coordinator")

	def test_search_schema_exposes_filters_and_bounds(self):
		schema = tools_registry.get_input_schema("search_logs", "1.0.0").to_json_schema()
		properties = schema["properties"]
		for name in ("levels", "sources", "addon", "message", "time", "threads", "trace_id", "cursor", "limit"):
			self.assertIn(name, properties)
		self.assertIn("include", properties["sources"]["properties"])
		self.assertIn("regex", properties["message"]["properties"])
		self.assertIn("last_seconds", properties["time"]["properties"])

	def test_legacy_get_logs_contract_is_unchanged(self):
		metadata = tools_registry.get("get_logs", "1.0.0").get_metadata()
		schema = tools_registry.get_input_schema("get_logs", "1.0.0").to_json_schema()
		self.assertTrue(metadata.read_only)
		self.assertEqual(metadata.required_permission, "diagnostics")
		self.assertEqual(
			set(schema["properties"]),
			{"max_lines", "filter_level", "use_tail"},
		)
		self.assertEqual(schema["properties"]["filter_level"]["anyOf"][0]["enum"], [
			"DEBUG",
			"INFO",
			"WARNING",
			"ERROR",
			"CRITICAL",
		])

	def test_new_output_types_are_published(self):
		registry = importlib.import_module(f"{PACKAGE_NAME}.validation.output_type_registry")
		types_registry = registry.build_output_type_registry()
		for output_type in ("LogSearchResult", "LogContextResult", "LogSummaryResult"):
			self.assertIn(output_type, types_registry)


class LogToolHandlerTests(unittest.TestCase):
	def setUp(self):
		self.executor = executor_module.ToolExecutor()

	def execute(self, name, payload):
		tool = tools_registry.get(name, "1.0.0")
		return self.executor.execute_and_serialize(tool, payload)

	def test_search_logs_success_payload_validates_and_serializes(self):
		result = self.execute("search_logs", {"levels": ["DEBUGWARNING"], "limit": 1})
		self.assertTrue(result["success"])
		data = result["output"]
		self.assertEqual(data["page"]["returned"], 1)
		self.assertEqual(data["entries"][0]["level"], "DEBUGWARNING")
		self.assertEqual(data["source"]["source"], "nvda_native_log")
		self.assertIn("generation", data["source"])
		self.assertIn("time_filter_precision", data["diagnostics"])
		self.assertIsNone(data["entries"][0]["raw"])

	def test_search_logs_include_raw_is_opt_in(self):
		result = self.execute(
			"search_logs",
			{"message": {"contains": "Foreground took too long"}, "limit": 1, "include_raw": True},
		)
		self.assertTrue(result["success"])
		self.assertIn("Foreground took too long", result["output"]["entries"][0]["raw"])

	def test_invalid_regex_is_a_structured_input_error(self):
		result = self.execute("search_logs", {"message": {"regex": "["}})
		self.assertFalse(result["success"])
		self.assertEqual(result["error"]["error_code"], "invalid_input_type")
		self.assertIn("message.regex", result["error"]["message"])

	def test_context_tool_uses_the_entry_id_from_search(self):
		found = self.execute(
			"search_logs",
			{"message": {"contains": "BrowserNav focus handler"}, "limit": 1},
		)
		entry_id = found["output"]["entries"][0]["entry_id"]
		result = self.execute("get_log_context", {"entry_id": entry_id, "before": 1, "after": 1})
		self.assertTrue(result["success"])
		self.assertEqual(result["output"]["entries"][1]["entry_id"], entry_id)
		self.assertEqual(result["output"]["returned"], 3)

	def test_summary_tool_returns_deterministic_sections(self):
		payload = {
			"include": ["counts", "exceptions", "warnings", "noise"],
			"limit": 10,
		}
		first = self.execute("summarize_logs", payload)
		second = self.execute("summarize_logs", payload)
		self.assertTrue(first["success"])
		self.assertEqual(first["output"]["summary"], second["output"]["summary"])
		self.assertIn("severity_counts", first["output"]["summary"])
		self.assertIn("exceptions", first["output"]["summary"])
		self.assertIn("parse_failures", first["output"]["coverage"])

	def test_search_tool_rejects_unknown_fields(self):
		result = self.execute("search_logs", {"unexpected": True})
		self.assertFalse(result["success"])
		self.assertEqual(result["error"]["error_code"], "invalid_input_schema")


if __name__ == "__main__":
	_ = unittest.main()
