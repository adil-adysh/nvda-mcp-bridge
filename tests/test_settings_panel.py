# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Regression tests for the NVDA Settings panel hosted by the add-on.

These tests exercise the panel against a minimal stand-in for NVDA's
gui/settingsDialogs module. They pin the lifecycle contract that NVDA's
MultiCategorySettingsDialog relies on: a category panel only becomes visible
when its ``onPanelActivated`` delegates to ``SettingsPanel``'s implementation
(which calls ``self.Show()``). Failing to do so leaves the
"NVDA MCP Bridge" settings page blank.

The add-on package is imported under a private alias (``nvda_mcp_gui_test``) so
that the GUI stubs installed here never leak into the other test modules.
"""

import importlib
import os
import sys
import types
import unittest
from unittest import mock

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
sys.modules.setdefault("logHandler", types.SimpleNamespace(log=_fake_logger))

_PACKAGE_NAME = "nvda_mcp_gui_test"
_STUBBED_MODULES = ("wx", "gui", "gui.settingsDialogs", "gui.guiHelper")


class _Stub:
    """Permissive wx widget stand-in: accepts any call and tolerates any attribute."""

    def __init__(self, *args, **kwargs):
        self._args = args
        self._kwargs = kwargs

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _build_fake_wx() -> types.ModuleType:
    module = types.ModuleType("wx")
    classes: dict[str, type] = {}

    def _attribute(name: str):
        if name.isupper():
            # Style flags / identifiers: stable integers so bitwise OR works.
            return 0x1000 + len(name)
        stub_class = classes.get(name)
        if stub_class is None:
            stub_class = type(name, (_Stub,), {})
            classes[name] = stub_class
        return stub_class

    module.__getattr__ = _attribute
    return module


def _build_fake_gui(wx: types.ModuleType) -> types.ModuleType:
    settings_dialogs = types.ModuleType("gui.settingsDialogs")

    class SettingsPanel(_Stub):
        """Stand-in for NVDA's base panel, mirroring its visibility contract."""

        title = ""

        def __init__(self, parent):
            self.parent = parent
            self.shown = False
            self.activation_count = 0
            self.deactivation_count = 0
            self.discard_count = 0
            self.layout_update_count = 0
            self.mainSizer = _Stub()
            self.settingsSizer = _Stub()
            self.makeSettings(self.settingsSizer)

        def onPanelActivated(self):
            self.activation_count += 1
            self.shown = True

        def onPanelDeactivated(self):
            self.deactivation_count += 1
            self.shown = False

        def onDiscard(self):
            self.discard_count += 1

        def _sendLayoutUpdatedEvent(self):
            self.layout_update_count += 1

    class NVDASettingsDialog:
        categoryClasses: list = []

    settings_dialogs.SettingsPanel = SettingsPanel
    settings_dialogs.NVDASettingsDialog = NVDASettingsDialog

    gui_helper = types.ModuleType("gui.guiHelper")

    class BoxSizerHelper:
        def __init__(self, parent, sizer=None, **kwargs):
            self.parent = parent
            self.sizer = sizer

        def addItem(self, item, *args, **kwargs):
            return item

    gui_helper.BoxSizerHelper = BoxSizerHelper

    gui = types.ModuleType("gui")
    gui.settingsDialogs = settings_dialogs
    gui.guiHelper = gui_helper
    gui.message_boxes = []

    def message_box(*args, **kwargs):
        gui.message_boxes.append((args, kwargs))
        return 0x1001

    gui.messageBox = message_box
    gui.mainFrame = None
    gui.wx = wx
    return gui


def _import_addon_gui_settings():
    """Import gui_settings with GUI stubs, then restore the real module table."""
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULES}
    fake_wx = _build_fake_wx()
    fake_gui = _build_fake_gui(fake_wx)
    sys.modules["wx"] = fake_wx
    sys.modules["gui"] = fake_gui
    sys.modules["gui.settingsDialogs"] = fake_gui.settingsDialogs
    sys.modules["gui.guiHelper"] = fake_gui.guiHelper

    if _PACKAGE_NAME in sys.modules:
        return sys.modules[f"{_PACKAGE_NAME}.gui_settings"]
    package = types.ModuleType(_PACKAGE_NAME)
    package.__path__ = [_ADDON_DIR]
    sys.modules[_PACKAGE_NAME] = package
    try:
        return importlib.import_module(f"{_PACKAGE_NAME}.gui_settings")
    finally:
        for name in _STUBBED_MODULES:
            if saved[name] is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved[name]


gui_settings = _import_addon_gui_settings()
from nvda_mcp_gui_test.server.security import PermissionPolicy  # noqa: E402
_wx = gui_settings.wx
_fake_gui = gui_settings.gui


class _FakeDeveloperModeManager:
    is_enabled = False
    time_remaining_seconds = 0.0


class _FakeBridge:
    def __init__(self, policy):
        self.permission_policy = policy
        self.developer_mode_manager = _FakeDeveloperModeManager()
        self.port = 8765


class _Toggle:
    def __init__(self, checked: bool):
        self._checked = checked

    def IsChecked(self) -> bool:
        return self._checked


class _Client:
    def __init__(self, client_id: str, name: str, has_conflict: bool = False):
        self.client_id = client_id
        self.name = name
        self.has_conflict = has_conflict


class TestSettingsPanelLifecycle(unittest.TestCase):
    def setUp(self):
        _fake_gui.message_boxes.clear()
        self.policy = PermissionPolicy()
        self.bridge = _FakeBridge(self.policy)
        with (
            mock.patch.object(
                gui_settings.MCPBridgeSettingsPanel,
                "_find_bridge",
                new=staticmethod(lambda: self.bridge),
            ),
            mock.patch.object(gui_settings, "get_bridge_status_summary", return_value="status"),
        ):
            self.panel = gui_settings.MCPBridgeSettingsPanel(parent=None)

    def test_activation_shows_the_panel(self):
        """The category page must become visible when NVDA activates it."""
        self.assertFalse(self.panel.shown)
        self.panel.onPanelActivated()
        self.assertTrue(self.panel.shown, "onPanelActivated must delegate to SettingsPanel so the panel is shown")
        self.assertEqual(1, self.panel.activation_count)

    def test_activation_refreshes_status_and_relayouts(self):
        self.panel.onPanelActivated()
        self.assertGreaterEqual(self.panel.layout_update_count, 1)

    def test_deactivation_hides_the_panel(self):
        self.panel.onPanelActivated()
        self.panel.onPanelDeactivated()
        self.assertFalse(self.panel.shown)
        self.assertEqual(1, self.panel.deactivation_count)

    def test_discard_delegates_to_base_panel(self):
        self.panel.onDiscard()
        self.assertEqual(1, self.panel.discard_count)

    def test_save_applies_permission_checkboxes(self):
        self.panel.inputControlCheckbox = _Toggle(True)
        self.panel.diagnosticsCheckbox = _Toggle(True)
        self.panel.lifecycleCheckbox = _Toggle(False)

        self.panel.onSave()

        self.assertTrue(self.policy.is_granted(gui_settings.PERMISSION_INPUT_CONTROL))
        self.assertTrue(self.policy.is_granted(gui_settings.PERMISSION_DIAGNOSTICS))
        self.assertFalse(self.policy.is_granted(gui_settings.PERMISSION_NVDA_LIFECYCLE))

    def test_save_without_bridge_does_not_raise(self):
        self.panel._bridge = None
        self.panel.onSave()

    def test_discard_restores_checkbox_state_from_policy(self):
        self.policy.grant(gui_settings.PERMISSION_DIAGNOSTICS)
        self.panel.diagnosticsCheckbox = _Stub()
        restored = {}
        self.panel.diagnosticsCheckbox.SetValue = lambda value: restored.setdefault("value", value)

        self.panel.onDiscard()

        self.assertTrue(restored["value"])


class TestConfigureClientsDialog(unittest.TestCase):
    def _make_dialog(self):
        dialog = object.__new__(gui_settings.ConfigureClientsDialog)
        dialog.client_checkboxes = [(_Client("vscode", "Visual Studio Code"), _Toggle(True))]
        dialog.overwrite_cb = None
        dialog.on_complete = None
        dialog.ended = []
        dialog.EndModal = dialog.ended.append
        return dialog

    def test_ok_shows_summary_and_closes(self):
        """Clicking 'Configure Selected' must not raise NameError for 'parent'."""
        _fake_gui.message_boxes.clear()
        dialog = self._make_dialog()
        with mock.patch.object(
            gui_settings,
            "configure_clients_workflow",
            return_value=(1, ["Visual Studio Code: configured"]),
        ):
            dialog.on_ok(None)

        self.assertTrue(_fake_gui.message_boxes, "the configuration summary should be shown")
        self.assertEqual([_wx.ID_OK], dialog.ended)


if __name__ == "__main__":
    unittest.main()

