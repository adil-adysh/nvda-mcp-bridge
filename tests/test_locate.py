# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Tests for `locate` (P5): the match rules, and the handler's contract.

Two things are worth pinning without an NVDA session:

* the match vocabulary — roles have aliases, names are case-insensitive
  substrings unless ``exact`` is set, and a role with no UIA control type must be
  recognisable as "the platform cannot filter on this" rather than silently
  matching nothing;
* the handler's bounds and its ref bookkeeping — one ref per match, in order,
  because the caller's next call is ``invoke`` on match 3.
"""

import importlib
import os
import sys
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

locate_rules = importlib.import_module(f"{_PACKAGE_NAME}.server.locate")
refs = importlib.import_module(f"{_PACKAGE_NAME}.server.refs")

# The handler imports the NVDA-side search lazily, inside the call, so a module
# standing in for the bridge package is enough to exercise it without NVDA.
# Installed only for this module (see setUpModule) so other test modules that
# need the real package are unaffected.
_BRIDGE_KEY = f"{_PACKAGE_NAME}.nvda_bridge"
_fake_bridge = types.ModuleType(_BRIDGE_KEY)
_original_bridge = sys.modules.get(_BRIDGE_KEY)
handlers = importlib.import_module(f"{_PACKAGE_NAME}.tools.handlers")
models = importlib.import_module(f"{_PACKAGE_NAME}.tools.models")
validator = importlib.import_module(f"{_PACKAGE_NAME}.validation.dataclass_validator")


def setUpModule():
	sys.modules[_BRIDGE_KEY] = _fake_bridge


def tearDownModule():
	if _original_bridge is not None:
		sys.modules[_BRIDGE_KEY] = _original_bridge
	else:
		sys.modules.pop(_BRIDGE_KEY, None)


class RoleNormalizationTests(unittest.TestCase):
	def test_case_and_separators_are_ignored(self):
		for written in ("button", "Button", "push button", "push_button", "PUSH-BUTTON"):
			self.assertEqual(locate_rules.normalize_role(written), "BUTTON", written)

	def test_common_aliases_resolve_to_nvda_names(self):
		self.assertEqual(locate_rules.normalize_role("edit"), "EDITABLETEXT")
		self.assertEqual(locate_rules.normalize_role("text field"), "EDITABLETEXT")
		self.assertEqual(locate_rules.normalize_role("hyperlink"), "LINK")
		self.assertEqual(locate_rules.normalize_role("radio"), "RADIOBUTTON")
		self.assertEqual(locate_rules.normalize_role("combo"), "COMBOBOX")

	def test_empty_input_is_no_filter(self):
		self.assertEqual(locate_rules.normalize_role(None), "")
		self.assertEqual(locate_rules.normalize_role(""), "")
		self.assertEqual(locate_rules.normalize_role("   "), "")

	def test_unaliased_roles_are_preserved(self):
		self.assertEqual(locate_rules.normalize_role("table cell"), "TABLECELL")


class RoleMatchingTests(unittest.TestCase):
	def test_missing_filter_matches_everything(self):
		self.assertTrue(locate_rules.role_matches(None, "WINDOW"))
		self.assertTrue(locate_rules.role_matches("", "BUTTON"))

	def test_exact_role_name_required(self):
		self.assertTrue(locate_rules.role_matches("BUTTON", "BUTTON"))
		self.assertFalse(locate_rules.role_matches("BUTTON", "TOGGLEBUTTON"))

	def test_alias_matches_the_canonical_object_role(self):
		self.assertTrue(locate_rules.role_matches("edit", "EDITABLETEXT"))
		self.assertTrue(locate_rules.role_matches("hyperlink", "LINK"))


class NameMatchingTests(unittest.TestCase):
	def test_substring_and_case_insensitive_by_default(self):
		self.assertTrue(locate_rules.name_matches("save", "Save document"))
		self.assertTrue(locate_rules.name_matches("AVE", "Save document"))

	def test_exact_requires_the_whole_name_but_ignores_case(self):
		self.assertTrue(locate_rules.name_matches("save document", "Save Document", exact=True))
		self.assertFalse(locate_rules.name_matches("save", "Save document", exact=True))

	def test_missing_values(self):
		self.assertTrue(locate_rules.name_matches("", "anything"))
		self.assertTrue(locate_rules.name_matches(None, None))
		self.assertFalse(locate_rules.name_matches("save", None))
		self.assertFalse(locate_rules.name_matches("save", ""))


class UiaMappingTests(unittest.TestCase):
	def test_roles_with_equivalent_control_types(self):
		self.assertEqual(locate_rules.uia_control_type_name("BUTTON"), "Button")
		self.assertEqual(locate_rules.uia_control_type_name("edit"), "Edit")
		self.assertEqual(locate_rules.uia_control_type_name("link"), "Hyperlink")
		self.assertEqual(locate_rules.uia_control_type_name("checkbox"), "CheckBox")

	def test_role_without_equivalent_is_reported_as_unsupported(self):
		self.assertIsNone(locate_rules.uia_control_type_name("TABLECELL"))
		self.assertIsNone(locate_rules.uia_control_type_name("SECTION"))

	def test_control_type_maps_back_to_one_role_name(self):
		self.assertEqual(locate_rules.role_for_uia_control_type("Button"), "BUTTON")
		self.assertEqual(locate_rules.role_for_uia_control_type("Edit"), "EDITABLETEXT")
		self.assertEqual(locate_rules.role_for_uia_control_type("NoSuchType"), "UNKNOWN")


class UiaIdentityTests(unittest.TestCase):
	"""A located reference is recognised by UIA fields, not by NVDA ones."""

	def setUp(self):
		self.table = refs.ReferenceTable(ttl_s=60)

	def _identity(self, **overrides):
		base = {
			"controlType": 50000,
			"automationId": "save",
			"runtimeId": [42, 462502],
			"name": "Save",
		}
		base.update(overrides)
		return base

	def test_a_dead_element_is_stale_when_its_control_type_changes(self):
		element = types.SimpleNamespace(identity=self._identity())
		reader = lambda obj: obj.identity  # noqa: E731
		self.table.register({"n1": element}, state_version=1, identity_of=reader)
		element.identity = self._identity(controlType=50004)
		with self.assertRaises(refs.StaleReference) as caught:
			self.table.get("n1", identity_of=reader)
		self.assertIn("controlType", caught.exception.reason)

	def test_a_recycled_element_is_stale_when_its_runtime_id_changes(self):
		element = types.SimpleNamespace(identity=self._identity())
		reader = lambda obj: obj.identity  # noqa: E731
		self.table.register({"n1": element}, state_version=1, identity_of=reader)
		element.identity = self._identity(runtimeId=[42, 999999])
		with self.assertRaises(refs.StaleReference):
			self.table.get("n1", identity_of=reader)

	def test_a_rename_alone_is_tolerated(self):
		element = types.SimpleNamespace(identity=self._identity())
		reader = lambda obj: obj.identity  # noqa: E731
		self.table.register({"n1": element}, state_version=1, identity_of=reader)
		element.identity = self._identity(name="Save as")
		self.assertIs(self.table.get("n1", identity_of=reader).obj, element)


class FakeSearch:
	def __init__(self, records, objects):
		self.records = records
		self.objects = objects
		self.calls = []

	def find(self, **kwargs):
		self.calls.append(kwargs)
		return {"matches": [dict(record) for record in self.records], "method": "uia", "searched": len(self.records), "truncated": False, "elapsed_ms": 1.0, "scope": "WINDOW 'Probe'"}, list(self.objects)


class FakeContext:
	def __init__(self):
		self.issued = []
		self.resolved = []

	def dispatch(self, operation):
		return operation()

	def resolve(self, ref):
		self.resolved.append(ref)
		return f"object-for-{ref}"

	def register(self, obj):
		ref = f"n{len(self.issued) + 1}"
		self.issued.append((ref, obj))
		return ref


class LocateHandlerTests(unittest.TestCase):
	def setUp(self):
		self._previous = getattr(_fake_bridge, "locate", None)

	def tearDown(self):
		if self._previous is None:
			if hasattr(_fake_bridge, "locate"):
				delattr(_fake_bridge, "locate")
		else:
			_fake_bridge.locate = self._previous

	def test_requires_a_role_or_a_name(self):
		with self.assertRaises(ValueError) as caught:
			handlers.locate(None, None, False, None, 10, 400, 500, FakeContext())
		self.assertIn("role", str(caught.exception))

	def test_bounds_are_rejected_with_the_caller_bounds_named(self):
		context = FakeContext()
		cases = (
			(dict(limit=0), "limit"),
			(dict(limit=51), "limit"),
			(dict(max_nodes=0), "max_nodes"),
			(dict(max_nodes=2001), "max_nodes"),
			(dict(timeout_ms=10), "timeout_ms"),
			(dict(timeout_ms=5000), "timeout_ms"),
		)
		for overrides, field in cases:
			kwargs = dict(role="BUTTON", name=None, exact=False, within=None, limit=10, max_nodes=400, timeout_ms=500)
			kwargs.update(overrides)
			with self.assertRaises(ValueError) as caught:
				handlers.locate(
					kwargs["role"], kwargs["name"], kwargs["exact"], kwargs["within"],
					kwargs["limit"], kwargs["max_nodes"], kwargs["timeout_ms"], context,
				)
			self.assertIn(field, str(caught.exception), overrides)

	def test_each_match_gets_a_ref_in_order(self):
		search = FakeSearch(
			[
				{"role": "BUTTON", "name": "Alpha", "target": "uia"},
				{"role": "BUTTON", "name": "Beta", "target": "uia"},
			],
			["element-alpha", "element-beta"],
		)
		_fake_bridge.locate = search
		context = FakeContext()

		payload = handlers.locate("BUTTON", None, False, None, 10, 400, 500, context)

		self.assertEqual([match["ref"] for match in payload["matches"]], ["n1", "n2"])
		self.assertEqual([obj for _ref, obj in context.issued], ["element-alpha", "element-beta"])
		self.assertEqual(search.calls[0]["role"], "BUTTON")
		self.assertIsNone(search.calls[0]["within"])

	def test_within_is_resolved_before_searching(self):
		search = FakeSearch([], [])
		_fake_bridge.locate = search
		context = FakeContext()

		handlers.locate("LINK", None, False, "c3", 10, 400, 500, context)

		self.assertEqual(context.resolved, ["c3"])
		self.assertEqual(search.calls[0]["within"], "object-for-c3")

	def test_an_empty_result_is_a_normal_answer(self):
		_fake_bridge.locate = FakeSearch([], [])
		payload = handlers.locate("BUTTON", "nothing", False, None, 10, 400, 500, FakeContext())
		self.assertEqual(payload["matches"], [])
		self.assertEqual(payload["method"], "uia")

	def test_payload_validates_against_the_published_output_model(self):
		record = {
			"ref": "n1",
			"role": "LINK",
			"name": "Two",
			"value": "",
			"states": ["ENABLED"],
			"location": [10, 20, 30, 40],
			"automationId": "link-2",
			"depth": -1,
			"path": [],
			"target": "uia",
		}
		payload = {
			"matches": [record],
			"method": "uia",
			"searched": 1,
			"truncated": False,
			"elapsed_ms": 12.5,
			"scope": "WINDOW 'Probe'",
		}
		model = validator.validate_and_map(payload, models.LocateResult, strict=True)
		self.assertEqual(model.matches[0].ref, "n1")
		self.assertEqual(model.matches[0].automationId, "link-2")


if __name__ == "__main__":
	unittest.main()
