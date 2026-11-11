# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Tests for persisting capability grants through NVDA's configuration system.

The settings panel writes the user's ticks to ``[nvdaMcpBridge] permissions``;
the bridge restores them on startup. Without this, every NVDA restart or plugin
reload silently discarded the user's choice and dropped back to safe defaults.
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

_fake_logger = types.SimpleNamespace(
    debug=lambda *args, **kwargs: None,
    info=lambda *args, **kwargs: None,
    warning=lambda *args, **kwargs: None,
    error=lambda *args, **kwargs: None,
)
sys.modules["logHandler"] = types.SimpleNamespace(log=_fake_logger)

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


class FakeConfig:
	"""Minimal stand-in for NVDA's ``config.conf`` mapping."""

	def __init__(self):
		self.spec = {}
		self.values = {}
		self.save_count = 0

	def __getitem__(self, section):
		result = self.values.setdefault(section, {})
		return result

	def __setitem__(self, section, value):
		self.values[section] = value

	def save(self):
		self.save_count += 1


def _install_fake_config() -> FakeConfig:
	conf = FakeConfig()
	module = types.ModuleType("config")
	module.conf = conf
	sys.modules["config"] = module
	return conf


def _import_security():
	module_name = f"{_PACKAGE_NAME}.server.security"
	if module_name in sys.modules:
		return importlib.reload(sys.modules[module_name])
	return importlib.import_module(module_name)


class TestPermissionPersistence(unittest.TestCase):
	def setUp(self):
		self._previous_config = sys.modules.get("config")
		self.conf = _install_fake_config()
		self.security = _import_security()

	def tearDown(self):
		if self._previous_config is None:
			sys.modules.pop("config", None)
		else:
			sys.modules["config"] = self._previous_config

	def test_register_config_spec_adds_section(self):
		self.security.register_config_spec()
		self.assertIn("nvdaMcpBridge", self.conf.spec)
		self.assertIn("permissions", self.conf.spec["nvdaMcpBridge"])

	def test_load_returns_none_when_nothing_saved(self):
		self.security.register_config_spec()
		self.assertIsNone(self.security.load_persisted_permissions())

	def test_save_then_load_roundtrip(self):
		self.security.register_config_spec()
		saved = self.security.save_persisted_permissions(
			{"read_only", "speech_output", "input_control", "diagnostics"},
		)

		self.assertTrue(saved)
		self.assertEqual(1, self.conf.save_count)
		restored = self.security.load_persisted_permissions()
		self.assertEqual(
			frozenset({"read_only", "speech_output", "input_control", "diagnostics"}),
			restored,
		)

	def test_developer_capability_is_never_persisted(self):
		self.security.register_config_spec()
		self.security.save_persisted_permissions({"read_only", "developer"})

		self.assertEqual("read_only", self.conf["nvdaMcpBridge"]["permissions"])
		self.assertNotIn("developer", self.security.load_persisted_permissions())

	def test_unknown_entries_are_ignored_and_baseline_kept(self):
		self.security.register_config_spec()
		self.conf["nvdaMcpBridge"]["permissions"] = "not_a_permission,input_control"

		restored = self.security.load_persisted_permissions()

		self.assertIn("input_control", restored)
		self.assertIn("read_only", restored)
		self.assertIn("speech_output", restored)
		self.assertNotIn("not_a_permission", restored)

	def test_reset_persists_safe_defaults(self):
		self.security.register_config_spec()
		self.security.save_persisted_permissions({"read_only", "speech_output", "input_control", "diagnostics"})
		self.security.save_persisted_permissions({"read_only", "speech_output"})

		self.assertEqual(
			frozenset({"read_only", "speech_output"}),
			self.security.load_persisted_permissions(),
		)


class TestPolicyRestoresPersistedGrants(unittest.TestCase):
	def tearDown(self):
		if getattr(self, "_previous_config", None) is None:
			sys.modules.pop("config", None)
		else:
			sys.modules["config"] = self._previous_config

	def test_bridge_applies_saved_permissions_on_startup(self):
		self._previous_config = sys.modules.get("config")
		conf = _install_fake_config()
		security = _import_security()
		security.register_config_spec()
		conf["nvdaMcpBridge"]["permissions"] = "read_only,speech_output,input_control"

		bridge_module = importlib.import_module(f"{_PACKAGE_NAME}.server.http_bridge")
		bridge = bridge_module.NVDAHTTPBridge(bridge_token="test-token")

		self.assertTrue(bridge.permission_policy.is_granted("input_control"))
		self.assertTrue(bridge.permission_policy.is_granted("read_only"))
		self.assertFalse(bridge.permission_policy.is_granted("diagnostics"))


if __name__ == "__main__":
	unittest.main()

