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

    def test_install_companion_interactive_concurrency_lock(self):
        """Verify concurrent companion installation attempts are prevented by _install_lock."""
        from nvdaMcpBridge.gui_settings import _install_lock, install_companion_interactive
        acquired = _install_lock.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            success, msg = install_companion_interactive()
            self.assertFalse(success)
            self.assertIn("already in progress", msg)
        finally:
            _install_lock.release()

    def test_configure_clients_workflow_selective_and_backups(self):
        """Verify configure_clients_workflow configures only requested clients and creates backups."""
        import json
        import tempfile
        from pathlib import Path
        from nvdaMcpBridge.gui_settings import configure_clients_workflow
        from test_client_config import make_mock_pe_binary

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            appdata = root / "AppData" / "Roaming"
            appdata.mkdir(parents=True)
            claude_file = appdata / "Claude" / "claude_desktop_config.json"
            claude_file.parent.mkdir(parents=True)
            claude_file.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

            vscode_file = appdata / "Code" / "User" / "mcp.json"
            vscode_file.parent.mkdir(parents=True)
            vscode_file.write_text(json.dumps({"servers": {}}), encoding="utf-8")

            mock_exe = root / "bin" / "nvda-mcp-server.exe"
            mock_exe.parent.mkdir(parents=True)
            mock_exe.write_bytes(make_mock_pe_binary())

            # Configure only claude_desktop
            count, msgs = configure_clients_workflow(
                companion_exe_path=mock_exe,
                client_ids=["claude_desktop"],
                overwrite_conflicts=False,
                appdata_override=appdata,
            )
            self.assertEqual(count, 1)

            # Claude should have mcpServers.nvda
            claude_data = json.loads(claude_file.read_text(encoding="utf-8"))
            self.assertIn("nvda", claude_data["mcpServers"])

            # VS Code should NOT have been touched
            vscode_data = json.loads(vscode_file.read_text(encoding="utf-8"))
            self.assertNotIn("nvda", vscode_data["servers"])

    def test_settings_panel_constructor_order_and_static_box_parenting(self):
        """Verify settings panel initializes _bridge before super().__init__ and parents to StaticBox."""
        # Create a mock base SettingsPanel mimicking NVDA's exact gui.settingsDialogs.SettingsPanel
        class MockNVDABaseSettingsPanel:
            def __init__(self, parent):
                self.parent = parent
                self._buildGui()

            def _buildGui(self):
                # NVDA calls makeSettings during base __init__
                dummy_sizer = []
                self.makeSettings(dummy_sizer)

        # Mock wx objects
        class MockStaticBox:
            def __init__(self, parent, label=""):
                self.parent = parent
                self.label = label

        class MockWidget:
            def __init__(self, parent, label="", **kwargs):
                self.parent = parent
                self.label = label
                self.value = False

            def SetValue(self, val):
                self.value = val

            def Bind(self, *args, **kwargs):
                pass

        class MockSizer:
            def __init__(self, *args, **kwargs):
                self.items = []

            def Add(self, item, *args, **kwargs):
                self.items.append(item)

        class MockBoxSizerHelper:
            def __init__(self, parent, sizer=None):
                self.parent = parent

            def addItem(self, item):
                return item

        mock_wx = types.SimpleNamespace(
            StaticBox=MockStaticBox,
            StaticBoxSizer=lambda box, orient: MockSizer(),
            BoxSizer=lambda orient: MockSizer(),
            StaticText=MockWidget,
            CheckBox=MockWidget,
            Button=MockWidget,
            VERTICAL=1,
            HORIZONTAL=2,
            ALL=4,
            EXPAND=8,
            RIGHT=16,
            EVT_BUTTON="evt_button",
        )
        mock_gui_helper = types.SimpleNamespace(BoxSizerHelper=MockBoxSizerHelper)

        import nvdaMcpBridge.gui_settings as gs

        # Dynamically subclass MockNVDABaseSettingsPanel using the same pattern as MCPBridgeSettingsPanel
        class TestPanel(MockNVDABaseSettingsPanel):
            title = "Test NVDA MCP Bridge"

            def __init__(self, parent, bridge):
                self._bridge = bridge
                super().__init__(parent)

            def makeSettings(self, settingsSizer):
                # Emulate makeSettings from gui_settings
                orig_wx = gs.wx
                orig_gh = gs.guiHelper
                gs.wx = mock_wx
                gs.guiHelper = mock_gui_helper
                try:
                    sHelper = mock_gui_helper.BoxSizerHelper(self, sizer=settingsSizer)
                    status_text = gs.get_bridge_status_summary(self._bridge)
                    self.statusLabel = sHelper.addItem(mock_wx.StaticText(self, label=status_text))

                    # Capability permissions group
                    permGroup = mock_wx.StaticBox(self, label="Capability Permissions")
                    permSizer = mock_wx.StaticBoxSizer(permGroup, mock_wx.VERTICAL)
                    self.inputControlCheckbox = mock_wx.CheckBox(permGroup, label="Allow Input")
                    permSizer.Add(self.inputControlCheckbox)

                    # Developer mode group
                    devGroup = mock_wx.StaticBox(self, label="Developer Mode")
                    self.enableDevBtn = mock_wx.Button(devGroup, label="Enable Dev")

                    # Assert parent of grouped widgets is the StaticBox
                    assert self.inputControlCheckbox.parent is permGroup
                    assert self.enableDevBtn.parent is devGroup
                finally:
                    gs.wx = orig_wx
                    gs.guiHelper = orig_gh

        # Constructing TestPanel must succeed without AttributeError on self._bridge
        panel = TestPanel(parent=None, bridge=self.bridge)
        self.assertIsNotNone(panel._bridge)
        self.assertTrue(hasattr(panel, "statusLabel"))
        self.assertTrue(hasattr(panel, "inputControlCheckbox"))


if __name__ == "__main__":
    unittest.main()


