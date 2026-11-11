
# NVDA MCP Bridge

A thread-safe NVDA add-on that exposes NVDA internals as an MCP-compatible interface, enabling direct execution and inspection of NVDA APIs from VS Code.

---

## What this enables

* Execute Python inside a **live NVDA instance**
* Inspect NVDA internals (focus, objects, config, addons)
* Replace manual console probing with **MCP-driven inspection**
* Validate APIs before implementing them in your add-on

---

## Quick Start (Single-Package Experience)

**Goal:** Install the bridge, deploy the bundled MCP companion, and connect an MCP client (VS Code, Claude Desktop, Cursor) without manual binary builds or copying authentication tokens.

---

### 1. Install the Add-on in NVDA

1. Download the release package:
   ```
   nvdaMcpBridge-1.0.0.nvda-addon
   ```
2. Open the `.nvda-addon` file with NVDA to install it.
3. Restart NVDA when prompted.

---

### 2. Install the Bundled MCP Companion

1. In NVDA, open **NVDA Menu (`NVDA+N`) → Tools → NVDA MCP Bridge → Install or Repair MCP Companion...** (or via NVDA Settings).
2. When prompted, select **Yes**.
3. The add-on safely extracts and verifies the bundled companion binary to:
   ```
   %LOCALAPPDATA%\Programs\NVDA MCP Bridge\bin\nvda-mcp-server.exe
   ```
   *(Per-user installation — no administrator privileges required).*

---

### 3. Configure Your MCP Client Automatically

1. Open **NVDA Menu (`NVDA+N`) → Tools → NVDA MCP Bridge → Configure MCP Clients...**.
2. The configurator automatically detects supported clients (Visual Studio Code, Claude Desktop, Cursor) and safely merges the `nvda` server entry.
3. Existing settings and other MCP servers are completely preserved, and a timestamped backup (`.bak.<timestamp>`) is created before modifying any configuration.

---

### 4. Manual MCP Client Configuration (Alternative)

If configuring an MCP client manually, add the following to your client's MCP configuration:

#### Claude Desktop (`%APPDATA%\Claude\claude_desktop_config.json`)
```json
{
  "mcpServers": {
    "nvda": {
      "command": "%LOCALAPPDATA%\\Programs\\NVDA MCP Bridge\\bin\\nvda-mcp-server.exe",
      "args": []
    }
  }
}
```

#### Visual Studio Code (`%APPDATA%\Code\User\mcp.json` or `.vscode/mcp.json`)
```json
{
  "servers": {
    "nvda": {
      "type": "stdio",
      "command": "%LOCALAPPDATA%\\Programs\\NVDA MCP Bridge\\bin\\nvda-mcp-server.exe",
      "args": []
    }
  }
}
```

#### Cursor (`%USERPROFILE%\.cursor\mcp.json`)
```json
{
  "mcpServers": {
    "nvda": {
      "command": "%LOCALAPPDATA%\\Programs\\NVDA MCP Bridge\\bin\\nvda-mcp-server.exe",
      "args": []
    }
  }
}
```

> **Note on Authentication:** You do **not** need to copy or paste authentication tokens into client configurations. The companion binary automatically discovers the bridge credential generated in `%APPDATA%\nvda\nvda-mcp-bridge\token.key` using Windows user security controls.

---

### 5. Permission Controls and Developer Mode

By default, the bridge enforces a conservative, safe permission policy:
* **Safe defaults enabled:** `read_only` (focus/object inspection, window titles) and `speech_output` (`speak_text`, `cancel_speech`).
* **Input control:** `send_keyboard` requires granting the `input_control` capability.
* **NVDA Lifecycle:** `restart_nvda` and `reload_plugins` require `nvda_lifecycle`.
* **Developer Mode:** Arbitrary Python execution (`execute_python`) is disabled by default.

#### Managing Permissions in NVDA
Open **NVDA Menu → Preferences → Settings (`NVDA+Control+G`) → NVDA MCP Bridge**:
* Check/uncheck **Allow MCP Input Control**
* Check/uncheck **Allow MCP Diagnostic Inspection**
* Check/uncheck **Allow MCP NVDA Lifecycle actions**
* Set **Elevation window** to choose how long an approval lasts: 1, 5, 10, 15 or 30 minutes. The value is saved, capped at 30 minutes so elevation can never become permanent, and applies to the next approval — an approval already in progress keeps the window it was granted with.
* Click **Enable Developer Mode (…)…** to approve Python execution. A screen-reader confirmation dialog will appear explaining the security risk and stating the window you will actually get. Approving grants the developer capability atomically with an auto-expiration timer.
* Click **Disable Developer Mode** to immediately revoke execution rights.
* Click **Reset Permissions to Safe Defaults** to revoke all elevated permissions.
* Developer operations are strictly blocked on the Windows secure desktop under all circumstances.

---

### 6. Development Guide (Building from Source)

For contributors and developers building the add-on from source:

#### Prerequisites
* Python 3.11+
* Rust 1.80+ with `x86_64-pc-windows-msvc` toolchain
* SCons and Markdown (`pip install scons markdown`)
* Gettext tools (optional, for localization)

#### Build Steps
```powershell
# 1. Build Rust MCP release binary
cargo build --release --manifest-path rust_mcp_bridge/Cargo.toml

# 2. Package companion binary and generate manifest
python scripts/package_companion.py

# 3. Assemble .nvda-addon package
scons
scons pot

# 4. Validate package contents
python scripts/validate_addon_package.py
```

---

## Verify Speech and Focus Inspection (First Win)

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

Use `GET /tools` for the authoritative list, including per-tool `outputSchema`
and a `schema_hash` that changes whenever the contract does.

### Observation (start here)

* `observe` — focus, ancestors, children and dialog in one call, each with a `ref`
* `read` — text by scope (`line`, `selection`, `document`, `subtree`)
* `wait_for_events` — block until the next coalesced UI transition

### Action (by `ref`, settled)

* `invoke` — press an object's default or named action
* `set_value` — platform value pattern when available, keyboard otherwise
* `focus`, `navigate`, `scroll`
* `press` — keyboard keys, with an optional `expect` postcondition
* `click_at` — absolute pointer click, last resort
* `dismiss` — leave a dialog (needs the `dialog_control` capability)

Every action returns `result.changed`, `result.delta` and `result.settled_ms`, so
"the call ran" is distinguishable from "the UI did what was meant".

### Inspection and diagnostics

* `inspect_object`, `inspect_app_module`, `get_focus_lineage`
* `walk_object_tree`, `get_desktop_object`
* `get_window_title`, `list_loaded_addons`, `health_snapshot`, `get_logs`
* `speak_text`, `show_message`

### Development

* `execute_python`

### Deprecated (kept for compatibility)

* `get_current_focus` → `observe`
* `get_navigator_object` → `observe`
* `read_current_line` → `read`
* `move_focus` → `navigate`
* `send_keyboard` → `press`

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
