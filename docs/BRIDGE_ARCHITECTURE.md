# NVDA HTTP Bridge Addon - Architecture (v1)

## Overview

`nvda-mcp-bridge` is a thin HTTP bridge running inside NVDA. It exposes local actions to a separate MCP server process.

* Bridge runtime: NVDA add-on process
* Bridge bind: `127.0.0.1:8765`
* Protocol identity: `nvda-bridge-protocol` `1.0.0` (API level `1`)

## Three-Component Architecture

```text
┌─────────────────┐
│   VS Code       │
│  (MCP Client)   │
└────────┬────────┘
         │ MCP protocol
         │ (127.0.0.1:8000)
         ↓
┌─────────────────────────────────┐
│  nvda-mcp-server (Standalone)   │
│  - MCP protocol implementation   │
│  - Tool/session orchestration    │
└────────┬────────────────────────┘
         │ HTTP bridge protocol
         │ (127.0.0.1:8765)
         ↓
┌─────────────────────────────────┐
│  NVDA + this add-on             │
│  - Request validation/auth      │
│  - Main-thread NVDA execution   │
│  - Structured success/error env │
└─────────────────────────────────┘
```

## Responsibilities

### Bridge add-on

* Implements `GET /health`, `GET /tools`, `POST /action`.
* Authenticates protected endpoints via `X-NVDA-Bridge-Token`.
* Runs NVDA API calls through main-thread executor for thread safety.
* Returns structured envelopes with:
  * `request_id`, `success`, `action`, `duration_ms`
  * `bridge` metadata and `nvda_status_snapshot`
  * `data` on success or structured `error` on failure

## Startup Lifecycle

Bridge startup is explicit and deterministic:

1. Load output type registry
2. Load canonical `TOOL_MANIFEST`
3. Validate contracts using `ValidationMode`
4. Build immutable `ToolRegistry`
5. Bind/start HTTP server

No runtime tool discovery, subclass scanning, or dynamic tool registration occurs.

### MCP server (separate process)

* Speaks MCP to clients.
* Calls bridge endpoints.
* Performs higher-level orchestration and transport to clients.

## Example Flow

1. MCP server sends:

```json
{
  "request_id": "f6ee084a-c16d-4178-b16c-2f6ea3c55edf",
  "action": "get_current_focus",
  "params": {}
}
```

1. Bridge validates payload + auth, then executes tool on NVDA main thread.

1. Bridge responds:

```json
{
  "request_id": "f6ee084a-c16d-4178-b16c-2f6ea3c55edf",
  "success": true,
  "action": "get_current_focus",
  "duration_ms": 7.12,
  "bridge": { "bridge_version": "1.0.0", "api_level": 1 },
  "nvda_status_snapshot": {
    "secure_screen": false,
    "modal_blocking": false,
    "menu_open": false
  },
  "data": {}
}
```

## Security Model

* Loopback-only binding (`127.0.0.1`).
* Token-required for `/tools` and `/action`.
* Timing-safe token comparison.
* Centralized error registry with retry semantics.

## Notes

* The bridge does not implement MCP wire protocol directly.
* Tool and output contracts are discoverable at runtime via `GET /tools`.
* Tool order in discovery output is deterministic and matches manifest order.
* Protocol schemas live in `docs/protocol/`.
