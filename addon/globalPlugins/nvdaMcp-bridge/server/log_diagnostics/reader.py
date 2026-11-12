"""Streaming access to NVDA's canonical native log.

Nothing here takes a path from an MCP client: the only accepted source is
``globalVars.appArgs.logFileName``, the file NVDA itself opened. Reads are
streamed and bounded, and the file is never modified.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Iterator, Optional

from .models import MAX_RECORD_BYTES, LogRecord
from .parser import build_record, looks_like_header


class LogUnavailableError(RuntimeError):
	"""The active NVDA log cannot be read."""

	protocol_code = 4205


class StaleLogError(RuntimeError):
	"""A cursor or entry reference belongs to a different log generation."""

	protocol_code = 4206

	def __init__(self, message: str, *, reason: str = "generation_changed") -> None:
		super().__init__(message)
		self.reason = reason


@dataclass(frozen=True)
class LogSnapshot:
	"""Identity of one log file generation at a point in time."""

	path: str
	generation: str
	size: int
	mtime_ns: int
	ctime_ns: int
	device: int
	inode: int
	prefix_fingerprint: str

	@classmethod
	def capture(cls, path: str) -> "LogSnapshot":
		stat = os.stat(path)
		prefix = b""
		try:
			with open(path, "rb") as handle:
				prefix = handle.read(4096)
		except OSError:
			prefix = b""
		prefix_fingerprint = hashlib.sha256(prefix[:64]).hexdigest()[:16]
		# Append-only growth leaves identity unchanged. A rotated, truncated
		# or replaced file changes its file index/creation metadata (and, if it
		# is truncated in place, the size check below catches it). The prefix is
		# kept as diagnostic metadata, not mixed into the generation: appending
		# to a file smaller than the prefix window would otherwise change it.
		identity = (
			f"{getattr(stat, 'st_dev', 0)}|{getattr(stat, 'st_ino', 0)}|"
			f"{getattr(stat, 'st_ctime_ns', 0)}"
		).encode("utf-8")
		generation = "lg-" + hashlib.sha256(identity).hexdigest()[:20]
		return cls(
			path=path,
			generation=generation,
			size=int(stat.st_size),
			mtime_ns=int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
			ctime_ns=int(getattr(stat, "st_ctime_ns", 0)),
			device=int(getattr(stat, "st_dev", 0)),
			inode=int(getattr(stat, "st_ino", 0)),
			prefix_fingerprint=prefix_fingerprint,
		)


def active_log_path() -> str:
	"""Return NVDA's canonical active log path, or raise.

	Deliberately no fallback to an arbitrary path and no environment override:
	the tool surface must not become a file reader.
	"""
	try:
		import globalVars
	except ImportError as error:  # pragma: no cover - only outside NVDA
		raise LogUnavailableError(f"NVDA's globalVars module is unavailable: {error}") from error
	args = getattr(globalVars, "appArgs", None)
	path = getattr(args, "logFileName", None) if args is not None else None
	if not isinstance(path, str) or not path.strip():
		raise LogUnavailableError("Log file is not available (logging may be disabled or not initialized)")
	path = path.strip()
	if not os.path.isfile(path):
		raise LogUnavailableError("Log file not found; NVDA may have been started with logging disabled")
	return path


def snapshot_log(path: Optional[str] = None) -> LogSnapshot:
	source = path or active_log_path()
	try:
		return LogSnapshot.capture(source)
	except FileNotFoundError as error:
		raise LogUnavailableError("Log file not found; it may have been rotated") from error
	except OSError as error:
		raise LogUnavailableError(f"Log file cannot be read: {error}") from error


class LogReader:
	"""A bounded, append-aware reader for one generation of the log."""

	def __init__(self, snapshot: LogSnapshot, *, end_offset: Optional[int] = None) -> None:
		self.snapshot = snapshot
		self.end_offset = min(snapshot.size, end_offset) if end_offset is not None else snapshot.size
		self.parse_failures = 0
		self.invalid_utf8_replacements = 0
		self.records_read = 0
		self.truncated_by_budget = False
		self.bytes_read = 0

	def verify_unchanged(self, *, allow_growth: bool = True) -> bool:
		"""Check that the path still names the same generation.

		Returns ``True`` when the file grew during the read. Raises when the
		generation changed or the file shrank, because a cursor built against it
		is no longer addressable.
		"""
		current = snapshot_log(self.snapshot.path)
		if current.generation != self.snapshot.generation:
			raise StaleLogError(
				"The log file was replaced or rotated while it was being read; restart the search.",
				reason="file_replaced",
			)
		if current.size < self.end_offset:
			raise StaleLogError(
				"The log file shrank while it was being read; the evidence reference is stale.",
				reason="file_truncated",
			)
		grew = current.size > self.snapshot.size
		if grew and not allow_growth:
			raise StaleLogError("The log file changed during the read.", reason="file_grew")
		return grew

	def iter_records(
		self,
		*,
		start_offset: int = 0,
		start_line: int = 1,
		max_records: Optional[int] = None,
	) -> Iterator[LogRecord]:
		"""Yield logical records from ``start_offset`` up to the snapshot end."""
		if self.end_offset <= 0:
			return
		start_offset = max(0, min(start_offset, self.end_offset))
		with open(self.snapshot.path, "rb") as handle:
			handle.seek(start_offset)
			buffer = _RecordBuffer()
			line_number = start_line
			offset = start_offset
			while offset < self.end_offset:
				line_start = offset
				raw_line = handle.readline(self.end_offset - offset)
				if not raw_line:
					break
				offset += len(raw_line)
				self.bytes_read += len(raw_line)
				text, replaced = _decode(raw_line)
				if replaced:
					self.invalid_utf8_replacements += 1
					buffer.notes.append("invalid_utf8_replaced")
				line_text = text.rstrip("\r\n")
				is_header = looks_like_header(line_text)
				if is_header:
					if buffer.has_content:
						record = buffer.flush(
							generation=self.snapshot.generation,
							line_end=line_number - 1,
						)
						if record is not None:
							self._count(record)
							yield record
							if max_records is not None and self.records_read >= max_records:
								self.truncated_by_budget = True
								return
					buffer.start(
						byte_start=line_start,
						line_number=line_number,
						first_line=text,
					)
				elif buffer.has_content:
					buffer.append(text)
				elif line_text.strip():
					# Orphan content before any header: preserve it as a
					# malformed record instead of discarding the file prefix.
					buffer.start(
						byte_start=line_start,
						line_number=line_number,
						first_line=text,
					)
				else:
					buffer.notes.append("leading_blank_lines_ignored")
				buffer.byte_end = offset
				buffer.line_end = line_number
				line_number += 1

			if buffer.has_content:
				record = buffer.flush(
					generation=self.snapshot.generation,
					line_end=line_number - 1,
				)
				if record is not None:
					self._count(record)
					yield record
					if max_records is not None and self.records_read >= max_records:
						self.truncated_by_budget = True

	def _count(self, record: LogRecord) -> None:
		self.records_read += 1
		if record.parse_status != "parsed":
			self.parse_failures += 1


class _RecordBuffer:
	"""Accumulates physical lines without allowing one record to grow forever."""

	def __init__(self) -> None:
		self.parts: list[str] = []
		self.notes: list[str] = []
		self.byte_start = 0
		self.byte_end = 0
		self.line_start = 0
		self.line_end = 0
		self.size = 0
		self.oversize = False

	@property
	def has_content(self) -> bool:
		return bool(self.parts) or self.oversize

	def start(self, *, byte_start: int, line_number: int, first_line: str) -> None:
		self.parts = [first_line]
		self.size = len(first_line.encode("utf-8", "replace"))
		self.byte_start = byte_start
		self.byte_end = byte_start + self.size
		self.line_start = line_number
		self.line_end = line_number
		self.oversize = False

	def append(self, text: str) -> None:
		if self.oversize:
			return
		self.size += len(text.encode("utf-8", "replace"))
		if self.size > MAX_RECORD_BYTES:
			self.oversize = True
			self.notes.append("record_size_limit")
			return
		self.parts.append(text)

	def flush(self, *, generation: str, line_end: int) -> Optional[LogRecord]:
		if not self.parts:
			return None
		raw_line_end = self.line_end if self.line_end else line_end
		record = build_record(
			generation=generation,
			byte_start=self.byte_start,
			byte_end=self.byte_end,
			line_start=self.line_start,
			line_end=max(raw_line_end, self.line_start),
			lines=self.parts,
			notes=self.notes,
			oversize=self.oversize,
		)
		self.parts = []
		self.notes = []
		self.size = 0
		self.oversize = False
		return record


def _decode(raw: bytes) -> tuple[str, bool]:
	try:
		return raw.decode("utf-8"), False
	except UnicodeDecodeError:
		return raw.decode("utf-8", errors="replace"), True
