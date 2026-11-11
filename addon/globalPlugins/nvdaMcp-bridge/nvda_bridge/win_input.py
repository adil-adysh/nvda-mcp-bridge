"""Direct Windows input primitives: unicode text and mouse clicks.

NVDA's ``keyboardHandler`` resolves *named gestures* (``control+a``), which covers
keys but not arbitrary text: a value like ``Café #42`` has no gesture name. UIA's
``ValuePattern`` covers some native controls but, measured on a Chromium text
field, there is no write path at all — no ``value`` setter, no
``IAccessibleEditableText``, and no ``UIAElement``. The honest implementation of
``set_value`` on the web is therefore "focus, select all, type", and typing needs
``SendInput`` with ``KEYEVENTF_UNICODE``.

Everything here must be called from NVDA's main thread (it is input injection),
and nothing here sleeps or waits.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

import logHandler

_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1

_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004

_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_ABSOLUTE = 0x8000
_MOUSEEVENTF_VIRTUALDESK = 0x4000
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_MIDDLEDOWN = 0x0020
_MOUSEEVENTF_MIDDLEUP = 0x0040

_ULONG_PTR = ctypes.c_size_t


class _KEYBDINPUT(ctypes.Structure):
	_fields_ = [
		("wVk", wintypes.WORD),
		("wScan", wintypes.WORD),
		("dwFlags", wintypes.DWORD),
		("time", wintypes.DWORD),
		("dwExtraInfo", _ULONG_PTR),
	]


class _MOUSEINPUT(ctypes.Structure):
	_fields_ = [
		("dx", wintypes.LONG),
		("dy", wintypes.LONG),
		("mouseData", wintypes.DWORD),
		("dwFlags", wintypes.DWORD),
		("time", wintypes.DWORD),
		("dwExtraInfo", _ULONG_PTR),
	]


class _HARDWAREINPUT(ctypes.Structure):
	_fields_ = [
		("uMsg", wintypes.DWORD),
		("wParamL", wintypes.WORD),
		("wParamH", wintypes.WORD),
	]


class _INPUTUNION(ctypes.Union):
	_fields_ = [
		("ki", _KEYBDINPUT),
		("mi", _MOUSEINPUT),
		("hi", _HARDWAREINPUT),
	]


class _INPUT(ctypes.Structure):
	_anonymous_ = ("u",)
	_fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


_USER32 = ctypes.WinDLL("user32", use_last_error=True)
_USER32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
_USER32.SendInput.restype = wintypes.UINT
_USER32.GetSystemMetrics.argtypes = (ctypes.c_int,)
_USER32.GetSystemMetrics.restype = ctypes.c_int


class InputInjectionError(RuntimeError):
	"""``SendInput`` refused to inject the input."""

	protocol_code = 5003


def _send(inputs: list[_INPUT]) -> None:
	if not inputs:
		return
	array = (_INPUT * len(inputs))(*inputs)
	sent = _USER32.SendInput(len(inputs), array, ctypes.sizeof(_INPUT))
	if sent != len(inputs):
		error = ctypes.get_last_error()
		logHandler.log.warning(f"win_input.SendInput sent {sent}/{len(inputs)} events (error {error})")
		raise InputInjectionError(
			f"SendInput injected {sent} of {len(inputs)} input events (Windows error {error})"
		)


def _unicode_units(text: str) -> list[int]:
	"""UTF-16 code units, because ``KEYEVENTF_UNICODE`` scans one unit at a time."""
	encoded = text.encode("utf-16-le")
	return [int.from_bytes(encoded[index : index + 2], "little") for index in range(0, len(encoded), 2)]


def send_text(text: str) -> int:
	"""Type ``text`` into whatever currently has keyboard focus. Returns the unit count."""
	if not isinstance(text, str) or not text:
		raise ValueError("'value' must be a non-empty string")
	units = _unicode_units(text)
	inputs: list[_INPUT] = []
	for unit in units:
		inputs.append(_INPUT(type=_INPUT_KEYBOARD, ki=_KEYBDINPUT(0, unit, _KEYEVENTF_UNICODE, 0, 0)))
		inputs.append(
			_INPUT(type=_INPUT_KEYBOARD, ki=_KEYBDINPUT(0, unit, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP, 0, 0))
		)
	_send(inputs)
	return len(units)


_BUTTON_FLAGS = {
	"left": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP),
	"right": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP),
	"middle": (_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP),
}


def click(x: int, y: int, button: str = "left") -> tuple[int, int, bool]:
	"""Move the pointer to ``x, y`` and click. Absolute screen coordinates.

	Returns ``(cursor_x, cursor_y, on_target)``. The position is read back after
	the click: ``SendInput`` reports only that events were accepted, and a pointer
	that landed somewhere else is exactly the failure a "last resort" click must
	not hide.
	"""
	if button not in _BUTTON_FLAGS:
		raise ValueError(f"Unknown mouse button: {button}")
	left = _USER32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
	top = _USER32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
	width = max(1, _USER32.GetSystemMetrics(78))  # SM_CXVIRTUALSCREEN
	height = max(1, _USER32.GetSystemMetrics(79))  # SM_CYVIRTUALSCREEN

	# ``MOUSEEVENTF_VIRTUALDESK`` is what makes 0..65535 span the *virtual* screen;
	# without it the mapping is relative to the primary monitor and a click on a
	# secondary monitor lands on the wrong one (or on another window entirely).
	normalized_x = int(round((x - left) * 65535 / max(1, width - 1)))
	normalized_y = int(round((y - top) * 65535 / max(1, height - 1)))
	down, up = _BUTTON_FLAGS[button]
	move = _INPUT(
		type=_INPUT_MOUSE,
		mi=_MOUSEINPUT(
			normalized_x,
			normalized_y,
			0,
			_MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK,
			0,
			0,
		),
	)
	press = _INPUT(type=_INPUT_MOUSE, mi=_MOUSEINPUT(0, 0, 0, down, 0, 0))
	release = _INPUT(type=_INPUT_MOUSE, mi=_MOUSEINPUT(0, 0, 0, up, 0, 0))
	# Send the move separately, then the click. Browsers ignore a button press
	# that arrives in the same batch as the pointer move that preceded it: the
	# move is still being processed when the click is dispatched, so the click
	# lands on whatever was under the old position. 30 ms is below perception and
	# enough for the move to take effect.
	_send([move])
	time.sleep(0.03)
	_send([press, release])

	point = wintypes.POINT()
	_USER32.GetCursorPos(ctypes.byref(point))
	on_target = abs(point.x - x) <= 2 and abs(point.y - y) <= 2
	if not on_target:
		logHandler.log.warning(f"win_input.click landed at {point.x},{point.y} instead of {x},{y}")
	return point.x, point.y, on_target
