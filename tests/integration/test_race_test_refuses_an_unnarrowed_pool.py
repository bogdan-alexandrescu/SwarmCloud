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

THE NARROW GOES THROUGH THE ADMIN API NOW, NOT FIRESTORE (owner decision,
2026-09-24; docs/audits/2026-09-22/race-test-needs-a-write.md). swarm-verify
was made a platform admin, and race-test narrows and restores with
`PUT /v1/admin/limits/runner/mock` -- a route that validates the pool name,
bounds the value and CANNOT write `active`. So the fake below speaks that route
and keeps its Firestore write paths only as tripwires: the cases here also
prove the suite made no Firestore write at all, because a raw PATCH is the
capability the decision removed, and the one whose mistake (a clobbered
`active`) once inflated live capacity.

Four refusals come with that, each because the API cannot undo what the suite
would otherwise do:

  * a profile other than `mock` -- narrowing a provider-backed runner pool
    serialises every tenant's real agents behind one slot;
  * a pool that does not exist -- `upsert_pool` would CREATE it, and there is
    no admin route that deletes one;
  * a drained pool -- a runner pool has no undrain route, and a drained pool
    admits nothing, so there would be nothing to race;
  * a ceiling above `LimitRequest`'s bound -- the restoring PUT would be a 422
    and the pool would stay narrowed.

The scripts are driven end to end with a fake `curl` and a fake `gcloud` on
PATH, exactly as tests/integration/test_register_tenant_grants.py drives
register-tenant.sh. Nothing is created, no credentials are used and no cloud is
reached: the fake curl IS the platform, and it is the only thing that decides
whether a write lands.
"""

from __future__ import annotations

import json
import os
import re
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
LIMIT_N="${FAKE_STATE_DIR}/limit_n"
# Seeded ONCE, on the first request, so FAKE_POOL_ABSENT can describe a pool
# that does not exist yet while a later write could still create it -- which is
# exactly what the admin route would do, and what the suite must never ask for.
if [[ ! -f "${FAKE_STATE_DIR}/seeded" ]]; then
  : >"${FAKE_STATE_DIR}/seeded"
  [[ -n "${FAKE_POOL_ABSENT:-}" ]] || printf '%s' "${FAKE_POOL_BEFORE:-20}" >"${STATE}"
fi

status=200
body='{}'

# FAKE_SETTLE makes the platform behave well enough for the WHOLE suite to reach
# its summary: one slot is held until the suite cancels, and every task settles
# as CANCELLED once it has. Without it `active` is always 0 and a task document
# reads as absent, so the suite always fails somewhere in its middle -- which is
# right for the cases that stop early, and useless for the one question only a
# complete run can answer: what the exit code is when every race case passed.
CANCELLED="${FAKE_STATE_DIR}/cancelled"
settled_active() {
  if [[ -n "${FAKE_SETTLE:-}" && ! -f "${CANCELLED}" ]]; then printf '1'; else printf '0'; fi
}

pool_document() {
  local hl="$1"
  jq -nc --arg name "projects/${FAKE_PROJECT}/databases/swarm/documents/pools/runner:mock" \
         --arg hl "${hl}" --arg qd "${FAKE_QUOTA_DERIVED:-}" --arg act "$(settled_active)" \
         --argjson en "${FAKE_POOL_ENABLED:-true}" '
    {name: $name,
     fields: ({ "name":       {stringValue: "runner:mock"},
                "hard_limit": {integerValue: $hl},
                "active":     {integerValue: $act},
                "enabled":    {booleanValue: $en}}
              + (if $qd == "" then {} else {"quota_derived_limit": {integerValue: $qd}} end))}'
}

# The admin API's answer for a pool, in pool_to_api's shape.
pool_api() {
  jq -nc --argjson hl "$1" --argjson en "${FAKE_POOL_ENABLED:-true}" \
    '{pool: {name: "runner:mock", hard_limit: $hl, effective_limit: $hl,
             active: 0, enabled: $en}}'
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
  *"/documents/tasks/"*)
    if [[ -n "${FAKE_SETTLE:-}" ]]; then
      # RUNNING until the suite cancels, CANCELLED after: the sampling loop then
      # runs to its --timeout, and the post-storm case finds nothing holding
      # capacity.
      state=RUNNING
      [[ -f "${CANCELLED}" ]] && state=CANCELLED
      body="$(jq -nc --arg name "${url%%\?*}" --arg s "${state}" \
        '{name: $name, fields: {state: {stringValue: $s}}}')"
    else
      body='{"documents":[]}'
    fi
    ;;
  *"/documents/"*)
    # Any other collection or document read: an empty, healthy answer. A task
    # document read this way decodes to null, so `task_state` reports MISSING
    # and `task_is_terminal` is false -- which keeps the sampling loop running,
    # and is what the interrupt test needs.
    body='{"documents":[]}'
    ;;
  */v1/admin/limits/runner/*)
    # PUT /v1/admin/limits/runner/{profile} -- the ONLY write the suite is
    # entitled to make. One status per PUT, in order, exactly as
    # FAKE_PATCH_STATUS above: "200,403" accepts the narrow and refuses the
    # restore. FAKE_LIMIT_IGNORED answers 200 and changes nothing.
    if [[ "${method}" != "PUT" ]]; then
      status=405
      body='{"code":"method_not_allowed","message":"PUT only"}'
    else
      n=1
      [[ -f "${LIMIT_N}" ]] && n=$(( $(cat "${LIMIT_N}") + 1 ))
      printf '%s' "${n}" >"${LIMIT_N}"
      # Every body, in order, whatever the answer: the restore has to send the
      # ORIGINAL ceiling, and only the body can show that it did.
      printf '%s\n' "${data}" >>"${FAKE_STATE_DIR}/limit_bodies"
      status="$(printf '%s' "${FAKE_LIMIT_STATUS:-200}" | awk -F, -v n="${n}" \
        '{print (n <= NF ? $n : $NF)}')"
      if [[ "${status}" == 2* ]]; then
        hl="$(printf '%s' "${data}" | jq -r '.limit // .hard_limit // empty')"
        if [[ -z "${FAKE_LIMIT_IGNORED:-}" && -n "${hl}" ]]; then
          printf '%s' "${hl}" >"${STATE}"
        fi
        body="$(pool_api "$(cat "${STATE}" 2>/dev/null || printf '%s' "${hl:-0}")")"
      else
        body='{"code":"forbidden","message":"admin group membership is required for this operation"}'
      fi
    fi
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
  */v1/tasks/*/cancel)
    # Recorded whatever FAKE_SETTLE says; only FAKE_SETTLE reads it back.
    : >"${CANCELLED}"
    body='{}'
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


LIMIT_ROUTE = f"{API_URL}/v1/admin/limits/runner/mock"

#: Firestore's two READ endpoints that happen to be POSTs. Everything else sent
#: to Firestore with a method other than GET is a write.
_FIRESTORE_READ_POSTS = (":runQuery", ":runAggregationQuery")


def _firestore_writes(requests: str) -> list[str]:
    """Every request that could have changed a Firestore document directly.

    The property the 2026-09-24 decision buys is that this suite has NO direct
    write path into the database: a narrow is a ceiling change, and the admin
    route is the only thing that makes one. A PATCH, a DELETE or a commit here
    is the capability that once clobbered `active` on a live deployment.
    """
    writes = []
    for line in requests.splitlines():
        method, _, url = line.partition(" ")
        if "firestore.googleapis.com" not in url or method == "GET":
            continue
        if method == "POST" and url.rstrip().endswith(_FIRESTORE_READ_POSTS):
            continue
        writes.append(line)
    return writes


def _limit_puts(requests: str) -> list[str]:
    """The narrows and restores, as sent to the admin API."""
    return [
        line for line in requests.splitlines()
        if line.rstrip() == f"PUT {LIMIT_ROUTE}"
    ]


def _any_limit_write(requests: str) -> bool:
    """Any attempt at all to change a runner ceiling, by either path."""
    return bool(_limit_puts(requests)) or any(
        "/v1/admin/limits/" in line for line in requests.splitlines()
    ) or bool(_firestore_writes(requests))


# ---------------------------------------------------------------------------
# The narrow is load-bearing: not reaching it must stop the run.
# ---------------------------------------------------------------------------

def test_a_refused_narrow_stops_the_suite(tmp_path: Path) -> None:
    """HTTP 403 on the narrowing write -- the case seen in the VPC on 2026-09-22.

    Then it was a Firestore PATCH refused to roles/datastore.viewer. Now it is
    the admin route refusing a caller that is not in ADMIN_USERS -- which is
    what every environment that has not granted swarm-verify admin will see.
    Nothing downstream can mean anything after that, and in particular no task
    may be submitted: twelve mock tasks against an un-narrowed pool is a load
    test wearing a race test's assertions.
    """
    proc = _run(tmp_path, FAKE_LIMIT_STATUS="403")

    assert proc.returncode != 0, f"a refused narrow exited 0:\n{proc.transcript}"
    assert not _submitted_a_task(proc.requests), (
        "the suite submitted tasks after failing to narrow the pool; every "
        f"assertion below that point is about an un-narrowed pool:\n{proc.requests}"
    )
    assert "403" in proc.transcript, (
        f"the refusal was not reported as a 403:\n{proc.transcript}"
    )
    # A 403 from swarm-api here has exactly one fix, and it is in a tfvars file
    # a reader of this transcript would not otherwise think to open.
    assert "admin_users" in proc.transcript, (
        "a refused narrow did not name the grant that fixes it:\n"
        f"{proc.transcript}"
    )
    assert not _firestore_writes(proc.requests), (
        "the admin route refused the narrow and the suite fell back to writing "
        f"Firestore directly:\n{proc.requests}"
    )


def test_an_accepted_write_that_did_not_move_the_ceiling_stops_the_suite(
    tmp_path: Path,
) -> None:
    """HTTP 200, and the pool still reads its old limit.

    This is the shape the 403 case hid. The write succeeds, so `set -e` never
    fires; the readback disagrees, so `assert_eq` fails -- and then the suite
    carried on and sampled a pool at its original ceiling while asserting
    `active <= 1`. On an idle platform the peak is zero and that assertion
    PASSES, which is a green claim of no oversubscription over an experiment
    that never ran. The admin route's 200 is no more a measurement than the
    PATCH's was; the Firestore readback is.
    """
    proc = _run(tmp_path, FAKE_LIMIT_STATUS="200", FAKE_LIMIT_IGNORED="1")

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
    proc = _run(tmp_path, FAKE_LIMIT_STATUS="200", FAKE_QUOTA_DERIVED="0")

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
    proc = _run(tmp_path, FAKE_LIMIT_STATUS="200", FAKE_SUBMIT_STATUS="503")

    assert "effective_limit: 1" in proc.transcript, (
        f"a successful narrow did not report the effective limit:\n{proc.transcript}"
    )
    # The narrow really landed on the fake platform, and the trap then put it
    # back -- so the final state is 20 and the evidence that it was ever 1 is
    # the pair of admin PUTs and the confirmed restore.
    assert len(_limit_puts(proc.requests)) == 2, (
        f"expected a narrow and a restore through the admin API:\n{proc.requests}"
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
    proc = _run(tmp_path, FAKE_LIMIT_STATUS="200", FAKE_SUBMIT_STATUS="503")

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

    The restore goes through the same admin route as the narrow, so a refusal
    here is the gate losing admin mid-run (a redeploy that dropped it from
    ADMIN_USERS), a 429, or a dropped connection.
    """
    proc = _run(
        tmp_path,
        FAKE_LIMIT_STATUS="200,403",  # narrow accepted, restore refused
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
    # TWO REPAIRS, because one of them cannot be run from outside the VPC.
    #
    # The admin route is the one the suite used, so it is as bounded and as
    # attributed as the change it undoes. But the front door refuses every user
    # credential (api.sh: SWARM_IMPERSONATE_SA is REQUIRED there), so from a
    # workstation it only works as an identity that is BOTH an admin and
    # admitted by IAP. pool-limit.sh works today, as an operator, over a
    # Firestore updateMask naming hard_limit alone. Printing only the first sent
    # the person reading this at 3am to a 403 with nothing else on screen.
    assert "scripts/api.sh PUT /admin/limits/runner/mock '{\"limit\":20}'" in (
        proc.transcript
    ), (
        "the failure did not name the admin-route repair:\n"
        f"{proc.transcript}"
    )
    assert "scripts/pool-limit.sh --pool runner:mock --limit 20" in proc.transcript, (
        "the failure named only the admin-route repair, which the front door "
        "refuses to a user credential; the operator fallback that works from a "
        f"workstation is missing:\n{proc.transcript}"
    )
    assert "frontend_iap_members" in proc.transcript, (
        "the admin-route repair did not say what it needs through the front "
        f"door, so it reads as a command that simply works:\n{proc.transcript}"
    )
    # The ADDRESS, not only the name. #23 moved `frontend_iap_members` out of
    # the environment's tfvars into terraform/bootstrap, and every remedy that
    # named the old file survived a clean textual merge because it was only
    # checked for the variable's name (tests/unit/mcp/test_front_door.py has
    # the same property for the client's remedies). Whatever tfvars file this
    # repair sends the reader to must exist and must SET the list. Only the
    # path that follows the variable's name is read: the transcript can name
    # other tfvars files for other reasons (admin_users is in the
    # environment's), and those are not this claim. The window spans the line
    # break and the ` fail ` prefix `err` puts on the next line.
    named = re.findall(
        r"frontend_iap_members in[\s\S]{0,40}?(terraform/[\w<>./-]+\.tfvars)",
        proc.transcript,
    )
    assert named, (
        "the admin-route repair names frontend_iap_members but not the file "
        f"that sets it:\n{proc.transcript}"
    )
    for relative in named:
        path = REPO / relative
        assert path.is_file(), f"the repair sends the reader to {relative}, which does not exist"
        assert re.search(r"^\s*frontend_iap_members\s*=", path.read_text(), re.M), (
            f"the repair sends the reader to {relative}, which does not set "
            "frontend_iap_members"
        )
    assert not _firestore_writes(proc.requests), (
        "a refused restore fell back to writing Firestore directly:\n"
        f"{proc.requests}"
    )


#: t_fail's line, as testlib.sh prints it with NO_COLOR. `err` prints a
#: lower-case " fail", which is a diagnostic rather than a recorded failure.
_FAIL_LINE = re.compile(r"^\s*FAIL\s")
#: t_summary's verdict on a run with no failures.
_GREEN_SUMMARY = re.compile(r"race: \d+/\d+ passed")


def _recorded_failures(transcript: str) -> list[str]:
    return [line.strip() for line in transcript.splitlines() if _FAIL_LINE.match(line)]


#: A complete run, kept short: three contenders and a three-second sampling
#: window are enough for every case to reach its verdict against the fake.
_COMPLETE_RUN = ("--parallel", "3", "--timeout", "3")


def test_a_complete_race_that_restores_the_pool_exits_zero(tmp_path: Path) -> None:
    """The whole suite, end to end, green: the control for the case below.

    Every other case in this file stops the run early -- a refusal, a t_fatal --
    and so every one of them reaches a non-zero exit whatever the restore does.
    This one reaches `t_summary` with nothing failed. Without it, the next test
    could pass because the fixture never lets the race go green, and it would
    be measuring the fake rather than the exit status.
    """
    proc = _run(tmp_path, *_COMPLETE_RUN, FAKE_LIMIT_STATUS="200", FAKE_SETTLE="1")

    assert not _recorded_failures(proc.transcript), (
        "the fixture did not let the race go green, so it cannot isolate the "
        f"restore:\n{proc.transcript}"
    )
    assert "peak concurrent leases on runner:mock" in proc.transcript, (
        f"the suite never reached its concurrency verdict:\n{proc.transcript}"
    )
    assert "every pool is within [0, effective_limit]" in proc.transcript, (
        f"the suite never reached its last case:\n{proc.transcript}"
    )
    assert _GREEN_SUMMARY.search(proc.transcript), (
        f"a clean run was not summarised as passing:\n{proc.transcript}"
    )
    assert proc.returncode == 0, (
        f"a clean run with a confirmed restore exited {proc.returncode}:\n"
        f"{proc.transcript}"
    )
    assert proc.final_limit == "20", f"the pool was left at {proc.final_limit}"
    assert "restored runner:mock hard_limit to 20 (confirmed by readback)" in (
        proc.transcript
    ), f"the restore was not confirmed:\n{proc.transcript}"
    assert not _firestore_writes(proc.requests), (
        f"a complete run wrote Firestore directly:\n{proc.requests}"
    )


def test_a_restore_that_fails_after_a_green_race_fails_the_run(tmp_path: Path) -> None:
    """Every race case passes, then the restoring PUT gets a 503: exit non-zero.

    `restore` ran from `trap restore EXIT`, printed COULD NOT RESTORE and
    returned. An EXIT trap that does not call `exit` leaves the status alone,
    and the last command was a green `t_summary`, so the run exited 0 with
    runner:mock pinned at one slot. `make verify-remote` then printed
    `ok race-test` and ran e2e-test against a single mock slot, where the
    failure surfaced as a timeout in a different suite, and the only line
    explaining it sat in race-test's log above the 40 lines verify-remote shows.

    A live pool left narrowed is the one outcome this suite promised not to
    cause. The run that causes it is a failed run, and it must say so in the
    exit status and in the summary line people quote.
    """
    proc = _run(
        tmp_path, *_COMPLETE_RUN,
        FAKE_LIMIT_STATUS="200,503",  # the narrow lands, the restore does not
        FAKE_SETTLE="1",
    )

    assert proc.final_limit == "1", (
        "the fixture did not reproduce the case: the pool is not still narrowed "
        f"({proc.final_limit})"
    )
    assert "every pool is within [0, effective_limit]" in proc.transcript, (
        "the suite did not reach its last case, so this is not the green-race "
        f"case at all:\n{proc.transcript}"
    )
    unrelated = [
        line for line in _recorded_failures(proc.transcript)
        if "restore" not in line.lower()
    ]
    assert not unrelated, (
        "the race itself failed, so a non-zero exit would not isolate the "
        f"restore: {unrelated}\n{proc.transcript}"
    )
    assert "COULD NOT RESTORE" in proc.transcript, (
        f"the failed restore was not reported:\n{proc.transcript}"
    )
    assert proc.returncode != 0, (
        "the run left runner:mock narrowed to 1 slot and exited 0, so the gate "
        f"reads it as a pass:\n{proc.transcript}"
    )
    assert not _GREEN_SUMMARY.search(proc.transcript), (
        "the summary reported the run as passing while runner:mock is still "
        f"narrowed:\n{proc.transcript}"
    )


def test_a_successful_restore_is_confirmed_by_reading_the_pool_back(
    tmp_path: Path,
) -> None:
    """The positive half: when the pool really is back at 20, say so, and say why.

    Without this, "never claim a restore" would pass the test above.
    """
    proc = _run(tmp_path, FAKE_LIMIT_STATUS="200", FAKE_SUBMIT_STATUS="503")

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

    The narrowing write was refused, so the pool was never touched and there is
    nothing to put back. The log said "restored runner:mock hard_limit to 20"
    anyway -- about a write that had also been refused. Two untrue statements in
    one line, and it is the line the session handover quoted as evidence the
    cleanup path worked.
    """
    proc = _run(tmp_path, FAKE_LIMIT_STATUS="403")

    assert proc.final_limit == "20", "the fixture let a refused write change the pool"
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
    env["FAKE_LIMIT_STATUS"] = "200"

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
    assert not _firestore_writes(requests), (
        f"the interrupted run wrote Firestore directly on its way out:\n{requests}"
    )


# ---------------------------------------------------------------------------
# The write path is the admin API, and only the admin API (2026-09-24).
# ---------------------------------------------------------------------------

def test_the_narrow_and_the_restore_go_through_the_admin_api_and_nothing_else(
    tmp_path: Path,
) -> None:
    """Two PUTs to the runner-limit route -- 1, then the original 20 -- and no
    Firestore write of any kind.

    This is the decision itself, asserted. The admin route validates the pool
    name against the frozen catalogue, bounds the value, runs under
    `admin_auth` as the verified caller, and has no parameter that reaches
    `active`. A Firestore PATCH has none of those properties, and the one time
    this suite used a wider field mask it clobbered `active` on a live
    deployment and inflated capacity.
    """
    proc = _run(tmp_path, FAKE_LIMIT_STATUS="200", FAKE_SUBMIT_STATUS="503")

    assert not _firestore_writes(proc.requests), (
        f"the suite wrote Firestore directly:\n{proc.requests}"
    )
    assert len(_limit_puts(proc.requests)) == 2, (
        f"expected exactly a narrow and a restore on {LIMIT_ROUTE}:\n{proc.requests}"
    )
    bodies = [
        json.loads(line)
        for line in (tmp_path / "state" / "limit_bodies").read_text().splitlines()
    ]
    assert [b.get("limit") for b in bodies] == [1, 20], (
        "the narrow must send the requested slot count and the restore the "
        f"ORIGINAL ceiling read before the run; sent {bodies}"
    )
    assert proc.final_limit == "20", f"the pool was left at {proc.final_limit}"


@pytest.mark.parametrize("profile", ["claude-code", "generic", "codex"])
def test_a_profile_other_than_mock_is_refused_before_anything_is_written(
    tmp_path: Path, profile: str
) -> None:
    """Only `runner:mock` may be narrowed, and the refusal comes before any write.

    `mock` has no provider: narrowing it holds up nothing but this suite's own
    tasks, and the verify tenant (`providers = []`) can only run it anyway.
    Narrowing `runner:claude-code` to one slot on the live shared platform would
    serialise EVERY tenant's real agents behind a single lease for as long as
    the run lasts -- a platform-wide incident started by a flag.
    """
    proc = _run(tmp_path, "--profile", profile)

    assert proc.returncode != 0, f"--profile {profile} was accepted:\n{proc.transcript}"
    assert not _any_limit_write(proc.requests), (
        f"--profile {profile} changed a ceiling before being refused:\n{proc.requests}"
    )
    assert not _submitted_a_task(proc.requests), (
        f"--profile {profile} submitted tasks:\n{proc.requests}"
    )
    assert "only runs against the mock profile" in proc.transcript, (
        f"the refusal did not say what is allowed:\n{proc.transcript}"
    )


def test_a_pool_that_does_not_exist_is_refused_rather_than_created(
    tmp_path: Path,
) -> None:
    """No `pools/runner:mock` document: stop, do not invent one.

    `Store.upsert_pool` CREATES a pool it cannot find, and no admin route
    deletes one -- so a narrow here would leave behind a document the restore
    could not remove, capping a pool that admission used to treat as
    unlimited. The old Firestore path created it with a PATCH and deleted it
    afterwards; the admin API cannot express the second half.
    """
    proc = _run(tmp_path, FAKE_POOL_ABSENT="1")

    assert proc.returncode != 0, f"an absent pool was accepted:\n{proc.transcript}"
    assert not _any_limit_write(proc.requests), (
        f"the suite created or changed a pool it had to refuse:\n{proc.requests}"
    )
    assert proc.final_limit == "absent", (
        f"the pool now exists with hard_limit {proc.final_limit}"
    )
    assert "has no pool document" in proc.transcript, (
        f"the refusal did not say why:\n{proc.transcript}"
    )


def test_a_drained_pool_is_refused_rather_than_reopened(tmp_path: Path) -> None:
    """`enabled = false`: somebody drained runner:mock on purpose.

    The old narrow wrote `enabled: true` in the same PATCH, silently undoing an
    operator's drain. The runner-limit route never touches `enabled` and there
    is no undrain route for a runner pool, so the only honest move is to stop:
    a drained pool admits nothing, and there would be nothing to race for.
    """
    proc = _run(tmp_path, FAKE_POOL_ENABLED="false")

    assert proc.returncode != 0, f"a drained pool was accepted:\n{proc.transcript}"
    assert not _any_limit_write(proc.requests), (
        f"the suite changed a drained pool:\n{proc.requests}"
    )
    assert "drained" in proc.transcript, (
        f"the refusal did not say the pool is drained:\n{proc.transcript}"
    )


def test_a_ceiling_the_api_cannot_restore_is_refused_before_narrowing(
    tmp_path: Path,
) -> None:
    """A ceiling above `LimitRequest`'s bound cannot be put back through the API.

    `LimitRequest.limit` is `le=100_000`, and `Store.upsert_pool` writes
    1,000,000 for a pool it creates without a limit. Narrowing such a pool
    would succeed and its restore would be a 422 -- a pool left pinned at one
    slot by the suite that promised to put it back. So the restore has to be
    expressible BEFORE the narrow is sent.
    """
    proc = _run(tmp_path, FAKE_POOL_BEFORE="1000000")

    assert proc.returncode != 0, (
        f"an unrestorable ceiling was accepted:\n{proc.transcript}"
    )
    assert not _any_limit_write(proc.requests), (
        f"the suite narrowed a pool it could not restore:\n{proc.requests}"
    )
    assert proc.final_limit == "1000000", f"the pool now reads {proc.final_limit}"
    assert "100000" in proc.transcript, (
        f"the refusal did not name the bound:\n{proc.transcript}"
    )


def test_a_failure_after_the_narrow_still_restores_the_pool(tmp_path: Path) -> None:
    """The narrow landed and the suite then died: the trap must put it back.

    Here the readback shows an effective ceiling of 0 (the provider is
    exhausted), which is fatal to the experiment -- and the pool must still be
    returned to 20 through the admin route on the way out, not left at 1.
    """
    proc = _run(tmp_path, FAKE_LIMIT_STATUS="200", FAKE_QUOTA_DERIVED="0")

    assert proc.returncode != 0
    assert len(_limit_puts(proc.requests)) == 2, (
        f"expected the narrow and the restoring PUT:\n{proc.requests}"
    )
    assert proc.final_limit == "20", (
        f"a failed run left runner:mock at {proc.final_limit}"
    )
    assert "restored runner:mock hard_limit to 20 (confirmed by readback)" in (
        proc.transcript
    ), f"the restore after a failure was not confirmed:\n{proc.transcript}"


# ---------------------------------------------------------------------------
# The fake is a fixture, so it gets checked too.
# ---------------------------------------------------------------------------

def _fake_curl(tmp: Path, *args: str, **fakes: str) -> tuple[str, str]:
    """Call the fake curl DIRECTLY, the way api_request does.

    Not through race-test.sh: these checks are about the fixture, so they must
    pass whatever the script does -- a fixture test that fails with the script
    is measuring the script.
    """
    bin_dir = tmp / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(FAKE_CURL)
    curl.chmod(0o755)
    state = tmp / "state"
    state.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update(
        FAKE_PROJECT=PROJECT,
        FAKE_STATE_DIR=str(state),
        FAKE_CURL_LOG=str(tmp / "requests.log"),
    )
    env.update(fakes)
    out = subprocess.run(
        [str(curl), "-sS", "-K", "-", "-w", "\n%{http_code}", *args],
        input="header = \"Authorization: Bearer fake\"\n",
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    body, _, status = out.rpartition("\n")
    return body, status


def test_the_fixture_really_applies_and_refuses_writes(tmp_path: Path) -> None:
    """A fake platform that accepts everything proves nothing.

    Every assertion above rests on this fake distinguishing an applied write
    from a refused one, so that distinction is asserted directly -- against the
    admin route the suite now uses -- rather than inferred from the script.
    """
    put = ("-X", "PUT", "--data-binary", '{"limit":1}', LIMIT_ROUTE)

    _, status = _fake_curl(tmp_path / "refused", *put, FAKE_LIMIT_STATUS="403")
    assert status == "403"
    assert (tmp_path / "refused" / "state" / "hard_limit").read_text() == "20"

    body, status = _fake_curl(tmp_path / "applied", *put, FAKE_LIMIT_STATUS="200")
    assert status == "200"
    assert json.loads(body)["pool"]["hard_limit"] == 1
    assert (tmp_path / "applied" / "state" / "hard_limit").read_text() == "1"

    _, status = _fake_curl(
        tmp_path / "ignored", *put, FAKE_LIMIT_STATUS="200", FAKE_LIMIT_IGNORED="1"
    )
    assert status == "200"
    assert (tmp_path / "ignored" / "state" / "hard_limit").read_text() == "20", (
        "FAKE_LIMIT_IGNORED must answer 200 and change nothing; without that the "
        "silent-failure case is not being exercised at all"
    )

    doc_url = (
        f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/swarm"
        "/documents/pools/runner:mock"
    )
    _, status = _fake_curl(tmp_path / "absent", doc_url, FAKE_POOL_ABSENT="1")
    assert status == "404", "FAKE_POOL_ABSENT must make the pool document a 404"
    body, _ = _fake_curl(tmp_path / "drained", doc_url, FAKE_POOL_ENABLED="false")
    assert json.loads(body)["fields"]["enabled"] == {"booleanValue": False}


def test_the_fixture_settles_only_after_a_cancel(tmp_path: Path) -> None:
    """FAKE_SETTLE holds a slot and a RUNNING task until a cancel, then releases.

    The complete-run cases rest on this: the sampling loop must see one slot
    held (or "the pool was actually contended" fails), and the post-storm cases
    must see it released and every task CANCELLED (or they fail). If the fake
    got the order wrong, the complete run would fail for the fixture's reasons
    and the exit-status test would be measuring nothing.
    """
    base = f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/swarm/documents"
    pool_url = f"{base}/pools/runner:mock"
    task_url = f"{base}/tasks/task-fixture-1"
    cancel = ("-X", "POST", "--data-binary", "{}", f"{API_URL}/v1/tasks/task-fixture-1/cancel")

    def read(url: str) -> dict:
        body, status = _fake_curl(tmp_path, url, FAKE_SETTLE="1")
        assert status == "200", (url, status, body)
        return json.loads(body)["fields"]

    assert read(pool_url)["active"] == {"integerValue": "1"}
    assert read(task_url)["state"] == {"stringValue": "RUNNING"}
    _, status = _fake_curl(tmp_path, *cancel, FAKE_SETTLE="1")
    assert status == "200"
    assert read(pool_url)["active"] == {"integerValue": "0"}
    assert read(task_url)["state"] == {"stringValue": "CANCELLED"}

    # And without the knob, nothing changes for the cases that rely on an idle
    # pool and an unreadable task.
    body, _ = _fake_curl(tmp_path / "unsettled", pool_url)
    assert json.loads(body)["fields"]["active"] == {"integerValue": "0"}
    body, _ = _fake_curl(tmp_path / "unsettled", task_url)
    assert json.loads(body) == {"documents": []}


def test_the_fixture_speaks_the_shapes_common_sh_uses(tmp_path: Path) -> None:
    """The fake curl has to honour -o, -w and -K, or the scripts misread it.

    tests/integration/test_register_tenant_grants.py carries the scar: its fake
    curl ignored -o and -w, which made `status` the literal string `{}` the
    moment fs_request started checking status codes, and every test in that file
    errored in its fixture for 95 commits.
    """
    proc = _run(tmp_path, FAKE_LIMIT_STATUS="403")

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
    applied = _run(tmp_path / "applied", FAKE_LIMIT_STATUS="200", FAKE_SUBMIT_STATUS="503")
    assert "hard_limit to 20" in applied.transcript, (
        "the typed Firestore document did not decode; the script never learned "
        f"the original ceiling:\n{applied.transcript}"
    )
