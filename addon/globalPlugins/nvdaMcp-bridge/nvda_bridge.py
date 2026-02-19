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
	Get information about the currently focused object

	Returns:
		Dictionary with focus information
	"""
	try:
		focus = api.getFocusObject()
		if not focus:
			return {"error": "No focus object available"}

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
	except Exception as e:
		logHandler.log.error(f"Error getting focus: {e}", exc_info=True)
		return {"error": str(e)}


def read_current_line() -> Dict[str, Any]:
	"""
	Read the current line from the focused object

	Returns:
		Dictionary with line text and position
	"""
	try:
		focus = api.getFocusObject()
		if not focus:
			return {"error": "No focus object available"}

		# Try to get text info
		try:
			info = focus.makeTextInfo(textInfos.POSITION_CARET)
			if not info:
				return {"error": "Cannot create text info for current position"}

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
			# Object doesn't support text info
			return {
				"text": getattr(focus, "name", None) or getattr(focus, "value", None) or "",
				"lineNumber": None,
				"note": "Object doesn't support text navigation",
			}
	except Exception as e:
		logHandler.log.error(f"Error reading current line: {e}", exc_info=True)
		return {"error": str(e)}


def speak_text(text: str) -> Dict[str, Any]:
	"""
	Speak the given text using NVDA's speech system

	Args:
		text: Text to speak

	Returns:
		Success status
	"""
	try:
		# Validate input
		if not text:
			return {"error": "No text provided"}

		if not isinstance(text, str):
			return {"error": "Text must be a string"}

		# Limit text length to prevent issues
		if len(text) > 10000:
			return {"error": "Text too long (max 10000 characters)"}

		speech.speakText(text)
		return {"success": True, "spoken": text}
	except Exception as e:
		logHandler.log.error(f"Error speaking text: {e}", exc_info=True)
		return {"error": str(e)}


def get_window_title() -> Dict[str, Any]:
	"""
	Get the title of the current foreground window

	Returns:
		Dictionary with window title and handle
	"""
	try:
		# Get foreground window
		hwnd = winUser.getForegroundWindow()
		if not hwnd:
			return {"error": "No foreground window"}

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
	except Exception as e:
		logHandler.log.error(f"Error getting window title: {e}", exc_info=True)
		return {"error": str(e)}


def move_focus(direction: str) -> Dict[str, Any]:
	"""
	Move focus to next or previous object

	Args:
		direction: "next" or "previous"

	Returns:
		Success status with new focus information
	"""
	try:
		# Validate input
		if not direction or not isinstance(direction, str):
			return {"error": "Direction must be a non-empty string"}

		focus = api.getFocusObject()
		if not focus:
			return {"error": "No focus object available"}

		# Determine navigation direction
		if direction.lower() == "next":
			# Move to next object
			next_obj = getattr(focus, "next", None)
			if next_obj:
				try:
					next_obj.setFocus()
					return {
						"success": True,
						"direction": "next",
						"newFocus": {
							"name": getattr(next_obj, "name", None) or "",
							"role": getattr(next_obj, "roleText", None) or "",
						},
					}
				except Exception as e:
					logHandler.log.error(f"Error setting focus to next object: {e}", exc_info=True)
					return {"error": f"Failed to set focus: {str(e)}"}
			else:
				return {"error": "No next object available"}

		elif direction.lower() == "previous":
			# Move to previous object
			prev_obj = getattr(focus, "previous", None)
			if prev_obj:
				try:
					prev_obj.setFocus()
					return {
						"success": True,
						"direction": "previous",
						"newFocus": {
							"name": getattr(prev_obj, "name", None) or "",
							"role": getattr(prev_obj, "roleText", None) or "",
						},
					}
				except Exception as e:
					logHandler.log.error(f"Error setting focus to previous object: {e}", exc_info=True)
					return {"error": f"Failed to set focus: {str(e)}"}
			else:
				return {"error": "No previous object available"}
		else:
			return {"error": f"Invalid direction: {direction}. Use 'next' or 'previous'"}

	except Exception as e:
		logHandler.log.error(f"Error moving focus: {e}", exc_info=True)
		return {"error": str(e)}


def get_navigator_object() -> Dict[str, Any]:
	"""
	Get information about the current navigator object

	Returns:
		Dictionary with navigator object information
	"""
	try:
		nav = api.getNavigatorObject()
		if not nav:
			return {"error": "No navigator object available"}

		return {
			"name": getattr(nav, "name", None) or "",
			"role": getattr(nav, "roleText", None) or "",
			"value": getattr(nav, "value", None) or "",
			"description": getattr(nav, "description", None) or "",
		}
	except Exception as e:
		logHandler.log.error(f"Error getting navigator object: {e}", exc_info=True)
		return {"error": str(e)}


def get_desktop_object() -> Dict[str, Any]:
	"""
	Get information about the desktop object

	Returns:
		Dictionary with desktop object information
	"""
	try:
		desktop = api.getDesktopObject()
		if not desktop:
			return {"error": "No desktop object available"}

		return {
			"name": getattr(desktop, "name", None) or "",
			"role": getattr(desktop, "roleText", None) or "",
			"childCount": getattr(desktop, "childCount", 0),
		}
	except Exception as e:
		logHandler.log.error(f"Error getting desktop object: {e}", exc_info=True)
		return {"error": str(e)}


def show_message(message: str) -> Dict[str, Any]:
	"""
	Show a UI message to the user

	Args:
		message: Message to display

	Returns:
		Success status
	"""
	try:
		# Validate input
		if not message:
			return {"error": "No message provided"}

		if not isinstance(message, str):
			return {"error": "Message must be a string"}

		# Limit message length
		if len(message) > 1000:
			return {"error": "Message too long (max 1000 characters)"}

		ui.message(message)
		return {"success": True, "message": message}
	except Exception as e:
		logHandler.log.error(f"Error showing message: {e}", exc_info=True)
		return {"error": str(e)}
