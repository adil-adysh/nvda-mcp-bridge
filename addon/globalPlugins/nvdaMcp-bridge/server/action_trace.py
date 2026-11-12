"""Structured, bounded diagnostic record for one logical action.

Why this exists: when an action does not do what the caller expected, the only
things a caller could previously compare were the action's own output, the
coalesced event journal and the native NVDA log — three unrelated sources with
no shared identifier. This module ties them to one logical action without
touching how actions run: the bridge builds one record per action, the executor
and the settler add observations as they go, and the record is finalized and
stored when the envelope is built.

The three identifiers this project uses, kept deliberately distinct:

``trace_id``
	One per logical action. This is the correlation key for everything below.
	It survives handler failures, is returned with every action envelope, and
	names the retained record that ``get_action_trace`` reads back.
``request_id``
	One per HTTP request. The client may supply it for its own logs; a logical
	action made of several requests would have several.
``MCP request id``
	Belongs to the MCP protocol and never reaches this process; it is recorded
	as ``None`` with a limitation rather than being guessed.

Two rules are encoded in the shape of the record, not in prose:

* an observation is evidence, not a conclusion. ``projection_changed = false``
	means the fields the projector captures did not change, and the record says
	so instead of claiming the UI was unchanged;
* an event or log line inside the action window is temporally correlated, not
	proof of cause, so each is labelled with how it was matched.

Everything here is bounded and never raises into the action: a diagnostic bug
must not turn a working action into a failed one.
"""

from __future__ import annotations

import copy
import json
import re
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

SCHEMA_VERSION = 1

#: Size bounds. A trace is read by a model, so a few kilobytes of salient facts
#: beat a megabyte of document text.
MAX_TEXT_CHARS = 300
MAX_PARAM_CHARS = 300
MAX_PARAMS_SERIALIZED = 2048
MAX_MESSAGE_CHARS = 800
MAX_TRACEBACK_CHARS = 2000
MAX_EVENT_RECORDS = 50
MAX_LOG_MATCHES = 20
MAX_LIMITATIONS = 12
MAX_DELTA_ITEMS = 20

#: Retention. In memory only: a diagnostic record describes what the user's
#: screen was doing, so it must not outlive the process in a file.
DEFAULT_MAX_TRACES = 50
DEFAULT_MAX_AGE_SECONDS = 900.0

REDACTED = "<redacted>"

#: Substrings of a parameter name that mark a value as a credential. Matched
#: case-insensitively against the key, because a password field can be called
#: ``passwd``, ``apiKey`` or ``X-NVDA-Bridge-Token``.
_SENSITIVE_KEY_PARTS = (
	"password",
	"passwd",
	"secret",
	"token",
	"apikey",
	"api_key",
	"authorization",
	"credential",
	"cookie",
	"private_key",
	"sessionid",
	"session_id",
)

#: Evidence sources, used where a record needs to say where a fact came from.
SOURCE_ACTION_LIFECYCLE = "action_lifecycle"
SOURCE_TOOL_EXECUTOR = "tool_executor"
SOURCE_HANDLER = "handler"
SOURCE_EVENT_JOURNAL = "event_journal"
SOURCE_STATE_PROJECTOR = "state_projector"
SOURCE_SETTLER = "settler"
SOURCE_BRIDGE_LOG = "bridge_log"
SOURCE_NATIVE_LOG = "nvda_native_log"

#: Outcome vocabulary. ``incomplete`` is the honest answer when the action
#: never got far enough to know: the dispatch timed out, the connection died, or
#: an internal error interrupted the record.
OUTCOME_SUCCEEDED = "succeeded"
OUTCOME_FAILED = "failed"
OUTCOME_TIMED_OUT = "timed_out"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_INCOMPLETE = "incomplete"
OUTCOME_UNKNOWN = "unknown"

#: Categories (spec §7.3). These name *where* the failure happened; the wire
#: error code names *what* the caller can do about it.
CATEGORY_INPUT_VALIDATION = "input_validation"
CATEGORY_UNSUPPORTED = "unsupported_operation"
CATEGORY_HANDLER = "handler_exception"
CATEGORY_NVDA_API = "nvda_api_exception"
CATEGORY_TIMEOUT = "timeout"
CATEGORY_SETTLING = "settling_failure"
CATEGORY_SERIALIZATION = "output_serialization"
CATEGORY_TRANSPORT = "http_transport"
CATEGORY_INTERNAL = "internal_bridge_error"
CATEGORY_DOMAIN = "domain_error"
CATEGORY_PERMISSION = "permission_denied"


def category_for_protocol_code(code: Any, fallback: str = CATEGORY_INTERNAL) -> str:
	"""Map a wire error code onto the failure vocabulary.

	The wire codes classify what the *caller* can do (retry, fix the request,
	give up). The trace needs a different axis — where inside the bridge the
	failure happened — so the mapping lives here instead of being copied into
	the error table.
	"""
	try:
		value = int(code)
	except (TypeError, ValueError):
		return fallback
	if value in (4001, 4003):
		return CATEGORY_PERMISSION
	if 4100 <= value <= 4199:
		return CATEGORY_INPUT_VALIDATION
	if value == 4204:
		return CATEGORY_TIMEOUT
	if 4200 <= value <= 4299:
		return CATEGORY_DOMAIN
	if value == 5004:
		return CATEGORY_TIMEOUT
	return fallback


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def utc_iso(timestamp: Optional[float] = None) -> str:
	"""Render a wall-clock timestamp as UTC ISO 8601 with milliseconds."""
	moment = datetime.fromtimestamp(time.time() if timestamp is None else timestamp, timezone.utc)
	return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_trace_id() -> str:
	"""Return a fresh, unguessable action trace identifier."""
	return f"at-{uuid.uuid4().hex[:20]}"


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------


def _is_sensitive_key(key: Any) -> bool:
	text = str(key).lower()
	return any(part in text for part in _SENSITIVE_KEY_PARTS)


def bounded_text(value: Any, limit: int = MAX_TEXT_CHARS) -> tuple[str, bool]:
	"""Return ``(text, truncated)`` for any value, never raising."""
	try:
		text = value if isinstance(value, str) else str(value)
	except Exception:  # noqa: BLE001 — a broken __str__ is not worth an exception
		return "<unprintable>", False
	if len(text) <= limit:
		return text, False
	return text[: max(0, limit - 1)] + "\u2026", True


def sanitize_value(
	value: Any,
	depth: int = 0,
	redacted: Optional[list[str]] = None,
	truncated: Optional[list[str]] = None,
	path: str = "",
) -> Any:
	"""Copy a JSON-ish value, bounding sizes and redacting credentials.

	The returned value is always plain JSON: dict/list/str/int/float/bool/None.
	That matters because a stored trace must never hold a live NVDA object.
	"""
	redacted = redacted if redacted is not None else []
	truncated = truncated if truncated is not None else []
	if depth > 4:
		truncated.append(path or ".")
		return "<depth-limit>"
	if isinstance(value, dict):
		copied: dict[str, Any] = {}
		for index, (key, item) in enumerate(value.items()):
			if index >= 32:
				truncated.append(path or ".")
				break
			name = str(key)
			child = f"{path}.{name}" if path else name
			if _is_sensitive_key(name):
				copied[name] = REDACTED
				redacted.append(child)
				continue
			copied[name] = sanitize_value(item, depth + 1, redacted, truncated, child)
		return copied
	if isinstance(value, (list, tuple, set)):
		items = list(value)
		copied_items = [
			sanitize_value(item, depth + 1, redacted, truncated, f"{path}.{index}")
			for index, item in enumerate(items[:32])
		]
		if len(items) > 32:
			truncated.append(path or ".")
		return copied_items
	if isinstance(value, str):
		text, was_truncated = bounded_text(value)
		if was_truncated:
			truncated.append(path or ".")
		return text
	if isinstance(value, (int, float, bool)) or value is None:
		return value
	text, was_truncated = bounded_text(repr(value))
	if was_truncated:
		truncated.append(path or ".")
	return text


def sanitize_params(params: Any) -> tuple[dict[str, Any], dict[str, Any]]:
	"""Return ``(sanitized_params, metadata)`` for one action's parameters.

	The metadata is what tells a reader that the copy they are looking at is not
	the whole request: ``truncated``, which fields were redacted, and which were
	dropped to stay inside the serialized budget.
	"""
	redacted: list[str] = []
	truncated: list[str] = []
	if not isinstance(params, dict):
		text, was_truncated = bounded_text(params)
		payload = {"value": text}
		return payload, {
			"truncated": was_truncated,
			"redacted_fields": [],
			"dropped_fields": [],
			"original_field_count": 0,
		}

	payload = sanitize_value(params, redacted=redacted, truncated=truncated)
	assert isinstance(payload, dict)
	dropped: list[str] = []
	# Keep the payload inside the budget by dropping whole fields from the end,
	# rather than emitting half a JSON document that no one can read.
	while payload and len(json.dumps(payload, ensure_ascii=False)) > MAX_PARAMS_SERIALIZED:
		key = next(reversed(payload))
		payload.pop(key)
		dropped.append(key)
	return payload, {
		"truncated": bool(truncated or dropped),
		"redacted_fields": sorted(set(redacted)),
		"dropped_fields": dropped,
		"original_field_count": len(params),
		"truncated_fields": sorted(set(truncated)),
	}


def classify_exception(exc: Optional[BaseException], protocol_code: Optional[int] = None) -> str:
	"""Name the failure category for an exception. Never raises."""
	if protocol_code in (4204,):
		return CATEGORY_TIMEOUT
	if protocol_code in (4205, 4206):
		return CATEGORY_DOMAIN
	if isinstance(exc, TimeoutError):
		return CATEGORY_TIMEOUT
	if isinstance(exc, (ValueError, TypeError)) and exc is not None:
		return CATEGORY_INPUT_VALIDATION
	if isinstance(exc, NotImplementedError):
		return CATEGORY_UNSUPPORTED
	if exc is not None:
		module = type(exc).__module__ or ""
		if module.startswith(("comtypes", "ctypes", "_ctypes", "win32")):
			return CATEGORY_NVDA_API
		code = getattr(exc, "error_code", None)
		name = getattr(code, "value", "") or ""
		if "input" in name or "field" in name:
			return CATEGORY_INPUT_VALIDATION
		if "output" in name:
			return CATEGORY_SERIALIZATION
	return CATEGORY_HANDLER


def sanitize_traceback(exc: Optional[BaseException], limit: int = MAX_TRACEBACK_CHARS) -> str:
	"""Return a bounded traceback for a caught exception, without install paths.

	The frame's file name is kept and its directory is dropped: ``executor.py,
	line 195`` is what identifies the failure, while ``C:\\Users\\<name>\\…``
	identifies the person running the screen reader. The trace is read by a
	model and may be shared, so the second is not worth shipping.
	"""
	if exc is None:
		return ""
	try:
		text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
	except Exception:  # noqa: BLE001
		return ""
	text = _collapse_frame_paths(text)
	return bounded_text(text.strip(), limit)[0]


#: Matches the ``File "..."`` part of a formatted traceback frame.
_FRAME_PATH_RE = re.compile(r'File "([^"]+)"')


def _collapse_frame_paths(text: str) -> str:
	"""Replace absolute paths in traceback frames with their file name."""

	def _short(match: "re.Match[str]") -> str:
		name = match.group(1).replace("\\", "/").rsplit("/", 1)[-1]
		return f'File "{name}"'

	return _FRAME_PATH_RE.sub(_short, text)


def exception_record(
	exc: Optional[BaseException],
	*,
	stage: str,
	category: Optional[str] = None,
	caught: bool = True,
	returned_to_client: bool = True,
	protocol_code: Optional[int] = None,
	details: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
	"""Turn a caught exception into the structured record the trace stores."""
	if exc is None:
		return None
	message, message_truncated = bounded_text(str(exc), MAX_MESSAGE_CHARS)
	record: dict[str, Any] = {
		"type": type(exc).__name__,
		"message": message,
		"message_truncated": message_truncated,
		"category": category or classify_exception(exc, protocol_code),
		"stage": stage,
		"caught": bool(caught),
		"returned_to_client": bool(returned_to_client),
		"code": protocol_code,
		"source": SOURCE_HANDLER if stage in ("handler", "dispatch") else stage,
	}
	tb = sanitize_traceback(exc)
	if tb:
		record["traceback"] = tb
	if details:
		record["details"] = sanitize_value(details)
	return record


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------


class ActionTraceRecorder:
	"""Builds one :class:`dict` trace for one logical action.

	The recorder is the only writer. It is created per action by the bridge and
	handed to the executor and the settler, so instrumentation stays at the
	shared layers instead of appearing in every handler.
	"""

	def __init__(
		self,
		*,
		clock: Any = time.monotonic,
		wall_clock: Any = time.time,
		assembler: Optional["DiagnosticAssembler"] = None,
	) -> None:
		self._clock = clock
		self._wall = wall_clock
		self._assembler = assembler or DiagnosticAssembler()
		self._lock = threading.Lock()
		self.trace: Optional[dict[str, Any]] = None
		self.trace_id: Optional[str] = None
		self._started_monotonic = 0.0
		self._started_wall = 0.0

	# -- creation ----------------------------------------------------------

	@property
	def started_wall(self) -> float:
		"""Wall-clock start time, for matching against the native log."""
		return self._started_wall

	def start_action(
		self,
		action_name: str,
		params: Optional[dict[str, Any]] = None,
		*,
		request_id: Optional[str] = None,
		trace_id: Optional[str] = None,
		origin: Optional[str] = None,
		journal_id: Optional[str] = None,
		event_version: Optional[int] = None,
		baseline_state_version: Optional[int] = None,
		baseline_projection_available: bool = False,
	) -> dict[str, Any]:
		"""Open a trace for ``action_name`` and return the mutable record."""
		self.trace_id = trace_id or new_trace_id()
		self._started_monotonic = self._clock()
		self._started_wall = self._wall()
		sanitized, params_meta = sanitize_params(params or {})
		self.trace = {
			"schema_version": SCHEMA_VERSION,
			"trace_id": self.trace_id,
			"action": {
				"name": action_name,
				"origin": origin,
				"started_at": utc_iso(self._started_wall),
				"finished_at": None,
				"duration_ms": None,
				"request_ids": [request_id] if request_id else [],
				"mcp_request_id": None,
			},
			"request": {
				"params": sanitized,
				"params_meta": params_meta,
			},
			"lifecycle": {"milestones": []},
			"execution": {
				"dispatch_status": "not_dispatched",
				"handler_status": "not_started",
				"tool_status": "not_started",
				"exception": None,
				"error_category": None,
				"failure_stage": None,
			},
			"observation": {
				"journal_id": journal_id,
				"from_event_version": event_version,
				"to_event_version": None,
				"event_count": 0,
				"events": [],
				"events_complete": False,
				"events_truncated": False,
				"projection_available": bool(baseline_projection_available),
				"projection_changed": None,
				"baseline_state_version": baseline_state_version,
				"final_state_version": None,
				"delta": {},
				"settling_status": "not_started",
				"settled_ms": None,
				"budget_ms": None,
				"budget_expired": False,
				"observation_status": "not_observed",
				"uncertain": False,
				"expectation": {
					"status": "not_provided",
					"evaluated": False,
					"expected": None,
					"mismatch": None,
				},
				"sources": [],
			},
			"completion": {},
			"evidence": {
				"bridge_logs": [],
				"native_log_matches": [],
				"native_log_status": "not_attempted",
				"sources": [],
				"limitations": [],
			},
			"retention": {"stored_at": None, "expires_at": None, "state": "running"},
		}
		self._mark("received", status="ok")
		return self.trace

	# -- milestones --------------------------------------------------------

	def _mark(self, name: str, status: Optional[str] = None, **detail: Any) -> None:
		"""Append one lifecycle milestone. Never raises."""
		trace = self.trace
		if trace is None:
			return
		now_wall = self._wall()
		record: dict[str, Any] = {
			"name": name,
			"at": utc_iso(now_wall),
			"elapsed_ms": round(max(0.0, (self._clock() - self._started_monotonic) * 1000.0), 1),
			"source": SOURCE_ACTION_LIFECYCLE,
		}
		if status is not None:
			record["status"] = status
		if detail:
			sanitized = sanitize_value(detail)
			if isinstance(sanitized, dict):
				record["detail"] = sanitized
		with self._lock:
			milestones = trace["lifecycle"]["milestones"]
			milestones.append(record)
		# Every milestone is also a structured bridge log line: the same record
		# answers "what did the bridge log" without parsing free-form messages.
		with self._lock:
			logs = trace["evidence"]["bridge_logs"]
			logs.append(
				{
					"at": record["at"],
					"level": "warning" if status in ("failed", "timeout") else "info",
					"event": name,
					"trace_id": self.trace_id,
					"status": status,
					"source": SOURCE_BRIDGE_LOG,
				}
			)

	def record_validation(self, ok: bool, detail: Optional[dict[str, Any]] = None) -> None:
		"""Record that input validation ran."""
		if self.trace is None:
			return
		self.trace["request"]["validated"] = bool(ok)
		self._mark("validated", status="ok" if ok else "failed", **(detail or {}))

	def record_dispatch(self, status: str = "dispatched", **detail: Any) -> None:
		"""Record whether the tool was actually dispatched."""
		if self.trace is None:
			return
		with self._lock:
			self.trace["execution"]["dispatch_status"] = status
		self._mark("dispatched", status=status, **detail)

	def record_handler_start(self) -> None:
		"""Record that the handler body began."""
		if self.trace is None:
			return
		with self._lock:
			self.trace["execution"]["handler_status"] = "running"
		self._mark("handler_started", status="running")

	def record_handler_result(self, status: str, **detail: Any) -> None:
		"""Record that the handler returned (``succeeded``) or raised."""
		if self.trace is None:
			return
		with self._lock:
			self.trace["execution"]["handler_status"] = status
		self._mark("handler_completed", status=status, **detail)

	def record_exception(
		self,
		exc: Optional[BaseException],
		stage: str,
		*,
		category: Optional[str] = None,
		caught: bool = True,
		returned_to_client: bool = True,
		protocol_code: Optional[int] = None,
		details: Optional[dict[str, Any]] = None,
	) -> None:
		"""Record a structured exception without changing how it is handled."""
		if self.trace is None or exc is None:
			return
		record = exception_record(
			exc,
			stage=stage,
			category=category,
			caught=caught,
			returned_to_client=returned_to_client,
			protocol_code=protocol_code,
			details=details,
		)
		if record is None:
			return
		with self._lock:
			# First exception wins: it is the cause, and later ones are usually
			# its consequences (a serialization error after a handler failure).
			if self.trace["execution"]["exception"] is None:
				self.trace["execution"]["exception"] = record
				self.trace["execution"]["error_category"] = record["category"]
				self.trace["execution"]["failure_stage"] = stage
		self._mark("exception", status="failed", stage=stage, category=record["category"])

	def record_tool_result(self, status: str, **detail: Any) -> None:
		"""Record the end of the whole tool pipeline."""
		if self.trace is None:
			return
		with self._lock:
			self.trace["execution"]["tool_status"] = status
		self._mark("tool_completed", status=status, **detail)

	def record_wire_failure(
		self,
		*,
		code: Optional[int],
		message: str,
		stage: str,
		name: Optional[str] = None,
		category: Optional[str] = None,
	) -> None:
		"""Record a refusal that has no exception behind it.

		An unknown tool or a denied permission never raises; without this the
		trace would show a received request that simply stopped.
		"""
		if self.trace is None:
			return
		record = {
			"type": name or "BridgeError",
			"message": bounded_text(str(message), MAX_MESSAGE_CHARS)[0],
			"category": category or category_for_protocol_code(code),
			"stage": stage,
			"caught": True,
			"returned_to_client": True,
			"code": code,
			"source": SOURCE_ACTION_LIFECYCLE,
		}
		with self._lock:
			execution = self.trace["execution"]
			if execution["exception"] is None:
				execution["exception"] = record
				execution["error_category"] = record["category"]
				execution["failure_stage"] = stage
				execution["error_code"] = code
		self._mark("refused", status="failed", stage=stage, code=code)

	# -- settling and observation -----------------------------------------

	def record_settling_start(
		self,
		*,
		journal_id: Optional[str] = None,
		event_version: Optional[int] = None,
		baseline_state_version: Optional[int] = None,
		budget_ms: Optional[int] = None,
	) -> None:
		"""Record the start of a settle round and its event cursor boundary."""
		if self.trace is None:
			return
		observation = self.trace["observation"]
		with self._lock:
			observation["settling_status"] = "running"
			if journal_id is not None:
				observation["journal_id"] = journal_id
			if event_version is not None:
				observation["from_event_version"] = event_version
			if baseline_state_version is not None:
				observation["baseline_state_version"] = baseline_state_version
			if budget_ms is not None:
				observation["budget_ms"] = budget_ms
		self._mark(
			"settling_started",
			status="running",
			journal_id=journal_id,
			event_version=event_version,
		)

	def record_observation(self, settle_outcome: Any, journal_head: Optional[int] = None) -> None:
		"""Record the settled observation and its assessment of completeness."""
		if self.trace is None:
			return
		with self._lock:
			observation = self.trace["observation"]
			observation.update(self._assembler.observation_from_settle(settle_outcome))
			observation["sources"] = [SOURCE_SETTLER, SOURCE_STATE_PROJECTOR]
			if journal_head is not None:
				observation["journal_head_version"] = journal_head
		self._mark(
			"settling_completed",
			status=self.trace["observation"].get("settling_status"),
			events=len(self.trace["observation"].get("events") or []),
		)

	# -- native log --------------------------------------------------------

	def record_native_log(
		self,
		matches: Optional[list[dict[str, Any]]],
		status: str,
		reason: Optional[str] = None,
	) -> None:
		"""Record best-effort native-log evidence and how it was matched."""
		if self.trace is None:
			return
		with self._lock:
			evidence = self.trace["evidence"]
			evidence["native_log_status"] = status
			evidence["native_log_matches"] = sanitize_value(list(matches or [])[:MAX_LOG_MATCHES])
			if reason:
				evidence["native_log_reason"] = reason

	# -- finalization ------------------------------------------------------

	def finalize(
		self,
		*,
		outcome: Optional[str] = None,
		http_status: Optional[int] = None,
		error: Optional[dict[str, Any]] = None,
		store: Optional["ActionTraceStore"] = None,
	) -> dict[str, Any]:
		"""Close the record, derive its assessment, and store it.

		``outcome`` is what the code that generated the envelope knows. When it
		is not supplied the outcome is derived from what was recorded, which is
		how a failure recorded by the executor still reaches the trace even if
		the caller forgot to pass one.
		"""
		if self.trace is None:
			return {}
		trace = self.trace
		finished_wall = self._wall()
		duration_ms = round(max(0.0, (self._clock() - self._started_monotonic) * 1000.0), 1)
		if isinstance(error, dict):
			self._merge_wire_error(error)
		resolved = outcome or self._assembler.derive_outcome(trace)
		with self._lock:
			trace["action"]["finished_at"] = utc_iso(finished_wall)
			trace["action"]["duration_ms"] = duration_ms
			trace["execution"]["outcome"] = resolved
			trace["http_status"] = http_status
			# `unknown` is an outcome, not an unfinished record: the trace is
			# closed and readable either way.
			trace["retention"]["state"] = "finalized"
		self._mark("completed", status=resolved)
		assembled = self._assembler.assemble(trace)
		if store is not None:
			store.add(assembled)
			stored_retention = store.retention_for(assembled["trace_id"])
			if stored_retention:
				assembled["retention"]["expires_at"] = stored_retention.get("expires_at")
				assembled["retention"]["max_traces"] = stored_retention.get("max_traces")
				assembled["retention"]["max_age_seconds"] = stored_retention.get("max_age_seconds")
			with self._lock:
				trace["retention"].update(assembled["retention"])
		return assembled

	def _merge_wire_error(self, error: dict[str, Any]) -> None:
		"""Fold the envelope's error block into the structured exception record."""
		if self.trace is None:
			return
		execution = self.trace["execution"]
		message = str(error.get("message") or "")
		code = error.get("code")
		with self._lock:
			execution["error_code"] = code
			execution["error_name"] = error.get("name")
			execution["error_category"] = execution["error_category"] or error.get("category")
			if execution["exception"] is None and message:
				execution["exception"] = {
					"type": error.get("name") or "BridgeError",
					"message": bounded_text(message, MAX_MESSAGE_CHARS)[0],
					"category": category_for_protocol_code(code),
					"stage": execution["failure_stage"] or "unknown",
					"caught": True,
					"returned_to_client": True,
					"code": code,
					"source": SOURCE_ACTION_LIFECYCLE,
				}
			if execution["exception"] is not None and execution["exception"].get("code") is None:
				execution["exception"]["code"] = code


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class ActionTraceStore:
	"""Bounded, thread-safe, in-memory store of finalized traces.

	Retention is count- and age-bounded: a long session cannot accumulate
	traces, and a record describing the user's screen cannot outlive the
	process. Reads return copies, so a caller cannot mutate what a later read
	sees.
	"""

	def __init__(
		self,
		max_traces: int = DEFAULT_MAX_TRACES,
		max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
		clock: Any = time.monotonic,
	) -> None:
		self._lock = threading.Lock()
		self._entries: dict[str, dict[str, Any]] = {}
		self._order: list[str] = []
		self._max_traces = max(1, int(max_traces))
		self._max_age = max(1.0, float(max_age_seconds))
		self._clock = clock
		self._evicted = 0
		self._duplicates = 0
		self._last_added: Optional[str] = None

	@property
	def max_traces(self) -> int:
		return self._max_traces

	@property
	def max_age_seconds(self) -> float:
		return self._max_age

	def add(self, trace: dict[str, Any]) -> None:
		"""Store one finalized trace, replacing any record with the same id."""
		trace_id = trace.get("trace_id")
		if not isinstance(trace_id, str) or not trace_id:
			return
		now = self._clock()
		entry = {
			"trace": copy.deepcopy(trace),
			"stored_at": now,
			"expires_at": now + self._max_age,
		}
		with self._lock:
			self._purge(now)
			if trace_id in self._entries:
				# Same id twice means a caller supplied a trace id and reused it;
				# the newer record is the one that describes the action.
				self._duplicates += 1
				self._order.remove(trace_id)
			self._entries[trace_id] = entry
			self._order.append(trace_id)
			self._last_added = trace_id
			while len(self._order) > self._max_traces:
				oldest = self._order.pop(0)
				if self._entries.pop(oldest, None) is not None:
					self._evicted += 1

	def get(self, trace_id: str) -> Optional[dict[str, Any]]:
		"""Return a copy of one trace, or ``None`` when it is not retained."""
		if not isinstance(trace_id, str) or not trace_id:
			return None
		now = self._clock()
		with self._lock:
			self._purge(now)
			entry = self._entries.get(trace_id)
			return copy.deepcopy(entry["trace"]) if entry else None

	def __contains__(self, trace_id: object) -> bool:
		with self._lock:
			self._purge(self._clock())
			return trace_id in self._entries

	def __len__(self) -> int:
		with self._lock:
			self._purge(self._clock())
			return len(self._entries)

	def retention_for(self, trace_id: str) -> Optional[dict[str, Any]]:
		"""Return retention metadata for one trace."""
		now = self._clock()
		with self._lock:
			entry = self._entries.get(trace_id)
			if entry is None:
				return None
			return {
				"expires_at": utc_iso(time.time() + max(0.0, entry["expires_at"] - now)),
				"age_seconds": round(max(0.0, now - entry["stored_at"]), 1),
				"max_traces": self._max_traces,
				"max_age_seconds": self._max_age,
			}

	def stats(self) -> dict[str, Any]:
		"""Expose retention and eviction metadata for diagnostics."""
		now = self._clock()
		with self._lock:
			self._purge(now)
			oldest_age = None
			if self._order:
				oldest = self._entries.get(self._order[0])
				if oldest is not None:
					oldest_age = round(max(0.0, now - oldest["stored_at"]), 1)
			return {
				"count": len(self._order),
				"max_traces": self._max_traces,
				"max_age_seconds": self._max_age,
				"evicted_total": self._evicted,
				"duplicate_replacements": self._duplicates,
				"oldest_age_seconds": oldest_age,
				"last_trace_id": self._last_added,
			}

	def _purge(self, now: float) -> None:
		"""Drop expired entries. Caller must hold the lock."""
		expired = [key for key in self._order if self._entries[key]["expires_at"] <= now]
		for key in expired:
			self._order.remove(key)
			self._entries.pop(key, None)
			self._evicted += 1


# ---------------------------------------------------------------------------
# Assembler
# ---------------------------------------------------------------------------


class DiagnosticAssembler:
	"""Turns recorded facts into the final assessment. Executes nothing.

	Kept separate from the recorder so that "what happened" (milestones,
	observations, exceptions) and "what it means" (completion, evidence,
	limitations) cannot drift into each other.
	"""

	def observation_from_settle(self, outcome: Any) -> dict[str, Any]:
		"""Map a :class:`server.settle.SettleOutcome` onto observation fields."""
		value = lambda name, default=None: getattr(outcome, name, default)  # noqa: E731
		projection_changed = bool(value("changed", False))
		projection_available = value("projection") is not None
		uncertain = bool(value("uncertain", False))
		events = list(value("events", []) or [])[:MAX_EVENT_RECORDS]
		events_complete = bool(value("events_complete", False))
		settling_status = value("settling_status") or ("settled" if projection_available else "unavailable")
		expectation = {
			"status": "not_provided",
			"evaluated": False,
			"expected": sanitize_value(value("expect")) if value("expect") else None,
			"mismatch": sanitize_value(value("expect_mismatch")),
		}
		if value("expect"):
			if not projection_available:
				expectation["status"] = "not_evaluated"
			elif value("completed") is True:
				expectation["status"] = "satisfied"
				expectation["evaluated"] = True
			else:
				expectation["status"] = "not_satisfied"
				expectation["evaluated"] = True

		delta = sanitize_value(value("delta", {}))
		if isinstance(delta, dict) and len(delta) > MAX_DELTA_ITEMS:
			delta = dict(list(delta.items())[:MAX_DELTA_ITEMS])
			delta["_truncated"] = True

		observation: dict[str, Any] = {
			"journal_id": value("journal_id"),
			"from_event_version": value("from_event_version"),
			"to_event_version": value("to_event_version"),
			"event_count": len(events),
			"events": sanitize_value(events),
			"events_complete": events_complete,
			# Incomplete and truncated are the same fact seen from two sides: a
			# window that does not hold everything is a window that was cut.
			"events_truncated": (not events_complete) or bool(value("events_truncated", False)),
			"projection_available": projection_available,
			"projection_changed": projection_changed if projection_available else None,
			"baseline_state_version": value("baseline_state_version"),
			"final_state_version": value("final_state_version", value("state_version")),
			"delta": delta,
			"settling_status": settling_status,
			"settled_ms": value("settled_ms"),
			"budget_ms": value("budget_ms"),
			"budget_expired": bool(value("budget_expired", False)),
			"uncertain": uncertain,
			"expectation": expectation,
			"observation_status": self.observation_status(
				projection_available=projection_available,
				projection_changed=projection_changed,
				settling_status=settling_status,
				event_count=len(events),
				events_complete=events_complete,
			),
		}
		return observation

	@staticmethod
	def observation_status(
		*,
		projection_available: bool,
		projection_changed: bool,
		settling_status: str,
		event_count: int,
		events_complete: bool,
	) -> str:
		"""One label for the observation, keeping the §6.3 cases distinct.

		These used to be one fact ("changed" or not). They are seven different
		situations: no change in the projected fields, no projection at all, a
		timeout, an incomplete event history, an event with no projected change,
		and a real change.
		"""
		if not projection_available:
			return "projection_unavailable"
		if settling_status == "timed_out" or settling_status == "budget_expired":
			return "timed_out"
		if not events_complete and event_count:
			return "events_incomplete"
		if projection_changed:
			return "changed"
		if event_count:
			return "events_without_projected_change"
		return "no_detectable_change"

	def derive_outcome(self, trace: dict[str, Any]) -> str:
		"""Derive an outcome from what was recorded, when none was supplied."""
		execution = trace.get("execution") or {}
		exception = execution.get("exception") or {}
		category = exception.get("category")
		if exception:
			if category == CATEGORY_TRANSPORT:
				return OUTCOME_INCOMPLETE
			if category == CATEGORY_TIMEOUT:
				return OUTCOME_TIMED_OUT
			if exception.get("code") == 4204:
				return OUTCOME_CANCELLED
			return OUTCOME_FAILED
		if execution.get("tool_status") == "succeeded":
			return OUTCOME_SUCCEEDED
		if execution.get("tool_status") == "failed":
			# The pipeline said it failed even though no exception object was
			# kept (a serialization failure, for example, is re-raised).
			return OUTCOME_FAILED
		if execution.get("handler_status") == "running":
			return OUTCOME_INCOMPLETE
		return OUTCOME_UNKNOWN

	def assemble(self, trace: dict[str, Any]) -> dict[str, Any]:
		"""Return a copy of ``trace`` with completion and evidence filled in."""
		out = copy.deepcopy(trace)
		execution = out.setdefault("execution", {})
		observation = out.setdefault("observation", {})
		evidence = out.setdefault("evidence", {})
		milestones = list((out.get("lifecycle") or {}).get("milestones") or [])
		names = {milestone.get("name") for milestone in milestones}

		expectation = observation.get("expectation") or {"status": "not_provided"}
		projection_changed = observation.get("projection_changed")
		out["completion"] = {
			"request_accepted": "received" in names,
			"validated": bool(out.get("request", {}).get("validated")),
			"dispatched": execution.get("dispatch_status") == "dispatched",
			"handler_returned": execution.get("handler_status") in ("succeeded", "failed"),
			"handler_succeeded": execution.get("handler_status") == "succeeded",
			"tool_succeeded": execution.get("tool_status") == "succeeded",
			"expectation_status": expectation.get("status", "not_provided"),
			"expectation_satisfied": (
				True
				if expectation.get("status") == "satisfied"
				else False
				if expectation.get("status") == "not_satisfied"
				else None
			),
			"expectation_mismatch": expectation.get("mismatch"),
			"ui_change_detected": projection_changed,
			"observation_status": observation.get("observation_status"),
			"outcome": execution.get("outcome") or self.derive_outcome(out),
		}

		sources = [SOURCE_ACTION_LIFECYCLE]
		if names & {"validated", "tool_completed"}:
			sources.append(SOURCE_TOOL_EXECUTOR)
		if execution.get("handler_status") in ("succeeded", "failed"):
			sources.append(SOURCE_HANDLER)
		if observation.get("events"):
			sources.append(SOURCE_EVENT_JOURNAL)
		if observation.get("projection_available"):
			sources.append(SOURCE_STATE_PROJECTOR)
		if observation.get("settling_status") not in (None, "not_started"):
			sources.append(SOURCE_SETTLER)
		sources.append(SOURCE_BRIDGE_LOG)
		if evidence.get("native_log_status") not in (None, "not_attempted", "unavailable"):
			sources.append(SOURCE_NATIVE_LOG)
		evidence["sources"] = sorted(set(sources))
		evidence["limitations"] = self.limitations(
			outcome=out["completion"]["outcome"],
			observation=observation,
			execution=execution,
			evidence=evidence,
		)
		return out

	def limitations(
		self,
		*,
		outcome: str,
		observation: dict[str, Any],
		execution: dict[str, Any],
		evidence: dict[str, Any],
	) -> list[str]:
		"""List what this record cannot tell you. Always non-empty and bounded."""
		notes: list[str] = []
		if observation.get("projection_changed") is False:
			notes.append(
				"projection_changed=false means the fields the state projector captures "
				"(focus, its ancestors, window, dialog, selection) did not change; it is not "
				"proof that the rest of the UI was unchanged."
			)
		if observation.get("events"):
			notes.append(
				"Events in the action window are temporally correlated with the action, not "
				"proof that the action caused them."
			)
		if observation.get("events_truncated") and (
			observation.get("events") or observation.get("journal_id")
		):
			notes.append(
				"The event history for this action window is incomplete (the journal dropped "
				"entries or the page limit was reached)."
			)
		if observation.get("projection_available") is False:
			notes.append("No UI projection was captured for this action, so its effect is unknown.")
		if observation.get("settling_status") in ("timed_out", "budget_expired"):
			notes.append("The settle budget expired; the final UI state may still have been changing.")
		if execution.get("exception") and execution.get("exception", {}).get("traceback") is None:
			notes.append("No traceback was available for the recorded exception.")
		native_status = evidence.get("native_log_status")
		if native_status == "unavailable":
			notes.append(
				"The native NVDA log could not be read, so add-on-side evidence is missing: "
				+ str(evidence.get("native_log_reason") or "no reason recorded")
			)
		elif native_status == "time_window":
			notes.append(
				"Native NVDA log records are matched by time window only: NVDA's logger does "
				"not carry the trace id, so these matches are best-effort."
			)
		elif native_status == "not_attempted":
			notes.append("Native NVDA log correlation was not attempted for this action.")
		if outcome == OUTCOME_UNKNOWN:
			notes.append("The action ended without a recorded outcome.")
		notes.append(
			"MCP request ids are not forwarded to the bridge, so mcp_request_id is null."
		)
		notes.append("Traces are held in memory only and expire; an expired trace cannot be read back.")
		return notes[:MAX_LIMITATIONS]


# ---------------------------------------------------------------------------
# Summaries and log correlation
# ---------------------------------------------------------------------------


def build_trace_summary(trace: dict[str, Any]) -> dict[str, Any]:
	"""Return the compact diagnostic block carried by every action response.

	Deliberately small: the full record is fetched with ``get_action_trace`` by
	its ``trace_id``. Putting the event list and log matches in every response
	would cost context on every action ever taken.
	"""
	if not isinstance(trace, dict) or not trace:
		return {}
	completion = trace.get("completion") or {}
	observation = trace.get("observation") or {}
	execution = trace.get("execution") or {}
	exception = execution.get("exception") or None
	return {
		"schema_version": trace.get("schema_version", SCHEMA_VERSION),
		"trace_id": trace.get("trace_id"),
		"action": (trace.get("action") or {}).get("name"),
		"outcome": completion.get("outcome"),
		"duration_ms": (trace.get("action") or {}).get("duration_ms"),
		"exception": (
			{
				"type": exception.get("type"),
				"category": exception.get("category"),
				"stage": exception.get("stage"),
			}
			if exception
			else None
		),
		"event_count": observation.get("event_count", 0),
		"from_event_version": observation.get("from_event_version"),
		"to_event_version": observation.get("to_event_version"),
		"events_complete": observation.get("events_complete"),
		"projection_changed": observation.get("projection_changed"),
		"projection_available": observation.get("projection_available"),
		"observation_status": observation.get("observation_status"),
		"expectation_status": completion.get("expectation_status"),
		"native_log_status": (trace.get("evidence") or {}).get("native_log_status"),
		"retained": (trace.get("retention") or {}).get("state") == "finalized",
		"limitations": (trace.get("evidence") or {}).get("limitations", []),
	}


#: Matches ``(HH:MM:SS.mmm)`` (NVDA's IO log lines) and ``YYYY-MM-DD HH:MM:SS,mmm``
#: (its logger lines). Used only to decide whether a line falls in the action
#: window; it is never used to claim causation.
_LOG_TIME_PATTERNS = (
	r"\((\d{2}:\d{2}:\d{2}\.\d{3})\)",
	r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})",
)


def match_native_log(
	lines: Iterable[str],
	*,
	trace_id: Optional[str],
	start_wall: float,
	finish_wall: float,
	limit: int = MAX_LOG_MATCHES,
) -> tuple[list[dict[str, Any]], str]:
	"""Pick native log lines that may belong to this action.

	Returns ``(matches, status)`` where status is ``exact`` (the line contains
	the trace id, which the bridge writes itself), ``time_window`` (an
	ERROR/WARNING line whose timestamp falls inside the action window), or
	``none``. Matching by id is direct evidence; matching by time is not, and
	each match records which one it was.
	"""
	matches: list[dict[str, Any]] = []
	start_local = datetime.fromtimestamp(start_wall)
	finish_local = datetime.fromtimestamp(finish_wall)
	saw_time_match = False
	for line in lines:
		if len(matches) >= limit:
			break
		text = str(line)
		if trace_id and trace_id in text:
			matches.append(
				{
					"line": bounded_text(text, 500)[0],
					"severity": _severity_of(text),
					"matching": "exact_trace_id",
					"association": "direct",
				}
			)
			continue
		severity = _severity_of(text)
		if severity not in ("ERROR", "WARNING", "CRITICAL"):
			continue
		moment = _log_line_time(text)
		if moment is None:
			continue
		# One second of slack on each side: NVDA's timestamps are millisecond
		# precise, but the line is written after the record it describes.
		if start_local.timestamp() - 1.0 <= moment.timestamp() <= finish_local.timestamp() + 1.0:
			saw_time_match = True
			matches.append(
				{
					"line": bounded_text(text, 500)[0],
					"severity": severity,
					"matching": "time_window",
					"association": "temporally_correlated",
				}
			)
	if any(match["matching"] == "exact_trace_id" for match in matches):
		return matches, "exact"
	if saw_time_match:
		return matches, "time_window"
	return matches, "none"


def _severity_of(line: str) -> str:
	for level in ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "DEBUGWARNING", "IO"):
		if line.startswith(level + " ") or f" - {level} - " in line:
			return "WARNING" if level == "DEBUGWARNING" else level
	return "UNKNOWN"


def _log_line_time(line: str) -> Optional[datetime]:
	"""Parse a log line's timestamp as local wall time, or return ``None``."""
	import re

	for pattern in _LOG_TIME_PATTERNS:
		match = re.search(pattern, line)
		if not match:
			continue
		text = match.group(1)
		try:
			if "," in text:
				return datetime.strptime(text, "%Y-%m-%d %H:%M:%S,%f")
			parsed = datetime.strptime(text, "%H:%M:%S.%f")
			today = datetime.now()
			return parsed.replace(year=today.year, month=today.month, day=today.day)
		except ValueError:
			continue
	return None
