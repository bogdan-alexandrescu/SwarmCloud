"""swarm_check flags a deployment that is behind its repository's default branch.

On 2026-11-09 the console and the API ran the previous night's build for
about ten hours: every release failed (two download timeouts, then one
machine creation failure that failed the whole release), and nothing said so
until someone asked why a feature was not live. Rule `deploy_behind` reads
the deployed commit from `GET /v1/version` (`git_sha`), compares it with the
default branch through GitHub's public API, and is STUCK once the branch has
held a commit the deployment lacks for more than 60 minutes. Its evidence
names the deployed commit and its age, how many commits behind it is, and
the newest release run's conclusion with its failed job; `next` is that run.
A read that fails is "not read" with its reason, never "current".

No credentials, no network: the API and GitHub are fakes.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from swarm_mcp import check
from swarm_mcp.client import SwarmError

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
KEY = "github.com/example-org/example-repo"
RUN_URL = "https://github.com/example-org/example-repo/actions/runs/7001"
FAILED_JOB = "deploy (dev)"


def _sha() -> str:
    return secrets.token_hex(20)


def _iso(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


def _commit(sha: str, at: datetime) -> dict[str, Any]:
    return {"sha": sha, "commit": {"committer": {"date": _iso(at)}}}


@pytest.fixture(autouse=True)
def _no_deployment_reads():
    """Overrides the suite's stub: these tests are the rule's own."""


class _Api:
    def __init__(self, served: Any) -> None:
        self.served = served
        self.asked: list[str] = []

    def request(self, method: str, path: str, **_: Any) -> Any:
        assert method == "GET"
        self.asked.append(path)
        if isinstance(self.served, Exception):
            raise self.served
        return self.served


class _GitHub:
    """GitHub as `check.DeployGitHub` reads it."""

    def __init__(self, deployed: str, deployed_at: datetime, missing: list[tuple[str, datetime]],
                 runs: list[dict[str, Any]] | Exception, jobs: list[str] | None = None) -> None:
        self.deployed, self.deployed_at, self.missing = deployed, deployed_at, missing
        self.runs, self.jobs = runs, jobs or []
        self.actions = "https://github.com/example-org/example-repo/actions/workflows/release.yml"
        self.compared: list[tuple[str, str]] = []

    def __call__(self, key: str) -> _GitHub:
        assert key == KEY
        return self

    def default_branch(self) -> str:
        return "main"

    def compare(self, base: str, branch: str) -> dict[str, Any]:
        self.compared.append((base, branch))
        return {"ahead_by": len(self.missing), "base_commit": _commit(self.deployed, self.deployed_at),
                "commits": [_commit(sha, at) for sha, at in self.missing]}

    def release_runs(self, branch: str) -> list[dict[str, Any]]:
        assert branch == "main"
        if isinstance(self.runs, Exception):
            raise self.runs
        return self.runs

    def failed_jobs(self, run_id: int) -> list[str]:
        assert run_id == 7001
        return self.jobs


def _failed_run(head: str) -> dict[str, Any]:
    return {"id": 7001, "html_url": RUN_URL, "head_sha": head, "status": "completed",
            "conclusion": "failure", "created_at": _iso(NOW - timedelta(minutes=30))}


def test_a_deployment_behind_main_past_the_window_is_stuck_with_its_release_failure():
    deployed = _sha()
    first, head = _sha(), _sha()
    missing = [(first, NOW - timedelta(hours=10)), (_sha(), NOW - timedelta(hours=4)),
               (head, NOW - timedelta(minutes=40))]
    github = _GitHub(deployed, NOW - timedelta(hours=11), missing, [_failed_run(head)], [FAILED_JOB])

    got = check.deployment(_Api({"git_sha": deployed}), KEY, NOW, github)

    flag = got["flag"]
    assert flag is not None and got["unchecked"] is None
    assert (flag["kind"], flag["rule"]) == (check.STUCK, "deploy_behind")
    evidence = flag["evidence"]
    assert evidence["deployed"]["commit"] == deployed
    assert evidence["deployed"]["age_minutes"] == 11 * 60
    assert evidence["behind_main"]["commits"] == 3
    assert evidence["behind_main"]["head"] == head
    assert evidence["behind_main"]["oldest_missing"] == first
    assert evidence["behind_main"]["behind_minutes"] == 10 * 60
    assert evidence["release"]["conclusion"] == "failure"
    assert evidence["release"]["failed_jobs"] == [FAILED_JOB]
    assert flag["next"] == RUN_URL
    assert FAILED_JOB in flag["reason"] and "3 commits behind main" in flag["reason"]
    assert got["deployment"]["state"] == "behind"


def test_the_twelve_character_sha_the_build_stamps_is_compared_and_flagged():
    """scripts/lib/common.sh `git_sha` stamps `git rev-parse --short=12`: the
    shape every real deployment serves. GitHub's compare resolves it, and its
    `base_commit` names the deployed commit in full."""
    deployed = _sha()
    github = _GitHub(deployed, NOW - timedelta(hours=11), [(_sha(), NOW - timedelta(hours=10))],
                     [_failed_run(_sha())], [FAILED_JOB])

    got = check.deployment(_Api({"git_sha": deployed[:12]}), KEY, NOW, github)

    assert got["unchecked"] is None
    assert got["flag"] is not None and got["flag"]["kind"] == check.STUCK
    assert github.compared == [(deployed[:12], "main")]
    assert got["deployment"]["deployed"]["commit"] == deployed
    assert got["deployment"]["state"] == "behind"


def test_a_deployment_behind_main_inside_the_window_is_not_flagged():
    deployed = _sha()
    head = _sha()
    github = _GitHub(deployed, NOW - timedelta(hours=6), [(head, NOW - timedelta(minutes=59))],
                     [{**_failed_run(head), "status": "in_progress", "conclusion": None}])

    got = check.deployment(_Api({"git_sha": deployed}), KEY, NOW, github)

    assert got["flag"] is None and got["unchecked"] is None
    assert got["deployment"]["state"] == "behind_within_window"
    assert got["deployment"]["behind_main"]["commits"] == 1


def test_a_reused_image_whose_release_succeeded_is_not_behind():
    """`git_sha` names the commit that BUILT swarm-api's image; a release that
    reused it and succeeded deploys a newer commit, which is not behind."""
    built = _sha()
    released = _sha()
    github = _GitHub(built, NOW - timedelta(hours=8), [(_sha(), NOW - timedelta(hours=5)),
                                                       (released, NOW - timedelta(hours=2))],
                     [{**_failed_run(released), "conclusion": "success"}])

    got = check.deployment(_Api({"git_sha": built}), KEY, NOW, github)

    assert got["flag"] is None
    assert got["deployment"]["state"] == "current"
    assert got["deployment"]["release_reaches"]["commit"] == released


@pytest.mark.parametrize("served", [
    SwarmError("HTTP 503 from the API"),
    {"git_sha": None, "build_time": None},
    {"git_sha": "not-a-commit", "build_time": None},
])
def test_an_unreadable_deployed_commit_is_not_read_never_current(served):
    github = _GitHub(_sha(), NOW, [], [])

    got = check.deployment(_Api(served), KEY, NOW, github)

    assert got["flag"] is None
    assert got["deployment"]["state"] == "not read"
    assert got["unchecked"]["rule"] == "deploy_behind"
    assert got["unchecked"]["why"].startswith("not read: the deployed commit")
    assert github.compared == []


def test_an_unreadable_release_is_not_read_and_the_lag_is_still_stuck():
    deployed = _sha()
    github = _GitHub(deployed, NOW - timedelta(hours=11), [(_sha(), NOW - timedelta(hours=10))],
                     SwarmError("GitHub answered HTTP 403 for the runs"))

    got = check.deployment(_Api({"git_sha": deployed}), KEY, NOW, github)

    flag = got["flag"]
    assert flag is not None and flag["kind"] == check.STUCK
    assert flag["evidence"]["release"] == "not read: GitHub answered HTTP 403 for the runs"
    assert "not read" in flag["reason"]
    assert flag["next"] == github.actions


def test_the_check_reports_the_deployment_and_counts_its_flag(monkeypatch):
    """`run` reads the rule once and counts its flag as stuck."""
    deployed = _sha()
    github = _GitHub(deployed, NOW - timedelta(hours=11), [(_sha(), NOW - timedelta(hours=10))],
                     [_failed_run(deployed)], [FAILED_JOB])
    real = check.deployment
    monkeypatch.setattr(check, "deployment", lambda client, key, now: real(client, key, now, github))
    monkeypatch.setattr(check, "_check_tenant", lambda *a, **k: {
        "tenant_id": "eng", "flagged": [], "awaiting_input": [], "owner_questions": [],
        "running": {"workflows": [], "single_tasks": []},
        "out_of_scope": {"workflows": 0, "single_tasks": 0},
        "other_sessions": {g: {"other_session": 0, "untagged": 0} for g in ("workflows", "single_tasks")},
        "unchecked": []})

    out = check.run(_Api({"git_sha": deployed}), repository=f"https://{KEY}.git", now=NOW,
                    names=lambda _c: {})

    assert out["count"]["stuck"] == 1
    assert out["flagged"][0]["rule"] == "deploy_behind"
    assert out["deployment"]["state"] == "behind"


def test_the_github_reads_parse_the_rest_shapes_and_a_404_is_another_repository(monkeypatch):
    deployed, head = _sha(), _sha()
    base = f"{check.GITHUB_API}/repos/example-org/example-repo"
    answers: dict[str, Any] = {
        base: {"default_branch": "main"},
        f"{base}/compare/{deployed}...main": {
            "ahead_by": 1, "base_commit": _commit(deployed, NOW - timedelta(hours=3)),
            "commits": [_commit(head, NOW - timedelta(hours=2))]},
        f"{base}/actions/workflows/release.yml/runs?branch=main&per_page={check.RELEASE_RUNS_READ}":
            {"workflow_runs": [_failed_run(head)]},
        f"{base}/actions/runs/7001/jobs?filter=latest&per_page={check.RELEASE_JOBS_PAGE}":
            {"jobs": [{"name": "build", "conclusion": "success"},
                      {"name": FAILED_JOB, "conclusion": "failure"}]},
    }
    asked: list[str] = []

    def get(url: str) -> Any:
        asked.append(url)
        if url not in answers:
            raise check.GitHubNotFound(f"GitHub answered HTTP 404 for {url}")
        return answers[url]

    monkeypatch.setattr(check, "_github_get", get)

    got = check.deployment(_Api({"git_sha": deployed}), KEY, NOW)

    assert got["flag"]["evidence"]["release"]["failed_jobs"] == [FAILED_JOB]
    assert got["flag"]["evidence"]["behind_main"]["behind_minutes"] == 120

    elsewhere = check.deployment(_Api({"git_sha": _sha()}), KEY, NOW)

    assert elsewhere["flag"] is None and elsewhere["unchecked"] is None
    assert elsewhere["deployment"]["state"] == "other_repository"
