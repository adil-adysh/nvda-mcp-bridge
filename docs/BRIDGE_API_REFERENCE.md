# NVDA HTTP Bridge - Quick Reference

This document shows how to use the NVDA HTTP Bridge endpoints.

## Endpoints

### Health Check
```
GET /health
No authentication required
```

Response:
```json
{
  "status": "ok",
  "service": "NVDA HTTP Bridge",
  "version": "1.0"
}
```

### Execute Action
```
POST /action
Header: X-NVDA-Bridge-Token: <token>
```

Request format:
```json
{
  "action": "action_name",
  "params": {
    "param1": "value1"
  }
}
```

Response on success:
```json
{
  "success": true,
  "result": { ... }
}
```

Response on error:
```json
{
  "error": "error message",
  "type": "ErrorType"
}
```

## Available Actions

### get_current_focus
Get information about the currently focused UI element.

**Params:** None

**Returns:**
```json
{
  "name": "Button Name",
  "role": "button",
  "roleText": "Button",
  "value": "",
  "description": "Description",
  "states": "...",
  "windowClassName": "...",
  "windowControlID": 123
}
```

---

### read_current_line
Read the current line from a text element.

**Params:** None

**Returns:**
```json
{
  "text": "Current line text",
  "lineNumber": 0
}
```

---

### speak_text
Speak text using NVDA's speech synthesizer.

**Params:**
- `text` (required): Text to speak

**Request:**
```json
{
  "action": "speak_text",
  "params": {
    "text": "Hello world"
  }
}
```

**Returns:**
```json
{
  "success": true,
  "spoken": "Hello world"
}
```

---

### get_window_title
Get the title of the current foreground window.

**Params:** None

**Returns:**
```json
{
  "title": "Window Title",
  "handle": 12345,
  "className": "WindowClass"
}
```

---

### move_focus
Move focus to next or previous UI element.

**Params:**
- `direction` (required): "next" or "previous"

**Request:**
```json
{
  "action": "move_focus",
  "params": {
    "direction": "next"
  }
}
```

**Returns:**
```json
{
  "success": true,
  "direction": "next",
  "newFocus": {
    "name": "New Focus Name",
    "role": "button"
  }
}
```

---

### get_navigator_object
Get information about the current navigator object.

**Params:** None

**Returns:**
```json
{
  "name": "Object Name",
  "role": "button",
  "value": "",
  "description": "..."
}
```

---

### get_desktop_object
Get information about the desktop object.

**Params:** None

**Returns:**
```json
{
  "name": "Desktop",
  "role": "pane",
  "childCount": 5
}
```

---

### show_message
Display a message to the user.

**Params:**
- `message` (required): Message text

**Request:**
```json
{
  "action": "show_message",
  "params": {
    "message": "Hello from external script"
  }
}
```

**Returns:**
```json
{
  "success": true,
  "message": "Hello from external script"
}
```

---

## Example: Using with curl

### Health check
```bash
curl http://127.0.0.1:8765/health
```

### Get current focus
```bash
curl -X POST http://127.0.0.1:8765/action \
  -H "Content-Type: application/json" \
  -H "X-NVDA-Bridge-Token: nvda-bridge-secret-change-me" \
  -d '{"action": "get_current_focus", "params": {}}'
```

### Speak text
```bash
curl -X POST http://127.0.0.1:8765/action \
  -H "Content-Type: application/json" \
  -H "X-NVDA-Bridge-Token: nvda-bridge-secret-change-me" \
  -d '{"action": "speak_text", "params": {"text": "Hello world"}}'
```

---

## Architecture Context

This HTTP bridge is the **thin layer** in the architecture:

```
VS Code (MCP Client)
    ↓ (MCP Protocol)
nvda-mcp-server (Standalone)
    ↓ (HTTP Bridge)
NVDA + This Addon
    ↓
NVDA Core APIs
```

The nvda-mcp-server process converts MCP tool calls to these HTTP bridge endpoints.

You generally won't call this bridge directly—instead use the MCP server.

But if you're testing or building a custom client, this reference shows the raw endpoint format.

---

## Security Notes

- Bridge binds to `127.0.0.1` only (localhost)
- Token required: `X-NVDA-Bridge-Token` header
- Default token: `nvda-bridge-secret-change-me`
- **Change the token in production**

See `nvda_http_bridge.py` for configuration:
```python
DEFAULT_BRIDGE_TOKEN = "nvda-bridge-secret-change-me"
```
