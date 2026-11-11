# Computer-use and add-on development: tool surface redesign

Status: proposal. Written after a live exercise of every advertised tool against
NVDA 2026.3.1 with `nvdaMcpBridge` 1.0.0 on Windows 11.

Related: `docs/BRIDGE_API_REFERENCE.md` (current contract),
`docs/protocol/PROTOCOL.md` (wire format), `docs/TOOLS_AUDIT.md` (code quality,
a different axis from this document).

Reproduce the measurements below with:

```
python scripts/bridge_acceptance.py --verbose
```

---

## 1. What was exercised

18 tools are advertised on `/tools`; 19 are registered, with `execute_python`
withheld until developer mode is granted. Each tool was called with valid
arguments, with each optional argument varied, and with invalid or wrong-typed
arguments. The wire envelope, the `data` payload against `schema.json`, the error
taxonomy, auth, Host/Origin pinning, body limits, request-id handling and
concurrency were checked separately.

Result at the time of writing:

| Area | Result |
| --- | --- |
| Behavioural cases | 47/47 as expected |
| Success payloads matching declared schema | 18/18 tools |
| Wrong-typed input rejected | 8/16 — **8 leaks** |
| Security checks | 7/8 — request id not echoed |
| Concurrency (16 parallel reads) | 16/16, median 15–40 ms |
| Slowest tools | `health_snapshot` 84–227 ms, `send_keyboard` 110–195 ms |

Latency is fine. The read paths are fast, cheap and correctly validated. The
problems are all in *how state is observed* and *how actions are expressed* —
which is exactly the layer a computer-use agent depends on.

---

## 2. Findings

Ordered by how much they block computer use. Severity is judged against a
plausible agent loop: *observe → decide → act → observe again*.

### 2.1 Actions are not observable: focus reads race the dispatch (blocking)

`send_keyboard` returns as soon as NVDA has *posted* the input, not when the
target application has processed it. An immediate `get_current_focus` therefore
describes the **previous** focus:

```
tab1: immediate=(8, 'Message or run a task…')  settled=(102, 'Add files or run commands')
tab2: immediate=(102, 'Add files or run commands') settled=(9, 'Access mode…')
...
raced reads: 8/8  (settles by 50 ms)
```

Eight out of eight. The response also carries no indication that anything
changed:

```
send_keyboard result keys: cancelled, completed, delay_ms, gesture, repeat, sent, sent_count, uncertain
```

So an agent that does the natural thing — press Tab, read focus, act on it — acts
on the wrong element *every time*, and has no way to detect it. This is the
single largest defect for computer use, and it is also why the earlier attempt to
drive NVDA's own Settings dialog went blind.

### 2.2 There is no action vocabulary beyond keystrokes (blocking)

The only mutating primitives are `send_keyboard`, `move_focus`, `speak_text`,
`show_message`, `reload_plugins`, `restart_nvda`. A grep for
`doAction|leftClick|mouse|SetFocus|scroll|clipboard|screenshot` across the add-on
returns only `focus.py`'s two `setFocus()` calls.

Consequently an agent cannot express: activate the focused element, set a text
field to a value, type a string, switch to a named window, scroll, read the rest
of a document, dismiss a dialog. Every one of those currently has to be improvised
as a guess at a keystroke sequence, which is fragile across apps and locales and
is the reason screen-reader automation historically stays app-specific.

`move_focus` does not help: it walks **siblings** of the focused object and
reports `RuntimeError: No next object available` whenever focus is on an only
child — the common case in a browser. It also surfaces as HTTP 500, so a caller
cannot distinguish "no sibling" from "bridge crashed".

### 2.3 Any modal or menu is a dead end (blocking)

`send_keyboard` declares `allowed_nvda_states = ("normal",)`, and
`check_action_permissions` rejects it with 4202/4203 while a modal message box or
the NVDA menu is open (`server/security.py:585-599`). No other tool can send
input. So once a dialog appears, **no bridge call can dismiss it** — not even
Escape. The only recovery is a human.

This is the mechanism behind the earlier failed navigation of NVDA Settings: the
bridge could read a little and then had no legal move. A safety rule that
prevents the agent from getting *out* of a state it can get *into* is not a safety
rule.

### 2.4 Input validation does not validate types (high)

`InputSchema.validate` falls back to `self.model(**data)` when pydantic is absent
(`core/schema.py:185`), and pydantic is absent inside NVDA. A plain dataclass
constructor performs no type checking, so the "No implicit coercion" rule in the
module docstring holds only for tools whose handler happens to validate by hand.

Eight of sixteen wrong-typed inputs were accepted:

| Tool | Input | Accepted as |
| --- | --- | --- |
| `inspect_object` | `include_relations: "false"` | truthy → relations **included** |
| `health_snapshot` | `include_logs: "no"` | truthy → logs **included** |
| `get_logs` | `use_tail: "false"` | truthy → tail |
| `inspect_object` | `include_relations: 1` | `True` |
| `walk_object_tree` | `max_depth: true` | `1` |
| `send_keyboard` | `repeat: "2"`, `delay_ms: "10"` | ints |
| `send_keyboard` | `gesture: 5` | `"5"` |

The `"false"` → `True` cases are the dangerous ones: the caller's stated intent is
inverted silently. Rejection is also inconsistent — `get_logs` and
`walk_object_tree` reject bad types with 500, `restart_nvda` accepted
`disable_addons: "yes"`, and during this exercise that string restarted NVDA with
**all add-ons disabled**. That is the whole chain: no type check → truthy string
→ `core.restart(disableAddons="yes")` → `--disable-addons`.

Worth stating plainly: the permission system behaved correctly here
(`nvda_lifecycle` had been granted), and the tool was documented as destructive.
The defect is that a string satisfied a boolean.

### 2.5 `send_keyboard` bypasses the validation pipeline (high)

`execute_action` special-cases the one tool that synthesises input
(`server/http_bridge.py:866-936`) and calls `nvda_bridge.ui.send_keyboard`
directly, with ad-hoc `int()` / `str()` coercion of parameters. Every other tool
goes through `ToolExecutor.execute_and_serialize` — input schema, handler, output
schema.

Consequences:

* the most dangerous tool is the only one that skips input schema validation;
* its output schema is not checked, which is why `completed`/`uncertain` could
  drift out of `models.py` and survive unit tests;
* validation errors look different (400/4103 here, 500/5303 everywhere else);
* cancellation is implemented for exactly one tool, and only by accident of this
  branch.

### 2.6 Error taxonomy is inconsistent (medium)

| Observation | Expected |
| --- | --- |
| unknown action → 500 / `5003` | `4102` / 400 |
| `max_depth` out of range → 500 / `5003` | `4103` / 400 |
| `max_lines: 0` → 500 / `5003` | `4103` / 400 |
| `inspect_app_module(source="desktop")` → 500 / `5003 processHandle is 0` | domain error, not "internal" |
| `PUT /action` → 501 + HTML error page | 405 + JSON envelope |

The `if not tool:` branch at `http_bridge.py:828` that would answer `4102` is
dead code: `registry.get()` raises `RegistryError` (`core/registry.py:137`)
instead of returning `None`, and the exception is caught further out and
classified as an execution failure.

Parameter errors raised inside handlers are indistinguishable from genuine
runtime faults, so a client cannot know whether to correct its arguments or retry.

### 2.7 Smaller defects

* **Client `request_id` is discarded.** The bridge always generates its own, so
  callers cannot correlate their call with the audit log, and
  `MAX_REQUEST_ID_LENGTH = 128` is unenforced.
* **`inspect_app_module(source="desktop")` always fails** with `processHandle is
  0` — the desktop root has no process, and the tool does not special-case it.
* **`pythonClass` is `null` for Chromium UIA objects** (it was populated under
  IA2), and `roleText` is empty for roles 9/102. Object identity is weaker than
  it looks.
* **`/tools` does not publish `outputSchema`**, only the name `outputType`. A
  client must hardcode shapes or fetch `schema.json` out of band. This is why the
  `completed`/`uncertain` drift failed at runtime rather than at discovery.
* **The companion caches `/tools` for its lifetime**, so an add-on upgrade that
  changes schemas requires killing `nvda-mcp-server.exe`. Nothing in `/health`
  signals staleness.
* **Oversized bodies abort the connection** (413 with no JSON envelope).
* **Gesture modifier names are NVDA-internal and unaliased.** `ctrl+a` fails with
  `4103 Failed to resolve keyboard gesture 'ctrl+a': 'ctrl'`; only `control+a`
  resolves. The parameter is documented as "an NVDA keyboard gesture name", so an
  agent writing the conventional `ctrl+…` is corrected only by a runtime 400 —
  and `ctrl` is far more common in the wild than `control`. Accept aliases, or
  publish the accepted modifier vocabulary in the tool description.
* **`scripts/test_*.py` and `scripts/test_api.ps1` still send the retired default
  token** `nvda-bridge-secret-change-me` and cannot authenticate against a current
  install, so none of them run.

---

## 3. Redesign for computer use

The current surface is *object-oriented*: it exposes raw accessibility objects and
leaves interpretation to the caller. A model calling these tools sees a
300-node Chromium tree with no stable identity, no actionable affordances, and a
one-frame-stale focus.

The redesign inverts this. NVDA already maintains, event by event, exactly the
abstraction a computer-use agent needs: *what changed, what is focused, and what
the user would hear*. Expose that.

### 3.1 Principle: addressed state, synchronised actions

Three invariants make an agent loop safe:

1. **Every observation is a coherent snapshot** carrying a `state_version`.
2. **Every actionable element has a short-lived reference** valid for that
   `state_version`; using a stale reference fails loudly.
3. **Every action returns after the UI has settled**, with a description of what
   changed.

With those, "press Tab then click the focused thing" stops being a race.

### 3.2 Proposed observation tools

```jsonc
// observe() -> Observation
{
  "state_version": 1841,          // monotonic; bumped on every UI event NVDA sees
  "since_last_event_ms": 12,      // lets the caller decide to wait
  "window": {"title": "...", "handle": 657444, "className": "Chrome_WidgetWin_1"},
  "focus": {
    "ref": "n12",                 // addressable for this state_version
    "role": "editable_text", "role_text": "edit",
    "name": "Message or run a task",
    "value": "",
    "states": ["focusable", "editable", "focused"],
    "actions": ["set_value"],     // what invoke() would do, from IAccessible/UIA
    "bounds": {"left": 183, "top": 1364, "width": 1062, "height": 54}
  },
  "breadcrumb": [                 // ancestors, pruned to role+name
    {"role": "document", "name": "Configure NVDA MCP in dsh"},
    {"role": "pane", "name": ""}
  ],
  "dialog": null,                 // populated when a modal/menu is open
  "speech": "Message or run a task, edit, blank"   // what NVDA just said
}
```

`speech` is the key field. It is NVDA's own rendering of the element — the phrase
a blind user hears — and it collapses a large tree into actionable text. It can
be produced by registering a `speech.extensions.filter`, or by tapping
`speech.speech.speak`.

Supporting observers:

| Tool | Purpose | Replaces |
| --- | --- | --- |
| `read_document(max_chars)` | Full text of the focused document/browse buffer, not one line | `read_current_line` |
| `list_windows()` | Enumerate top-level windows with titles and handles | nothing today |
| `get_speech(count)` | Last *n* utterances NVDA produced | nothing today |
| `describe_region(left, top, width, height)` | Accessibility text of a screen region — text, not a bitmap | nothing today |

`describe_region` deliberately returns text rather than a screenshot: in a
headless or remote session a bitmap is often black, and the semantic content is
already reachable through the accessibility tree. A screenshot tool is worth
adding only for debugging add-on rendering, behind the diagnostics permission.

### 3.3 Proposed action tools

| Tool | Semantics | Notes |
| --- | --- | --- |
| `invoke(ref, action=null)` | Perform the element's default accessibility action | The screen-reader equivalent of a click; uses `NVDAObject.doAction` |
| `set_value(ref, text)` | Set a text/editable field's value | `IAccessible2::setValue` / `ValuePattern`; avoids 40 keystrokes |
| `type_text(text)` | Inject text at the current focus | Unicode `SendInput`, not per-character gestures |
| `press(gesture, repeat, delay_ms, settle)` | Existing `send_keyboard` plus a settle contract | Keep: it is the human-equivalent escape hatch |
| `navigate(direction)` | Object navigation: parent, first_child, next, previous, next_in_flow, previous_in_flow, flat_review_line | What NVDA's own object-review keys do; replaces `move_focus` |
| `dismiss(kind="escape")` | Escape / Enter / activate-default on a modal or menu | New permission, see 3.5 |
| `activate_window(title=null, handle=null)` | Bring a window to the foreground | Needed for multi-app work |
| `scroll(direction, amount)` | Scroll the nearest scrollable container | |

`navigate` and `invoke` together cover most of what "click the thing below the
heading" means in practice, without pixels.

### 3.4 The settle contract (fixes §2.1)

`press` and the other actions gain two parameters and one response section:

```jsonc
// press(gesture="tab", settle="focus_change", settle_timeout_ms=1000)
{
  "sent": true, "sent_count": 1, "completed": true,
  "state_version_before": 1841,
  "state_version_after": 1842,
  "changed": true,
  "elapsed_to_settle_ms": 34,
  "observation": { /* the new Observation, as in observe() */ }
}
```

Implementation sketch:

1. Register `eventHandler` hooks for `event_focusChanged`, `event_valueChange`,
   `event_nameChange`, `event_show`, `event_stateChange`, appending to a bounded
   ring buffer with a monotonic sequence number.
2. `state_version` is that sequence number.
3. After dispatch, wait until (a) at least one recorded event post-dates the
   dispatch, **and** (b) no new event has arrived for `quiet_ms` (default 60), or
   the timeout expires.
4. Return the fresh observation inline so the next decision costs one round trip
   instead of two.

Measured behaviour supports this: every raced read had settled by 50 ms, so a
60 ms quiet window would have removed 8/8 races while adding roughly one
frame of latency. Waiting on the event stream is exact, whereas sleeping is a
guess.

`settle` values: `none` (current behaviour, for benchmarking), `focus_change`,
`value_change`, `quiet` (any event, then quiet), `speech` (wait until NVDA speaks
something new).

### 3.5 Escape hatch for modal and menu states (fixes §2.3)

Add a `dialog_control` permission, granted separately from `input_control`, and
give `dismiss` an explicit exemption:

```python
ToolMetadata(
    name="dismiss",
    required_permission="dialog_control",
    allowed_nvda_states=("normal", "modal", "menu"),
    destructive=False,
    ...
)
```

Rationale: the risk being managed is an agent driving the desktop unsupervised.
Being unable to leave a dialog does not reduce that risk; it converts a
recoverable state into a stuck one. The exemption is narrow (Escape, Enter, or the
dialog's default button only), separately permissioned, and audited in the log
like every other permission change.

Also add `list_dialogs()` so the agent can see *what* is blocking before acting,
and include `dialog: {title, buttons[]}` in `observe()`.

### 3.6 Reference lifetime

`ref` values (`n12`) are valid only for the `state_version` that produced them.

* Acting on a stale ref returns `4205 STALE_REFERENCE` with the current
  `state_version` and a fresh observation attached.
* No silent re-resolution by name: in a browser, "the button called OK" is
  ambiguous, and guessing is how automation clicks the wrong thing.

This is the difference between an agent that can verify itself and one that
cannot. It also makes the bridge's own audit log meaningful.

---

## 4. Redesign for add-on development

The current diagnostics answer "what is the state of the object tree". An add-on
author asks different questions: *what events fired, what did NVDA say, did my
handler fight another add-on, and can I iterate without restarting?*

| Proposed tool | Answers | Why current tools cannot |
| --- | --- | --- |
| `get_events(limit, since, filter)` | Which `event_*` fired, on which object, how long each handler took | `get_logs` returns Python logs, not the NVDA event stream. Handler storms, feedback loops and `event_gainFocus` recursion — the most common add-on bugs — are invisible. |
| `get_speech(count)` | What the user actually heard | `speak_text` writes speech; nothing reads it. Speech is the natural assertion surface for an add-on test. |
| `list_gestures(app=null)` | Which scripts are bound, and where two add-ons collide | Nothing today. Gesture conflicts are a whole class of add-on bug and are currently diagnosed by pressing keys and listening. |
| `capture_object_tree(label)` / `diff_captures(a, b)` | What did my add-on change in the tree? | `inspect_object` returns a snapshot with no way to compare two, so "did my overlay appear" is a manual diff. |
| `reload_addon(name)` | Reload one add-on | `reload_plugins` reloads everything. Observed side effect in this session: hot reload makes third-party `taskSwitcher` fail `RegisterClass`, disabling it until a full NVDA restart. |
| `refresh_tools()` | Re-fetch `/tools` after a schema change | The companion caches discovery for its lifetime (see §2.7); today the fix is killing the process. |

### 4.1 Developer ergonomics

`execute_python` is the right idea with the wrong ergonomics. It is gated behind
time-limited developer mode that needs a fresh local approval token
(`server/security.py:428-496`) — appropriate for arbitrary code execution, but it
means an agent cannot even *introspect* without a human present.

Proposal: split the tier.

* **Introspection tier** (`developer_read`): import whitelisted NVDA modules
  (`api`, `eventHandler`, `gui`, `speech`, `config`, `appModuleHandler`) and
  evaluate read-only expressions — no `__import__`, no attribute assignment, no
  filesystem. Requires the normal, persistent permission grant.
* **Execution tier** (`developer`): current behaviour, unchanged. Arbitrary code
  keeps the local-approval requirement.

That preserves the security posture while making the common dev loop —
"why is my focus handler not firing?" — available to an agent without a human
clicking a dialog.

### 4.2 Dev loop

The loop an add-on author actually wants:

```
edit file → bridge reloads that add-on → call tool → read events/speech
```

Two pieces are missing: a single-add-on reload (§4 table) and a **watch mode** —
a filesystem watch on the add-on source directory that reloads the bridge's own
plugin when a file changes. That turns a 20-second restart cycle into a
sub-second one, and it is the difference between the bridge being a test tool and
being a development environment.

### 4.3 Schema handshake

Add `schema_hash` (and `tool_count`) to `GET /health`, and have the companion
re-fetch `/tools` when it changes. Then:

* schema drift becomes a self-healing condition instead of a stale-process bug;
* `refresh_tools` is a manual override for hosts that cannot wait;
* `/tools` should also publish `outputSchema` (the same object already present in
  `schema.json`), so clients validate without out-of-band files.

---

## 5. Contract fixes (independent of the new tools)

These are bug fixes, not design. Recommended order:

1. **Enforce input types.** Make `InputSchema.validate` validate against the same
   JSON Schema that `/tools` advertises, or reject in the dataclass fallback:
   `bool` is not `int`, `str` is not `bool`, no implicit conversion. This closes
   all eight leaks in §2.4 and the `disable_addons: "yes"` restart.
2. **Delete the `send_keyboard` special case** (`http_bridge.py:866-936`). Give
   `ToolMetadata` a `cancellation_supported` hook the executor honours, so one
   pipeline serves all tools and output schemas are always checked.
3. **Fix the taxonomy** (§2.6): unknown action → `4102`; handler parameter
   errors → `4103`; "no sibling"/"no process" → a `42xx` domain code; wrong
   method → 405 JSON. Remove or repair the dead `if not tool:` branch.
4. **Echo `request_id`** (and enforce `MAX_REQUEST_ID_LENGTH`).
5. **Special-case `source="desktop"`** in `inspect_app_module`, or return a
   structured `not_applicable` instead of a 500.
6. **Return a JSON envelope for 413** instead of aborting the connection.
7. **Repair the dev scripts** that hardcode the retired default token, or delete
   them in favour of `scripts/bridge_acceptance.py`.

---

## 6. Rollout

Everything above is additive except §5, which is behaviour-changing.

* Bump `api_level` to `2`; keep `supported_api_levels: [1, 2]` during transition.
  The protocol already carries both fields; nothing needs inventing.
* Keep the existing 18 tools. `observe` supersedes `get_current_focus` +
  `get_navigator_object` + `get_focus_lineage` for agent use, but the granular
  ones remain useful for debugging.
* New permissions: `dialog_control` (narrow, see §3.5), `developer_read` (see
  §4.1). Both persist via the existing `save_persisted_permissions` path; neither
  is `developer`, which must keep requiring fresh local approval.
* The settle contract should ship **on by default** (`settle="quiet"`), because
  the current default is the one that produces wrong answers.

Suggested first increment, smallest change with the largest safety gain:

> Fix input types (§5.1) and add `settle` to `press` (§3.4). Together they remove
> the silent-coercion class of bug and the off-by-one focus read, without adding
> a single new tool.

Second increment: `invoke`, `set_value`, `type_text`, `navigate`.
Third: `observe`, `state_version`, reference lifetimes, `dismiss`.
Fourth: the add-on development surface (§4).

---

## 7. Appendix: measured detail

Per-tool median bridge-reported duration at the time of the run:

| Tool | median (ms) | max (ms) |
| --- | ---: | ---: |
| `send_keyboard` (repeat=3, delay=40) | 110 | 195 |
| `health_snapshot` | 84 | 227 |
| `get_desktop_object` | 38 | 61 |
| `get_focus_lineage` | 33 | 141 |
| `get_logs` | 32 | 62 |
| `read_current_line` | 46 | 46 |
| `walk_object_tree` | 21 | 26 |
| `get_current_focus` | 17 | 17 |
| `get_window_title` | 16 | 16 |

Computer-use loop cost (press Tab, then read focus, two round trips): mean 129 ms
per step, max 197 ms. The settle contract (§3.4) removes the second round trip and
returns the observation with the action, so the same step costs one round trip at
roughly the current single-call latency.
