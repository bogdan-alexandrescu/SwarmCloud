"""`scripts/race-test.sh` must not measure a pool it failed to narrow.

WHY THIS FILE EXISTS
--------------------
race-test.sh narrows `runner:<profile>` to one slot and then submits a dozen
tasks at it. The narrowing is not setup in the incidental sense -- it IS the
experiment. Every assertion after it is a statement about what happens when many
tasks contend for one slot, and with the ceiling left where it was there is no
contention for any of them to be about.

The failure is not hypothetical. On 2026-09-22 the in-VPC gate ran race-test as
the `swarm-verify` service account, which holds `roles/datastore.viewer` and
therefore cannot write Firestore:

    [1] Narrow runner:mock to 1 slot(s)
     fail Firestore PATCH returned HTTP 403

That case died honestly, because `set -e` stopped the script on the refused
write. The dangerous neighbours of it did not:

  * a PATCH ACCEPTED but not applied -- the readback still shows the old
    ceiling. `assert_eq` recorded a failure and returned, the suite submitted
    its tasks anyway, and case 3 sampled a pool sitting at 20 while asserting
    `active <= 1`. On any quiet platform that assertion is satisfied by a peak
    of zero, so the one test in this repository built to catch oversubscription
    reported

        PASS  peak concurrent leases on runner:mock: 0 <= 1

    having created no contention whatsoever. The red summary line at the end
    does not retract that; the green assertion is the part a person quotes.

  * a ceiling narrowed in `hard_limit` and capped elsewhere. Admission uses
    `effective_limit` = min(hard_limit, adaptive_target, quota_derived_limit)
    (`swarm_common.models.SlotPool`). A pool whose provider is exhausted has
    `quota_derived_limit = 0`, so nothing is ever admitted, the peak stays at
    zero, and a comparison against `hard_limit` alone is perfectly satisfied.

  * a restore that did not restore. The trap printed "restored runner:mock
    hard_limit to 20" unconditionally, with the write's status and its error
    both sent to /dev/null. In the 403 run above it said that about a pool
    nothing had touched, using a write that had also been refused -- and in the
    case it actually exists for, where the narrow succeeded and the restore
    failed, it says it about a live pool left pinned at one slot.

The scripts are driven end to end with a fake `curl` and a fake `gcloud` on
PATH, exactly as tests/integration/test_register_tenant_grants.py drives
register-tenant.sh. Nothing is created, no credentials are used and no cloud is
reached: the fake curl IS the platform, and it is the only thing that decides
whether a write lands.
"""

from __future__ import annotations

import os
import signal
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "race-test.sh"

PROJECT = "swarm-test-project"
API_URL = "https://swarm-api-test.example.invalid"


#: AND SERIALLY. Every case in this file measures TIME -- an injected delay the
#: gate must notice, or a process that must still be running when it is looked
#: at. Both premises are about how long real work takes, so under `-n auto`
#: they compete with seven other workers for the CPU and the thing they measure
#: moves. They failed exactly that way and pass alone, which is the signature.
#: `make test` runs `-m "not serial" -n auto` first, then these on their own.
pytestmark = [
    pytest.mark.skipif(
        not SCRIPT.exists() or shutil.which("jq") is None,
        reason="race-test.sh and jq are both required",
    ),
    pytest.mark.serial,
]


#: `gcloud auth print-access-token` is the only gcloud call left on this path --
#: `access_token()` uses the metadata server only when K_SERVICE or
#: CLOUD_RUN_JOB is set, and neither is here. The ID token and the API URL are
#: supplied through SWARM_ID_TOKEN and API_URL, which common.sh consults first.
FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  *"print-access-token"*)   echo "fake-access-token" ;;
  *"print-identity-token"*) echo "fake-id-token" ;;
  *)                        : ;;
esac
exit 0
"""

#: A Firestore and a control plane, in ninety lines of bash.
#:
#: It answers the four shapes common.sh really uses:
#:
#:   fs_request        -K - [-X M] [--data-binary B] -o FILE -w '%{http_code}'
#:   api_request       -K - [-X M] [--data-binary B]         -w $'\n%{http_code}'
#:   readyz probe      -o /dev/null -w '%{http_code}'
#:   fs_database_exists                                   (body on stdout, no -w)
#:
#: and it holds the pool's hard_limit in a FILE, so a PATCH that this fake
#: accepts really does change what the next GET returns. That is the whole
#: point: the difference between "the write was accepted" and "the ceiling
#: moved" has to be expressible, because that difference is the bug.
FAKE_CURL = r"""#!/usr/bin/env bash
set -uo pipefail

out=""; wfmt=""; method="GET"; url=""; data=""; from_stdin=0
prev=""
for arg in "$@"; do
  case "${prev}" in
    -o)            out="${arg}" ;;
    -w)            wfmt="${arg}" ;;
    -X)            method="${arg}" ;;
    --data-binary) data="${arg}" ;;
    -K)            from_stdin=1 ;;
  esac
  case "${arg}" in http*) url="${arg}" ;; esac
  prev="${arg}"
done
# The Authorization header arrives on stdin as a curl config. Drain it so the
# writing side of the pipe never blocks.
[[ "${from_stdin}" -eq 1 ]] && cat >/dev/null 2>&1

printf '%s %s\n' "${method}" "${url}" >>"${FAKE_CURL_LOG}"

STATE="${FAKE_STATE_DIR}/hard_limit"
PATCH_N="${FAKE_STATE_DIR}/patch_n"
[[ -f "${STATE}" ]] || printf '%s' "${FAKE_POOL_BEFORE:-20}" >"${STATE}"

status=200
body='{}'

pool_document() {
  local hl="$1"
  jq -nc --arg name "projects/${FAKE_PROJECT}/databases/swarm/documents/pools/runner:mock" \
         --arg hl "${hl}" --arg qd "${FAKE_QUOTA_DERIVED:-}" '
    {name: $name,
     fields: ({ "name":       {stringValue: "runner:mock"},
                "hard_limit": {integerValue: $hl},
                "active":     {integerValue: "0"},
                "enabled":    {booleanValue: true}}
              + (if $qd == "" then {} else {"quota_derived_limit": {integerValue: $qd}} end))}'
}

case "${url}" in
  */readyz)
    status="${FAKE_READYZ_STATUS:-200}"; body=''
    ;;
  *"/documents/pools/"*)
    case "${method}" in
      PATCH)
        n=1
        [[ -f "${PATCH_N}" ]] && n=$(( $(cat "${PATCH_N}") + 1 ))
        printf '%s' "${n}" >"${PATCH_N}"
        # One status per PATCH, in order: "200,403" accepts the narrow and
        # refuses the restore. The last entry repeats.
        status="$(printf '%s' "${FAKE_PATCH_STATUS:-200}" | awk -F, -v n="${n}" \
          '{print (n <= NF ? $n : $NF)}')"
        if [[ "${status}" == 2* && -z "${FAKE_PATCH_IGNORED:-}" ]]; then
          hl="$(printf '%s' "${data}" | jq -r '.fields.hard_limit.integerValue // empty')"
          [[ -n "${hl}" ]] && printf '%s' "${hl}" >"${STATE}"
        fi
        if [[ "${status}" != 2* ]]; then
          body="$(jq -nc --argjson c "${status}" \
            '{error:{code:$c, message:"the caller does not have permission", status:"PERMISSION_DENIED"}}')"
        fi
        ;;
      DELETE)
        status="${FAKE_DELETE_STATUS:-200}"
        [[ "${status}" == 2* ]] && rm -f "${STATE}"
        ;;
      *)
        if [[ -s "${STATE}" ]]; then
          body="$(pool_document "$(cat "${STATE}")")"
        else
          status=404
          body='{"error":{"code":404,"message":"no such document","status":"NOT_FOUND"}}'
        fi
        ;;
    esac
    ;;
  *:runQuery|*:runAggregationQuery)
    # Firestore's query endpoints answer with a JSON ARRAY, not an object, and
    # fs_query/fs_count_where index into it. An object here made jq say
    # "Cannot index string with string" halfway through the suite.
    body='[]'
    ;;
  *"/documents/"*)
    # Any other collection or document read: an empty, healthy answer. A task
    # document read this way decodes to null, so `task_state` reports MISSING
    # and `task_is_terminal` is false -- which keeps the sampling loop running,
    # and is what the interrupt test needs.
    body='{"documents":[]}'
    ;;
  */v1/tasks)
    # Submission. An id here is what makes TASK_IDS non-empty and the suite
    # proceed to its sampling loop; FAKE_SUBMIT_STATUS refuses instead, which
    # is how a test reaches the "not enough contenders" floor without relying
    # on `--parallel`.
    status="${FAKE_SUBMIT_STATUS:-200}"
    if [[ "${status}" == 2* ]]; then
      # $$ as well as $RANDOM. The submissions run in parallel, and bash seeds
      # RANDOM from the pid and the clock: three of these starting in the same
      # second handed back the SAME id, which made the suite cancel one task
      # three times and looked exactly like a restore running twice. The pid is
      # unique per invocation by construction.
      body="$(jq -nc --arg id "task-fake-$$-${RANDOM}" '{id:$id}')"
    else
      body='{"code":"unavailable","message":"the fake platform refused this submission"}'
    fi
    ;;
  *firestore.googleapis.com*databases*)
    # fs_database_exists: present.
    body="$(jq -nc --arg n "projects/${FAKE_PROJECT}/databases/swarm" '{name:$n}')"
    ;;
  *)
    body='{}'
    ;;
esac

if [[ -n "${out}" ]]; then
  printf '%s' "${body}" >"${out}"
else
  printf '%s' "${body}"
fi
[[ -n "${wfmt}" ]] && printf '%s' "${wfmt//%\{http_code\}/${status}}"
exit 0
"""


def _run(tmp: Path, *args: str, **fakes: str) -> subprocess.CompletedProcess:
    """Drive the real race-test.sh against the fake platform in `fakes`."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, source in (("gcloud", FAKE_GCLOUD), ("curl", FAKE_CURL)):
        path = bin_dir / name
        path.write_text(source)
        path.chmod(0o755)

    # common.sh refuses to source a group- or world-writable env file, and it is
    # right to: it sources it as shell.
    env_file = tmp / "env"
    env_file.write_text(
        f"PROJECT_ID={PROJECT}\n"
        "REGION=us-central1\n"
        "ENVIRONMENT=dev\n"
        "FIRESTORE_DATABASE=swarm\n"
    )
    env_file.chmod(0o600)

    state = tmp / "state"
    state.mkdir(exist_ok=True)
    log = tmp / "requests.log"
    log.write_text("")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["SWARM_ENV_FILE"] = str(env_file)
    env["NO_COLOR"] = "1"
    env["API_URL"] = API_URL
    env["API_AUDIENCE"] = API_URL
    # Supplied rather than minted: id_token() consults this first, so no gcloud
    # call and no network are involved in authenticating to the fake API.
    env["SWARM_ID_TOKEN"] = "fake-id-token"
    env["FAKE_PROJECT"] = PROJECT
    env["FAKE_STATE_DIR"] = str(state)
    env["FAKE_CURL_LOG"] = str(log)
    env.update(fakes)

    proc = subprocess.run(
        [str(SCRIPT), *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    proc.requests = log.read_text()  # type: ignore[attr-defined]
    proc.transcript = proc.stdout + proc.stderr  # type: ignore[attr-defined]
    proc.final_limit = (state / "hard_limit").read_text() if (  # type: ignore[attr-defined]
        state / "hard_limit"
    ).exists() else "absent"
    return proc


def proc_requests(log: Path) -> str:
    """The fake curl's request log, for a run driven without `_run`."""
    return log.read_text()


def _submitted_a_task(requests: str) -> bool:
    """Did the suite POST a task, i.e. did it start the experiment?"""
    return any(
        line.startswith("POST ") and line.rstrip().endswith("/v1/tasks")
        for line in requests.splitlines()
    )


# ---------------------------------------------------------------------------
# The narrow is load-bearing: not reaching it must stop the run.
# ---------------------------------------------------------------------------

def test_a_refused_narrow_stops_the_suite(tmp_path: Path) -> None:
    """HTTP 403 on the narrowing PATCH -- the case seen in the VPC on 2026-09-22.

    The verify identity holds roles/datastore.viewer, so the write that creates
    the contention is refused. Nothing downstream can mean anything after that,
    and in particular no task may be submitted: twelve mock tasks against an
    un-narrowed pool is a load test wearing a race test's assertions.
    """
    proc = _run(tmp_path, FAKE_PATCH_STATUS="403")

    assert proc.returncode != 0, f"a refused narrow exited 0:\n{proc.transcript}"
    assert not _submitted_a_task(proc.requests), (
        "the suite submitted tasks after failing to narrow the pool; every "
        f"assertion below that point is about an un-narrowed pool:\n{proc.requests}"
    )
    assert "403" in proc.transcript, (
        f"the refusal was not reported as a 403:\n{proc.transcript}"
    )


def test_an_accepted_write_that_did_not_move_the_ceiling_stops_the_suite(
    tmp_path: Path,
) -> None:
    """HTTP 200, and the pool still reads its old limit.

    This is the shape the 403 case hid. `fs_patch` succeeds, so `set -e` never
    fires; the readback disagrees, so `assert_eq` fails -- and then the suite
    carried on and sampled a pool at its original ceiling while asserting
    `active <= 1`. On an idle platform the peak is zero and that assertion
    PASSES, which is a green claim of no oversubscription over an experiment
    that never ran.
    """
    proc = _run(tmp_path, FAKE_PATCH_STATUS="200", FAKE_PATCH_IGNORED="1")

    assert proc.returncode != 0, f"an ineffective narrow exited 0:\n{proc.transcript}"
    assert not _submitted_a_task(proc.requests), (
        "the write returned 200 and the ceiling did not move, and the suite "
        "submitted its tasks anyway. A pool at 20 is not a pool at 1:\n"
        f"{proc.requests}"
    )
    assert "effective_limit: 1" not in proc.transcript, (
        "the suite reported the pool as narrowed to 1 while it still reads 20:\n"
        + proc.transcript
    )


def test_a_ceiling_capped_below_the_narrow_stops_the_suite(tmp_path: Path) -> None:
    """hard_limit is 1 and effective_limit is 0, because the provider is exhausted.

    `swarm_common.models.SlotPool.effective_limit` is the minimum of hard_limit,
    adaptive_target and quota_derived_limit, and admission uses that number, not
    hard_limit. Comparing hard_limit alone accepts a pool that will admit
    NOTHING: every task queues, the peak stays at zero, and `active <= 1` is
    satisfied by an experiment with no participants.
    """
    proc = _run(tmp_path, FAKE_PATCH_STATUS="200", FAKE_QUOTA_DERIVED="0")

    assert proc.returncode != 0, (
        "a pool whose effective_limit is 0 was accepted as narrowed to 1:\n"
        f"{proc.transcript}"
    )
    assert not _submitted_a_task(proc.requests), (
        "the suite submitted tasks at a pool that can admit nothing:\n"
        f"{proc.requests}"
    )
    assert "effective_limit is 0" in proc.transcript, (
        "the failure did not name effective_limit, so a reader would go looking "
        f"at hard_limit and find it correct:\n{proc.transcript}"
    )


def test_the_narrow_is_reported_against_the_effective_limit(tmp_path: Path) -> None:
    """The passing case still has to say which number it checked.

    A narrow that really lands is reported as `effective_limit: 1`. This is the
    positive half of the test above: without it, changing the assertion to
    always fail would pass all three negative cases.
    """
    proc = _run(tmp_path, FAKE_PATCH_STATUS="200", FAKE_SUBMIT_STATUS="503")

    assert "effective_limit: 1" in proc.transcript, (
        f"a successful narrow did not report the effective limit:\n{proc.transcript}"
    )
    # The narrow really landed on the fake platform, and the trap then put it
    # back -- so the final state is 20 and the evidence that it was ever 1 is
    # the pair of PATCHes and the confirmed restore.
    methods = [line.split()[0] for line in proc.requests.splitlines()]
    assert methods.count("PATCH") == 2, (
        f"expected a narrow and a restore:\n{proc.requests}"
    )
    assert proc.final_limit == "20", (
        f"the pool was left narrowed: {proc.final_limit}"
    )


def test_a_race_needs_at_least_two_contenders(tmp_path: Path) -> None:
    """Twelve tasks requested, every one refused, and that is not a race.

    `assert_ge ${#TASK_IDS[@]} $((PARALLEL - 1))` records the shortfall and
    returns, so the suite went on to sample a pool with nothing in it and
    reported the peak as within the limit. With fewer than two tasks in flight
    there is no last slot to contend for, and "peak concurrent leases: 0 <= 1"
    is a statement about an empty platform.

    Submissions are refused by the fake rather than not requested, because the
    script now rejects `--parallel 1` outright -- see the argument-check test
    below.
    """
    proc = _run(tmp_path, FAKE_PATCH_STATUS="200", FAKE_SUBMIT_STATUS="503")

    assert proc.returncode != 0, (
        f"a race with no contenders exited 0:\n{proc.transcript}"
    )
    assert "two contenders are the minimum" in proc.transcript, (
        f"the suite did not say why it stopped:\n{proc.transcript}"
    )
    assert "peak concurrent leases" not in proc.transcript, (
        "the suite reported a peak-concurrency verdict with no tasks in flight:\n"
        f"{proc.transcript}"
    )


# ---------------------------------------------------------------------------
# Whatever it changed, it says truthfully what it put back.
# ---------------------------------------------------------------------------

def test_a_failed_restore_is_not_reported_as_a_restore(tmp_path: Path) -> None:
    """The narrow lands, the restoring write is refused, the pool stays at 1.

    This is the expensive case. runner:mock is left pinned at one slot on a live
    deployment and every mock task afterwards serialises behind a single lease.
    The trap used to print "restored runner:mock hard_limit to 20" here, because
    it discarded the write's status and never read the pool back -- so the only
    record of the incident denied it had happened.
    """
    proc = _run(
        tmp_path,
        FAKE_PATCH_STATUS="200,403",  # narrow accepted, restore refused
        FAKE_SUBMIT_STATUS="503",  # stop the run early; the restore is the subject
    )

    assert proc.final_limit == "1", (
        "the fixture did not reproduce the case: the pool is not still narrowed "
        f"({proc.final_limit})"
    )
    assert "restored runner:mock hard_limit to 20" not in proc.transcript, (
        "the suite claimed to have restored a pool that is still narrowed to 1:\n"
        f"{proc.transcript}"
    )
    assert "COULD NOT RESTORE" in proc.transcript, (
        f"a failed restore was not reported at all:\n{proc.transcript}"
    )
    assert "pool-limit.sh --pool runner:mock --limit 20" in proc.transcript, (
        "the failure did not name the command that fixes it, which is the only "
        f"thing a person reading this at 3am needs:\n{proc.transcript}"
    )


def test_a_successful_restore_is_confirmed_by_reading_the_pool_back(
    tmp_path: Path,
) -> None:
    """The positive half: when the pool really is back at 20, say so, and say why.

    Without this, "never claim a restore" would pass the test above.
    """
    proc = _run(tmp_path, FAKE_PATCH_STATUS="200", FAKE_SUBMIT_STATUS="503")

    assert proc.final_limit == "20", (
        f"the pool was not actually restored by the fixture: {proc.final_limit}"
    )
    assert "restored runner:mock hard_limit to 20 (confirmed by readback)" in (
        proc.transcript
    ), f"a real restore was not reported:\n{proc.transcript}"
    assert "COULD NOT RESTORE" not in proc.transcript, (
        f"a successful restore was reported as a failure:\n{proc.transcript}"
    )


def test_nothing_is_claimed_restored_when_the_narrow_never_landed(
    tmp_path: Path,
) -> None:
    """The 403 run again, this time for what the trap says afterwards.

    The narrowing PATCH was refused, so the pool was never touched and there is
    nothing to put back. The log said "restored runner:mock hard_limit to 20"
    anyway -- about a write that had also been refused. Two untrue statements in
    one line, and it is the line the session handover quoted as evidence the
    cleanup path worked.
    """
    proc = _run(tmp_path, FAKE_PATCH_STATUS="403")

    assert proc.final_limit == "20", "the fixture let a refused PATCH change the pool"
    assert "restored" not in proc.transcript.lower(), (
        "the suite reported a restore after a narrow that was refused:\n"
        f"{proc.transcript}"
    )


def test_a_parallel_count_that_cannot_race_is_refused_up_front(tmp_path: Path) -> None:
    """`--parallel 0` and `--parallel 1` are refused, and zero is the dangerous one.

    BSD seq counts DOWN when the first argument exceeds the last:

        $ seq 1 0      # GNU coreutils, which the swarm-verify image ships
        $ seq 1 0      # BSD, which is what macOS ships
        1
        0

    So `--parallel 0` ran the submission loop twice on a laptop and zero times
    in CI, from the same command line -- and nothing downstream could tell,
    because the loop body uses `i` only as a label. The two contenders it
    silently created would then have passed the floor check, and the operator
    would have got a race they had explicitly asked not to have.
    """
    for parallel in ("0", "1"):
        proc = _run(tmp_path / f"p{parallel}", "--parallel", parallel)
        assert proc.returncode != 0, (
            f"--parallel {parallel} was accepted:\n{proc.transcript}"
        )
        assert not _submitted_a_task(proc.requests), (
            f"--parallel {parallel} submitted tasks anyway:\n{proc.requests}"
        )
        assert "at least 2" in proc.transcript, (
            f"the refusal did not say what is required:\n{proc.transcript}"
        )


def test_a_limit_at_or_above_the_parallel_count_is_refused(tmp_path: Path) -> None:
    """`--limit 12 --parallel 12` admits everything and contends for nothing.

    The suite would narrow the pool, submit twelve tasks, observe a peak of
    twelve, and assert `12 <= 12` -- a pass, with no task ever waiting for
    another. Refusing the combination is cheaper than explaining the green run.
    """
    proc = _run(tmp_path, "--parallel", "4", "--limit", "4")

    assert proc.returncode != 0, f"a non-contending limit was accepted:\n{proc.transcript}"
    assert not _submitted_a_task(proc.requests)


def test_an_interrupted_run_restores_and_stops_rather_than_carrying_on(
    tmp_path: Path,
) -> None:
    """A signal mid-suite must end the run, not hand control back to it.

    `trap restore EXIT INT TERM` runs the handler and then RESUMES at the next
    statement, because the handler returns instead of exiting. So a signal
    during the sampling loop put the ceiling back to 20, cancelled every task,
    and then carried on asserting "the narrowed pool is never over its limit"
    against a pool that was no longer narrowed and had no work left in it -- and
    reported it green. An interrupted run that finishes green is worse than one
    that dies, because the summary is indistinguishable from a real pass.

    SIGTERM rather than SIGINT, for two reasons. It is the signal this suite
    will actually meet: a Cloud Run job execution that is cancelled or hits its
    timeout gets SIGTERM, and `make verify-remote` runs race-test as exactly
    that. And SIGINT is not reliably deliverable from a test: a process started
    with SIGINT already ignored cannot trap it at all (bash: "signals ignored on
    entry to the shell cannot be trapped"), and some runners do start children
    that way. The two traps are wired identically, so this covers the mechanism;
    what it does not prove is the INT path under a harness that ignores INT.

    The suite is started for real, signalled while it samples, and checked for
    three things: the exit code for a terminated process, the absence of any
    verdict that could only have been reached after the signal, and a pool that
    is back where it started.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, source in (("gcloud", FAKE_GCLOUD), ("curl", FAKE_CURL)):
        path = bin_dir / name
        path.write_text(source)
        path.chmod(0o755)

    env_file = tmp_path / "env"
    env_file.write_text(
        f"PROJECT_ID={PROJECT}\nREGION=us-central1\nENVIRONMENT=dev\n"
        "FIRESTORE_DATABASE=swarm\n"
    )
    env_file.chmod(0o600)

    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    log = tmp_path / "requests.log"
    log.write_text("")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["SWARM_ENV_FILE"] = str(env_file)
    env["NO_COLOR"] = "1"
    env["API_URL"] = API_URL
    env["API_AUDIENCE"] = API_URL
    env["SWARM_ID_TOKEN"] = "fake-id-token"
    env["FAKE_PROJECT"] = PROJECT
    env["FAKE_STATE_DIR"] = str(state)
    env["FAKE_CURL_LOG"] = str(log)
    env["FAKE_PATCH_STATUS"] = "200"

    # --timeout 20 bounds the case where the signal is swallowed: the loop then
    # samples for twenty seconds and reaches its verdict, which is exactly the
    # behaviour being ruled out.
    proc = subprocess.Popen(
        [str(SCRIPT), "--parallel", "3", "--timeout", "20"],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    # Long enough to be inside the sampling loop (the narrow, three submissions
    # and at least one sample), short enough to be well inside the 20s timeout.
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    assert proc.poll() is None, "the suite finished before it could be interrupted"
    proc.send_signal(signal.SIGTERM)
    transcript, _ = proc.communicate(timeout=60)

    assert proc.returncode == 143, (
        "SIGTERM did not end the run; it was swallowed and the suite carried on "
        f"(exit {proc.returncode}):\n{transcript}"
    )
    assert "peak concurrent leases" not in transcript, (
        "the suite reported its peak-concurrency verdict after being "
        f"terminated:\n{transcript}"
    )
    assert (state / "hard_limit").read_text() == "20", (
        "a terminated run left the pool narrowed: "
        f"{(state / 'hard_limit').read_text()}"
    )
    # The handler calls `restore` and then exits, which fires the EXIT trap and
    # would enter `restore` a second time. The read-first design makes the
    # second pass silent by itself -- the pool already reads 20, so there is
    # nothing to say -- but it does NOT make the cancellations idempotent, and
    # `cancel_all` would fire a second POST per task. That is what the RESTORED
    # flag is for, so it is asserted rather than assumed.
    requests = proc_requests(log)
    submitted = sum(1 for line in requests.splitlines() if line.endswith("/v1/tasks"))
    cancels = [line for line in requests.splitlines() if line.endswith("/cancel")]
    assert submitted > 0, f"nothing was submitted, so nothing was cancelled:\n{requests}"
    # Counted, not deduplicated: the ids come from the fake, and two tasks
    # sharing one would make a doubled cleanup look like a single one.
    assert len(cancels) == submitted, (
        f"{submitted} task(s) were submitted and {len(cancels)} cancellation(s) "
        "were sent, so `restore` ran more than once on the way out:\n"
        + "\n".join(cancels)
    )
    assert transcript.count("restored runner:mock hard_limit") == 1, (
        "the restore reported itself more than once:\n" + transcript
    )


# ---------------------------------------------------------------------------
# The fake is a fixture, so it gets checked too.
# ---------------------------------------------------------------------------

def test_the_fixture_really_applies_and_refuses_writes(tmp_path: Path) -> None:
    """A fake platform that accepts everything proves nothing.

    Every assertion above rests on this fake distinguishing an applied write
    from a refused one, so that distinction is asserted directly rather than
    inferred from the scripts' behaviour.
    """
    refused = _run(tmp_path / "refused", FAKE_PATCH_STATUS="403")
    assert refused.final_limit == "20"

    applied = _run(tmp_path / "applied", FAKE_PATCH_STATUS="200", FAKE_SUBMIT_STATUS="503")
    # Narrowed to 1 and then restored to 20 by the trap: two PATCHes, both
    # applied, the second undoing the first.
    assert applied.final_limit == "20"
    methods = [line.split()[0] for line in applied.requests.splitlines()]
    assert methods.count("PATCH") == 2, (
        f"expected a narrow and a restore, got: {applied.requests}"
    )

    ignored = _run(
        tmp_path / "ignored", FAKE_PATCH_STATUS="200", FAKE_PATCH_IGNORED="1"
    )
    assert ignored.final_limit == "20", (
        "FAKE_PATCH_IGNORED must answer 200 and change nothing; without that the "
        "silent-failure case is not being exercised at all"
    )


def test_the_fixture_speaks_the_shapes_common_sh_uses(tmp_path: Path) -> None:
    """The fake curl has to honour -o, -w and -K, or the scripts misread it.

    tests/integration/test_register_tenant_grants.py carries the scar: its fake
    curl ignored -o and -w, which made `status` the literal string `{}` the
    moment fs_request started checking status codes, and every test in that file
    errored in its fixture for 95 commits.
    """
    proc = _run(tmp_path, FAKE_PATCH_STATUS="403")

    # require_platform got past fs_database_exists (body on stdout, no -w) and
    # past the /readyz probe (-o /dev/null -w '%{http_code}'), which it can only
    # do if both shapes work.
    assert f"{API_URL}/readyz" in proc.requests, (
        f"the readiness probe never happened:\n{proc.requests}"
    )
    assert "does not exist" not in proc.transcript, (
        "require_fs_database read the fake's answer as a missing database:\n"
        f"{proc.transcript}"
    )
    # And the typed Firestore document decodes. `{"integerValue":"20"}` has to
    # come back through FS_JQ as the number 20 for the script to know what the
    # ceiling was; a run that gets as far as restoring it says so out loud.
    applied = _run(tmp_path / "applied", FAKE_PATCH_STATUS="200", FAKE_SUBMIT_STATUS="503")
    assert "hard_limit to 20" in applied.transcript, (
        "the typed Firestore document did not decode; the script never learned "
        f"the original ceiling:\n{applied.transcript}"
    )
