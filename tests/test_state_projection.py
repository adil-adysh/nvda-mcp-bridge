# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Tests for the coalesced UI-state core (P1).

The behaviour these pin is the one the measurements demanded: a burst of events
must not bump the version a reference binds to, and "nothing changed" must be a
distinguishable result rather than an absence of information.
"""

import importlib
import os
import sys
import threading
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

state = importlib.import_module(f"{_PACKAGE_NAME}.server.state")


class Role:
    def __init__(self, name):
        self.name = name


class State:
    def __init__(self, name):
        self.name = name


class FakeObject:
    """Duck-typed stand-in for an NVDAObject."""

    def __init__(self, role, name, parent=None, value=None, states=(), location=(0, 0, 10, 10)):
        self.role = Role(role)
        self.name = name
        self.parent = parent
        self.value = value
        self.states = [State(s) for s in states]
        self.location = location


class TestProjection(unittest.TestCase):
    def test_unfocused_produces_an_empty_observation(self):
        self.assertEqual(state.project(None), {"focus": None, "ancestors": []})

    def test_describe_is_json_ready(self):
        obj = FakeObject("BUTTON", "Save", value="v", states=("FOCUSED", "FOCUSABLE"))
        described = state.describe(obj)

        self.assertEqual(described["role"], "BUTTON")
        self.assertEqual(described["name"], "Save")
        self.assertEqual(described["value"], "v")
        self.assertEqual(described["states"], ["FOCUSABLE", "FOCUSED"])
        self.assertEqual(described["location"], [0, 0, 10, 10])

    def test_ancestors_are_nearest_first_and_bounded(self):
        desktop = FakeObject("WINDOW", "Desktop")
        window = FakeObject("WINDOW", "Notepad", parent=desktop)
        document = FakeObject("DOCUMENT", "Text editor", parent=window)
        button = FakeObject("BUTTON", "Save", parent=document)

        observation = state.project(button, max_ancestors=2)

        self.assertEqual([a["name"] for a in observation["ancestors"]], ["Text editor", "Notepad"])

    def test_long_values_are_truncated(self):
        obj = FakeObject("EDITABLETEXT", "Field", value="x" * 500)
        self.assertEqual(len(state.describe(obj, max_value=50)["value"]), 50)

    def test_a_broken_object_degrades_instead_of_raising(self):
        class Exploding:
            role = Role("BUTTON")

            @property
            def name(self):
                raise RuntimeError("COM failure")

            @property
            def states(self):
                raise RuntimeError("COM failure")

        described = state.describe(Exploding())

        self.assertIsNone(described["name"])
        self.assertEqual(described["states"], [])


class TestDiff(unittest.TestCase):
    def _observation(self, obj):
        return state.project(obj)

    def test_identical_projections_produce_no_changes(self):
        obj = FakeObject("BUTTON", "Save")
        before = self._observation(obj)

        self.assertEqual(state.diff(before, self._observation(obj)), {})

    def test_focus_move_names_both_ends(self):
        field = FakeObject("EDITABLETEXT", "Text field")
        checkbox = FakeObject("CHECKBOX", "Checkbox")

        changes = state.diff(self._observation(field), self._observation(checkbox))

        self.assertEqual(changes["name"], {"from": "Text field", "to": "Checkbox"})
        self.assertEqual(changes["role"], {"from": "EDITABLETEXT", "to": "CHECKBOX"})

    def test_state_toggle_is_reported(self):
        obj = FakeObject("CHECKBOX", "Checkbox", states=("FOCUSABLE",))
        before = self._observation(obj)
        obj.states = [State("FOCUSABLE"), State("CHECKED")]

        changes = state.diff(before, self._observation(obj))

        self.assertEqual(changes["states"], {"from": ["FOCUSABLE"], "to": ["CHECKED", "FOCUSABLE"]})

    def test_window_change_is_reported_separately_from_focus(self):
        desktop = FakeObject("WINDOW", "Desktop")
        first = FakeObject("WINDOW", "Notepad", parent=desktop)
        second = FakeObject("WINDOW", "Calculator", parent=desktop)
        before = self._observation(FakeObject("BUTTON", "OK", parent=first))
        after = self._observation(FakeObject("BUTTON", "OK", parent=second))

        changes = state.diff(before, after)

        self.assertEqual(changes["window"], {"from": "Notepad", "to": "Calculator"})
        self.assertNotIn("name", changes)

    def test_missing_side_produces_no_diff(self):
        self.assertEqual(state.diff(None, self._observation(FakeObject("BUTTON", "Save"))), {})
        self.assertEqual(state.diff(self._observation(FakeObject("BUTTON", "Save")), None), {})


class TestStateProjector(unittest.TestCase):
    def setUp(self):
        self.object = FakeObject("EDITABLETEXT", "Text field", value="")
        self.projector = state.StateProjector(lambda: state.project(self.object))

    def test_first_refresh_publishes_version_one(self):
        projection, changes, version = self.projector.refresh()

        self.assertEqual(changes, {})
        self.assertEqual(version, 1)
        self.assertEqual(projection["focus"]["name"], "Text field")

    def test_unchanged_projection_does_not_bump_the_version(self):
        self.projector.refresh()
        _, changes, version = self.projector.refresh()

        self.assertEqual(changes, {})
        self.assertEqual(version, 1, "an unchanged projection must not invalidate references")

    def test_a_real_change_bumps_the_version_once_per_change(self):
        self.projector.refresh()
        self.object.value = "a"
        _, changes, version = self.projector.refresh()
        self.assertEqual(version, 2)
        self.assertEqual(changes["value"], {"from": "", "to": "a"})

        _, _, unchanged_version = self.projector.refresh()
        self.assertEqual(unchanged_version, 2)

    def test_many_events_do_not_bump_the_state_version(self):
        """The 29-events-per-word case: events move `revision`, never `state_version`."""
        self.projector.refresh()
        for _ in range(29):
            self.projector.mark_dirty()

        _, _, version = self.projector.refresh()

        self.assertEqual(version, 1)
        self.assertEqual(self.projector.revision, 29)

    def test_mark_dirty_wakes_a_waiter(self):
        since = self.projector.revision
        woken = []

        def wait():
            woken.append(self.projector.wait_for_revision(since, timeout_ms=1000))

        thread = threading.Thread(target=wait)
        thread.start()
        time.sleep(0.05)
        self.projector.mark_dirty()
        thread.join(timeout=2)

        self.assertEqual(woken, [True])

    def test_wait_returns_false_on_timeout(self):
        started = time.perf_counter()
        result = self.projector.wait_for_revision(self.projector.revision, timeout_ms=80)

        self.assertFalse(result)
        self.assertLess(time.perf_counter() - started, 1.0)

    def test_wait_returns_immediately_when_already_ahead(self):
        self.projector.mark_dirty()
        started = time.perf_counter()

        self.assertTrue(self.projector.wait_for_revision(0, timeout_ms=1000))
        self.assertLess(time.perf_counter() - started, 0.05)

    def test_snapshot_is_atomic_and_consistent(self):
        self.projector.refresh()
        projection, version, revision = self.projector.snapshot()

        self.assertEqual(projection["focus"]["name"], "Text field")
        self.assertEqual(version, 1)
        self.assertEqual(revision, 0)


if __name__ == "__main__":
    unittest.main()
