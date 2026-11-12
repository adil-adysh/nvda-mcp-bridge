"""Surrounding evidence for one stable log entry reference.

The anchor is addressed by byte offset, not by a line number, so the same
reference survives append-only log growth. Count-based before-context is found
by scanning backward for header lines and parsing only that window; a time
window falls back to one forward scan because a clock can cross midnight.
"""

from __future__ import annotations

import re
from collections import deque
from typing import Any, Optional

from .filters import InvalidFilterError
from .models import LogRecord
from .parser import parse_entry_hash, parse_entry_id, record_digest
from .reader import LogReader, LogSnapshot, StaleLogError


MAX_CONTEXT_SIDE = 50
MAX_TIME_WINDOW_MS = 60_000
MAX_CONTEXT_SCAN = 500_000
MAX_CONTEXT_RESPONSE_CHARS = 600_000
_DAY_MS = 24 * 3600 * 1000
_HEADER_BYTES_RE = re.compile(
	rb"(?m)^(?:\d{4}-\d{2}-\d{2}[ T])?[A-Z][A-Z0-9_]{1,24}\s+-\s+"
)
_CHUNK_BYTES = 256 * 1024


def get_log_context(
	snapshot: LogSnapshot,
	*,
	entry_id: str,
	before: int = 20,
	after: int = 30,
	include_related_threads: bool = True,
	include_raw: bool = False,
	before_ms: Optional[int] = None,
	after_ms: Optional[int] = None,
) -> dict[str, Any]:
	"""Return the chronological window around ``entry_id``.

	``before``/``after`` count records. When ``before_ms`` or ``after_ms`` is
	supplied for a side, that side is bounded by elapsed time instead; the count
	for that side is ignored. The anchor is always included.
	"""
	if not isinstance(entry_id, str) or not entry_id:
		raise InvalidFilterError("entry_id must be a non-empty log entry reference")
	if not isinstance(before, int) or isinstance(before, bool) or not 0 <= before <= MAX_CONTEXT_SIDE:
		raise InvalidFilterError(f"before must be between 0 and {MAX_CONTEXT_SIDE}")
	if not isinstance(after, int) or isinstance(after, bool) or not 0 <= after <= MAX_CONTEXT_SIDE:
		raise InvalidFilterError(f"after must be between 0 and {MAX_CONTEXT_SIDE}")
	for name, value in (("before_ms", before_ms), ("after_ms", after_ms)):
		if value is not None and (
			not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_TIME_WINDOW_MS
		):
			raise InvalidFilterError(f"{name} must be between 1 and {MAX_TIME_WINDOW_MS}")

	parsed = parse_entry_id(entry_id)
	if parsed is None:
		raise InvalidFilterError("entry_id is not a valid log entry reference")
	generation, anchor_offset = parsed
	expected_hash = parse_entry_hash(entry_id)
	if generation != snapshot.generation:
		raise StaleLogError(
			"The log was replaced or rotated after this entry was recorded; run the search again.",
			reason="generation_changed",
		)

	anchor_line = _line_number_at(snapshot.path, anchor_offset)
	anchor_reader = LogReader(snapshot)
	anchor = next(anchor_reader.iter_records(start_offset=anchor_offset, start_line=anchor_line, max_records=1), None)
	if anchor is None or anchor.byte_start != anchor_offset:
		raise StaleLogError(
			"The entry no longer exists in this log generation; run the search again.",
			reason="entry_not_found",
		)
	if expected_hash and record_digest(anchor.raw) != expected_hash:
		raise StaleLogError(
			"The log bytes at this entry reference changed since the search; run the search again.",
			reason="entry_changed",
		)

	anchor_thread = (anchor.thread_name, anchor.thread_id)
	count_mode = before_ms is None and after_ms is None
	if count_mode:
		prior_offsets = _header_offsets_before(snapshot.path, anchor_offset, before + 2) if before else []
		before_limited = len(prior_offsets) > before
		scan_start = prior_offsets[0] if prior_offsets else (0 if before else anchor_offset)
		scan_line = _line_number_at(snapshot.path, scan_start)
		scan_limit = max(1, (before + 2) + 1 + (after + 2))
	else:
		before_limited = False
		scan_start = 0
		scan_line = 1
		scan_limit = MAX_CONTEXT_SCAN

	before_records: deque[LogRecord] = deque(maxlen=MAX_CONTEXT_SIDE)
	before_eligible = 0
	after_records: list[LogRecord] = []
	after_eligible = 0
	after_limited = False
	reader = LogReader(snapshot)
	for record in reader.iter_records(start_offset=scan_start, start_line=scan_line, max_records=scan_limit):
		if record.byte_start < anchor_offset:
			if not include_related_threads and _thread_key(record) != anchor_thread:
				continue
			if before_ms is not None and not _within_before(record.timestamp_ms, anchor.timestamp_ms, before_ms):
				continue
			before_eligible += 1
			if len(before_records) == MAX_CONTEXT_SIDE:
				before_limited = True
			before_records.append(record)
			continue
		if record.byte_start == anchor_offset:
			continue
		if after == 0 and after_ms is None:
			break
		if not include_related_threads and _thread_key(record) != anchor_thread:
			continue
		if after_ms is not None and not _within_after(record.timestamp_ms, anchor.timestamp_ms, after_ms):
			continue
		after_eligible += 1
		if len(after_records) < MAX_CONTEXT_SIDE:
			after_records.append(record)
		else:
			after_limited = True
			break

	if before_ms is not None:
		before_selected = list(before_records)
		# The time mode scanned from the beginning, so truncation is the only
		# reason some matching before records are not shown.
		before_limited = before_limited or before_eligible > len(before_selected)
	else:
		before_selected = list(before_records)[-before:] if before else []
		if before:
			before_limited = before_limited or before_eligible > len(before_selected)
		else:
			before_limited = False
	if after_ms is not None:
		after_selected = after_records[:MAX_CONTEXT_SIDE]
		after_limited = after_limited or after_eligible > len(after_selected)
	else:
		after_selected = after_records[:after]
		after_limited = after_eligible > len(after_selected) if after else False

	before_selected, after_selected, before_trimmed, after_trimmed = _fit_context(
		before_selected,
		after_selected,
		anchor,
		include_raw=include_raw,
	)
	if before_trimmed:
		before_limited = True
	if after_trimmed:
		after_limited = True
	records = before_selected + [anchor] + after_selected
	entries = [record.public(include_raw=include_raw) for record in records]
	try:
		grew = reader.verify_unchanged()
	except StaleLogError:
		raise

	limitations: list[str] = []
	if not include_related_threads:
		limitations.append("Only the anchor entry's thread is included; other threads may hold related evidence.")
	else:
		limitations.append("All threads in the chronological window are included; no thread relationship is inferred.")
	if before_limited:
		limitations.append("Earlier matching records exist beyond the returned before window.")
	if after_limited:
		limitations.append("Later matching records exist beyond the returned after window.")
	if before_ms is not None or after_ms is not None:
		limitations.append(
			"Time windows use time-of-day because the native log has no calendar date; a window can wrap around midnight."
		)
	if grew:
		limitations.append("The log grew while it was read; the window reflects the file at the start of the call.")
	scan_truncated = reader.truncated_by_budget
	if scan_truncated:
		limitations.append(f"Context scan stopped after {MAX_CONTEXT_SCAN} records; the window may be incomplete.")
		if after_ms is None:
			after_limited = True
	if before_trimmed or after_trimmed:
		limitations.append("The context window was shortened to keep the response under its size budget.")

	return {
		"schema_version": 1,
		"entry_id": entry_id,
		"anchor": anchor.public(include_raw=include_raw),
		"entries": entries,
		"before": len(before_selected),
		"after": len(after_selected),
		"returned": len(entries),
		"complete_before": not before_limited,
		"complete_after": not after_limited,
		"include_related_threads": bool(include_related_threads),
		"source": {
			"generation": snapshot.generation,
			"size_bytes": snapshot.size,
			"source": "nvda_native_log",
		},
		"diagnostics": {
			"truncated": bool(before_limited or after_limited),
			"response_truncated": bool(before_trimmed or after_trimmed),
			"parse_failures": reader.parse_failures,
			"invalid_utf8_replacements": reader.invalid_utf8_replacements,
			"records_examined": reader.records_read,
			"limitations": limitations,
		},
	}


def _line_number_at(path: str, offset: int) -> int:
	"""Count physical lines before ``offset`` in bounded chunks."""
	if offset <= 0:
		return 1
	count = 0
	remaining = offset
	with open(path, "rb") as handle:
		while remaining > 0:
			chunk = handle.read(min(_CHUNK_BYTES, remaining))
			if not chunk:
				break
			count += chunk.count(b"\n")
			remaining -= len(chunk)
	return count + 1


def _header_offsets_before(path: str, anchor_offset: int, wanted: int) -> list[int]:
	"""Collect up to ``wanted`` header start offsets before the anchor.

	Only header lines are candidates; the window is parsed afterwards, so a
	false positive here cannot change a record's content. The extra preceding
	byte lets the regex distinguish a real line start from a chunk boundary.
	"""
	found: list[int] = []
	if wanted <= 0 or anchor_offset <= 0:
		return found
	end = anchor_offset
	with open(path, "rb") as handle:
		while end > 0 and len(found) < wanted:
			read_start = max(0, end - _CHUNK_BYTES)
			with_prefix = max(0, read_start - 1)
			handle.seek(with_prefix)
			data = handle.read(end - with_prefix)
			# Matches are in file order. Walk backwards from the anchor so the
			# window contains the records immediately before it, not the
			# oldest five in the chunk.
			for match in reversed(list(_HEADER_BYTES_RE.finditer(data))):
				position = with_prefix + match.start()
				if position >= anchor_offset or position in found:
					continue
				if match.start() == 0 and with_prefix > 0:
					continue
				found.append(position)
				if len(found) >= wanted:
					break
			end = read_start
	# Nearest first while scanning backwards; caller wants the earliest window.
	found.sort()
	return found


def _fit_context(
	before: list[LogRecord],
	after: list[LogRecord],
	anchor: LogRecord,
	*,
	include_raw: bool,
) -> tuple[list[LogRecord], list[LogRecord], bool, bool]:
	"""Shorten a context window without removing the anchor.

	Distance is preserved: the record after the anchor is dropped before the
	one before it, and only when the page is still over budget.
	"""
	before = list(before)
	after = list(after)
	before_trimmed = False
	after_trimmed = False

	def size() -> int:
		total = 400
		for record in before + [anchor] + after:
			total += min(len(record.message), 8000) + 500
			if include_raw:
				total += min(len(record.raw), 32000)
		return total

	while size() > MAX_CONTEXT_RESPONSE_CHARS:
		if after:
			after.pop()
			after_trimmed = True
		elif before:
			before.pop(0)
			before_trimmed = True
		else:
			break
	return before, after, before_trimmed, after_trimmed


def _thread_key(record: LogRecord) -> tuple[str, Optional[int]]:
	return record.thread_name, record.thread_id


def _within_before(record_ms: Optional[int], anchor_ms: Optional[int], window_ms: int) -> bool:
	if record_ms is None or anchor_ms is None:
		return False
	return (anchor_ms - record_ms) % _DAY_MS <= window_ms


def _within_after(record_ms: Optional[int], anchor_ms: Optional[int], window_ms: int) -> bool:
	if record_ms is None or anchor_ms is None:
		return False
	return (record_ms - anchor_ms) % _DAY_MS <= window_ms
