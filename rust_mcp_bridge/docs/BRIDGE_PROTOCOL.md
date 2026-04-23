# NVDA Bridge Protocol

Canonical protocol specification and JSON Schemas live in the bridge repository:

- `../nvda-mcp-bridge/docs/protocol/PROTOCOL.md`
- `../nvda-mcp-bridge/docs/protocol/common.schema.json`
- `../nvda-mcp-bridge/docs/protocol/health.schema.json`
- `../nvda-mcp-bridge/docs/protocol/tool-discovery.schema.json`
- `../nvda-mcp-bridge/docs/protocol/action-request.schema.json`
- `../nvda-mcp-bridge/docs/protocol/action-response.schema.json`

## Server conformance notes

- The Rust server should validate bridge responses against the above schemas at the boundary.
- Keep serde structs in sync with schema changes.
- Preserve `request_id` in logs and error propagation for traceability.
