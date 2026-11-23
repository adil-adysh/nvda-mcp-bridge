# pyright: reportMissingImports=false, reportUnusedCallResult=false

import importlib
import http.client
import json
import os
import sys
import threading
import time
import types
import unittest
from email.message import Message
from urllib.parse import urlsplit
from unittest import mock

# Add addon directory to path for imports
_ADDON_DIR = os.path.normpath(
	os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "addon", "globalPlugins", "nvdaMcp-bridge")
)

# Stub NVDA dependencies used by http_bridge at import-time.
_fake_logger = types.SimpleNamespace(
    debug=lambda *args, **kwargs: None,
    info=lambda *args, **kwargs: None,
    warning=lambda *args, **kwargs: None,
    error=lambda *args, **kwargs: None,
)
sys.modules["logHandler"] = types.SimpleNamespace(log=_fake_logger)

_queue_handler = types.ModuleType("queueHandler")
_queue_handler.queueFunction = lambda func, *args, **kwargs: func(*args, **kwargs)
sys.modules["queueHandler"] = _queue_handler

# Build a synthetic package namespace for the addon to support relative imports.
_PACKAGE_NAME = "nvdaMcpBridge"
_package = types.ModuleType(_PACKAGE_NAME)
_package.__path__ = [_ADDON_DIR]
sys.modules[_PACKAGE_NAME] = _package

bridge_module = importlib.import_module(f"{_PACKAGE_NAME}.server.http_bridge")

NVDABridgeRequestHandler = bridge_module.NVDABridgeRequestHandler
NVDAHTTPBridge = bridge_module.NVDAHTTPBridge
PROTOCOL = bridge_module.PROTOCOL
PROTOCOL_VERSION = bridge_module.PROTOCOL_VERSION
API_LEVEL = bridge_module.API_LEVEL
_is_nvda_menu_open = bridge_module._is_nvda_menu_open


def _speech_bridge(*, enabled=True, secure=False, protected=False):
	permissions = {"read_only", "speech_output"}
	if enabled:
		permissions.add("speech_observation")
	bridge = NVDAHTTPBridge(
		host="127.0.0.1",
		port=0,
		bridge_token="test-token",
		permission_policy=bridge_module.PermissionPolicy(granted_permissions=permissions),
	)
	bridge.speech_monitor = types.SimpleNamespace(
		is_secure_desktop=secure,
		is_protected_focus=protected,
	)
	return bridge


def _get_speech(bridge, url, token=None):
	handler = object.__new__(NVDABridgeRequestHandler)
	handler.server = bridge
	handler.headers = Message()
	if token is not None:
		handler.headers[bridge_module.TOKEN_HEADER] = token
	responses = []
	handler._send_json_response = lambda payload, status=200, extra_headers=None: responses.append(
		(payload, status, extra_headers)
	)
	handler._handle_speech(urlsplit(url))
	return responses[-1]


class HttpBridgeTests(unittest.TestCase):

    def test_speech_endpoint_rejects_missing_or_invalid_token_without_reading(self):
        bridge = _speech_bridge()
        with mock.patch.object(bridge.speech_journal, "wait", wraps=bridge.speech_journal.wait) as wait:
            self.assertEqual(_get_speech(bridge, "/speech")[1], 401)
            self.assertEqual(_get_speech(bridge, "/speech", token="invalid")[1], 401)
        wait.assert_not_called()

    def test_speech_endpoint_requires_speech_observation_without_returning_text(self):
        bridge = _speech_bridge(enabled=False)
        bridge.speech_journal.record_speech("should not leak", "queued")
        payload, status, _ = _get_speech(bridge, "/speech", token="test-token")
        self.assertEqual(status, 403)
        self.assertNotIn("should not leak", str(payload))

    def test_speech_endpoint_denies_secure_desktop_and_protected_focus(self):
        for state in ({"secure": True}, {"protected": True}):
            bridge = _speech_bridge(**state)
            bridge.speech_journal.record_speech("private", "queued")
            payload, status, _ = _get_speech(bridge, "/speech", token="test-token")
            self.assertEqual(status, 403)
            self.assertNotIn("private", str(payload))

    def test_speech_endpoint_clamps_query_values_and_preserves_typed_cursor(self):
        bridge = _speech_bridge()
        bridge.speech_journal.record_speech("announcement", "about_to_synthesize")
        journal_id = bridge.speech_journal.journal_id
        with mock.patch.object(bridge.speech_journal, "wait", wraps=bridge.speech_journal.wait) as wait:
            payload, status, _ = _get_speech(
                bridge,
                f"/speech?since=0&journal_id={journal_id}&timeout_ms=999999&limit=999999",
                token="test-token",
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["journal_id"], journal_id)
        self.assertEqual(payload["events"][0]["text"], "announcement")
        self.assertEqual(wait.call_args.kwargs["timeout_ms"], 30_000)
        self.assertEqual(wait.call_args.kwargs["limit"], 256)
        self.assertEqual(wait.call_args.kwargs["since"], 0)
        self.assertEqual(wait.call_args.kwargs["journal_id"], journal_id)

    def test_speech_endpoint_invalid_query_values_use_bounded_defaults(self):
        bridge = _speech_bridge()
        with mock.patch.object(bridge.speech_journal, "wait", wraps=bridge.speech_journal.wait) as wait:
            payload, status, _ = _get_speech(
                bridge, "/speech?since=bad&timeout_ms=bad&limit=bad", token="test-token"
            )
        self.assertEqual(status, 200)
        self.assertEqual(wait.call_args.kwargs["since"], 0)
        self.assertEqual(wait.call_args.kwargs["timeout_ms"], 0)
        self.assertEqual(wait.call_args.kwargs["limit"], 20)

    def test_started_http_server_exposes_speech_journal_and_access_gate(self):
        bridge = _speech_bridge()
        server_thread = threading.Thread(target=bridge.start, daemon=True)
        server_thread.start()
        try:
            deadline = time.monotonic() + 2
            while (bridge.server is None or not hasattr(bridge.server, "event_journal")) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsNotNone(bridge.server, "bridge server did not start")
            self.assertTrue(hasattr(bridge.server, "event_journal"), "server services were not attached")
            bridge.speech_journal.record_speech("live server speech", "queued")
            port = bridge.server.server_address[1]
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            connection.request("GET", "/speech", headers={bridge_module.TOKEN_HEADER: "test-token"})
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 200, payload)
            self.assertEqual(payload["events"][0]["text"], "live server speech")
            connection.close()
        finally:
            bridge.shutdown_event.set()
            bridge.stop()
            server_thread.join(timeout=3)

    def test_speech_endpoint_rechecks_access_after_wait(self):
        for transition in ("secure", "protected", "revoke", "revoke_regrant"):
            with self.subTest(transition=transition):
                bridge = _speech_bridge()
                bridge.speech_journal.record_speech("must not escape", "queued")
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
                    payload, status, _ = _get_speech(
                        bridge, "/speech?since=0", token="test-token"
                    )
                self.assertEqual(status, 403)
                self.assertNotIn("must not escape", str(payload))

    def test_speech_tool_is_marked_as_explicit_opt_in_during_discovery(self):
        metadata = bridge_module.NVDA_TOOLS_REGISTRY.get("wait_for_speech").get_metadata()
        constraints = NVDAHTTPBridge._compute_tool_constraints(metadata)
        self.assertTrue(constraints["requires_explicit_opt_in"])
    def test_cached_nvda_menu_identity_does_not_block_after_menu_closes(self):
        # NVDA caches the identified menu popup after it closes. A normal app
        # window in the foreground proves the menu is no longer active.
        self.assertFalse(
            _is_nvda_menu_open(("nvda-menu", 3), "Chrome_WidgetWin_1", 1234, 999)
        )

    def test_focused_nvda_menu_popup_keeps_menu_guard_active(self):
        self.assertTrue(_is_nvda_menu_open(("nvda-menu", 3), "#32768", 999, 999))

    def test_foreign_popup_does_not_count_as_the_nvda_menu(self):
        self.assertFalse(_is_nvda_menu_open(("nvda-menu", 3), "#32768", 1234, 999))

    def test_unidentified_menu_opening_remains_blocked_without_foreground(self):
        self.assertTrue(_is_nvda_menu_open(True, "", 0, 999))

    def test_stale_unidentified_identity_does_not_block_a_known_foreground(self):
        # A menu that failed to appear can leave ``True`` cached indefinitely.
        # Foreground evidence must win so input is not blocked until restart.
        self.assertFalse(
            _is_nvda_menu_open(True, "Chrome_WidgetWin_1", 1234, 999)
        )

    def test_missing_identity_is_never_menu_open(self):
        self.assertFalse(_is_nvda_menu_open(None, "#32768", 999, 999))

    def test_gui_thread_menu_mode_flags_identify_the_open_menu(self):
        # wx keeps the main frame as the foreground window while a popup menu is
        # open, so the native-popup class check alone is not sufficient.
        self.assertTrue(
            _is_nvda_menu_open(("nvda-menu", 3), "wxWindowNR", 999, 999, 0x14)
        )

    def test_closed_menu_with_cached_identity_and_main_frame_is_not_open(self):
        self.assertFalse(
            _is_nvda_menu_open(("nvda-menu", 3), "wxWindowNR", 999, 999, 0)
        )

    def test_validate_tools_discovery_payload_accepts_valid_payload(self):
        payload = {
            "protocol": PROTOCOL,
            "protocol_version": PROTOCOL_VERSION,
            "bridge_version": "1.0.0",
            "api_level": API_LEVEL,
            "types": {
                "ToolResponse": {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "type": "object",
                    "properties": {},
                }
            },
            "tools": [
                {
                    "name": "get_current_focus",
                    "description": "Get current focus",
                    "inputSchema": {},
                    "outputType": "ToolResponse",
                    "version": "1.0.0",
                    "category": "diagnostics",
                }
            ],
            "count": 1,
        }

        # Should not raise
        NVDABridgeRequestHandler._validate_tools_discovery_payload(payload)

    def test_build_tools_payload_contains_expected_fields(self):
        tools = [
            {
                "name": "get_current_focus",
                "description": "Get current focus",
                "inputSchema": {},
                "outputType": "ToolResponse",
                "version": "1.0.0",
                "category": "diagnostics",
            }
        ]
        types = {
            "ToolResponse": {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "type": "object",
                "properties": {},
            }
        }
        payload = NVDABridgeRequestHandler._build_tools_payload(None, tools, types)

        self.assertEqual(payload["protocol"], PROTOCOL)
        self.assertEqual(payload["protocol_version"], PROTOCOL_VERSION)
        self.assertEqual(payload["bridge_version"], bridge_module.BRIDGE_VERSION)
        self.assertEqual(payload["api_level"], API_LEVEL)
        self.assertEqual(payload["types"], types)
        self.assertEqual(payload["tools"], tools)
        self.assertEqual(payload["count"], 1)

    def test_check_action_allowed_blocks_modal_actions(self):
        bridge = NVDAHTTPBridge(host="127.0.0.1", port=0, bridge_token="test-token")
        error = bridge._check_action_allowed(
            request_id="req-1",
            action_name="move_focus",
            tool_category="focus",
            status_snapshot={"secure_screen": False, "modal_blocking": True, "menu_open": False},
            duration_ms=1.0,
        )

        self.assertIsNotNone(error)
        self.assertEqual(error["error"]["code"], 4202)
        self.assertEqual(error["action"], "move_focus")


if __name__ == "__main__":
    _ = unittest.main()
