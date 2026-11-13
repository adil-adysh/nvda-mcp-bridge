# NVDA Bridge Protocol Specification

- Protocol: `nvda-bridge-protocol`
- Version: `1.0.0` (bridge/add-on `0.10.0`, API level `1`)
- Status: Stable (v1 bridge release)

This document standardizes communication between:

- NVDA MCP Bridge (Python, in-process NVDA add-on)
- NVDA MCP Server (Rust, MCP-facing executable)

## 1) Transport and Security

- Transport: HTTP over loopback (`127.0.0.1`)
- Content type: `application/json; charset=utf-8`
- Auth header (required except `GET /health`): `X-NVDA-Bridge-Token`
- Unknown JSON fields MUST be ignored (forward compatibility)
- Additive fields are allowed in minor versions

## 2) Versioning

- Semantic versioning for `protocol_version`
- `api_level` is integer capability gate for server behavior
- Server startup policy:
  - MUST call `GET /health`
  - MUST fail fast if `api_level` is unsupported
  - MUST fail fast on protocol mismatch or discovery contract violations

## 3) Endpoints

### `GET /health`

Purpose: versioned handshake and runtime readiness.

Response fields:

- `status`: `"ok"` | `"degraded"` | `"error"`
- `protocol`: fixed string `"nvda-bridge-protocol"`
- `protocol_version`: `"1.0.0"`
- `bridge_version`: bridge implementation version
- `api_level`: active API level
- `supported_api_levels`: integer array
- `uptime_seconds`: bridge uptime in seconds
- `timestamp_utc`: RFC3339 UTC timestamp
- `nvda_status`:
  - `running`: bool
  - `secure_screen`: bool
  - `modal_blocking`: bool
  - `menu_open`: bool
  - `speech_available`: bool
  - `braille_available`: bool

### `GET /tools`

Purpose: runtime tool discovery.

Canonical response:

- `protocol`, `protocol_version`, `bridge_version`, `api_level`
- `types`: non-empty object registry of named output types
- `tools`: array of tool metadata
- `count`: integer count of tools

Tool metadata:

- `name`: string
- `description`: string
- `inputSchema`: JSON Schema object (`type: object`) describing required parameters; the bridge must provide a concrete schema, tools should avoid broad unions.
- `outputType`: PascalCase named type that MUST exist in top-level `types`.
- `version`: string
- `category`: string
- `constraints` (optional):
  - `blocked_on_secure_screen`: bool
  - `blocked_on_modal`: bool
  - `blocked_on_menu`: bool

`types` rules:

- MUST be present and non-empty.
- Keys MUST be PascalCase.
- Values MUST be draft-07 JSON Schemas with object root.
- Every tool `outputType` MUST resolve to an existing key.
- Inline output contracts are not allowed.
- No unused type definitions are allowed.

### `POST /action` (primary) and `POST /execute` (alias)

Purpose: execute one tool/action.

`POST /execute` is a Rust-client-compatible alias for `POST /action`.
Both endpoints accept and return the same payload shape.

Request:

- `request_id`: correlation id (UUID recommended)
- `action`: tool/action name
- `params`: JSON object
- `timeout_ms` (optional): client timeout hint
- `client` (optional): `{ name, version }`

Response envelope (always):

- `request_id`: echoed from request
- `success`: bool
- `action`: echoed from request
- `duration_ms`: elapsed bridge execution time
- `bridge`: `{ bridge_version, api_level }`
- `nvda_status_snapshot`: `{ secure_screen, modal_blocking, menu_open }`
- `result`: the uniform result block (see below); present on success, and with
  `ok: false` on tool failure
- `data`: the tool's own typed payload, present on success
- `error`: present on failure

The result block is the same for every tool, read or action:

- `ok`: bool — the call ran; says nothing about the UI
- `state_version`: integer — the observation version this answer is valid for
- `changed` (actions): whether the projection actually differed
- `delta` (actions): what differed, field by field (`{from, to}` per field)
- `settled_ms` (actions): how long until the observation was stable
- `uncertain` (actions): delivery could not be confirmed
- `completed` (actions, only with an `expect` request): whether the stated
  postcondition was observed
- `expect_mismatch` (actions, only when `expect` failed): expected vs actual

It lives beside `data` rather than inside it: the fields mean the same thing for
every tool, and v1 output schemas are closed.

## 4) Error Model

Error object:

- `code`: integer from registry
- `name`: stable machine-readable identifier
- `message`: human-readable summary
- `category`: one of `auth | validation | nvda_state | runtime | internal`
- `retry_policy`: one of `never | immediate | after_delay | after_state_change`
- `retry_after_ms` (optional): positive integer when `retry_policy = after_delay`
- `details` (optional): object with structured diagnostics

## 5) Error Code Registry

### 400x Auth / Access

- `4001` `AUTH_FAILURE`
- `4002` `ACTION_FORBIDDEN`

### 410x Request / Validation

- `4101` `INVALID_JSON`
- `4102` `INVALID_ACTION_NAME`
- `4103` `INVALID_PARAMS_SCHEMA`
- `4104` `INVALID_TIMEOUT`

### 420x NVDA State / Preconditions

- `4201` `SECURE_SCREEN_ACTIVE`
- `4202` `MODAL_DIALOG_BLOCKING`
- `4203` `MENU_OPEN_BLOCKING`
- `4204` `ACTION_CANCELLED`
- `4205` `TARGET_UNAVAILABLE`
- `4206` `STALE_REFERENCE` — re-observe; references are never re-resolved by name

### 500x Execution / Runtime

- `5001` `NVDA_COM_TIMEOUT`
- `5002` `MAIN_THREAD_TIMEOUT`
- `5003` `TOOL_EXECUTION_EXCEPTION`
- `5004` `ACTION_TIMEOUT`

### 530x Bridge / Internal

- `5301` `TOOL_REGISTRY_UNAVAILABLE`
- `5302` `DISCOVERY_PAYLOAD_MALFORMED`
- `5303` `BRIDGE_INTERNAL_ERROR`

## 6) NVDA-Specific Edge Cases

### Secure Screen

When `secure_screen = true`, bridge SHOULD reject blocked tools with:

- `code=4201`
- `retry_policy=after_state_change`

### Modal Dialog Blocking Input

When `modal_blocking = true`, bridge SHOULD reject UI-navigation/input tools with:

- `code=4202`
- `retry_policy=after_delay`
- `retry_after_ms` recommended range: `300..1000`

### Menu Open

When `menu_open = true`, bridge SHOULD reject focus-changing operations with:

- `code=4203`
- `retry_policy=after_delay`
- `retry_after_ms` recommended default: `500`

## 7) Machine-Readable Schemas

Schemas are in this directory:

- `common.schema.json`
- `health.schema.json`
- `tool-discovery.schema.json`
- `action-request.schema.json`
- `action-response.schema.json`

## 8) Implementation Guidance

- Rust: use strict serde models aligned to schemas; reject unknown/legacy fields.
- Python: fail bridge startup if output type registry or tool references are invalid.
- Preserve `request_id` end-to-end for observability and timeout tracing.
