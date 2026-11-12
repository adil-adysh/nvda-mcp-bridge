"""Paged, bounded log search over one generation of the native log."""

from __future__ import annotations

import base64
import json
import time
from collections import deque
from typing import Any, Optional

from .filters import CompiledFilter, InvalidFilterError
from .models import SCHEMA_VERSION, LogRecord
from .reader import LogReader, LogSnapshot, StaleLogError


MAX_PAGE_SIZE = 200
MAX_RESPONSE_CHARS = 600_000
CURSOR_MAX_AGE_SECONDS = 30 * 60
MAX_SCAN_RECORDS = 500_000


def encode_cursor(
	*,
	generation: str,
	position: int,
	order: str,
	filter_signature: str,
	size: int = 0,
) -> str:
	payload = {
		"v": 1,
		"g": generation,
		"p": int(position),
		"o": order,
		"f": filter_signature,
		"s": int(size),
		"t": int(time.time()),
	}
	raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
	return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> dict[str, Any]:
	if not isinstance(cursor, str) or not cursor or len(cursor) > 4096:
		raise InvalidFilterError("cursor is not a valid log cursor")
	try:
		padding = "=" * (-len(cursor) % 4)
		decoded = base64.urlsafe_b64decode((cursor + padding).encode("ascii"))
		payload = json.loads(decoded.decode("utf-8"))
	except (ValueError, UnicodeError, json.JSONDecodeError) as error:
		raise InvalidFilterError("cursor is not a valid log cursor") from error
	if not isinstance(payload, dict) or payload.get("v") != 1:
		raise InvalidFilterError("cursor is not a valid log cursor")
	created = payload.get("t")
	if not isinstance(created, int) or created > int(time.time()) + 60:
		raise InvalidFilterError("cursor is not a valid log cursor")
	if time.time() - created > CURSOR_MAX_AGE_SECONDS:
		raise StaleLogError("The log cursor expired; start a new search.", reason="cursor_expired")
	if not isinstance(payload.get("g"), str) or not isinstance(payload.get("p"), int):
		raise InvalidFilterError("cursor is not a valid log cursor")
	size_value = payload.get("s", 0)
	if payload["p"] < 0 or not isinstance(size_value, int) or size_value < 0:
		raise InvalidFilterError("cursor is not a valid log cursor")
	if payload.get("o") not in ("newest_first", "oldest_first"):
		raise InvalidFilterError("cursor is not a valid log cursor")
	if not isinstance(payload.get("f"), str):
		raise InvalidFilterError("cursor is not a valid log cursor")
	return payload


def search_logs(
	snapshot: LogSnapshot,
	filters: CompiledFilter,
	*,
	limit: int = 50,
	cursor: Optional[str] = None,
	order: str = "newest_first",
	include_raw: bool = False,
	max_scan_records: int = MAX_SCAN_RECORDS,
) -> dict[str, Any]:
	"""Search records and return the public result payload."""
	if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_PAGE_SIZE:
		raise InvalidFilterError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
	if order not in ("newest_first", "oldest_first"):
		raise InvalidFilterError("order must be 'newest_first' or 'oldest_first'")
	if not isinstance(max_scan_records, int) or isinstance(max_scan_records, bool) or not 1000 <= max_scan_records <= 2_000_000:
		raise InvalidFilterError("max_scan_records must be between 1000 and 2000000")

	signature = filters.signature()
	anchor: Optional[int] = None
	if cursor:
		decoded = decode_cursor(cursor)
		if decoded["g"] != snapshot.generation:
			raise StaleLogError(
				"The log was replaced or rotated after this cursor was issued; start a new search.",
				reason="generation_changed",
			)
		recorded_size = decoded.get("s")
		if isinstance(recorded_size, int) and snapshot.size < recorded_size:
			raise StaleLogError(
				"The log was truncated after this cursor was issued; start a new search.",
				reason="file_truncated",
			)
		if decoded["o"] != order:
			raise InvalidFilterError("cursor was issued for a different result order")
		if decoded["f"] != signature:
			raise InvalidFilterError("cursor was issued for different filters")
		anchor = decoded["p"]

	reader = LogReader(snapshot)
	selected: list[LogRecord] = []
	has_more = False
	matched_total = 0
	saw_explicit_date = False
	saw_missing_date = False

	if order == "oldest_first":
		for record in reader.iter_records(max_records=max_scan_records):
			if filters.time_mode != "none":
				if record.timestamp_date:
					saw_explicit_date = True
				elif record.timestamp_ms is not None:
					saw_missing_date = True
			if anchor is not None and record.byte_start <= anchor:
				continue
			if not filters.matches(record):
				continue
			matched_total += 1
			selected.append(record)
			if len(selected) > limit:
				has_more = True
				selected.pop()
				break
	else:
		window: deque[LogRecord] = deque(maxlen=limit + 1)
		for record in reader.iter_records(max_records=max_scan_records):
			if filters.time_mode != "none":
				if record.timestamp_date:
					saw_explicit_date = True
				elif record.timestamp_ms is not None:
					saw_missing_date = True
			if anchor is not None and record.byte_start >= anchor:
				continue
			if not filters.matches(record):
				continue
			matched_total += 1
			window.append(record)
		if len(window) > limit:
			has_more = True
			selected = list(window)[-limit:]
		else:
			selected = list(window)
		selected.reverse()

	truncated = reader.truncated_by_budget
	if not selected and truncated:
		has_more = False
	elif truncated:
		# The scan stopped before the file ended, so "no more matches" is not
		# something this call can claim.
		has_more = True
	grew = False
	try:
		grew = reader.verify_unchanged()
	except StaleLogError:
		raise

	entries = [
		record.public(include_raw=include_raw)
		for record in selected
	]
	entries, response_truncated = _fit_response(entries)
	if response_truncated:
		has_more = True
		selected = selected[: len(entries)]

	if has_more and selected:
		next_cursor = encode_cursor(
			generation=snapshot.generation,
			position=selected[-1].byte_start,
			order=order,
			filter_signature=signature,
			size=snapshot.size,
		)
	elif has_more and not selected:
		next_cursor = None
	else:
		next_cursor = None

	precision = _time_precision(filters, saw_explicit_date, saw_missing_date)
	limitations = list(filters.limitations)
	if truncated:
		limitations.append(
			f"Scan stopped after {max_scan_records} records; older records may contain more matches."
		)
	if response_truncated:
		limitations.append("The page was shortened to keep the response under its size budget.")
	if precision == "time_of_day":
		limitations.append(
			"Filtering used time-of-day only; the native log has no calendar date, so entries from another day with the same clock time cannot be distinguished."
		)
	elif precision == "mixed":
		limitations.append(
			"Some records carried an explicit date and some did not; time filtering is exact only for the former."
		)
	if grew:
		limitations.append("The log grew while it was being read; this page covers the file as it was at the start.")

	return {
		"schema_version": SCHEMA_VERSION,
		"entries": entries,
		"page": {
			"returned": len(entries),
			"has_more": bool(has_more),
			"next_cursor": next_cursor,
			"limit": limit,
			"order": order,
		},
		"source": {
			"generation": snapshot.generation,
			"size_bytes": snapshot.size,
			"source": "nvda_native_log",
		},
		"diagnostics": {
			"truncated": bool(truncated or response_truncated),
			"parse_failures": reader.parse_failures,
			"time_filter_precision": precision,
			"scan_budget_exhausted": bool(truncated),
			"response_truncated": bool(response_truncated),
			"invalid_utf8_replacements": reader.invalid_utf8_replacements,
			"records_examined": reader.records_read,
			"matched_records": matched_total,
			"limitations": limitations,
		},
	}


def _time_precision(filters: CompiledFilter, saw_explicit: bool, saw_missing: bool) -> str:
	if filters.time_mode == "none":
		return "not_applicable"
	if saw_explicit and saw_missing:
		return "mixed"
	if saw_explicit:
		return "absolute"
	return "time_of_day"


def _fit_response(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
	"""Trim a page to the response byte budget without changing its order."""
	if not entries:
		return entries, False
	encoded = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
	if len(encoded) <= MAX_RESPONSE_CHARS:
		return entries, False
	trimmed = list(entries)
	while len(trimmed) > 1:
		trimmed.pop()
		if len(json.dumps(trimmed, ensure_ascii=False, separators=(",", ":"))) <= MAX_RESPONSE_CHARS:
			return trimmed, True
	return trimmed, True
