
# NVDA MCP Bridge

A thread-safe NVDA add-on that exposes NVDA internals as an MCP-compatible interface, enabling direct execution and inspection of NVDA APIs from VS Code.

---

## What this enables

* Execute Python inside a **live NVDA instance**
* Inspect NVDA internals (focus, objects, config, addons)
* Replace manual console probing with **MCP-driven inspection**
* Validate APIs before implementing them in your add-on

---

## Quick Start (VS Code + MCP)

**Goal:** Execute Python inside NVDA from VS Code and verify it works.

---

### 1. Install and build the bridge

```powershell
git clone https://github.com/adil-adysh/nvda-mcp-bridge.git
cd nvda-mcp-bridge
pip install scons markdown
scons
```

---

### 2. Install the add-on in NVDA

* Locate the generated file:

  ```
  nvdaMcpBridge-1.0.0.nvda-addon
  ```
* Open it to install in NVDA
* Restart NVDA

---

### 3. Build the MCP server

```powershell
cd rust_mcp_bridge
cargo build --release
```

* Ensure the binary is available on your `PATH`

---

### 4. Configure MCP in VS Code

Create `.vscode/mcp.json`:

```json
{
  "servers": {
    "nvda": {
      "type": "stdio",
      "command": "nvda-mcp-server",
      "env": {
        "NVDA_BRIDGE_URL": "http://127.0.0.1:8765"
      }
    }
  }
}
```

> **Note on Authentication:** The NVDA bridge generates a secure random token at `%APPDATA%\nvda\nvda-mcp-bridge\token.key` upon first start. `nvda-mcp-server` automatically detects this file when running under the same Windows user. If running across user accounts or containers, set `NVDA_BRIDGE_TOKEN` to your token value.

> **Note on Developer Mode:** By default, arbitrary code execution via `execute_python` is disabled for safety. Use standard tools (`speak_text`, `get_current_focus`, `send_keyboard`, etc.) out of the box. Elevation to developer mode cannot be authorized by remote HTTP callers using only the bridge token; it requires independent local user approval (via a time-limited local approval token in `%APPDATA%\nvda\nvda-mcp-bridge\dev_approval.key` or local approval API).

Then in VS Code:

* Run `MCP: Reset Cached Tools`
* Run `MCP: List Servers`
* Start the `nvda` server

---

### 5. Verify Speech and Focus Inspection (first win)

In the **VS Code chat interface with MCP enabled**, invoke:

Use the NVDA MCP tool `speak_text` to say: "Hello from NVDA MCP Bridge".

Or test tone generation interactively:
Use the tool `send_keyboard` with `gesture="windows+d"` to toggle the desktop.

---

### ✅ Expected result

* You hear three tones:

  * medium pitch
  * higher pitch
  * highest pitch (longer)

This confirms:

> VS Code chat → MCP → Bridge → NVDA execution is working

---

## Inspect NVDA (example)

Once execution works, inspect runtime state.

Use the NVDA MCP tool `execute_python` to run:

import addonHandler

addons = addonHandler.getRunningAddons()
[result.name for result in addons]

---

### ✅ Expected result

* A list of currently loaded add-ons returned in VS Code

---

## Exploring NVDA APIs interactively

Use Python introspection to understand NVDA internals:

Use the NVDA MCP tool `execute_python` to run:

import tones

tones.beep(100, 100)
tones.beep(200, 100)
tones.beep(400, 100)

help(tones.beep)

---

### What this gives you

* Immediate feedback from NVDA
* Function signatures via `help(...)`
* Faster understanding of internal APIs

---

## Core workflow

1. Use `execute_python` to probe NVDA internals
2. Inspect behavior and outputs
3. Validate APIs and patterns
4. Implement the same logic in your add-on

---

## Available endpoints

The bridge runs locally:

```
http://127.0.0.1:8765
```

### Endpoints

* `GET /health` — bridge status
* `GET /tools` — discover available tools
* `POST /action` — execute tool actions

---

## Example (direct HTTP)

```powershell
$token = (Get-Content "$env:APPDATA\nvda\nvda-mcp-bridge\token.key").Trim()
$headers = @{ 'X-NVDA-Bridge-Token' = $token }

$body = '{
  "request_id": "demo-1",
  "action": "speak_text",
  "params": { "text": "Hello from the bridge" }
}'

Invoke-WebRequest `
  -Uri http://127.0.0.1:8765/action `
  -Method POST `
  -Headers $headers `
  -Body $body `
  -ContentType 'application/json'
```

---

## Available tools

Use `GET /tools` for the authoritative list.

### Inspection

* `get_current_focus`
* `get_navigator_object`
* `inspect_object`
* `inspect_app_module`
* `get_focus_lineage`

### Interaction

* `speak_text`
* `read_current_line`
* `send_keyboard`
* `show_message`

### System / state

* `get_window_title`
* `list_loaded_addons`
* `health_snapshot`
* `get_logs`

### Navigation

* `move_focus`
* `walk_object_tree`
* `get_desktop_object`

### Development

* `execute_python`

---

## Architecture overview

The bridge implements a layered design:

1. **HTTP transport**

   * Local server on `127.0.0.1:8765`
   * Handles `/health`, `/tools`, `/action`

2. **Tool registry**

   * Manifest-driven tool definitions
   * JSON schema validation

3. **Main thread execution**

   * All NVDA calls routed through a `MainThreadExecutor`
   * Prevents unsafe threading access

4. **NVDA add-on runtime**

   * Runs as a global plugin inside NVDA

---

## Configuration

* Localhost-only access (`127.0.0.1`)
* Auth via `X-NVDA-Bridge-Token`
* Configure the same token in:

  * MCP server
  * VS Code environment

---

## Prerequisites

* Windows 10/11
* NVDA with add-on support
* Python ≥ 3.12
* `scons` (`pip install scons`)
* `markdown` (`pip install markdown`)
* `cargo` + `rustup` (for MCP server)
* `gettext` (optional, for docs build)

---

## Development notes

* Python ≥ 3.12
* Strict typing (`pyright`)
* Linting via `ruff`
* Add-on code:

  ```
  addon/globalPlugins/nvdaMcp-bridge/
  ```
* Tool definitions are manifest-driven
* Protocol schemas:

  ```
  docs/protocol/
  ```

---

## Companion MCP server

Located in:

```
rust_mcp_bridge/
```

Responsibilities:

* Discover tools from the bridge
* Expose them via MCP (stdio)
* Enable VS Code integration

---

## Contributing

Follow Conventional Commits:

* `feat:` new features
* `fix:` bug fixes
* `docs:` documentation
* `chore:` maintenance

Ensure:

* Tool changes are reflected in the manifest
* Behavior matches runtime discovery

---

## License

See `LICENSE`.
