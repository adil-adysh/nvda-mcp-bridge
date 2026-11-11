from __future__ import annotations

from typing import Any, Literal


def get_current_focus() -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.get_current_focus()


def move_focus(direction: Literal["next", "previous"]) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.move_focus(direction=direction)


def get_navigator_object() -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.get_navigator_object()


def get_desktop_object() -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.get_desktop_object()


def read_current_line() -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.read_current_line()


def speak_text(text: str) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.speak_text(text=text)


def show_message(message: str) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.show_message(message=message)


def send_keyboard(
	gesture: str,
	repeat: int = 1,
	delay_ms: int = 0,
	context: Any = None,
) -> dict[str, Any]:
	"""Send a keyboard sequence.

	Coordinator tool: runs on the bridge worker thread so the inter-gesture
	pacing does not block NVDA's main thread, and uses the execution context to
	move each individual gesture onto it.
	"""
	from .. import nvda_bridge
	from ..nvda_bridge import ui as nvda_ui

	cancellation_event = getattr(context, "cancellation_event", None) if context else None
	timeout = getattr(context, "timeout", None) if context else None
	dispatch = getattr(context, "dispatch", None) if context else None

	main_thread_fn = None
	if dispatch is not None:
		main_thread_fn = lambda resolved_gesture: dispatch(  # noqa: E731
			lambda: nvda_ui.send_single_gesture(resolved_gesture),
		)

	return nvda_bridge.send_keyboard(
		gesture=gesture,
		repeat=repeat,
		delay_ms=delay_ms,
		cancellation_event=cancellation_event,
		main_thread_fn=main_thread_fn,
		timeout=timeout if timeout is not None else 10.0,
	)


def get_window_title() -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.get_window_title()


def inspect_object(
	target: Literal["focus", "navigator", "desktop"] = "focus",
	include_relations: bool = True,
) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.inspect_object(target=target, include_relations=include_relations)


def walk_object_tree(
	target: Literal["focus", "navigator", "desktop"] = "focus",
	max_depth: int = 2,
	max_children: int = 20,
) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.walk_object_tree(target=target, max_depth=max_depth, max_children=max_children)


def get_focus_lineage(max_ancestors: int = 15) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.get_focus_lineage(max_ancestors=max_ancestors)


def inspect_app_module(source: Literal["focus", "navigator", "desktop"] = "focus") -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.inspect_app_module(source=source)


def list_loaded_addons(scope: Literal["running", "available", "all"] = "running") -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.list_loaded_addons(scope=scope)


def health_snapshot(
	include_logs: bool = True,
	log_lines: int = 30,
	include_tree: bool = False,
	tree_depth: int = 1,
	tree_max_children: int = 10,
	addon_scope: Literal["running", "available", "all"] = "running",
) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.health_snapshot(
		include_logs=include_logs,
		log_lines=log_lines,
		include_tree=include_tree,
		tree_depth=tree_depth,
		tree_max_children=tree_max_children,
		addon_scope=addon_scope,
	)


def restart_nvda(disable_addons: bool = False, debug_logging: bool = False) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.restart_nvda(disable_addons=disable_addons, debug_logging=debug_logging)


def reload_plugins() -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.reload_plugins()


def get_logs(
	max_lines: int = 100,
	filter_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] | None = None,
	use_tail: bool = True,
) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.get_logs(max_lines=max_lines, filter_level=filter_level, use_tail=use_tail)


def execute_python(
	code: str,
	snapshot_vars: bool = False,
	capture_vars: list[str] | None = None,
) -> dict[str, Any]:
	from .. import nvda_bridge

	return nvda_bridge.execute_python(code=code, snapshot_vars=snapshot_vars, capture_vars=capture_vars)


# ---------------------------------------------------------------------------
# Action handlers (implementation plan P2)
#
# Every handler here is COORDINATOR mode: the body runs on the bridge worker
# thread so it can wait for the UI to settle, and each individual NVDA call is
# dispatched onto the main thread. Nothing in this section blocks the main
# thread, which is what keeps NVDA responsive while an action settles.
# ---------------------------------------------------------------------------


def _dispatch(context: Any, operation: Any) -> Any:
	"""Run ``operation`` on NVDA's main thread."""
	if context is None or getattr(context, "dispatch", None) is None:
		return operation()
	return context.dispatch(operation)


def observe(include: list[str], max_text: int, context: Any) -> dict[str, Any]:
	"""Capture the screen, with references to what was seen."""
	return _dispatch(context, lambda: context.observe(include, max_text))


def invoke(ref: str, action: str | None, expect: dict[str, Any] | None, context: Any) -> dict[str, Any]:
	from ..nvda_bridge import actions

	captured: dict[str, Any] = {}

	def perform() -> dict[str, Any]:
		def act() -> dict[str, Any]:
			return actions.do_action(context.resolve(ref), action)

		captured.update(context.dispatch(act))
		return captured

	context.settle(perform, expect=expect)
	return {
		"ref": ref,
		"invoked": True,
		"index": int(captured.get("index", -1)),
		"actionName": str(captured.get("actionName", "")),
		"available": list(captured.get("available", [])),
	}


def set_value(ref: str, value: str, expect: dict[str, Any] | None, context: Any) -> dict[str, Any]:
	from ..nvda_bridge import actions

	captured: dict[str, Any] = {}

	def perform() -> dict[str, Any]:
		def act() -> dict[str, Any]:
			return actions.set_value(context.resolve(ref), value)

		captured.update(context.dispatch(act))
		return captured

	context.settle(perform, expect=expect)
	return {
		"ref": ref,
		"mode": str(captured.get("mode", "keyboard")),
		"value": value,
		"units": int(captured.get("units", 0)),
	}


def focus_object(ref: str, expect: dict[str, Any] | None, context: Any) -> dict[str, Any]:
	from ..nvda_bridge import actions

	def perform() -> dict[str, Any]:
		return context.dispatch(lambda: actions.set_focus(context.resolve(ref)))

	context.settle(perform, expect=expect)
	return {"ref": ref, "requested": True}


def press(
	keys: str,
	repeat: int,
	delay_ms: int,
	expect: dict[str, Any] | None,
	context: Any,
) -> dict[str, Any]:
	"""Send keys, then wait for the UI to settle.

	The keystroke is still the same primitive it always was; what changed is that
	the call no longer returns before its effect is visible.
	"""
	from .. import nvda_bridge
	from ..nvda_bridge import ui as nvda_ui

	captured: dict[str, Any] = {}

	def perform() -> dict[str, Any]:
		result = nvda_bridge.send_keyboard(
			gesture=keys,
			repeat=repeat,
			delay_ms=delay_ms,
			cancellation_event=getattr(context, "cancellation_event", None),
			main_thread_fn=lambda gesture: context.dispatch(
				lambda: nvda_ui.send_single_gesture(gesture)
			),
			timeout=getattr(context, "timeout", None) or 10.0,
		)
		captured.update(result)
		return result

	context.settle(perform, expect=expect)
	return {
		"gesture": captured.get("gesture", keys),
		"repeat": int(captured.get("repeat", repeat)),
		"delay_ms": int(captured.get("delay_ms", delay_ms)),
		"sent": bool(captured.get("sent", False)),
		"sent_count": int(captured.get("sent_count", 0)),
		"dispatched": bool(captured.get("dispatched", False)),
		"cancelled": bool(captured.get("cancelled", False)),
		"uncertain": bool(captured.get("uncertain", False)),
	}


def read(
	ref: str | None,
	scope: str,
	max_chars: int,
	context: Any,
) -> dict[str, Any]:
	from ..nvda_bridge import actions

	def perform() -> dict[str, Any]:
		import api

		obj = context.resolve(ref) if ref else api.getFocusObject()
		if obj is None:
			raise RuntimeError("No focused object to read from")
		return actions.read_text(obj, scope=scope, max_chars=max_chars)

	result = _dispatch(context, perform)
	return {"ref": ref, **result}


def navigate(
	direction: str,
	ref: str | None,
	expect: dict[str, Any] | None,
	context: Any,
) -> dict[str, Any]:
	from ..nvda_bridge import actions

	captured: dict[str, Any] = {}

	def perform() -> dict[str, Any]:
		def act() -> dict[str, Any]:
			import api

			obj = context.resolve(ref) if ref else api.getFocusObject()
			if obj is None:
				raise RuntimeError("No focused object to navigate from")
			target, focused = actions.navigate(obj, direction)
			captured["focused"] = focused
			captured["ref"] = context.register(target)
			return captured

		return context.dispatch(act)

	context.settle(perform, expect=expect)
	return {
		"direction": direction,
		"ref": captured.get("ref"),
		"focused": bool(captured.get("focused", False)),
	}


def scroll(
	direction: str,
	amount: str,
	ref: str | None,
	expect: dict[str, Any] | None,
	context: Any,
) -> dict[str, Any]:
	from ..nvda_bridge import actions

	captured: dict[str, Any] = {}

	def perform() -> dict[str, Any]:
		def act() -> dict[str, Any]:
			import api

			obj = context.resolve(ref) if ref else api.getFocusObject()
			if obj is None:
				raise RuntimeError("No focused object to scroll")
			return actions.scroll(obj, direction, amount)

		captured.update(context.dispatch(act))
		return captured

	context.settle(perform, expect=expect)
	return {
		"direction": direction,
		"amount": amount,
		"method": str(captured.get("method", "keyboard")),
		"key": str(captured.get("key", "")),
	}


def dismiss(expect: dict[str, Any] | None, context: Any) -> dict[str, Any]:
	"""Leave a dialog: Escape first, then its default button.

	This exists because the previous rule let an agent enter a modal and then
	forbade every tool from leaving it — a safety rule that only traps. It needs
	the narrow ``dialog_control`` permission and is allowed in modal and menu
	states.
	"""
	from ..nvda_bridge import actions
	from ..nvda_bridge import ui as nvda_ui

	steps: list[str] = []

	def press_escape() -> None:
		context.dispatch(lambda: nvda_ui.send_single_gesture("escape"))
		steps.append("pressed Escape")

	outcome = context.settle(press_escape, budget_ms=600)
	dialog = (outcome.projection or {}).get("dialog")

	if dialog:
		def activate_default() -> dict[str, Any]:
			def act() -> dict[str, Any]:
				active = actions.dialog_object()
				if active is None:
					return {}
				button = actions.default_dialog_button(active)
				if button is None:
					steps.append("dialog has no default button")
					return {}
				return actions.do_action(button)

			result = context.dispatch(act)
			if result:
				steps.append(f"activated button {result.get('actionName')!r}")
			return result

		outcome = context.settle(activate_default, budget_ms=800)
		dialog = (outcome.projection or {}).get("dialog")
	else:
		steps.append("no dialog was open")

	# The expectation is about the final state, whichever attempt produced it.
	from ..server.settle import evaluate_expect

	expectation = evaluate_expect(outcome.projection, expect)
	if expect:
		outcome.completed = expectation.satisfied
		outcome.expect_mismatch = expectation.mismatch
	if context.result is not None:
		context.result.settle = outcome

	return {
		"dismissed": dialog is None,
		"steps": steps,
		"dialogName": str((dialog or {}).get("name") or ""),
	}


def click_at(x: int, y: int, button: str, expect: dict[str, Any] | None, context: Any) -> dict[str, Any]:
	from ..nvda_bridge import actions

	captured: dict[str, Any] = {}

	def perform() -> dict[str, Any]:
		captured.update(context.dispatch(lambda: actions.click_at(x, y, button)))
		return captured

	context.settle(perform, expect=expect)
	return {
		"x": int(captured.get("x", x)),
		"y": int(captured.get("y", y)),
		"button": button,
		"hit": captured.get("hit"),
		"cursor": list(captured.get("cursor") or []),
		"onTarget": bool(captured.get("onTarget", True)),
	}


def wait_for_events(
	since: int,
	timeout_ms: int,
	kinds: list[str] | None,
	context: Any,
) -> dict[str, Any]:
	if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms < 0 or timeout_ms > 30000:
		raise ValueError("'timeout_ms' must be an integer between 0 and 30000")
	result = context.wait_events(since=since, timeout_ms=timeout_ms, kinds=kinds)
	return {
		"from_version": int(result.get("from_version", since)),
		"to_version": int(result.get("to_version", since)),
		"events": list(result.get("events", [])),
		"truncated": bool(result.get("truncated", False)),
		"timed_out": int(result.get("to_version", since)) <= since,
	}
