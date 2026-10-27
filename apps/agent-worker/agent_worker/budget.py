"""The per-agent budget, enforced from the runner's own usage stream (BUDGET-CAP, part of #2459).

swarm-api resolves an attempt's budget at submission -- min(step, workspace,
ceiling), each unit on its own (`swarm_api.budget`) -- and pins it in the
signed dispatch block as `metadata.dispatch.budget`: `{usd?, tokens?, from}`.
A task without the key has no budget, and this module is never built for it:
its child environment, its CLI settings and its supervision are exactly what
they were before #2459.

WHAT IS COUNTED (BGD, 2026-10-26).
  * TOKENS are the runner's own usage reports: input plus output plus cache
    reads, as reported. claude-code's stream-json carries each message's
    `usage` on its `assistant` events (the last copy of each message id
    counts, as `lifecycle._stream_usage` counts it); codex's `--json` stream
    (the codex profile runs `codex exec --json` since CODEX-JSON, BGD's
    answer to BUDGET-CAP question 6) carries a `turn.completed` event's
    `usage`, once per start, cached input inside its input. Cache WRITES are not counted:
    the owner's rule names input, output and cache reads.
  * DOLLARS are the API-price estimate the worker already records as
    `cost_usd`. A live run has not reported its cost yet -- claude-code
    prints `total_cost_usd` only in its last event -- so the running figure
    is the per-model usage priced by swarm-api (`spendprice`, the figure a
    stopped run's `cost_usd` is), at most once every PRICE_INTERVAL_SECONDS.
    The worker holds no price table (#1481).

WHO DOES WHAT. Two processes, the way the account channel is split
(`cliagent.AccountStreamWatcher`):
  * the RUNNER reads the CLI's stream as it is written
    (`runners.budgetwatch.BudgetStreamWatcher`), writes the usage it has seen
    to the CHANNEL file, and stops the CLI when the SIGNAL file says so: at
    the next turn boundary for `stop`, at once for `kill`;
  * the WORKER (`BudgetGuard`, driven from the lifecycle's supervision loop)
    reads the channel, prices it, and decides.

THE THRESHOLDS.
  * 80% (WARN_AT): the agent is told to wrap up and publish, by the one
    mechanism claude-code allows mid-run -- a PostToolUse hook's
    `additionalContext` (`runners/budget_note_hook.py`), which reads the NOTE
    file once -- and a `budget_warning` event is recorded for the console
    (`EventType.RUNNING` with that cause, as `account_assigned` rides on it,
    until contract request 121 gives it a type). codex has no such mechanism
    in `codex exec`: its event says `told: false`.
  * 100% (STOP_AT): the runner stops the CLI at its next turn boundary; the
    lifecycle then checkpoints, publishes what the attempt has exactly as a
    timed-out step does, and ends the step FAILED with the end cause
    `budget_exceeded` and the numbers (`summary`, `error`).
  * 120% (KILL_AT): the one case it stops mid-turn -- a single turn that runs
    past 120% of a limit is killed where it is.

`budget_exceeded` IS NOT YET A FROZEN END CAUSE (contract request 118). Until
it is, the step ends with `end_cause` null -- the contract's own rule for "a
writer ended it for a reason with no value here" (`EndCause`'s docstring) --
its `last_error` beginning `budget_exceeded:`, and `result_summary.budget`
carrying `end_cause: "budget_exceeded"` and the numbers.

Never raises into the supervision loop: a budget that cannot be read or
priced leaves the agent running as it would without one, said in the log.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

#: `metadata.dispatch.budget`, as `swarm_api.budget.DISPATCH_KEY` writes it.
DISPATCH_KEY = "budget"

#: The files the worker and its runner talk through, named to the runner in
#: its environment. All three are in the worker's `private/`, never `work/`,
#: which a checkpoint archives and the agent can write.
CHANNEL_ENV = "SWARM_BUDGET_CHANNEL"
SIGNAL_ENV = "SWARM_BUDGET_SIGNAL"
NOTE_ENV = "SWARM_BUDGET_NOTE"

WARN_AT = 0.8
STOP_AT = 1.0
KILL_AT = 1.2

#: What the signal file asks of the runner.
ACTION_STOP = "stop"
ACTION_KILL = "kill"
#: What the runner says it stopped the CLI for, in the channel.
STOPPED_AT_BOUNDARY = "budget_turn_boundary"
STOPPED_MID_TURN = "budget_mid_turn"
STOPPED_FOR = (STOPPED_AT_BOUNDARY, STOPPED_MID_TURN)

#: The end cause (contract request 118) and the event's cause.
END_CAUSE = "budget_exceeded"
WARNING_CAUSE = "budget_warning"
#: Where the numbers go in the step's result summary.
SUMMARY_KEY = "budget"

#: How often the running dollar figure is asked of swarm-api, at most. One
#: request per new usage, throttled: turns can be seconds apart, and the
#: stop waits for the next boundary anyway.
PRICE_INTERVAL_SECONDS = 20.0

#: What the agent is told at 80%, through the hook.
WRAP_UP_NOTE = (
    "Budget notice from the platform: this step has used {percent}% of its budget "
    "({used} of {limit}). Wrap up now: finish the change you are making, commit it, "
    "write the outputs your brief asks for (pr-title.txt, pr-body.md and any others), "
    "and end. At 100% the step is stopped at its next turn boundary and what it has "
    "is published as it stands."
)

_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
#: codex `--json` items that run something and hand its result back to the
#: model (`UsageMeter.feed`): their completion is a turn boundary. Not
#: `agent_message`, `reasoning`, `todo_list` or `error`, which return nothing
#: to the model and so stop nothing halfway.
CODEX_TOOL_ITEMS = frozenset({"command_execution", "file_change", "mcp_tool_call", "web_search"})
#: The model a message that names none is counted under: swarm-api prices
#: nothing for it, so the dollar figure reads "not known" rather than one
#: that left those tokens out (as `lifecycle._UNNAMED_MODEL`).
UNNAMED_MODEL = "unknown"


@dataclass(frozen=True)
class Limits:
    """The attempt's limits; None is "no limit in that unit"."""

    usd: float | None = None
    tokens: int | None = None

    @classmethod
    def from_dispatch(cls, dispatch: Mapping[str, Any] | None) -> "Limits | None":
        """The pinned budget, or None for a task that has none. A unit that is
        not a positive number is no limit in that unit; a block with neither
        is no budget at all."""
        block = (dispatch or {}).get(DISPATCH_KEY)
        if not isinstance(block, Mapping):
            return None
        usd, tokens = block.get("usd"), block.get("tokens")
        limits = cls(
            usd=float(usd) if _positive(usd) else None,
            tokens=int(tokens) if _positive(tokens) and isinstance(tokens, int) else None,
        )
        return None if limits.usd is None and limits.tokens is None else limits

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.usd is not None:
            out["usd"] = self.usd
        if self.tokens is not None:
            out["tokens"] = self.tokens
        return out


def _positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _count(usage: Mapping[str, Any], key: str) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def tokens_of(usage: Mapping[str, Any]) -> int:
    """Input plus output plus cache reads, as the runner reported them."""
    return (_count(usage, "input_tokens") + _count(usage, "output_tokens")
            + _count(usage, "cache_read_input_tokens"))


class UsageMeter:
    """The usage a CLI's stream has reported so far, and its turn boundaries.

    `feed(event)` answers True when the event is a TURN BOUNDARY: a top-level
    `user` event of claude-code's stream (the tool results of a turn are in,
    the next model request not yet answered -- the boundary the account swap
    uses); for codex, a completed tool item (`CODEX_TOOL_ITEMS`: the same
    point, the tool's result in and the model not yet answering it) or its
    `turn.completed`.

    CODEX REPORTS USAGE ONCE A TURN, and `codex exec` runs ONE turn per start
    (codex-cli 0.154.0, `--json`): its usage reaches the meter when the start
    ends, not as it goes. So a codex step is measured per start, and a stop
    the worker has asked -- the budget spent by an earlier start, folded in
    by `BudgetGuard.runner_ended` -- is honoured at the next completed tool
    item rather than after a whole further turn.
    """

    def __init__(self) -> None:
        self._messages: dict[str, tuple[str, dict[str, int]]] = {}
        self._anonymous = 0
        self.turns = 0

    def feed(self, event: Mapping[str, Any]) -> bool:
        kind = event.get("type")
        if kind == "assistant":
            message = event.get("message")
            if isinstance(message, Mapping) and isinstance(message.get("usage"), Mapping):
                key = message.get("id")
                if not isinstance(key, str) or not key:
                    self._anonymous += 1
                    key = f"\x00{self._anonymous}"
                model = message.get("model")
                self._messages[key] = (
                    model if isinstance(model, str) and model else UNNAMED_MODEL,
                    {k: _count(message["usage"], k) for k in _USAGE_FIELDS},
                )
            return False
        if kind == "user" and not event.get("parent_tool_use_id"):
            self.turns += 1
            return True
        if kind == "item.completed":
            # A codex tool's result is in and the model has not answered it:
            # the codex form of claude-code's top-level `user` event. Not a
            # turn of usage (codex reports usage only at `turn.completed`,
            # once per `codex exec` start), so `turns` is not counted.
            item = event.get("item")
            return isinstance(item, Mapping) and item.get("type") in CODEX_TOOL_ITEMS
        if kind == "turn.completed":
            usage = event.get("usage")
            if isinstance(usage, Mapping):
                self._anonymous += 1
                # codex reports cached input inside its input count.
                cached = _count(usage, "cached_input_tokens")
                self._messages[f"\x00{self._anonymous}"] = (UNNAMED_MODEL, {
                    "input_tokens": max(_count(usage, "input_tokens") - cached, 0),
                    "output_tokens": _count(usage, "output_tokens"),
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": cached,
                })
            self.turns += 1
            return True
        return False

    def tokens(self) -> int:
        return sum(tokens_of(usage) for _model, usage in self._messages.values())

    def usage_by_model(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for model, usage in self._messages.values():
            mine = out.setdefault(model, dict.fromkeys(_USAGE_FIELDS, 0))
            for k in _USAGE_FIELDS:
                mine[k] += usage[k]
        return out


def merge_usage(*tables: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    """Per-model usage tables added together."""
    out: dict[str, dict[str, int]] = {}
    for table in tables:
        for model, usage in (table or {}).items():
            if not isinstance(model, str) or not isinstance(usage, Mapping):
                continue
            mine = out.setdefault(model, dict.fromkeys(_USAGE_FIELDS, 0))
            for k in _USAGE_FIELDS:
                mine[k] += _count(usage, k)
    return out


def fraction(limits: Limits, *, tokens: int, usd: float | None) -> float:
    """The largest share of any limit used; a dollar figure not known counts 0."""
    shares = [0.0]
    if limits.tokens is not None:
        shares.append(tokens / limits.tokens)
    if limits.usd is not None and usd is not None:
        shares.append(usd / limits.usd)
    return max(shares)


def action_for(share: float, *, warned: bool, stop_asked: bool) -> str | None:
    """What a share of the budget calls for now: `warn` once at 80%, `stop`
    once at 100%, `kill` at 120%, or None."""
    if share >= KILL_AT:
        return ACTION_KILL
    if share >= STOP_AT and not stop_asked:
        return ACTION_STOP
    if share >= WARN_AT and not warned and not stop_asked:
        return "warn"
    return None


def _describe(limits: Limits) -> str:
    parts = []
    if limits.usd is not None:
        parts.append(f"${limits.usd:,.2f}")
    if limits.tokens is not None:
        parts.append(f"{limits.tokens:,} tokens")
    return " and ".join(parts)


def _used(limits: Limits, tokens: int, usd: float | None) -> str:
    parts = []
    if limits.usd is not None:
        parts.append(f"${usd:,.2f}" if usd is not None else "an unpriced amount")
    if limits.tokens is not None:
        parts.append(f"{tokens:,} tokens")
    return " and ".join(parts)


def _write_private(path: Path, body: str) -> None:
    """`path`, replaced atomically, mode 0600."""
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(body)
    os.replace(tmp, path)


def read_json(path: Path) -> dict[str, Any]:
    """The file's JSON object, or {} for one that is absent or not an object."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class BudgetGuard:
    """One attempt's budget, on the worker's side. Built only for a budgeted task.

    `pricer` prices a per-model usage table in dollars (`SpendPricer.price`),
    or is None when the attempt has no dollar limit. `emit` records one task
    event's detail. `can_tell` says whether the runner can put the 80% note in
    front of the agent (claude-code's hook).
    """

    def __init__(
        self,
        limits: Limits,
        *,
        private: Path,
        emit: Callable[[dict[str, Any]], None],
        log: Any,
        pricer: Callable[[Mapping[str, Mapping[str, Any]]], float | None] | None = None,
        can_tell: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = limits
        self.channel = private / "budget-channel.json"
        self.signal = private / "budget-signal.json"
        self.note = private / "budget-note.json"
        self._emit = emit
        self.log = log
        self._pricer = pricer if limits.usd is not None else None
        self._can_tell = can_tell
        self._clock = clock
        #: Usage of the runners that have ended in this attempt (an account
        #: move or a credential reload starts another, continuing the session).
        self._before: dict[str, dict[str, int]] = {}
        self._current: dict[str, dict[str, int]] = {}
        self._seen: int | None = None
        self._priced_usage: dict[str, dict[str, int]] | None = None
        self._priced_at: float | None = None
        self.usd: float | None = None
        self.tokens = 0
        self.warned = False
        self.stop_asked = False
        self.kill_asked = False
        #: Set when a runner said it stopped the CLI for the budget.
        self.stopped_for: str | None = None
        for stale in (self.channel, self.signal, self.note,
                      self.note.with_name(self.note.name + ".told")):
            stale.unlink(missing_ok=True)

    # -- the runner's environment -------------------------------------------
    def env(self) -> dict[str, str]:
        return {CHANNEL_ENV: str(self.channel), SIGNAL_ENV: str(self.signal),
                NOTE_ENV: str(self.note)}

    @property
    def exceeded(self) -> bool:
        """The budget stopped the agent: the step ends `budget_exceeded`."""
        return self.stopped_for in STOPPED_FOR

    def usage(self) -> dict[str, dict[str, int]]:
        return merge_usage(self._before, self._current)

    # -- while the runner runs ------------------------------------------------
    def watch(self) -> str | None:
        """One look at the channel; the action taken, or None. Never raises."""
        try:
            return self._watch()
        except Exception as exc:
            self.log.warning("could not check this attempt's budget; the agent carries on",
                             error_type=type(exc).__name__)
            return None

    def _watch(self) -> str | None:
        try:
            seen = self.channel.stat().st_mtime_ns
        except OSError:
            seen = None
        if seen is not None and seen != self._seen:
            self._seen = seen
            data = read_json(self.channel)
            usage = data.get("usage_by_model")
            self._current = merge_usage(usage if isinstance(usage, Mapping) else {})
        return self._decide()

    def _refresh(self) -> None:
        """The running figures from the usage seen so far."""
        usage = self.usage()
        self.tokens = sum(tokens_of(u) for u in usage.values())
        self._price(usage)

    def _decide(self) -> str | None:
        self._refresh()
        share = fraction(self.limits, tokens=self.tokens, usd=self.usd)
        action = action_for(share, warned=self.warned, stop_asked=self.stop_asked)
        if action == ACTION_KILL and self.kill_asked:
            return None
        if action == "warn":
            self._warn(share)
        elif action == ACTION_STOP:
            self.stop_asked = True
            _write_private(self.signal, json.dumps({"action": ACTION_STOP}))
            self.log.warning("this attempt has spent its budget; the agent stops at its "
                             "next turn boundary", **self._numbers())
        elif action == ACTION_KILL:
            self.stop_asked = self.kill_asked = True
            _write_private(self.signal, json.dumps({"action": ACTION_KILL}))
            self.log.warning("this attempt's turn ran past 120% of its budget; the agent is "
                             "stopped mid-turn", **self._numbers())
        return action

    def _price(self, usage: dict[str, dict[str, int]]) -> None:
        if self._pricer is None or not usage or usage == self._priced_usage:
            return
        now = self._clock()
        if self._priced_at is not None and now - self._priced_at < PRICE_INTERVAL_SECONDS:
            return
        self._priced_at = now
        self._priced_usage = usage
        price = self._pricer(usage)
        if price is not None:
            self.usd = float(price)

    def _warn(self, share: float) -> None:
        self.warned = True
        percent = int(share * 100)
        told = False
        if self._can_tell:
            _write_private(self.note, json.dumps({"message": WRAP_UP_NOTE.format(
                percent=percent, used=_used(self.limits, self.tokens, self.usd),
                limit=_describe(self.limits))}))
            told = True
        self.log.warning("this attempt has used 80% of its budget; the agent is told to "
                         "wrap up and publish", told=told, **self._numbers())
        try:
            self._emit({"cause": WARNING_CAUSE, "percent": percent, "told": told,
                        **self._numbers()})
        except Exception as exc:
            self.log.warning("could not record the budget warning on the task's events",
                             error_type=type(exc).__name__)

    def _numbers(self) -> dict[str, Any]:
        return {"limit": self.limits.as_dict(),
                "used": {"tokens": self.tokens,
                         **({"usd": round(self.usd, 6)} if self.usd is not None else {})}}

    # -- when a runner has ended ---------------------------------------------
    def runner_ended(self) -> None:
        """Fold the ended runner's usage in and read why it stopped. Never raises."""
        try:
            data = read_json(self.channel)
            usage = data.get("usage_by_model")
            final = merge_usage(usage if isinstance(usage, Mapping) else {})
            self._before = merge_usage(self._before, final or self._current)
            self._current = {}
            self._seen = None
            self.channel.unlink(missing_ok=True)
            stopped = data.get("stopped_for")
            if stopped in STOPPED_FOR:
                self.stopped_for = stopped
            # The last figure, priced now whatever the throttle says.
            self._priced_at = None
            self._refresh()
        except Exception as exc:
            self.log.warning("could not read the runner's budget channel",
                             error_type=type(exc).__name__)

    # -- the end ---------------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """`result_summary.budget`: the limits, what was used, how it ended."""
        out: dict[str, Any] = {**self._numbers(), "warned": self.warned}
        if self.exceeded:
            out["end_cause"] = END_CAUSE
            out["stopped"] = ("turn_boundary" if self.stopped_for == STOPPED_AT_BOUNDARY
                              else "mid_turn")
        return out

    def error(self) -> str:
        """The step's `last_error` when the budget ended it."""
        where = ("at a turn boundary" if self.stopped_for == STOPPED_AT_BOUNDARY
                 else "mid-turn, past 120% of the limit")
        return (f"{END_CAUSE}: the agent used {_used(self.limits, self.tokens, self.usd)} "
                f"of a budget of {_describe(self.limits)} and was stopped {where}; "
                f"its work was checkpointed and published as it stood")


__all__ = [
    "ACTION_KILL",
    "ACTION_STOP",
    "BudgetGuard",
    "CHANNEL_ENV",
    "CODEX_TOOL_ITEMS",
    "DISPATCH_KEY",
    "END_CAUSE",
    "KILL_AT",
    "Limits",
    "NOTE_ENV",
    "SIGNAL_ENV",
    "STOPPED_AT_BOUNDARY",
    "STOPPED_MID_TURN",
    "STOP_AT",
    "SUMMARY_KEY",
    "UsageMeter",
    "WARNING_CAUSE",
    "WARN_AT",
    "action_for",
    "fraction",
    "merge_usage",
    "read_json",
    "tokens_of",
]
