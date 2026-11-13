# NVDA Bridge Protocol

Canonical protocol specification and JSON Schemas live in the bridge repository:

- `../../docs/protocol/PROTOCOL.md`
- `../../docs/protocol/common.schema.json`
- `../../docs/protocol/health.schema.json`
- `../../docs/protocol/tool-discovery.schema.json`
- `../../docs/protocol/action-request.schema.json`
- `../../docs/protocol/action-response.schema.json`

## Server conformance notes

- The Rust server validates bridge responses against the output type schemas at the boundary.
  Validation targets the **inner `data` schema** only, not the full HTTP envelope,
  so transport fields (`request_id`, `action`, `duration_ms`, `bridge`,
  `nvda_status_snapshot`) are accepted without false rejections.
- Keep serde structs in sync with schema changes.
- `structuredContent` is the tool's `data` object with the uniform `result`
  block and `trace_id` flattened alongside it. `outputSchema` is the bridge's
  data schema extended with those optional fields, so a strict client can
  validate what it receives. Verification, evidence, `scope` and
  `state_version` are therefore visible without parsing the text block.
- The text block is the same object as **compact JSON**, not a second
  pretty-printed envelope. The verbose per-call `trace` object is not sent;
  `trace_id` plus `get_action_trace` is the supported path.
- Tool-level errors are compact JSON in the text block (`isError: true`) with
  `error.code`, `name`, `category`, `retry_policy`, `details` and `recovery`.
  No `structuredContent` accompanies an error, because `outputSchema` describes
  a successful payload.
- Local input validation uses the bridge's `4103 INVALID_PARAMS_SCHEMA`
  vocabulary and reports `details.field_errors` keyed by JSON Pointer, instead
  of a generic "does not match inputSchema" sentence.
- The Rust server falls back to `/execute` if `/action` returns 404.
- Tool `constraints` from discovery (`dangerous`, `requires_explicit_opt_in`,
  `blocked_on_secure_screen`, `blocked_on_modal`, `blocked_on_menu`) are forwarded
  to MCP tool annotations. Mutating default actions (`invoke`, `set_value`,
  `press`, `click_at`) are marked destructive by the bridge.
- Tool titles are derived from the tool name and advertised as MCP `title`.
- Bridge identity (`instance_id`, `started_at`) is preserved in catalogs and
  action metadata. A changed instance id is a replacement, not continuity.
- Event polls send `journal_id` with `since`. A page with
  `resync_required: true` is never treated as complete history: the pump jumps
  to `resync_from.version`, adopts `resync_from.journal_id`, notifies
  subscribers and re-observes rather than replaying a mutation.
- If the bridge is unreachable at startup, the companion serves an **empty**
  catalog and keeps retrying, instead of exiting and removing every tool from
  the client. The refresh loop installs the surface and announces
  `tools/list_changed` when the bridge appears.
