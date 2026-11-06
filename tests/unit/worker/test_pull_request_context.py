"""A step that works on a pull request reads it from work/pull_request.md.

Product Oracle, 2026-11-06 (part of #2688): its review lanes could not run
`gh pr view` -- an agent holds no GitHub token, by design -- so they fell back
to the checked-out head and never read the pull request's body or comments.
The worker now reads the pull request with the tenant's own forge credential,
as it reads an issue (`agent_worker.issue`), and stages it beside the
checkout, scrubbed and capped, before the agent starts; the brief names it.

Run through the production worker over in-memory Firestore and a local object
store, as `test_builds_on_owner_answers.py` is; the forge is the recorded one
`test_issue_input.py` uses, answering by URL path.
"""

from __future__ import annotations

import secrets
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from agent_worker import issue as issue_mod
from agent_worker import pull_request as pull_request_mod
from agent_worker.errors import ExitCode
from swarm_common.states import TaskState

from worker_seeds import TENANT, seed_attempt

REPO = "https://github.com/octo/widgets.git"
WORKFLOW = "wf_review_reads_its_pr"
AUTHOR = "task_author42"
REVIEW = "task_review42"
NUMBER = 42
BASE = "/repos/octo/widgets"


def _token() -> str:
    """A GitHub-token-shaped value, built at runtime, never one literal."""
    return "gh" + "p_" + secrets.token_hex(18)


def _pull(**overrides: Any) -> dict[str, Any]:
    doc = {
        "number": NUMBER,
        "title": "Review lanes read their pull request",
        "state": "open",
        "merged": False,
        "draft": False,
        "user": {"login": "lane-author"},
        "html_url": f"https://github.com/octo/widgets/pull/{NUMBER}",
        "base": {"ref": "main"},
        "head": {"ref": "swarm/task_author42", "sha": "a" * 40},
        "body": "The body says why: reviews read the head only.",
        "labels": [{"name": "ready"}],
        "comments": 1,
        "review_comments": 2,
    }
    doc.update(overrides)
    return doc


def _thread(first_body: str = "This line drops the cap.") -> list[dict[str, Any]]:
    return [
        {"id": 7, "user": {"login": "reviewer"}, "created_at": "2026-11-06T01:00:00Z",
         "path": "apps/agent-worker/agent_worker/issue.py", "line": 128, "body": first_body},
        {"id": 8, "in_reply_to_id": 7, "user": {"login": "lane-author"},
         "created_at": "2026-11-06T02:00:00Z",
         "path": "apps/agent-worker/agent_worker/issue.py", "line": 128,
         "body": "Kept the cap; see the next commit."},
    ]


def _answers(**overrides: Any) -> dict[str, tuple[int, Any]]:
    answers: dict[str, tuple[int, Any]] = {
        f"{BASE}/pulls/{NUMBER}": (200, _pull()),
        f"{BASE}/issues/{NUMBER}/comments?per_page=100&page=1": (200, [
            {"user": {"login": "owner"}, "created_at": "2026-11-06T00:30:00Z",
             "body": "Please keep the scrub."},
        ]),
        f"{BASE}/pulls/{NUMBER}/reviews?per_page=100&page=1": (200, [
            {"user": {"login": "reviewer"}, "submitted_at": "2026-11-06T01:00:00Z",
             "state": "CHANGES_REQUESTED", "body": "One thread to settle."},
        ]),
        f"{BASE}/pulls/{NUMBER}/comments?per_page=100&page=1": (200, _thread()),
    }
    answers.update(overrides)
    return answers


class _Forge:
    """Answers by URL path and query, and records every request."""

    def __init__(self, answers: dict[str, tuple[int, Any]]) -> None:
        self.answers = answers
        self.requests: list[urllib.request.Request] = []

    def __call__(self, req: urllib.request.Request) -> tuple[int, Any, dict[str, str]]:
        self.requests.append(req)
        path = "/" + req.full_url.split("://", 1)[1].split("/", 1)[1]
        status, data = self.answers.get(path, (404, {"message": "Not Found"}))
        return status, data, {}


@pytest.fixture
def forge(monkeypatch):
    def install(answers: dict[str, tuple[int, Any]]) -> _Forge:
        fake = _Forge(answers)
        # The one request path that holds the tenant credential (`issue._open`).
        monkeypatch.setattr(issue_mod, "_open", fake)
        return fake

    return install


# ---------------------------------------------------------------------------
# the module
# ---------------------------------------------------------------------------


def test_the_fetch_reads_the_body_the_conversation_and_the_review_threads(forge):
    token = _token()
    fake = forge(_answers())

    pr = pull_request_mod.fetch_pull_request(repository_url=REPO, number=NUMBER, token=token)
    text = pull_request_mod.render(pr)

    assert text.startswith(f"# Pull request #{NUMBER}: Review lanes read their pull request")
    assert "- Base: main" in text and "- Head: swarm/task_author42" in text
    assert "- State: open" in text and "- Labels: ready" in text
    assert "The body says why: reviews read the head only." in text
    assert "Please keep the scrub." in text
    assert "@reviewer review (changes_requested)" in text
    # The thread, with its file and line, its reply after it.
    assert "### apps/agent-worker/agent_worker/issue.py:128" in text
    assert text.index("This line drops the cap.") < text.index("Kept the cap; see the next commit.")
    # Newest last: the owner's comment (00:30) before the review (01:00).
    assert text.index("Please keep the scrub.") < text.index("One thread to settle.")
    assert "Cut to fit" not in text
    # Every request carried the token in its header, and only to api.github.com.
    assert fake.requests and all(
        r.get_header("Authorization") == f"Bearer {token}" for r in fake.requests
    )
    assert {r.full_url.split("/")[2] for r in fake.requests} == {"api.github.com"}


def test_an_oversize_thread_is_cut_and_the_file_says_so(forge):
    big = "x" * (60 * 1024)
    rows = [
        {"id": 100 + i, "in_reply_to_id": 100 if i else None, "user": {"login": "reviewer"},
         "created_at": f"2026-11-06T01:{i:02d}:00Z", "path": "a.py", "line": 1, "body": big}
        for i in range(30)
    ]
    forge(_answers(**{
        f"{BASE}/pulls/{NUMBER}/comments?per_page=100&page=1": (200, rows),
        f"{BASE}/pulls/{NUMBER}": (200, _pull(review_comments=30)),
    }))

    pr = pull_request_mod.fetch_pull_request(repository_url=REPO, number=NUMBER, token=None)
    text = pull_request_mod.render(pr)

    assert len(text.encode("utf-8")) <= pull_request_mod.MAX_FILE_BYTES
    written = text.count("**@reviewer**")
    assert 0 < written < 30
    assert text.rstrip().endswith("the pull request itself has them all._")
    assert f"and {30 - written} review-thread comment(s) are not here" in text


def test_one_oversize_body_is_cut_where_it_stands_and_says_so(forge):
    forge(_answers(**{f"{BASE}/pulls/{NUMBER}": (200, _pull(body="y" * (200 * 1024)))}))

    text = pull_request_mod.render(
        pull_request_mod.fetch_pull_request(repository_url=REPO, number=NUMBER, token=None)
    )

    assert "more bytes of this text were cut" in text
    # The conversation after it is still there.
    assert "Please keep the scrub." in text


# ---------------------------------------------------------------------------
# through the production worker
# ---------------------------------------------------------------------------


def _seed_author(db: Any, number: int | None = NUMBER) -> None:
    """The step the review builds on, SUCCEEDED, which opened pull request 42."""
    git: dict[str, Any] = {"branch": "swarm/task_author42"}
    if number is not None:
        git["pull_request"] = {"number": number, "url": f"https://github.com/octo/widgets/pull/{number}"}
    db.seed(f"tasks/{AUTHOR}", {
        "id": AUTHOR, "tenant_id": TENANT, "state": TaskState.SUCCEEDED.value,
        "workflow_id": WORKFLOW, "step_id": "author",
        "result_summary": {"git": git},
    })


def _run_review(
    db: Any, worker_factory: Any, monkeypatch: Any, *, builds_on: str | None, token: str
) -> int:
    seed_attempt(db, task_id=REVIEW, attempt_id="att_r", lease_id="lease_r",
                 task_input={"prompt": "review the change", "steps": 1, "sleep_seconds": 0.01})
    doc = db.doc(f"tasks/{REVIEW}")
    doc["workflow_id"] = WORKFLOW
    doc["step_id"] = "reread"
    doc["depends_on"] = [AUTHOR]
    if builds_on is not None:
        doc["metadata"] = {"dispatch": {"builds_on": builds_on}}
    worker, _config, _exporter = worker_factory(
        task_id=REVIEW, attempt_id="att_r", lease_id="lease_r")
    # The tenant's forge token, as `secrets.resolve_git_token` hands it. The
    # step has no clone here, so the read is pointed at the GitHub repository
    # the recorded forge answers for; everything else is the worker's own.
    monkeypatch.setattr(worker, "_git_token", lambda **_: token)
    monkeypatch.setattr(worker, "_git_token_refusal", lambda: None)
    real = pull_request_mod.stage_pull_request
    monkeypatch.setattr(
        pull_request_mod, "stage_pull_request",
        lambda **kwargs: real(**{**kwargs, "repository_url": REPO}),
    )
    return worker.run()


@pytest.fixture
def keep_workspace(monkeypatch):
    """The workspace is destroyed on exit; keep it so the test can read it."""
    from agent_worker import lifecycle

    monkeypatch.setattr(lifecycle.workspace_mod, "destroy", lambda ws: None)


def _staged(tmp_path: Path) -> str | None:
    found = list((tmp_path / "workspace").rglob(pull_request_mod.FILE_NAME))
    return found[0].read_text() if found else None


def test_a_review_step_of_a_workflow_with_a_pull_request_reads_it(
    db, store, worker_factory, monkeypatch, tmp_path, forge, runner_inputs, keep_workspace
):
    token = _token()
    pasted = _token()  # a token-shaped string nobody registered
    answers = _answers()
    answers[f"{BASE}/pulls/{NUMBER}/comments?per_page=100&page=1"] = (
        200, _thread(first_body=f"This line drops the cap. I pasted {pasted} by mistake."))
    fake = forge(answers)
    _seed_author(db)

    code = _run_review(db, worker_factory, monkeypatch, builds_on=AUTHOR, token=token)

    assert code == ExitCode.OK, db.doc(f"tasks/{REVIEW}")
    text = _staged(tmp_path)
    # MUTATION: stage nothing for a builds_on review, and this is None.
    assert text is not None, "the review step was not given pull_request.md"
    assert "The body says why: reviews read the head only." in text
    assert "### apps/agent-worker/agent_worker/issue.py:128" in text
    assert "This line drops the cap. I pasted" in text
    # MUTATION: write the comment unscrubbed, and the pasted token is in the file.
    assert pasted not in text
    assert token not in text
    # Read with the tenant's own token, never one put in the container.
    assert all(r.get_header("Authorization") == f"Bearer {token}" for r in fake.requests)
    # The brief gains one line naming the file, by absolute path.
    prompt = runner_inputs[-1]["prompt"]
    assert prompt.startswith("review the change")
    path = next((tmp_path / "workspace").rglob(pull_request_mod.FILE_NAME))
    assert pull_request_mod.prompt_line(path) in prompt
    assert prompt.count(pull_request_mod.FILE_NAME) == 1


def test_a_step_with_no_pull_request_gets_no_file_and_no_line(
    db, store, worker_factory, monkeypatch, tmp_path, forge, runner_inputs, keep_workspace
):
    fake = forge(_answers())
    _seed_author(db, number=None)

    code = _run_review(db, worker_factory, monkeypatch, builds_on=AUTHOR, token=_token())

    assert code == ExitCode.OK, db.doc(f"tasks/{REVIEW}")
    assert _staged(tmp_path) is None
    assert pull_request_mod.FILE_NAME not in runner_inputs[-1]["prompt"]
    assert not fake.requests


def test_a_step_that_builds_on_nothing_gets_no_file(
    db, store, worker_factory, monkeypatch, tmp_path, forge, runner_inputs, keep_workspace
):
    fake = forge(_answers())
    _seed_author(db)

    code = _run_review(db, worker_factory, monkeypatch, builds_on=None, token=_token())

    assert code == ExitCode.OK, db.doc(f"tasks/{REVIEW}")
    assert _staged(tmp_path) is None
    assert pull_request_mod.FILE_NAME not in runner_inputs[-1]["prompt"]
    assert not fake.requests


def test_a_pull_request_the_forge_refuses_is_named_unread_and_the_agent_still_runs(
    db, store, worker_factory, monkeypatch, tmp_path, forge, runner_inputs, keep_workspace
):
    forge({})  # every path answers 404
    _seed_author(db)

    code = _run_review(db, worker_factory, monkeypatch, builds_on=AUTHOR, token=_token())

    assert code == ExitCode.OK, db.doc(f"tasks/{REVIEW}")
    assert db.doc(f"tasks/{REVIEW}")["state"] == TaskState.SUCCEEDED.value
    text = _staged(tmp_path)
    assert text is not None
    assert text.startswith(f"# Pull request #{NUMBER}: not read")
    assert "no such pull request" in text
    assert pull_request_mod.FILE_NAME in runner_inputs[-1]["prompt"]


def test_a_single_pr_reader_reads_the_pull_request_its_author_opened(db, worker_factory):
    """A `single-pr` reader builds on nothing: its pull request is its signed
    `pr_author` step's."""
    _seed_author(db)
    seed_attempt(db, task_id=REVIEW, attempt_id="att_r", lease_id="lease_r",
                 task_input={"prompt": "review the change"})
    worker, _config, _exporter = worker_factory(
        task_id=REVIEW, attempt_id="att_r", lease_id="lease_r")
    worker._task = {
        "id": REVIEW, "workflow_id": WORKFLOW, "input": {"prompt": "review the change"},
        "metadata": {"dispatch": {"strategy": "single-pr", "pr_role": "reader",
                                  "pr_author": AUTHOR}},
    }

    # MUTATION: read only `builds_on`, and a reader names no pull request.
    assert worker._requested_pull_request() == NUMBER


def test_a_step_with_no_brief_names_no_pull_request(db, worker_factory):
    _seed_author(db)
    seed_attempt(db, task_id=REVIEW, attempt_id="att_r", lease_id="lease_r")
    worker, _config, _exporter = worker_factory(
        task_id=REVIEW, attempt_id="att_r", lease_id="lease_r")
    worker._task = {
        "id": REVIEW, "workflow_id": WORKFLOW, "input": {},
        "metadata": {"dispatch": {"builds_on": AUTHOR}},
    }

    assert worker._requested_pull_request() is None


def test_input_pull_request_is_read_where_the_profile_declares_it():
    """The owner's pick on #3470 (2026-11-06): claude-code, codex and
    claude-code-review declare `pull_request`, so a caller may name the pull
    request a step works on; a profile that does not declare it reads none."""
    from swarm_common.profiles import RUNNER_PROFILES

    for name in ("claude-code", "codex", "claude-code-review"):
        profile = RUNNER_PROFILES[name]
        assert pull_request_mod.INPUT_KEY in profile.inputs, name
        task_input = {"prompt": "review it", pull_request_mod.INPUT_KEY: NUMBER}
        assert pull_request_mod.from_input(task_input, profile) == NUMBER, name
        for wrong in (0, -1, True, "42", pull_request_mod.MAX_NUMBER + 1):
            refused = {"prompt": "review it", pull_request_mod.INPUT_KEY: wrong}
            assert pull_request_mod.from_input(refused, profile) is None, (name, wrong)
    for name in ("claude-code-large", "indexer", "claude-code-browser"):
        profile = RUNNER_PROFILES[name]
        task_input = {"prompt": "review it", pull_request_mod.INPUT_KEY: NUMBER}
        assert pull_request_mod.from_input(task_input, profile) is None, name
