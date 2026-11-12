# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""The logical action boundary: one call, one trace, one retrievable record.

These exercise the real bridge object rather than a mock, because the thing
being tested is exactly that the boundary wires the executor, the settler and
the store together.
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
# Non-destructive: other test modules install a fuller `queueHandler` stub and
# rely on it surviving. Replacing it here would break them when the whole suite
# is collected.
if "queueHandler" not in sys.modules:
	_queue_handler = types.ModuleType("queueHandler")
	_queue_handler.queueFunction = lambda func, *args, **kwargs: func(*args, **kwargs)
	_queue_handler.eventQueue = None
	sys.modules["queueHandler"] = _queue_handler

_PACKAGE_NAME = "nvdaMcpBridge"
if _PACKAGE_NAME not in sys.modules:
	_package = types.ModuleType(_PACKAGE_NAME)
	_package.__path__ = [_ADDON_DIR]
	sys.modules[_PACKAGE_NAME] = _package

http_bridge = importlib.import_module(f"{_PACKAGE_NAME}.server.http_bridge")
NVDAHTTPBridge = http_bridge.NVDAHTTPBridge


class ActionBoundaryTests(unittest.TestCase):
	def setUp(self):
		self.bridge = NVDAHTTPBridge(host="127.0.0.1", port=0, bridge_token="test-token")
		# Native log access is exercised by its own unit tests; here the bridge
		# runs outside NVDA, where the file does not exist.
		self.bridge._native_log_enabled = False
		self.bridge.grant_permission("input_control")
		self.bridge.grant_permission("diagnostics")

	def test_a_failed_action_returns_a_trace_id_and_a_summary(self):
		envelope = self.bridge.execute_action(request_id="req-1", action_name="press", params={"keys": 5})

		self.assertFalse(envelope["success"])
		self.assertEqual(envelope["error"]["code"], 4103)
		self.assertTrue(envelope["trace_id"].startswith("at-"))
		summary = envelope["trace"]
		self.assertEqual(summary["trace_id"], envelope["trace_id"])
		self.assertEqual(summary["action"], "press")
		self.assertEqual(summary["outcome"], "failed")
		self.assertEqual(summary["exception"]["stage"], "validation")
		self.assertIn("limitations", summary)

	def test_the_summary_is_compact_and_does_not_carry_the_event_list(self):
		envelope = self.bridge.execute_action(request_id="req-1", action_name="press", params={"keys": 5})

		self.assertNotIn("events", envelope["trace"])
		self.assertLess(len(str(envelope["trace"])), 4000)

	def test_the_envelope_keeps_its_existing_fields(self):
		envelope = self.bridge.execute_action(request_id="req-1", action_name="press", params={"keys": 5})

		for key in ("request_id", "success", "action", "duration_ms", "bridge", "nvda_status_snapshot", "error"):
			self.assertIn(key, envelope)
		self.assertEqual(envelope["request_id"], "req-1")

	def test_an_unknown_action_is_recorded_as_a_refusal(self):
		envelope = self.bridge.execute_action(request_id="req-1", action_name="no_such_tool", params={})

		self.assertEqual(envelope["error"]["code"], 4102)
		trace = self.bridge.get_trace_payload(envelope["trace_id"])
		self.assertEqual(trace["completion"]["outcome"], "failed")
		self.assertEqual(trace["execution"]["failure_stage"], "validation")
		self.assertFalse(trace["completion"]["dispatched"])
		self.assertFalse(trace["completion"]["tool_succeeded"])

	def test_the_trace_can_be_read_back_through_the_tool(self):
		failed = self.bridge.execute_action(request_id="req-1", action_name="press", params={"keys": 5})

		read = self.bridge.execute_action(
			request_id="req-2",
			action_name="get_action_trace",
			params={"trace_id": failed["trace_id"]},
		)

		self.assertTrue(read["success"], read.get("error"))
		self.assertTrue(read["data"]["found"])
		self.assertEqual(read["data"]["trace"]["trace_id"], failed["trace_id"])
		self.assertEqual(read["data"]["trace"]["action"]["name"], "press")
		self.assertIn("max_traces", read["data"]["retention"])

	def test_a_missing_trace_reports_the_retention_window_instead_of_failing(self):
		read = self.bridge.execute_action(
			request_id="req-1", action_name="get_action_trace", params={"trace_id": "at-does-not-exist"}
		)

		self.assertTrue(read["success"], read.get("error"))
		self.assertFalse(read["data"]["found"])
		self.assertEqual(read["data"]["missing_reason"], "expired_or_unknown")
		self.assertEqual(read["data"]["retention"]["max_traces"], self.bridge.trace_store.max_traces)

	def test_a_client_supplied_trace_id_is_used(self):
		envelope = self.bridge.execute_action(
			request_id="req-1", action_name="press", params={"keys": 5}, trace_id="at-client-chosen"
		)

		self.assertEqual(envelope["trace_id"], "at-client-chosen")
		self.assertIsNotNone(self.bridge.get_trace_payload("at-client-chosen"))

	def test_the_trace_records_the_event_window_it_started_from(self):
		envelope = self.bridge.execute_action(request_id="req-1", action_name="press", params={"keys": 5})

		trace = self.bridge.get_trace_payload(envelope["trace_id"])

		self.assertEqual(trace["observation"]["journal_id"], self.bridge.event_journal.journal_id)
		self.assertEqual(trace["observation"]["from_event_version"], 0)
		self.assertEqual(trace["evidence"]["native_log_status"], "not_attempted")

	def test_a_successful_action_is_marked_succeeded_not_merely_answered(self):
		envelope = self.bridge.execute_action(
			request_id="req-1", action_name="get_action_trace", params={"trace_id": "at-nothing"}
		)

		self.assertTrue(envelope["success"])
		self.assertEqual(envelope["trace"]["outcome"], "succeeded")
		self.assertTrue(envelope["trace"]["retained"])

	def test_a_denied_permission_is_recorded_as_a_refusal(self):
		# `press` needs input_control; drop it to exercise the authorization stage.
		self.bridge.revoke_permission("input_control")

		envelope = self.bridge.execute_action(request_id="req-1", action_name="press", params={"keys": "tab"})

		self.assertFalse(envelope["success"])
		self.assertEqual(envelope["trace"]["outcome"], "failed")
		trace = self.bridge.get_trace_payload(envelope["trace_id"])
		self.assertEqual(trace["execution"]["failure_stage"], "authorization")


if __name__ == "__main__":
	unittest.main()
