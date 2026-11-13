"""Action primitives: the verbs an agent needs, without synthesising keystrokes.

Measured facts that shape this module (`docs/IMPLEMENTATION_PLAN.md` §3, §4):

* ``doAction()`` on an unfocused button works and the page reacts (0.2 ms), so
  ``invoke`` does not need to focus first.
* A Chromium text field has **no write path**: no ``value`` setter, no
  ``IAccessibleEditableText``, no ``UIAElement``. UIA applications do expose
  ``ValuePattern``, so ``set_value`` tries the platform first and falls back to
  "focus, select all, type" — and reports which one it used rather than
  pretending they are the same thing.
* References resolve to captured objects; nothing here searches by name.

Every function must run on NVDA's main thread. None of them waits for the UI to
settle — that is ``server/settle.py``'s job, driven by the coordinator handler.
"""

from __future__ import annotations

from typing import Any, Optional

import api
import logHandler
import textInfos

from ..server.state import _enum_name, _safe_get, describe
from . import ui as nvda_ui
from . import win_input
from .errors import DomainUnavailableError

#: Text read from a subtree is a bounded walk: every ``children`` call crosses a
#: process boundary (~9 ms per node on Chromium), so the cap is small.
MAX_SUBTREE_NODES = 25
MAX_SUBTREE_DEPTH = 2


def identity_of(obj: Any) -> dict[str, Any]:
	"""Return the fields used to recognise ``obj`` again.

	Read deliberately through ``_safe_get``: the point of checking identity is to
	find out that an object died, and ``getattr`` raising is a valid answer.
	"""
	return {
		"role": _enum_name(_safe_get(obj, "role")),
		"name": _safe_get(obj, "name"),
		"windowClassName": _safe_get(obj, "windowClassName"),
		"windowControlID": _safe_get(obj, "windowControlID"),
	}


def _invalidate(obj: Any) -> None:
	invalidate = getattr(obj, "invalidateCache", None)
	if callable(invalidate):
		try:
			invalidate()
		except Exception:  # noqa: BLE001
			pass


def _action_names(obj: Any) -> list[str]:
	try:
		count = int(getattr(obj, "actionCount", 0) or 0)
	except Exception:  # noqa: BLE001
		count = 0
	names: list[str] = []
	for index in range(count):
		try:
			names.append(str(obj.getActionName(index) or ""))
		except Exception:  # noqa: BLE001
			names.append("")
	return names


def do_action(obj: Any, action_name: Optional[str] = None) -> dict[str, Any]:
	"""Invoke an object's default action, or the one named.

	``action_name`` is matched case-insensitively against ``getActionName(i)``.
	A name the object does not implement is a caller error with the available
	names attached, not a silent default.
	"""
	_invalidate(obj)
	names = _action_names(obj)
	try:
		default_index = int(getattr(obj, "defaultActionIndex", -1))
	except Exception:  # noqa: BLE001
		default_index = -1

	if action_name is not None:
		normalized = action_name.strip().lower()
		index = next((i for i, name in enumerate(names) if name.lower() == normalized), None)
		if index is None:
			raise ValueError(
				f"Object has no action named {action_name!r}; available actions: {names or 'none'}"
			)
	elif default_index >= 0:
		index = default_index
	elif names:
		index = 0
	else:
		raise DomainUnavailableError(
			"Object exposes no actions; try press or click_at instead",
			details={"role": _enum_name(_safe_get(obj, "role"))},
		)

	try:
		obj.doAction(index)
	except Exception as error:  # noqa: BLE001
		raise DomainUnavailableError(
			f"Invoking action {index} failed: {type(error).__name__}: {error}",
			details={"index": index, "actions": names},
		) from error

	logHandler.log.debug(f"bridge.invoke index={index} name={names[index] if index < len(names) else ''}")
	return {
		"index": int(index),
		"actionName": names[index] if index < len(names) else "",
		"available": names,
	}


def set_focus(obj: Any) -> dict[str, Any]:
	"""Move keyboard focus to ``obj``.

	``setFocus()`` posts an asynchronous Windows message, so this reports that
	focus *was requested*; the settle round that follows is what confirms it.
	"""
	set_focus_method = getattr(obj, "setFocus", None)
	if not callable(set_focus_method):
		raise DomainUnavailableError(
			f"Object cannot take focus (role {_enum_name(_safe_get(obj, 'role'))})",
		)
	try:
		set_focus_method()
	except Exception as error:  # noqa: BLE001
		raise DomainUnavailableError(f"setFocus failed: {type(error).__name__}: {error}") from error
	return {"requested": True}


def _uia_set_value(obj: Any, value: str) -> bool:
	"""Try UIA ``ValuePattern.SetValue``. Returns False when unavailable."""
	element = getattr(obj, "UIAElement", None)
	if element is None:
		return False
	try:
		import UIAHandler

		pattern = element.GetCurrentPattern(UIAHandler.UIA_ValuePatternId)
		if pattern is None:
			return False
		value_pattern = pattern.QueryInterface(UIAHandler.IUIAutomationValuePattern)
		if value_pattern is None:
			return False
		if bool(value_pattern.CurrentIsReadOnly):
			raise DomainUnavailableError("Control is read-only")
		value_pattern.SetValue(value)
		return True
	except DomainUnavailableError:
		raise
	except Exception as error:  # noqa: BLE001 — no pattern is a normal outcome
		logHandler.log.debug(f"bridge.set_value.uia_unavailable {type(error).__name__}: {error}")
		return False


def set_value(obj: Any, value: str, force_keyboard: bool = False) -> dict[str, Any]:
	"""Set an object's value by platform call when possible, keyboard otherwise.

	``force_keyboard`` skips the platform attempt. Callers use it for the retry
	after a platform write that the control accepted without applying: some
	editors expose ``ValuePattern`` but ignore ``SetValue``, and keystrokes are
	the only thing that actually moves them.
	"""
	_invalidate(obj)
	if not force_keyboard and _uia_set_value(obj, value):
		return {"mode": "platform", "value": value}

	# Keyboard path. Documented as such on the wire: it is focus + select-all +
	# type, not an atomic assignment, and it can be observed in progress.
	set_focus(obj)
	nvda_ui.send_single_gesture("control+a")
	if value == "":
		# Clearing a field is a normal write, not a typing accident: send_text
		# refuses the empty string, so the selection is deleted instead.
		nvda_ui.send_single_gesture("delete")
		return {"mode": "keyboard", "value": "", "units": 0}
	typed = win_input.send_text(value)
	return {"mode": "keyboard", "value": value, "units": typed}


_POSITION_FOR_SCOPE = {
	"line": textInfos.POSITION_CARET,
	"selection": textInfos.POSITION_SELECTION,
	"document": textInfos.POSITION_ALL,
}


def read_text(obj: Any, scope: str = "line", max_chars: int = 4000) -> dict[str, Any]:
	"""Read text from an object for a given scope.

	``document`` is deliberately unbounded by the platform but truncated here:
	a whole browser document was 86,134 characters, which no model should be
	handed by accident.
	"""
	_invalidate(obj)
	if scope == "subtree":
		text = _subtree_text(obj)
		return {
			"text": text[:max_chars] if max_chars > 0 else text,
			"scope": scope,
			"truncated": len(text) > max_chars > 0,
			"source": "subtree",
		}

	position = _POSITION_FOR_SCOPE.get(scope)
	if position is None:
		raise ValueError(f"Unknown scope: {scope}")
	try:
		info = obj.makeTextInfo(position)
		if scope == "line":
			info.expand(textInfos.UNIT_LINE)
		text = info.text or ""
		source = "textinfo"
	except (NotImplementedError, AttributeError, RuntimeError):
		text = ""
		source = "fallback"
	# Some WebView/Chromium documents answer with only object-replacement
	# characters (U+FFFC) even though their accessibility tree has the text. A
	# silent "￼" is worse than no answer, so fall back to the tree and say so.
	if not text.strip() or all(char in "\ufffc\r\n\t " for char in text):
		tree_text = _subtree_text(obj)
		if tree_text.strip():
			text = tree_text
			source = "tree"
		else:
			text = ""
			source = "unavailable"
	elif source == "fallback":
		text = str(_safe_get(obj, "name") or _safe_get(obj, "value") or "")
	return {
		"text": text[:max_chars] if max_chars > 0 else text,
		"scope": scope,
		"truncated": len(text) > max_chars > 0,
		"source": source,
	}


def _subtree_text(root: Any) -> str:
	"""Bounded depth-first collection of names and values under ``root``."""
	parts: list[str] = []
	visited = 0
	stack: list[tuple[Any, int]] = [(root, 0)]
	while stack and visited < MAX_SUBTREE_NODES:
		node, depth = stack.pop(0)
		visited += 1
		name = _safe_get(node, "name")
		value = _safe_get(node, "value")
		if name:
			parts.append(str(name))
		if value and value != name:
			parts.append(str(value))
		if depth >= MAX_SUBTREE_DEPTH:
			continue
		try:
			count = int(getattr(node, "childCount", 0) or 0)
		except Exception:  # noqa: BLE001
			count = 0
		for index in range(min(count, 10)):
			try:
				child = node.getChild(index)
			except Exception:  # noqa: BLE001
				continue
			if child is not None:
				stack.append((child, depth + 1))
	return "\n".join(parts)


def navigate(obj: Any, direction: str) -> tuple[Any, bool]:
	"""Resolve a tree neighbour and, when it can take focus, focus it.

	This walks one step in the accessibility tree, which is what NVDA exposes
	(``next``/``previous``/``parent``/``child``); it is not tab order.
	"""
	_invalidate(obj)
	if direction in ("next", "previous"):
		target = _safe_get(obj, direction)
	elif direction == "parent":
		target = _safe_get(obj, "parent")
	elif direction in ("first", "child"):
		target = _first_child(obj)
	elif direction == "last":
		target = _last_child(obj)
	else:
		raise ValueError(f"Unknown direction: {direction}. Use next, previous, first, last, parent or child")

	if target is None:
		raise DomainUnavailableError(
			f"No {direction} object available from the current focus",
			details={"direction": direction, "role": _enum_name(_safe_get(obj, "role"))},
		)

	focused = False
	set_focus_method = getattr(target, "setFocus", None)
	if callable(set_focus_method):
		try:
			set_focus_method()
			focused = True
		except Exception:  # noqa: BLE001 — some tree neighbours are not focusable
			focused = False
	return target, focused


def _first_child(obj: Any) -> Any:
	try:
		count = int(getattr(obj, "childCount", 0) or 0)
	except Exception:  # noqa: BLE001
		return None
	for index in range(min(count, 10)):
		try:
			child = obj.getChild(index)
		except Exception:  # noqa: BLE001
			continue
		if child is not None:
			return child
	return None


def _last_child(obj: Any) -> Any:
	try:
		count = int(getattr(obj, "childCount", 0) or 0)
	except Exception:  # noqa: BLE001
		return None
	for index in range(min(count, 10) - 1, -1, -1):
		try:
			child = obj.getChild(index)
		except Exception:  # noqa: BLE001
			continue
		if child is not None:
			return child
	return None


_SCROLL_KEYS = {
	("up", "line"): "up",
	("down", "line"): "down",
	("left", "line"): "left",
	("right", "line"): "right",
	("up", "page"): "pageup",
	("down", "page"): "pagedown",
	("left", "page"): "left",
	("right", "page"): "right",
}


def scroll(obj: Any, direction: str, amount: str = "page") -> dict[str, Any]:
	"""Scroll by synthesising the conventional scroll key.

	NVDA exposes ``scrollIntoView`` on objects but not a portable "scroll this
	container by N lines" call, so the honest mechanism is the keyboard. Focus is
	requested first so the key reaches the intended control.
	"""
	key = _SCROLL_KEYS.get((direction, amount))
	if key is None:
		raise ValueError(f"Unknown scroll direction/amount: {direction}/{amount}")
	try:
		set_focus_method = getattr(obj, "setFocus", None)
		if callable(set_focus_method):
			set_focus_method()
	except Exception:  # noqa: BLE001
		pass
	nvda_ui.send_single_gesture(key)
	return {"direction": direction, "amount": amount, "method": "keyboard", "key": key}


def click_at(x: int, y: int, button: str = "left") -> dict[str, Any]:
	"""Click absolute screen coordinates. Last resort when nothing is actionable."""
	if int(x) < -100000 or int(y) < -100000:
		raise ValueError("'x' and 'y' must be screen coordinates")
	hit = None
	try:
		hit = api.getDesktopObject().objectFromPoint(x, y)
	except Exception:  # noqa: BLE001
		hit = None
	cursor_x, cursor_y, on_target = win_input.click(int(x), int(y), button)
	return {
		"x": int(x),
		"y": int(y),
		"button": button,
		"hit": describe(hit) if hit is not None else None,
		"cursor": [cursor_x, cursor_y],
		"onTarget": on_target,
	}


def dialog_object() -> Any:
	"""Return the dialog containing the focus, or None.

	Uses the same structural rule as ``observe``: a window nested inside another
	window is an owned window (a dialog), which is how Notepad's Replace box
	presents itself — a ``WINDOW`` named "Pop-up" inside the frame, with no
	``DIALOG`` role anywhere in the chain.
	"""
	from .observation import find_dialog

	return find_dialog()


_SAFE_DIALOG_DISMISS_LABELS = {"cancel", "no", "close", "abort", "stop"}


def cancel_dialog_button(dialog: Any) -> Any:
	"""Return a clearly non-affirmative dialog button, if one is available.

	Never fall back to a dialog's default button: on confirmation prompts that
	button is commonly the affirmative action (for example, Yes/Install).
	"""
	from .observation import dialog_children

	for child in dialog_children(dialog, 20):
		role = _enum_name(_safe_get(child, "role"))
		if role not in ("BUTTON", "PUSH_BUTTON", "SPLIT_BUTTON"):
			continue
		label = _safe_get(child, "name")
		if not isinstance(label, str):
			continue
		# Strip the UI accelerator marker and common trailing ellipses, but
		# require an exact label so labels such as "No to All" are not guessed.
		normalized = label.replace("&", "").strip().casefold().rstrip(" .…")
		if normalized in _SAFE_DIALOG_DISMISS_LABELS:
			return child
	return None
