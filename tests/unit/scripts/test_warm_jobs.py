"""`scripts/warm-jobs.sh` runs one no-op execution of each worker job after a release (#363).

WHY IT EXISTS. Measured 2026-10-07 on #363: the first execution of each Cloud
Run job after a new image digest spends 30-59 s importing the image
(ContainerReady "Imported container image in Xs"); every later start pays
1-3 s. Owner decision the same day: a post-deploy warm run per Cloud Run job
absorbs that import, so the first tenant task after a release does not.

The properties asserted here, against the REAL script with a fake `gcloud` on
PATH serving a job listing:

  * it executes exactly the per-tenant worker jobs -- terraform-managed, named
    `swarm-job-*`, carrying a `swarm-tenant` label -- and nothing else in the
    listing (not swarm-verify, not a job something else created);
  * every execution passes `--args=--self-test`, the worker's no-op mode, so
    no task is leased and no Firestore task state is touched, and `--wait`;
  * a deny-listed name (scripts/lib/common.sh SHARED_DENY_LIST) is refused and
    never executed, whether it was named on the command line or listed;
  * a name outside the worker-job prefix is refused the same way;
  * a failed warm execution, or a listing that failed, is a WARNING and the
    script still exits 0: a cold start is a slower first task, not a broken
    release;
  * it prints how many jobs it visited, so an empty run cannot read as a pass.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "warm-jobs.sh"
PROJECT = "swarm-test-project"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="warm-jobs.sh needs bash and jq",
)

FAKE_GCLOUD = r'''#!{python}
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_EVENTS"], "a") as fh:
    fh.write(json.dumps(args) + "\n")
if args[:3] == ["run", "jobs", "list"]:
    if os.environ.get("FAKE_LIST_FAIL"):
        print("ERROR: (gcloud.run.jobs.list) PERMISSION_DENIED: run.jobs.list", file=sys.stderr)
        sys.exit(1)
    print(open(os.environ["FAKE_LISTING"]).read())
    sys.exit(0)
if args[:3] == ["run", "jobs", "execute"]:
    job = args[3]
    if job in os.environ.get("FAKE_FAIL_JOBS", "").split(","):
        print(f"ERROR: (gcloud.run.jobs.execute) The execution failed. "
              f"gcloud run jobs executions describe {job}-abcde", file=sys.stderr)
        sys.exit(1)
    print(f"Execution [{job}-xyz12] has successfully completed.", file=sys.stderr)
    sys.exit(0)
print("fake gcloud: unexpected call " + " ".join(args), file=sys.stderr)
sys.exit(2)
'''


def _job(name: str, *, managed: str = "swarm-terraform", tenant: str | None = "acme") -> dict:
    labels = {"managed-by": managed}
    if tenant is not None:
        labels["swarm-tenant"] = tenant
    return {"metadata": {"name": name, "labels": labels}}


LISTING = [
    _job("swarm-job-acme-mock"),
    _job("swarm-job-acme-claude-code"),
    _job("swarm-job-globex-mock", tenant="globex"),
    # Terraform's verification job: not a worker, no tenant.
    _job("swarm-verify", tenant=None),
    # Created by something other than terraform.
    _job("swarm-job-acme-adhoc", managed="swarm-scheduler"),
    # Terraform-managed but no tenant label: not a per-tenant worker job.
    _job("swarm-job-orphan", tenant=None),
    # Another team's name, even dressed as ours.
    _job("default"),
    _job("agents-staging"),
]


@pytest.fixture
def world(tmp_path: Path) -> dict:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gcloud = bindir / "gcloud"
    gcloud.write_text(FAKE_GCLOUD.replace("{python}", sys.executable))
    gcloud.chmod(0o755)
    (tmp_path / "listing.json").write_text(json.dumps(LISTING))
    env = {k: v for k, v in os.environ.items()
           if k not in ("K_SERVICE", "CLOUD_RUN_JOB", "SWARM_IMPERSONATE_SA", "SWARM_NAME_PREFIX")}
    env.update({
        "PATH": f"{bindir}{os.pathsep}{env['PATH']}",
        "FAKE_EVENTS": str(tmp_path / "events.jsonl"),
        "FAKE_LISTING": str(tmp_path / "listing.json"),
        "SWARM_ENV_FILE": str(tmp_path / "no.env"),
        "PROJECT_ID": PROJECT,
        "REGION": "us-central1",
        "NO_COLOR": "1",
    })
    return {"env": env, "tmp": tmp_path}


def _run(world: dict, *args: str, **extra: str) -> subprocess.CompletedProcess:
    env = dict(world["env"], **extra)
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True,
                          env=env, timeout=120, check=False)


def _executed(world: dict) -> list[list[str]]:
    path = world["tmp"] / "events.jsonl"
    if not path.exists():
        return []
    calls = [json.loads(line) for line in path.read_text().splitlines()]
    return [c for c in calls if c[:3] == ["run", "jobs", "execute"]]


def test_warms_each_per_tenant_worker_job_once_with_the_self_test(world):
    proc = _run(world)
    assert proc.returncode == 0, proc.stderr
    calls = _executed(world)
    jobs = sorted(c[3] for c in calls)
    assert jobs == ["swarm-job-acme-claude-code", "swarm-job-acme-mock", "swarm-job-globex-mock"]
    for call in calls:
        assert "--args=--self-test" in call
        assert "--wait" in call
        assert call[call.index("--project") + 1] == PROJECT
        assert call[call.index("--region") + 1] == "us-central1"
    assert "3 of 3" in proc.stderr


def test_a_deny_listed_name_on_the_command_line_is_refused_and_never_executed(world):
    proc = _run(world, "agents-staging", "swarm-job-acme-mock")
    assert proc.returncode != 0
    assert "agents-staging" in proc.stderr
    assert "deny-list" in proc.stderr
    assert [c[3] for c in _executed(world)] == ["swarm-job-acme-mock"]


def test_a_deny_listed_name_in_the_listing_is_never_executed(world):
    (world["tmp"] / "listing.json").write_text(json.dumps([
        _job("swarm-job-acme-mock"),
        _job("default"),
    ]))
    proc = _run(world)
    executed = [c[3] for c in _executed(world)]
    assert "default" not in executed
    assert executed == ["swarm-job-acme-mock"]
    assert proc.returncode == 0, proc.stderr


def test_a_name_outside_the_worker_job_prefix_is_refused(world):
    proc = _run(world, "swarm-verify")
    assert proc.returncode != 0
    assert "swarm-verify" in proc.stderr
    assert _executed(world) == []


def test_a_failed_warm_execution_is_a_warning_not_a_failed_release(world):
    proc = _run(world, FAKE_FAIL_JOBS="swarm-job-acme-claude-code")
    assert proc.returncode == 0, proc.stderr
    assert "warn" in proc.stderr
    assert "swarm-job-acme-claude-code" in proc.stderr
    assert "2 of 3" in proc.stderr
    # The others were still warmed.
    assert len(_executed(world)) == 3


def test_a_listing_that_failed_is_a_warning_and_warms_nothing(world):
    proc = _run(world, FAKE_LIST_FAIL="1")
    assert proc.returncode == 0, proc.stderr
    assert "warn" in proc.stderr
    assert "could not list" in proc.stderr
    assert _executed(world) == []


def test_no_worker_job_listed_says_so_rather_than_reading_as_a_pass(world):
    (world["tmp"] / "listing.json").write_text(json.dumps([_job("swarm-verify", tenant=None)]))
    proc = _run(world)
    assert proc.returncode == 0, proc.stderr
    assert "no per-tenant worker job" in proc.stderr
    assert _executed(world) == []


def test_parallelism_bounds_each_batch(world):
    proc = _run(world, WARM_PARALLELISM="1")
    assert proc.returncode == 0, proc.stderr
    assert len(_executed(world)) == 3


def test_a_parallelism_that_is_not_a_positive_integer_is_refused(world):
    proc = _run(world, WARM_PARALLELISM="0")
    assert proc.returncode != 0
    assert "WARM_PARALLELISM" in proc.stderr
    assert _executed(world) == []


def test_the_release_warms_after_verifying_the_digests_and_never_fails_on_it():
    from .test_release_reuses_ci_images import _workflow

    # Composite actions flattened into the steps they run (the verify step is
    # .github/actions/release-verify).
    workflow = _workflow("release.yml")
    steps = workflow["jobs"]["deploy"]["steps"]
    runs = [str(s.get("run", "")) for s in steps]
    warm = [i for i, r in enumerate(runs) if "scripts/warm-jobs.sh" in r]
    assert len(warm) == 1, "release.yml's deploy job must run scripts/warm-jobs.sh exactly once"
    index = warm[0]
    verify = next(i for i, s in enumerate(steps) if s.get("id") == "verify")
    smoke = next(i for i, r in enumerate(runs) if "verify-remote.sh smoke-test" in r)
    assert verify < index < smoke
    step = steps[index]
    assert step.get("continue-on-error") is True
    assert "steps.verify.outcome == 'success'" in step["if"]
    assert "!cancelled()" in step["if"]
    # Acceptance is a later job that needs deploy, so it runs after the warm step.
    assert "deploy" in workflow["jobs"]["acceptance"]["needs"]
