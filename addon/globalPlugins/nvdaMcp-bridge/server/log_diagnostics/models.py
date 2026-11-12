"""Internal record model for the native NVDA log.

The public MCP models live in ``tools.models``; this module is the parser's own
representation and deliberately has no NVDA imports, so it can be exercised
without a running NVDA.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .redaction import redact_text, redact_value


SCHEMA_VERSION = 1

#: Severity ordering used by ``min_level`` and summaries. NVDA's own logging
#: levels are DEBUG < IO < DEBUGWARNING < INFO < WARNING < ERROR < CRITICAL.
#: DEBUGWARNING is not folded into DEBUG: NVDA emits it when a debug-level
#: subsystem detects a condition worth surfacing (a slow foreground change, for
#: example), and treating it as noise loses precisely the signal being sought.
LEVEL_RANK = {
	"DEBUG": 10,
	"IO": 12,
	"DEBUGWARNING": 15,
	"INFO": 20,
	"WARNING": 30,
	"ERROR": 40,
	"CRITICAL": 50,
}

KNOWN_LEVELS = tuple(LEVEL_RANK)
WARNING_LEVELS = frozenset({"DEBUGWARNING", "WARNING"})
ERROR_LEVELS = frozenset({"ERROR", "CRITICAL"})

#: Parse statuses. ``parsed`` means every header field was read directly.
PARSE_PARSED = "parsed"
PARSE_MALFORMED = "malformed"
PARSE_PARTIAL = "partial"

#: Bounds. They are deliberately far above real NVDA messages (the largest in
#: the shipped fixture is a few kilobytes) but low enough that a corrupt or
#: malicious file cannot exhaust NVDA's memory.
MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_MESSAGE_CHARS = 256 * 1024


@dataclass
class LogRecord:
	"""One logical NVDA log record, including every continuation line."""

	entry_id: str
	line_start: int
	line_end: int
	byte_start: int
	byte_end: int
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
	thread_name: str
	thread_id: Optional[int]
	message: str
	raw: str
	parse_status: str
	parse_notes: list[str] = field(default_factory=list)
	derived_fields: list[str] = field(default_factory=list)
	exception: Optional[dict[str, Any]] = None
	enrichment: Optional[dict[str, Any]] = None
	header_parsed: bool = False

	def public(self, *, include_raw: bool = False, max_message_chars: int = 8000, max_raw_chars: int = 32000) -> dict[str, Any]:
		"""Return the compact transport shape used by the MCP tools.

		The raw record is optional because it is the largest and most sensitive
		part of the evidence. When omitted, the exact bytes remain recoverable
		from the log by ``entry_id`` for as long as the file generation is
		unchanged.
		"""
		message, message_truncated = _bounded_text(redact_text(self.message), max_message_chars)
		notes = list(self.parse_notes)
		if message_truncated:
			notes.append("message_truncated_in_response")
		payload: dict[str, Any] = {
			"entry_id": self.entry_id,
			"timestamp": self.timestamp,
			"timestamp_ms": self.timestamp_ms,
			"timestamp_date": self.timestamp_date,
			"timestamp_date_source": self.timestamp_date_source,
			"timestamp_absolute": self.timestamp_absolute,
			"timestamp_timezone": self.timestamp_timezone,
			"level": self.level,
			"logger": self.logger,
			"module": self.module,
			"function": self.function,
			"subsystem": self.subsystem,
			"addon": self.addon,
			"thread": {"name": self.thread_name, "id": self.thread_id},
			"message": message,
			"line_start": self.line_start,
			"line_end": self.line_end,
			"parse_status": self.parse_status,
			"parse_notes": notes,
			"derived_fields": list(self.derived_fields),
			"exception": redact_value(self.exception) if self.exception else None,
			"enrichment": redact_value(self.enrichment) if self.enrichment else None,
		}
		if include_raw:
			raw, raw_truncated = _bounded_text(redact_text(self.raw), max_raw_chars)
			payload["raw"] = raw
			if raw_truncated:
				payload["parse_notes"].append("raw_truncated_in_response")
		return payload


def _bounded_text(value: str, limit: int) -> tuple[str, bool]:
	if limit <= 0 or len(value) <= limit:
		return value, False
	return value[:limit], True
