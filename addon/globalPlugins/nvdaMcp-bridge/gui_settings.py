"""Accessible NVDA Configuration Interface, Dialogs, and Menus for NVDA MCP Bridge.

Provides:
- An accessible NVDA Settings Panel (under NVDA Preferences -> Settings)
- NVDA Tools menu items ("NVDA MCP Bridge" submenu)
- Confirmation dialogs for developer mode authorization
- Interactive companion installation/repair and MCP client configuration
- Screen-reader announcements and status summaries
"""

from __future__ import annotations

import os
from typing import Any, Optional, Tuple

import logHandler

from .client_config import configure_client, detect_supported_clients
from .companion_manager import CompanionManager
from .server.security import (
	PERMISSION_DIAGNOSTICS,
	PERMISSION_INPUT_CONTROL,
	PERMISSION_NVDA_LIFECYCLE,
	PERMISSION_READ_ONLY,
	PERMISSION_SPEECH_OUTPUT,
)

# Defensive imports for headless / test environments
try:
	import wx
	import gui
	from gui import SettingsPanel, guiHelper, messageBox
	_GUI_AVAILABLE = True
except Exception:
	wx = None
	gui = None
	SettingsPanel = object
	guiHelper = None
	messageBox = None
	_GUI_AVAILABLE = False


def get_bridge_status_summary(bridge: Any) -> str:
	"""Generate a screen-reader-friendly text summary of bridge status."""
	if not bridge:
		return "NVDA MCP Bridge is not active."

	status = bridge.get_health_payload() if hasattr(bridge, "get_health_payload") else {}
	dev_mgr = getattr(bridge, "developer_mode_manager", None)
	policy = getattr(bridge, "permission_policy", None)

	dev_active = dev_mgr.is_enabled if dev_mgr else False
	time_left = int(dev_mgr.time_remaining_seconds) if (dev_mgr and dev_active) else 0

	granted = sorted(list(policy.get_granted())) if policy else []

	comp_mgr = CompanionManager()
	comp_status = comp_mgr.inspect_installed()

	lines = [
		f"NVDA MCP Bridge Status: Running (port {getattr(bridge, 'port', 8765)})",
		f"Developer Mode: {'Enabled (' + str(time_left) + 's remaining)' if dev_active else 'Disabled'}",
		f"Active Permissions: {', '.join(granted) if granted else 'None'}",
		f"Companion: {comp_status.status_message}",
	]
	return "\n".join(lines)


def enable_developer_mode_interactive(bridge: Any, parent: Any = None) -> bool:
	"""Prompt the user with an accessible confirmation dialog before enabling developer mode.

	Returns:
		True if enabled, False if declined or unavailable.
	"""
	if not bridge or not hasattr(bridge, "developer_mode_manager"):
		return False

	dev_mgr = bridge.developer_mode_manager
	policy = getattr(bridge, "permission_policy", None)

	if _GUI_AVAILABLE and wx and gui:
		title = "NVDA MCP Bridge — Enable Developer Mode"
		warning_msg = (
			"SECURITY WARNING:\n\n"
			"Enabling Developer Mode allows connected MCP clients to execute arbitrary Python code "
			"with the full process privileges of NVDA.\n\n"
			"Only enable this if you are actively debugging or writing tools with a trusted AI assistant.\n\n"
			"Developer mode will automatically expire and revoke execution privileges after 5 minutes (300 seconds).\n\n"
			"Do you want to enable Developer Mode now?"
		)
		resp = gui.messageBox(
			warning_msg,
			title,
			wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
			parent=parent,
		)
		if resp != wx.YES:
			return False

	dev_mgr.approve_locally(duration_seconds=300)
	if policy is not None:
		from .server.security import PERMISSION_DEVELOPER
		policy.grant(PERMISSION_DEVELOPER)

	try:
		import ui
		ui.message("NVDA MCP Bridge: Developer Mode enabled for 5 minutes.")
	except Exception:
		pass

	return True


def disable_developer_mode_interactive(bridge: Any) -> bool:
	"""Immediately disable developer mode and revoke privileges."""
	if not bridge or not hasattr(bridge, "developer_mode_manager"):
		return False

	dev_mgr = bridge.developer_mode_manager
	dev_mgr.disable()

	policy = getattr(bridge, "permission_policy", None)
	if policy is not None:
		from .server.security import PERMISSION_DEVELOPER
		policy.revoke(PERMISSION_DEVELOPER)

	try:
		import ui
		ui.message("NVDA MCP Bridge: Developer Mode disabled.")
	except Exception:
		pass

	return True


def install_companion_interactive(parent: Any = None) -> Tuple[bool, str]:
	"""Prompt and install or repair the bundled companion executable."""
	comp_mgr = CompanionManager()
	status = comp_mgr.inspect_installed()

	if _GUI_AVAILABLE and wx and gui:
		title = "NVDA MCP Bridge — Install Companion"
		prompt = (
			f"The NVDA MCP Companion (nvda-mcp-server.exe) enables MCP clients to communicate "
			f"with NVDA.\n\n"
			f"Destination: {comp_mgr.install_dir}\n"
			f"Current status: {status.status_message}\n\n"
			f"Would you like to install / repair the companion executable now?"
		)
		resp = gui.messageBox(prompt, title, wx.YES_NO | wx.ICON_QUESTION, parent=parent)
		if resp != wx.YES:
			return False, "Installation cancelled by user"

	success, msg = comp_mgr.install_or_repair(force=True)
	if _GUI_AVAILABLE and wx and gui:
		icon = wx.ICON_INFORMATION if success else wx.ICON_ERROR
		gui.messageBox(msg, "NVDA MCP Bridge — Companion Installation", icon, parent=parent)

	try:
		import ui
		ui.message(msg)
	except Exception:
		pass

	return success, msg


def configure_clients_interactive(parent: Any = None) -> Tuple[bool, str]:
	"""Detect installed MCP clients and configure them to use the companion executable."""
	comp_mgr = CompanionManager()
	status = comp_mgr.inspect_installed()

	if not status.installed or not comp_mgr.installed_exe_path.is_file():
		msg = "Please install the MCP Companion first before configuring MCP clients."
		if _GUI_AVAILABLE and wx and gui:
			gui.messageBox(msg, "NVDA MCP Bridge", wx.ICON_WARNING, parent=parent)
		return False, msg

	clients = detect_supported_clients(expected_companion_exe=comp_mgr.installed_exe_path)
	detected = [c for c in clients if c.detected]

	if not detected:
		msg = "No supported MCP clients (VS Code, Claude Desktop, Cursor) were detected."
		if _GUI_AVAILABLE and wx and gui:
			gui.messageBox(msg, "NVDA MCP Bridge", wx.ICON_INFORMATION, parent=parent)
		return False, msg

	configured_count = 0
	messages = []

	for client in detected:
		success, info = configure_client(
			client_id=client.client_id,
			companion_exe_path=comp_mgr.installed_exe_path,
			overwrite_conflict=True,
		)
		if success:
			configured_count += 1
		messages.append(f"{client.name}: {info}")

	summary = "\n".join(messages)
	if _GUI_AVAILABLE and wx and gui:
		title = f"NVDA MCP Bridge — Configured {configured_count} Client(s)"
		gui.messageBox(summary, title, wx.ICON_INFORMATION, parent=parent)

	try:
		import ui
		ui.message(f"Configured {configured_count} MCP client(s).")
	except Exception:
		pass

	return configured_count > 0, summary


if _GUI_AVAILABLE:
	class MCPBridgeSettingsPanel(gui.SettingsPanel):
		"""Accessible settings panel in NVDA Preferences -> Settings."""

		# Translators: Title of the settings panel in NVDA settings
		title = "NVDA MCP Bridge"

		def __init__(self, parent):
			super().__init__(parent)
			self._bridge = None
			self._find_bridge()

		def _find_bridge(self):
			try:
				import globalPluginHandler
				for plugin in globalPluginHandler.runningPlugins:
					if getattr(plugin, "_bridge", None):
						self._bridge = plugin._bridge
						break
			except Exception:
				pass

		def makeSettings(self, settingsSizer):
			sHelper = guiHelper.BoxSizerHelper(self, sizer=settingsSizer)

			# 1. Bridge Status Label
			status_text = get_bridge_status_summary(self._bridge)
			self.statusLabel = sHelper.addItem(
				wx.StaticText(self, label=status_text)
			)

			# 2. Permissions Group
			permGroup = sHelper.addItem(wx.StaticBox(self, label="Capability Permissions"))
			permSizer = wx.StaticBoxSizer(permGroup, wx.VERTICAL)

			policy = getattr(self._bridge, "permission_policy", None)

			self.inputControlCheckbox = wx.CheckBox(
				self,
				label="Allow MCP Input Control (send keyboard gestures)",
			)
			if policy:
				self.inputControlCheckbox.SetValue(policy.is_granted(PERMISSION_INPUT_CONTROL))
			permSizer.Add(self.inputControlCheckbox, 0, wx.ALL, 5)

			self.diagnosticsCheckbox = wx.CheckBox(
				self,
				label="Allow MCP Diagnostic Inspection (logs, crash dumps)",
			)
			if policy:
				self.diagnosticsCheckbox.SetValue(policy.is_granted(PERMISSION_DIAGNOSTICS))
			permSizer.Add(self.diagnosticsCheckbox, 0, wx.ALL, 5)

			self.lifecycleCheckbox = wx.CheckBox(
				self,
				label="Allow MCP NVDA Lifecycle actions (restart NVDA, reload plugins)",
			)
			if policy:
				self.lifecycleCheckbox.SetValue(policy.is_granted(PERMISSION_NVDA_LIFECYCLE))
			permSizer.Add(self.lifecycleCheckbox, 0, wx.ALL, 5)

			settingsSizer.Add(permSizer, 0, wx.EXPAND | wx.ALL, 5)

			# 3. Developer Mode Controls
			devGroup = sHelper.addItem(wx.StaticBox(self, label="Developer Mode (Arbitrary Python Execution)"))
			devSizer = wx.StaticBoxSizer(devGroup, wx.VERTICAL)

			devBtnSizer = wx.BoxSizer(wx.HORIZONTAL)
			self.enableDevBtn = wx.Button(self, label="Enable Developer Mode (5 min)...")
			self.enableDevBtn.Bind(wx.EVT_BUTTON, self.onEnableDev)
			devBtnSizer.Add(self.enableDevBtn, 0, wx.RIGHT, 5)

			self.disableDevBtn = wx.Button(self, label="Disable Developer Mode")
			self.disableDevBtn.Bind(wx.EVT_BUTTON, self.onDisableDev)
			devBtnSizer.Add(self.disableDevBtn, 0, wx.RIGHT, 5)

			devSizer.Add(devBtnSizer, 0, wx.ALL, 5)
			settingsSizer.Add(devSizer, 0, wx.EXPAND | wx.ALL, 5)

			# 4. Companion & MCP Clients Setup
			compGroup = sHelper.addItem(wx.StaticBox(self, label="Companion & MCP Clients"))
			compSizer = wx.StaticBoxSizer(compGroup, wx.VERTICAL)

			compBtnSizer = wx.BoxSizer(wx.HORIZONTAL)
			self.installCompBtn = wx.Button(self, label="Install or Repair MCP Companion...")
			self.installCompBtn.Bind(wx.EVT_BUTTON, self.onInstallCompanion)
			compBtnSizer.Add(self.installCompBtn, 0, wx.RIGHT, 5)

			self.configClientsBtn = wx.Button(self, label="Configure MCP Clients...")
			self.configClientsBtn.Bind(wx.EVT_BUTTON, self.onConfigClients)
			compBtnSizer.Add(self.configClientsBtn, 0, wx.RIGHT, 5)

			compSizer.Add(compBtnSizer, 0, wx.ALL, 5)
			settingsSizer.Add(compSizer, 0, wx.EXPAND | wx.ALL, 5)

			# 5. Reset Permissions Button
			self.resetPermsBtn = sHelper.addItem(
				wx.Button(self, label="Reset Permissions to Safe Defaults")
			)
			self.resetPermsBtn.Bind(wx.EVT_BUTTON, self.onResetPermissions)

		def onEnableDev(self, event):
			enable_developer_mode_interactive(self._bridge, parent=self)
			self._refresh_status()

		def onDisableDev(self, event):
			disable_developer_mode_interactive(self._bridge)
			self._refresh_status()

		def onResetPermissions(self, event):
			policy = getattr(self._bridge, "permission_policy", None)
			if policy:
				policy.reset()
				self.inputControlCheckbox.SetValue(policy.is_granted(PERMISSION_INPUT_CONTROL))
				self.diagnosticsCheckbox.SetValue(policy.is_granted(PERMISSION_DIAGNOSTICS))
				self.lifecycleCheckbox.SetValue(policy.is_granted(PERMISSION_NVDA_LIFECYCLE))
			self._refresh_status()

		def onInstallCompanion(self, event):
			install_companion_interactive(parent=self)
			self._refresh_status()

		def onConfigClients(self, event):
			configure_clients_interactive(parent=self)
			self._refresh_status()

		def _refresh_status(self):
			if hasattr(self, "statusLabel"):
				self.statusLabel.SetLabel(get_bridge_status_summary(self._bridge))
				self.Layout()

		def onSave(self):
			policy = getattr(self._bridge, "permission_policy", None)
			if not policy:
				return

			if self.inputControlCheckbox.IsChecked():
				policy.grant(PERMISSION_INPUT_CONTROL)
			else:
				policy.revoke(PERMISSION_INPUT_CONTROL)

			if self.diagnosticsCheckbox.IsChecked():
				policy.grant(PERMISSION_DIAGNOSTICS)
			else:
				policy.revoke(PERMISSION_DIAGNOSTICS)

			if self.lifecycleCheckbox.IsChecked():
				policy.grant(PERMISSION_NVDA_LIFECYCLE)
			else:
				policy.revoke(PERMISSION_NVDA_LIFECYCLE)

			logHandler.log.info("Saved NVDA MCP Bridge permission configuration")
else:
	MCPBridgeSettingsPanel = None  # type: ignore[assignment]


_menu_item = None
_submenu = None


def install_menu_items(plugin: Any) -> None:
	"""Add NVDA MCP Bridge submenu to NVDA Tools menu."""
	global _menu_item, _submenu
	if not _GUI_AVAILABLE or not wx or not gui or not hasattr(gui, "mainFrame"):
		return

	try:
		tools_menu = gui.mainFrame.sysTrayIcon.toolsMenu
		_submenu = wx.Menu()

		# 1. Status
		item_status = _submenu.Append(wx.ID_ANY, "MCP Bridge Status...")
		gui.mainFrame.sysTrayIcon.Bind(
			wx.EVT_MENU,
			lambda evt: gui.messageBox(
				get_bridge_status_summary(plugin._bridge),
				"NVDA MCP Bridge Status",
				wx.ICON_INFORMATION,
			),
			item_status,
		)

		# 2. Enable Dev Mode
		item_dev_on = _submenu.Append(wx.ID_ANY, "Enable Developer Mode (5 minutes)...")
		gui.mainFrame.sysTrayIcon.Bind(
			wx.EVT_MENU,
			lambda evt: enable_developer_mode_interactive(plugin._bridge),
			item_dev_on,
		)

		# 3. Disable Dev Mode
		item_dev_off = _submenu.Append(wx.ID_ANY, "Disable Developer Mode")
		gui.mainFrame.sysTrayIcon.Bind(
			wx.EVT_MENU,
			lambda evt: disable_developer_mode_interactive(plugin._bridge),
			item_dev_off,
		)

		_submenu.AppendSeparator()

		# 4. Install Companion
		item_comp = _submenu.Append(wx.ID_ANY, "Install or Repair MCP Companion...")
		gui.mainFrame.sysTrayIcon.Bind(
			wx.EVT_MENU,
			lambda evt: install_companion_interactive(),
			item_comp,
		)

		# 5. Configure Clients
		item_clients = _submenu.Append(wx.ID_ANY, "Configure MCP Clients...")
		gui.mainFrame.sysTrayIcon.Bind(
			wx.EVT_MENU,
			lambda evt: configure_clients_interactive(),
			item_clients,
		)

		_menu_item = tools_menu.AppendSubMenu(_submenu, "NVDA MCP Bridge")
		logHandler.log.info("Added NVDA MCP Bridge tools menu")
	except Exception as e:
		logHandler.log.warning(f"Could not register NVDA MCP Bridge tools menu: {e}")


def remove_menu_items(plugin: Any) -> None:
	"""Remove NVDA MCP Bridge menu from NVDA Tools menu."""
	global _menu_item, _submenu
	if not _GUI_AVAILABLE or not wx or not gui or not hasattr(gui, "mainFrame") or not _menu_item:
		return

	try:
		tools_menu = gui.mainFrame.sysTrayIcon.toolsMenu
		tools_menu.Remove(_menu_item.GetId())
		_menu_item = None
		_submenu = None
	except Exception as e:
		logHandler.log.debug(f"Error removing tools menu item: {e}")
