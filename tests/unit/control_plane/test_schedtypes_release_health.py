"""The `release-health` schedule type (docs/schedules.md §3.9, lane S10e).

WHAT IS HELD HERE
-----------------
* THE TYPE IS BUILT: its file exists, it declares its caps, so the catalogue
  serves it available and the tick's real loader imports it.
* AN API TYPE (SD8): each repository is one `api_action`, recorded on the
  firing; no task is submitted and nothing holds capacity.
* ONE ISSUE PER FAILURE STREAK: a red run with no open streak issue opens
  one (title as a fact, failing job and step, a REDACTED log tail, label
  `issue_label`); the same run seen again writes nothing; a NEW failing run
  comments once on the open issue.
* GREEN AGAIN: the first success after a failure comments "green again at
  {sha}" once, and the issue is NEVER closed.
* THE PERMISSION S0 FOUND MISSING: a 403 on the runs read ends the
  repository's action `failed` with `actions_read_missing` and a hint naming
  `actions: read`, never an empty success. A 404 is `workflow_not_found`.
* TENANT AND ROOM: another tenant's registration is never read; a firing
  takes at most its room in repositories.
* DRY RUN (§2.8): the same reads, what each repository would do, and no write.

No cloud and no emulator: the in-memory Firestore, and GitHub as a fake
transport under the real `forgewrite.GitHubWriter`.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from swarm_api import forgewrite, schedulefire, scheduletypes
from swarm_api.schedtypes import release_health

from . import forge_fakes
from .conftest import seed_tenant
from .test_schedtypes_issue_sweep import OWNER, REPO, firing_for, make_context, make_schedule, register, tasks

BASE = f"/repos/{OWNER}/{REPO}"
LOG_URL = "https://pipelines.actions.githubusercontent.com/logs/job"


def sha(n: int) -> str:
    return f"{n:040x}"


def run(run_id: int, conclusion: str, *, branch: str = "main") -> dict[str, Any]:
    return {
        "id": run_id, "run_number": run_id, "head_sha": sha(run_id), "head_branch": branch,
        "conclusion": conclusion, "status": "completed",
        "created_at": f"2026-10-11T{run_id % 24:02d}:00:00Z",
        "html_url": f"https://github.com/{OWNER}/{REPO}/actions/runs/{run_id}",
    }


class ActionsGitHub:
    """api.github.com's Actions and issue routes for one repository, in memory."""

    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []
        self.jobs: dict[int, list[dict[str, Any]]] = {}
        self.issues: dict[int, dict[str, Any]] = {}
        self.comments: dict[int, list[dict[str, Any]]] = {}
        self.logs: dict[int, bytes] = {}
        #: path prefix -> the status every request under it answers instead.
        self.refuse: dict[str, int] = {}
        self.calls: list[tuple[str, str]] = []
        self.headers: list[dict[str, str]] = []
        self._next = 100

    def writes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p in self.calls if m != "GET"]

    def _id(self) -> int:
        self._next += 1
        return self._next

    def send(self, method: str, url: str, headers: dict[str, str], body: bytes | None,
             timeout: float) -> tuple[int, bytes]:
        parsed = urlparse(url)
        path, query = parsed.path, parse_qs(parsed.query)
        self.calls.append((method, path))
        self.headers.append(dict(headers))
        for prefix, status in self.refuse.items():
            if path.startswith(prefix):
                return status, b'{"message": "Resource not accessible by integration"}'
        data = json.loads(body) if body else None
        if path == f"{BASE}/actions/workflows/release.yml/runs":
            assert query["status"] == ["completed"]
            return 200, json.dumps({"workflow_runs": [
                r for r in self.runs if r["head_branch"] == query["branch"][0]
            ]}).encode()
        if path.startswith(f"{BASE}/actions/runs/") and path.endswith("/jobs"):
            return 200, json.dumps({"jobs": self.jobs.get(int(path.split("/")[-2]), [])}).encode()
        if path == f"{BASE}/issues" and method == "GET":
            label = query["labels"][0]
            found = [i for i in self.issues.values()
                     if i["state"] == "open" and label in [x["name"] for x in i["labels"]]]
            return 200, json.dumps(found).encode()
        if path == f"{BASE}/issues" and method == "POST":
            number = self._id()
            self.issues[number] = {"number": number, "state": "open", "title": data["title"],
                                   "body": data["body"], "labels": [{"name": n} for n in data["labels"]]}
            return 201, json.dumps(self.issues[number]).encode()
        if path.startswith(f"{BASE}/issues/") and path.endswith("/comments"):
            number = int(path.split("/")[-2])
            if method == "GET":
                page = int(query.get("page", ["1"])[0])
                return 200, json.dumps(self.comments.get(number, []) if page == 1 else []).encode()
            comment = {"id": self._id(), "body": data["body"], "user": {"login": "swarm-bot"},
                       "html_url": f"https://github.com/{OWNER}/{REPO}/issues/{number}#c"}
            self.comments.setdefault(number, []).append(comment)
            return 201, json.dumps(comment).encode()
        raise AssertionError(f"unexpected {method} {path}")

    def locate(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, str]:
        job_id = int(urlparse(url).path.split("/")[-2])
        return (302, f"{LOG_URL}/{job_id}") if job_id in self.logs else (404, "")

    def fetch(self, url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        assert "Authorization" not in headers
        return 200, self.logs[int(url.rsplit("/", 1)[1])]


@pytest.fixture
def github() -> ActionsGitHub:
    return ActionsGitHub()


@pytest.fixture
def forge_tokens() -> forge_fakes.AnyTenantTokens:
    return forge_fakes.AnyTenantTokens()


@pytest.fixture
def ctx(db, tokens, group_map, objects, github, forge_tokens, monkeypatch):
    monkeypatch.delenv("SCHEDULES_ENABLED", raising=False)
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    context = make_context(db, tokens, group_map, objects, forge_tokens=forge_tokens)
    context.forge_writer = forgewrite.GitHubWriter(send=github.send, locate=github.locate, fetch=github.fetch)
    return context


@pytest.fixture
def repo_id(db) -> str:
    return register(db)


def health(db, *repo_ids: str, **kwargs):
    return make_schedule(db, type_="release-health", repo_ids=repo_ids, **kwargs)


def token_of(forge_tokens) -> str:
    (token,) = forge_tokens.issued.values()
    return token


def red_run(github: ActionsGitHub, run_id: int, *, log: bytes = b"step output\nError: apply failed\n") -> None:
    github.runs.insert(0, run(run_id, "failure"))
    job_id = run_id * 10
    github.jobs[run_id] = [
        {"id": job_id - 1, "name": "build", "conclusion": "success", "steps": []},
        {"id": job_id, "name": "deploy", "conclusion": "failure", "steps": [
            {"name": "Checkout", "conclusion": "success"},
            {"name": "Apply", "conclusion": "failure"},
        ]},
    ]
    github.logs[job_id] = log


def green_run(github: ActionsGitHub, run_id: int) -> None:
    github.runs.insert(0, run(run_id, "success"))


# ---------------------------------------------------------------------------


def test_the_type_is_available_because_its_file_exists_and_declares_its_caps() -> None:
    entry = scheduletypes.get("release-health")
    assert scheduletypes.availability(entry) == (True, "")
    assert schedulefire.load_executor(entry) is release_health
    caps = scheduletypes.budget_caps(entry)
    assert caps is release_health.BUDGET_CAPS and caps.per_run_usd > 0 and caps.max_concurrent >= 1
    assert "release-health" in scheduletypes.available_names()


def test_a_red_run_opens_one_issue_naming_the_job_and_step_with_a_redacted_log(
        db, ctx, github, forge_tokens, repo_id) -> None:
    red_run(github, 7)
    recorded: list = []
    firing = firing_for(ctx, health(db, repo_id), recorded=recorded)
    # The token itself in the log, as a leaked environment line would put it.
    token = forge_tokens.issued.setdefault("swarm-tenant-eng-git", forge_fakes.make_token())
    github.logs[70] = f"setting up\nGH_TOKEN={token}\nError: apply failed @octocat\n".encode()

    work = release_health.create(firing)

    (number,) = github.issues
    issue = github.issues[number]
    assert issue["title"] == f"release.yml on main failed at {sha(7)[:12]} in job deploy, step Apply"
    assert [label["name"] for label in issue["labels"]] == ["bug"]
    body = issue["body"]
    assert body.startswith(release_health.streak_marker("release.yml", "main"))
    assert release_health.run_marker(7) in body
    assert "`deploy`" in body and "`Apply`" in body and "Error: apply failed" in body
    assert f"https://github.com/{OWNER}/{REPO}/actions/runs/7" in body
    assert token not in body and token not in issue["title"]
    assert "never closes this issue" in body
    # An API action, recorded the moment it ended (§2.2); no task, no capacity.
    (item,) = work
    assert recorded == work and tasks(db) == []
    assert item["kind"] == "api_action" and item["outcome"] == "succeeded" and item["repo_id"] == repo_id
    assert item["detail"]["action"] == "open_issue" and item["detail"]["written"] == {"issue_opened": number}
    # The token went to api.github.com in the Authorization header only.
    assert all(token not in path for _m, path in github.calls)


def test_the_same_red_run_is_reported_once_and_a_new_one_comments_once(db, ctx, github, repo_id) -> None:
    red_run(github, 7)
    schedule = health(db, repo_id)
    release_health.create(firing_for(ctx, schedule))
    (number,) = github.issues

    again = release_health.create(firing_for(ctx, schedule, fid="f2"))

    assert again[0]["detail"]["action"] == "nothing" and again[0]["outcome"] == "succeeded"
    assert len(github.issues) == 1 and github.comments == {}

    red_run(github, 8)
    release_health.create(firing_for(ctx, schedule, fid="f3"))
    release_health.create(firing_for(ctx, schedule, fid="f4"))

    assert len(github.issues) == 1
    (comment,) = github.comments[number]
    assert comment["body"].startswith(release_health.run_marker(8)) and "Failed again" in comment["body"]
    assert sha(8) in comment["body"] and "`Apply`" in comment["body"]


def test_the_first_success_after_a_failure_comments_once_and_never_closes(db, ctx, github, repo_id) -> None:
    red_run(github, 7)
    schedule = health(db, repo_id)
    release_health.create(firing_for(ctx, schedule))
    (number,) = github.issues
    green_run(github, 8)
    green_run(github, 9)

    work = release_health.create(firing_for(ctx, schedule, fid="f2"))
    release_health.create(firing_for(ctx, schedule, fid="f3"))

    (comment,) = github.comments[number]
    # The FIRST green run, not the newest.
    assert comment["body"].startswith(release_health.green_marker(8))
    assert f"Green again at `{sha(8)}`" in comment["body"]
    assert work[0]["detail"]["action"] == "green_again"
    assert github.issues[number]["state"] == "open"
    assert not [w for w in github.writes() if w[0] == "PATCH"]


def test_green_with_no_failure_writes_nothing(db, ctx, github, repo_id) -> None:
    green_run(github, 1)
    green_run(github, 2)
    github.runs.insert(0, run(3, "cancelled"))

    (item,) = release_health.create(firing_for(ctx, health(db, repo_id)))

    assert item["outcome"] == "succeeded" and item["detail"]["action"] == "nothing"
    assert item["detail"]["latest_run"]["run_id"] == 2
    assert github.writes() == []


def test_a_cancelled_run_does_not_hide_the_failure_before_it(db, ctx, github, repo_id) -> None:
    red_run(github, 1)
    github.runs.insert(0, run(2, "cancelled"))

    (item,) = release_health.create(firing_for(ctx, health(db, repo_id)))

    assert item["detail"]["action"] == "open_issue" and item["detail"]["run"]["run_id"] == 1


def test_without_actions_read_the_action_fails_and_says_so(db, ctx, github, repo_id) -> None:
    red_run(github, 7)
    github.refuse[f"{BASE}/actions/"] = 403

    (item,) = release_health.create(firing_for(ctx, health(db, repo_id)))

    assert item["outcome"] == "failed"
    detail = item["detail"]
    assert detail["code"] == "actions_read_missing"
    assert "actions: read" in detail["message"] and "Actions: Read" in detail["hint"]
    assert "onboarding GitHub App" in detail["hint"]
    assert github.writes() == [] and github.issues == {}


def test_an_unknown_workflow_is_named_and_the_permission_is_offered_as_a_cause(db, ctx, github, repo_id) -> None:
    github.refuse[f"{BASE}/actions/workflows/"] = 404

    (item,) = release_health.create(firing_for(ctx, health(db, repo_id)))

    assert item["outcome"] == "failed" and item["detail"]["code"] == "workflow_not_found"
    assert "release.yml" in item["detail"]["hint"] and "actions: read" in item["detail"]["hint"]


def test_a_tenant_with_no_forge_credential_fails_each_repository_with_the_code(
        db, ctx, github, forge_tokens, repo_id) -> None:
    forge_tokens.missing.add("swarm-tenant-eng-git")

    (item,) = release_health.create(firing_for(ctx, health(db, repo_id)))

    assert item["outcome"] == "failed" and item["detail"]["code"] == "no_forge_credential"
    assert github.calls == []


def test_another_tenants_registration_is_never_read(db, ctx, github) -> None:
    theirs = register(db, tenant_id="research")

    (item,) = release_health.create(firing_for(ctx, health(db, theirs)))

    assert item["outcome"] == "failed" and item["detail"]["code"] == "repository_not_registered"
    assert github.calls == []


def test_a_firing_reads_at_most_its_room_in_repositories(db, ctx, github, repo_id) -> None:
    other = register(db, repo="gadgets")
    green_run(github, 1)

    work = release_health.create(firing_for(ctx, health(db, repo_id, other), room=1))

    assert [w["repo_id"] for w in work] == [repo_id]


def test_branch_and_label_parameters_are_used(db, ctx, github, repo_id) -> None:
    github.runs.insert(0, run(5, "failure", branch="release"))
    github.jobs[5] = []

    release_health.create(firing_for(ctx, health(db, repo_id, params={"branch": "release",
                                                                       "issue_label": "release-red"})))

    (issue,) = github.issues.values()
    assert [label["name"] for label in issue["labels"]] == ["release-red"]
    assert issue["body"].startswith(release_health.streak_marker("release.yml", "release"))
    assert "job unknown" in issue["title"]
    assert "could not be read" in issue["body"]


def test_dry_run_reads_and_writes_nothing(db, ctx, github, repo_id) -> None:
    red_run(github, 7)

    out = release_health.dry_run(firing_for(ctx, health(db, repo_id)))

    (row,) = out["repositories"]
    assert row["action"] == "open_issue" and row["failure"] == {"job": "deploy", "step": "Apply"}
    assert out["workflow"] == "release.yml" and out["issue_label"] == "bug"
    assert github.writes() == [] and github.issues == {}
    # No job log is fetched in a dry run.
    assert not [p for _m, p in github.calls if p.endswith("/logs")]


def test_a_branch_marker_cannot_close_its_html_comment() -> None:
    assert "-->" not in release_health.streak_marker("release.yml", "a-->b")[:-3]


def test_a_log_cannot_close_the_excerpt_fence() -> None:
    text = release_health.excerpt("~~~~\ninjected\n~~~~~~", "x" * 8)
    fence = text.split("\n")[2][: -len("text")]
    assert set(fence) == {"~"} and len(fence) > 6
