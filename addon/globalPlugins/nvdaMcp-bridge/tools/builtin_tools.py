# pyright: reportImplicitOverride=false

"""Built-in auto-discovered tools for the NVDA MCP Bridge."""

from abc import ABC
from collections.abc import Callable
from typing import Any

from .. import nvda_bridge
from ..tool_base import ToolBase, ToolMetadata


def _exec_get_current_focus(_params: dict[str, Any]) -> Any:
	return nvda_bridge.get_current_focus()


def _exec_move_focus(params: dict[str, Any]) -> Any:
	return nvda_bridge.move_focus(direction=str(params["direction"]))


def _exec_get_navigator_object(_params: dict[str, Any]) -> Any:
	return nvda_bridge.get_navigator_object()


def _exec_get_desktop_object(_params: dict[str, Any]) -> Any:
	return nvda_bridge.get_desktop_object()


def _exec_read_current_line(_params: dict[str, Any]) -> Any:
	return nvda_bridge.read_current_line()


def _exec_speak_text(params: dict[str, Any]) -> Any:
	return nvda_bridge.speak_text(text=str(params["text"]))


def _exec_show_message(params: dict[str, Any]) -> Any:
	return nvda_bridge.show_message(message=str(params["message"]))


def _exec_get_window_title(_params: dict[str, Any]) -> Any:
	return nvda_bridge.get_window_title()


def _exec_restart_nvda(params: dict[str, Any]) -> Any:
	return nvda_bridge.restart_nvda(
		disable_addons=bool(params.get("disable_addons", False)),
		debug_logging=bool(params.get("debug_logging", False)),
	)


def _exec_reload_plugins(_params: dict[str, Any]) -> Any:
	return nvda_bridge.reload_plugins()


def _exec_get_logs(params: dict[str, Any]) -> Any:
	filter_level = params.get("filter_level")
	# Reject non-string values explicitly so callers receive a clear
	# validation error instead of silently getting unfiltered results
	# (the old code coerced anything non-string to None).
	if filter_level is not None and not isinstance(filter_level, str):
		raise ValueError(f"'filter_level' must be a string or null, got {type(filter_level).__name__}")
	return nvda_bridge.get_logs(
		max_lines=int(params.get("max_lines", 100)),
		filter_level=filter_level,
		use_tail=bool(params.get("use_tail", True)),
	)


class NVDAFunctionTool(ToolBase, ABC):
	_is_template = True
	NAME: str = ""
	DESCRIPTION: str = ""
	REQUIRED_PARAMS: list[str] = []
	OPTIONAL_PARAMS: list[str] = []
	VERSION: str = "1.0.0"
	CATEGORY: str = "general"
	EXECUTOR: Callable[[dict[str, Any]], Any]

	def metadata(self) -> ToolMetadata:
		return ToolMetadata(
			name=self.NAME,
			description=self.DESCRIPTION,
			required_params=self.REQUIRED_PARAMS,
			optional_params=self.OPTIONAL_PARAMS,
			version=self.VERSION,
			category=self.CATEGORY,
		)

	def execute(self, **params: Any) -> Any:
		# Bypass Python's descriptor protocol: accessing a plain function via
		# `self.EXECUTOR` would bind `self` as the first argument (turning the
		# function into a bound method), causing an "N+1 positional arguments"
		# error for every executor that doesn't accept `self`.
		# Walking the MRO and reading from __dict__ directly retrieves the raw
		# function object without any binding.
		executor: Callable[[dict[str, Any]], Any] | None = None
		for cls in type(self).__mro__:
			if "EXECUTOR" in cls.__dict__:
				executor = cls.__dict__["EXECUTOR"]
				break
		if executor is None:
			raise AttributeError(f"{type(self).__name__} has no EXECUTOR defined")
		return executor(params)


class GetCurrentFocusTool(NVDAFunctionTool):
	NAME = "get_current_focus"
	DESCRIPTION = "Get information about the currently focused UI element"
	CATEGORY = "focus"
	EXECUTOR = _exec_get_current_focus


class MoveFocusTool(NVDAFunctionTool):
	NAME = "move_focus"
	DESCRIPTION = "Move focus to next or previous object"
	REQUIRED_PARAMS = ["direction"]
	CATEGORY = "focus"
	EXECUTOR = _exec_move_focus


class GetNavigatorObjectTool(NVDAFunctionTool):
	NAME = "get_navigator_object"
	DESCRIPTION = "Get information about the current navigator object"
	CATEGORY = "navigation"
	EXECUTOR = _exec_get_navigator_object


class GetDesktopObjectTool(NVDAFunctionTool):
	NAME = "get_desktop_object"
	DESCRIPTION = "Get information about the desktop object"
	CATEGORY = "navigation"
	EXECUTOR = _exec_get_desktop_object


class ReadCurrentLineTool(NVDAFunctionTool):
	NAME = "read_current_line"
	DESCRIPTION = "Read the current line from the focused object"
	CATEGORY = "text"
	EXECUTOR = _exec_read_current_line


class SpeakTextTool(NVDAFunctionTool):
	NAME = "speak_text"
	DESCRIPTION = "Speak the given text using NVDA's speech system"
	REQUIRED_PARAMS = ["text"]
	CATEGORY = "speech"
	EXECUTOR = _exec_speak_text


class ShowMessageTool(NVDAFunctionTool):
	NAME = "show_message"
	DESCRIPTION = "Show a UI message to the user"
	REQUIRED_PARAMS = ["message"]
	CATEGORY = "ui"
	EXECUTOR = _exec_show_message


class GetWindowTitleTool(NVDAFunctionTool):
	NAME = "get_window_title"
	DESCRIPTION = "Get the title and details of the current foreground window"
	CATEGORY = "window"
	EXECUTOR = _exec_get_window_title


class RestartNVDATool(NVDAFunctionTool):
	NAME = "restart_nvda"
	DESCRIPTION = "Restart NVDA with optional parameters (disable addons, debug logging)"
	OPTIONAL_PARAMS = ["disable_addons", "debug_logging"]
	CATEGORY = "system"
	EXECUTOR = _exec_restart_nvda


class ReloadPluginsTool(NVDAFunctionTool):
	NAME = "reload_plugins"
	DESCRIPTION = "Reload all global plugins and app modules from scratchpad"
	CATEGORY = "system"
	EXECUTOR = _exec_reload_plugins


class GetLogsTool(NVDAFunctionTool):
	NAME = "get_logs"
	DESCRIPTION = "Retrieve recent log entries from NVDA log file with optional filtering"
	OPTIONAL_PARAMS = ["max_lines", "filter_level", "use_tail"]
	CATEGORY = "diagnostics"
	EXECUTOR = _exec_get_logs
