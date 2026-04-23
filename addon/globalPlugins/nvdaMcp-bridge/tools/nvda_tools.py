"""NVDA MCP Bridge Tool implementations using new architecture.

All 19 tools migrated from old ToolSpec to new Tool[Input, Output] interface.
"""

from __future__ import annotations

from . import handlers as tool_handlers  # Reuse existing handlers
from ..core import Tool, ToolMetadata
from ..core import InputSchema, OutputSchema
from . import models


# ============================================================================
# HELPER
# ============================================================================


def _schema(model_class):
	"""Create schema from model class."""
	return InputSchema(model_class, allow_extra_fields=False)


# ============================================================================
# FOCUS TOOLS
# ============================================================================


class GetCurrentFocusTool(Tool[models.EmptyInput, models.ObjectInfo]):
	"""Get information about the currently focused UI element."""

	def get_metadata(self) -> ToolMetadata:
		return ToolMetadata(
			name="get_current_focus",
			version="1.0.0",
			description="Get information about the currently focused UI element.",
			category="focus",
		)

	def _create_input_schema(self) -> InputSchema:
		return InputSchema(models.EmptyInput)

	def _create_output_schema(self) -> OutputSchema:
		return OutputSchema(models.ObjectInfo)

	def execute(self, input_obj: models.EmptyInput) -> models.ObjectInfo:
		data = tool_handlers.get_current_focus()
		# Filter to only fields defined in the ObjectInfo model/API contract
		return models.ObjectInfo(
			name=data["name"],
			role=data["role"],
			roleText=data["roleText"],
			value=data["value"],
			description=data["description"],
			states=data["states"],
		)


class MoveFocusTool(Tool[models.MoveFocusInput, models.MoveFocusResult]):
	"""Move focus to next or previous object."""

	def get_metadata(self) -> ToolMetadata:
		return ToolMetadata(
			name="move_focus",
			version="1.0.0",
			description="Move focus to next or previous object.",
			category="focus",
		)

	def _create_input_schema(self) -> InputSchema:
		return InputSchema(models.MoveFocusInput)

	def _create_output_schema(self) -> OutputSchema:
		return OutputSchema(models.MoveFocusResult)

	def execute(self, input_obj: models.MoveFocusInput) -> models.MoveFocusResult:
		data = tool_handlers.move_focus(direction=input_obj.direction)
		return models.MoveFocusResult(**data)


# ============================================================================
# NAVIGATION TOOLS
# ============================================================================


class GetNavigatorObjectTool(Tool[models.EmptyInput, models.ObjectInfo]):
	"""Get information about the current navigator object."""

	def get_metadata(self) -> ToolMetadata:
		return ToolMetadata(
			name="get_navigator_object",
			version="1.0.0",
			description="Get information about the current navigator object.",
			category="navigation",
		)

	def _create_input_schema(self) -> InputSchema:
		return InputSchema(models.EmptyInput)

	def _create_output_schema(self) -> OutputSchema:
		return OutputSchema(models.ObjectInfo)

	def execute(self, input_obj: models.EmptyInput) -> models.ObjectInfo:
		data = tool_handlers.get_navigator_object()
		# Filter to only fields defined in the ObjectInfo model/API contract
		return models.ObjectInfo(
			name=data["name"],
			role=data["role"],
			roleText=data["roleText"],
			value=data["value"],
			description=data["description"],
			states=data["states"],
		)


class GetDesktopObjectTool(Tool[models.EmptyInput, models.ObjectInfo]):
	"""Get information about the desktop object."""

	def get_metadata(self) -> ToolMetadata:
		return ToolMetadata(
			name="get_desktop_object",
			version="1.0.0",
			description="Get information about the desktop object.",
			category="navigation",
		)

	def _create_input_schema(self) -> InputSchema:
		return InputSchema(models.EmptyInput)

	def _create_output_schema(self) -> OutputSchema:
		return OutputSchema(models.ObjectInfo)

	def execute(self, input_obj: models.EmptyInput) -> models.ObjectInfo:
		data = tool_handlers.get_desktop_object()
		# Filter to only fields defined in the ObjectInfo model/API contract
		return models.ObjectInfo(
			name=data["name"],
			role=data["role"],
			roleText=data["roleText"],
			value=data["value"],
			description=data["description"],
			states=data["states"],
		)


# ============================================================================
# TEXT TOOLS
# ============================================================================


class ReadCurrentLineTool(Tool[models.EmptyInput, models.TextLineInfo]):
	"""Read the current line from the focused object."""

	def get_metadata(self) -> ToolMetadata:
		return ToolMetadata(
			name="read_current_line",
			version="1.0.0",
			description="Read the current line from the focused object.",
			category="text",
		)

	def _create_input_schema(self) -> InputSchema:
		return InputSchema(models.EmptyInput)

	def _create_output_schema(self) -> OutputSchema:
		return OutputSchema(models.TextLineInfo)

	def execute(self, input_obj: models.EmptyInput) -> models.TextLineInfo:
		data = tool_handlers.read_current_line()
		return models.TextLineInfo(**data)


# ============================================================================
# SPEECH & UI TOOLS
# ============================================================================


class SpeakTextTool(Tool[models.SpeakTextInput, models.SpeechResult]):
	"""Speak the given text using NVDA's speech system."""

	def get_metadata(self) -> ToolMetadata:
		return ToolMetadata(
			name="speak_text",
			version="1.0.0",
			description="Speak the given text using NVDA's speech system.",
			category="speech",
		)

	def _create_input_schema(self) -> InputSchema:
		return InputSchema(models.SpeakTextInput)

	def _create_output_schema(self) -> OutputSchema:
		return OutputSchema(models.SpeechResult)

	def execute(self, input_obj: models.SpeakTextInput) -> models.SpeechResult:
		data = tool_handlers.speak_text(text=input_obj.text)
		return models.SpeechResult(**data)


class ShowMessageTool(Tool[models.ShowMessageInput, models.MessageResult]):
	"""Show a UI message to the user."""

	def get_metadata(self) -> ToolMetadata:
		return ToolMetadata(
			name="show_message",
			version="1.0.0",
			description="Show a UI message to the user.",
			category="ui",
		)

	def _create_input_schema(self) -> InputSchema:
		return InputSchema(models.ShowMessageInput)

	def _create_output_schema(self) -> OutputSchema:
		return OutputSchema(models.MessageResult)

	def execute(self, input_obj: models.ShowMessageInput) -> models.MessageResult:
		data = tool_handlers.show_message(message=input_obj.message)
		return models.MessageResult(**data)


class SendKeyboardTool(Tool[models.SendKeyboardInput, models.KeyboardActionResult]):
	"""Send a keyboard gesture through NVDA for app/addon automation."""

	def get_metadata(self) -> ToolMetadata:
		return ToolMetadata(
			name="send_keyboard",
			version="1.0.0",
			description="Send a keyboard gesture through NVDA for app/addon automation.",
			category="ui",
		)

	def _create_input_schema(self) -> InputSchema:
		return InputSchema(models.SendKeyboardInput)

	def _create_output_schema(self) -> OutputSchema:
		return OutputSchema(models.KeyboardActionResult)

	def execute(self, input_obj: models.SendKeyboardInput) -> models.KeyboardActionResult:
		data = tool_handlers.send_keyboard(
			gesture=input_obj.gesture, repeat=input_obj.repeat, delay_ms=input_obj.delay_ms
		)
		return models.KeyboardActionResult(**data)


class GetWindowTitleTool(Tool[models.EmptyInput, models.WindowInfo]):
	"""Get the title and details of the current foreground window."""

	def get_metadata(self) -> ToolMetadata:
		return ToolMetadata(
			name="get_window_title",
			version="1.0.0",
			description="Get the title and details of the current foreground window.",
			category="window",
		)

	def _create_input_schema(self) -> InputSchema:
		return InputSchema(models.EmptyInput)

	def _create_output_schema(self) -> OutputSchema:
		return OutputSchema(models.WindowInfo)

	def execute(self, input_obj: models.EmptyInput) -> models.WindowInfo:
		data = tool_handlers.get_window_title()
		return models.WindowInfo(**data)


# ============================================================================
# DIAGNOSTICS TOOLS
# ============================================================================


class InspectObjectTool(Tool[models.InspectObjectInput, models.ObjectInspection]):
	"""Inspect focus/navigator/desktop object with rich metadata."""

	def get_metadata(self) -> ToolMetadata:
		return ToolMetadata(
			name="inspect_object",
			version="1.0.0",
			description="Inspect focus/navigator/desktop object with rich metadata.",
			category="diagnostics",
		)

	def _create_input_schema(self) -> InputSchema:
		return InputSchema(models.InspectObjectInput)

	def _create_output_schema(self) -> OutputSchema:
		return OutputSchema(models.ObjectInspection)

	def execute(self, input_obj: models.InspectObjectInput) -> models.ObjectInspection:
		data = tool_handlers.inspect_object(
			target=input_obj.target, include_relations=input_obj.include_relations
		)
		return models.ObjectInspection(**data)


class WalkObjectTreeTool(Tool[models.WalkObjectTreeInput, models.ObjectTreeSnapshot]):
	"""Walk a bounded object subtree for focus/navigator/desktop."""

	def get_metadata(self) -> ToolMetadata:
		return ToolMetadata(
			name="walk_object_tree",
			version="1.0.0",
			description="Walk a bounded object subtree for focus/navigator/desktop.",
			category="diagnostics",
		)

	def _create_input_schema(self) -> InputSchema:
		return InputSchema(models.WalkObjectTreeInput)

	def _create_output_schema(self) -> OutputSchema:
		return OutputSchema(models.ObjectTreeSnapshot)

	def execute(self, input_obj: models.WalkObjectTreeInput) -> models.ObjectTreeSnapshot:
		data = tool_handlers.walk_object_tree(
			target=input_obj.target, max_depth=input_obj.max_depth, max_children=input_obj.max_children
		)
		return models.ObjectTreeSnapshot(**data)


class GetFocusLineageTool(Tool[models.GetFocusLineageInput, models.FocusLineage]):
	"""Get focused object with ancestor chain for hierarchy debugging."""

	def get_metadata(self) -> ToolMetadata:
		return ToolMetadata(
			name="get_focus_lineage",
			version="1.0.0",
			description="Get focused object with ancestor chain for hierarchy debugging.",
			category="diagnostics",
		)

	def _create_input_schema(self) -> InputSchema:
		return InputSchema(models.GetFocusLineageInput)

	def _create_output_schema(self) -> OutputSchema:
		return OutputSchema(models.FocusLineage)

	def execute(self, input_obj: models.GetFocusLineageInput) -> models.FocusLineage:
		data = tool_handlers.get_focus_lineage(max_ancestors=input_obj.max_ancestors)
		return models.FocusLineage(**data)


class InspectAppModuleTool(Tool[models.InspectAppModuleInput, models.AppModuleInfo]):
	"""Inspect app module metadata for focus/navigator/desktop source."""

	def get_metadata(self) -> ToolMetadata:
		return ToolMetadata(
			name="inspect_app_module",
			version="1.0.0",
			description="Inspect app module metadata for focus/navigator/desktop source.",
			category="diagnostics",
		)

	def _create_input_schema(self) -> InputSchema:
		return InputSchema(models.InspectAppModuleInput)

	def _create_output_schema(self) -> OutputSchema:
		return OutputSchema(models.AppModuleInfo)

	def execute(self, input_obj: models.InspectAppModuleInput) -> models.AppModuleInfo:
		data = tool_handlers.inspect_app_module(source=input_obj.source)
		return models.AppModuleInfo(**data)


class ListLoadedAddonsTool(Tool[models.ListLoadedAddonsInput, models.AddonMetadataList]):
	"""List running/available NVDA addons with metadata."""

	def get_metadata(self) -> ToolMetadata:
		return ToolMetadata(
			name="list_loaded_addons",
			version="1.0.0",
			description="List running/available NVDA addons with metadata.",
			category="diagnostics",
		)

	def _create_input_schema(self) -> InputSchema:
		return InputSchema(models.ListLoadedAddonsInput)

	def _create_output_schema(self) -> OutputSchema:
		return OutputSchema(models.AddonMetadataList)

	def execute(self, input_obj: models.ListLoadedAddonsInput) -> models.AddonMetadataList:
		data = tool_handlers.list_loaded_addons(scope=input_obj.scope)
		return models.AddonMetadataList(**data)


class HealthSnapshotTool(Tool[models.HealthSnapshotInput, models.HealthSnapshot]):
	"""Collect one-call diagnostics snapshot for NVDA addon debugging."""

	def get_metadata(self) -> ToolMetadata:
		return ToolMetadata(
			name="health_snapshot",
			version="1.0.0",
			description="Collect one-call diagnostics snapshot for NVDA addon debugging.",
			category="diagnostics",
		)

	def _create_input_schema(self) -> InputSchema:
		return InputSchema(models.HealthSnapshotInput)

	def _create_output_schema(self) -> OutputSchema:
		return OutputSchema(models.HealthSnapshot)

	def execute(self, input_obj: models.HealthSnapshotInput) -> models.HealthSnapshot:
		data = tool_handlers.health_snapshot(
			include_logs=input_obj.include_logs,
			log_lines=input_obj.log_lines,
			include_tree=input_obj.include_tree,
			tree_depth=input_obj.tree_depth,
			tree_max_children=input_obj.tree_max_children,
			addon_scope=input_obj.addon_scope,
		)
		return models.HealthSnapshot(**data)


# ============================================================================
# SYSTEM TOOLS
# ============================================================================


class RestartNvdaTool(Tool[models.RestartNvdaInput, models.NvdaRestartResult]):
	"""Restart NVDA with optional parameters (disable addons, debug logging)."""

	def get_metadata(self) -> ToolMetadata:
		return ToolMetadata(
			name="restart_nvda",
			version="1.0.0",
			description="Restart NVDA with optional parameters (disable addons, debug logging).",
			category="system",
		)

	def _create_input_schema(self) -> InputSchema:
		return InputSchema(models.RestartNvdaInput)

	def _create_output_schema(self) -> OutputSchema:
		return OutputSchema(models.NvdaRestartResult)

	def execute(self, input_obj: models.RestartNvdaInput) -> models.NvdaRestartResult:
		data = tool_handlers.restart_nvda(
			disable_addons=input_obj.disable_addons, debug_logging=input_obj.debug_logging
		)
		return models.NvdaRestartResult(**data)


class ReloadPluginsTool(Tool[models.EmptyInput, models.ToolResponse]):
	"""Reload all global plugins and app modules from scratchpad."""

	def get_metadata(self) -> ToolMetadata:
		return ToolMetadata(
			name="reload_plugins",
			version="1.0.0",
			description="Reload all global plugins and app modules from scratchpad.",
			category="system",
		)

	def _create_input_schema(self) -> InputSchema:
		return InputSchema(models.EmptyInput)

	def _create_output_schema(self) -> OutputSchema:
		return OutputSchema(models.ToolResponse)

	def execute(self, input_obj: models.EmptyInput) -> models.ToolResponse:
		data = tool_handlers.reload_plugins()
		return models.ToolResponse(**data)


class GetLogsTool(Tool[models.GetLogsInput, models.LogEntries]):
	"""Retrieve recent log entries from NVDA log file with optional filtering."""

	def get_metadata(self) -> ToolMetadata:
		return ToolMetadata(
			name="get_logs",
			version="1.0.0",
			description="Retrieve recent log entries from NVDA log file with optional filtering.",
			category="diagnostics",
		)

	def _create_input_schema(self) -> InputSchema:
		return InputSchema(models.GetLogsInput)

	def _create_output_schema(self) -> OutputSchema:
		return OutputSchema(models.LogEntries)

	def execute(self, input_obj: models.GetLogsInput) -> models.LogEntries:
		data = tool_handlers.get_logs(
			max_lines=input_obj.max_lines, filter_level=input_obj.filter_level, use_tail=input_obj.use_tail
		)
		return models.LogEntries(**data)


# ============================================================================
# DEVELOPER TOOLS
# ============================================================================


class ExecutePythonTool(Tool[models.ExecutePythonInput, models.PythonExecutionResult]):
	"""Execute Python source inside NVDA and return console output/result."""

	def get_metadata(self) -> ToolMetadata:
		return ToolMetadata(
			name="execute_python",
			version="1.0.0",
			description="Execute Python source inside NVDA and return console output/result.",
			category="developer",
		)

	def _create_input_schema(self) -> InputSchema:
		return InputSchema(models.ExecutePythonInput)

	def _create_output_schema(self) -> OutputSchema:
		return OutputSchema(models.PythonExecutionResult)

	def execute(self, input_obj: models.ExecutePythonInput) -> models.PythonExecutionResult:
		data = tool_handlers.execute_python(
			code=input_obj.code, snapshot_vars=input_obj.snapshot_vars, capture_vars=input_obj.capture_vars
		)
		return models.PythonExecutionResult(**data)


# ============================================================================
# TOOL REGISTRY (all 19 tools)
# ============================================================================

ALL_NVDA_TOOLS = [
	GetCurrentFocusTool(),
	MoveFocusTool(),
	GetNavigatorObjectTool(),
	GetDesktopObjectTool(),
	ReadCurrentLineTool(),
	SpeakTextTool(),
	ShowMessageTool(),
	SendKeyboardTool(),
	GetWindowTitleTool(),
	InspectObjectTool(),
	WalkObjectTreeTool(),
	GetFocusLineageTool(),
	InspectAppModuleTool(),
	ListLoadedAddonsTool(),
	HealthSnapshotTool(),
	RestartNvdaTool(),
	ReloadPluginsTool(),
	GetLogsTool(),
	ExecutePythonTool(),
]
