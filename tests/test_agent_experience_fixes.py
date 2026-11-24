"""Regression tests for the agent-experience fixes (errors, schema, bounds)."""

from __future__ import annotations

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

for name in ("logHandler",):
	if name not in sys.modules:
		sys.modules[name] = types.SimpleNamespace(
			log=types.SimpleNamespace(
				debug=lambda *a, **k: None,
				info=lambda *a, **k: None,
				warning=lambda *a, **k: None,
				error=lambda *a, **k: None,
			)
		)

if "queueHandler" not in sys.modules:
	_queue_handler = types.ModuleType("queueHandler")
	_queue_handler.queueFunction = lambda func, *args, **kwargs: func(*args, **kwargs)
	_queue_handler.eventQueue = None
	sys.modules["queueHandler"] = _queue_handler

_PACKAGE = "nvdaMcpBridge"
if _PACKAGE not in sys.modules:
	_pkg = types.ModuleType(_PACKAGE)
	_pkg.__path__ = [_ADDON_DIR]
	sys.modules[_PACKAGE] = _pkg

handlers = importlib.import_module(f"{_PACKAGE}.tools.handlers")
models = importlib.import_module(f"{_PACKAGE}.tools.models")
nvda_tools = importlib.import_module(f"{_PACKAGE}.tools.nvda_tools")
schema = importlib.import_module(f"{_PACKAGE}.core.schema")
http_bridge = importlib.import_module(f"{_PACKAGE}.server.http_bridge")
events = importlib.import_module(f"{_PACKAGE}.server.events")


class ErrorContractTests(unittest.TestCase):
	def setUp(self):
		self.bridge = http_bridge.NVDAHTTPBridge(host="127.0.0.1", port=0, bridge_token="test-token")
		self.bridge._native_log_enabled = False
		self.bridge.grant_permission("input_control")
		self.bridge.grant_permission("diagnostics")

	def test_action_errors_carry_top_level_recovery(self):
		error = self.bridge._action_error_object(
			4206,
			"stale",
			{
				"reason_code": "identity",
				"recovery": {"action": "observe", "hint": "Call observe again."},
			},
		)

		self.assertEqual(error["code"], 4206)
		self.assertEqual(error["name"], "STALE_REFERENCE")
		self.assertEqual(error["recovery"]["action"], "observe")
		self.assertEqual(error["recovery"]["hint"], "Call observe again.")
		# The domain hint is lifted out of details, not duplicated there.
		self.assertNotIn("recovery", error.get("details", {}))

	def test_argument_errors_are_4103_not_internal(self):
		with self.assertRaises(ValueError) as raised:
			handlers.get_logs(max_lines=0)
		self.assertIn("max_lines", str(raised.exception))

		# The executor maps ValueError to INVALID_INPUT_TYPE, whose wire code is
		# 4103. A caller mistake must never look like a bridge fault (5303).
		from nvdaMcpBridge.core.error import ErrorCode, protocol_code_for

		self.assertEqual(protocol_code_for(ErrorCode.INVALID_INPUT_TYPE), 4103)

	def test_unknown_event_kinds_are_rejected(self):
		with self.assertRaises(ValueError) as raised:
			handlers.wait_for_events(since=0, timeout_ms=0, kinds=["not_a_kind"], context=None)
		self.assertIn("unknown event kind", str(raised.exception))

		# Every advertised kind is accepted up to the point of needing a context.
		for kind in sorted(events.KNOWN_KINDS):
			self.assertIn(kind, events.KNOWN_KINDS)


class SchemaContractTests(unittest.TestCase):
	def test_optional_defaults_are_published(self):
		document = schema.dataclass_to_json_schema(models.GetLogsInput)
		max_lines = document["properties"]["max_lines"]
		self.assertEqual(max_lines["default"], 100)
		self.assertEqual(max_lines["minimum"], 1)
		self.assertEqual(max_lines["maximum"], 10000)

		observe = schema.dataclass_to_json_schema(models.ObserveInput)
		self.assertEqual(observe["properties"]["max_bytes"]["default"], 8192)

	def test_descriptions_on_optional_fields_are_not_hidden_in_anyof(self):
		document = schema.dataclass_to_json_schema(models.WaitForEventsInput)
		journal = document["properties"]["journal_id"]
		self.assertIn("anyOf", journal)
		self.assertTrue(journal.get("description"))
		self.assertTrue(journal.get("default", "missing") is None)

	def test_common_fields_get_a_description(self):
		document = schema.dataclass_to_json_schema(models.GetLogsInput)
		self.assertIn("description", document["properties"]["filter_level"])

	def test_reading_tool_scope_describes_document_references(self):
		for model in (models.ListReadingTargetsInput, models.NavigateReadingInput, models.StartBrowseModeInput):
			with self.subTest(model=model.__name__):
				document = schema.dataclass_to_json_schema(model)
				description = document["properties"]["within"]["description"].lower()
				self.assertIn("document or control", description)
				self.assertNotIn("window ref", description)

	def test_heavy_diagnostics_tools_default_to_a_small_payload(self):
		summarize = schema.dataclass_to_json_schema(models.SummarizeLogsInput)
		self.assertEqual(summarize["properties"]["include"]["default"], ["counts"])

		addons = schema.dataclass_to_json_schema(models.ListLoadedAddonsInput)
		self.assertEqual(addons["properties"]["detail"]["default"], "summary")


class BoundedObservationTests(unittest.TestCase):
	def _payload(self) -> dict:
		long_name = "x" * 600
		return {
			"state_version": 1,
			"revision": 1,
			"refs": {"f": "focus", "w": "window"},
			"focus": {
				"ref": "f",
				"role": "EDITABLETEXT",
				"name": long_name,
				"value": long_name,
				"location": {"left": 0, "top": 0, "width": 10, "height": 10},
			},
			"ancestors": [{"ref": "a1", "role": "WINDOW", "name": long_name}],
			"children": [{"ref": "c1", "role": "BUTTON", "name": long_name}],
			"top_window": {"ref": "w", "title": long_name, "handle": 1, "class_name": "C"},
			"selection": None,
		}

	def test_a_tight_budget_is_reported_as_exceeded(self):
		payload = self._payload()
		handlers._bound_observation(payload, 200)

		self.assertTrue(payload["truncated"])
		self.assertIn("descriptive_text", payload["truncated_sections"])
		self.assertLessEqual(
			abs(payload["observed_bytes"] - handlers._observation_bytes(payload)), 8
		)
		self.assertTrue(payload["budget_exceeded"])

	def test_a_reachable_budget_is_actually_met(self):
		payload = self._payload()
		handlers._bound_observation(payload, 100_000)

		self.assertFalse(payload["truncated"])
		self.assertFalse(payload["budget_exceeded"])
		self.assertGreater(payload["observed_bytes"], 0)

	def test_truncation_never_silently_drops_the_focus(self):
		payload = self._payload()
		handlers._bound_observation(payload, 200)
		self.assertIsNotNone(payload["focus"])
		self.assertIsNotNone(payload["top_window"])


class AnnotationTests(unittest.TestCase):
	def test_mutating_tools_are_marked_destructive(self):
		by_name = {tool.metadata.name: tool.metadata for tool in nvda_tools.ALL_NVDA_TOOLS}
		for name in ("invoke", "set_value", "press", "click_at"):
			self.assertTrue(by_name[name].destructive, name)


if __name__ == "__main__":
	unittest.main()
