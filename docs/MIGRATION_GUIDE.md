"""Migration Guide: Pydantic Models → Pure Python Dataclasses

This guide shows how to migrate all NVDA tool models from Pydantic to
zero-dependency dataclasses with the new validation system.

## Summary

**What we're replacing:**
- Pydantic BaseModel classes
- Pydantic Field() descriptors
- Pydantic Config classes
- 3+ MB Pydantic library dependency

**What we're using instead:**
- Standard library dataclasses (Python 3.13)
- dataclass_validator.py (400 lines, zero dependencies)
- Type hints from typing module

**Benefits:**
- ✅ Zero external dependencies
- ✅ 3+ MB smaller addon size
- ✅ No zoneinfo compatibility issues
- ✅ Faster validation (no Pydantic overhead)
- ✅ Full control over validation logic
- ✅ Works in any Python 3.13 environment

## Migration Pattern

### 1. Basic Model (No Defaults)

**BEFORE (Pydantic):**
```python
from pydantic import BaseModel, Field

class EmptyInput(BaseModel):
    pass  # or no fields
    
    class Config:
        extra = "forbid"

class MoveFocusInput(BaseModel):
    direction: str = Field(..., description="'next' or 'previous'")
    
    class Config:
        extra = "forbid"
```

**AFTER (Dataclass):**
```python
from dataclasses import dataclass

@dataclass(frozen=True)
class EmptyInput:
    '''No input required.'''
    pass

@dataclass(frozen=True)
class MoveFocusInput:
    '''Move focus to next or previous object.'''
    direction: str
```

**Notes:**
- Remove `BaseModel` inheritance
- Add `@dataclass(frozen=True)` decorator
- Remove `Field(...)` - just use type annotations
- Remove `Config` class - validation handles strict checking
- Move description to docstring

### 2. Model with Optional Fields

**BEFORE (Pydantic):**
```python
from pydantic import BaseModel, Field
from typing import Optional

class InspectObjectInput(BaseModel):
    target: str = Field(default="focus", description="'focus', 'navigator', or 'desktop'")
    include_relations: Optional[bool] = None
    
    class Config:
        extra = "forbid"
```

**AFTER (Dataclass):**
```python
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class InspectObjectInput:
    '''Inspect object with rich metadata.'''
    target: str = "focus"
    include_relations: Optional[bool] = None
```

**Notes:**
- `Field(default=X)` becomes just `= X`
- `Optional[T] = None` stays the same
- Validator handles None checking automatically

### 3. Model with default_factory (Lists/Dicts)

**BEFORE (Pydantic):**
```python
from pydantic import BaseModel, Field

class ObjectInfo(BaseModel):
    name: str
    states: list[str] = Field(default_factory=list)
    properties: dict[str, str] = Field(default_factory=dict)
    
    class Config:
        extra = "forbid"
```

**AFTER (Dataclass):**
```python
from dataclasses import dataclass, field

@dataclass(frozen=True)
class ObjectInfo:
    '''Information about a UI object.'''
    name: str
    states: list[str] = field(default_factory=list)
    properties: dict[str, str] = field(default_factory=dict)
```

**Notes:**
- Import `field` from dataclasses
- Change `Field(default_factory=...)` to `field(default_factory=...)`
- Note: lowercase `field` (dataclass), not uppercase `Field` (Pydantic)

### 4. Model with Integer Constraints

**BEFORE (Pydantic):**
```python
from pydantic import BaseModel, Field

class SendKeyboardInput(BaseModel):
    gesture: str = Field(..., description="NVDA keyboard gesture")
    repeat: int = Field(default=1, description="Number of times to repeat (1-20)")
    delay_ms: int = Field(default=0, description="Delay between repeats (0-5000ms)")
    
    class Config:
        extra = "forbid"
```

**AFTER (Dataclass):**
```python
from dataclasses import dataclass

@dataclass(frozen=True)
class SendKeyboardInput:
    '''Send a keyboard gesture through NVDA.
    
    Constraints:
        repeat: 1-20
        delay_ms: 0-5000ms
    '''
    gesture: str
    repeat: int = 1
    delay_ms: int = 0
```

**Notes:**
- Move constraints to docstring (or validate in handler)
- Pydantic's `ge`/`le` constraints can be added to validation if needed
- For this addon, constraint validation happens in tool handlers

### 5. Nested Models

**BEFORE (Pydantic):**
```python
from pydantic import BaseModel
from typing import Optional

class Position(BaseModel):
    x: int
    y: int
    
    class Config:
        extra = "forbid"

class ObjectInfo(BaseModel):
    name: str
    position: Optional[Position] = None
    
    class Config:
        extra = "forbid"
```

**AFTER (Dataclass):**
```python
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class Position:
    '''Screen position coordinates.'''
    x: int
    y: int

@dataclass(frozen=True)
class ObjectInfo:
    '''Information about a UI object.'''
    name: str
    position: Optional[Position] = None
```

**Notes:**
- Same pattern - just use dataclasses
- Validator handles nested dataclass validation automatically

## Validation Usage

### Old Way (Pydantic v1):
```python
from pydantic import ValidationError

try:
    obj = MoveFocusInput.parse_obj(data)
except ValidationError as e:
    print(e.errors())
```

### New Way (Dataclass Validator):
```python
from dataclass_validator import validate, ValidationError

try:
    obj = validate(data, MoveFocusInput)
except ValidationError as e:
    print(e.errors)  # Note: .errors not .errors()
```

### Serialization

### Old Way (Pydantic):
```python
json_data = obj.dict()
# or: json_data = obj.model_dump()  # Pydantic v2
```

### New Way (Dataclass):
```python
from dataclass_validator import to_json_dict

json_data = to_json_dict(obj)
```

## Complete Migration Example

Here's a complete before/after for a complex output model:

**BEFORE (Pydantic):**
```python
from pydantic import BaseModel, Field
from typing import Optional, Any
from datetime import datetime

class ObjectInfo(BaseModel):
    name: str
    role: str
    value: Optional[str] = None
    children_count: int = 0
    states: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)
    position: Optional[dict[str, int]] = None
    timestamp: Optional[datetime] = None
    
    class Config:
        extra = "forbid"
```

**AFTER (Dataclass):**
```python
from dataclasses import dataclass, field
from typing import Optional, Any
from datetime import datetime

@dataclass(frozen=True)
class ObjectInfo:
    '''Information about a UI object.
    
    Attributes:
        name: Object name/label
        role: ARIA/NVDA role
        value: Current value (if applicable)
        children_count: Number of child objects
        states: List of state strings
        properties: Additional properties
        position: Screen position {x, y}
        timestamp: When object was inspected
    '''
    name: str
    role: str
    value: Optional[str] = None
    children_count: int = 0
    states: list[str] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)
    position: Optional[dict[str, int]] = None
    timestamp: Optional[datetime] = None
```

## Migration Checklist

For each Pydantic model in nvda_tool_models.py:

1. [ ] Replace `class MyModel(BaseModel):` with `@dataclass(frozen=True)` + `class MyModel:`
2. [ ] Remove `from pydantic import BaseModel, Field`
3. [ ] Add `from dataclasses import dataclass, field`
4. [ ] Replace `Field(...)` with plain default values
5. [ ] Replace `Field(default_factory=X)` with `field(default_factory=X)`
6. [ ] Remove `class Config:` block
7. [ ] Move descriptions to docstrings
8. [ ] Keep type annotations exactly the same

## Files to Update

1. **nvda_tool_models.py**
   - Migrate all 35+ Pydantic models to dataclasses
   - Remove Pydantic imports
   - Keep all type annotations

2. **tool_schema.py**
   - Replace Pydantic validation with dataclass_validator
   - Update InputSchema.validate() to use validate_and_map()
   - Update OutputSchema.validate() to use validate_and_map()

3. **nvda_tools.py**
   - Update imports (if any direct Pydantic usage)
   - Validation usage automatically handled by schemas

4. **__init__.py**
   - Remove lib/ sys.path manipulation (no longer needed)

5. **buildVars.py**
   - Remove lib/**/*.py from pythonSources

6. **Delete lib/ directory**
   - Remove entire Pydantic bundle (~3 MB)

## Testing After Migration

```bash
# 1. Run validation tests
python tests\test_dataclass_validator.py

# 2. Compile all modules
python -m compileall addon\globalPlugins\nvdaMcp-bridge\

# 3. Rebuild addon
scons

# 4. Test in NVDA
# - Install nvdaMcpBridge-1.0.0.nvda-addon
# - Check NVDA log for startup
# - Test a simple tool: http://127.0.0.1:8765/v1/tools
```

## Expected Outcome

**Before Migration:**
- Addon size: ~4 MB (with Pydantic v1)
- Dependencies: Pydantic 1.10.19 + dependencies
- Compatibility: Pydantic v1/v2 version conflicts

**After Migration:**
- Addon size: ~200 KB (pure Python)
- Dependencies: None (stdlib only)
- Compatibility: Works anywhere Python 3.13 runs

## Rollback Plan

If migration fails:
1. Git revert changes
2. Restore lib/ directory from previous commit
3. Restore original Pydantic-based validation

## Support

- See migration_example.py for detailed before/after examples
- See test_dataclass_validator.py for validation patterns
- dataclass_validator.py contains all validation logic

## Next Steps

1. Start migration with nvda_tool_models.py
2. Update tool_schema.py validation
3. Test each module incrementally
4. Remove Pydantic dependency
5. Rebuild and test in NVDA
"""
