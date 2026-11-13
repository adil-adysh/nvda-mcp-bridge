# pyright: reportMissingImports=false, reportUnusedCallResult=false

import importlib
import os
import sys
import types
import unittest

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


class HttpBridgeTests(unittest.TestCase):
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
