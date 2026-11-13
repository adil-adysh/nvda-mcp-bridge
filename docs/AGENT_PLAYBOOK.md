# Agent playbook

Five canonical loops for the NVDA MCP bridge. The principle in all of them is
the same: observe once, keep the refs (not the `state_version`) as the locator,
act by `ref`, then read `result.verification` before deciding whether to observe
again.

`state_version` is an advisory projection revision. It advancing does **not**
invalidate a reference; a reference ends when it expires, the object/window
changes identity, the Bridge instance is replaced, or the target is gone.

## 1. Menu → submenu → item

```text
observe(include=["focus","dialog"])
press("nvda+n")
wait_for_events(timeout_ms=2000)          # since defaults to the journal head
observe(include=["focus","dialog","children"])
navigate("next") / press("down")
press("return")
```

If the menu is open and an action is refused with `4203`, the menu is blocking
the action; press `escape` or finish the menu first.

## 2. Dialog → edit → submit

```text
observe(include=["dialog"], detail="full")
set_value(ref="d3", value="bridge")
invoke(ref="d5")                          # e.g. OK
```

`set_value` returns `previous`, `observed` and `verified`. A `verified: false`
is an answer, not a transport failure: read `observed` before retrying.

## 3. List selection

```text
observe(include=["focus","children"])
focus(ref="c2")
press("down")
observe(include=["focus"])
```

`navigate` moves through the accessibility tree, not tab order. For a virtual
list where children are not exposed, use `locate` or `press` instead.

## 4. Find a control that is not focused

```text
observe()                                  # get top_window ref "w"
locate(role="BUTTON", name="Save", within="w")
invoke(ref="n1")
```

`within` must be a reference, never a window title. An empty `matches` list is
a completed search; `truncated` is what says it stopped early.

## 5. Log triage

```text
health_snapshot()                          # lean status
summarize_logs(include=["counts","exceptions"], limit=10)
search_logs(levels=["ERROR"], limit=20)
get_log_context(entry_id="…")
get_action_trace(trace_id="at-…")          # for a known action
```

Use `min_level` or `levels`, not both. `get_logs` is the raw tail; prefer
`search_logs` when you know what you are looking for.

## Verification, not optimism

Any action that takes `expect` returns `result.verification`:

* `confirmed` — the post-action observation matched every `expect` path.
* `contradicted` — it did not; `result.evidence` carries the expected/observed
  values (bounded, redacted). Decide from the evidence, do not re-dispatch
  blindly.
* `unavailable` — no reliable read-back existed (for example a key press with
  no `expect`). This is not `uncertain`; it simply makes no claim.

`set_value` has its own read-back and reports `verified` / `observed` plus
`verification`. A `verified: false` is an answer, not a transport failure.

## Event catch-up

* No cursor: `wait_for_events()` waits from the current journal head and never
  replays history.
* Explicit catch-up: pass `since` **and** `journal_id` from the previous page.
* `resync_required: true` (or `expired: true`) means the returned history is
  incomplete. Re-observe current state, then continue from `resync_from`.
* `cursor_status: "journal_mismatch"` means the Bridge or NVDA restarted. The
  old version is meaningless in the new journal; treat it as a fresh start.
* `GET /health`, `/tools` and every action expose `instance_id`. A changed id
  invalidates cached refs and cursors; re-observe.

## Recovery rules

* `4206 STALE_REFERENCE`: read `details.reason_code` and call `observe` for a
  fresh ref. Do not retry the same ref, and do not substitute a lookalike.
* `4103 INVALID_PARAMS_SCHEMA`: read the field path and expected shape in
  `error.details`; do not guess. A window title passed as `ref` is this error,
  not `4206`.
* `4203 MENU_OPEN_BLOCKING`: finish or dismiss the NVDA menu first.
* `4202 MODAL_DIALOG_BLOCKING`: act on the dialog, or `dismiss` it.
* `result.uncertain: true`: the input was posted but the effect was not
  confirmed. Observe before assuming success; never auto-retry a mutation.
* `error.recovery.action` is a typed next step. It is advice, not permission to
  replay an uncertain operation.
