# NVDA MCP Bridge

An HTTP bridge add-on that enables external applications, AI tools, and MCP servers to control NVDA and access screen reader functionality.

This add-on allows external services to:
- Speak text and send messages to users
- Read screen content and navigate UI elements
- Access window and focus information
- Retrieve NVDA logs and system status
- Reload plugins and manage NVDA state

Perfect for building AI assistants, MCP server integration, automation tools, and accessibility solutions that need to interact with NVDA.

---

## What It Does

When installed, the add-on runs in the background and creates a simple HTTP service that external applications can call to perform NVDA actions. Instead of requiring complex direct integration with NVDA's APIs, your external tool just makes web requests to perform actions like speaking text or reading the focused element.


## How It Works

The add-on runs as an NVDA plugin and listens on `http://127.0.0.1:8765`. Your external application sends HTTP requests to perform actions. The bridge safely queues all NVDA operations to the main thread, ensuring stability and preventing crashes.

```
Your Application / AI Tool
         ↓  (HTTP request)
nvda-mcp-bridge
         ↓
NVDA (safely on main thread)
         ↓
Screen Reader Actions
```

No MCP server required—any application that can make HTTP requests can use it.

---

## Getting Started

### Installation

1. Build the add-on:
   ```powershell
   git clone https://github.com/adil-adysh/nvda-mcp-bridge.git
   cd nvda-mcp-bridge
   scons
   ```

2. Install:
   - Open the generated `.nvda-addon` file
   - Restart NVDA

3. Verify:
   ```powershell
   Invoke-WebRequest http://127.0.0.1:8765/health
   ```

The listener starts automatically when NVDA loads.

---

## What You Can Do

Use the bridge to:

| Action | What It Does | Use Case |
|--------|--------------|----------|
| **speak_text** | Read text aloud | AI assistants providing spoken feedback |
| **get_current_focus** | Find what's focused | Tools that need to know the current UI element |
| **read_current_line** | Read text at cursor | Assistants extracting current document content |
| **show_message** | Display a UI notification | User alerts and confirmations |
| **get_window_title** | Get active window info | Context-aware AI responses |
| **move_focus** | Navigate UI elements | Automation that needs to interact with windows |
| **get_logs** | Retrieve NVDA logs | Debugging and monitoring |
| **reload_plugins** | Refresh add-ons | Management and troubleshooting |

---

## API Overview

### Performing Actions

Send a POST request to `http://127.0.0.1:8765/action` with:

```json
{
  "request_id": "any-unique-id",
  "action": "speak_text",
  "params": { "text": "Hello, screen reader!" }
}
```

Add authentication header:
```
X-NVDA-Bridge-Token: nvda-bridge-secret-change-me
```

Example (PowerShell):
```powershell
Invoke-WebRequest `
  -Uri http://127.0.0.1:8765/action `
  -Method POST `
  -Headers @{ "X-NVDA-Bridge-Token" = "nvda-bridge-secret-change-me" } `
  -Body '{"request_id":"demo-1","action":"speak_text","params":{"text":"Hello"}}' `
  -ContentType "application/json"
```

Success returns:
```json
{
  "success": true,
  "action": "speak_text",
  "duration_ms": 45,
  "data": { /* action-specific result */ }
}
```

Error returns:
```json
{
  "success": false,
  "action": "speak_text",
  "error": { 
    "code": 4000,
    "name": "ERROR_NAME",
    "message": "What went wrong"
  }
}
```

---

## Available Actions Reference

### speak_text
Speak text through NVDA.  
**Params:** `text` (required, string)

### get_current_focus
Get details about the focused UI element.  
**Params:** none

### read_current_line
Read the text at the current cursor position.  
**Params:** none

### show_message
Display a notification message to the user.  
**Params:** `message` (required, string)

### get_window_title
Get the active window title and handle.  
**Params:** none

### move_focus
Navigate to the next or previous UI element.  
**Params:** `direction` (required: `"next"` or `"previous"`)

### get_navigator_object
Get details about the navigator object.  
**Params:** none

### get_desktop_object
Get information about the desktop.  
**Params:** none

### reload_plugins
Reload global plugins and app modules (for development/testing).  
**Params:** none

### restart_nvda
Restart NVDA.  
**Params:** `disable_addons` (optional, bool), `debug_logging` (optional, bool)

### get_logs
Retrieve recent NVDA log entries.  
**Params:** `max_lines` (optional, int, default 100), `filter_level` (optional, filters by severity)

---

## For Developers

### Building from Source

Installation requires Python 3.12+ and SCons. See [Getting Started](#getting-started) section for detailed build instructions.

To modify the add-on:

```powershell
git clone https://github.com/adil-adysh/nvda-mcp-bridge.git
cd nvda-mcp-bridge
pip install scons  # if not already installed
scons
```

### Contributing

The add-on uses a tool registry pattern for organizing actions:

1. Implement NVDA functionality in [addon/globalPlugins/nvdaMcp-bridge/nvda_bridge](addon/globalPlugins/nvdaMcp-bridge/nvda_bridge)
2. Add a typed handler in [addon/globalPlugins/nvdaMcp-bridge/tool_handlers.py](addon/globalPlugins/nvdaMcp-bridge/tool_handlers.py)
3. Register the tool explicitly in [addon/globalPlugins/nvdaMcp-bridge/tool_manifest.py](addon/globalPlugins/nvdaMcp-bridge/tool_manifest.py)
3. All NVDA API calls are automatically queued to the main thread for safety
4. Return JSON-serializable data from your action

---

## Requirements

* NVDA 2026.1 or later
* Python 3.12+ (for building)
* SCons (install with `pip install scons`)

---

## Limitations

* Localhost only (no remote access)
* Single HTTP listener per NVDA instance
* Default authentication token should be changed for security
* No built-in MCP protocol implementation (use a separate MCP server if needed)

---

## License

This project is licensed under the GNU General Public License v2.0. See [LICENSE](LICENSE) and [COPYING.txt](COPYING.txt) for details.

---

## Technical Details

For detailed protocol specifications, JSON schemas, and API reference documentation, see:
* [docs/protocol/PROTOCOL.md](docs/protocol/PROTOCOL.md) - Full protocol contract
* [docs/BRIDGE_ARCHITECTURE.md](docs/BRIDGE_ARCHITECTURE.md) - System design and internals
* [docs/BRIDGE_API_REFERENCE.md](docs/BRIDGE_API_REFERENCE.md) - Detailed API documentation
* [docs/protocol/*.schema.json](docs/protocol/) - Machine-readable schemas
  