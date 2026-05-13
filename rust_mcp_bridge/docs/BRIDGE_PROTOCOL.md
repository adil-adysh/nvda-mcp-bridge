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
- Preserve `request_id`, `action`, `duration_ms`, and `bridge` metadata in MCP content
  for downstream AI agent observability and traceability.
- The Rust server falls back to `/execute` if `/action` returns 404.
- Tool `constraints` from discovery (`dangerous`, `requires_explicit_opt_in`,
  `blocked_on_secure_screen`, `blocked_on_modal`, `blocked_on_menu`) are forwarded
  to MCP tool annotations.
