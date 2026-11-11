"""Domain errors raised by the NVDA bridge layer.

These are conditions the caller can understand and act on — "there is no next
object", "this target has no app module" — as opposed to bridge faults. Each
carries the protocol error code it should surface as, so the transport does not
have to classify a bare ``RuntimeError`` by matching message text.
"""

from __future__ import annotations

from typing import Any


class DomainUnavailableError(RuntimeError):
	"""The requested operation is not available in the current UI state.

	Distinct from a crash: the request was well-formed and the bridge is healthy,
	but the target cannot supply what was asked for.
	"""

	#: Domain condition; clients may retry after the UI changes.
	protocol_code: int = 4205

	def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
		super().__init__(message)
		self.details = details or {}

	def to_details(self) -> dict[str, Any]:
		"""Structured detail for the wire error."""
		return dict(self.details)


class ElementUnavailableError(DomainUnavailableError):
	"""A located UIA element no longer exists.

	Reported as a stale reference (4206) rather than as a domain failure: the
	caller's next move is to locate again, exactly as for an expired ref, and the
	element's disappearance is not a bridge fault.
	"""

	protocol_code: int = 4206
