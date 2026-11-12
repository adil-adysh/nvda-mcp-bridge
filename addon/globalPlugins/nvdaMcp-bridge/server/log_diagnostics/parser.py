"""Turn one physical NVDA log record into a structured ``LogRecord``.

The native format is a header followed by zero or more message lines::

    DEBUGWARNING - IAccessibleHandler.internalWinEventHandler._shouldGetEvents (02:03:37.589) - MainThread (35940):
    Foreground took too long to change. ...

The parser only decides facts that are visible in the text. Dates, timezones
and causal relationships are not visible, so they are reported as unknown.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable, Optional

from .exceptions import parse_exception
from .models import (
	KNOWN_LEVELS,
	MAX_MESSAGE_CHARS,
	PARSE_MALFORMED,
	PARSE_PARSED,
	PARSE_PARTIAL,
	LogRecord,
)


# The timestamp is ``(HH:MM:SS.mmm)``; NVDA has also shipped formats with a
# comma and with microseconds, so accept those without reinterpreting them.
_TIME = r"(?P<time>\d{1,2}:\d{2}:\d{2}(?:[.,]\d{1,6})?)"
_HEADER_RE = re.compile(
	r"^(?:(?P<date>\d{4}-\d{2}-\d{2})[ T])?"
	r"(?P<level>[A-Z][A-Z0-9_]{1,24})\s+-\s+"
	r"(?P<logger>.+?)\s+\(" + _TIME + r"\)\s+-\s+"
	r"(?P<thread>.*?)\((?P<thread_id>\d+)\):\s?"
	r"(?P<message>.*)$"
)
#: A header-shaped line that did not parse. Splitting it out preserves the
#: malformed record instead of silently swallowing it into the previous one.
_HEADER_HINT_RE = re.compile(r"^(?:\d{4}-\d{2}-\d{2}[ T])?[A-Z][A-Z0-9_]{1,24}\s+-\s+")

_FOREGROUND_RE = re.compile(
	r"Foreground still\s+(?P<still_handle>\d+)\s+\((?P<still_class>[^)]*)\)\."
	r"\s+Should be\s+(?P<expected_handle>\d+)\s+\((?P<expected_class>[^)]*)\)"
)
_FOCUS_RE = re.compile(r"\bfocus(?:ed|ing)?\b|\bgainFocus\b|\bforeground\b", re.IGNORECASE)
_GESTURE_RE = re.compile(r"\bgesture\s+(?P<gesture>[A-Za-z0-9_+^`~!@#$%&*()\-=\[\]{};:'\",.<>/?\\|]+)", re.IGNORECASE)
_SPEECH_RE = re.compile(
	r"\b(?P<speech_kind>speak(?:ing)?|queued|cancell?ed|interrupt(?:ed|ing)?|silence)\b",
	re.IGNORECASE,
)
_CONFIG_RE = re.compile(r"\b(?:profile|config(?:uration)?)\b", re.IGNORECASE)
_BRIDGE_RE = re.compile(r"nvda[\s_-]?mcp|NVDAHTTPBridge|bridge\.(?:action|tool|http)", re.IGNORECASE)

_ADDON_GLOBAL_PLUGIN_RE = re.compile(
	r"^(?:external:)?globalPlugins\.(?P<addon>[^.\s]+)(?:\.|$)",
	re.IGNORECASE,
)
_ADDON_APP_MODULE_RE = re.compile(
	r"^(?:external:)?appModules\.(?P<addon>[^.\s]+)(?:\.|$)",
	re.IGNORECASE,
)


def parse_header(text: str) -> Optional[dict[str, Any]]:
	"""Parse one physical line as an NVDA header, or return ``None``."""
	match = _HEADER_RE.match(text.rstrip("\r\n"))
	if not match:
		return None
	date = match.group("date")
	time_text = match.group("time")
	timestamp_ms = _time_to_ms(time_text)
	return {
		"date": date,
		"level": match.group("level"),
		"logger": match.group("logger").strip(),
		"thread_name": match.group("thread").strip(),
		"thread_id": int(match.group("thread_id")),
		"timestamp": time_text.replace(",", "."),
		"timestamp_ms": timestamp_ms,
		"first_message": match.group("message"),
	}


def looks_like_header(text: str) -> bool:
	"""Whether a physical line begins a record, valid or malformed.

	A cheap start-pattern test is used here because the reader calls this on
	every physical line; the full header regex runs once per record in
	``parse_header``.
	"""
	return bool(_HEADER_HINT_RE.match(text.rstrip("\r\n")))


def build_record(
	*,
	generation: str,
	byte_start: int,
	byte_end: int,
	line_start: int,
	line_end: int,
	lines: Iterable[str],
	notes: Optional[list[str]] = None,
	oversize: bool = False,
) -> LogRecord:
	"""Build a record from its physical lines.

	``lines`` are decoded lines including their original newlines. The first
	line is expected to be a header, but an orphan block is accepted and marked
	malformed rather than dropped.
	"""
	line_list = list(lines)
	raw = "".join(line_list)
	parse_notes = list(notes or [])
	record_notes = []
	header_text = line_list[0].rstrip("\r\n") if line_list else ""
	header = parse_header(header_text)

	if header is None:
		message = raw
		level = _guess_level(header_text)
		parse_status = PARSE_MALFORMED
		record_notes.append("header_not_parsed")
		if oversize:
			record_notes.append("record_size_limit")
			message = _head_tail(message)
		return _finish_record(
			generation=generation,
			byte_start=byte_start,
			byte_end=byte_end,
			line_start=line_start,
			line_end=line_end,
			raw=raw,
			level=level,
			logger="",
			state={
				"timestamp": None,
				"timestamp_ms": None,
				"timestamp_date": None,
				"timestamp_date_source": "unknown",
				"timestamp_absolute": None,
				"timestamp_timezone": "unknown",
				"thread_name": "",
				"thread_id": None,
				"message": message,
			},
			parse_status=parse_status,
			parse_notes=parse_notes + record_notes,
			derived_fields=["level", "subsystem", "addon", "module", "function"],
		)

	message_lines = [header["first_message"]]
	message_lines.extend(line.rstrip("\r\n") for line in line_list[1:])
	if message_lines and message_lines[0] == "" and len(message_lines) > 1:
		# The header's own message field is empty for the normal NVDA format;
		# the logical message starts on the next physical line.
		message_lines = message_lines[1:]
	if oversize:
		record_notes.append("record_size_limit")
		message = _head_tail("\n".join(message_lines))
	else:
		message = "\n".join(message_lines)
	if len(message) > MAX_MESSAGE_CHARS:
		message = message[:MAX_MESSAGE_CHARS]
		record_notes.append("message_truncated_in_parser")
	if any("invalid_utf8_replaced" in note for note in parse_notes):
		record_notes.append("invalid_utf8_replaced")

	logger = header["logger"]
	level = header["level"]
	date_source = "explicit_header" if header["date"] else "unknown"
	absolute = None
	if header["date"] and re.search(r"(?:Z|[+-]\d{2}:?\d{2})$", header["timestamp"]):
		# The native format never carries an offset. This branch exists for
		# compatible third-party loggers that do, and is only used when the
		# offset is actually present in the text.
		absolute = f"{header['date']}T{header['timestamp']}"
	state = {
		"timestamp": header["timestamp"],
		"timestamp_ms": header["timestamp_ms"],
		"timestamp_date": header["date"],
		"timestamp_date_source": date_source,
		"timestamp_absolute": absolute,
		"timestamp_timezone": "explicit" if absolute else "unknown",
		"thread_name": header["thread_name"],
		"thread_id": header["thread_id"],
		"message": message,
	}
	parse_status = PARSE_PARSED
	if header["level"] not in KNOWN_LEVELS:
		# Unknown levels are preserved exactly; this is a derived warning, not
		# a parse failure.
		record_notes.append("unknown_level_preserved")
	if oversize or len(message) > MAX_MESSAGE_CHARS:
		parse_status = PARSE_PARTIAL
	return _finish_record(
		generation=generation,
		byte_start=byte_start,
		byte_end=byte_end,
		line_start=line_start,
		line_end=line_end,
		raw=raw,
		level=level,
		logger=logger,
		state=state,
		parse_status=parse_status,
		parse_notes=parse_notes + record_notes,
		derived_fields=["module", "function", "subsystem", "addon", "exception", "enrichment"],
	)


def _finish_record(
	*,
	generation: str,
	byte_start: int,
	byte_end: int,
	line_start: int,
	line_end: int,
	raw: str,
	level: str,
	logger: str,
	state: dict[str, Any],
	parse_status: str,
	parse_notes: list[str],
	derived_fields: list[str],
) -> LogRecord:
	module, function = _module_and_function(logger)
	addon = classify_addon(logger)
	subsystem = classify_subsystem(logger, state["message"])
	exception = parse_exception(state["message"])
	enrichment = enrich_record(logger, state["message"], exception)
	entry_id = make_entry_id(generation, byte_start, raw)
	return LogRecord(
		entry_id=entry_id,
		line_start=line_start,
		line_end=line_end,
		byte_start=byte_start,
		byte_end=byte_end,
		timestamp=state["timestamp"],
		timestamp_ms=state["timestamp_ms"],
		timestamp_date=state["timestamp_date"],
		timestamp_date_source=state["timestamp_date_source"],
		timestamp_absolute=state["timestamp_absolute"],
		timestamp_timezone=state["timestamp_timezone"],
		level=level,
		logger=logger,
		module=module,
		function=function,
		subsystem=subsystem,
		addon=addon,
		thread_name=state["thread_name"],
		thread_id=state["thread_id"],
		message=state["message"],
		raw=raw,
		parse_status=parse_status,
		parse_notes=parse_notes,
		derived_fields=derived_fields,
		exception=exception,
		enrichment=enrichment,
	)


def make_entry_id(generation: str, byte_start: int, raw: str = "") -> str:
	"""Return a stable reference: generation + exact record start + content hash.

	A line number is not stable under insertion; the generation plus byte offset
	is stable under append-only growth, which is how NVDA actually writes its
	log. The short hash makes an in-place rewrite detectable rather than letting
	an old reference silently address different bytes.
	"""
	suffix = format(byte_start, "x")
	digest = hashlib.sha256(raw[:1024].encode("utf-8", "replace")).hexdigest()[:8]
	return f"{generation}:{suffix}:{digest}"


def parse_entry_id(entry_id: str) -> Optional[tuple[str, int]]:
	try:
		parts = entry_id.split(":")
		if len(parts) < 2 or not parts[0] or not parts[1]:
			return None
		return parts[0], int(parts[1], 16)
	except (ValueError, AttributeError):
		return None


def parse_entry_hash(entry_id: str) -> Optional[str]:
	parts = entry_id.split(":")
	return parts[2] if len(parts) >= 3 and parts[2] else None


def record_digest(raw: str) -> str:
	return hashlib.sha256(raw[:1024].encode("utf-8", "replace")).hexdigest()[:8]


def _time_to_ms(text: str) -> Optional[int]:
	try:
		clock = text.replace(",", ".")
		main, _, fraction = clock.partition(".")
		hours, minutes, seconds = (int(part) for part in main.split(":"))
		millis = int((fraction + "000")[:3]) if fraction else 0
		return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis
	except (ValueError, TypeError):
		return None


def _guess_level(line: str) -> str:
	match = re.match(r"^([A-Z][A-Z0-9_]{1,24})\s+-\s+", line)
	return match.group(1) if match else "UNKNOWN"


def _module_and_function(logger: str) -> tuple[str, str]:
	"""Derive module/function from the logger string without losing it."""
	cleaned = logger[9:] if logger.lower().startswith("external:") else logger
	parts = [part for part in cleaned.split(".") if part]
	if not parts:
		return "", ""
	module = parts[0]
	function = parts[-1] if len(parts) > 1 else ""
	return module, function


def classify_addon(logger: str) -> Optional[str]:
	"""Return an add-on identity only when NVDA's naming makes it unambiguous."""
	match = _ADDON_GLOBAL_PLUGIN_RE.match(logger)
	if match:
		name = match.group("addon")
		if name and name.lower() not in {"external", "globalplugins"}:
			return name
	match = _ADDON_APP_MODULE_RE.match(logger)
	if match:
		name = match.group("addon")
		if name and name.lower() not in {"external", "appmodules"}:
			return name
	return None


def classify_subsystem(logger: str, message: str) -> str:
	"""Deterministic, explainable subsystem classification.

	The original logger is always preserved; this label is a derived hint. The
	rules are ordered from the most specific source to the broadest, and a
	message-level focus signal is allowed to outrank the API layer that logged
	it because "foreground took too long" is a focus diagnostic even though
	IAccessibleHandler emitted it.
	"""
	low_logger = logger.lower()
	low_message = message.lower()
	if "nvdamcp" in low_logger.replace("-", "").replace("_", "") or "nvdahttpbridge" in low_logger or "bridge." in low_logger:
		return "bridge"
	if "comtypes" in low_logger or "_post_coinit" in low_logger or "idispatch" in low_logger:
		return "com"
	if "uiahandler" in low_logger or "uiautomation" in low_logger or "iuiautomation" in low_logger:
		return "uia"
	if _FOREGROUND_RE.search(message):
		return "focus"
	if "iaccessible" in low_logger or "oleacc" in low_logger or "msaa" in low_logger:
		return "msaa"
	if "speech" in low_logger or "synth" in low_logger or "speechviewer" in low_logger:
		return "speech"
	if "braille" in low_logger or "louis" in low_logger:
		return "braille"
	if "inputcore" in low_logger or "keyboardhandler" in low_logger or "gesture" in low_logger:
		return "input"
	if "config" in low_logger and ("profile" in low_message or "config" in low_message):
		return "configuration"
	if _FOREGROUND_RE.search(message) or (
		("focus" in low_message or "foreground" in low_message)
		and ("event" in low_logger or "focus" in low_logger or "iaccessible" in low_logger)
	):
		return "focus"
	if _CONFIG_RE.search(message) and ("config" in low_logger or "profile" in low_message):
		return "configuration"
	if any(token in low_message for token in ("took too long", "freeze", "hang", "watchdog", "timed out", "timeout")):
		return "performance"
	if "event" in low_logger and ("focus" in low_message or "foreground" in low_message):
		return "focus"
	if "addon" in low_logger or "globalplugins" in low_logger or "appmodules" in low_logger:
		return "addon"
	if any(
		token in low_logger
		for token in (
			"core.",
			"loghandler",
			"appmodulehandler",
			"globalpluginhandler",
			"extensionpoints",
			"watchdog",
			"eventhandler",
		)
	):
		return "nvda_core"
	if "accessibility" in low_logger:
		return "accessibility"
	return "unknown"


def enrich_record(
	logger: str,
	message: str,
	exception: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
	"""Extract known structured facts; never turn a fact into a conclusion."""
	low_logger = logger.casefold()
	low_message = message[:2000].casefold()
	could_match = (
		"foreground" in low_message
		or "focus" in low_message
		or "gesture" in low_message
		or "speech" in low_logger
		or "synth" in low_logger
		or "speech" in low_message
		or "bridge" in low_logger
		or "nvdahttpbridge" in low_logger
		or "trace_id=" in message
		or "traceback" in low_message
	)
	if not could_match:
		return None
	enrichment: dict[str, Any] = {}
	foreground = _FOREGROUND_RE.search(message)
	if foreground:
		enrichment["focus"] = {
			"still_handle": int(foreground.group("still_handle")),
			"still_class": foreground.group("still_class"),
			"expected_handle": int(foreground.group("expected_handle")),
			"expected_class": foreground.group("expected_class"),
			"observation": "foreground_change_delayed",
		}
	gesture = _GESTURE_RE.search(message)
	if gesture and ("inputcore" in logger.lower() or "gesture" in message.lower()):
		enrichment["input"] = {"gesture": gesture.group("gesture")}
	if "speech" in logger.lower() or "synth" in logger.lower():
		speech = _SPEECH_RE.search(message)
		enrichment["speech"] = {
			# This is what the log says was attempted or emitted by the speech
			# subsystem; it is not evidence that the user heard audio.
			"event": _speech_kind(speech.group("speech_kind")) if speech else "activity",
			"observed_in": "nvda_speech_subsystem",
			"audio_audibility": "not_observable_from_log",
		}
	if _BRIDGE_RE.search(logger) or _BRIDGE_RE.search(message):
		trace_match = re.search(r"\btrace_id=([A-Za-z0-9_-]+)", message)
		if trace_match:
			enrichment["bridge"] = {"trace_id": trace_match.group(1)}
	if exception:
		enrichment["exception_summary"] = {
			"type": exception.get("type"),
			"parse_status": exception.get("parse_status"),
		}
	return enrichment or None


def _speech_kind(value: str) -> str:
	low = value.lower()
	if low.startswith("speak"):
		return "speech_requested_or_emitted"
	if "queued" in low:
		return "speech_queued"
	if "cancel" in low:
		return "speech_cancelled"
	if "interrupt" in low:
		return "speech_interrupted"
	if "silence" in low:
		return "silence_requested"
	return "speech_activity"


def _head_tail(text: str, limit: int = 256 * 1024) -> str:
	if len(text) <= limit:
		return text
	half = limit // 2
	return f"{text[:half]}\n...<record truncated in parser>...\n{text[-half:]}"



