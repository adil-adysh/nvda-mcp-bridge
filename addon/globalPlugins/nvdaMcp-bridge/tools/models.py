"""Dataclass models for all NVDA MCP Bridge tools.

Strict input/output schemas for all 19 tools.
No implicit coercion, all fields explicit.
Zero-dependency models using stdlib dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional


# ============================================================================
# INPUT MODELS
# ============================================================================


@dataclass(frozen=True)
class EmptyInput:
	"""No input required."""

	pass


@dataclass(frozen=True)
class MoveFocusInput:
	"""Move focus to next or previous object."""

	direction: Literal["next", "previous"]


@dataclass(frozen=True)
class SpeakTextInput:
	"""Speak text using NVDA's speech system."""

	text: str


@dataclass(frozen=True)
class ShowMessageInput:
	"""Show a UI message to the user."""

	message: str


@dataclass(frozen=True)
class SendKeyboardInput:
	"""Send keyboard gesture through NVDA."""

	gesture: str
	repeat: int = 1
	delay_ms: int = 0


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
# OUTPUT MODELS
# ============================================================================


@dataclass(frozen=True)
class ObjectInfo:
	"""Information about a UI object."""

	name: str
	role: Optional[int]
	roleText: str
	value: str
	description: str
	states: str
	windowClassName: str = ""
	windowControlID: int = 0


@dataclass(frozen=True)
class WindowInfo:
	"""Information about a window."""

	title: str
	handle: int
	className: str


@dataclass(frozen=True)
class TextLineInfo:
	"""Text line information."""

	text: str
	lineNumber: Optional[int] = None
	note: str = ""
	status: str = "ok"


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
class KeyboardActionResult:
	"""Keyboard action result.

	Mirrors the dictionary returned by ``nvda_bridge.ui.send_keyboard`` exactly.
	The bridge validates responses against this schema with extra fields
	rejected, so every key the dispatch code reports must be declared here.
	"""

	gesture: str
	repeat: int
	delay_ms: int
	sent: bool
	sent_count: int = 1
	cancelled: bool = False
	completed: bool = True
	uncertain: bool = False


@dataclass(frozen=True)
class MoveFocusResult:
	"""Move focus result."""

	direction: str
	pendingFocus: bool
	newFocus: dict[str, Any]


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
	options: dict[str, Any]
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
