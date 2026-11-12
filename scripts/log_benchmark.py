#!/usr/bin/env python3
"""Measure native-log parsing, search, context and summary cost.

The script uses the same package the add-on uses; it does not simulate a
different parser. Synthetic files are deterministic and include COM noise,
warnings, tracebacks and speech records so the numbers are representative of
the shipped log rather than of a best case.

    python scripts/log_benchmark.py
    python scripts/log_benchmark.py --real "%LOCALAPPDATA%\\Temp\\nvda.log"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ADDON_DIR = REPO_ROOT / "addon" / "globalPlugins" / "nvdaMcp-bridge"
sys.path.insert(0, str(ADDON_DIR))

_PACKAGE_NAME = "nvdaMcpBridge"
if _PACKAGE_NAME not in sys.modules:
	import types

	package = types.ModuleType(_PACKAGE_NAME)
	package.__path__ = [str(ADDON_DIR)]
	sys.modules[_PACKAGE_NAME] = package

from nvdaMcpBridge.server import log_diagnostics  # noqa: E402


TEMPLATE = """\
DEBUG - comtypes._post_coinit.unknwn._log (02:03:37.600) - MainThread (35940):
Release <POINTER(IAccessible) ptr=0x212f14b8c48 at 212f1e9f950>
INFO - external:globalPlugins.browserNav.handlers (02:03:40.100) - MainThread (35940):
BrowserNav focus handler entered
DEBUGWARNING - IAccessibleHandler.internalWinEventHandler._shouldGetEvents (02:03:41.500) - MainThread (35940):
Foreground took too long to change. Foreground still 3276986 (CabinetWClass). Should be 263518 (XamlExplorerHostIslandWindow)
IO - speech.speech.speak (02:03:42.100) - MainThread (35940):
Speaking [LangChangeCommand ('en_US'), 'Example page', 'window']
ERROR - external:globalPlugins.example (02:03:43.100) - Dummy-3 (77):
Example failure 12345
Traceback (most recent call last):
  File "globalPlugins\\example\\__init__.py", line 84, in event_gainFocus
    raise RuntimeError("boom")
RuntimeError: boom
"""


def make_synthetic(path: Path, target_bytes: int) -> None:
	block = TEMPLATE.encode("utf-8")
	with open(path, "wb") as handle:
		written = 0
		index = 0
		while written < target_bytes:
			chunk = block.replace(b"12345", str(10000 + index % 50000).encode("ascii"))
			handle.write(chunk)
			written += len(chunk)
			index += 1


def measure(path: Path) -> dict:
	snapshot = log_diagnostics.snapshot_log(str(path))
	result = {"path": str(path), "size_bytes": snapshot.size}

	started = time.perf_counter()
	reader = log_diagnostics.LogReader(snapshot)
	records = 0
	for _ in reader.iter_records():
		records += 1
	parse_seconds = time.perf_counter() - started

	# Peak memory is measured in a second pass: tracing allocations while
	# timing would report the profiler's cost as parser throughput.
	tracemalloc.start()
	memory_reader = log_diagnostics.LogReader(snapshot)
	for _ in memory_reader.iter_records():
		pass
	_, peak = tracemalloc.get_traced_memory()
	tracemalloc.stop()
	result.update(
		{
			"records": records,
			"parse_ms": round(parse_seconds * 1000, 1),
			"parse_mb_per_second": round((snapshot.size / 1_000_000) / parse_seconds, 2) if parse_seconds else None,
			"peak_memory_mb": round(peak / 1_000_000, 1),
		}
	)

	filters = log_diagnostics.compile_filter(levels=["DEBUGWARNING"])
	started = time.perf_counter()
	search = log_diagnostics.search_logs(snapshot, filters, limit=20, order="oldest_first")
	result["search_ms"] = round((time.perf_counter() - started) * 1000, 1)
	result["search_entries"] = len(search["entries"])
	result["search_response_chars"] = len(json.dumps(search))

	if search["entries"]:
		started = time.perf_counter()
		context = log_diagnostics.get_log_context(snapshot, entry_id=search["entries"][0]["entry_id"], before=10, after=10)
		result["context_ms"] = round((time.perf_counter() - started) * 1000, 1)
		result["context_entries"] = context["returned"]

	filters = log_diagnostics.compile_filter()
	started = time.perf_counter()
	summary = log_diagnostics.summarize_logs(snapshot, filters, limit=20)
	result["summary_ms"] = round((time.perf_counter() - started) * 1000, 1)
	result["summary_response_chars"] = len(json.dumps(summary))
	result["summary_total_records"] = summary["summary"]["total_records"]
	return result


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--real", action="append", default=[], help="Measure an existing NVDA log (repeatable)")
	parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
	args = parser.parse_args()

	rows = []
	with tempfile.TemporaryDirectory() as directory:
		for size in (10_000, 1_000_000, 10_000_000, 50_000_000):
			path = Path(directory) / f"synthetic-{size}.log"
			make_synthetic(path, size)
			rows.append(measure(path))
	for path_text in args.real:
		path = Path(os.path.expandvars(path_text)).expanduser()
		if not path.is_file():
			print(f"skip missing log: {path}", file=sys.stderr)
			continue
		rows.append(measure(path))

	if args.json:
		print(json.dumps(rows, indent=2))
		return 0
	headers = [
		"dataset",
		"MB",
		"records",
		"parse ms",
		"MB/s",
		"peak MB",
		"search ms",
		"context ms",
		"summary ms",
	]
	print("\t".join(headers))
	for row in rows:
		print(
			"\t".join(
				str(value)
				for value in (
					Path(row["path"]).name,
					round(row["size_bytes"] / 1_000_000, 2),
					row["records"],
					row["parse_ms"],
					row["parse_mb_per_second"],
					row["peak_memory_mb"],
					row["search_ms"],
					row.get("context_ms", "-"),
					row["summary_ms"],
				)
			)
		)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
