# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Unit tests for Developer Authorization lifecycle and UI interactions."""

import os
import sys
import time
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

_queue_handler = types.ModuleType("queueHandler")
_queue_handler.eventQueue = "dummy-queue"
_queue_handler.queueFunction = lambda q, fn, *args, **kwargs: fn(*args, **kwargs)
sys.modules["queueHandler"] = _queue_handler

for mod_name in (
    "api",
    "ui",
    "speech",
    "keyboardHandler",
    "tones",
    "core",
    "versionInfo",
    "globalVars",
    "textInfos",
    "controlTypes",
    "speechDictHandler",
    "addonHandler",
    "config",
    "winUser",
):
    if mod_name not in sys.modules:
        sys.modules[mod_name] = types.ModuleType(mod_name)

from nvdaMcpBridge.server.http_bridge import NVDAHTTPBridge  # noqa: E402
from nvdaMcpBridge.server.security import (  # noqa: E402
    PERMISSION_DEVELOPER,
    PERMISSION_INPUT_CONTROL,
    DeveloperModeManager,
    PermissionPolicy,
)
from nvdaMcpBridge.gui_settings import (  # noqa: E402
    disable_developer_mode_interactive,
    enable_developer_mode_interactive,
    get_bridge_status_summary,
)


class TestDeveloperAuthorizationUI(unittest.TestCase):
    def setUp(self):
        self.policy = PermissionPolicy()
        self.bridge = NVDAHTTPBridge(
            bridge_token="test-token",
            permission_policy=self.policy,
            developer_mode=False,
        )

    def test_default_state_is_disabled(self):
        """Verify developer mode is disabled by default and capability is not granted."""
        self.assertFalse(self.bridge.is_developer_mode_enabled)
        self.assertFalse(self.policy.is_granted(PERMISSION_DEVELOPER))

    def test_interactive_enable_grants_developer_capability_atomically(self):
        """Verify enabling developer mode grants developer capability atomically."""
        enabled = enable_developer_mode_interactive(self.bridge)
        self.assertTrue(enabled)
        self.assertTrue(self.bridge.is_developer_mode_enabled)
        self.assertTrue(self.policy.is_granted(PERMISSION_DEVELOPER))
        self.assertGreater(self.bridge.developer_mode_manager.time_remaining_seconds, 0)

    def test_immediate_disable_revokes_developer_capability(self):
        """Verify disabling developer mode immediately revokes developer capability."""
        enable_developer_mode_interactive(self.bridge)
        self.assertTrue(self.policy.is_granted(PERMISSION_DEVELOPER))

        disabled = disable_developer_mode_interactive(self.bridge)
        self.assertTrue(disabled)
        self.assertFalse(self.bridge.is_developer_mode_enabled)
        self.assertFalse(self.policy.is_granted(PERMISSION_DEVELOPER))

    def test_developer_mode_ttl_auto_expiration_revokes_capability(self):
        """Verify developer mode expiration automatically revokes the capability."""
        # Enable with short 0.05s TTL
        self.bridge.developer_mode_manager.approve_locally(duration_seconds=1)
        # Manually backdate expiration to simulate time passing
        self.bridge.developer_mode_manager._expires_at = time.monotonic() - 0.01

        self.assertFalse(self.bridge.is_developer_mode_enabled)
        self.assertFalse(self.policy.is_granted(PERMISSION_DEVELOPER))

    def test_reset_permissions_resets_to_defaults(self):
        """Verify reset revokes non-default permissions and developer capability."""
        self.policy.grant(PERMISSION_INPUT_CONTROL)
        enable_developer_mode_interactive(self.bridge)
        self.assertTrue(self.policy.is_granted(PERMISSION_INPUT_CONTROL))
        self.assertTrue(self.policy.is_granted(PERMISSION_DEVELOPER))

        self.bridge.reset_permissions()
        self.assertFalse(self.policy.is_granted(PERMISSION_INPUT_CONTROL))

    def test_status_summary_contains_key_elements(self):
        """Verify get_bridge_status_summary contains bridge status and developer mode info."""
        summary = get_bridge_status_summary(self.bridge)
        self.assertIn("NVDA MCP Bridge Status", summary)
        self.assertIn("Developer Mode: Disabled", summary)
        self.assertIn("Active Permissions", summary)


if __name__ == "__main__":
    unittest.main()
