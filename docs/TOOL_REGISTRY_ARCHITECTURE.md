# Tool Registry Architecture

## Overview

The NVDA HTTP Bridge now uses a **Tool Registry Pattern** for managing bridge actions. This refactoring improves extensibility, maintainability, and separation of concerns without changing the external HTTP interface.

## Architecture Layers

### 1. HTTP Transport Layer (`NVDABridgeRequestHandler`)
- Handles HTTP request/response
- Validates authentication
- Parses JSON payloads
- Sends responses

### 2. Bridge Orchestration (`NVDAHTTPBridge`)
- Manages tool registry
- Coordinates main-thread execution
- Wraps results in HTTP envelope
- Logs execution timing (observability)
- Handles bridge lifecycle

### 3. Tool Registry (`ToolRegistry`)
- Stores available tools
- Validates tool parameters
- Dispatches to tool handlers (pure - no error wrapping)

### 4. Tool Implementations (`nvda_bridge.py`)
- Pure NVDA API calls
- Self-contained functions
- No HTTP/routing/error envelope logic
- Returns pure results or raises exceptions

### 5. Main Thread Executor (`_execute_on_main_thread`)
- Queues NVDA calls to main thread
- Ensures thread safety
- Prevents crashes
- Propagates exceptions (no error wrapping)

## Key Components

### Tool Class
```python
@dataclass
class Tool:
    name: str                      # Unique identifier
    handler: Callable              # Function to execute
    description: str               # Human-readable description
    required_params: List[str]     # Required parameter names
    optional_params: List[str]     # Optional parameter names
```

### ToolRegistry Class
```python
class ToolRegistry:
    def register(name, handler, description, required_params, optional_params)
    def get(name) -> Tool
    def has(name) -> bool
    def execute(name, params) -> Dict[str, Any]
    def list_tools() -> List[Dict[str, Any]]
```

## Execution Pipeline

```
HTTP Request
    ↓
Authentication Check
    ↓
JSON Parse
    ↓
Tool Lookup (Registry)
    ↓
Parameter Validation (Tool)
    ↓
Queue to Main Thread (_execute_on_main_thread)
    ↓
Execute Tool Handler [PURE RESULT or EXCEPTION]
    ↓
Bridge Wraps in HTTP Envelope {"success": bool, ...}
    ↓
Bridge Logs Execution Time
    ↓
JSON Response
```

## Adding a New Tool

### Step 1: Create Handler Function

In `nvda_bridge.py` (or a new module):

```python
def my_new_action(param1: str, param2: int = 42) -> Any:
    """
    My new action description.
    
    Args:
        param1: Required parameter
        param2: Optional parameter with default
        
    Returns:
        Result data (pure - NOT wrapped in HTTP envelope)
        
    Raises:
        ValueError: If validation fails
        RuntimeError: If execution fails
    """
    # Validate inputs
    if not param1:
        raise ValueError("param1 is required")
    
    if len(param1) > 1000:
        raise ValueError("param1 too long (max 1000 chars)")
        
    # Call NVDA APIs
    # (This function runs on main thread automatically via bridge)
    result = do_something_with_nvda(param1, param2)
    
    # Return pure result - NO error envelopes
    return result
```

**Key principles:**
- Return pure results (not `{"success": True, "result": ...}`)
- Raise exceptions for errors (not `{"error": "..."}`)
- Focus only on NVDA logic
- No HTTP/protocol awareness

### Step 2: Register the Tool

In `default_tools.py`:

```python
def create_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    
    # ... existing tools ...
    
    registry.register(
        name="my_new_action",
        handler=nvda_bridge.my_new_action,
        description="Does something useful",
        required_params=["param1"],
        optional_params=["param2"],
    )
    
    return registry
```

### Step 3: Done!

That's it. No changes to:
- HTTP routing logic
- Request handlers
- Thread execution
- Response formatting

### Testing Your New Tool

```powershell
$body = @{
    action = "my_new_action"
    params = @{
        param1 = "test"
        param2 = 100
    }
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://127.0.0.1:8765/action" `
    -Method Post `
    -Headers @{"X-NVDA-Bridge-Token" = "nvda-bridge-secret-change-me"} `
    -Body $body
```

## Benefits of This Architecture

### 1. **Extensibility**
- Add new tools without modifying core logic
- Each tool is self-contained
- Clear registration point

### 2. **Maintainability**
- Single responsibility per component
- No conditional dispatch chains
- Easy to understand each layer

### 3. **Type Safety**
- Parameter validation per tool
- Clear function signatures
- Type hints throughout

### 4. **Testability**
- Tools can be tested independently
- Mock tool registry for tests
- Isolated error handling

### 5. **Documentation**
- Tool metadata includes descriptions
- Parameter requirements explicit
- Self-documenting API

## Thread Safety

The architecture preserves thread safety:

1. **HTTP Handler** runs in background thread
2. **Tool Registry** lookup is thread-safe (frozen after init)
3. **Tool Execution** is queued to main thread via `_execute_on_main_thread`
4. **NVDA APIs** only called from main thread

## Observability

The bridge layer provides execution timing for performance monitoring:

- **Timing location**: Bridge layer only (not tools, registry, or executor)
- **Precision**: `time.perf_counter()` for accurate millisecond timing
- **Logging levels**:
  - `debug`: Successful execution < 500ms
  - `warning`: Slow execution > 500ms
  - `error`: Execution failures with full traceback
- **Metrics**: Main-thread execution time (excludes HTTP/JSON overhead)

**Why bridge layer?**
- Owns request lifecycle
- Knows tool name and timing
- Single logging authority
- Keeps lower layers clean

## Layer Responsibilities

| Layer | Return Contract | Error Handling | HTTP Awareness | Example |
|-------|----------------|----------------|----------------|---------|
| **Tool Handler** | Pure result or raise exception | `raise ValueError("...")` | ❌ None | `return {"text": "..."}` |
| **Registry** | Pure result or propagate exception | Validates params, propagates errors | ❌ None | `tool.execute(params)` |
| **Main Thread Executor** | Pure result or propagate exception | Re-raises exceptions | ❌ None | `raise exception` |
| **Bridge** | HTTP envelope | Catches & wraps in envelope | ✅ Full | `{"success": False, "error": "..."}` |
| **HTTP Handler** | HTTP response | Returns status codes | ✅ Full | `200`, `400`, `500` |

## Design Decisions

### Tool Return Contract: Pure Results

**Decision**: Tools return pure results or raise exceptions.

**Why:**
- Clean separation of domain logic from protocol
- Tools focus only on NVDA operations
- Bridge owns HTTP envelope creation
- Easier testing (no HTTP mocking needed)
- Exception-based control flow is Pythonic

**Example:**
```python
# ✅ Clean - Tool returns pure result
def get_focus():
    focus = api.getFocusObject()
    if not focus:
        raise RuntimeError("No focus available")
    return {"name": focus.name, "role": focus.role}

# ❌ Old - Tool aware of HTTP
def get_focus():
    try:
        focus = api.getFocusObject()
        if not focus:
            return {"error": "No focus available"}
        return {"success": True, "result": {...}}
    except Exception as e:
        return {"error": str(e)}
```

### Execution Timing: Bridge Layer Only

**Decision**: Log execution time only in bridge.

**Why:**
- Bridge owns request lifecycle
- Single logging authority prevents duplication
- Measures meaningful metric (NVDA action duration)
- Keeps tools, registry, and executor focused

### Registry Freezing: Safety First

**Decision**: Freeze registry after initialization.

**Why:**
- Prevents accidental runtime modifications
- Thread-safe by design (immutable after freeze)
- Detects misconfiguration early
- Clear initialization boundary

### Why Not Decorators?

We considered `@register_tool` decorators but chose explicit registration because:
- More explicit and visible
- Easier to see all tools in one place
- Avoids import-time side effects
- Better for testing with custom registries

### Why ToolRegistry Class?

A class-based registry provides:
- Encapsulation of tool storage
- Easy testing with custom registries
- Clear interface for tool management
- Future extensibility (filtering, permissions, etc.)

### Why Separate default_tools.py?

Separating tool registration from:
- **nvda_bridge.py**: Keeps it focused on NVDA API calls
- **nvda_http_bridge.py**: Keeps it focused on HTTP handling
- **tool_registry.py**: Keeps it generic and reusable

## Future Extensibility

This architecture enables future enhancements:

### Custom Tool Modules
```python
# my_custom_tools.py
from tool_registry import ToolRegistry

def register_custom_tools(registry: ToolRegistry):
    registry.register("custom_action", my_handler, ...)
```

### Tool Filtering
```python
def list_tools_by_category(category: str):
    return [t for t in registry.list_tools() if t.category == category]
```

### Permission Checks
```python
class Tool:
    permissions: List[str] = []
    
    def can_execute(self, user_permissions):
        return all(p in user_permissions for p in self.permissions)
```

### Tool Metadata
```python
class Tool:
    examples: List[Dict] = []
    version: str = "1.0"
    deprecated: bool = False
```

## Module Structure

```
addon/globalPlugins/nvdaMcp-bridge/
├── __init__.py              # Plugin initialization
├── nvda_http_bridge.py      # HTTP server & bridge
├── nvda_bridge.py           # NVDA API wrappers
├── tool_registry.py         # Tool registry pattern (NEW)
└── default_tools.py         # Tool registration (NEW)
```

## Summary

This refactoring achieves production-grade architecture:

### Code Quality
- ✅ Clean separation of concerns (5 distinct layers)
- ✅ Pure tool handlers (no HTTP awareness)
- ✅ Exception-based error flow (Pythonic)
- ✅ Type hints throughout
- ✅ Registry frozen after init (safety)

### Observability  
- ✅ Execution timing logged in bridge (perf_counter precision)
- ✅ Slow-action warnings (> 500ms threshold)
- ✅ Structured logging levels (debug/warning/error)
- ✅ Early detection of performance regressions

### Extensibility
- ✅ Add tools without modifying core logic
- ✅ Clear registration point (default_tools.py)
- ✅ Custom tool modules supported
- ✅ Future-ready for permissions/metadata

### NVDA Safety
- ✅ Thread safety preserved (main-thread execution)
- ✅ Frozen registry prevents runtime mutations
- ✅ Comprehensive error handling
- ✅ All 15 tests passing

### Architecture Maturity
**From**: "It works"  
**To**: "It's observable, stable, diagnosable, and maintainable"

This is **production infrastructure**, not a script.
