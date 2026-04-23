# NVDA MCP Bridge - Complete Architecture & Component Interactions

## System Overview

The NVDA MCP Bridge is a **three-tier architecture** that enables external applications (like VS Code) to control and query NVDA screen reader through the Model Context Protocol (MCP).

```
┌─────────────────────────────────────────────────────────────────────┐
│                        EXTERNAL CLIENTS                              │
│  (VS Code, Claude Desktop, other MCP clients)                        │
└────────────────────────────┬────────────────────────────────────────┘
                             │ MCP Protocol (JSON-RPC over stdio/SSE)
                             │ Port: 8000
                             ↓
┌─────────────────────────────────────────────────────────────────────┐
│                    TIER 1: MCP SERVER                                │
│  Process: nvda-mcp-server (Standalone Python process)               │
│  Location: Separate from NVDA                                        │
│  Responsibilities:                                                   │
│    • MCP protocol implementation (tools/resources/prompts)           │
│    • Session management                                              │
│    • Request orchestration                                           │
│    • Error translation (bridge errors → MCP errors)                 │
└────────────────────────────┬────────────────────────────────────────┘
                             │ HTTP Bridge Protocol (JSON over HTTP)
                             │ Port: 8765
                             ↓
┌─────────────────────────────────────────────────────────────────────┐
│                    TIER 2: HTTP BRIDGE                               │
│  Process: NVDA + nvdaMcp-bridge addon (this codebase)               │
│  Location: Inside NVDA process                                       │
│  Responsibilities:                                                   │
│    • HTTP server (127.0.0.1:8765)                                   │
│    • Authentication (X-NVDA-Bridge-Token header)                    │
│    • Request validation                                              │
│    • Tool routing and execution orchestration                        │
│    • Response envelope construction                                  │
│    • Thread safety coordination (HTTP → NVDA main thread)           │
└────────────────────────────┬────────────────────────────────────────┘
                             │ Thread-safe queue
                             │ (queueHandler.queueFunction)
                             ↓
┌─────────────────────────────────────────────────────────────────────┐
│                 TIER 3: NVDA MAIN THREAD                             │
│  Process: NVDA main thread                                           │
│  Responsibilities:                                                   │
│    • NVDA API execution (focus, speech, objects, etc.)              │
│    • UI automation (keyboard, mouse)                                 │
│    • State inspection (windows, addons, logs)                        │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Component Architecture (Tier 2: HTTP Bridge)

### Core Components and Data Flow

```
HTTP Request (from MCP server)
       │
       ↓
┌──────────────────────────────────────────────────────────────┐
│  1. NVDAHTTPBridge (nvda_http_bridge.py)                     │
│     • HTTPServer wrapper                                     │
│     • Request routing (GET /health, GET /tools, POST /action)│
│     • Authentication via TOKEN_HEADER                        │
│     • Response envelope construction                         │
└─────────────┬────────────────────────────────────────────────┘
              │
              ↓
┌──────────────────────────────────────────────────────────────┐
│  2. ToolRegistry (tool_registry_v2.py)                       │
│     • Immutable registry of Tool[Input, Output] objects      │
│     • Indexed by (name, version) tuples                      │
│     • Built from NVDA_TOOLS_REGISTRY at startup              │
│     • Contract validation (schemas match types)              │
└─────────────┬────────────────────────────────────────────────┘
              │
              ↓
┌──────────────────────────────────────────────────────────────┐
│  3. ToolExecutor (tool_executor.py)                          │
│     5-Phase Validation & Execution Pipeline:                 │
│     ┌────────────────────────────────────────────────┐      │
│     │ Phase 1: Input Validation                      │      │
│     │   raw dict → validate against input_schema     │      │
│     │              → typed InputModel                │      │
│     └────────────┬───────────────────────────────────┘      │
│                  ↓                                           │
│     ┌────────────────────────────────────────────────┐      │
│     │ Phase 2: Pre-execution Contract Check          │      │
│     │   Verify InputType matches input_schema.model  │      │
│     └────────────┬───────────────────────────────────┘      │
│                  ↓                                           │
│     ┌────────────────────────────────────────────────┐      │
│     │ Phase 3: Handler Execution (Main Thread)       │      │
│     │   handler.execute(typed_input) → typed_output  │      │
│     └────────────┬───────────────────────────────────┘      │
│                  ↓                                           │
│     ┌────────────────────────────────────────────────┐      │
│     │ Phase 4: Output Validation                     │      │
│     │   typed_output → validate against output_schema│      │
│     └────────────┬───────────────────────────────────┘      │
│                  ↓                                           │
│     ┌────────────────────────────────────────────────┐      │
│     │ Phase 5: Serialization                         │      │
│     │   typed_output → dict (for JSON response)      │      │
│     └────────────────────────────────────────────────┘      │
└─────────────┬────────────────────────────────────────────────┘
              │
              ↓
┌──────────────────────────────────────────────────────────────┐
│  4. MainThreadExecutor (main_thread_executor.py)             │
│     • Queues handler execution to NVDA main thread           │
│     • Blocks HTTP thread until completion                    │
│     • Propagates exceptions back to HTTP thread              │
│     • Timeout protection (default: 10 seconds)               │
│     • Uses queueHandler.queueFunction(eventQueue, callback)  │
└─────────────┬────────────────────────────────────────────────┘
              │
              ↓
┌──────────────────────────────────────────────────────────────┐
│  5. Tool Handler (nvda_tools.py)                             │
│     • Implements Tool[InputType, OutputType] interface       │
│     • execute(input: InputType) -> OutputType                │
│     • Pure business logic (no validation, no serialization)  │
│     • Calls NVDA APIs (api, ui, textInfos, etc.)            │
└──────────────────────────────────────────────────────────────┘
              │
              ↓
         NVDA APIs
    (focus, objects, speech, etc.)
```

---

## Detailed Component Responsibilities

### 1. **GlobalPlugin (__init__.py)**

**Purpose**: NVDA addon entry point and lifecycle manager

**Responsibilities**:
- Initialize when NVDA loads the addon
- Create `NVDAHTTPBridge` instance
- Start HTTP server in background daemon thread
- Handle graceful shutdown on NVDA exit
- Add bundled dependencies (Pydantic) to sys.path

**Key Code**:
```python
class GlobalPlugin(globalPluginHandler.GlobalPlugin):
    def __init__(self):
        self._bridge = nvda_http_bridge.NVDAHTTPBridge(
            host="127.0.0.1", port=8765
        )
        self._bridge_thread = threading.Thread(
            target=self._run_bridge, daemon=True
        )
        self._bridge_thread.start()
```

**Thread Model**: Runs on NVDA main thread, spawns daemon thread for HTTP server

---

### 2. **NVDAHTTPBridge (nvda_http_bridge.py)**

**Purpose**: HTTP server that exposes NVDA functionality via REST endpoints

**Endpoints**:
1. `GET /health` - Bridge health check (no auth required)
   ```json
   {
     "status": "ok",
     "bridge_version": "1.0.0",
     "api_level": 1,
     "uptime_seconds": 123.45
   }
   ```

2. `GET /tools` - List available tools (requires auth)
   ```json
   {
     "tools": [
       {
         "name": "get_current_focus",
         "version": "1.0.0",
         "category": "focus",
         "description": "Get currently focused UI element",
         "inputSchema": {...},
         "outputType": "ObjectInfo"
       }
     ]
   }
   ```

3. `POST /action` - Execute tool (requires auth)
   ```json
   // Request:
   {
     "request_id": "uuid",
     "action": "get_current_focus",
     "params": {}
   }
   
   // Success Response:
   {
     "request_id": "uuid",
     "success": true,
     "action": "get_current_focus",
     "duration_ms": 12.3,
     "bridge": {"bridge_version": "1.0.0", "api_level": 1},
     "nvda_status_snapshot": {
       "secure_screen": false,
       "modal_blocking": false,
       "menu_open": false
     },
     "data": { /* tool output */ }
   }
   ```

**Responsibilities**:
- HTTP request parsing and routing
- Token authentication (`X-NVDA-Bridge-Token` header)
- NVDA state gating (block if secure screen/modal/menu active)
- Tool resolution via ToolRegistry
- Execution orchestration via ToolExecutor
- Response envelope construction (structured success/error)
- Error code mapping (4xxx client, 5xxx server)
- Logging and diagnostics

**Thread Model**: Runs in daemon thread, delegates NVDA calls to main thread

---

### 3. **ToolRegistry & ToolRegistryBuilder (tool_registry_v2.py)**

**Purpose**: Immutable registry of all available tools

**Build Process** (at addon startup):
```python
# 1. Create builder
builder = ToolRegistryBuilder()

# 2. Register tools from NVDA_TOOLS_REGISTRY
for tool in NVDA_TOOLS_REGISTRY:
    builder.register(tool)  # Validates contract, freezes schemas

# 3. Build immutable registry
registry = builder.build()  # No further mutations allowed
```

**Responsibilities**:
- **Registration Phase**:
  - Validate tool contracts (has input_schema, output_schema, handler)
  - Check for duplicate (name, version) tuples
  - Freeze schemas to prevent runtime mutations
  - Enforce type safety (InputType == input_schema.model)

- **Runtime Phase**:
  - Resolve tool by name (latest version) or (name, version)
  - List all tools with metadata
  - Provide read-only access (MappingProxyType)

**Key Methods**:
```python
registry.get_tool(name: str) -> Tool  # Latest version
registry.get_tool_by_version(name: str, version: str) -> Tool
registry.list_tools() -> list[ToolMetadata]
```

**Invariants**:
- Immutable after build (no register/unregister at runtime)
- All tools have validated contracts
- Schemas are frozen (prevent mutations)

---

### 4. **Tool[InputType, OutputType] (tool_base.py)**

**Purpose**: Generic base class defining tool contract

**Type Parameters**:
- `InputType`: Pydantic model (e.g., `MoveFocusInput`)
- `OutputType`: Pydantic model (e.g., `MoveFocusResult`)

**Abstract Methods**:
```python
class Tool(ABC, Generic[InputType, OutputType]):
    @abstractmethod
    def get_metadata(self) -> ToolMetadata:
        """Name, version, description, category, tags"""
    
    @abstractmethod
    def get_input_schema(self) -> InputSchema[InputType]:
        """Schema for validating input"""
    
    @abstractmethod
    def get_output_schema(self) -> OutputSchema[OutputType]:
        """Schema for validating output"""
    
    @abstractmethod
    def get_handler(self) -> HandlerProtocol[InputType, OutputType]:
        """Handler function: (InputType) -> OutputType"""
```

**Contract Enforcement**:
- `InputType` MUST match `input_schema.model`
- `OutputType` MUST match `output_schema.model`
- Handler signature: `(InputType) -> OutputType`

**Example Tool**:
```python
class GetCurrentFocusTool(Tool[EmptyInput, ObjectInfo]):
    def get_metadata(self):
        return ToolMetadata(
            name="get_current_focus",
            version="1.0.0",
            description="Get focused UI element",
            category="focus"
        )
    
    def get_input_schema(self):
        return InputSchema(EmptyInput)
    
    def get_output_schema(self):
        return OutputSchema(ObjectInfo)
    
    def get_handler(self):
        return get_current_focus_handler
```

---

### 5. **InputSchema[T] & OutputSchema[T] (tool_schema.py)**

**Purpose**: Schema validation wrappers around Pydantic models

**InputSchema Responsibilities**:
- Validate raw dict → typed Pydantic model
- Enforce strict validation (no extra fields, no coercion)
- Version tracking (semantic versioning)
- Freeze schema to prevent runtime changes

**OutputSchema Responsibilities**:
- Validate typed output → ensure matches schema
- Serialize typed output → dict (for JSON)
- Type enforcement (catches handler bugs)

**Key Methods**:
```python
# InputSchema
input_schema.validate(data: dict) -> InputType
input_schema.freeze() -> None  # Prevent mutations

# OutputSchema
output_schema.validate(obj: OutputType) -> OutputType
output_schema.serialize(obj: OutputType) -> dict
output_schema.freeze() -> None
```

**Migration Note**: Will replace Pydantic with `dataclass_validator.py` for zero-dependency validation

---

### 6. **ToolExecutor (tool_executor.py)**

**Purpose**: Execute tool with strict 5-phase pipeline

**Execution Pipeline**:

```python
def execute(self, tool: Tool, raw_input: dict) -> ExecutionResult:
    # Phase 1: Input Validation
    try:
        typed_input = tool.get_input_schema().validate(raw_input)
    except ValidationError as e:
        return ExecutionResult(
            success=False,
            error=ToolExecutionError(
                error_code=ErrorCode.INVALID_INPUT,
                message=str(e)
            )
        )
    
    # Phase 2: Contract Check (compile-time safety)
    assert type(typed_input) == tool.get_input_schema().model
    
    # Phase 3: Handler Execution (on main thread)
    try:
        handler = tool.get_handler()
        typed_output = main_thread_executor.execute(
            handler, typed_input
        )
    except Exception as e:
        return ExecutionResult(
            success=False,
            error=ToolExecutionError(
                error_code=ErrorCode.HANDLER_ERROR,
                message=str(e)
            )
        )
    
    # Phase 4: Output Validation
    try:
        validated_output = tool.get_output_schema().validate(typed_output)
    except ValidationError as e:
        return ExecutionResult(
            success=False,
            error=ToolExecutionError(
                error_code=ErrorCode.INVALID_OUTPUT,
                message=f"Handler returned invalid output: {e}"
            )
        )
    
    # Phase 5: Serialization
    output_dict = tool.get_output_schema().serialize(validated_output)
    
    return ExecutionResult(success=True, output=output_dict)
```

**Fail-Fast Behavior**: Stops at first error, returns detailed error info

---

### 7. **MainThreadExecutor (main_thread_executor.py)**

**Purpose**: Execute functions on NVDA's main thread with exception propagation

**Why Needed**: 
NVDA APIs (focus, speech, UI objects) MUST run on the main thread. Calling from background thread causes crashes.

**How It Works**:
```python
def execute(self, func, *args, timeout=10.0):
    result = None
    error = None
    done = threading.Event()
    
    def _run():
        nonlocal result, error
        try:
            result = func(*args)
        except Exception as e:
            error = e
        finally:
            done.set()
    
    # Queue to NVDA main thread
    queueHandler.queueFunction(queueHandler.eventQueue, _run)
    
    # Block HTTP thread until done
    if not done.wait(timeout=timeout):
        raise TimeoutError("Operation timed out")
    
    # Propagate exception to HTTP thread
    if error:
        raise error
    
    return result
```

**Thread Safety Pattern**:
1. HTTP thread receives request
2. HTTP thread queues callback to main thread
3. HTTP thread BLOCKS waiting for completion
4. Main thread executes NVDA APIs
5. Main thread signals completion
6. HTTP thread resumes and returns response

**Timeout Protection**: Default 10 seconds, prevents hung requests

---

### 8. **Tool Handlers (nvda_tools.py)**

**Purpose**: Business logic implementations for each tool

**Handler Signature**:
```python
def handler(input: InputType) -> OutputType:
    """Pure business logic, no validation or serialization"""
```

**Example**:
```python
def get_current_focus_handler(input: EmptyInput) -> ObjectInfo:
    """Get currently focused UI element"""
    import api
    
    focus = api.getFocusObject()
    
    return ObjectInfo(
        object_id=str(id(focus)),
        class_name=focus.windowClassName,
        name=focus.name,
        role=focus.role,
        state=focus.states,
        position={
            "x": focus.location.left,
            "y": focus.location.top,
            "width": focus.location.width,
            "height": focus.location.height
        }
    )
```

**Responsibilities**:
- Implement tool-specific logic
- Call NVDA APIs (api, ui, textInfos, etc.)
- Return typed Pydantic models
- Raise exceptions for errors (caught by executor)

**What Handlers DON'T Do**:
- Validation (handled by schemas)
- Serialization (handled by executor)
- Thread management (handled by MainThreadExecutor)
- Error envelopes (handled by NVDAHTTPBridge)

---

### 9. **Tool Models (nvda_tool_models.py)**

**Purpose**: Pydantic models for all tool inputs and outputs

**Structure**:
```python
# Input Models
class EmptyInput(BaseModel):
    """No parameters needed"""
    class Config:
        extra = "forbid"

class MoveFocusInput(BaseModel):
    direction: str  # "next" or "previous"
    class Config:
        extra = "forbid"

# Output Models
class ObjectInfo(BaseModel):
    object_id: str
    class_name: str
    name: Optional[str]
    role: str
    state: str
    position: dict[str, Any]
    class Config:
        extra = "forbid"

class MoveFocusResult(BaseModel):
    success: bool
    old_object: Optional[ObjectInfo]
    new_object: Optional[ObjectInfo]
    class Config:
        extra = "forbid"
```

**Total Models**: 35+ models (19 input, 16+ output)

**Config Rules**:
- `extra = "forbid"` - Reject unknown fields
- Strong typing - No implicit coercion
- Nested models supported (ObjectInfo inside MoveFocusResult)

**Migration Plan**: Replace Pydantic with stdlib dataclasses using `dataclass_validator.py`

---

### 10. **NVDA Tools Registry (nvda_tools_registry.py)**

**Purpose**: Central list of all available tools

**Structure**:
```python
NVDA_TOOLS_REGISTRY: list[Tool] = [
    GetCurrentFocusTool(),
    MoveFocusTool(),
    GetNavigatorObjectTool(),
    # ... 16 more tools ...
]
```

**Usage**: Loaded by ToolRegistryBuilder at startup

---

### 11. **Error System (tool_error.py)**

**Purpose**: Structured error codes and exceptions

**Error Categories**:
- `4xxx` = Client errors (invalid input, auth failure)
- `5xxx` = Server errors (NVDA state, execution failure)

**Error Codes**:
```python
4001: AUTH_FAILURE
4101: INVALID_JSON
4102: INVALID_ACTION_NAME
4103: INVALID_PARAMS_SCHEMA
4201: SECURE_SCREEN_ACTIVE
4202: MODAL_DIALOG_BLOCKING
4203: MENU_OPEN_BLOCKING
5001: NVDA_COM_TIMEOUT
5003: TOOL_EXECUTION_EXCEPTION
5303: BRIDGE_INTERNAL_ERROR
```

**Error Response Structure**:
```json
{
  "code": 4103,
  "name": "INVALID_PARAMS_SCHEMA",
  "message": "Field 'direction' is required",
  "category": "validation",
  "retry_policy": "never",
  "details": {
    "field": "direction",
    "expected_type": "string"
  }
}
```

---

## Data Flow: Complete Request Lifecycle

### Example: `get_current_focus` Request

```
┌──────────────────────────────────────────────────────────────┐
│ 1. MCP Server sends HTTP request                            │
│    POST http://127.0.0.1:8765/action                        │
│    X-NVDA-Bridge-Token: secret                              │
│    Body: {                                                   │
│      "request_id": "abc-123",                               │
│      "action": "get_current_focus",                         │
│      "params": {}                                           │
│    }                                                         │
└────────────┬─────────────────────────────────────────────────┘
             │
             ↓
┌──────────────────────────────────────────────────────────────┐
│ 2. NVDAHTTPBridge receives on daemon thread                 │
│    • Parse JSON                                              │
│    • Validate request_id, action, params keys               │
│    • Check X-NVDA-Bridge-Token header                       │
│    • Extract request_id="abc-123"                           │
│    • Extract action="get_current_focus"                     │
│    • Extract params={}                                       │
└────────────┬─────────────────────────────────────────────────┘
             │
             ↓
┌──────────────────────────────────────────────────────────────┐
│ 3. Check NVDA State                                          │
│    • Is secure screen active? → No                          │
│    • Is modal dialog blocking? → No                         │
│    • Is menu open? → No                                     │
│    Verdict: PROCEED                                          │
└────────────┬─────────────────────────────────────────────────┘
             │
             ↓
┌──────────────────────────────────────────────────────────────┐
│ 4. ToolRegistry.get_tool("get_current_focus")               │
│    Returns: GetCurrentFocusTool instance                    │
│    • metadata.name = "get_current_focus"                    │
│    • metadata.version = "1.0.0"                             │
│    • input_schema = InputSchema[EmptyInput]                 │
│    • output_schema = OutputSchema[ObjectInfo]               │
│    • handler = get_current_focus_handler                    │
└────────────┬─────────────────────────────────────────────────┘
             │
             ↓
┌──────────────────────────────────────────────────────────────┐
│ 5. ToolExecutor.execute(tool, params={})                    │
│                                                              │
│    Phase 1: Input Validation                                │
│      input_schema.validate({})                              │
│      → EmptyInput() instance                                │
│                                                              │
│    Phase 2: Contract Check                                  │
│      type(EmptyInput()) == EmptyInput ✓                     │
│                                                              │
│    Phase 3: Handler Execution                               │
│      MainThreadExecutor.execute(                            │
│        get_current_focus_handler,                           │
│        EmptyInput()                                         │
│      )                                                       │
│      ┌───────────────────────────────┐                     │
│      │ Queued to NVDA main thread:   │                     │
│      │   import api                  │                     │
│      │   focus = api.getFocusObject()│                     │
│      │   return ObjectInfo(...)      │                     │
│      └───────────────────────────────┘                     │
│      → ObjectInfo(                                          │
│           object_id="12345",                                │
│           class_name="Edit",                                │
│           name="Username",                                  │
│           role="ROLE_EDITABLETEXT",                         │
│           state="STATE_FOCUSED",                            │
│           position={...}                                    │
│         )                                                   │
│                                                              │
│    Phase 4: Output Validation                               │
│      output_schema.validate(ObjectInfo(...))                │
│      → Valid ✓                                              │
│                                                              │
│    Phase 5: Serialization                                   │
│      output_schema.serialize(ObjectInfo(...))               │
│      → {                                                    │
│          "object_id": "12345",                              │
│          "class_name": "Edit",                              │
│          ...                                                │
│        }                                                    │
│                                                              │
│    Returns: ExecutionResult(success=True, output={...})     │
└────────────┬─────────────────────────────────────────────────┘
             │
             ↓
┌──────────────────────────────────────────────────────────────┐
│ 6. NVDAHTTPBridge constructs response envelope              │
│    {                                                         │
│      "request_id": "abc-123",                               │
│      "success": true,                                       │
│      "action": "get_current_focus",                         │
│      "duration_ms": 12.5,                                   │
│      "bridge": {                                            │
│        "bridge_version": "1.0.0",                           │
│        "api_level": 1                                       │
│      },                                                     │
│      "nvda_status_snapshot": {                              │
│        "secure_screen": false,                              │
│        "modal_blocking": false,                             │
│        "menu_open": false                                   │
│      },                                                     │
│      "data": {                                              │
│        "object_id": "12345",                                │
│        "class_name": "Edit",                                │
│        "name": "Username",                                  │
│        "role": "ROLE_EDITABLETEXT",                         │
│        "state": "STATE_FOCUSED",                            │
│        "position": {...}                                    │
│      }                                                      │
│    }                                                         │
└────────────┬─────────────────────────────────────────────────┘
             │
             ↓
┌──────────────────────────────────────────────────────────────┐
│ 7. HTTP Response sent to MCP Server                         │
│    Status: 200 OK                                            │
│    Content-Type: application/json                           │
│    Body: { ... envelope ... }                               │
└──────────────────────────────────────────────────────────────┘
             │
             ↓
┌──────────────────────────────────────────────────────────────┐
│ 8. MCP Server translates to MCP response                    │
│    Returns to VS Code client                                │
└──────────────────────────────────────────────────────────────┘
```

**Time Breakdown**:
- HTTP parsing: < 1ms
- Auth check: < 1ms
- Tool resolution: < 1ms
- Input validation: ~1-2ms
- Main thread queue wait: ~5-10ms
- NVDA API call: ~5-10ms
- Output validation: ~1-2ms
- Response construction: < 1ms
- **Total: ~12-25ms** (mostly waiting for main thread)

---

## Thread Safety Architecture

### Thread Model

```
┌──────────────────────────────────────────────────────────────┐
│                    NVDA Main Thread                          │
│  • UI event loop                                             │
│  • All NVDA APIs (api.*, ui.*, textInfos.*)                 │
│  • Tool handler execution                                    │
│  • GlobalPlugin.__init__ / terminate                         │
└────────────┬─────────────────────────────────────────────────┘
             │ queueHandler.queueFunction()
             │ (inter-thread communication)
             ↓
┌──────────────────────────────────────────────────────────────┐
│              HTTP Bridge Daemon Thread                       │
│  • HTTP server (HTTPServer.serve_forever)                    │
│  • Request parsing                                           │
│  • Validation (schemas, auth)                                │
│  • Response construction                                     │
│  • BLOCKS waiting for main thread execution                 │
└──────────────────────────────────────────────────────────────┘
```

### Synchronization Mechanism

**Problem**: NVDA APIs crash if called from background thread

**Solution**: `MainThreadExecutor` pattern
1. HTTP thread queues callback to main thread
2. HTTP thread waits on `threading.Event()`
3. Main thread executes callback
4. Main thread sets event (signals completion)
5. HTTP thread wakes up and returns response

**Code**:
```python
# HTTP thread:
result = main_thread_executor.execute(
    handler, typed_input, timeout=10.0
)
# ↑ Blocks here until main thread completes

# Main thread (queued):
def _run():
    result = handler(typed_input)  # Calls NVDA APIs
    done_event.set()  # Wake HTTP thread
```

**Timeout Protection**: If main thread hangs, HTTP thread times out after 10s and returns error

---

## Security Model

### Authentication

**Token-Based Authentication**:
- Header: `X-NVDA-Bridge-Token: <secret>`
- Token: Configurable via environment or config file
- Default: `"nvda-bridge-secret-change-me"` (MUST change in production)

**Endpoint Protection**:
- `GET /health` - **Public** (no auth required)
- `GET /tools` - **Protected** (requires token)
- `POST /action` - **Protected** (requires token)

**Token Validation**:
```python
def _verify_token(self, token: str) -> bool:
    expected = self._bridge_token
    return hmac.compare_digest(token, expected)
```

### NVDA State Gating

**Protected when**:
- Secure screen active (Windows login, UAC)
- Modal dialog blocking user input
- NVDA menu open

**Gating Logic**:
```python
def _check_nvda_state(self, category: str) -> tuple[bool, dict]:
    if api.isSecureScreen():
        return False, {"error_code": 4201, ...}
    
    if category in ["focus", "navigation", "ui"]:
        if globalVars.modalLevel > 0:
            return False, {"error_code": 4202, ...}
    
    return True, {}
```

**Error Response**: Returns structured error with retry policy

---

## Extension Points

### Adding a New Tool

**Steps**:
1. Define input/output Pydantic models in `nvda_tool_models.py`
2. Implement handler in `nvda_tools.py`
3. Create Tool class in `nvda_tools.py`
4. Add to `NVDA_TOOLS_REGISTRY`

**Example**:
```python
# 1. Models
class ReadTextInput(BaseModel):
    length: int
    class Config:
        extra = "forbid"

class ReadTextResult(BaseModel):
    text: str
    encoding: str
    class Config:
        extra = "forbid"

# 2. Handler
def read_text_handler(input: ReadTextInput) -> ReadTextResult:
    import api
    obj = api.getFocusObject()
    text = obj.makeTextInfo().text[:input.length]
    return ReadTextResult(text=text, encoding="utf-8")

# 3. Tool class
class ReadTextTool(Tool[ReadTextInput, ReadTextResult]):
    def get_metadata(self):
        return ToolMetadata(
            name="read_text",
            version="1.0.0",
            description="Read text from focus",
            category="text"
        )
    
    def get_input_schema(self):
        return InputSchema(ReadTextInput)
    
    def get_output_schema(self):
        return OutputSchema(ReadTextResult)
    
    def get_handler(self):
        return read_text_handler

# 4. Register
NVDA_TOOLS_REGISTRY.append(ReadTextTool())
```

**No HTTP code changes needed** - tool discovery is automatic via registry

---

## Configuration & Deployment

### Build Process

```bash
# Build addon
scons

# Produces:
# nvdaMcpBridge-1.0.0.nvda-addon (currently ~4 MB with Pydantic)
```

### Installation

```bash
# Install in NVDA
1. Open NVDA
2. NVDA menu → Tools → Manage add-ons
3. Install → Select nvdaMcpBridge-1.0.0.nvda-addon
4. Restart NVDA
```

### Verification

```powershell
# Check if bridge is running
curl http://127.0.0.1:8765/health

# Should return:
# {
#   "status": "ok",
#   "bridge_version": "1.0.0",
#   "api_level": 1,
#   "uptime_seconds": 123.45
# }
```

---

## Performance Characteristics

### Latency Breakdown

| Operation | Time | Notes |
|-----------|------|-------|
| HTTP parsing | < 1ms | Python HTTPServer |
| Auth check | < 1ms | HMAC comparison |
| Tool resolution | < 1ms | Dict lookup |
| Input validation | 1-2ms | Pydantic validation |
| Main thread queue | 5-10ms | NVDA event loop latency |
| NVDA API call | 5-50ms | Depends on API |
| Output validation | 1-2ms | Pydantic validation |
| Response construction | < 1ms | JSON serialization |
| **Total** | **12-65ms** | Mostly NVDA API time |

### Memory Footprint

| Component | Size | Notes |
|-----------|------|-------|
| Pydantic library | ~3.5 MB | Bundled in lib/ |
| Tool models | ~50 KB | 35+ Pydantic models |
| Tool handlers | ~30 KB | 19 tool implementations |
| Registry | ~10 KB | Metadata only |
| HTTP server | ~20 KB | HTTPServer instance |
| **Total** | **~3.6 MB** | **Will reduce to ~200 KB with dataclass migration** |

### Concurrency Model

- **Single HTTP server thread** - Handles one request at a time
- **No thread pool** - Simple daemon thread
- **Blocking execution** - HTTP thread blocks on main thread completion
- **Throughput**: ~20-80 requests/second (depending on tool complexity)

**Rationale**: NVDA is a desktop application, not a high-traffic server. Simplicity > throughput.

---

## Testing Strategy

### Unit Tests

```bash
# Test dataclass validator
python tests\test_dataclass_validator.py  # 14 tests

# Test tool registry
python tests\test_tool_registry_contract.py

# Test schema validation
python tests\test_schema_utils.py
```

### Integration Tests

```bash
# Test HTTP bridge manually
python test_http_bridge.ps1
```

### NVDA Runtime Tests

1. Install addon in NVDA
2. Check NVDA log for startup message
3. Hit `/health` endpoint
4. Execute sample tool via `/action`

---

## Migration: Pydantic → Dataclass

### Current State (with Pydantic v1)

- ✅ All 19 tools working
- ✅ Compilation successful
- ❌ 3.5 MB Pydantic dependency
- ❌ zoneinfo compatibility issues (Pydantic v2)

### Target State (with stdlib dataclasses)

- ✅ Zero dependencies
- ✅ ~200 KB addon size (95% reduction!)
- ✅ Works in any Python 3.13 environment
- ✅ Faster validation (no Pydantic overhead)

### Files to Migrate

1. **nvda_tool_models.py** - Replace BaseModel with @dataclass
2. **tool_schema.py** - Replace Pydantic with dataclass_validator
3. **Remove lib/ directory** - Delete Pydantic bundle

See [MIGRATION_GUIDE.md](MIGRATION_GUIDE.md) for detailed steps.

---

## Summary: Component Interaction Flow

```
User Action in VS Code
    ↓
MCP Client sends tool call
    ↓
nvda-mcp-server (MCP protocol layer)
    ↓
HTTP POST to 127.0.0.1:8765/action
    ↓
NVDAHTTPBridge (daemon thread)
    ├─→ Authenticate (token check)
    ├─→ Parse request (JSON)
    ├─→ Check NVDA state (secure screen, modal, menu)
    ├─→ ToolRegistry.get_tool(name)
    └─→ ToolExecutor.execute(tool, params)
            ├─→ InputSchema.validate(params)
            ├─→ MainThreadExecutor.execute(handler, typed_input)
            │       ↓
            │   [Queue to NVDA main thread]
            │       ↓
            │   Handler calls NVDA APIs
            │       ↓
            │   Returns typed output
            ├─→ OutputSchema.validate(output)
            └─→ OutputSchema.serialize(output)
    ↓
Response envelope constructed
    ↓
HTTP 200 OK with JSON body
    ↓
nvda-mcp-server translates to MCP response
    ↓
VS Code receives result
```

**Key Invariants**:
1. All NVDA APIs run on main thread (via MainThreadExecutor)
2. All inputs validated before execution (via InputSchema)
3. All outputs validated after execution (via OutputSchema)
4. All tools registered at startup (immutable ToolRegistry)
5. All requests authenticated (token-based)
6. All errors structured (error codes + retry policies)

---

This architecture provides:
- ✅ **Type safety** (Tool[Input, Output] contracts)
- ✅ **Thread safety** (MainThreadExecutor pattern)
- ✅ **Security** (token auth + NVDA state gating)
- ✅ **Extensibility** (add tools without HTTP changes)
- ✅ **Reliability** (fail-fast validation, structured errors)
- ✅ **Performance** (~12-65ms latency, suitable for desktop UI)
