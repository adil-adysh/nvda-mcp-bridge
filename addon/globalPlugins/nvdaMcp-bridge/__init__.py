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

import globalPluginHandler
import addonHandler
import logHandler
import threading

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
			from . import nvda_http_bridge

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

		except Exception as e:
			logHandler.log.error(f"Failed to start NVDA HTTP Bridge: {e}", exc_info=True)
			# Don't crash NVDA if bridge fails
			self._bridge = None
			self._bridge_thread = None

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
			logHandler.log.info("Shutting down NVDA HTTP Bridge...")

			# Signal shutdown
			if self._shutdown_event:
				self._shutdown_event.set()

			# Stop the bridge
			if self._bridge:
				try:
					self._bridge.stop()
				except Exception as e:
					logHandler.log.error(f"Error stopping bridge: {e}", exc_info=True)

			# Wait for thread to finish (with timeout)
			if self._bridge_thread:
				try:
					if self._bridge_thread.is_alive():
						self._bridge_thread.join(timeout=5.0)
						if self._bridge_thread.is_alive():
							logHandler.log.warning("Bridge thread did not stop within timeout")
				except Exception as e:
					logHandler.log.error(f"Error joining bridge thread: {e}", exc_info=True)

			logHandler.log.info("NVDA HTTP Bridge stopped successfully")

		except Exception as e:
			logHandler.log.error(f"Error during HTTP Bridge shutdown: {e}", exc_info=True)

		finally:
			# Always call parent terminate, even if shutdown failed
			try:
				super(GlobalPlugin, self).terminate()
			except Exception as e:
				logHandler.log.error(f"Error in parent terminate: {e}", exc_info=True)
