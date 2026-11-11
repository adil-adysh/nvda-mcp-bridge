"""Act on a UIA element directly, for targets found by ``locate``.

Why this module exists: ``locate`` searches with the platform's own UIA provider
(``FindAll`` over a 2,346-element Edge window: 65 ms for everything, ~12 ms for
role + name), and what comes back is an ``IUIAutomationElement``, not an NVDA
object. Converting the one into the other does not work — ``NVDAObjects.UIA.UIA``
rejected a live element with ``E_INVALIDARG`` — and hit-testing the element's
centre only works while it is on screen. So a located target is acted on through
the same patterns UIA exposes: Invoke, Value, Toggle, SelectionItem,
ExpandCollapse and SetFocus.

Consequences stated on the wire, not hidden:

* A located ref is not an NVDA object, so ``navigate`` and ``scroll`` (which walk
  NVDA's tree and synthesise keys) refuse it and say to use ``locate`` again.
* Identity is re-read from the element (control type, automation id, runtime id).
  A dead element raises on the first read, which the reference table reports as
  stale (4206) rather than pretending the target is fine.

Everything here must run on NVDA's main thread.
"""

from __future__ import annotations

from typing import Any, Optional

import logHandler

from ..server.locate import role_for_uia_control_type
from .errors import DomainUnavailableError, ElementUnavailableError

#: A readable value is capped the same way NVDA reads are; a document's value can
#: be arbitrary and no caller should be handed it by accident.
DEFAULT_MAX_TEXT = 400

_CONTROL_TYPE_NAMES: dict[int, str] = {}


def _uia():
	"""Return the UIAHandler module, or raise if UIA is disabled."""
	try:
		import UIAHandler

		return UIAHandler
	except Exception as error:  # noqa: BLE001
		raise DomainUnavailableError(
			f"UIA is not available in this NVDA session ({type(error).__name__}), so this target cannot be acted on",
		) from error


def module():
	"""Return the UIAHandler module for callers that need raw UIA objects."""
	return _uia()


def is_element(obj: Any) -> bool:
	"""Whether ``obj`` is a UIA element rather than an NVDA object.

	Duck-typed on purpose: the comtypes pointer's type is a generic interface
	pointer, and importing the generated interface just to isinstance against it
	would make this module unusable outside a live NVDA.

	Every read is guarded: a dead element can raise on attribute access instead of
	returning, and "cannot tell" must mean "not an element" here, because the
	reference table has already recorded which kind a ref is.
	"""
	try:
		return hasattr(obj, "CurrentControlType") and hasattr(obj, "GetRuntimeId")
	except Exception:  # noqa: BLE001
		return False


def _control_type_name(control_type: Optional[int]) -> str:
	"""Return ``Button`` for ``UIA_ButtonControlTypeId``, etc."""
	global _CONTROL_TYPE_NAMES
	U = _uia()
	if not _CONTROL_TYPE_NAMES:
		names: dict[int, str] = {}
		for attribute in dir(U):
			if not attribute.startswith("UIA_") or not attribute.endswith("ControlTypeId"):
				continue
			try:
				value = getattr(U, attribute)
			except Exception:  # noqa: BLE001
				continue
			if isinstance(value, int) and not isinstance(value, bool):
				names[value] = attribute[len("UIA_") : -len("ControlTypeId")]
		_CONTROL_TYPE_NAMES = names
	try:
		key = int(control_type) if control_type is not None else -1
	except (TypeError, ValueError):
		key = -1
	return _CONTROL_TYPE_NAMES.get(key, "")


def identity_of(element: Any) -> dict[str, Any]:
	"""Return the fields used to recognise an element again.

	Deliberately reads properties that a dead element cannot answer: the caller
	uses the failure to report the reference stale. ``name`` is included but is
	not a structural field — labels legitimately change while the control lives.
	"""
	return {
		"controlType": int(element.CurrentControlType),
		"automationId": str(element.CurrentAutomationId or ""),
		"runtimeId": [int(part) for part in (element.GetRuntimeId() or ())],
		"name": element.CurrentName or "",
	}


def _safe(reader: Any, default: Any = None) -> Any:
	try:
		return reader()
	except Exception:  # noqa: BLE001 — a property that cannot be read is unknown
		return default


def _bounds(element: Any) -> list[int]:
	"""Return the element's screen rectangle as ``[left, top, width, height]``.

	Same shape as an NVDA object's ``location``, so a located node and an observed
	node can be compared without a second convention.
	"""
	rect = _safe(lambda: element.CurrentBoundingRectangle)
	if rect is None:
		return []
	try:
		left, top, right, bottom = int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
	except Exception:  # noqa: BLE001
		return []
	if right <= left or bottom <= top:
		return []
	return [left, top, right - left, bottom - top]


def _states(element: Any) -> list[str]:
	states: list[str] = []
	if _safe(lambda: element.CurrentIsEnabled) is False:
		states.append("DISABLED")
	else:
		states.append("ENABLED")
	if _safe(lambda: element.CurrentIsKeyboardFocusable):
		states.append("FOCUSABLE")
	if _safe(lambda: element.CurrentHasKeyboardFocus):
		states.append("FOCUSED")
	if _safe(lambda: element.CurrentIsOffscreen):
		states.append("OFFSCREEN")
	if _safe(lambda: element.CurrentIsInvokePatternAvailable):
		states.append("CLICKABLE")
	return states


def _pattern(element: Any, name: str) -> Any:
	"""Return the named pattern interface, or ``None`` when unsupported."""
	U = _uia()
	try:
		raw = element.GetCurrentPattern(getattr(U, f"UIA_{name}PatternId"))
		if raw is None:
			return None
		interface = getattr(U, f"IUIAutomation{name}Pattern")
		return raw.QueryInterface(interface)
	except Exception:  # noqa: BLE001 — unsupported is a normal answer
		return None


def _value_text(element: Any, max_text: int = DEFAULT_MAX_TEXT) -> str:
	pattern = _pattern(element, "Value")
	if pattern is not None:
		text = _safe(lambda: pattern.CurrentValue, "")
		if text:
			return str(text)[:max_text]
	name = _safe(lambda: element.CurrentName, "") or ""
	return str(name)[:max_text]


def describe(element: Any, max_text: int = DEFAULT_MAX_TEXT) -> dict[str, Any]:
	"""Return a JSON-ready description shaped like an observed node."""
	control_type = _safe(lambda: element.CurrentControlType)
	name = _safe(lambda: element.CurrentName, "") or ""
	return {
		"role": role_for_uia_control_type(_control_type_name(control_type)),
		"name": str(name)[:max_text],
		"value": _value_text(element, max_text),
		"states": _states(element),
		"location": _bounds(element),
		"automationId": str(_safe(lambda: element.CurrentAutomationId, "") or "")[:max_text],
	}


#: Order in which a located target is activated when no action is named. The
#: default invocation first, then the accessibility escape hatch, then the
#: patterns whose meaning is unambiguous.
_ACTION_ORDER = (
	("Invoke", "invoke"),
	("LegacyIAccessible", "default action"),
	("Toggle", "toggle"),
	("SelectionItem", "select"),
	("ExpandCollapse", "expand"),
)

_ACTION_ALIASES = {
	"invoke": "Invoke",
	"press": "Invoke",
	"click": "Invoke",
	"default": "Invoke",
	"toggle": "Toggle",
	"select": "SelectionItem",
	"expand": "ExpandCollapse",
	"collapse": "ExpandCollapse",
}


def _perform(element: Any, pattern_name: str, what: str) -> None:
	"""Run the action a pattern provides, translating a missing element."""
	U = _uia()
	pattern = _pattern(element, pattern_name)
	if pattern is None:
		raise DomainUnavailableError(
			f"Target does not support {pattern_name}; it cannot {what}",
		)
	try:
		if pattern_name == "Invoke":
			pattern.Invoke()
		elif pattern_name == "LegacyIAccessible":
			pattern.DoDefaultAction()
		elif pattern_name == "Toggle":
			pattern.Toggle()
		elif pattern_name == "SelectionItem":
			pattern.Select()
		elif pattern_name == "ExpandCollapse":
			pattern.Expand()
		else:  # pragma: no cover — the table above is the whole vocabulary
			raise DomainUnavailableError(f"Unknown pattern {pattern_name!r}")
	except Exception as error:  # noqa: BLE001
		raise _translate(error, element) from error


def _hresult_of(error: Exception) -> Optional[int]:
	"""Return the COM status code carried by an exception, if it has one.

	``COMError`` exposes an unsigned ``hresult`` in some comtypes versions and
	only the signed value in ``args[0]`` in others; both mean the same thing.
	"""
	raw = getattr(error, "hresult", None)
	if not isinstance(raw, int) and error.args and isinstance(error.args[0], int):
		raw = error.args[0]
	return raw if isinstance(raw, int) else None


def _translate(error: Exception, element: Any) -> Exception:
	"""Turn a COM failure on a disappeared element into a stale-reference report.

	``UIA_E_ELEMENTNOTAVAILABLE`` (0x80040201) is what a dead element raises; it
	must not look like "the bridge is broken", because the remedy is to locate
	again.
	"""
	code = _hresult_of(error)
	if code is not None and (code & 0xFFFFFFFF) == 0x80040201:
		return ElementUnavailableError(
			"The target element no longer exists; call locate again",
			details={"hresult": hex(code & 0xFFFFFFFF)},
		)
	return DomainUnavailableError(
		f"UIA action failed: {type(error).__name__}: {error}",
		details={"hresult": hex(code & 0xFFFFFFFF) if code is not None else None},
	)


def invoke(element: Any, action_name: Optional[str] = None) -> dict[str, Any]:
	"""Activate a located element, by named pattern or by the default order."""
	available = [
		pattern for pattern, _ in _ACTION_ORDER if _pattern(element, pattern) is not None
	]
	if action_name:
		normalized = str(action_name).strip().lower().replace(" ", "")
		pattern_name = _ACTION_ALIASES.get(normalized)
		if pattern_name is None:
			raise ValueError(
				f"Unknown action {action_name!r} for a located target; use one of "
				f"{sorted(_ACTION_ALIASES)}"
			)
		_perform(element, pattern_name, normalized)
		used = pattern_name
	else:
		for pattern_name, _description in _ACTION_ORDER:
			try:
				_perform(element, pattern_name, _description)
			except DomainUnavailableError:
				continue
			used = pattern_name
			break
		else:
			raise DomainUnavailableError(
				"Target exposes none of Invoke, LegacyIAccessible, Toggle, SelectionItem or ExpandCollapse",
				details={"available": available},
			)

	logHandler.log.debug(f"bridge.uia.invoke pattern={used}")
	return {"index": -1, "actionName": used, "available": available}


def set_focus(element: Any) -> dict[str, Any]:
	"""Move keyboard focus to a located element."""
	try:
		element.SetFocus()
	except Exception as error:  # noqa: BLE001
		raise _translate(error, element) from error
	return {"requested": True}


def set_value(element: Any, value: str) -> dict[str, Any]:
	"""Assign a value through the platform, with no keystrokes involved."""
	pattern = _pattern(element, "Value")
	if pattern is not None:
		if _safe(lambda: pattern.CurrentIsReadOnly) is True:
			raise DomainUnavailableError("Control is read-only")
		try:
			pattern.SetValue(value)
		except Exception as error:  # noqa: BLE001
			raise _translate(error, element) from error
		return {"mode": "platform", "value": value}

	legacy = _pattern(element, "LegacyIAccessible")
	if legacy is not None:
		try:
			legacy.SetValue(value)
		except Exception as error:  # noqa: BLE001
			raise _translate(error, element) from error
		return {"mode": "platform", "value": value}

	raise DomainUnavailableError(
		"Target exposes no Value or LegacyIAccessible pattern; it cannot take a value",
	)


def current_value(element: Any) -> str:
	"""The element's value, read back through the same pattern ``set_value`` writes.

	Reading back is not a formality: a browser exposes the *placeholder* through
	``ValuePattern.CurrentValue`` on an empty field, so a write that silently did
	nothing can look, at a glance, like a write that produced the hint text. The
	caller compares this against what it asked for and decides.
	"""
	pattern = _pattern(element, "Value")
	if pattern is not None:
		return str(_safe(lambda: pattern.CurrentValue, "") or "")
	legacy = _pattern(element, "LegacyIAccessible")
	if legacy is not None:
		return str(_safe(lambda: legacy.CurrentValue, "") or "")
	return ""


def read(element: Any, scope: str = "line", max_chars: int = 4000) -> dict[str, Any]:
	"""Read text from a located element.

	A UIA element has no caret and no text range in this adapter: ``document`` and
	``selection`` both fall back to the element's value, and ``subtree`` to name
	plus value. Reported as ``source: "uia"`` so the caller can tell.
	"""
	if scope not in ("line", "document", "selection", "subtree"):
		raise ValueError(f"Unknown scope: {scope}")
	pattern = _pattern(element, "Value")
	text = ""
	if pattern is not None:
		text = str(_safe(lambda: pattern.CurrentValue, "") or "")
	if not text:
		text = str(_safe(lambda: element.CurrentName, "") or "")
	if scope == "subtree":
		name = str(_safe(lambda: element.CurrentName, "") or "")
		parts = [part for part in (name, text) if part]
		text = "\n".join(dict.fromkeys(parts))
	if max_chars > 0:
		truncated = len(text) > max_chars
		text = text[:max_chars]
	else:
		truncated = False
	return {"text": text, "scope": scope, "source": "uia", "truncated": truncated}


#: Scroll pattern amounts, as the direction/amount vocabulary the tool exposes.
_SCROLL_AMOUNT = {"page": "LargeIncrement", "line": "SmallIncrement"}


def scroll(element: Any, direction: str, amount: str = "page") -> dict[str, Any]:
	"""Scroll a located element through the Scroll pattern, when it has one."""
	U = _uia()
	pattern = _pattern(element, "Scroll")
	if pattern is None:
		raise DomainUnavailableError("Target does not support the Scroll pattern")
	step = getattr(U, "UIA_ScrollAmount_" + _SCROLL_AMOUNT.get(amount, "LargeIncrement"))
	none = U.UIA_ScrollAmount_NoAmount
	horizontal, vertical = none, none
	if direction in ("up", "down"):
		vertical = step
	elif direction in ("left", "right"):
		horizontal = step
	else:
		raise ValueError(f"Unknown scroll direction: {direction}")
	try:
		pattern.Scroll(horizontal, vertical)
	except Exception as error:  # noqa: BLE001
		raise _translate(error, element) from error
	return {"direction": direction, "amount": amount, "method": "uia", "key": ""}
