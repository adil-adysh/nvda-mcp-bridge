# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Tests for document-order reading: `list_reading_targets` and `navigate_reading`.

Three things are worth pinning without a screen reader running:

* the vocabulary — item types are case- and separator-insensitive, an unknown
  one names the valid set, and directions are the four moves a reader has;
* the enumeration contract — document order, bounded labels, truncation
  detected by one deliberate extra pull rather than by guessing, and a document
  that cannot answer a type saying what it *can* answer;
* the handler's bookkeeping — one ref per position, none for plain text, and
  `navigate_reading` reporting where it landed after settling.

The document is a fake but the protocol is not: the reading layer is exercised
through the real module, with only NVDA's `api`/`logHandler` standing in.
"""

import importlib
import os
import sys
import time
import types
import unittest

_ADDON_DIR = os.path.normpath(
	os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "addon", "globalPlugins", "nvdaMcp-bridge")
)
if _ADDON_DIR not in sys.path:
	sys.path.insert(0, _ADDON_DIR)

if "logHandler" not in sys.modules:
	sys.modules["logHandler"] = types.SimpleNamespace(
		log=types.SimpleNamespace(
			debug=lambda *args, **kwargs: None,
			info=lambda *args, **kwargs: None,
			warning=lambda *args, **kwargs: None,
			error=lambda *args, **kwargs: None,
		)
	)

_PACKAGE_NAME = "nvdaMcpBridge"
if _PACKAGE_NAME not in sys.modules:
	_package = types.ModuleType(_PACKAGE_NAME)
	_package.__path__ = [_ADDON_DIR]
	sys.modules[_PACKAGE_NAME] = _package

# The bridge package pulls in NVDA itself; register a stand-in parent carrying
# the real directory so its submodules load straight from disk.
_BRIDGE_KEY = f"{_PACKAGE_NAME}.nvda_bridge"
if _BRIDGE_KEY not in sys.modules or not hasattr(sys.modules[_BRIDGE_KEY], "__path__"):
	_bridge_package = types.ModuleType(_BRIDGE_KEY)
	_bridge_package.__path__ = [os.path.join(_ADDON_DIR, "nvda_bridge")]
	sys.modules[_BRIDGE_KEY] = _bridge_package

if "api" not in sys.modules:
	sys.modules["api"] = types.SimpleNamespace(getFocusObject=lambda: None)

rules = importlib.import_module(f"{_PACKAGE_NAME}.server.reading")
reading = importlib.import_module(f"{_PACKAGE_NAME}.nvda_bridge.reading")
errors = importlib.import_module(f"{_PACKAGE_NAME}.nvda_bridge.errors")
handlers = importlib.import_module(f"{_PACKAGE_NAME}.tools.handlers")
models = importlib.import_module(f"{_PACKAGE_NAME}.tools.models")
validator = importlib.import_module(f"{_PACKAGE_NAME}.validation.dataclass_validator")


# ============================================================================
# Fakes: a document the reading layer can actually walk
# ============================================================================


class Item:
	"""One quick navigation position."""

	def __init__(self, label, text=None, role="HEADING", level=0, obj=None):
		self.label = label
		self.textInfo = types.SimpleNamespace(text=label if text is None else text)
		self._obj = obj
		self.moved = False

	@property
	def obj(self):
		return self._obj

	def moveTo(self):
		self.moved = True
		document = getattr(self, "document", None)
		if document is not None:
			# Moving the cursor is what makes the next search start after it.
			document.position = getattr(self, "index", 0) + 1


class Document:
	"""A browse mode document with no type table (the Word/UIA shape).

	A document searched from a position answers with what follows that position,
	which is what makes stepping through several positions observable.
	"""

	def __init__(self, items, name="Fake Page", pass_through=False):
		self.items = list(items)
		for index, item in enumerate(self.items):
			item.index = index
			item.document = self
		self.position = 0
		self.rootNVDAObject = types.SimpleNamespace(name=name)
		self.passThrough = pass_through
		self.selection = "caret"
		self.currentNVDAObject = types.SimpleNamespace(role="DOCUMENT")
		self.calls = []

	def _iterNodesByType(self, itemType, direction="next", pos=None):
		self.calls.append((itemType, direction, pos))
		start = 0 if pos is None else self.position
		if direction == "previous":
			return iter(list(reversed(self.items[:start])))
		return iter(self.items[start:])


class ProbingDocument(Document):
	"""A document that can say which types it supports (the virtual buffer shape)."""

	def __init__(self, items, supported=("heading", "link", "heading1"), **kwargs):
		super().__init__(items, **kwargs)
		self.supported = set(supported)

	def _searchableAttribsForNodeType(self, nodeType):
		return {"role": [nodeType]} if nodeType in self.supported else None

	def _iterNodesByType(self, itemType, direction="next", pos=None):
		# Chromium's virtual buffer does exactly this: a type it never mapped
		# raises, rather than answering with an empty list.
		if itemType not in self.supported:
			raise NotImplementedError
		return super()._iterNodesByType(itemType, direction, pos)


class Host:
	"""An object that owns a document, which is how the interceptor is found."""

	def __init__(self, document):
		self.treeInterceptor = document


def heading(label, level=2):
	return Item(label, role="HEADING", level=level, obj=types.SimpleNamespace(role="HEADING", headingLevel=level))


HEADINGS = [heading("Introduction"), heading("Methods"), heading("Results")]


# ============================================================================
# Vocabulary
# ============================================================================


class VocabularyTests(unittest.TestCase):
	def test_item_types_ignore_case_and_separators(self):
		for written in ("formfield", "formField", "form field", "FORM_FIELD", "Form-Field"):
			self.assertEqual(rules.normalize_item(written), "formfield", written)
		self.assertEqual(rules.normalize_item("Heading 3"), "heading3")

	def test_an_unknown_item_names_the_valid_set(self):
		with self.assertRaises(ValueError) as caught:
			rules.normalize_item("paragraph")
		message = str(caught.exception)
		self.assertIn("heading", message)
		self.assertIn("link", message)

	def test_directions_are_the_four_moves(self):
		for written in ("next", "NEXT", " previous ", "first", "last"):
			self.assertIn(rules.normalize_direction(written), rules.DIRECTIONS)
		with self.assertRaises(ValueError):
			rules.normalize_direction("up")

	def test_nvda_spelling_is_translated_both_ways(self):
		self.assertEqual(rules.nvda_item_type("formfield"), "formField")
		self.assertEqual(rules.nvda_item_type("listitem"), "listItem")
		self.assertEqual(rules.public_item_type("radioButton"), "radiobutton")
		self.assertEqual(rules.public_item_type("nonsense"), "nonsense")

	def test_labels_are_collapsed_and_bounded(self):
		self.assertEqual(rules.clip("  a\n\n b  "), "a b")
		self.assertEqual(rules.clip("x" * 400, 10), "x" * 10 + "…")
		self.assertEqual(rules.clip(None), "")


# ============================================================================
# Enumeration
# ============================================================================


class EnumerationTests(unittest.TestCase):
	def test_positions_come_back_in_reading_order(self):
		document = ProbingDocument(HEADINGS)
		payload, objects = reading.targets("heading", limit=10, root=Host(document))

		self.assertEqual([target["label"] for target in payload["targets"]], ["Introduction", "Methods", "Results"])
		self.assertEqual([target["level"] for target in payload["targets"]], [2, 2, 2])
		self.assertEqual(len(objects), 3)
		self.assertTrue(payload["browse_mode"])
		self.assertEqual(payload["interface"], "ProbingDocument")
		self.assertEqual(payload["document"], "Fake Page")
		self.assertEqual(document.calls, [("heading", "next", None)])

	def test_the_list_is_cut_at_the_limit_and_says_so(self):
		document = ProbingDocument(HEADINGS)
		cut, _objects = reading.targets("heading", limit=2, root=Host(document))
		whole, _objects = reading.targets("heading", limit=3, root=Host(document))
		longer, _objects = reading.targets("heading", limit=30, root=Host(document))

		self.assertTrue(cut["truncated"])
		self.assertFalse(whole["truncated"])
		self.assertFalse(longer["truncated"])

	def test_an_empty_document_is_a_normal_answer(self):
		payload, objects = reading.targets("heading", limit=5, root=Host(ProbingDocument([])))
		self.assertEqual(payload["targets"], [])
		self.assertFalse(payload["truncated"])
		self.assertEqual(objects, [])

	def test_what_a_document_supports_is_probed_not_assumed(self):
		document = ProbingDocument(HEADINGS, supported=("heading", "link"))
		payload, _objects = reading.targets("link", limit=5, root=Host(document))

		self.assertEqual(payload["source"], "probed")
		self.assertIn("heading", payload["available"])
		self.assertNotIn("landmark", payload["available"])

	def test_an_unsupported_type_lists_what_is_available(self):
		document = ProbingDocument(HEADINGS, supported=("heading",))
		with self.assertRaises(errors.DomainUnavailableError) as caught:
			reading.targets("landmark", limit=5, root=Host(document))

		self.assertEqual(caught.exception.protocol_code, 4205)
		details = caught.exception.details
		self.assertEqual(details["item"], "landmark")
		self.assertEqual(details["available"], ["heading"])
		self.assertEqual(details["source"], "probed")

	def test_a_document_that_cannot_be_asked_declares_its_types(self):
		document = Document(HEADINGS)
		payload, _objects = reading.targets("heading", limit=5, root=Host(document))
		self.assertEqual(payload["source"], "declared")
		self.assertIn("heading", payload["available"])

	def test_no_document_is_reported_as_no_browse_mode(self):
		with self.assertRaises(errors.DomainUnavailableError) as caught:
			reading.targets("heading", limit=5, root=Host(None))

		details = caught.exception.details
		self.assertFalse(details["browse_mode"])
		self.assertIn("browse mode", details["reason"])

	def test_a_position_without_a_control_still_has_a_label(self):
		plain = Item("Just a heading", role="HEADING")
		document = ProbingDocument([plain])
		payload, objects = reading.targets("heading", limit=5, root=Host(document))
		self.assertEqual(payload["targets"][0]["label"], "Just a heading")
		self.assertEqual(objects, [None])

	def test_a_heading_level_comes_from_the_nodes_own_attributes(self):
		# A virtual buffer keeps the level in the control field, not on the
		# object: the object at a heading position is often just the text.
		item = Item("Deep heading", role="HEADING")
		item.vbufFieldIdentifier = (7, 3)
		item.textInfo = types.SimpleNamespace(
			text="Deep heading",
			_getControlFieldAttribs=lambda _doc, _node: {"level": "3"},
		)
		document = ProbingDocument([item])
		payload, _objects = reading.targets("heading", limit=5, root=Host(document))
		self.assertEqual(payload["targets"][0]["level"], 3)

	def test_only_headings_carry_a_level(self):
		item = Item("a link", role="LINK", obj=types.SimpleNamespace(role="LINK", headingLevel=4))
		document = ProbingDocument([item])
		payload, _objects = reading.targets("link", limit=5, root=Host(document))
		self.assertEqual(payload["targets"][0]["level"], 0)


# ============================================================================
# Navigation
# ============================================================================


class NavigationTests(unittest.TestCase):
	def test_steps_land_on_the_nth_position(self):
		items = [heading("Introduction"), heading("Methods"), heading("Results")]
		document = ProbingDocument(items)
		payload, _obj = reading.navigate("heading", "next", steps=2, root=Host(document))

		self.assertEqual(payload["label"], "Methods")
		self.assertEqual(payload["reached"], 2)
		self.assertTrue(payload["landed"])
		# Stepping moves through every intermediate position, as the key does.
		self.assertEqual([item.moved for item in items], [True, True, False])
		self.assertEqual(document.calls, [("heading", "next", "caret"), ("heading", "next", "caret")])

	def test_the_cursor_moves_to_the_chosen_position(self):
		item = heading("Only")
		document = ProbingDocument([item])
		reading.navigate("heading", "next", steps=1, root=Host(document))
		self.assertTrue(item.moved)

	def test_the_document_ending_first_stops_at_the_last_position(self):
		document = ProbingDocument(HEADINGS)
		payload, _obj = reading.navigate("heading", "next", steps=9, root=Host(document))

		self.assertEqual(payload["label"], "Results")
		self.assertEqual(payload["reached"], 3)
		self.assertEqual(payload["steps"], 9)

	def test_first_with_steps_asks_for_the_nth_in_the_document(self):
		document = ProbingDocument(HEADINGS)
		payload, _obj = reading.navigate("heading", "first", steps=3, root=Host(document))

		self.assertEqual(payload["label"], "Results")
		self.assertEqual(payload["direction"], "first")
		self.assertEqual(
			document.calls,
			[("heading", "next", None), ("heading", "next", "caret"), ("heading", "next", "caret")],
		)

	def test_last_scans_the_whole_document(self):
		document = ProbingDocument(HEADINGS)
		payload, _obj = reading.navigate("heading", "last", root=Host(document))

		self.assertEqual(payload["label"], "Results")
		self.assertEqual(payload["reached"], 3)

	def test_previous_walks_backwards_from_the_cursor(self):
		items = [heading("Introduction"), heading("Methods"), heading("Results")]
		document = ProbingDocument(items)
		document.position = 2  # the cursor sits on Results
		payload, _obj = reading.navigate("heading", "previous", steps=1, root=Host(document))

		self.assertEqual(payload["label"], "Methods")
		self.assertEqual(payload["direction"], "previous")
		self.assertEqual(document.calls, [("heading", "previous", "caret")])

	def test_an_empty_document_names_the_move_that_failed(self):
		with self.assertRaises(errors.DomainUnavailableError) as caught:
			reading.navigate("heading", "next", root=Host(ProbingDocument([])))

		self.assertEqual(caught.exception.protocol_code, 4205)
		self.assertIn("next", str(caught.exception))
		self.assertIn("heading", str(caught.exception))
		self.assertIn("available", caught.exception.details)

	def test_focus_mode_is_reported_with_the_move(self):
		document = ProbingDocument(HEADINGS, pass_through=True)
		payload, _obj = reading.navigate("heading", "next", root=Host(document))
		self.assertTrue(payload["pass_through"])

	def test_the_landing_object_prefers_the_position_over_the_cursor(self):
		own = types.SimpleNamespace(role="LINK", headingLevel=None)
		item = Item("a link", role="LINK", obj=own)
		document = ProbingDocument([item])
		payload, obj = reading.navigate("link", "next", root=Host(document))

		self.assertIs(obj, own)
		self.assertEqual(payload["role"], "LINK")
		self.assertEqual(payload["level"], 0)


# ============================================================================
# Handler bookkeeping
# ============================================================================


class FakeContext:
	def __init__(self, objects=None):
		self.objects = objects or {}
		self.issued = []
		self.settled = []
		self.resolved = []

	def dispatch(self, operation):
		return operation()

	def resolve(self, ref):
		self.resolved.append(ref)
		return self.objects[ref]

	def register(self, obj):
		ref = f"n{len(self.issued) + 1}"
		self.issued.append((ref, obj))
		return ref

	def settle(self, perform, expect=None):
		self.settled.append(expect)
		return perform()


class HandlerTests(unittest.TestCase):
	def test_each_target_gets_a_ref_but_plain_text_gets_none(self):
		link = types.SimpleNamespace(role="LINK", headingLevel=None)
		document = ProbingDocument([Item("plain"), Item("a link", obj=link)])
		context = FakeContext({"d1": Host(document)})

		payload = handlers.list_reading_targets("heading", 10, "d1", 1000, context)

		self.assertEqual([target["ref"] for target in payload["targets"]], ["", "n1"])
		self.assertEqual(context.issued, [("n1", link)])
		self.assertEqual(context.resolved, ["d1"])

	def test_the_limit_is_bounded(self):
		context = FakeContext()
		for limit in (0, 51, -1, True, "5"):
			with self.assertRaises(ValueError) as caught:
				handlers.list_reading_targets("heading", limit, None, 1000, context)
			self.assertIn("limit", str(caught.exception), limit)

	def test_the_steps_are_bounded(self):
		context = FakeContext()
		for steps in (0, 21, -3, True):
			with self.assertRaises(ValueError) as caught:
				handlers.navigate_reading("heading", "next", steps, None, 1000, None, context)
			self.assertIn("steps", str(caught.exception), steps)

	def test_last_does_not_take_steps(self):
		with self.assertRaises(ValueError) as caught:
			handlers.navigate_reading("heading", "last", 2, None, 1000, None, FakeContext())
		self.assertIn("last", str(caught.exception))

	def test_navigation_settles_and_reports_the_landing(self):
		target = types.SimpleNamespace(role="HEADING", headingLevel=1)
		document = ProbingDocument([heading("Intro", 1), Item("Body", role="HEADING", level=1, obj=target)])
		context = FakeContext({"d1": Host(document)})

		payload = handlers.navigate_reading("heading", "next", 2, "d1", 1000, {"ref": "anything"}, context)

		self.assertEqual(payload["reached"], 2)
		self.assertEqual(payload["label"], "Body")
		self.assertEqual(payload["ref"], "n1")
		self.assertEqual(context.issued, [("n1", target)])
		self.assertEqual(context.settled, [{"ref": "anything"}])

	def test_no_landing_object_leaves_the_ref_empty(self):
		document = ProbingDocument([Item("plain")])
		# The cursor's object is the document itself, which is not a control.
		document.currentNVDAObject = document.rootNVDAObject
		context = FakeContext({"d1": Host(document)})
		payload = handlers.navigate_reading("heading", "next", 1, "d1", 1000, None, context)

		self.assertEqual(payload["ref"], "")
		self.assertTrue(payload["landed"])
		self.assertEqual(context.issued, [])

	def test_the_cursors_object_is_used_when_it_is_a_real_control(self):
		control = types.SimpleNamespace(role="LINK", headingLevel=None)
		document = ProbingDocument([Item("plain")])
		document.currentNVDAObject = control
		context = FakeContext({"d1": Host(document)})
		payload = handlers.navigate_reading("heading", "next", 1, "d1", 1000, None, context)

		self.assertEqual(payload["ref"], "n1")
		self.assertEqual(context.issued, [("n1", control)])

	def test_the_timeout_reaches_the_reading_layer(self):
		"""The budget has to arrive intact; it is what bounds the main-thread hop."""
		seen: dict = {}
		original = reading.targets

		def fake(**kwargs):
			seen.update(kwargs)
			return (
				{
					"item": "heading",
					"available": [],
					"browse_mode": True,
					"document": "",
					"interface": "",
					"pass_through": False,
					"source": "probed",
					"targets": [],
					"truncated": False,
					"stopped": "end",
					"scanned": 0,
					"timeout_ms": kwargs.get("timeout_ms"),
					"elapsed_ms": 0.0,
				},
				[],
			)

		reading.targets = fake
		try:
			handlers.list_reading_targets("heading", 5, None, 750, FakeContext())
		finally:
			reading.targets = original

		self.assertEqual(seen["timeout_ms"], 750)
		self.assertIsNone(seen["root"])

	def test_a_timeout_outside_the_range_is_refused(self):
		for timeout in (0, -5, rules.MAX_TIMEOUT_MS + 1, True, "500"):
			with self.assertRaises(ValueError) as caught:
				handlers.list_reading_targets("heading", 5, None, timeout, FakeContext())
			self.assertIn("timeout_ms", str(caught.exception), timeout)
			with self.assertRaises(ValueError):
				handlers.navigate_reading("heading", "next", 1, None, timeout, None, FakeContext())


class SlowDocument(Document):
	"""A document whose iterator costs time, the way a slow provider does.

	The budget is checked between positions, not inside one, so the only way to
	test it without a real browser is a document that makes each position
	expensive.
	"""

	def __init__(self, items, delay=0.01, **kwargs):
		super().__init__(items, **kwargs)
		self.delay = delay

	def _iterNodesByType(self, itemType, direction="next", pos=None):
		source = super()._iterNodesByType(itemType, direction, pos)

		def stream():
			for item in source:
				time.sleep(self.delay)
				yield item

		return stream()


# ============================================================================
# The main-thread budget
# ============================================================================


class BudgetTests(unittest.TestCase):
	"""A call that runs out of time says so instead of running long."""

	def test_a_slow_document_is_cut_and_says_it_ran_out_of_time(self):
		document = SlowDocument([heading(f"H{index}") for index in range(40)])
		payload, _objects = reading.targets("heading", limit=40, root=Host(document), timeout_ms=45)

		self.assertTrue(payload["truncated"])
		self.assertEqual(payload["stopped"], rules.STOP_TIMEOUT)
		self.assertGreater(len(payload["targets"]), 0)
		self.assertLess(len(payload["targets"]), 40)
		self.assertGreater(payload["elapsed_ms"], 0)
		self.assertEqual(payload["timeout_ms"], 45)

	def test_a_list_cut_by_the_limit_says_which_limit(self):
		document = ProbingDocument([heading(f"H{index}") for index in range(10)])
		payload, _objects = reading.targets("heading", limit=3, root=Host(document))

		self.assertEqual(len(payload["targets"]), 3)
		self.assertEqual(payload["stopped"], rules.STOP_LIMIT)
		self.assertTrue(payload["truncated"])
		self.assertEqual(payload["scanned"], 4)

	def test_a_list_that_is_the_whole_document_is_not_truncated(self):
		document = ProbingDocument([heading("Only")])
		payload, _objects = reading.targets("heading", limit=1, root=Host(document))

		self.assertEqual(payload["stopped"], rules.STOP_END)
		self.assertFalse(payload["truncated"])

	def test_a_move_that_runs_out_of_time_reports_the_steps_it_made(self):
		document = SlowDocument([heading(f"H{index}") for index in range(30)])
		payload, _obj = reading.navigate(
			"heading", "next", steps=20, root=Host(document), timeout_ms=60
		)

		self.assertEqual(payload["stopped"], rules.STOP_TIMEOUT)
		self.assertGreater(payload["reached"], 0)
		self.assertLess(payload["reached"], 20)
		self.assertTrue(payload["landed"])

	def test_last_refuses_rather_than_guessing_when_the_budget_expires(self):
		document = SlowDocument([heading(f"H{index}") for index in range(50)])

		with self.assertRaises(errors.DomainUnavailableError) as caught:
			reading.navigate("heading", "last", root=Host(document), timeout_ms=30)

		self.assertIn("within 30 ms", str(caught.exception))
		self.assertEqual(caught.exception.details["timeout_ms"], 30)
		self.assertGreater(caught.exception.details["scanned"], 0)

	def test_a_move_that_makes_every_step_says_so(self):
		document = ProbingDocument([heading(f"H{index}") for index in range(5)])
		payload, _obj = reading.navigate("heading", "next", steps=2, root=Host(document))

		self.assertEqual(payload["reached"], 2)
		self.assertEqual(payload["stopped"], rules.STOP_STEPS)

	def test_a_move_that_the_document_ends_first_says_so(self):
		document = ProbingDocument([heading(f"H{index}") for index in range(2)])
		payload, _obj = reading.navigate("heading", "next", steps=9, root=Host(document))

		self.assertEqual(payload["reached"], 2)
		self.assertEqual(payload["stopped"], rules.STOP_END)


class ScanCapTests(unittest.TestCase):
	"""`last` scans to the end; the cap is a refusal, and it must be exact."""

	def test_a_document_holding_exactly_the_cap_can_be_read_to_the_end(self):
		items = [heading(f"H{index:04d}") for index in range(rules.MAX_SCAN)]
		payload, _obj = reading.navigate("heading", "last", root=Host(ProbingDocument(items)))

		self.assertEqual(payload["reached"], rules.MAX_SCAN)
		self.assertEqual(payload["label"], f"H{rules.MAX_SCAN - 1:04d}")
		self.assertEqual(payload["stopped"], rules.STOP_END)

	def test_one_position_past_the_cap_is_refused_by_name(self):
		items = [heading(f"H{index:04d}") for index in range(rules.MAX_SCAN + 1)]

		with self.assertRaises(errors.DomainUnavailableError) as caught:
			reading.navigate("heading", "last", root=Host(ProbingDocument(items)))

		self.assertIn(str(rules.MAX_SCAN), str(caught.exception))
		self.assertEqual(caught.exception.details["scanned"], rules.MAX_SCAN + 1)


# ============================================================================
# Starting browse mode
# ============================================================================


class CreationTests(unittest.TestCase):
	"""Creating a document is its own call, and it knows what it did."""

	def setUp(self):
		self._had = "treeInterceptorHandler" in sys.modules
		self._previous = sys.modules.get("treeInterceptorHandler")
		reading._CREATED.clear()

	def tearDown(self):
		if self._had:
			sys.modules["treeInterceptorHandler"] = self._previous
		else:
			sys.modules.pop("treeInterceptorHandler", None)
		reading._CREATED.clear()

	def install(self, update):
		sys.modules["treeInterceptorHandler"] = types.SimpleNamespace(update=update)

	def test_a_document_already_in_browse_mode_is_not_created_again(self):
		document = ProbingDocument(HEADINGS)
		payload = reading.create_document(Host(document))

		self.assertFalse(payload["created"])
		self.assertTrue(payload["browse_mode"])
		self.assertEqual(payload["interface"], "ProbingDocument")

	def test_a_focus_mode_document_is_created_once_and_then_remembered(self):
		document = ProbingDocument(HEADINGS)
		# A document model knows whether it is still alive; the bridge reuses a
		# remembered one only while it is.
		document.isAlive = True
		calls = []
		self.install(lambda obj, force=False: (calls.append((obj, force)), document)[1])
		target = types.SimpleNamespace(
			treeInterceptor=None,
			windowHandle=4242,
			appModule=types.SimpleNamespace(appName="Word"),
		)

		first = reading.create_document(target)
		second = reading.create_document(target)

		self.assertTrue(first["created"])
		self.assertFalse(second["created"])
		self.assertEqual(len(calls), 1)
		self.assertEqual(calls[0][1], True, "the model has to be forced, as NVDA+space does")
		self.assertEqual(first["interface"], "ProbingDocument")

	def test_an_object_that_cannot_be_a_document_says_so(self):
		self.install(lambda obj, force=False: None)

		with self.assertRaises(errors.DomainUnavailableError) as caught:
			reading.create_document(types.SimpleNamespace(treeInterceptor=None, name="Notepad"))

		self.assertIn("does not support browse mode", str(caught.exception))
		self.assertFalse(caught.exception.details["created"])

	def test_a_document_started_earlier_is_found_by_the_next_read(self):
		"""Starting browse mode and then reading it are two calls.

		The focused object is a fresh proxy each time, so the model has to be
		remembered across them — otherwise every read would start a new document.
		"""
		document = ProbingDocument(HEADINGS)
		document.isAlive = True
		self.install(lambda obj, force=False: document)
		target = types.SimpleNamespace(
			treeInterceptor=None,
			windowHandle=77,
			appModule=types.SimpleNamespace(appName="Word"),
		)
		reading.create_document(target)
		later = types.SimpleNamespace(
			treeInterceptor=None,
			windowHandle=77,
			appModule=types.SimpleNamespace(appName="Word"),
		)

		payload, _objects = reading.targets("heading", limit=5, root=later)

		self.assertEqual(len(payload["targets"]), 3)
		self.assertEqual(payload["interface"], "ProbingDocument")

	def test_the_handler_resolves_within_and_settles(self):
		document = ProbingDocument(HEADINGS)
		context = FakeContext({"w1": Host(document)})

		payload = handlers.start_browse_mode("w1", {"ref": "x"}, context)

		self.assertFalse(payload["created"])
		self.assertEqual(context.resolved, ["w1"])
		self.assertEqual(context.settled, [{"ref": "x"}])


class OutputModelTests(unittest.TestCase):
	"""The published output schema accepts exactly what the handlers return."""

	def test_enumeration_payload_matches_its_model(self):
		document = ProbingDocument(HEADINGS)
		payload = handlers.list_reading_targets(
			"heading", 5, "d1", 1000, FakeContext({"d1": Host(document)})
		)
		model = validator.validate_and_map(payload, models.ListReadingTargetsResult, strict=True)

		self.assertEqual(model.item, "heading")
		self.assertEqual(model.targets[1].label, "Methods")
		self.assertIn("heading", model.available)

	def test_navigation_payload_matches_its_model(self):
		document = ProbingDocument(HEADINGS)
		payload = handlers.navigate_reading(
			"heading", "next", 1, "d1", 1000, None, FakeContext({"d1": Host(document)})
		)
		model = validator.validate_and_map(payload, models.NavigateReadingResult, strict=True)

		self.assertTrue(model.landed)
		self.assertEqual(model.reached, 1)
		self.assertEqual(model.direction, "next")


if __name__ == "__main__":
	unittest.main()
