# NVDA HTTP Bridge API Reference (v1)

This document describes the current bridge behavior implemented in `nvda_http_bridge.py`.

* Bridge protocol: `nvda-bridge-protocol`
* Protocol version: `1.0.0`
* API level: `4` (advertises `supported_api_levels: [2, 3, 4]`)
* Bridge docs maturity: v1 payloads below, plus the level-2, level-3 and
  level-4 additions at the end

The JSON examples in this document are v1 responses. API level 2 adds fields
without changing the v1 shape, so a v1 client keeps working; levels 3 and 4 are
subtractive in places, and every removal is listed at the end with its
replacement.

## Transport and Auth

* Base URL: `http://127.0.0.1:8765`
* Content-Type: `application/json`
* Auth header for protected endpoints: `X-NVDA-Bridge-Token`
* `GET /health` is unauthenticated.

## Endpoints

### `GET /health`

Returns a versioned health payload.

Example response:

```json
{
  "status": "ok",
  "protocol": "nvda-bridge-protocol",
  "protocol_version": "1.0.0",
  "bridge_version": "1.0.0",
  "api_level": 1,
  "supported_api_levels": [1],
  "uptime_seconds": 123,
  "timestamp_utc": "2026-02-21T11:22:33.000000+00:00",
  "nvda_status": {
    "running": true,
    "secure_screen": false,
    "modal_blocking": false,
    "menu_open": false,
    "speech_available": true,
    "braille_available": true
  }
}
```

### `GET /tools`

Returns tool discovery metadata and named output types.

Requires `X-NVDA-Bridge-Token`.

Example response shape:

```json
{
  "protocol": "nvda-bridge-protocol",
  "protocol_version": "1.0.0",
  "bridge_version": "1.0.0",
  "api_level": 1,
  "types": { "ObjectInfo": { "$schema": "http://json-schema.org/draft-07/schema#" } },
  "tools": [
    {
      "name": "get_current_focus",
      "description": "Get information about the currently focused UI element.",
      "inputSchema": { "type": "object", "properties": {}, "additionalProperties": false },
      "outputType": "ObjectInfo",
      "version": "1.0.0",
      "category": "focus"
    }
  ],
  "count": 1
}
```

### `POST /action`

Executes one action/tool.

Requires `X-NVDA-Bridge-Token`.

Request body:

```json
{
  "request_id": "e0dc2bc6-8eb4-4f8f-a95b-6fb953953f77",
  "action": "get_current_focus",
  "params": {}
}
```

Notes:

* `request_id` is required and must be a non-empty string.
* `action` is required and must be a non-empty string.
* `params` is required and must be a JSON object.
* Optional fields such as `timeout_ms`, `client` and `trace_id` are accepted for
  forward compatibility. `trace_id` is the *logical action* identifier; passing
  it is optional and only needed when a client wants its own id to be the one the
  diagnostic record uses. `request_id` is per HTTP request and is a different
  thing.

Success envelope:

```json
{
  "request_id": "e0dc2bc6-8eb4-4f8f-a95b-6fb953953f77",
  "trace_id": "at-9f3c2a1b8e4d5f607182",
  "success": true,
  "action": "get_current_focus",
  "duration_ms": 8.41,
  "bridge": { "bridge_version": "1.0.0", "api_level": 1 },
  "nvda_status_snapshot": {
    "secure_screen": false,
    "modal_blocking": false,
    "menu_open": false
  },
  "result": {
    "ok": true,
    "state_version": 12,
    "changed": false,
    "delta": {},
    "settled_ms": 0,
    "uncertain": false,
    "projection_available": true,
    "events_complete": true,
    "settling_status": "settled"
  },
  "trace": {
    "trace_id": "at-9f3c2a1b8e4d5f607182",
    "outcome": "succeeded",
    "observation_status": "no_detectable_change",
    "expectation_status": "not_provided"
  },
  "data": {}
}
```

`trace_id` and `trace` appear on success and failure envelopes alike; the summary
is bounded (no event list, no log lines) and the full record is fetched with the
`get_action_trace` tool. See [ACTION_TRACE.md](ACTION_TRACE.md).

Error envelope:

```json
{
  "request_id": "e0dc2bc6-8eb4-4f8f-a95b-6fb953953f77",
  "trace_id": "at-9f3c2a1b8e4d5f607182",
  "success": false,
  "action": "move_focus",
  "duration_ms": 1.23,
  "bridge": { "bridge_version": "1.0.0", "api_level": 1 },
  "nvda_status_snapshot": {
    "secure_screen": false,
    "modal_blocking": false,
    "menu_open": true
  },
  "error": {
    "code": 4203,
    "name": "MENU_OPEN_BLOCKING",
    "message": "Action blocked while NVDA menu is open",
    "category": "nvda_state",
    "retry_policy": "after_delay",
    "retry_after_ms": 500
  }
}
```

## Action Names

The bridge exposes discoverable actions via `GET /tools`, 28 of them at
api_level 4. The surface is organised by what a tool answers:

* **reading a screen**: `observe`
* **finding something**: `locate`, `list_reading_targets`
* **acting**: `invoke`, `set_value`, `focus`, `press`, `navigate`,
  `navigate_reading`, `start_browse_mode`, `scroll`, `dismiss`, `click_at`
* **windows**: `activate_window`
* **waiting**: `wait_for_events`
* **the user's own output**: `speak_text`, `show_message`
* **diagnostics**: `inspect_object`, `walk_object_tree`, `get_focus_lineage`,
  `inspect_app_module`, `list_loaded_addons`, `health_snapshot`, `get_logs`,
  `get_action_trace`
* **lifecycle** (each requires an explicit grant): `restart_nvda`,
  `reload_plugins`, `execute_python`

api_level 3 **removed** the v1 aliases rather than deprecating them again:
`get_current_focus`, `get_navigator_object`, `read_current_line`, `move_focus`,
`send_keyboard`, `get_desktop_object` and `get_window_title` are gone. Their
replacements have been in the surface since api level 2 — `observe`,
`observe`, `read`, `navigate`, `press`, `inspect_object(target="desktop")` and
`observe().window` respectively — and a catalog is context an agent pays for on
every turn, so a second name for an existing question is a cost with no answer
attached. A client calling a retired name gets `4102` naming the action, not a
silently different tool.

`GET /tools?category=action` narrows the catalog, and `category` may repeat or
be comma-separated (`?category=action,observation`). A production companion that
sets `NVDA_BRIDGE_CATEGORIES` asks only for what it needs: `action,observation`
is 58 KB of the 101 KB full catalog, and leaves out the developer escape hatch.
An unknown category is `4103` with the known list, never an empty catalog.

`observe` returns a projection with a `ref` on every node; `locate` searches for
controls by role and name and returns refs too. References are locators, not
indices, and a stale one is reported as `4206` rather than resolved by guessing.

`set_value(ref, value)` writes the control, then reads it back: `verified` says
the value landed, `observed` carries what the control actually contains when it
did not (and the envelope's `uncertain` is set), and `value_source` says which
view answered. An empty `value` clears the control. When a platform write
changes nothing, the tool retries once with keystrokes — some editors accept
`ValuePattern.SetValue` and ignore it.

`activate_window(title?, handle?, exact=false)` brings a top-level window to the
foreground, restoring it if minimised, and reports `method` (`foreground`,
`already-foreground`), `previous_title` and whether it was `restored`. The
returned `ref` points at the window, so it can be used as `within` for `locate`.
`focus_in_window` answers a different question: Windows can move the foreground
without moving the keyboard focus, so a window can be in front while the screen
reader is still reading the previous one. `false` means that happened, and
`focus_window` names where the focus actually is; `null` means it could not be
determined.
Titles are matched case- and whitespace-insensitively, ignoring zero-width
characters; a title with no match is `4205 TARGET_UNAVAILABLE` with the available
titles in `details`.

`start_browse_mode(within?)` builds the document model NVDA+space would build,
for documents NVDA reads in focus mode — Word, and PDF readers that declare no
tree interceptor. It is an action, not a flag on a read: it changes the user's
session, and a `read_only` caller must not be able to do it. `created` says
whether this call built the model or one was already there, so asking twice is
one document; `interface` names the implementation that now answers, which is
what the reading tools will report next. A document that has no browse mode even
when asked is `4205`, with `created: false`.

`list_reading_targets(item, limit=20, within?, timeout_ms=1000)` enumerates a
document's positions of one kind — `heading`, `heading1`…`heading6`, `link`,
`landmark`, `formfield`, `button`, `edit`, `list`, `listitem`, `table`,
`graphic`, `checkbox`, `radiobutton`, `combobox`, `frame`, `annotation`,
`reference` — **in reading order**, without moving the cursor. This is NVDA's
quick navigation, not a tree search: a virtual buffer's order, visibility rules
and heading levels are the screen reader's, which is why it is a different
question from `locate(role="HEADING")`. Each target carries `label` (what NVDA
would announce), `text` (the position's own text), `role`, `level`, and `ref`
when a control backs the position — a plain heading is a text range and has no
ref. `available` is what this document can be searched by, and `source` says how
that was learned: `probed` (the implementation's own type table), `attempted`
(measured by asking, which is what a refused type triggers), or `declared` (read
out of NVDA's source for documents that cannot be asked).

Both reading tools run on NVDA's main thread, so both take `timeout_ms` (default
1000, at most 5000) and both answer with `stopped`: `end` (the document ran out),
`limit` (the requested number of targets was reached), `steps` (all the moves
were made), `stalled` (the search came back to where it already was) or
`timeout` (the budget expired). A list cut by the budget is still
`truncated: true`, and `scanned` says how many positions were visited: a partial
answer is never reported as a complete one. This is the same shape as `locate`'s
`max_nodes` plus `timeout_ms`, and it exists because measured cost is
0.15-0.17 ms per position, `steps=20` is about 1.3 s, and a 50-target list on a
2,000-link page is about 0.8 s — real work, on a thread the screen reader needs.

`navigate_reading(item, direction, steps=1, within?, timeout_ms=1000)` moves the
reading position the way the quick nav key would: `next`, `previous`, `first`,
`last`. `steps` skips ahead — three headings is one call — and the cursor is
moved between steps, because a document searched from a position can yield a
single match. `reached` reports how many moves actually happened, which is how a
document that ends first is told apart from a completed move. `last` scans to the
end of the document, up to 2000 positions, and refuses — rather than guessing —
when there are more than that or when the budget expires. It is an action: the
screen reader announces the new position and the reply settles.

Both tools are about *documents*: browsers, Word and PDF viewers have a document
model, and an ordinary application does not. A document with no browse mode, or
a window in front whose page does not hold focus, is `4205` with `browse_mode`
and a `reason` naming the fix (put focus in its page) rather than an answer about
some other window.

Use `GET /tools` as the source of truth for parameter schemas and output types.

## Protocol 2 (api_level 2)

Additive only. A client that asks for api level 1 sees the v1 payloads above.

* **`result` block** on the action envelope, beside `data`: `ok`,
  `state_version`, and for actions `changed`, `delta`, `settled_ms`, `uncertain`;
  `completed` and `expect_mismatch` appear only when the caller sent `expect`.
  `state_version` identifies the observation the answer is valid for.
* **`expect`** on every action: a map of projection paths to values (for example
  `{"focus.name": "Search"}`). `completed` reports whether it was confirmed; a
  mismatch returns both values.
* **`outputSchema`** per tool, plus a top-level `schema_hash` on `GET /tools`, so
  a companion can detect a contract change instead of validating against a stale
  schema.
* **`deprecated` / `supersededBy`** per tool, for the tools that still carry a
  replacement. Nothing is deprecated at api level 3: the aliases that were
  waited out were removed rather than deprecated a second time.
* **Error `4206` `STALE_REFERENCE`** (HTTP 409): the referenced object is gone or
  expired. Call `observe` or `locate` for fresh references.
* **`GET /events?raw=1`** returns the raw event journal instead of coalesced
  transitions, for debugging.
* **`4103` for a handler-level bad argument.** A `ValueError`/`TypeError` raised
  inside a tool is the caller's error, not a bridge fault (`5303`). Domain errors
  keep their own code and are not prefixed with an exception class name.
* **Tool surface changes are announced.** The companion advertises
  `tools.listChanged` and sends `notifications/tools/list_changed` when the
  bridge's `schema_hash` changes, so a connected client re-lists instead of
  keeping the tool set it saw at connect time.
* **Errors survive the companion.** A 4xx with a complete error envelope is
  passed through as a tool error carrying the bridge's code, name, category,
  retry policy and details, rather than being flattened into "bridge
  unreachable"; only a real transport failure is reported as one.

## Protocol 3 (api_level 3)

The first *subtractive* level. At this level `supported_api_levels` was `[2, 3]`:
a client that speaks 3 uses the catalog above, and the bridge no longer serves
1 or 2. (Level 4 widened it again; see the last section.)

* **Seven v1 tools removed**: `get_current_focus`, `get_navigator_object`,
  `read_current_line`, `move_focus`, `send_keyboard`, `get_desktop_object` and
  `get_window_title`. Each had a replacement in the level-2 surface for a full
  release cycle. A call to a removed name is `4102 INVALID_ACTION_NAME`, and the
  message names the action — it does not fall back to another tool.
* **`GET /tools?category=`** filters the catalog. It accepts a comma-separated or
  repeated list, so a companion can advertise `action,observation` (58 KB) rather
  than the whole surface (101 KB). The companion reads
  `NVDA_BRIDGE_CATEGORIES` and appends the filter to its own discovery call.
* **What a retirement owes a client**: a level bump, a documented replacement,
  and an error that names the replacement rather than the absence. The aliases
  were given a full cycle at level 2 before this one removed them.

## Protocol 4 (api_level 4)

`supported_api_levels` is `[2, 3, 4]`: a companion speaking any of those levels is
accepted, and one speaking a level the bridge does not list is refused with a
message naming both. The companion compares against the bridge's advertised list
rather than demanding equality, which is what the field was always for — the
previous exact-match rule meant a level-3 companion and a level-4 bridge refused
each other and the client saw an empty tool list.

* **`create` removed from both reading tools.** It became `start_browse_mode`, an
  action. A tool that is `read_only` and `side_effects: false` must not be able to
  change NVDA's browse-mode state, and the metadata was the only thing saying it
  could not.
* **`timeout_ms` and `stopped` added to both reading tools**, so a call that runs
  out of main-thread budget reports a partial answer instead of blocking the
  reader. `list_reading_targets(limit=50)` on a 2,000-link page measures ~0.8 s;
  `navigate_reading(steps=20)` measures ~1.3 s. Both now stop at the budget.
* **`last` no longer refuses a document holding exactly 2000 positions.** The scan
  stops at the cap, so it can only tell "exactly the cap" from "more" by reaching
  one position further, which it now does.
* **`/events` carries `journal_id` and pages from the oldest unseen entry.**
  `to_version` is a cursor to the last event returned, not the journal's head;
  `has_more` says to poll again immediately. Following the old cursor skipped
  every event between the page and the head, with no record that it happened.
  `journal_id` changes when NVDA restarts, which is how the companion knows its
  cursor is meaningless rather than merely quiet.
* **`expected_duration_ms` corrected** for the reading tools (300 and 400 ms,
  against measured handler times of ~0.8 s and ~1.3 s worst cases). It is
  internal metadata and is not published in `/tools`, so no client was misled by
  the old numbers — but a declaration next to the code should be about right.
