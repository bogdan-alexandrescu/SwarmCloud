"""A publish that ADOPTS an open pull request keeps its body and title (#807).

WHAT WENT WRONG. A CI-fixer continuation pushed its fix to the red pull
request's branch and the worker's publish then PATCHed that pull request with
the fixer's own body -- its generated provenance block, because the fixer
wrote only `pr-title.txt`. On #775 that removed `Closes #138 / #98 / #117`, so
GitHub closed nothing when it merged.

WHAT IS PINNED. Opening a NEW pull request is unchanged: the task's body (its
`pr-body.md`, then the platform's block). Adopting an open one:

* keeps the existing body verbatim and APPENDS one marked section --
  `## Continuation (attempt N): <task>` for a continuation, `## Republished
  (attempt N): <task>` for a task's own later attempt -- holding the task's
  `pr-body.md` when it wrote one and a short provenance block either way;
* appends that section once: the same heading already on the body changes
  nothing;
* keeps the existing title, unless the task wrote `pr-title.txt` AND the
  existing title is the worker's own default (the `issue` input's title, or
  the retired `[swarm] task_...`);
* never sends a body that drops a closing-keyword or `part of #N` line the
  current body has (`forge._update_pull_request`'s guard).

Only HTTP is faked: the clone, the harvest and the push are the production
code against a real bare repository, and `forge.open_pull_request` runs for
real against a fake GitHub that holds the pull request's state.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from agent_worker import forge as forge_mod
from agent_worker import lifecycle, workspace as workspace_mod
from agent_worker.forge import PullRequest, RepoAccess, RepoRef

from test_continue_swarm_branch import (  # noqa: F401 - fixtures are used by name
    FIX,
    ROOT,
    local_urls,
    origin,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

#: A task that publishes its own branch, from `main`.
OWN = "task_" + "1" * 20

IMPLEMENTER_BODY = (
    "Closes #138\n"
    "Closes #98\n"
    "Fixes acme/widgets#117\n"
    "part of #454\n"
    "\n"
    "The widget refused nothing.\n"
    "\n"
    "---\n"
    "\n"
    "Opened by SwarmCloud. This branch is written only by this task.\n"
    "\n"
    f"- task: `{ROOT}`\n"
)
IMPLEMENTER_TITLE = "The widget refuses a negative size"
CLOSING = ["Closes #138", "Closes #98", "Fixes acme/widgets#117", "part of #454"]


class FakeGitHub:
    """The pulls API of one repository, with state: a POST for a head that has
    an open pull request is a 422, the list finds it, a PATCH edits it."""

    def __init__(self) -> None:
        self.pulls: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict | None]] = []

    def seed(self, head: str, *, title: str, body: str) -> None:
        self.pulls[head] = {"number": 775, "title": title, "body": body}

    def probe(self, *, url: str, token: str | None) -> RepoAccess:
        return RepoAccess(
            ref=RepoRef(host="github.com", owner="acme", name="widgets"),
            default_branch="main",
            can_push=True,
            reason="the token has write permission on this repository",
        )

    def _json(self, pull: dict) -> dict:
        return {
            "number": pull["number"],
            "html_url": f"https://github.com/acme/widgets/pull/{pull['number']}",
            "state": "open",
            "title": pull["title"],
            "body": pull["body"],
        }

    def request(self, url, *, token, method="GET", payload=None):
        self.calls.append((method, url, payload))
        if method == "POST" and url.endswith("/pulls"):
            head = payload["head"]
            if head in self.pulls:
                return 422, {"errors": [{"message": "A pull request already exists"}]}
            self.pulls[head] = {
                "number": 800 + len(self.pulls), "title": payload["title"], "body": payload["body"]
            }
            return 201, self._json(self.pulls[head])
        if method == "GET" and "/pulls?" in url:
            head = re.search(r"head=acme:([^&]+)", url).group(1)
            return 200, [self._json(self.pulls[head])] if head in self.pulls else []
        if method == "PATCH":
            number = int(url.rsplit("/", 1)[1])
            pull = next(p for p in self.pulls.values() if p["number"] == number)
            pull.update(payload)
            return 200, self._json(pull)
        raise AssertionError(f"unexpected request {method} {url}")

    def patches(self) -> list[dict]:
        return [payload for method, _, payload in self.calls if method == "PATCH"]


@pytest.fixture
def github(monkeypatch) -> FakeGitHub:
    fake = FakeGitHub()
    monkeypatch.setattr(lifecycle, "probe_repository", fake.probe)
    monkeypatch.setattr(forge_mod, "_request", fake.request)
    return fake


def _publish(
    worker_factory, monkeypatch, origin: Path, *, task_id: str, files: dict,
    continues: str | None = None, attempt_count: int = 1, task_input: dict | None = None,
    edit: bool = True,
):
    worker, config, _ = worker_factory(
        task_id=task_id,
        attempt_id=f"att-{task_id}-{attempt_count}",
        lease_id=f"lease-{task_id}",
        repository_url=f"file://{origin}",
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    dispatch: dict = {"strategy": "direct-pr"}
    if continues is not None:
        dispatch["continues"] = continues
    task: dict = {
        "task_id": task_id, "metadata": {"dispatch": dispatch}, "attempt_count": attempt_count,
    }
    if task_input is not None:
        task["input"] = task_input
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-" + "token")
    assert worker._maybe_clone(task) is not None, "the clone did not land"
    if edit:
        (worker.ws.work / lifecycle.REPO_DIR_NAME / "fix.txt").write_text(f"{task_id}\n")
    for name, content in files.items():
        (worker.ws.artifacts / name).write_bytes(content)
    out = worker._harvest_git(publish=True)
    return worker, config, out


def _fix(worker_factory, monkeypatch, origin, github, *, files: dict, **kwargs):
    github.seed(f"swarm/{ROOT}", title=kwargs.pop("existing_title", IMPLEMENTER_TITLE),
                body=IMPLEMENTER_BODY)
    _, config, out = _publish(
        worker_factory, monkeypatch, origin, task_id=FIX, continues=ROOT, attempt_count=2,
        files=files, **kwargs,
    )
    assert out["published"] is True, out
    return config, out, github.pulls[f"swarm/{ROOT}"]


# -- opening a new pull request is unchanged ----------------------------------


def test_the_first_publish_uses_the_tasks_body(worker_factory, monkeypatch, origin, local_urls, github):
    _, config, out = _publish(
        worker_factory, monkeypatch, origin, task_id=OWN,
        files={"pr-title.txt": b"The widget refuses a negative size\n",
               "pr-body.md": b"Closes #12\n\nThe widget accepted -1.\n"},
    )
    pull = github.pulls[f"swarm/{OWN}"]
    assert out["pull_request"]["created"] is True, out
    assert pull["title"] == "The widget refuses a negative size"
    assert pull["body"].startswith("Closes #12\n\nThe widget accepted -1."), pull["body"]
    assert "Opened by SwarmCloud" in pull["body"]
    assert "## Continuation" not in pull["body"] and "## Republished" not in pull["body"]
    assert github.patches() == []


# -- a continuation keeps the body and appends ---------------------------------


def test_a_continuation_with_no_body_keeps_the_body_and_appends_provenance(
    worker_factory, monkeypatch, origin, local_urls, github
):
    """#775 exactly: the fixer wrote only `pr-title.txt`."""
    config, out, pull = _fix(
        worker_factory, monkeypatch, origin, github,
        files={"pr-title.txt": b"Fix the failing lint\n"},
    )
    body = pull["body"]
    assert body.startswith(IMPLEMENTER_BODY.rstrip()), body
    section = body[len(IMPLEMENTER_BODY.rstrip()):]
    assert section.lstrip("\n").startswith(f"## Continuation (attempt 2): `{FIX}`"), section
    assert f"- task: `{FIX}`" in section
    assert f"- attempt: `{config.attempt_id}`" in section
    # Only the short provenance: no second "Opened by" block claiming the branch.
    assert body.count("Opened by SwarmCloud") == 1, body
    for line in CLOSING:
        assert line in body.splitlines(), line
    assert out["pull_request"]["updated"] is True, out


def test_a_continuation_with_a_body_appends_it_under_a_marked_section(
    worker_factory, monkeypatch, origin, local_urls, github
):
    _, _, pull = _fix(
        worker_factory, monkeypatch, origin, github,
        files={"pr-title.txt": b"Fix the failing lint\n",
               "pr-body.md": b"Sorted the imports ruff flagged.\n"},
    )
    body = pull["body"]
    assert body.startswith(IMPLEMENTER_BODY.rstrip()), body
    heading = body.index(f"## Continuation (attempt 2): `{FIX}`")
    assert body.index("Sorted the imports ruff flagged.") > heading, body
    for line in CLOSING:
        assert line in body.splitlines(), line


def test_a_republish_appends_its_section_once(
    worker_factory, monkeypatch, origin, local_urls, github
):
    """The same attempt publishing twice (a retried publish) adds nothing the
    second time: the heading is already on the body."""
    _fix(worker_factory, monkeypatch, origin, github,
         files={"pr-title.txt": b"Fix the failing lint\n"})
    first = github.pulls[f"swarm/{ROOT}"]["body"]
    _publish(worker_factory, monkeypatch, origin, task_id=FIX, continues=ROOT, attempt_count=2,
             files={"pr-title.txt": b"Fix the failing lint\n"}, edit=False)
    assert github.pulls[f"swarm/{ROOT}"]["body"] == first
    assert len(github.patches()) == 1, github.patches()


def test_a_tasks_own_later_attempt_appends_a_republished_section(
    worker_factory, monkeypatch, origin, local_urls, github
):
    """The pull request attempt 1 opened is open on the task's own branch;
    attempt 2 adopts it."""
    opened = "Closes #12\n\n---\n\nOpened by SwarmCloud.\n"
    github.seed(f"swarm/{OWN}", title="Refuse negatives", body=opened)
    _publish(worker_factory, monkeypatch, origin, task_id=OWN, attempt_count=2,
             files={"pr-title.txt": b"Refuse negatives\n"})
    body = github.pulls[f"swarm/{OWN}"]["body"]
    assert body.startswith(opened.rstrip()), body
    assert f"## Republished (attempt 2): `{OWN}`" in body
    assert "Closes #12" in body.splitlines()


# -- the title ------------------------------------------------------------------


def test_the_implementers_title_survives_a_fixers_title(
    worker_factory, monkeypatch, origin, local_urls, github
):
    _, _, pull = _fix(worker_factory, monkeypatch, origin, github,
                      files={"pr-title.txt": b"Fix the failing lint\n"})
    assert pull["title"] == IMPLEMENTER_TITLE
    assert all("title" not in p for p in github.patches()), github.patches()


def test_the_implementers_title_survives_a_fixers_default_title(
    worker_factory, monkeypatch, origin, local_urls, github
):
    """The fixer wrote no title; its `issue` input makes the worker's default
    ("Work on issue #42 (part of #42)"), which must not replace the
    implementer's own."""
    _, _, pull = _fix(worker_factory, monkeypatch, origin, github, files={},
                      task_input={"issue": 42})
    assert pull["title"] == IMPLEMENTER_TITLE


def test_a_written_title_replaces_the_workers_default_title(
    worker_factory, monkeypatch, origin, local_urls, github
):
    _, _, pull = _fix(worker_factory, monkeypatch, origin, github,
                      files={"pr-title.txt": b"Refuse a negative widget size\n"},
                      existing_title="Work on issue #42 (part of #42)")
    assert pull["title"] == "Refuse a negative widget size"
    assert pull["body"].startswith(IMPLEMENTER_BODY.rstrip())


# -- the forge: append, never replace, and the closing-line guard -----------------


def _access() -> RepoAccess:
    return RepoAccess(ref=RepoRef("github.com", "acme", "widgets"), default_branch="main",
                      can_push=True, reason="ok")


def test_the_forge_appends_the_section_to_the_existing_body(monkeypatch):
    fake = FakeGitHub()
    fake.seed("swarm/task_1", title=IMPLEMENTER_TITLE, body=IMPLEMENTER_BODY)
    monkeypatch.setattr(forge_mod, "_request", fake.request)
    pr = forge_mod.open_pull_request(
        access=_access(), token="t", head="swarm/task_1", base="main",
        title="Another title", body="a whole new body", amendment="## Continuation (attempt 1): `x`\n\nmore",
        retitle_if=lambda t: False,
    )
    assert pr.created is False and pr.updated is True and pr.appended is True and pr.retitled is False
    assert fake.patches() == [
        {"body": IMPLEMENTER_BODY.rstrip() + "\n\n## Continuation (attempt 1): `x`\n\nmore\n"}
    ]


def test_without_an_amendment_an_adopted_body_is_never_touched(monkeypatch):
    fake = FakeGitHub()
    fake.seed("swarm/task_1", title=IMPLEMENTER_TITLE, body=IMPLEMENTER_BODY)
    monkeypatch.setattr(forge_mod, "_request", fake.request)
    pr = forge_mod.open_pull_request(
        access=_access(), token="t", head="swarm/task_1", base="main",
        title="Another title", body="a whole new body",
    )
    assert pr.updated is False and fake.patches() == []


@pytest.mark.parametrize(
    "line",
    ["Closes #1", "closes: #1", "- Fixes #22", "Resolved acme/widgets#3", "FIXED #4",
     "This is part of #5", "Part of acme/widgets#6"],
)
def test_closing_lines_are_recognised(line):
    assert forge_mod.closing_lines(f"intro\n{line}\noutro") == [line]


@pytest.mark.parametrize("line", ["Closed the door", "fixes the bug", "part of the plan", "#7 alone"])
def test_ordinary_lines_are_not_closing_lines(line):
    assert forge_mod.closing_lines(line) == []


def test_a_body_that_drops_a_closing_line_is_never_sent(monkeypatch):
    seen: list = []

    def fake(url, *, token, method="GET", payload=None):
        seen.append((method, payload))
        return 200, {}

    monkeypatch.setattr(forge_mod, "_request", fake)
    existing = PullRequest(number=775, url="u", state="open", created=False,
                           title="T", body="Closes #138\r\npart of #454\r\nprose")
    pr = forge_mod._update_pull_request(
        access=_access(), token="t", existing=existing, title="New", body="Closes #138\nprose",
    )
    assert seen == [("PATCH", {"title": "New"})], seen
    assert pr.updated is True and pr.retitled is True and pr.appended is False
    assert pr.body_refused == ("part of #454",), pr

    seen.clear()
    pr = forge_mod._update_pull_request(
        access=_access(), token="t", existing=existing, title=None, body="nothing closing",
    )
    assert seen == [] and pr.updated is False, (seen, pr)
    assert set(pr.body_refused) == {"Closes #138", "part of #454"}


def test_an_amendment_too_long_for_github_is_cut_not_the_existing_body(monkeypatch):
    existing = "Closes #1\n" + "x" * (forge_mod.PR_BODY_MAX_CHARS - 2000)
    section = "## Continuation (attempt 1): `x`\n\n" + "y" * 5000
    body = forge_mod.appended_body(existing, section)
    assert body is not None and len(body) <= forge_mod.PR_BODY_MAX_CHARS
    assert body.startswith(existing)
    assert "## Continuation (attempt 1): `x`" in body
    # Nothing fits: the body is left as it is.
    assert forge_mod.appended_body("x" * forge_mod.PR_BODY_MAX_CHARS, section) is None
