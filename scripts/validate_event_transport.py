"""Validate the transport claims in the event-delivery design against the bridge.

Checks, in order:
  1. a long-poll with no events returns an empty page after roughly the timeout
  2. a normal action stays fast while a long-poll is in flight (no starvation)
  3. two concurrent long-polls both work and an action still lands
  4. the cursor contract reports the fields a consumer needs to resync
  5. paging with a limit returns a cursor at the last entry, not the head

Run: python scripts/validate_event_transport.py
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

BRIDGE = "http://127.0.0.1:8765"
TOKEN = (Path(os.environ["APPDATA"]) / "nvda" / "nvda-mcp-bridge" / "token.key").read_text(
    encoding="utf-8"
).strip()
HEADERS = {"X-NVDA-Bridge-Token": TOKEN, "Content-Type": "application/json"}


def get(path: str, timeout: float = 40.0):
    request = urllib.request.Request(BRIDGE + path, headers=HEADERS, method="GET")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read()), (time.perf_counter() - started) * 1000
    except urllib.error.HTTPError as exc:
        return {"_status": exc.code}, (time.perf_counter() - started) * 1000


def action(name: str, params: dict | None = None):
    payload = json.dumps({"action": name, "params": params or {}}).encode()
    request = urllib.request.Request(BRIDGE + "/action", data=payload, headers=HEADERS, method="POST")
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read()), (time.perf_counter() - started) * 1000


def main() -> int:
    failures = 0

    # 1. An idle long-poll from the current head respects its timeout and
    #    returns an empty page (it must not spin or return the backlog).
    head_page, _ = get("/events?since=0&timeout_ms=0")
    head = head_page.get("state_version", 0)
    page, elapsed = get(f"/events?since={head}&timeout_ms=1500")
    quiet = page.get("events") == []
    on_time = 1200 <= elapsed <= 3000
    print(f"1. idle long-poll: {elapsed:.0f} ms, events={len(page.get('events', []))}, "
          f"timed_out_cleanly={quiet and on_time}")
    failures += 0 if (quiet and on_time) else 1

    # 2. The long-poll must not stall ordinary calls: it runs on an HTTP worker
    #    thread, never on NVDA's main thread.
    baseline, _ = action("observe")
    baseline_ms = baseline.get("duration_ms", 0)

    holder: dict = {}

    def poll() -> None:
        holder["page"], holder["elapsed"] = get("/events?since=0&timeout_ms=3000")

    worker = threading.Thread(target=poll, daemon=True)
    worker.start()
    time.sleep(0.4)  # let the poll block

    during, _ = action("observe")
    during_ms = during.get("duration_ms", 0)
    worker.join(timeout=10)

    print(f"2. action during in-flight long-poll: {during_ms} ms "
          f"(baseline {baseline_ms} ms) -> {'not blocked' if during_ms < baseline_ms + 60 else 'SLOW'}")
    failures += 0 if during.get("success") and during_ms < baseline_ms + 60 else 1

    # 3. Two concurrent waiters plus a normal call.
    results: list = []

    def poll2() -> None:
        results.append(get("/events?since=0&timeout_ms=1200")[0])

    workers = [threading.Thread(target=poll2, daemon=True) for _ in range(2)]
    for w in workers:
        w.start()
    time.sleep(0.3)
    concurrent_action, _ = action("observe")
    for w in workers:
        w.join(timeout=10)
    print(f"3. two waiters + action: waiters={len(results)} action_ok={concurrent_action.get('success')}")
    failures += 0 if len(results) == 2 and concurrent_action.get("success") else 1

    # 4. Cursor fields a consumer needs: a journal identity to detect a restart,
    #    a cursor that never jumps over unseen events, and a flag saying a page
    #    was cut so the consumer keeps polling instead of assuming it is caught up.
    current, _ = get("/events?since=0&timeout_ms=0")
    required = {
        "journal_id",
        "state_version",
        "from_version",
        "to_version",
        "truncated",
        "has_more",
        "dropped",
        "events",
    }
    missing = required - set(current)
    print(f"4. cursor contract: missing={sorted(missing)} journal={current.get('journal_id')} "
          f"state_version={current.get('state_version')}")
    failures += 0 if not missing else 1

    # 4b. Paging must not skip: with a limit of one, the cursor has to point at
    #     the entry that was returned, never at the head of the journal.
    page_one, _ = get("/events?since=0&timeout_ms=0&limit=1")
    events_one = page_one.get("events") or []
    paged_right = (
        not events_one
        or page_one.get("to_version") == events_one[-1].get("version")
    )
    print(f"4b. paged cursor: events={len(events_one)} to_version={page_one.get('to_version')} "
          f"head={page_one.get('state_version')} has_more={page_one.get('has_more')}")
    failures += 0 if paged_right else 1

    # 5. A cursor at head must return immediately with an empty page.
    head = current.get("state_version", 0)
    at_head, at_head_ms = get(f"/events?since={head}&timeout_ms=0")
    fast = at_head_ms < 500 and at_head.get("events") == []
    print(f"5. at-head read: {at_head_ms:.0f} ms, events={len(at_head.get('events', []))}, fast={fast}")
    failures += 0 if fast else 1

    print(f"\nresult: {'PASS' if failures == 0 else f'{failures} check(s) failed'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
