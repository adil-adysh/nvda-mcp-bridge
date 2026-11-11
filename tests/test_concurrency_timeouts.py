# pyright: reportMissingImports=false, reportUnusedCallResult=false

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


class _DummyGesture:
    @classmethod
    def fromName(cls, name):
        return _DummyGesture()

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

MainThreadExecutor = executor_module.MainThreadExecutor
send_keyboard = ui_module.send_keyboard


class ConcurrencyAndTimeoutTests(unittest.TestCase):
    def test_send_keyboard_cancellation_stops_repetitions(self):
        cancellation_event = threading.Event()
        dispatched_count = [0]

        def mock_dispatcher():
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

    def test_send_keyboard_serializes_concurrent_calls(self):
        execution_order = []

        def make_dispatcher(thread_id, gesture):
            def _dispatch():
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

        t1 = threading.Thread(target=run_keystrokes, args=("A", "left"))
        t2 = threading.Thread(target=run_keystrokes, args=("B", "right"))

        t1.start()
        t2.start()
        t1.join(timeout=3.0)
        t2.join(timeout=3.0)

        # Because _input_lock serializes keyboard streams, all "A" gestures must either
        # run entirely before "B", or all "B" gestures must run before "A".
        # They should NOT interleave like A, B, A, B.
        thread_ids = [item.split("_")[0] for item in execution_order]
        first_thread = thread_ids[0]
        # First 3 should all belong to first_thread
        self.assertEqual(thread_ids[:3], [first_thread, first_thread, first_thread])
        second_thread = "B" if first_thread == "A" else "A"
        self.assertEqual(thread_ids[3:], [second_thread, second_thread, second_thread])

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
            executor.execute(slow_action, timeout=0.05)

        self.assertEqual(len(executed), 0, "Action should not have executed before queue processed")

        # Now simulate NVDA's event loop finally processing the queued item
        for func in delayed_queue.queue_items:
            func()

        # Even though NVDA processed the queued callback, because the executor timed out,
        # cancelled.set() was called, so slow_action() was NOT executed!
        self.assertEqual(
            len(executed),
            0,
            "Cancelled/timed-out action must NOT execute when NVDA processes delayed queue",
        )


if __name__ == "__main__":
    unittest.main()
