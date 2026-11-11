"""Route an action to the right primitive for the object a ref resolved to.

Two kinds of target now exist, and they need different APIs:

* an **NVDA object** — what ``observe`` hands out, acted on through
  ``nvda_bridge.actions`` (``doAction``, ``setFocus``, text infos);
* a **UIA element** — what ``locate`` hands out, acted on through
  ``nvda_bridge.uia`` (Invoke/Value/Toggle patterns).

The caller should not have to care which one a reference points at, and the tool
contract does not change: ``invoke`` is still "activate this", and it still
reports what it did. Keeping the branch here means the handlers stay one line per
verb and neither backend grows knowledge of the other.
"""

from __future__ import annotations

from typing import Any, Optional

from . import actions, uia
from .errors import DomainUnavailableError


def invoke(target: Any, action_name: Optional[str] = None) -> dict[str, Any]:
	"""Activate a target, by named action when one is given."""
	if uia.is_element(target):
		return uia.invoke(target, action_name)
	return actions.do_action(target, action_name)


def set_focus(target: Any) -> dict[str, Any]:
	"""Move keyboard focus to a target."""
	if uia.is_element(target):
		return uia.set_focus(target)
	return actions.set_focus(target)


def set_value(target: Any, value: str, force_keyboard: bool = False) -> dict[str, Any]:
	"""Assign a value, preferring a platform write over keystrokes."""
	if uia.is_element(target):
		return uia.set_value(target, value)
	return actions.set_value(target, value, force_keyboard=force_keyboard)


def read_text(target: Any, scope: str = "line", max_chars: int = 4000) -> dict[str, Any]:
	"""Read text from a target at the requested scope."""
	if uia.is_element(target):
		return uia.read(target, scope=scope, max_chars=max_chars)
	return actions.read_text(target, scope=scope, max_chars=max_chars)


def read_value(target: Any) -> dict[str, Any]:
	"""Read a control's current value, for verifying a write.

	Deliberately reads the *live object* rather than a cached projection: the
	point is to find out whether the write reached the control, and a projection
	taken before the action would answer a different question.

	``source`` says which view answered: ``uia-value`` (the platform value
	pattern), ``value`` (the object's own value attribute) or ``text`` (a text
	range, used when a control has a value attribute that is not a string).
	"""
	if uia.is_element(target):
		return {"value": uia.current_value(target), "source": "uia-value"}

	value = getattr(target, "value", None)
	if isinstance(value, str):
		return {"value": value, "source": "value"}

	text = actions.read_text(target, scope="document", max_chars=0)
	return {"value": str(text.get("text", "") or ""), "source": "text"}


def scroll(target: Any, direction: str, amount: str = "page") -> dict[str, Any]:
	"""Scroll a target, through the platform when it is a UIA element."""
	if uia.is_element(target):
		return uia.scroll(target, direction, amount)
	return actions.scroll(target, direction, amount)


def navigate(target: Any, direction: str) -> tuple[Any, bool]:
	"""Move to a tree neighbour.

	A located element has no NVDA tree around it, so this refuses rather than
	pretending: the right move is another ``locate`` or ``observe``.
	"""
	if uia.is_element(target):
		raise DomainUnavailableError(
			"A located target has no NVDA tree neighbours; call observe or locate again",
			details={"direction": direction},
		)
	return actions.navigate(target, direction)
