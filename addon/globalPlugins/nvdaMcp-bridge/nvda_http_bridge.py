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

Thread Safety:
- HTTP server runs in background daemon thread
- All NVDA API calls are queued to main thread via queueHandler.queueFunction()
- Requests block while waiting for main thread execution (with timeout)
- This prevents NVDA freezing/crashes from background thread API access
"""

import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, Any, Optional, Callable
import logHandler
import queueHandler

from . import nvda_bridge


# Security configuration
DEFAULT_BRIDGE_TOKEN = "nvda-bridge-secret-change-me"
TOKEN_HEADER = "X-NVDA-Bridge-Token"


def _execute_on_main_thread(func: Callable, *args, timeout: float = 5.0, **kwargs) -> Any:
	"""
	Execute a function on NVDA's main thread and wait for the result.

	This is critical for thread safety - NVDA APIs MUST be called from the main thread.

	Args:
		func: Function to execute
		*args: Positional arguments
		timeout: Maximum time to wait for result (seconds)
		**kwargs: Keyword arguments

	Returns:
		Result from the function

	Raises:
		TimeoutError: If function doesn't complete within timeout
	"""
	result_container = {"result": None, "error": None, "completed": False}
	completed_event = threading.Event()

	def _wrapper():
		"""Wrapper that runs on main thread"""
		try:
			result = func(*args, **kwargs)
			result_container["result"] = result
			result_container["completed"] = True
		except Exception as e:
			logHandler.log.error(f"Error executing {func.__name__} on main thread: {e}", exc_info=True)
			result_container["error"] = e
			result_container["completed"] = True
		finally:
			completed_event.set()

	# Queue the function to NVDA's main thread
	try:
		queueHandler.queueFunction(queueHandler.eventQueue, _wrapper)
	except Exception as e:
		logHandler.log.error(f"Failed to queue function to main thread: {e}", exc_info=True)
		return {"error": f"Failed to queue to main thread: {str(e)}"}

	# Wait for completion with timeout
	if not completed_event.wait(timeout=timeout):
		logHandler.log.error(f"Timeout waiting for {func.__name__} on main thread")
		return {"error": f"Operation timed out after {timeout} seconds"}

	# Return result or raise error
	if result_container["error"]:
		return {"error": str(result_container["error"])}

	return result_container["result"]


class NVDABridgeRequestHandler(BaseHTTPRequestHandler):
	"""HTTP request handler for NVDA bridge"""

	def log_message(self, format, *args):
		"""Suppress verbose HTTP server logging to avoid crashes"""
		pass

	def _send_json_response(self, data: Dict[str, Any], status_code: int = 200):
		"""Send JSON response with error handling"""
		try:
			self.send_response(status_code)
			self.send_header("Content-Type", "application/json")
			self.send_header("Access-Control-Allow-Origin", "127.0.0.1")
			self.end_headers()
			response_json = json.dumps(data, ensure_ascii=False, indent=2)
			self.wfile.write(response_json.encode("utf-8"))
		except Exception as e:
			logHandler.log.error(f"Error sending response: {e}", exc_info=True)
			# Don't crash - response already partially sent

	def _check_auth(self) -> bool:
		"""Check bridge token authentication"""
		token = self.headers.get(TOKEN_HEADER, "")
		expected_token = getattr(self.server, "bridge_token", DEFAULT_BRIDGE_TOKEN)
		return token == expected_token

	def do_OPTIONS(self):
		"""Handle CORS preflight"""
		self.send_response(200)
		self.send_header("Access-Control-Allow-Origin", "127.0.0.1")
		self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		self.send_header("Access-Control-Allow-Headers", f"Content-Type, {TOKEN_HEADER}")
		self.end_headers()

	def do_GET(self):
		"""Handle GET requests with error handling"""
		try:
			if self.path == "/health":
				# Health check - no auth required
				self._send_json_response({"status": "ok", "service": "NVDA HTTP Bridge", "version": "1.0"})
			else:
				self._send_json_response({"error": "Not found"}, 404)
		except Exception as e:
			logHandler.log.error(f"Error in GET handler: {e}", exc_info=True)
			try:
				self._send_json_response({"error": "Internal server error"}, 500)
			except Exception:
				pass  # Don't crash if response fails

	def do_POST(self):
		"""Handle POST requests for bridge actions with comprehensive error handling"""
		try:
			# Check authentication
			if not self._check_auth():
				self._send_json_response({"error": "Unauthorized (invalid token)"}, 401)
				return

			# Parse request body
			try:
				content_length = int(self.headers.get("Content-Length", 0))
				# Limit request size to prevent memory issues
				if content_length > 1024 * 1024:  # 1MB limit
					self._send_json_response({"error": "Request too large"}, 413)
					return

				if content_length == 0:
					self._send_json_response({"error": "Empty request body"}, 400)
					return

				body = self.rfile.read(content_length)
				request_data = json.loads(body.decode("utf-8"))

			except json.JSONDecodeError as e:
				self._send_json_response({"error": f"Invalid JSON: {e}"}, 400)
				return
			except ValueError as e:
				self._send_json_response({"error": f"Invalid content length: {e}"}, 400)
				return
			except Exception as e:
				logHandler.log.error(f"Error reading request: {e}", exc_info=True)
				self._send_json_response({"error": f"Error reading request: {e}"}, 400)
				return

			# Validate request data structure
			if not isinstance(request_data, dict):
				self._send_json_response({"error": "Request must be a JSON object"}, 400)
				return

			# Handle action routing
			if self.path == "/action":
				self._handle_action(request_data)
			else:
				self._send_json_response({"error": "Unknown endpoint"}, 404)
		except Exception as e:
			logHandler.log.error(f"Unhandled error in POST handler: {e}", exc_info=True)
			try:
				self._send_json_response({"error": "Internal server error"}, 500)
			except Exception:
				pass  # Don't crash if response fails

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
			self._send_json_response({"error": str(e), "type": type(e).__name__}, 500)


class NVDAHTTPBridge:
	"""Minimal HTTP bridge that runs inside NVDA process"""

	def __init__(
		self,
		host: str = "127.0.0.1",
		port: int = 8765,
		shutdown_event: Optional[threading.Event] = None,
		bridge_token: Optional[str] = None,
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
		Execute a bridge action with parameter validation.
		All NVDA API calls are safely queued to the main thread.

		Args:
			action_name: Name of the action to execute
			params: Parameters to pass to the action

		Returns:
			Result from the action handler

		Raises:
			ValueError: If action is unknown or parameters are invalid
		"""
		try:
			if not isinstance(action_name, str):
				raise ValueError("Action name must be a string")

			if action_name not in self.actions:
				raise ValueError(f"Unknown action: {action_name}")

			handler = self.actions[action_name]

			# Validate params is dict
			if params is not None and not isinstance(params, dict):
				raise ValueError("Parameters must be a dictionary")

			# Execute handler on NVDA's main thread for thread safety
			# This prevents freezing or crashes from background thread API calls
			try:
				if params:
					result = _execute_on_main_thread(handler, timeout=10.0, **params)
				else:
					result = _execute_on_main_thread(handler, timeout=10.0)
				return result
			except TypeError as e:
				# Handle invalid parameter errors
				logHandler.log.error(f"Invalid parameters for action {action_name}: {e}", exc_info=True)
				return {"error": f"Invalid parameters: {str(e)}"}
		except Exception as e:
			logHandler.log.error(f"Error executing action {action_name}: {e}", exc_info=True)
			raise

	def start(self):
		"""Start the HTTP bridge server (blocking) with robust error handling"""
		try:
			# Create HTTP server with timeout
			self.server = HTTPServer((self.host, self.port), NVDABridgeRequestHandler)
			self.server.timeout = 1.0  # 1 second timeout for handle_request

			# Attach configuration to server
			self.server.bridge_token = self.bridge_token
			self.server.execute_action = self.execute_action

			logHandler.log.info(f"NVDA HTTP Bridge listening on {self.host}:{self.port}")
			logHandler.log.info(f"Bridge token required: {TOKEN_HEADER}")

			# Serve requests until shutdown
			while not self.shutdown_event.is_set():
				try:
					self.server.handle_request()
				except OSError as e:
					# Handle socket errors without crashing
					logHandler.log.error(f"Socket error in bridge: {e}", exc_info=True)
					if not self.shutdown_event.is_set():
						# Brief pause before retry
						self.shutdown_event.wait(0.1)
				except Exception as e:
					# Log but don't crash on individual request errors
					logHandler.log.error(f"Error handling request: {e}", exc_info=True)

			logHandler.log.info("NVDA HTTP Bridge stopped")

		except OSError as e:
			if "address already in use" in str(e).lower() or e.errno == 10048:
				logHandler.log.error(f"Port {self.port} already in use. Cannot start bridge.", exc_info=True)
			else:
				logHandler.log.error(f"Socket error starting bridge: {e}", exc_info=True)
			raise
		except Exception as e:
			logHandler.log.error(f"NVDA HTTP Bridge error: {e}", exc_info=True)
			raise

	def stop(self):
		"""Stop the bridge server gracefully"""
		try:
			if self.server:
				try:
					self.server.shutdown()
				except Exception:
					pass  # shutdown may fail if server never started
				try:
					self.server.server_close()
				except Exception:
					pass  # server_close may fail if already closed
				logHandler.log.info("NVDA HTTP Bridge closed")
		except Exception as e:
			logHandler.log.error(f"Error stopping NVDA HTTP Bridge: {e}", exc_info=True)
