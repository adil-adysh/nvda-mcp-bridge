# Speech Observation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an explicitly authorized MCP client retrieve bounded text and control events from NVDA's speech pipeline without changing speech output.

**Architecture:** Add an independent in-memory `SpeechJournal` and a `SpeechMonitor` that registers NVDA speech hooks. Gate capture and retrieval with a new default-off capability, expose the journal through authenticated `GET /speech` and a typed `wait_for_speech` tool, and leave UI event versions untouched.

**Tech Stack:** Python 3.12, NVDA extension points, stdlib `threading`/`collections`, existing dataclass tool registry and schema generator, pytest, Rust companion's dynamic tool discovery.

**Spec:** `docs/superpowers/specs/2026-11-23-speech-observation-design.md`

## Global Constraints

- Preserve the add-on's `2026.1.0` minimum NVDA version; feature-detect `synthDriverHandler.pre_synthSpeak` and fall back to `speech.extensions.pre_speechQueued`.
- The event phase is `about_to_synthesize` for `pre_synthSpeak` and `queued` for `pre_speechQueued`.
- Keep the journal in memory, with at most 256 events and at most 1,024 text characters per speech event.
- `speech_observation` is separate from `speech_output`, absent from defaults, and retained only when explicitly granted.
- Do not capture or return transcript text when permission is absent, NVDA is on the secure desktop, or protected focus is active.
- Clear retained speech and change journal generation on permission revocation or permission reset.
- Speech hooks do bounded text extraction and journal writes only; no network access, logging of speech text, or NVDA object-tree traversal.
- Preserve the UI event journal and its state/version behavior. Keep MCP resource subscriptions and action-trace correlation out of this release.
- Cap `/speech` and `wait_for_speech` waits at 30,000 ms; honor any Python cancellation event supplied to the journal.

## Review Focus

- Mixed speech commands, non-string items, empty sequences, and overlong text: assert only string chunks are joined and text truncation is reported in `tests/test_speech.py`.
- Journal overflow, old cursors, wrong journal IDs, and concurrent reset: assert no gap is silently reported as complete in `tests/test_speech.py`.
- Missing modern NVDA hook, callback registration failure, and shutdown: assert the legacy hook is selected only when necessary and every registered hook is removed in `tests/test_speech_monitor.py`.
- Permission revocation, secure desktop, and protected focus during reads: assert history is cleared or access denied in `tests/test_speech_security.py` and `tests/test_http_bridge.py`.
- Invalid query values and unauthenticated retrieval: assert the endpoint clamps bounded values and returns the existing auth/error contract in `tests/test_http_bridge.py`.

---

### Task 1: Speech text extraction and journal

**Files:**
- Create: `addon/globalPlugins/nvdaMcp-bridge/server/speech.py`
- Create: `tests/test_speech.py`

**Interfaces:**
- `extract_speech_text(sequence: Iterable[Any], limit: int = 1024) -> tuple[str, bool]`
- `SpeechJournal(capacity: int = 256, max_text_chars: int = 1024)` with `record_speech(text, phase, text_truncated=False)`, `record_control(kind, paused=None)`, `snapshot(since=0, limit=20, journal_id=None)`, `wait(since=0, timeout_ms=0, limit=20, journal_id=None, cancellation_event=None)`, `reset(reason="reset")`, and read-only `version`/`journal_id` properties.
- Speech entries have `kind="speech"`, `phase`, `text`, `text_truncated`, `at_ms`, and `version`. Control entries have a control kind, `at_ms`, and `version`, with no `text` field.
- Snapshot/wait responses use the event cursor vocabulary: `journal_id`, `from_version`, `to_version`, `earliest_available_version`, `dropped`, `events`, `expired`, `resync_required`, `resync_reason`, `resync_from`, `history_complete`, `truncated`, `has_more`, `cursor_status`, `timed_out`, and `cancelled`.

- [ ] **Step 1: Write failing extraction and journal tests**

  Add these tests with the stated assertions:

  - `test_extract_speech_text_joins_only_string_chunks`: `"Save"`, a command object, and `" complete"` produce `("Save complete", False)`.
  - `test_extract_speech_text_truncates_at_limit`: 1,030 characters with limit 1,024 produce a 1,024-character string and `True`.
  - `test_empty_speech_sequence_is_not_recorded`: empty/non-string-only sequences produce no text event.
  - `test_speech_journal_versions_are_independent_and_ordered`: record two entries; assert versions 1 and 2, increasing timestamps, and no mutation of a separate `EventJournal.version`.
  - `test_speech_journal_retains_256_and_reports_expired_cursor`: record 257 entries; assert the oldest retained version is 2, `dropped == 1`, and cursor 0 requires resync.
  - `test_speech_journal_pagination_has_no_gaps`: page retained records by a small limit and assert every version is returned once in order.
  - `test_speech_journal_reset_changes_id_and_rejects_old_cursor`: reset; assert an old typed cursor returns `journal_mismatch` and no old event text.
  - `test_speech_journal_wait_handles_timeout_event_and_cancellation`: assert timeout, wake on append, and a set cancellation event are each reported correctly.

- [ ] **Step 2: Run the tests to verify the expected failure**

  Run: `pytest tests/test_speech.py -q`

  Expected: FAIL because `server.speech` and its interfaces do not exist.

- [ ] **Step 3: Implement extraction and `SpeechJournal`**

  Use a `deque(maxlen=capacity)` and a `threading.Condition`, following `EventJournal`'s typed-cursor, pagination, reset, timeout, and cancellation semantics without sharing or incrementing its version.

- [ ] **Step 4: Run the focused tests**

  Run: `pytest tests/test_speech.py -q`

  Expected: PASS, including overflow and reset metadata assertions.

- [ ] **Step 5: Commit Task 1**

  ```powershell
  git add addon/globalPlugins/nvdaMcp-bridge/server/speech.py tests/test_speech.py
  git commit -m "feat: add bounded speech journal"
  ```

### Task 2: Native speech monitor and plugin lifecycle

**Files:**
- Modify: `addon/globalPlugins/nvdaMcp-bridge/server/speech.py`
- Modify: `addon/globalPlugins/nvdaMcp-bridge/__init__.py`
- Create: `tests/test_speech_monitor.py`

**Interfaces:**
- `SpeechMonitor(journal: SpeechJournal, is_enabled: Callable[[], bool], initially_secure: bool = False)` exposes `start()`, `stop()`, `set_protected_focus(obj)`, and `is_protected_focus`.
- `start()` registers exactly one text hook: `synthDriverHandler.pre_synthSpeak` when present, otherwise `speech.extensions.pre_speechQueued`. It also registers `speech.extensions.speechCanceled`, `speech.extensions.post_speechPaused`, and `winAPI.secureDesktop.post_secureDesktopStateChange` when available.
- Hook callbacks accept `speechSequence`, `switch`, and `isSecureDesktop` keyword arguments as documented by NVDA. `speechCanceled` has no arguments.
- The protected-focus cache checks the current focus object's protected state during focus events, never during a synthesis callback. Initialize it from `api.getFocusObject()` on the NVDA main thread.

- [ ] **Step 1: Write failing monitor and lifecycle tests**

  Add these tests:

  - `test_monitor_prefers_pre_synth_hook_and_unregisters`: fake both text hooks; assert only `pre_synthSpeak` is registered, emits phase `about_to_synthesize`, and all hooks are unregistered on stop.
  - `test_monitor_uses_queued_fallback_when_pre_synth_hook_is_missing`: omit `pre_synthSpeak`; assert `pre_speechQueued` emits phase `queued`.
  - `test_monitor_start_and_stop_are_idempotent`: repeated calls do not double-register or double-unregister callbacks.
  - `test_monitor_suppresses_capture_without_permission`: invoke a text hook with `is_enabled() == False`; assert the journal stays empty.
  - `test_monitor_suppresses_capture_on_secure_desktop_and_protected_focus`: invoke hooks in both states; assert no speech text is retained.
  - `test_monitor_records_cancel_pause_and_resume_without_text`: invoke callbacks; assert `speech_canceled`, `speech_paused`, and `speech_resumed` entries have no `text` key.
  - `test_monitor_focus_state_uses_protected_state_only`: protected focus sets the cache; a later ordinary focus clears it.

- [ ] **Step 2: Run the monitor tests to verify the expected failure**

  Run: `pytest tests/test_speech_monitor.py -q`

  Expected: FAIL because `SpeechMonitor` is not implemented.

- [ ] **Step 3: Implement `SpeechMonitor` and lifecycle wiring**

  Register/unregister callbacks symmetrically. Initialize secure state from the current NVDA status before hooks can capture. In `GlobalPlugin`, start capture only after the bridge exists; seed focus protection and update it on focus changes; stop the monitor during termination before stopping the HTTP bridge.

- [ ] **Step 4: Run monitor tests**

  Run: `pytest tests/test_speech_monitor.py -q`

  Expected: PASS for preferred/fallback selection, suppression, and cleanup.

- [ ] **Step 5: Commit Task 2**

  ```powershell
  git add addon/globalPlugins/nvdaMcp-bridge/server/speech.py addon/globalPlugins/nvdaMcp-bridge/__init__.py tests/test_speech_monitor.py
  git commit -m "feat: capture NVDA speech through native hooks"
  ```

### Task 3: Speech-observation permission and settings

**Files:**
- Modify: `addon/globalPlugins/nvdaMcp-bridge/server/security.py`
- Modify: `addon/globalPlugins/nvdaMcp-bridge/server/http_bridge.py`
- Modify: `addon/globalPlugins/nvdaMcp-bridge/gui_settings.py`
- Modify: `tests/test_permission_persistence.py`
- Modify: `tests/test_settings_panel.py`
- Create: `tests/test_speech_security.py`

**Interfaces:**
- Add `PERMISSION_SPEECH_OBSERVATION = "speech_observation"` to `ALL_PERMISSIONS` and `PERSISTABLE_PERMISSIONS`, but not `DEFAULT_PERMISSIONS`.
- `NVDAHTTPBridge.set_speech_observation_enabled(enabled: bool) -> None` updates the capability and resets the speech journal generation when disabling it. `reset_permissions()` also clears the speech journal.
- The Settings panel checkbox label is `Allow MCP Speech Observation (read NVDA announcements)`; save, discard, and reset keep it synchronized with the policy.

- [ ] **Step 1: Write failing permission, persistence, reset, and panel tests**

  Add assertions that:

  - `PermissionPolicy()` and `PermissionPolicy.reset()` do not grant `speech_observation`.
  - Saving and reloading an explicit grant preserves it while unknown permission names remain ignored.
  - Revoking speech observation and resetting permissions clear entries and change the speech journal ID.
  - The settings checkbox reflects a stored grant; save grants/revokes and persists it; discard restores the live value; reset unchecks it.

- [ ] **Step 2: Run the focused tests to verify failure**

  Run: `pytest tests/test_permission_persistence.py tests/test_settings_panel.py tests/test_speech_security.py -q`

  Expected: FAIL because the new permission and checkbox are absent.

- [ ] **Step 3: Implement permission and settings integration**

  Add the capability to allowlists only. Route the checkbox through `set_speech_observation_enabled`; make reset clear speech history while preserving existing safe defaults and other permission behavior.

- [ ] **Step 4: Run the focused tests**

  Run: `pytest tests/test_permission_persistence.py tests/test_settings_panel.py tests/test_speech_security.py -q`

  Expected: PASS; speech observation remains off after default construction and reset.

- [ ] **Step 5: Commit Task 3**

  ```powershell
  git add addon/globalPlugins/nvdaMcp-bridge/server/security.py addon/globalPlugins/nvdaMcp-bridge/server/http_bridge.py addon/globalPlugins/nvdaMcp-bridge/gui_settings.py tests/test_permission_persistence.py tests/test_settings_panel.py tests/test_speech_security.py
  git commit -m "feat: add opt-in speech observation permission"
  ```

### Task 4: Authenticated speech endpoint and execution context

**Files:**
- Modify: `addon/globalPlugins/nvdaMcp-bridge/server/http_bridge.py`
- Modify: `addon/globalPlugins/nvdaMcp-bridge/core/base.py`
- Modify: `tests/test_http_bridge.py`
- Modify: `tests/test_speech_security.py`

**Interfaces:**
- Add `GET /speech?since=&journal_id=&timeout_ms=&limit=`. The endpoint requires the bridge token and `speech_observation`; `timeout_ms` is clamped to 0–30,000 and `limit` to 1–256. It denies reads on secure desktop or protected focus.
- Add `ExecutionContext.wait_speech` and `ExecutionContext.speech_version`; the bridge supplies the speech journal wait/head callbacks.
- Provide one bridge access predicate used by the HTTP route and `wait_for_speech` path so permission, secure desktop, and protected-focus checks use the same rules.

- [ ] **Step 1: Write failing endpoint and access-gate tests**

  Add these tests to `tests/test_http_bridge.py`/`tests/test_speech_security.py`:

  - `test_speech_endpoint_rejects_missing_or_invalid_token`: assert HTTP 401 and no journal read.
  - `test_speech_endpoint_requires_speech_observation`: with a valid token and default policy, assert HTTP 403 and no text response.
  - `test_speech_endpoint_denies_secure_desktop_and_protected_focus`: assert both states return 403.
  - `test_speech_endpoint_clamps_timeout_and_limit`: pass out-of-range integers and assert the journal receives 30,000 ms and 256.
  - `test_speech_endpoint_preserves_typed_cursor_metadata`: assert a supplied `journal_id`/`since` is passed through and the page is returned unchanged.
  - `test_bridge_reset_and_revocation_clear_speech_history`: assert no endpoint or tool read can retrieve pre-revocation events.

- [ ] **Step 2: Run endpoint/access tests to verify failure**

  Run: `pytest tests/test_http_bridge.py tests/test_speech_security.py -q`

  Expected: FAIL because `/speech`, access gating, and context callbacks do not exist.

- [ ] **Step 3: Implement `/speech` and context callbacks**

  Add a route parallel to `/events`, but source it only from `speech_journal`; verify token before capability/state checks; return no speech content on denial. Wire `wait_speech` and `speech_version` through `ExecutionContext`.

- [ ] **Step 4: Run endpoint/access tests**

  Run: `pytest tests/test_http_bridge.py tests/test_speech_security.py -q`

  Expected: PASS for auth, permission, secure/protected denial, bounds, and cursor forwarding.

- [ ] **Step 5: Commit Task 4**

  ```powershell
  git add addon/globalPlugins/nvdaMcp-bridge/server/http_bridge.py addon/globalPlugins/nvdaMcp-bridge/core/base.py tests/test_http_bridge.py tests/test_speech_security.py
  git commit -m "feat: expose authenticated speech journal endpoint"
  ```

### Task 5: `wait_for_speech` MCP tool and published schema

**Files:**
- Modify: `addon/globalPlugins/nvdaMcp-bridge/tools/models.py`
- Modify: `addon/globalPlugins/nvdaMcp-bridge/tools/handlers.py`
- Modify: `addon/globalPlugins/nvdaMcp-bridge/tools/nvda_tools.py`
- Modify: `schema.json` (generated)
- Modify: `tests/test_action_tools.py`
- Modify: `tests/test_generated_schema.py` only if it needs a new assertion beyond its existing registry consistency checks.

**Interfaces:**
- `WaitForSpeechInput` fields: `since: Optional[int] = None`, `journal_id: Optional[str] = None`, `timeout_ms: int = 1000` with range 0–30,000, `limit: int = 20` with range 1–256.
- `WaitSpeechResult` mirrors journal metadata and carries `events: list[dict[str, Any]]`.
- `wait_for_speech(since, timeout_ms, context, limit=20, journal_id=None) -> dict[str, Any]` validates direct calls, defaults omitted `since` to `context.speech_version()`, and passes `context.cancellation_event` to `context.wait_speech`.
- Register `wait_for_speech` as a read-only coordinator tool in category `observation`, requiring `speech_observation`, allowed only in NVDA state `normal`.

- [ ] **Step 1: Write failing tool and schema tests**

  Add tests that assert:

  - `wait_for_speech` defaults to the current speech journal head and returns no prior events.
  - A typed cursor returns events plus `has_more`, expiry, resync, and dropped metadata unchanged.
  - Invalid `since`, `journal_id`, `timeout_ms`, and `limit` are rejected by direct handler calls.
  - The supplied cancellation event is passed to `context.wait_speech`; `timed_out` is true only when no page advances the cursor.
  - Registry metadata requires `speech_observation`, and the committed schema contains the matching input/output types.

- [ ] **Step 2: Run the focused tool/schema tests to verify failure**

  Run: `pytest tests/test_action_tools.py tests/test_generated_schema.py -q`

  Expected: FAIL because the tool models and registry entry are absent and `schema.json` is stale.

- [ ] **Step 3: Implement models, handler, and tool registration**

  Follow the `wait_for_events` contract for typed cursors and timeout behavior, while using only `speech_version`/`wait_speech` and never the UI event journal.

- [ ] **Step 4: Regenerate schema and run focused tests**

  Run: `python scripts/generate_bridge_schema.py --output schema.json`

  Then run: `pytest tests/test_action_tools.py tests/test_generated_schema.py -q`

  Expected: PASS; the tool input schema and output type registry match the dataclasses.

- [ ] **Step 5: Run Rust companion contract tests**

  Run: `cargo test --manifest-path rust_mcp_bridge/Cargo.toml`

  Expected: PASS; the Rust catalog loads `wait_for_speech` from `schema.json` and can validate its output schema without Rust source changes.

- [ ] **Step 6: Commit Task 5**

  ```powershell
  git add addon/globalPlugins/nvdaMcp-bridge/tools/models.py addon/globalPlugins/nvdaMcp-bridge/tools/handlers.py addon/globalPlugins/nvdaMcp-bridge/tools/nvda_tools.py schema.json tests/test_action_tools.py tests/test_generated_schema.py
  git commit -m "feat: add wait for NVDA speech tool"
  ```

### Task 6: End-to-end regression and verification

**Files:**
- Modify: `tests/test_speech.py`, `tests/test_speech_monitor.py`, `tests/test_speech_security.py`, `tests/test_http_bridge.py`, and `tests/test_action_tools.py` only for any uncovered contract cases identified by review.
- Modify: `docs/AGENT_PLAYBOOK.md` with concise guidance on using `wait_for_speech` and interpreting its phase as pre-synthesis evidence.

- [ ] **Step 1: Add the end-to-end contract test**

  `test_wait_for_speech_returns_only_authorized_retained_speech` should build a bridge with explicit speech permission, record a string sequence and a cancellation control event, call the real registered tool handler, and assert ordered text/control events, typed cursor metadata, and no UI event version change. Repeat with permission removed and assert denial/no text.

- [ ] **Step 2: Verify the failure before implementation**

  Run: `pytest tests/test_speech.py::test_wait_for_speech_returns_only_authorized_retained_speech -q`

  Expected: FAIL until all capture, permission, bridge, and tool tasks are wired.

- [ ] **Step 3: Add concise agent guidance**

  Document that agents should call `wait_for_speech` explicitly, correlate returned text with UI state and action results, and treat `queued`/`about_to_synthesize` as evidence of pipeline preparation rather than proof of audio completion or action causality.

- [ ] **Step 4: Run the full Python suite and schema check**

  Run: `pytest -q`

  Expected: all tests pass, including `test_generated_schema.py`.

- [ ] **Step 5: Run repository verification**

  Run: `python scripts/generate_bridge_schema.py --output schema.json` followed by `git diff --check` and `git status --short`.

  Expected: schema is unchanged after regeneration, no whitespace errors remain, and only intended feature changes are present.

- [ ] **Step 6: Commit Task 6**

  ```powershell
  git add docs/AGENT_PLAYBOOK.md tests
  git commit -m "test: cover speech observation end to end"
  ```
