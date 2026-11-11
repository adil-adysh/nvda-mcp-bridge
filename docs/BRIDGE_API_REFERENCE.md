# NVDA HTTP Bridge API Reference (v1)

This document describes the current bridge behavior implemented in `nvda_http_bridge.py`.

* Bridge protocol: `nvda-bridge-protocol`
* Protocol version: `1.0.0`
* API level: `2` (advertises `supported_api_levels: [1, 2]`)
* Bridge docs maturity: v1 payloads below, plus the v2 additions in
  [Protocol 2](#protocol-2-api_level-2) at the end

The JSON examples in this document are v1 responses. API level 2 adds fields
without changing the v1 shape, so a v1 client keeps working; the additions are
listed at the end and specified in `docs/IMPLEMENTATION_PLAN.md` §4.1.

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
* Optional fields such as `timeout_ms` and `client` are accepted for forward compatibility.

Success envelope:

```json
{
  "request_id": "e0dc2bc6-8eb4-4f8f-a95b-6fb953953f77",
  "success": true,
  "action": "get_current_focus",
  "duration_ms": 8.41,
  "bridge": { "bridge_version": "1.0.0", "api_level": 1 },
  "nvda_status_snapshot": {
    "secure_screen": false,
    "modal_blocking": false,
    "menu_open": false
  },
  "data": {}
}
```

Error envelope:

```json
{
  "request_id": "e0dc2bc6-8eb4-4f8f-a95b-6fb953953f77",
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

The bridge exposes discoverable actions via `GET /tools`. The original surface,
still present, is:

* `get_current_focus`
* `move_focus`
* `get_navigator_object`
* `get_desktop_object`
* `read_current_line`
* `speak_text`
* `show_message`
* `send_keyboard`
* `get_window_title`
* `inspect_object`
* `walk_object_tree`
* `get_focus_lineage`
* `inspect_app_module`
* `list_loaded_addons`
* `health_snapshot`
* `restart_nvda`
* `reload_plugins`
* `get_logs`
* `execute_python`

The observation and action surface added with api level 2:

* `observe`, `locate`, `invoke`, `set_value`, `focus`, `press`, `read`,
  `navigate`, `scroll`, `dismiss`, `click_at`, `wait_for_events`

`observe` returns a projection with a `ref` on every node; `locate` searches for
controls by role and name and returns refs too. References are locators, not
indices, and a stale one is reported as `4206` rather than resolved by guessing.

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
* **`deprecated` / `supersededBy`** per tool. `get_current_focus`,
  `get_navigator_object`, `read_current_line`, `move_focus` and `send_keyboard`
  are deprecated aliases for `observe`, `observe`, `read`, `navigate` and
  `press` respectively.
* **Error `4206` `STALE_REFERENCE`** (HTTP 409): the referenced object is gone or
  expired. Call `observe` or `locate` for fresh references.
* **`GET /events?raw=1`** returns the raw event journal instead of coalesced
  transitions, for debugging.
