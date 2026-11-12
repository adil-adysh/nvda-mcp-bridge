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

## What has since been fixed

1. **Done.** `set_value` now reads the control back and returns `verified`,
   `observed` and `value_source`; when the value did not land the envelope's
   `uncertain` is set. An empty value clears the control, and a platform write
   that changes nothing is retried once with keystrokes.
2. **Done.** `value_source` says which view answered (`uia-value`, `value`,
   `text`), so a placeholder is no longer indistinguishable from content.
3. **Partly.** `within` still widens when the ref has no UIA element of its own;
   `method: "uia-window"` and the `scope` string are the only warning. It is
   documented in `BRIDGE_API_REFERENCE.md`, not prevented.
4. **Done.** `activate_window(title?, handle?, exact=false)` raises a top-level
   window, restores it when minimised, verifies the foreground changed, and
   returns a ref for the window. A title with no match is `4205` with the
   available titles listed.

The DSH GUI finding (a reasoning disclosure named after its whole paragraph)
stands and is still worth an `aria-label`.

## Reading a document in order (P5.2)

Two tools came out of asking what a screen reader can do that the bridge could
not: `list_reading_targets` enumerates a document's headings, links, landmarks
and form fields *in reading order*, and `navigate_reading` moves the reading
position the way the quick nav keys do.

What the exercise changed:

1. **Chromium does support landmark quick nav.** The assumption that it did not
   came from reading `virtualBuffers/webKit.py`; Edge's document is a
   `ChromeVBuf`, which inherits `GeckoVBuf`'s type table. The tool now asks
   the document what it supports (`source: "probed"`) instead of consulting a
   table someone typed.
2. **One browser page, two implementations.** The same page was answered by
   `ChromeVBuf` on one run and `ChromiumUIATreeInterceptor` on another, with
   different metadata: heading levels come from a control field in the first and
   from the position's own `level` property in the second. The reply's
   `interface` field is what lets a caller tell them apart.
3. **Word is not browsable until asked.** `WordDocument` declares
   `shouldCreateTreeInterceptor = False`, so NVDA reads it in focus mode and
   there is no quick navigation to drive. `start_browse_mode` starts browse mode
   the way NVDA+space does, and the model is reused for the same application and
   window rather than rebuilt per call. It is an action rather than a flag on a
   read because it changes the user's session, and a `read_only` client must not
   be able to do that.
4. **Stepping is not iterating.** A UIA document searched *from a position*
   yields a single match, so `steps` moves the cursor between searches. That
   also means `navigate_reading` speaks the intermediate positions, exactly as
   pressing the key three times would.
5. **The document is the focused one, or nothing.** Resolving it from the
   foreground window looked right until the measurement: a browser window holds a
   document per tab, and nothing says which tab is showing. The tool now refuses
   when the focus is not in the window in front, and the refusal names the fix
   (F6, then ask again).
6. **A raised window is not a focused one.** Windows can move the foreground
   without moving the keyboard focus; a program that launches a browser gets the
   first and often not the second. `activate_window` now answers both questions:
   `activated` and `method` describe what it verified with Win32, and
   `focus_in_window` says whether the screen reader's focus came along — `false`
   means the reading tools will still see the previous window until something
   gives the new one keyboard focus (a click, or F6 in a browser). Before the
   field existed, "activated" read like "we are there now" and the next call
   answered about the wrong window.
7. **One position's worth of slack.** Twice in a few hundred acceptance checks,
   `navigate_reading(next, steps=2)` landed one heading further than the previous
   move reported. The call that lands is checked against its own `label` and
   `reached`, and both were right; the *start* of the next search was one
   position ahead, which is NVDA's virtual cursor updating on its own schedule
   after `moveTo()`. It is not fixed here — the honest check is the landing
   label, which every call reports, and the acceptance suite re-checks the
   document in front before it starts moving.

## The surface, weighed (api_level 3)

Asked whether the reading tools should replace the old ones, the answer came
from measuring the catalog rather than from taste. Nothing overlapped — and the
reading tools call the others (activate_window, read, invoke) — but the
catalog itself was 33 tools, 41 KB of definitions, a 120 KB `/tools` payload.
Every session paid that before doing anything.

So the aliases went, and only the aliases: `get_current_focus`,
`get_navigator_object`, `read_current_line`, `move_focus`,
`send_keyboard`, `get_desktop_object` and `get_window_title`. 26 tools at
api_level 3, `supported_api_levels: [2, 3]`.

At api_level 4 the surface is 27: `start_browse_mode` was added as the honest
home for the reading tools' `create` flag, and both reading tools gained
`timeout_ms` and `stopped` so a call that outruns its main-thread budget says
where it stopped instead of blocking the reader. `supported_api_levels` is
`[2, 3, 4]`, and the companion now checks membership rather than equality — the
exact-match rule had made the field decorative.

Two things make the next removal cheaper than this one:

`GET /tools?category=`
	`action,observation` is 58 KB of the 101 KB surface, and the companion asks
	for it with `NVDA_BRIDGE_CATEGORIES`. An unknown category is `4103` with
	the known list — never an empty catalog, which would look like "the bridge
	has no tools".

the error that names the replacement
	A removed name is `4102` with the action in the message, not a fallback to
	something similar. Deleting a name is safe only when the failure is loud.
