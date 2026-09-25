"""The mock runner: the one that must always work.

Every default smoke test, every concurrency test and every quota test in this
platform runs `runner_profile="mock"`, and the reason is in the frozen
catalogue: `provider=None`. It needs no API key, so it works on a brand-new
tenant, in a brand-new project, before anybody has registered a credential --
which means "is the execution plane healthy?" can always be answered without
involving a third party's availability.

It is a real workload, not a stub:

* sleeps for a configurable time, interruptibly, so cancellation and timeout
  paths have something to interrupt;
* optionally burns CPU, so resource-class sizing and concurrency limits can be
  exercised under genuine load;
* writes incremental progress files into the work directory, so a checkpoint
  taken mid-run contains partial work and a restored run can be seen to
  continue from it rather than start over;
* writes result.json and a text artifact, so the artifact upload path is
  covered;
* fails deterministically, or reports a provider rate limit with a chosen
  retry-after, so the failure and park paths are testable without a provider.
  The rate limit parks ONE attempt: the count is kept in the state file, and
  the attempt after the park runs (see `QUOTA_EXHAUSTED_PARKS`).

What a caller may send is declared in the frozen catalogue
(`RUNNER_PROFILES["mock"].inputs`, contract request 25), and swarm-api refuses
anything else. The keys below that it does not declare -- `spend`, `provider`,
`credential_revoked_times`, `credential_detail`, `quota_detail`, `reset_at` --
reach this runner only from a test that writes the input itself.

Input (all optional):

    {"prompt": "...", "sleep_seconds": 2.0, "cpu_burn_seconds": 0.0,
     "steps": 4, "fail": false, "fail_message": "...", "exit_code": 1,
     "quota_exhausted": false, "retry_after_seconds": 1800,
     "artifact_text": "...", "artifact_name": "output.txt",
     "spend": {"usage": {"input_tokens": 10}, "total_cost_usd": 0.01}}

`spend` is reported on EVERY exit -- success, failure, rate limit, refused
credential and SIGTERM -- in the same shape a CLI runner reports it. The mock
costs nothing, so without it the paths that record spend on a park or a
cancellation could only ever be tested through a CLI runner, and the
cancellation path cannot be: a CLI runner killed on SIGTERM writes nothing.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

from .base import (
    CredentialRevokedSignal,
    QuotaExhaustedSignal,
    RunnerContext,
    RunnerFailure,
    run_runner,
)

PROGRESS_DIR = "progress"
STATE_FILE = "mock_state.json"

#: How many ATTEMPTS `quota_exhausted` parks before the run goes ahead. One,
#: by the owner's decision on #142 (2026-09-25): one park is the whole park
#: path -- checkpoint, park, release, promote, restore, finish -- and the
#: unbounded version parked every attempt, and since a park does not spend an
#: attempt, a task sent it never ended. The count is `quota_exhausted_times` in
#: STATE_FILE, which lives in `work/`, so the park's own checkpoint carries it
#: to the next attempt. It is a count of attempts, not of signals: a retry in
#: place is the same attempt and is refused again, because a provider that
#: keeps saying no must still end in a park.
QUOTA_EXHAUSTED_PARKS = 1


def _load_state(work: Path) -> dict[str, Any]:
    """Resume state left behind by a previous, checkpointed attempt."""
    path = work / STATE_FILE
    if not path.exists():
        return {"completed_steps": 0, "resumed": 0}
    try:
        data = json.loads(path.read_text() or "{}")
    except json.JSONDecodeError:
        return {"completed_steps": 0, "resumed": 0}
    data["resumed"] = int(data.get("resumed", 0)) + 1
    return data


def _save_state(work: Path, state: dict[str, Any]) -> None:
    (work / STATE_FILE).write_text(json.dumps(state, indent=2))


def _parks_this_attempt(work: Path, state: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Whether `quota_exhausted` parks THIS attempt, recording it if it is a new one.

    The attempt is named by `attempt_id`, which the lifecycle adds to every
    runner's input. The attempt that parked is kept beside the count, so a
    retry in place -- the same attempt, the same input -- is refused again
    without being counted twice. The state is saved BEFORE the signal is
    raised: the park checkpoints `work/` next, and a count written after it
    would not reach the attempt that has to read it.
    """
    attempt = str(payload.get("attempt_id") or "")
    parked = int(state.get("quota_exhausted_times", 0) or 0)
    if parked and state.get("quota_exhausted_attempt") == attempt:
        return True
    if parked >= QUOTA_EXHAUSTED_PARKS:
        return False
    state["quota_exhausted_times"] = parked + 1
    state["quota_exhausted_attempt"] = attempt
    _save_state(work, state)
    return True


def _burn_cpu(seconds: float, stop: Any) -> int:
    """Genuine CPU load: repeated SHA-256 over a growing buffer."""
    if seconds <= 0:
        return 0
    deadline = time.monotonic() + seconds
    digest = hashlib.sha256(b"swarm")
    rounds = 0
    while time.monotonic() < deadline:
        for _ in range(2000):
            digest.update(digest.digest())
        rounds += 1
        if stop():
            break
    return rounds


def body(ctx: RunnerContext) -> dict[str, Any]:
    payload = ctx.payload
    work = ctx.work_dir
    progress_dir = work / PROGRESS_DIR
    progress_dir.mkdir(parents=True, exist_ok=True)

    prompt = str(payload.get("prompt", "no prompt supplied"))
    steps = max(1, int(payload.get("steps", 4)))
    sleep_seconds = float(payload.get("sleep_seconds", 1.0))
    cpu_burn_seconds = float(payload.get("cpu_burn_seconds", 0.0))

    state = _load_state(work)
    completed = int(state.get("completed_steps", 0))
    if completed >= steps:
        # A restored checkpoint already contains all the work.
        completed = steps

    # Reported in the CLI runners' shape on every exit below; see the module
    # docstring for why the mock needs it at all.
    spend = payload.get("spend") if isinstance(payload.get("spend"), dict) else {}

    # `credential_revoked_times` rather than a bare flag: the behaviour worth
    # testing is that the worker RELOADS and carries on, which needs a runner
    # that refuses a bounded number of times and then succeeds. A permanent
    # refusal can only ever test the give-up path.
    refuse_times = int(payload.get("credential_revoked_times", 0) or 0)
    if refuse_times:
        marker = work / ".credential-refusals"
        seen = int(marker.read_text().strip() or 0) if marker.exists() else 0
        if seen < refuse_times:
            marker.write_text(str(seen + 1))
            raise CredentialRevokedSignal(
                provider=str(payload.get("provider", "mock-provider")),
                detail=str(
                    payload.get(
                        "credential_detail",
                        "mock runner asked to simulate a revoked credential",
                    )
                ),
                marker="oauth access token has been revoked",
                spend=spend,
            )

    if payload.get("quota_exhausted") and _parks_this_attempt(work, state, payload):
        raise QuotaExhaustedSignal(
            provider=str(payload.get("provider", "mock-provider")),
            retry_after_seconds=(
                int(payload["retry_after_seconds"])
                if payload.get("retry_after_seconds") is not None
                else None
            ),
            reset_at=payload.get("reset_at"),
            detail=str(payload.get("quota_detail", "mock runner asked to simulate a rate limit")),
            spend=spend,
        )

    per_step_sleep = sleep_seconds / steps if steps else sleep_seconds
    per_step_burn = cpu_burn_seconds / steps if steps else cpu_burn_seconds

    while completed < steps:
        if ctx.stop_requested:
            break
        index = completed + 1
        _burn_cpu(per_step_burn, lambda: ctx.stop_requested)
        # Interruptible sleep: the worker's SIGTERM must not wait out the nap.
        remaining = per_step_sleep
        while remaining > 0 and not ctx.stop_requested:
            slice_ = min(0.1, remaining)
            time.sleep(slice_)
            remaining -= slice_
        (progress_dir / f"step-{index:04d}.txt").write_text(
            f"step {index} of {steps}\nprompt: {prompt}\n"
        )
        completed = index
        state["completed_steps"] = completed
        _save_state(work, state)

    if payload.get("fail"):
        # Deterministic failure AFTER the progress files exist, so the failure
        # path still has artifacts and a checkpoint to upload.
        message = str(payload.get("fail_message", "mock runner was asked to fail"))
        exit_code = int(payload.get("exit_code", 1))
        if exit_code != 1:
            failed: dict[str, Any] = {"completed_steps": completed}
            if spend:
                failed["structured_output"] = dict(spend)
            ctx.write_result(status="failed", summary=message, error=message, output=failed)
            print(f"[mock] deterministic failure, exiting {exit_code}", file=sys.stderr)
            raise SystemExit(exit_code)
        raise RunnerFailure(message, spend=spend)

    artifact_name = str(payload.get("artifact_name", "output.txt"))
    artifact_text = str(
        payload.get(
            "artifact_text",
            f"mock runner completed {completed} step(s)\nprompt: {prompt}\n",
        )
    )
    ctx.write_artifact(artifact_name, artifact_text)

    output: dict[str, Any] = {
        "summary": f"mock runner completed {completed}/{steps} steps",
        "prompt": prompt,
        "completed_steps": completed,
        "requested_steps": steps,
        "resumed_count": int(state.get("resumed", 0)),
        "was_resumed": int(state.get("resumed", 0)) > 0,
        "metrics": {
            "sleep_seconds": sleep_seconds,
            "cpu_burn_seconds": cpu_burn_seconds,
        },
    }
    if spend:
        # Also on the SIGTERM path: the loop above breaks on stop_requested and
        # falls through to here, and `run_runner` writes this as "terminated".
        output["structured_output"] = dict(spend)
    return output


def main() -> int:
    return run_runner(body, name="mock")


if __name__ == "__main__":
    raise SystemExit(main())
