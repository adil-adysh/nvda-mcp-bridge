# Correlated action traces

Every action the bridge executes gets one **trace**: a bounded, structured
record of what was asked, what ran, what was observed, what failed, and what is
still unknown. The native NVDA log remains a separate evidence source; a trace
links to it rather than replacing it.

Read one back with `get_action_trace`. Every action response already carries the
id and a compact summary, so no extra call is needed to find out that something
went wrong — only to find out *why*.

## Three identifiers, three meanings

| Identifier | Scope | Who creates it | Where it appears |
| --- | --- | --- | --- |
| `trace_id` | One logical action | The bridge, at `execute_action` | Every action envelope, every stored trace, every bridge log line for the action |
| `request_id` | One HTTP request | The caller, or the bridge if absent | The envelope only |
| MCP request id | One MCP protocol call | The MCP client | The MCP protocol; **not** forwarded to the bridge |

A logical action is one `/action` call. Nothing in the bridge currently performs
several HTTP requests for one action, but the distinction is enforced so that a
future composed action does not have to invent a new correlation scheme: a caller
may send `trace_id` in the request body, and several requests carrying the same
id would produce several HTTP request ids and one logical action.

`mcp_request_id` is recorded as `null` on purpose. The Rust companion does not
forward the MCP request id, and guessing an association would be worse than
admitting there is none; the trace lists this as a known limitation.

Bridge log lines carry `trace_id=…` so that the native NVDA log can be searched
for one action:

```
INFO - bridge.action.success action=press duration_ms=214.3 trace_id=at-9f3c…
```

## Where the record is built

Nothing here runs an action; it observes the existing pipeline.

```
MCP client
  └─ Rust companion ── POST /action ────────────────┐
                                                    │
  Python bridge, per request:                       │
    execute_action()                                │
      ActionTraceRecorder.start_action()  ── trace opened, journal cursor taken
      registry lookup / permission check
      ToolExecutor.execute(trace=…)       ── validation, dispatch, handler,
                                             output validation, serialization
      Settler.run(journal=…, trace=…)     ── settle started, event window,
                                             projection delta, expectation
      _finish_action()                    ── native log match, assemble, store
      envelope += trace_id, trace summary
                                                    │
  Rust companion: keeps `trace_id` and `trace` in both success and error content
```

Instrumentation lives in shared layers — the executor and the settler — rather
than in individual handlers, so a new tool inherits it by declaring
`COORDINATOR` mode and calling `context.settle(...)`.

## The record

```json
{
  "schema_version": 1,
  "trace_id": "at-9f3c2a1b8e4d5f607182",
  "action": {
    "name": "press",
    "origin": null,
    "started_at": "2026-11-12T15:10:02.104Z",
    "finished_at": "2026-11-12T15:10:02.318Z",
    "duration_ms": 214.3,
    "request_ids": ["6f1a…"],
    "mcp_request_id": null
  },
  "request": {
    "params": {"keys": "tab"},
    "params_meta": {"truncated": false, "redacted_fields": [], "dropped_fields": []},
    "validated": true
  },
  "lifecycle": {"milestones": [
    {"name": "received", "at": "…", "elapsed_ms": 0.1, "source": "action_lifecycle"},
    {"name": "validated", "status": "ok", "elapsed_ms": 1.2},
    {"name": "dispatched", "status": "dispatched", "elapsed_ms": 1.3},
    {"name": "handler_started", "status": "running", "elapsed_ms": 1.4},
    {"name": "settling_started", "status": "running", "elapsed_ms": 1.5},
    {"name": "settling_completed", "elapsed_ms": 210.0},
    {"name": "handler_completed", "status": "succeeded", "elapsed_ms": 212.0},
    {"name": "tool_completed", "status": "succeeded", "elapsed_ms": 212.5},
    {"name": "completed", "status": "succeeded", "elapsed_ms": 214.3}
  ]},
  "execution": {
    "dispatch_status": "dispatched",
    "handler_status": "succeeded",
    "tool_status": "succeeded",
    "exception": null,
    "outcome": "succeeded"
  },
  "observation": {
    "journal_id": "4b1e9c7a2d5f8e03",
    "from_event_version": 118,
    "to_event_version": 121,
    "event_count": 3,
    "events": [{"kind": "focus", "kinds": ["gainFocus"], "count": 1, "version": 121}],
    "events_complete": true,
    "events_truncated": false,
    "projection_available": true,
    "projection_changed": true,
    "baseline_state_version": 41,
    "final_state_version": 42,
    "delta": {"focus": {"from": {"role": "BUTTON"}, "to": {"role": "TEXT"}}},
    "settling_status": "settled",
    "settled_ms": 208.4,
    "budget_ms": 1000,
    "budget_expired": false,
    "uncertain": false,
    "expectation": {"status": "not_provided", "evaluated": false, "expected": null, "mismatch": null},
    "observation_status": "changed"
  },
  "completion": {
    "request_accepted": true,
    "validated": true,
    "dispatched": true,
    "handler_returned": true,
    "handler_succeeded": true,
    "tool_succeeded": true,
    "expectation_status": "not_provided",
    "expectation_satisfied": null,
    "expectation_mismatch": null,
    "ui_change_detected": true,
    "observation_status": "changed",
    "outcome": "succeeded"
  },
  "evidence": {
    "bridge_logs": [{"at": "…", "level": "info", "event": "validated", "trace_id": "at-…"}],
    "native_log_matches": [
      {"line": "INFO - bridge.action.success … trace_id=at-…", "severity": "INFO",
       "matching": "exact_trace_id", "association": "direct"}
    ],
    "native_log_status": "exact",
    "sources": ["action_lifecycle", "bridge_log", "event_journal", "nvda_native_log",
                "settler", "state_projector", "tool_executor", "handler"],
    "limitations": ["Projection changed; …"]
  },
  "retention": {"state": "finalized", "stored_at": "…", "expires_at": "…",
                "max_traces": 50, "max_age_seconds": 900.0}
}
```

### Status vocabularies

`execution.outcome` (also `completion.outcome`) is one of `running`, `succeeded`,
`failed`, `timed_out`, `cancelled`, `incomplete`, `unknown`. `incomplete` means
the action never got far enough to know — the dispatch timed out, the connection
died, or an internal error interrupted the record. A response being generated is
never on its own a reason to say `succeeded`.

`observation.observation_status` keeps seven situations apart:

| Status | Means |
| --- | --- |
| `changed` | The projection differs from the baseline |
| `no_detectable_change` | Nothing the projector captures changed, and no events arrived |
| `events_without_projected_change` | Events arrived; the projected fields look the same |
| `projection_unavailable` | No projection could be captured |
| `timed_out` | The settle budget expired while the UI may still have been moving |
| `events_incomplete` | The event window is missing entries |
| `not_observed` | No observation ran for this action |

`observation.expectation.status` is `not_provided`, `satisfied`, `not_satisfied`
or `not_evaluated` (an `expect` was supplied but no projection existed to check
it against). An action can succeed and still miss its postcondition:
`completion.outcome` stays `succeeded` and `expectation_status` says
`not_satisfied`. Those are two different facts and the record keeps them apart.

### Event cursor semantics

The event journal reports two numbers, and confusing them loses events:

* `to_version` — the last event version **in this page**. This is the value to
  pass back as `since`.
* `event_version` (and its older alias `state_version`) — the journal head after
  the page was taken.

`has_more` says another page is already waiting; `next_cursor` repeats
`to_version` under a name that says to use it. The Rust event pump advances to
`to_version`, never to the head, and fetches remaining pages back to back.

`cursor_status` distinguishes `ok`, `paged` (more remains), `stale` (the cursor
predates retained history, so events were lost) and `future_cursor` (the cursor
is beyond this journal's head, which means it came from another journal — an NVDA
restart). `complete` describes the page's range; `caught_up` describes the
caller. A page can be complete and still not caught up.

## Evidence, and what it is worth

Each evidence item names its source and, for log matches, how it was matched:

| `matching` | `association` | Meaning |
| --- | --- | --- |
| `exact_trace_id` | `direct` | The line contains this action's `trace_id`; the bridge wrote it |
| `time_window` | `temporally_correlated` | A WARNING/ERROR line whose timestamp falls in the action window; NVDA's logger does not carry the trace id |
| — | `unavailable` | The log could not be read; `native_log_reason` says why |

Lines the bridge itself writes land in NVDA's log without a level prefix, so
their `severity` is reported as `UNKNOWN`; they are still direct evidence,
because they carry the trace id. NVDA's own records do carry a prefix, which is
what the time-window path relies on.

The record never presents correlation as cause. `evidence.limitations` always
contains at least the relevant caveats, including:* `projection_changed = false` means the fields the projector captures (focus,
  its ancestor chain, window, dialog, selection) did not change. It is not proof
  that the rest of the UI was unchanged;
* events inside the window are correlated with the action, not caused by it;
* the native log cannot carry a trace id, so matches beyond the bridge's own
  lines are best-effort;
* MCP request ids are not forwarded, so `mcp_request_id` is `null`;
* traces are in memory only and expire.

## Bounds and retention

| Bound | Value |
| --- | --- |
| Retained traces | 50 |
| Trace age | 900 s |
| Parameter payload | 2 KB serialized, 300 chars per string |
| Events per trace | 50 |
| Native log matches | 20 |
| Traceback | 2 000 chars |
| Limitations | 12 |

Credential-shaped parameter names (`password`, `token`, `secret`, `apiKey`,
`authorization`, …) are replaced with `<redacted>` and named in
`params_meta.redacted_fields`. Nothing in a trace holds a live NVDA object: the
projection and the events are plain JSON copies.

Retention and eviction are reported by `get_action_trace`'s `retention` block
(`max_traces`, `max_age_seconds`, `retained_count`) so an agent can tell "this
never existed" from "this expired".

## Using it

```
press {keys: "tab", expect: {"focus.role": "BUTTON"}}
  → result.changed=true, trace_id="at-9f3c…"
  → trace.observation_status="changed", trace.expectation_status="satisfied"

get_action_trace {trace_id: "at-9f3c…"}
  → found=true, trace={…the record above…}
```

An expired or unknown id is a normal answer, not an action failure:
`found: false`, `missing_reason: "expired_or_unknown"`, plus the retention
window. The bridge has no protocol error code meaning "diagnostic record
unavailable", and every existing code either blames the caller's request or the
action; inventing one here would be a protocol change, so the tool answers
instead of failing.

A read is independent of the `nvda://ui/state` resource: `get_action_trace` is a
plain tool call and works whether or not a client subscribes to resources.

## Code map

| Path | Responsibility |
| --- | --- |
| `server/action_trace.py` | Record shape, sanitization, recorder, bounded store, assembler, log matching |
| `server/http_bridge.py` | Opens the trace at the action boundary, finalizes it around the envelope |
| `core/executor.py` | Stages: validation, dispatch, handler, output validation, serialization |
| `server/settle.py` | Observation: event window, projection delta, budget, expectation |
| `tools/handlers.py`, `tools/nvda_tools.py` | `get_action_trace` |
| `rust_mcp_bridge/src/main.rs`, `protocol.rs` | Preserves `trace_id`/`trace`, paginates the event pump |
