# nvda-mcp-bridge

An NVDA add-on that exposes a local HTTP interface intended for use with an MCP server.

The add-on runs inside NVDA and listens on:

```
http://127.0.0.1:8765
```

It provides a small JSON-based API that a separate MCP server process can call to access selected NVDA functionality.

---

## Purpose

`nvda-mcp-bridge`:

* Runs inside NVDA as a global plugin
* Exposes a localhost-only HTTP endpoint
* Allows an external MCP server to invoke NVDA actions
* Dispatches NVDA API calls to the main thread

It does not implement the MCP protocol itself.
It is designed to be used by an MCP server running separately.

---

## Architecture

```
LLM / Client
      ↓
MCP Server (separate process)
      ↓ HTTP (localhost)
nvda-mcp-bridge (inside NVDA)
      ↓
NVDA APIs
```

The MCP server handles protocol and tool exposure.
The bridge receives HTTP requests and runs the corresponding NVDA action.

---

## Requirements

* NVDA 2026.1 or later
* Python 3.7+ (for building)
* SCons (`pip install scons`)
* A compatible MCP server

---

## Build

```powershell
git clone https://github.com/adil-adysh/nvda-mcp-bridge.git
cd nvda-mcp-bridge
scons
```

This generates a `.nvda-addon` package.

---

## Installation

1. Build the add-on.
2. Open the generated `.nvda-addon` file.
3. Restart NVDA.

The HTTP listener starts when NVDA loads the add-on.

---

## API Endpoints

### GET /health

Unauthenticated health check.

### GET /tools

Returns discovered tools and metadata.

Requires header:

```
X-NVDA-Bridge-Token: <token>
```

### POST /action

Runs one action.

Requires header:

```
X-NVDA-Bridge-Token: <token>
```

Request body:

```json
{
  "action": "action_name",
  "params": {}
}
```

Notes:

* `action` is required.
* `params` is optional and defaults to `{}`.
* `params` must be a JSON object when provided.

Example:

```powershell
Invoke-WebRequest `
  -Uri http://127.0.0.1:8765/action `
  -Method POST `
  -Headers @{ "X-NVDA-Bridge-Token" = "nvda-bridge-secret-change-me" } `
  -Body '{"action":"get_window_title","params":{}}' `
  -ContentType "application/json"
```

Success envelope:

```json
{
  "success": true,
  "action": "get_window_title",
  "duration_ms": 12.34,
  "result": {
    "title": "pwsh",
    "handle": 2296154,
    "className": "CASCADIA_HOSTING_WINDOW_CLASS"
  }
}
```

Error envelope:

```json
{
  "success": false,
  "action": "move_focus",
  "duration_ms": 1.23,
  "error": "Missing required parameter: direction",
  "error_type": "validation"
}
```

---

## Actions and Params

* `get_current_focus`  
  Description: Get information about the currently focused UI element.  
  Params: none.

* `read_current_line`  
  Description: Read the current line from the focused object.  
  Params: none.

* `speak_text`  
  Description: Speak text using NVDA.  
  Required params: `text` (string).

* `get_window_title`  
  Description: Get foreground window title/details.  
  Params: none.

* `move_focus`  
  Description: Move focus to next or previous object.  
  Required params: `direction` (string: `next` or `previous`).

* `get_navigator_object`  
  Description: Get current navigator object details.  
  Params: none.

* `get_desktop_object`  
  Description: Get desktop object details.  
  Params: none.

* `show_message`  
  Description: Show a UI message.  
  Required params: `message` (string).

* `reload_plugins`  
  Description: Reload global plugins and app modules.  
  Params: none.

* `restart_nvda`  
  Description: Restart NVDA.  
  Optional params: `disable_addons` (bool), `debug_logging` (bool).

* `get_logs`  
  Description: Read recent NVDA log lines.  
  Optional params: `max_lines` (int, default 100), `filter_level` (string or null), `use_tail` (bool, default true).

---

## Project Structure

```
addon/
└── globalPlugins/
    └── nvdaMcp-bridge/
        ├── __init__.py
        ├── nvda_http_bridge.py
        ├── nvda_bridge.py
        ├── tool_registry.py
        └── tools/
            └── builtin_tools.py
```

* `__init__.py` – plugin lifecycle
* `nvda_http_bridge.py` – HTTP server and routing
* `nvda_bridge.py` – NVDA action wrappers
* `tool_registry.py` / `tools/` – tool discovery and execution

---

## Development

To add a new action:

1. Implement the function in `nvda_bridge.py`
2. Add a corresponding tool in `tools/builtin_tools.py`
3. Ensure NVDA API access occurs on the main thread
4. Return JSON-serializable data

---

## Limitations

* Localhost only
* Single HTTP listener
* Static token unless modified in code
* No built-in MCP protocol implementation

---

## License

GPL v2
[https://www.gnu.org/licenses/gpl-2.0.html](https://www.gnu.org/licenses/gpl-2.0.html)
