"""
NVDA Bridge Module
Provides thin wrappers around NVDA APIs for the HTTP bridge.

This module contains pure NVDA API calls with no protocol logic.
Functions are called by nvda_http_bridge.py and return JSON-serializable dicts.

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
import controlTypes
import textInfos
import ui
import logHandler
from typing import Optional, Dict, Any


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
		
		return {
			"name": focus.name or "",
			"role": focus.role if hasattr(focus, 'role') else None,
			"roleText": focus.roleText if hasattr(focus, 'roleText') else "",
			"value": focus.value if hasattr(focus, 'value') else "",
			"description": focus.description if hasattr(focus, 'description') else "",
			"states": str(focus.states) if hasattr(focus, 'states') else "",
			"windowClassName": focus.windowClassName if hasattr(focus, 'windowClassName') else "",
			"windowControlID": focus.windowControlID if hasattr(focus, 'windowControlID') else 0,
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
			info.expand(textInfos.UNIT_LINE)
			text = info.text
			
			# Get line number if available
			line_num = None
			try:
				bookmark = info.bookmark
				line_num = bookmark.startOffset if hasattr(bookmark, 'startOffset') else None
			except:
				pass
			
			return {
				"text": text or "",
				"lineNumber": line_num,
			}
		except NotImplementedError:
			# Object doesn't support text info
			return {
				"text": focus.name or focus.value or "",
				"lineNumber": None,
				"note": "Object doesn't support text navigation"
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
		if not text:
			return {"error": "No text provided"}
		
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
		
		# Get window text
		title = winUser.getWindowText(hwnd)
		
		# Get class name
		className = winUser.getClassName(hwnd)
		
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
		focus = api.getFocusObject()
		if not focus:
			return {"error": "No focus object available"}
		
		# Determine navigation direction
		if direction.lower() == "next":
			# Move to next object
			next_obj = focus.next
			if next_obj:
				next_obj.setFocus()
				return {
					"success": True,
					"direction": "next",
					"newFocus": {
						"name": next_obj.name or "",
						"role": next_obj.roleText if hasattr(next_obj, 'roleText') else "",
					}
				}
			else:
				return {"error": "No next object available"}
				
		elif direction.lower() == "previous":
			# Move to previous object
			prev_obj = focus.previous
			if prev_obj:
				prev_obj.setFocus()
				return {
					"success": True,
					"direction": "previous",
					"newFocus": {
						"name": prev_obj.name or "",
						"role": prev_obj.roleText if hasattr(prev_obj, 'roleText') else "",
					}
				}
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
			"name": nav.name or "",
			"role": nav.roleText if hasattr(nav, 'roleText') else "",
			"value": nav.value if hasattr(nav, 'value') else "",
			"description": nav.description if hasattr(nav, 'description') else "",
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
			"name": desktop.name or "",
			"role": desktop.roleText if hasattr(desktop, 'roleText') else "",
			"childCount": desktop.childCount if hasattr(desktop, 'childCount') else 0,
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
		if not message:
			return {"error": "No message provided"}
		
		ui.message(message)
		return {"success": True, "message": message}
	except Exception as e:
		logHandler.log.error(f"Error showing message: {e}", exc_info=True)
		return {"error": str(e)}
