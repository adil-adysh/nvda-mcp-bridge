"""Bounded, opt-in observation of text sent through NVDA's speech pipeline."""

from __future__ import annotations

import collections
import secrets
import threading
import time
from typing import Any, Iterable

MAX_SPEECH_TEXT_CHARS = 1024
MAX_SPEECH_EVENTS = 256
MAX_PAGE_LIMIT = 256
MAX_WAIT_MS = 30_000
WAIT_RECHECK_SECONDS = 0.25


def extract_speech_text(sequence: Iterable[Any], limit: int = MAX_SPEECH_TEXT_CHARS) -> tuple[str, bool]:
	"""Join string chunks from an NVDA speech sequence and bound the result.

	Speech command instances are deliberately ignored. The return value is the
	retained text and whether any text was omitted by the character limit.
	"""
	max_chars = max(0, min(MAX_SPEECH_TEXT_CHARS, int(limit)))
	parts: list[str] = []
	length = 0
	truncated = False
	for item in sequence:
		if not isinstance(item, str) or not item:
			continue
		remaining = max_chars - length
		if len(item) > remaining:
			if remaining > 0:
				parts.append(item[:remaining])
			length += remaining
			truncated = True
			break
		parts.append(item)
		length += len(item)
	return "".join(parts), truncated


class SpeechJournal:
	"""Thread-safe memory-only ring buffer for speech and speech controls."""

	def __init__(self, capacity: int = MAX_SPEECH_EVENTS, max_text_chars: int = MAX_SPEECH_TEXT_CHARS) -> None:
		self._capacity = max(1, min(MAX_SPEECH_EVENTS, int(capacity)))
		self._max_text_chars = max(0, min(MAX_SPEECH_TEXT_CHARS, int(max_text_chars)))
		self._events: collections.deque[dict[str, Any]] = collections.deque(maxlen=self._capacity)
		self._condition = threading.Condition()
		self._version = 0
		self._dropped = 0
		self._journal_id = secrets.token_hex(8)
		self._last_at_ms = 0
		self._reset_reason: str | None = None

	@property
	def version(self) -> int:
		with self._condition:
			return self._version

	@property
	def journal_id(self) -> str:
		with self._condition:
			return self._journal_id

	def _append(self, event: dict[str, Any]) -> int:
		with self._condition:
			self._version += 1
			at_ms = int(time.monotonic() * 1000)
			# Keep timestamps strictly ordered even when two events land in one ms.
			self._last_at_ms = max(at_ms, self._last_at_ms + 1)
			event["at_ms"] = self._last_at_ms
			event["version"] = self._version
			if len(self._events) == self._capacity:
				self._dropped += 1
			self._events.append(event)
			self._condition.notify_all()
			return self._version

	def record_speech(self, text: str, phase: str, text_truncated: bool = False) -> int | None:
		"""Record non-empty text only; returns its speech-journal version."""
		if not isinstance(text, str) or not text:
			return None
		truncated = bool(text_truncated or len(text) > self._max_text_chars)
		bounded_text = text[:self._max_text_chars]
		if not bounded_text:
			return None
		return self._append({
			"kind": "speech",
			"phase": str(phase),
			"text": bounded_text,
			"text_truncated": truncated,
		})

	def record_control(self, kind: str, paused: bool | None = None) -> int:
		"""Record a speech lifecycle control without retaining transcript text."""
		event: dict[str, Any] = {"kind": str(kind)}
		if paused is not None:
			event["paused"] = bool(paused)
		return self._append(event)

	def reset(self, reason: str = "reset") -> str:
		"""Clear retained speech and begin a new cursor generation."""
		with self._condition:
			self._events.clear()
			self._version = 0
			self._dropped = 0
			self._journal_id = secrets.token_hex(8)
			self._reset_reason = str(reason or "reset")
			self._condition.notify_all()
			return self._journal_id

	def snapshot(self, since: int = 0, limit: int = 20, journal_id: str | None = None) -> dict[str, Any]:
		"""Return an oldest-first page and explicit cursor recovery metadata."""
		requested_since = max(0, int(since))
		page_limit = max(1, min(MAX_PAGE_LIMIT, int(limit)))
		with self._condition:
			current = self._version
			current_id = self._journal_id
			events = list(self._events)
			dropped = self._dropped
			reset_reason = self._reset_reason
			oldest = events[0]["version"] if events else 0
			if journal_id is not None and journal_id != current_id:
				return self._mismatch_page(requested_since, current_id, current, oldest, dropped)

			expired = bool(oldest and requested_since + 1 < oldest)
			if requested_since > current:
				cursor_status = "future_cursor"
			elif expired:
				cursor_status = "stale"
			else:
				cursor_status = "ok"
			eligible = [event for event in events if event["version"] > requested_since]
			page = eligible[:page_limit]
			has_more = len(eligible) > len(page)
			to_version = page[-1]["version"] if page else requested_since
			payload: dict[str, Any] = {
				"journal_id": current_id,
				"from_version": requested_since,
				"to_version": to_version,
				"earliest_available_version": oldest,
				"dropped": dropped,
				"events": [dict(event) for event in page],
				"expired": expired,
				"resync_required": expired,
				"resync_reason": "cursor_expired" if expired else None,
				"resync_from": ({"journal_id": current_id, "version": oldest - 1} if expired else None),
				"history_complete": not expired,
				"truncated": expired,
				"has_more": has_more,
				"cursor_status": cursor_status,
				"timed_out": False,
				"cancelled": False,
			}
			if reset_reason is not None:
				payload["reset_reason"] = reset_reason
			return payload

	@staticmethod
	def _mismatch_page(since: int, journal_id: str, current: int, oldest: int, dropped: int) -> dict[str, Any]:
		return {
			"journal_id": journal_id,
			"from_version": since,
			"to_version": since,
			"earliest_available_version": oldest,
			"dropped": dropped,
			"events": [],
			"expired": False,
			"resync_required": True,
			"resync_reason": "journal_mismatch",
			"resync_from": {"journal_id": journal_id, "version": current},
			"history_complete": False,
			"truncated": False,
			"has_more": False,
			"cursor_status": "journal_mismatch",
			"timed_out": False,
			"cancelled": False,
		}

	def wait(
		self,
		since: int = 0,
		timeout_ms: int = 0,
		limit: int = 20,
		journal_id: str | None = None,
		cancellation_event: Any = None,
	) -> dict[str, Any]:
		"""Wait for a newer event, timeout, cancellation, or generation reset."""
		timeout = max(0, min(MAX_WAIT_MS, int(timeout_ms)))
		requested_since = max(0, int(since))
		if timeout == 0:
			return self.snapshot(requested_since, limit, journal_id)
		with self._condition:
			start_id = self._journal_id
			if journal_id is not None and journal_id != start_id:
				return self.snapshot(requested_since, limit, journal_id)
			deadline = time.monotonic() + timeout / 1000.0
			cancelled = False
			while self._version <= requested_since and self._journal_id == start_id:
				if cancellation_event is not None and cancellation_event.is_set():
					cancelled = True
					break
				remaining = deadline - time.monotonic()
				if remaining <= 0:
					break
				self._condition.wait(timeout=min(remaining, WAIT_RECHECK_SECONDS))
			reset = self._journal_id != start_id
			current = self._version
		payload = self.snapshot(requested_since, limit, start_id if reset or journal_id is None else journal_id)
		payload["cancelled"] = cancelled
		payload["timed_out"] = not cancelled and not reset and current <= requested_since and not payload["resync_required"]
		return payload
