"""e2e-test.sh's checks [6], [7] and [10] judge the platform, not the test.

The 2026-10-20 baseline of `make verify-remote` (execution swarm-verify-cltfv)
failed three e2e checks, each on a test defect (#1754, items 3-5):

  [7]  it scanned EVERY tenant's attempts and failed on the API's 404 for the
       smoke tenant's tasks, while the gate runs as eng. That 404 is tenant
       isolation working. The scan is now scoped to the tenant this run's API
       calls act as, and a control asserts another tenant's task IS refused.
  [10] it compared every capacity holder with Cloud Run executions only, and
       flagged ten eng claude-code agents running on GKE Autopilot. It now reads
       the newest attempt's backend and asks GKE for GKE work. Since
       VERIFY-GKE-READ swarm-verify holds that read (terraform/bootstrap/
       verify_cluster_read.tf, owner-applied; and in eng and smoke,
       kubernetes/rbac/verify-rbac.yaml), so a refused cluster read is the grant
       missing and fails the check; an unreachable cluster, or a namespace the
       read is not rendered in, is still "not checked".
  [6]  it expected the producer that never wrote its promised file to SUCCEED.
       The platform now fails that producer (`expected outputs missing`,
       end_cause outputs_missing) and its dependant never runs.

The functions are lifted from the shipped script and run with every read stubbed.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from .gate_script_functions import SCRIPTS, firestore_document, functions, lines, run_bash

SUITE = SCRIPTS / "e2e-test.sh"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")


def attempt_doc(aid: str, task: str, tenant: str, **extra) -> dict:
    return firestore_document("attempts", aid, {"attempt_id": aid, "task_id": task, "tenant_id": tenant, **extra})


# --- [7] the spend scan is the caller's tenant's ----------------------------


def test_the_spend_scan_keeps_only_the_calling_tenants_attempts(tmp_path: Path) -> None:
    """The baseline's three smoke tasks must never reach the API comparison."""
    rows = [
        attempt_doc("att_0128e390edcc468d9be0", "task_dcedd511e54345349960", "smoke", input_tokens=10),
        attempt_doc("att_a", "task_19c440f8627f44548f8c", "smoke", input_tokens=11),
        attempt_doc("att_b", "task_eng_1", "eng", input_tokens=12),
        attempt_doc("att_c", "task_25633ce0bd874d619819", "smoke", input_tokens=13),
        attempt_doc("att_d", "task_eng_2", "eng", input_tokens=14),
    ]
    feed = tmp_path / "rows.jsonl"
    feed.write_text("".join(json.dumps(r) + "\n" for r in rows))
    r = run_bash(functions(SUITE, "own_tenant_rows") + f'\nown_tenant_rows eng 25 <"{feed}"\n')
    assert r.returncode == 0, r.stderr
    got = [json.loads(line) for line in r.stdout.splitlines() if line.strip()]
    # MUTATION: drop the tenant filter and the smoke tenant's three attempts come back.
    assert [g["task_id"] for g in got] == ["task_eng_1", "task_eng_2"]


def test_the_spend_scan_honours_its_cap(tmp_path: Path) -> None:
    rows = [attempt_doc(f"att_{i}", f"task_{i}", "eng", input_tokens=i) for i in range(40)]
    feed = tmp_path / "rows.jsonl"
    feed.write_text("".join(json.dumps(r) + "\n" for r in rows))
    r = run_bash(functions(SUITE, "own_tenant_rows") + f'\nown_tenant_rows eng 25 <"{feed}"\n')
    # MUTATION: ignore MAX and every one of the 40 is compared.
    assert len(r.stdout.splitlines()) == 25


ISOLATION_STUB = r'''
fs_query() { [[ -s "${FOREIGN_ROW}" ]] && cat "${FOREIGN_ROW}"; return 0; }
api_get() {
  printf '%s\n' "$1" >>"${API_LOG}"
  API_STATUS="${FOREIGN_STATUS}"
  printf '{"detail":"x"}'
  [[ "${API_STATUS}" -ge 200 && "${API_STATUS}" -lt 300 ]]
}
'''


def _isolation(tmp: Path, status: str, *, foreign: bool = True):
    row = tmp / "foreign.jsonl"
    row.write_text(json.dumps(attempt_doc("att_s", "task_dcedd511e54345349960", "smoke")) + "\n" if foreign else "")
    log = tmp / "api.log"
    r = run_bash(
        ISOLATION_STUB + functions(SUITE, "foreign_tenant_refused") + "\nforeign_tenant_refused eng\n",
        env={"FOREIGN_ROW": str(row), "FOREIGN_STATUS": status, "API_LOG": str(log), "TMPDIR": str(tmp)},
    )
    asked = log.read_text().split() if log.exists() else []
    return r, asked


def test_another_tenants_task_is_refused_with_404(tmp_path: Path) -> None:
    r, asked = _isolation(tmp_path, "404")
    assert r.returncode == 0, r.stderr
    assert asked == ["/tasks/task_dcedd511e54345349960/attempts"]
    # MUTATION: never ask about another tenant's task and no PASS for isolation is printed.
    assert any("404" in p for p in lines(r, "PASS")), r.stderr
    assert lines(r, "FAIL") == []


def test_another_tenants_task_served_to_this_caller_is_a_failure(tmp_path: Path) -> None:
    r, _ = _isolation(tmp_path, "200")
    # MUTATION: accept any answer for the foreign task and broken isolation passes.
    fails = lines(r, "FAIL")
    assert any("isolation" in f for f in fails), r.stderr


def test_a_refusal_other_than_404_is_reported_as_what_it_is(tmp_path: Path) -> None:
    r, _ = _isolation(tmp_path, "500")
    # MUTATION: treat every non-2xx as "refused" and a 500 reads as isolation.
    assert any("HTTP 500" in f for f in lines(r, "FAIL")), r.stderr


def test_no_other_tenant_to_test_against_is_not_measured_not_passed(tmp_path: Path) -> None:
    r, asked = _isolation(tmp_path, "404", foreign=False)
    assert asked == []
    assert lines(r, "PASS") == []
    # MUTATION: print PASS when there was nothing to refuse.
    assert len(lines(r, "SKIP")) == 1, r.stderr


# --- [10] a GKE-backed holder is asked of GKE, or reported not checked ------

GKE_EXEC = "swarm-tenant-eng/swarm-cf154a98da3f42bfae36-1"

VERDICT_STUB = r'''
gke_job_status() { printf '%s %s\n' "$1" "$2" >>"${GKE_LOG}"; printf '%s' "${GKE_ANSWER}"; }
gke_cluster_access() { [[ -n "${GKE_ACCESS}" ]] || GKE_ACCESS="${GKE_ACCESS_ANSWER}"; }
'''


def _verdict(tmp: Path, attempt: dict, *, access: str = "ok", answer: str = "running", running: str = ""):
    log = tmp / "gke.log"
    body = (
        functions(SUITE, "capacity_holder_verdict")
        + VERDICT_STUB
        + "\nGKE_ACCESS=''\n"
        + f"capacity_holder_verdict '{json.dumps(attempt)}' eng '{running}'\n"
        + 'printf "%s|%s\\n" "${VERDICT}" "${VERDICT_DETAIL}"\n'
    )
    r = run_bash(body, env={"GKE_LOG": str(log), "GKE_ANSWER": answer, "GKE_ACCESS_ANSWER": access, "TMPDIR": str(tmp)})
    asked = log.read_text().splitlines() if log.exists() else []
    verdict, _, detail = r.stdout.strip().partition("|")
    return r, verdict, detail, asked


GKE_ATTEMPT = {"attempt_id": "att_g", "task_id": "task_cf154a98da3f42bfae36", "tenant_id": "eng",
               "generation": 1, "backend": "GKE_AUTOPILOT", "execution_name": GKE_EXEC}


def test_a_gke_holder_the_cluster_refuses_is_refused_not_skipped(tmp_path: Path) -> None:
    """The baseline's ten eng claude-code agents, now that swarm-verify holds the
    cluster read: a 403 on it is the grant missing, and that is a finding."""
    r, verdict, detail, asked = _verdict(
        tmp_path, GKE_ATTEMPT,
        access="refused:container.clusters.get on swarm-autopilot answered HTTP 403")
    assert r.returncode == 0, r.stderr
    # MUTATION: keep reading a refusal as "unchecked" and a gate whose grant was
    # never applied passes [10] on a skip, which is the silence it exists to end.
    assert verdict == "refused", (verdict, detail)
    assert "HTTP 403" in detail
    assert asked == []


def test_a_gke_holder_on_an_unreachable_cluster_is_not_checked(tmp_path: Path) -> None:
    r, verdict, detail, asked = _verdict(
        tmp_path, GKE_ATTEMPT, access="no:the GKE API could not be reached (curl exit 28)")
    assert r.returncode == 0, r.stderr
    # MUTATION: compare it with Cloud Run executions again and it is "gone".
    assert verdict == "unchecked", (verdict, detail)
    assert "not checked: the GKE API could not be reached" in detail
    assert asked == []


def test_a_job_read_refused_in_its_namespace_is_not_checked(tmp_path: Path) -> None:
    """A personal namespace is not given the verify read (render.py
    verify_read_files), so a jobs.get 403 there is not a finding."""
    _, verdict, detail, _ = _verdict(
        tmp_path, GKE_ATTEMPT, answer="unreadable no RBAC read (jobs.get in swarm-tenant-eng answered HTTP 403)")
    # MUTATION: read the job's 403 as "gone" and every holder in a namespace
    # without the read fails.
    assert verdict == "unchecked", (verdict, detail)
    assert "jobs.get in swarm-tenant-eng" in detail


def test_a_gke_holder_whose_job_runs_is_fine(tmp_path: Path) -> None:
    r, verdict, _, asked = _verdict(tmp_path, GKE_ATTEMPT, answer="running")
    assert verdict == "ok", r.stderr
    # MUTATION: derive the namespace from the tenant and ignore the attempt's,
    # or split the name wrongly, and the job asked for is not the one dispatched.
    assert asked == ["swarm-tenant-eng swarm-cf154a98da3f42bfae36-1"]


@pytest.mark.parametrize("answer", ["gone", "finished"])
def test_a_gke_holder_whose_job_is_gone_or_finished_is_a_mismatch(tmp_path: Path, answer: str) -> None:
    _, verdict, detail, _ = _verdict(tmp_path, GKE_ATTEMPT, answer=answer)
    # MUTATION: treat every GKE answer as fine and a dead GKE Job holds capacity unseen.
    assert verdict == "gone", detail
    assert answer in detail


def test_a_gke_job_named_without_a_namespace_is_looked_up_in_the_tenants(tmp_path: Path) -> None:
    attempt = dict(GKE_ATTEMPT, execution_name="swarm-cf154a98da3f42bfae36-1")
    _, verdict, _, asked = _verdict(tmp_path, attempt, answer="running")
    assert verdict == "ok"
    # MUTATION: hard-code a namespace instead of tenant_namespace(eng).
    assert asked == ["swarm-tenant-eng swarm-cf154a98da3f42bfae36-1"]


#: #1804: before SWARM_EXECUTION_NAME, a GKE worker recorded its POD's hostname,
#: the Job's name and a 5-character suffix, with no namespace.
LEGACY_POD = "swarm-cf154a98da3f42bfae36-1-2cq5s"


def test_a_live_job_recorded_under_its_pod_name_is_asked_for_as_its_job(tmp_path: Path) -> None:
    """The 06:45Z gate run: seven live agents called gone over a 404 on jobs/<pod>."""
    attempt = dict(GKE_ATTEMPT, execution_name=LEGACY_POD)
    r, verdict, detail, asked = _verdict(tmp_path, attempt, answer="running")
    assert r.returncode == 0, r.stderr
    # MUTATION: look the pod name up as a Job and the cluster's 404 makes a
    # live agent "gone"; strip more or less than the pod suffix and the same.
    assert asked == ["swarm-tenant-eng swarm-cf154a98da3f42bfae36-1"]
    assert verdict == "ok", (verdict, detail)


def test_a_job_recorded_under_its_pod_name_that_is_really_gone_still_fails(tmp_path: Path) -> None:
    attempt = dict(GKE_ATTEMPT, execution_name=LEGACY_POD)
    _, verdict, detail, asked = _verdict(tmp_path, attempt, answer="gone")
    assert asked == ["swarm-tenant-eng swarm-cf154a98da3f42bfae36-1"]
    # MUTATION: call every legacy record "ok" or "unchecked" and a dead GKE Job
    # recorded before the fix holds capacity unseen.
    assert verdict == "gone", (verdict, detail)


def test_a_live_job_recorded_as_namespace_and_job_passes(tmp_path: Path) -> None:
    """The name every attempt records since #1804."""
    _, verdict, _, asked = _verdict(tmp_path, GKE_ATTEMPT, answer="running")
    assert asked == ["swarm-tenant-eng swarm-cf154a98da3f42bfae36-1"]
    assert verdict == "ok"


@pytest.mark.parametrize(
    "name,changes",
    [
        # Another task's pod.
        ("swarm-3f3f8e2700ed46f89c17-1-2cq5s", {}),
        # This task's pod, another generation's Job.
        ("swarm-cf154a98da3f42bfae36-2-2cq5s", {}),
        # Not a pod suffix.
        ("swarm-cf154a98da3f42bfae36-1-2cq5", {}),
        ("swarm-cf154a98da3f42bfae36-1-2CQ5S", {}),
        # A pod name with no generation to check it against.
        (LEGACY_POD, {"generation": None}),
        # Something that is no Job or pod of this task at all.
        ("localhost", {}),
    ],
)
def test_a_bare_name_that_is_not_this_attempts_job_or_pod_is_not_checked(
        tmp_path: Path, name: str, changes: dict) -> None:
    attempt = dict(GKE_ATTEMPT, execution_name=name, **changes)
    _, verdict, detail, asked = _verdict(tmp_path, attempt, answer="gone")
    # MUTATION: look any bare name up as a Job and a name the gate cannot map
    # exactly reads "gone" off a 404 that proves nothing about this attempt.
    assert asked == [], asked
    assert verdict == "unchecked", (verdict, detail)
    assert "not checked" in detail


def test_a_cloud_run_holder_is_still_compared_with_cloud_run(tmp_path: Path) -> None:
    attempt = {"attempt_id": "att_c", "task_id": "t", "tenant_id": "eng", "generation": 1,
               "backend": "CLOUD_RUN_JOB",
               "execution_name": "projects/p/locations/r/jobs/swarm-eng-mock/executions/exec-1"}
    _, verdict, _, asked = _verdict(tmp_path, attempt, running="exec-0 exec-1")
    assert verdict == "ok"
    assert asked == []
    _, verdict, detail, _ = _verdict(tmp_path, attempt, running="exec-0")
    # MUTATION: drop the Cloud Run comparison and a dead execution passes.
    assert verdict == "gone", detail


def test_an_attempt_naming_no_execution_is_still_a_mismatch(tmp_path: Path) -> None:
    attempt = dict(GKE_ATTEMPT, execution_name=None)
    _, verdict, _, _ = _verdict(tmp_path, attempt)
    assert verdict == "gone"


CURL_403 = r'''
curl() {
  local out="" fmt="" a
  printf 'curl' >>"${CURL_LOG}"; printf ' %s' "$@" >>"${CURL_LOG}"; printf '\n' >>"${CURL_LOG}"
  while [[ $# -gt 0 ]]; do
    case "$1" in -o) out="$2"; shift 2 ;; -w) fmt="$2"; shift 2 ;; *) shift ;; esac
  done
  cat >/dev/null
  printf '{"error":{"code":403,"message":"Required container.clusters.get permission"}}' >"${out}"
  printf '403'
}
access_token() { printf 'fake'; }
'''


def test_a_refused_cluster_read_is_a_refusal_naming_the_grant(tmp_path: Path) -> None:
    log = tmp_path / "curl.log"
    r = run_bash(
        CURL_403 + functions(SUITE, "gke_cluster_access")
        + '\nGKE_ACCESS=""\nPROJECT_ID=p\nGKE_LOCATION=us-central1\nGKE_CLUSTER=swarm-autopilot\n'
        + 'gke_cluster_access\nprintf "%s\\n" "${GKE_ACCESS}"\n',
        env={"CURL_LOG": str(log), "TMPDIR": str(tmp_path)},
    )
    assert r.returncode == 0, r.stderr
    # MUTATION: leave a 403 as "no:" and the missing grant is a skip again.
    assert r.stdout.strip().startswith("refused:"), r.stdout
    # The reader is sent to the grant, not left to guess which one: the
    # owner-applied bootstrap root, since VERIFY-GKE-READ-2.
    # MUTATION: name terraform/infra/verify.tf again, where CI cannot grant it.
    assert "terraform/bootstrap/verify_cluster_read.tf" in r.stdout
    assert "terraform/infra/verify.tf" not in r.stdout
    assert "clusters/swarm-autopilot" in log.read_text()


def test_a_refused_job_read_names_the_rbac_and_its_apply(tmp_path: Path) -> None:
    curl_job_403 = CURL_403.replace("container.clusters.get", "jobs.get")
    r = run_bash(
        curl_job_403 + functions(SUITE, "gke_job_status")
        + f'\nGKE_CA_FILE={tmp_path}/ca\nGKE_ENDPOINT=10.0.0.2\n'
        + 'gke_job_status swarm-tenant-eng swarm-x-1\n',
        env={"CURL_LOG": str(tmp_path / "curl.log"), "TMPDIR": str(tmp_path)},
    )
    assert r.returncode == 0, r.stderr
    # MUTATION: read a 403 as "gone" and a namespace without the read fails every holder.
    assert r.stdout.startswith("unreadable "), r.stdout
    assert "kubernetes/rbac/verify-rbac.yaml" in r.stdout
    assert "kubernetes/apply.sh --tenant eng" in r.stdout
    # apply.sh resolves swarm-verify's uniqueId itself (VERIFY-GKE-READ-2), and
    # refuses the flag on its command line, so the reader is not told to pass it.
    # MUTATION: keep "--verify-uid <swarm-verify uniqueId>" in the message.
    assert "--verify-uid" not in r.stdout


# --- [10]'s judgement over what the loop counted -----------------------------


def _judge(tmp: Path, checked: int, mismatched: int, unchecked: int, refused: int, access: str = "ok"):
    return run_bash(
        functions(SUITE, "judge_capacity_holders")
        + f"\nGKE_ACCESS='{access}'\n"
        + f"judge_capacity_holders {checked} {mismatched} {unchecked} {refused}\n",
        env={"TMPDIR": str(tmp)},
    )


def test_a_refused_cluster_read_fails_the_check(tmp_path: Path) -> None:
    r = _judge(tmp_path, 3, 0, 0, 10, access="refused:container.clusters.get on swarm-autopilot answered HTTP 403")
    assert r.returncode == 0, r.stderr
    fails = lines(r, "FAIL")
    # MUTATION: report refused holders with t_skip and this is empty.
    assert len(fails) == 1, r.stderr
    assert "10 task(s)" in fails[0] and "HTTP 403" in fails[0]


def test_unchecked_holders_are_a_skip_and_not_a_failure(tmp_path: Path) -> None:
    r = _judge(tmp_path, 3, 0, 2, 0)
    assert lines(r, "FAIL") == [], r.stderr
    # MUTATION: fail on unchecked and a personal namespace without the read reds the gate.
    assert len(lines(r, "SKIP")) == 1, r.stderr
    assert len(lines(r, "PASS")) == 1, r.stderr


def test_a_gone_gke_job_fails_the_check(tmp_path: Path) -> None:
    r = _judge(tmp_path, 3, 1, 0, 0)
    # MUTATION: drop the assert_eq and a dead GKE Job holding capacity passes.
    assert len(lines(r, "FAIL")) == 1, r.stderr


def test_nothing_holding_capacity_is_not_measured(tmp_path: Path) -> None:
    r = _judge(tmp_path, 0, 0, 0, 0)
    assert lines(r, "FAIL") == [] and lines(r, "PASS") == [], r.stderr
    assert len(lines(r, "SKIP")) == 1, r.stderr


def test_the_shared_cluster_is_never_read(tmp_path: Path) -> None:
    log = tmp_path / "curl.log"
    r = run_bash(
        CURL_403 + functions(SUITE, "gke_cluster_access")
        + '\nGKE_ACCESS=""\nPROJECT_ID=p\nGKE_LOCATION=us-central1\nGKE_CLUSTER=agents-staging\n'
        + 'gke_cluster_access\nprintf "%s\\n" "${GKE_ACCESS}"\n',
        env={"CURL_LOG": str(log), "TMPDIR": str(tmp_path)},
    )
    # MUTATION: drop the is_shared_resource guard and the other team's cluster is queried.
    assert not log.exists(), log.read_text()
    assert r.stdout.strip().startswith("no:")


# --- [6] the negative control expects the producer to fail -----------------

NEG_STUB = r'''
lookup() {
  local key="$1" table="$2" pair
  for pair in ${table}; do
    if [[ "${pair%%=*}" == "${key}" ]]; then printf '%s' "${pair#*=}"; return 0; fi
  done
  return 1
}
wait_for_state() { lookup "$1" "${STATES}"; }
task_field() { jq -r "$2" "${DOCS}/$1.json"; }
task_lease_ids() { local v; v="$(lookup "$1" "${LEASES}")" || return 1; [[ "${v}" == "-" ]] || printf '%s\n' "${v}"; }
TERMINAL="SUCCEEDED|FAILED|CANCELLED|DEAD_LETTERED"
TIMEOUT=5
MISSING_NAME="never-written-20261020.txt"
'''

NEW_ERROR = (
    "expected outputs missing (not written: never-written-20261020.txt). A later step of this "
    "workflow stages them from this task, so the attempt failed; it is retried while the task "
    "has attempts left, and the retry must write every expected output again."
)


def _negative(tmp: Path, *, produce: str, consume: str, produce_error: str, consume_leases: str = "-"):
    docs = tmp / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "p.json").write_text(json.dumps({"state": produce, "last_error": produce_error, "end_cause": "outputs_missing"}))
    (docs / "c.json").write_text(json.dumps({"state": consume, "last_error": None}))
    return run_bash(
        NEG_STUB + functions(SUITE, "check_negative_control") + "\ncheck_negative_control p c\n",
        env={"STATES": f"p={produce} c={consume}", "LEASES": f"p=lp c={consume_leases}",
             "DOCS": str(docs), "TMPDIR": str(tmp)},
    )


def test_the_producer_failing_for_its_missing_output_passes(tmp_path: Path) -> None:
    r = _negative(tmp_path, produce="FAILED", consume="CANCELLED", produce_error=NEW_ERROR)
    assert r.returncode == 0, r.stderr
    # MUTATION: expect SUCCEEDED from the producer again and this FAILs, as on 2026-10-20.
    assert lines(r, "FAIL") == [], r.stderr
    passes = " ".join(lines(r, "PASS"))
    assert "names the file that was missing" in passes
    assert "never ran" in passes


def test_the_old_behaviour_is_now_the_failure(tmp_path: Path) -> None:
    """A producer that SUCCEEDS without its promised file is the defect now."""
    r = _negative(tmp_path, produce="SUCCEEDED", consume="FAILED",
                  produce_error="", consume_leases="lc")
    # MUTATION: accept any terminal producer state and this passes.
    assert any("FAILED" in f for f in lines(r, "FAIL")), r.stderr


def test_a_consumer_that_was_leased_ran_on_a_missing_input(tmp_path: Path) -> None:
    r = _negative(tmp_path, produce="FAILED", consume="CANCELLED", produce_error=NEW_ERROR, consume_leases="lc")
    # MUTATION: check only the consumer's state and a child that started reads as never run.
    assert any("leased" in f for f in lines(r, "FAIL")), r.stderr


def test_a_consumer_that_succeeded_is_caught(tmp_path: Path) -> None:
    r = _negative(tmp_path, produce="FAILED", consume="SUCCEEDED", produce_error=NEW_ERROR)
    assert any("reported SUCCEEDED" in f for f in lines(r, "FAIL")), r.stderr


def test_an_error_that_does_not_name_the_file_is_caught(tmp_path: Path) -> None:
    r = _negative(tmp_path, produce="FAILED", consume="CANCELLED",
                  produce_error="expected outputs missing (not written). Retried.")
    # MUTATION: drop the filename check (kept from the old control) and this passes.
    assert any("does not name" in f for f in lines(r, "FAIL")), r.stderr


def test_a_producer_failed_for_another_reason_is_caught(tmp_path: Path) -> None:
    r = _negative(tmp_path, produce="FAILED", consume="CANCELLED",
                  produce_error="the runner exited 1 before writing never-written-20261020.txt")
    # MUTATION: accept any FAILED producer and a crash reads as the missing-output refusal.
    assert any("expected outputs missing" in f for f in lines(r, "FAIL")), r.stderr
