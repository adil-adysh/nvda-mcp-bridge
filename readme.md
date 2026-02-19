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
The bridge receives HTTP requests and executes the corresponding NVDA action.

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

## Endpoint

### Execute Action

```
POST /action
```

Header required:

```
X-NVDA-Bridge-Token: <token>
```

Example:

```powershell
Invoke-WebRequest `
  -Uri http://127.0.0.1:8765/action `
  -Method POST `
  -Headers @{ "X-NVDA-Bridge-Token" = "nvda-bridge-secret-change-me" } `
  -Body '{"action":"get_window_title"}' `
  -ContentType "application/json"
```

Example response:

```json
{
  "success": true,
  "result": {
    "title": "pwsh",
    "handle": 2296154,
    "className": "CASCADIA_HOSTING_WINDOW_CLASS"
  }
}
```

---

## Available Actions

* `get_current_focus`
* `read_current_line`
* `speak_text`
* `get_window_title`
* `move_focus`
* `show_message`

Requests use this format:

```json
{
  "action": "action_name",
  "arguments": {}
}
```

---

## Project Structure

```
addon/
└── globalPlugins/
    └── nvdaMcp/
        ├── __init__.py
        ├── mcp_server.py
        └── nvda_bridge.py
```

* `__init__.py` – plugin lifecycle
* `mcp_server.py` – HTTP server and routing
* `nvda_bridge.py` – NVDA action wrappers

---

## Development

To add a new action:

1. Implement the function in `nvda_bridge.py`
2. Register it in `mcp_server.py`
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
