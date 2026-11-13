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
import secrets
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

#: Hard ceiling on one page. A caller that asks for more is clamped here rather
#: than allowed to turn one response into an unbounded payload.
MAX_PAGE_LIMIT = 500

#: A cursor older than retained history is not silently usable. These are the
#: stable classifications returned in ``resync_reason``.
RESYNC_EXPIRED = "cursor_expired"
RESYNC_JOURNAL_MISMATCH = "journal_mismatch"

#: Waiters re-check the journal generation while blocked, so a reset during a
#: long poll ends the wait instead of leaving the client watching a dead journal.
WAIT_RECHECK_SECONDS = 0.25

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
		#: Identifies this journal for as long as it exists.
		#:
		#: A version number only means something inside the journal that issued
		#: it. NVDA restarts begin again at zero, so a client holding a cursor
		#: from before the restart would ignore everything until the new sequence
		#: outgrew the old: comparing this id is how it knows to reset instead.
		self.journal_id = secrets.token_hex(8)
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

	# -- journal generation ------------------------------------------------

	def reset(self, reason: str = "reset") -> str:
		"""Start a new journal generation and return its id.

		A version number is only meaningful inside the generation that issued
		it. Anything that drops retained history (an explicit reset, a manual
		reload that deliberately discards the journal) must therefore also
		change the id: a client holding ``(old_id, 7)`` can then tell that its
		cursor no longer refers to this history instead of silently reading
		version 7 of a different sequence.
		"""
		with self._condition:
			self._transitions.clear()
			self._raw.clear()
			self._version = 0
			self._dropped = 0
			self.journal_id = secrets.token_hex(8)
			self._reset_reason = str(reason or "reset")
			self._condition.notify_all()
			return self.journal_id

	def cursor(self) -> Dict[str, Any]:
		"""Return the current typed cursor ``{journal_id, version}``."""
		with self._condition:
			return {"journal_id": self.journal_id, "version": self._version}

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
		journal_id: Optional[str] = None,
	) -> Dict[str, Any]:
		"""Return entries after ``since``, oldest first, plus what they mean.

		``limit`` pages from the *oldest* unseen entry, not the newest: returning
		the newest would hand back a ``to_version`` at the head of the journal
		while dropping everything between ``since`` and it, so a client that
		polled with ``since = to_version`` would skip events with no record that
		it had. The caller continues with ``since = to_version`` while
		``has_more`` is true.

		``journal_id`` turns ``since`` into a typed cursor ``(journal_id,
		version)``. When it does not match this journal's current generation the
		call returns a *journal mismatch*: no events, ``resync_required: true``
		and a ``resync_from`` cursor at this journal's head. A bare ``since``
		with no journal id remains accepted as a current-journal convenience;
		it cannot detect a replacement whose version has already overtaken the
		old cursor, which is exactly why explicit catch-up should send the id.

		Version vocabulary, which the response spells out because two different
		counters used to be reported as ``state_version``:

		``event_version``
			The journal head after this page was taken — ``state_version``
			kept for compatibility, since clients already read it.
		``to_version``
			The last event version *in this page*, which is the correct value
			to pass back. ``next_cursor`` repeats it under a name that says so.
		``oldest_retained_version`` / ``earliest_available_version``
			The oldest version still in the buffer; 0 when nothing is retained.
		``cursor_status``
			One of ``ok`` (caught up or moving forward), ``paged`` (more
			remains), ``stale`` (the cursor predates retained history),
			``future_cursor`` (the cursor is beyond this journal's head),
			``journal_mismatch`` (the supplied journal id is a different
			generation).
		``expired`` / ``resync_required``
			History before the cursor was evicted, so the returned page cannot
			be treated as a complete record of what happened. ``resync_from``
			is a typed cursor for the earliest usable retained version.
		``complete``/``caught_up``
			Whether this page lost nothing, and whether the caller has seen
			everything after ``since``.
		"""
		wanted = {kind for kind in (kinds or ()) if kind}
		source = self._raw if raw else self._transitions
		page_limit = max(1, min(MAX_PAGE_LIMIT, int(limit))) if limit else 0

		# Every fact below is read under one lock, so a concurrent append or
		# eviction cannot produce a page whose versions and flags disagree.
		with self._condition:
			current = self._version
			current_journal_id = self.journal_id
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
			visible = entries[:page_limit] if page_limit else entries
			has_more = len(visible) < len(entries)
			# History before `since` was evicted exactly when the buffer no
			# longer reaches back to the entry after the cursor. This is true
			# even when the kind filter matches nothing in the remaining page:
			# otherwise a filtered catch-up could silently report a complete
			# history for a kind whose events were evicted.
			expired = since + 1 < oldest_raw
			to_version = visible[-1]["version"] if visible else since

			if journal_id is not None and journal_id != current_journal_id:
				return self._mismatch_page(
					since=since,
					current_journal_id=current_journal_id,
					current=current,
					oldest_raw=oldest_raw if source else 0,
					dropped=dropped,
					source=raw,
				)

			if since > current:
				# Only another journal can issue a version this one has not
				# reached; report it, do not rewind the caller's cursor.
				cursor_status = "future_cursor"
			elif expired:
				cursor_status = "stale"
			elif has_more:
				cursor_status = "paged"
			else:
				cursor_status = "ok"

			earliest = oldest_raw if source else 0
			resync_required = expired
			payload: Dict[str, Any] = {
				"journal_id": current_journal_id,
				"from_version": since,
				"to_version": to_version,
				# Journal head. Kept under its old name because clients read it;
				# `event_version` is the name that says which counter it is.
				"state_version": current,
				"event_version": current,
				"oldest_retained_version": earliest,
				"earliest_available_version": earliest,
				"next_cursor": to_version,
				"cursor": {"journal_id": current_journal_id, "version": to_version},
				"cursor_status": cursor_status,
				"expired": expired,
				"resync_required": resync_required,
				"resync_reason": RESYNC_EXPIRED if expired else None,
				"resync_from": (
					{"journal_id": current_journal_id, "version": max(0, earliest - 1)}
					if expired and source
					else None
				),
				"history_complete": not expired,
				"truncated": expired,
				"has_more": has_more,
				# `complete` is about this page's range; `caught_up` is about the
				# caller's. Paging without loss is complete but not yet caught up.
				"complete": not expired,
				"caught_up": not has_more and not expired,
				"dropped": dropped,
				"source": "raw" if raw else "transitions",
				"events": visible,
			}
			if expired:
				payload["recovery"] = {
					"action": "resync",
					"hint": (
						"History before this cursor was evicted. Re-observe current state, then "
						"continue from resync_from; the returned events are not a complete record."
					),
				}
			return payload

	@staticmethod
	def _mismatch_page(
		since: int,
		current_journal_id: str,
		current: int,
		oldest_raw: int,
		dropped: int,
		source: bool,
	) -> Dict[str, Any]:
		"""A typed cursor that belongs to another journal generation.

		The old numeric version is deliberately *not* interpreted here. Doing so
		is the silent skip this classification exists to prevent: version 7 of
		the previous journal and version 7 of this one are unrelated.
		"""
		return {
			"journal_id": current_journal_id,
			"from_version": since,
			"to_version": since,
			"state_version": current,
			"event_version": current,
			"oldest_retained_version": oldest_raw if source else 0,
			"earliest_available_version": oldest_raw if source else 0,
			"next_cursor": since,
			"cursor": {"journal_id": current_journal_id, "version": since},
			"cursor_status": "journal_mismatch",
			"expired": False,
			"resync_required": True,
			"resync_reason": RESYNC_JOURNAL_MISMATCH,
			"resync_from": {"journal_id": current_journal_id, "version": current},
			"history_complete": False,
			"truncated": False,
			"has_more": False,
			"complete": False,
			"caught_up": False,
			"dropped": dropped,
			"source": "raw" if source else "transitions",
			"events": [],
			"recovery": {
				"action": "resync",
				"hint": (
					"The cursor belongs to a different journal generation (Bridge or NVDA "
					"restarted). Re-observe current state and continue from resync_from."
				),
			},
		}

	def wait(
		self,
		since: int = 0,
		timeout_ms: int = 0,
		limit: int = 100,
		kinds: Optional[Iterable[str]] = None,
		raw: bool = False,
		journal_id: Optional[str] = None,
		cancellation_event: Any = None,
	) -> Dict[str, Any]:
		"""Block until the version advances past ``since`` or the timeout expires.

		``timeout_ms == 0`` returns immediately, which makes this the same call as
		:meth:`snapshot`. Blocking happens on the caller's (HTTP worker) thread;
		never call this from NVDA's main thread.

		The wait ends early when the journal generation changes, and a cursor
		from another generation never blocks: both cases return the mismatch
		page immediately so a client is told to resync instead of waiting for
		events that this journal cannot describe. ``cancellation_event`` (any
		object with ``is_set``) ends the wait too, so a shutdown or a dropped
		request does not pin the worker for the full timeout.
		"""
		if timeout_ms <= 0:
			return self.snapshot(since=since, limit=limit, kinds=kinds, raw=raw, journal_id=journal_id)

		with self._condition:
			start_journal_id = self.journal_id
		if journal_id is not None and journal_id != start_journal_id:
			return self.snapshot(since=since, limit=limit, kinds=kinds, raw=raw, journal_id=journal_id)

		cancelled = False
		deadline = time.monotonic() + (timeout_ms / 1000.0)
		with self._condition:
			while self._version <= since:
				if self.journal_id != start_journal_id:
					# A reset during the wait invalidates the cursor, even if the
					# new generation has already overtaken its number.
					break
				if cancellation_event is not None and cancellation_event.is_set():
					cancelled = True
					break
				remaining = deadline - time.monotonic()
				if remaining <= 0:
					break
				self._condition.wait(timeout=min(remaining, WAIT_RECHECK_SECONDS))
		# Pass the generation observed at wait start only when it still exists;
		# after a reset this is false and the caller sees the mismatch.
		if journal_id is None and start_journal_id != self.journal_id:
			payload = self.snapshot(
				since=since, limit=limit, kinds=kinds, raw=raw, journal_id=start_journal_id
			)
		else:
			payload = self.snapshot(
				since=since,
				limit=limit,
				kinds=kinds,
				raw=raw,
				journal_id=journal_id or start_journal_id,
			)
		if cancelled:
			payload["cancelled"] = True
		return payload


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
