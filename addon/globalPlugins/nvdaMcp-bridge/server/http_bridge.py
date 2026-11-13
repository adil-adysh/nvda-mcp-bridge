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

import hashlib
import json
import os
import secrets
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
	ResultMeta,
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
	SUPPORTED_API_LEVELS,
	TOKEN_HEADER,
	http_status_from_error_code,
	validate_tools_discovery_payload,
	build_tools_payload,
)
from .events import EventJournal, EventRecorder
from .action_trace import (
	ActionTraceRecorder,
	ActionTraceStore,
	DiagnosticAssembler,
	build_trace_summary,
	match_native_log,
)
from .refs import ReferenceTable
from .response import build_action_error, build_error_envelope
from .settle import DEFAULT_BUDGET_MS, Settler
from .state import StateProjector
from .security import (
	DeveloperModeManager,
	PermissionPolicy,
	check_action_permissions,
	constant_time_compare,
	get_or_create_bridge_token,
	load_developer_ttl_seconds,
	load_persisted_permissions,
)
from ..validation.output_type_registry import build_output_type_registry, validate_output_type_registry

#: Upper bound on how much of an oversized body is read and discarded before
#: answering 413, so the response reaches the client instead of a socket reset.
_MAX_DRAIN_BYTES = 8 * 1024 * 1024

#: Longest a /events long-poll may hold a worker thread. The bridge serves a
#: bounded thread pool, so waiters must be short-lived and few.
_MAX_EVENT_WAIT_MS = 30000


#: Win32 GUI-thread flags that indicate an active menu loop.
_GUI_MENU_FLAGS = 0x04 | 0x08 | 0x10  # INMENUMODE | SYSTEMMENUMODE | POPUPMENUMODE


def _is_nvda_menu_open(
	menu_identity: Any,
	foreground_class: str,
	foreground_process_id: int,
	nvda_process_id: int,
	gui_flags: int = 0,
) -> bool:
	"""Distinguish an active NVDA popup from its cached accessibility identity.

	NVDA sets ``nvdaMenuIaIdentity`` to ``True`` just before showing its menu and
	keeps the identified popup identity cached after the menu closes. Neither
	value alone proves the menu is open: a menu that failed to appear (or was
	dismissed while another app owned the foreground) can leave ``True`` cached,
	which previously blocked every input action until NVDA restarted. Prefer the
	GUI thread's own menu-mode flags; then the native popup window; fall back to
	the conservative identity check only when no foreground window is available.
	"""
	if gui_flags & _GUI_MENU_FLAGS:
		return True
	if menu_identity is None:
		return False
	if not foreground_class:
		return menu_identity is True
	return foreground_class == "#32768" and foreground_process_id == nvda_process_id


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


def _schema_hash(payload: Dict[str, Any]) -> str:
	"""Return a stable digest of the tool contract a discovery response carries."""
	contract = {
		"tools": payload.get("tools"),
		"types": payload.get("types"),
	}
	canonical = json.dumps(contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
	return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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

			# Route on the path alone: `/tools?category=action` is the same
			# endpoint as `/tools`, with a filter.
			route = urlsplit(self.path)

			if route.path == "/health":
				self._send_json_response(self.server.get_health_payload())
				return

			if route.path == "/tools":
				self._handle_tools_discovery()
				return

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
		# Raw per-event history is a debugging view; the default surface is the
		# coalesced transition, because one user action produces many events.
		raw = (query.get("raw", ["0"])[0] or "0").lower() in ("1", "true", "yes")
		# ``journal_id`` turns ``since`` into a typed cursor. Omitting it keeps
		# the documented current-journal convenience for numeric callers.
		journal_id = (query.get("journal_id", [""])[0] or "").strip() or None

		payload = self.server.event_journal.wait(
			since=since,
			timeout_ms=timeout_ms,
			limit=limit,
			kinds=kinds,
			raw=raw,
			journal_id=journal_id,
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

		query = parse_qs(urlsplit(self.path).query)
		categories, problem = self._discovery_filters(query)
		if problem is not None:
			self._send_json_response(problem, 400)
			return

		tools = self.server.list_tools(categories=categories)
		if not isinstance(tools, list):
			raise RuntimeError(f"Invalid tools payload type: {type(tools).__name__}")

		types = self.server.list_output_types()
		if not isinstance(types, dict) or not types:
			raise RuntimeError("Invalid or empty output type registry")

		payload = build_tools_payload(tools, types)
		# The digest describes the tool contract only; identity is added after
		# it so a Bridge restart does not look like a schema change. The
		# companion still refreshes because `instance_id` itself changed.
		payload["schema_hash"] = _schema_hash(payload)
		# A caller must be able to tell a replaced Bridge from the one whose
		# references and journal it remembers.
		payload["instance_id"] = getattr(self.server, "instance_id", "")
		started_at = getattr(self.server, "started_at", None)
		payload["started_at"] = started_at.isoformat() if started_at is not None else ""
		validate_tools_discovery_payload(payload)
		self._send_json_response(payload)

	def _discovery_filters(
		self, query: Dict[str, list]
	) -> tuple[Optional[set], Optional[Dict[str, Any]]]:
		"""Read the `category` allow-list from a discovery query.

		Comma-separated or repeated, so a production client can ask for actions
		and observations and never pay context for diagnostics. An unknown
		category is a request error rather than an empty catalog: a typo that
		silently removes every tool is the worst available answer.
		"""
		raw_categories: list[str] = []
		for value in query.get("category", []):
			raw_categories.extend(part.strip() for part in value.split(",") if part.strip())
		if not raw_categories:
			return None, None

		known = {meta.category for meta in self.server.registry.list_tools()}
		unknown = [name for name in raw_categories if name not in known]
		if unknown:
			return None, self._build_error_envelope(
				request_id=_new_request_id(),
				action="tools_discovery",
				code=4103,
				message=(
					f"Unknown category {unknown[0]!r}; categories are "
					f"{', '.join(sorted(known))}"
				),
			)
		return set(raw_categories), None

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
				action, params, request_id, timeout_ms, trace_id = self._validate_action_request(
					request_data, request_id
				)
				if action is None:
					return
				self._handle_action(
					action, params or {}, request_id, timeout_ms=timeout_ms, trace_id=trace_id
				)
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
	) -> tuple[Optional[str], Optional[Dict[str, Any]], str, Optional[int], Optional[str]]:
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
				return None, None, request_id, None, None, None
			client_request_id = client_request_id.strip()
			if len(client_request_id) > MAX_REQUEST_ID_LENGTH:
				envelope = self._build_error_envelope(
					request_id=request_id,
					action="unknown",
					code=4101,
					message=f"'request_id' is too long (max {MAX_REQUEST_ID_LENGTH} characters)",
				)
				self._send_json_response(envelope, 400)
				return None, None, request_id, None, None, None
			if client_request_id:
				request_id = client_request_id

		# An action-level id, distinct from the per-request one: a logical action
		# that costs several HTTP requests can carry the same trace_id through
		# all of them, and its diagnostic timeline stays one record.
		trace_id: Optional[str] = None
		client_trace_id = request_data.get("trace_id")
		if client_trace_id is not None:
			if not isinstance(client_trace_id, str) or not client_trace_id.strip():
				envelope = self._build_error_envelope(
					request_id=request_id,
					action="unknown",
					code=4101,
					message="'trace_id' must be a non-empty string",
				)
				self._send_json_response(envelope, 400)
				return None, None, request_id, None, None, None
			trace_id = client_trace_id.strip()
			if len(trace_id) > MAX_REQUEST_ID_LENGTH:
				envelope = self._build_error_envelope(
					request_id=request_id,
					action="unknown",
					code=4101,
					message=f"'trace_id' is too long (max {MAX_REQUEST_ID_LENGTH} characters)",
				)
				self._send_json_response(envelope, 400)
				return None, None, request_id, None, None, None

		if timeout_ms is not None:
			if not isinstance(timeout_ms, int) or timeout_ms < 100 or timeout_ms > 300000:
				envelope = self._build_error_envelope(
					request_id=request_id,
					action="unknown",
					code=4101,
					message="'timeout_ms' must be an integer between 100 and 300000",
				)
				self._send_json_response(envelope, 400)
				return None, None, request_id, None, None

		if action is None:
			envelope = self._build_error_envelope(
				request_id=request_id,
				action="unknown",
				code=4102,
				message="Missing 'action' field",
			)
			self._send_json_response(envelope, 400)
			return None, None, request_id, None, None

		if not isinstance(action, str):
			envelope = self._build_error_envelope(
				request_id=request_id,
				action=str(action),
				code=4102,
				message="'action' must be a string",
			)
			self._send_json_response(envelope, 400)
			return None, None, request_id, None, None

		action = action.strip()
		if not action:
			envelope = self._build_error_envelope(
				request_id=request_id,
				action="",
				code=4102,
				message="'action' cannot be empty",
			)
			self._send_json_response(envelope, 400)
			return None, None, request_id, None, None

		if len(action) > MAX_REQUEST_ID_LENGTH:
			envelope = self._build_error_envelope(
				request_id=request_id,
				action=action,
				code=4102,
				message="'action' is too long (max 128 characters)",
			)
			self._send_json_response(envelope, 400)
			return None, None, request_id, None, None

		if "params" not in request_data:
			envelope = self._build_error_envelope(
				request_id=request_id,
				action=action,
				code=4103,
				message="Missing 'params' field",
			)
			self._send_json_response(envelope, 400)
			return None, None, request_id, None, None

		if not isinstance(params, dict):
			envelope = self._build_error_envelope(
				request_id=request_id,
				action=action,
				code=4103,
				message="'params' must be a JSON object",
			)
			self._send_json_response(envelope, 400)
			return None, None, request_id, None, None

		return action, params, request_id, timeout_ms, trace_id

	def _handle_action(
		self,
		action: str,
		params: Dict[str, Any],
		request_id: str,
		timeout_ms: Optional[int] = None,
		trace_id: Optional[str] = None,
	):
		"""Execute a validated bridge action request with deadline and error mapping."""
		envelope = self.server.execute_action(
			request_id=request_id,
			action_name=action,
			params=params,
			timeout_ms=timeout_ms,
			trace_id=trace_id,
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
		#: Identity of *this* Bridge process. Regenerated on every construction;
		#: never persisted, because the point is to distinguish a replacement
		#: from the instance whose references and journal a caller remembers.
		self.instance_id = secrets.token_hex(16)
		self.started_at = datetime.now(timezone.utc)
		# Versioned UI event journal: backs /events (long-poll and history) and
		# the MCP companion's resource-update notifications. Raw per-event history
		# is a debugging aid (`/events?raw=1`); the default surface is coalesced
		# transitions, because one user action produces many events.
		self.event_journal = EventJournal()
		# Current observation and its two counters. The projector reads NVDA
		# objects, so every refresh goes through the main-thread executor.
		self.projector = StateProjector(self._project)
		# Seed the projection once so the first action already has a scope and a
		# state_version even when the caller did not observe first.
		try:
			self.projector.adopt(self._project())
		except Exception:  # noqa: BLE001 — a cold start without focus is still usable
			pass
		self.recorder = EventRecorder(self.event_journal, self.projector)
		# Diagnostic records for actions, bounded and in memory only. The store
		# is what `get_action_trace` reads; nothing here executes an action.
		self.trace_store = ActionTraceStore()
		self.assembler = DiagnosticAssembler()
		self._native_log_enabled = True
		# References captured by `observe`. Bounded and expiring; never scanned.
		# Bound to this Bridge instance so a process replacement cannot leave an
		# old ref addressable.
		self.refs = ReferenceTable(instance_id=self.instance_id)
		self._ref_lock = threading.Lock()
		self._ref_sequence = 0
		if permission_policy is None:
			# Restore the capability set the user saved through the settings panel.
			# An unset value keeps the conservative in-memory defaults.
			persisted_permissions = load_persisted_permissions()
			if persisted_permissions is not None:
				self.permission_policy.set_granted(persisted_permissions)
		self.developer_mode_manager = DeveloperModeManager(
			initial_enabled=developer_mode,
			default_ttl_seconds=load_developer_ttl_seconds(),
			permission_policy=self.permission_policy,
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

	def _build_tools_list(self, categories: Optional[set] = None) -> list[Dict[str, Any]]:
		"""Build tools list for /tools endpoint in protocol v1 format.

		Two filters, because the catalog is context rather than a directory
		listing: developer-only tools are omitted unless developer mode is on, and
		a caller that names categories gets only those.
		"""
		tools = []
		for meta in self.registry.list_tools():
			# Exclude developer-only tools from discovery if developer mode is not active
			if getattr(meta, "required_permission", "") == "developer" and not self.developer_mode_manager.is_enabled:
				continue
			if categories and getattr(meta, "category", "") not in categories:
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
					"outputSchema": output_schema_obj.to_json_schema(),
					"constraints": self._compute_tool_constraints(meta),
					"readOnly": getattr(meta, "read_only", False),
					"destructive": getattr(meta, "destructive", False),
					"idempotent": getattr(meta, "idempotent", False),
					"sideEffects": getattr(meta, "side_effects", False),
					"requiredPermission": getattr(meta, "required_permission", "read_only"),
					"cancellationSupported": getattr(meta, "cancellation_supported", False),
					"retrySafe": getattr(meta, "retry_safe", False),
					"deprecated": bool(getattr(meta, "deprecated", False)),
					"supersededBy": getattr(meta, "superseded_by", None),
				}
			)
		return tools

	def _get_output_type_name(self, output_schema_obj: Any) -> str:
		return output_schema_obj.model.__name__ if hasattr(output_schema_obj, "model") else "ToolResponse"

	def _build_output_types(self) -> Dict[str, Any]:
		return dict(self.output_types)

	def _bridge_metadata(self) -> Dict[str, Any]:
		return {"bridge_version": BRIDGE_VERSION, "api_level": API_LEVEL}

	def _project(self) -> Dict[str, Any]:
		"""Capture the current observation. Must run on NVDA's main thread.

		The objects behind the projection are deliberately discarded here: only
		``observe`` hands out references, because a reference must keep pointing at
		the object the caller was shown. A projection refreshed by a settle round
		must not silently re-point ``f`` at a new focus.
		"""
		from ..nvda_bridge import observation

		projection, _objects = observation.capture()
		return projection

	def _observe(self, include: list[str], max_text: int, detail: str = "summary") -> Dict[str, Any]:
		"""Capture an observation and register its references. Main thread only."""
		from ..nvda_bridge import actions, observation
		from .refs import _kind_of

		wanted = {item for item in (include or ()) if item}
		projection, objects = observation.capture(
			max_text=max(0, int(max_text)),
			include_children="children" in wanted,
			include_selection="selection" in wanted,
			include_states=detail != "summary",
		)
		projection, _changes, version = self.projector.adopt(projection)
		self.refs.register(objects, version, actions.identity_of)

		refs: Dict[str, str] = {}
		if projection.get("focus"):
			refs["f"] = "focus"
		if "ancestors" in wanted:
			refs.update({ref: _kind_of(ref) for ref in objects if ref.startswith("a")})
		if "children" in wanted:
			refs.update({ref: _kind_of(ref) for ref in objects if ref.startswith("c")})
		if "dialog" in wanted and projection.get("dialog"):
			refs.update({ref: _kind_of(ref) for ref in objects if ref.startswith("d") or ref == "f"})
			if projection["dialog"].get("ref"):
				refs[projection["dialog"]["ref"]] = "dialog"
		window = projection.get("top_window") or {}
		if window.get("ref"):
			refs[window["ref"]] = "window"

		payload: Dict[str, Any] = {
			"state_version": version,
			"revision": self.projector.revision,
			"refs": refs,
			"focus": projection.get("focus"),
			"ancestors": projection.get("ancestors") if "ancestors" in wanted else [],
			"children": projection.get("children") if "children" in wanted else [],
			"dialog": projection.get("dialog") if "dialog" in wanted else None,
			"top_window": projection.get("top_window"),
		}
		if "selection" in wanted:
			payload["selection"] = projection.get("selection")
		return payload

	def _resolve_ref(self, ref: str) -> Any:
		"""Resolve a reference to its captured object. Main thread only.

		The submitted value must *look* like a reference before it is looked up.
		A window title here is an argument error (4103), not a stale reference:
		telling a caller its title "expired" would hide the actual mistake.
		Nothing in this path searches for a replacement object.
		"""
		from ..nvda_bridge import actions, uia

		if not isinstance(ref, str) or not ref.strip():
			raise ValueError("'ref' must be a non-empty string")
		cleaned = ref.strip()
		if not cleaned[0].islower() or not cleaned.isalnum():
			raise ValueError(
				"'ref' must be a reference returned by observe or locate (for example 'f' or 'n3'), "
				f"not {ref!r}; call observe and pass a ref from its refs map"
			)
		scope = self._scope_label()
		# Peek first: the kind recorded at capture time chooses the identity
		# reader, and reading identity is the step that can report staleness.
		# Probing the object instead would raise on exactly the dead elements this
		# check exists to report.
		entry = self.refs.get(cleaned, scope=scope)
		reader = uia.identity_of if entry.target_kind == "uia" else actions.identity_of
		entry = self.refs.get(cleaned, identity_of=reader, scope=scope)
		return entry.obj

	def _scope_label(self) -> str:
		"""A bounded, human-readable name for the current top-level window."""
		projection = self.projector.projection or {}
		window = projection.get("top_window")
		if isinstance(window, dict):
			title = str(window.get("title") or "").strip()
			if title:
				return title[:120]
		return ""

	def _register_ref(self, obj: Any) -> str:
		"""Capture an arbitrary object under a fresh ref. Main thread only."""
		from ..nvda_bridge import actions, uia

		target_kind = "uia" if uia.is_element(obj) else "nvda"
		with self._ref_lock:
			self._ref_sequence += 1
			ref = f"n{self._ref_sequence}"
		reader = uia.identity_of if target_kind == "uia" else actions.identity_of
		self.refs.register(
			{ref: obj},
			self.projector.state_version,
			reader,
			target_kind=target_kind,
		)
		return ref

	def _result_payload(self, context: ExecutionContext) -> Dict[str, Any]:
		"""Build the uniform result block for the response envelope.

		Every result carries ``ok``, ``state_version`` and ``scope`` (the
		top-level window). The block lives beside ``data`` rather than inside it:
		the fields mean the same thing for every tool, so putting them in each
		closed output schema would duplicate them everywhere.
		"""
		meta = context.result
		settle = getattr(meta, "settle", None) if meta is not None else None
		if settle is not None:
			payload = settle.as_dict()
		else:
			payload = {"ok": True, "state_version": self.projector.state_version}
		projection = self.projector.projection or {}
		window = projection.get("top_window")
		if isinstance(window, dict):
			payload["scope"] = window
		return payload

	def get_state_payload(self, timeout: float = 2.0) -> Dict[str, Any]:
		"""Collect the current observation for the ``nvda://ui/state`` resource.

		NVDA API calls must run on the main thread; the caller is an HTTP worker.
		The projection is refreshed here rather than served from cache because a
		resource read is a request for "now", not for "what the last action saw".
		"""

		def _collect() -> Dict[str, Any]:
			projection, _changes, version = self.projector.refresh()
			return {
				"state_version": version,
				"revision": self.projector.revision,
				"instance_id": self.instance_id,
				"journal_id": self.event_journal.journal_id,
				"event_version": self.event_journal.version,
				"focus": projection.get("focus"),
				"ancestors": projection.get("ancestors"),
				"children": projection.get("children"),
				"dialog": projection.get("dialog"),
				"top_window": projection.get("top_window"),
			}

		payload = self.main_thread_executor.execute(_collect, timeout=timeout)
		payload["nvda"] = self.get_nvda_status()
		return payload

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

		menu_identity = None
		try:
			import appModules.nvda

			menu_identity = appModules.nvda.nvdaMenuIaIdentity
			if menu_identity is not None:
				# Win32 window metadata is safe to query from this request thread and
				# distinguishes the live popup from NVDA's cached menu identity.
				import winUser

				foreground = winUser.getForegroundWindow()
				if not foreground:
					menu_open = _is_nvda_menu_open(menu_identity, "", 0, os.getpid())
				else:
					foreground_process_id, foreground_thread_id = winUser.getWindowThreadProcessID(foreground)
					gui_flags = 0
					if foreground_process_id == os.getpid():
						gui_flags = int(winUser.getGUIThreadInfo(foreground_thread_id).flags)
					menu_open = _is_nvda_menu_open(
						menu_identity,
						winUser.getClassName(foreground),
						foreground_process_id,
						os.getpid(),
						gui_flags,
					)
		except Exception:
			# A known menu identity with unavailable foreground evidence remains
			# blocked. A missing identity means NVDA has not opened this menu.
			menu_open = menu_identity is not None

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

	def _bridge_meta(self) -> Dict[str, Any]:
		"""Identity of this Bridge instance, attached to every response.

		Read from one place so a response cannot mix the version of one
		instance with the id of another.
		"""
		return {
			"bridge_version": BRIDGE_VERSION,
			"api_level": API_LEVEL,
			"instance_id": self.instance_id,
			"started_at": self.started_at.isoformat(),
		}

	def get_health_payload(self) -> Dict[str, Any]:
		capabilities = {
			"event_cursors": "typed",
			"event_journal": True,
			"structured_actions": True,
			"action_verification": True,
			"reference_identity": "identity",
		}
		return {
			"status": "ok",
			"protocol": PROTOCOL,
			"protocol_version": PROTOCOL_VERSION,
			"bridge_version": BRIDGE_VERSION,
			"api_level": API_LEVEL,
			"supported_api_levels": list(SUPPORTED_API_LEVELS),
			"instance_id": self.instance_id,
			"started_at": self.started_at.isoformat(),
			"capabilities": capabilities,
			"uptime_seconds": int(max(0.0, time.monotonic() - self.started_at_monotonic)),
			"timestamp_utc": datetime.now(timezone.utc).isoformat(),
			"nvda_status": self.get_nvda_status(),
			"developer_mode": self.developer_mode_manager.is_enabled,
		}

	def _collect_native_log(self, recorder: ActionTraceRecorder, trace_id: Optional[str]) -> None:
		"""Attach best-effort native NVDA log evidence to a trace.

		Optional by design: the native logger cannot carry a trace id, so this
		never claims more than "this line was written while the action ran" —
		unless the line is one the bridge itself wrote, which does carry it.
		"""
		if not self._native_log_enabled:
			recorder.record_native_log([], "not_attempted")
			return
		try:
			from ..nvda_bridge import diagnostics

			payload = diagnostics.get_logs(max_lines=200, use_tail=True)
			lines = payload.get("lines") or []
		except Exception as e:  # noqa: BLE001 — log access is never load-bearing
			recorder.record_native_log([], "unavailable", reason=str(e))
			return
		matches, status = match_native_log(
			lines,
			trace_id=trace_id,
			start_wall=recorder.started_wall,
			finish_wall=time.time(),
		)
		recorder.record_native_log(matches, status)

	def _finish_action(
		self,
		recorder: ActionTraceRecorder,
		envelope: Dict[str, Any],
		*,
		outcome: Optional[str] = None,
	) -> Dict[str, Any]:
		"""Finalize the trace, store it, and attach its summary to the envelope.

		A failure here must not change the answer the caller already earned, so
		the envelope goes back untouched apart from whatever trace block was
		built before the failure.
		"""
		trace_id = recorder.trace_id
		try:
			self._collect_native_log(recorder, trace_id)
			trace = recorder.finalize(
				outcome=outcome,
				http_status=None,
				error=envelope.get("error") if isinstance(envelope.get("error"), dict) else None,
				store=self.trace_store,
			)
		except Exception as e:  # noqa: BLE001
			logHandler.log.warning(f"bridge.trace.failed trace_id={trace_id} error={e}")
			trace = {}
		if trace:
			envelope["trace_id"] = trace.get("trace_id", trace_id)
			envelope["trace"] = build_trace_summary(trace)
		elif trace_id:
			envelope["trace_id"] = trace_id
		return envelope

	def get_trace_payload(self, trace_id: str) -> Optional[Dict[str, Any]]:
		"""Return one retained action trace, or ``None`` when it has expired."""
		return self.trace_store.get(trace_id)

	def execute_action(
		self,
		request_id: str,
		action_name: str,
		params: Dict[str, Any],
		timeout_ms: Optional[int] = None,
		trace_id: Optional[str] = None,
	) -> Dict[str, Any]:
		"""Execute a bridge action with full error handling, threading, and cancellation.

		This is the logical action boundary: one call, one ``trace_id``, one
		diagnostic record. Everything the executor, the settler and the error
		paths learn is attached to that record, and every response carries the
		id back so the caller can read the full timeline.
		"""
		start_time = time.perf_counter()
		recorder = ActionTraceRecorder(assembler=self.assembler)
		trace = recorder.start_action(
			action_name,
			params or {},
			request_id=request_id,
			trace_id=trace_id,
			journal_id=self.event_journal.journal_id,
			event_version=self.event_journal.version,
			baseline_state_version=self.projector.state_version,
			baseline_projection_available=self.projector.projection is not None,
		)
		active_trace_id = trace["trace_id"]
		logHandler.log.debug(f"bridge.action.start action={action_name} trace_id={active_trace_id}")

		try:
			try:
				tool = self.registry.get(action_name)
			except RegistryError:
				# The registry raises for unknown names; a caller typo is a request
				# problem (4102), not a bridge failure (5303).
				logHandler.log.warning(
					f"bridge.action.unknown action={action_name} trace_id={active_trace_id}"
				)
				recorder.record_wire_failure(
					code=4102,
					name=ERROR_CODES[4102]["name"],
					message=f"Unknown action: {action_name}",
					stage="validation",
				)
				return self._finish_action(
					recorder,
					{
						"request_id": request_id,
						"success": False,
						"action": action_name,
						"duration_ms": round((time.perf_counter() - start_time) * 1000, 2),
						"bridge": self._bridge_meta(),
						"nvda_status_snapshot": self.get_nvda_status_snapshot(),
						"error": {
							"code": 4102,
							"name": ERROR_CODES[4102]["name"],
							"message": f"Unknown action: {action_name}",
							"category": ERROR_CODES[4102]["category"],
							"retry_policy": ERROR_CODES[4102]["retry_policy"],
						},
					},
					outcome="failed",
				)

			status_snapshot = self.get_nvda_status_snapshot()
			duration_ms = (time.perf_counter() - start_time) * 1000
			blocked = self._check_action_allowed(
				request_id, action_name, tool.get_metadata(), status_snapshot, duration_ms
			)
			if blocked is not None:
				blocked_error = blocked.get("error") if isinstance(blocked.get("error"), dict) else {}
				recorder.record_wire_failure(
					code=blocked_error.get("code"),
					name=blocked_error.get("name"),
					message=str(blocked_error.get("message") or "Action not permitted"),
					stage="authorization",
				)
				return self._finish_action(recorder, blocked, outcome="failed")

			resolved_params = params or {}
			effective_timeout = (
				(timeout_ms / 1000.0)
				if (timeout_ms is not None and 100 <= timeout_ms <= 300000)
				else self.main_thread_executor.default_timeout
			)

			cancellation_event = threading.Event()

			def _dispatch(operation: Any) -> Any:
				return self.main_thread_executor.execute(
					operation,
					timeout=max(0.1, effective_timeout),
					cancellation_event=cancellation_event,
				)

			result_meta = ResultMeta()
			settler = Settler(
				self.projector,
				_dispatch,
				budget_ms=min(DEFAULT_BUDGET_MS, max(100, int(effective_timeout * 1000))),
				journal=self.event_journal,
				trace=recorder,
			)

			def _settle(
				action_fn: Any,
				expect: Dict[str, Any] | None = None,
				budget_ms: int | None = None,
			) -> Any:
				"""Run one action and wait for its effect to be observable."""
				outcome = settler.run(
					action_fn,
					expect=expect,
					budget_ms=budget_ms,
					cancellation_event=cancellation_event,
				)
				result_meta.settle = outcome
				return outcome

			# COORDINATOR tools own a sequence (pacing, waiting) and therefore run
			# on this worker thread, reaching NVDA's main thread per operation
			# through the execution context. Every other tool runs entirely on the
			# main thread. Both paths share one validation pipeline.
			execution_context = ExecutionContext(
				dispatch=_dispatch,
				cancellation_event=cancellation_event,
				timeout=effective_timeout,
				settle=_settle,
				resolve=self._resolve_ref,
				register=self._register_ref,
				observe=self._observe,
				wait_events=self.event_journal.wait,
				state_version=lambda: self.projector.state_version,
				event_version=lambda: self.event_journal.version,
				result=result_meta,
				trace_store=self.trace_store,
			)

			def _execute_tool() -> Dict[str, Any]:
				return self.tool_executor.execute_and_serialize(
					tool, resolved_params, execution_context, trace=recorder
				)

			if tool.get_metadata().execution_mode is ExecutionMode.COORDINATOR:
				result = _execute_tool()
			else:
				result = self.main_thread_executor.execute(
					_execute_tool,
					timeout=effective_timeout,
					cancellation_event=cancellation_event,
				)

			duration_ms = (time.perf_counter() - start_time) * 1000

			if bool(result.get("success")):
				# Only a result that actually succeeded says so. The old line was
				# emitted before this check, so a failing tool logged "success"
				# immediately followed by "failed" — two contradictory native log
				# lines describing one action, with the same trace id.
				if duration_ms > 500:
					logHandler.log.warning(
						f"bridge.action.slow action={action_name} duration_ms={duration_ms:.1f} "
						f"success=true trace_id={active_trace_id}"
					)
				else:
					logHandler.log.info(
						f"bridge.action.success action={action_name} duration_ms={duration_ms:.1f} "
						f"trace_id={active_trace_id}"
					)
				return self._finish_action(
					recorder,
					{
						"request_id": request_id,
						"success": True,
						"action": action_name,
						"duration_ms": round(duration_ms, 2),
						"bridge": self._bridge_meta(),
						"nvda_status_snapshot": self.get_nvda_status_snapshot(),
						"result": self._result_payload(execution_context),
						"data": result.get("output"),
					},
					outcome="succeeded",
				)

			inner_error = result.get("error") if isinstance(result.get("error"), dict) else {}
			inner_message = str(inner_error.get("message") or "Tool execution failed")
			error_code = protocol_code_for_serialized(inner_error)
			if error_code not in ERROR_CODES:
				error_code = 5303
			# Flatten one level: the tool's own details belong beside the tool
			# metadata, not nested under a second "details" key that a caller has
			# to know about to find them.
			domain_details = inner_error.get("details")
			envelope_details = {
				key: value for key, value in inner_error.items() if key != "details"
			}
			if isinstance(domain_details, dict):
				envelope_details.update(domain_details)
			logHandler.log.info(
				f"bridge.action.failed action={action_name} code={error_code} "
				f"error_code={inner_error.get('error_code')} duration_ms={duration_ms:.1f} "
				f"trace_id={active_trace_id}"
			)
			return self._finish_action(
				recorder,
				{
					"request_id": request_id,
					"success": False,
					"action": action_name,
					"duration_ms": round(duration_ms, 2),
					"bridge": self._bridge_meta(),
					"nvda_status_snapshot": self.get_nvda_status_snapshot(),
					"result": {"ok": False, "state_version": self.projector.state_version},
					"error": {
						"code": error_code,
						"name": ERROR_CODES[error_code]["name"],
						"message": inner_message,
						"category": ERROR_CODES[error_code]["category"],
						"retry_policy": ERROR_CODES[error_code]["retry_policy"],
						"details": envelope_details,
					},
				},
				outcome=(
					"cancelled"
					if error_code == 4204
					else "timed_out"
					if error_code == 5004
					else "failed"
				),
			)

		except TimeoutError as e:
			duration_ms = (time.perf_counter() - start_time) * 1000
			logHandler.log.error(
				f"bridge.action.timeout action={action_name} duration_ms={duration_ms:.1f} "
				f"trace_id={active_trace_id} error={str(e)}",
				exc_info=True,
			)
			recorder.record_exception(e, "dispatch", protocol_code=5004)
			recorder.record_dispatch("timeout")
			return self._finish_action(
				recorder,
				{
					"request_id": request_id,
					"success": False,
					"action": action_name,
					"duration_ms": round(duration_ms, 2),
					"bridge": self._bridge_meta(),
					"nvda_status_snapshot": self.get_nvda_status_snapshot(),
					"error": {
						"code": 5004,
						"name": ERROR_CODES[5004]["name"],
						"message": f"Operation timed out: {str(e)}",
						"category": ERROR_CODES[5004]["category"],
						"retry_policy": ERROR_CODES[5004]["retry_policy"],
					},
				},
				outcome="timed_out",
			)
		except ValueError as e:
			duration_ms = (time.perf_counter() - start_time) * 1000
			logHandler.log.warning(
				f"bridge.action.validation_failed action={action_name} "
				f"duration_ms={duration_ms:.1f} trace_id={active_trace_id} error={str(e)}"
			)
			recorder.record_exception(e, "validation", protocol_code=4103)
			return self._finish_action(
				recorder,
				{
					"request_id": request_id,
					"success": False,
					"action": action_name,
					"duration_ms": round(duration_ms, 2),
					"bridge": self._bridge_meta(),
					"nvda_status_snapshot": self.get_nvda_status_snapshot(),
					"error": {
						"code": 4103,
						"name": ERROR_CODES[4103]["name"],
						"message": str(e),
						"category": ERROR_CODES[4103]["category"],
						"retry_policy": ERROR_CODES[4103]["retry_policy"],
					},
				},
				outcome="failed",
			)
		except Exception as e:
			duration_ms = (time.perf_counter() - start_time) * 1000
			logHandler.log.error(
				f"bridge.action.failed action={action_name} duration_ms={duration_ms:.1f} "
				f"trace_id={active_trace_id} error={str(e)}",
				exc_info=True,
			)
			recorder.record_exception(e, "internal", protocol_code=5003)
			return self._finish_action(
				recorder,
				{
					"request_id": request_id,
					"success": False,
					"action": action_name,
					"duration_ms": round(duration_ms, 2),
					"bridge": self._bridge_meta(),
					"nvda_status_snapshot": self.get_nvda_status_snapshot(),
					"error": {
						"code": 5003,
						"name": ERROR_CODES[5003]["name"],
						"message": f"Execution failed: {str(e)}",
						"category": ERROR_CODES[5003]["category"],
						"retry_policy": ERROR_CODES[5003]["retry_policy"],
						"retry_after_ms": ERROR_CODES[5003].get("retry_after_ms", 1000),
					},
				},
				outcome="failed",
			)


	def start(self):
		"""Start the HTTP bridge server (blocking) with robust error handling."""
		try:
			# The coalescer turns event bursts into transitions; it must be running
			# before the first event arrives, or a burst is flushed late.
			self.recorder.start()
			self.server = BoundedThreadingHTTPServer((self.host, self.port), NVDABridgeRequestHandler)
			self.server.timeout = 1.0

			self.server.bridge_token = self.bridge_token
			self.server.developer_mode_manager = self.developer_mode_manager
			self.server.permission_policy = self.permission_policy
			# The request handler needs the catalog's metadata (categories,
			# permissions) without building the payload; the registry is the cheap
			# way to ask.
			self.server.registry = self.registry
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
			self.recorder.stop()
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
