# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.11.1

### Fixed

- Install or Repair stops every companion process started from the install
  directory before replacing the binary. Renaming a running executable already
  worked, but the live process kept serving the old build and its client kept a
  stale tool catalog. The result now reports how many clients must reconnect,
  and a process that cannot be stopped aborts before any file is changed.
- Uninstall stops running companions first, so the executable is no longer
  reported as permanently in use.
- Windows process handles are declared as 64-bit handles; the previous
  default `c_int` return could truncate a handle on 64-bit Windows.

## 0.11.0

### Changed

- `tools/list` omits each tool's `outputSchema` by default. Output schemas were
  about half the catalog bytes; `structuredContent` is still validated against
  the bridge schema, `get_tool_schema(include_output_schema=true)` still returns
  it, and `NVDA_BRIDGE_OUTPUT_SCHEMAS=1` restores the advertised copy.
- `get_action_trace` returns a summary by default. The event list, projection
  delta and native-log line lists are available with `detail="full"`, and
  `omitted` names what the summary removed.

### Fixed

- `locate` rejects a role NVDA does not have (for example `banana`) with a
  correction and close matches, instead of walking the window and returning an
  empty success.
- `tools/list` honours MCP pagination (`NVDA_BRIDGE_TOOLS_PAGE_SIZE`, default
  100) and rejects an unknown cursor instead of silently returning every tool.
- Documented the version line: the API-4 `1.0.0` package predates the reset and
  is not an upgrade from `0.10.0`/`0.11.0`.

## 0.10.0

### Added

- Typed event cursors: `journal_id` joins `since` in `wait_for_events`, with
  `expired`, `resync_required`, `resync_reason`, `resync_from`,
  `earliest_available_version` and `history_complete` in the result. A journal
  reset generates a new random identity; a foreign cursor is reported as
  `journal_mismatch` instead of being interpreted against unrelated history.
- Bridge instance identity (`instance_id`, `started_at`, `capabilities`) on
  `/health`, `/tools`, `/state` and every action envelope. References are bound
  to the issuing instance.
- Reference staleness reasons `ttl`, `identity`, `window`, `bridge_instance`
  and `unavailable`, with typed recovery details and window identity
  (handle + process + class) separate from object identity.
- Action verification vocabulary (`confirmed`, `contradicted`, `unavailable`)
  and bounded, redacted evidence on results that take `expect`.
- A typed `recovery` object on every error, with stable recovery actions.
- `observe(max_bytes=...)` with explicit `truncated`, `truncated_sections`,
  `more_available`, `observed_bytes`, `budget_exceeded` and a continuation hint.
  The bound is enforced for the optional payload; a budget smaller than the
  minimum identity payload reports `budget_exceeded` instead of overrunning
  silently.
- `docs/REFERENCE_AND_VERIFICATION.md`, `docs/tool_inventory.json`,
  `docs/AGENT_EXPERIENCE_RATING.md`, and deterministic suites for cursors,
  references, verification, envelopes, defaults and bounds.
- Tool `title` on every MCP tool, humanised from the tool name and prefixed
  with its group (`Diagnostics · Health Snapshot`).
- `find_tools` and `get_tool_schema` for compact catalog search and
  schema-on-demand, including category/read-only filters and hidden-tool
  enforcement.
- MCP `structuredContent` carries the uniform `result` block (verification,
  evidence, scope, state_version) and `trace_id`; `outputSchema` advertises
  those optional fields.

### Changed

- Reset the unreleased contract to `api_level` 1 and the add-on version to `0.10.0`.
- Lean, bounded defaults: `health_snapshot`, `observe`, `locate`, logs and
  `wait_for_events` no longer return detail or history nobody asked for.
- `observe` returns a `refs` map and `top_window`; every action result carries
  `state_version` and `scope`.
- `press` normalizes key aliases and reports `requested` / `normalized_keys`.
- All JSON is snake_case; `location` is one object shape everywhere.
- The MCP companion advertises `outputSchema` and returns `structuredContent`.
  It now sends `journal_id` with event polls, resynchronises on
  `resync_required`, detects Bridge instance replacement, and forwards
  `error.recovery`.
- Action failures use the same error builder as transport failures, so
  `recovery` is always top level; `details.recovery` is no longer duplicated.
- `get_logs(max_lines=...)` is validated against 1–10000 and published with
  those bounds; an out-of-range value is `4103`, not `5303`.
- Unknown `wait_for_events.kinds` values are rejected instead of silently
  matching nothing.
- Schema generation hoists descriptions/defaults onto optional properties and
  publishes every default; common fields gained fallback descriptions.
- `health_snapshot.top_window.ref` is `null` (it registers no reference).
- `invoke`, `set_value`, `press` and `click_at` are marked destructive.
- MCP success text is compact JSON of `structuredContent` (no duplicate
  pretty-printed envelope, no per-call `trace` block). Errors are compact JSON
  without `structuredContent`. The companion starts with an empty catalog and
  retries when the bridge is unreachable, instead of exiting.
- `summarize_logs` defaults to `["counts"]` (~1 KB instead of ~25 KB); request
  `exceptions`, `warnings`, `repeated_patterns`, `top_sources`, `time_buckets`
  or `noise` explicitly.
- `list_loaded_addons` defaults to `detail="summary"` (identity and state,
  ~3 KB); `detail="full"` returns paths, authors and compatibility versions.
- The Rust companion no longer treats an unknown Bridge instance as continuity.
- Removed legacy architecture/migration documents and API-4 history.

### Fixed

- `wait_for_events` no longer drops `journal_id` and the completeness flags
  before returning to the caller.
- A stale reference hint no longer claims references are valid for exactly one
  `state_version`.
- Unknown wire error codes are normalised to `5303` instead of being returned
  under a fabricated code.
- An empty event journal is no longer considered expired.
- Cancellation during an event wait ends the wait instead of pinning the worker.



### ≡ƒÜÇ Features

- execute_python functionality and update API reference

- execute_python to support multiline class definitions and update API reference with execution details

- diagnostic tools and API reference updates for object inspection and health snapshot

- Introduce NVDA Bridge Protocol Specification and JSON Schemas


### ≡ƒôÜ Documentation

- NVDA HTTP Bridge and MCP Server functionality

- add-on version to 0.2.0 and enhance README with API endpoint details and action descriptions


### ≡ƒöº Refactoring

- README to clarify purpose and functionality of nvda-mcp-bridge

- NVDA MCP Bridge to use Tool Registry Pattern


### ≡ƒÜÇ Features

- NVDA MCP Server and related tools

- NVDA MCP Bridge with new development tools and improve shutdown process

- ToolBase and ToolMetadata for NVDA MCP Bridge

<!-- generated by git-cliff -->
