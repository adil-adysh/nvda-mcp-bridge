"""Dataclass models for all NVDA MCP Bridge tools.

Strict input/output schemas for every bridge tool.
No implicit coercion, all fields explicit.
Zero-dependency models using stdlib dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional


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
	"""Collect diagnostics snapshot."""

	include_logs: bool = False
	log_lines: int = 100
	include_tree: bool = False
	tree_depth: int = 2
	tree_max_children: int = 10
	addon_scope: Literal["running", "available", "all"] = "running"


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
class ExecutePythonInput:
	"""Execute Python code in NVDA."""

	code: str
	snapshot_vars: bool = False
	capture_vars: Optional[list[str]] = None


# ============================================================================
# ACTION INPUT MODELS (implementation plan P2)
# ============================================================================

#: A postcondition an action can be asked to confirm, e.g. ``{"focus.name": "Search"}``.
Expectation = Optional[dict[str, Any]]


@dataclass(frozen=True)
class ObserveInput:
	"""What to capture in one observation."""

	include: list[Literal["focus", "ancestors", "children", "selection", "dialog"]] = field(
		default_factory=lambda: ["focus", "ancestors", "children", "dialog"]
	)
	max_text: int = 200


@dataclass(frozen=True)
class InvokeInput:
	"""Invoke an object's default action, or a named one."""

	ref: str
	action: Optional[str] = None
	expect: Expectation = None


@dataclass(frozen=True)
class SetValueInput:
	"""Set the value of a control."""

	ref: str
	value: str
	expect: Expectation = None


@dataclass(frozen=True)
class FocusInput:
	"""Move focus to a referenced object."""

	ref: str
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
	"""Send keyboard keys."""

	keys: str
	repeat: int = 1
	delay_ms: int = 0
	expect: Expectation = None


@dataclass(frozen=True)
class ReadInput:
	"""Read text from a referenced object, or the focus."""

	ref: Optional[str] = None
	scope: Literal["line", "selection", "document", "subtree"] = "line"
	max_chars: int = 4000


@dataclass(frozen=True)
class NavigateInput:
	"""Move through the accessibility tree."""

	direction: Literal["next", "previous", "first", "last", "parent", "child"]
	ref: Optional[str] = None
	expect: Expectation = None


@dataclass(frozen=True)
class ScrollInput:
	"""Scroll the focused container."""

	direction: Literal["up", "down", "left", "right"] = "down"
	amount: Literal["page", "line"] = "page"
	ref: Optional[str] = None
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
	"""Wait for the next coalesced transition."""

	since: int = 0
	timeout_ms: int = 1000
	kinds: Optional[list[str]] = None


@dataclass(frozen=True)
class LocateInput:
	"""Search for controls by role and name.

	``within`` narrows the search to a referenced subtree; without it the search
	runs over the containing dialog when one is open, otherwise the application
	window. ``max_nodes`` and ``timeout_ms`` bound the fallback walk, which is the
	only path that can be slow.
	"""

	role: Optional[str] = None
	name: Optional[str] = None
	exact: bool = False
	within: Optional[str] = None
	limit: int = 10
	max_nodes: int = 400
	timeout_ms: int = 500


# ============================================================================
# OUTPUT MODELS
# ============================================================================





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
	ancestorCount: int
	truncated: bool
	ancestors: list[dict[str, Any]]


@dataclass(frozen=True)
class AppModuleInfo:
	"""App module information."""

	source: str
	object: dict[str, Any]
	appModule: dict[str, Any]


@dataclass(frozen=True)
class AddonMetadataList:
	"""List of addon metadata."""

	scope: str
	count: int
	runningCount: int
	availableCount: int
	addons: list[dict[str, Any]]


@dataclass(frozen=True)
class HealthSnapshot:
	"""Health diagnostics snapshot."""

	timestamp: int
	options: "SnapshotOptions"
	errors: dict[str, Any]
	ok: bool
	errorCount: int
	window: Optional[dict[str, Any]] = None
	focus: Optional[dict[str, Any]] = None
	navigator: Optional[dict[str, Any]] = None
	appModule: Optional[dict[str, Any]] = None
	addons: Optional[dict[str, Any]] = None
	focusLineage: Optional[dict[str, Any]] = None
	focusTree: Optional[dict[str, Any]] = None
	logs: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class SnapshotOptions:
	"""The options a health_snapshot call was made with, echoed back.

	Typed rather than ``dict[str, Any]`` so the published schema declares the
	fields instead of degrading to a permissive object.
	"""

	include_logs: bool
	log_lines: int
	include_tree: bool
	tree_depth: int
	tree_max_children: int
	addon_scope: str


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

	Every entry carries a ``ref``. References are locators captured in this
	observation, not indices: they are resolved by re-reading the object they were
	taken from, never by scanning the tree.
	"""

	state_version: int
	revision: int
	focus: Optional[dict[str, Any]] = None
	ancestors: list[dict[str, Any]] = field(default_factory=list)
	children: list[dict[str, Any]] = field(default_factory=list)
	dialog: Optional[dict[str, Any]] = None
	window: Optional[dict[str, Any]] = None
	selection: Optional[str] = None


@dataclass(frozen=True)
class ReadResult:
	"""Text read from a scope."""

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
	actionName: str
	available: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SetValueResult:
	"""How a value was set, and whether the control agrees.

	``mode`` is ``platform`` for a UIA ``ValuePattern`` assignment, and
	``keyboard`` for focus + select-all + type. They are not equivalent, and a
	caller that needs atomicity should know which one it got.

	``verified`` is the read-back: the value was read from the control afterwards
	and matched. When it did not match, ``observed`` carries what the control
	actually contains, and the response's ``uncertain`` flag is set — a keyboard
	write can be dispatched successfully and still not reach the field.
	"""

	ref: str
	mode: str
	value: str
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
	"""Result of a keyboard dispatch."""

	gesture: str
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
	dialogName: str = ""


@dataclass(frozen=True)
class ClickAtResult:
	"""Result of a raw pointer click.

	``onTarget`` is read back from the pointer after the click: ``SendInput`` only
	says the events were accepted, and a click that landed somewhere else must not
	look successful.
	"""

	x: int
	y: int
	button: str
	hit: Optional[dict[str, Any]] = None
	cursor: list[int] = field(default_factory=list)
	onTarget: bool = True


@dataclass(frozen=True)
class WaitEventsResult:
	"""Coalesced transitions observed while waiting.

	``to_version`` is the cursor for the next call — the version of the last
	transition returned, not the journal's head. When more transitions are
	already waiting than one page holds, ``has_more`` says to poll again
	immediately with ``since = to_version``; nothing is skipped by paging.
	"""

	from_version: int
	to_version: int
	events: list[dict[str, Any]] = field(default_factory=list)
	truncated: bool = False
	has_more: bool = False
	timed_out: bool = False


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

	``location`` is ``[left, top, width, height]`` in screen pixels, the same
	shape an observed node uses; its centre is a legal ``click_at`` point.
	"""

	ref: str
	role: str
	name: str = ""
	value: str = ""
	states: list[str] = field(default_factory=list)
	location: list[int] = field(default_factory=list)
	automationId: str = ""
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
	limit: int = 20
	within: Optional[str] = None
	timeout_ms: int = 1000


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

	within: Optional[str] = None
	expect: Optional[dict[str, Any]] = None


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
	steps: int = 1
	within: Optional[str] = None
	timeout_ms: int = 1000
	expect: Optional[dict[str, Any]] = None


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
