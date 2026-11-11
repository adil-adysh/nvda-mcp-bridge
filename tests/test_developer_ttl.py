# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Tests for the configurable developer-mode elevation window.

Developer mode is deliberately time-limited, but the window was a hardcoded 300
seconds, which is shorter than a debugging session: the elevation expired
mid-probe and every later call returned PERMISSION_DENIED. The window is now a
persisted setting, bounded so elevation can never become permanent.
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
	def __init__(self):
		self.spec = {}
		self.values = {}
		self.save_count = 0

	def __getitem__(self, section):
		return self.values.setdefault(section, {})

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


class DeveloperTtlConfigTests(unittest.TestCase):
	def setUp(self):
		self._previous_config = sys.modules.get("config")
		self.conf = _install_fake_config()
		self.security = _import_security()

	def tearDown(self):
		if self._previous_config is None:
			sys.modules.pop("config", None)
		else:
			sys.modules["config"] = self._previous_config

	def test_clamp_keeps_values_inside_the_bounds(self):
		clamp = self.security.clamp_developer_ttl
		self.assertEqual(clamp(300), 300)
		self.assertEqual(clamp(60), 60)
		self.assertEqual(clamp(1800), 1800)

	def test_clamp_bounds_values_outside_the_bounds(self):
		clamp = self.security.clamp_developer_ttl
		self.assertEqual(clamp(5), self.security.MIN_DEVELOPER_TTL_SECONDS)
		self.assertEqual(clamp(999999), self.security.MAX_DEVELOPER_TTL_SECONDS)
		self.assertEqual(clamp(-1), self.security.MIN_DEVELOPER_TTL_SECONDS)

	def test_clamp_rejects_junk(self):
		clamp = self.security.clamp_developer_ttl
		self.assertEqual(clamp(None), self.security.DEFAULT_DEVELOPER_TTL_SECONDS)
		self.assertEqual(clamp("not a number"), self.security.DEFAULT_DEVELOPER_TTL_SECONDS)

	def test_load_returns_default_when_unset(self):
		self.assertEqual(
			self.security.load_developer_ttl_seconds(), self.security.DEFAULT_DEVELOPER_TTL_SECONDS
		)

	def test_load_clamps_a_stored_value(self):
		self.security.register_config_spec()
		self.conf["nvdaMcpBridge"]["developerModeTtlSeconds"] = 99999
		self.assertEqual(
			self.security.load_developer_ttl_seconds(), self.security.MAX_DEVELOPER_TTL_SECONDS
		)

	def test_save_persists_the_clamped_value(self):
		self.assertTrue(self.security.save_developer_ttl_seconds(600))
		self.assertEqual(self.conf["nvdaMcpBridge"]["developerModeTtlSeconds"], 600)
		self.assertEqual(self.conf.save_count, 1)

		self.assertTrue(self.security.save_developer_ttl_seconds(99999))
		self.assertEqual(
			self.conf["nvdaMcpBridge"]["developerModeTtlSeconds"],
			self.security.MAX_DEVELOPER_TTL_SECONDS,
		)

	def test_register_config_spec_declares_the_key_as_integer(self):
		self.security.register_config_spec()
		self.assertEqual(self.conf.spec["nvdaMcpBridge"]["developerModeTtlSeconds"], "integer")


class DeveloperTtlManagerTests(unittest.TestCase):
	def setUp(self):
		self._previous_config = sys.modules.get("config")
		self.conf = _install_fake_config()
		self.security = _import_security()
		self.Manager = self.security.DeveloperModeManager

	def tearDown(self):
		if self._previous_config is None:
			sys.modules.pop("config", None)
		else:
			sys.modules["config"] = self._previous_config

	def test_manager_exposes_the_configured_window(self):
		self.assertEqual(self.Manager(default_ttl_seconds=600).default_ttl_seconds, 600)

	def test_set_default_ttl_seconds_clamps(self):
		mgr = self.Manager()
		self.assertEqual(mgr.set_default_ttl_seconds(3600), self.security.MAX_DEVELOPER_TTL_SECONDS)
		self.assertEqual(mgr.default_ttl_seconds, self.security.MAX_DEVELOPER_TTL_SECONDS)

	def test_elevation_uses_the_configured_window(self):
		mgr = self.Manager(default_ttl_seconds=600)
		mgr.approve_locally()
		self.assertGreater(mgr.time_remaining_seconds, 500)

	def test_changing_the_setting_does_not_cut_an_active_elevation(self):
		mgr = self.Manager(default_ttl_seconds=900)
		mgr.approve_locally()
		granted = mgr.time_remaining_seconds

		mgr.set_default_ttl_seconds(60)

		self.assertTrue(mgr.is_enabled, "an active elevation keeps the window it was granted with")
		self.assertGreater(mgr.time_remaining_seconds, granted - 5)
		self.assertEqual(mgr.default_ttl_seconds, 60, "the next elevation uses the new window")


if __name__ == "__main__":
	unittest.main()
