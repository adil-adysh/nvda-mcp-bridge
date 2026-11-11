"""NVDA Bridge - Unified API for NVDA accessibility automation.

This package provides a comprehensive set of tools for interacting with NVDA's
UI, getting focus information, executing code, and gathering diagnostics.

All functions are safe to call from NVDA's main thread and return JSON-serializable dicts.
"""

# Re-export validators and serializers
from .validators import (
	validate_target,
	validate_scope,
	validate_bounded_int,
	validate_log_filter_level,
	validate_non_empty_string,
	validate_code_string,
	validate_string_list,
)
from .serializers import serialize_exception, serialize_exceptions

# Re-export object inspection functions
from .objects import (
	inspect_object,
	walk_object_tree,
	get_navigator_object,
	get_desktop_object,
	inspect_app_module,
)

# Re-export focus functions
from .focus import (
	get_current_focus,
	move_focus,
	get_focus_lineage,
)

# Re-export text functions
from .text import (
	read_current_line,
	speak_text,
)

# Re-export UI functions
from .ui import (
	send_keyboard,
	send_single_gesture,
	resolve_gesture,
	get_window_title,
	show_message,
)

# Re-export system functions
from .system import (
	restart_nvda,
	reload_plugins,
	execute_python,
)

# Re-export diagnostic functions
from .diagnostics import (
	health_snapshot,
	list_loaded_addons,
	get_logs,
)

__all__ = [
	# Validators
	"validate_target",
	"validate_scope",
	"validate_bounded_int",
	"validate_log_filter_level",
	"validate_non_empty_string",
	"validate_code_string",
	"validate_string_list",
	# Serializers
	"serialize_exception",
	"serialize_exceptions",
	# Objects
	"inspect_object",
	"walk_object_tree",
	"get_navigator_object",
	"get_desktop_object",
	"inspect_app_module",
	# Focus
	"get_current_focus",
	"move_focus",
	"get_focus_lineage",
	# Text
	"read_current_line",
	"speak_text",
	# UI
	"send_keyboard",
	"send_single_gesture",
	"resolve_gesture",
	"get_window_title",
	"show_message",
	# System
	"restart_nvda",
	"reload_plugins",
	"execute_python",
	# Diagnostics
	"health_snapshot",
	"list_loaded_addons",
	"get_logs",
]
