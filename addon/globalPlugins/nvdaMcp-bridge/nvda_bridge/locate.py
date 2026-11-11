"""Find a control by role and name (implementation plan P5).

The observation model reaches the focus, its ancestors, its children and the open
dialog. That is everything needed to *act*, and nothing needed to *find*: a
control outside that neighbourhood could previously only be reached by pressing
Tab and hoping. This module searches.

Two backends, one match rule (``server/locate.py``):

``uia``
	Searches with the application's own UIA provider. One call answers for the
	whole window: measured on an Edge window holding 2,346 elements, a full
	``FindAll`` took 65 ms and a role + name search took ~12 ms. The matches are
	``IUIAutomationElement`` objects and are acted on through ``nvda_bridge.uia``.

``walk``
	Breadth-first through NVDA objects, bounded by a node cap and a time budget.
	This is the fallback for applications with no usable UIA provider (or a role
	with no UIA equivalent). It is honest but slow: measured at ~4.8 ms per node
	on Chromium, because every ``children`` call crosses a process boundary, so
	the budget is what stops it. The reply says which backend answered.

The root defaults to the dialog containing the focus when one is open (a modal
search must not silently reach the frame behind it), otherwise the nearest
application window.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Optional

import api
import logHandler

from ..server.locate import name_matches, role_matches, uia_control_type_name
from ..server.state import MAX_ANCESTORS, _enum_name, _safe_get, describe
from . import uia
from .observation import _children_of, _find_dialog, _is_desktop

#: Text kept per field in a match. A located node is a candidate, not a read: the
#: caller follows up with ``read`` when it wants the content.
MATCH_TEXT = 120

#: Children examined per node by the walk. The cap exists so one wide container
#: cannot eat the whole node budget on its own.
WALK_CHILDREN = 12

#: Depth cap for the walk. A browser page nests deeply; this is a safety net under
#: the node and time budgets rather than the primary bound.
WALK_DEPTH = 20


def _scope_label(root: Any) -> str:
	"""Describe where a search started, for the reply's ``scope`` field."""
	if uia.is_element(root):
		described = uia.describe(root, MATCH_TEXT)
		return f"{described['role']} {described['name']!r}".strip()
	role = _enum_name(_safe_get(root, "role"))
	name = _safe_get(root, "name") or ""
	return f"{role} {name!r}".strip()


def _resolve_root(within: Any) -> Any:
	"""Return the object to search under.

	An explicit ``within`` wins. Otherwise, in order:

	1. the containing dialog, when the focus sits in an owned window;
	2. the foreground window — which is where a *top-level* dialog lives, and is
	   the only way to reach one, since it is not an ancestor of the focus;
	3. the nearest non-desktop window ancestor of the focus;
	4. the foreground object, whatever it is.

	One upward walk answers 1 and 3. Walking twice (once for the dialog, once for
	the window) cost ~400 ms on a browser before the search had even started,
	because every ``parent`` read crosses into the renderer.
	"""
	if within is not None:
		return within

	focus = api.getFocusObject()
	chain: list[Any] = []
	node = focus
	for _ in range(MAX_ANCESTORS):
		node = _safe_get(node, "parent") if node is not None else None
		if node is None:
			break
		chain.append(node)

	if focus is not None:
		dialog = _find_dialog(focus, chain)
		if dialog is not None:
			return dialog

	foreground = api.getForegroundObject()
	if foreground is not None and _enum_name(_safe_get(foreground, "role")) in ("WINDOW", "DIALOG"):
		if not _is_desktop(foreground):
			return foreground

	for candidate in chain:
		if _enum_name(_safe_get(candidate, "role")) == "WINDOW" and not _is_desktop(candidate):
			return candidate
	return foreground if foreground is not None else focus


def _root_element(root: Any) -> tuple[Any, bool]:
	"""Return ``(element, widened)`` for the search root, or ``(None, False)``.

	``widened`` is true when the root had no UIA representation of its own and the
	search was pushed up to its containing window — which is a wider scope than the
	caller asked for, and is reported rather than hidden. A window reached through
	its handle is *not* widened: that is simply how a window's element is obtained.
	"""
	if uia.is_element(root):
		return root, False

	element = getattr(root, "UIAElement", None)
	if element is not None:
		return element, False

	handle = _safe_get(root, "windowHandle")
	if isinstance(handle, int) and not isinstance(handle, bool) and handle:
		try:
			client = uia.module().handler.clientObject
			is_window = _enum_name(_safe_get(root, "role")) in ("WINDOW", "DIALOG")
			return client.ElementFromHandle(handle), not is_window
		except Exception as error:  # noqa: BLE001 — fall back to the walk
			logHandler.log.debug(f"bridge.locate.element_from_handle_failed {type(error).__name__}: {error}")
	return None, False


def _condition(client: Any, role: Optional[str], name: Optional[str], exact: bool) -> Any:
	"""Build the UIA condition, or ``None`` when the role cannot be expressed."""
	U = uia.module()
	parts = []

	if role:
		type_name = uia_control_type_name(role)
		if type_name is None:
			return None
		control_type = getattr(U, f"UIA_{type_name}ControlTypeId", None)
		if control_type is None:
			return None
		parts.append(client.CreatePropertyCondition(U.UIA_ControlTypePropertyId, control_type))

	if name:
		flags = U.PropertyConditionFlags_IgnoreCase
		if not exact:
			flags |= U.PropertyConditionFlags_MatchSubstring
		parts.append(client.CreatePropertyConditionEx(U.UIA_NamePropertyId, name, flags))

	if not parts:
		return client.CreateTrueCondition()
	if len(parts) == 1:
		return parts[0]
	return client.CreateAndCondition(parts[0], parts[1])


def _search_uia(
	root: Any,
	role: Optional[str],
	name: Optional[str],
	exact: bool,
	limit: int,
) -> Optional[tuple[list[dict], list[Any], bool, int]]:
	"""Search with the platform. Returns ``None`` when UIA cannot answer."""
	element, widened = _root_element(root)
	if element is None:
		return None
	if role and uia_control_type_name(role) is None:
		# The walk knows more roles than UIA has control types; do not guess.
		return None

	try:
		U = uia.module()
		client = U.handler.clientObject
		condition = _condition(client, role, name, exact)
		if condition is None:
			return None
		matches = element.FindAll(U.TreeScope_Descendants, condition)
	except Exception as error:  # noqa: BLE001 — no provider, no answer; walk instead
		logHandler.log.debug(f"bridge.locate.uia_failed {type(error).__name__}: {error}")
		return None

	total = int(getattr(matches, "Length", 0) or 0)
	records: list[dict] = []
	objects: list[Any] = []
	for index in range(min(total, limit)):
		try:
			candidate = matches.GetElement(index)
		except Exception:  # noqa: BLE001 — a collection can go stale mid-read
			continue
		if candidate is None:
			continue
		records.append(uia.describe(candidate, MATCH_TEXT))
		objects.append(candidate)
	return records, objects, widened, total


def _matches(node: Any, role: Optional[str], name: Optional[str], exact: bool) -> bool:
	if role and not role_matches(role, _enum_name(_safe_get(node, "role"))):
		return False
	if name and not name_matches(name, _safe_get(node, "name"), exact):
		return False
	return True


def _walk_record(node: Any, depth: int, trail: tuple[str, ...]) -> dict:
	described = describe(node, max_value=MATCH_TEXT)
	return {
		"role": described["role"],
		"name": described["name"] or "",
		"value": described["value"] or "",
		"states": described["states"],
		"location": described["location"] or [],
		"automationId": "",
		"depth": depth,
		"path": list(trail),
	}


def _trail_label(node: Any) -> str:
	role = _enum_name(_safe_get(node, "role"))
	name = _safe_get(node, "name") or ""
	return f"{role}:{name}"[:60] if name else role


def _search_walk(
	root: Any,
	role: Optional[str],
	name: Optional[str],
	exact: bool,
	limit: int,
	max_nodes: int,
	timeout_ms: int,
) -> tuple[list[dict], list[Any], int, bool]:
	"""Bounded breadth-first search through NVDA objects."""
	deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
	queue: deque[tuple[Any, int, tuple[str, ...]]] = deque([(root, 0, ())])
	records: list[dict] = []
	objects: list[Any] = []
	visited = 0
	truncated = False

	while queue:
		if visited >= max_nodes or time.monotonic() >= deadline:
			truncated = True
			break
		node, depth, trail = queue.popleft()
		visited += 1

		if depth > 0 and _matches(node, role, name, exact):
			records.append(_walk_record(node, depth, trail))
			objects.append(node)
			if len(objects) >= limit:
				truncated = bool(queue)
				break

		if depth >= WALK_DEPTH:
			continue
		for child in _children_of(node, WALK_CHILDREN):
			queue.append((child, depth + 1, (trail + (_trail_label(node),))[-4:]))

	return records, objects, visited, truncated


def find(
	role: Optional[str] = None,
	name: Optional[str] = None,
	exact: bool = False,
	within: Any = None,
	limit: int = 10,
	max_nodes: int = 400,
	timeout_ms: int = 500,
) -> tuple[dict[str, Any], list[Any]]:
	"""Locate controls; return ``(payload, objects)`` where ``objects[i]`` backs match ``i``.

	``payload`` is JSON-ready except that the caller still has to attach a ``ref``
	to each match, which it does by registering ``objects`` in the same order.
	"""
	started = time.monotonic()
	root = _resolve_root(within)
	scope = _scope_label(root)

	result = _search_uia(root, role, name, exact, limit)
	if result is not None:
		records, objects, widened, total = result
		method = "uia-window" if widened else "uia"
		searched = total
		truncated = total > len(records)
		for record in records:
			record.setdefault("depth", -1)
			record.setdefault("path", [])
			record["target"] = "uia"
	else:
		records, objects, visited, truncated = _search_walk(
			root, role, name, exact, limit, max_nodes, timeout_ms
		)
		method = "walk"
		searched = visited
		for record in records:
			record["target"] = "nvda"

	elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
	logHandler.log.debug(
		f"bridge.locate method={method} matches={len(records)} searched={searched} elapsed_ms={elapsed_ms}"
	)
	return {
		"matches": records,
		"method": method,
		"searched": int(searched),
		"truncated": bool(truncated),
		"elapsed_ms": elapsed_ms,
		"scope": scope,
	}, objects
