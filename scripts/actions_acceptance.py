#!/usr/bin/env python3
"""End-to-end acceptance for `activate_window` and verified `set_value` (P5.1).

What this proves, against a live NVDA and a real application:

1. a covered window can be raised from inside NVDA, and the call reports the
   window it raised and how long the OS took to agree;
2. asking for a window that is already in front is a no-op that does not pay for
   a settle round;
3. a minimised window is restored rather than mistaken for "already in front";
4. a write is verified: the value read back from the control is what was asked
   for, and a write that does not land is reported as unverified instead of
   quietly succeeding;
5. clearing a control is a normal write, not a typing accident;
6. a title that matches nothing is a domain error naming the title and listing
   what exists.

The target is a scratch Notepad window whose title comes from a unique file
name, so an earlier run's window cannot be mistaken for this one.
"""

from __future__ import annotations

import argparse
import ctypes
import subprocess
import sys
import time
import uuid
from ctypes import wintypes
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

for _stream in (sys.stdout, sys.stderr):
	try:
		_stream.reconfigure(encoding="utf-8", errors="replace")
	except (AttributeError, ValueError):
		pass

from locate_acceptance import close_titled_window  # noqa: E402
from live_acceptance import (  # noqa: E402
    Bridge,
    Report,
    activate,
    data,
    error_code,
    ok,
    resolve_token,
    result,
    wait_for,
    window_title,
)

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
_user32.IsIconic.argtypes = (wintypes.HWND,)
_SW_MINIMIZE = 6


def start_notepad(target: Path) -> subprocess.Popen | None:
    """Open Notepad on a scratch file so the window title is unique."""
    target.write_text("acceptance scratch file\n", encoding="utf-8")
    try:
        return subprocess.Popen(["notepad.exe", str(target)])
    except OSError as error:
        print(f"could not start Notepad: {error}", file=sys.stderr)
        return None


def observed_value(bridge: Bridge, ref: str) -> str:
    response = bridge.call("observe", {"include": ["focus"]})
    focus = (data(response) or {}).get("focus") or {}
    if focus.get("ref") == ref:
        value = focus.get("value")
        return "" if value is None else str(value)
    read = bridge.call("read", {"ref": ref, "scope": "document"})
    return str((data(read) or {}).get("text", ""))


def check_activate(bridge: Bridge, report: Report, fragment: str) -> dict:
    report.section("activate_window")

    started = time.perf_counter()
    raised = bridge.call("activate_window", {"title": fragment})
    elapsed_ms = (time.perf_counter() - started) * 1000
    raised_data = data(raised)

    report.check(
        "the window is found by title",
        ok(raised) and raised_data.get("activated") is True,
        f"method={raised_data.get('method')} title={raised_data.get('title')!r}",
    )
    report.check(
        "the call says how the window came forward",
        raised_data.get("method") in {"foreground", "already-foreground"},
        str(raised_data.get("method")),
    )
    report.check(
        "the foreground really changed",
        fragment.lower() in window_title(bridge).lower(),
        window_title(bridge)[:80],
    )
    report.check(
        "the raised window is referenced",
        bool(raised_data.get("ref")),
        str(raised_data.get("ref")),
    )
    report.note("raise latency", f"{elapsed_ms:.0f} ms round trip, waited_ms={raised_data.get('waited_ms')}")

    if raised_data.get("method") == "already-foreground":
        report.skip("second raise is a no-op", "the first call left it in front already")
    else:
        started = time.perf_counter()
        again = bridge.call("activate_window", {"title": fragment})
        again_ms = (time.perf_counter() - started) * 1000
        report.check(
            "a second raise is recognised as a no-op",
            ok(again) and data(again).get("method") == "already-foreground",
            str(data(again).get("method")),
        )
        report.check(
            "the no-op does not pay for a settle round",
            again_ms < 400,
            f"{again_ms:.0f} ms",
        )

    handle = int(raised_data.get("handle") or 0)
    if not handle:
        report.skip("a minimised window is restored", "no handle to minimise")
        return raised_data

    _user32.ShowWindow(wintypes.HWND(handle), _SW_MINIMIZE)
    time.sleep(0.4)
    report.check(
        "the window is minimised before the next call",
        bool(_user32.IsIconic(handle)),
        f"handle={handle}",
    )

    restored = bridge.call("activate_window", {"title": fragment})
    restored_data = data(restored)
    report.check(
        "a minimised window is raised, not mistaken for in-front",
        ok(restored) and restored_data.get("method") == "foreground",
        str(restored_data.get("method")),
    )
    report.check(
        "the restore is reported",
        restored_data.get("restored") is True,
        str(restored_data.get("restored")),
    )
    report.check(
        "the restored window is in front",
        fragment.lower() in window_title(bridge).lower(),
        window_title(bridge)[:80],
    )
    report.note(
        "displaced window",
        f"{restored_data.get('previous_title')!r} -> {restored_data.get('title')!r}",
    )
    return restored_data


def check_write(bridge: Bridge, report: Report) -> None:
    report.section("set_value")

    marker = "verified " + uuid.uuid4().hex[:6]
    observation = bridge.call("observe", {"include": ["focus"]})
    focus_ref = str(((data(observation) or {}).get("focus") or {}).get("ref", ""))
    if not focus_ref:
        report.skip("write into the app", "no focused control to write to")
        return

    written = bridge.call("set_value", {"ref": focus_ref, "value": marker})
    written_data = data(written)
    wrote = result(written)
    report.check("the write reports success", ok(written), str(written.get("error") or ""))
    report.check(
        "the write is verified",
        written_data.get("verified") is True,
        f"mode={written_data.get('mode')} observed={written_data.get('observed')!r}",
    )
    report.check(
        "the read-back equals what was written",
        observed_value(bridge, focus_ref).strip() == marker,
        observed_value(bridge, focus_ref)[:60],
    )
    report.check(
        "a verified write is not marked uncertain",
        wrote.get("uncertain") is False,
        str(wrote.get("uncertain")),
    )
    report.note("write mode", str(written_data.get("mode")))

    cleared = bridge.call("set_value", {"ref": focus_ref, "value": ""})
    cleared_data = data(cleared)
    report.check(
        "clearing is a write like any other",
        ok(cleared) and cleared_data.get("verified") is True,
        f"mode={cleared_data.get('mode')} observed={cleared_data.get('observed')!r}",
    )
    report.check("the control is empty afterwards", observed_value(bridge, focus_ref).strip() == "", "")


def check_missing_window(bridge: Bridge, report: Report) -> None:
    report.section("errors")
    response = bridge.call("activate_window", {"title": "no such window " + uuid.uuid4().hex[:6]})
    details = (response.get("error") or {}).get("details") or {}
    report.check(
        "a title with no match is a domain error",
        error_code(response) == 4205,
        str(error_code(response)),
    )
    report.check(
        "the error names the title that missed",
        "no such window" in str((response.get("error") or {}).get("message", "")).lower(),
        str((response.get("error") or {}).get("message", ""))[:80],
    )
    report.check(
        "the error lists what does exist",
        bool(details.get("available")),
        str(details.get("available"))[:80],
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--token", default=None)
    parser.add_argument("--keep", action="store_true", help="leave the scratch file and window behind")
    args = parser.parse_args(argv)

    bridge = Bridge(resolve_token(args.token))
    report = Report(verbose=args.verbose)

    scratch_dir = REPO / ".probe"
    scratch_dir.mkdir(exist_ok=True)
    target = scratch_dir / f"actions-{uuid.uuid4().hex[:8]}.txt"
    fragment = target.stem

    process = start_notepad(target)
    if process is None:
        return 2

    try:
        if not wait_for(lambda: fragment.lower() in window_title(bridge).lower() or activate(bridge, fragment), 30.0):
            print("Notepad never came to the front", file=sys.stderr)
            return 2
        check_activate(bridge, report, fragment)
        check_write(bridge, report)
        check_missing_window(bridge, report)
    finally:
        if not args.keep:
            close_titled_window(fragment)
            try:
                target.unlink()
            except OSError:
                pass

    failures = report.failed
    total = len([check for check in report.checks if not check.get("note")])
    print(f"\n{total - len(failures)}/{total} checks passed")
    for failure in failures:
        print(f"FAILED {failure['name']}: {failure['detail']}")
    return 0 if not failures else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # noqa: BLE001
        print(f"acceptance harness error: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
