"""Dataclass models for all NVDA MCP Bridge tools.

Strict input/output schemas for every bridge tool.
No implicit coercion, all fields explicit.
Zero-dependency models using stdlib dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional


#: Every reference handed out by ``observe``/``locate`` matches this shape.
REF_PATTERN = r"^[a-z][a-z0-9]*$"


# ============================================================================
# INPUT MODELS
# ============================================================================


@dataclass(frozen=True)
class EmptyInput:
	"""No input required."""

	pass





@dataclass(frozen=True)
class SpeakTextInput:
	"""Speak text using NVDA's speech system."""

	text: str


@dataclass(frozen=True)
class ShowMessageInput:
	"""Show a UI message to the user."""

	message: str





@dataclass(frozen=True)
class InspectObjectInput:
	"""Inspect object with rich metadata."""

	target: Literal["focus", "navigator", "desktop"] = "focus"
	include_relations: bool = False


@dataclass(frozen=True)
class WalkObjectTreeInput:
	"""Walk object tree with bounds."""

	target: Literal["focus", "navigator", "desktop"] = "focus"
	max_depth: int = 3
	max_children: int = 20


@dataclass(frozen=True)
class GetFocusLineageInput:
	"""Get focus lineage with ancestors."""

	max_ancestors: int = 10


@dataclass(frozen=True)
class InspectAppModuleInput:
	"""Inspect app module metadata."""

	source: Literal["focus", "navigator", "desktop"] = "focus"


@dataclass(frozen=True)
class ListLoadedAddonsInput:
	"""List loaded addons."""

	scope: Literal["running", "available", "all"] = "running"


@dataclass(frozen=True)
class HealthSnapshotInput:
	"""Collect a small health summary. Detail is opt-in."""

	include_focus: bool = field(
		default=True,
		metadata={"description": "Include the focused control and its top-level window."},
	)
	include_addons: bool = field(
		default=False,
		metadata={"description": "Include add-on counts and the running add-on names."},
	)
	include_logs: bool = field(
		default=False,
		metadata={"description": "Include the tail of NVDA's native log."},
	)
	log_lines: int = field(
		default=20,
		metadata={"minimum": 1, "maximum": 500, "description": "Log lines when include_logs is true."},
	)


@dataclass(frozen=True)
class RestartNvdaInput:
	"""Restart NVDA with options."""

	disable_addons: bool = False
	debug_logging: bool = False


@dataclass(frozen=True)
class GetLogsInput:
	"""Get NVDA logs."""

	max_lines: int = 100
	filter_level: Optional[Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]] = None
	use_tail: bool = True


@dataclass(frozen=True)
class LogSourceFilter:
	"""Glob filters over the native logger/source string.

	``include`` and ``exclude`` accept exact names, dotted prefixes and ``*`` /
	``?`` globs. Exclusion wins. No regular expressions are evaluated here.
	"""

	include: list[str] = field(default_factory=list)
	exclude: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LogMessageFilter:
	"""Substring or bounded-regex filtering of a record's message."""

	contains: Optional[str] = None
	regex: Optional[str] = None
	case_sensitive: bool = False


@dataclass(frozen=True)
class LogTimeFilter:
	"""Time-of-day relative or absolute range.

	NVDA's native log header has no calendar date, so a bare ``HH:MM:SS``
	filters time-of-day and reports that limitation. ISO values with a date are
	used when a record carries a date.
	"""

	last_seconds: Optional[int] = None
	start: Optional[str] = None
	end: Optional[str] = None


@dataclass(frozen=True)
class SearchLogsInput:
	"""Search native NVDA log records with structured filters.

	Use either ``levels`` (exact set) or ``min_level`` (threshold), not both.
	"""

	levels: list[str] = field(default_factory=list)
	min_level: Optional[str] = None
	sources: LogSourceFilter = field(default_factory=LogSourceFilter)
	addon: Optional[str] = None
	message: LogMessageFilter = field(default_factory=LogMessageFilter)
	time: LogTimeFilter = field(default_factory=LogTimeFilter)
	threads: list[str] = field(default_factory=list)
	trace_id: Optional[str] = None
	limit: int = field(default=20, metadata={"minimum": 1, "maximum": 200})
	cursor: Optional[str] = None
	order: Literal["newest_first", "oldest_first"] = "newest_first"
	include_raw: bool = False
	max_scan_records: int = field(default=500000, metadata={"minimum": 1, "maximum": 5000000})


@dataclass(frozen=True)
class GetLogContextInput:
	"""Return surrounding evidence for one previously found log entry."""

	entry_id: str
	before: int = 20
	after: int = 30
	include_related_threads: bool = True
	include_raw: bool = False
	before_ms: Optional[int] = None
	after_ms: Optional[int] = None


@dataclass(frozen=True)
class SummarizeLogsInput:
	"""Compute bounded deterministic statistics over native log records."""

	levels: list[str] = field(default_factory=list)
	min_level: Optional[str] = None
	sources: LogSourceFilter = field(default_factory=LogSourceFilter)
	addon: Optional[str] = None
	time: LogTimeFilter = field(default_factory=LogTimeFilter)
	threads: list[str] = field(default_factory=list)
	group_by: list[str] = field(
		default_factory=lambda: ["source", "level", "message_pattern"]
	)
	include: list[str] = field(
		default_factory=lambda: [
			"counts",
			"repeated_patterns",
			"exceptions",
			"warnings",
			"top_sources",
			"time_buckets",
			"noise",
		]
	)
	limit: int = 20
	max_scan_records: int = 500000


@dataclass(frozen=True)
class ExecutePythonInput:
	"""Execute Python code in NVDA."""

	code: str
	snapshot_vars: bool = False
	capture_vars: Optional[list[str]] = None


# ============================================================================
# ACTION INPUT MODELS (implementation plan P2)
# ============================================================================

#: A postcondition an action can be asked to confirm, e.g. ``{"focus.name": "Search"}``.
#: Values are scalars or lists of scalars; arbitrary nested objects are rejected so
#: a typo is a schema error rather than an expectation that can never match.
ExpectationValue = str | int | float | bool | list[str]
Expectation = Optional[dict[str, ExpectationValue]]


@dataclass(frozen=True)
class ObserveInput:
	"""What to capture in one observation.

	The default is deliberately small: focus, dialog and the top-level window.
	Ancestry, children and selection cost a main-thread read each and are opt-in.
	"""

	include: list[Literal["focus", "ancestors", "children", "selection", "dialog"]] = field(
		default_factory=lambda: ["focus", "dialog"],
		metadata={"description": "Optional sections beyond focus and the top-level window."},
	)
	max_text: int = field(
		default=120,
		metadata={"minimum": 0, "maximum": 2000, "description": "Maximum characters per value."},
	)
	detail: Literal["summary", "full"] = field(
		default="summary",
		metadata={"description": "summary leaves states empty; full includes them."},
	)
	max_bytes: int = field(
		default=0,
		metadata={
			"minimum": 0,
			"maximum": 200000,
			"description": "Bound the serialized observation; 0 disables the bound. Truncation is reported.",
		},
	)


@dataclass(frozen=True)
class InvokeInput:
	"""Invoke an object's default action, or a named one."""

	ref: str = field(metadata={"pattern": REF_PATTERN})
	action: Optional[str] = None
	expect: Expectation = None


@dataclass(frozen=True)
class SetValueInput:
	"""Set the value of a control."""

	ref: str = field(metadata={"pattern": REF_PATTERN})
	value: str
	expect: Expectation = None


@dataclass(frozen=True)
class FocusInput:
	"""Move focus to a referenced object."""

	ref: str = field(metadata={"pattern": REF_PATTERN})
	expect: Expectation = None


@dataclass(frozen=True)
class ActivateWindowInput:
	"""Bring a top-level window to the foreground."""

	title: str = ""
	handle: int = 0
	exact: bool = False
	expect: Expectation = None


@dataclass(frozen=True)
class PressInput:
	"""Send keyboard keys.

	``keys`` accepts NVDA gesture spelling plus the aliases agents type:
	``ctrl``, ``win``, ``esc``, ``return``, ``pgup``, ``pgdn``, and the arrow
	names ``up``/``down``/``left``/``right``.
	"""

	keys: str = field(
		metadata={"minLength": 1, "description": "Gesture name, e.g. control+a or down."},
	)
	repeat: int = field(default=1, metadata={"minimum": 1, "maximum": 20})
	delay_ms: int = field(default=0, metadata={"minimum": 0, "maximum": 5000})
	expect: Expectation = None


@dataclass(frozen=True)
class ReadInput:
	"""Read text from a referenced object, or the focus."""

	ref: Optional[str] = field(default=None, metadata={"pattern": REF_PATTERN})
	scope: Literal["line", "selection", "document", "subtree"] = "line"
	max_chars: int = field(default=4000, metadata={"minimum": 0, "maximum": 100000})


@dataclass(frozen=True)
class NavigateInput:
	"""Move through the accessibility tree."""

	direction: Literal["next", "previous", "first", "last", "parent", "child"]
	ref: Optional[str] = field(default=None, metadata={"pattern": REF_PATTERN})
	expect: Expectation = None


@dataclass(frozen=True)
class ScrollInput:
	"""Scroll the focused container."""

	direction: Literal["up", "down", "left", "right"] = "down"
	amount: Literal["page", "line"] = "page"
	ref: Optional[str] = field(default=None, metadata={"pattern": REF_PATTERN})
	expect: Expectation = None


@dataclass(frozen=True)
class DismissInput:
	"""Dismiss the current dialog."""

	expect: Expectation = None


@dataclass(frozen=True)
class ClickAtInput:
	"""Click absolute screen coordinates."""

	x: int
	y: int
	button: Literal["left", "right", "middle"] = "left"
	expect: Expectation = None


@dataclass(frozen=True)
class WaitForEventsInput:
	"""Wait for the next coalesced transition.

	``since`` defaults to the bridge's current journal head, so the common
	"wait for the next change" call never replays journal history. Pass ``0``
	explicitly to read history.

	For explicit catch-up, pair ``since`` with ``journal_id``. Together they
	form the typed cursor ``(journal_id, version)``; a cursor from a different
	journal generation is reported as a mismatch instead of being interpreted
	against unrelated history. ``journal_id`` alone means "the current head of
	that generation", which is a valid no-history wait.
	"""

	since: Optional[int] = field(default=None, metadata={"minimum": 0})
	journal_id: Optional[str] = field(
		default=None,
		metadata={"description": "Journal generation that issued `since`. Omit only for a current-journal convenience."},
	)
	timeout_ms: int = field(default=1000, metadata={"minimum": 0, "maximum": 30000})
	kinds: Optional[list[str]] = None
	limit: int = field(default=50, metadata={"minimum": 1, "maximum": 500})


@dataclass(frozen=True)
class LocateInput:
	"""Search for controls by role and name.

	``within`` must be a reference returned by ``observe``/``locate``; a window
	title is not a reference and is rejected with a validation error.
	"""

	role: Optional[str] = None
	name: Optional[str] = None
	exact: bool = False
	within: Optional[str] = field(default=None, metadata={"pattern": REF_PATTERN})
	limit: int = field(default=10, metadata={"minimum": 1, "maximum": 50})
	max_nodes: int = field(default=400, metadata={"minimum": 1, "maximum": 2000})
	timeout_ms: int = field(default=500, metadata={"minimum": 50, "maximum": 3000})
	detail: Literal["summary", "full"] = field(
		default="summary",
		metadata={"description": "summary leaves path, depth, automation id and states empty."},
	)


# ============================================================================
# OUTPUT MODELS
# ============================================================================





@dataclass(frozen=True)
class Location:
	"""Screen rectangle in pixels; the same shape everywhere."""

	left: int
	top: int
	width: int
	height: int


@dataclass(frozen=True)
class WindowInfo:
	"""The top-level window that scopes an observation or action."""

	ref: str = ""
	title: str = ""
	handle: int = 0
	class_name: str = ""


@dataclass(frozen=True)
class Node:
	"""One accessibility node, with the reference that points at it."""

	ref: str = field(metadata={"pattern": REF_PATTERN})
	role: str
	name: Optional[str] = None
	value: Optional[str] = None
	states: list[str] = field(default_factory=list)
	location: Optional[Location] = None


@dataclass(frozen=True)
class DialogNode(Node):
	"""A dialog node, with the actionable children captured for it."""

	children: list[Node] = field(default_factory=list)


@dataclass(frozen=True)
class SpeechResult:
	"""Speech action result."""

	spoken: str
	status: str = "ok"


@dataclass(frozen=True)
class MessageResult:
	"""Message display result."""

	message: str
	status: str = "ok"








@dataclass(frozen=True)
class ObjectInspection:
	"""Rich object inspection."""

	target: str
	object: dict[str, Any]


@dataclass(frozen=True)
class ObjectTreeSnapshot:
	"""Object tree snapshot."""

	target: str
	max_depth: int
	max_children: int
	tree: dict[str, Any]


@dataclass(frozen=True)
class FocusLineage:
	"""Focus lineage with ancestors."""

	focus: dict[str, Any]
	ancestor_count: int
	truncated: bool
	ancestors: list[dict[str, Any]]


@dataclass(frozen=True)
class AppModuleInfo:
	"""App module information."""

	source: str
	object: dict[str, Any]
	app_module: dict[str, Any]


@dataclass(frozen=True)
class AddonMetadataList:
	"""List of addon metadata."""

	scope: str
	count: int
	running_count: int
	available_count: int
	addons: list[dict[str, Any]]


@dataclass(frozen=True)
class AddonSummary:
	"""Lean add-on counts; names are included because a stale add-on is the common bug."""

	running_count: int
	available_count: int
	running: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class HealthSnapshot:
	"""Health diagnostics snapshot.

	The default response is status, focus/window and errors. Logs and add-on
	names are opt-in, because they were the bulk of the old payload.
	"""

	timestamp: int
	ok: bool
	error_count: int
	errors: dict[str, Any] = field(default_factory=dict)
	nvda: dict[str, Any] = field(default_factory=dict)
	focus: Optional[Node] = None
	top_window: Optional[WindowInfo] = None
	addons: Optional[AddonSummary] = None
	logs: Optional["LogEntries"] = None


@dataclass(frozen=True)
class NvdaRestartResult:
	"""NVDA restart result."""

	status: str


@dataclass(frozen=True)
class LogEntries:
	"""Log entries."""

	log_file: str
	returned_lines: int
	max_lines: int
	lines: list[str]
	file_size: int
	filter_level: Optional[str] = None


@dataclass(frozen=True)
class LogThread:
	"""The thread named by one native log header."""

	name: str
	id: Optional[int] = None


@dataclass(frozen=True)
class LogEntry:
	"""One native log record, parsed into structured evidence.

	``timestamp`` is the observed clock time and ``timestamp_ms`` its
	millisecond offset within the day. ``timestamp_date`` is populated only when
	the header itself carried a date. ``raw`` is returned only on explicit
	request; without it the exact bytes remain recoverable from the file by
	``entry_id`` while the generation is unchanged.
	"""

	entry_id: str
	timestamp: Optional[str]
	timestamp_ms: Optional[int]
	timestamp_date: Optional[str]
	timestamp_date_source: str
	timestamp_absolute: Optional[str]
	timestamp_timezone: str
	level: str
	logger: str
	module: str
	function: str
	subsystem: str
	addon: Optional[str]
	thread: LogThread
	message: str
	line_start: int
	line_end: int
	parse_status: str
	parse_notes: list[str] = field(default_factory=list)
	derived_fields: list[str] = field(default_factory=list)
	exception: Optional[dict[str, Any]] = None
	enrichment: Optional[dict[str, Any]] = None
	raw: Optional[str] = None


@dataclass(frozen=True)
class LogSearchPage:
	"""One stable page of search results."""

	returned: int
	has_more: bool
	next_cursor: Optional[str]
	limit: int
	order: str


@dataclass(frozen=True)
class LogSourceInfo:
	"""Identity of the log generation a response was read from."""

	generation: str
	size_bytes: int
	source: str = "nvda_native_log"


@dataclass(frozen=True)
class LogSearchDiagnostics:
	"""Completeness and precision facts about one search."""

	truncated: bool
	parse_failures: int
	time_filter_precision: str
	scan_budget_exhausted: bool
	response_truncated: bool
	invalid_utf8_replacements: int
	records_examined: int
	matched_records: int
	limitations: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LogSearchResult:
	"""Structured native log search response."""

	schema_version: int
	entries: list[LogEntry]
	page: LogSearchPage
	source: LogSourceInfo
	diagnostics: LogSearchDiagnostics


@dataclass(frozen=True)
class LogContextDiagnostics:
	"""Completeness facts about a context window."""

	truncated: bool
	response_truncated: bool
	parse_failures: int
	invalid_utf8_replacements: int
	records_examined: int
	limitations: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LogContextResult:
	"""A chronological window around one log entry."""

	schema_version: int
	entry_id: str
	anchor: LogEntry
	entries: list[LogEntry]
	before: int
	after: int
	returned: int
	complete_before: bool
	complete_after: bool
	include_related_threads: bool
	source: LogSourceInfo
	diagnostics: LogContextDiagnostics


@dataclass(frozen=True)
class LogSummaryCoverage:
	"""What a summary examined and what it could not claim."""

	records_examined: int
	records_matched: int
	complete: bool
	time_budget_exhausted: bool
	parse_failures: int
	invalid_utf8_replacements: int
	time_filter_precision: str
	source: str
	generation: str
	size_bytes: int
	scan_ms: float
	group_by: list[str] = field(default_factory=list)
	limitations: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LogSummaryResult:
	"""Deterministic summary of native log evidence."""

	schema_version: int
	summary: dict[str, Any]
	coverage: LogSummaryCoverage


@dataclass(frozen=True)
class PythonExecutionResult:
	"""Python execution result."""

	stdout: str = ""
	stderr: str = ""
	result: Optional[str] = None
	captured_variables: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class ReloadResult:
	"""Result of reloading global plugins and app modules."""

	status: str
	message: str = ""


# ============================================================================
# ACTION OUTPUT MODELS (implementation plan P2)
# ============================================================================


@dataclass(frozen=True)
class ObserveResult:
	"""One observation of the screen.

	``refs`` maps every reference in this payload to its kind (``focus``,
	``ancestor``, ``child``, ``dialog``, ``window``). ``state_version`` is the
	version these references are valid for; every action result repeats it.
	"""

	state_version: int
	revision: int
	refs: dict[str, str] = field(default_factory=dict)
	focus: Optional[Node] = None
	ancestors: list[Node] = field(default_factory=list)
	children: list[Node] = field(default_factory=list)
	dialog: Optional[DialogNode] = None
	top_window: Optional[WindowInfo] = None
	selection: Optional[str] = None
	truncated: bool = False
	truncated_sections: list[str] = field(default_factory=list)
	more_available: bool = False
	truncation_hint: str = ""


@dataclass(frozen=True)
class ReadResult:
	"""Text read from a scope.

	``source`` is ``textinfo``, ``uia``, ``tree`` (the accessibility tree was
	used because the text interface yielded no text) or ``unavailable`` (neither
	worked). A ``text`` of ``""`` with ``source: "unavailable"`` is an answer,
	not a silent success.
	"""

	text: str
	scope: str
	source: str
	ref: Optional[str] = None
	truncated: bool = False


@dataclass(frozen=True)
class InvokeResult:
	"""Result of invoking an object action."""

	ref: str
	invoked: bool
	index: int
	action_name: str
	available: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SetValueResult:
	"""How a value was set, and whether the control agrees."""

	ref: str
	mode: str
	value: str
	previous: str = ""
	units: int = 0
	verified: bool = True
	observed: str = ""
	value_source: str = ""


@dataclass(frozen=True)
class ActivateWindowResult:
	"""The window that ended up in front, and how it got there.

	``method`` is ``foreground`` (this call raised it), ``already-foreground``
	(nothing to do) or ``refused`` (Windows said no — reported as an error, this
	value is only reachable if the check is ever relaxed).

	``matches`` counts the windows whose title matched, so a caller that passed a
	loose title can tell whether the choice was ambiguous.

	``focus_in_window`` answers a different question from ``activated``: Windows
	can move the foreground without moving the keyboard focus, so a window can be
	in front while the screen reader is still reading the previous one. ``None``
	means the question could not be answered; ``False`` means it was, and
	``focus_window`` names where the focus actually is.
	"""

	activated: bool
	handle: int
	title: str
	ref: str = ""
	previous_title: str = ""
	method: str = "foreground"
	restored: bool = False
	waited_ms: float = 0.0
	matches: int = 1
	focus_in_window: Optional[bool] = None
	focus_window: str = ""


@dataclass(frozen=True)
class FocusResult:
	"""Result of a focus request."""

	ref: str
	requested: bool


@dataclass(frozen=True)
class PressResult:
	"""Result of a keyboard dispatch.

	``requested`` is what the caller sent; ``normalized_keys`` is the NVDA
	gesture spelling that was actually dispatched.
	"""

	requested: str
	normalized_keys: str
	repeat: int
	delay_ms: int
	sent: bool
	sent_count: int
	dispatched: bool
	cancelled: bool = False
	uncertain: bool = False


@dataclass(frozen=True)
class NavigateResult:
	"""Result of moving through the tree."""

	direction: str
	ref: Optional[str]
	focused: bool


@dataclass(frozen=True)
class ScrollResult:
	"""How a scroll was performed."""

	direction: str
	amount: str
	method: str
	key: str


@dataclass(frozen=True)
class DismissResult:
	"""What dismiss tried, and whether a dialog remained."""

	dismissed: bool
	steps: list[str] = field(default_factory=list)
	dialog_name: str = ""


@dataclass(frozen=True)
class ClickAtResult:
	"""Result of a raw pointer click.

	``on_target`` is read back from the pointer after the click: ``SendInput``
	only says the events were accepted, and a click that landed somewhere else
	must not look successful.
	"""

	x: int
	y: int
	button: str
	hit: Optional[dict[str, Any]] = None
	cursor: list[int] = field(default_factory=list)
	on_target: bool = True


@dataclass(frozen=True)
class WaitEventsResult:
	"""Coalesced transitions observed while waiting.

	``journal_id`` identifies the generation that issued ``from_version`` and
	``to_version``. ``to_version`` is the cursor for the next call — the
	version of the last transition returned, not the journal's head. When more
	transitions are already waiting than one page holds, ``has_more`` says to
	poll again immediately with ``since = to_version``; nothing is skipped by
	paging.

	Explicit catch-up is only safe when the history is complete. ``expired``
	means events before the cursor were evicted, ``resync_required`` means the
	caller must not treat this page as a complete record, and ``resync_from``
	is the typed cursor to continue from after re-observing. A cursor from a
	different generation is reported by ``cursor_status =
	"journal_mismatch"`` with ``resync_required = true``.
	"""

	journal_id: str
	from_version: int
	to_version: int
	earliest_available_version: int = 0
	events: list[dict[str, Any]] = field(default_factory=list)
	expired: bool = False
	resync_required: bool = False
	resync_reason: Optional[str] = None
	resync_from: Optional[dict[str, Any]] = None
	history_complete: bool = True
	truncated: bool = False
	has_more: bool = False
	cursor_status: str = "ok"
	timed_out: bool = False
	cancelled: bool = False


# ============================================================================
# LOCATE OUTPUT MODELS (implementation plan P5)
# ============================================================================


@dataclass(frozen=True)
class LocateMatch:
	"""One control found by ``locate``.

	``target`` says what kind of handle ``ref`` is:

	* ``uia`` — an ``IUIAutomationElement``; invoke/focus/read use UIA patterns
	  and need no keyboard. ``automationId`` is often the most stable selector a
	  replay could use.
	* ``nvda`` — an NVDA object from the bounded walk; identical to an
	  observation reference, so every action works as usual.

	``location`` is a ``{left, top, width, height}`` rectangle in screen pixels,
	the same shape an observed node uses; its centre is a legal ``click_at``
	point.
	"""

	ref: str = field(metadata={"pattern": REF_PATTERN})
	role: str
	name: str = ""
	value: str = ""
	states: list[str] = field(default_factory=list)
	location: Optional[Location] = None
	automation_id: str = ""
	depth: int = -1
	path: list[str] = field(default_factory=list)
	target: str = "uia"


@dataclass(frozen=True)
class LocateResult:
	"""The outcome of one search.

	``method`` is ``uia`` (provider-side), ``uia-window`` (the ref had no UIA
	representation of its own, so the search ran over its containing window —
	wider than asked), or ``walk`` (bounded breadth-first through NVDA objects).

	``searched`` counts what the backend examined: candidates returned by UIA, or
	nodes visited by the walk. An empty ``matches`` list is a normal answer and
	means the search completed; ``truncated`` is what says it stopped early.
	"""

	matches: list[LocateMatch] = field(default_factory=list)
	method: str = "uia"
	searched: int = 0
	truncated: bool = False
	elapsed_ms: float = 0.0
	scope: str = ""


# ============================================================================
# DOCUMENT READING MODELS (implementation plan P5.2)
# ============================================================================


@dataclass(frozen=True)
class ListReadingTargetsInput:
	"""Enumerate a document's positions of one kind, without moving the cursor.

	``item`` is one of ``heading``, ``heading1``…``heading6``, ``link``,
	``landmark``, ``formfield``, ``button``, ``edit``, ``list``, ``listitem``,
	``table``, ``graphic``, ``checkbox``, ``radiobutton``, ``combobox``,
	``frame``, ``annotation``, ``reference``. A document that cannot answer
	reports what it can.

	``timeout_ms`` bounds the work done on NVDA's main thread, because a large
	document is otherwise the only limit: the call returns what it reached and
	says it was cut. Starting browse mode for a document read in focus mode is a
	separate action — ``start_browse_mode`` — not something a read does for you.
	"""

	item: str = "heading"
	limit: int = field(default=20, metadata={"minimum": 1, "maximum": 200})
	within: Optional[str] = field(default=None, metadata={"pattern": REF_PATTERN})
	timeout_ms: int = field(default=1000, metadata={"minimum": 50, "maximum": 5000})


@dataclass(frozen=True)
class ReadingTarget:
	"""One position in a document, in reading order.

	``label`` is what NVDA would announce for the position; ``text`` is the raw
	text there (they differ for links, where the label carries "visited" state,
	and for form fields, where the label carries the control's role and states).

	``level`` is a heading level when the position is a heading, otherwise 0.
	``ref`` is empty for a position with no control behind it — a plain heading
	is a text range, and ``navigate_reading`` is how you get there.
	"""

	ref: str = ""
	role: str = ""
	label: str = ""
	text: str = ""
	level: int = 0


@dataclass(frozen=True)
class ListReadingTargetsResult:
	"""A document's positions of one kind, in the order a reader meets them.

	``available`` is what this document can be searched by, and ``source`` says
	how that was learned: ``probed`` (the implementation's own type table) or
	``declared`` (read out of NVDA's source for document types that cannot be
	asked).

	``interface`` names the browse mode implementation that answered, which is
	the same distinction ``locate`` makes with ``method``: ``ChromeVBuf`` is a
	virtual buffer, ``WordBrowseModeDocument`` a text-pattern document.

	``pass_through`` is focus mode: the move is available to a tool either way,
	but a user's keyboard would not do it while focus mode is on.

	``truncated`` says the list is not the whole document. ``stopped`` says why
	it ends where it does: ``end`` (the document ran out), ``limit`` (the
	requested number was reached) or ``timeout`` (the main-thread budget
	expired). A list that stopped on ``timeout`` is still truncated — a partial
	answer is never presented as a complete one.
	"""

	item: str = "heading"
	available: list[str] = field(default_factory=list)
	browse_mode: bool = True
	document: str = ""
	interface: str = ""
	pass_through: bool = False
	source: str = ""
	targets: list[ReadingTarget] = field(default_factory=list)
	truncated: bool = False
	stopped: str = ""
	scanned: int = 0
	timeout_ms: int = 0
	elapsed_ms: float = 0.0


@dataclass(frozen=True)
class StartBrowseModeInput:
	"""Start browse mode for a document NVDA is reading in focus mode.

	The same thing NVDA+space does: it builds the document model the reading
	tools need, which some applications — Word, by default — only create when
	asked. ``within`` names the window or document to start it for; without it
	the focused object is used.

	This is an action: it changes the user's session, so it is not a flag on a
	read and a read-only client cannot do it.
	"""

	within: Optional[str] = field(default=None, metadata={"pattern": REF_PATTERN})
	expect: Expectation = None


@dataclass(frozen=True)
class StartBrowseModeResult:
	"""The document that is now readable, and whether this call created it.

	``created`` is false when a model was already there, which makes the call
	idempotent: asking twice is asking once, and the reply says which one
	happened. ``interface`` names the implementation that answers — the same
	distinction the reading tools report — so the next call's expectations are
	not a guess.
	"""

	created: bool = False
	browse_mode: bool = False
	document: str = ""
	interface: str = ""
	pass_through: bool = False
	elapsed_ms: float = 0.0


@dataclass(frozen=True)
class NavigateReadingInput:
	"""Move the document's reading position.

	``direction`` is ``next``, ``previous`` (relative to where the document's
	cursor is now), or ``first``/``last`` (absolute). ``steps`` skips ahead —
	three headings forward is one call, not three. ``timeout_ms`` bounds the
	main-thread work; a move that runs out of budget reports how far it got
	instead of continuing.
	"""

	item: str = "heading"
	direction: str = "next"
	steps: int = field(default=1, metadata={"minimum": 1, "maximum": 50})
	within: Optional[str] = field(default=None, metadata={"pattern": REF_PATTERN})
	timeout_ms: int = field(default=1000, metadata={"minimum": 50, "maximum": 5000})
	expect: Expectation = None


@dataclass(frozen=True)
class NavigateReadingResult:
	"""Where the reading position landed.

	``reached`` is how many positions were actually passed over; when the
	document ends first the cursor stops at the last one, exactly as pressing the
	key repeatedly would, and ``reached`` is lower than ``steps``. ``stopped``
	says which of those happened: ``steps``, ``end``, ``stalled`` or ``timeout``
	— a move that ran out of budget really did move the cursor that far.

	``ref`` is empty when the position is text rather than a control.
	"""

	item: str = "heading"
	direction: str = "next"
	steps: int = 1
	reached: int = 0
	stopped: str = ""
	ref: str = ""
	role: str = ""
	label: str = ""
	text: str = ""
	level: int = 0
	landed: bool = False
	browse_mode: bool = True
	document: str = ""
	interface: str = ""
	pass_through: bool = False
	timeout_ms: int = 0
	elapsed_ms: float = 0.0


# ============================================================================
# ACTION TRACE OUTPUT MODELS
# ============================================================================


@dataclass(frozen=True)
class GetActionTraceInput:
	"""Identify one action trace to read back."""

	trace_id: str


@dataclass(frozen=True)
class ActionTraceResult:
	"""One retained action trace, or the reason it is not available.

	``found: false`` is a normal answer, not a failure: traces are held in
	memory and expire, and a caller that keeps an id past the retention window
	needs to be told that rather than given an error that looks like the
	action's own failure. ``retention`` always says what the window was.
	"""

	trace_id: str
	found: bool
	schema_version: int = 1
	trace: Optional[dict[str, Any]] = None
	retention: Optional[dict[str, Any]] = None
	missing_reason: str = ""
	"""Why the record is unavailable: ``expired_or_unknown`` or ``not_stored``."""
