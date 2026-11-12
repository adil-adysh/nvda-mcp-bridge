"""Bounded, deterministic filters for native log records.

Source matching uses globs (``*``, ``?``), never regular expressions, so a
source filter cannot become a regex denial of service. Message regexes are
available but pass through a conservative complexity guard and a per-message
input limit; the guard is documented in ``docs/LOG_DIAGNOSTICS.md``.
"""

from __future__ import annotations

import fnmatch
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .models import KNOWN_LEVELS, LEVEL_RANK, LogRecord


MAX_PATTERN_CHARS = 256
MAX_MESSAGE_REGEX_CHARS = 64 * 1024
MAX_SOURCE_PATTERNS = 32
MAX_THREAD_FILTERS = 16
MAX_TIME_WINDOW_SECONDS = 7 * 24 * 3600


class InvalidFilterError(ValueError):
	"""The request's filter is malformed or unsafe."""


@dataclass
class CompiledFilter:
	"""A filter set compiled once, then applied to every record."""

	levels: frozenset[str] = frozenset()
	min_rank: Optional[int] = None
	source_include: tuple[str, ...] = ()
	source_exclude: tuple[str, ...] = ()
	addon: Optional[str] = None
	message_contains: Optional[str] = None
	message_regex: Optional[re.Pattern[str]] = None
	regex_text: Optional[str] = None
	case_sensitive: bool = False
	threads: tuple[str, ...] = ()
	trace_id: Optional[str] = None
	time_mode: str = "none"
	time_start_ms: Optional[int] = None
	time_end_ms: Optional[int] = None
	time_start_abs: Optional[datetime] = None
	time_end_abs: Optional[datetime] = None
	time_last_seconds: Optional[int] = None
	time_reference_local: Optional[datetime] = None
	limitations: list[str] = field(default_factory=list)

	def matches(self, record: LogRecord) -> bool:
		if self.levels and record.level not in self.levels:
			return False
		if self.min_rank is not None:
			rank = LEVEL_RANK.get(record.level)
			if rank is None or rank < self.min_rank:
				return False
		if self.source_include and not any(_source_matches(pattern, record.logger) for pattern in self.source_include):
			return False
		if self.source_exclude and any(_source_matches(pattern, record.logger) for pattern in self.source_exclude):
			return False
		if self.addon is not None:
			if not record.addon or record.addon.casefold() != self.addon.casefold():
				return False
		if self.threads and not _thread_matches(self.threads, record):
			return False
		if self.trace_id is not None and self.trace_id not in record.raw:
			return False
		if self.message_contains is not None:
			haystack = record.message if self.case_sensitive else record.message.casefold()
			needle = self.message_contains if self.case_sensitive else self.message_contains.casefold()
			if needle not in haystack:
				return False
		if self.message_regex is not None:
			# Bound the input a user-supplied regex can examine. Truncation can
			# only produce a false negative, never a false positive; the
			# response reports that a record was truncated.
			sample = record.message[:MAX_MESSAGE_REGEX_CHARS]
			if self.message_regex.search(sample) is None:
				return False
		if self.time_mode != "none" and not self._matches_time(record):
			return False
		return True

	def _matches_time(self, record: LogRecord) -> bool:
		# Use a real date when both the header and the filter carry one. This is
		# the only case where "2026-01-01 10:00" can be compared as a date.
		moment: Optional[datetime] = None
		if self.time_mode == "absolute" and record.timestamp_absolute:
			try:
				moment = datetime.fromisoformat(record.timestamp_absolute)
			except ValueError:
				moment = None
		elif (
			self.time_mode == "absolute"
			and record.timestamp_date
			and record.timestamp
			and (self.time_start_abs or self.time_end_abs)
		):
			try:
				moment = datetime.fromisoformat(f"{record.timestamp_date}T{record.timestamp}")
			except ValueError:
				moment = None
		if moment is not None:
			if self.time_start_abs and moment < self.time_start_abs:
				return False
			if self.time_end_abs and moment > self.time_end_abs:
				return False
			return True
		if record.timestamp_ms is None:
			return False
		if self.time_mode == "wrap":
			return (
				self.time_start_ms is not None
				and self.time_end_ms is not None
				and (record.timestamp_ms >= self.time_start_ms or record.timestamp_ms <= self.time_end_ms)
			)
		if self.time_start_ms is not None and record.timestamp_ms < self.time_start_ms:
			return False
		if self.time_end_ms is not None and record.timestamp_ms > self.time_end_ms:
			return False
		return True

	def signature(self) -> str:
		"""Canonical identity used to stop a cursor being reused with new filters."""
		payload = {
			"levels": sorted(self.levels),
			"min_rank": self.min_rank,
			"include": list(self.source_include),
			"exclude": list(self.source_exclude),
			"addon": self.addon.casefold() if self.addon else None,
			"contains": self.message_contains,
			"regex": self.regex_text,
			"case": self.case_sensitive,
			"threads": list(self.threads),
			"trace": self.trace_id,
			"time_mode": self.time_mode,
			"start": self.time_start_ms,
			"end": self.time_end_ms,
			"start_abs": self.time_start_abs.isoformat() if self.time_start_abs else None,
			"end_abs": self.time_end_abs.isoformat() if self.time_end_abs else None,
			"last": self.time_last_seconds,
		}
		return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def compile_filter(
	*,
	levels: Optional[list[str]] = None,
	min_level: Optional[str] = None,
	source_include: Optional[list[str]] = None,
	source_exclude: Optional[list[str]] = None,
	addon: Optional[str] = None,
	message_contains: Optional[str] = None,
	message_regex: Optional[str] = None,
	case_sensitive: bool = False,
	threads: Optional[list[str]] = None,
	trace_id: Optional[str] = None,
	last_seconds: Optional[int] = None,
	start: Optional[str] = None,
	end: Optional[str] = None,
	now: Optional[datetime] = None,
) -> CompiledFilter:
	compiled_levels = _compile_levels(levels)
	min_rank = _compile_min_level(min_level)
	includes = _compile_source_patterns(source_include, "sources.include")
	excludes = _compile_source_patterns(source_exclude, "sources.exclude")
	compiled_threads = _compile_threads(threads)
	needle = _optional_string(message_contains, "message.contains", max_length=1024)
	regex_text = _optional_string(message_regex, "message.regex", max_length=MAX_PATTERN_CHARS)
	regex_flags = 0 if case_sensitive else re.IGNORECASE
	message_re = compile_safe_regex(regex_text, regex_flags) if regex_text else None
	compiled_addon = _optional_string(addon, "addon", max_length=128)
	compiled_trace = _optional_string(trace_id, "trace_id", max_length=128)
	time_mode, start_ms, end_ms, start_abs, end_abs, limitations = _compile_time(last_seconds, start, end, now)
	return CompiledFilter(
		levels=compiled_levels,
		min_rank=min_rank,
		source_include=includes,
		source_exclude=excludes,
		addon=compiled_addon.casefold() if compiled_addon else None,
		message_contains=needle,
		message_regex=message_re,
		regex_text=regex_text,
		case_sensitive=bool(case_sensitive),
		threads=compiled_threads,
		trace_id=compiled_trace,
		time_mode=time_mode,
		time_start_ms=start_ms,
		time_end_ms=end_ms,
		time_start_abs=start_abs,
		time_end_abs=end_abs,
		time_last_seconds=last_seconds,
		time_reference_local=now,
		limitations=limitations,
	)


def compile_safe_regex(pattern: Optional[str], flags: int = 0) -> Optional[re.Pattern[str]]:
	"""Compile a bounded regex, rejecting patterns with catastrophic shape.

	Python's ``re`` has no timeout. A small local model can still send a
	pathological pattern by accident, so this rejects nested unbounded
	quantifiers, backreferences and enormous counted repeats before compiling.
	The message sample is additionally capped, which bounds the remaining risk
	without pretending to be a full regex engine sandbox.
	"""
	if pattern is None:
		return None
	if len(pattern) > MAX_PATTERN_CHARS:
		raise InvalidFilterError(f"message.regex is longer than {MAX_PATTERN_CHARS} characters")
	if re.search(r"\\[1-9]", pattern):
		raise InvalidFilterError("message.regex backreferences are not supported")
	# Strip escaped characters and character classes before looking for nested
	# quantifiers, so ``\\)+`` and ``[+*]+`` do not look dangerous.
	probe = _strip_regex_literals(pattern)
	dangerous = False
	for _ in range(4):
		changed = re.sub(r"\([^()]{0,200}[*+][^()]{0,200}\)\s*(?:[*+]|\{\d*,?\d*\})", "()", probe)
		if changed == probe:
			break
		dangerous = True
		probe = changed
	if dangerous:
		raise InvalidFilterError("message.regex contains a nested quantifier that could stall NVDA")
	if re.search(r"\([^()]{0,200}\|[^()]{0,200}\)\s*(?:[*+]|\{\d*,?\d*\})", probe):
		raise InvalidFilterError("message.regex contains a quantified alternation that could stall NVDA")
	for match in re.finditer(r"\{(\d+)(?:,(\d*))?\}", pattern):
		low = int(match.group(1))
		high = match.group(2)
		if low > 1000 or (high and high.isdigit() and int(high) > 1000):
			raise InvalidFilterError("message.regex repeats a group too many times")
	try:
		return re.compile(pattern, flags)
	except re.error as error:
		raise InvalidFilterError(f"message.regex is invalid: {error}") from error


def _strip_regex_literals(pattern: str) -> str:
	out = []
	index = 0
	while index < len(pattern):
		char = pattern[index]
		if char == "\\" and index + 1 < len(pattern):
			out.append("  ")
			index += 2
			continue
		if char == "[":
			end = pattern.find("]", index + 1)
			if end >= 0:
				out.append(" " * (end - index + 1))
				index = end + 1
				continue
		out.append(char)
		index += 1
	return "".join(out)


def _source_matches(pattern: str, source: str) -> bool:
	"""Match one source pattern.

	* ``speech.*`` is a prefix match on a dotted namespace, including
	  ``speech`` itself.
	* Any other value is a case-insensitive glob (``*`` and ``?``), where a
	  pattern with no wildcard is an exact match.
	* A bare prefix such as ``UIAHandler`` matches the logger and every dotted
	  descendant, which is what an agent usually means by a source name.
	"""
	normalized = pattern.strip().casefold()
	haystack = (source or "").casefold()
	if not normalized:
		return False
	if normalized.endswith(".*"):
		prefix = normalized[:-2]
		return haystack == prefix or haystack.startswith(prefix + ".")
	if any(char in normalized for char in "*?[]"):
		return fnmatch.fnmatchcase(haystack, normalized)
	return haystack == normalized or haystack.startswith(normalized + ".")


def _thread_matches(threads: tuple[str, ...], record: LogRecord) -> bool:
	for wanted in threads:
		if wanted == str(record.thread_id):
			return True
		if wanted.casefold() == (record.thread_name or "").casefold():
			return True
	return False


def _compile_levels(levels: Optional[list[str]]) -> frozenset[str]:
	if not levels:
		return frozenset()
	if not isinstance(levels, list):
		raise InvalidFilterError("levels must be a list of strings")
	if len(levels) > 16:
		raise InvalidFilterError("levels accepts at most 16 values")
	normalized = set()
	for value in levels:
		if not isinstance(value, str) or not value.strip():
			raise InvalidFilterError("levels must contain non-empty strings")
		level = value.strip().upper()
		if len(level) > 24 or not re.fullmatch(r"[A-Z][A-Z0-9_]*", level):
			raise InvalidFilterError(f"invalid log level: {value!r}")
		normalized.add(level)
	return frozenset(normalized)


def _compile_min_level(value: Optional[str]) -> Optional[int]:
	if value is None:
		return None
	if not isinstance(value, str) or not value.strip():
		raise InvalidFilterError("min_level must be a non-empty string")
	level = value.strip().upper()
	if level not in KNOWN_LEVELS:
		raise InvalidFilterError(f"min_level must be one of {', '.join(KNOWN_LEVELS)}")
	return LEVEL_RANK[level]


def _compile_source_patterns(patterns: Optional[list[str]], name: str) -> tuple[str, ...]:
	if not patterns:
		return ()
	if not isinstance(patterns, list):
		raise InvalidFilterError(f"{name} must be a list of strings")
	if len(patterns) > MAX_SOURCE_PATTERNS:
		raise InvalidFilterError(f"{name} accepts at most {MAX_SOURCE_PATTERNS} patterns")
	compiled = []
	for pattern in patterns:
		if not isinstance(pattern, str) or not pattern.strip():
			raise InvalidFilterError(f"{name} must contain non-empty strings")
		if len(pattern) > 256:
			raise InvalidFilterError(f"{name} contains a pattern longer than 256 characters")
		compiled.append(pattern.strip())
	return tuple(compiled)


def _compile_threads(threads: Optional[list[str]]) -> tuple[str, ...]:
	if not threads:
		return ()
	if not isinstance(threads, list) or len(threads) > MAX_THREAD_FILTERS:
		raise InvalidFilterError(f"threads accepts at most {MAX_THREAD_FILTERS} values")
	compiled = []
	for thread in threads:
		if isinstance(thread, int) and not isinstance(thread, bool):
			compiled.append(str(thread))
		elif isinstance(thread, str) and thread.strip():
			compiled.append(thread.strip())
		else:
			raise InvalidFilterError("threads must contain thread names or numeric thread ids")
	return tuple(compiled)


def _optional_string(value: Any, name: str, *, max_length: int) -> Optional[str]:
	if value is None:
		return None
	if not isinstance(value, str):
		raise InvalidFilterError(f"{name} must be a string")
	if len(value) > max_length:
		raise InvalidFilterError(f"{name} is longer than {max_length} characters")
	return value


def _compile_time(
	last_seconds: Optional[int],
	start: Optional[str],
	end: Optional[str],
	now: Optional[datetime],
) -> tuple[str, Optional[int], Optional[int], Optional[datetime], Optional[datetime], list[str]]:
	limitations: list[str] = []
	if last_seconds is not None:
		if isinstance(last_seconds, bool) or not isinstance(last_seconds, int):
			raise InvalidFilterError("time.last_seconds must be an integer")
		if last_seconds < 1 or last_seconds > MAX_TIME_WINDOW_SECONDS:
			raise InvalidFilterError(f"time.last_seconds must be between 1 and {MAX_TIME_WINDOW_SECONDS}")
		if start or end:
			raise InvalidFilterError("time.last_seconds cannot be combined with time.start or time.end")
		reference = now or datetime.now()
		current_ms = _clock_ms(reference)
		start_ms = (current_ms - last_seconds * 1000) % (24 * 3600 * 1000)
		limitations.append(
			"Relative time was applied to time-of-day because NVDA's native log has no calendar date. "
			"A log spanning more than 24 hours cannot be separated by this filter."
		)
		return ("wrap" if start_ms > current_ms else "time_of_day"), start_ms, current_ms, None, None, limitations

	if not start and not end:
		return "none", None, None, None, None, []

	start_abs = _parse_datetime(start, "time.start")
	end_abs = _parse_datetime(end, "time.end")
	start_time_only = _is_time_only(start)
	end_time_only = _is_time_only(end)
	if start is not None and end is not None and start_time_only != end_time_only:
		raise InvalidFilterError(
			"time.start and time.end must both be times of day or both include a date"
		)
	time_only = start_time_only and end_time_only
	if start_abs and end_abs and start_abs > end_abs and not time_only:
		raise InvalidFilterError("time.start must not be after time.end")
	if time_only and start_abs and end_abs and start_abs > end_abs:
		limitations.append("The time-of-day window crosses midnight; records on either side are included.")
		return "wrap", _clock_ms(start_abs), _clock_ms(end_abs), start_abs, end_abs, limitations
	if (start_abs and start_abs.tzinfo) or (end_abs and end_abs.tzinfo):
		limitations.append("Timezone offsets in time.start/time.end are compared only when a record carries an offset.")
	if start_abs or end_abs:
		start_ms = _clock_ms(start_abs) if start_abs else None
		end_ms = _clock_ms(end_abs) if end_abs else None
		has_calendar_date = (start is not None and not _is_time_only(start)) or (
			end is not None and not _is_time_only(end)
		)
		if has_calendar_date:
			limitations.append(
				"Absolute date filtering is used when a record carries a date; otherwise only its time-of-day is compared."
			)
		return ("absolute" if has_calendar_date else "time_of_day"), start_ms, end_ms, start_abs, end_abs, limitations
	return "none", None, None, None, None, limitations


def _is_time_only(value: Optional[str]) -> bool:
	if value is None:
		return True
	text = value.strip()
	return bool(re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?", text))


def _parse_datetime(value: Optional[str], name: str) -> Optional[datetime]:
	if value is None:
		return None
	if not isinstance(value, str) or not value.strip():
		raise InvalidFilterError(f"{name} must be a non-empty ISO date/time or HH:MM:SS string")
	text = value.strip()
	# Time of day only.
	for fmt in ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M"):
		try:
			parsed = datetime.strptime(text, fmt)
			return parsed.replace(year=1970, month=1, day=1)
		except ValueError:
			pass
	# Date or full ISO timestamp.
	try:
		parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
	except ValueError as error:
		raise InvalidFilterError(
			f"{name} must be an ISO 8601 date/time, a time of day (HH:MM:SS), or null"
		) from error
	if isinstance(parsed, datetime):
		return parsed
	return datetime.combine(parsed, datetime.min.time())


def _clock_ms(value: datetime) -> int:
	return ((value.hour * 60 + value.minute) * 60 + value.second) * 1000 + value.microsecond // 1000


def has_explicit_date(value: Optional[str]) -> bool:
	return bool(value)


def now_utc_iso() -> str:
	return datetime.now().astimezone().isoformat(timespec="seconds")


def monotonic() -> float:
	return time.monotonic()
