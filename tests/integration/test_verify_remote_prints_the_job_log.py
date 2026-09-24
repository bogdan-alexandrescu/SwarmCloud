"""`scripts/verify-remote.sh` must say WHY a target failed, not only that it did.

WHY THIS FILE EXISTS
--------------------
Release 36038727721 ran the smoke suite inside the VPC and the release log said
this, in full, about why the deploy was red:

    Executing job failed
    ERROR: (gcloud.run.jobs.execute) The execution failed.
    View details about this execution by running:
    gcloud run jobs executions describe swarm-verify-m9prt
     fail smoke-test FAILED (exit 1)

The suite's own transcript -- 9 of 11 passed, and the two failures were "could
not read the runner-profile catalogue" and "the backend matrix visited 0
backends" -- went to Cloud Logging, which the release log does not show. The
diagnosis took a second person with a second tool and the execution name copied
out by hand.

verify-remote.sh now names the execution from gcloud's own output and prints
that execution's stdout/stderr tail, oldest first, through `redact`.

THE FAKE APPLIES THE WHOLE FILTER IT IS GIVEN
---------------------------------------------
`gcloud` on PATH plays both `run jobs execute --wait` and `logging read`. For
`logging read` it parses EVERY clause of the filter -- `path="value"` as
equality and `path:"value"` as containment, over whatever path the clause names
-- keeps only the entries that satisfy all of them, and honours --order and
--limit. A clause it cannot parse is an error, never a pass.

The entries include one that each of the script's clauses alone keeps out:
another job's line, a line from something that is not a job, another
execution's line and this execution's audit event. So deleting any clause from
the script's filter prints a line that is not this execution's transcript, and
a test below fails.

The first version of this fake applied only the execution label and the logName
substring while its docstring said it applied the filter, so the resource.type
and job_name clauses could have been deleted with every test here still green.
`test_the_fake_applies_every_clause_of_the_filter_it_is_given` now holds the
fake itself to the claim.

THE RELEASE'S IDENTITY IS MODELLED, NOT ASSUMED
-----------------------------------------------
The release runs this script as swarm-tf-deployer. Its only logging role,
roles/logging.configWriter, cannot read a single entry, so until 2026-09-24 the
release log printed the refusal instead of the transcript.
terraform/bootstrap/verify_logs.tf copies the job's transcript into a log bucket
of its own and grants the deployer that bucket's view and nothing wider.

`_release_identity()` reads the view named by the grant's condition, and the
sink's filter, OUT OF THAT FILE. The fake then refuses every read except through
that one view, and the view holds only what the sink's filter routes into it.
So the release path is proven through the grant as written. Renaming the bucket,
re-scoping the grant or pointing the sink at another job fails here, not in a
release.

WHAT IT CANNOT PROVE
--------------------
* that the real Cloud Logging returns entries in this shape;
* that a sink into a bucket in its own project needs no writer grant (the
  Logging documentation says "the sink is automatically authorized");
* that the grant is APPLIED: it is in the bootstrap root, which the owner
  applies with `make bootstrap`, never the release.

The transcript entries are copied from the real swarm-verify-m9prt execution
(read with `gcloud logging read` on 2026-09-24), trimmed, plus one line
carrying a token-shaped string to prove the redaction.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "verify-remote.sh"
#: Where the release identity's read of the job log is granted.
BOOTSTRAP_VERIFY_LOGS_TF = REPO / "terraform" / "bootstrap" / "verify_logs.tf"
#: Where the job itself, and so its name, is declared.
INFRA_VERIFY_TF = REPO / "terraform" / "infra" / "verify.tf"

PROJECT = "swarm-test-project"
REGION = "us-central1"
EXECUTION = "swarm-verify-m9prt"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="verify-remote.sh needs bash and jq",
)


def _infra_job_name() -> str:
    """The name terraform/infra gives the verification job -- the job whose
    logs every filter here, the script's and the sink's, must select."""
    m = re.search(
        r'resource\s+"google_cloud_run_v2_job"\s+"verify"\s*\{.*?^\s*name\s*=\s*"([^"]+)"',
        INFRA_VERIFY_TF.read_text(),
        re.S | re.M,
    )
    assert m, "google_cloud_run_v2_job.verify has no literal name in terraform/infra/verify.tf"
    return m.group(1)


JOB = _infra_job_name()


#: Cloud Logging's filter language, as far as verify-remote.sh and the sink use
#: it: a conjunction of `path="value"` (equality) and `path:"value"`
#: (containment). Anything else is REFUSED, because a fake that skips a clause
#: it does not understand is how two of the script's four clauses went
#: unexercised. `$routed` is the filter of the sink feeding the view being read
#: ("" when the read is not through a view); `$filter` is the caller's.
FILTER_JQ = r"""
def clauses($f):
  if $f == "" then []
  else [ $f | splits(" AND ")
         | . as $c
         | ( capture("^(?<field>[A-Za-z_]+(?:\\.(?:[A-Za-z_]+|\"[^\"]+\"))*)(?<op>[=:])\"(?<value>[^\"]*)\"$")
             // error("the fake gcloud cannot apply the clause: \($c)") )
         | .path = [ .field | scan("\"[^\"]+\"|[A-Za-z_]+") | ltrimstr("\"") | rtrimstr("\"") ] ]
  end;
def satisfies($cs):
  . as $e
  | all($cs[]; . as $c
      | ($e | getpath($c.path)) as $v
      | if $c.op == "=" then $v == $c.value
        else (($v | type) == "string") and ($v | contains($c.value)) end);
clauses($routed) as $into_view
| clauses($filter) as $wanted
| [ .[] | select(satisfies($into_view)) | select(satisfies($wanted)) ]
| sort_by(.timestamp)
| if $order == "desc" then reverse else . end
| .[:$limit]
"""

#: gcloud, as far as verify-remote.sh uses it.
#:
#:   run jobs execute swarm-verify ... --args scripts/<target>.sh --wait
#:       fails, in gcloud's exact words, for a target in FAKE_FAILING_TARGETS;
#:       FAKE_EXECUTE_NAMES_NO_EXECUTION fails before an execution exists.
#:   logging read FILTER --project P [--bucket B --location L --view V]
#:                --order desc --limit N --format json
#:       answers FAKE_LOG_ENTRIES through FILTER_JQ: every clause of FILTER,
#:       then --order and --limit. FAKE_LOG_READ=denied refuses; =empty
#:       answers []. With FAKE_READABLE_VIEW set the caller is the release's
#:       identity: a read through any other view, or through none, is refused,
#:       and the view holds only the entries FAKE_VIEW_FILTER routes into it.
FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -uo pipefail
printf '%s\n' "$*" >>"${FAKE_GCLOUD_LOG}"

if [[ "$1 $2 $3" == "run jobs execute" ]]; then
  target=""; prev=""
  for a in "$@"; do [[ "${prev}" == "--args" ]] && target="${a}"; prev="${a}"; done
  echo "Creating execution..."
  if [[ -n "${FAKE_EXECUTE_NAMES_NO_EXECUTION:-}" ]]; then
    echo "ERROR: (gcloud.run.jobs.execute) PERMISSION_DENIED: Permission 'run.jobs.run' denied on resource" >&2
    exit 1
  fi
  if grep -qxF "${target}" "${FAKE_FAILING_TARGETS}"; then
    cat >&2 <<EOF
Provisioning resources.....done
Starting execution.....
Running execution.....
Executing job failed
ERROR: (gcloud.run.jobs.execute) The execution failed.
View details about this execution by running:
gcloud run jobs executions describe ${FAKE_EXECUTION}

Or visit https://console.cloud.google.com/run/jobs/executions/details/${FAKE_REGION}/${FAKE_EXECUTION}?project=209012342332
EOF
    exit 1
  fi
  echo "Execution [swarm-verify-ok000] has successfully completed."
  exit 0
fi

if [[ "$1 $2" == "logging read" ]]; then
  filter="$3"; limit=1000; order="desc"; project=""; bucket=""; location=""; view=""
  set_flag() {
    case "$1" in
      --limit) limit="$2" ;;
      --order) order="$2" ;;
      --project) project="$2" ;;
      --bucket) bucket="$2" ;;
      --location) location="$2" ;;
      --view) view="$2" ;;
    esac
  }
  prev=""
  for a in "${@:4}"; do
    case "${a}" in
      --*=*) set_flag "${a%%=*}" "${a#*=}"; prev=""; continue ;;
    esac
    [[ -n "${prev}" ]] && set_flag "${prev}" "${a}"
    prev=""
    case "${a}" in --*) prev="${a}" ;; esac
  done
  case "${FAKE_LOG_READ:-ok}" in
    denied)
      echo "ERROR: (gcloud.logging.read) PERMISSION_DENIED: Permission 'logging.views.access' denied on resource (or it may not exist)." >&2
      exit 1 ;;
    empty)
      echo '[]'; exit 0 ;;
  esac
  routed=""
  if [[ -n "${FAKE_READABLE_VIEW:-}" ]]; then
    asked="the project's own log buckets, through no view"
    if [[ -n "${view}" ]]; then
      asked="projects/${project}/locations/${location}/buckets/${bucket}/views/${view}"
    fi
    if [[ "${asked}" != "${FAKE_READABLE_VIEW}" ]]; then
      echo "ERROR: (gcloud.logging.read) PERMISSION_DENIED: this identity may read ${FAKE_READABLE_VIEW} and nothing else; asked for ${asked}" >&2
      exit 1
    fi
    routed="${FAKE_VIEW_FILTER}"
  fi
  if ! jq --arg filter "${filter}" --arg routed "${routed}" --arg order "${order}" \
          --argjson limit "${limit}" -f "${FAKE_FILTER_JQ}" "${FAKE_LOG_ENTRIES}"; then
    echo "the fake gcloud could not apply the filter: ${filter}" >&2
    exit 2
  fi
  exit 0
fi

echo "the fake gcloud has no answer for: $*" >&2
exit 2
"""

TRIPWIRE = r"""#!/usr/bin/env bash
echo "$(basename "$0") must not be reached by verify-remote.sh" >&2
exit 127
"""

#: Oldest first, as the suite wrote them. The fake serves them in the order
#: asked for.
TRANSCRIPT = [
    ("stderr", "== Smoke test: saga-agents-staging / dev =="),
    ("stderr", "[5] Baseline capacity before submitting"),
    ("stderr", "  PASS baseline captured"),
    ("stderr", "  FAIL could not read the runner-profile catalogue; this suite cannot know which backends exist"),
    ("stderr", "       header = \"Authorization: Bearer ya29.a0AfB_byC-leaked-access-token-value\""),
    ("stderr", "[6] Every execution backend was exercised"),
    ("stderr", "  FAIL the backend matrix visited 0 backends; nothing below proves any dispatch path works"),
    ("stderr", " fail smoke: 2 of 11 failed in 266s"),
    ("varlog/system", "Container called exit(1)."),
]
#: One line per clause of the script's filter, each kept out by that clause
#: ALONE. None of them may reach the release log.
AUDIT_MARKER = "AUDIT-EVENT-MUST-NOT-BE-PRINTED"
OTHER_EXECUTION = "SOMEBODY ELSES EXECUTION"
OTHER_JOB = "ANOTHER JOB'S LINE, CARRYING THIS EXECUTION'S LABEL"
NOT_A_JOB = "A LINE FROM SOMETHING THAT IS NOT A JOB"
NEIGHBOURS = (AUDIT_MARKER, OTHER_EXECUTION, OTHER_JOB, NOT_A_JOB)


def _row(stream: str, text: str, ts: str, *, execution: str = EXECUTION,
         job: str = JOB, rtype: str = "cloud_run_job") -> dict:
    return {
        "logName": f"projects/{PROJECT}/logs/run.googleapis.com%2F{stream.replace('/', '%2F')}",
        "labels": {"run.googleapis.com/execution_name": execution},
        "resource": {"type": rtype, "labels": {"job_name": job}},
        "timestamp": ts,
        "textPayload": text,
    }


def _entries() -> list[dict]:
    rows = [_row(stream, text, f"2026-09-24T18:36:{10 + i:02d}Z")
            for i, (stream, text) in enumerate(TRANSCRIPT)]
    # Shares the execution's label, as the real system_event does: only the
    # logName clause keeps it out.
    rows.append({
        "logName": f"projects/{PROJECT}/logs/cloudaudit.googleapis.com%2Fsystem_event",
        "labels": {"run.googleapis.com/execution_name": EXECUTION},
        "resource": {"type": "cloud_run_job", "labels": {"job_name": JOB}},
        "timestamp": "2026-09-24T18:40:43Z",
        "protoPayload": {"methodName": AUDIT_MARKER},
    })
    # Only the execution clause keeps this out.
    rows.append(_row("stderr", OTHER_EXECUTION, "2026-09-24T18:40:44Z",
                     execution="swarm-verify-other"))
    # Only the job_name clause keeps this out.
    rows.append(_row("stderr", OTHER_JOB, "2026-09-24T18:40:45Z",
                     job="swarm-job-eng-claude-code"))
    # Only the resource.type clause keeps this out.
    rows.append(_row("stderr", NOT_A_JOB, "2026-09-24T18:40:46Z",
                     rtype="cloud_run_revision"))
    # Deliberately NOT sorted: the fake has to honour --order itself.
    return rows


# ---------------------------------------------------------------------------
# The release identity's grant, read out of terraform rather than restated
# ---------------------------------------------------------------------------

def _hcl_unescape(s: str) -> str:
    return s.replace('\\"', '"').replace("\\\\", "\\")


def _string_locals(text: str) -> dict[str, str]:
    """Every `name = "literal"` line, unescaped -- the locals the file builds
    its view path and sink filter from."""
    return {
        m.group(1): _hcl_unescape(m.group(2))
        for m in re.finditer(r'^\s*([a-z_]+)\s*=\s*"((?:[^"\\]|\\.)*)"\s*$', text, re.M)
    }


def _resolve(value: str, local: dict[str, str], depth: int = 0) -> str:
    assert depth < 10, f"interpolation does not terminate: {value}"

    def one(m: re.Match) -> str:
        kind, name = m.group(1), m.group(2)
        if kind == "var":
            assert name == "project_id", f"cannot resolve var.{name} in {value!r}"
            return PROJECT
        assert name in local, f"local.{name} is not a string literal in {BOOTSTRAP_VERIFY_LOGS_TF.name}"
        return _resolve(local[name], local, depth + 1)

    return re.sub(r"\$\{(var|local)\.([a-z_]+)\}", one, value)


def _release_identity() -> tuple[str, str]:
    """(the view the deployer's grant names, the filter of the sink feeding it)."""
    rel = BOOTSTRAP_VERIFY_LOGS_TF.relative_to(REPO)
    assert BOOTSTRAP_VERIFY_LOGS_TF.exists(), (
        f"{rel} does not exist: nothing routes {JOB}'s log anywhere the release's "
        f"identity may read it, so the release log can only print the refusal"
    )
    text = BOOTSTRAP_VERIFY_LOGS_TF.read_text()
    local = _string_locals(text)

    grant = re.search(
        r'resource\s+"google_project_iam_member"\s+"deployer_reads_verify_logs"\s*\{(.*?)\n\}',
        text, re.S,
    )
    assert grant, f"{rel} grants the deployer nothing (google_project_iam_member.deployer_reads_verify_logs)"
    role = re.search(r'^\s*role\s*=\s*"([^"]+)"', grant.group(1), re.M)
    assert role and role.group(1) == "roles/logging.viewAccessor", (
        f"the deployer's grant in {rel} is {role.group(1) if role else 'no literal role'}; "
        f"reading one view needs roles/logging.viewAccessor, and anything wider reads "
        f"the other team's logs too"
    )
    expr = re.search(r'^\s*expression\s*=\s*"((?:[^"\\]|\\.)*)"', grant.group(1), re.M)
    assert expr, f"the deployer's grant in {rel} has no literal condition expression"
    condition = _resolve(_hcl_unescape(expr.group(1)), local)
    view = re.fullmatch(r'resource\.name == "([^"]+)"', condition)
    assert view, f"the grant's condition is not one exact view: {condition}"

    sink = re.search(
        r'resource\s+"google_logging_project_sink"\s+"verify"\s*\{(.*?)\n\}', text, re.S,
    )
    assert sink, f"{rel} declares no google_logging_project_sink.verify"
    ref = re.search(r'^\s*filter\s*=\s*local\.([a-z_]+)\s*$', sink.group(1), re.M)
    assert ref and ref.group(1) in local, f"the sink's filter in {rel} is not a string local"
    return view.group(1), _resolve(local[ref.group(1)], local)


# ---------------------------------------------------------------------------

def _install_fake(tmp: Path) -> dict[str, str]:
    bin_dir = tmp / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "gcloud").write_text(FAKE_GCLOUD)
    (bin_dir / "curl").write_text(TRIPWIRE)
    for path in bin_dir.iterdir():
        path.chmod(0o755)
    (tmp / "filter.jq").write_text(FILTER_JQ)
    (tmp / "entries.json").write_text(json.dumps(_entries()))
    (tmp / "gcloud.log").write_text("")
    (tmp / "failing").touch()
    env = dict(os.environ)
    env.update({
        "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
        "FAKE_GCLOUD_LOG": str(tmp / "gcloud.log"),
        "FAKE_FAILING_TARGETS": str(tmp / "failing"),
        "FAKE_LOG_ENTRIES": str(tmp / "entries.json"),
        "FAKE_FILTER_JQ": str(tmp / "filter.jq"),
        "FAKE_EXECUTION": EXECUTION,
        "FAKE_REGION": REGION,
    })
    return env


def _run(tmp: Path, *targets: str, failing: tuple[str, ...] = (),
         extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = _install_fake(tmp)
    (tmp / "failing").write_text("".join(f"scripts/{t}.sh\n" for t in failing))
    (tmp / "tmp").mkdir(exist_ok=True)

    env_file = tmp / "env"
    env_file.write_text(f"PROJECT_ID={PROJECT}\nREGION={REGION}\nENVIRONMENT=dev\n")
    env_file.chmod(0o600)

    env.update({
        "SWARM_ENV_FILE": str(env_file),
        "NO_COLOR": "1",
        "TMPDIR": str(tmp / "tmp"),
        "SWARM_VERIFY_LOG_WAIT_SECONDS": "0",
    })
    env.update(extra_env or {})
    proc = subprocess.run(
        ["bash", str(SCRIPT), *targets],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120,
    )
    proc.transcript = proc.stdout + proc.stderr  # type: ignore[attr-defined]
    proc.calls = (tmp / "gcloud.log").read_text().splitlines()  # type: ignore[attr-defined]
    return proc


def _explain(proc) -> str:
    calls = "\n".join(proc.calls)
    return f"exit {proc.returncode}\n--- transcript ---\n{proc.transcript}\n--- gcloud calls ---\n{calls}"


def _log_reads(proc) -> list[str]:
    return [c for c in proc.calls if c.startswith("logging read")]


def _assert_the_transcript_and_nothing_else(proc) -> None:
    out = proc.transcript
    first = "FAIL could not read the runner-profile catalogue"
    second = "FAIL the backend matrix visited 0 backends"
    summary = "smoke: 2 of 11 failed"
    for line in (first, second, summary, "Container called exit(1)."):
        assert line in out, f"missing from the release log: {line!r}\n" + _explain(proc)
    # Oldest first: the transcript reads the way the suite wrote it.
    assert out.index(first) < out.index(second) < out.index(summary), _explain(proc)
    # This execution of this job, its stdout/stderr, and nothing else.
    for neighbour in NEIGHBOURS:
        assert neighbour not in out, f"printed a line that is not this execution's transcript: {neighbour!r}\n" + _explain(proc)


def test_a_failed_target_prints_the_executions_own_log(tmp_path):
    """The two FAIL lines that took a second person to find, in the release log."""
    proc = _run(tmp_path, "smoke-test", failing=("smoke-test",))
    assert proc.returncode != 0, _explain(proc)
    assert "smoke-test FAILED" in proc.transcript, _explain(proc)
    _assert_the_transcript_and_nothing_else(proc)
    reads = _log_reads(proc)
    assert len(reads) == 1, _explain(proc)
    assert EXECUTION in reads[0] and JOB in reads[0], _explain(proc)
    assert f"--project {PROJECT}" in reads[0], _explain(proc)


def test_the_release_identity_reads_the_transcript_through_the_view_bootstrap_grants(tmp_path):
    """As swarm-tf-deployer: only the granted view is readable, and it holds
    only what the sink routes into it. The release log must still say WHY."""
    view, sink_filter = _release_identity()
    proc = _run(tmp_path, "smoke-test", failing=("smoke-test",),
                extra_env={"FAKE_READABLE_VIEW": view, "FAKE_VIEW_FILTER": sink_filter})
    assert proc.returncode != 0, _explain(proc)
    assert "could not read the log" not in proc.transcript, _explain(proc)
    _assert_the_transcript_and_nothing_else(proc)
    assert len(_log_reads(proc)) == 1, _explain(proc)


def test_the_sink_routes_this_job_s_transcript_and_nothing_else(tmp_path):
    """The bucket the deployer may read receives the job's stdout and stderr,
    every execution of it, and none of the neighbours -- in particular not the
    audit events, and not another job's (a tenant worker's) transcript."""
    _, sink_filter = _release_identity()
    env = _install_fake(tmp_path)
    proc = subprocess.run(
        ["jq", "-c", "--arg", "filter", sink_filter, "--arg", "routed", "",
         "--arg", "order", "asc", "--argjson", "limit", "1000",
         "-f", env["FAKE_FILTER_JQ"], env["FAKE_LOG_ENTRIES"]],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"the sink's filter is not one the fake can apply: {sink_filter}\n{proc.stderr}"
    routed = [r.get("textPayload") for r in json.loads(proc.stdout)]
    assert routed == [text for _, text in TRANSCRIPT] + [OTHER_EXECUTION], (
        f"sink filter {sink_filter!r} routes {routed}"
    )


def test_the_log_tail_is_redacted(tmp_path):
    """A test transcript is exactly where a token turns up."""
    proc = _run(tmp_path, "smoke-test", failing=("smoke-test",))
    assert "leaked-access-token-value" not in proc.transcript, _explain(proc)
    assert "ya29.********" in proc.transcript, _explain(proc)


def test_a_refused_log_read_is_reported_and_the_target_still_fails(tmp_path):
    """The grant not applied yet: say so, name what grants it, keep the verdict."""
    proc = _run(tmp_path, "smoke-test", failing=("smoke-test",),
                extra_env={"FAKE_LOG_READ": "denied"})
    out = proc.transcript
    assert proc.returncode != 0, _explain(proc)
    assert "smoke-test FAILED" in out, _explain(proc)
    assert f"could not read the log of execution {EXECUTION}" in out, _explain(proc)
    assert "PERMISSION_DENIED" in out, _explain(proc)
    # What grants the read, and where it is applied from.
    assert "roles/logging.viewAccessor" in out, _explain(proc)
    assert "terraform/bootstrap/verify_logs.tf" in out, _explain(proc)
    # The same lines, by hand, for whoever does hold a project-wide log read.
    assert "gcloud logging read" in out, _explain(proc)


def test_an_empty_log_is_read_three_times_in_all_then_reported_as_empty(tmp_path):
    """Ingestion lags the job, so an empty first answer is not the final one.
    Three READS in all -- the first and two retries -- not three retries."""
    proc = _run(tmp_path, "smoke-test", failing=("smoke-test",),
                extra_env={"FAKE_LOG_READ": "empty"})
    assert proc.returncode != 0, _explain(proc)
    assert len(_log_reads(proc)) == 3, _explain(proc)
    assert f"execution {EXECUTION} has no stdout/stderr" in proc.transcript, _explain(proc)
    assert "after 3 read(s)" in proc.transcript, _explain(proc)


def test_a_failure_before_any_execution_reads_no_log(tmp_path):
    """gcloud refused to create an execution: there is no log to go looking for."""
    proc = _run(tmp_path, "smoke-test", failing=("smoke-test",),
                extra_env={"FAKE_EXECUTE_NAMES_NO_EXECUTION": "1"})
    assert proc.returncode != 0, _explain(proc)
    assert _log_reads(proc) == [], _explain(proc)
    assert "gcloud named no execution for smoke-test" in proc.transcript, _explain(proc)


def test_a_passing_target_reads_no_log_and_passes(tmp_path):
    proc = _run(tmp_path, "smoke-test")
    assert proc.returncode == 0, _explain(proc)
    assert _log_reads(proc) == [], _explain(proc)


def test_every_target_still_runs_after_one_fails(tmp_path):
    """Printing a log must not stop the gate before the remaining targets."""
    proc = _run(tmp_path, "smoke-test", "concurrency-test", failing=("smoke-test",))
    executes = [c for c in proc.calls if c.startswith("run jobs execute")]
    assert len(executes) == 2, _explain(proc)
    assert "1 of 2 targets failed: smoke-test" in proc.transcript, _explain(proc)
    assert proc.returncode != 0, _explain(proc)


# ---------------------------------------------------------------------------
# The fake itself: the claim this file's docstring makes about it
# ---------------------------------------------------------------------------

def _fake_read(tmp: Path, env: dict, filter_: str, *flags: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gcloud", "logging", "read", filter_, "--project", PROJECT, *flags, "--format", "json"],
        env=env, capture_output=True, text=True, timeout=30, cwd=tmp,
    )


def test_the_fake_applies_every_clause_of_the_filter_it_is_given(tmp_path):
    """Each clause, alone, keeps out exactly the neighbour it exists for; the
    fake honours --order and --limit; and a clause it cannot parse is an
    error rather than a clause it quietly ignores."""
    env = _install_fake(tmp_path)
    clauses = {
        f'resource.type="cloud_run_job"': NOT_A_JOB,
        f'resource.labels.job_name="{JOB}"': OTHER_JOB,
        f'labels."run.googleapis.com/execution_name"="{EXECUTION}"': OTHER_EXECUTION,
        'logName:"run.googleapis.com%2F"': AUDIT_MARKER,
    }

    def texts(proc) -> list[str]:
        assert proc.returncode == 0, proc.stderr
        return [r.get("textPayload") or r.get("protoPayload", {}).get("methodName")
                for r in json.loads(proc.stdout)]

    everything = texts(_fake_read(tmp_path, env, ""))
    assert set(NEIGHBOURS) <= set(everything), everything
    for clause, excluded in clauses.items():
        kept = texts(_fake_read(tmp_path, env, clause))
        assert excluded not in kept, f"{clause} did not keep out {excluded!r}: {kept}"
        assert sorted(set(everything) - set(kept)) == [excluded], (
            f"{clause} kept out more than {excluded!r}: {sorted(set(everything) - set(kept))}"
        )

    transcript = [text for _, text in TRANSCRIPT]
    both = " AND ".join(clauses)
    assert texts(_fake_read(tmp_path, env, both, "--order", "asc")) == transcript
    assert texts(_fake_read(tmp_path, env, both, "--order", "desc")) == transcript[::-1]
    assert texts(_fake_read(tmp_path, env, both, "--order", "desc", "--limit", "2")) == transcript[:-3:-1]

    for unparsed in ('severity>=ERROR', 'NOT logName:"x"', 'textPayload=~"FAIL"'):
        proc = _fake_read(tmp_path, env, f'{both} AND {unparsed}')
        assert proc.returncode == 2, f"the fake accepted {unparsed!r} it cannot apply: {proc.stdout}"
