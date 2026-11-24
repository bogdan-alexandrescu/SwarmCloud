"""A runner's run and its spend: the moment a runner ends, its entry in
`_runs` opened and closed, the attempt's spend collected from its result and
priced when it was stopped before it could say, and the spend written onto the
attempt document.

Moved out of `lifecycle.py` unchanged (issue 68, a slice of splitting that file
by concern): `Worker` inherits `SpendMixin`, so the methods keep their `self`
access and every `worker._child_ended(...)` call and test still resolves.
`_RUN_TOKEN_KEYS` and `_reread_of` live here alone (issue 193); `lifecycle` no
longer binds them, nor `_unpriced_sessions` and `_cost_incomplete`, which only
`_collect_spend` read through it. `_read_json` and `_rfc3339_now` stay in
`lifecycle`, which uses them outside this concern too, and are imported where
they are used: a module-level import of `lifecycle` from here would be
circular.
"""

from __future__ import annotations

from typing import Any

from .control import ControlPlane
from .procman import ChildResult
from .runners.streams import agent_stream_files
from .usage import (
    _add_spend,
    _cost_incomplete,
    _stream_usage,
    _unpriced_sessions,
    _usage_summary,
)

#: The token counts a run's entry in `_runs` carries, each its own share.
_RUN_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _reread_of(channel: dict[str, Any]) -> dict[str, Any]:
    """A run's re-read cost from its account channel (`cliagent.AccountStreamWatcher`).

    `reached: true` with the seconds and tokens spent before the run's first
    action and the tool it was; `reached: false` with everything the run
    spent when it never acted; `measured: false` when the channel said
    nothing (a runner that wrote none). Numbers only, never stream text.
    """
    def figures(block: Any) -> dict[str, Any] | None:
        if not isinstance(block, dict):
            return None
        seconds = block.get("seconds")
        tokens = block.get("tokens")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            return None
        counts = {
            key: int(tokens[key])
            for key in _RUN_TOKEN_KEYS
            if isinstance(tokens, dict) and isinstance(tokens.get(key), int)
            and not isinstance(tokens.get(key), bool)
        }
        return {"seconds": round(float(seconds), 3), "tokens": counts}

    first = channel.get("first_action")
    reached = figures(first)
    if reached is not None:
        tool = first.get("tool") if isinstance(first, dict) else None
        if isinstance(tool, str) and tool:
            reached["tool"] = tool[:80]
        return {"reached": True, **reached}
    spent = figures(channel.get("spent"))
    if spent is not None:
        return {"reached": False, **spent}
    return {"measured": False}


class SpendMixin:
    """The run and spend methods of `lifecycle.Worker`, which inherits them."""

    # What `lifecycle.Worker` supplies to these methods; annotations only (no
    # value, so nothing is bound at runtime), so the type checker resolves them.
    _account: Any
    _child: Any
    _credential_kept: Any
    _just_swapped: Any
    _read_account_channel: Any
    _runs: Any
    _stop_sampler: Any
    _writes_forbidden: Any
    budget: Any
    cfg: Any
    control: ControlPlane
    log: Any
    spend_pricer: Any
    ws: Any

    def _child_ended(self, result: ChildResult | None = None) -> None:
        """Everything that has to happen the moment a runner is gone.

        Called at every site that reaps a runner, so a new site gets every
        part by calling one thing: the resource sample is stopped and
        written, the run's spend is taken before a restart can overwrite
        the result.json it is in, and the run is closed in `_runs` with what
        it spent. `result` is the reaped runner's, where the site kept it.
        """
        self._stop_sampler()
        before = dict(self._spend)
        self._collect_spend()
        if self.budget is not None:
            self.budget.runner_ended()
        self._close_run(result, spent_before=before)

    def _open_run(self, *, resumed: bool) -> None:
        """Start this runner's entry in `_runs` (SWAP-RUNS). Never raises."""
        from .lifecycle import _rfc3339_now

        account = self._account
        self._run_open = {
            "run": len(self._runs) + 1,
            "started_at": _rfc3339_now(),
            "account": account.account_id if account is not None else None,
            "resumed": resumed,
            # `_just_swapped` is set by a swap and read after the next run
            # ends, so here it says this run is the one the swap started.
            "after_swap": bool(self._just_swapped),
        }

    def _close_run(self, result: ChildResult | None, *, spent_before: dict[str, Any]) -> None:
        """Close the run `_open_run` started, once. Never raises.

        Its duration and exit are the reaped runner's; its tokens and cost
        are what `_collect_spend` added for it -- the difference, because a
        resumed session reports the session's running totals (`_add_spend`).
        A run that followed a swap also carries `reread`: the seconds and
        tokens it spent before its first action (`cliagent.is_action`), as
        the runner's account channel measured them, or everything it spent
        when it never acted (`reached: false`).
        """
        from .lifecycle import _rfc3339_now

        entry, self._run_open = self._run_open, None
        if entry is None:
            return
        try:
            child = self._child
            if result is not None:
                seconds, exit_code, timed_out = (
                    result.duration_seconds, result.exit_code, result.timed_out)
            else:
                # None when there is no child or it never started (issue 56).
                seconds = child.elapsed_seconds if child is not None else None
                exit_code = child.poll() if child is not None else None
                timed_out = False
            entry["ended_at"] = _rfc3339_now()
            entry["duration_s"] = round(float(seconds), 3) if seconds is not None else None
            entry["exit_code"] = exit_code
            if timed_out:
                entry["timed_out"] = True
            tokens = {
                key: self._spend[key] - spent_before.get(key, 0)
                for key in _RUN_TOKEN_KEYS
                if key in self._spend
            }
            if tokens:
                entry["tokens"] = tokens
            if "total_cost_usd" in self._spend:
                entry["cost_usd"] = round(
                    self._spend["total_cost_usd"] - spent_before.get("total_cost_usd", 0), 10)
            if entry.get("after_swap") and self.ws is not None:
                entry["reread"] = _reread_of(self._read_account_channel())
        except Exception as exc:  # noqa: BLE001 # pragma: no cover - defensive; teardown path
            self.log.warning("could not close the run's record", error=str(exc))
        self._runs.append(entry)

    def _collect_spend(self) -> None:
        """Add the runner that just ended to this attempt's spend. Never raises.

        ONCE PER RUNNER: `_spend_pending` is set when a runner starts and
        cleared here, so the `_cleanup` backstop cannot count a run twice.

        Reads result.json knowing it is THIS runner's or nothing: the reply
        files are cleared before every start (`_run_child_supervised`), so a
        runner killed before writing leaves no file here rather than a
        restored one from a previous attempt.
        """
        from .lifecycle import _read_json

        if not self._spend_pending or self.ws is None:
            return
        self._spend_pending = False
        try:
            result = _read_json(self.ws.result_path) or {}
            usage = _usage_summary(result.get("output"))
            unpriced = _unpriced_sessions(result.get("output"))
            incomplete = _cost_incomplete(result)
        except Exception as exc:  # noqa: BLE001 # pragma: no cover - defensive; teardown path
            self.log.warning("could not read the runner's spend", error=str(exc))
            return
        if incomplete:
            # A SESSION WHOSE USAGE WAS NOT READ (COST-ACROSS-SESSIONS, part
            # of archived issue 2688): the runner dropped its partial `total_cost_usd`, so
            # what follows estimates the capture or reports no cost. Kept
            # sticky on the attempt's spend and said in the summary
            # (`result_summary.cost_incomplete`), never inferred from
            # `cost_estimated` alone, which a priced stopped run also sets.
            self._spend["cost_incomplete"] = True
        stopped_cost: float | None = None
        if "total_cost_usd" in usage and unpriced:
            # SESSIONS A CONTEXT-CEILING HANDOFF ENDED (archived issue 2896): stopped at a
            # turn boundary, they printed no result event, and the reported
            # `total_cost_usd` is the last session's alone. Their per-model
            # counts are priced by swarm-api like a stopped run's and added,
            # so the attempt's cost is every session's. Only here: a run with
            # no reported cost at all is priced from its whole stream below,
            # which covers these sessions already. They ended before the
            # last session began, so their cost goes on the session base
            # below, never into the last session's running total: a later
            # runner that RESUMES that session must neither lose it nor
            # count it twice (`_add_spend`).
            cost, tier_estimated = self.spend_pricer.estimate(unpriced)
            if cost is None:
                self.log.warning(
                    "the cost of the sessions a context-ceiling handoff ended is not "
                    "reported; the attempt's cost covers its other sessions only",
                    sessions_models=sorted(unpriced))
            else:
                stopped_cost = cost
                self._spend["cost_estimated"] = True
                if tier_estimated:
                    self._spend["tier_estimated"] = True
        if "total_cost_usd" not in usage:
            # A RUN STOPPED BEFORE IT COULD SAY (archived issue 627): a CLI killed on a
            # cancel, a SIGTERM, a fence or a timeout prints no result event.
            # What its stream showed is summed per model, and only the keys
            # the run did not report itself are taken, so nothing is counted
            # twice. SWARM-API PRICES IT (archived issue 1481): the worker sends the usage
            # per model and takes back a figure or None -- "cost not
            # reported" -- never one of its own.
            estimate = self._stopped_run_usage()
            by_model = estimate.pop("usage_by_model", None) or {}
            more = {k: v for k, v in estimate.items() if k not in usage}
            tier_estimated = False
            if by_model:
                cost, tier_estimated = self.spend_pricer.estimate(by_model)
                if cost is not None:
                    more["total_cost_usd"] = cost
            if more:
                usage = {**usage, **more}
                if "total_cost_usd" in more:
                    self._spend["cost_estimated"] = True
                    if tier_estimated:
                        # A tiered model's tier was picked from summed counts,
                        # not per request (PRICES-TIER-PROMPT).
                        self._spend["tier_estimated"] = True
        self._note_model_mismatch(usage.get("models"))
        if stopped_cost is not None:
            self._spend = _add_spend(self._spend, {"total_cost_usd": stopped_cost})
            if self._session_spend_base is not None:
                self._session_spend_base = _add_spend(
                    self._session_spend_base, {"total_cost_usd": stopped_cost})
        if usage:
            self._spend = _add_spend(
                self._spend,
                usage,
                session_base=self._session_spend_base if self._spend_resumes_session else None,
            )

    def _note_model_mismatch(self, models: Any) -> None:
        """Log `model_mismatch` when the stream ran none of the pinned model.

        docs/design/model-per-step.md §4.6. `Attempt.model` is what the
        worker was started with; `usage.models` is what the CLI's stream says
        it ran. The two are different facts, and a pinned CLI that
        substituted another model shows as a disagreement here. Judged as
        "the pin is not among them", not "anything else is": the CLI also
        reports the small model it runs for its own housekeeping beside the
        one it was told to run, and that is not a substitution. A `[...]`
        context suffix (`claude-opus-5-5[1m]`) is the same model.
        """
        pinned = self.cfg.model
        if not pinned or not isinstance(models, list) or not models:
            return
        reported = sorted(str(m) for m in models)
        if any(m.split("[", 1)[0] == pinned for m in reported):
            return
        self.log.warning(
            "model_mismatch: the agent's stream reports models other than the pinned one",
            cause="model_mismatch",
            job_model=pinned,
            stream_models=reported,
        )

    def _note_runner_spend_start(self, *, resumed: bool) -> None:
        """Say, at a runner's start, whose session its spend will report.

        A runner that starts a NEW session -- every first start, and a restart
        from the prompt -- marks the spend so far as what was spent before
        that session. A runner that RESUMES one leaves the mark where the
        session began, so `_add_spend` can replace the session's earlier
        figures with its later, cumulative ones (archived issue 667, observer P14).
        """
        self._spend_resumes_session = resumed
        if not resumed or self._session_spend_base is None:
            self._session_spend_base = dict(self._spend)

    def _stopped_run_usage(self) -> dict[str, Any]:
        """`_stream_usage` over this profile's agent capture, or {}."""
        files = agent_stream_files(self.cfg.runner_profile)
        if files is None or self.ws is None:
            return {}
        try:
            return _stream_usage(self.ws.artifacts / files.stdout)
        except Exception as exc:  # noqa: BLE001 # pragma: no cover - defensive; teardown path
            self.log.warning("could not estimate the stopped run's spend", error=str(exc))
            return {}

    def _record_spend(self) -> None:
        """Write what this attempt has spent onto its attempt document. Never raises.

        Called from every exit (see `_upload_outputs` and `_cleanup`), and
        writes only when there is something new: `record_spend` merge-sets
        absolute totals, so writing the same figure twice is harmless and
        writing a larger one later -- a crash that reaped its runner after the
        first write -- corrects it.

        Not fatal. An attempt that ran is not a failed attempt because its
        accounting write failed, and this runs on the teardown path where the
        lease is about to be released either way.
        """
        kept = self._credential_kept
        if self._writes_forbidden or (not self._spend and kept is None):
            return
        snapshot = dict(self._spend)
        if kept is not None:
            # Request 152: the credential the attempt KEPT LAST, beside spend.
            snapshot["credential"] = dict(kept)
        if snapshot == self._spend_recorded:
            return
        try:
            self.control.record_spend(snapshot)
        except Exception as exc:  # noqa: BLE001 -- logged; spend is never fatal
            self.log.warning("could not record spend", error=f"{type(exc).__name__}: {exc}")
            return
        self._spend_recorded = snapshot
