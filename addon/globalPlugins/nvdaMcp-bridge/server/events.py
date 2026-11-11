"""Bounded, versioned journal of NVDA UI events.

Why this exists: every higher-level way of telling a client "the UI changed"
needs the same three things — a monotonic version, a cheap "wait until something
happens", and a bounded history to read from a cursor. Doing it once here keeps
that logic out of the tool handlers, out of the HTTP layer and out of the MCP
companion.

Cost on the main thread is one deque append plus a condition notify per event, so
it is safe to record from NVDA's event chain. Nothing here may block.
"""

from __future__ import annotations

import collections
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

#: Events worth telling a client about. Deliberately coarse: a client wants to
#: know "the UI changed and here is what the focus is now", not each raw UIA
#: property change.
JOURNAL_KINDS = ("focus", "name", "value", "state", "show", "foreground")

#: How much history to keep. A client that falls further behind than this must
#: re-observe instead of replaying, which is why `truncated` is reported.
DEFAULT_CAPACITY = 512


class EventJournal:
	"""Thread-safe ring buffer of UI events with a waitable version counter."""

	def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
		self._condition = threading.Condition()
		self._events: collections.deque[Dict[str, Any]] = collections.deque(maxlen=capacity)
		self._version = 0
		self._dropped = 0
		self._capacity = capacity
		#: Set false to skip recording entirely (idle cost: one attribute read).
		self.enabled = True

	def record(self, kind: str, obj: Any = None, **extra: Any) -> Optional[int]:
		"""Append an event and wake any waiter. Never raises."""
		if not self.enabled:
			return None
		try:
			entry: Dict[str, Any] = {"kind": kind, "at_ms": int(time.monotonic() * 1000)}
			if obj is not None:
				entry["role"] = _safe_int(getattr(obj, "role", None))
				entry["name"] = _safe_str(getattr(obj, "name", None))
			entry.update(extra)
		except Exception:  # noqa: BLE001 — a journal must never break an event chain
			entry = {"kind": kind, "at_ms": int(time.monotonic() * 1000)}

		with self._condition:
			self._version += 1
			entry["version"] = self._version
			if len(self._events) == self._capacity:
				self._dropped += 1
			self._events.append(entry)
			self._condition.notify_all()
			return self._version

	@property
	def version(self) -> int:
		"""Current sequence number; 0 means nothing has been recorded."""
		with self._condition:
			return self._version

	def snapshot(
		self,
		since: int = 0,
		limit: int = 100,
		kinds: Optional[Iterable[str]] = None,
	) -> Dict[str, Any]:
		"""Return events after ``since``, plus whether history was lost."""
		wanted = {kind for kind in (kinds or ()) if kind}
		with self._condition:
			current = self._version
			events: List[Dict[str, Any]] = [
				event
				for event in self._events
				if event["version"] > since and (not wanted or event["kind"] in wanted)
			]
			oldest = self._events[0]["version"] if self._events else current + 1
		truncated = bool(since) and since + 1 < oldest
		return {
			"from_version": current,
			"to_version": current,
			"state_version": current,
			"truncated": truncated,
			"dropped": self._dropped,
			"events": events[-limit:] if limit > 0 else events,
		}

	def wait(
		self,
		since: int = 0,
		timeout_ms: int = 0,
		limit: int = 100,
		kinds: Optional[Iterable[str]] = None,
	) -> Dict[str, Any]:
		"""Block until the version advances past ``since`` or the timeout expires.

		``timeout_ms == 0`` returns immediately, which makes this the same call as
		:meth:`snapshot`. Blocking happens on the caller's (HTTP worker) thread;
		never call this from NVDA's main thread.
		"""
		if timeout_ms <= 0:
			return self.snapshot(since=since, limit=limit, kinds=kinds)

		deadline = time.monotonic() + (timeout_ms / 1000.0)
		with self._condition:
			while self._version <= since:
				remaining = deadline - time.monotonic()
				if remaining <= 0:
					break
				self._condition.wait(timeout=remaining)
		return self.snapshot(since=since, limit=limit, kinds=kinds)


def _safe_int(value: Any) -> Optional[int]:
	try:
		return int(value) if value is not None else None
	except Exception:  # noqa: BLE001
		return None


def _safe_str(value: Any) -> str:
	try:
		return str(value) if value is not None else ""
	except Exception:  # noqa: BLE001
		return ""
