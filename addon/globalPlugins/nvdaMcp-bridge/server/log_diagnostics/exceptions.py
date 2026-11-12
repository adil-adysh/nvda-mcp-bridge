"""Parse Python exception tracebacks out of NVDA's native log.

NVDA writes the ``Traceback (most recent call last):`` block as ordinary
continuation lines of one log record. A traceback is therefore *inside* a
record, not a record of its own. Parsing is deliberately conservative: a line
that merely contains the word ``Error`` is not an exception, and a traceback
that ends before its exception line is reported as partial rather than guessed.
"""

from __future__ import annotations

import re
from typing import Any, Optional


_TRACEBACK_RE = re.compile(r"^\s*Traceback \(most recent call last\):\s*$")
_FRAME_RE = re.compile(r'^\s*File "(?P<file>.+?)", line (?P<line>\d+)(?:, in (?P<function>.+?))?\s*$')
_EXCEPTION_RE = re.compile(
	r"^(?P<type>(?:[A-Za-z_][\w]*\.)*[A-Za-z_][\w]*(?:Error|Exception|Warning|Interrupt|Exit|Fault|Failure|Abort|Overflow|UnicodeDecodeError|UnicodeEncodeError))"
	r"(?::\s?(?P<message>.*))?$"
)
#: An exception with no colon (``KeyboardInterrupt``) is still a terminal line.
_BARE_EXCEPTION_RE = re.compile(r"^(?P<type>KeyboardInterrupt|SystemExit|GeneratorExit|StopIteration)$")
#: A traceback block has frames, so an unindented dotted identifier after them
#: is an exception even when its class name does not end in ``Error`` (add-ons
#: can define ``MyProblem``). This fallback only runs after at least one frame.
_FALLBACK_EXCEPTION_RE = re.compile(r"^(?P<type>[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*)(?::\s?(?P<message>.*))?$")
_CHAIN_RE = re.compile(
	r"^(?P<relation>The above exception was the direct cause|During handling of the above exception)"
	r"[^\n]*:\s*$"
)
_SYNTAX_TAIL_RE = re.compile(r"^\s*(?:\^+|~+)\s*$")

MAX_EXCEPTION_TEXT = 20000
MAX_FRAMES = 80
MAX_CHAIN = 8
MAX_MESSAGE = 2000


def parse_exception(message: str) -> Optional[dict[str, Any]]:
	"""Return a structured traceback from ``message``, or ``None``.

	The returned record always carries ``parse_status`` and, for partial
	parse, the original traceback text under ``raw_traceback``. It never
	invents a missing type or frame.
	"""
	if "Traceback (most recent call last):" not in message:
		return None
	lines = message.splitlines()
	starts = [index for index, line in enumerate(lines) if _TRACEBACK_RE.match(line)]
	if not starts:
		return None

	blocks: list[dict[str, Any]] = []
	raw_sections: list[str] = []
	for start_index in starts:
		# The separator line immediately before a later traceback says how the
		# blocks are related. It is evidence, not a guess.
		relation = "outer"
		for probe in range(start_index - 1, max(-1, start_index - 4), -1):
			match = _CHAIN_RE.match(lines[probe].strip())
			if match:
				relation = "direct_cause" if "direct cause" in match.group("relation") else "during_handling"
				break
		block = _parse_block(lines, start_index)
		# A separator names the relationship between the block that precedes
		# it and the block that follows. Record it on the preceding block, so
		# chain[i].relation describes how chain[i] leads to the next exception.
		if blocks:
			blocks[-1]["relation"] = relation if relation != "outer" else "context"
		block["relation"] = "outer"
		blocks.append(block)
		raw_sections.append("\n".join(lines[start_index : block.pop("_end")]))

	if not blocks:
		return None

	primary = blocks[-1]
	chain = []
	for block in blocks[:-1][-MAX_CHAIN:]:
		chain.append(
			{
				"type": block.get("type"),
				"message": block.get("message"),
				"frames": block.get("frames") or [],
				"relation": block.get("relation"),
				"parse_status": block.get("parse_status"),
			}
		)

	status = "complete" if all(block.get("type") and block.get("parse_status") == "complete" for block in blocks) else "partial"
	exception: dict[str, Any] = {
		"type": primary.get("type"),
		"message": primary.get("message"),
		"frames": primary.get("frames") or [],
		"chain": chain,
		"parse_status": status,
		"traceback_line_offset": starts[-1],
	}
	if status != "complete":
		exception["raw_traceback"] = "\n".join(raw_sections)[:MAX_EXCEPTION_TEXT]
		if len("\n".join(raw_sections)) > MAX_EXCEPTION_TEXT:
			exception["raw_traceback_truncated"] = True
	return exception


def _parse_block(lines: list[str], start_index: int) -> dict[str, Any]:
	"""Parse one ``Traceback`` block starting at ``start_index``."""
	frames: list[dict[str, Any]] = []
	exception_type: Optional[str] = None
	exception_message = ""
	end = start_index + 1
	status = "partial"
	saw_frame = False
	index = start_index + 1
	while index < len(lines):
		line = lines[index]
		frame = _FRAME_RE.match(line)
		if frame:
			saw_frame = True
			if len(frames) < MAX_FRAMES:
				frames.append(
					{
						"file": _short_file(frame.group("file")),
						"line": int(frame.group("line")),
						"function": (frame.group("function") or "").strip(),
					}
				)
			index += 1
			end = index
			continue
		# Source lines under a frame are indented, and SyntaxError adds caret
		# lines. They are not frames and must not be mistaken for exceptions.
		if line.startswith((" ", "\t")) or _SYNTAX_TAIL_RE.match(line):
			index += 1
			end = index
			continue
		exception = _EXCEPTION_RE.match(line.strip()) or _BARE_EXCEPTION_RE.match(line.strip())
		if exception is None and saw_frame and line == line.lstrip():
			exception = _FALLBACK_EXCEPTION_RE.match(line.strip())
		if exception:
			exception_type = exception.group("type").split(".")[-1]
			exception_message = (exception.groupdict().get("message") or "")[:MAX_MESSAGE]
			status = "complete"
			end = index + 1
			break
		# A non-indented, non-exception line ends the block. This happens when
		# NVDA truncates a traceback before its terminal exception, which is a
		# partial parse, not a different format.
		break

	return {
		"type": exception_type,
		"message": exception_message,
		"frames": frames,
		"parse_status": status if (exception_type or saw_frame) else "partial",
		"_end": end,
	}


def _short_file(path: str) -> str:
	"""Keep the part of a frame path an add-on developer can act on."""
	cleaned = path.replace("\\", "/").strip()
	markers = ("/addons/", "/globalPlugins/", "/appModules/", "/NVDA/")
	for marker in markers:
		position = cleaned.find(marker)
		if position >= 0:
			return cleaned[position + 1 :]
	if len(cleaned) > 200:
		return cleaned[-200:]
	return cleaned
