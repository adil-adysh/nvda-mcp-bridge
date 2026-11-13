# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Bridge instance identity and replacement (API level 1).

A UI reference or event cursor is meaningful only inside the Bridge instance
that issued it. These tests pin that identity to the process instance and make
sure two instances never share one.
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
if "queueHandler" not in sys.modules:
	_queue = types.ModuleType("queueHandler")
	_queue.eventQueue = None
	_queue.queueFunction = lambda queue, func, *args, **kwargs: func(*args, **kwargs)
	sys.modules["queueHandler"] = _queue

_PACKAGE_NAME = "nvdaMcpBridge"
if _PACKAGE_NAME not in sys.modules:
	_package = types.ModuleType(_PACKAGE_NAME)
	_package.__path__ = [_ADDON_DIR]
	sys.modules[_PACKAGE_NAME] = _package

http_bridge = importlib.import_module(f"{_PACKAGE_NAME}.server.http_bridge")
security = importlib.import_module(f"{_PACKAGE_NAME}.server.security")
refs = importlib.import_module(f"{_PACKAGE_NAME}.server.refs")


def _bridge(token="test-token"):
	return http_bridge.NVDAHTTPBridge(
		host="127.0.0.1",
		port=0,
		bridge_token=token,
		permission_policy=security.PermissionPolicy(),
	)


class BridgeIdentityTests(unittest.TestCase):
	def setUp(self):
		self.bridge = _bridge()

	def test_health_exposes_a_fresh_instance_id_and_start_time(self):
		health = self.bridge.get_health_payload()
		self.assertRegex(health["instance_id"], r"^[0-9a-f]{32}$")
		self.assertTrue(health["started_at"])
		self.assertEqual(health["api_level"], 1)
		self.assertEqual(health["supported_api_levels"], [1])
		self.assertEqual(health["capabilities"]["event_cursors"], "typed")
		self.assertTrue(health["capabilities"]["action_verification"])

	def test_two_bridge_instances_never_share_an_id(self):
		other = _bridge()
		self.assertNotEqual(self.bridge.instance_id, other.instance_id)
		self.assertNotEqual(
			self.bridge.get_health_payload()["instance_id"],
			other.get_health_payload()["instance_id"],
		)

	def test_a_journal_reset_does_not_change_the_bridge_instance(self):
		instance = self.bridge.instance_id
		journal = self.bridge.event_journal.journal_id
		self.bridge.event_journal.reset("test")
		self.assertEqual(self.bridge.instance_id, instance)
		self.assertNotEqual(self.bridge.event_journal.journal_id, journal)

	def test_the_bridge_meta_block_is_consistent(self):
		meta = self.bridge._bridge_meta()
		self.assertEqual(meta["instance_id"], self.bridge.instance_id)
		self.assertEqual(meta["api_level"], 1)
		self.assertEqual(meta["started_at"], self.bridge.started_at.isoformat())

	def test_references_are_bound_to_the_issuing_instance(self):
		class Thing:
			role = "BUTTON"
			name = "Run"
			windowClassName = "Chrome"
			windowControlID = 4
			windowHandle = 10
			processID = 20

		reader = lambda obj: {  # noqa: E731
			"role": obj.role,
			"name": obj.name,
			"windowClassName": obj.windowClassName,
			"windowControlID": obj.windowControlID,
			"windowHandle": obj.windowHandle,
			"processID": obj.processID,
		}
		self.bridge.refs.register({"f": Thing()}, state_version=1, identity_of=reader)

		other = _bridge()
		with self.assertRaises(refs.StaleReference) as caught:
			other.refs.get("f", identity_of=reader)
		self.assertEqual(caught.exception.reason_code, refs.REASON_UNAVAILABLE)

		# Rebinding the same table to a replacement clears it.
		self.assertTrue(self.bridge.refs.bind_instance("replacement"))
		self.assertEqual(len(self.bridge.refs), 0)


if __name__ == "__main__":
	unittest.main()
