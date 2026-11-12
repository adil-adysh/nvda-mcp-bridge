"""Structured investigation of NVDA's native log.

The package is deliberately independent of NVDA imports at module load time so
the parser, filters, pagination and summaries can be tested outside NVDA. Only
``active_log_path`` reaches into NVDA, and only when a request is executed.
"""

from .context import get_log_context
from .filters import CompiledFilter, InvalidFilterError, compile_filter
from .models import LEVEL_RANK, SCHEMA_VERSION
from .parser import parse_entry_hash, parse_entry_id
from .reader import (
	LogReader,
	LogSnapshot,
	LogUnavailableError,
	StaleLogError,
	active_log_path,
	snapshot_log,
)
from .redaction import load_bridge_secret_once, redact_text
from .search import decode_cursor, encode_cursor, search_logs
from .summary import normalize_message, summarize_logs

__all__ = [
	"CompiledFilter",
	"InvalidFilterError",
	"LEVEL_RANK",
	"LogReader",
	"LogSnapshot",
	"LogUnavailableError",
	"SCHEMA_VERSION",
	"StaleLogError",
	"active_log_path",
	"compile_filter",
	"decode_cursor",
	"encode_cursor",
	"get_log_context",
	"load_bridge_secret_once",
	"normalize_message",
	"parse_entry_hash",
	"parse_entry_id",
	"redact_text",
	"search_logs",
	"snapshot_log",
	"summarize_logs",
]
