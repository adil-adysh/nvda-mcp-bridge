"""Settle: wait for an action's effect to be observable, not for a fixed delay.

The browser measurements in `docs/IMPLEMENTATION_PLAN.md` §3.3 killed the 60 ms
quiet window: in Edge a single typed character produced `valueChange` and then
`stateChange` **146 ms later**, and focus took 117-182 ms to settle. A quiet
window shorter than the slowest participant returns a half-updated observation —
the value changed, the state has not — which is how a tool can report success
while the caller reads a stale screen.

So settle is defined on the observation:

1. project a baseline (and remember the revision),
2. perform the action,
3. wait for activity on the revision counter (thread-safe; no NVDA calls),
4. wait until the burst has been quiet for ``quiet_ms``,
5. project again and compare with the baseline.

``changed: false`` is a real, reportable outcome: the action did nothing. It is
not an error, and a caller must be able to tell it apart from one.

Budget arithmetic: a focus change settles in ~140 ms and one correct projection
costs ~14 ms, so a round costs roughly 180-250 ms. Two or three projections per
action is one authored decision, not a stall.

Nothing here touches NVDA: projection runs through ``dispatch`` (the main thread)
and waiting happens on the calling worker thread.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .state import diff

#: How long a burst must be quiet before the observation is trusted.
DEFAULT_QUIET_MS = 60

#: Longest a single wait sleeps before re-checking cancellation and the budget.
DEFAULT_POLL_FLOOR_MS = 25

#: Default budget for one action. Not per event.
DEFAULT_BUDGET_MS = 1000

#: How long to wait for the *first* sign of activity before concluding that the
#: action produced no events at all. Below the 140 ms median focus settle.
FIRST_ACTIVITY_MS = 250

#: Some changes produce no events (a plain div rewritten 40x/s produced zero), so
#: an action that emitted nothing still gets one projection after this grace.
NO_EVENT_GRACE_MS = 120

#: Two rounds is enough: the second only exists for changes still in flight.
MAX_ROUNDS = 2

#: How many transitions one settle round keeps for its diagnostic record. The
#: window's completeness is reported separately, so a truncated list is never
#: mistaken for a quiet UI.
SETTLE_EVENT_LIMIT = 50


@dataclass
class SettleOutcome:
	"""What settle observed.

	The optional fields exist so an action trace can say *how* the observation
	was obtained, not merely whether something changed: an absent projection, an
	expired budget and an incomplete event window are three different answers,
	and collapsing them into ``changed: false`` is how a caller ends up
	believing a screen it never actually saw.
	"""

	changed: bool
	state_version: int
	settled_ms: float
	delta: dict[str, Any] = field(default_factory=dict)
	uncertain: bool = False
	completed: Optional[bool] = None
	expect_mismatch: Optional[dict[str, Any]] = None
	projection: Optional[dict[str, Any]] = None
	#: The ``expect`` condition that was evaluated, for the trace's record.
	expect: Optional[dict[str, Any]] = None
	#: Event-journal window observed around the action.
	journal_id: Optional[str] = None
	from_event_version: Optional[int] = None
	to_event_version: Optional[int] = None
	events: list[dict[str, Any]] = field(default_factory=list)
	events_complete: bool = False
	events_truncated: bool = False
	#: Projection versions either side of the action.
	baseline_state_version: Optional[int] = None
	final_state_version: Optional[int] = None
	#: ``settled``, ``budget_expired`` or ``unavailable``.
	settling_status: str = "settled"
	budget_ms: int = 0
	budget_expired: bool = False

	@property
	def projection_changed(self) -> bool:
		"""Explicit name for ``changed``; this is a projection diff, not the UI."""
		return self.changed

	@property
	def projection_available(self) -> bool:
		return self.projection is not None

	def as_dict(self) -> dict[str, Any]:
		"""The §4.1 result fields, with unset optional members omitted.

		``changed`` keeps its name for compatibility; it means exactly
		:attr:`projection_changed`, and the action trace spells that out.
		"""
		payload: dict[str, Any] = {
			"ok": True,
			"state_version": self.state_version,
			"changed": self.changed,
			"delta": self.delta,
			"settled_ms": self.settled_ms,
			"uncertain": self.uncertain,
			# Additive: a caller that only reads `changed` is unaffected, and a
			# caller that wants to know whether the observation was complete can
			# now tell without fetching the trace.
			"projection_available": self.projection_available,
			"events_complete": self.events_complete,
			"settling_status": self.settling_status,
		}
		if self.completed is not None:
			payload["completed"] = self.completed
		if self.expect_mismatch:
			payload["expect_mismatch"] = self.expect_mismatch
		return payload


def lookup(projection: Optional[dict], path: str) -> Any:
	"""Read a dotted path out of a projection, e.g. ``focus.name`` or ``children.0.role``."""
	node: Any = projection
	for part in [segment for segment in path.split(".") if segment]:
		if isinstance(node, dict):
			if part not in node:
				return None
			node = node[part]
		elif isinstance(node, (list, tuple)):
			try:
				node = node[int(part)]
			except (ValueError, IndexError):
				return None
		else:
			return None
	return node


@dataclass
class _Expectation:
	mismatch: Optional[dict[str, Any]]

	@property
	def satisfied(self) -> bool:
		return not self.mismatch


def evaluate_expect(projection: Optional[dict], expect: Optional[dict[str, Any]]) -> _Expectation:
	"""Compare every ``path: value`` in ``expect`` against the projection."""
	if not expect:
		return _Expectation(None)
	mismatch: dict[str, Any] = {}
	for path, wanted in expect.items():
		actual = lookup(projection, path)
		if isinstance(wanted, list) and isinstance(actual, list):
			if list(wanted) != list(actual):
				mismatch[path] = {"expected": wanted, "actual": actual}
		elif actual != wanted:
			mismatch[path] = {"expected": wanted, "actual": actual}
	return _Expectation(mismatch or None)


class Settler:
	"""Owns one settle round for one action.

	``dispatch`` runs a callable on NVDA's main thread and waits for it; it is the
	only way this class reaches the screen, and it is expected to raise on timeout
	so the caller sees a deadline rather than a silently stale answer.

	``action_fn`` is called on the *calling* (worker) thread. It must move each of
	its own NVDA operations through ``dispatch``; the settle round will not do it,
	because dispatching the action itself would deadlock as soon as the action
	dispatches an operation of its own.

	``journal`` and ``trace`` are optional diagnostic wiring. When present, the
	round records the event-journal window it observed and the projection it
	ended on, so the answer to "what did this action do" is attached to the
	action rather than left to be reconstructed from a log. Neither can affect
	the round: every call into the trace is non-raising by contract.
	"""

	def __init__(
		self,
		projector: Any,
		dispatch: Callable[[Callable[[], Any]], Any],
		budget_ms: int = DEFAULT_BUDGET_MS,
		quiet_ms: int = DEFAULT_QUIET_MS,
		poll_floor_ms: int = DEFAULT_POLL_FLOOR_MS,
		clock: Callable[[], float] = time.monotonic,
		sleep: Callable[[float], None] = time.sleep,
		journal: Any = None,
		trace: Any = None,
	) -> None:
		self._projector = projector
		self._dispatch = dispatch
		self._budget_ms = max(0, budget_ms)
		self._quiet_ms = max(0, quiet_ms)
		self._poll_floor_ms = max(1, poll_floor_ms)
		self._clock = clock
		self._sleep = sleep
		self._journal = journal
		self._trace = trace

	def run(
		self,
		action_fn: Callable[[], Any],
		expect: Optional[dict[str, Any]] = None,
		budget_ms: Optional[int] = None,
		cancellation_event: Any = None,
	) -> SettleOutcome:
		budget = self._budget_ms if budget_ms is None else max(0, budget_ms)
		start = self._clock()
		deadline = start + budget / 1000.0

		journal = self._journal
		journal_id = getattr(journal, "journal_id", None)
		from_version = journal.version if journal is not None else None
		self._record_settling_start(journal_id, from_version, budget)

		baseline, _changes, version = self._dispatch(self._projector.refresh)
		revision = self._projector.revision

		# The action runs on the *calling* thread, not through ``dispatch``: a
		# coordinator's action performs several main-thread operations and would
		# deadlock if the main thread were busy waiting for the action itself.
		action_result = action_fn()
		uncertain = bool(action_result.get("uncertain")) if isinstance(action_result, dict) else False

		saw_activity = self._projector.wait_for_revision(revision, min(FIRST_ACTIVITY_MS, budget))
		if not saw_activity:
			self._pause(start, NO_EVENT_GRACE_MS, budget, cancellation_event)

		projection = baseline
		changes: dict[str, Any] = {}
		changed = False
		version_now = version
		# Whether the last round saw the burst go quiet. A round that reached
		# quiet produced a trustworthy observation even if it spent the whole
		# budget doing so; only a round that ran out of time while activity was
		# still arriving leaves the final state unknown.
		quiet_observed = False

		for round_index in range(MAX_ROUNDS):
			if round_index > 0:
				# A second round only makes sense while something is still moving.
				revision = self._projector.revision
				if not self._projector.wait_for_revision(revision, min(FIRST_ACTIVITY_MS, budget)):
					break

			# Debounce on the revision counter only: cheap, thread-safe, and it
			# says "something happened", not "the observation changed".
			while True:
				remaining_ms = (deadline - self._clock()) * 1000.0
				if remaining_ms <= 0:
					quiet_observed = False
					break
				current = self._projector.revision
				quiet = not self._projector.wait_for_revision(
					current,
					int(max(self._poll_floor_ms, min(self._quiet_ms, remaining_ms))),
				)
				if quiet:
					quiet_observed = True
					break

			projection, _reported_changes, version_now = self._dispatch(self._projector.refresh)
			# Diff against the *baseline* rather than trusting the projector's own
			# delta: another caller may have refreshed in between, in which case the
			# projector's delta describes a different pair of observations.
			changes = diff(baseline, projection)
			changed = bool(changes)
			if changed:
				break
			# A satisfied postcondition is the other reason to stop. This round has
			# already waited for the burst to go quiet, so the observation is as
			# settled as the budget can make it; spending the next round would only
			# delay an answer that is already correct.
			if expect and evaluate_expect(projection, expect).satisfied:
				break
			if self._clock() >= deadline:
				break

		settled_ms = round((self._clock() - start) * 1000.0, 1)
		expectation = evaluate_expect(projection, expect)
		budget_expired = self._clock() >= deadline and not quiet_observed
		if projection is None:
			settling_status = "unavailable"
		elif budget_expired:
			settling_status = "budget_expired"
		else:
			settling_status = "settled"

		outcome = SettleOutcome(
			changed=changed,
			state_version=version_now,
			settled_ms=settled_ms,
			delta=changes,
			uncertain=uncertain,
			completed=expectation.satisfied if expect else None,
			expect_mismatch=expectation.mismatch,
			projection=projection,
			expect=dict(expect) if expect else None,
			journal_id=journal_id,
			from_event_version=from_version,
			baseline_state_version=version,
			final_state_version=version_now,
			settling_status=settling_status,
			budget_ms=budget,
			budget_expired=budget_expired,
		)
		self._record_events(outcome, journal, from_version)
		self._record_observation(outcome)
		return outcome

	def _record_events(self, outcome: SettleOutcome, journal: Any, from_version: Optional[int]) -> None:
		"""Attach the event window the settle round spanned, with its completeness."""
		if journal is None or from_version is None:
			return
		try:
			page = journal.snapshot(since=from_version, limit=SETTLE_EVENT_LIMIT)
		except Exception:  # noqa: BLE001 — diagnostics must not fail the action
			return
		events = list(page.get("events") or [])
		# `complete` describes this page; `has_more` says the window continued
		# past the page limit, which makes the window incomplete too.
		complete = bool(page.get("complete", not page.get("truncated"))) and not page.get("has_more", False)
		outcome.to_event_version = page.get("to_version", from_version)
		outcome.events = events
		outcome.events_complete = complete
		outcome.events_truncated = not complete

	def _record_settling_start(self, journal_id: Optional[str], from_version: Optional[int], budget_ms: int) -> None:
		trace = self._trace
		if trace is None:
			return
		try:
			trace.record_settling_start(
				journal_id=journal_id,
				event_version=from_version,
				baseline_state_version=self._projector.state_version,
				budget_ms=budget_ms,
			)
		except Exception:  # noqa: BLE001
			pass

	def _record_observation(self, outcome: SettleOutcome) -> None:
		trace = self._trace
		if trace is None:
			return
		try:
			trace.record_observation(outcome, journal_head=self._journal.version if self._journal else None)
		except Exception:  # noqa: BLE001
			pass

	def _pause(self, start: float, grace_ms: int, budget_ms: int, cancellation_event: Any) -> None:
		"""Sleep briefly, respecting the budget and a cancellation signal."""
		elapsed_ms = (self._clock() - start) * 1000.0
		wait_ms = max(0.0, min(float(grace_ms), budget_ms - elapsed_ms))
		if wait_ms <= 0:
			return
		if cancellation_event is not None and hasattr(cancellation_event, "wait"):
			cancellation_event.wait(wait_ms / 1000.0)
		else:
			self._sleep(wait_ms / 1000.0)
