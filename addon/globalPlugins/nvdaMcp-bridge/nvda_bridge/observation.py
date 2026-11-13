"""Capture the current observation from NVDA, with references to what was seen.

This is the only module that reads the screen. `server/state.py` is the pure half
(describe/diff/versioning) and stays importable outside NVDA; everything here runs
on NVDA's main thread and may fail on a dead object or a hung application.

Three properties are load-bearing, each from a measurement in
`docs/IMPLEMENTATION_PLAN.md`:

* **Invalidate before reading.** NVDA caches properties per object and nothing
  invalidates them behind its back: a checkbox that had just been toggled still
  reported its old `states`. A cached read costs 0.01 ms and is wrong; an
  invalidated read costs ~14 ms in Edge and is right.
* **Never walk.** Resolving a control by scanning children costs ~9 ms per node
  because every call crosses into the browser process; a 500-item page blew the
  main-thread budget and froze NVDA. So a capture records the focus, a bounded
  ancestor chain and a bounded neighbourhood, and hands back the objects
  themselves as references.
* **Bound the neighbourhood.** At most `MAX_ANCESTORS + MAX_CHILDREN +
  MAX_DIALOG_CHILDREN` nodes, so one observation has a predictable cost.
"""

from __future__ import annotations

import api
import logHandler

from ..server.state import MAX_ANCESTORS, describe, _enum_name, _safe_get

#: How many immediate children of the focus to expose as references.
MAX_CHILDREN = 10

#: How many children of the containing dialog to expose. This is what makes
#: ``dismiss`` and dialog automation possible without a tree walk. Fourteen,
#: because a Windows Find/Replace dialog spends ten controls before it reaches
#: the action buttons.
MAX_DIALOG_CHILDREN = 14


def _invalidate(obj: object) -> None:
	"""Drop NVDA's cached properties for one object, best effort."""
	invalidate = getattr(obj, "invalidateCache", None)
	if callable(invalidate):
		try:
			invalidate()
		except Exception:  # noqa: BLE001 — a stale read is better than a crash
			pass


def _children_of(obj: object, limit: int) -> list:
	"""Return up to ``limit`` immediate children, tolerating either API shape."""
	if limit <= 0:
		return []
	children: list = []
	try:
		count = int(getattr(obj, "childCount", 0) or 0)
	except Exception:  # noqa: BLE001
		count = 0
	get_child = getattr(obj, "getChild", None)
	if count > 0 and callable(get_child):
		for index in range(min(count, limit)):
			try:
				child = get_child(index)
			except Exception:  # noqa: BLE001
				continue
			if child is not None:
				children.append(child)
		return children
	try:
		for child in getattr(obj, "children", None) or ():
			if child is not None:
				children.append(child)
			if len(children) >= limit:
				break
	except Exception:  # noqa: BLE001
		return children
	return children


def _is_desktop(node: object) -> bool:
	"""Whether ``node`` is NVDA's desktop root.

	The desktop object reports ``role=WINDOW`` and ``name="Desktop"`` with the
	shell's window class, so a role check alone would mistake it for an
	application window and label every observation "Desktop".
	"""
	return _safe_get(node, "name") == "Desktop" or _safe_get(node, "windowClassName") == "#32769"


def _find_dialog(focus: object, ancestors: list) -> object | None:
	"""Return the dialog containing the focus, when there is one.

	Not every dialog reports ``role=DIALOG``: Notepad's Replace box is an owned
	``WINDOW`` named "Pop-up" nested inside the application window. So the rule is
	structural — a window *inside another application window*, with a different
	name, is an owned window (a dialog or a popup), while the outermost window is
	the application frame. The desktop is not an application window and is skipped.
	"""
	nodes = [focus, *ancestors]
	for index, candidate in enumerate(nodes):
		role = _enum_name(_safe_get(candidate, "role"))
		if role == "DIALOG":
			return candidate
		if role != "WINDOW" or _is_desktop(candidate):
			continue
		outer_node = next(
			(
				node
				for node in nodes[index + 1 :]
				if _enum_name(_safe_get(node, "role")) == "WINDOW" and not _is_desktop(node)
			),
			None,
		)
		if outer_node is None:
			# The outermost application window: the frame, not a dialog.
			return None
		outer_name = _safe_get(outer_node, "name")
		inner_name = _safe_get(candidate, "name")
		if inner_name and inner_name != outer_name:
			return candidate
		return None
	return None


def _node(obj: object, ref: str, max_text: int, include_states: bool = True) -> dict:
	described = describe(obj, max_value=max_text, include_states=include_states)
	described["ref"] = ref
	return described


def _window(primary: object, ancestors: list) -> tuple[dict | None, object | None]:
	"""Describe the top-level application window: the last non-desktop ``WINDOW``.

	The *last* such window, not the nearest: a dialog is also a window, and the
	useful label for "where am I" is the application frame, not the popup.
	"""
	nodes = [primary, *ancestors]
	chosen = None
	for candidate in nodes:
		if _enum_name(_safe_get(candidate, "role")) == "WINDOW" and not _is_desktop(candidate):
			chosen = candidate
	if chosen is not None:
		handle = _safe_get(chosen, "windowHandle")
		return (
			{
				"ref": "w",
				"title": _safe_get(chosen, "name") or "",
				"handle": int(handle) if isinstance(handle, int) else 0,
				"class_name": _safe_get(chosen, "windowClassName") or "",
			},
			chosen,
		)
	try:
		from . import ui as nvda_ui
		from .windows import object_for_handle

		foreground = nvda_ui.get_window_title()
		window_object = object_for_handle(foreground.get("handle", 0))
	except Exception:  # noqa: BLE001
		return None, None
	return (
		{
			"ref": "w" if window_object is not None else "",
			"title": foreground.get("title", ""),
			"handle": foreground.get("handle", 0),
			"class_name": foreground.get("className", ""),
		},
		window_object,
	)


#: Container roles a dialog wraps its controls in. A dialog's immediate children
#: are usually one or two groupings, not the buttons an agent needs, so the
#: capture descends through them to depth two.
_DIALOG_CONTAINER_ROLES = {"GROUPING", "PANE", "PROPERTYPAGE", "PAGE", "SECTION"}


def dialog_children(dialog: object, limit: int = MAX_DIALOG_CHILDREN) -> list:
	"""Return a dialog's actionable descendants, flattening container groupings.

	Depth is capped at two levels and the result at ``limit``: this is a bounded
	walk that only happens while a dialog is open, and it is what makes ``invoke``
	work on a dialog's buttons instead of on the grouping around them.
	"""
	queue: list[tuple] = [(child, 0) for child in _children_of(dialog, 20)]
	found: list = []
	while queue and len(found) < limit:
		node, depth = queue.pop(0)
		role = _enum_name(_safe_get(node, "role"))
		if role in _DIALOG_CONTAINER_ROLES and depth < 2:
			queue.extend((child, depth + 1) for child in _children_of(node, 10))
			continue
		found.append(node)
	return found


def find_dialog(max_ancestors: int = 8) -> object | None:
	"""Return the dialog containing the current focus, if any.

	Shared with the action layer so that "is there a dialog" means the same thing
	when observing and when dismissing.
	"""
	focus = api.getFocusObject()
	if focus is None:
		return None
	ancestors: list = []
	node = focus
	for _ in range(max(0, max_ancestors)):
		node = _safe_get(node, "parent")
		if node is None:
			break
		ancestors.append(node)
	return _find_dialog(focus, ancestors)


def capture(
	max_ancestors: int = MAX_ANCESTORS,
	max_children: int = MAX_CHILDREN,
	max_text: int = 200,
	include_children: bool = True,
	include_selection: bool = False,
	include_states: bool = True,
) -> tuple[dict, dict]:
	"""Return ``(projection, objects_by_ref)`` for the current focus.

	``projection`` is JSON-ready and is what `StateProjector` stores and diffs.
	``objects_by_ref`` holds the live NVDA objects behind each ``ref``; it is kept
	by the reference table and never serialized.
	"""
	focus = api.getFocusObject()
	if focus is None:
		return {
			"focus": None,
			"ancestors": [],
			"children": [],
			"dialog": None,
			"top_window": None,
		}, {}

	# Invalidate the focus and its ancestors *before* reading anything: reads
	# below share these objects, and NVDA's per-object cache does not notice a
	# change made by another process.
	objects: dict[str, object] = {}
	_invalidate(focus)

	ancestors: list = []
	node = focus
	for _ in range(max(0, max_ancestors)):
		node = _safe_get(node, "parent")
		if node is None:
			break
		_invalidate(node)
		ancestors.append(node)

	projection: dict = {
		"focus": _node(focus, "f", max_text, include_states),
		"ancestors": [
			_node(ancestor, f"a{index}", max_text, include_states)
			for index, ancestor in enumerate(ancestors, 1)
		],
		"children": [],
		"dialog": None,
		"top_window": None,
	}
	objects["f"] = focus
	for index, ancestor in enumerate(ancestors, 1):
		objects[f"a{index}"] = ancestor

	if include_children:
		for index, child in enumerate(_children_of(focus, max_children), 1):
			_invalidate(child)
			ref = f"c{index}"
			projection["children"].append(_node(child, ref, max_text, include_states))
			objects[ref] = child

	dialog = _find_dialog(focus, ancestors)
	if dialog is not None:
		dialog_ref = "f" if dialog is focus else next(
			(ref for ref, obj in objects.items() if obj is dialog),
			None,
		)
		if dialog_ref is None:
			dialog_ref = "d0"
			objects[dialog_ref] = dialog
		dialog_node = _node(dialog, dialog_ref, max_text, include_states)
		dialog_children_projection = []
		for index, child in enumerate(dialog_children(dialog), 1):
			_invalidate(child)
			ref = f"d{index}"
			dialog_children_projection.append(_node(child, ref, max_text, include_states))
			objects[ref] = child
		dialog_node["children"] = dialog_children_projection
		projection["dialog"] = dialog_node

	window, window_object = _window(focus, ancestors)
	projection["top_window"] = window
	if window_object is not None:
		objects["w"] = window_object

	if include_selection:
		projection["selection"] = selection_text(focus, max_text)

	logHandler.log.debug(
		f"bridge.observe nodes={len(objects)} role={projection['focus']['role']}"
	)
	return projection, objects


def selection_text(obj: object, max_chars: int = 200) -> str:
	"""Return the selected text of an object, or an empty string."""
	try:
		import textInfos

		info = obj.makeTextInfo(textInfos.POSITION_SELECTION)
		text = info.text or ""
	except Exception:  # noqa: BLE001 — most objects have no selection
		return ""
	return text[:max_chars] if max_chars > 0 else text
