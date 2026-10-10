"""The hotfix lane ships a labelled commit to dev without queueing behind a
normal release, and cannot do anything the normal release would have stopped.

Owner decision 2026-10-08 (observer proposal H; docs/ci.md, "Hotfix
releases"). Measured that day over 22 successful release.yml runs: new digests
live 43 min p50 / 97 p90 after the run is created, `release-dev` held 100 min
p50, and a hotfix (#857) waited about 1 h 45 min. hotfix.yml runs on every push
to main in a concurrency group of its own and proceeds only for a pull request
merged with the `hotfix` label; it runs release.yml's own stages
(.github/actions/release-*), skips verify, warm, acceptance, the GKE proof and
the plugin tag, and FAILS on a plan that changes IAM.

What this file holds, modelled on test_release_prod_gate.py and
test_release_dev_iam_gate.py:

* the gate (scripts/lib/hotfix-gate.sh) proceeds only for a MERGED pull
  request into main carrying the label, and nothing that promotes, applies or
  deploys starts unless it did;
* an IAM-changing plan fails the hotfix before anything is applied, and the
  normal release still holds it for dev-iam;
* scripts/lib/release-order.sh, which both lanes use, refuses a commit that is
  not a descendant of what is already promoted or applied (green,
  "superseded"), and its lock is exclusive;
* release.yml keeps its prod gate and its dev-iam gate after its steps moved
  into the composite actions, and both lanes run the same ones.

WHAT THIS CANNOT PROVE: that GitHub runs the workflows as read, that `gcloud
storage cp --if-generation-match=0` is atomic (GCS documents it as a
precondition on the write), or that the deployer's binding admits hotfix.yml
before the owner applies terraform/bootstrap. Only a labelled merge on main
proves the lane end to end.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys

import pytest

from . import test_release_prod_gate as gate_model
from .test_release_prod_gate import (
    DEV_IAM_ENVIRONMENT,
    PROD_FACING,
    PROD_RELEASES,
    _attempts,
    _condition,
    _environment,
    _holders,
    _shell,
)
from .test_release_reuses_ci_images import (
    REPO,
    _code,
    _evaluate,
    _load_workflow,
    _needs,
    _reuse_mode,
    _steps,
    _upstream,
    _workflow,
)

GATE_SH = REPO / "scripts" / "lib" / "hotfix-gate.sh"
ORDER_SH = REPO / "scripts" / "lib" / "release-order.sh"
ACTIONS = REPO / ".github" / "actions"

PUSH = {
    "github.event_name": "push",
    "github.ref": "refs/heads/main",
    "github.event.inputs.environment": None,
    "github.event.inputs.skip_build": None,
}
DISPATCH = {**PUSH, "github.event_name": "workflow_dispatch"}


def _hotfix() -> dict:
    return _workflow("hotfix.yml")


def _jobs() -> dict:
    return _hotfix()["jobs"]


# ---------------------------------------------------------------------------
# The workflow's shape


def test_it_runs_on_main_only_and_never_on_a_pull_request():
    on = _hotfix()["on"]
    assert set(on) == {"push", "workflow_dispatch"}, f"hotfix.yml triggers on {sorted(on)}"
    assert on["push"]["branches"] == ["main"]
    # Exactly where a release can start, so a hotfix is possible exactly where
    # a release is.
    release_paths = set(_workflow("release.yml")["on"]["push"]["paths"])
    assert release_paths <= set(on["push"]["paths"]), sorted(release_paths - set(on["push"]["paths"]))
    assert ".github/workflows/hotfix.yml" in on["push"]["paths"]


def test_the_concurrency_group_is_on_the_work_jobs_and_never_on_the_gate_or_the_workflow():
    # An ordinary push's gate-only run must not enter the group: GitHub keeps
    # one pending run per group, and it would displace a queued hotfix.
    assert "concurrency" not in _hotfix(), _hotfix().get("concurrency")
    jobs = _jobs()
    assert "concurrency" not in jobs["gate"]
    work = [name for name in jobs if name != "gate"]
    assert work, "no work jobs"
    for name in work:
        assert jobs[name].get("concurrency") == {"group": "hotfix-dev", "cancel-in-progress": False}, name
    # Every work job runs only on the gate's proceed output: the first
    # directly, the rest through the chain of needs.
    assert jobs["images"]["needs"] == ["gate"]
    assert jobs["images"]["if"] == "needs.gate.outputs.proceed == 'true'"

    def reaches_images(name):
        needs = jobs[name].get("needs") or []
        needs = [needs] if isinstance(needs, str) else needs
        return name == "images" or any(reaches_images(n) for n in needs)

    for name in work:
        assert reaches_images(name), name
    assert "release-" not in str(_workflow("release.yml")["concurrency"]["group"]).replace("release-${{", "")


def test_the_gate_reads_the_label_and_mints_no_google_identity():
    job = _jobs()["gate"]
    assert job.get("if") == "github.ref == 'refs/heads/main'"
    assert "id-token" not in (job.get("permissions") or {}), job.get("permissions")
    assert (job.get("permissions") or {}).get("pull-requests") == "read"
    runs = [_code(s.get("run", "")) for s in job["steps"]]
    called = [r for r in runs if "hotfix-gate.sh" in r]
    assert len(called) == 1 and "--label hotfix" in called[0], runs
    assert '>> "$GITHUB_OUTPUT"' in called[0]
    assert job["outputs"]["proceed"] == "${{ steps.label.outputs.proceed }}"


@pytest.fixture
def gated(monkeypatch):
    """The prod-gate model, taught the one step output hotfix.yml's job
    conditions read."""
    monkeypatch.setitem(gate_model.STEP_OUTPUTS, ("gate", "proceed"), ("true", "false", ""))
    return _jobs()


@pytest.mark.parametrize("ctx", [PUSH, DISPATCH], ids=["push", "dispatch"])
def test_nothing_prod_facing_starts_unless_the_gate_proceeds(gated, ctx):
    """MUTATION: drop `if: needs.gate.outputs.proceed == 'true'` from `images`,
    or give `promote` an `always()`."""
    jobs = gated
    facing = sorted({j for what in PROD_FACING for j in _holders(jobs, what)})
    assert {"promote", "infrastructure", "deploy"} <= set(facing), facing
    for job_id in facing:
        assert "gate" in _upstream(jobs, job_id), f"hotfix.yml's {job_id} does not wait for the gate"
    tried = 0
    for results, outs in _attempts(jobs, ctx):
        if outs["gate"].get("proceed") == "true" and results["gate"] == "success":
            continue
        tried += 1
        started = [j for j in facing if results[j] != "skipped"]
        assert not started, f"with the gate {results['gate']} and proceed={outs['gate'].get('proceed')!r}: {results}"
    assert tried


@pytest.mark.parametrize("ctx", [PUSH, DISPATCH], ids=["push", "dispatch"])
def test_a_labelled_commit_reaches_every_stage(gated, ctx):
    jobs = gated
    clean = [r for r, o in _attempts(jobs, ctx, endings=("success",)) if o["gate"].get("proceed") == "true"]
    assert clean
    for results in clean:
        assert all(r == "success" for r in results.values()), results


def test_it_is_dev_only_and_names_no_environment():
    """No approval to wait for -- and so nothing that could reach prod, or
    stand in for dev-iam."""
    for job_id, job in _jobs().items():
        assert job.get("environment") is None, f"hotfix.yml's {job_id} names an environment"
        if job_id != "gate":
            assert (job.get("env") or {}).get("ENVIRONMENT") == "dev", f"hotfix.yml's {job_id} is not pinned to dev"


def test_it_obtains_ci_s_record_and_never_builds():
    hotfix = _hotfix()
    builds = list(_steps(hotfix, "build-images.sh"))
    assert len(builds) == 1
    for event in ("push", "workflow_dispatch"):
        assert _reuse_mode(builds[0][3], event) == "only"


def test_it_skips_what_the_normal_release_of_the_same_push_still_runs():
    """verify (CI's record implies it passed), warm, acceptance, the GKE proof
    and the plugin tag -- all still run by release.yml (verify on a dispatch)
    or by accept.yml after it."""
    jobs = _jobs()
    code = "\n".join(_code(s.get("run", "")) for job in jobs.values() for s in job.get("steps") or [])
    for absent in ("pytest", "warm-jobs.sh", "acceptance/", "prove-gke-dispatch", "git push", "tag -a"):
        assert absent not in code, f"hotfix.yml runs {absent!r}"
    for present in ("push-images.sh", "plan-guard.sh", "--classify-iam", "deploy.sh --verify-only", "smoke-test"):
        assert present in code, f"hotfix.yml never runs {present!r}"
    # release.yml, and since 2026-10-08 accept.yml, which runs when the
    # release of the same push completes (cut A of the release timing report).
    rcode = "\n".join(
        _code(s.get("run", ""))
        for name in ("release.yml", "accept.yml")
        for job in _workflow(name)["jobs"].values()
        for s in job.get("steps") or []
    )
    for kept in ("pytest tests/unit", "warm-jobs.sh", "acceptance/", "prove-gke-dispatch"):
        assert kept in rcode, f"neither release.yml nor accept.yml runs {kept!r}; the hotfix lane relies on it"


def test_the_hotfix_waits_longer_for_terraform_s_state_lock():
    applies = [s for s in _jobs()["infrastructure"]["steps"] if PROD_FACING["applies"](_shell(s))]
    assert len(applies) == 1
    assert "'900s'" in str(applies[0].get("env")), applies[0].get("env")


# ---------------------------------------------------------------------------
# A plan that changes IAM


def _infra_steps(workflow: str) -> list[dict]:
    return _workflow(workflow)["jobs"]["infrastructure"]["steps"]


def _runs_step(step: dict, iam: str, environment: str = "dev") -> bool:
    text = _condition("step", step.get("if"))
    values = {
        "env.ENVIRONMENT": environment,
        "github.event.inputs.environment": environment,
        "steps.iam.outputs.iam": iam,
        "steps.order.outputs.superseded": "false",
        "always()": True,
        "success()": True,
        "cancelled()": False,
    }
    return bool(_evaluate("${{ " + text + " }}", **values))


def _refusal(steps: list[dict]) -> list[dict]:
    return [s for s in steps if re.search(r"(^|\n)\s*exit\s+1\s*$", _shell(s).rstrip()) and "dev-iam" in _shell(s)]


@pytest.mark.parametrize("iam", ["true", "false", ""])
def test_an_iam_plan_fails_the_hotfix_before_anything_is_applied(iam):
    """MUTATION: condition the refusal on `inputs.lane == 'release'`, or let
    the apply step run on 'true'."""
    steps = _infra_steps("hotfix.yml")
    refusal = _refusal(steps)
    assert len(refusal) == 1, "the hotfix lane has no step that refuses an IAM plan and names dev-iam"
    message = _shell(refusal[0])
    assert "release.yml" in message and "dev-iam" in message
    applies = [i for i, s in enumerate(steps) if PROD_FACING["applies"](_shell(s))]
    assert len(applies) == 1
    assert steps.index(refusal[0]) < applies[0], "the refusal comes after the apply"
    assert _runs_step(refusal[0], iam) is (iam == "true")
    assert _runs_step(steps[applies[0]], iam) is (iam == "false")
    held = [s for s in steps if s.get("id") == "held"]
    assert held and not _runs_step(held[0], iam), "the hotfix lane holds a plan for a dev-iam job it does not have"


def test_the_hotfix_has_no_dev_iam_job():
    for ctx in (PUSH, DISPATCH):
        named = [j for j, job in _jobs().items() if _environment(job, ctx) == DEV_IAM_ENVIRONMENT]
        assert not named


@pytest.mark.parametrize("environment", ["dev", "prod"])
def test_the_normal_release_still_holds_an_iam_plan_for_dev_iam(environment):
    steps = _infra_steps("release.yml")
    refusal = _refusal(steps)
    assert len(refusal) == 1
    assert not _runs_step(refusal[0], "true", environment), "release.yml refuses the IAM plan dev-iam should review"
    held = [s for s in steps if s.get("id") == "held"]
    assert len(held) == 1 and _runs_step(held[0], "true", environment)


# ---------------------------------------------------------------------------
# release.yml after its steps moved into the composite actions


def test_release_yml_keeps_its_prod_gate_and_its_dev_iam_gate():
    jobs = _workflow("release.yml")["jobs"]
    for ctx in PROD_RELEASES.values():
        assert [j for j, job in jobs.items() if _environment(job, ctx) == "prod"] == ["approval"]
    assert [j for j, job in jobs.items() if _environment(job, PUSH) == DEV_IAM_ENVIRONMENT] == ["infrastructure-iam"]
    assert _needs(jobs["promote"]) == ["build", "approval"]
    assert "approval" in _needs(jobs["infrastructure"]) and "approval" in _needs(jobs["deploy"])
    assert "infrastructure-iam" in _needs(jobs["deploy"])
    for what in PROD_FACING:
        assert _holders(jobs, what), f"no step of release.yml {what} once its composite actions are flattened"


def _composites(workflow: str) -> dict[str, set[str]]:
    raw = _load_workflow(workflow)
    used: dict[str, set[str]] = {}
    for job_id, job in raw["jobs"].items():
        for step in job.get("steps") or []:
            uses = str(step.get("uses", ""))
            if uses.startswith("./.github/actions/"):
                used.setdefault(uses, set()).add(job_id)
    return used


def test_both_lanes_run_the_same_stages():
    """One copy of the steps. MUTATION: inline a stage's steps back into
    either workflow."""
    release, hotfix = _composites("release.yml"), _composites("hotfix.yml")
    stages = {f"./.github/actions/{p.name}" for p in ACTIONS.iterdir() if p.name.startswith("release-")}
    assert stages == set(release) == set(hotfix), (stages, release, hotfix)
    for uses in stages:
        assert len(release[uses]) == 1 and len(hotfix[uses]) == 1, (uses, release[uses], hotfix[uses])


def test_no_composite_names_vars_or_an_environment():
    """A composite action cannot read `vars`, and the gates stay in the
    workflows."""
    for path in sorted(ACTIONS.glob("release-*/action.yml")):
        body = path.read_text().split("\nruns:\n", 1)[1]
        code = "\n".join(l for l in body.splitlines() if not l.lstrip().startswith("#"))
        expressions = re.findall(r"\$\{\{.*?\}\}", code, re.S) + re.findall(r"^\s*if:.*$", code, re.M)
        assert not any(re.search(r"\bvars\.", e) for e in expressions), f"{path} reads vars"
        assert "environment:" not in re.sub(r"--environment", "", code).replace("environment: ${{", ""), path


@pytest.mark.parametrize("name", ["TERRAFORM_VERSION", "PYTHON_VERSION", "TF_ROOT", "TRIVY_VERSION"])
def test_the_two_lanes_pin_the_same_tools(name):
    release, hotfix = _workflow("release.yml")["env"], _hotfix()["env"]
    assert release[name] == hotfix[name], f"{name}: release.yml {release[name]!r}, hotfix.yml {hotfix[name]!r}"


@pytest.mark.parametrize("workflow", ["release.yml", "hotfix.yml"])
def test_every_job_that_orders_the_release_reads_main_s_history(workflow):
    raw = _load_workflow(workflow)
    for job_id, job in _workflow(workflow)["jobs"].items():
        if "release-order.sh" not in "\n".join(_code(s.get("run", "")) for s in job.get("steps") or []):
            continue
        checkout = next(s for s in raw["jobs"][job_id]["steps"] if str(s.get("uses", "")).startswith("actions/checkout@"))
        assert (checkout.get("with") or {}).get("fetch-depth") == 0, f"{workflow}'s {job_id} orders with a shallow clone"


def test_every_lock_is_released_whatever_happened():
    for workflow in ("release.yml", "hotfix.yml"):
        for job_id, job in _workflow(workflow)["jobs"].items():
            steps = job.get("steps") or []
            locks = [s for s in steps if re.search(r"release-order\.sh lock\b", _code(s.get("run", "")))]
            unlocks = [s for s in steps if re.search(r"release-order\.sh unlock\b", _code(s.get("run", "")))]
            assert len(locks) == len(unlocks), f"{workflow}'s {job_id}: {len(locks)} locks, {len(unlocks)} unlocks"
            for s in unlocks:
                assert str(s["if"]).startswith("always()"), f"{workflow}'s {job_id} leaves the lock on a failure"
                assert steps.index(s) == len(steps) - 1 or all(
                    "release-order.sh lock" not in _code(t.get("run", "")) for t in steps[steps.index(s) :]
                )


# ---------------------------------------------------------------------------
# scripts/lib/hotfix-gate.sh, against a fake `gh`

SHA = "a" * 40
FAKE_GH = r"""#!{python}
import os, sys
if os.environ.get("FAKE_GH_FAIL"):
    sys.stderr.write("HTTP 502: bad gateway\n"); sys.exit(1)
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\n")
sys.stdout.write(open(os.environ["FAKE_GH_PULLS"]).read())
"""


def _pr(number, *, merged=True, base="main", labels=()):
    return {
        "number": number,
        "merged_at": "2026-10-08T10:00:00Z" if merged else None,
        "base": {"ref": base},
        "labels": [{"name": n} for n in labels],
    }


def _gate(tmp_path, pulls, **env):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH.replace("{python}", sys.executable))
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "pulls.json").write_text(json.dumps(pulls))
    proc = subprocess.run(
        [str(GATE_SH), "--sha", SHA, "--repo", "owner/name", "--label", "hotfix"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_GH_PULLS": str(tmp_path / "pulls.json"),
            "FAKE_GH_LOG": str(tmp_path / "gh.log"),
            "SWARM_HOTFIX_GATE_RETRY_DELAY": "0",
            **env,
        },
        timeout=60,
    )
    return proc, dict(l.split("=", 1) for l in proc.stdout.splitlines() if "=" in l)


def test_the_gate_proceeds_for_a_merged_pull_request_with_the_label(tmp_path):
    proc, out = _gate(tmp_path, [_pr(857, labels=("bug", "hotfix"))])
    assert proc.returncode == 0, proc.stderr
    assert out == {"proceed": "true", "pr": "857"}
    assert f"repos/owner/name/commits/{SHA}/pulls" in (tmp_path / "gh.log").read_text()


@pytest.mark.parametrize(
    "pulls",
    [
        [_pr(1)],
        [_pr(2, merged=False, labels=("hotfix",))],
        [_pr(3, base="release", labels=("hotfix",))],
        [_pr(4, labels=("hotfixes",))],
        [],
    ],
    ids=["merged-unlabelled", "open-labelled", "other-base-labelled", "other-label", "no-pull-request"],
)
def test_the_gate_does_not_proceed_without_a_merged_labelled_pull_request(tmp_path, pulls):
    """MUTATION: drop `.merged_at != null`, or the base check, from the jq."""
    proc, out = _gate(tmp_path, pulls)
    assert proc.returncode == 0, proc.stderr
    assert out.get("proceed") == "false", out


def test_the_gate_fails_rather_than_guess_when_github_does_not_answer(tmp_path):
    proc, out = _gate(tmp_path, [_pr(857, labels=("hotfix",))], FAKE_GH_FAIL="1")
    assert proc.returncode == 1
    assert "proceed" not in out
    assert "502" in proc.stderr


# ---------------------------------------------------------------------------
# scripts/lib/release-order.sh, against a fake `gcloud storage` with generations

FAKE_GCLOUD = r"""#!{python}
import os, sys
root = os.environ["FAKE_GCS_ROOT"]
args = sys.argv[1:]
if args[:1] != ["storage"]:
    sys.stderr.write("fake gcloud: only `storage` is modelled\n"); sys.exit(2)
cmd = args[1]
rest = args[2:]
if cmd == "objects":
    cmd, rest = "objects " + rest[0], rest[1:]
match = None
plain = []
for a in rest:
    if a.startswith("--if-generation-match="):
        match = int(a.split("=", 1)[1])
    elif a.startswith("--"):
        continue
    else:
        plain.append(a)
def path(url):
    assert url.startswith("gs://"), url
    return os.path.join(root, url[5:])
def gen(p):
    return int(open(p + ".gen").read()) if os.path.exists(p + ".gen") else 0
def missing(url):
    sys.stderr.write(f"ERROR: (gcloud.storage) gs object not found: 404 {url}\n"); sys.exit(1)
if cmd == "objects describe":
    p = path(plain[0])
    if not os.path.exists(p): missing(plain[0])
    sys.stdout.write('{"generation": "%d", "name": "%s"}\n' % (gen(p), plain[0]))
elif cmd == "cat":
    p = path(plain[0])
    if not os.path.exists(p): missing(plain[0])
    sys.stdout.write(open(p).read())
elif cmd == "cp":
    src, url = plain
    p = path(url)
    exists = os.path.exists(p)
    if match is not None and ((match == 0 and exists) or (match != 0 and gen(p) != match)):
        sys.stderr.write("ERROR: PreconditionFailed: 412 At least one of the pre-conditions you specified did not hold.\n"); sys.exit(1)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    counter = os.path.join(root, ".counter")
    n = (int(open(counter).read()) if os.path.exists(counter) else 1000) + 1
    open(counter, "w").write(str(n))
    open(p, "w").write(open(src).read())
    open(p + ".gen", "w").write(str(n))
elif cmd == "rm":
    p = path(plain[0])
    if not os.path.exists(p): missing(plain[0])
    if match is not None and gen(p) != match:
        sys.stderr.write("ERROR: PreconditionFailed: 412\n"); sys.exit(1)
    os.remove(p); os.remove(p + ".gen")
else:
    sys.stderr.write(f"fake gcloud: {cmd} is not modelled\n"); sys.exit(2)
"""


@pytest.fixture
def world(tmp_path):
    """A repository with main = A -> B -> C, an unrelated commit X, and an
    empty fake state bucket."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"},
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    shas = {}
    for name in ("A", "B", "C"):
        git("commit", "-q", "--allow-empty", "-m", name)
        shas[name] = git("rev-parse", "HEAD")
    git("checkout", "-q", "--orphan", "other")
    git("commit", "-q", "--allow-empty", "-m", "X")
    shas["X"] = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gcloud = bin_dir / "gcloud"
    gcloud.write_text(FAKE_GCLOUD.replace("{python}", sys.executable))
    gcloud.chmod(gcloud.stat().st_mode | stat.S_IXUSR)
    gcs = tmp_path / "gcs"
    gcs.mkdir()
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")},
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GCS_ROOT": str(gcs),
        "TF_STATE_BUCKET": "state",
        "SWARM_ENV_FILE": str(tmp_path / "no.env"),
        "SWARM_RELEASE_ORDER_CHECKOUT": str(repo),
        "SWARM_RELEASE_LOCK_POLL": "0",
        "GITHUB_WORKFLOW": "release",
        "GITHUB_RUN_ID": "42",
        "NO_COLOR": "1",
    }

    class World:
        pass

    w = World()
    w.repo, w.shas, w.gcs, w.env, w.git, w.tmp = repo, shas, gcs, env, git, tmp_path
    return w


def _order(world, *args: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(ORDER_SH), *args], capture_output=True, text=True, env={**world.env, **env}, timeout=60
    )


def _verdict(world, sha: str, **env: str) -> str:
    proc = _order(world, "check", "--environment", "dev", "--sha", sha, **env)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip().splitlines()[-1]


def _record(world, what: str, sha: str) -> None:
    proc = _order(world, "record", "--environment", "dev", "--what", what, "--sha", sha)
    assert proc.returncode == 0, proc.stderr


def test_with_nothing_recorded_every_commit_proceeds(world):
    assert _verdict(world, world.shas["A"]) == "proceed"


@pytest.mark.parametrize("what", ["applied", "promoted"])
def test_a_commit_older_than_what_is_out_is_superseded(world, what):
    """The hotfix applied C; the normal release of B reaches its apply.
    MUTATION: swap the arguments of `merge-base --is-ancestor`."""
    _record(world, what, world.shas["C"])
    assert _verdict(world, world.shas["B"]) == "superseded"
    assert _verdict(world, world.shas["A"]) == "superseded"


def test_the_same_commit_and_its_descendants_proceed(world):
    _record(world, "applied", world.shas["B"])
    _record(world, "promoted", world.shas["B"])
    assert _verdict(world, world.shas["B"]) == "proceed"
    assert _verdict(world, world.shas["C"]) == "proceed"


def test_a_commit_that_is_no_ancestor_at_all_is_superseded(world):
    _record(world, "applied", world.shas["X"])
    assert _verdict(world, world.shas["C"]) == "superseded"


def test_the_verdict_reaches_github_output_and_says_why(world):
    _record(world, "applied", world.shas["C"])
    out = world.tmp / "github_output"
    proc = _order(world, "check", "--environment", "dev", "--sha", world.shas["A"], GITHUB_OUTPUT=str(out))
    assert proc.returncode == 0, proc.stderr
    assert out.read_text() == "superseded=true\n"
    assert "::notice title=Superseded::" in proc.stdout
    out.unlink()
    _order(world, "check", "--environment", "dev", "--sha", world.shas["C"], GITHUB_OUTPUT=str(out))
    assert out.read_text() == "superseded=false\n"


def test_a_record_naming_no_commit_is_refused_not_guessed(world):
    (world.gcs / "state/releases/dev").mkdir(parents=True)
    (world.gcs / "state/releases/dev/applied.json").write_text('{"sha": "main"}')
    (world.gcs / "state/releases/dev/applied.json.gen").write_text("7")
    proc = _order(world, "check", "--environment", "dev", "--sha", world.shas["C"])
    assert proc.returncode == 1
    assert "names no commit SHA" in proc.stderr


def test_a_shallow_checkout_is_refused(world, tmp_path):
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{world.repo}", str(shallow)], check=True)
    _record(world, "applied", world.shas["B"])
    proc = _order(world, "check", "--environment", "dev", "--sha", world.shas["C"],
                  SWARM_RELEASE_ORDER_CHECKOUT=str(shallow))
    assert proc.returncode == 1
    assert "shallow" in proc.stderr


def test_the_record_says_what_shipped_and_who_shipped_it(world):
    _record(world, "applied", world.shas["C"])
    record = json.loads((world.gcs / "state/releases/dev/applied.json").read_text())
    assert record["sha"] == world.shas["C"]
    assert record["what"] == "applied" and record["environment"] == "dev"
    assert record["workflow"] == "release" and record["run_id"] == "42"


def _lock(world, **env: str) -> subprocess.CompletedProcess:
    return _order(world, "lock", "--environment", "dev", "--sha", world.shas["C"], **env)


def test_the_lock_is_exclusive_and_released_by_generation(world):
    """MUTATION: drop --if-generation-match=0 from the create."""
    first = _lock(world)
    assert first.returncode == 0, first.stderr
    generation = first.stdout.strip().removeprefix("generation=")
    assert generation.isdigit(), first.stdout
    second = _lock(world, SWARM_RELEASE_LOCK_WAIT="0", GITHUB_WORKFLOW="hotfix")
    assert second.returncode == 1
    assert "held by release run 42" in second.stderr
    assert "Nothing was changed" in second.stderr
    released = _order(world, "unlock", "--environment", "dev", "--generation", generation)
    assert released.returncode == 0, released.stderr
    assert not (world.gcs / "state/releases/dev/apply.lock").exists()
    assert _lock(world, GITHUB_WORKFLOW="hotfix").returncode == 0


def test_unlock_never_deletes_another_lane_s_lock(world):
    first = _lock(world)
    generation = first.stdout.strip().removeprefix("generation=")
    # Broken as stale and taken by the other lane meanwhile.
    (world.gcs / "state/releases/dev/apply.lock").unlink()
    (world.gcs / "state/releases/dev/apply.lock.gen").unlink()
    assert _lock(world, GITHUB_WORKFLOW="hotfix").returncode == 0
    proc = _order(world, "unlock", "--environment", "dev", "--generation", generation)
    assert proc.returncode == 0, proc.stderr
    assert "left alone" in proc.stderr
    assert (world.gcs / "state/releases/dev/apply.lock").exists()


def test_a_lock_whose_holder_died_is_broken_once_stale(world):
    assert _lock(world).returncode == 0
    path = world.gcs / "state/releases/dev/apply.lock"
    held = json.loads(path.read_text())
    held["at"] = 1
    path.write_text(json.dumps(held))
    proc = _lock(world, SWARM_RELEASE_LOCK_WAIT="0", SWARM_RELEASE_LOCK_STALE="60", GITHUB_WORKFLOW="hotfix")
    assert proc.returncode == 0, proc.stderr
    assert "Breaking it" in proc.stderr
    assert json.loads(path.read_text())["holder"].startswith("hotfix run")


def test_unless_superseded_turns_a_failure_green_only_when_superseded(world, tmp_path):
    out = tmp_path / "github_output"
    fail = ["--", "sh", "-c", "exit 3"]
    proc = _order(world, "unless-superseded", "--environment", "dev", "--sha", world.shas["B"], *fail,
                  GITHUB_OUTPUT=str(out))
    assert proc.returncode == 3, "a failure that is not explained by a newer commit stays a failure"
    assert not out.exists() or "superseded=true" not in out.read_text()

    _record(world, "applied", world.shas["C"])
    proc = _order(world, "unless-superseded", "--environment", "dev", "--sha", world.shas["B"], *fail,
                  GITHUB_OUTPUT=str(out))
    assert proc.returncode == 0, proc.stderr
    assert out.read_text().splitlines()[-1] == "superseded=true"

    ok = _order(world, "unless-superseded", "--environment", "dev", "--sha", world.shas["B"], "--", "true")
    assert ok.returncode == 0


def test_unless_superseded_is_the_command_alone_on_prod(world):
    """A prod release is a person's choice of commit, behind its approval; a
    deliberate rollback must not be called superseded."""
    _record(world, "applied", world.shas["C"])
    proc = _order(world, "unless-superseded", "--environment", "prod", "--sha", world.shas["A"], "--", "sh", "-c", "exit 4")
    assert proc.returncode == 4


@pytest.mark.parametrize("script", [ORDER_SH, GATE_SH], ids=lambda p: p.name)
def test_the_scripts_are_executable_and_shellcheck_clean(script):
    assert os.access(script, os.X_OK)
    lines = [l for l in script.read_text().splitlines() if l.strip() and not l.startswith("#")]
    assert lines[0] == "set -euo pipefail"
    proc = subprocess.run(["shellcheck", "-x", str(script)], capture_output=True, text=True, cwd=REPO)
    assert proc.returncode == 0, proc.stdout


def test_every_step_reading_the_order_runs_on_dev_only_in_the_composites():
    """Ordering prod would refuse a deliberate rollback. In the shared stages
    every lock, check and record is conditioned on a non-prod environment."""
    for workflow in ("release.yml",):
        for job_id, job in _workflow(workflow)["jobs"].items():
            if job_id == "infrastructure-iam":
                continue  # never runs on prod (test_release_dev_iam_gate.py)
            for step in job.get("steps") or []:
                code = _code(step.get("run", ""))
                if re.search(r"release-order\.sh (lock|check|record)\b", code):
                    for ctx in PROD_RELEASES.values():
                        values = {
                            **ctx,
                            "env.ENVIRONMENT": "prod",
                            "always()": True,
                            "success()": True,
                            "cancelled()": False,
                            "steps.promote.outcome": "success",
                            "steps.apply.outcome": "success",
                        }
                        text = _condition(f"{job_id} {step.get('name')}", step.get("if"))
                        assert not _evaluate("${{ " + text + " }}", **values), (
                            f"release.yml's {job_id} step {step.get('name')!r} orders a prod release"
                        )
