"""Versioned references to UI objects.

A reference is a *locator*, not a pointer, and resolving one must never scan the
tree. Walking to a control in a 15-node page took 130 ms — about 9 ms per node,
because every ``children`` call crosses into the browser process — and a 500-item
page blew the main-thread budget and froze NVDA. So this table holds the object
that was captured, plus the identity fields needed to notice that it died, and
nothing else.

Two consequences worth stating:

* **No lookup by name.** A stale reference is reported as stale (4206) with the
  caller's next `observe` as the remedy, never "resolved" by searching for a
  similar name.
* **Identity is checked by re-reading, not by a flag.** ``isAlive`` and
  ``isDefunct`` do not exist on the objects NVDA hands out, and ``isCurrent`` was
  ``False`` for a focused, valid text field, so it cannot be used as a validity
  predicate. The predicate is "the identity fields still read back, and the read
  does not raise".

This module is deliberately NVDA-agnostic: the caller supplies ``identity_of``.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

#: Fields used to recognise an object again. ``role`` and the window coordinates
#: are structural; ``name`` is included but only reported, because a name can
#: change legitimately (labels, document titles) while the object survives.
IDENTITY_FIELDS = ("role", "name", "windowClassName", "windowControlID")

#: A long session must not accumulate dead objects. Entries are also evicted
#: oldest-first once the table is full.
DEFAULT_TTL_SECONDS = 300.0
DEFAULT_MAX_ENTRIES = 256


class StaleReference(Exception):
	"""The reference no longer identifies an object on screen.

	Carries the protocol code directly: this is a condition the caller fixes by
	re-observing, not an internal fault.
	"""

	protocol_code = 4206

	def __init__(self, ref: str, reason: str) -> None:
		super().__init__(f"Reference '{ref}' is stale: {reason}. Call observe to capture fresh references.")
		self.ref = ref
		self.reason = reason

	def to_details(self) -> dict[str, Any]:
		return {"ref": self.ref, "reason": self.reason}


@dataclass
class Reference:
	"""One captured object and the fields that identify it."""

	ref: str
	obj: Any
	identity: dict[str, Any]
	state_version: int
	created: float = field(default_factory=time.monotonic)
	kind: str = "node"

	def is_expired(self, ttl_s: float, now: float) -> bool:
		return ttl_s > 0 and (now - self.created) > ttl_s


class ReferenceTable:
	"""Thread-safe, bounded, expiring map of ``ref`` to captured object."""

	def __init__(
		self,
		max_entries: int = DEFAULT_MAX_ENTRIES,
		ttl_s: float = DEFAULT_TTL_SECONDS,
		clock: Callable[[], float] = time.monotonic,
	) -> None:
		self._lock = threading.Lock()
		self._refs: "OrderedDict[str, Reference]" = OrderedDict()
		self._max_entries = max(1, max_entries)
		self._ttl_s = ttl_s
		self._clock = clock

	def register(
		self,
		objects: dict[str, Any],
		state_version: int,
		identity_of: Callable[[Any], dict[str, Any]],
	) -> list[str]:
		"""Capture ``objects`` (ref -> object) and return the captured refs."""
		registered: list[str] = []
		for ref, obj in objects.items():
			try:
				identity = identity_of(obj)
			except Exception:  # noqa: BLE001 — a dead object is not registrable
				continue
			entry = Reference(
				ref=ref,
				obj=obj,
				identity=identity,
				state_version=state_version,
				created=self._clock(),
				kind=_kind_of(ref),
			)
			with self._lock:
				self._refs[ref] = entry
				self._refs.move_to_end(ref)
				while len(self._refs) > self._max_entries:
					self._refs.popitem(last=False)
			registered.append(ref)
		return registered

	def get(self, ref: str, identity_of: Optional[Callable[[Any], dict[str, Any]]] = None) -> Reference:
		"""Return the reference, or raise :class:`StaleReference`."""
		with self._lock:
			entry = self._refs.get(ref)
			if entry is not None:
				self._refs.move_to_end(ref)
		if entry is None:
			raise StaleReference(ref, "no object is registered under this reference")

		if entry.is_expired(self._ttl_s, self._clock()):
			self.discard(ref)
			raise StaleReference(ref, f"it was captured more than {int(self._ttl_s)} s ago")

		if identity_of is not None:
			try:
				current = identity_of(entry.obj)
			except Exception as error:  # noqa: BLE001
				self.discard(ref)
				raise StaleReference(ref, f"reading the object failed ({type(error).__name__})") from error
			mismatch = _identity_mismatch(entry.identity, current)
			if mismatch:
				self.discard(ref)
				raise StaleReference(ref, mismatch)
		return entry

	def discard(self, ref: str) -> None:
		with self._lock:
			self._refs.pop(ref, None)

	def clear(self) -> None:
		with self._lock:
			self._refs.clear()

	def __len__(self) -> int:
		with self._lock:
			return len(self._refs)

	def list_refs(self) -> list[str]:
		with self._lock:
			return list(self._refs.keys())


def _kind_of(ref: str) -> str:
	if ref == "f":
		return "focus"
	if ref.startswith("a"):
		return "ancestor"
	if ref.startswith("c"):
		return "child"
	if ref.startswith("d"):
		return "dialog"
	return "node"


def _identity_mismatch(expected: dict[str, Any], current: dict[str, Any]) -> Optional[str]:
	"""Return a human-readable mismatch, or None when the object still matches.

	``name`` is compared only to *report* a rename; a renamed object is still the
	same object, and rejecting it would make references unusable on exactly the
	controls whose label tracks their value.
	"""
	for field_name in ("role", "windowClassName", "windowControlID"):
		before = expected.get(field_name)
		after = current.get(field_name)
		if before is None or after is None:
			continue
		if before != after:
			return f"{field_name} changed from {before!r} to {after!r}"
	return None
