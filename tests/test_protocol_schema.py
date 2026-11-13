# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Protocol documents validate against the published JSON Schemas (API level 1)."""

import importlib
import json
import os
import sys
import types
import unittest
from pathlib import Path

_ADDON_DIR = os.path.normpath(
	os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "addon", "globalPlugins", "nvdaMcp-bridge")
)
_PROTOCOL_DIR = Path(__file__).resolve().parent.parent / "docs" / "protocol"
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
protocol = importlib.import_module(f"{_PACKAGE_NAME}.server.protocol")
response = importlib.import_module(f"{_PACKAGE_NAME}.server.response")


def _load(name):
	with open(_PROTOCOL_DIR / name, encoding="utf-8") as handle:
		return json.load(handle)


def _validate(instance, schema_name):
	import jsonschema
	from referencing import Registry, Resource

	common = _load("common.schema.json")
	registry = Registry().with_resource(
		"https://nvda.local/schemas/common.schema.json",
		Resource.from_contents(common),
	)
	schema = _load(schema_name)
	jsonschema.validate(instance, schema, registry=registry)


class ProtocolSchemaTests(unittest.TestCase):
	def setUp(self):
		self.bridge = http_bridge.NVDAHTTPBridge(
			host="127.0.0.1",
			port=0,
			bridge_token="test-token",
		)

	def test_health_payload_matches_the_published_schema(self):
		_validate(self.bridge.get_health_payload(), "health.schema.json")

	def test_tools_discovery_payload_matches_the_published_schema(self):
		# The developer-only tool is omitted unless developer mode is on; the
		# inventory count (32) is the full registry surface.
		self.bridge.enable_developer_mode()
		tools = self.bridge._list_tools()
		payload = protocol.build_tools_payload(tools, self.bridge._list_output_types())
		payload["schema_hash"] = http_bridge._schema_hash(payload)
		payload["instance_id"] = self.bridge.instance_id
		payload["started_at"] = self.bridge.started_at.isoformat()
		_validate(payload, "tool-discovery.schema.json")
		self.assertEqual(payload["count"], 32)

	def test_error_envelope_matches_the_published_schema(self):
		envelope = response.build_error_envelope(
			request_id="req-0001",
			action="press",
			code=4206,
			message="stale",
			nvda_status_snapshot=self.bridge.get_nvda_status_snapshot(),
			duration_ms=5.0,
			details={"reason_code": "window", "ref": "f"},
		)
		_validate(envelope, "action-response.schema.json")

	def test_the_schema_hash_is_contract_only_not_instance_identity(self):
		def payload_for(bridge):
			bridge.enable_developer_mode()
			body = protocol.build_tools_payload(bridge._list_tools(), bridge._list_output_types())
			digest = http_bridge._schema_hash(body)
			body["instance_id"] = bridge.instance_id
			body["started_at"] = bridge.started_at.isoformat()
			return digest, body

		first = http_bridge.NVDAHTTPBridge(host="127.0.0.1", port=0, bridge_token="test-token")
		digest_a, body_a = payload_for(first)
		digest_b, body_b = payload_for(self.bridge)
		self.assertEqual(digest_a, digest_b, "a restart is not a contract change")
		self.assertNotEqual(body_a["instance_id"], body_b["instance_id"])

	def test_success_envelope_matches_the_published_schema(self):
		envelope = {
			"request_id": "req-0002",
			"success": True,
			"action": "press",
			"duration_ms": 12.0,
			"bridge": self.bridge._bridge_meta(),
			"nvda_status_snapshot": self.bridge.get_nvda_status_snapshot(),
			"result": {
				"ok": True,
				"state_version": 4,
				"observed": True,
				"verification": "confirmed",
				"evidence": [{"source": "projection", "path": "focus.name", "matched": True, "value": "Save"}],
			},
			"data": {"requested": "ctrl+s"},
		}
		_validate(envelope, "action-response.schema.json")


if __name__ == "__main__":
	unittest.main()
