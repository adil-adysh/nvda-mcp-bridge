"""
NVDA HTTP Bridge
Provides a minimal local HTTP server that wraps NVDA APIs for external access.

This is a thin bridge layer - it does NOT implement MCP protocol.
All MCP logic is handled by the separate nvda-mcp-server process.

Architecture:
- NVDA Bridge (this module): Hardened localhost HTTP bridge on 127.0.0.1:8765
- nvda-mcp-server (standalone): MCP protocol implementation on stdio
- AI Client (VS Code, Claude, etc.): Connects to nvda-mcp-server via MCP

Thread Safety & Responsiveness:
- HTTP server runs in background daemon thread
- All NVDA API calls are safely dispatched to NVDA's main thread
- Main thread operations are non-blocking; repeated keystroke delays execute in background
- Requests enforce socket deadlines, connection limits, and Host/Origin validation
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse, urlsplit
from typing import Dict, Any, Optional
import logHandler

from ..core import (
	ExecutionContext,
	ExecutionMode,
	RegistryError,
	ToolExecutor,
	ToolRegistry,
	protocol_code_for_serialized,
)
from ..tools.registry import NVDA_TOOLS_REGISTRY
from .main_thread_executor import MainThreadExecutor
from .protocol import (
	API_LEVEL,
	BRIDGE_VERSION,
	ERROR_CODES,
	MAX_REQUEST_BODY_SIZE,
	MAX_REQUEST_ID_LENGTH,
	PROTOCOL,
	PROTOCOL_VERSION,
	TOKEN_HEADER,
	http_status_from_error_code,
	validate_tools_discovery_payload,
	build_tools_payload,
)
from .events import EventJournal
from .response import build_action_error, build_error_envelope
from .security import (
	DeveloperModeManager,
	PermissionPolicy,
	check_action_permissions,
	constant_time_compare,
	get_or_create_bridge_token,
	load_persisted_permissions,
)
from ..validation.output_type_registry import build_output_type_registry, validate_output_type_registry

#: Upper bound on how much of an oversized body is read and discarded before
#: answering 413, so the response reaches the client instead of a socket reset.
_MAX_DRAIN_BYTES = 8 * 1024 * 1024

#: Longest a /events long-poll may hold a worker thread. The bridge serves a
#: bounded thread pool, so waiters must be short-lived and few.
_MAX_EVENT_WAIT_MS = 30000


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
	"""Multi-threaded HTTP server with bounded concurrency to prevent resource exhaustion."""

	daemon_threads = True
	max_concurrency = 16

	def __init__(self, server_address, RequestHandlerClass, bind_and_activate=True):
		super().__init__(server_address, RequestHandlerClass, bind_and_activate=bind_and_activate)
		self.concurrency_semaphore = threading.Semaphore(self.max_concurrency)

	def process_request(self, request, client_address):
		if not self.concurrency_semaphore.acquire(blocking=False):
			try:
				request.settimeout(2.0)
				request.sendall(
					b"HTTP/1.1 503 Service Unavailable\r\n"
					b"Content-Type: application/json\r\n"
					b"Connection: close\r\n\r\n"
					b'{"success": false, "error": {"code": 5003, "message": "Server concurrency limit reached"}}\n'
				)
			except Exception:
				pass
			finally:
				self.shutdown_request(request)
			return

		def _worker():
			try:
				super(BoundedThreadingHTTPServer, self).process_request(request, client_address)
			finally:
				self.concurrency_semaphore.release()

		t = threading.Thread(target=_worker, daemon=True, name="NVDA-HTTP-Worker")
		t.start()



def _new_request_id() -> str:
	return str(uuid.uuid4())


class NVDABridgeRequestHandler(BaseHTTPRequestHandler):
	"""HTTP request handler for NVDA bridge with security hardening."""

	def setup(self):
		"""Set up request socket with strict deadline to prevent hanging."""
		super().setup()
		try:
			self.request.settimeout(15.0)
		except Exception:
			pass

	def log_message(self, format, *args):
		"""Suppress verbose HTTP server logging to avoid clutter and potential log crashes."""
		pass

	def _send_json_response(
		self,
		data: Dict[str, Any],
		status_code: int = 200,
		extra_headers: Optional[Dict[str, str]] = None,
	):
		"""Send JSON response with error handling and secure headers."""
		try:
			self.send_response(status_code)
			self.send_header("Content-Type", "application/json")
			self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1")
			self.send_header("X-Content-Type-Options", "nosniff")
			for name, value in (extra_headers or {}).items():
				self.send_header(name, value)
			self.end_headers()
			response_json = json.dumps(data, ensure_ascii=False, indent=2)
			self.wfile.write(response_json.encode("utf-8"))
		except Exception as e:
			logHandler.log.error(f"Error sending response: {e}", exc_info=True)

	def _validate_host_and_origin(self) -> bool:
		"""Validate Host and Origin headers using strict URL parsing to prevent DNS rebinding and cross-site attacks."""
		host_header = self.headers.get("Host", "").strip()
		if host_header:
			split_host = urlsplit(f"//{host_header}")
			hostname = (split_host.hostname or "").lower()
			if hostname not in ("127.0.0.1", "localhost", "::1"):
				logHandler.log.warning(f"Rejected request with unauthorized Host header: {host_header}")
				return False

		origin = self.headers.get("Origin", "").strip()
		if origin:
			parsed_origin = urlparse(origin)
			if parsed_origin.scheme not in ("http", "https"):
				logHandler.log.warning(f"Rejected request with unauthorized Origin scheme: {origin}")
				return False
			origin_host = (parsed_origin.hostname or "").lower()
			if origin_host not in ("127.0.0.1", "localhost", "::1"):
				logHandler.log.warning(f"Rejected cross-origin request from unauthorized Origin: {origin}")
				return False
		return True

	def _check_auth(self) -> bool:
		"""Check bridge token authentication using constant-time comparison. Fails closed."""
		token = self.headers.get(TOKEN_HEADER, "").strip()
		if not token:
			auth_header = self.headers.get("Authorization", "").strip()
			if auth_header.startswith("Bearer "):
				token = auth_header[7:].strip()
		expected_token = getattr(self.server, "bridge_token", "")
		return constant_time_compare(token, expected_token)

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
		"""Handle CORS preflight with localhost restriction."""
		if not self._validate_host_and_origin():
			self.send_response(403)
			self.end_headers()
			return

		self.send_response(200)
		self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1")
		self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		self.send_header("Access-Control-Allow-Headers", f"Content-Type, {TOKEN_HEADER}")
		self.end_headers()

	def _method_not_allowed(self) -> None:
		"""Answer unsupported HTTP methods with a JSON envelope instead of HTML."""
		self._drain_request_body(int(self.headers.get("Content-Length", 0) or 0))
		self.close_connection = True
		envelope = self._build_error_envelope(
			request_id=_new_request_id(),
			action="http_method",
			code=4102,
			message=f"HTTP method {self.command} is not supported; use GET or POST",
		)
		self._send_json_response(envelope, 405, extra_headers={"Allow": "GET, POST, OPTIONS"})

	def do_PUT(self):
		self._method_not_allowed()

	def do_DELETE(self):
		self._method_not_allowed()

	def do_PATCH(self):
		self._method_not_allowed()

	def do_HEAD(self):
		self._method_not_allowed()

	def do_GET(self):
		"""Handle GET requests with error handling."""
		try:
			if not self._validate_host_and_origin():
				envelope = self._build_error_envelope(
					request_id=_new_request_id(),
					action="http_get",
					code=4001,
					message="Forbidden Host/Origin header",
				)
				self._send_json_response(envelope, 403)
				return

			if self.path == "/health":
				self._send_json_response(self.server.get_health_payload())
				return

			if self.path == "/tools":
				self._handle_tools_discovery()
				return

			route = urlsplit(self.path)
			if route.path == "/state":
				self._handle_state(route)
				return

			if route.path == "/events":
				self._handle_events(route)
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
				pass

	def _require_auth(self, action: str) -> bool:
		"""Send a 401 envelope when the bridge token is missing or wrong."""
		if self._check_auth():
			return True
		envelope = self._build_error_envelope(
			request_id=_new_request_id(),
			action=action,
			code=4001,
			message="Unauthorized (invalid or missing token)",
		)
		self._send_json_response(envelope, 401)
		return False

	def _handle_state(self, route) -> None:
		"""Serve the current observation as a single document."""
		if not self._require_auth("state"):
			return
		self._send_json_response(self.server.get_state_payload())

	def _handle_events(self, route) -> None:
		"""Serve event history, or block until the version advances.

		``since`` is a cursor; ``timeout_ms > 0`` turns the call into a long-poll.
		``timeout_ms`` is capped so a client cannot pin a worker thread forever.
		"""
		if not self._require_auth("events"):
			return

		query = parse_qs(route.query)

		def _int_param(name: str, default: int) -> int:
			try:
				return int(query.get(name, [default])[0])
			except (TypeError, ValueError):
				return default

		since = max(0, _int_param("since", 0))
		limit = max(1, min(1000, _int_param("limit", 100)))
		timeout_ms = max(0, min(_MAX_EVENT_WAIT_MS, _int_param("timeout_ms", 0)))
		kinds = [kind for kind in ",".join(query.get("kinds", [""])).split(",") if kind]

		payload = self.server.event_journal.wait(
			since=since,
			timeout_ms=timeout_ms,
			limit=limit,
			kinds=kinds,
		)
		self._send_json_response(payload)

	def _handle_tools_discovery(self):
		if not self._check_auth():
			request_id = _new_request_id()
			envelope = self._build_error_envelope(
				request_id=request_id,
				action="tools_discovery",
				code=4001,
				message="Unauthorized (invalid or missing token)",
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
		"""Handle POST requests for bridge actions with comprehensive error handling."""
		try:
			request_id = _new_request_id()

			if not self._validate_host_and_origin():
				envelope = self._build_error_envelope(
					request_id=request_id,
					action="unknown",
					code=4001,
					message="Forbidden Host/Origin header",
				)
				self._send_json_response(envelope, 403)
				return

			if not self._check_auth():
				envelope = self._build_error_envelope(
					request_id=request_id,
					action="unknown",
					code=4001,
					message="Unauthorized (invalid or missing token)",
				)
				self._send_json_response(envelope, 401)
				return

			request_data = self._parse_request_body(request_id)
			if request_data is None:
				return

			if self.path in ("/action", "/execute"):
				action, params, request_id, timeout_ms = self._validate_action_request(request_data, request_id)
				if action is None:
					return
				self._handle_action(action, params or {}, request_id, timeout_ms=timeout_ms)
				return

			if self.path == "/admin/developer-mode":
				self._handle_developer_mode(request_data, request_id)
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
				pass

	def _handle_developer_mode(self, request_data: Dict[str, Any], request_id: str):
		"""Handle developer mode toggle requests with mandatory local user authorization."""
		enabled = request_data.get("enabled")
		if not isinstance(enabled, bool):
			envelope = self._build_error_envelope(
				request_id=request_id,
				action="admin/developer-mode",
				code=4101,
				message="Missing or non-boolean 'enabled' field",
			)
			self._send_json_response(envelope, 400)
			return

		manager: DeveloperModeManager = getattr(self.server, "developer_mode_manager", None)
		if not manager:
			envelope = self._build_error_envelope(
				request_id=request_id,
				action="admin/developer-mode",
				code=5303,
				message="DeveloperModeManager not available",
			)
			self._send_json_response(envelope, 500)
			return

		if not enabled:
			# Disabling developer mode is always permitted and takes effect immediately
			manager.disable()
			self._send_json_response({
				"success": True,
				"developer_mode": False,
				"message": "Developer mode disabled successfully",
			})
			return

		# Enabling developer mode requires independent local approval
		local_approval_token = request_data.get("local_approval_token")
		if not local_approval_token or not isinstance(local_approval_token, str):
			envelope = self._build_error_envelope(
				request_id=request_id,
				action="admin/developer-mode",
				code=4003,
				message="Developer mode elevation requires independent local user approval; "
				"ordinary bridge token cannot authorize developer mode.",
			)
			self._send_json_response(envelope, 403)
			return

		duration_seconds = request_data.get("duration_seconds")
		if duration_seconds is not None and (not isinstance(duration_seconds, int) or duration_seconds < 1):
			duration_seconds = None

		if not manager.approve_with_token(local_approval_token, duration_seconds=duration_seconds):
			envelope = self._build_error_envelope(
				request_id=request_id,
				action="admin/developer-mode",
				code=4003,
				message="Invalid, expired, or previously consumed local developer approval token",
			)
			self._send_json_response(envelope, 403)
			return

		self._send_json_response({
			"success": True,
			"developer_mode": True,
			"time_remaining_seconds": round(manager.time_remaining_seconds, 1),
			"message": "Developer mode enabled successfully via verified local approval",
		})

	def _drain_request_body(self, length: int) -> None:
		"""Read and discard up to ``length`` bytes of an unwanted request body."""
		remaining = max(0, length)
		while remaining > 0:
			try:
				chunk = self.rfile.read(min(65536, remaining))
			except Exception:  # noqa: BLE001 — draining is best-effort
				return
			if not chunk:
				return
			remaining -= len(chunk)

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
				# Drain what the client already pushed so the socket closes cleanly.
				# Answering without reading makes the kernel reset the connection,
				# and the caller then sees a transport error instead of this JSON
				# envelope explaining what was wrong.
				self._drain_request_body(min(content_length, _MAX_DRAIN_BYTES))
				self.close_connection = True
				envelope = self._build_error_envelope(
					request_id=request_id,
					action="unknown",
					code=4101,
					message=(
						f"Request too large: {content_length} bytes exceeds the "
						f"{MAX_REQUEST_BODY_SIZE} byte limit"
					),
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
			if len(body) < content_length:
				envelope = self._build_error_envelope(
					request_id=request_id,
					action="unknown",
					code=4101,
					message=f"Incomplete request body: received {len(body)} of {content_length} bytes",
				)
				self._send_json_response(envelope, 400)
				return None

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

	def _validate_action_request(
		self, request_data: Dict[str, Any], request_id: str
	) -> tuple[Optional[str], Optional[Dict[str, Any]], str, Optional[int]]:
		action = request_data.get("action")
		params = request_data.get("params")
		timeout_ms = request_data.get("timeout_ms")

		# Honour a caller-supplied id so requests can be correlated with client
		# logs and with the bridge audit trail. Generate one only when absent.
		client_request_id = request_data.get("request_id")
		if client_request_id is not None:
			if not isinstance(client_request_id, str):
				envelope = self._build_error_envelope(
					request_id=request_id,
					action="unknown",
					code=4101,
					message="'request_id' must be a string",
				)
				self._send_json_response(envelope, 400)
				return None, None, request_id, None
			client_request_id = client_request_id.strip()
			if len(client_request_id) > MAX_REQUEST_ID_LENGTH:
				envelope = self._build_error_envelope(
					request_id=request_id,
					action="unknown",
					code=4101,
					message=f"'request_id' is too long (max {MAX_REQUEST_ID_LENGTH} characters)",
				)
				self._send_json_response(envelope, 400)
				return None, None, request_id, None
			if client_request_id:
				request_id = client_request_id

		if timeout_ms is not None:
			if not isinstance(timeout_ms, int) or timeout_ms < 100 or timeout_ms > 300000:
				envelope = self._build_error_envelope(
					request_id=request_id,
					action="unknown",
					code=4101,
					message="'timeout_ms' must be an integer between 100 and 300000",
				)
				self._send_json_response(envelope, 400)
				return None, None, request_id, None

		if action is None:
			envelope = self._build_error_envelope(
				request_id=request_id,
				action="unknown",
				code=4102,
				message="Missing 'action' field",
			)
			self._send_json_response(envelope, 400)
			return None, None, request_id, None

		if not isinstance(action, str):
			envelope = self._build_error_envelope(
				request_id=request_id,
				action=str(action),
				code=4102,
				message="'action' must be a string",
			)
			self._send_json_response(envelope, 400)
			return None, None, request_id, None

		action = action.strip()
		if not action:
			envelope = self._build_error_envelope(
				request_id=request_id,
				action="",
				code=4102,
				message="'action' cannot be empty",
			)
			self._send_json_response(envelope, 400)
			return None, None, request_id, None

		if len(action) > MAX_REQUEST_ID_LENGTH:
			envelope = self._build_error_envelope(
				request_id=request_id,
				action=action,
				code=4102,
				message="'action' is too long (max 128 characters)",
			)
			self._send_json_response(envelope, 400)
			return None, None, request_id, None

		if "params" not in request_data:
			envelope = self._build_error_envelope(
				request_id=request_id,
				action=action,
				code=4103,
				message="Missing 'params' field",
			)
			self._send_json_response(envelope, 400)
			return None, None, request_id, None

		if not isinstance(params, dict):
			envelope = self._build_error_envelope(
				request_id=request_id,
				action=action,
				code=4103,
				message="'params' must be a JSON object",
			)
			self._send_json_response(envelope, 400)
			return None, None, request_id, None

		return action, params, request_id, timeout_ms

	def _handle_action(
		self, action: str, params: Dict[str, Any], request_id: str, timeout_ms: Optional[int] = None
	):
		"""Execute a validated bridge action request with deadline and error mapping."""
		envelope = self.server.execute_action(
			request_id=request_id, action_name=action, params=params, timeout_ms=timeout_ms
		)
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
	"""Minimal HTTP bridge that runs inside NVDA process with hardened security."""

	def __init__(
		self,
		host: str = "127.0.0.1",
		port: int = 8765,
		shutdown_event: Optional[threading.Event] = None,
		bridge_token: Optional[str] = None,
		tool_registry: Optional[ToolRegistry] = None,
		developer_mode: bool = False,
		permission_policy: Optional[PermissionPolicy] = None,
	):
		self.host = host
		self.port = port
		self.shutdown_event = shutdown_event or threading.Event()
		self.permission_policy = permission_policy or PermissionPolicy()
		# Versioned UI event journal: backs /events (long-poll and history) and
		# the MCP companion's resource-update notifications.
		self.event_journal = EventJournal()
		if permission_policy is None:
			# Restore the capability set the user saved through the settings panel.
			# An unset value keeps the conservative in-memory defaults.
			persisted_permissions = load_persisted_permissions()
			if persisted_permissions is not None:
				self.permission_policy.set_granted(persisted_permissions)
		self.developer_mode_manager = DeveloperModeManager(
			initial_enabled=developer_mode, permission_policy=self.permission_policy
		)
		self.bridge_token = bridge_token if bridge_token is not None else get_or_create_bridge_token()
		self.server: Optional[BoundedThreadingHTTPServer] = None
		self.main_thread_executor = MainThreadExecutor(default_timeout=10.0)
		self.tool_executor = ToolExecutor()
		self.started_at_monotonic = time.monotonic()

		self.output_types = build_output_type_registry()
		validate_output_type_registry(self.output_types)

		self.registry = tool_registry or NVDA_TOOLS_REGISTRY
		logHandler.log.info(f"bridge.tools.loaded count={len(self.registry)}")
		logHandler.log.debug(f"bridge.registry.frozen tools={len(self.registry)}")

		self._list_tools = self._build_tools_list
		self._list_output_types = self._build_output_types

	def enable_developer_mode(self) -> None:
		self.developer_mode_manager.enable()

	def disable_developer_mode(self) -> None:
		self.developer_mode_manager.disable()

	@property
	def is_developer_mode_enabled(self) -> bool:
		return self.developer_mode_manager.is_enabled

	def grant_permission(self, permission: str) -> None:
		self.permission_policy.grant(permission)

	def revoke_permission(self, permission: str) -> None:
		self.permission_policy.revoke(permission)

	def reset_permissions(self) -> None:
		self.permission_policy.reset()

	@staticmethod
	def _compute_tool_constraints(meta: Any) -> Dict[str, Any]:
		"""Compute bridge-level constraints for a tool based on explicit metadata."""
		constraints: Dict[str, Any] = {}

		allowed_states = getattr(meta, "allowed_nvda_states", ("normal",))
		constraints["blocked_on_secure_screen"] = "secure_desktop" not in allowed_states
		constraints["blocked_on_modal"] = "modal" not in allowed_states
		constraints["blocked_on_menu"] = "menu" not in allowed_states

		is_developer = getattr(meta, "required_permission", "") == "developer" or meta.name == "execute_python"
		is_dangerous = getattr(meta, "destructive", False) or is_developer

		constraints["dangerous"] = is_dangerous
		constraints["requires_explicit_opt_in"] = is_developer or getattr(meta, "required_permission", "") == "nvda_lifecycle"
		constraints["requires_developer_mode"] = is_developer

		return constraints

	def _build_tools_list(self) -> list[Dict[str, Any]]:
		"""Build tools list for /tools endpoint in protocol v1 format.

		Omits developer-mode tools (like execute_python) when developer mode is disabled.
		"""
		tools = []
		for meta in self.registry.list_tools():
			# Exclude developer-only tools from discovery if developer mode is not active
			if getattr(meta, "required_permission", "") == "developer" and not self.developer_mode_manager.is_enabled:
				continue

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
					"readOnly": getattr(meta, "read_only", False),
					"destructive": getattr(meta, "destructive", False),
					"idempotent": getattr(meta, "idempotent", False),
					"sideEffects": getattr(meta, "side_effects", False),
					"requiredPermission": getattr(meta, "required_permission", "read_only"),
					"cancellationSupported": getattr(meta, "cancellation_supported", False),
					"retrySafe": getattr(meta, "retry_safe", False),
				}
			)
		return tools

	def _get_output_type_name(self, output_schema_obj: Any) -> str:
		return output_schema_obj.model.__name__ if hasattr(output_schema_obj, "model") else "ToolResponse"

	def _build_output_types(self) -> Dict[str, Any]:
		return dict(self.output_types)

	def _bridge_metadata(self) -> Dict[str, Any]:
		return {"bridge_version": BRIDGE_VERSION, "api_level": API_LEVEL}

	def get_state_payload(self, timeout: float = 2.0) -> Dict[str, Any]:
		"""Collect the current observation for the ``nvda://ui/state`` resource.

		NVDA API calls must run on the main thread; the caller is an HTTP worker.
		"""
		from ..nvda_bridge import focus as nvda_focus
		from ..nvda_bridge import ui as nvda_ui

		def _collect() -> Dict[str, Any]:
			try:
				focus = nvda_focus.get_current_focus()
			except Exception as e:  # noqa: BLE001
				focus = {"error": str(e)}
			try:
				window = nvda_ui.get_window_title()
			except Exception as e:  # noqa: BLE001
				window = {"error": str(e)}
			return {
				"state_version": self.event_journal.version,
				"focus": focus,
				"window": window,
				"nvda": self.get_nvda_status(),
			}

		return self.main_thread_executor.execute(_collect, timeout=timeout)

	def get_events_payload(
		self,
		since: int = 0,
		timeout_ms: int = 0,
		limit: int = 100,
		kinds: Optional[list] = None,
	) -> Dict[str, Any]:
		"""Thin wrapper so the journal is reachable through the bridge object."""
		return self.event_journal.wait(since=since, timeout_ms=timeout_ms, limit=limit, kinds=kinds)

	def _check_action_allowed(
		self,
		request_id: str,
		action_name: str,
		metadata: Any = None,
		status_snapshot: Optional[Dict[str, bool]] = None,
		duration_ms: float = 0.0,
		tool_category: Optional[str] = None,
	) -> Optional[Dict[str, Any]]:
		"""Enforce explicit capabilities and NVDA state restrictions."""
		if metadata is None:
			try:
				metadata = self.registry.get_metadata(action_name)
			except Exception:
				metadata = None
		if status_snapshot is None:
			status_snapshot = self.get_nvda_status_snapshot()
		allowed, code, message = check_action_permissions(
			action_name=action_name,
			metadata=metadata,
			nvda_status_snapshot=status_snapshot,
			dev_mode_enabled=self.developer_mode_manager.is_enabled,
			permission_policy=self.permission_policy,
			tool_category=tool_category,
		)
		if not allowed:
			return build_action_error(
				request_id=request_id,
				action_name=action_name,
				duration_ms=0.0,
				code=code,
				message=message,
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
			"developer_mode": self.developer_mode_manager.is_enabled,
		}

	def execute_action(
		self,
		request_id: str,
		action_name: str,
		params: Dict[str, Any],
		timeout_ms: Optional[int] = None,
	) -> Dict[str, Any]:
		"""Execute a bridge action with full error handling, threading, and cancellation."""
		start_time = time.perf_counter()
		logHandler.log.debug(f"bridge.action.start action={action_name}")

		try:
			try:
				tool = self.registry.get(action_name)
			except RegistryError:
				# The registry raises for unknown names; a caller typo is a request
				# problem (4102), not a bridge failure (5303).
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

			status_snapshot = self.get_nvda_status_snapshot()
			duration_ms = (time.perf_counter() - start_time) * 1000
			blocked = self._check_action_allowed(
				request_id, action_name, tool.get_metadata(), status_snapshot, duration_ms
			)
			if blocked is not None:
				return blocked

			resolved_params = params or {}
			effective_timeout = (
				(timeout_ms / 1000.0)
				if (timeout_ms is not None and 100 <= timeout_ms <= 300000)
				else self.main_thread_executor.default_timeout
			)

			cancellation_event = threading.Event()

			# COORDINATOR tools own a sequence (pacing, waiting) and therefore run
			# on this worker thread, reaching NVDA's main thread per operation
			# through the execution context. Every other tool runs entirely on the
			# main thread. Both paths share one validation pipeline.
			execution_context = ExecutionContext(
				dispatch=lambda operation: self.main_thread_executor.execute(
					operation,
					timeout=max(0.1, effective_timeout),
					cancellation_event=cancellation_event,
				),
				cancellation_event=cancellation_event,
				timeout=effective_timeout,
			)

			def _execute_tool() -> Dict[str, Any]:
				return self.tool_executor.execute_and_serialize(tool, resolved_params, execution_context)

			if tool.get_metadata().execution_mode is ExecutionMode.COORDINATOR:
				result = _execute_tool()
			else:
				result = self.main_thread_executor.execute(
					_execute_tool,
					timeout=effective_timeout,
					cancellation_event=cancellation_event,
				)

			duration_ms = (time.perf_counter() - start_time) * 1000

			if duration_ms > 500:
				logHandler.log.warning(
					f"bridge.action.slow action={action_name} duration_ms={duration_ms:.1f} success=true"
				)
			else:
				logHandler.log.info(
					f"bridge.action.success action={action_name} duration_ms={duration_ms:.1f}"
				)

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
			error_code = protocol_code_for_serialized(inner_error)
			if error_code not in ERROR_CODES:
				error_code = 5303
			logHandler.log.info(
				f"bridge.action.failed action={action_name} code={error_code} "
				f"error_code={inner_error.get('error_code')} duration_ms={duration_ms:.1f}"
			)
			return {
				"request_id": request_id,
				"success": False,
				"action": action_name,
				"duration_ms": round(duration_ms, 2),
				"bridge": {"bridge_version": BRIDGE_VERSION, "api_level": API_LEVEL},
				"nvda_status_snapshot": self.get_nvda_status_snapshot(),
				"error": {
					"code": error_code,
					"name": ERROR_CODES[error_code]["name"],
					"message": inner_message,
					"category": ERROR_CODES[error_code]["category"],
					"retry_policy": ERROR_CODES[error_code]["retry_policy"],
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
					"code": 5004,
					"name": ERROR_CODES[5004]["name"],
					"message": f"Operation timed out: {str(e)}",
					"category": ERROR_CODES[5004]["category"],
					"retry_policy": ERROR_CODES[5004]["retry_policy"],
				},
			}
		except ValueError as e:
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
					"retry_after_ms": ERROR_CODES[5003].get("retry_after_ms", 1000),
				},
			}

	def start(self):
		"""Start the HTTP bridge server (blocking) with robust error handling."""
		try:
			self.server = BoundedThreadingHTTPServer((self.host, self.port), NVDABridgeRequestHandler)
			self.server.timeout = 1.0

			self.server.bridge_token = self.bridge_token
			self.server.developer_mode_manager = self.developer_mode_manager
			self.server.permission_policy = self.permission_policy
			self.server.execute_action = self.execute_action
			self.server.list_tools = self._list_tools
			self.server.list_output_types = self._list_output_types
			self.server.get_nvda_status = self.get_nvda_status
			self.server.get_nvda_status_snapshot = self.get_nvda_status_snapshot
			self.server.get_health_payload = self.get_health_payload
			self.server.get_state_payload = self.get_state_payload
			self.server.get_events_payload = self.get_events_payload
			self.server.event_journal = self.event_journal

			logHandler.log.info(f"NVDA HTTP Bridge listening on {self.host}:{self.port}")
			logHandler.log.info(f"Bridge token authentication active on header {TOKEN_HEADER}")

			while not self.shutdown_event.is_set():
				try:
					self.server.handle_request()
				except OSError as e:
					logHandler.log.error(f"Socket error in bridge: {e}", exc_info=True)
					if not self.shutdown_event.is_set():
						self.shutdown_event.wait(0.1)
				except Exception as e:
					logHandler.log.error(f"Error handling request: {e}", exc_info=True)

			logHandler.log.info("NVDA HTTP Bridge stopped")

		except OSError as e:
			if "address already in use" in str(e).lower() or getattr(e, "errno", 0) == 10048:
				logHandler.log.error(f"Port {self.port} already in use. Cannot start bridge.", exc_info=True)
			else:
				logHandler.log.error(f"Socket error starting bridge: {e}", exc_info=True)
			raise
		except Exception as e:
			logHandler.log.error(f"NVDA HTTP Bridge error: {e}", exc_info=True)
			raise

	def stop(self):
		"""Stop the bridge server gracefully and reset developer mode and permissions."""
		try:
			logHandler.log.info("Stopping NVDA HTTP Bridge...")
			self.shutdown_event.set()
			self.developer_mode_manager.reset()
			self.permission_policy.reset()

			if self.server:
				try:
					self.server.server_close()
					logHandler.log.debug("Server socket closed")
				except Exception as e:
					logHandler.log.warning(f"Error closing server socket: {e}")

			logHandler.log.info("NVDA HTTP Bridge stopped")

		except Exception as e:
			logHandler.log.error(f"Error stopping NVDA HTTP Bridge: {e}", exc_info=True)
