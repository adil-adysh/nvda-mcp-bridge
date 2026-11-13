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

#: Fields that identify the *window* an object belongs to. A window handle on
#: its own is reusable by the OS, so it is never the sole identity: the process
#: and class it belonged to are checked with it. Title is descriptive metadata.
WINDOW_IDENTITY_FIELDS = ("windowHandle", "processID", "processId", "windowClassName")

#: Fields that can distinguish two objects rather than merely describe one.
#: ``role``/``control_type`` are shared by thousands of controls and a name can
#: change, so neither is enough on its own to say "this is still the object".
#: When none of these carries a value, identity cannot be verified and a
#: mutating operation must fail rather than guess.
STRONG_IDENTITY_FIELDS = (
	"windowHandle",
	"windowControlID",
	"processID",
	"processId",
	"runtime_id",
	"runtimeId",
	"automation_id",
	"automationId",
	"control_id",
	"controlId",
	"windowClassName",
)

#: Stable reasons reported in ``details.reason`` (and ``reason_code``).
REASON_TTL = "ttl"
REASON_IDENTITY = "identity"
REASON_WINDOW = "window"
REASON_BRIDGE_INSTANCE = "bridge_instance"
REASON_UNAVAILABLE = "unavailable"

#: A long session must not accumulate dead objects. Entries are also evicted
#: oldest-first once the table is full.
DEFAULT_TTL_SECONDS = 300.0
DEFAULT_MAX_ENTRIES = 256


class StaleReference(Exception):
	"""The reference no longer identifies an object on screen.

	Carries the protocol code directly: this is a condition the caller fixes by
	re-observing, not an internal fault. ``reason_code`` is the stable enum;
	``reason`` is the bounded human detail.
	"""

	protocol_code = 4206

	def __init__(
		self,
		ref: str,
		reason: str,
		reason_code: str = REASON_IDENTITY,
		scope: str = "",
	) -> None:
		super().__init__(f"Reference '{ref}' is stale: {reason}. Call observe to capture fresh references.")
		self.ref = ref
		self.reason = reason
		self.reason_code = reason_code
		self.scope = scope

	def to_details(self) -> dict[str, Any]:
		return {
			"name": "STALE_REFERENCE",
			"ref": self.ref,
			"reason": self.reason,
			"reason_code": self.reason_code,
			"expected_reference": "a short lowercase ref from observe/locate, for example 'f' or 'n3'",
			"received": self.ref,
			"scope": self.scope,
			"recovery": {
				"action": "observe",
				"argument": "ref",
				"hint": (
					"Call observe and pass a ref from its refs map. Validity does not end when "
					"state_version advances; it ends when the reference is evicted, expires, or the "
					"object/window it identified is gone."
				),
			},
		}


@dataclass
class Reference:
	"""One captured object and the fields that identify it."""

	ref: str
	obj: Any
	identity: dict[str, Any]
	state_version: int
	created: float = field(default_factory=time.monotonic)
	kind: str = "node"
	#: Which identity reader to use when resolving this reference again. Recorded
	#: at capture time because probing a dead object for its type can itself raise:
	#: a dead UIA element raises from the first attribute access, which would make
	#: "is this an NVDA object or an element?" fail instead of answering.
	target_kind: str = "nvda"
	#: Identity of the owning window, captured separately so a handle swap is
	#: reported as a window failure rather than a generic identity mismatch.
	window_identity: dict[str, Any] = field(default_factory=dict)
	#: Bridge instance that issued this reference. A reference cannot survive a
	#: Bridge replacement even if an equal-looking object exists afterwards.
	bridge_instance: str = ""

	def is_expired(self, ttl_s: float, now: float) -> bool:
		return ttl_s > 0 and (now - self.created) > ttl_s


class ReferenceTable:
	"""Thread-safe, bounded, expiring map of ``ref`` to captured object."""

	def __init__(
		self,
		max_entries: int = DEFAULT_MAX_ENTRIES,
		ttl_s: float = DEFAULT_TTL_SECONDS,
		clock: Callable[[], float] = time.monotonic,
		instance_id: str = "",
	) -> None:
		self._lock = threading.Lock()
		self._refs: "OrderedDict[str, Reference]" = OrderedDict()
		self._max_entries = max(1, max_entries)
		self._ttl_s = ttl_s
		self._clock = clock
		self._instance_id = str(instance_id or "")

	def bind_instance(self, instance_id: str) -> bool:
		"""Bind this table to one Bridge instance; clear on replacement.

		Returns True when the instance changed and cached references were
		dropped. References are process-local locators, so an object that looks
		the same under a new Bridge is still not the object the caller observed.
		"""
		instance_id = str(instance_id or "")
		with self._lock:
			if self._instance_id == instance_id:
				return False
			self._refs.clear()
			self._instance_id = instance_id
			return True

	def register(
		self,
		objects: dict[str, Any],
		state_version: int,
		identity_of: Callable[[Any], dict[str, Any]],
		target_kind: str = "nvda",
		scope: str = "",
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
				target_kind=target_kind,
				window_identity=_window_identity(identity),
				bridge_instance=self._instance_id,
			)
			with self._lock:
				self._refs[ref] = entry
				self._refs.move_to_end(ref)
				while len(self._refs) > self._max_entries:
					self._refs.popitem(last=False)
			registered.append(ref)
		return registered

	def get(
		self,
		ref: str,
		identity_of: Optional[Callable[[Any], dict[str, Any]]] = None,
		scope: str = "",
	) -> Reference:
		"""Return the reference, or raise :class:`StaleReference`.

		Validation is identity-based. ``state_version`` is advisory metadata and
		is never compared here: an unrelated screen change does not invalidate a
		reference that still resolves to the same object.
		"""
		with self._lock:
			entry = self._refs.get(ref)
			if entry is not None:
				self._refs.move_to_end(ref)
			instance_id = self._instance_id
		if entry is None:
			raise StaleReference(
				ref,
				"no object is registered under this reference",
				REASON_UNAVAILABLE,
				scope,
			)

		if entry.bridge_instance and entry.bridge_instance != instance_id:
			self.discard(ref)
			raise StaleReference(
				ref,
				"it belongs to a previous Bridge instance",
				REASON_BRIDGE_INSTANCE,
				scope,
			)

		if entry.is_expired(self._ttl_s, self._clock()):
			self.discard(ref)
			raise StaleReference(
				ref,
				f"it was captured more than {int(self._ttl_s)} s ago",
				REASON_TTL,
				scope,
			)

		if identity_of is not None:
			try:
				current = identity_of(entry.obj)
			except Exception as error:  # noqa: BLE001
				self.discard(ref)
				raise StaleReference(
					ref,
					f"reading the object failed ({type(error).__name__})",
					REASON_UNAVAILABLE,
					scope,
				) from error
			if not _identity_is_verifiable(current):
				self.discard(ref)
				raise StaleReference(
					ref,
					"the identity fields that could confirm it are unavailable (no runtime id, "
					"automation id, control id or window handle)",
					REASON_IDENTITY,
					scope,
				)
			reason_code, mismatch = _identity_mismatch(entry.identity, current)
			if mismatch:
				self.discard(ref)
				raise StaleReference(ref, mismatch, reason_code, scope)
			window_reason, window_mismatch = _window_mismatch(entry.window_identity, current)
			if window_mismatch:
				self.discard(ref)
				raise StaleReference(ref, window_mismatch, window_reason, scope)
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
	if ref == "w":
		return "window"
	if ref.startswith("a"):
		return "ancestor"
	if ref.startswith("c"):
		return "child"
	if ref.startswith("d"):
		return "dialog"
	if ref.startswith("n"):
		return "located"
	return "node"


def _window_identity(identity: dict[str, Any]) -> dict[str, Any]:
	"""Project the window-identity fields out of an object identity."""
	return {name: identity.get(name) for name in WINDOW_IDENTITY_FIELDS if identity.get(name) not in (None, "", 0)}


def _is_meaningful(value: Any) -> bool:
	if value is None:
		return False
	if isinstance(value, (list, tuple, dict, set)):
		return bool(value)
	if isinstance(value, str):
		return bool(value.strip())
	return value != 0


def _identity_is_verifiable(identity: dict[str, Any]) -> bool:
	"""Whether at least one field can actually distinguish this object."""
	return any(_is_meaningful(identity.get(name)) for name in STRONG_IDENTITY_FIELDS)


def _identity_mismatch(expected: dict[str, Any], current: dict[str, Any]) -> tuple[str, Optional[str]]:
	"""Return ``(reason_code, human detail)``; detail is None when still the same.

	Every recorded field except ``name`` is structural and must still read back
	the same. The comparison is generic rather than a fixed field list because
	references point at two kinds of target — NVDA objects (role, window class,
	control id) and UIA elements (control type, automation id, runtime id) — and
	each brings its own unambiguous fields.

	``name`` is compared only to *report* a rename; a renamed object is still the
	same object, and rejecting it would make references unusable on exactly the
	controls whose label tracks their value.
	"""
	window_fields = set(WINDOW_IDENTITY_FIELDS)
	for field_name in sorted(set(expected) | set(current)):
		if field_name == "name":
			continue
		before = expected.get(field_name)
		after = current.get(field_name)
		if before is None or after is None:
			continue
		if before != after:
			reason = REASON_WINDOW if field_name in window_fields else REASON_IDENTITY
			return reason, f"{field_name} changed from {before!r} to {after!r}"
	return REASON_IDENTITY, None


def _window_mismatch(
	expected_window: dict[str, Any],
	current: dict[str, Any],
) -> tuple[str, Optional[str]]:
	"""Compare the owning window explicitly so the reason is precise.

	The generic comparison above already covers these fields when both sides
	have them; this adds the case where the reference recorded a window and the
	resolved object can no longer name one (or vice versa) for a field the
	generic pass skipped because one side was missing.
	"""
	current_window = _window_identity(current)
	for field_name in sorted(set(expected_window) | set(current_window)):
		before = expected_window.get(field_name)
		after = current_window.get(field_name)
		if before is None or after is None:
			continue
		if before != after:
			return REASON_WINDOW, f"owning window {field_name} changed from {before!r} to {after!r}"
	return REASON_WINDOW, None
