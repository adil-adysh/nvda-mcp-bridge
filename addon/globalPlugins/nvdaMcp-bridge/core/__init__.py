"""Core tool execution framework.

Provides generic Tool[InputType, OutputType] interface with schema validation,
execution pipeline, and error handling.
"""

from .base import EmptyInput, Tool, ToolMetadata, HandlerProtocol, TypedToolImpl
from .schema import InputSchema, OutputSchema
from .error import (
	ToolExecutionError,
	ValidationError,
	HandlerError,
	RegistryError,
	ErrorCode,
)
from .executor import ToolExecutor
from .registry import ToolRegistry, ToolRegistryBuilder

__all__ = [
	"EmptyInput",
	"Tool",
	"ToolMetadata",
	"HandlerProtocol",
	"TypedToolImpl",
	"InputSchema",
	"OutputSchema",
	"ToolExecutionError",
	"ValidationError",
	"HandlerError",
	"RegistryError",
	"ErrorCode",
	"ToolExecutor",
	"ToolRegistry",
	"ToolRegistryBuilder",
]
