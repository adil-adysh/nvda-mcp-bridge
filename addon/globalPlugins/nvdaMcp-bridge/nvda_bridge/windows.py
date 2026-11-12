"""Top-level windows: name them, and bring one to the front.

Windows only grants the foreground to a process that already has it. An external
program calling ``SetForegroundWindow`` is ignored unless it attaches its input
queue to the target's first, which is what :func:`activate` does. NVDA is in a
better position than a random helper process — it is a registered accessibility
client and usually already owns part of the foreground — but the rules still
apply, so the sequence below is the same one that made window activation
reliable in the acceptance scripts: restore, raise with the topmost flag,
attach, ask, then verify by polling until the OS agrees.

Enumeration goes through NVDA's own desktop object rather than ``EnumWindows``:
the objects come back already wrapped (name, handle, role), so a caller can
register a reference to the window it just raised without a second lookup.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Any

from .errors import DomainUnavailableError
from ..server.state import _enum_name, _safe_get

_SW_RESTORE = 9
_HWND_TOPMOST = -1
_HWND_NOTOPMOST = -2
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.IsWindow.argtypes = (wintypes.HWND,)
_user32.IsWindow.restype = wintypes.BOOL
_user32.IsIconic.argtypes = (wintypes.HWND,)
_user32.IsIconic.restype = wintypes.BOOL
_user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
_user32.ShowWindow.restype = wintypes.BOOL
_user32.BringWindowToTop.argtypes = (wintypes.HWND,)
_user32.BringWindowToTop.restype = wintypes.BOOL
_user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
_user32.SetForegroundWindow.restype = wintypes.BOOL
_user32.SetWindowPos.argtypes = (
	wintypes.HWND,
	wintypes.HWND,
	ctypes.c_int,
	ctypes.c_int,
	ctypes.c_int,
	ctypes.c_int,
	ctypes.c_uint,
)
_user32.SetWindowPos.restype = wintypes.BOOL
_user32.SwitchToThisWindow.argtypes = (wintypes.HWND, wintypes.BOOL)
_user32.SwitchToThisWindow.restype = None
_user32.SetFocus.argtypes = (wintypes.HWND,)
_user32.SetFocus.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
_user32.AttachThreadInput.restype = wintypes.BOOL
_user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
_user32.GetWindowTextLengthW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
_user32.GetWindowTextW.restype = ctypes.c_int

_kernel32.GetCurrentThreadId.restype = wintypes.DWORD


#: Characters that render as nothing but still break a substring match. Edge's
#: tab title carries a zero-width space inside "Microsoft Edge", which is enough
#: to make a perfectly reasonable title query miss.
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None)


def _normalize(text: str) -> str:
	"""Compare titles the way a person reads them: whitespace, case, invisibles."""
	cleaned = str(text or "").translate(_INVISIBLE)
	return " ".join(cleaned.split()).casefold()


def window_title(handle: int) -> str:
	"""The current title of a top-level window, or an empty string."""
	if not handle:
		return ""
	length = _user32.GetWindowTextLengthW(handle)
	if length <= 0:
		return ""
	buffer = ctypes.create_unicode_buffer(length + 1)
	if _user32.GetWindowTextW(handle, buffer, length + 1) <= 0:
		return ""
	return buffer.value


def foreground_handle() -> int:
	return int(_user32.GetForegroundWindow() or 0)


def foreground_title() -> str:
	"""The title of the window that is in front right now."""
	return window_title(foreground_handle())


def title_matches(candidate: str, wanted: str, exact: bool = False) -> bool:
	"""Whether ``candidate`` would be selected by a query for ``wanted``."""
	return _rank(candidate, wanted, exact) is not None


def is_minimized(handle: int) -> bool:
	"""Whether the window is minimised (and therefore not really in front)."""
	return bool(handle) and bool(_user32.IsIconic(handle))


def focus_window() -> tuple[int, str]:
	"""The top-level window that NVDA's focus is in, and its title.

	Windows can move the foreground without moving the keyboard focus — a window
	launched by a program is the common case — and then the two answers disagree.
	``activate`` verifies with Win32, because that is the state it can change;
	this is the other answer, so the caller can tell when the screen reader will
	still be reading the previous window.
	"""
	try:
		import api

		node = api.getFocusObject()
	except Exception:  # noqa: BLE001 — no object model means no answer
		return 0, ""
	chosen = None
	for _ in range(10):
		if node is None:
			break
		if _enum_name(_safe_get(node, "role")) in ("WINDOW", "DIALOG"):
			chosen = node
			break
		node = _safe_get(node, "parent")
	handle = _safe_get(chosen, "windowHandle")
	if not isinstance(handle, int) or isinstance(handle, bool) or handle <= 0:
		return 0, ""
	return int(handle), window_title(int(handle))


def object_for_handle(handle: int) -> Any:
	"""The NVDA object for a window handle, so a caller can be given a ref."""
	for candidate in top_level_windows():
		if candidate["handle"] == int(handle):
			return candidate["object"]
	return None


def top_level_windows() -> list[dict[str, Any]]:
	"""Every top-level window NVDA can see, with its handle and title.

	Hidden and untitled windows are skipped: they cannot be activated in a way a
	caller could verify, and they would only dilute title matching.
	"""
	try:
		import api

		desktop = api.getDesktopObject()
		children = list(desktop.children)
	except Exception:  # noqa: BLE001 — no NVDA object model means no candidates
		return []

	windows: list[dict[str, Any]] = []
	for child in children:
		try:
			handle = int(getattr(child, "windowHandle", 0) or 0)
			title = str(getattr(child, "name", "") or "")
		except Exception:  # noqa: BLE001 — a dying window can throw on any attribute
			continue
		if not handle or not title or not _user32.IsWindow(handle):
			continue
		windows.append({"object": child, "handle": handle, "title": title})
	return windows


def _rank(title: str, wanted: str, exact: bool) -> tuple[int, str] | None:
	"""Score one candidate title against the wanted text.

	Exact beats prefix beats substring, because "Notepad" is a prefix of
	"*abc 77777 - Notepad" and calling that an exact match would be a lie.
	"""
	haystack = _normalize(title)
	needle = _normalize(wanted)
	if not needle:
		return None
	if haystack == needle:
		return (0, title)
	if exact:
		return None
	if haystack.startswith(needle):
		return (1, title)
	if needle in haystack:
		return (2, title)
	return None


def match_window(
	title: str,
	exact: bool,
	windows: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], int]:
	"""Pick the best window for ``title``; return it and how many matched."""
	candidates = top_level_windows() if windows is None else list(windows)
	scored: list[tuple[tuple[int, int], dict[str, Any]]] = []
	for candidate in candidates:
		ranked = _rank(candidate["title"], title, exact)
		if ranked is None:
			continue
		rank, _ = ranked
		# Shorter titles win ties: "Notepad" before "Notepad - 3 tabs".
		scored.append(((rank, len(candidate["title"])), candidate))

	if not scored:
		available = sorted({candidate["title"] for candidate in candidates})[:8]
		raise DomainUnavailableError(
			f"No top-level window matches {title!r}",
			details={"title": title, "exact": exact, "available": available},
		)

	scored.sort(key=lambda pair: pair[0])
	return scored[0][1], len(scored)


def _raise(hwnd: int) -> None:
	"""Restore, raise and claim the foreground for ``hwnd``.

	The topmost toggle raises the window without leaving it pinned above
	everything; attaching this thread's input to the owner's is what makes the
	following ``SetForegroundWindow`` take effect instead of flashing the taskbar.
	"""
	if _user32.IsIconic(hwnd):
		_user32.ShowWindow(hwnd, _SW_RESTORE)

	_user32.SetWindowPos(hwnd, wintypes.HWND(_HWND_TOPMOST), 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE)
	_user32.SetWindowPos(hwnd, wintypes.HWND(_HWND_NOTOPMOST), 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE)

	target_thread = _user32.GetWindowThreadProcessId(hwnd, None)
	current_thread = _kernel32.GetCurrentThreadId()
	attached = bool(_user32.AttachThreadInput(current_thread, target_thread, True))
	try:
		_user32.BringWindowToTop(hwnd)
		_user32.SwitchToThisWindow(hwnd, True)
		_user32.SetForegroundWindow(hwnd)
		_user32.SetFocus(hwnd)
	finally:
		if attached:
			_user32.AttachThreadInput(current_thread, target_thread, False)


def _wait_for_foreground(hwnd: int, timeout_ms: int) -> float | None:
	"""Poll until ``hwnd`` owns the foreground; return how long it took."""
	deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
	while True:
		if foreground_handle() == hwnd:
			return 0.0
		if time.monotonic() >= deadline:
			return None
		time.sleep(0.01)


def activate(
	title: str = "",
	handle: int = 0,
	exact: bool = False,
	timeout_ms: int = 600,
) -> dict[str, Any]:
	"""Raise the window matching ``title`` (or ``handle``) and verify it took.

	A window that is already in front is a success that costs nothing: the tool
	reports ``method: "already-foreground"`` rather than raising it again and
	leaving the caller to wonder whether anything happened.
	"""
	if not title and not handle:
		raise ValueError("'activate_window' needs a title or a handle")

	chosen: dict[str, Any] | None = None
	matched = 0
	if handle:
		for candidate in top_level_windows():
			if candidate["handle"] == int(handle):
				chosen = candidate
				matched = 1
				break
		if chosen is None:
			if not _user32.IsWindow(int(handle)):
				raise DomainUnavailableError(
					f"No window with handle {int(handle)}",
					details={"handle": int(handle)},
				)
			chosen = {"object": None, "handle": int(handle), "title": window_title(int(handle))}
	else:
		chosen, matched = match_window(title, exact)

	hwnd = int(chosen["handle"])
	resolved_title = str(chosen["title"] or window_title(hwnd))
	previous_title = window_title(foreground_handle())
	started = time.monotonic()

	# A minimised window can still be the window Windows calls foreground. It is
	# not in front in any sense the caller means, so it goes through the raise
	# path and comes back restored.
	minimized = bool(_user32.IsIconic(hwnd))
	was_foreground = foreground_handle() == hwnd and not minimized
	if was_foreground:
		waited = 0.0
		method = "already-foreground"
	else:
		_raise(hwnd)
		waited = _wait_for_foreground(hwnd, timeout_ms)
		method = "foreground" if waited is not None else "refused"
	restored = minimized and waited is not None

	if waited is None:
		raise DomainUnavailableError(
			f"Windows refused the foreground for {resolved_title!r}",
			details={
				"handle": hwnd,
				"title": resolved_title,
				"foreground": previous_title,
				"hint": "The window may be elevated, or another process is holding the foreground",
			},
		)

	# The OS foreground and the screen reader's focus are two different facts, and
	# a window can be raised without its content ever taking the keyboard. The
	# reply carries both, so "activated" is not read as "the reader is there now".
	focus_handle, focus_title = focus_window()
	focus_followed = None if not focus_handle else focus_handle == hwnd
	return {
		"activated": True,
		"handle": hwnd,
		"title": resolved_title,
		"previous_title": previous_title,
		"method": method,
		"restored": restored,
		"waited_ms": round((time.monotonic() - started) * 1000.0, 1),
		"matches": matched,
		"focus_in_window": focus_followed,
		"focus_window": "" if focus_followed in (None, True) else focus_title,
		"object": chosen["object"],
	}
