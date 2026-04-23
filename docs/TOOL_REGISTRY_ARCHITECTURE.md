# Tool Registry Architecture (v2)

## Overview

The bridge uses an explicit, deterministic registration flow:

* HTTP layer validates/authenticates requests.
* `NVDAHTTPBridge` orchestrates timing and envelopes.
* `TOOL_MANIFEST` is the single source of truth for tools.
* `build_registry(...)` validates and builds immutable `ToolRegistry`.
* `MainThreadExecutor` guarantees NVDA API thread safety.

No reflection or subclass scanning is used in the tool registration path.

## Core Model

`ToolSpec` includes:

* `name`, `description`, `version`, `category`
* `handler` (callable)
* `input_schema` (JSON Schema object)
* `output_type` (named type key from top-level `types` registry)
* `constraints` (optional metadata)

This supports strict discovery contracts in `GET /tools`:

* Tool entries expose `inputSchema` and `outputType`.
* Output schemas are centralized in `types`.
* Tool ordering is deterministic and matches manifest order.

## Validation Modes

Validation behavior is centralized in `validation.py` via `ValidationMode`:

* `STRICT` (CI): duplicate names, missing output types, invalid schema, unused output types, or empty manifest are fatal.
* `RUNTIME` (NVDA startup): duplicate names and empty active manifest are fatal; tools with missing output types/invalid schema are quarantined; unused output types are warnings.

Validated tools are passed to immutable `ToolRegistry` with no mutation APIs.

## Execution Flow

1. `POST /action` request is parsed and validated (`request_id`, `action`, `params`).
2. Bridge resolves `ToolSpec` and applies NVDA state gating using `category`.
3. Tool execution is performed through `MainThreadExecutor`.
4. Response is wrapped as structured envelope.

Success envelope shape:

```json
{
  "request_id": "uuid",
  "success": true,
  "action": "tool_name",
  "duration_ms": 12.3,
  "bridge": { "bridge_version": "1.0.0", "api_level": 1 },
  "nvda_status_snapshot": {
    "secure_screen": false,
    "modal_blocking": false,
    "menu_open": false
  },
  "data": {}
}
```

Error envelope shape:

```json
{
  "request_id": "uuid",
  "success": false,
  "action": "tool_name",
  "duration_ms": 1.7,
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

## Extending with New Tools

To add a new tool:

1. Add a typed handler function to `tool_handlers.py`.
2. Add explicit `ToolSpec` entry to `tool_manifest.py`.
3. Ensure output type exists in `output_type_registry.py`.

No HTTP routing changes are needed for normal tool additions.
