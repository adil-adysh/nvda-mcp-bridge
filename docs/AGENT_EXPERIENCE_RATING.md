# NVDA MCP Server — Agent Experience Rating

**Date:** 2026-11-14
**Evaluator:** an AI agent driving the tools through MCP over stdio, as a real client would.
**Build evaluated:** bridge `0.10.0` (API 1), companion `0.10.0` (SHA-256 `6df47321…`), rebuilt with
`scons` and installed into NVDA. 31 tools exposed (`execute_python` correctly hidden without
developer mode).

> **Follow-up (same day):** the discovery gaps below were implemented. The current
> build exposes **33** tools by default (`find_tools` and `get_tool_schema` are new;
> `execute_python` is still developer-only). The historical scores in this document
> describe the build above; see **Discovery follow-up** for the current state.

## Verdict

**6.8 / 10 after a clean redeploy — 3.5 / 10 on the machine as I found it.**

The contract is unusually thoughtful: identity-bound refs, typed event cursors, explicit action
verification, and honest resync after an NVDA restart are all real and demonstrable. The two things
that keep it out of the top tier are **token cost** and **schema/transport ergonomics** — and the
out-of-box installation was actively broken by version skew.

## Scorecard

| Dimension | Score | One-line reason |
|---|---|---|
| Input schema design | 6.5 | Typed, bounded, but 14% of properties documented and no defaults/titles |
| Output schema correctness | 8.5 | 31/31 schemas, 0 violations across 22 live calls; errors violate them |
| Token efficiency | 4.0 | 58 KB discovery; 2–20× duplication; 32–44 KB log defaults |
| Ease of use | 7.0 | Excellent prose and defaults, but silent empty searches and split metadata |
| MCP standards | 7.5 | Modern protocol, annotations, resources, notifications; non-JSON error text |
| Reliability / lifecycle | 7.0 | Resync and recovery are honest; install skew and startup exit are not |
| Safety | 8.0 | No auto-retry or re-targeting; some destructive tools marked non-destructive |
| Discoverability | 6.0 | Great descriptions; 58 KB and no `title` make the list expensive to read |

## What I actually did

1. Found the installed companion was API level 4 against the API 1 bridge — it refused to start, and
   the process already running served a stale catalog. Redeployed the add-on and companion.
2. Exercised every safe tool (22 calls) and validated each `structuredContent` against its advertised
   `outputSchema` with `jsonschema`.
3. Ran mutation, stale-ref, cross-window ref, trace, and bounded-observation tests against Notepad.
4. Ran six concurrent calls, restarted NVDA mid-session, and replayed the old event cursor.
5. Tested protocol edges (unknown tool, wrong types, empty filters), resources, and bridge-down startup.
6. Measured payload bytes for text vs structured content and for the whole discovery document.

## Evidence by dimension

### 1. Input schemas — 6.5/10

Good:
- Draft-07, `additionalProperties: false`, real enums (`include`, `detail`, `filter_level`, `button`,
  `source`, `reading item`), sensible numeric bounds, and 21 tools callable with no arguments.
- The per-tool prose is genuinely good: it explains *when* to use a tool, not just what it does.

Bad:
- **15 of 110 input properties (14%) have a description.** `role`, `since`, `limit`, `max_nodes`,
  `x`/`y`, `keys` behaviour and most `expect` shapes are undocumented in the schema itself.
- **98 optional properties declare no `default`**, so a model cannot know what happens when it omits
  them (even though the server has defaults).
- **No `title` on any of the 31 tools.**
- Schema and handler disagree: `get_logs.max_lines` declares no minimum or maximum, the handler
  enforces 1–10000, and 0 / −5 / 20000 all return **5303 BRIDGE_INTERNAL_ERROR** with
  `retry_policy: after_delay` — an argument error classified as a retryable internal fault.
- Free-form filters fail silently: `locate(role="banana")` returned success with zero matches (547 ms),
  and `wait_for_events(kinds=["not_a_kind"])` returned success with no events.
- Validation errors from the companion are generic — `"request arguments do not match inputSchema"` —
  with **no field, expected value, or machine-readable code** (and no `structuredContent`).

### 2. Output schemas — 8.5/10

Good:
- All 31 tools publish a closed `outputSchema`; all 22 live success payloads validated with **zero
  violations**. `observe` is compact (1.9 KB) and includes `refs`, `state_version`, `revision`,
  `top_window`, and truncation fields. `set_value` reports `verified`, `value_source`, `previous`,
  `observed`; the envelope adds `verification: "confirmed"` with bounded evidence.
- Resource `nvda://ui/state` exists and reads correctly.

Bad:
- **Error results violate the declared `outputSchema`** (5 violations for one `invoke` error): the
  error `structuredContent` is the full transport envelope, while `outputSchema` describes `data`.
  A spec-following client that validates will reject every tool error.
- **Action metadata is missing from `structuredContent`.** `verification`, `evidence`, `scope`,
  `state_version`, and `trace_id` live only in the text envelope. A client that consumes
  `structuredContent` — the recommended path — cannot see that an action was confirmed, cannot
  retrieve the trace, and cannot correlate state. `set_value` exposes `verified` but not
  `verification`/`evidence`.
- `observe(max_bytes=400)` returned **857 bytes** (2.1× the budget) and still said `truncated: true`;
  the "bound the serialized observation" promise is only best-effort.
- `health_snapshot.data.top_window.ref` is `""`, while `observe` returns a usable `"w"` for the same
  window.

### 3. Token efficiency — 4.0/10

Measured through the MCP surface:

| Item | Size |
|---|---|
| `tools/list` (31 tools) | **57,974 B ≈ 14.5 k tokens** |
| — output schemas | 29,441 B |
| — input schemas | 15,875 B |
| `summarize_logs` default | **43,851 B text / 32,139 B structured** |
| `summarize_logs` `limit=5` | 11.5 KB structured |
| `list_loaded_addons` running | 13.1 KB structured (17.9 KB text) |
| `get_action_trace` | 10,346 B |
| `search_logs` `limit=5` | 9,968 B text / 6,484 B structured |
| `read` one line | **74 B structured / 1,496 B text (20×)** |
| `observe` | 1,866 B structured / 4,563 B text (2.4×) |
| `inspect_object` | 362 B / 1,866 B (5.2×) |
| stale-ref error | 2,669 B |

Every success is sent twice: a pretty-printed JSON envelope as text and a compact `data` object as
`structuredContent`. The envelope also carries a ~600 B `trace` object on **every** call. For an
agent that reads text (as this host does), the overhead is the whole bill; for one that reads
structured content, the envelope's best information (verification, trace) is unavailable.

### 4. Ease of use — 7.0/10

Good:
- `observe()` with no arguments is a useful first call; refs are stable while `state_version`
  advances (`f`/`w` unchanged across versions 18→19), and refs work while another window is in front.
- `set_value` gave `verification: "confirmed"`, `settled_ms: 144.8`, `state_version: 17`, and bounded
  evidence — this is exactly what an agent needs to avoid blind retries.
- `wait_for_events` with a foreign `journal_id` returns `cursor_status: "journal_mismatch"` and
  `resync_required: true` instead of an empty, misleading result.
- After an NVDA restart the first call failed honestly (`BRIDGE_UNREACHABLE`) and calls recovered in
  about 5 s; the old cursor came back as `journal_mismatch` with a new `resync_from`.

Bad:
- `press` is keyboard-only (its schema has no `ref`), yet sits beside `invoke`, which is the
  element-action tool. The names invite the wrong choice.
- `locate()` with no filter returns success and zero matches — indistinguishable from "nothing here".
- `activate_window` returned `focus_in_window: false` while the subsequent `observe` showed focus had
  in fact moved; the field is checked too early to be trusted.
- Error text is **not valid JSON** (`"Bridge tool error [4206] …{json}"`), while success text is pure
  JSON. A text-parsing client has to special-case every failure.
- The docs promise a top-level `recovery` object on errors; in practice `invoke` returns
  `recovery: null` with `details.recovery` set, and `get_logs` has neither. Recovery is real but not
  where the contract says it is.

### 5. MCP standards — 7.5/10

Good:
- Protocol `2025-06-18`, `initialize` instructions describe API level and bridge version, `ping`,
  `prompts/list`, `resources/list|read`, `tools/list_changed`, and 31/31 tool annotations.
- Unknown tool → `-32602`; missing required args → `isError: true`; resources advertise `subscribe`.
- Tool names are snake_case, schemas are object-rooted, `structuredContent` on success matches the
  schema.

Bad:
- Errors are not schema-conformant (above), and input-validation errors carry no `structuredContent`
  at all.
- No `title`; `tools/list` ignores an unknown `cursor` instead of rejecting it; no progress
  notifications; resource content is a stringified JSON document rather than a typed payload.
- Annotations understate risk: **23 mutating tools say `destructiveHint: false`**, including
  `invoke`, `set_value`, and `click_at`. Only `restart_nvda` and `reload_plugins` are marked
  destructive.
- `dismiss` is marked `idempotentHint: true` and non-destructive; closing a dialog is a state change.

### 6. Reliability and lifecycle — 7.0/10

Good:
- Six concurrent calls serialized cleanly (157 ms wall, no errors, identical payload sizes).
- `instance_id` changes across restart (`446d…` → `1ff8…`); old journal correctly demands resync.
- `BRIDGE_UNREACHABLE` is a structured, non-retryable-looking error rather than a crash.
- Stale-ref handling is genuinely safe: `4206` with `reason_code`, `expected_reference`, `received`,
  `scope`, and a hint that refs do not expire merely because `state_version` advanced.

Bad:
- **The installed companion was API 4 against an API 1 bridge.** Running it by hand exited with
  `Bridge API level 1 does not support this companion's level 4`. The process already running served
  a stale catalog: `health_snapshot`, `observe`, `locate`, `get_focus_lineage`, and
  `list_loaded_addons` all failed with "output validation failed", and `observe` rejected
  `include_tree` that its stale schema advertised. This is the single worst finding: the tools looked
  present but five of them were unusable.
- The install directory contained three leftover binaries (`old.exe`, `old2`, `.new.exe`), evidence
  of repeated partial upgrades.
- If the bridge is missing at startup, the companion retries for ~13 s, prints one line, and **exits**
  — the client gets no tools at all instead of degraded tools. There is no reconnecting state.
- `set_value` on a mis-identified control wrote into Edge's address bar during testing. The ref was
  valid; the agent's model of the UI was wrong. Verification reported success for the write it made,
  which is correct — but it shows that a "valid ref" is not a guarantee of the intended target.

### 7. Safety — 8.0/10

Good:
- No automatic replay or re-targeting after stale refs; mutating tools act on exactly the submitted
  ref and fail closed.
- Developer mode gates `execute_python` (31 vs 32 tools).
- Evidence is bounded and redacts values on sensitive paths; event cursors never silently skip
  history.

Bad:
- `destructiveHint: false` on `set_value` (overwrites text), `invoke` (can delete), and `click_at`
  (can do anything) is an unsafe claim for a UI automation server.
- The promised top-level `recovery` is missing, so an agent cannot reliably key off it.
- `5303` + `after_delay` for an out-of-range argument can induce a retry loop on a request that will
  never succeed.

## What is genuinely excellent

- **Verification semantics.** `confirmed` vs `contradicted` vs `unavailable`, `uncertain`, and
  `dispatched` are the right distinctions, and `set_value` really proves the value changed.
- **Event continuity.** Typed cursors, journal generations, `resync_required`/`resync_from`, and
  `history_complete` are better than most MCP servers manage.
- **Reference identity.** Refs survive `state_version`; they die on TTL, identity, window, or bridge
  instance, and the error explains which.
- **Prose quality.** The tool descriptions are the best part of the surface: specific, cautious, and
  written for an agent that must choose between `invoke`, `click_at`, and `dismiss`.

## Prioritized fixes

**P0 — correctness and trust**
1. Make errors schema-conformant: a dedicated error payload (or `structuredContent` omitted) plus a
   machine-readable `error.code`, `category`, `recovery`, and offending field.
2. Move `verification`, `evidence`, `scope`, `state_version`, and `trace_id` into `structuredContent`
   and extend `outputSchema`; keep text as a one-line summary.
3. Make input validation report the field and the rule, and align schema with handler
   (`get_logs.max_lines` 1–10000; enums for `role`, `kinds`; `locate` requires a filter or returns a
   bounded sample).
4. Fix deployment skew: refuse to run with an incompatible bridge and say exactly what to reinstall;
   clean up old binaries; surface the installed companion version in `/health`.

**P1 — token and ergonomics**
5. Stop sending pretty JSON twice; either short text + structured, or one structured payload.
6. Make `trace` opt-in (`include_trace`); cap `summarize_logs`, `list_loaded_addons`, and
   `get_action_trace` defaults; add `fields` projection to `observe`/`locate`/logs.
7. Add `default` to every optional property and `title` to every tool; document the remaining 95
   properties.
8. Enforce `max_bytes` (or rename it `max_bytes_hint` and report `budget_exceeded`); fix
   `health_snapshot.top_window.ref` to be empty-or-valid.

**P2 — lifecycle and protocol polish**
9. Keep the companion alive when the bridge is down, returning `BRIDGE_UNAVAILABLE` per call and
   reconnecting with backoff; add an explicit `degraded` state.
10. Correct annotations (`destructiveHint` for `set_value`, `invoke`, `click_at`; `dismiss`
    idempotency), validate `tools/list` cursors, and consider progress notifications for long
    `locate`/`navigate` calls.

## Resolution status (implemented after this rating)

The findings above were used as the work list. This section records what changed
and what is still open; it supersedes the "Bottom line" for current builds.

### Fixed

| Finding | Resolution |
|---|---|
| Verification/evidence/trace invisible to structured clients | `structuredContent` now carries `result` and `trace_id`; `outputSchema` advertises both. `set_value` shows `verification: "contradicted"` with evidence in `structuredContent`. |
| Error results violated `outputSchema`; error text was not JSON | Errors are `isError: true` with compact JSON text and **no** `structuredContent`; the schema now describes success only. Input errors use `4103` + `details.field_errors` keyed by JSON Pointer. |
| Top-level `recovery` missing | Action failures use the shared error builder; `recovery` is always top level and no longer duplicated under `details`. The companion's parser also stopped hard-coding `recovery: None`. |
| `get_logs(max_lines=0)` → `5303` | Bounds are published (`1–10000`) and validated; the bridge returns `4103 fix_arguments`. |
| Unknown event kinds silently matched nothing | `wait_for_events` rejects unknown kinds and lists valid ones. |
| 98 optional properties without defaults; 95 undocumented | Defaults are emitted from the dataclass; common fields have fallback descriptions (metadata on optional fields is hoisted out of `anyOf`). |
| No tool `title` | All 31 tools publish a humanised `title`. |
| `max_bytes` silently overran | `observed_bytes` is always real; `budget_exceeded` is `true` when even the minimum identity payload cannot fit. |
| `destructiveHint: false` on mutating tools | `invoke`, `set_value`, `press`, `click_at` are destructive in the bridge registry and MCP annotations. |
| Companion exited when NVDA was down | It now starts with an empty catalog, stays connected, and installs/announces the surface when the bridge appears. |
| Stale binaries and API-4/API-1 skew in `Programs\NVDA MCP Bridge\bin` | The rebuilt companion is installed and byte-verified; the leftover `old`/`old2`/`new` binaries were removed. |

### Improved, with measurements

| Metric | Before | After |
|---|---|---|
| `observe` response text | 2,048 B | 844 B (−59%) |
| `read` response text | 1,489 B | 363 B (−76%) |
| Stale-ref error | 2,669 B | 1,184 B (−56%) |
| Per-call `trace` block | ~600 B every call | removed; `trace_id` + `get_action_trace` |
| Text vs structured | two different representations | identical compact JSON |
| `tools/list` discovery | 57,974 B | 76,189 B (**+31%**) |
| `summarize_logs` default | 25,687 B | 985 B (−96%) |
| `list_loaded_addons` default | 13,402 B | 2,883 B (−78%) |

`summarize_logs` now defaults to counts only; add facets with `include`.
`list_loaded_addons` defaults to `detail="summary"` (identity + state); pass
`detail="full"` for paths, authors and compatibility versions.

The discovery increase is the cost of publishing defaults, descriptions,
titles and the extended output schema. `NVDA_BRIDGE_CATEGORIES` can restrict the
catalog for small-context agents.

### Still open

- `get_action_trace` still returns the full trace when it finds one (~10 KB);
  `found: false` is small.
- `locate` accepts any role string; a typo returns an empty result rather than
  `4103`. (The earlier claim that `locate` with no filter returns empty was
  wrong: it raises `4103` for missing `role`/`name`.)
- `tools/list` still ignores an unknown `cursor`.
- No small-model evaluation or workflow benchmark was run, so the token
  improvements are measured as bytes, not as task cost.
- Live acceptance remains limited to this machine and build.

### Corrected claim

The original text said `set_value` wrote into Edge's address bar because the ref
was mis-modelled. That is accurate, but it is not a stale-reference defect: the
ref was valid and `set_value` correctly reported the write it made. It remains a
useful caution that a valid ref is not proof of the intended target.

## Discovery follow-up

The original 6/10 discoverability score had a concrete cause: 31 flat tools, a
~76 KB `tools/list`, no way to search the catalog, and no schema-on-demand path.
The follow-up keeps the API level at 1 and adds:

| Change | Effect |
|---|---|
| `find_tools(query, category, read_only, limit)` | Word-start search over name, title, category, tags and description. Returns compact summaries and required parameters, not schemas. |
| `get_tool_schema(name, include_output_schema=false)` | One input schema on demand; output schema only when requested. Unknown/hidden names point back to `find_tools`. |
| Grouped titles | Approval lists and pickers show `Diagnostics · Health Snapshot`; tool names are unchanged. |
| Disambiguated descriptions | `health_snapshot`, `observe`, `get_focus_lineage` and `inspect_object` now say which sibling to use. |
| AI Assistant search/filter | Search by name or description; filter by approved, denied or server-declared read-only. |
| Details preview | The full description and input schema appear for the selected tool, not a 160-character truncation. |
| Bulk read-only approval | One warning-led action for non-destructive tools the server *claims* are read-only, still individually revocable. |

**Revised discovery score: 8.0/10.** The selection problem is materially
smaller: an agent can ask for "log search", get a short ranked list, then fetch
one schema. The remaining caveat is protocol-level: `find_tools` is an index
*inside* the catalog, so a client that eagerly fetches `tools/list` still pays
for all 33 definitions. `NVDA_BRIDGE_CATEGORIES` remains the only way to shrink
that initial surface; a meta-tool that hides tools behind one generic executor
would trade per-tool approval for token savings, which is not an acceptable
default for a screen reader.

## Bottom line

This is a strong, semantically honest tool contract wrapped in an expensive and slightly
inconsistent transport. Fix the error/structured-content split and the token bill, and it moves to
8.5–9/10 for agent use. Leave the install/version-skew problem unaddressed and the first experience
of many users will be five broken tools and a companion that refuses to start — which is exactly what
happened here before redeploying.
