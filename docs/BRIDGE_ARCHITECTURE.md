# NVDA HTTP Bridge Addon - Architecture

## Overview

This addon provides a **thin HTTP bridge** that allows external processes to interact with NVDA.

It does NOT implement MCP protocol. That responsibility belongs to the standalone `nvda-mcp-server` process.

## Three-Component Architecture

```
┌─────────────────┐
│   VS Code       │
│  (MCP Client)   │
└────────┬────────┘
         │ MCP Protocol
         │ (http://127.0.0.1:8000)
         ↓
┌─────────────────────────────────┐
│  nvda-mcp-server (Standalone)   │
│  - Implements MCP protocol      │
│  - Handles tool definitions     │
│  - Validates JSON schemas       │
└────────┬────────────────────────┘
         │ Simple HTTP Bridge
         │ (http://127.0.0.1:8765)
         ↓
┌─────────────────────────────────┐
│  NVDA + This Addon              │
│  - Thin HTTP bridge layer       │
│  - Calls NVDA APIs              │
│  - Returns JSON responses       │
└─────────────────────────────────┘
         ↓
┌─────────────────────────────────┐
│  NVDA Core APIs                 │
│  - api.getFocusObject()         │
│  - speech.speakText()           │
│  - navigation APIs              │
└─────────────────────────────────┘
```

## Component Responsibilities

### 1. NVDA + This Addon (Thin Bridge)
- Runs a minimal local HTTP server on `127.0.0.1:8765`
- Accepts simple JSON requests with format:
  ```json
  {
    "action": "get_current_focus",
    "params": {}
  }
  ```
- Executes corresponding NVDA API calls
- Returns structured JSON responses
- **Does NOT**:
  - Implement MCP protocol
  - Run uvicorn or async servers
  - Define tool schemas
  - Handle complex routing logic

### 2. nvda-mcp-server (Standalone)
- Separate Poetry project running independently
- Implements official MCP protocol specification
- Registers MCP tools with proper JSON schemas
- Converts MCP tool calls to HTTP bridge actions
- Handles streaming and protocol negotiation
- Runs on `127.0.0.1:8000`

### 3. VS Code / MCP Client
- Connects to nvda-mcp-server on port 8000
- Never communicates directly with NVDA
- Treats nvda-mcp-server as any other MCP tool provider
- Unaware that NVDA exists

## Communication Flow Example

**User in VS Code**: "What is currently focused?"

1. VS Code sends MCP tool call to nvda-mcp-server
2. nvda-mcp-server validates JSON schema
3. nvda-mcp-server sends HTTP request to NVDA bridge:
   ```
   POST http://127.0.0.1:8765/action
   {
     "action": "get_current_focus",
     "params": {}
   }
   ```
4. NVDA addon receives request, checks token
5. NVDA addon calls `nvda_bridge.get_current_focus()`
6. NVDA addon returns:
   ```json
   {
     "success": true,
     "result": {
       "name": "Submit Button",
       "role": "button",
       "value": "",
       ...
     }
   }
   ```
7. nvda-mcp-server wraps response in MCP format
8. VS Code displays result to user

## Security Model

- Both servers bind to `127.0.0.1` (localhost only)
- HTTP bridge requires `X-NVDA-Bridge-Token` header
- Change `DEFAULT_BRIDGE_TOKEN` in `nvda_http_bridge.py`
- For production: use secure token exchange mechanism

## File Structure

```
nvdaMcp-bridge/
├── __init__.py              # Global plugin entry point
├── nvda_http_bridge.py      # Simple HTTP bridge (NEW)
├── nvda_bridge.py           # NVDA API wrappers
│   ├── get_current_focus()
│   ├── read_current_line()
│   ├── speak_text()
│   ├── get_window_title()
│   ├── move_focus()
│   ├── get_navigator_object()
│   ├── get_desktop_object()
│   └── show_message()
└── (mcp_server.py)          # REMOVED - now in separate nvda-mcp-server
```

## Key Design Principles

1. **Separation of Concerns**
   - Addon: Thin bridge only
   - MCP server: Protocol implementation only
   - Client: MCP consumer only

2. **Isolation**
   - If nvda-mcp-server crashes → NVDA still works
   - If NVDA crashes → nvda-mcp-server returns connection error
   - Direct failures are isolated to their components

3. **Simplicity**
   - Addon uses basic HTTP, no complex protocols
   - JSON requests/responses only
   - Easy to debug and test

4. **Extensibility**
   - Add new actions to `nvda_bridge.py`
   - Register in `nvda_http_bridge.py` actions dict
   - nvda-mcp-server automatically provides as MCP tools

## Running the System

1. **Start NVDA** (with addon installed)
   - Addon automatically starts HTTP bridge on startup
   - Logs appear in NVDA's log viewer

2. **Start nvda-mcp-server** (in parent directory)
   ```bash
   poetry run python -m nvda_mcp_server
   ```
   - MCP server starts on `127.0.0.1:8000`
   - Connects to addon's HTTP bridge on startup
   - Exposes NVDA tools via MCP protocol

3. **Connect VS Code** (or any MCP client)
   - Point to `http://127.0.0.1:8000`
   - Use NVDA tools like any other MCP tools

## Future Extensions

Currently supports request/response calls. Can evolve to:
- Event streaming (focus change notifications)
- Screen content snapshots
- Real-time accessibility tree updates
- Event subscriptions

But the bridge layer stays thin—complex logic moves to nvda-mcp-server.
