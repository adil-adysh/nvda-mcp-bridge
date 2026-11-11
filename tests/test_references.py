# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Tests for versioned references (P2).

The contract these pin: a reference either resolves to the object that was
captured, or reports itself stale. It must never silently resolve to a different
object, and it must never scan the tree to find one.
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

refs = importlib.import_module(f"{_PACKAGE_NAME}.server.refs")


class FakeObject:
	def __init__(self, role="BUTTON", name="Run", windowClassName="Chrome", windowControlID=0):
		self.role = role
		self.name = name
		self.windowClassName = windowClassName
		self.windowControlID = windowControlID


def identity_of(obj):
	return {
		"role": obj.role,
		"name": obj.name,
		"windowClassName": obj.windowClassName,
		"windowControlID": obj.windowControlID,
	}


class Clock:
	def __init__(self):
		self.now = 1000.0

	def __call__(self):
		return self.now


class ReferenceTableTests(unittest.TestCase):
	def setUp(self):
		self.clock = Clock()
		self.table = refs.ReferenceTable(ttl_s=60, clock=self.clock)

	def test_register_then_resolve_returns_the_same_object(self):
		button = FakeObject()
		self.table.register({"f": button}, state_version=3, identity_of=identity_of)
		entry = self.table.get("f", identity_of=identity_of)
		self.assertIs(entry.obj, button)
		self.assertEqual(entry.state_version, 3)

	def test_unknown_reference_is_stale_with_the_protocol_code(self):
		with self.assertRaises(refs.StaleReference) as caught:
			self.table.get("f", identity_of=identity_of)
		self.assertEqual(caught.exception.protocol_code, 4206)
		self.assertIn("observe", str(caught.exception))

	def test_a_different_role_is_stale(self):
		self.table.register({"f": FakeObject()}, state_version=1, identity_of=identity_of)
		# The reference's object was recycled into something else.
		self.table._refs["f"].obj.role = "EDIT"
		with self.assertRaises(refs.StaleReference) as caught:
			self.table.get("f", identity_of=identity_of)
		self.assertIn("role", caught.exception.reason)

	def test_a_rename_is_tolerated(self):
		obj = FakeObject(name="Before")
		self.table.register({"f": obj}, state_version=1, identity_of=identity_of)
		obj.name = "After"
		self.assertIs(self.table.get("f", identity_of=identity_of).obj, obj)

	def test_a_read_that_raises_is_stale(self):
		class Dead(FakeObject):
			@property
			def windowClassName(self):
				raise RuntimeError("object died")

		self.table.register({"f": FakeObject()}, state_version=1, identity_of=identity_of)
		# The object dies between capture and resolution: re-reading it raises.
		self.table._refs["f"].obj = Dead.__new__(Dead)

		with self.assertRaises(refs.StaleReference) as caught:
			self.table.get(
				"f",
				identity_of=lambda obj: {
					"role": obj.role,
					"name": obj.name,
					"windowClassName": obj.windowClassName,
					"windowControlID": obj.windowControlID,
				},
			)
		self.assertIn("failed", caught.exception.reason)

	def test_expired_reference_is_stale(self):
		self.table.register({"f": FakeObject()}, state_version=1, identity_of=identity_of)
		self.clock.now += 61
		with self.assertRaises(refs.StaleReference) as caught:
			self.table.get("f", identity_of=identity_of)
		self.assertIn("60", caught.exception.reason)

	def test_re_registering_a_ref_repoints_it(self):
		first = FakeObject(name="First")
		second = FakeObject(name="Second")
		self.table.register({"f": first}, state_version=1, identity_of=identity_of)
		self.table.register({"f": second}, state_version=2, identity_of=identity_of)
		self.assertIs(self.table.get("f", identity_of=identity_of).obj, second)

	def test_the_table_is_bounded(self):
		table = refs.ReferenceTable(max_entries=2, ttl_s=0, clock=self.clock)
		table.register({"a1": FakeObject(), "a2": FakeObject()}, state_version=1, identity_of=identity_of)
		table.register({"a3": FakeObject()}, state_version=1, identity_of=identity_of)
		self.assertEqual(len(table), 2)
		self.assertEqual(table.list_refs(), ["a2", "a3"])

	def test_unregistrable_objects_are_skipped(self):
		class Bad:
			pass

		registered = self.table.register({"f": Bad()}, state_version=1, identity_of=lambda obj: obj.missing)
		self.assertEqual(registered, [])
		self.assertEqual(len(self.table), 0)

	def test_ref_kinds_are_derived_from_the_prefix(self):
		self.assertEqual(refs._kind_of("f"), "focus")
		self.assertEqual(refs._kind_of("a3"), "ancestor")
		self.assertEqual(refs._kind_of("c2"), "child")
		self.assertEqual(refs._kind_of("d1"), "dialog")
		self.assertEqual(refs._kind_of("n9"), "node")

	def test_the_target_kind_is_recorded_when_the_reference_is_captured(self):
		element = FakeObject()
		self.table.register({"n1": element}, state_version=1, identity_of=identity_of, target_kind="uia")
		self.assertEqual(self.table.get("n1").target_kind, "uia")
		# The default is an NVDA object, which is what observe hands out.
		self.table.register({"f": FakeObject()}, state_version=1, identity_of=identity_of)
		self.assertEqual(self.table.get("f").target_kind, "nvda")


if __name__ == "__main__":
	unittest.main()
