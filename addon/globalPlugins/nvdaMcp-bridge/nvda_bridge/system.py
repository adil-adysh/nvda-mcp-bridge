"""System management utilities for NVDA Bridge.

Provides functions for system-level operations like restarting NVDA
and executing Python code.
"""

import ast
import contextlib
import traceback
from typing import Any, Dict

import logHandler

from .validators import validate_code_string, validate_string_list


def restart_nvda(disable_addons: bool = False, debug_logging: bool = False) -> Dict[str, Any]:
	"""Restart NVDA with optional parameters.

	Args:
		disable_addons: Whether to restart with addons disabled
		debug_logging: Whether to enable debug logging on restart

	Returns:
		Dictionary with restart confirmation

	Raises:
		RuntimeError: If restart fails
	"""
	import core

	try:
		logHandler.log.info(
			f"Restarting NVDA (disable_addons={disable_addons}, debug_logging={debug_logging})"
		)

		# Trigger NVDA restart
		core.restart(disableAddons=disable_addons, debugLogging=debug_logging)

		# If we reach here, restart was initiated
		return {"status": "restarting"}
	except Exception as e:
		logHandler.log.error(f"Failed to restart NVDA: {e}", exc_info=True)
		raise RuntimeError(f"Restart failed: {str(e)}")


def reload_plugins() -> Dict[str, Any]:
	"""Reload all global plugins and app modules from the scratchpad.

	This reloads:
	- All global plugins (including those in scratchpad)
	- All app modules (including those in scratchpad)
	- Clears the dynamic NVDAObject class cache

	Useful for development when testing changes to plugins without restarting NVDA.

	Returns:
		Dictionary with reload confirmation and counts

	Raises:
		RuntimeError: If reload fails
	"""
	import globalPluginHandler
	import appModuleHandler
	from NVDAObjects import NVDAObject

	try:
		logHandler.log.info("Reloading all plugins from scratchpad")

		# Reload app modules
		appModuleHandler.reloadAppModules()

		# Reload global plugins
		globalPluginHandler.reloadGlobalPlugins()

		# Clear dynamic class cache
		NVDAObject.clearDynamicClassCache()

		logHandler.log.info("Successfully reloaded all plugins")

		return {
			"status": "ok",
			"message": "Reloaded global plugins and app modules",
		}
	except Exception as e:
		logHandler.log.error(f"Failed to reload plugins: {e}", exc_info=True)
		raise RuntimeError(f"Plugin reload failed: {str(e)}")


def execute_python(
	code: str, snapshot_vars: bool = False, capture_vars: list[str] | None = None
) -> Dict[str, Any]:
	"""Execute Python code inside NVDA and return captured output, result and optional variables.

	This uses NVDA's `pythonConsole.PythonConsole` so the execution environment
	contains NVDA-specific helpers such as `api`, `appModules`, etc.

	Security: this is remote code execution. Access to the bridge should be
	restricted and authenticated.

	Args:
		code: Python source to execute (string)
		snapshot_vars: If True, populate namespace snapshot vars (focus, nav, etc.)
		capture_vars: Optional list of names to return from the console namespace (repr strings)

	Returns:
		Dictionary with:
		- output: captured stdout/stderr and tracebacks
		- result: repr() of the last expression value, or None if no expression/failed
		- vars: optional dict of requested variable -> repr(value) or None if undefined

	Raises:
		ValueError: for invalid input
	"""
	code = validate_code_string(code)
	if capture_vars is not None:
		capture_vars = validate_string_list(capture_vars)

	# Sanitize execution logging: never log code snippets, variable names, or execution results
	try:
		logHandler.log.debug(
			"execute_python.executing code_len=%d lines=%d snapshot_vars=%s",
			len(code),
			code.count("\n") + 1,
			snapshot_vars,
		)
	except Exception:
		pass

	# Import locally to avoid widening module import surface at top-level.
	import pythonConsole as _pythonConsole
	import reprlib

	output_parts: list[str] = []

	def _write(s: str) -> None:
		output_parts.append(s)

	class _CaptureStream:
		def __init__(self, writer):
			self._writer = writer

		def write(self, text: str) -> int:
			if text:
				self._writer(text)
			return len(text)

		def flush(self) -> None:
			return None

	console = _pythonConsole.PythonConsole(
		outputFunc=_write, setPromptFunc=lambda p: None, exitFunc=lambda: None
	)
	if snapshot_vars:
		try:
			console.updateNamespaceSnapshotVars()
		except Exception:
			logHandler.log.debug("execute_python: failed to update namespace snapshot vars", exc_info=True)

	namespace = console.namespace
	last_result: Any = None
	stream = _CaptureStream(_write)

	try:
		parsed = ast.parse(code, filename="<console>", mode="exec")
		body = parsed.body
		trailing_expr = body[-1].value if body and isinstance(body[-1], ast.Expr) else None
		exec_body = body[:-1] if trailing_expr is not None else body

		with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
			if exec_body:
				exec_module = ast.Module(body=exec_body, type_ignores=[])
				exec_code = compile(exec_module, "<console>", "exec")
				exec(exec_code, namespace, namespace)
			if trailing_expr is not None:
				expr_code = compile(ast.Expression(body=trailing_expr), "<console>", "eval")
				last_result = eval(expr_code, namespace, namespace)
	except Exception:
		output_parts.append(traceback.format_exc())
		last_result = None

	console.lastResult = last_result
	output = "".join(output_parts)
	try:
		result_repr = repr(last_result)
	except Exception:
		result_repr = f"<unreprable {type(last_result).__name__}>"

	result: dict[str, object] = {
		"stdout": output,
		"stderr": "",
		"result": result_repr,
	}

	if capture_vars:
		vars_out: dict[str, object] = {}
		for name in capture_vars:
			try:
				if name in console.namespace:
					val = console.namespace[name]
					try:
						vars_out[name] = reprlib.repr(val)
					except Exception:
						vars_out[name] = f"<unreprable {type(val).__name__}>"
				else:
					vars_out[name] = None
			except Exception:
				vars_out[name] = "<error>"
		result["captured_variables"] = vars_out

	return result
