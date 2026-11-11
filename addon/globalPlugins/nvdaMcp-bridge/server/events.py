"""Bounded, versioned journals of NVDA UI activity.

Why this exists: every higher-level way of telling a client "the UI changed"
needs the same three things — a monotonic version, a cheap "wait until something
happens", and a bounded history to read from a cursor. Doing it once here keeps
that logic out of the tool handlers, out of the HTTP layer and out of the MCP
companion.

Two journals, because events and changes are not the same thing:

``transitions``
	The default. A coalesced record of "something happened": one entry per burst
	of events, written after the burst goes quiet. Typing five characters into
	Notepad produced 29 events but one visible change, so this is the surface a
	client should read.
``raw``
	One entry per dispatched event, behind a debug flag. Useful when the
	question is "which events arrived and in what order"; useless as a change
	feed, and off by default because it costs an attribute read per event.

Cost on the main thread is one dict build plus a condition notify per event, so
it is safe to record from NVDA's event chain. Nothing here may block.
"""

from __future__ import annotations

import collections
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

#: Event kinds worth recording (`docs/IMPLEMENTATION_PLAN.md` §3.5). Chosen
#: because they carry the transitions an agent needs. `show`/`hide` are omitted:
#: `shouldAcceptEvent` drops them except for notification bars, tooltips and IME
#: candidates, so as a "UI appeared" signal they are a trap.
JOURNAL_KINDS = (
	"gainFocus",
	"loseFocus",
	"foreground",
	"focusEntered",
	"focusExited",
	"nameChange",
	"valueChange",
	"stateChange",
	"caret",
	"selection",
	"liveRegionChange",
	"alert",
	"menuStart",
	"menuEnd",
	"documentLoadComplete",
	"typedCharacter",
	"UIA_notification",
	"textChange",
)

#: Raw events for an old burst are rarely useful; keeping them bounded keeps a
#: long debugging session from growing without limit.
DEFAULT_CAPACITY = 256
RAW_CAPACITY = 512

#: How long a burst of events has to be quiet before it becomes one transition.
#: 40 ms is above the inter-event gap of a single keystroke and far below the
#: 117-182 ms a focus change takes to settle, so a burst is coalesced without
#: merging two different user actions.
QUIET_MS = 40

#: Order in which a burst's event names are summarised into one transition kind.
#: Structural events win over property changes: "focus moved" is the useful
#: headline even when the same burst also carried `nameChange`.
_KIND_PRIORITY = (
	"gainFocus",
	"foreground",
	"loseFocus",
	"focusEntered",
	"focusExited",
	"menuStart",
	"menuEnd",
	"alert",
	"liveRegionChange",
	"documentLoadComplete",
	"UIA_notification",
	"valueChange",
	"stateChange",
	"nameChange",
	"selection",
	"caret",
	"typedCharacter",
	"textChange",
)

#: Coarse transition kinds a caller can filter on, mapped from the NVDA names.
_COARSE_KIND = {
	"gainFocus": "focus",
	"loseFocus": "focus",
	"foreground": "focus",
	"focusEntered": "focus",
	"focusExited": "focus",
	"nameChange": "name",
	"valueChange": "value",
	"stateChange": "state",
	"caret": "caret",
	"selection": "selection",
	"liveRegionChange": "liveRegion",
	"alert": "alert",
	"menuStart": "menu",
	"menuEnd": "menu",
	"documentLoadComplete": "document",
	"typedCharacter": "text",
	"textChange": "text",
	"UIA_notification": "notification",
}


def coarse_kind(event_names: Iterable[str]) -> str:
	"""Summarise a burst of NVDA event names into one transition kind."""
	present = set(event_names)
	for name in _KIND_PRIORITY:
		if name in present and name in _COARSE_KIND:
			return _COARSE_KIND[name]
	return "other"


class EventJournal:
	"""Thread-safe ring buffers with a shared, waitable version counter."""

	def __init__(
		self,
		capacity: int = DEFAULT_CAPACITY,
		raw_capacity: int = RAW_CAPACITY,
	) -> None:
		self._condition = threading.Condition()
		self._transitions: collections.deque[Dict[str, Any]] = collections.deque(maxlen=capacity)
		self._raw: collections.deque[Dict[str, Any]] = collections.deque(maxlen=raw_capacity)
		self._version = 0
		self._dropped = 0
		self._capacity = capacity
		#: Set false to skip recording entirely (idle cost: one attribute read).
		self.enabled = True
		#: Raw per-event history is a debugging aid, not a change feed.
		self.raw_enabled = False

	# -- recording ---------------------------------------------------------

	def record_transition(
		self,
		kind: str,
		kinds: Iterable[str] = (),
		count: int = 1,
		duration_ms: float = 0.0,
		role: Any = None,
		name: Any = None,
		**extra: Any,
	) -> Optional[int]:
		"""Append one coalesced transition and wake any waiter. Never raises."""
		if not self.enabled:
			return None
		entry: Dict[str, Any] = {
			"kind": kind,
			"kinds": sorted({k for k in kinds if k}),
			"coarse": sorted({_COARSE_KIND.get(k, "other") for k in kinds if k}),
			"count": max(1, int(count)),
			"at_ms": int(time.monotonic() * 1000),
			"duration_ms": round(max(0.0, float(duration_ms)), 1),
			"role": _safe_int(role),
			"name": _safe_str(name),
		}
		if extra:
			entry.update(extra)
		return self._append(self._transitions, entry)

	def record_raw(self, kind: str, obj: Any = None, **extra: Any) -> Optional[int]:
		"""Append one raw event. Only kept when ``raw_enabled`` is set."""
		if not self.enabled or not self.raw_enabled:
			return None
		entry: Dict[str, Any] = {"kind": kind, "at_ms": int(time.monotonic() * 1000)}
		if obj is not None:
			try:
				entry["role"] = _safe_int(getattr(obj, "role", None))
				entry["name"] = _safe_str(getattr(obj, "name", None))
			except Exception:  # noqa: BLE001 — a journal must never break an event chain
				pass
		if extra:
			entry.update(extra)
		return self._append(self._raw, entry)

	def _append(self, target: collections.deque, entry: Dict[str, Any]) -> int:
		with self._condition:
			self._version += 1
			entry["version"] = self._version
			if len(target) == target.maxlen:
				self._dropped += 1
			target.append(entry)
			self._condition.notify_all()
			return self._version

	# -- reading -----------------------------------------------------------

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
		raw: bool = False,
	) -> Dict[str, Any]:
		"""Return entries after ``since``, plus whether history was lost."""
		wanted = {kind for kind in (kinds or ()) if kind}
		source = self._raw if raw else self._transitions
		with self._condition:
			current = self._version
			entries: List[Dict[str, Any]] = [
				entry
				for entry in source
				if entry["version"] > since
				and (
					not wanted
					or entry["kind"] in wanted
					or bool(wanted & set(entry.get("kinds", ())))
					or bool(wanted & set(entry.get("coarse", ())))
				)
			]
			oldest_raw = source[0]["version"] if source else current + 1
			dropped = self._dropped
		# A cursor older than the oldest surviving *matched* entry means history
		# was lost. When nothing matched, there is nothing to have lost.
		truncated = since + 1 < oldest_raw and bool(entries)
		return {
			"from_version": since,
			"to_version": current,
			"state_version": current,
			"truncated": truncated,
			"dropped": dropped,
			"source": "raw" if raw else "transitions",
			"events": entries[-limit:] if limit > 0 else entries,
		}

	def wait(
		self,
		since: int = 0,
		timeout_ms: int = 0,
		limit: int = 100,
		kinds: Optional[Iterable[str]] = None,
		raw: bool = False,
	) -> Dict[str, Any]:
		"""Block until the version advances past ``since`` or the timeout expires.

		``timeout_ms == 0`` returns immediately, which makes this the same call as
		:meth:`snapshot`. Blocking happens on the caller's (HTTP worker) thread;
		never call this from NVDA's main thread.
		"""
		if timeout_ms <= 0:
			return self.snapshot(since=since, limit=limit, kinds=kinds, raw=raw)

		deadline = time.monotonic() + (timeout_ms / 1000.0)
		with self._condition:
			while self._version <= since:
				remaining = deadline - time.monotonic()
				if remaining <= 0:
					break
				self._condition.wait(timeout=remaining)
		return self.snapshot(since=since, limit=limit, kinds=kinds, raw=raw)


class TransitionCoalescer:
	"""Turns a burst of events into one journal entry.

	NVDA dispatches several events per user action (three typed characters in
	Edge produced 16 events of five kinds). A client waiting on that stream wakes
	repeatedly and can read the screen between two halves of one change. The
	coalescer therefore holds the burst until it has been quiet for
	``quiet_ms``, then writes a single transition.

	Threading: :meth:`record` is called from NVDA's main thread and only touches
	a lock-protected buffer, so it never blocks the event chain. The flush runs on
	its own daemon thread and never touches NVDA objects — the object's role and
	name are read at record time, precisely because it may be gone later.
	"""

	def __init__(
		self,
		journal: EventJournal,
		quiet_ms: int = QUIET_MS,
		clock: Any = time.monotonic,
	) -> None:
		self._journal = journal
		self._quiet_s = max(0, quiet_ms) / 1000.0
		self._clock = clock
		self._condition = threading.Condition()
		self._pending: Optional[Dict[str, Any]] = None
		self._thread: Optional[threading.Thread] = None
		self._stopped = False
		#: Called for every recorded event, on the recording thread. Cheap by
		#: contract: the state projector uses it to bump its revision counter.
		self.on_event: Any = None

	def start(self) -> None:
		with self._condition:
			if self._thread is not None:
				return
			self._thread = threading.Thread(
				target=self._run,
				daemon=True,
				name="NVDA-Event-Coalescer",
			)
			self._thread.start()

	def record(self, event_name: str, obj: Any = None) -> None:
		"""Record one event. Cheap, non-blocking, never raises."""
		role = None
		name = None
		try:
			if obj is not None:
				role = getattr(obj, "role", None)
				name = getattr(obj, "name", None)
		except Exception:  # noqa: BLE001
			role, name = None, None

		callback = self.on_event
		if callback is not None:
			try:
				callback()
			except Exception:  # noqa: BLE001 — a recorder must never break NVDA
				pass

		self._journal.record_raw(event_name, None, role=_safe_int(role), name=_safe_str(name))

		now = self._clock()
		with self._condition:
			pending = self._pending
			if pending is None:
				pending = {
					"names": set(),
					"count": 0,
					"first": now,
					"last": now,
					"role": role,
					"name": name,
				}
				self._pending = pending
			pending["names"].add(event_name)
			pending["count"] += 1
			pending["last"] = now
			# The most recent event is the one worth labelling the transition
			# with: a focus burst ends on the new focus, and its name is the
			# useful headline.
			pending["role"] = role
			pending["name"] = name
			self._condition.notify_all()

	def flush(self) -> None:
		"""Emit any pending burst now, regardless of the quiet window."""
		with self._condition:
			pending = self._pending
			self._pending = None
		if pending is not None:
			self._emit(pending)

	def _emit(self, pending: Dict[str, Any]) -> None:
		names = pending["names"]
		self._journal.record_transition(
			kind=coarse_kind(names),
			kinds=names,
			count=pending["count"],
			duration_ms=(pending["last"] - pending["first"]) * 1000.0,
			role=pending["role"],
			name=pending["name"],
		)

	def _run(self) -> None:
		while True:
			with self._condition:
				while self._pending is None and not self._stopped:
					self._condition.wait(timeout=0.25)
				if self._stopped:
					return
				pending = self._pending
				assert pending is not None
				wait_s = (pending["last"] + self._quiet_s) - self._clock()
				if wait_s > 0:
					self._condition.wait(timeout=wait_s)
					continue
				self._pending = None
			self._emit(pending)

	def stop(self) -> None:
		with self._condition:
			self._stopped = True
			self._condition.notify_all()
		thread = self._thread
		if thread is not None and thread.is_alive():
			thread.join(timeout=1.0)
		self.flush()


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


class EventRecorder:
	"""The object NVDA's event chain talks to.

	Keeps the two jobs separate and cheap: bump the projector's revision so a
	waiter wakes (thread-safe, no NVDA access), and hand the event to the
	coalescer so it becomes part of one transition.
	"""

	def __init__(self, journal: EventJournal, projector: Any = None) -> None:
		self.journal = journal
		self.projector = projector
		self.coalescer = TransitionCoalescer(journal)
		if projector is not None:
			self.coalescer.on_event = projector.mark_dirty

	def start(self) -> None:
		self.coalescer.start()

	def record(self, event_name: str, obj: Any = None) -> None:
		"""Record one NVDA event. Must stay cheap and safe."""
		try:
			self.coalescer.record(event_name, obj)
		except Exception:  # noqa: BLE001 — never break NVDA's event chain
			pass

	def stop(self) -> None:
		self.coalescer.stop()
