"""A release reuses the images CI built for its commit, instead of building
them a second time.

THE DEFECT THIS PINS. Every push to main built all eight images twice:
application.yml's `build images` job, and release.yml's own build, competing
for Cloud Build in a project whose build quota is shared with another team --
and the release waited behind the duplicate. Measured on commit 94ee043:
application.yml built it 16:33:09-16:41:51 (run 36027980122), and release
36027980448 built the same commit again 16:39:24-16:48:17. The owner decided on
2026-09-24 that application.yml builds once per commit on main and the release
promotes exactly what that build recorded.

`scripts/lib/ci-built-images.sh` is how the release finds that record, and
`scripts/build-images.sh --reuse-ci` is the front door the release calls. The
properties asserted here, against the REAL scripts with a fake `gh` (and, for
the build path, a fake `gcloud`) on PATH:

  * a build still running is WAITED FOR, and a run still queued with no build
    job yet is not mistaken for "never built";
  * a FAILED build is reported as failed -- with the job's URL -- and is never
    rebuilt, not even by a release that is allowed to build;
  * "never built" (no run, cancelled, skipped, or built for another
    environment) fails a push release saying how to release the commit anyway,
    and hands a dispatched release exit 3, on which build-images.sh builds;
  * an unreadable GitHub API is never read as "never built";
  * a record that names another commit or another environment is refused;
  * a pull request's run of the same commit is never reused;
  * `--reuse-ci only` submits NO Cloud Build; `--reuse-ci or-build` on a
    commit CI never built builds every image, at the commit's tag, and records
    the commit in the manifest;
  * the job and artifact the script looks for are the ones application.yml
    actually has -- read from application.yml, not restated here.

WHAT THIS CANNOT PROVE: that GitHub's API answers in the shape the fake
serves. The shapes are the documented ones (workflow runs filtered by
head_sha, a run's jobs, a run's artifacts by name, `gh run download`), and a
job that calls a reusable workflow is matched both as `<name>` and as
`<name> / <called job>`. Only a release on main exercises the real API.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
OWNER_REPO = "owner/swarm"
REGISTRY = "us-central1-docker.pkg.dev/swarm-test-project/swarm-images"
SHA = "c0ffee" * 6 + "abcd"
OTHER_SHA = "0badc0de" * 5
IMAGES = [
    "agent-runtime-base",
    "agent-runtime-browser",
    "swarm-api",
    "swarm-scheduler",
    "swarm-quota-broker",
    "swarm-reconciler",
    "swarm-ui",
    "swarm-verify",
]

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None or shutil.which("git") is None,
    reason="these scripts need bash, jq and git",
)


# ---------------------------------------------------------------------------
# What application.yml actually builds and uploads, read from the file, so the
# fake world below is built from the real names and a rename fails here.
# ---------------------------------------------------------------------------
def _workflow(name: str) -> dict:
    data = yaml.safe_load((REPO / ".github" / "workflows" / name).read_text())
    # PyYAML reads the bare key `on` as the boolean True.
    if True in data:
        data["on"] = data.pop(True)
    return data


def _uncommented(run: str) -> str:
    return "\n".join(l for l in run.splitlines() if not l.lstrip().startswith("#"))


def application_build() -> dict:
    """The job in application.yml that builds the images on main, with the
    name it runs under, the environment it builds for and the artifact it
    uploads -- or an assertion naming what could not be found."""
    jobs = _workflow("application.yml")["jobs"]
    builders = [
        (job_id, job)
        for job_id, job in jobs.items()
        for step in job.get("steps") or []
        if "scripts/build-images.sh" in _uncommented(step.get("run", ""))
    ]
    assert len(builders) == 1, (
        f"expected exactly one job in application.yml to run build-images.sh, found "
        f"{[j for j, _ in builders]}"
    )
    job_id, job = builders[0]
    env = dict(job.get("env") or {})
    uploads = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact@")]
    assert len(uploads) == 1, f"application.yml's {job_id} job uploads {len(uploads)} artifacts, not one"
    with_ = uploads[0].get("with") or {}
    env.update(uploads[0].get("env") or {})

    def resolve(value: str) -> str:
        return re.sub(r"\$\{\{\s*env\.([A-Z_]+)\s*\}\}", lambda m: str(env.get(m.group(1), m.group(0))), str(value))

    return {
        "job_id": job_id,
        "name": job.get("name", job_id),
        "environment": str(env.get("ENVIRONMENT", "")),
        "artifact": resolve(with_.get("name", "")),
        "path": resolve(with_.get("path", "")),
    }


# ---------------------------------------------------------------------------
# A fake `gh`: runs, jobs and artifacts from a JSON world, with counters so a
# job can be "in progress" for its first N looks.
# ---------------------------------------------------------------------------
FAKE_GH = r'''#!{python}
import json, os, sys, urllib.parse

args = sys.argv[1:]
with open(os.environ["FAKE_WORLD"]) as fh:
    world = json.load(fh)


def record(**event):
    with open(os.environ["FAKE_EVENTS"], "a") as fh:
        fh.write(json.dumps(event) + "\n")


def bump(key):
    path = os.environ["FAKE_COUNTS"]
    counts = {}
    if os.path.exists(path):
        with open(path) as fh:
            counts = json.load(fh)
    counts[key] = counts.get(key, 0) + 1
    with open(path, "w") as fh:
        json.dump(counts, fh)
    return counts[key]


def failing():
    mode = os.environ.get("FAKE_GH_FAIL", "")
    if not mode:
        return False
    return mode == "always" or bump("fail") <= int(mode)


def flag(name):
    for i, arg in enumerate(args):
        if arg == name:
            return args[i + 1]
    return None


def run_by_id(run_id):
    return next(r for r in world["runs"] if r["id"] == run_id)


def job_url(run, index):
    return f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{run['id']}/job/{run['id'] * 10 + index}"


if args[:1] == ["api"]:
    path = next(a for a in args[1:] if a.startswith("repos/"))
    parsed = urllib.parse.urlparse(path)
    query = dict(urllib.parse.parse_qsl(parsed.query))
    parts = parsed.path.split("/")
    if failing():
        record(event="api-failed", path=parsed.path)
        print(f"HTTP 502: Bad Gateway (https://api.github.com/{parsed.path})", file=sys.stderr)
        sys.exit(1)
    record(event="api", path=parsed.path, query=query)
    if parts[3:5] == ["actions", "workflows"] and parts[6] == "runs":
        n = bump("runs")
        out = []
        for run in world["runs"]:
            if run.get("workflow", "application.yml") != parts[5] or run["head_sha"] != query.get("head_sha"):
                continue
            if n <= run.get("appears_after", 0):
                continue
            status, conclusion = run.get("status", "completed"), run.get("conclusion", "success")
            if n <= run.get("pending_polls", 0):
                status, conclusion = run.get("pending_status", "in_progress"), None
            out.append({
                "id": run["id"], "head_sha": run["head_sha"], "head_branch": run.get("head_branch", "main"),
                "event": run.get("event", "push"), "status": status, "conclusion": conclusion,
                "html_url": f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{run['id']}",
            })
        print(json.dumps({"total_count": len(out), "workflow_runs": out}))
        sys.exit(0)
    if parts[3:5] == ["actions", "runs"] and parts[6] == "jobs":
        run = run_by_id(int(parts[5]))
        n = bump(f"jobs-{run['id']}")
        out = []
        for i, job in enumerate(run.get("jobs", [])):
            if n <= job.get("appears_after", 0):
                continue
            status, conclusion = job.get("status", "completed"), job.get("conclusion", "success")
            if n <= job.get("pending_polls", 0):
                status, conclusion = "in_progress", None
            out.append({"name": job["name"], "status": status, "conclusion": conclusion, "html_url": job_url(run, i)})
        record(event="jobs", run=run["id"], states=[(j["status"], j["conclusion"]) for j in out])
        print(json.dumps({"total_count": len(out), "jobs": out}))
        sys.exit(0)
    if parts[3:5] == ["actions", "runs"] and parts[6] == "artifacts":
        run = run_by_id(int(parts[5]))
        out = [
            {"name": a["name"], "expired": a.get("expired", False)}
            for a in run.get("artifacts", [])
            if "name" not in query or a["name"] == query["name"]
        ]
        print(json.dumps({"total_count": len(out), "artifacts": out}))
        sys.exit(0)
    print("fake gh: unhandled api " + path, file=sys.stderr)
    sys.exit(2)

if args[:2] == ["run", "download"]:
    run = run_by_id(int(args[2]))
    name, target = flag("--name"), flag("--dir")
    if failing():
        record(event="download-failed", run=run["id"], name=name)
        print("HTTP 502: Bad Gateway", file=sys.stderr)
        sys.exit(1)
    record(event="download", run=run["id"], name=name)
    artifact = next(a for a in run["artifacts"] if a["name"] == name)
    os.makedirs(target, exist_ok=True)
    for fname, content in artifact["files"].items():
        with open(os.path.join(target, fname), "w") as fh:
            json.dump(content, fh)
    sys.exit(0)

print("fake gh: unhandled " + " ".join(args), file=sys.stderr)
sys.exit(2)
'''

# Enough gcloud for build-images.sh to build offline: every submit is recorded
# and succeeds at once, and each tag reads back as one digest.
FAKE_GCLOUD = r'''#!{python}
import hashlib, json, os, re, sys

args = sys.argv[1:]


def record(**event):
    with open(os.environ["FAKE_EVENTS"], "a") as fh:
        fh.write(json.dumps(event) + "\n")


if args[:3] == ["artifacts", "repositories", "describe"]:
    print("projects/p/locations/r/repositories/swarm-images")
    sys.exit(0)
if args[:2] == ["builds", "submit"]:
    config = args[args.index("--config") + 1]
    match = re.search(r"cloudbuild-(.+)\.yaml$", config) or re.search(r"/images/([^/]+)/cloudbuild\.yaml$", config)
    record(event="builds-submit", target=match.group(1))
    sys.exit(0)
if args[:4] == ["artifacts", "docker", "tags", "list"]:
    image = args[4]
    wanted = next(a for a in args if a.startswith("--filter=")).split(":", 1)[1]
    base = f"projects/p/locations/r/repositories/swarm-images/packages/{image.rsplit('/', 1)[-1]}"
    digest = "sha256:" + hashlib.sha256(f"{image}@{wanted}".encode()).hexdigest()
    print(json.dumps([{"image": image, "tag": f"{base}/tags/{wanted}", "version": f"{base}/versions/{digest}"}]))
    sys.exit(0)
record(event="gcloud-unhandled", args=args)
print("fake gcloud: unhandled " + " ".join(args), file=sys.stderr)
sys.exit(2)
'''


def _digest(image: str, sha: str) -> str:
    return "sha256:" + (re.sub(r"[^0-9a-f]", "", (image + sha).encode().hex()) * 2)[:64]


def record_for(sha: str = SHA, environment: str = "dev") -> dict:
    """What build-images.sh writes to build/images-<env>.json."""
    return {
        "tag": sha[:12],
        "commit": sha,
        "built_at": "2026-09-24T16:41:51Z",
        "environment": environment,
        "images": [
            {
                "name": n,
                "image": f"{REGISTRY}/{n}",
                "tag": sha[:12],
                "digest": _digest(n, sha),
                "ref": f"{REGISTRY}/{n}@{_digest(n, sha)}",
            }
            for n in IMAGES
        ],
    }


def ci_run(
    run_id: int,
    *,
    sha: str = SHA,
    job_conclusion: str = "success",
    job_pending: int = 0,
    job_appears_after: int = 0,
    run_pending: int = 0,
    record: dict | None = None,
    environment: str | None = None,
    expired: bool = False,
    **fields,
) -> dict:
    """application.yml's run of `sha`, shaped from application.yml itself."""
    build = application_build()
    env = environment or build["environment"]
    artifacts = []
    if job_conclusion == "success":
        artifacts.append({
            "name": build["artifact"].replace(build["environment"], env) if environment else build["artifact"],
            "expired": expired,
            "files": {Path(build["path"]).name.replace(build["environment"], env): record or record_for(sha, env)},
        })
    return {
        "id": run_id,
        "head_sha": sha,
        "status": "completed",
        "conclusion": "success" if job_conclusion == "success" else "failure",
        "pending_polls": run_pending,
        "jobs": [
            {"name": "shellcheck"},
            {"name": "format / unit tests"},
            {
                "name": build["name"],
                "conclusion": job_conclusion,
                "pending_polls": job_pending,
                "appears_after": job_appears_after,
            },
        ],
        "artifacts": artifacts,
        **fields,
    }


def _fakes(tmp_path: Path, world: dict) -> tuple[Path, dict]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, body in (("gh", FAKE_GH), ("gcloud", FAKE_GCLOUD)):
        path = bindir / name
        path.write_text(body.replace("{python}", sys.executable))
        path.chmod(0o755)
    (tmp_path / "world.json").write_text(json.dumps(world))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "SWARM_ENV_FILE": str(tmp_path / "no-such.env"),
        "PROJECT_ID": "swarm-test-project",
        "REGION": "us-central1",
        "ENVIRONMENT": "dev",
        "NO_COLOR": "1",
        "GITHUB_REPOSITORY": OWNER_REPO,
        "FAKE_WORLD": str(tmp_path / "world.json"),
        "FAKE_EVENTS": str(tmp_path / "events.jsonl"),
        "FAKE_COUNTS": str(tmp_path / "counts.json"),
        "CI_BUILD_POLL": "0.05",
        "CI_BUILD_WAIT": "30",
        "CI_BUILD_APPEAR": "1",
        "CI_BUILD_API_TRIES": "3",
        "ALLOW_DIRTY_BUILD": "1",
        "BUILD_PARALLELISM": "8",
        "BUILD_POLL_INTERVAL": "0.05",
        "BUILD_SUBMIT_STAGGER": "0",
    }
    # This suite runs INSIDE GitHub Actions in CI; nothing here may annotate or
    # write to the real job's summary, or reach the real API with its token.
    for leak in ("GITHUB_ACTIONS", "GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT", "GH_TOKEN", "GITHUB_TOKEN"):
        env.pop(leak, None)
    return bindir, env


def _events(tmp_path: Path) -> list[dict]:
    path = tmp_path / "events.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines() if l] if path.exists() else []


def _last_line(proc: subprocess.CompletedProcess) -> str:
    lines = [l for l in proc.stderr.splitlines() if l.strip()]
    assert lines, "the script said nothing at all"
    return lines[-1]


def lookup(tmp_path: Path, world: dict, *, environment: str = "dev", if_absent: str = "fail", **env_extra: str):
    root = tmp_path / "repo"
    if not root.exists():
        shutil.copytree(REPO / "scripts", root / "scripts")
    _, env = _fakes(tmp_path, world)
    env.update(env_extra)
    out = root / "build" / f"images-{environment}.json"
    proc = subprocess.run(
        [
            "bash", str(root / "scripts" / "lib" / "ci-built-images.sh"),
            "--sha", SHA, "--environment", environment, "--out", str(out), "--if-absent", if_absent,
        ],
        env=env, capture_output=True, text=True, timeout=120, check=False,
    )
    return proc, out, _events(tmp_path)


# ---------------------------------------------------------------------------
# The lookup itself.
# ---------------------------------------------------------------------------
def test_a_build_still_running_is_waited_for_and_then_reused(tmp_path):
    world = {"runs": [ci_run(101, job_pending=3)]}
    proc, out, events = lookup(tmp_path, world)
    assert proc.returncode == 0, proc.stderr[-3000:]

    looks = [e for e in events if e["event"] == "jobs" and e["run"] == 101]
    assert len(looks) >= 4, (
        f"the build job was looked at {len(looks)} time(s); it was in progress for the first 3, "
        "so the script did not wait for it"
    )
    downloads = [i for i, e in enumerate(events) if e["event"] == "download"]
    last_running = max(
        i for i, e in enumerate(events)
        if e["event"] == "jobs" and any(s == "in_progress" for s, _ in e["states"])
    )
    assert downloads and min(downloads) > last_running, (
        "the record was downloaded while the build was still running"
    )
    assert json.loads(out.read_text()) == record_for(), "--out is not the record CI made"


def test_a_run_still_queued_with_no_build_job_yet_is_not_never_built(tmp_path):
    """On main the application run queues behind the previous commit's, and a
    job with `needs:` is not listed until it starts. Neither is "never built":
    reading them that way would fail every push release made in a busy hour."""
    # Queued for three looks; the build job is not listed for the first two.
    world = {"runs": [ci_run(102, run_pending=3, pending_status="queued", job_appears_after=2)]}
    proc, out, _ = lookup(tmp_path, world, if_absent="fail")
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert json.loads(out.read_text())["commit"] == SHA


@pytest.mark.parametrize("if_absent", ["fail", "build"])
def test_a_failed_build_is_reported_and_never_rebuilt(tmp_path, if_absent):
    world = {"runs": [ci_run(103, job_conclusion="failure")]}
    proc, out, events = lookup(tmp_path, world, if_absent=if_absent)
    assert proc.returncode == 1, (
        f"exit {proc.returncode}: a FAILED build is not 'never built' -- rebuilding it would "
        "repeat the failure behind a second Cloud Build"
    )
    assert not out.exists()
    assert not [e for e in events if e["event"] == "download"]
    summary = _last_line(proc)
    assert "failed" in summary and "failure" in summary, summary
    assert f"/actions/runs/103/job/" in summary, f"the last line does not link the failed job: {summary!r}"


NEVER_BUILT = {
    # No application.yml run for the commit at all (CI_BUILD_APPEAR is 1s here).
    "no-run": lambda: {"runs": []},
    # A newer push replaced the run while it was queued.
    "cancelled": lambda: {"runs": [ci_run(104, job_conclusion="cancelled")]},
    # The checks the build needs failed first.
    "skipped": lambda: {"runs": [ci_run(105, job_conclusion="skipped")]},
    # Built -- for dev. A prod release cannot use a dev swarm-ui.
    "other-environment": lambda: {"runs": [ci_run(106)]},
    # A pull request's run of the same commit: never the branch being released.
    "pull-request-run": lambda: {"runs": [ci_run(107, event="pull_request", head_branch="lane/x")]},
}


@pytest.mark.parametrize("case", sorted(NEVER_BUILT))
def test_never_built_fails_a_push_release_and_hands_a_dispatched_one_the_build(tmp_path, case):
    environment = "prod" if case == "other-environment" else "dev"

    proc, out, events = lookup(tmp_path / "push", NEVER_BUILT[case](), environment=environment, if_absent="fail")
    assert proc.returncode == 1, proc.stderr[-3000:]
    assert not out.exists()
    summary = _last_line(proc)
    assert "never built" in summary and "dispatch" in summary, (
        f"a push release that cannot reuse must say why and how to release the commit anyway: {summary!r}"
    )
    assert not [e for e in events if e["event"] == "download"], "it downloaded a record it then did not use"

    proc, out, _ = lookup(tmp_path / "dispatch", NEVER_BUILT[case](), environment=environment, if_absent="build")
    assert proc.returncode == 3, (
        f"exit {proc.returncode} under --if-absent build: the caller builds on exit 3, and only on 3\n"
        + proc.stderr[-2000:]
    )
    assert not out.exists()


def test_a_record_that_names_another_commit_is_refused_even_when_building_is_allowed(tmp_path):
    world = {"runs": [ci_run(108, record=record_for(sha=OTHER_SHA))]}
    proc, out, _ = lookup(tmp_path, world, if_absent="build")
    assert proc.returncode == 1, proc.stderr[-3000:]
    assert not out.exists(), "a record of another commit was handed to the promotion"
    assert OTHER_SHA in _last_line(proc) and SHA in _last_line(proc), _last_line(proc)


def test_a_record_built_for_another_environment_is_refused(tmp_path):
    # The artifact is named for dev, and what is inside it says prod.
    world = {"runs": [ci_run(109, record=record_for(environment="prod"))]}
    proc, out, _ = lookup(tmp_path, world, if_absent="build")
    assert proc.returncode == 1, proc.stderr[-3000:]
    assert not out.exists()
    assert "prod" in _last_line(proc), _last_line(proc)


def test_an_unreadable_api_is_never_read_as_never_built(tmp_path):
    proc, out, events = lookup(tmp_path, {"runs": [ci_run(110)]}, if_absent="build", FAKE_GH_FAIL="always")
    assert proc.returncode == 1, (
        f"exit {proc.returncode}: the API could not be read, and a dispatched release would now "
        "build a commit CI may well have built"
    )
    assert not out.exists()
    assert len([e for e in events if e["event"] == "api-failed"]) == 3, "not retried CI_BUILD_API_TRIES times"
    assert "could not read" in _last_line(proc), _last_line(proc)


def test_a_transient_api_failure_is_retried(tmp_path):
    proc, out, _ = lookup(tmp_path, {"runs": [ci_run(111)]}, FAKE_GH_FAIL="2")
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert json.loads(out.read_text()) == record_for()


def test_a_build_that_never_finishes_times_out_naming_the_job(tmp_path):
    world = {"runs": [ci_run(112, job_pending=10_000)]}
    proc, out, _ = lookup(tmp_path, world, if_absent="build", CI_BUILD_WAIT="2")
    assert proc.returncode == 1, proc.stderr[-3000:]
    assert not out.exists()
    summary = _last_line(proc)
    assert "timed out" in summary and "/actions/runs/112/job/" in summary, summary


def test_the_script_looks_for_what_application_yml_actually_builds_and_uploads():
    """The job name and the artifact are an interface between two workflows.
    Stated once in each, so compared here: application.yml's build job must
    answer to the name the script looks for, build for the environment its
    artifact is named after, and upload the file build-images.sh writes."""
    build = application_build()
    script = (REPO / "scripts" / "lib" / "ci-built-images.sh").read_text()
    default_job = re.search(r'^JOB="\$\{CI_BUILD_JOB:-([^}]*)\}"', script, re.M)
    assert default_job, "ci-built-images.sh no longer declares its default job name as JOB=\"${CI_BUILD_JOB:-...}\""
    assert build["name"] == default_job.group(1), (
        f"application.yml's build job is named {build['name']!r}; ci-built-images.sh looks for "
        f"{default_job.group(1)!r}, so every release would find it 'never built'"
    )
    assert build["environment"] == "dev", f"application.yml builds for {build['environment']!r}"
    assert build["artifact"] == f"images-{build['environment']}", build
    assert build["path"] == f"build/images-{build['environment']}.json", build


# ---------------------------------------------------------------------------
# build-images.sh --reuse-ci: the front door the release calls.
# ---------------------------------------------------------------------------
def _git_sandbox(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    shutil.copytree(REPO / "scripts", root / "scripts")
    shutil.copytree(REPO / "images", root / "images")
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
           "-c", "commit.gpgsign=false"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "sandbox"], check=True)
    head = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    return root, head


def build(tmp_path: Path, mode: str, world_for) -> tuple[subprocess.CompletedProcess, Path, list[dict], str]:
    root, head = _git_sandbox(tmp_path)
    _, env = _fakes(tmp_path, world_for(head))
    proc = subprocess.run(
        ["bash", str(root / "scripts" / "build-images.sh"), "--reuse-ci", mode],
        env=env, capture_output=True, text=True, timeout=180, check=False,
    )
    return proc, root / "build" / "images-dev.json", _events(tmp_path), head


def test_a_release_that_reuses_ci_submits_no_cloud_build(tmp_path):
    proc, manifest, events, head = build(
        tmp_path, "only", lambda head: {"runs": [ci_run(201, sha=head)]}
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    submitted = [e["target"] for e in events if e["event"] == "builds-submit"]
    assert not submitted, f"the release submitted {submitted} although CI had built this commit"
    assert json.loads(manifest.read_text()) == record_for(sha=head), "the manifest is not CI's record"


def test_a_push_release_whose_commit_ci_never_built_builds_nothing_and_says_so(tmp_path):
    proc, manifest, events, _ = build(tmp_path, "only", lambda head: {"runs": []})
    assert proc.returncode != 0
    assert not [e for e in events if e["event"] == "builds-submit"], "a push release built images"
    assert not manifest.exists()
    assert "dispatch" in _last_line(proc), _last_line(proc)


def test_a_dispatched_release_builds_a_commit_ci_never_built_exactly_as_ci_would(tmp_path):
    proc, manifest, events, head = build(tmp_path, "or-build", lambda head: {"runs": []})
    assert proc.returncode == 0, proc.stderr[-3000:]
    submitted = sorted(e["target"] for e in events if e["event"] == "builds-submit")
    assert submitted == sorted(IMAGES), f"built {submitted}, not every image"
    recorded = json.loads(manifest.read_text())
    assert recorded["commit"] == head, (
        f"the manifest records commit {recorded.get('commit')!r}, not {head}: a later reuse "
        "could not tell which commit it describes"
    )
    assert recorded["tag"] == head[:12], f"tagged {recorded['tag']!r}, not the commit's own tag"
    assert recorded["environment"] == "dev"


def test_a_dispatched_release_does_not_rebuild_a_build_ci_failed(tmp_path):
    proc, manifest, events, _ = build(
        tmp_path, "or-build", lambda head: {"runs": [ci_run(202, sha=head, job_conclusion="failure")]}
    )
    assert proc.returncode != 0
    assert not [e for e in events if e["event"] == "builds-submit"]
    assert not manifest.exists()
    assert "failed" in _last_line(proc), _last_line(proc)
