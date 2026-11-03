"""An attempt's Jev spend through swarm-api's state routes (lane AQ-JEV-ACCOUNTING).

WHY. A tenant switched to the state routes holds no Firestore client, so the
direct path's `budget.settle_attempt` cannot run there: until this module,
its calls were never settled, and with a monthly cap set every call was
skipped, because an unread month counts as spent. The worker reaches
swarm-api only through its signed `statecall.StateCall`, so the accounting
goes through three routes beside the others (`swarm_api.jevaccount`):

* `state/jev-reserve`, at admission (`judge.attempt.setup`): swarm-api
  reserves a bounded allowance for this attempt in ONE transaction, from its
  own documents -- the workspace's signed-off cap, the month's spend, every
  other live reservation, the task's earlier attempts. The body carries the
  tuple and nothing else: the worker names no number, so editing the local
  ledger cannot raise what is reserved. Two attempts reserving at once are
  serialised by the transaction: they cannot both take the last of the month.
* `state/jev-usage`, one record per Jev call, keyed by its `call_id`: a
  replayed record (a lost answer, a settle run again) is answered without a
  second charge.
* `state/jev-settle`, when the attempt ends however it ends -- succeeded,
  failed, parked, cancelled, retried or crashed in the worker -- releases
  what the reservation still holds and records the outcome, once. A worker
  that died without settling is reconciled by swarm-api: the task's next
  attempt's reserve releases the superseded reservation and marks the
  attempt `abandoned` with its unreported spend UNKNOWN, never 0; a
  reservation nobody renews expires.

The records are the ledger's audit rows, sent by the WORKER after the agent
(the agent's own process cannot sign a state call). Nothing here sends a
payload, an answer, a key or a question: the fields named in `call_record`.

THE QUALITY HOOKS AND THE JEV BROWSER (owner's pick on #3111). jev-belay,
jev-enforce and the live browser call Jev themselves, through the image's
launcher (`swarm-jev-gate.mjs`), which appends one record per call to
`private/jev-gates.calls` or `private/jev-browser.calls`. On every exit,
before the settlement, `record_tool_calls` sends each one through the same
`state/jev-usage`, with source `quality_hook` or `browser`: the month's
total and the reservation's remainder count them beside the judgments'. A
file's sent offset is kept beside it (`.sent`), so a second exit sends only
what is new, and a resend after a lost answer is a replay that charges
nothing. These tools are not admitted against the ledger -- they are the
workspace's hooks, not the judgments' budget -- so their records are spend
reported, never refused; a record the file cannot hold is skipped.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from agent_worker.judge import budget

RESERVE_OP = "jev-reserve"
USAGE_OP = "jev-usage"
SETTLE_OP = "jev-settle"
#: Who made a call: the worker's and the agent's judgments (every ledger row
#: today), a quality hook (jev-belay, jev-enforce), the live browser.
SOURCE_JUDGMENT = "judgment"
SOURCE_QUALITY_HOOK = "quality_hook"
SOURCE_BROWSER = "browser"
SOURCES = (SOURCE_JUDGMENT, SOURCE_QUALITY_HOOK, SOURCE_BROWSER)
#: The launcher's per-call files, in `private/`: the hooks' (the gates'
#: config `calls_file`, `qualitygates.CALLS_FILE_NAME`) and the browser's
#: (`runners/jev_launch.BROWSER_CALLS_FILE_NAME`), with the source each may
#: name. tests/unit/worker/test_jev_tool_calls.py holds the names equal.
TOOL_CALL_FILES = (("jev-gates.calls", SOURCE_QUALITY_HOOK),
                   ("jev-browser.calls", SOURCE_BROWSER))
#: A file's sent offset, beside it.
SENT_SUFFIX = ".sent"
#: A calls file larger than this is not the launcher's: nothing is read
#: past it. One line longer than MAX_LINE_BYTES is not a record.
MAX_CALLS_BYTES = 4 * 1024 * 1024
MAX_LINE_BYTES = 4096
#: How an attempt ended, as `state/jev-settle` takes it.
OUTCOMES = ("succeeded", "failed", "parked", "cancelled", "retried", "crashed")
#: The end the attempt committed (`control.committed_end`, a `TaskState`
#: value) as an outcome. READY is a retry's or a person's return: another
#: attempt follows either way. No committed end -- a crash, a fence, an exit
#: before any end was written -- is `crashed`.
ENDS = {"SUCCEEDED": "succeeded", "FAILED": "failed", "DEAD_LETTERED": "failed",
        "CANCELLED": "cancelled", "PARKED": "parked", "READY": "retried",
        "QUEUED": "retried"}


def outcome_of(committed_end: Any) -> str:
    """`ENDS` of a `TaskState` (or its value); `crashed` for anything else."""
    value = getattr(committed_end, "value", committed_end)
    return ENDS.get(value, "crashed") if isinstance(value, str) else "crashed"
#: `swarm_api.jevaccount`'s patterns, restated: a row that does not fit is
#: not sent as a record and is settled as unrecorded spend instead.
CALL_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")
IDENT = re.compile(r"^[A-Za-z0-9_.-]{1,48}$")
CALL_OUTCOME = re.compile(r"^(ok|failed|malformed|skipped:[a-z_]{1,48})$")
MODEL = re.compile(r"^[A-Za-z0-9_.:@/+-]{1,128}$")
#: Each call's deadline: the state reads' own (`indexrun.STATE_CALL_SECONDS`).
CALL_SECONDS = 30.0


class AccountError(Exception):
    """swarm-api's answer was not the shape this module sends against."""


@dataclass(frozen=True)
class Reservation:
    """What `state/jev-reserve` answered: the task's carried spend, and the
    grant -- the most the task may have spent in all after this attempt."""

    grant: budget.Grant
    task_calls: int
    task_input_tokens: int
    reason: str | None


def _count(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AccountError("swarm-api's jev reservation is not a count")
    return value


def reserve(state: Any) -> Reservation:
    """`state/jev-reserve`. Raises what the call raises, or `AccountError`:
    `judge.attempt.setup` turns either into Jev off for the attempt."""
    answer = state.post(RESERVE_OP, {}, deadline_seconds=CALL_SECONDS)
    calls, tokens = _count(answer.get("task_calls")), _count(answer.get("task_input_tokens"))
    granted_calls = _count(answer.get("granted_calls"))
    granted_tokens = _count(answer.get("granted_tokens"))
    reason = answer.get("reason")
    return Reservation(budget.Grant(calls + granted_calls, tokens + granted_tokens),
                       calls, tokens, reason if isinstance(reason, str) else None)


def _ident(value: Any, pattern: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def call_record(row: Mapping[str, Any], *,
                source: str = SOURCE_JUDGMENT) -> dict[str, Any] | None:
    """One audit row as `state/jev-usage` takes it, or None when it cannot
    be one (no usable call id): its spend is then settled as unrecorded.
    `source` is who made the call (`SOURCES`), never the row's word."""
    if source not in SOURCES:
        raise ValueError(f"unknown jev call source {source!r}")
    call_id = _ident(row.get("call_id"), CALL_ID)
    if call_id is None:
        return None
    outcome = row.get("outcome")
    outcome = outcome if isinstance(outcome, str) and CALL_OUTCOME.fullmatch(outcome) \
        else "failed"
    tokens = row.get("input_tokens")
    output = row.get("output_tokens")
    return {
        "call_id": call_id,
        "source": source,
        "kind": _ident(row.get("kind"), IDENT) or budget.UNATTRIBUTED_KIND,
        "backend": _ident(row.get("backend"), IDENT),
        "requested_model": _ident(row.get("requested_model"), MODEL),
        "served_model": _ident(row.get("served_model"), MODEL),
        "input_tokens": tokens if isinstance(tokens, int) and not isinstance(tokens, bool)
        and tokens >= 0 else 0,
        "output_tokens": output if isinstance(output, int) and not isinstance(output, bool)
        and output >= 0 else None,
        "tokens_estimated": row.get("tokens_estimated") is True,
        "outcome": outcome,
    }


def settle(state: Any, ledger: budget.Ledger, *, outcome: str | None) -> dict[str, Any]:
    """Send every call the ledger has not settled, one record each, then
    `state/jev-settle` with `outcome` and the calls and tokens the ledger
    counted beyond its rows (its audit is bounded): unrecorded, charged under
    `unattributed`. With `outcome` None (after the agent, before the attempt's
    end is known) only the records are sent, and what they did not cover
    stays unsettled for the settle at the end. A failure anywhere returns all
    of it to the unsettled count and raises; sending it again charges nothing
    twice."""
    if outcome is not None and outcome not in OUTCOMES:
        raise ValueError(f"unknown attempt outcome {outcome!r}")
    calls, tokens, rows = ledger.take_unsettled_attempt()
    try:
        sent_calls = sent_tokens = 0
        for row in rows:
            record = call_record(row)
            if record is None:
                continue
            state.post(USAGE_OP, record, deadline_seconds=CALL_SECONDS)
            if not record["outcome"].startswith(budget.SKIPPED_PREFIX):
                sent_calls += 1
            sent_tokens += record["input_tokens"]
        if outcome is None:
            ledger.restore_unsettled(max(0, calls - sent_calls), max(0, tokens - sent_tokens))
            return {"ok": True, "records": len(rows)}
        answer = state.post(SETTLE_OP, {
            "outcome": outcome,
            "unrecorded_calls": max(0, calls - sent_calls),
            "unrecorded_tokens": max(0, tokens - sent_tokens),
        }, deadline_seconds=CALL_SECONDS)
    except Exception:
        ledger.restore_unsettled(calls, tokens, len(rows))
        raise
    return answer


def _sent_offset(path: Path) -> int:
    try:
        value = int(Path(f"{path}{SENT_SUFFIX}").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0
    return value if value >= 0 else 0


def _new_lines(path: Path) -> tuple[list[bytes], int]:
    """The complete lines past the sent offset, and the offset after them.
    A symlink, a non-file or a missing file is nothing."""
    try:
        if path.is_symlink() or not path.is_file():
            return [], 0
        with path.open("rb") as handle:
            data = handle.read(MAX_CALLS_BYTES)
    except OSError:
        return [], 0
    start = _sent_offset(path)
    if start > len(data):
        start = 0  # the file was replaced: everything in it is new
    end = data.rfind(b"\n") + 1
    if end <= start:
        return [], start
    return data[start:end].splitlines(), end


def _tool_rows(lines: Iterable[bytes], source: str) -> list[dict[str, Any]]:
    out = []
    for line in lines:
        if len(line) > MAX_LINE_BYTES:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("source") != source:
            continue
        record = call_record(row, source=source)
        if record is not None:
            out.append(record)
    return out


def record_tool_calls(state: Any, private_dir: Path) -> int:
    """Every quality-hook and Jev-browser call `private_dir`'s calls files
    hold that was not yet sent, one `state/jev-usage` record each; then each
    file's sent offset. Returns how many were sent. Raises what a call
    raises: the offset of that file is not advanced, and sending again
    charges nothing twice."""
    sent = 0
    for name, source in TOOL_CALL_FILES:
        path = Path(private_dir) / name
        lines, end = _new_lines(path)
        if not lines:
            continue
        for record in _tool_rows(lines, source):
            state.post(USAGE_OP, record, deadline_seconds=CALL_SECONDS)
            sent += 1
        marker = Path(f"{path}{SENT_SUFFIX}")
        if marker.is_symlink():
            marker.unlink()
        marker.write_text(str(end), encoding="utf-8")
    return sent
