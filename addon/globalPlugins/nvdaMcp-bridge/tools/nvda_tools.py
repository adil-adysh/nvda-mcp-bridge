"""NVDA MCP Bridge tool implementations using a declarative registry.

This module defines all bridge tools through a shared builder and
standardized output mapping. The refactor removes repetitive class
boilerplate while preserving tool metadata, schemas, and behavior.
"""

from __future__ import annotations

from typing import Any, Callable

from ..core import ToolMetadata, TypedToolImpl
from . import handlers as tool_handlers
from . import models


ToolHandler = Callable[[Any], Any]


ALL_NVDA_TOOLS = [
    TypedToolImpl.from_inputless(
        ToolMetadata(name="get_current_focus", version="1.0.0", description="Get information about the currently focused UI element.", category="focus"),
        models.ObjectInfo,
        tool_handlers.get_current_focus,
    ),
    TypedToolImpl.from_handler(
        ToolMetadata(name="move_focus", version="1.0.0", description="Move focus to next or previous object.", category="focus"),
        models.MoveFocusInput,
        models.MoveFocusResult,
        lambda input_obj: tool_handlers.move_focus(direction=input_obj.direction),
    ),
    TypedToolImpl.from_inputless(
        ToolMetadata(name="get_navigator_object", version="1.0.0", description="Get information about the current navigator object.", category="navigation"),
        models.ObjectInfo,
        tool_handlers.get_navigator_object,
    ),
    TypedToolImpl.from_inputless(
        ToolMetadata(name="get_desktop_object", version="1.0.0", description="Get information about the desktop object.", category="navigation"),
        models.ObjectInfo,
        tool_handlers.get_desktop_object,
    ),
    TypedToolImpl.from_inputless(
        ToolMetadata(name="read_current_line", version="1.0.0", description="Read the current line from the focused object.", category="text"),
        models.TextLineInfo,
        tool_handlers.read_current_line,
    ),
    TypedToolImpl.from_handler(
        ToolMetadata(name="speak_text", version="1.0.0", description="Speak the given text using NVDA's speech system.", category="speech"),
        models.SpeakTextInput,
        models.SpeechResult,
        lambda input_obj: tool_handlers.speak_text(text=input_obj.text),
    ),
    TypedToolImpl.from_handler(
        ToolMetadata(name="show_message", version="1.0.0", description="Show a UI message to the user.", category="ui"),
        models.ShowMessageInput,
        models.MessageResult,
        lambda input_obj: tool_handlers.show_message(message=input_obj.message),
    ),
    TypedToolImpl.from_handler(
        ToolMetadata(name="send_keyboard", version="1.0.0", description="Send a keyboard gesture through NVDA for app/addon automation.", category="ui"),
        models.SendKeyboardInput,
        models.KeyboardActionResult,
        lambda input_obj: tool_handlers.send_keyboard(
            gesture=input_obj.gesture,
            repeat=input_obj.repeat,
            delay_ms=input_obj.delay_ms,
        ),
    ),
    TypedToolImpl.from_inputless(
        ToolMetadata(name="get_window_title", version="1.0.0", description="Get the title and details of the current foreground window.", category="window"),
        models.WindowInfo,
        tool_handlers.get_window_title,
    ),
    TypedToolImpl.from_handler(
        ToolMetadata(name="inspect_object", version="1.0.0", description="Inspect focus/navigator/desktop object with rich metadata.", category="diagnostics"),
        models.InspectObjectInput,
        models.ObjectInspection,
        lambda input_obj: tool_handlers.inspect_object(
            target=input_obj.target,
            include_relations=input_obj.include_relations,
        ),
    ),
    TypedToolImpl.from_handler(
        ToolMetadata(name="walk_object_tree", version="1.0.0", description="Walk a bounded object subtree for focus/navigator/desktop.", category="diagnostics"),
        models.WalkObjectTreeInput,
        models.ObjectTreeSnapshot,
        lambda input_obj: tool_handlers.walk_object_tree(
            target=input_obj.target,
            max_depth=input_obj.max_depth,
            max_children=input_obj.max_children,
        ),
    ),
    TypedToolImpl.from_handler(
        ToolMetadata(name="get_focus_lineage", version="1.0.0", description="Get focused object with ancestor chain for hierarchy debugging.", category="diagnostics"),
        models.GetFocusLineageInput,
        models.FocusLineage,
        lambda input_obj: tool_handlers.get_focus_lineage(max_ancestors=input_obj.max_ancestors),
    ),
    TypedToolImpl.from_handler(
        ToolMetadata(name="inspect_app_module", version="1.0.0", description="Inspect app module metadata for focus/navigator/desktop source.", category="diagnostics"),
        models.InspectAppModuleInput,
        models.AppModuleInfo,
        lambda input_obj: tool_handlers.inspect_app_module(source=input_obj.source),
    ),
    TypedToolImpl.from_handler(
        ToolMetadata(name="list_loaded_addons", version="1.0.0", description="List running/available NVDA addons with metadata.", category="diagnostics"),
        models.ListLoadedAddonsInput,
        models.AddonMetadataList,
        lambda input_obj: tool_handlers.list_loaded_addons(scope=input_obj.scope),
    ),
    TypedToolImpl.from_handler(
        ToolMetadata(name="health_snapshot", version="1.0.0", description="Collect one-call diagnostics snapshot for NVDA addon debugging.", category="diagnostics"),
        models.HealthSnapshotInput,
        models.HealthSnapshot,
        lambda input_obj: tool_handlers.health_snapshot(
            include_logs=input_obj.include_logs,
            log_lines=input_obj.log_lines,
            include_tree=input_obj.include_tree,
            tree_depth=input_obj.tree_depth,
            tree_max_children=input_obj.tree_max_children,
            addon_scope=input_obj.addon_scope,
        ),
    ),
    TypedToolImpl.from_handler(
        ToolMetadata(name="restart_nvda", version="1.0.0", description="Restart NVDA with optional parameters (disable addons, debug logging).", category="system"),
        models.RestartNvdaInput,
        models.NvdaRestartResult,
        lambda input_obj: tool_handlers.restart_nvda(
            disable_addons=input_obj.disable_addons,
            debug_logging=input_obj.debug_logging,
        ),
    ),
    TypedToolImpl.from_inputless(
        ToolMetadata(name="reload_plugins", version="1.0.0", description="Reload all global plugins and app modules from scratchpad.", category="system"),
        models.ReloadResult,
        tool_handlers.reload_plugins,
    ),
    TypedToolImpl.from_handler(
        ToolMetadata(name="get_logs", version="1.0.0", description="Retrieve recent log entries from NVDA log file with optional filtering.", category="diagnostics"),
        models.GetLogsInput,
        models.LogEntries,
        lambda input_obj: tool_handlers.get_logs(
            max_lines=input_obj.max_lines,
            filter_level=input_obj.filter_level,
            use_tail=input_obj.use_tail,
        ),
    ),
    TypedToolImpl.from_handler(
        ToolMetadata(name="execute_python", version="1.0.0", description="Execute Python source inside NVDA and return console output/result.", category="developer"),
        models.ExecutePythonInput,
        models.PythonExecutionResult,
        lambda input_obj: tool_handlers.execute_python(
            code=input_obj.code,
            snapshot_vars=input_obj.snapshot_vars,
            capture_vars=input_obj.capture_vars,
        ),
    ),
]
