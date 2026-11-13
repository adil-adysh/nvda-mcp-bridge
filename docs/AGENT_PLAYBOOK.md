# Agent playbook

Five canonical loops for the NVDA MCP bridge. The principle in all of them is
the same: observe once, keep the `state_version`, act by `ref`, then trust
`result` before reading the screen again.

## 1. Menu → submenu → item

```text
observe(include=["focus","dialog"])
press("nvda+n")
wait_for_events(timeout_ms=2000)          # since defaults to now
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

## Recovery rules

* `4206 STALE_REFERENCE`: call `observe` and use a ref from the new `refs` map.
  Do not retry the same ref.
* `4103 INVALID_PARAMS_SCHEMA`: read the field path and expected shape in
  `error.details`; do not guess.
* `4203 MENU_OPEN_BLOCKING`: finish or dismiss the NVDA menu first.
* `4202 MODAL_DIALOG_BLOCKING`: act on the dialog, or `dismiss` it.
* `result.uncertain: true`: the input was posted but the effect was not
  confirmed. Observe before assuming success.
