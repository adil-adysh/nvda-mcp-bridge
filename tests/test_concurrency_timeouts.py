# pyright: reportMissingImports=false, reportUnusedCallResult=false

import importlib
import os
import sys
import threading
import time
import types
import unittest
from pathlib import Path

_ADDON_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "addon", "globalPlugins", "nvdaMcp-bridge")
)
if _ADDON_DIR not in sys.path:
    sys.path.insert(0, _ADDON_DIR)

_fake_logger = types.SimpleNamespace(
    debug=lambda *args, **kwargs: None,
    info=lambda *args, **kwargs: None,
    warning=lambda *args, **kwargs: None,
    error=lambda *args, **kwargs: None,
)
sys.modules["logHandler"] = types.SimpleNamespace(log=_fake_logger)

_queue_handler = types.ModuleType("queueHandler")
_queue_handler.eventQueue = "dummy-queue"
_queue_handler.queueFunction = lambda queue, func, *args, **kwargs: func(*args, **kwargs)
sys.modules["queueHandler"] = _queue_handler

for mod_name in (
    "api",
    "ui",
    "speech",
    "keyboardHandler",
    "tones",
    "core",
    "versionInfo",
    "globalVars",
    "textInfos",
    "controlTypes",
    "speechDictHandler",
    "addonHandler",
    "config",
    "winUser",
):
    if mod_name not in sys.modules:
        sys.modules[mod_name] = types.ModuleType(mod_name)

# `press` settles, and settling reads the projection, which asks NVDA for the
# focus. An empty window is the honest stand-in: this file is about pacing and
# cancellation, not about what is on screen.
if not hasattr(sys.modules["api"], "getFocusObject"):
    sys.modules["api"].getFocusObject = lambda: None
    sys.modules["api"].getForegroundObject = lambda: None
    sys.modules["api"].getDesktopObject = lambda: None


class _DummyGesture:
    def __init__(self, name="dummy"):
        self.name = name

    @classmethod
    def fromName(cls, name):
        return _DummyGesture(name)

    def send(self):
        pass


sys.modules["keyboardHandler"].KeyboardInputGesture = _DummyGesture

_PACKAGE_NAME = "nvdaMcpBridge"
if _PACKAGE_NAME not in sys.modules:
    _package = types.ModuleType(_PACKAGE_NAME)
    _package.__path__ = [_ADDON_DIR]
    sys.modules[_PACKAGE_NAME] = _package

ui_module = importlib.import_module(f"{_PACKAGE_NAME}.nvda_bridge.ui")
executor_module = importlib.import_module(f"{_PACKAGE_NAME}.server.main_thread_executor")
http_bridge_module = importlib.import_module(f"{_PACKAGE_NAME}.server.http_bridge")
security_module = importlib.import_module(f"{_PACKAGE_NAME}.server.security")

MainThreadExecutor = executor_module.MainThreadExecutor
send_keyboard = ui_module.send_keyboard
send_single_gesture = ui_module.send_single_gesture
_input_lock = ui_module._input_lock
NVDAHTTPBridge = http_bridge_module.NVDAHTTPBridge
PermissionPolicy = security_module.PermissionPolicy


class ConcurrencyAndTimeoutTests(unittest.TestCase):
    def test_repeat_5_gestures_through_actual_bridge_execution_path(self):
        """Verify 5 gestures with delays execute cleanly through the actual bridge without deadlocking."""
        dispatched = []

        class MockMainThreadQueue(executor_module.MainThreadQueue):
            def queue(self, func):
                # Execute in simulated main thread
                func()

        policy = PermissionPolicy(granted_permissions=["read_only", "speech_output", "input_control"])
        bridge = NVDAHTTPBridge(
            bridge_token="test-token",
            permission_policy=policy,
        )
        bridge.main_thread_executor = MainThreadExecutor(
            queue_adapter=MockMainThreadQueue(),
            default_timeout=5.0,
        )

        # Mock send_single_gesture to record execution
        original_send = ui_module.send_single_gesture
        try:
            ui_module.send_single_gesture = lambda g: dispatched.append(getattr(g, "name", str(g)))

            res = bridge.execute_action(
                request_id="test-req-5",
                action_name="press",
                params={"keys": "windows+d", "repeat": 5, "delay_ms": 5},
                timeout_ms=5000,
            )

            self.assertTrue(res.get("success"), f"Action should succeed: {res}")
            data = res.get("data", {})
            self.assertEqual(data.get("sent_count"), 5)
            self.assertFalse(data.get("cancelled"))
            self.assertEqual(len(dispatched), 5)
        finally:
            ui_module.send_single_gesture = original_send

    def test_no_nested_lock_acquisition(self):
        """Verify that single gesture dispatch on the main thread does NOT reacquire _input_lock."""
        class MonitoredLock:
            def __init__(self, real_lock):
                self.real_lock = real_lock
                self.acquire_count = 0

            def acquire(self, *args, **kwargs):
                self.acquire_count += 1
                return self.real_lock.acquire(*args, **kwargs)

            def release(self):
                return self.real_lock.release()

        monitored_lock = MonitoredLock(_input_lock)
        orig_lock = ui_module._input_lock
        ui_module._input_lock = monitored_lock
        try:
            result = send_keyboard(
                gesture="escape",
                repeat=3,
                delay_ms=2,
                main_thread_fn=lambda g: send_single_gesture(g),
            )
            self.assertTrue(result["sent"])
            self.assertEqual(result["sent_count"], 3)
            # The lock should have been acquired exactly ONCE by the coordinator
            self.assertEqual(monitored_lock.acquire_count, 1)
        finally:
            ui_module._input_lock = orig_lock

    def test_send_keyboard_cancellation_stops_repetitions(self):
        cancellation_event = threading.Event()
        dispatched_count = [0]

        def mock_dispatcher(g=None):
            dispatched_count[0] += 1
            # Cancel after first gesture is dispatched
            cancellation_event.set()

        # Request 10 repetitions with 50ms delay between them
        result = send_keyboard(
            gesture="tab",
            repeat=10,
            delay_ms=50,
            cancellation_event=cancellation_event,
            main_thread_fn=mock_dispatcher,
        )

        self.assertTrue(result["cancelled"], "Operation should be marked cancelled")
        self.assertLess(result["sent_count"], 10, "Should not have sent all 10 repetitions")
        self.assertEqual(dispatched_count[0], result["sent_count"])

    def test_cancellation_after_second_gesture(self):
        """Verify cancellation after exactly 2 gestures reports sent_count=2 and cancelled=True."""
        cancellation_event = threading.Event()
        dispatched = []

        def mock_dispatcher(g=None):
            dispatched.append(len(dispatched) + 1)
            if len(dispatched) >= 2:
                cancellation_event.set()

        result = send_keyboard(
            gesture="alt+tab",
            repeat=5,
            delay_ms=30,
            cancellation_event=cancellation_event,
            main_thread_fn=mock_dispatcher,
        )

        self.assertTrue(result["cancelled"])
        self.assertEqual(result["sent_count"], 2)
        self.assertEqual(len(dispatched), 2)

    def test_send_keyboard_serializes_concurrent_calls(self):
        execution_order = []

        def make_dispatcher(thread_id, gesture):
            def _dispatch(g=None):
                execution_order.append(f"{thread_id}_{gesture}")
                time.sleep(0.01)
            return _dispatch

        def run_keystrokes(thread_id, gesture):
            send_keyboard(
                gesture=gesture,
                repeat=3,
                delay_ms=5,
                main_thread_fn=make_dispatcher(thread_id, gesture),
            )

        t1 = threading.Thread(target=run_keystrokes, args=("A", "left"), name="Worker-A")
        t2 = threading.Thread(target=run_keystrokes, args=("B", "right"), name="Worker-B")

        t1.start()
        t2.start()
        t1.join(timeout=3.0)
        t2.join(timeout=3.0)

        # Because _input_lock serializes keyboard streams, all "A" gestures must either
        # run entirely before "B", or all "B" gestures must run before "A".
        # They should NOT interleave like A, B, A, B.
        thread_ids = [item.split("_")[0] for item in execution_order]
        first_thread = thread_ids[0]
        self.assertEqual(thread_ids[:3], [first_thread, first_thread, first_thread])
        second_thread = "B" if first_thread == "A" else "A"
        self.assertEqual(thread_ids[3:], [second_thread, second_thread, second_thread])

    def test_timeout_waiting_for_input_lock(self):
        """Verify that waiting for an occupied input lock times out cleanly."""
        lock_held = threading.Event()
        release_lock = threading.Event()

        def hold_lock():
            _input_lock.acquire()
            lock_held.set()
            release_lock.wait(timeout=2.0)
            _input_lock.release()

        t = threading.Thread(target=hold_lock, daemon=True)
        t.start()
        lock_held.wait(timeout=1.0)

        try:
            with self.assertRaises(TimeoutError) as ctx:
                send_keyboard(gesture="enter", timeout=0.05)
            self.assertIn("input lock", str(ctx.exception))
        finally:
            release_lock.set()
            t.join(timeout=1.0)

    def test_timeout_during_sequence(self):
        """Verify that exceeding sequence deadline halts further gestures and reports timeout."""
        dispatched = []

        def slow_dispatcher(g=None):
            dispatched.append(len(dispatched) + 1)
            time.sleep(0.03)

        with self.assertRaises(TimeoutError) as ctx:
            send_keyboard(
                gesture="down",
                repeat=10,
                delay_ms=30,
                timeout=0.07,
                main_thread_fn=slow_dispatcher,
            )
        self.assertIn("timed out", str(ctx.exception).lower())
        self.assertGreater(len(dispatched), 0)
        self.assertLess(len(dispatched), 10)

    def test_main_thread_never_blocked_by_pacing_delays(self):
        """Verify that when send_keyboard is called on main thread, pacing delays do not sleep."""
        # On main thread, delay_ms is ignored to keep NVDA UI responsive
        start = time.perf_counter()
        result = send_keyboard(
            gesture="up",
            repeat=3,
            delay_ms=500,  # 500ms delay would take >1s if sleeping
        )
        elapsed = time.perf_counter() - start
        self.assertTrue(result["sent"])
        self.assertEqual(result["sent_count"], 3)
        self.assertLess(elapsed, 0.4, "Main thread should not be blocked by 500ms pacing delays")

    def test_main_thread_executor_timeout_prevents_abandoned_execution(self):
        """When an executor times out, the queued action must not run later in NVDA."""
        executed = []

        class DelayedQueue(executor_module.MainThreadQueue):
            def __init__(self):
                self.queue_items = []

            def queue(self, func):
                self.queue_items.append(func)

        delayed_queue = DelayedQueue()
        executor = MainThreadExecutor(queue_adapter=delayed_queue, default_timeout=0.05)

        def slow_action():
            executed.append("ran")
            return "ok"

        with self.assertRaises(TimeoutError):
            executor.execute(slow_action)

        # Delayed processing of the queue item (e.g. after NVDA unfreezes)
        self.assertEqual(len(delayed_queue.queue_items), 1)
        delayed_queue.queue_items[0]()

        # Abandoned work must NOT have executed
        self.assertEqual(executed, [], "Abandoned work must not execute after caller timeout")


if __name__ == "__main__":
    unittest.main()
