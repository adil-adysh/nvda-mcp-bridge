"""Accessible NVDA GUI controls and Settings Panel for NVDA MCP Bridge.

Provides accessible wxPython interfaces conforming to NVDA GUI standards:
- Settings Panel under NVDA Preferences -> Settings -> NVDA MCP Bridge
- NVDA Tools menu submenu with keyboard access
- Interactive developer authorization with confirmation dialog and auto-expiration
- Non-blocking, threaded companion installation and repair with rollback protection
- User-controlled MCP client configuration dialog with preview and explicit conflict approval
- Full screen reader speech feedback via ui.message()
- Compliant wx.StaticBox child widget parenting preventing layout and clipping defects
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import logHandler

from .client_config import (
	ClientInfo,
	ConfigPreview,
	configure_client,
	detect_supported_clients,
	format_preview_text,
	preview_client_configuration,
)
from .companion_manager import CompanionManager
from .server.security import (
	PERMISSION_DEVELOPER,
	PERMISSION_DIAGNOSTICS,
	PERMISSION_INPUT_CONTROL,
	PERMISSION_NVDA_LIFECYCLE,
	PERMISSION_READ_ONLY,
	PERMISSION_SPEECH_OUTPUT,
)

# Defensive imports conforming to official NVDA settingsDialogs architecture
try:
	import wx
	import gui
	from gui import guiHelper, messageBox
	try:
		from gui.settingsDialogs import NVDASettingsDialog, SettingsPanel
	except (ImportError, AttributeError):
		SettingsPanel = getattr(gui, "SettingsPanel", object)
		NVDASettingsDialog = getattr(gui, "NVDASettingsDialog", object)
	_GUI_AVAILABLE = True
except Exception:
	wx = None
	gui = None
	SettingsPanel = object
	NVDASettingsDialog = object
	guiHelper = None
	messageBox = None
	_GUI_AVAILABLE = False


_install_lock = threading.Lock()


def get_bridge_status_summary(bridge: Any) -> str:
	"""Generate a screen-reader-friendly text summary of bridge status."""
	if not bridge:
		return "NVDA MCP Bridge is not active."

	dev_mgr = getattr(bridge, "developer_mode_manager", None)
	policy = getattr(bridge, "permission_policy", None)

	dev_active = dev_mgr.is_enabled if dev_mgr else False
	time_left = int(dev_mgr.time_remaining_seconds) if (dev_mgr and dev_active) else 0

	granted = sorted(list(policy.get_granted())) if policy else []

	comp_mgr = CompanionManager()
	comp_status = comp_mgr.inspect_installed(verify_hash=False)

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
		policy.revoke(PERMISSION_DEVELOPER)

	try:
		import ui
		ui.message("NVDA MCP Bridge: Developer Mode disabled.")
	except Exception:
		pass

	return True


def install_companion_interactive(
	parent: Any = None,
	on_done: Optional[Callable[[bool, str], None]] = None,
) -> Tuple[bool, str]:
	"""Prompt and install or repair the bundled companion executable off the GUI thread.

	Runs file I/O and SHA-256 calculation in a background thread to prevent blocking
	the NVDA GUI loop, marshaling completion dialogs and speech back safely.
	"""
	comp_mgr = CompanionManager()
	status = comp_mgr.inspect_installed(verify_hash=False)

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

	if not _install_lock.acquire(blocking=False):
		msg = "Companion installation is already in progress. Please wait for it to complete."
		if _GUI_AVAILABLE and wx and gui:
			gui.messageBox(msg, "NVDA MCP Bridge", wx.ICON_WARNING, parent=parent)
		return False, msg

	def _worker():
		try:
			success, msg = comp_mgr.install_or_repair(force=True)
		except Exception as e:
			success, msg = False, f"Unexpected error during companion installation: {e}"
		finally:
			_install_lock.release()

		def _on_finish():
			if _GUI_AVAILABLE and wx and gui:
				icon = wx.ICON_INFORMATION if success else wx.ICON_ERROR
				gui.messageBox(msg, "NVDA MCP Bridge — Companion Installation", icon, parent=parent)
			try:
				import ui
				ui.message(msg)
			except Exception:
				pass
			if on_done:
				try:
					on_done(success, msg)
				except Exception:
					pass

		if _GUI_AVAILABLE and wx:
			wx.CallAfter(_on_finish)
		else:
			_on_finish()

	if _GUI_AVAILABLE:
		t = threading.Thread(target=_worker, name="NVDA-MCP-Companion-Install", daemon=True)
		t.start()
		return True, "Installation started in background"
	else:
		_worker()
		return True, "Installed synchronously"


def configure_clients_workflow(
	companion_exe_path: Path,
	client_ids: Optional[List[str]] = None,
	overwrite_conflicts: bool = False,
	appdata_override: Optional[Path] = None,
	userprofile_override: Optional[Path] = None,
) -> Tuple[int, List[str]]:
	"""Execute client configuration non-interactively for specified or detected clients.

	Returns:
		Tuple of (configured_count: int, messages: List[str])
	"""
	clients = detect_supported_clients(
		appdata_override=appdata_override,
		userprofile_override=userprofile_override,
		expected_companion_exe=companion_exe_path,
	)
	detected = [c for c in clients if c.detected]

	if client_ids is not None:
		targets = [c for c in detected if c.client_id in client_ids]
	else:
		targets = [c for c in detected if not c.configured and (not c.has_conflict or overwrite_conflicts)]

	configured_count = 0
	messages = []

	for client in targets:
		success, info = configure_client(
			client_id=client.client_id,
			companion_exe_path=companion_exe_path,
			overwrite_conflict=overwrite_conflicts,
			appdata_override=appdata_override,
			userprofile_override=userprofile_override,
		)
		if success:
			configured_count += 1
		messages.append(f"{client.name}: {info}")

	return configured_count, messages


if _GUI_AVAILABLE and wx:
	class ConfigureClientsDialog(wx.Dialog):
		"""Accessible dialog allowing user selection, diff preview, and explicit conflict resolution."""

		def __init__(
			self,
			parent: Any,
			clients: List[ClientInfo],
			companion_exe: Path,
			on_complete: Optional[Callable[[int, List[str]], None]] = None,
		):
			super().__init__(
				parent,
				title="NVDA MCP Bridge — Configure MCP Clients",
				style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
			)
			self.clients = clients
			self.companion_exe = companion_exe
			self.on_complete = on_complete
			self.client_checkboxes: List[Tuple[ClientInfo, wx.CheckBox]] = []
			self._init_ui()

		def _init_ui(self):
			main_sizer = wx.BoxSizer(wx.VERTICAL)

			# Prompt text
			instructions = wx.StaticText(
				self,
				label=(
					"Select the MCP clients you want to configure to use the NVDA MCP Companion.\n"
					"Existing settings and other MCP servers will be preserved. Backups are created automatically."
				),
			)
			main_sizer.Add(instructions, 0, wx.ALL | wx.EXPAND, 10)

			# Clients static box with proper StaticBox child parenting
			clients_box = wx.StaticBox(self, label="Detected MCP Clients")
			clients_sizer = wx.StaticBoxSizer(clients_box, wx.VERTICAL)

			has_conflicts = any(c.has_conflict for c in self.clients)

			for client in self.clients:
				if client.configured:
					status_label = "Already configured"
					default_check = False
				elif client.has_conflict:
					status_label = f"CONFLICT: points to '{client.existing_command}'"
					default_check = False
				else:
					status_label = "Not configured"
					default_check = True

				# Proper parenting to the StaticBox
				cb = wx.CheckBox(
					clients_box,
					label=f"{client.name} — [{status_label}]",
				)
				cb.SetValue(default_check)
				clients_sizer.Add(cb, 0, wx.ALL | wx.EXPAND, 5)
				self.client_checkboxes.append((client, cb))

			main_sizer.Add(clients_sizer, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)

			# Overwrite conflict confirmation
			if has_conflicts:
				self.overwrite_cb = wx.CheckBox(
					self,
					label="Allow replacing conflicting 'nvda' server definitions in selected clients",
				)
				self.overwrite_cb.SetValue(False)
				main_sizer.Add(self.overwrite_cb, 0, wx.ALL | wx.EXPAND, 10)
			else:
				self.overwrite_cb = None

			# Buttons sizer
			btn_sizer = wx.BoxSizer(wx.HORIZONTAL)

			self.preview_btn = wx.Button(self, label="Preview Changes...")
			self.preview_btn.Bind(wx.EVT_BUTTON, self.on_preview)
			btn_sizer.Add(self.preview_btn, 0, wx.RIGHT, 10)

			btn_sizer.AddStretchSpacer(1)

			self.ok_btn = wx.Button(self, wx.ID_OK, label="Configure Selected")
			self.ok_btn.Bind(wx.EVT_BUTTON, self.on_ok)
			btn_sizer.Add(self.ok_btn, 0, wx.RIGHT, 5)

			self.cancel_btn = wx.Button(self, wx.ID_CANCEL, label="Cancel")
			btn_sizer.Add(self.cancel_btn, 0)

			main_sizer.Add(btn_sizer, 0, wx.ALL | wx.EXPAND, 10)
			self.SetSizerAndFit(main_sizer)
			self.CenterOnParent()

		def on_preview(self, event):
			selected = [c for c, cb in self.client_checkboxes if cb.IsChecked()]
			if not selected:
				gui.messageBox(
					"Please select at least one client to preview changes.",
					"Configuration Preview",
					wx.ICON_INFORMATION,
					parent=self,
				)
				return

			preview_sections = []
			for client in selected:
				ok, prev, err = preview_client_configuration(
					client_id=client.client_id,
					companion_exe_path=self.companion_exe,
				)
				if ok:
					preview_sections.append(format_preview_text(prev))
				else:
					preview_sections.append(f"{client.name}: {err}")

			preview_summary = "\n\n" + ("=" * 40) + "\n\n".join(preview_sections)
			gui.messageBox(
				preview_summary,
				"NVDA MCP Bridge — Configuration Preview",
				wx.ICON_INFORMATION,
				parent=self,
			)

		def on_ok(self, event):
			selected = [c for c, cb in self.client_checkboxes if cb.IsChecked()]
			if not selected:
				gui.messageBox(
					"No clients were selected to configure.",
					"NVDA MCP Bridge",
					wx.ICON_WARNING,
					parent=self,
				)
				return

			allow_overwrite = bool(self.overwrite_cb and self.overwrite_cb.IsChecked())
			conflicting_selected = [c for c in selected if c.has_conflict]

			if conflicting_selected and not allow_overwrite:
				names = ", ".join(c.name for c in conflicting_selected)
				gui.messageBox(
					f"The following client(s) contain conflicting server entries:\n{names}\n\n"
					"To replace them, please check 'Allow replacing conflicting nvda server definitions' "
					"or deselect those clients.",
					"Conflict Confirmation Required",
					wx.ICON_WARNING,
					parent=self,
				)
				return

			selected_ids = [c.client_id for c in selected]
			configured_count, messages = configure_clients_workflow(
				companion_exe_path=self.companion_exe,
				client_ids=selected_ids,
				overwrite_conflicts=allow_overwrite,
			)

			summary = "\n".join(messages)
			title = f"NVDA MCP Bridge — Configured {configured_count} Client(s)"
			gui.messageBox(summary, title, wx.ICON_INFORMATION, parent=self)

			try:
				import ui
				ui.message(f"Configured {configured_count} MCP client(s).")
			except Exception:
				pass

			if self.on_complete:
				try:
					self.on_complete(configured_count, messages)
				except Exception:
					pass

			self.EndModal(wx.ID_OK)


def configure_clients_interactive(parent: Any = None) -> Tuple[bool, str]:
	"""Detect installed MCP clients and launch accessible selection and configuration workflow."""
	comp_mgr = CompanionManager()
	status = comp_mgr.inspect_installed(verify_hash=False)

	if not status.installed or not comp_mgr.installed_exe_path.is_file():
		msg = "Please install the MCP Companion first before configuring MCP clients."
		if _GUI_AVAILABLE and wx and gui:
			gui.messageBox(msg, "NVDA MCP Bridge", wx.ICON_WARNING, parent=parent)
		return False, msg

	clients = detect_supported_clients(expected_companion_exe=comp_mgr.installed_exe_path)
	detected = [c for c in clients if c.detected]

	if not detected:
		msg = "No supported MCP clients (VS Code, Claude Desktop, Cursor, Antigravity) were detected."
		if _GUI_AVAILABLE and wx and gui:
			gui.messageBox(msg, "NVDA MCP Bridge", wx.ICON_INFORMATION, parent=parent)
		return False, msg

	if _GUI_AVAILABLE and wx:
		dlg = ConfigureClientsDialog(
			parent=parent,
			clients=detected,
			companion_exe=comp_mgr.installed_exe_path,
		)
		dlg.ShowModal()
		dlg.Destroy()
		return True, "Interactive client configuration completed"
	else:
		count, msgs = configure_clients_workflow(
			companion_exe_path=comp_mgr.installed_exe_path,
			overwrite_conflicts=False,
		)
		return count > 0, "\n".join(msgs)


if _GUI_AVAILABLE:
	class MCPBridgeSettingsPanel(SettingsPanel):
		"""Accessible settings panel in NVDA Preferences -> Settings.

		Conforms strictly to the official NVDA SettingsPanel lifecycle:
		- Initializes _bridge reference BEFORE calling super().__init__()
		- Supports onPanelActivated() for dynamic status refreshes
		- Correctly parents grouped controls to wx.StaticBox
		- Implements onSave() and onDiscard()
		"""

		# Translators: Title of the settings panel in NVDA settings
		title = "NVDA MCP Bridge"

		def __init__(self, parent):
			# Crucial: Initialize _bridge BEFORE super().__init__ because NVDA's base constructor
			# immediately calls _buildGui() -> makeSettings() before returning control!
			self._bridge = self._find_bridge()
			super().__init__(parent)

		@staticmethod
		def _find_bridge() -> Any:
			"""Locate the active NVDA MCP Bridge instance from running global plugins."""
			try:
				import globalPluginHandler
				for plugin in globalPluginHandler.runningPlugins:
					mod_name = getattr(plugin.__class__, "__module__", "")
					if mod_name.startswith("globalPlugins.nvdaMcp-bridge") or getattr(plugin, "_bridge", None):
						bridge = getattr(plugin, "_bridge", None)
						if bridge:
							return bridge
			except Exception:
				pass
			return None

		def onPanelActivated(self):
			"""Called by NVDA when this panel category is selected in the Settings dialog.

			NVDA's SettingsPanel.onPanelActivated() is responsible for showing the
			panel (it calls self.Show()). MultiCategorySettingsDialog hides every
			panel and only invokes onPanelActivated() for the newly selected
			category, so failing to delegate to the base implementation leaves
			this panel hidden and the category page renders blank.
			"""
			baseActivated = getattr(super(), "onPanelActivated", None)
			if callable(baseActivated):
				baseActivated()
			else:
				# Older NVDA releases have no base implementation to delegate to.
				self.Show()
			self._refresh_status()

		def onPanelDeactivated(self):
			"""Called by NVDA when another settings category replaces this one."""
			baseDeactivated = getattr(super(), "onPanelDeactivated", None)
			if callable(baseDeactivated):
				baseDeactivated()
			else:
				self.Hide()

		def onDiscard(self):
			"""Called by NVDA when user cancels the settings dialog."""
			baseDiscard = getattr(super(), "onDiscard", None)
			if callable(baseDiscard):
				baseDiscard()
			policy = getattr(self._bridge, "permission_policy", None)
			if policy:
				if hasattr(self, "inputControlCheckbox"):
					self.inputControlCheckbox.SetValue(policy.is_granted(PERMISSION_INPUT_CONTROL))
				if hasattr(self, "diagnosticsCheckbox"):
					self.diagnosticsCheckbox.SetValue(policy.is_granted(PERMISSION_DIAGNOSTICS))
				if hasattr(self, "lifecycleCheckbox"):
					self.lifecycleCheckbox.SetValue(policy.is_granted(PERMISSION_NVDA_LIFECYCLE))

		def makeSettings(self, settingsSizer):
			sHelper = guiHelper.BoxSizerHelper(self, sizer=settingsSizer)

			# 1. Bridge Status Label
			status_text = get_bridge_status_summary(self._bridge)
			self.statusLabel = sHelper.addItem(
				wx.StaticText(self, label=status_text)
			)

			# 2. Permissions Group with compliant StaticBox parenting
			permGroup = wx.StaticBox(self, label="Capability Permissions")
			permSizer = wx.StaticBoxSizer(permGroup, wx.VERTICAL)

			policy = getattr(self._bridge, "permission_policy", None)

			self.inputControlCheckbox = wx.CheckBox(
				permGroup,
				label="Allow MCP Input Control (send keyboard gestures)",
			)
			if policy:
				self.inputControlCheckbox.SetValue(policy.is_granted(PERMISSION_INPUT_CONTROL))
			permSizer.Add(self.inputControlCheckbox, 0, wx.ALL, 5)

			self.diagnosticsCheckbox = wx.CheckBox(
				permGroup,
				label="Allow MCP Diagnostic Inspection (logs, crash dumps)",
			)
			if policy:
				self.diagnosticsCheckbox.SetValue(policy.is_granted(PERMISSION_DIAGNOSTICS))
			permSizer.Add(self.diagnosticsCheckbox, 0, wx.ALL, 5)

			self.lifecycleCheckbox = wx.CheckBox(
				permGroup,
				label="Allow MCP NVDA Lifecycle actions (restart NVDA, reload plugins)",
			)
			if policy:
				self.lifecycleCheckbox.SetValue(policy.is_granted(PERMISSION_NVDA_LIFECYCLE))
			permSizer.Add(self.lifecycleCheckbox, 0, wx.ALL, 5)

			settingsSizer.Add(permSizer, 0, wx.EXPAND | wx.ALL, 5)

			# 3. Developer Mode Controls with compliant StaticBox parenting
			devGroup = wx.StaticBox(self, label="Developer Mode (Arbitrary Python Execution)")
			devSizer = wx.StaticBoxSizer(devGroup, wx.VERTICAL)

			devBtnSizer = wx.BoxSizer(wx.HORIZONTAL)
			self.enableDevBtn = wx.Button(devGroup, label="Enable Developer Mode (5 min)...")
			self.enableDevBtn.Bind(wx.EVT_BUTTON, self.onEnableDev)
			devBtnSizer.Add(self.enableDevBtn, 0, wx.RIGHT, 5)

			self.disableDevBtn = wx.Button(devGroup, label="Disable Developer Mode")
			self.disableDevBtn.Bind(wx.EVT_BUTTON, self.onDisableDev)
			devBtnSizer.Add(self.disableDevBtn, 0, wx.RIGHT, 5)

			devSizer.Add(devBtnSizer, 0, wx.ALL, 5)
			settingsSizer.Add(devSizer, 0, wx.EXPAND | wx.ALL, 5)

			# 4. Companion & MCP Clients Setup with compliant StaticBox parenting
			compGroup = wx.StaticBox(self, label="Companion & MCP Clients")
			compSizer = wx.StaticBoxSizer(compGroup, wx.VERTICAL)

			compBtnSizer = wx.BoxSizer(wx.HORIZONTAL)
			self.installCompBtn = wx.Button(compGroup, label="Install or Repair MCP Companion...")
			self.installCompBtn.Bind(wx.EVT_BUTTON, self.onInstallCompanion)
			compBtnSizer.Add(self.installCompBtn, 0, wx.RIGHT, 5)

			self.configClientsBtn = wx.Button(compGroup, label="Configure MCP Clients...")
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
			install_companion_interactive(parent=self, on_done=lambda ok, msg: self._refresh_status())

		def onConfigClients(self, event):
			configure_clients_interactive(parent=self)
			self._refresh_status()

		def _refresh_status(self):
			if hasattr(self, "statusLabel") and self.statusLabel:
				self.statusLabel.SetLabel(get_bridge_status_summary(self._bridge))
				self.Layout()
				# The status text changes length at runtime (permission list,
				# developer-mode countdown). Let the scrolled container recompute
				# its layout and scrollbars so the label is never clipped.
				sendLayoutUpdated = getattr(self, "_sendLayoutUpdatedEvent", None)
				if callable(sendLayoutUpdated):
					sendLayoutUpdated()

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
		if _menu_item:
			remove_menu_items(plugin)

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
