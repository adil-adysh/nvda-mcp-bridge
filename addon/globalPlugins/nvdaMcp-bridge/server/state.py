"""Coalesced UI state: projection, diff and versioning.

This is the P1 core of `docs/IMPLEMENTATION_PLAN.md`. NVDA dispatches a burst of
low-level events per user action — five typed characters produced 29 events in
Notepad, three produced 16 in Edge — so the useful abstraction is not the event
stream but a *projection* of what is currently on screen, plus a compact diff of
what changed.

Three counters, deliberately distinct:

``event_seq``
	One per dispatched event. Lives in the journal, for debugging only.
``revision``
	Bumped when NVDA reports activity. Thread-safe signal for a waiter; it means
	"something happened", not "something changed".
``state_version``
	Bumped only when the projection actually differs. This is what references
	bind to and what callers compare, so a caret blink does not invalidate them.

The projection is deliberately NVDA-agnostic: it accepts any object exposing
``role``/``name``/``value``/``states``/``location``/``parent``, which keeps it
testable outside NVDA and keeps the NVDA import surface in the caller.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

#: How far up the tree a projection reaches. Deeper chains cost a COM call each
#: and rarely add meaning: the window name is the useful part.
MAX_ANCESTORS = 5

#: Field order for a focus diff. Order matters only for readability of the delta.
FOCUS_FIELDS = ("role", "name", "value", "states", "location")


def _enum_name(value: Any) -> str:
	"""Render an NVDA enum as a stable upper-case name.

	``controlTypes.Role.BUTTON`` has no useful ``str()`` but a good ``name``; the
	fallback keeps arbitrary test doubles and future enum types working.
	"""
	if value is None:
		return "UNKNOWN"
	name = getattr(value, "name", None)
	if isinstance(name, str) and name:
		return name.upper()
	return str(value)


def _safe_get(obj: Any, attribute: str) -> Any:
	"""Read an attribute, treating any failure as "not available".

	NVDA property reads cross into another process and can fail on a dead object
	or a hung application; a projection must degrade, never raise.
	"""
	try:
		return getattr(obj, attribute)
	except Exception:
		return None


def _describe_states(obj: Any) -> list[str]:
	try:
		return sorted(_enum_name(state) for state in obj.states)
	except Exception:
		return []


def describe(obj: Any, max_value: int = 200) -> dict:
	"""Return a JSON-ready description of one object."""
	location = _safe_get(obj, "location")
	if location is not None:
		try:
			location = [int(part) for part in location]
		except (TypeError, ValueError):
			location = None

	value = _safe_get(obj, "value")
	if isinstance(value, str) and len(value) > max_value:
		value = value[:max_value]

	return {
		"role": _enum_name(_safe_get(obj, "role")),
		"name": _safe_get(obj, "name"),
		"value": value,
		"states": _describe_states(obj),
		"location": location,
	}


def ancestor_chain(focus: Any, limit: int = MAX_ANCESTORS) -> list[dict]:
	"""Describe the parents of ``focus``, nearest first."""
	chain: list[dict] = []
	parent = focus
	for _ in range(max(0, limit)):
		parent = _safe_get(parent, "parent")
		if parent is None:
			break
		chain.append(describe(parent))
	return chain


def project(focus: Any, max_ancestors: int = MAX_ANCESTORS) -> dict:
	"""Return the observation for a focused object, or an empty one if unfocused."""
	if focus is None:
		return {"focus": None, "ancestors": []}
	return {"focus": describe(focus), "ancestors": ancestor_chain(focus, max_ancestors)}


def _window_name(observation: Optional[dict]) -> Optional[str]:
	"""Return the name of the window containing the focus.

	The *nearest* window ancestor, not the topmost one: every chain ends at the
	desktop, so the topmost name is "Desktop" for every application and a window
	change would diff as "Desktop -> Desktop".
	"""
	ancestors = (observation or {}).get("ancestors") or []
	for ancestor in ancestors:
		if ancestor.get("role") == "WINDOW":
			return ancestor.get("name")
	return ancestors[-1].get("name") if ancestors else None


def diff(before: Optional[dict], after: Optional[dict]) -> dict:
	"""Return what changed between two projections, field by field.

	An empty result means "no observable change", which is a legitimate and
	reportable outcome — the action may have done nothing, and a caller must be
	able to tell that apart from "the action failed".
	"""
	if before is None or after is None:
		return {}

	before_focus = before.get("focus") or {}
	after_focus = after.get("focus") or {}
	changes: dict[str, dict] = {}

	for field in FOCUS_FIELDS:
		old = before_focus.get(field)
		new = after_focus.get(field)
		if old != new:
			changes[field] = {"from": old, "to": new}

	if before.get("ancestors") != after.get("ancestors"):
		changes["window"] = {
			"from": _window_name(before),
			"to": _window_name(after),
		}

	return changes


class StateProjector:
	"""Holds the current projection and the counters derived from it.

	Threading contract: ``refresh`` reads NVDA objects, so it must run on NVDA's
	main thread. ``mark_dirty`` and ``wait_for_revision`` are safe from any thread
	and never touch NVDA objects — a waiter must not be able to reach the main
	thread, or a long poll would freeze the screen reader it is waiting for.
	"""

	def __init__(
		self,
		project_fn: Callable[[], dict],
		clock: Callable[[], float] = time.monotonic,
	):
		self._project_fn = project_fn
		self._clock = clock
		self._condition = threading.Condition(threading.Lock())
		self._projection: Optional[dict] = None
		self._state_version = 0
		self._revision = 0

	@property
	def state_version(self) -> int:
		with self._condition:
			return self._state_version

	@property
	def revision(self) -> int:
		with self._condition:
			return self._revision

	@property
	def projection(self) -> Optional[dict]:
		with self._condition:
			return self._projection

	def mark_dirty(self) -> int:
		"""Record that NVDA dispatched an event; wakes anyone waiting."""
		with self._condition:
			self._revision += 1
			self._condition.notify_all()
			return self._revision

	def refresh(self) -> tuple[dict, dict, int]:
		"""Re-project now; bump ``state_version`` only if something changed.

		Returns ``(projection, changes, state_version)``. The projection is taken
		before the lock is held because it performs cross-process reads.
		"""
		projection = self._project_fn()
		with self._condition:
			previous = self._projection
			changes = diff(previous, projection) if previous is not None else {}
			self._projection = projection
			if previous is None or changes:
				self._state_version += 1
			self._condition.notify_all()
			return projection, changes, self._state_version

	def snapshot(self) -> tuple[Optional[dict], int, int]:
		"""Return ``(projection, state_version, revision)`` atomically."""
		with self._condition:
			return self._projection, self._state_version, self._revision

	def wait_for_revision(self, since: int, timeout_ms: int) -> bool:
		"""Block until ``revision`` advances past ``since``, or the timeout expires."""
		deadline = self._clock() + max(0, timeout_ms) / 1000.0
		with self._condition:
			while self._revision <= since:
				remaining = deadline - self._clock()
				if remaining <= 0:
					return False
				self._condition.wait(timeout=remaining)
			return True
