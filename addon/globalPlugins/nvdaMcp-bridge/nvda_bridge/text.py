"""Text reading and speech utilities for NVDA Bridge.

Provides functions to read text from focused objects and speak text.
"""

from typing import Any, Dict

import api
import textInfos
import speech


def read_current_line() -> Dict[str, Any]:
	"""Read the current line from the focused object.

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

		return {
			"text": text or "",
			"note": "ok",
			"status": "ok",
		}
	except Exception:  # noqa: BLE001
		# Any object without a caret — a link, a list item, a container — reports
		# "ok" from a text query it cannot answer. Fall back to the label rather
		# than failing the call: this is a read, and a label is still an answer.
		return {
			"text": getattr(focus, "name", None) or getattr(focus, "value", None) or "",
			"note": "fallback",
			"status": "fallback",
		}


def speak_text(text: str) -> Dict[str, Any]:
	"""Speak the given text using NVDA's speech system.

	Args:
		text: Text to speak

	Returns:
		Dictionary with spoken text

	Raises:
		ValueError: If text is invalid or too long
	"""
	# Type check first: a falsy non-string (e.g. 0, False, []) would
	# otherwise raise "No text provided" hiding the real type mismatch.
	if not isinstance(text, str):
		raise ValueError("Text must be a string")

	if not text:
		raise ValueError("No text provided")

	# Limit text length to prevent issues
	if len(text) > 10000:
		raise ValueError("Text too long (max 10000 characters)")

	speech.speakText(text)
	return {"spoken": text}
