"""
NVDA Bridge Module
Provides thin wrappers around NVDA APIs for the HTTP bridge.

This module contains pure NVDA API calls with no protocol logic.
Functions are called by nvda_http_bridge.py and return JSON-serializable dicts.

CRITICAL THREAD SAFETY REQUIREMENT:
All functions in this module MUST be called from NVDA's main thread only.
They are automatically queued to the main thread via _execute_on_main_thread()
in nvda_http_bridge.py. Never call these functions directly from background threads.

Keep this module focused on:
- NVDA API access
- Error handling
- JSON-compatible return values

Do NOT add:
- Protocol implementation
- MCP schemas
- Tool definitions
- HTTP routing
"""

import api
import speech
import winUser
import textInfos
import ui
import logHandler
from typing import Dict, Any


def get_current_focus() -> Dict[str, Any]:
	"""
	Get information about the currently focused object.

	Returns:
		Dictionary with focus information

	Raises:
		RuntimeError: If no focus object is available or operation fails
	"""
	focus = api.getFocusObject()
	if not focus:
		raise RuntimeError("No focus object available")

	# Safely get attributes with defaults
	return {
		"name": getattr(focus, "name", None) or "",
		"role": getattr(focus, "role", None),
		"roleText": getattr(focus, "roleText", None) or "",
		"value": getattr(focus, "value", None) or "",
		"description": getattr(focus, "description", None) or "",
		"states": str(getattr(focus, "states", "")),
		"windowClassName": getattr(focus, "windowClassName", None) or "",
		"windowControlID": getattr(focus, "windowControlID", 0),
	}


def read_current_line() -> Dict[str, Any]:
	"""
	Read the current line from the focused object.

	Returns:
		Dictionary with line text and position

	Raises:
		RuntimeError: If no focus object or cannot create text info
	"""
	focus = api.getFocusObject()
	if not focus:
		raise RuntimeError("No focus object available")

	# Try to get text info
	try:
		info = focus.makeTextInfo(textInfos.POSITION_CARET)
		if not info:
			raise RuntimeError("Cannot create text info for current position")

		info.expand(textInfos.UNIT_LINE)
		text = info.text

		# Get line number if available
		line_num = None
		try:
			bookmark = getattr(info, "bookmark", None)
			if bookmark:
				line_num = getattr(bookmark, "startOffset", None)
		except Exception:
			pass  # Line number not available

		return {
			"text": text or "",
			"lineNumber": line_num,
		}
	except (NotImplementedError, AttributeError):
		# Object doesn't support text info - return fallback
		return {
			"text": getattr(focus, "name", None) or getattr(focus, "value", None) or "",
			"lineNumber": None,
			"note": "Object doesn't support text navigation",
		}


def speak_text(text: str) -> Dict[str, Any]:
	"""
	Speak the given text using NVDA's speech system.

	Args:
		text: Text to speak

	Returns:
		Dictionary with spoken text

	Raises:
		ValueError: If text is invalid or too long
	"""
	# Validate input
	if not text:
		raise ValueError("No text provided")

	if not isinstance(text, str):
		raise ValueError("Text must be a string")

	# Limit text length to prevent issues
	if len(text) > 10000:
		raise ValueError("Text too long (max 10000 characters)")

	speech.speakText(text)
	return {"spoken": text}


def get_window_title() -> Dict[str, Any]:
	"""
	Get the title of the current foreground window.

	Returns:
		Dictionary with window title and handle

	Raises:
		RuntimeError: If no foreground window is available
	"""
	# Get foreground window
	hwnd = winUser.getForegroundWindow()
	if not hwnd:
		raise RuntimeError("No foreground window")

	# Get window text with error handling
	try:
		title = winUser.getWindowText(hwnd)
	except Exception as e:
		logHandler.log.warning(f"Error getting window text: {e}")
		title = ""

	# Get class name with error handling
	try:
		className = winUser.getClassName(hwnd)
	except Exception as e:
		logHandler.log.warning(f"Error getting class name: {e}")
		className = ""

	return {
		"title": title or "",
		"handle": hwnd,
		"className": className or "",
	}


def move_focus(direction: str) -> Dict[str, Any]:
	"""
	Move focus to next or previous object.

	Args:
		direction: "next" or "previous"

	Returns:
		Dictionary with navigation result

	Raises:
		ValueError: If direction is invalid
		RuntimeError: If no focus object or navigation fails
	"""
	# Validate input
	if not direction or not isinstance(direction, str):
		raise ValueError("Direction must be a non-empty string")

	focus = api.getFocusObject()
	if not focus:
		raise RuntimeError("No focus object available")

	# Determine navigation direction
	if direction.lower() == "next":
		# Move to next object
		next_obj = getattr(focus, "next", None)
		if not next_obj:
			raise RuntimeError("No next object available")

		next_obj.setFocus()
		return {
			"direction": "next",
			"newFocus": {
				"name": getattr(next_obj, "name", None) or "",
				"role": getattr(next_obj, "roleText", None) or "",
			},
		}

	elif direction.lower() == "previous":
		# Move to previous object
		prev_obj = getattr(focus, "previous", None)
		if not prev_obj:
			raise RuntimeError("No previous object available")

		prev_obj.setFocus()
		return {
			"direction": "previous",
			"newFocus": {
				"name": getattr(prev_obj, "name", None) or "",
				"role": getattr(prev_obj, "roleText", None) or "",
			},
		}
	else:
		raise ValueError(f"Invalid direction: {direction}. Use 'next' or 'previous'")


def get_navigator_object() -> Dict[str, Any]:
	"""
	Get information about the current navigator object.

	Returns:
		Dictionary with navigator object information

	Raises:
		RuntimeError: If no navigator object is available
	"""
	nav = api.getNavigatorObject()
	if not nav:
		raise RuntimeError("No navigator object available")

	return {
		"name": getattr(nav, "name", None) or "",
		"role": getattr(nav, "roleText", None) or "",
		"value": getattr(nav, "value", None) or "",
		"description": getattr(nav, "description", None) or "",
	}


def get_desktop_object() -> Dict[str, Any]:
	"""
	Get information about the desktop object.

	Returns:
		Dictionary with desktop object information

	Raises:
		RuntimeError: If no desktop object is available
	"""
	desktop = api.getDesktopObject()
	if not desktop:
		raise RuntimeError("No desktop object available")

	return {
		"name": getattr(desktop, "name", None) or "",
		"role": getattr(desktop, "roleText", None) or "",
		"childCount": getattr(desktop, "childCount", 0),
	}


def show_message(message: str) -> Dict[str, Any]:
	"""
	Show a UI message to the user.

	Args:
		message: Message to display

	Returns:
		Dictionary with displayed message

	Raises:
		ValueError: If message is invalid or too long
	"""
	# Validate input
	if not message:
		raise ValueError("No message provided")

	if not isinstance(message, str):
		raise ValueError("Message must be a string")

	# Limit message length
	if len(message) > 1000:
		raise ValueError("Message too long (max 1000 characters)")

	ui.message(message)
	return {"message": message}
