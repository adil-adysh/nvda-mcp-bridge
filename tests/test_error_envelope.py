# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Common success and error envelopes (API level 1)."""

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

refs = importlib.import_module(f"{_PACKAGE_NAME}.server.refs")
response = importlib.import_module(f"{_PACKAGE_NAME}.server.response")
protocol = importlib.import_module(f"{_PACKAGE_NAME}.server.protocol")

RETRY_POLICIES = {"never", "after_delay", "after_state_change"}
CATEGORIES = {"auth", "validation", "nvda_state", "runtime", "internal"}
RECOVERY_ACTIONS = {
	"none",
	"observe",
	"wait",
	"fix_arguments",
	"configure_token",
	"request_permission",
	"retry_later",
	"read_diagnostics",
	"resync",
}


class ErrorEnvelopeTests(unittest.TestCase):
	def test_every_known_error_has_stable_metadata_and_a_recovery(self):
		for code, spec in protocol.ERROR_CODES.items():
			with self.subTest(code=code):
				error = response.build_error_object(code, "message")
				self.assertEqual(error["code"], code)
				self.assertEqual(error["name"], spec["name"])
				self.assertIn(error["category"], CATEGORIES)
				self.assertIn(error["retry_policy"], RETRY_POLICIES)
				self.assertIn(error["recovery"]["action"], RECOVERY_ACTIONS)
				self.assertTrue(error["recovery"]["hint"])

	def test_stale_reference_keeps_code_4206_and_lifts_recovery(self):
		stale = refs.StaleReference("f", "windowHandle changed", refs.REASON_WINDOW, "Notepad")
		error = response.build_error_object(4206, "The target reference is no longer valid.", stale.to_details())
		self.assertEqual(error["code"], 4206)
		self.assertEqual(error["name"], "STALE_REFERENCE")
		self.assertEqual(error["category"], "nvda_state")
		self.assertEqual(error["retry_policy"], "after_state_change")
		self.assertEqual(error["details"]["reason_code"], "window")
		self.assertEqual(error["details"]["received"], "f")
		self.assertEqual(error["details"]["scope"], "Notepad")
		self.assertEqual(error["recovery"]["action"], "observe")
		self.assertNotIn("recovery", error["details"], "recovery belongs at the error level")

	def test_a_recovery_hint_never_orders_an_automatic_replay(self):
		for code in protocol.ERROR_CODES:
			error = response.build_error_object(code, "message")
			hint = error["recovery"]["hint"].lower()
			self.assertNotIn("automatically replay", hint)
			self.assertNotIn("retry the action", hint)

	def test_validation_errors_are_retry_policy_never(self):
		for code in (4101, 4102, 4103):
			self.assertEqual(response.build_error_object(code, "bad")["retry_policy"], "never")

	def test_the_error_envelope_is_machine_readable_and_complete(self):
		envelope = response.build_error_envelope(
			request_id="req-1",
			action="press",
			code=4206,
			message="stale",
			nvda_status_snapshot={"secure_screen": False},
			duration_ms=12.5,
		)
		self.assertFalse(envelope["success"])
		self.assertEqual(envelope["request_id"], "req-1")
		self.assertEqual(envelope["action"], "press")
		self.assertEqual(envelope["duration_ms"], 12.5)
		self.assertEqual(envelope["bridge"]["api_level"], 1)
		self.assertEqual(envelope["error"]["code"], 4206)
		self.assertEqual(envelope["error"]["recovery"]["action"], "observe")

	def test_unknown_codes_fall_back_to_the_documented_internal_error(self):
		error = response.build_error_object(9999, "boom")
		self.assertEqual(error["code"], 5303)
		self.assertEqual(error["name"], "BRIDGE_INTERNAL_ERROR")


if __name__ == "__main__":
	unittest.main()
