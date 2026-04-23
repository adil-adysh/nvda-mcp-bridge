# TYPED TOOL EXECUTION FRAMEWORK

A production-grade, type-safe, schema-driven tool execution framework for large open-source ecosystems.

## Overview

This framework enforces strict architectural contracts:

- **Every tool owns its input and output schemas** - No generic types, no implicit coercion
- **Handlers contain only business logic** - No validation, serialization, or framework concerns
- **Validation happens outside handlers** - Input validated before handler runs, output validated after
- **Execution engine controls the lifecycle** - Deterministic pipeline with fail-fast behavior
- **Registry prevents schema drift** - Schemas frozen after registration, versioning supported

## Architecture

### Type Safety Contract

```
Raw Input (dict) 
    ↓
[Input Validation]
    ↓
Typed Input Model
    ↓
[Handler Execution]
    ↓
Typed Output Model
    ↓
[Output Validation]
    ↓
Serialized Result
```

### Core Components

#### 1. **Tool Abstraction** (`tool_base.py`)

Generic base class for all tools:

```python
class Tool(ABC, Generic[InputType, OutputType]):
    def get_metadata(self) -> ToolMetadata: ...
    def get_input_schema(self) -> InputSchema[InputType]: ...
    def get_output_schema(self) -> OutputSchema[OutputType]: ...
    def execute(self, input_obj: InputType) -> OutputType: ...
```

**Contract Guarantees:**
- `execute()` receives `InputType`, never raw dict
- `execute()` returns `OutputType`, never raw dict
- Input is pre-validated before execution
- Output is post-validated after execution

#### 2. **Schema Definitions** (`tool_schema.py`)

Strict input/output validation:

```python
class InputSchema(Generic[T]):
    """Enforces:
    - All fields explicitly typed
    - No dynamic types
    - No implicit coercion
    - No extra fields allowed
    - All required fields declared
    """

class OutputSchema(Generic[T]):
    """Enforces:
    - Explicit return structure
    - Domain-specific field names only
    - No partial output
    - Handler output must exactly match schema
    """
```

#### 3. **Execution Engine** (`tool_executor.py`)

Orchestrates the validation pipeline:

```python
class ToolExecutor:
    def execute(tool: Tool, raw_input: dict) -> ExecutionResult:
        # Phase 1: Input validation
        # Phase 2: Handler execution
        # Phase 3: Output validation
        # Phase 4: Return serializable result
```

#### 4. **Versioned Registry** (`tool_registry_v2.py`)

Central, immutable tool registry:

```python
class ToolRegistry:
    """Immutable registry supporting:
    - Multiple versions of same tool
    - Schema freezing (no mutations)
    - Fast O(1) lookups by (name, version)
    - Duplicate prevention
    """
```

#### 5. **Error Model** (`tool_error.py`)

Unified error handling:

```python
class ToolExecutionError:
    error_code: ErrorCode
    tool_name: str
    version: str | None
    message: str
    details: dict[str, Any]
    root_cause: Exception | None
```

## Implementation Guide

### 1. Define Input/Output Models

Use Pydantic `BaseModel` for strict validation:

```python
from pydantic import BaseModel, Field

class MyToolInput(BaseModel):
    """Input model - all fields explicit, no extra allowed."""
    
    required_field: str = Field(..., description="...")
    optional_field: int = Field(default=0, description="...")
    
    class Config:
        extra = "forbid"  # CRITICAL: no extra fields

class MyToolOutput(BaseModel):
    """Output model - domain-specific field names."""
    
    result: str = Field(..., description="...")
    status: str = Field(..., description="...")
    
    class Config:
        extra = "forbid"
```

### 2. Create Handler Function

Pure business logic with typed signature:

```python
def my_tool_handler(input_obj: MyToolInput) -> MyToolOutput:
    """Handler signature:
    - Input: typed (NEVER raw dict)
    - Output: typed (NEVER raw dict)
    - No validation logic
    - No serialization logic
    - Raise exceptions for failures (caught by engine)
    """
    
    # Pure business logic
    result = do_work(input_obj.required_field)
    
    return MyToolOutput(
        result=result,
        status="success"
    )
```

### 3. Create Tool Implementation

Subclass `Tool[InputType, OutputType]`:

```python
from tool_base import Tool, ToolMetadata
from tool_schema import InputSchema, OutputSchema

class MyTool(Tool[MyToolInput, MyToolOutput]):
    
    def get_metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="my_tool",
            version="1.0.0",
            description="...",
            category="...",
            author="...",
            tags=("tag1", "tag2"),
        )
    
    def get_input_schema(self) -> InputSchema[MyToolInput]:
        return InputSchema(
            model=MyToolInput,
            version="1.0.0",
            allow_extra_fields=False,
            description="...",
        )
    
    def get_output_schema(self) -> OutputSchema[MyToolOutput]:
        return OutputSchema(
            model=MyToolOutput,
            version="1.0.0",
            description="...",
        )
    
    def execute(self, input_obj: MyToolInput) -> MyToolOutput:
        return my_tool_handler(input_obj)
```

### 4. Register Tool

Use the registry builder:

```python
from tool_registry_v2 import ToolRegistryBuilder

builder = ToolRegistryBuilder()
builder.register(MyTool())
builder.register(AnotherTool())
registry = builder.build()  # Immutable after build
```

### 5. Execute Tool

Use the executor:

```python
from tool_executor import ToolExecutor

executor = ToolExecutor()
result = executor.execute(tool, raw_input)

if result.success:
    print(f"Output: {result.output}")
else:
    print(f"Error: {result.error}")

# For HTTP/MCP bridges:
response = executor.execute_and_serialize(tool, raw_input)
# Returns: {"success": true, "output": {...}} or {"success": false, "error": {...}}
```

## Design Patterns

### Pattern 1: Versioning

Support multiple versions simultaneously:

```python
builder = ToolRegistryBuilder()
builder.register(MyToolV1())  # 1.0.0
builder.register(MyToolV2())  # 2.0.0
registry = builder.build()

# Get specific version
tool_v1 = registry.get("my_tool", "1.0.0")
tool_v2 = registry.get("my_tool", "2.0.0")

# Get latest version
tool_latest = registry.get("my_tool")  # Returns 2.0.0
```

### Pattern 2: Plugin Architecture

3rd-party developers follow the same contracts:

```python
class CommunityTool(Tool[InputType, OutputType]):
    """Community-contributed tool - same interface as built-in tools."""
    # Implementation...

# Validation at registration
if not validate_plugin_contract(CommunityTool):
    raise PluginError("...")

registry.register(CommunityTool())
```

### Pattern 3: Error Handling

Handlers raise domain exceptions; engine wraps them:

```python
def my_handler(input_obj: Input) -> Output:
    """Raise domain exceptions - never return error dicts."""
    
    if not is_valid(input_obj):
        raise ValueError("Validation failed")  # Engine wraps this
    
    if operation_failed():
        raise RuntimeError("Operation failed")  # Engine wraps this
    
    return Output(...)  # Success path

# Engine converts to:
# HandlerError(
#     error_code=HANDLER_EXECUTION_FAILED,
#     message="ValueError: Validation failed",
#     root_cause=<exception>
# )
```

### Pattern 4: Simplified Tool Creation

Use `TypedToolImpl` for less boilerplate:

```python
from tool_base import TypedToolImpl, ToolMetadata

tool = TypedToolImpl(
    metadata=ToolMetadata(
        name="my_tool",
        version="1.0.0",
        description="...",
        category="...",
    ),
    input_model=MyInput,
    output_model=MyOutput,
    handler=my_handler,
)
```

## Validation Rules

### Input Validation Rules

❌ REJECTED:
- Raw dicts instead of typed models
- Extra fields (unless explicitly allowed)
- Missing required fields
- Wrong field types
- Implicit type coercion

✓ ACCEPTED:
- Explicitly typed Pydantic models
- All required fields present
- No extra fields
- Exact type matches
- Models created via `model_validate()`

### Output Validation Rules

❌ REJECTED:
- Wrong output type
- Missing required fields
- Incomplete output
- None return (unless nullable in schema)
- Generic "result" or "data" fields

✓ ACCEPTED:
- Exact TypedType instance
- All required fields populated
- Domain-specific field names
- Full output returned

## Error Codes

All errors conform to `ErrorCode` enum:

```python
# Input validation errors
INVALID_INPUT_SCHEMA
INVALID_INPUT_TYPE
MISSING_REQUIRED_FIELD
EXTRA_FIELD_NOT_ALLOWED

# Tool registration errors
TOOL_NOT_FOUND
VERSION_NOT_FOUND
TOOL_REGISTRATION_CONFLICT
INCOMPLETE_TOOL_CONTRACT

# Output validation errors
INVALID_OUTPUT_TYPE
INVALID_OUTPUT_SCHEMA
MISSING_REQUIRED_OUTPUT_FIELD

# Handler errors
HANDLER_EXECUTION_FAILED
HANDLER_RAISED_EXCEPTION
HANDLER_RETURNED_NONE

# Registry errors
REGISTRY_FROZEN
SCHEMA_MUTATION_ATTEMPTED

# Plugin errors
PLUGIN_LOAD_FAILED
PLUGIN_CONTRACT_VIOLATION
```

## Testing Strategy

### Contract Tests

Test that schemas match implementation:

```python
def test_input_schema_matches_handler():
    tool = MyTool()
    schema = tool.get_input_schema()
    
    # Valid input is accepted
    valid = schema.validate({"required_field": "value"})
    assert isinstance(valid, MyToolInput)
    
    # Invalid input is rejected
    with pytest.raises(ValidationError):
        schema.validate({"extra_field": "value"})

def test_output_schema_matches_handler():
    tool = MyTool()
    schema = tool.get_output_schema()
    
    output = MyToolOutput(result="...", status="...")
    validated = schema.validate(output)
    assert validated is output
    
    with pytest.raises(ValidationError):
        schema.validate({"result": "missing_status"})
```

### Handler Tests

Test business logic independently:

```python
def test_handler_logic():
    output = my_tool_handler(
        MyToolInput(required_field="test")
    )
    
    assert isinstance(output, MyToolOutput)
    assert output.result == expected_result()
```

### Integration Tests

Test full execution pipeline:

```python
def test_execution_pipeline():
    executor = ToolExecutor()
    result = executor.execute(MyTool(), {"required_field": "test"})
    
    assert result.success
    assert isinstance(result.output, MyToolOutput)
```

## Non-Negotiable Constraints

These rules are enforced by the framework and cannot be bypassed:

1. ❌ No dynamic schema construction
2. ❌ No runtime mutation of schemas
3. ❌ No raw dict crossing execution boundary
4. ❌ No silent coercion
5. ❌ No implicit casting
6. ❌ No mixed success/error objects
7. ❌ No partial output
8. ❌ Fail fast and explicitly

## Migration from Old System

If migrating from `tool_factory/tool_base`:

1. Convert `_TOOLS_CONFIG` to Pydantic models
2. Convert executor functions to handlers
3. Create `Tool` subclasses
4. Update manifest to use new registry
5. Run contract tests to validate

```python
# OLD
tool_class = create_tool_class(config)

# NEW
class MyTool(Tool[InputType, OutputType]):
    def execute(self, input_obj: InputType) -> OutputType:
        return handler(input_obj)
```

## Best Practices

1. **Use Pydantic for all schemas** - Maximum validation power
2. **Use descriptive field names** - Never generic "result"
3. **Document with Field descriptions** - Generated for discovery
4. **Version early** - Plan schema changes ahead
5. **Test contracts** - Freeze tests for breaking changes
6. **Separate concerns** - Handler contains only business logic
7. **Raise exceptions** - Never return error dicts
8. **Keep handlers pure** - No registry access, no side effects

## Performance Characteristics

- **Tool lookup**: O(1) by (name, version)
- **Input validation**: O(n) where n = input field count (Pydantic)
- **Output validation**: O(n) where n = output field count (Pydantic)
- **Execution**: Direct function call (no reflection)
- **Registry build**: O(m log m) where m = tool count (sorting for versions)
- **Memory**: One instance per tool version, immutable after build

## Extensibility

The framework supports extensibility without breaking contracts:

- **New error codes**: Add to `ErrorCode` enum
- **Custom validators**: Create `InputSchema`/`OutputSchema` subclasses
- **Plugin loaders**: Use `validate_plugin_contract()` helper
- **Custom serialization**: Override `ExecutionResult.as_dict()`
- **Tool factories**: Factory functions can create tools dynamically

**Cannot be extended**:
- Handler signature (must be `(InputType) -> OutputType`)
- Registry mutability (must remain immutable after build)
- Schema contract (must have `get_input/output_schema()`)
- Error model (all errors must be `ToolExecutionError`)
