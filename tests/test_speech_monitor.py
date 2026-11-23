# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""NVDA extension-point registration and privacy gating for speech capture."""

import importlib
import os
import sys
import types
import unittest
from unittest.mock import patch

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


class _Point:
	def __init__(self, fail=False):
		self.callbacks = []
		self.register_calls = 0
		self.unregister_calls = 0
		self.fail = fail

	def register(self, callback):
		self.register_calls += 1
		self.callbacks.append(callback)
		if self.fail:
			raise RuntimeError("registration failed")

	def unregister(self, callback):
		self.unregister_calls += 1
		if callback in self.callbacks:
			self.callbacks.remove(callback)


class SpeechMonitorTests(unittest.TestCase):
	def _monitor(self, *, modern=True, permission=True, secure=None, fallback=None):
		modern_point = _Point() if modern else None
		fallback_point = fallback or _Point()
		cancel = _Point()
		pause = _Point()
		secure_point = _Point()
		synth_module = types.SimpleNamespace(pre_synthSpeak=modern_point) if modern else types.SimpleNamespace()
		extension_module = types.SimpleNamespace(
			pre_speechQueued=fallback_point,
			speechCanceled=cancel,
			post_speechPaused=pause,
		)
		secure_module = types.SimpleNamespace(post_secureDesktopStateChange=secure_point)
		modules = {
			"synthDriverHandler": synth_module,
			"speech.extensions": extension_module,
			"winAPI.secureDesktop": secure_module,
		}
		journal = speech.SpeechJournal()
		monitor = speech.SpeechMonitor(journal, lambda: permission, initially_secure=bool(secure))
		return monitor, journal, modules, modern_point, fallback_point, cancel, pause, secure_point

	def test_monitor_prefers_pre_synth_hook_and_unregisters(self):
		monitor, journal, modules, modern, fallback, cancel, pause, secure = self._monitor()
		with patch.object(speech.importlib, "import_module", side_effect=lambda name: modules[name]):
			self.assertTrue(monitor.start())
			modern.callbacks[0](speechSequence=["Saved"])
			page = journal.snapshot()
			self.assertEqual(page["events"][0]["phase"], "about_to_synthesize")
			self.assertEqual(fallback.register_calls, 0)
			monitor.stop()
			monitor.stop()
		self.assertEqual(modern.unregister_calls, 1)
		self.assertEqual(cancel.unregister_calls, 1)
		self.assertEqual(pause.unregister_calls, 1)
		self.assertEqual(secure.unregister_calls, 1)
		self.assertEqual(modern.callbacks, [])

	def test_monitor_uses_queued_fallback_when_pre_synth_hook_is_missing(self):
		monitor, journal, modules, modern, fallback, *_ = self._monitor(modern=False)
		with patch.object(speech.importlib, "import_module", side_effect=lambda name: modules[name]):
			self.assertTrue(monitor.start())
			fallback.callbacks[0](speechSequence=["Queued"])
		self.assertEqual(journal.snapshot()["events"][0]["phase"], "queued")
		self.assertEqual(modern, None)

	def test_monitor_start_and_stop_are_idempotent(self):
		monitor, _, modules, modern, *_ = self._monitor()
		with patch.object(speech.importlib, "import_module", side_effect=lambda name: modules[name]):
			self.assertTrue(monitor.start())
			self.assertTrue(monitor.start())
			monitor.stop()
			monitor.stop()
		self.assertEqual(modern.register_calls, 1)
		self.assertEqual(modern.unregister_calls, 1)

	def test_monitor_suppresses_capture_without_permission(self):
		monitor, journal, modules, modern, *_ = self._monitor(permission=False)
		with patch.object(speech.importlib, "import_module", side_effect=lambda name: modules[name]):
			self.assertTrue(monitor.start())
			modern.callbacks[0](speechSequence=["Private"])
		self.assertEqual(journal.version, 0)

	def test_monitor_suppresses_capture_on_secure_desktop_and_protected_focus(self):
		monitor, journal, modules, modern, _, _, _, secure = self._monitor()
		with patch.object(speech.importlib, "import_module", side_effect=lambda name: modules[name]):
			self.assertTrue(monitor.start())
			secure.callbacks[0](isSecureDesktop=True)
			modern.callbacks[0](speechSequence=["Secure"])
			secure.callbacks[0](isSecureDesktop=False)
			monitor.set_protected_focus(types.SimpleNamespace(isProtected=True))
			modern.callbacks[0](speechSequence=["Password"])
		self.assertEqual(journal.version, 0)

	def test_monitor_records_cancel_pause_and_resume_without_text(self):
		monitor, journal, modules, _, _, cancel, pause, _ = self._monitor()
		with patch.object(speech.importlib, "import_module", side_effect=lambda name: modules[name]):
			self.assertTrue(monitor.start())
			cancel.callbacks[0]()
			pause.callbacks[0](switch=True)
			pause.callbacks[0](switch=False)
		page = journal.snapshot()
		self.assertEqual([item["kind"] for item in page["events"]], [
			"speech_canceled", "speech_paused", "speech_resumed",
		])
		self.assertTrue(all("text" not in item for item in page["events"]))

	def test_monitor_focus_state_uses_protected_state_only(self):
		monitor = speech.SpeechMonitor(speech.SpeechJournal(), lambda: True)
		monitor.set_protected_focus(types.SimpleNamespace(isProtected=True))
		self.assertTrue(monitor.is_protected_focus)
		monitor.set_protected_focus(types.SimpleNamespace(isProtected=False))
		self.assertFalse(monitor.is_protected_focus)

	def test_monitor_registration_failure_unregisters_partial_hooks(self):
		monitor, _, modules, modern, _, cancel, _, _ = self._monitor()
		cancel.fail = True
		with patch.object(speech.importlib, "import_module", side_effect=lambda name: modules[name]):
			self.assertFalse(monitor.start())
		self.assertEqual(modern.callbacks, [])
		self.assertEqual(modern.unregister_calls, 1)
		self.assertFalse(monitor.is_started)


if __name__ == "__main__":
	unittest.main()
