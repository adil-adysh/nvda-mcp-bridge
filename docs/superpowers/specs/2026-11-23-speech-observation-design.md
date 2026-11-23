# Speech Observation Design

## Intent

Add an optional, privacy-controlled way for an MCP client to retrieve text NVDA is about to send to its synthesizer. This gives an agent evidence about an action's result that may not be obvious from UI state alone. The record describes prepared speech; it does not prove the user heard the complete utterance or that an action caused it.

## Goals

- Capture text through NVDA's native speech extension points without changing, delaying, or replacing NVDA speech.
- Keep speech in its own bounded, memory-only journal with sequence numbers, timestamps, cursor paging, expiration, reset, and wait behavior.
- Expose retrieval through an authenticated bridge endpoint and an explicit `wait_for_speech` MCP tool.
- Require a separate `speech_observation` capability, off by default and explicitly grantable in NVDA settings.
- Clear retained text immediately when the capability is revoked or safe defaults are restored.
- Suppress capture while NVDA is on the secure desktop or the last observed focus is a protected input; document that protected-field suppression is best-effort.
- Keep the current NVDA 2026.1 minimum supported version by registering `pre_synthSpeak` when available and using `speech.pre_speechQueued` as a feature-detected fallback. Distinguish their capture phases and make no claim that synthesis completed.
- Mark the capture phase truthfully: `about_to_synthesize` for `pre_synthSpeak`, and `queued` for the earlier fallback hook.
- Bound a text event to 1,024 characters and the journal to 256 records. Omit empty text and speech-command objects; retain only the concatenated string content.
- Record all text sequences sent through NVDA's speech pipeline, including text requested through the existing `speak_text` tool. This feature does not speak or inject content automatically, and the speech hook does not provide reliable origin attribution; the client decides whether to retrieve or interpret those records.

## Non-goals

- Automatic injection of speech into the model context.
- MCP resource subscriptions or `nvda://speech/recent` notifications in this release.
- Correlating speech with action traces or claiming causality.
- Streaming the agent's own generated speech, audio loopback, speech recognition, disk logging, or changes to the synthesizer pipeline.

## Constraints and safety

- Existing UI transitions and their version counter must not change when speech is recorded.
- Speech callbacks must perform only capability/state checks, bounded sequence extraction, and a journal append. They must not perform network I/O or traverse NVDA object trees.
- The journal endpoint and MCP tool must both fail closed when `speech_observation` is absent and must deny transcript reads on the secure desktop or while protected focus is active.
- The monitor may cache a protected-focus flag from existing focus events; it must not inspect focus from the synthesis callback. This is a suppression measure, not a complete privacy guarantee because speech can originate outside the focused object.
- Cancellation and pause/resume are control records without speech text. A normal text record is labeled as speech sent toward synthesis, not completed playback.
- The journal wait honors the supplied Python cancellation event. The current Rust MCP `call_tool` path ignores request cancellation context, matching the existing `wait_for_events` limitation; first release bounds waits to 30 seconds and does not add a cross-process cancellation protocol.
- MCP tool schemas follow the typed registry and committed `schema.json` conventions. The Rust companion already discovers tools dynamically; no Rust source change is needed for the pull tool.

## Approaches

1. **Pull-tool vertical slice (proposed):** native capture, separate journal, permission/settings integration, authenticated `/speech` retrieval, and `wait_for_speech`; defer subscriptions and action correlation. This makes the feature usable through MCP while honoring the proposal's pull-first recommendation.
2. **Capture foundation only:** add the monitor, journal, permission, and local retrieval API, then defer the MCP tool. This narrows MCP integration but leaves the main client workflow unavailable in the first release.
3. **Full streaming integration:** add the vertical slice plus `nvda://speech/recent`, Rust notification batching, and action-trace correlation. This increases cross-process and privacy scope before the pull contract has been validated.

The proposed choice is approach 1, with the NVDA 2026.1 fallback described above. This follows the supplied recommendation to make speech an optional observation channel and postpone subscriptions. It also resolves the phase-table gap by including `wait_for_speech` in the first usable slice.

## Implementation boundaries

- `server/speech.py`: `SpeechJournal`, bounded speech extraction, `SpeechMonitor`, hook registration/unregistration, secure/protected gating, and control events.
- `__init__.py`: create/start the monitor after the bridge exists; stop/unregister it during plugin termination; feed focus protection state from existing focus events.
- `server/http_bridge.py`: own the speech journal/monitor, provide a `/speech` authenticated long-poll route and execution-context wait callback, and clear speech state on permission revocation/reset.
- `server/security.py`: add the non-default, persistable `speech_observation` capability.
- `gui_settings.py`: add the explicit speech-observation checkbox; keep it unchecked by default, restore it on discard/reset, and clear retained speech when revoked.
- `tools/models.py`, `tools/handlers.py`, `tools/nvda_tools.py`: define and register typed `wait_for_speech` input/output and pass cancellation through to the journal wait.
- `schema.json`: regenerate the published registry artifact for the new tool.
- Tests: add focused journal/monitor tests, handler/schema tests, permission persistence/settings tests, and bridge route tests. Keep NVDA runtime acceptance separate from deterministic unit tests.

## Acceptance tests

1. String chunks in a speech sequence are joined in order; command objects and non-string values are ignored; empty output is omitted; text truncates at 1,024 characters and reports truncation.
2. A speech record increments only the speech journal version, never the UI event journal version; retained records are ordered and capped at 256.
3. Speech cursor pagination has no gaps or duplicates; expired history, a mismatched journal ID, reset, timeout, and cancellation are reported explicitly.
4. The monitor registers the supported speech hooks once and unregisters them on shutdown; the legacy fallback is used only when `pre_synthSpeak` is unavailable and emits `phase="queued"`, while the preferred hook emits `phase="about_to_synthesize"`.
5. With no `speech_observation` grant, no speech is captured and reads are denied. Grant enables capture. Revocation and safe reset clear history and rotate the journal ID.
6. Secure-desktop and protected-focus state prevent capture and retrieval. Control events for cancellation and pause/resume carry no speech text.
7. `/speech` requires valid bridge authentication and the observation capability; it clamps timeout/page size and returns the same cursor guarantees as the journal.
8. `wait_for_speech` validates cursors/limits, defaults to the current journal head when `since` is omitted, honors a supplied Python cancellation event, and returns the typed cursor/overflow metadata. Its timeout is capped at 30 seconds; MCP request cancellation has the same bounded-delay behavior as `wait_for_events`.
9. Speech requested through `speak_text` appears like other NVDA speech in the journal; retrieval is explicit and there is no automatic event injection.
10. The generated tool schema matches the registered dataclasses; the Rust companion can discover and call the tool through its existing dynamic tool catalog.

## Validation after implementation

- Run the focused new tests, then the full Python test suite and schema consistency test.
- Run Rust companion tests if any companion-facing contract or artifact change requires it.
- Perform real NVDA acceptance with Notepad, Edge, a dialog, an ARIA live region, cancellation, a paused utterance, and a protected field. Verify hook cleanup and secure-desktop suppression on the supported NVDA versions.
- Compare capture latency and NVDA responsiveness with observation disabled and enabled before considering resource subscriptions.

## External API references

- [NVDA 2026.2 release notes](https://api.nvaccess.org/releases/2026.2/documentation/changes.html) identify `synthDriverHandler.pre_synthSpeak` as a new extension point in 2026.2.
- [NVDA 2026.1 developer guide](https://api.nvaccess.org/releases/2026.1/documentation/developerGuide.html) lists `speech.pre_speechQueued` as an available speech extension point.
- [NVDA developer guide: extension points](https://api.nvaccess.org/releases/stable/documentation/developerGuide.html) documents speech, synthesizer, and secure-desktop extension points.
