"""The vocabulary for reading a document in order (implementation plan P5.2).

Two vocabularies need translating, and neither translation belongs in the NVDA
layer:

* **item types** — the caller says ``formfield``; NVDA's quick nav calls it
  ``formField``, and each browse mode implementation supports a different
  subset. Lower case in, NVDA's exact spelling out, so a caller does not have to
  know which of ``checkbox``/``checkBox``/``check box`` the screen reader uses.
* **directions** — ``next``, ``previous``, ``first`` and ``last``. NVDA also
  has ``up``, used internally for tables; it is not a reader move and is not
  offered.

Kept out of ``nvda_bridge/reading.py`` for the same reason ``server/locate.py``
is kept out of ``nvda_bridge/locate.py``: a table is testable without NVDA, and
the NVDA layer should be about the document, not about spelling.
"""

from __future__ import annotations

from typing import Optional

#: Caller-facing item type -> the name NVDA's quick navigation uses.
#:
#: Only types that some browse mode implementation can actually answer are
#: listed. ``frame``, ``annotation`` and ``reference`` are Word/Firefox-only,
#: ``landmark`` is Firefox-only (Chromium's provider does not expose landmarks as
#: a searchable node type), and a document that cannot answer reports what it
#: does support rather than an empty list.
ITEM_TYPES: dict[str, str] = {
	"heading": "heading",
	"heading1": "heading1",
	"heading2": "heading2",
	"heading3": "heading3",
	"heading4": "heading4",
	"heading5": "heading5",
	"heading6": "heading6",
	"link": "link",
	"landmark": "landmark",
	"formfield": "formField",
	"button": "button",
	"edit": "edit",
	"list": "list",
	"listitem": "listItem",
	"table": "table",
	"graphic": "graphic",
	"checkbox": "checkBox",
	"radiobutton": "radioButton",
	"combobox": "comboBox",
	"frame": "frame",
	"annotation": "annotation",
	"reference": "reference",
}

#: NVDA's spelling -> the name this API accepts, for reporting what a document
#: supports. First writer wins, so the bare type is preferred over a level.
PUBLIC_NAMES: dict[str, str] = {}
for _public, _nvda in ITEM_TYPES.items():
	PUBLIC_NAMES.setdefault(_nvda, _public)

#: Moves this API offers. ``first`` and ``last`` are absolute; the other two are
#: relative to the document's current position.
DIRECTIONS = ("next", "previous", "first", "last")

#: Targets returned by one enumeration. The list is a map, not a dump: a page
#: with 1,000 links is answered by the first page of them and ``truncated``.
MAX_TARGETS = 50

#: Moves one call may skip. Each step is a position in the document, not a
#: keystroke, so the cap is about keeping one call's cost bounded.
MAX_STEPS = 20

#: How far ``first``/``last`` may scan. A document holding more items than this
#: is reported rather than silently answered with the wrong position.
MAX_SCAN = 2000

#: Main-thread time one enumeration or move may spend, by default.
#:
#: Measured cost is 0.15-0.17 ms per position (Edge ``ChromeVBuf``, Firefox
#: ``Gecko_ia2``), so 1 s is several times the slowest measured request — a
#: 50-target list on a 2,000-link page — and it still bounds the worst case.
#: Without it the only limit is the size of the document: ``steps=20`` costs
#: ~1.3 s and ``last`` costs ~0.35 s per 2,000 positions, on NVDA's main thread,
#: where a screen reader cannot answer anything else.
DEFAULT_TIMEOUT_MS = 1000

#: Ceiling for a caller-supplied budget. A reading call that needs more than
#: this is better split into several calls than left to block the UI.
MAX_TIMEOUT_MS = 5000

#: Why an answer stopped short of the whole document.
STOP_END = "end"
STOP_LIMIT = "limit"
STOP_STEPS = "steps"
STOP_STALLED = "stalled"
STOP_TIMEOUT = "timeout"

#: Longest label or text returned per target. A table's label is its whole text.
MAX_TEXT = 200


def normalize_timeout(timeout_ms: Optional[int]) -> int:
	"""Return a usable main-thread budget, or raise with the valid range."""
	if timeout_ms is None:
		return DEFAULT_TIMEOUT_MS
	if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
		raise ValueError(f"'timeout_ms' must be an integer between 1 and {MAX_TIMEOUT_MS}")
	if not 1 <= timeout_ms <= MAX_TIMEOUT_MS:
		raise ValueError(f"'timeout_ms' must be an integer between 1 and {MAX_TIMEOUT_MS}")
	return timeout_ms


def normalize_item(name: Optional[str]) -> str:
	"""Return the caller-facing item type, or raise with the valid vocabulary.

	Case and separators are ignored because they are not part of the request:
	``formField``, ``form field`` and ``FORMFIELD`` all mean the same thing.
	"""
	key = str(name or "").strip().lower().replace(" ", "").replace("_", "").replace("-", "")
	if key in ITEM_TYPES:
		return key
	raise ValueError(
		f"'item' must be one of {', '.join(ITEM_TYPES)}; got {name!r}",
	)


def nvda_item_type(public_name: str) -> str:
	"""Translate a normalized item type into the spelling NVDA uses."""
	return ITEM_TYPES[public_name]


def public_item_type(nvda_name: str) -> str:
	"""Translate an NVDA item type back, for reporting what is available."""
	return PUBLIC_NAMES.get(nvda_name, nvda_name)


def normalize_direction(direction: Optional[str]) -> str:
	"""Return the move direction, or raise with the valid set."""
	key = str(direction or "").strip().lower()
	if key in DIRECTIONS:
		return key
	raise ValueError(f"'direction' must be one of {', '.join(DIRECTIONS)}; got {direction!r}")


def clip(text: object, limit: int = MAX_TEXT) -> str:
	"""Collapse whitespace and bound a label for the wire.

	A quick navigation label can be the entire content of a heading containing a
	paragraph, and a table's label is the whole table. The caller asked where
	something is, not for the document.
	"""
	value = "" if text is None else str(text)
	collapsed = " ".join(value.split())
	if limit > 0 and len(collapsed) > limit:
		return collapsed[:limit].rstrip() + "…"
	return collapsed
