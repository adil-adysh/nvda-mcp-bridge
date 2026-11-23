# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Opt-in speech permission, persistence clearing, and history reset contracts."""

import importlib
import os
import sys
import threading
import time
import types
import unittest
from unittest import mock

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
if "queueHandler" not in sys.modules:
	_queue_handler = types.ModuleType("queueHandler")
	_queue_handler.eventQueue = "dummy-queue"
	_queue_handler.queueFunction = lambda queue, func, *args, **kwargs: func(*args, **kwargs)
	sys.modules["queueHandler"] = _queue_handler
_PACKAGE_NAME = "nvdaMcpBridge"
if _PACKAGE_NAME not in sys.modules:
	_package = types.ModuleType(_PACKAGE_NAME)
	_package.__path__ = [_ADDON_DIR]
	sys.modules[_PACKAGE_NAME] = _package

security = importlib.import_module(f"{_PACKAGE_NAME}.server.security")
http_bridge = importlib.import_module(f"{_PACKAGE_NAME}.server.http_bridge")
speech = importlib.import_module(f"{_PACKAGE_NAME}.server.speech")


class SpeechPermissionTests(unittest.TestCase):
	def _bridge(self):
		policy = security.PermissionPolicy(granted_permissions={"read_only", "speech_output", "speech_observation"})
		return http_bridge.NVDAHTTPBridge(bridge_token="test-token", permission_policy=policy)

	def test_speech_observation_is_not_granted_by_default_or_reset(self):
		policy = security.PermissionPolicy(granted_permissions={"read_only", "speech_output"})
		self.assertFalse(policy.is_granted("speech_observation"))
		policy.grant("speech_observation")
		policy.reset()
		self.assertFalse(policy.is_granted("speech_observation"))

	def test_revoking_observation_clears_history_and_rotates_generation(self):
		bridge = self._bridge()
		bridge.speech_journal.record_speech("retained announcement", "queued")
		old_id = bridge.speech_journal.journal_id
		bridge.revoke_permission("speech_observation")
		self.assertFalse(bridge.permission_policy.is_granted("speech_observation"))
		self.assertEqual(bridge.speech_journal.version, 0)
		self.assertNotEqual(bridge.speech_journal.journal_id, old_id)
		self.assertEqual(bridge.speech_journal.snapshot()["events"], [])

	def test_reset_permissions_clears_history_and_observation_grant(self):
		bridge = self._bridge()
		bridge.speech_journal.record_speech("retained announcement", "queued")
		old_id = bridge.speech_journal.journal_id
		bridge.reset_permissions()
		self.assertFalse(bridge.permission_policy.is_granted("speech_observation"))
		self.assertEqual(bridge.speech_journal.version, 0)
		self.assertNotEqual(bridge.speech_journal.journal_id, old_id)

	def test_observation_setting_grants_and_revocation_clears_history(self):
		bridge = self._bridge()
		bridge.set_speech_observation_enabled(True)
		self.assertTrue(bridge.permission_policy.is_granted("speech_observation"))
		bridge.speech_journal.record_speech("announcement", "queued")
		old_id = bridge.speech_journal.journal_id
		bridge.set_speech_observation_enabled(False)
		self.assertFalse(bridge.permission_policy.is_granted("speech_observation"))
		self.assertNotEqual(bridge.speech_journal.journal_id, old_id)
		self.assertEqual(bridge.speech_journal.snapshot()["events"], [])

	def test_shared_access_predicate_requires_grant_and_safe_runtime_state(self):
		bridge = self._bridge()
		bridge.speech_monitor = types.SimpleNamespace(is_secure_desktop=False, is_protected_focus=False)
		self.assertEqual(bridge.check_speech_access(), (True, None))
		bridge.speech_monitor.is_secure_desktop = True
		allowed, reason = bridge.check_speech_access()
		self.assertFalse(allowed)
		self.assertIn("secure desktop", reason)
		bridge.speech_monitor.is_secure_desktop = False
		bridge.speech_monitor.is_protected_focus = True
		allowed, reason = bridge.check_speech_access()
		self.assertFalse(allowed)
		self.assertIn("protected focus", reason)

	def test_wait_for_speech_returns_only_authorized_retained_speech(self):
		bridge = self._bridge()
		bridge._native_log_enabled = False
		bridge.speech_monitor = types.SimpleNamespace(is_secure_desktop=False, is_protected_focus=False)
		initial_ui_version = bridge.event_journal.version
		text, truncated = speech.extract_speech_text(["Save", object(), " complete"])
		bridge.speech_journal.record_speech(text, "about_to_synthesize", truncated)
		bridge.speech_journal.record_control("speech_canceled")
		journal_id = bridge.speech_journal.journal_id

		observed = bridge.execute_action(
			request_id="speech-1",
			action_name="wait_for_speech",
			params={"since": 0, "journal_id": journal_id, "timeout_ms": 0, "limit": 20},
		)

		self.assertTrue(observed["success"], observed.get("error"))
		self.assertEqual(
			[(event["kind"], event.get("text")) for event in observed["data"]["events"]],
			[("speech", "Save complete"), ("speech_canceled", None)],
		)
		self.assertEqual(observed["data"]["events"][0]["phase"], "about_to_synthesize")
		self.assertFalse(observed["data"]["events"][0]["text_truncated"])
		self.assertEqual(observed["data"]["journal_id"], journal_id)
		self.assertEqual(observed["data"]["from_version"], 0)
		self.assertEqual(observed["data"]["to_version"], 2)
		self.assertEqual(observed["data"]["cursor_status"], "ok")
		self.assertFalse(observed["data"]["has_more"])
		self.assertTrue(observed["data"]["history_complete"])
		self.assertFalse(observed["data"]["timed_out"])
		self.assertFalse(observed["data"]["cancelled"])
		self.assertEqual(bridge.event_journal.version, initial_ui_version)

		bridge.revoke_permission("speech_observation")
		denied = bridge.execute_action(
			request_id="speech-2",
			action_name="wait_for_speech",
			params={"since": 0, "journal_id": journal_id, "timeout_ms": 0, "limit": 20},
		)
		self.assertFalse(denied["success"])
		self.assertEqual(denied["error"]["code"], 4003)
		self.assertNotIn("Save complete", str(denied))
		self.assertEqual(bridge.speech_journal.snapshot()["events"], [])

	def test_registered_wait_rechecks_access_after_wait(self):
		for transition in ("secure", "protected", "revoke", "revoke_regrant"):
			with self.subTest(transition=transition):
				bridge = self._bridge()
				bridge._native_log_enabled = False
				bridge.speech_monitor = types.SimpleNamespace(is_secure_desktop=False, is_protected_focus=False)
				bridge.speech_journal.record_speech("must not escape", "queued")
				journal_id = bridge.speech_journal.journal_id
				original_wait = bridge.speech_journal.wait

				def wait_then_transition(**kwargs):
					page = original_wait(**kwargs)
					if transition == "secure":
						bridge.speech_monitor.is_secure_desktop = True
					elif transition == "protected":
						bridge.speech_monitor.is_protected_focus = True
					elif transition == "revoke":
						bridge.revoke_permission("speech_observation")
					else:
						bridge.revoke_permission("speech_observation")
						bridge.grant_permission("speech_observation")
					return page

				with mock.patch.object(bridge.speech_journal, "wait", side_effect=wait_then_transition):
					denied = bridge.execute_action(
						request_id="speech-race",
						action_name="wait_for_speech",
						params={"since": 0, "journal_id": journal_id, "timeout_ms": 0},
					)
				self.assertFalse(denied["success"])
				self.assertEqual(denied["error"]["code"], 4205 if transition == "revoke_regrant" else 4003)
				self.assertNotIn("must not escape", str(denied))

	def test_stop_resets_speech_journal_and_wakes_pending_waiter(self):
		bridge = self._bridge()
		bridge.speech_journal.record_speech("shutdown transcript", "queued")
		journal_id = bridge.speech_journal.journal_id
		entered_wait = threading.Event()
		finished_wait = threading.Event()
		original_condition_wait = bridge.speech_journal._condition.wait

		def signal_wait(timeout=None):
			entered_wait.set()
			return original_condition_wait(timeout)

		bridge.speech_journal._condition.wait = signal_wait
		result = {}

		def wait_for_change():
			result.update(
				bridge.speech_journal.wait(
					since=1, timeout_ms=5000, journal_id=journal_id
				)
			)
			finished_wait.set()

		waiter = threading.Thread(target=wait_for_change, daemon=True)
		waiter.start()
		self.assertTrue(entered_wait.wait(1))
		bridge.stop()
		try:
			self.assertTrue(finished_wait.wait(0.5))
			self.assertTrue(result["resync_required"])
			self.assertEqual(result["cursor_status"], "journal_mismatch")
			self.assertEqual(result["events"], [])
			self.assertNotEqual(result["journal_id"], journal_id)
			self.assertEqual(bridge.speech_journal.snapshot()["events"], [])
		finally:
			bridge.speech_journal.reset("test_cleanup")
			waiter.join(timeout=1)


if __name__ == "__main__":
	unittest.main()
