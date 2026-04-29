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
import uuid
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, Any, Optional
import logHandler

from ..core import ToolRegistry, ToolExecutor
from ..tools.registry import NVDA_TOOLS_REGISTRY
from .main_thread_executor import MainThreadExecutor
from ..validation.output_type_registry import build_output_type_registry, validate_output_type_registry


# Security configuration
DEFAULT_BRIDGE_TOKEN = "nvda-bridge-secret-change-me"
TOKEN_HEADER = "X-NVDA-Bridge-Token"
BRIDGE_VERSION = "1.0.0"
PROTOCOL = "nvda-bridge-protocol"
PROTOCOL_VERSION = "1.0.0"
API_LEVEL = 1

ERROR_CODES: Dict[int, Dict[str, Any]] = {
	4001: {"name": "AUTH_FAILURE", "category": "auth", "retry_policy": "never"},
	4101: {"name": "INVALID_JSON", "category": "validation", "retry_policy": "never"},
	4102: {"name": "INVALID_ACTION_NAME", "category": "validation", "retry_policy": "never"},
	4103: {"name": "INVALID_PARAMS_SCHEMA", "category": "validation", "retry_policy": "never"},
	4201: {"name": "SECURE_SCREEN_ACTIVE", "category": "nvda_state", "retry_policy": "after_state_change"},
	4202: {
		"name": "MODAL_DIALOG_BLOCKING",
		"category": "nvda_state",
		"retry_policy": "after_delay",
		"retry_after_ms": 500,
	},
	4203: {
		"name": "MENU_OPEN_BLOCKING",
		"category": "nvda_state",
		"retry_policy": "after_delay",
		"retry_after_ms": 500,
	},
	5001: {
		"name": "NVDA_COM_TIMEOUT",
		"category": "runtime",
		"retry_policy": "after_delay",
		"retry_after_ms": 1000,
	},
	5003: {
		"name": "TOOL_EXECUTION_EXCEPTION",
		"category": "runtime",
		"retry_policy": "after_delay",
		"retry_after_ms": 1000,
	},
	5303: {
		"name": "BRIDGE_INTERNAL_ERROR",
		"category": "internal",
		"retry_policy": "after_delay",
		"retry_after_ms": 1000,
	},
}


def _new_request_id() -> str:
	return str(uuid.uuid4())


class NVDABridgeRequestHandler(BaseHTTPRequestHandler):
	"""HTTP request handler for NVDA bridge"""

	@staticmethod
	def _validate_tools_discovery_payload(payload: Dict[str, Any]) -> None:
		"""Strict in-process validation for protocol v1 /tools payload."""
		required_top = {
			"protocol",
			"protocol_version",
			"bridge_version",
			"api_level",
			"types",
			"tools",
			"count",
		}
		missing_top = required_top - set(payload.keys())
		if missing_top:
			raise RuntimeError(f"tools payload missing required fields: {', '.join(sorted(missing_top))}")

		if payload.get("protocol") != PROTOCOL:
			raise RuntimeError("tools payload has invalid protocol")
		if payload.get("protocol_version") != PROTOCOL_VERSION:
			raise RuntimeError("tools payload has invalid protocol_version")
		if payload.get("api_level") != API_LEVEL:
			raise RuntimeError("tools payload has invalid api_level")

		types = payload.get("types")
		tools = payload.get("tools")
		count = payload.get("count")
		if not isinstance(types, dict) or not types:
			raise RuntimeError("tools payload has invalid or empty types registry")
		if not isinstance(tools, list):
			raise RuntimeError("tools payload has invalid tools list")
		if not isinstance(count, int) or count != len(tools):
			raise RuntimeError("tools payload count mismatch")

		for type_name in types.keys():
			if not isinstance(type_name, str) or not type_name or not type_name[0].isupper():
				raise RuntimeError(f"invalid output type name in types registry: {type_name!r}")

		used_types: set[str] = set()
		for tool in tools:
			if not isinstance(tool, dict):
				raise RuntimeError("tool entry must be an object")
			for key in ("name", "description", "inputSchema", "outputType", "version", "category"):
				if key not in tool:
					raise RuntimeError(f"tool entry missing required field: {key}")
			if "outputSchema" in tool:
				raise RuntimeError("outputSchema is forbidden in protocol v1")

			output_type = tool.get("outputType")
			if not isinstance(output_type, str) or not output_type:
				raise RuntimeError("tool outputType must be a non-empty string")
			if output_type not in types:
				raise RuntimeError(f"tool references unknown outputType: {output_type}")
			used_types.add(output_type)

		unused_types = set(types.keys()) - used_types
		if unused_types:
			raise RuntimeError("unused output types are not allowed")

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

	def _http_status_from_error_code(self, code: int) -> int:
		if code == 4001:
			return 401
		if 4100 <= code < 4200:
			return 400
		if 4200 <= code < 4300:
			return 409
		if 5000 <= code < 6000:
			return 500
		return 500

	def _build_error_object(
		self,
		code: int,
		message: str,
		details: Optional[Dict[str, Any]] = None,
	) -> Dict[str, Any]:
		spec = ERROR_CODES.get(code, ERROR_CODES[5303])
		error: Dict[str, Any] = {
			"code": code,
			"name": spec["name"],
			"message": message,
			"category": spec["category"],
			"retry_policy": spec["retry_policy"],
		}
		retry_after_ms = spec.get("retry_after_ms")
		if retry_after_ms is not None:
			error["retry_after_ms"] = retry_after_ms
		if details is not None:
			error["details"] = details
		return error

	def _build_error_envelope(
		self,
		request_id: str,
		action: str,
		code: int,
		message: str,
		duration_ms: float = 0.0,
		details: Optional[Dict[str, Any]] = None,
	) -> Dict[str, Any]:
		return {
			"request_id": request_id,
			"success": False,
			"action": action,
			"duration_ms": round(duration_ms, 2),
			"bridge": {
				"bridge_version": BRIDGE_VERSION,
				"api_level": API_LEVEL,
			},
			"nvda_status_snapshot": self.server.get_nvda_status_snapshot(),
			"error": self._build_error_object(code=code, message=message, details=details),
		}

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
				self._send_json_response(self.server.get_health_payload())
			elif self.path == "/tools":
				if not self._check_auth():
					request_id = _new_request_id()
					envelope = self._build_error_envelope(
						request_id=request_id,
						action="tools_discovery",
						code=4001,
						message="Unauthorized (invalid token)",
					)
					self._send_json_response(envelope, 401)
					return
				tools = self.server.list_tools()
				if not isinstance(tools, list):
					raise RuntimeError(f"Invalid tools payload type: {type(tools).__name__}")
				types = self.server.list_output_types()
				if not isinstance(types, dict) or not types:
					raise RuntimeError("Invalid or empty output type registry")
				payload = {
					"protocol": PROTOCOL,
					"protocol_version": PROTOCOL_VERSION,
					"bridge_version": BRIDGE_VERSION,
					"api_level": API_LEVEL,
					"types": types,
					"tools": tools,
					"count": len(tools),
				}
				self._validate_tools_discovery_payload(payload)
				self._send_json_response(payload)
			else:
				self._send_json_response({"error": "Not found"}, 404)
		except Exception as e:
			logHandler.log.error(f"Error in GET handler: {e}", exc_info=True)
			try:
				request_id = _new_request_id()
				envelope = self._build_error_envelope(
					request_id=request_id,
					action="http_get",
					code=5303,
					message="Internal server error",
				)
				self._send_json_response(envelope, 500)
			except Exception:
				pass  # Don't crash if response fails

	def do_POST(self):
		"""Handle POST requests for bridge actions with comprehensive error handling"""
		try:
			request_id = _new_request_id()
			# Check authentication
			if not self._check_auth():
				envelope = self._build_error_envelope(
					request_id=request_id,
					action="unknown",
					code=4001,
					message="Unauthorized (invalid token)",
				)
				self._send_json_response(envelope, 401)
				return

			# Parse request body
			try:
				content_length = int(self.headers.get("Content-Length", 0))
				# Reject negative values before the 0-check: rfile.read(-N) reads
				# until EOF on CPython, bypassing the empty-body guard entirely.
				if content_length < 0:
					envelope = self._build_error_envelope(
						request_id=request_id,
						action="unknown",
						code=4101,
						message="Invalid Content-Length",
					)
					self._send_json_response(envelope, 400)
					return
				# Limit request size to prevent memory issues
				if content_length > 1024 * 1024:  # 1MB limit
					envelope = self._build_error_envelope(
						request_id=request_id,
						action="unknown",
						code=4101,
						message="Request too large",
					)
					self._send_json_response(envelope, 413)
					return

				if content_length == 0:
					envelope = self._build_error_envelope(
						request_id=request_id,
						action="unknown",
						code=4101,
						message="Empty request body",
					)
					self._send_json_response(envelope, 400)
					return

				body = self.rfile.read(content_length)
				request_data = json.loads(body.decode("utf-8"))

			except json.JSONDecodeError as e:
				envelope = self._build_error_envelope(
					request_id=request_id,
					action="unknown",
					code=4101,
					message=f"Invalid JSON: {e}",
				)
				self._send_json_response(envelope, 400)
				return
			except ValueError as e:
				envelope = self._build_error_envelope(
					request_id=request_id,
					action="unknown",
					code=4101,
					message=f"Invalid content length: {e}",
				)
				self._send_json_response(envelope, 400)
				return
			except Exception as e:
				logHandler.log.error(f"Error reading request: {e}", exc_info=True)
				envelope = self._build_error_envelope(
					request_id=request_id,
					action="unknown",
					code=4101,
					message=f"Error reading request: {e}",
				)
				self._send_json_response(envelope, 400)
				return

			# Validate request data structure
			if not isinstance(request_data, dict):
				envelope = self._build_error_envelope(
					request_id=request_id,
					action="unknown",
					code=4101,
					message="Request must be a JSON object",
				)
				self._send_json_response(envelope, 400)
				return

			# Handle action routing
			if self.path == "/action":
				if "request_id" not in request_data:
					envelope = self._build_error_envelope(
						request_id=request_id,
						action="unknown",
						code=4101,
						message="Missing 'request_id' field",
					)
					self._send_json_response(envelope, 400)
					return

				request_id_value = request_data.get("request_id")
				if not isinstance(request_id_value, str):
					envelope = self._build_error_envelope(
						request_id=request_id,
						action="unknown",
						code=4101,
						message="'request_id' must be a string",
					)
					self._send_json_response(envelope, 400)
					return

				request_id = request_id_value.strip()
				if not request_id:
					envelope = self._build_error_envelope(
						request_id=_new_request_id(),
						action="unknown",
						code=4101,
						message="'request_id' cannot be empty",
					)
					self._send_json_response(envelope, 400)
					return

				if len(request_id) > 128:
					envelope = self._build_error_envelope(
						request_id=_new_request_id(),
						action="unknown",
						code=4101,
						message="'request_id' is too long (max 128 characters)",
					)
					self._send_json_response(envelope, 400)
					return

				if "params" not in request_data:
					envelope = self._build_error_envelope(
						request_id=request_id,
						action="unknown",
						code=4103,
						message="Missing 'params' field",
					)
					self._send_json_response(envelope, 400)
					return

				self._handle_action(request_data, request_id=request_id)
			else:
				envelope = self._build_error_envelope(
					request_id=request_id,
					action="unknown",
					code=4102,
					message="Unknown endpoint",
				)
				self._send_json_response(envelope, 404)
		except Exception as e:
			logHandler.log.error(f"Unhandled error in POST handler: {e}", exc_info=True)
			try:
				envelope = self._build_error_envelope(
					request_id=_new_request_id(),
					action="unknown",
					code=5303,
					message="Internal server error",
				)
				self._send_json_response(envelope, 500)
			except Exception:
				pass  # Don't crash if response fails

	def _handle_action(self, request_data: Dict[str, Any], request_id: str):
		"""
		Handle a bridge action request.

		Expected format:
		{
			"request_id": "uuid",
			"action": "get_current_focus",
			"params": {}
		}
		"""
		action = request_data.get("action")
		params = request_data.get("params")

		if action is None:
			envelope = self._build_error_envelope(
				request_id=request_id,
				action="unknown",
				code=4102,
				message="Missing 'action' field",
			)
			self._send_json_response(envelope, 400)
			return

		if not isinstance(action, str):
			envelope = self._build_error_envelope(
				request_id=request_id,
				action=str(action),
				code=4102,
				message="'action' must be a string",
			)
			self._send_json_response(envelope, 400)
			return

		action = action.strip()
		if not action:
			envelope = self._build_error_envelope(
				request_id=request_id,
				action="",
				code=4102,
				message="'action' cannot be empty",
			)
			self._send_json_response(envelope, 400)
			return

		if len(action) > 128:
			envelope = self._build_error_envelope(
				request_id=request_id,
				action=action,
				code=4102,
				message="'action' is too long (max 128 characters)",
			)
			self._send_json_response(envelope, 400)
			return

		if not isinstance(params, dict):
			envelope = self._build_error_envelope(
				request_id=request_id,
				action=action,
				code=4103,
				message="'params' must be a JSON object",
			)
			self._send_json_response(envelope, 400)
			return

		# Execute action and get HTTP envelope
		# The bridge layer handles all error wrapping and threading
		envelope = self.server.execute_action(request_id=request_id, action_name=action, params=params)

		# Determine HTTP status code from envelope
		if envelope.get("success"):
			status = 200
		else:
			error_code = (
				envelope.get("error", {}).get("code") if isinstance(envelope.get("error"), dict) else 5303
			)
			if not isinstance(error_code, int):
				error_code = 5303
			status = self._http_status_from_error_code(error_code)

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
		self.main_thread_executor = MainThreadExecutor(default_timeout=10.0)
		self.tool_executor = ToolExecutor()
		self.started_at_monotonic = time.monotonic()

		# Startup lifecycle:
		# 1) load output registry
		# 2) load immutable typed tool registry (NVDA_TOOLS_REGISTRY)
		# 3) freeze wiring and start HTTP server
		self.output_types = build_output_type_registry()
		validate_output_type_registry(self.output_types)

		# Use the new immutable typed registry built at module load time
		self.registry = tool_registry or NVDA_TOOLS_REGISTRY
		logHandler.log.info(f"bridge.tools.loaded count={len(self.registry)}")
		logHandler.log.debug(f"bridge.registry.frozen tools={len(self.registry)}")

		# Attach list_tools to server so the GET /tools handler can reach it
		# without holding a reference to the full bridge object.
		# Build complete tool descriptors with inputSchema and outputType
		def _build_tools_list():
			"""Build tools list for /tools endpoint in protocol v1 format."""
			tools = []
			for meta in self.registry.list_tools():
				input_schema_obj = self.registry.get_input_schema(meta.name, meta.version)
				output_schema_obj = self.registry.get_output_schema(meta.name, meta.version)

				# Get outputType name from the output schema model
				output_type = (
					output_schema_obj.model.__name__
					if hasattr(output_schema_obj, "model")
					else "ToolResponse"
				)

				# Generate input JSON schema using the InputSchema.to_json_schema() method
				input_json_schema = input_schema_obj.to_json_schema()

				tools.append(
					{
						"name": meta.name,
						"version": meta.version,
						"description": meta.description,
						"category": meta.category,
						"inputSchema": input_json_schema,
						"outputType": output_type,
					}
				)
			return tools

		self._list_tools = _build_tools_list
		self._list_output_types = lambda: dict(self.output_types)

	def get_nvda_status(self) -> Dict[str, bool]:
		secure_screen = False
		modal_blocking = False
		menu_open = False
		speech_available = False
		braille_available = False

		try:
			import globalVars

			secure_screen = bool(getattr(getattr(globalVars, "appArgs", None), "secure", False))
		except Exception:
			pass

		try:
			from utils.security import isRunningOnSecureDesktop

			secure_screen = bool(isRunningOnSecureDesktop()) or secure_screen
		except Exception:
			pass

		try:
			from gui.message import isModalMessageBoxActive

			modal_blocking = bool(isModalMessageBoxActive())
		except Exception:
			pass

		try:
			import appModules.nvda

			menu_open = appModules.nvda.nvdaMenuIaIdentity is not None
		except Exception:
			pass

		try:
			import speech

			speech_available = hasattr(speech, "speakText")
		except Exception:
			speech_available = False

		try:
			import braille

			braille_available = hasattr(braille, "handler")
		except Exception:
			braille_available = False

		return {
			"running": True,
			"secure_screen": secure_screen,
			"modal_blocking": modal_blocking,
			"menu_open": menu_open,
			"speech_available": speech_available,
			"braille_available": braille_available,
		}

	def get_nvda_status_snapshot(self) -> Dict[str, bool]:
		status = self.get_nvda_status()
		return {
			"secure_screen": bool(status.get("secure_screen", False)),
			"modal_blocking": bool(status.get("modal_blocking", False)),
			"menu_open": bool(status.get("menu_open", False)),
		}

	def get_health_payload(self) -> Dict[str, Any]:
		return {
			"status": "ok",
			"protocol": PROTOCOL,
			"protocol_version": PROTOCOL_VERSION,
			"bridge_version": BRIDGE_VERSION,
			"api_level": API_LEVEL,
			"supported_api_levels": [API_LEVEL],
			"uptime_seconds": int(max(0.0, time.monotonic() - self.started_at_monotonic)),
			"timestamp_utc": datetime.now(timezone.utc).isoformat(),
			"nvda_status": self.get_nvda_status(),
		}

	def execute_action(self, request_id: str, action_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
		"""
		Execute a bridge action with full error handling and threading.

		This is the orchestration layer that:
		1. Validates input
		2. Gets tool from registry
		3. Executes through ToolExecutor (validates input, executes, validates output)
		4. Wraps result in HTTP-compatible envelope
		5. Logs execution time for observability

		Args:
			request_id: Unique request identifier
			action_name: Name of the action/tool to execute
			params: Parameters to pass to the tool

		Returns:
			HTTP envelope with action metadata:
			Success: {"success": True, "action": str, "duration_ms": float, "data": {...}}
			Error: {"success": False, "action": str, "duration_ms": float, "error": {...}}
		"""
		start_time = time.perf_counter()
		logHandler.log.debug(f"bridge.action.start action={action_name}")

		try:
			# NOTE: action_name and params are already fully validated by
			# _handle_action before this method is called.

			# Look up the tool in the immutable registry
			tool = self.registry.get(action_name)
			if not tool:
				logHandler.log.warning(f"bridge.action.unknown action={action_name}")
				return {
					"request_id": request_id,
					"success": False,
					"action": action_name,
					"duration_ms": round((time.perf_counter() - start_time) * 1000, 2),
					"bridge": {"bridge_version": BRIDGE_VERSION, "api_level": API_LEVEL},
					"nvda_status_snapshot": self.get_nvda_status_snapshot(),
					"error": {
						"code": 4102,
						"name": ERROR_CODES[4102]["name"],
						"message": f"Unknown action: {action_name}",
						"category": ERROR_CODES[4102]["category"],
						"retry_policy": ERROR_CODES[4102]["retry_policy"],
					},
				}

			# Check NVDA state constraints
			status_snapshot = self.get_nvda_status_snapshot()
			if status_snapshot["secure_screen"] and tool.get_metadata().category not in {
				"diagnostics",
				"developer",
			}:
				return {
					"request_id": request_id,
					"success": False,
					"action": action_name,
					"duration_ms": round((time.perf_counter() - start_time) * 1000, 2),
					"bridge": {"bridge_version": BRIDGE_VERSION, "api_level": API_LEVEL},
					"nvda_status_snapshot": status_snapshot,
					"error": {
						"code": 4201,
						"name": ERROR_CODES[4201]["name"],
						"message": "Action blocked while NVDA is on secure desktop",
						"category": ERROR_CODES[4201]["category"],
						"retry_policy": ERROR_CODES[4201]["retry_policy"],
					},
				}

			if status_snapshot["modal_blocking"] and tool.get_metadata().category in {
				"focus",
				"navigation",
				"ui",
				"window",
				"text",
			}:
				return {
					"request_id": request_id,
					"success": False,
					"action": action_name,
					"duration_ms": round((time.perf_counter() - start_time) * 1000, 2),
					"bridge": {"bridge_version": BRIDGE_VERSION, "api_level": API_LEVEL},
					"nvda_status_snapshot": status_snapshot,
					"error": {
						"code": 4202,
						"name": ERROR_CODES[4202]["name"],
						"message": "Action blocked by modal NVDA dialog",
						"category": ERROR_CODES[4202]["category"],
						"retry_policy": ERROR_CODES[4202]["retry_policy"],
						"retry_after_ms": ERROR_CODES[4202]["retry_after_ms"],
					},
				}

			if status_snapshot["menu_open"] and tool.get_metadata().category in {
				"focus",
				"navigation",
				"ui",
				"window",
			}:
				return {
					"request_id": request_id,
					"success": False,
					"action": action_name,
					"duration_ms": round((time.perf_counter() - start_time) * 1000, 2),
					"bridge": {"bridge_version": BRIDGE_VERSION, "api_level": API_LEVEL},
					"nvda_status_snapshot": status_snapshot,
					"error": {
						"code": 4203,
						"name": ERROR_CODES[4203]["name"],
						"message": "Action blocked while NVDA menu is open",
						"category": ERROR_CODES[4203]["category"],
						"retry_policy": ERROR_CODES[4203]["retry_policy"],
						"retry_after_ms": ERROR_CODES[4203]["retry_after_ms"],
					},
				}

			# Execute tool through ToolExecutor pipeline:
			# - Validates input against schema
			# - Executes tool on main thread (via MainThreadExecutor)
			# - Validates output
			# - Serializes to JSON-compatible dict
			resolved_params = params or {}

			def _execute_tool():
				return self.tool_executor.execute_and_serialize(tool, resolved_params)

			# Execute on main thread for thread safety
			result = self.main_thread_executor.execute(_execute_tool)

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

			# Flatten ToolExecutor envelope to protocol envelope.
			# ToolExecutor returns: {"success": bool, "output": ...} or {"success": False, "error": ...}
			if bool(result.get("success")):
				return {
					"request_id": request_id,
					"success": True,
					"action": action_name,
					"duration_ms": round(duration_ms, 2),
					"bridge": {"bridge_version": BRIDGE_VERSION, "api_level": API_LEVEL},
					"nvda_status_snapshot": self.get_nvda_status_snapshot(),
					"data": result.get("output"),
				}

			inner_error = result.get("error") if isinstance(result.get("error"), dict) else {}
			inner_message = str(inner_error.get("message") or "Tool execution failed")
			return {
				"request_id": request_id,
				"success": False,
				"action": action_name,
				"duration_ms": round(duration_ms, 2),
				"bridge": {"bridge_version": BRIDGE_VERSION, "api_level": API_LEVEL},
				"nvda_status_snapshot": self.get_nvda_status_snapshot(),
				"error": {
					"code": 5303,
					"name": ERROR_CODES[5303]["name"],
					"message": inner_message,
					"category": ERROR_CODES[5303]["category"],
					"retry_policy": ERROR_CODES[5303]["retry_policy"],
					"details": inner_error,
				},
			}

		except TimeoutError as e:
			duration_ms = (time.perf_counter() - start_time) * 1000
			logHandler.log.error(
				f"bridge.action.timeout action={action_name} duration_ms={duration_ms:.1f} error={str(e)}",
				exc_info=True,
			)
			return {
				"request_id": request_id,
				"success": False,
				"action": action_name,
				"duration_ms": round(duration_ms, 2),
				"bridge": {"bridge_version": BRIDGE_VERSION, "api_level": API_LEVEL},
				"nvda_status_snapshot": self.get_nvda_status_snapshot(),
				"error": {
					"code": 5001,
					"name": ERROR_CODES[5001]["name"],
					"message": f"Operation timed out: {str(e)}",
					"category": ERROR_CODES[5001]["category"],
					"retry_policy": ERROR_CODES[5001]["retry_policy"],
					"retry_after_ms": ERROR_CODES[5001]["retry_after_ms"],
				},
			}
		except ValueError as e:
			# Input validation errors from ToolExecutor
			duration_ms = (time.perf_counter() - start_time) * 1000
			logHandler.log.warning(
				f"bridge.action.validation_failed action={action_name} "
				f"duration_ms={duration_ms:.1f} error={str(e)}"
			)
			return {
				"request_id": request_id,
				"success": False,
				"action": action_name,
				"duration_ms": round(duration_ms, 2),
				"bridge": {"bridge_version": BRIDGE_VERSION, "api_level": API_LEVEL},
				"nvda_status_snapshot": self.get_nvda_status_snapshot(),
				"error": {
					"code": 4103,
					"name": ERROR_CODES[4103]["name"],
					"message": str(e),
					"category": ERROR_CODES[4103]["category"],
					"retry_policy": ERROR_CODES[4103]["retry_policy"],
				},
			}
		except Exception as e:
			# Unexpected errors
			duration_ms = (time.perf_counter() - start_time) * 1000
			logHandler.log.error(
				f"bridge.action.failed action={action_name} duration_ms={duration_ms:.1f} error={str(e)}",
				exc_info=True,
			)
			return {
				"request_id": request_id,
				"success": False,
				"action": action_name,
				"duration_ms": round(duration_ms, 2),
				"bridge": {"bridge_version": BRIDGE_VERSION, "api_level": API_LEVEL},
				"nvda_status_snapshot": self.get_nvda_status_snapshot(),
				"error": {
					"code": 5003,
					"name": ERROR_CODES[5003]["name"],
					"message": f"Execution failed: {str(e)}",
					"category": ERROR_CODES[5003]["category"],
					"retry_policy": ERROR_CODES[5003]["retry_policy"],
					"retry_after_ms": ERROR_CODES[5003]["retry_after_ms"],
				},
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
			self.server.list_output_types = self._list_output_types
			self.server.get_nvda_status = self.get_nvda_status
			self.server.get_nvda_status_snapshot = self.get_nvda_status_snapshot
			self.server.get_health_payload = self.get_health_payload

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
