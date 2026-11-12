# Native Log Diagnostics

`search_logs`, `get_log_context` and `summarize_logs` turn NVDA's native log
into structured evidence an agent can page through, expand and count. They are
read-only, use the add-on's existing authentication and `diagnostics`
permission, never accept a file path, and never modify or rotate the log.

They do **not** determine a root cause. A warning near an action is a
temporal correlation until the record says otherwise.

## Why not just `get_logs`?

`get_logs` remains the compatibility tool: it returns the last N physical lines,
optionally filtered by an exact level. That is useful for a quick look and
useless for a 50 MB log with 100 000 multiline records, because a traceback and
its header are different lines, a page has no stable identity, and a message
can wrap. The diagnostic tools share one streaming parser and return logical
records with stable references.

| Tool | Question it answers |
|---|---|
| `summarize_logs` | What is in this log? Which sources, severities, exceptions, warnings and repeated patterns dominate? |
| `search_logs` | Where are the records matching my filters? |
| `get_log_context` | What happened immediately before and after this record? |
| `get_logs` | Give me the last N raw lines (legacy). |
| `get_action_trace` | What did the bridge observe while this action ran? |

## Recommended agent workflow

Unknown problem:

```text
summarize_logs
    -> pick a suspicious source, level or repeated pattern
search_logs
    -> get entry_id references and timestamps
get_log_context
    -> inspect surrounding records and other threads
reproduce with the existing action/observation tools
    -> get_action_trace(trace_id)
    -> search_logs(trace_id=...)
compare evidence and state what remains unknown
```

Start with `get_action_trace` when a `trace_id` is already known. Start with
`search_logs` when the logger or message is already known. Do not call every
tool by habit; each result is evidence, not a checklist.

## `search_logs`

### Input

```json
{
  "levels": ["WARNING", "ERROR", "DEBUGWARNING"],
  "min_level": null,
  "sources": {
    "include": ["external:globalPlugins.browserNav.*"],
    "exclude": ["comtypes.*"]
  },
  "addon": null,
  "message": {
    "contains": "foreground",
    "regex": null,
    "case_sensitive": false
  },
  "time": {
    "last_seconds": 300,
    "start": null,
    "end": null
  },
  "threads": [],
  "trace_id": null,
  "limit": 50,
  "cursor": null,
  "order": "newest_first",
  "include_raw": false,
  "max_scan_records": 500000
}
```

`limit` is 1–200. `max_scan_records` is 1 000–2 000 000 and is the scan budget;
when it is reached the response says so instead of pretending the result is
complete.

### Output

```json
{
  "schema_version": 1,
  "entries": [
    {
      "entry_id": "lg-b1e2676975b2dafa07cf:44c:8f2a91cd",
      "timestamp": "02:03:37.589",
      "timestamp_ms": 7421589,
      "timestamp_date": null,
      "timestamp_date_source": "unknown",
      "timestamp_absolute": null,
      "timestamp_timezone": "unknown",
      "level": "DEBUGWARNING",
      "logger": "IAccessibleHandler.internalWinEventHandler._shouldGetEvents",
      "module": "IAccessibleHandler",
      "function": "_shouldGetEvents",
      "subsystem": "focus",
      "addon": null,
      "thread": {"name": "MainThread", "id": 35940},
      "message": "Foreground took too long to change...",
      "line_start": 100,
      "line_end": 101,
      "parse_status": "parsed",
      "parse_notes": [],
      "derived_fields": ["module", "function", "subsystem", "addon", "exception", "enrichment"],
      "exception": null,
      "enrichment": {
        "focus": {
          "still_handle": 67678,
          "still_class": "Chrome_WidgetWin_1",
          "expected_handle": 263518,
          "expected_class": "XamlExplorerHostIslandWindow",
          "observation": "foreground_change_delayed"
        }
      },
      "raw": null
    }
  ],
  "page": {
    "returned": 1,
    "has_more": true,
    "next_cursor": "eyJ2IjoxLC...",
    "limit": 50,
    "order": "newest_first"
  },
  "source": {
    "generation": "lg-b1e2676975b2dafa07cf",
    "size_bytes": 17063362,
    "source": "nvda_native_log"
  },
  "diagnostics": {
    "truncated": false,
    "parse_failures": 0,
    "time_filter_precision": "not_applicable",
    "scan_budget_exhausted": false,
    "response_truncated": false,
    "invalid_utf8_replacements": 0,
    "records_examined": 114678,
    "matched_records": 93,
    "limitations": []
  }
}
```

`raw` is returned only with `include_raw: true`. Even then it is bounded
(32 000 characters) and is explicitly marked in `parse_notes` if shortened.
The exact bytes remain addressable by `entry_id` while the generation is
unchanged.

## Severity semantics

NVDA's levels, ordered from most verbose:

```text
DEBUG (10) < IO (12) < DEBUGWARNING (15) < INFO (20) < WARNING (30) < ERROR (40) < CRITICAL (50)
```

* `DEBUGWARNING` is a distinct level. It is emitted by debug-level subsystems
  when they detect something worth surfacing — the foreground-delay warning is
  the canonical case. It is **not** folded into `DEBUG`, and `min_level:
  "WARNING"` does not include it. Use `levels: ["DEBUGWARNING"]` explicitly, or
  the `warnings` section of `summarize_logs`, which includes it.
* `IO` is the speech/braille/input trace level, not an error.
* Unknown uppercase level names are preserved exactly and marked
  `unknown_level_preserved` in `parse_notes`; they never crash the parser and
  only match an explicit `levels` entry.
* Exact `levels` are matched case-insensitively after upper-casing.

## Source and add-on matching

Source patterns are case-insensitive and use **globs, never regular
expressions**:

| Pattern | Matches |
|---|---|
| `speech.speech.speak` | the exact logger |
| `speech` | `speech` and every dotted descendant |
| `speech.*` | the same dotted-prefix match, explicitly |
| `UIAHandler.UIAHandler.*` | that logger namespace |
| `external:globalPlugins.browser*` | glob over the whole source string |

`exclude` wins over `include`. At most 32 patterns per list.

`addon` is a convenience filter over the add-on identity NVDA's module naming
makes unambiguous:

* `external:globalPlugins.<name>...` or `globalPlugins.<name>...` → `<name>`
* `external:appModules.<name>...` or `appModules.<name>...` → `<name>`

A logger merely containing `globalPlugins` without a concrete module segment
does not manufacture an add-on name. Core loggers (`core.main`,
`IAccessibleHandler...`) correctly have `addon: null`. The original logger is
always preserved even when `addon` is derived.

## Message matching

* `contains` is a literal substring, case-insensitive by default.
* `regex` uses Python's `re` with `IGNORECASE` unless `case_sensitive`.
* Invalid regexes are a caller error (`4103` via the HTTP envelope), not an
  empty result.
* Regex length is capped at 256 characters; backreferences, nested unbounded
  quantifiers, quantified alternations and counted repeats above 1 000 are
  rejected before evaluation. Each message is examined at most 64 KiB. This is
  a practical guard, not a full regex sandbox.
* Both `contains` and `regex` may be supplied; both must match.

## Time filtering and ambiguous dates

NVDA's native header contains a time of day only:

```text
DEBUGWARNING - ... (02:03:37.589) - MainThread (35940):
```

There is no calendar date and no timezone in that line. The tools therefore
report:

* `timestamp`: observed `HH:MM:SS.mmm`
* `timestamp_ms`: milliseconds since local midnight
* `timestamp_date`: `null` unless the header itself carried `YYYY-MM-DD`
* `timestamp_date_source`: `unknown` or `explicit_header`
* `timestamp_absolute`: only when a date **and** offset are present; normally `null`
* `timestamp_timezone`: `unknown` for native NVDA records

Consequences:

* `time.last_seconds` compares against the current clock time-of-day, not a
  date. A log spanning more than 24 hours cannot be separated this way. The
  response reports `time_filter_precision: "time_of_day"` and a limitation.
* `time.start`/`time.end` accept ISO 8601 or `HH:MM:SS(.mmm)`. A time-only range
  that crosses midnight (`23:59:00` → `00:02:00`) is treated as a wrapping
  window and labelled.
* A dated range is compared as a date only for records whose own header carries
  a date. Records without one fall back to time-of-day, and the precision is
  reported as `mixed` when both kinds occur.
* File modification time is never used as proof of an entry's date.
* Midnight rollover is never hidden: `timestamp_ms` goes back to 0, and summaries
  state that a rollover was not resolved.

## Pagination, cursors and log generations

`entry_id` is `<generation>:<byte-offset-in-hex>:<record-hash>`, where the
generation is a fingerprint of the log's file identity (device, inode/file
index, creation metadata) and the short hash covers the first 1 024 bytes of the
record. It is stable under **append-only growth**, which is how NVDA writes its
log, and it changes when the file is replaced or rotated. The hash makes an
in-place rewrite at the same offset detectable (`4206`, reason
`entry_changed`) instead of silently returning different bytes.

A cursor is base64-encoded JSON containing:

* protocol version
* generation
* record byte position
* result order
* a hash of the complete filter set
* the file size when the cursor was issued
* issue time

The cursor is stateless, so two clients paging concurrently cannot advance each
other's position. The server rejects:

* a cursor issued for different filters or order (`4103`)
* a cursor older than 30 minutes (`4206 STALE_REFERENCE`, reason
  `cursor_expired`)
* a cursor from a replaced/rotated file (`4206`, reason `generation_changed`)
* a cursor whose recorded file size is larger than the current file (`4206`,
  reason `file_truncated`)

Pages never duplicate or skip records while the content is unchanged. If the
log grows during a page read, the page covers the file as it was at the start,
and `diagnostics.limitations` says it grew. `next_cursor` is present exactly
when `page.has_more` is true. A shortened response also sets
`diagnostics.response_truncated`.

## `get_log_context`

### Input

```json
{
  "entry_id": "lg-b1e2676975b2dafa07cf:44c:8f2a91cd",
  "before": 20,
  "after": 30,
  "include_related_threads": true,
  "include_raw": false,
  "before_ms": null,
  "after_ms": null
}
```

`before`/`after` are 0–50 records. `before_ms`/`after_ms` are 1–60 000 ms; when
one is supplied for a side it replaces that side's count. The anchor is always
included. Count mode finds the enclosing window by scanning backward for header
lines and parsing only that window; time mode scans forward once because a
clock can wrap.

`include_related_threads` means:

* `true` (default): include every thread in the chronological window. No thread
  relationship is inferred.
* `false`: include only the anchor record's thread. This is a convenience, not
  evidence that other threads are unrelated.

Output carries the anchor, the ordered entries, `complete_before`,
`complete_after`, generation, parse-failure counts, `response_truncated` and
limitations. Context is not silently trimmed: when the 600 000-character
response budget is exceeded, the farthest records are removed and the
completeness flags are cleared.

A malformed `entry_id` is `4103`; a generation mismatch or missing anchor is
`4206`.

## `summarize_logs`

### Input

```json
{
  "levels": [],
  "min_level": null,
  "sources": {"include": [], "exclude": []},
  "addon": null,
  "time": {"last_seconds": 600},
  "threads": [],
  "group_by": ["source", "level", "message_pattern"],
  "include": [
    "counts", "repeated_patterns", "exceptions",
    "warnings", "top_sources", "time_buckets", "noise"
  ],
  "limit": 20,
  "max_scan_records": 500000
}
```

`limit` is 1–50. `group_by` accepts `source`, `level`, `message_pattern`,
`subsystem`, `thread` and `addon`; the summary always computes the dimensions
needed by the requested sections and echoes the requested grouping in coverage.

### Summary sections

* `severity_counts`: every level present, including unknown levels.
* `top_sources`: count, share and per-level breakdown per logger.
* `repeated_patterns`: normalized messages, count, first/last timestamp, up to
  three `entry_id`s and the **original** example.
* `exceptions`: grouped by type, source module, top stack frame and normalized
  message; carries parse status.
* `warnings`: repeated `WARNING`/`DEBUGWARNING` groups.
* `time_buckets`: adaptive 1 s–1 h buckets, top by count, with
  `anomaly_candidate` computed deterministically as `count >= 5` and more than
  two standard deviations above the mean bucket.
* `noise`: count and share of COM pointer-release diagnostics and UIA activity.
  Noisy records are counted, never removed; high COM volume is not an error.

Normalization replaces volatile values: COM pointers, object addresses, hex
addresses, UUIDs, dates/times, window/process/thread identifiers and standalone
numbers of five or more digits. It does **not** replace words, exception types,
file names or the rest of a message, so distinct errors stay distinct.

Pattern groups, sources, exception groups and time buckets are bounded. When a
bound is hit, the response says how many records fell outside the table. The
scan also stops at a 12-second budget; `coverage.complete` becomes false and
`coverage.time_budget_exhausted` says why. A summary never invents a causal
statement; it reports counts and examples.

## Exception parsing

A traceback inside a log record is parsed into:

```json
{
  "type": "AttributeError",
  "message": "Example error",
  "frames": [
    {"file": "globalPlugins/example/__init__.py", "line": 90, "function": "event_gainFocus"}
  ],
  "chain": [
    {
      "type": "ZeroDivisionError",
      "message": "division by zero",
      "frames": [],
      "relation": "direct_cause",
      "parse_status": "complete"
    }
  ],
  "parse_status": "complete",
  "traceback_line_offset": 12
}
```

* The final block is the primary exception; earlier blocks are the chain.
* `relation` is `direct_cause`, `during_handling` or `context`, copied from the
  separator Python writes between blocks.
* Standard, chained, nested and add-on tracebacks are supported. A custom
  exception class is accepted after at least one parsed frame.
* If the traceback ends before its exception line, `parse_status` is `partial`,
  `raw_traceback` preserves the text, and no type is invented.
* A line that merely contains the word `Error` is not a traceback. Only a
  `Traceback (most recent call last):` block triggers this parser.

## NVDA-specific enrichment

`subsystem` is a deterministic derived label: `bridge`, `addon`, `com`, `uia`,
`msaa`, `speech`, `braille`, `input`, `focus`, `configuration`, `performance`,
`nvda_core`, `accessibility` or `unknown`. The original logger is never
replaced. A foreground-delay message logged by `IAccessibleHandler` is labelled
`focus`, because that is the diagnostic subject, while other IAccessible
records remain `msaa`.

`enrichment` only contains facts visible in the record:

* focus/foreground handles and window classes from NVDA's known delay message;
* gesture identifiers when `inputCore`/gesture text provides one;
* speech activity and its direction (`requested/emitted`, `queued`,
  `cancelled`, `interrupted`), with `audio_audibility:
  "not_observable_from_log"` — the log cannot prove the user heard audio;
* bridge `trace_id` when the line explicitly contains it.

## Relationship to action traces

Every bridge action gets an `at-...` trace id. `get_action_trace` remains the
primary way to read lifecycle milestones, exceptions, events, UI projection and
best-effort native matches. Its existing contract is unchanged.

To connect a trace to the log:

```text
action envelope (trace_id)
    -> get_action_trace(trace_id)
    -> search_logs(trace_id="at-...")
    -> get_log_context(entry_id=...)
```

`search_logs(trace_id=...)` requires the literal identifier in the record. That
is direct evidence. `get_action_trace`'s `time_window` native matches are
temporal correlations and remain labelled as such. A third-party exception
that happens to be near an action is not evidence that the action caused it.

## Security and privacy

* The canonical path comes only from NVDA's `globalVars.appArgs.logFileName`.
  There is no path parameter and no environment override.
* The existing bridge token authentication and `diagnostics` permission are
  required. No new permission is introduced.
* Tools are read-only; they do not change NVDA's log level, rotate/delete the
  log, or execute Python.
* Outputs are bounded by page size, per-message/raw caps, scan budgets and
  response-size budgets.
* The new tools do not return the log file path. The legacy `get_logs` still
  does, as it always has.
* The bridge's own token and obvious `password=`, `token=`, `api_key=`,
  `authorization:` assignments are redacted from message, raw, exception and
  summary examples.
* **Redaction is best effort.** Logs can contain usernames, local paths,
  document contents, typed characters, window titles, URLs, third-party
  credentials and exception messages. There is no general content redaction,
  and an agent must treat log text as sensitive.

## Performance and limits

The parser streams the file; it does not load the log into memory. A single
record is capped at 4 MiB (with an explicit truncation note), a page at 200
entries, and search/summary scans at their `max_scan_records` budget. Context
uses a 600 000-character response budget. No persistent database or unbounded
cache is used.

Measurements from `scripts/log_benchmark.py` on the development machine
(Windows 11, Python 3.13, local SSD) are:

| Dataset | Parse | Search (limit 20) | Context (10+10) | Summary |
|---|---:|---:|---:|---:|
| 10 KB | 5 ms | 1.7 ms | 1.2 ms | 3.6 ms |
| 1 MB | 159 ms | 2.8 ms | 1.0 ms | 229 ms |
| 10 MB | 1.18 s | 4.2 ms | 1.5 ms | 4.2 s |
| 50 MB synthetic | 9.74 s | 2.3 ms | 0.9 ms | 12.1 s (budget-capped, 164 864 of 285 065 records) |
| Real NVDA log (2.8 MB) | 352 ms | 257 ms | 7.7 ms | 573 ms |

The search column is an oldest-first search, which stops after the requested
page; a newest-first search scans the file. Peak parser memory stayed at
0.1 MB because records are yielded one at a time; the largest response above
was 33 KB. A live concurrent run had an `observe` action complete in about
108 ms while a summary scanned the log on the bridge worker.

The 50 MB summary is intentionally stopped at the 12 s budget and reports
`complete: false` and `time_budget_exhausted: true`; returning a partial answer
under a known bound is preferable to running past the transport timeout. These
are measurements of this machine, not guarantees for every host.

No cache of decoded records is kept: retaining screen text in NVDA's memory is
a privacy cost, and the log remains the authoritative source. The generation
and byte-offset reference makes re-reading safe and keeps context lookup cheap.

## Known limitations

* Native NVDA headers have no date or timezone; relative windows can confuse
  two different days that have the same clock time.
* A physical line that begins like a header inside a message (for example a
  configuration dump line starting `ERROR -`) is split as a malformed record.
  The original text is preserved in both records' `raw`.
* Records above the 4 MiB cap are truncated with an explicit note.
* Regex safety is heuristic; the input and pattern limits bound but do not
  mathematically eliminate regex cost.
* Unknown third-party log formats outside NVDA's `LEVEL - logger (time) -
  thread (id):` shape are preserved as malformed records, not interpreted.
* The parser does not inspect Windows event logs, ETW, crash dumps or rotated
  `.old` files; it follows NVDA's active log only.
* Redaction cannot find arbitrary secrets.
* Summary grouping is bounded; when the group table is full, totals for that
  section are reported as overflow rather than silently dropped.

## Practical examples

### Debug a specific add-on

```json
{
  "addon": "browserNav",
  "levels": ["WARNING", "ERROR", "DEBUGWARNING"],
  "limit": 50,
  "order": "newest_first"
}
```

Then take a result's `entry_id` into `get_log_context` with
`include_related_threads: true` to see what NVDA core and the UI thread did
around it.

### Investigate foreground/focus delays

```json
{
  "levels": ["DEBUGWARNING"],
  "message": {"contains": "Foreground took too long"},
  "time": {"last_seconds": 600},
  "limit": 20
}
```

The enriched `focus` facts show which window kept the foreground and which
window NVDA expected. They do not prove why Windows refused the change.

### Find repeated exceptions

```json
{
  "include": ["exceptions", "counts"],
  "time": {"last_seconds": 1800},
  "limit": 20
}
```

Use the returned `example_entry_id` with `get_log_context` to read the full
traceback without asking for the whole log.

### Diagnose speech behaviour

```json
{"sources": {"include": ["speech.*"]}, "levels": ["IO"], "limit": 50}
```

`enrichment.speech.event` distinguishes requested/queued/cancelled activity.
`audio_audibility` is always `not_observable_from_log`.

### Investigate UIA/COM noise

```json
{
  "include": ["noise", "top_sources", "repeated_patterns"],
  "sources": {"include": ["comtypes.*", "UIAHandler.*"]},
  "limit": 20
}
```

A high COM release share is normal in a busy session; it becomes interesting
only alongside an exception, a stalled action or a UIA-specific warning.

### Find bridge action failures

```json
{
  "message": {"contains": "bridge.action.failed"},
  "levels": ["INFO"],
  "limit": 20
}
```

Then `search_logs(trace_id="at-...")` for the exact correlation, and
`get_action_trace` for the structured lifecycle. The log line and the trace are
two views of the same bridge action; a nearby third-party error is still just
nearby.
