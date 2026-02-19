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

import hmac
import json
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, Any, Optional
import logHandler

from .tool_registry import ToolRegistry
from .default_tools import create_default_registry
from .main_thread_executor import MainThreadExecutor


# Security configuration
DEFAULT_BRIDGE_TOKEN = "nvda-bridge-secret-change-me"
TOKEN_HEADER = "X-NVDA-Bridge-Token"


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
			# Origin must include the scheme; bare IPs are not valid CORS origins.
			self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1")
			self.end_headers()
			response_json = json.dumps(data, ensure_ascii=False, indent=2)
			self.wfile.write(response_json.encode("utf-8"))
		except Exception as e:
			logHandler.log.error(f"Error sending response: {e}", exc_info=True)
			# Don't crash - response already partially sent

	def _check_auth(self) -> bool:
		"""Check bridge token authentication using a timing-safe comparison.

		A plain ``==`` short-circuits on the first differing byte, leaking
		information about the correct prefix through response latency.
		``hmac.compare_digest`` runs in constant time regardless of where the
		strings diverge, eliminating that timing oracle.
		"""
		token = self.headers.get(TOKEN_HEADER, "")
		expected_token = getattr(self.server, "bridge_token", DEFAULT_BRIDGE_TOKEN)
		try:
			return hmac.compare_digest(
				token.encode("utf-8"),
				expected_token.encode("utf-8"),
			)
		except Exception:
			# compare_digest requires bytes or str; fall back safely.
			return False

	def do_OPTIONS(self):
		"""Handle CORS preflight"""
		self.send_response(200)
		self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1")
		self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		self.send_header("Access-Control-Allow-Headers", f"Content-Type, {TOKEN_HEADER}")
		self.end_headers()

	def do_GET(self):
		"""Handle GET requests with error handling"""
		try:
			if self.path == "/health":
				# Health check — intentionally unauthenticated so monitoring
				# probes can reach it without knowing the bridge token.
				self._send_json_response({"status": "ok", "service": "NVDA HTTP Bridge", "version": "1.0"})
			elif self.path == "/tools":
				# List all registered tools — requires authentication so callers
				# cannot enumerate capabilities without the bridge token.
				if not self._check_auth():
					self._send_json_response({"error": "Unauthorized (invalid token)"}, 401)
					return
				tools = self.server.list_tools()
				self._send_json_response({"tools": tools, "count": len(tools)})
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
				# Reject negative values before the 0-check: rfile.read(-N) reads
				# until EOF on CPython, bypassing the empty-body guard entirely.
				if content_length < 0:
					self._send_json_response({"error": "Invalid Content-Length"}, 400)
					return
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
		action = request_data.get("action")
		params = request_data.get("params", {})

		if action is None:
			# No action field at all — respond without an action key since we
			# have nothing meaningful to echo back.
			self._send_json_response(
				{"success": False, "error": "Missing 'action' field", "error_type": "validation"},
				400,
			)
			return

		if not isinstance(action, str):
			self._send_json_response(
				{
					"success": False,
					"action": str(action),
					"error": "'action' must be a string",
					"error_type": "validation",
				},
				400,
			)
			return

		action = action.strip()
		if not action:
			self._send_json_response(
				{
					"success": False,
					"action": "",
					"error": "'action' cannot be empty",
					"error_type": "validation",
				},
				400,
			)
			return

		if len(action) > 128:
			self._send_json_response(
				{
					"success": False,
					"action": action,
					"error": "'action' is too long (max 128 characters)",
					"error_type": "validation",
				},
				400,
			)
			return

		if params is None:
			params = {}
		if not isinstance(params, dict):
			self._send_json_response(
				{
					"success": False,
					"action": action,
					"error": "'params' must be a JSON object",
					"error_type": "validation",
				},
				400,
			)
			return

		# Execute action and get HTTP envelope
		# The bridge layer handles all error wrapping and threading
		envelope = self.server.execute_action(action, params)

		# Determine HTTP status code from envelope
		if envelope.get("success"):
			status = 200
		else:
			error_type = envelope.get("error_type", "execution")
			if error_type == "validation":
				status = 400  # Bad request
			elif error_type == "timeout":
				status = 504  # Gateway timeout
			elif error_type == "auth":
				status = 401
			else:
				status = 500  # Internal server error

		self._send_json_response(envelope, status)


class NVDAHTTPBridge:
	"""Minimal HTTP bridge that runs inside NVDA process"""

	def __init__(
		self,
		host: str = "127.0.0.1",
		port: int = 8765,
		shutdown_event: Optional[threading.Event] = None,
		bridge_token: Optional[str] = None,
		tool_registry: Optional[ToolRegistry] = None,
	):
		"""
		Initialize HTTP bridge.

		Args:
			host: Host to bind to (should be 127.0.0.1)
			port: Port to bind to (should be 8765)
			shutdown_event: Event to signal shutdown
			bridge_token: Authentication token for requests
			tool_registry: Custom tool registry (uses default if None)
		"""
		self.host = host
		self.port = port
		self.shutdown_event = shutdown_event or threading.Event()
		self.bridge_token = bridge_token or DEFAULT_BRIDGE_TOKEN
		self.server: Optional[HTTPServer] = None
		self.executor = MainThreadExecutor(default_timeout=10.0)

		# Initialize tool registry
		self.registry = tool_registry or create_default_registry()
		logHandler.log.info(f"bridge.tools.loaded count={len(self.registry)}")

		# Freeze registry to prevent runtime modifications (improves safety)
		self.registry.freeze()
		logHandler.log.debug(f"bridge.registry.frozen tools={len(self.registry)}")

		# Attach list_tools to server so the GET /tools handler can reach it
		# without holding a reference to the full bridge object.
		self._list_tools = self.registry.list_tools

	def execute_action(self, action_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
		"""
		Execute a bridge action with full error handling and threading.

		This is the orchestration layer that:
		1. Validates input
		2. Gets tool from registry
		3. Executes on main thread via MainThreadExecutor
		4. Wraps result in HTTP-compatible envelope
		5. Logs execution time for observability

		Args:
			action_name: Name of the action to execute
			params: Parameters to pass to the action

		Returns:
			HTTP envelope with action metadata:
			Success: {"success": True, "action": str, "duration_ms": float, "result": ...}
			Error: {"success": False, "action": str, "duration_ms": float, "error": str, "error_type": str}
		"""
		start_time = time.perf_counter()
		logHandler.log.debug(f"bridge.action.start action={action_name}")

		try:
			# NOTE: action_name and params are already fully validated by
			# _handle_action before this method is called. The type annotations
			# (str / dict) are truthful here — no need to re-check.

			# Look up the tool once and reuse the instance throughout.
			# registry.execute() would do a second get() internally — avoid.
			tool = self.registry.get(action_name)
			if not tool:
				logHandler.log.warning(f"bridge.action.unknown action={action_name}")
				return {
					"success": False,
					"action": action_name,
					"duration_ms": round((time.perf_counter() - start_time) * 1000, 2),
					"error": f"Unknown action: {action_name}",
					"error_type": "validation",
				}

			# validate_params is pure dict/metadata logic — safe on any thread.
			# Fail early here rather than consuming a main-thread queue slot.
			resolved_params = params or {}
			tool.validate_params(resolved_params)

			# Execute tool on NVDA's main thread for thread safety.
			# Only tool.execute touches NVDA APIs; validation already done above.
			result = self.executor.execute(tool.execute, **resolved_params)

			# Log execution time for performance monitoring
			duration_ms = (time.perf_counter() - start_time) * 1000

			if duration_ms > 500:
				logHandler.log.warning(
					f"bridge.action.slow action={action_name} duration_ms={duration_ms:.1f} success=true"
				)
			else:
				logHandler.log.info(
					f"bridge.action.success action={action_name} duration_ms={duration_ms:.1f}"
				)

			# Success - wrap in envelope
			return {
				"success": True,
				"action": action_name,
				"duration_ms": round(duration_ms, 2),
				"result": result,
			}

		except TimeoutError as e:
			duration_ms = (time.perf_counter() - start_time) * 1000
			logHandler.log.error(
				f"bridge.action.timeout action={action_name} "
				f"duration_ms={duration_ms:.1f} error={str(e)}",
				exc_info=True,
			)
			return {
				"success": False,
				"action": action_name,
				"duration_ms": round(duration_ms, 2),
				"error": f"Operation timed out: {str(e)}",
				"error_type": "timeout",
			}
		except ValueError as e:
			# Parameter validation errors
			duration_ms = (time.perf_counter() - start_time) * 1000
			logHandler.log.warning(
				f"bridge.action.validation_failed action={action_name} "
				f"duration_ms={duration_ms:.1f} error={str(e)}"
			)
			return {
				"success": False,
				"action": action_name,
				"duration_ms": round(duration_ms, 2),
				"error": str(e),
				"error_type": "validation",
			}
		except Exception as e:
			# Unexpected errors
			duration_ms = (time.perf_counter() - start_time) * 1000
			logHandler.log.error(
				f"bridge.action.failed action={action_name} " f"duration_ms={duration_ms:.1f} error={str(e)}",
				exc_info=True,
			)
			return {
				"success": False,
				"action": action_name,
				"duration_ms": round(duration_ms, 2),
				"error": f"Execution failed: {str(e)}",
				"error_type": "execution",
			}

	def start(self):
		"""Start the HTTP bridge server (blocking) with robust error handling"""
		try:
			# Create HTTP server with timeout
			self.server = HTTPServer((self.host, self.port), NVDABridgeRequestHandler)
			self.server.timeout = 1.0  # 1 second timeout for handle_request

			# Attach configuration to server
			self.server.bridge_token = self.bridge_token
			self.server.execute_action = self.execute_action
			self.server.list_tools = self._list_tools

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
			logHandler.log.info("Stopping NVDA HTTP Bridge...")

			# Signal shutdown (should already be set by terminate(), but ensure it)
			self.shutdown_event.set()

			# Close the server socket
			if self.server:
				try:
					# Close server socket to interrupt any blocking operations
					self.server.server_close()
					logHandler.log.debug("Server socket closed")
				except Exception as e:
					logHandler.log.warning(f"Error closing server socket: {e}")

			logHandler.log.info("NVDA HTTP Bridge stopped")

		except Exception as e:
			logHandler.log.error(f"Error stopping NVDA HTTP Bridge: {e}", exc_info=True)
