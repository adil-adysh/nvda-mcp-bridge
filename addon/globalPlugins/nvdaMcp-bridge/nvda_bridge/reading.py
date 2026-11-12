"""Read a document in the order a screen reader reads it (implementation plan P5.2).

Everything else in the bridge is object-centric: it reaches the focus, its
neighbours, controls found by role and name. None of that can answer "the next
heading" or "the links on this page", because the ordering being asked for is
not in the accessibility tree. It is in NVDA's document model — the virtual
buffer for browsers and PDFs, the text-pattern browse mode for Word and UIA
documents — and NVDA exposes it as quick navigation::

    ti = focus.treeInterceptor
    ti._iterNodesByType("heading", "next", ti.selection)   # a stream of items

`locate` on ``role="HEADING"`` is not the same question. It searches the tree,
in tree order, under a UIA-visible-or-not rule of its own; quick navigation
walks a flattened document stream that NVDA built while deciding what a reader
would encounter. A page's reading order and its tree order disagree often enough
that this is a different capability, not a cheaper implementation of one.

Deliberate limits, so the tool stays a screen-reader move rather than a
reimplementation of browse mode:

* the interceptor's own iterator does the work — no ordering is computed here;
* keystrokes are not synthesised. Driving ``script_nextHeading`` through a
  gesture would route through NVDA's focus and the user's key bindings, and
  would swallow the reason for a failure into ``ui.message``;
* a document that cannot answer says what it *can* answer, probed rather than
  assumed (Chromium has no ``landmark`` quick nav type; Firefox does).
"""

from __future__ import annotations

import time
from typing import Any

import api
import logHandler

from ..server import reading as rules
from ..server.state import _enum_name, _safe_get
from .errors import DomainUnavailableError

#: What a UIA or Word document is expected to answer when it cannot be asked
#: directly. Marked ``declared`` in the reply, because it is read out of NVDA's
#: source rather than measured on this machine: the whole point of the
#: ``probed`` path is not to trust a table like this one.
DECLARED_UIA = (
	"heading",
	"heading1",
	"heading2",
	"heading3",
	"heading4",
	"heading5",
	"heading6",
	"link",
	"button",
	"edit",
	"formfield",
	"list",
	"table",
	"graphic",
	"checkbox",
	"radiobutton",
	"combobox",
)

#: Word adds what a word processor has: comments, tracked changes, footnotes.
DECLARED_WORD_EXTRA = ("annotation", "reference", "frame")


def _document_interceptor(obj: Any) -> Any:
	"""The document ``obj`` is inside, if any. Never creates one."""
	if obj is None:
		return None
	try:
		return obj.treeInterceptor
	except Exception as error:  # noqa: BLE001 — "no document here" is the answer
		logHandler.log.debug(f"bridge.reading.interceptor_failed {type(error).__name__}: {error}")
		return None


def _same_process(tree_interceptor: Any, obj: Any) -> bool:
	"""Whether a document lives in the foreground window's process."""
	document_process = _safe_get(_safe_get(tree_interceptor, "rootNVDAObject"), "processID")
	window_process = _safe_get(obj, "processID")
	if not isinstance(document_process, int) or not isinstance(window_process, int):
		# Cannot tell: treat as the same, because switching on a guess would be
		# worse than reading the document the focus is in.
		return True
	return document_process == window_process


def _application_name(obj: Any) -> str:
	"""The app module behind an object, by name.

	A Chromium document lives in a renderer process, so comparing process ids
	would say "different application" for every browser page. The app module is
	the same for the window and its pages, which is the question being asked.
	"""
	return str(_safe_get(_safe_get(obj, "appModule"), "appName") or "")


def _same_application(tree_interceptor: Any, obj: Any) -> bool:
	"""Whether a document belongs to the window's application."""
	document_name = _application_name(_safe_get(tree_interceptor, "rootNVDAObject"))
	window_name = _application_name(obj)
	if document_name and window_name:
		return document_name == window_name
	return _same_process(tree_interceptor, obj)


#: Document models this bridge started, by application and window.
#:
#: NVDA looks a document model up by walking from the focused object, and the
#: focused object is a fresh proxy on each read, so a model that was just
#: created for a Word document is not found again by the next call. Remembering
#: it is cheaper than starting a second model per call — and a second model per
#: call is what would otherwise happen.
_CREATED: dict[tuple[str, int], Any] = {}


def _document_key(obj: Any) -> tuple[str, int]:
	handle = _safe_get(obj, "windowHandle")
	return (_application_name(obj), int(handle) if isinstance(handle, int) else 0)


def _recall_created(obj: Any) -> Any:
	"""The model this bridge started for that document, if it is still alive."""
	key = _document_key(obj)
	created = _CREATED.get(key)
	if created is None:
		return None
	if not _safe_get(created, "isAlive"):
		_CREATED.pop(key, None)
		return None
	return created


def _create_interceptor(obj: Any) -> tuple[Any, bool]:
	"""Ask NVDA to build a document model for ``obj``, as NVDA+space does.

	Some documents are not read in browse mode until asked: a Word document
	declares ``shouldCreateTreeInterceptor = False``, so NVDA keeps it in focus
	mode and the quick navigation machinery never exists. Turning it on is a
	real change to the user's session, which is why it is its own tool rather
	than a flag on a read.

	The model is remembered per application and window, so a caller that asks
	again gets the same document rather than a second model of it. Returns
	``(model, built_now)``: a remembered model is not a change made by this call.
	"""
	import treeInterceptorHandler

	recalled = _recall_created(obj)
	if recalled is not None:
		return recalled, False
	try:
		created = treeInterceptorHandler.update(obj, force=True)
	except Exception as error:  # noqa: BLE001 — not every object has a document
		logHandler.log.debug(f"bridge.reading.create_failed {type(error).__name__}: {error}")
		return None, False
	if created is not None:
		_CREATED[_document_key(obj)] = created
	logHandler.log.info(
		f"bridge.reading.created interface={_interface(created) if created is not None else '-'}",
	)
	return created, created is not None


def _interceptor(root: Any = None, create: bool = False) -> tuple[Any, str]:
	"""Return the document to read, and say why there is none.

	The document is *the focused one*, and only when it belongs to the window in
	front. Focus-first is what a screen reader does — quick nav applies to the
	document the cursor is in — and it is also the only reliable pointer to which
	of a browser's tabs is showing: one window holds a document per tab, so
	"the foreground window's document" is not a question the document model can
	answer.

	When the two disagree the reply is a refusal, because the alternatives are to
	read a document behind the window in front or to guess among tabs. The
	reason names the fix, which is one keystroke.

	``create`` builds a model for a document that is being read in focus mode.
	Only ``create_document`` (the ``start_browse_mode`` action) passes it: forcing
	a document model changes the user's session, so it is not a side effect of a
	question.
	"""
	if root is not None:
		tree_interceptor = _document_interceptor(root)
		if tree_interceptor is not None:
			return tree_interceptor, ""
		if create:
			tree_interceptor, _built = _create_interceptor(root)
			if tree_interceptor is not None:
				return tree_interceptor, ""
		# A model this bridge started stays usable for the read that follows:
		# reusing it is not a session change, it is remembering our own work.
		tree_interceptor = _recall_created(root)
		if tree_interceptor is not None:
			return tree_interceptor, ""
		return None, (
			"the target is not inside a document that NVDA reads in browse mode; "
			"if it is a document NVDA is reading in focus mode, start browse mode first"
		)

	try:
		focus = api.getFocusObject()
	except Exception:  # noqa: BLE001
		focus = None
	try:
		foreground = api.getForegroundObject()
	except Exception:  # noqa: BLE001
		foreground = None

	focus_document = _document_interceptor(focus)
	if focus_document is None and create and focus is not None:
		focus_document, _built = _create_interceptor(focus)
	if focus_document is None and focus is not None:
		# The model may be one this bridge started in an earlier call; using it
		# again is the point of starting it.
		focus_document = _recall_created(focus)
	logHandler.log.debug(
		f"bridge.reading.resolve focus={_safe_get(focus, 'name')!r} pid={_safe_get(focus, 'processID')!r} "
		f"app={_application_name(focus)!r} foreground={_safe_get(foreground, 'name')!r} "
		f"fg_pid={_safe_get(foreground, 'processID')!r} fg_app={_application_name(foreground)!r} "
		f"document={_interface(focus_document) if focus_document is not None else '-'} create={create}",
	)
	if focus_document is None:
		if focus is None:
			return None, "there is no focus object to read from"
		if create:
			return None, "this object does not support browse mode, even when asked to start it"
		return None, (
			"the target is not inside a document that NVDA reads in browse mode; "
			"if it is a document NVDA is reading in focus mode, start browse mode first"
		)

	if foreground is None or _same_application(focus_document, foreground):
		return focus_document, ""

	# The focus is in a document, but not in the window in front. Which document
	# a browser window is showing cannot be recovered from the document model —
	# one window holds a document per tab — so guessing would answer about the
	# wrong page. Refusing is the only honest move, and the reason is the fix.
	return None, (
		"the window in front does not hold the page focus; "
		"put focus in its page first (F6), then ask again"
	)

	return None, "the target is not inside a document that NVDA reads in browse mode"


def _interface(tree_interceptor: Any) -> str:
	"""Which browse mode implementation is answering, for the reply."""
	return type(tree_interceptor).__name__


def _document(tree_interceptor: Any) -> str:
	"""The document's own name — usually the page title.

	A Word document deliberately carries no name of its own, so the enclosing
	window answers instead: it is what says which file is open.
	"""
	root = _safe_get(tree_interceptor, "rootNVDAObject")
	name = _safe_get(root, "name")
	if not name:
		node = root
		for _ in range(4):
			node = _safe_get(node, "parent")
			if node is None:
				break
			if _enum_name(_safe_get(node, "role")) in ("WINDOW", "DIALOG"):
				name = _safe_get(node, "name")
				break
	return rules.clip(name or "", 120)


def _pass_through(tree_interceptor: Any) -> bool:
	"""Whether the document is in focus mode.

	In focus mode NVDA passes quick nav keys to the application, so the move is
	still available to a tool but it is not what the user's keyboard would do.
	The reply says which mode was in force when the move happened.
	"""
	return bool(_safe_get(tree_interceptor, "passThrough"))


def _supported(tree_interceptor: Any, exhaustive: bool = False) -> tuple[list[str], str]:
	"""Return what this document can be searched by, and how that was learned.

	``probed`` asks the implementation's own type table (one pure lookup per
	candidate). ``declared`` is the fallback for the UIA and Word documents that
	have no such table: it is read out of NVDA's source and says so rather than
	pretending to have been measured.

	``exhaustive`` measures instead, by asking the document for one position of
	each type. That is ten times the work, so it runs only when a type has
	already been refused — which is exactly when the caller needs the list.
	"""
	probe = getattr(tree_interceptor, "_searchableAttribsForNodeType", None)
	if callable(probe):
		found: list[str] = []
		for public_name in rules.ITEM_TYPES:
			nvda_name = rules.nvda_item_type(public_name)
			try:
				attributes = probe(nvda_name)
			except Exception:  # noqa: BLE001 — an unsupported type may raise
				continue
			if attributes:
				found.append(public_name)
		return found, "probed"

	if exhaustive:
		attempted = [name for name in rules.ITEM_TYPES if _accepts(tree_interceptor, name)]
		if attempted:
			return attempted, "attempted"

	declared = list(DECLARED_UIA)
	if "Word" in _interface(tree_interceptor):
		declared.extend(item for item in DECLARED_WORD_EXTRA if item not in declared)
	return [item for item in rules.ITEM_TYPES if item in declared], "declared"


def _accepts(tree_interceptor: Any, public_name: str) -> bool:
	"""Whether the document can produce a position of this kind."""
	iterate = getattr(tree_interceptor, "_iterNodesByType", None)
	if not callable(iterate):
		return False
	try:
		iterator = iterate(rules.nvda_item_type(public_name), "next", None)
	except NotImplementedError:
		return False
	except Exception:  # noqa: BLE001
		return False
	try:
		next(iterator)
	except StopIteration:
		# An empty document still answers, with nothing.
		return True
	except NotImplementedError:
		return False
	except Exception:  # noqa: BLE001
		return False
	return True


def _unsupported(tree_interceptor: Any, public_name: str) -> DomainUnavailableError:
	available, source = _supported(tree_interceptor, exhaustive=True)
	return DomainUnavailableError(
		f"this document cannot be searched by {public_name!r}",
		details={
			"item": public_name,
			"available": available,
			"source": source,
			"interface": _interface(tree_interceptor),
			"document": _document(tree_interceptor),
		},
	)


def _iterator(tree_interceptor: Any, public_name: str, direction: str, pos: Any) -> Any:
	"""Ask the document for a stream of positions, in document order."""
	iterate = getattr(tree_interceptor, "_iterNodesByType", None)
	if not callable(iterate):
		raise DomainUnavailableError(
			"this document type does not support quick navigation",
			details={
				"item": public_name,
				"interface": _interface(tree_interceptor),
				"document": _document(tree_interceptor),
			},
		)
	nvda_name = rules.nvda_item_type(public_name)
	try:
		return iterate(nvda_name, direction, pos)
	except NotImplementedError as error:
		raise _unsupported(tree_interceptor, public_name) from error
	except Exception as error:  # noqa: BLE001 — a document can die mid-search
		raise DomainUnavailableError(
			f"the document could not be searched for {public_name!r}: {type(error).__name__}: {error}",
			details={"item": public_name, "interface": _interface(tree_interceptor)},
		) from error


def _advance(iterator: Any, tree_interceptor: Any, public_name: str) -> Any:
	"""Pull the next position, or ``None`` at the end of the document."""
	try:
		return next(iterator)
	except StopIteration:
		return None
	except NotImplementedError as error:
		raise _unsupported(tree_interceptor, public_name) from error
	except Exception as error:  # noqa: BLE001
		raise DomainUnavailableError(
			f"the document could not be read for {public_name!r}: {type(error).__name__}: {error}",
			details={"item": public_name, "interface": _interface(tree_interceptor)},
		) from error


#: Why ``_pick`` stopped, for the caller that has to explain it.
_PICK_COMPLETE = "complete"


class _Budget:
	"""How long one call may spend inside NVDA's main thread.

	Checking between document operations — not inside one, which cannot be
	interrupted — is what keeps a big document from blocking the screen reader:
	an implementation that takes a second per position still yields after the
	first one, with an honest partial answer.

	The deadline is monotonic, so a clock change cannot extend it.
	"""

	def __init__(self, timeout_ms: int) -> None:
		self.started = time.monotonic()
		self.timeout_ms = max(1, int(timeout_ms))
		self.deadline = self.started + self.timeout_ms / 1000.0

	def expired(self) -> bool:
		return time.monotonic() >= self.deadline

	def elapsed_ms(self) -> float:
		return round((time.monotonic() - self.started) * 1000.0, 1)

	def remaining_ms(self) -> int:
		return max(0, int((self.deadline - time.monotonic()) * 1000.0))


def _pick(
	iterator: Any,
	tree_interceptor: Any,
	public_name: str,
	count: int,
	budget: _Budget,
) -> tuple[Any, int, str]:
	"""Keep the ``count``-th position; say how many were reached and why it stopped.

	``complete`` means ``count`` positions were reached and more may follow,
	``end`` that the document ran out, and ``timeout`` that the budget expired
	first — which is a refusal, not a partial answer about "last".
	"""
	chosen = None
	reached = 0
	while reached < count:
		if budget.expired():
			return chosen, reached, rules.STOP_TIMEOUT
		candidate = _advance(iterator, tree_interceptor, public_name)
		if candidate is None:
			return chosen, reached, rules.STOP_END
		chosen = candidate
		reached += 1
	return chosen, reached, _PICK_COMPLETE


def _object_of(item: Any, tree_interceptor: Any = None) -> Any:
	"""The NVDA object at a quick navigation position, when there is one.

	A position can be plain text — a heading is a range, not a control — so the
	absence of an object is normal and the caller gets a label instead of a ref.
	The document itself is never returned: a reference that can only mean "the
	whole page" reads as an answer while doing nothing when invoked.
	"""
	try:
		obj = item.obj
	except Exception:  # noqa: BLE001 — a position can die with its document
		return None
	if obj is None:
		return None
	if tree_interceptor is not None and obj is _safe_get(tree_interceptor, "rootNVDAObject"):
		return None
	if _enum_name(_safe_get(obj, "role")) in ("DOCUMENT", "APPLICATION", "ROOTPANE"):
		return None
	return obj


def _control_attributes(item: Any) -> dict:
	"""The position's own node attributes, where the implementation has them.

	A virtual buffer keeps a heading's level in the control field rather than on
	the object, and NVDA's own quick nav reads it from here to decide whether one
	heading nests under another. Word documents have no such field, so the empty
	dictionary is the normal answer there.
	"""
	identifier = _safe_get(item, "vbufFieldIdentifier")
	text_info = _safe_get(item, "textInfo")
	if not identifier or text_info is None:
		return {}
	fetch = getattr(text_info, "_getControlFieldAttribs", None)
	if not callable(fetch):
		return {}
	try:
		return fetch(*identifier) or {}
	except Exception:  # noqa: BLE001 — a position can die with its document
		return {}


def _heading_level(item: Any, obj: Any = None) -> int:
	"""How deep a heading sits, from the position, its node, or its object.

	Three document models keep this in three places: a UIA text range answers
	with a ``level`` property, a virtual buffer node keeps it in the control
	field, and a Word or UIA element puts it on the object.
	"""
	level = _safe_get(item, "level")
	if isinstance(level, int) and not isinstance(level, bool) and level > 0:
		return level

	attributes = _control_attributes(item)
	if attributes:
		try:
			level = int(attributes.get("level"))
		except (TypeError, ValueError):
			level = 0
		if level > 0:
			return level

	if obj is not None:
		level = _safe_get(obj, "headingLevel")
		if isinstance(level, int) and not isinstance(level, bool) and level > 0:
			return level
	return 0


def _describe(item: Any, item_type: str = "", tree_interceptor: Any = None) -> dict[str, Any]:
	"""Label, text, role and heading level for one position, all bounded.

	The level is only looked for on headings: reading a node's control field for
	a link would cost a document call per item to learn nothing.
	"""
	record: dict[str, Any] = {"label": "", "text": "", "role": "", "level": 0}
	try:
		record["label"] = rules.clip(item.label)
	except Exception:  # noqa: BLE001 — a label is a convenience, not the answer
		pass
	text_info = _safe_get(item, "textInfo")
	if text_info is not None:
		record["text"] = rules.clip(_safe_get(text_info, "text"))

	obj = _object_of(item, tree_interceptor)
	if obj is not None:
		record["role"] = _enum_name(_safe_get(obj, "role"))
	if item_type.startswith("heading"):
		record["level"] = _heading_level(item, obj)
	return record


def _selection(tree_interceptor: Any) -> Any:
	"""The document's current reading position."""
	try:
		return tree_interceptor.selection
	except Exception as error:  # noqa: BLE001
		raise DomainUnavailableError(
			"the document has no current reading position",
			details={"interface": _interface(tree_interceptor), "reason": type(error).__name__},
		) from error


def _move_to(candidate: Any, tree_interceptor: Any, public_name: str) -> None:
	"""Move the document's cursor to a position."""
	try:
		candidate.moveTo()
	except Exception as error:  # noqa: BLE001
		raise DomainUnavailableError(
			f"the {public_name} could not be reached: {type(error).__name__}: {error}",
			details={"item": public_name, "interface": _interface(tree_interceptor)},
		) from error


def _landing_object(candidate: Any, tree_interceptor: Any) -> Any:
	"""The object under the cursor after a move, for the reference.

	A position backed by a control carries its own object. A position in plain
	text does not, and the cursor's object is then the *document* itself, which
	would make a reference that can only ever mean "the whole page" — worse than
	no reference at all, because ``invoke`` on it would look like an answer.
	"""
	obj = _object_of(candidate, tree_interceptor)
	if obj is not None:
		return obj
	try:
		current = tree_interceptor.currentNVDAObject
	except Exception:  # noqa: BLE001
		return None
	if current is None or current is _safe_get(tree_interceptor, "rootNVDAObject"):
		return None
	if _enum_name(_safe_get(current, "role")) in ("DOCUMENT", "APPLICATION", "ROOTPANE"):
		return None
	return current


def capabilities(root: Any = None, create: bool = False) -> dict[str, Any]:
	"""What the current document can be read by, without searching it."""
	tree_interceptor, reason = _interceptor(root, create=create)
	if tree_interceptor is None:
		return {
			"browse_mode": False,
			"document": "",
			"interface": "",
			"items": [],
			"source": "",
			"pass_through": False,
			"reason": reason,
		}
	items, source = _supported(tree_interceptor)
	return {
		"browse_mode": True,
		"document": _document(tree_interceptor),
		"interface": _interface(tree_interceptor),
		"items": items,
		"source": source,
		"pass_through": _pass_through(tree_interceptor),
		"reason": "",
	}


def create_document(root: Any = None) -> dict[str, Any]:
	"""Build the document model NVDA+space would build, and report it.

	Some documents are not read in browse mode until asked: a Word document
	declares ``shouldCreateTreeInterceptor = False``, so NVDA keeps it in focus
	mode and the quick navigation machinery never exists. Creating the model is a
	change to the user's session, so it is an action of its own — a read must not
	be able to do it, and the reply says whether this call is what changed
	anything (``created``) or a model was already there.

	Returns a payload rather than an object: the useful next step is to ask the
	document a question, not to hold a reference to it.
	"""
	started = time.monotonic()
	target = root
	if target is None:
		try:
			target = api.getFocusObject()
		except Exception:  # noqa: BLE001
			target = None
	if target is None:
		raise DomainUnavailableError(
			"there is no focused object to start browse mode on",
			details={"created": False, "browse_mode": False},
		)

	existing = _document_interceptor(target)
	if existing is not None:
		created, built = existing, False
	else:
		created, built = _create_interceptor(target)
	if created is None:
		raise DomainUnavailableError(
			"this object does not support browse mode, even when asked to start it",
			details={
				"created": False,
				"browse_mode": False,
				"document": rules.clip(_safe_get(target, "name") or "", 120),
			},
		)

	elapsed_ms = round((time.monotonic() - started) * 1000.0, 1)
	logHandler.log.debug(
		f"bridge.reading.create_document document={_document(created)!r} "
		f"interface={_interface(created)} created={built} elapsed_ms={elapsed_ms}",
	)
	return {
		"created": bool(built),
		"browse_mode": True,
		"document": _document(created),
		"interface": _interface(created),
		"pass_through": _pass_through(created),
		"elapsed_ms": elapsed_ms,
	}


def targets(
	item: str,
	limit: int = 20,
	root: Any = None,
	timeout_ms: int = rules.DEFAULT_TIMEOUT_MS,
) -> tuple[dict[str, Any], list[Any]]:
	"""Enumerate positions of one kind, in document order, without moving.

	Returns ``(payload, objects)``; ``objects[i]`` backs ``payload["targets"][i]``
	and is ``None`` when the position is plain text rather than a control. The
	caller attaches references, exactly as ``locate`` does.

	``timeout_ms`` bounds the work done on NVDA's main thread. ``stopped`` says
	why the list ends where it does: ``end`` of the document, the requested
	``limit``, or the ``timeout``. A list cut by the budget still says
	``truncated``, so a partial answer can never be mistaken for the whole
	document.
	"""
	public_name = rules.normalize_item(item)
	budget = _Budget(timeout_ms)
	tree_interceptor, reason = _interceptor(root)
	if tree_interceptor is None:
		raise DomainUnavailableError(
			"there is no document to read here",
			details={"browse_mode": False, "reason": reason, "item": public_name, "available": []},
		)

	available, source = _supported(tree_interceptor)
	records: list[dict[str, Any]] = []
	objects: list[Any] = []
	truncated = False
	scanned = 0
	stopped = rules.STOP_END

	iterator = _iterator(tree_interceptor, public_name, "next", None)
	while len(records) < limit:
		if budget.expired():
			truncated = True
			stopped = rules.STOP_TIMEOUT
			break
		candidate = _advance(iterator, tree_interceptor, public_name)
		if candidate is None:
			stopped = rules.STOP_END
			break
		scanned += 1
		record = _describe(candidate, public_name, tree_interceptor)
		objects.append(_object_of(candidate, tree_interceptor))
		records.append(record)

	if len(records) >= limit and stopped != rules.STOP_TIMEOUT:
		# One extra position is pulled on purpose: it is what tells "exactly the
		# limit" apart from "the list was cut here".
		if budget.expired():
			truncated = True
			stopped = rules.STOP_TIMEOUT
		else:
			extra = _advance(iterator, tree_interceptor, public_name)
			if extra is None:
				stopped = rules.STOP_END
			else:
				scanned += 1
				truncated = True
				stopped = rules.STOP_LIMIT

	elapsed_ms = budget.elapsed_ms()
	logHandler.log.debug(
		f"bridge.reading.targets item={public_name} found={len(records)} scanned={scanned} "
		f"truncated={truncated} stopped={stopped} elapsed_ms={elapsed_ms}",
	)
	return {
		"item": public_name,
		"available": available,
		"browse_mode": True,
		"document": _document(tree_interceptor),
		"interface": _interface(tree_interceptor),
		"pass_through": _pass_through(tree_interceptor),
		"source": source,
		"targets": records,
		"truncated": truncated,
		"stopped": stopped,
		"scanned": scanned,
		"timeout_ms": int(timeout_ms),
		"elapsed_ms": elapsed_ms,
	}, objects


def navigate(
	item: str,
	direction: str = "next",
	steps: int = 1,
	root: Any = None,
	timeout_ms: int = rules.DEFAULT_TIMEOUT_MS,
) -> tuple[dict[str, Any], Any]:
	"""Move the document's reading position, and report where it landed.

	Like pressing the quick nav key: moving three headings forward from the
	fourth heading lands on the seventh, and if the document ends first the
	cursor stops at the last one — ``reached`` says how many moves happened,
	``steps`` how many were asked for. ``first`` starts at the top of the
	document, so ``first`` with ``steps=3`` asks for the third position in it.

	``stopped`` says why the move ended: ``steps`` (all of them were made),
	``end`` (the document ran out), ``stalled`` (the search came back to where
	it already was) or ``timeout`` (the budget expired between steps). Landing
	short is reported, never retried blindly — the cursor really did move that
	far, and the screen reader announced it.
	"""
	public_name = rules.normalize_item(item)
	move = rules.normalize_direction(direction)
	budget = _Budget(timeout_ms)
	tree_interceptor, reason = _interceptor(root)
	if tree_interceptor is None:
		raise DomainUnavailableError(
			"there is no document to read here",
			details={"browse_mode": False, "reason": reason, "item": public_name, "available": []},
		)

	available, _source = _supported(tree_interceptor)
	focus_mode = _pass_through(tree_interceptor)
	wanted = max(1, int(steps))
	iteration = "previous" if move == "previous" else "next"
	stopped = rules.STOP_STEPS

	if move == "last":
		# One position past the cap is what tells "exactly MAX_SCAN" apart from
		# "more than MAX_SCAN": the scan stops at the cap, so it can only tell
		# them apart by reaching one further.
		candidate, reached, why = _pick(
			_iterator(tree_interceptor, public_name, "next", None),
			tree_interceptor,
			public_name,
			rules.MAX_SCAN + 1,
			budget,
		)
		if why == rules.STOP_TIMEOUT:
			raise DomainUnavailableError(
				f"could not find the last {public_name} within {int(timeout_ms)} ms "
				f"({reached} positions scanned); the document is too long to scan",
				details={
					"item": public_name,
					"direction": move,
					"scanned": reached,
					"timeout_ms": int(timeout_ms),
					"interface": _interface(tree_interceptor),
				},
			)
		if why == _PICK_COMPLETE:
			raise DomainUnavailableError(
				f"this document holds more than {rules.MAX_SCAN} {public_name} positions; "
				"ask for 'next' from the start instead of 'last'",
				details={
					"item": public_name,
					"direction": move,
					"scanned": reached,
					"interface": _interface(tree_interceptor),
				},
			)
		stopped = rules.STOP_END
	else:
		# One search per step, moving in between. A document model that is
		# searched *from a position* can yield a single match — NVDA's UIA
		# documents do — so stepping through several positions has to advance
		# the cursor between searches, which is also what pressing the key
		# repeatedly does.
		candidate = None
		reached = 0
		landed_label = None
		for _ in range(wanted):
			if budget.expired():
				stopped = rules.STOP_TIMEOUT
				break
			start = None if move == "first" and reached == 0 else _selection(tree_interceptor)
			found = _advance(
				_iterator(tree_interceptor, public_name, iteration, start),
				tree_interceptor,
				public_name,
			)
			if found is None:
				# The document ended first: the cursor stays where the last
				# successful move put it, exactly as repeated key presses would.
				stopped = rules.STOP_END
				break
			label = _safe_get(found, "label")
			if isinstance(label, str) and label == landed_label:
				# No progress: the search came back to where it already is.
				stopped = rules.STOP_STALLED
				break
			landed_label = label
			candidate = found
			reached += 1
			_move_to(found, tree_interceptor, public_name)

	if candidate is None:
		raise DomainUnavailableError(
			f"there is no {move} {public_name} in this document",
			details={
				"item": public_name,
				"direction": move,
				"available": available,
				"interface": _interface(tree_interceptor),
				"document": _document(tree_interceptor),
			},
		)
	if move == "last":
		_move_to(candidate, tree_interceptor, public_name)
	record = _describe(candidate, public_name, tree_interceptor)
	elapsed_ms = budget.elapsed_ms()
	logHandler.log.debug(
		f"bridge.reading.navigate item={public_name} direction={move} "
		f"steps={steps} reached={reached} stopped={stopped} elapsed_ms={elapsed_ms}",
	)
	payload = {
		"item": public_name,
		"direction": move,
		"steps": int(steps),
		"reached": int(reached),
		"stopped": stopped,
		"ref": "",
		**record,
		"landed": True,
		"browse_mode": True,
		"document": _document(tree_interceptor),
		"interface": _interface(tree_interceptor),
		"pass_through": focus_mode,
		"timeout_ms": int(timeout_ms),
		"elapsed_ms": elapsed_ms,
	}
	return payload, _landing_object(candidate, tree_interceptor)
