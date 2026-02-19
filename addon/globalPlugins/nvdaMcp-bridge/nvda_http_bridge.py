"""
NVDA HTTP Bridge
Provides a minimal local HTTP server that wraps NVDA APIs for external access.

This is a thin bridge layer - it does NOT implement MCP protocol.
All MCP logic is handled by the separate nvda-mcp-server process.

Architecture:
- NVDA Bridge (this module): Simple HTTP bridge on 127.0.0.1:8765
- nvda-mcp-server (standalone): MCP protocol implementation on 127.0.0.1:8000
- VS Code client: Connects to nvda-mcp-server, not directly to NVDA

Protocol: Simple JSON over HTTP (NOT MCP)
"""

import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, Any, Optional
import logHandler

from . import nvda_bridge


# Security configuration
DEFAULT_BRIDGE_TOKEN = "nvda-bridge-secret-change-me"
TOKEN_HEADER = "X-NVDA-Bridge-Token"


class NVDABridgeRequestHandler(BaseHTTPRequestHandler):
	"""HTTP request handler for NVDA bridge"""

	def log_message(self, format, *args):
		"""Suppress verbose HTTP server logging to avoid crashes"""
		pass

	def _send_json_response(self, data: Dict[str, Any], status_code: int = 200):
		"""Send JSON response"""
		self.send_response(status_code)
		self.send_header("Content-Type", "application/json")
		self.send_header("Access-Control-Allow-Origin", "127.0.0.1")
		self.end_headers()
		response_json = json.dumps(data, ensure_ascii=False, indent=2)
		self.wfile.write(response_json.encode('utf-8'))

	def _check_auth(self) -> bool:
		"""Check bridge token authentication"""
		token = self.headers.get(TOKEN_HEADER, "")
		expected_token = getattr(self.server, 'bridge_token', DEFAULT_BRIDGE_TOKEN)
		return token == expected_token

	def do_OPTIONS(self):
		"""Handle CORS preflight"""
		self.send_response(200)
		self.send_header("Access-Control-Allow-Origin", "127.0.0.1")
		self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		self.send_header("Access-Control-Allow-Headers", f"Content-Type, {TOKEN_HEADER}")
		self.end_headers()

	def do_GET(self):
		"""Handle GET requests"""
		if self.path == "/health":
			# Health check - no auth required
			self._send_json_response({
				"status": "ok",
				"service": "NVDA HTTP Bridge",
				"version": "1.0"
			})
		else:
			self._send_json_response({"error": "Not found"}, 404)

	def do_POST(self):
		"""Handle POST requests for bridge actions"""
		# Check authentication
		if not self._check_auth():
			self._send_json_response({"error": "Unauthorized (invalid token)"}, 401)
			return

		# Parse request body
		try:
			content_length = int(self.headers.get('Content-Length', 0))
			if content_length == 0:
				self._send_json_response({"error": "Empty request body"}, 400)
				return

			body = self.rfile.read(content_length)
			request_data = json.loads(body.decode('utf-8'))

		except json.JSONDecodeError as e:
			self._send_json_response({"error": f"Invalid JSON: {e}"}, 400)
			return
		except Exception as e:
			self._send_json_response({"error": f"Error reading request: {e}"}, 400)
			return

		# Handle action routing
		if self.path == "/action":
			self._handle_action(request_data)
		else:
			self._send_json_response({"error": "Unknown endpoint"}, 404)

	def _handle_action(self, request_data: Dict[str, Any]):
		"""
		Handle a bridge action request.
		
		Expected format:
		{
			"action": "get_current_focus",
			"params": {}
		}
		"""
		try:
			action = request_data.get("action")
			params = request_data.get("params", {})

			if not action:
				self._send_json_response({"error": "Missing 'action' field"}, 400)
				return

			# Route to action handler
			result = self.server.execute_action(action, params)
			self._send_json_response({"success": True, "result": result})

		except ValueError as e:
			self._send_json_response({"error": str(e)}, 400)
		except Exception as e:
			logHandler.log.error(f"Error executing action: {e}", exc_info=True)
			self._send_json_response({
				"error": str(e),
				"type": type(e).__name__
			}, 500)


class NVDAHTTPBridge:
	"""Minimal HTTP bridge that runs inside NVDA process"""

	def __init__(
		self,
		host: str = "127.0.0.1",
		port: int = 8765,
		shutdown_event: Optional[threading.Event] = None,
		bridge_token: Optional[str] = None
	):
		"""
		Initialize HTTP bridge.

		Args:
			host: Host to bind to (should be 127.0.0.1)
			port: Port to bind to (should be 8765)
			shutdown_event: Event to signal shutdown
			bridge_token: Authentication token for requests
		"""
		self.host = host
		self.port = port
		self.shutdown_event = shutdown_event or threading.Event()
		self.bridge_token = bridge_token or DEFAULT_BRIDGE_TOKEN
		self.server: Optional[HTTPServer] = None

		# Map actions to nvda_bridge functions
		self.actions: Dict[str, callable] = {
			"get_current_focus": nvda_bridge.get_current_focus,
			"read_current_line": nvda_bridge.read_current_line,
			"speak_text": nvda_bridge.speak_text,
			"get_window_title": nvda_bridge.get_window_title,
			"move_focus": nvda_bridge.move_focus,
			"get_navigator_object": nvda_bridge.get_navigator_object,
			"get_desktop_object": nvda_bridge.get_desktop_object,
			"show_message": nvda_bridge.show_message,
		}

	def execute_action(self, action_name: str, params: Dict[str, Any]) -> Any:
		"""
		Execute a bridge action.

		Args:
			action_name: Name of the action to execute
			params: Parameters to pass to the action

		Returns:
			Result from the action handler

		Raises:
			ValueError: If action is unknown
		"""
		if action_name not in self.actions:
			raise ValueError(f"Unknown action: {action_name}")

		handler = self.actions[action_name]

		# Call handler with parameters
		if params:
			return handler(**params)
		else:
			return handler()

	def start(self):
		"""Start the HTTP bridge server (blocking)"""
		try:
			# Create HTTP server
			self.server = HTTPServer((self.host, self.port), NVDABridgeRequestHandler)

			# Attach configuration to server
			self.server.bridge_token = self.bridge_token
			self.server.execute_action = self.execute_action

			logHandler.log.info(f"NVDA HTTP Bridge listening on {self.host}:{self.port}")
			logHandler.log.info(f"Bridge token required: {TOKEN_HEADER}")

			# Serve requests until shutdown
			while not self.shutdown_event.is_set():
				self.server.handle_request()

			logHandler.log.info("NVDA HTTP Bridge stopped")

		except Exception as e:
			logHandler.log.error(f"NVDA HTTP Bridge error: {e}", exc_info=True)
			raise

	def stop(self):
		"""Stop the bridge server"""
		try:
			if self.server:
				self.server.shutdown()
				self.server.server_close()
				logHandler.log.info("NVDA HTTP Bridge closed")
		except Exception as e:
			logHandler.log.error(f"Error stopping NVDA HTTP Bridge: {e}", exc_info=True)
