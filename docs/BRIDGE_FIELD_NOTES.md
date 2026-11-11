# Field notes: driving the bridge through DSH's MCP client

A first end-to-end exercise of the v2 surface (protocol `api_level 2`) from a
real MCP client — DSH's Web GUI — against whatever happened to be on screen:
Microsoft Edge showing the DSH web app, a Chromium/IA2 + UIA target with about
2,300 elements.

The point was not to prove the tools exist but to find where the contract
frays in ordinary use. It frays in four places, one of which is a real bug in
the GUI being driven.

## What worked

| Call | Result | Wall time |
| --- | --- | --- |
| `observe(focus)` | focus + 8 ancestors, refs `f`, `a1`…`a8`, `state_version` 22 | 17.7 ms |
| `locate(role=button)` | 20 browser-chrome buttons, `method: uia`, 383–405 candidates | 95–127 ms |
| `locate(role=button, within=<section>)` | page buttons, `method: uia-window`, 385 candidates | 104 ms |
| `locate(role=edit)` | address bar, composer, find box, one unnamed edit | 128–168 ms |
| `press("control+f")` | find bar opened; delta carried name, location, states, window title | 362 ms |
| `set_value(ref, "bridge round-trip")` | `mode: platform`, delta `value: "" → "bridge round-trip"` | 302 ms |
| `read(ref, scope=document)` | `"bridge round-trip"`, `source: uia` | 17.6 ms |
| `press("escape")` | find bar closed; delta `value → ""`, focus back to composer | 250 ms |

That is the whole chain an agent needs — find, focus, write, read back,
dismiss — and the read-back matched exactly what was written. The `result`
block behaved as designed: `changed`, `delta` and `settled_ms` arrived on every
call that altered anything, and stayed empty on the calls that did not.

## Finding 1 — `set_value` in keyboard mode can report success without writing

Target: the DSH composer (`EDITABLETEXT`, `MULTILINE`, focused).

```
set_value(f, "bridge round-trip")
  → success: true, mode: "keyboard", units: 17, duration 453 ms
  → result: { ok: true, changed: false, delta: {}, uncertain: false }
```

The field was still empty afterwards, and `read` agreed. So `changed: false`
was honest — but `success: true` plus `units: 17` reads as "17 characters were
typed", and `uncertain: false` claims the operation was confirmed when it was
not. An agent that checks only `success` will believe it wrote text that never
landed.

Why the text did not land is still open: the window plainly receives keys
(`control+f` and `escape` both worked seconds later), the object reported
`FOCUSED`, and the keystrokes were dispatched without error. The composer is a
custom editor, so it is the leading suspect — but the tool cannot tell you that.

Suggested contract change: after a keyboard-mode write, read the value back and
report `verified: false` (or `uncertain: true`) when the projection did not
move. The information is already at hand; it is thrown away.

## Finding 2 — `locate` reports the placeholder as an edit's value

For the same composer:

- `locate(role=edit)` → `value: "Message or run a task, / commands, @ files or sessions"`
- `observe`/`read` → `value: ""`

Same for the find box, whose `value` came back as `"Find on page"` before
anything had been typed. UIA's `ValuePattern` returns the placeholder for these
elements; NVDA's projection returns the real content. Two views of one control,
disagreeing, with nothing in the payload saying which one you are looking at.

Suggested: mark the field as `value_source: "uia"` and/or fall back to the
projection, or drop `value` from `locate` results for edits and let callers
`read` the ref. The second is cheaper and removes a trap.

## Finding 3 — `within` is a hint, not a scope

`locate(role=button, within=<an Ia2Web section>)` returned `method: "uia-window"`
and searched 385 candidates — the whole window — because the NVDA object had no
UIA element and was resolved to its window. The label is honest and the result
was still correct, but "search inside this container" is not what happened, and
a caller who expects containment (for example, to disambiguate two identically
named buttons) gets a silent superset. Worth saying so in the tool description.

## Finding 4 — the DSH web GUI names a disclosure after its whole paragraph

From `get_navigator_object`, the collapsed reasoning block:

```
role: button, states: {COLLAPSED, FOCUSABLE}
name: "Good question. Let me think about what..."
      (the entire first paragraph of the reasoning, several hundred characters)
```

A screen reader reads that paragraph as a button label, and there is no short
name to jump between disclosures. `aria-label="Show reasoning"` with the text as
content (and `aria-expanded` for the state) would fix both.

## Gap: no way to activate a window

Two of the obvious tasks to try — "open Notepad, write, save" — could not be
started from MCP at all. The bridge can move focus inside the foreground
window, but nothing can raise a covered one: `SetForegroundWindow` from an
external process is refused by Windows, and the working technique
(`force_foreground` in `scripts/live_acceptance.py`) lives outside the tool
surface. An `activate_window(title-or-handle)` verb — implemented inside NVDA,
which the foreground rules treat differently — would close the largest remaining
hole for task-style automation.

## Note: a dismissed control keeps answering

After `escape` closed the find bar, `read` on the old ref still returned
`"bridge round-trip"`. The element was hidden, not dead, so identity checking
passed and the value was still readable. That is defensible, but it means
"ref still works" is not evidence that the control is on screen; callers need
`observe` for that.

## Suggested next steps

1. Verify `set_value` after a keyboard write and report the result honestly.
2. Decide what `locate`'s `value` means for edits, and say it in the payload.
3. Document `within` as a search-root hint, or make widening an error.
4. Add `activate_window` — the missing verb for anything that does not start in
   the foreground window.
