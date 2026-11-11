# pyright: reportMissingImports=false, reportUnusedCallResult=false
"""Dedicated tests for keyboard dispatch semantics and thread coordination.

Verifies:
1. Single gesture dispatch
2. Repeated gestures
3. Queue scheduling failure
4. Main-thread callback exception
5. Callback raising TypeError
6. Cancellation before first gesture
7. Cancellation after multiple gestures
8. Timeout before execution
9. Timeout after partial execution
10. Concurrent keyboard sequences
11. Correct thread identity
12. Confirmed dispatch count
"""

import importlib
import os
import queue
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
send_keyboard = ui_module.send_keyboard
send_single_gesture = ui_module.send_single_gesture
KeyboardExecutionError = ui_module.KeyboardExecutionError
KeyboardTimeoutError = ui_module.KeyboardTimeoutError


class SimulatedNVDAQueue:
    """Controllable simulated NVDA event queue that executes on a distinct simulated main thread."""

    def __init__(self):
        self._work_queue = queue.Queue()
        self._running = True
        self.main_thread_id = None
        self.executed_thread_ids = []
        self._thread = threading.Thread(target=self._run_loop, name="Simulated-NVDA-MainThread")
        self._thread.daemon = True
        self._thread.start()
        # Wait for thread id
        while self.main_thread_id is None:
            time.sleep(0.001)

    def _run_loop(self):
        self.main_thread_id = threading.get_ident()
        while self._running:
            try:
                item = self._work_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            if item is None:
                break
            fn, done_event = item
            try:
                self.executed_thread_ids.append(threading.get_ident())
                fn()
            finally:
                if done_event:
                    done_event.set()

    def queue_fn(self, fn, wait=False, timeout=5.0):
        evt = threading.Event() if wait else None
        self._work_queue.put((fn, evt))
        if wait:
            return evt.wait(timeout=timeout)
        return True

    def stop(self):
        self._running = False
        self._work_queue.put(None)
        self._thread.join(timeout=2.0)


class TestKeyboardDispatchSemantics(unittest.TestCase):
    def setUp(self):
        self.sim_queue = SimulatedNVDAQueue()
        _queue_handler.queueFunction = lambda q, fn, *args, **kwargs: self.sim_queue.queue_fn(fn)

    def tearDown(self):
        self.sim_queue.stop()

    def test_01_single_gesture_dispatch(self):
        """1. Verify single gesture dispatch completes and confirms sent_count == 1."""
        dispatched = []

        def dispatcher(resolved):
            dispatched.append(resolved.name)

        result = send_keyboard("enter", repeat=1, main_thread_fn=dispatcher)
        self.assertEqual(result["sent_count"], 1)
        self.assertTrue(result["sent"])
        self.assertTrue(result["completed"])
        self.assertEqual(dispatched, ["enter"])

    def test_02_repeated_gestures(self):
        """2. Verify repeated gestures dispatch exactly the requested count with pacing."""
        dispatched = []

        def dispatcher(resolved):
            dispatched.append(resolved.name)

        result = send_keyboard("tab", repeat=4, delay_ms=10, main_thread_fn=dispatcher)
        self.assertEqual(result["sent_count"], 4)
        self.assertTrue(result["completed"])
        self.assertEqual(len(dispatched), 4)

    def test_03_queue_scheduling_failure(self):
        """3. Verify queue scheduling failure produces an explicit error without background fallback."""
        _queue_handler.queueFunction = lambda q, fn, *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("Simulated queueHandler failure")
        )

        with self.assertRaises(RuntimeError) as ctx:
            # Run from worker thread so queueHandler is invoked
            result = []
            exc = []

            def worker():
                try:
                    result.append(send_keyboard("escape", repeat=1, main_thread_fn=None))
                except Exception as e:
                    exc.append(e)

            t = threading.Thread(target=worker)
            t.start()
            t.join()
            if exc:
                raise exc[0]

        self.assertIn("Failed to queue keyboard gesture to NVDA main thread", str(ctx.exception))

    def test_04_main_thread_callback_exception(self):
        """4. Verify main-thread callback exceptions propagate faithfully."""
        def failing_dispatcher(resolved):
            raise ArithmeticError("Custom calculation failure inside gesture")

        with self.assertRaises(KeyboardExecutionError) as ctx:
            send_keyboard("downArrow", repeat=3, main_thread_fn=failing_dispatcher)

        self.assertIsInstance(ctx.exception.original_error, ArithmeticError)
        self.assertEqual(ctx.exception.sent_count, 0)
        self.assertEqual(ctx.exception.repeat, 3)

    def test_05_callback_raising_type_error_no_duplicate_invocation(self):
        """5. Verify callback raising TypeError is NOT caught as signature mismatch and is not retried."""
        call_count = 0

        def buggy_dispatcher(resolved):
            nonlocal call_count
            call_count += 1
            raise TypeError("Intentional TypeError raised inside user callback")

        with self.assertRaises(KeyboardExecutionError) as ctx:
            send_keyboard("ctrl+s", repeat=1, main_thread_fn=buggy_dispatcher)

        # Must be called exactly ONCE: no retry without arguments
        self.assertEqual(call_count, 1)
        self.assertIsInstance(ctx.exception.original_error, TypeError)
        self.assertEqual(ctx.exception.sent_count, 0)

    def test_06_cancellation_before_first_gesture(self):
        """6. Verify cancellation before first gesture terminates cleanly with sent_count == 0."""
        cancel_evt = threading.Event()
        cancel_evt.set()

        dispatched = []
        result = send_keyboard("alt+f4", repeat=3, cancellation_event=cancel_evt, main_thread_fn=dispatched.append)

        self.assertTrue(result["cancelled"])
        self.assertFalse(result["completed"])
        self.assertEqual(result["sent_count"], 0)
        self.assertEqual(len(dispatched), 0)

    def test_07_cancellation_after_multiple_gestures(self):
        """7. Verify cancellation after multiple gestures halts further dispatch and reports partial count."""
        cancel_evt = threading.Event()
        dispatched = []

        def paced_dispatcher(resolved):
            dispatched.append(resolved.name)
            if len(dispatched) == 2:
                cancel_evt.set()

        # Run on a background thread so pacing delay uses wait(cancellation_event)
        result_holder = []

        def worker():
            res = send_keyboard(
                "tab",
                repeat=5,
                delay_ms=20,
                cancellation_event=cancel_evt,
                main_thread_fn=paced_dispatcher,
            )
            result_holder.append(res)

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=2.0)

        self.assertTrue(result_holder)
        res = result_holder[0]
        self.assertTrue(res["cancelled"])
        self.assertEqual(res["sent_count"], 2)
        self.assertEqual(len(dispatched), 2)
        self.assertFalse(res["completed"])

    def test_08_timeout_before_execution_waiting_for_input_lock(self):
        """8. Verify timeout waiting for input lock raises KeyboardTimeoutError with sent_count == 0."""
        from nvdaMcpBridge.nvda_bridge.ui import _input_lock

        # Hold lock from another thread
        _input_lock.acquire()
        try:
            with self.assertRaises(KeyboardTimeoutError) as ctx:
                send_keyboard("windows+r", repeat=1, timeout=0.05)

            self.assertEqual(ctx.exception.sent_count, 0)
            self.assertTrue(ctx.exception.timed_out)
        finally:
            _input_lock.release()

    def test_09_timeout_after_partial_execution(self):
        """9. Verify timeout after partial execution reports exact dispatched count."""
        dispatched = []

        def slow_dispatcher(resolved):
            dispatched.append(resolved)
            time.sleep(0.08)

        # Running on background thread with 50ms delay and 0.15s timeout
        worker_exc = []

        def worker():
            try:
                send_keyboard("space", repeat=5, delay_ms=50, timeout=0.15, main_thread_fn=slow_dispatcher)
            except Exception as e:
                worker_exc.append(e)

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=2.0)

        self.assertTrue(worker_exc)
        self.assertIsInstance(worker_exc[0], KeyboardTimeoutError)
        self.assertGreater(worker_exc[0].sent_count, 0)
        self.assertLess(worker_exc[0].sent_count, 5)
        self.assertTrue(worker_exc[0].timed_out)

    def test_10_concurrent_keyboard_sequences_do_not_interleave(self):
        """10. Verify concurrent keyboard sequences are serialized by input lock."""
        log = []

        def make_dispatcher(tag):
            def _disp(resolved):
                log.append(f"{tag}-start")
                time.sleep(0.02)
                log.append(f"{tag}-end")
            return _disp

        t1 = threading.Thread(
            target=lambda: send_keyboard("leftArrow", repeat=3, delay_ms=10, main_thread_fn=make_dispatcher("A"))
        )
        t2 = threading.Thread(
            target=lambda: send_keyboard("rightArrow", repeat=3, delay_ms=10, main_thread_fn=make_dispatcher("B"))
        )

        t1.start()
        t2.start()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        self.assertEqual(len(log), 12)  # 3 repeats * 2 (start/end) * 2 threads
        # All actions for whichever thread acquired the lock first must finish before the other starts
        first_tag = log[0].split("-")[0]
        second_tag = "B" if first_tag == "A" else "A"
        first_half = [item.split("-")[0] for item in log[:6]]
        second_half = [item.split("-")[0] for item in log[6:]]
        self.assertTrue(all(tag == first_tag for tag in first_half))
        self.assertTrue(all(tag == second_tag for tag in second_half))

    def test_11_correct_thread_identity(self):
        """11. Verify gesture sending from background thread executes strictly on simulated main thread."""
        dispatch_threads = []

        def dummy_send():
            dispatch_threads.append(threading.get_ident())

        gesture = _DummyGesture("test")
        gesture.send = dummy_send

        # Execute from worker thread through queueHandler
        def worker():
            _queue_handler.queueFunction = lambda q, fn, *args, **kwargs: self.sim_queue.queue_fn(fn)
            import nvdaMcpBridge.nvda_bridge.ui as ui_m
            # Monkeypatch resolve_gesture to return our gesture
            orig_resolve = ui_m.resolve_gesture
            ui_m.resolve_gesture = lambda g: gesture
            try:
                ui_m.send_keyboard("home", repeat=1, main_thread_fn=None)
            finally:
                ui_m.resolve_gesture = orig_resolve

        t = threading.Thread(target=worker, name="Worker-Thread")
        t.start()
        t.join(timeout=2.0)

        self.assertEqual(len(dispatch_threads), 1)
        self.assertEqual(dispatch_threads[0], self.sim_queue.main_thread_id)
        self.assertNotEqual(dispatch_threads[0], t.ident)

    def test_12_confirmed_dispatch_count_only_on_success(self):
        """12. Verify confirmed dispatch count increments only when callback succeeds."""
        attempts = 0

        def fail_on_third(resolved):
            nonlocal attempts
            attempts += 1
            if attempts == 3:
                raise ValueError("Third gesture dispatch failed")

        with self.assertRaises(KeyboardExecutionError) as ctx:
            send_keyboard("upArrow", repeat=5, main_thread_fn=fail_on_third)

        # 2 succeeded, 3rd failed; sent_count must be exactly 2
        self.assertEqual(ctx.exception.sent_count, 2)
        self.assertEqual(attempts, 3)


if __name__ == "__main__":
    unittest.main()
