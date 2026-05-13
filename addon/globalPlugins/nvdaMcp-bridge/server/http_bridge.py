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
from .protocol import (
	API_LEVEL,
	BRIDGE_VERSION,
	DEFAULT_BRIDGE_TOKEN,
	ERROR_CODES,
	MAX_REQUEST_BODY_SIZE,
	MAX_REQUEST_ID_LENGTH,
	MENU_OPEN_BLOCKED_CATEGORIES,
	MODAL_BLOCKING_BLOCKED_CATEGORIES,
	PROTOCOL,
	PROTOCOL_VERSION,
	SECURE_SCREEN_ALLOWED_CATEGORIES,
	TOKEN_HEADER,
	http_status_from_error_code,
	validate_tools_discovery_payload,
	build_tools_payload,
)
from .response import build_action_error, build_error_envelope
from ..validation.output_type_registry import build_output_type_registry, validate_output_type_registry


def _new_request_id() -> str:
	return str(uuid.uuid4())


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

	@staticmethod
	def _validate_tools_discovery_payload(payload: Dict[str, Any]) -> None:
		return validate_tools_discovery_payload(payload)

	def _build_tools_payload(self, tools: list[Dict[str, Any]], types: Dict[str, Any]) -> Dict[str, Any]:
		return build_tools_payload(tools, types)

	def _build_error_envelope(
		self,
		request_id: str,
		action: str,
		code: int,
		message: str,
		duration_ms: float = 0.0,
		details: Optional[Dict[str, Any]] = None,
	) -> Dict[str, Any]:
		return build_error_envelope(
			request_id=request_id,
			action=action,
			code=code,
			message=message,
			nvda_status_snapshot=self.server.get_nvda_status_snapshot(),
			duration_ms=duration_ms,
			details=details,
		)

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
				return

			if self.path == "/tools":
				self._handle_tools_discovery()
				return

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

	def _handle_tools_discovery(self):
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

		payload = build_tools_payload(tools, types)
		validate_tools_discovery_payload(payload)
		self._send_json_response(payload)

	def do_POST(self):
		"""Handle POST requests for bridge actions with comprehensive error handling"""
		try:
			request_id = _new_request_id()

			if not self._check_auth():
				envelope = self._build_error_envelope(
					request_id=request_id,
					action="unknown",
					code=4001,
					message="Unauthorized (invalid token)",
				)
				self._send_json_response(envelope, 401)
				return

			request_data = self._parse_request_body(request_id)
			if request_data is None:
				return

			if self.path in ("/action", "/execute"):
				action, params, request_id = self._validate_action_request(request_data, request_id)
				if action is None:
					return
				self._handle_action(action, params, request_id)
				return

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

	def _parse_request_body(self, request_id: str) -> Optional[Dict[str, Any]]:
		try:
			content_length = int(self.headers.get("Content-Length", 0))
			if content_length < 0:
				envelope = self._build_error_envelope(
					request_id=request_id,
					action="unknown",
					code=4101,
					message="Invalid Content-Length",
				)
				self._send_json_response(envelope, 400)
				return None

			if content_length > MAX_REQUEST_BODY_SIZE:
				envelope = self._build_error_envelope(
					request_id=request_id,
					action="unknown",
					code=4101,
					message="Request too large",
				)
				self._send_json_response(envelope, 413)
				return None

			if content_length == 0:
				envelope = self._build_error_envelope(
					request_id=request_id,
					action="unknown",
					code=4101,
					message="Empty request body",
				)
				self._send_json_response(envelope, 400)
				return None

			body = self.rfile.read(content_length)
			request_data = json.loads(body.decode("utf-8"))
			if not isinstance(request_data, dict):
				envelope = self._build_error_envelope(
					request_id=request_id,
					action="unknown",
					code=4101,
					message="Request must be a JSON object",
				)
				self._send_json_response(envelope, 400)
				return None
			return request_data
		except json.JSONDecodeError as e:
			envelope = self._build_error_envelope(
				request_id=request_id,
				action="unknown",
				code=4101,
				message=f"Invalid JSON: {e}",
			)
			self._send_json_response(envelope, 400)
			return None
		except ValueError as e:
			envelope = self._build_error_envelope(
				request_id=request_id,
				action="unknown",
				code=4101,
				message=f"Invalid content length: {e}",
			)
			self._send_json_response(envelope, 400)
			return None
		except Exception as e:
			logHandler.log.error(f"Error reading request: {e}", exc_info=True)
			envelope = self._build_error_envelope(
				request_id=request_id,
				action="unknown",
				code=4101,
				message=f"Error reading request: {e}",
			)
			self._send_json_response(envelope, 400)
			return None

	def _validate_action_request(self, request_data: Dict[str, Any], request_id: str) -> tuple[Optional[str], Optional[Dict[str, Any]], str]:
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
			return None, None, request_id

		if not isinstance(action, str):
			envelope = self._build_error_envelope(
				request_id=request_id,
				action=str(action),
				code=4102,
				message="'action' must be a string",
			)
			self._send_json_response(envelope, 400)
			return None, None, request_id

		action = action.strip()
		if not action:
			envelope = self._build_error_envelope(
				request_id=request_id,
				action="",
				code=4102,
				message="'action' cannot be empty",
			)
			self._send_json_response(envelope, 400)
			return None, None, request_id

		if len(action) > MAX_REQUEST_ID_LENGTH:
			envelope = self._build_error_envelope(
				request_id=request_id,
				action=action,
				code=4102,
				message="'action' is too long (max 128 characters)",
			)
			self._send_json_response(envelope, 400)
			return None, None, request_id

		if "params" not in request_data:
			envelope = self._build_error_envelope(
				request_id=request_id,
				action=action,
				code=4103,
				message="Missing 'params' field",
			)
			self._send_json_response(envelope, 400)
			return None, None, request_id

		if not isinstance(params, dict):
			envelope = self._build_error_envelope(
				request_id=request_id,
				action=action,
				code=4103,
				message="'params' must be a JSON object",
			)
			self._send_json_response(envelope, 400)
			return None, None, request_id

		return action, params, request_id

	def _handle_action(self, action: str, params: Dict[str, Any], request_id: str):
		"""Execute a validated bridge action request."""
		envelope = self.server.execute_action(request_id=request_id, action_name=action, params=params)
		if envelope.get("success"):
			status = 200
		else:
			error_code = (
				envelope.get("error", {}).get("code") if isinstance(envelope.get("error"), dict) else 5303
			)
			if not isinstance(error_code, int):
				error_code = 5303
			status = http_status_from_error_code(error_code)
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
		self._list_tools = self._build_tools_list
		self._list_output_types = self._build_output_types

	@staticmethod
	def _compute_tool_constraints(meta: Any) -> Dict[str, Any]:
		"""Compute bridge-level constraints for a tool based on its category and name.

		These constraints are surfaced in the tool discovery payload so downstream
		consumers (Rust MCP server, AI agents) know when a tool is expected to be
		blocked by NVDA state without having to replicate NVDA state-check logic.
		"""
		category = meta.category
		name = meta.name
		constraints: Dict[str, Any] = {}

		# NVDA state-based blocking hints (mirrors _check_action_allowed logic)
		constraints["blocked_on_secure_screen"] = category not in SECURE_SCREEN_ALLOWED_CATEGORIES
		constraints["blocked_on_modal"] = category in MODAL_BLOCKING_BLOCKED_CATEGORIES
		constraints["blocked_on_menu"] = category in MENU_OPEN_BLOCKED_CATEGORIES

		# Dangerous / opt-in tools
		constraints["dangerous"] = name in ("execute_python",)
		constraints["requires_explicit_opt_in"] = name in ("execute_python",)

		return constraints

	def _build_tools_list(self) -> list[Dict[str, Any]]:
		"""Build tools list for /tools endpoint in protocol v1 format.

		Each tool entry includes metadata, JSON Schema input, named output type
		reference, and computed constraints for downstream AI agents.
		"""
		tools = []
		for meta in self.registry.list_tools():
			input_schema_obj = self.registry.get_input_schema(meta.name, meta.version)
			output_schema_obj = self.registry.get_output_schema(meta.name, meta.version)

			tools.append(
				{
					"name": meta.name,
					"version": meta.version,
					"description": meta.description,
					"category": meta.category,
					"inputSchema": input_schema_obj.to_json_schema(),
					"outputType": self._get_output_type_name(output_schema_obj),
					"constraints": self._compute_tool_constraints(meta),
				}
			)
		return tools

	def _get_output_type_name(self, output_schema_obj: Any) -> str:
		"""Resolve a schema's output type name for tool discovery."""
		return output_schema_obj.model.__name__ if hasattr(output_schema_obj, "model") else "ToolResponse"

	def _build_output_types(self) -> Dict[str, Any]:
		"""Return the bridge output type registry."""
		return dict(self.output_types)

	def _bridge_metadata(self) -> Dict[str, Any]:
		return {"bridge_version": BRIDGE_VERSION, "api_level": API_LEVEL}

	def _check_action_allowed(self, request_id: str, action_name: str, tool_category: str, status_snapshot: Dict[str, bool], duration_ms: float) -> Optional[Dict[str, Any]]:
		if status_snapshot["secure_screen"] and tool_category not in SECURE_SCREEN_ALLOWED_CATEGORIES:
			return build_action_error(
				request_id=request_id,
				action_name=action_name,
				duration_ms=0.0,
				code=4201,
				message="Action blocked while NVDA is on secure desktop",
				nvda_status_snapshot=self.get_nvda_status_snapshot(),
			)

		if status_snapshot["modal_blocking"] and tool_category in MODAL_BLOCKING_BLOCKED_CATEGORIES:
			return build_action_error(
				request_id=request_id,
				action_name=action_name,
				duration_ms=0.0,
				code=4202,
				message="Action blocked by modal NVDA dialog",
				nvda_status_snapshot=self.get_nvda_status_snapshot(),
			)

		if status_snapshot["menu_open"] and tool_category in MENU_OPEN_BLOCKED_CATEGORIES:
			return build_action_error(
				request_id=request_id,
				action_name=action_name,
				duration_ms=0.0,
				code=4203,
				message="Action blocked while NVDA menu is open",
				nvda_status_snapshot=self.get_nvda_status_snapshot(),
			)

		return None

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

			# Check NVDA state constraints using centralized policy helpers
			status_snapshot = self.get_nvda_status_snapshot()
			duration_ms = (time.perf_counter() - start_time) * 1000
			blocked = self._check_action_allowed(request_id, action_name, tool.get_metadata().category, status_snapshot, duration_ms)
			if blocked is not None:
				return blocked

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
