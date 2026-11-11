# Implementation plan: from a read-only inspector to an agent that acts

Status: **implemented** (P1-P4). The parameters below were measured rather than
assumed, and the pass/fail checks in §5 were run against a live NVDA; see §5.1 for
what was observed and `scripts/live_acceptance.py` for the harness.
Verified against NVDA 2026.3.1 on Windows 11, with Edge/Firefox (Chromium/UIA and
Gecko/IA2) and Notepad, using the live bridge and `execute_python` inside NVDA's
process.

This document answers six questions directly, then specifies the work:

1. [How should we implement?](#3-architecture)
2. [Should we drop the existing tools?](#4-tool-surface)
3. [What should we redesign?](#2-what-to-redesign-and-what-not-to)
4. [What is the plan?](#5-phases)
5. [What is the new architecture?](#3-architecture)
6. [How do NVDA events work, and are the APIs confirmed?](#1-how-nvda-events-work)

Related: `docs/COMPUTER_USE_REDESIGN.md` (the findings and the proposal),
`docs/protocol/PROTOCOL.md` (wire format), `docs/BRIDGE_API_REFERENCE.md`.

Reproduce the measurements with `python scripts/browser_event_probe.py`
(requires developer mode, opens and closes its own window).

---

## 1. How NVDA events work

### 1.1 The pipeline, with the function names verified in this build

```
platform                       NVDA core                          consumers
─────────────────────────────────────────────────────────────────────────────
SetWinEventHook (MSAA/IA2)  →  internalWinEventHandler.pumpAll
UIAHandler global/local        →  orderedWinEventLimiter.flushEvents
   event handlers                 ↓  dedupe + rate limit + ordering
                                  eventHandler.shouldAcceptEvent   ← filter 1
                                  winEventToNVDAEvent              ← MSAA id → name
                                  eventHandler.queueEvent
                                     ↓  queueHandler.eventQueue (main thread)
                                  eventHandler.executeEvent
                                     ├─ screen-lock check
                                     ├─ sleepMode check             ← filter 2
                                     ├─ doPreGainFocus (gainFocus only)
                                     └─ _EventExecuter.gen          ← fan-out
                                          global plugins  →  appModule
                                          →  treeInterceptor  →  NVDAObject
                                          →  speech / braille
```

Four properties matter for a tool surface:

* **`gainFocus` is special.** `doPreGainFocus` sets the focus object *before* the
  handler chain runs, and fires `foreground` plus `focusEntered` for each new
  ancestor on the way. That is why a plugin's `event_gainFocus` already sees the
  new object, and why one focus move produces several events.
* **The fan-out order is fixed** (global plugins → app module → tree interceptor
  → the object). A global plugin sees every event of that name, before any
  app-specific handling, and must call `nextHandler()`.
* **The stream is lossy by design.** Upstream dedupes duplicate IAccessible focus
  events (`isDuplicateIAccessibleEvent`), rate-limits and reorders win events
  (`orderedWinEventLimiter`), and `shouldAcceptEvent` drops `hide` always and
  `show` except for four window classes. It is a *change signal*, not an audit
  log.
* **`sleepMode` objects produce no events at all.** A tool that "waits for an
  event" from a sleeping object waits forever.

### 1.2 Confirmed APIs

Read live from the running process, not from documentation:

| API | Confirmed signature / value | Notes |
| --- | --- | --- |
| `eventHandler.requestEvents` | `(eventName=None, processId=None, windowClassName=None)` | Opt-in per (event, process, class). Not a per-object subscription, and it cannot add an event type NVDA does not already hook. |
| `eventHandler.shouldAcceptEvent` | `(eventName, windowHandle=None)` | The filter above. |
| `eventHandler.queueEvent` | `(eventName, obj, **kwargs)` | Bare names: `gainFocus`, not `event_gainFocus`. |
| `eventHandler.isPendingEvents` | `(eventName=None, obj=None)` | Useful to distinguish "not yet processed" from "never happened". |
| `eventHandler._acceptEvents` | `set()` — empty | No add-on on this machine has requested extra events. |
| Global plugin hooks | `event_<name>(self, obj, nextHandler)` | Confirmed by the dispatch chain and by our own recorder. |
| MSAA/IA2 → NVDA event map | 26 entries | See below. |
| Event names implemented by `NVDAObject` subclasses | 36, across 201 classes | Caret, selection, text change, typed characters, live regions, UIA notifications. |
| `NVDAObject.doAction` | `(index=None)` | Default action of the object. |
| `NVDAObject.setFocus`, `scrollIntoView` | `()` | Object-level navigation, no keystrokes. |
| `NVDAObject.getActionName`, `actionCount`, `defaultActionIndex` | — | Lets a tool name the action it is about to take. |
| Text access | `makeTextInfo(POSITION_ALL/CARET/SELECTION/FIRST/LAST)` | 86,134 characters read from the focused browser document. |
| Pointer | `getDesktopObject().objectFromPoint(x, y)` | Hit-testing without a mouse click. |

MSAA/IA2 event IDs mapped by this build:

```
2 alert        3 gainFocus    4 menuStart    5 menuEnd      6 menuStart
7 menuEnd     18 scrollingStart 21 switchEnd 32 desktopSwitch
261 documentLoadComplete        272 IA2AttributeChange        273 pageChange
283 caret     32769 destroy    32770 show     32771 hide     32773 gainFocus
32774 selection 32775 selectionAdd 32776 selectionRemove
32777 selectionWithIn           32778 stateChange 32779 locationChange
32780 nameChange                32781 descriptionChange
32782 valueChange               32793 liveRegionChange
```

Additionally, these arrived on the browser UIA path and are not in the MSAA map:
`focusEntered`, `focusExited`, `loseFocus`, `foreground`,
`becomeNavigatorObject`, `typedCharacter`, `UIA_notification`,
`UIA_window_windowOpen`, `UIA_elementSelected`, `UIA_layoutInvalidated`,
`UIA_systemAlert`, `UIA_toolTipOpened`, `textChange`, `textInsert`,
`textRemove`, `controllerForChange`, `suggestionsOpened`,
`suggestionsClosed`.

### 1.3 What does not exist

* **No firehose.** There is no "give me every accessibility event" subscription.
  `requestEvents` only un-blocks events NVDA already hooks, per process and
  window class. Anything NVDA does not hook (most `show`/`hide`, many UIA
  property changes, text changes on elements without a local handler group) is
  invisible to an add-on, and no API changes that.
* **No per-object subscription.** Granularity is (event name, process, window
  class), so "tell me when *this* button changes" is not expressible.
* **No payload.** `valueChange` says a value changed; it does not say to what.
  `stateChange` says a state changed; not which. Every consumer must re-read.
* **No event for ordinary content change.** A page that rewrote a plain `div`
  every 25 ms produced **zero** events over 3 s; the only events were 9
  `liveRegionChange` from a separate `aria-live` region updated 2.5 times/s.
  Content changes are visible to an agent only through `liveRegionChange`, a
  focus/name/state change, the focused element's own text, or an explicit read.

That last point is the whole argument for the architecture below: the events are
*edge triggers for a re-read*, so the useful abstraction is a coalesced
observation, not an event stream.

---

## 2. What to redesign, and what not to

Redesign five things:

| Axis | Now | Target |
| --- | --- | --- |
| Observation | one call per question (`get_current_focus`, `get_focus_lineage`, `read_current_line`) | one `observe()` returning focus, ancestors, value, selection, dialog, and a version |
| Action | keystrokes only, fire-and-forget | object actions (`invoke`, `set_value`, `focus`, `scroll`) plus keystrokes as last resort, all settled |
| Reference | none; callers re-describe objects by name | versioned `ref` handles, stale ones rejected |
| Permission | `send_keyboard` blocked in modal/menu | narrow `dialog_control` permission with an escape hatch |
| Delivery | poll after acting, hope | settle inline with the action; `wait_for_events` for idle time |

Do not redesign: the transport (localhost HTTP, bounded threads, condition-variable
waits), the auth model, the error taxonomy, or the typed-tool pipeline. Those are
measured and correct; the last two rounds of work fixed them.

---

## 3. Architecture

### 3.1 Layers

```
L0  platform     NVDA event dispatch + NVDAObject API          (given)
L1  event hub    recorder → normaliser → coalescer
                 ├─ transitions journal  (event_seq, bounded)
                 └─ dirty flag + revision counter
L2  observation  project() on the main thread: focus, ancestors,
                 value, states, selection, dialog, window title
                 → state_version = hash(projection), bumped only on change
L3  references   locator + weakref fast path, validated against state_version
L4  actions      resolve → execute on main thread → settle → return observation
L5  surface      MCP tools + nvda://ui/state + wait_for_events
```

### 3.2 Versioning: three counters, not one

This is the correction the browser test forced. The current `EventJournal` bumps
one counter per event, and 5 characters typed into a text field produced 18
`caret` events, 5 `valueChange` and 6 `nameChange` — 29 version bumps for one
visible change. If a `ref` is valid "only for the producing version", every ref
would be stale before the next call.

* `event_seq` — one per dispatched event. Debugging only.
* `revision` — bumped when a burst of events goes quiet (40 ms debounce). Means
  "something happened".
* `state_version` — bumped only when the *projection* differs from the last one.
  This is what `ref`s bind to and what callers compare.

`project()` is not free, and the cheap number is a lie. Reading a warm cache
costs 0.01 ms, but the cache is wrong: a checkbox that had just been toggled
still reported its old `states`, because NVDA caches properties per object and
nothing invalidates them behind its back. Invalidating the focus and its
ancestors before reading gives the true observation at **median 13.9 ms, p90
17.0 ms** in Edge. So:

* projection runs **on demand** (per `observe`, per settle round), never per
  event;
* it always invalidates first, or `changed` is systematically false for exactly
  the actions that worked;
* the settle budget must account for 2–3 projections, not one.

Projection must never run on the HTTP thread (NVDA's API is main-thread only),
which is why `revision` exists as the thread-safe signal to wait on.

### 3.3 Settle: stability, not a fixed quiet window

The proposal in `COMPUTER_USE_REDESIGN.md` §3.4 used a 60 ms quiet window. The
browser says that is too short, for a structural reason:

| Measurement (Edge, typing into a text field) | Value |
| --- | --- |
| `stateChange` relative to `valueChange` | +146 ms (one character), i.e. after the value already changed |
| Focus settle after one Tab | 117–182 ms, median 140 ms |
| Actions whose burst spans more than 100 ms | 3 of 5 |

A quiet window shorter than the slowest participant returns a half-updated
observation: the value changed, the state has not. So settle should be defined on
the observation itself:

```
settle(action, budget_ms=1000):
    baseline = project(); target = state_version
    act()
    wait for revision to advance, up to budget
    loop:
        stable_since = now
        while now - stable_since < 60:      # debounce on revision only
            wait for next revision, up to budget
        current = project()
        if current != baseline: return current, changed=True
        if now - start > budget: return baseline, changed=False
```

Two properties worth stating: `changed: false` is a real, reportable outcome (the
action did nothing, which an agent must know rather than infer), and the budget
is per action, not per event.

Budget arithmetic from the measurements: a focus change settles in ~140 ms and one
correct projection costs ~14 ms, so a settle round should be event-driven with a
25 ms poll floor and two or three projections — roughly 180–250 ms per action,
which is one authored decision, not a stall. Polling faster is wasted effort: the
observation cannot change faster than the application emits work.

### 3.4 References

`ref` is a locator, not a pointer, and **resolving one must not walk the tree.**
Walking to the focused control in a 15-node page took 130 ms — about 9 ms per
node, because every `children` call crosses into the browser process. Scaling that
to a 500-item page did not just get slow: the walk ran past the main-thread
executor's 10 s budget, returned `504`, and froze NVDA's UI for those seconds. A
ref resolver that walks is not a slow component, it is an outage.

So:

* an `observe` captures the focused node, its ancestors, and a *bounded*
  neighbourhood (cap ~40 nodes), assigning refs to what it saw and keeping both
  a strong reference and its identity fields;
* resolution uses that reference first, re-reads `role`/`name`/`windowHandle` to
  confirm identity, and re-invalidates before use;
* if the object is gone, the answer is `4206 STALE_REFERENCE` with a fresh
  observation — never a re-scan, and never re-resolution by name.

Identity checks have no shortcut: `isAlive` and `isDefunct` do not exist on these
objects, and `isCurrent` was `False` for the focused, valid text field, so it
cannot be used as a validity predicate. The predicate is "the identity fields
still read back, and the read does not raise".

Positive evidence for this design: the same DOM node kept **one** Python object
across 12 samples over 1.8 s, so the retained-reference path will usually hit.

### 3.5 Event kinds to record

Eighteen, chosen because they carry the transitions an agent needs:

```
gainFocus loseFocus foreground focusEntered focusExited
nameChange valueChange stateChange caret selection
liveRegionChange alert menuStart menuEnd documentLoadComplete
typedCharacter UIA_notification
```

`show`/`hide` are excluded because `shouldAcceptEvent` drops them except for
notification bars, tooltips and IME candidates — as a UI-appeared signal they are
a trap. `becomeNavigatorObject` is excluded because NVDA fires it on every focus
change as the navigator follows focus: it carries no information the focus event
did not already carry. `UIA_window_windowOpen` is excluded for the same reason as
`show` — window appearance is better derived from `foreground` plus `focusEntered`.
`UIA_notification` is kept despite its obscurity: it carries `displayString`,
which is the closest thing to "what NVDA announced" without capturing speech.

---

## 4. Tool surface

The answer to "should we drop the existing tools" is **no, but three of them
change contract and two are absorbed**. Deleting working diagnostics would throw
away the debugging surface that produced this document.

| Tool | Verdict | Why |
| --- | --- | --- |
| `health_snapshot`, `get_logs`, `list_loaded_addons`, `inspect_app_module` | keep | Cheap, correct, no overlap with the agent loop. |
| `inspect_object`, `walk_object_tree`, `get_focus_lineage`, `get_desktop_object` | keep | Debugging depth; `inspect_object` also backs `ref`. |
| `get_window_title` | keep, promote | Browser title changes produced no `nameChange`, so this is the only reliable window label. |
| `get_current_focus` | absorb into `observe` | One observation replaces three calls. Keep as a thin alias during migration. |
| `get_navigator_object` | retire | Navigator is an NVDA-internal concept; no agent task needs it. |
| `read_current_line` | replace by `read(ref, scope)` | Scope is a parameter (line, sentence, selection, document, subtree). |
| `move_focus` | replace by `focus(ref)` and `navigate(direction)` | Current version walks siblings only and returns 500 on an only child. |
| `send_keyboard` | rename to `press`, add `settle` | Same primitive; the race is the defect, not the keystroke. |
| `speak_text`, `show_message`, `reload_plugins`, `restart_nvda`, `execute_python` | keep | Development surface; `restart_nvda` keeps its confirmation. |

New in this plan:

| Tool | Signature (essentials) | Settle |
| --- | --- | --- |
| `observe` | `(include: ["focus","ancestors","value","selection","dialog"], max_text: 200)` | returns `state_version` |
| `invoke` | `(ref, action: null \| name)` | yes |
| `set_value` | `(ref, value)` — platform `SetValue` when the object exposes it, keyboard otherwise | yes |
| `focus` | `(ref)` | yes |
| `press` | `(keys, repeat=1, delay_ms=0, expect=null)` | yes, default `quiet` |
| `read` | `(ref = null, scope="line" \| "selection" \| "document" \| "subtree", max_chars=4000)` | no |
| `navigate` | `(direction="next" \| "previous" \| "first" \| "last" \| "parent" \| "child", ref = null)` | yes |
| `scroll` | `(ref = null, direction, amount="page" \| "line")` | yes |
| `dismiss` | `()` — Escape, then the dialog's default button | yes |
| `click_at` | `(x, y, button="left")` — last resort when nothing is actionable | yes |
| `wait_for_events` | `(since, timeout_ms, kinds=[])` | — |

`dismiss` requires the new `dialog_control` permission and is allowed in
`("normal", "modal", "menu")` states. It exists because the current rule lets an
agent enter a modal and then forbids every tool from leaving it — a safety rule
that only traps.

`set_value` is deliberately written as "platform when available, keyboard
otherwise" because the probe found **no write path** on a Chromium text field:
`value` has no setter, the object exposes neither `UIAElement` nor
`IAccessibleEditableText` (`QueryInterface` returns "No such interface
supported"), and `TextInfo` offers only `setEndPoint`/`updateSelection`. UIA
applications do expose `ValuePattern`, so the platform path is worth having — but
on the web the honest implementation is focus, select-all, type, and the tool
must say so rather than pretend it set a value atomically.

### 4.1 The result envelope

Every tool returns the same shape, and it is the same for reads and actions. Without
this a caller cannot tell "the call ran" from "the UI did what I meant", which is
exactly how `send_keyboard` came to report `completed: true` while the focus read
was still stale.

| Field | Where | Meaning |
| --- | --- | --- |
| `ok` | all | the call ran; says nothing about the UI |
| `state_version` | all | the version this result is valid for, so staleness is detectable without another call |
| `changed` | actions | whether the projection actually differed — measured, not assumed |
| `delta` | actions | *what* differed, as a compact diff (§3.2) |
| `settled_ms` | actions | how long until the observation was stable |
| `uncertain` | actions | delivery could not be confirmed; this is the field that must abort a replay |
| `completed` | actions | reserved for "the stated `expect` was confirmed" — never for "input was posted" |

`send_keyboard`'s current `completed` means *posted* and must be renamed
`dispatched`. Dispatch facts (`sent`, `sent_count`, `gesture`) stay as they are.

Actions also accept an optional `expect`, e.g. `expect={"focus.name": "Search"}`,
returning `completed: true|false` plus the delta either way. Two consumers want
the same thing: a replay engine verifying step 3, and a test asserting an add-on
behaves. Without it both have to re-derive intent from the diff after the fact.

The delta is deliberately not the observation. A full browser projection is
vertices of ancestors plus states plus text — easily a thousand tokens on every
keystroke. A delta is typically one to three fields, and the projection it is
diffed against already runs for settle at 13.9 ms median, so it is nearly free.

Adding these fields is an `api_level: 2` change on both the add-on and the
companion: v1 output schemas are closed (`additionalProperties: false`). P1 must
therefore produce the projection and the diff internally even though nothing on
the wire carries them yet — the envelope is then a serialization, not a refactor.

**As implemented**, the block is `result` on the *response envelope*, beside
`data`, not inside it. Twenty-odd output dataclasses would otherwise each have to
declare the same seven fields, every published `outputSchema` would change for
fields that mean the same thing everywhere, and reads would still have nothing to
put in `changed`. `data` keeps the tool's own typed payload; `result` is the
uniform part. `completed` is present only when the caller sent `expect`, and
`state_version` is always present so staleness is detectable without a follow-up
call.

---

## 5. Phases

Each phase is independently shippable and has a pass/fail check that can be run
against a live NVDA.

### P1 — Observation core (no new tools, no contract change)

* `server/state.py`: normaliser, coalescer, projection, diff, `revision` and
  `state_version`. The diff is the payload the §4.1 envelope will carry later, so
  it is built here rather than retrofitted.
* Recorder wired to the 16 kinds; journal stores *transitions*, not raw events.
* `GET /state` serves the projection; `GET /events` keeps raw events behind a
  debug flag for `get_events`.

Pass: typing `abc` in Edge produces at most 2 transitions and 1 `state_version`
bump; an idle minute produces zero bumps; a focus move yields a delta naming the
old and new name; projection stays under 20 ms cold, 1 ms warm.

### P2 — Actions, refs, settle

* `ref` locators, `observe`, `invoke`, `set_value`, `focus`, `press`, `read`,
  `navigate`, `scroll`, `dismiss`, `click_at`.
* `dialog_control` permission and the modal state exemption.
* `settle` contract as §3.3, returned inline.

Pass: scripted navigation of NVDA's own Settings dialog completes without a human
(adding an add-on, changing an option, confirming, closing); a modal can be
dismissed; `set_value` is verified by reading the value back; a browser button is
invoked by `ref` with `changed: true` and the resulting `liveRegionChange` text
visible in the observation.

### P3 — Delivery

* `wait_for_events(since, timeout_ms, kinds)`.
* Single receiver task + broadcast for `listen`, replacing one poll loop per
  subscription.
* Idle timeout so a dead client cannot hold a loop.

Pass: the existing `scripts/mcp_subscription_spike.py` still passes; two
simultaneous clients both receive updates; a killed client is reaped within the
timeout.

### P4 — Deprecation and protocol 2

* Mark superseded tools deprecated in their descriptions (a `deprecated` field
  needs `api_level: 2`, since v1 tool schemas are closed).
* Publish `outputSchema` and `schema_hash`; companion re-fetches `/tools` on
  change.

Pass: `bridge_acceptance.py` green at api level 2 with both levels advertised.

---

### 5.1 What the implementation actually does, and what was observed

Every phase shipped. The deviations from the plan above are listed with the
reason, because a plan that quietly changes is worse than one that is updated.

| Phase | Landed as | Deviation and why |
| --- | --- | --- |
| P1 | `server/events.py` (transitions + raw debug journal, 40 ms coalescer), `server/state.py` (projection, diff, three counters), `nvda_bridge/observation.py`, `/state` from the projector, 18 event hooks | Ancestor depth raised 5 → 8: a dialog's chain runs through its own window to the application frame (Notepad's Replace box is at depth 4, its frame at depth 8), so a 5-deep chain cannot tell a dialog from the main window. |
| P2 | `refs.py`, `settle.py`, `nvda_bridge/actions.py`, `win_input.py`, 11 tools, `dialog_control` | `get_navigator_object` is deprecated rather than retired, and `move_focus`/`read_current_line`/`get_current_focus`/`send_keyboard` are kept as deprecated aliases: deleting working diagnostics would throw away the surface that produced this document. Dialog detection is structural (a named window nested in another window), because the dialog itself reports `WINDOW`, not `DIALOG`. |
| P3 | `wait_for_events`, one bridge event pump + `tokio::sync::broadcast` in the companion, 10 s sink timeout | — |
| P4 | `api_level: 2` with `supported_api_levels: [1, 2]`, `result` block, `outputSchema`, `schema_hash`, `deprecated`/`supersededBy`, companion catalog refresh | The companion cannot send `tools/list_changed` (its catalog capability is static), so it swaps the catalog under a lock and the next `tools/list` sees it. |

Live observations from `scripts/live_acceptance.py` (44/44 checks, 1
environmental skip) and `scripts/bridge_acceptance.py` (69/70 behavioural,
0/16 type leaks, 8/8 security, 16/16 concurrency):

| Check | Observed |
| --- | --- |
| `observe`, live, Firefox/Edge/Notepad | 19-75 ms, one call for focus + ancestors + dialog + window |
| `set_value` into Notepad | platform `ValuePattern` (not keystrokes); read-back verified |
| `abc` typed in one action | **1** transition, `state_version` +1 |
| Idle 2 s | 0 transitions, 0 version bumps |
| Focus move (`alt+tab`, `tab`) | `changed: true`, `settled_ms` 198-362, delta names old and new |
| No-op keypress (`shift`) | `changed: false`, `settled_ms` ~800, `dispatched: true` |
| Notepad Replace dialog | detected (`WINDOW "Pop-up"`), `invoke` on its Cancel closes it, `dismiss` reports the steps |
| Browser button by `ref` | `invoke` → `changed: true`, delta `name: "Run action" → "Run action 1"`; a `liveRegion` transition appeared in Chromium |
| `click_at` | pointer read back within 2 px of the request; reports the object under the point |
| Stale `ref` | `4206`, message points the caller at `observe` |
| Unknown tool surface | 29 tools advertised, strict input schemas, no type leaks |

Still true and worth keeping in view: a settle that observes no change costs
about 800 ms (two rounds of the 250 ms first-activity wait), which is the price
of not returning a half-updated answer; and the first `press` after a restart is
slower than later ones while NVDA warms its caches.

---

## 6. Risks and non-goals

* **Main-thread cost.** Every projection and every action runs on NVDA's main
  thread. `execute_python` demonstrated the failure mode: a `time.sleep` inside it
  blocks the event queue, so queued `gainFocus` events are never processed and
  focus appears frozen. Nothing in L1–L4 may sleep on that thread; waits belong on
  the HTTP worker via a condition variable.
* **Property reads can block** on a hung application. Projection must read cached
  properties only, and must be budgeted; NVDA's watchdog will otherwise cancel it
  mid-read.
* **Not a firehose.** If a future requirement is "every DOM mutation", the answer
  is not this event path — it is polling `textChange` on a local handler group, or
  reading the document, and it will not be portable across apps.
* **No SSE/WebSocket.** The bridge is a bounded-thread server and the MCP boundary
  already has one SDK-managed long-lived stream.
* **Speech capture is out of scope** for now. If announcements matter, start from
  `UIA_notification.displayString`, which arrives with the event and needs no
  speech interception.
* **Errors that cannot be acted on — corrected.** Probing first suggested that a
  `403` carried no usable reason. It does: the body is
  `{"code": 4003, "name": "PERMISSION_DENIED", "message": "Action 'execute_python'
  requires permission 'developer' which has not been granted"}` — the elevation
  had simply expired (300 s TTL). The mistake was in the client, which turned
  every `URLError` into "bridge unreachable" and then guessed. Two lessons worth
  keeping: clients must surface `error.code` rather than the HTTP status, and
  `retry_policy: "never"` is the wrong answer for a permission that the user can
  re-grant — that should say "re-request elevation", not "give up".

## 7. Evidence index

Every design decision above traces to a measurement. Reproduce with
`scripts/browser_event_probe.py` (events, settle) and
`scripts/action_surface_probe.py` (actions, refs, projection).

| Measurement | Value | Decision it drives |
| --- | --- | --- |
| 5 typed characters in Notepad | 29 events, 1 visible change | journal records transitions, not events |
| 3 typed characters in Edge | 16 events, 5 kinds, 1 change | coalesce to a state; `state_version` is not the event counter |
| `valueChange` → `stateChange`, same character | +146 ms | a 60 ms quiet window is wrong; settle on the observation |
| Focus settle after one Tab, Edge | 117–182 ms, median 140 | settle budget 1 s, poll floor 25 ms |
| Immediate read after a keystroke that was stale | 3 of 5 | settle must ride with the action |
| Plain `div` rewritten 40×/s for 3 s | 0 events | content change is not observable; `read` exists for it |
| `aria-live` region at 2.5/s | 9 `liveRegionChange` in 3 s | keep `liveRegionChange`; it is the only dynamic-content signal |
| Tree walk to the focused control | 130 ms / 15 nodes (~9 ms/node) | refs are captured, not resolved by scanning |
| Walk to the 450th of 500 links | exceeded the 10 s executor budget, `504`, NVDA frozen | a walking resolver is an outage, not a slow path |
| Same DOM node, 12 samples over 1.8 s | 1 Python object | retained-reference resolution will usually hit |
| `doAction()` on an unfocused button | page reacted, 0.2 ms | `invoke` is viable and does not need focus |
| Checkbox toggled, cached `states` unchanged | stale | projection must invalidate before reading |
| Projection, cached vs invalidated | 0.01 ms vs 13.9 ms median | projection on demand, 2–3 per settle |
| `value` setter / IA2 editable text / UIA element | absent / absent / absent | `set_value` is keyboard-backed on the web |
| `isAlive`, `isDefunct`, `isCurrent` | missing, missing, `False` on a valid object | validate refs by identity re-read, not a flag |

Still unmeasured, and worth checking before the phases that depend on them:

* **The bounded-walk number and the UIA value path** — both blocked when the 300 s
  developer elevation expired mid-probe. `scripts/large_tree_probe.py` is written
  and one re-grant runs it.
* **Heavy applications** — Word, VS Code, a 1000-node web app. The 130 ms walk
  and 14 ms projection are small-page numbers; both could be an order of
  magnitude worse, which would change the ref cap and the settle budget.
* **UIA-only applications**, where `ValuePattern` and UIA text patterns exist and
  `set_value` may be a real assignment rather than a keystroke impersonation.
* **Long sessions** — whether repeated invalidation leaks or slows down; the
  probe ran for seconds, not hours.
* **Non-Chromium browsers** (Firefox/Gecko) and **terminal emulators**, which
  carry their own event quirks and are common computer-use targets.

## 8. Deferred: reusing a solved task

A recorded task cannot be replayed as a list of calls: `ref`s are version-bound by
design, element indices shift, and keystrokes encode layout rather than intent.
The replayable artifact is a **step with a selector and a postcondition** —
"invoke the button named Run action inside the document, expect the live region to
change" — and that is exactly what `expect` (§4.1) and selector-based refs (§3.4)
exist to make expressible.

Two consequences for the phases above, and no phase of its own yet:

* **Record the intent, not the keystroke.** A recorded Tab that moved focus to the
  search field generalizes to `focus(selector=…)`, not to `press("tab")`. Keyboard
  steps are how the model discovers intent, and are tagged least-replayable
  because Tab order depends on layout, locale and visible controls.
* **A cache key needs a fingerprint**, not a process: app name, window class, URL
  host, and a structural digest of the reachable nodes; anything else marks the
  entry stale rather than replaying it silently.

Ordering note: this is worth building only after P2, because a recorder with
nothing to select by records keystrokes, and keystrokes are the one thing that
does not replay.
