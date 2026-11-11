"""Core tool execution framework.

Provides generic Tool[InputType, OutputType] interface with schema validation,
execution pipeline, and error handling.
"""

from .base import (
	EmptyInput,
	ExecutionContext,
	ExecutionMode,
	HandlerProtocol,
	ResultMeta,
	Tool,
	ToolMetadata,
	TypedToolImpl,
)
from .schema import InputSchema, OutputSchema, dataclass_to_json_schema
from .error import (
	PROTOCOL_CODE_FOR,
	ToolExecutionError,
	ValidationError,
	HandlerError,
	RegistryError,
	ErrorCode,
	protocol_code_for,
	protocol_code_for_serialized,
)
from .executor import ToolExecutor
from .registry import ToolRegistry, ToolRegistryBuilder

__all__ = [
	"EmptyInput",
	"ExecutionContext",
	"ExecutionMode",
	"ResultMeta",
	"Tool",
	"ToolMetadata",
	"HandlerProtocol",
	"TypedToolImpl",
	"InputSchema",
	"OutputSchema",
	"dataclass_to_json_schema",
	"PROTOCOL_CODE_FOR",
	"protocol_code_for",
	"protocol_code_for_serialized",
	"ToolExecutionError",
	"ValidationError",
	"HandlerError",
	"RegistryError",
	"ErrorCode",
	"ToolExecutor",
	"ToolRegistry",
	"ToolRegistryBuilder",
]
