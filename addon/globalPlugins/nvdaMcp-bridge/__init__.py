"""
NVDA HTTP Bridge Global Plugin
Runs a minimal local HTTP bridge inside NVDA process for external access.

This addon provides a thin HTTP bridge layer that:
- Accepts simple JSON requests on 127.0.0.1:8765
- Calls NVDA APIs (safely queued to main thread)
- Returns structured JSON responses

It does NOT implement MCP protocol. That is handled by the separate
nvda-mcp-server process which runs standalone and connects to this bridge.

Architecture:
1. NVDA + This Addon: Simple HTTP bridge (127.0.0.1:8765)
2. nvda-mcp-server: MCP protocol handler (127.0.0.1:8000)
3. VS Code: Connects to nvda-mcp-server, not directly to NVDA

Thread Safety:
- Plugin initialization/termination: Runs on NVDA main thread
- HTTP server: Runs in background daemon thread
- NVDA API calls: Automatically queued to main thread via queueHandler
"""

import sys
import os

# Add bundled dependencies (Pydantic) to Python path
_LIB_DIR = os.path.join(os.path.dirname(__file__), "lib")
if os.path.exists(_LIB_DIR) and _LIB_DIR not in sys.path:
	sys.path.insert(0, _LIB_DIR)

import globalPluginHandler  # noqa: E402
import addonHandler  # noqa: E402
import logHandler  # noqa: E402
import threading  # noqa: E402

# Initialize translations
addonHandler.initTranslation()


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	"""Global plugin that manages the NVDA HTTP bridge lifecycle"""

	def __init__(self):
		"""Initialize the plugin and start HTTP bridge"""
		super(GlobalPlugin, self).__init__()
		self._bridge = None
		self._bridge_thread = None
		self._shutdown_event = threading.Event()

		try:
			# Import HTTP bridge module
			from .server import http_bridge as nvda_http_bridge

			# Create bridge instance
			self._bridge = nvda_http_bridge.NVDAHTTPBridge(
				host="127.0.0.1", port=8765, shutdown_event=self._shutdown_event
			)

			# Start bridge in background thread
			self._bridge_thread = threading.Thread(
				target=self._run_bridge, daemon=True, name="NVDA-HTTP-Bridge"
			)
			self._bridge_thread.start()

			logHandler.log.info("NVDA HTTP Bridge started successfully on 127.0.0.1:8765")
			logHandler.log.info("Connect nvda-mcp-server to this bridge for MCP access")

			# Register accessible Settings Panel and Tools menu items
			try:
				import gui
				from .gui_settings import MCPBridgeSettingsPanel, install_menu_items

				if hasattr(gui, "settingsDialogs") and hasattr(gui.settingsDialogs, "NVDASettingsDialog"):
					if MCPBridgeSettingsPanel and MCPBridgeSettingsPanel not in gui.settingsDialogs.NVDASettingsDialog.categoryClasses:
						gui.settingsDialogs.NVDASettingsDialog.categoryClasses.append(MCPBridgeSettingsPanel)
				elif hasattr(gui, "NVDASettingsDialog"):
					if MCPBridgeSettingsPanel and MCPBridgeSettingsPanel not in gui.NVDASettingsDialog.categoryClasses:
						gui.NVDASettingsDialog.categoryClasses.append(MCPBridgeSettingsPanel)

				install_menu_items(self)
			except Exception as e:
				logHandler.log.debug(f"GUI registration skipped or failed: {e}")

			# Fast, non-blocking inspection of companion installation
			try:
				from .companion_manager import CompanionManager
				comp_mgr = CompanionManager()
				comp_status = comp_mgr.inspect_installed()
				if not comp_status.installed or not comp_status.compatible:
					logHandler.log.info(
						f"NVDA MCP Companion status: {comp_status.status_message}. "
						"You can install it via NVDA Tools Menu -> NVDA MCP Bridge -> Install or Repair MCP Companion."
					)
			except Exception as e:
				logHandler.log.debug(f"Lightweight companion startup check skipped: {e}")

		except Exception as e:
			logHandler.log.error(f"Failed to start NVDA HTTP Bridge: {e}", exc_info=True)
			# Don't crash NVDA if bridge fails
			self._bridge = None
			self._bridge_thread = None

	def script_showBridgeStatus(self, gesture):
		"""Report current NVDA MCP Bridge status, permissions, and companion installation."""
		from .gui_settings import get_bridge_status_summary
		import ui
		summary = get_bridge_status_summary(self._bridge)
		ui.message(summary)

	script_showBridgeStatus.__doc__ = "Reports NVDA MCP Bridge status, permissions, and companion state."

	def script_toggleDeveloperMode(self, gesture):
		"""Toggle developer mode with accessible confirmation dialog."""
		from .gui_settings import disable_developer_mode_interactive, enable_developer_mode_interactive
		if self._bridge and self._bridge.developer_mode_manager.is_enabled:
			disable_developer_mode_interactive(self._bridge)
		else:
			enable_developer_mode_interactive(self._bridge)

	script_toggleDeveloperMode.__doc__ = "Toggles NVDA MCP Bridge developer mode with confirmation dialog."

	def script_installCompanion(self, gesture):
		"""Launch accessible companion installation or repair dialog."""
		from .gui_settings import install_companion_interactive
		install_companion_interactive()

	script_installCompanion.__doc__ = "Installs or repairs the bundled NVDA MCP Companion binary."

	def script_configureClients(self, gesture):
		"""Launch accessible MCP client configurator."""
		from .gui_settings import configure_clients_interactive
		configure_clients_interactive()

	script_configureClients.__doc__ = "Configures supported MCP clients to use the NVDA MCP Companion."

	def _run_bridge(self):
		"""Run the HTTP bridge in background thread"""
		try:
			if self._bridge:
				self._bridge.start()
		except Exception as e:
			logHandler.log.error(f"NVDA HTTP Bridge error: {e}", exc_info=True)

	def terminate(self):
		"""Clean shutdown of HTTP bridge with comprehensive error handling"""
		try:
			logHandler.log.info("Terminating NVDA MCP Bridge addon...")

			# Step 1: Signal shutdown to stop accepting new requests
			if self._shutdown_event:
				self._shutdown_event.set()
				logHandler.log.debug("Shutdown event set")

			# Step 2: Stop the bridge server
			if self._bridge:
				try:
					self._bridge.stop()
					logHandler.log.debug("Bridge stop() called")
				except Exception as e:
					logHandler.log.error(f"Error stopping bridge: {e}", exc_info=True)

			# Step 3: Wait for bridge thread to finish (with timeout)
			if self._bridge_thread and self._bridge_thread.is_alive():
				try:
					logHandler.log.debug("Waiting for bridge thread to finish (timeout: 5s)...")
					self._bridge_thread.join(timeout=5.0)

					if self._bridge_thread.is_alive():
						logHandler.log.warning(
							"Bridge thread did not stop within 5 seconds timeout. "
							"Force terminating may leave socket in TIME_WAIT state."
						)
					else:
						logHandler.log.debug("Bridge thread stopped cleanly")

				except Exception as e:
					logHandler.log.error(f"Error joining bridge thread: {e}", exc_info=True)

			# Step 4: Clean up GUI items
			try:
				import gui
				from .gui_settings import MCPBridgeSettingsPanel, remove_menu_items
				remove_menu_items(self)
				if hasattr(gui, "settingsDialogs") and hasattr(gui.settingsDialogs, "NVDASettingsDialog"):
					if MCPBridgeSettingsPanel and MCPBridgeSettingsPanel in gui.settingsDialogs.NVDASettingsDialog.categoryClasses:
						gui.settingsDialogs.NVDASettingsDialog.categoryClasses.remove(MCPBridgeSettingsPanel)
				elif hasattr(gui, "NVDASettingsDialog"):
					if MCPBridgeSettingsPanel and MCPBridgeSettingsPanel in gui.NVDASettingsDialog.categoryClasses:
						gui.NVDASettingsDialog.categoryClasses.remove(MCPBridgeSettingsPanel)
			except Exception as e:
				logHandler.log.debug(f"GUI cleanup error: {e}")

			# Step 5: Clean up references
			self._bridge = None
			self._bridge_thread = None
			self._shutdown_event = None

			logHandler.log.info("NVDA MCP Bridge addon terminated successfully")

		except Exception as e:
			logHandler.log.error(f"Error during NVDA MCP Bridge shutdown: {e}", exc_info=True)

		finally:
			# Always call parent terminate, even if shutdown failed
			try:
				super(GlobalPlugin, self).terminate()
			except Exception as e:
				logHandler.log.error(f"Error in parent terminate: {e}", exc_info=True)
