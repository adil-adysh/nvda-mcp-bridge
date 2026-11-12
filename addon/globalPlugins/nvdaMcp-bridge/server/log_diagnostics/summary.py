"""Deterministic, bounded statistics over native NVDA log records.

No language model runs inside NVDA. Every number here is a count, a normalized
pattern, or a labelled anomaly candidate; the summary never says that one
record caused another.
"""

from __future__ import annotations

import math
import re
import time
from collections import Counter
from typing import Any, Optional

from .filters import CompiledFilter, InvalidFilterError
from .models import WARNING_LEVELS, LogRecord
from .reader import LogReader, LogSnapshot
from .redaction import redact_text


MAX_GROUPS = 5000
MAX_BUCKETS = 1500
MAX_SCAN_RECORDS = 500_000
#: A summary is bounded in time as well as records. The value leaves room for
#: the HTTP transport's 15 s default while keeping the worker responsive.
MAX_SCAN_SECONDS = 12.0
_ALLOWED_SECTIONS = {
	"counts",
	"repeated_patterns",
	"exceptions",
	"warnings",
	"top_sources",
	"time_buckets",
	"noise",
}
_ALLOWED_GROUPS = {"source", "level", "message_pattern", "subsystem", "thread", "addon"}


def summarize_logs(
	snapshot: LogSnapshot,
	filters: CompiledFilter,
	*,
	group_by: Optional[list[str]] = None,
	include: Optional[list[str]] = None,
	limit: int = 20,
	max_scan_records: int = MAX_SCAN_RECORDS,
) -> dict[str, Any]:
	if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
		raise InvalidFilterError("limit must be between 1 and 50")
	if max_scan_records < 1000 or max_scan_records > 2_000_000:
		raise InvalidFilterError("max_scan_records must be between 1000 and 2000000")
	groups = _validate_values(group_by, _ALLOWED_GROUPS, "group_by")
	sections = _validate_values(include, _ALLOWED_SECTIONS, "include")
	if not sections:
		sections = sorted(_ALLOWED_SECTIONS)

	started = time.perf_counter()
	reader = LogReader(snapshot)
	total = 0
	severity: Counter[str] = Counter()
	sources: Counter[str] = Counter()
	levels_by_source: dict[str, Counter[str]] = {}
	patterns: dict[str, dict[str, Any]] = {}
	exceptions: dict[tuple[str, str, str, str], dict[str, Any]] = {}
	warnings: dict[tuple[str, str, str], dict[str, Any]] = {}
	buckets: Counter[int] = Counter()
	timestamps: list[int] = []
	pattern_overflow = 0
	exception_overflow = 0
	warning_overflow = 0
	bucket_overflow = 0
	source_overflow = 0
	noise_records = 0
	noise_sources: Counter[str] = Counter()
	uia_noise = 0
	saw_explicit_date = False
	saw_missing_date = False
	first_timestamp: Optional[str] = None
	last_timestamp: Optional[str] = None

	time_budget_exhausted = False
	for examined, record in enumerate(reader.iter_records(max_records=max_scan_records)):
		if examined % 512 == 0 and time.perf_counter() - started > MAX_SCAN_SECONDS:
			time_budget_exhausted = True
			break
		if filters.time_mode != "none":
			if record.timestamp_date:
				saw_explicit_date = True
			elif record.timestamp_ms is not None:
				saw_missing_date = True
		if not filters.matches(record):
			continue
		total += 1
		severity[record.level] += 1
		if record.logger in sources or len(sources) < MAX_GROUPS:
			sources[record.logger] += 1
			levels_by_source.setdefault(record.logger, Counter())[record.level] += 1
		else:
			source_overflow += 1
		if record.timestamp:
			first_timestamp = first_timestamp or record.timestamp
			last_timestamp = record.timestamp
		if record.timestamp_ms is not None:
			timestamps.append(record.timestamp_ms)
		noisy = _is_noisy(record)
		if noisy:
			noise_records += 1
			noise_sources[record.logger] += 1
		if _is_uia(record):
			uia_noise += 1

		if "repeated_patterns" in sections:
			key = normalize_message(record.message)
			group = patterns.get(key)
			if group is None:
				if len(patterns) >= MAX_GROUPS:
					pattern_overflow += 1
				else:
					patterns[key] = _new_group(record, key)
			else:
				_update_group(group, record)
		if "exceptions" in sections and record.exception:
			exception = record.exception
			exc_type = str(exception.get("type") or "unparsed_traceback")
			top_frame = _top_frame(exception)
			key = (
				exc_type,
				record.module or record.logger,
				top_frame,
				normalize_message(str(exception.get("message") or record.message)),
			)
			group = exceptions.get(key)
			if group is None:
				if len(exceptions) >= MAX_GROUPS:
					exception_overflow += 1
				else:
					exceptions[key] = _new_exception_group(record, exception, top_frame)
			else:
				_update_exception_group(group, record)
		if "warnings" in sections and record.level in WARNING_LEVELS:
			key = (record.logger, record.level, normalize_message(record.message))
			group = warnings.get(key)
			if group is None:
				if len(warnings) >= MAX_GROUPS:
					warning_overflow += 1
				else:
					warnings[key] = _new_warning_group(record)
			else:
				_update_group(group, record)

	if "time_buckets" in sections and timestamps:
		bucket_ms = _choose_bucket_ms(min(timestamps), max(timestamps))
		for moment in timestamps:
			if len(buckets) >= MAX_BUCKETS and moment // bucket_ms not in buckets:
				bucket_overflow += 1
				continue
			buckets[(moment // bucket_ms) * bucket_ms] += 1

	truncated = reader.truncated_by_budget or time_budget_exhausted
	try:
		grew = reader.verify_unchanged()
	except Exception:
		grew = False

	elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
	summary: dict[str, Any] = {
		"total_records": total,
		"severity_counts": dict(sorted(severity.items(), key=lambda item: (-item[1], item[0]))),
	}
	if "top_sources" in sections:
		summary["top_sources"] = _top_sources(sources, levels_by_source, total, limit)
	if "repeated_patterns" in sections:
		summary["repeated_patterns"] = _top_patterns(patterns, total, limit, pattern_overflow)
	if "exceptions" in sections:
		summary["exceptions"] = _top_exceptions(exceptions, total, limit, exception_overflow)
	if "warnings" in sections:
		summary["warnings"] = _top_warnings(warnings, total, limit, warning_overflow)
	if "time_buckets" in sections:
		summary["time_buckets"] = _bucket_payload(buckets, bucket_overflow, limit)
	if "noise" in sections:
		summary["noise"] = {
			"com_records": noise_records,
			"com_share": round(noise_records / total, 4) if total else 0.0,
			"sources": [
				{"source": source, "count": count}
				for source, count in sorted(noise_sources.items(), key=lambda item: (-item[1], item[0]))[:limit]
			],
			"uia_records": uia_noise,
			"note": "Noisy records are counted, never removed; a high COM release count is not by itself an error.",
		}

	precision = _precision(filters, saw_explicit_date, saw_missing_date)
	limitations = list(filters.limitations)
	if reader.truncated_by_budget:
		limitations.append(f"Scan stopped after {max_scan_records} records; totals are lower bounds.")
	if time_budget_exhausted:
		limitations.append(
			f"Scan stopped after the {MAX_SCAN_SECONDS:g} s summary budget; totals are lower bounds."
		)
	if grew:
		limitations.append("The log grew while it was summarized; totals cover the file at the start of the call.")
	if precision == "time_of_day":
		limitations.append(
			"Time filtering used time-of-day only; the native log has no calendar date."
		)
	elif precision == "mixed":
		limitations.append("Only some records carry an explicit date; time filtering is exact only for those.")
	if first_timestamp and last_timestamp:
		limitations.append(
			f"Observed time-of-day range is {first_timestamp} to {last_timestamp}; a midnight rollover is not resolved."
		)
	for label, overflow in (
		("message patterns", pattern_overflow),
		("exception groups", exception_overflow),
		("warning groups", warning_overflow),
		("time buckets", bucket_overflow),
		("log sources", source_overflow),
	):
		if overflow:
			limitations.append(f"{overflow} records fell outside the bounded {label} group table.")

	return {
		"schema_version": 1,
		"summary": summary,
		"coverage": {
			"records_examined": reader.records_read,
			"records_matched": total,
			"complete": not truncated,
			"time_budget_exhausted": time_budget_exhausted,
			"parse_failures": reader.parse_failures,
			"invalid_utf8_replacements": reader.invalid_utf8_replacements,
			"time_filter_precision": precision,
			"source": "nvda_native_log",
			"generation": snapshot.generation,
			"size_bytes": snapshot.size,
			"scan_ms": elapsed_ms,
			"group_by": groups,
			"limitations": limitations,
		},
	}


_VOLATILE_HINT_RE = re.compile(
	r"0x|<POINTER|\b\d{5,}\b|\b\d{2}:\d{2}:\d{2}|\b\d{4}-\d{2}-\d{2}|"
	r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b|"
	r"\b(?:process|thread|pid|tid|hwnd|handle|window|foreground|still|expected)\b",
	re.IGNORECASE,
)
_POINTER_RE = re.compile(r"<POINTER\([^>]*\)\s+ptr=0x[0-9A-Fa-f]+\s+at\s+[0-9A-Fa-f]+>")
_OBJECT_RE = re.compile(r"<[^>\n]{0,80} object at 0x[0-9A-Fa-f]+>")
_HEX_RE = re.compile(r"0x[0-9A-Fa-f]+")
_UUID_RE = re.compile(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b")
_TIMESTAMP_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b")
_TIME_RE = re.compile(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b")
_KEYED_ID_RE = re.compile(
	r"\b(process|thread|pid|tid|hwnd|handle|window|foreground|still|expected)\b"
	r"(\s*(?:id|handle)?\s*[=:#]?\s*)\d+\b",
	re.IGNORECASE,
)
_LONG_NUMBER_RE = re.compile(r"\b\d{5,}\b")
_SPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINE_RE = re.compile(r"\n{2,}")


def normalize_message(message: str) -> str:
	"""Normalize volatile identifiers without erasing the diagnostic shape."""
	text = redact_text(message).replace("\r\n", "\n").strip()
	# COM pointer-release diagnostics dominate real logs. Handle their exact
	# shapes before the general regex path; this is the same normalization, not
	# a different one, and it removes most of the per-record cost.
	if text.startswith("Release <POINTER("):
		return "Release <com-pointer>"
	if text.startswith("wrap_outparam(<POINTER("):
		return "wrap_outparam(<com-pointer>)"
	if text.startswith("GetBestInterface(<POINTER("):
		return "GetBestInterface(<com-pointer>)"
	if text.startswith("IDispatch.GetTypeInfo("):
		return "IDispatch.GetTypeInfo failed: <object>"
	if text.startswith("Does NOT implement "):
		return text
	if len(text) <= 500 and not _VOLATILE_HINT_RE.search(text):
		return text or "<empty message>"
	text = _POINTER_RE.sub("<com-pointer>", text)
	text = _OBJECT_RE.sub("<object>", text)
	text = _HEX_RE.sub("<hex>", text)
	text = _UUID_RE.sub("<uuid>", text)
	text = _TIMESTAMP_RE.sub("<timestamp>", text)
	text = _TIME_RE.sub("<time>", text)
	text = _KEYED_ID_RE.sub(r"\1\2<number>", text)
	text = _LONG_NUMBER_RE.sub("<number>", text)
	text = _SPACE_RE.sub(" ", text)
	text = _BLANK_LINE_RE.sub("\n", text).strip()
	if len(text) > 500:
		text = text[:500] + "…"
	return text or "<empty message>"


def _validate_values(values: Optional[list[str]], allowed: set[str], name: str) -> list[str]:
	if values is None:
		return []
	if not isinstance(values, list):
		raise InvalidFilterError(f"{name} must be a list of strings")
	if len(values) > 12:
		raise InvalidFilterError(f"{name} accepts at most 12 values")
	result = []
	for value in values:
		if not isinstance(value, str) or not value:
			raise InvalidFilterError(f"{name} must contain non-empty strings")
		if value not in allowed:
			raise InvalidFilterError(f"{name} contains unsupported value {value!r}")
		if value not in result:
			result.append(value)
	return result


def _new_group(record: LogRecord, pattern: str) -> dict[str, Any]:
	return {
		"pattern": pattern,
		"count": 1,
		"example": _short(record.message),
		"example_entry_id": record.entry_id,
		"level": record.level,
		"source": record.logger,
		"first_timestamp": record.timestamp,
		"last_timestamp": record.timestamp,
		"entry_ids": [record.entry_id],
	}


def _update_group(group: dict[str, Any], record: LogRecord) -> None:
	group["count"] += 1
	group["last_timestamp"] = record.timestamp
	if len(group["entry_ids"]) < 3:
		group["entry_ids"].append(record.entry_id)


def _new_exception_group(record: LogRecord, exception: dict[str, Any], top_frame: str) -> dict[str, Any]:
	return {
		"type": exception.get("type") or "unparsed_traceback",
		"source": record.module or record.logger,
		"logger": record.logger,
		"top_frame": top_frame,
		"message_pattern": normalize_message(str(exception.get("message") or record.message)),
		"parse_status": exception.get("parse_status"),
		"count": 1,
		"example": _short(str(exception.get("message") or record.message)),
		"example_entry_id": record.entry_id,
		"entry_ids": [record.entry_id],
	}


def _update_exception_group(group: dict[str, Any], record: LogRecord) -> None:
	group["count"] += 1
	if group.get("parse_status") != "partial" and record.exception and record.exception.get("parse_status") == "partial":
		group["parse_status"] = "partial"
	if len(group["entry_ids"]) < 3:
		group["entry_ids"].append(record.entry_id)


def _new_warning_group(record: LogRecord) -> dict[str, Any]:
	return {
		"pattern": normalize_message(record.message),
		"level": record.level,
		"source": record.logger,
		"count": 1,
		"example": _short(record.message),
		"example_entry_id": record.entry_id,
		"entry_ids": [record.entry_id],
	}


def _top_sources(
	sources: Counter[str],
	levels: dict[str, Counter[str]],
	total: int,
	limit: int,
) -> list[dict[str, Any]]:
	rows = []
	for source, count in sorted(sources.items(), key=lambda item: (-item[1], item[0]))[:limit]:
		rows.append(
			{
				"source": source,
				"count": count,
				"share": round(count / total, 4) if total else 0.0,
				"levels": dict(sorted(levels.get(source, Counter()).items(), key=lambda item: (-item[1], item[0]))),
			}
		)
	return rows


def _top_patterns(
	patterns: dict[str, dict[str, Any]],
	total: int,
	limit: int,
	overflow: int,
) -> list[dict[str, Any]]:
	rows = []
	for group in sorted(patterns.values(), key=lambda item: (-item["count"], item["pattern"]))[:limit]:
		row = dict(group)
		row["share"] = round(group["count"] / total, 4) if total else 0.0
		rows.append(row)
	if overflow:
		rows.append({"pattern": "<other normalized patterns>", "count": overflow, "overflow": True})
	return rows


def _top_exceptions(
	groups: dict[tuple[str, str, str, str], dict[str, Any]],
	total: int,
	limit: int,
	overflow: int,
) -> list[dict[str, Any]]:
	rows = []
	for group in sorted(groups.values(), key=lambda item: (-item["count"], item["type"], item["source"]))[:limit]:
		row = dict(group)
		row["share"] = round(group["count"] / total, 4) if total else 0.0
		rows.append(row)
	if overflow:
		rows.append({"type": "<other exception groups>", "count": overflow, "overflow": True})
	return rows


def _top_warnings(
	groups: dict[tuple[str, str, str], dict[str, Any]],
	total: int,
	limit: int,
	overflow: int,
) -> list[dict[str, Any]]:
	rows = []
	for group in sorted(groups.values(), key=lambda item: (-item["count"], item["pattern"]))[:limit]:
		row = dict(group)
		row["share"] = round(group["count"] / total, 4) if total else 0.0
		rows.append(row)
	if overflow:
		rows.append({"pattern": "<other warning groups>", "count": overflow, "overflow": True})
	return rows


def _bucket_payload(buckets: Counter[int], overflow: int, limit: int) -> list[dict[str, Any]]:
	if not buckets:
		return []
	counts = list(buckets.values())
	mean = sum(counts) / len(counts)
	variance = sum((count - mean) ** 2 for count in counts) / len(counts)
	stddev = math.sqrt(variance)
	threshold = mean + 2 * stddev
	rows = []
	for bucket, count in buckets.items():
		rows.append(
			{
				"start": _clock_label(bucket),
				"start_ms": bucket,
				"count": count,
				"anomaly_candidate": bool(count >= 5 and count > threshold),
			}
		)
	rows.sort(key=lambda item: (-item["count"], item["start_ms"]))
	rows = rows[:limit]
	if overflow:
		rows.append({"start": "<other buckets>", "start_ms": 0, "count": overflow, "overflow": True})
	return rows


def _choose_bucket_ms(first: int, last: int) -> int:
	span = max(0, last - first)
	for step_seconds in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600):
		if span / (step_seconds * 1000) <= 90:
			return step_seconds * 1000
	return 3600 * 1000


def _clock_label(timestamp_ms: int) -> str:
	seconds = (timestamp_ms // 1000) % (24 * 3600)
	return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def _top_frame(exception: dict[str, Any]) -> str:
	frames = exception.get("frames") or []
	if not frames:
		return ""
	frame = frames[-1]
	return f"{frame.get('file') or ''}:{frame.get('line') or 0} {frame.get('function') or ''}".strip()


def _is_noisy(record: LogRecord) -> bool:
	low = record.logger.casefold()
	if "comtypes" in low or "_post_coinit" in low:
		return True
	return bool(re.search(r"^(?:Release|wrap_outparam|GetBestInterface|Does NOT implement)\b", record.message))


def _is_uia(record: LogRecord) -> bool:
	if "uia" in record.logger.casefold():
		return True
	return "IUIAutomation" in record.message or "UIAutomation" in record.message


def _short(value: str, limit: int = 600) -> str:
	value = redact_text(value)
	return value if len(value) <= limit else value[:limit] + "…"


def _precision(filters: CompiledFilter, has_date: bool, missing_date: bool) -> str:
	if filters.time_mode == "none":
		return "not_applicable"
	if has_date and missing_date:
		return "mixed"
	if has_date:
		return "absolute"
	return "time_of_day"
