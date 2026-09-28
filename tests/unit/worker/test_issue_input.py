"""A claude-code or codex step pointed at a GitHub issue finds it in its workspace (#265).

Contract request 28, accepted by the owner on 2026-09-28: `input.issue` names
an issue in the step's own repository. The worker fetches its title, body and
comments read-only with the tenant's forge credential and writes them to
`work/issue.md`; the runner names that file in the prompt. What is held here:

  * the fetch asks the repository's own forge, carries the credential only in
    its `Authorization` header, and follows no redirect to another host;
  * the file is scrubbed of every secret the attempt holds, is never written
    through a link, and lands in `work/`, not in the checkout or the artifacts;
  * the prompt names the file by absolute path between the caller's prompt and
    the platform's instructions, and a runner with no file refuses to start;
  * end to end, a fetch that fails fails the attempt INPUTS_UNAVAILABLE and the
    agent never starts, and one that succeeds hands the agent the issue and
    never the token.

Offline: the forge is a function standing in for urllib's opener.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from agent_worker import gitops
from agent_worker import issue as issue_mod
from agent_worker.errors import ExitCode, InputUnavailable
from agent_worker.logs import build_logger
from agent_worker.runners.base import RunnerContext, RunnerFailure
from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.models import EndCause
from swarm_common.states import TaskState

from conftest import TENANT, seed_attempt

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

REPO = "https://github.com/octo/widgets.git"
TOKEN = "ghp_issuefetchtoken0123456789abcdef"


# ---------------------------------------------------------------------------
# a forge, recorded
# ---------------------------------------------------------------------------


def _issue_doc(**overrides: Any) -> dict[str, Any]:
    doc = {
        "number": 265,
        "title": "A step cannot be pointed at an issue",
        "state": "open",
        "user": {"login": "bogdan"},
        "created_at": "2026-09-28T10:00:00Z",
        "html_url": "https://github.com/octo/widgets/issues/265",
        "body": "Steps restate their issue by hand.\n\nReproduce on the Agents screen.",
        "labels": [{"name": "enhancement"}],
        "comments": 0,
    }
    doc.update(overrides)
    return doc


class _Forge:
    """Answers by URL path and records every request as the forge saw it."""

    def __init__(self, answers: dict[str, tuple[int, Any]]) -> None:
        self.answers = answers
        self.requests: list[urllib.request.Request] = []

    def __call__(self, req: urllib.request.Request) -> tuple[int, Any]:
        self.requests.append(req)
        path = req.full_url.split("://", 1)[1].split("/", 1)[1]
        return self.answers.get("/" + path, (404, {"message": "Not Found"}))


@pytest.fixture
def forge(monkeypatch):
    def install(answers: dict[str, tuple[int, Any]]) -> _Forge:
        fake = _Forge(answers)
        monkeypatch.setattr(issue_mod, "_open", fake)
        return fake

    return install


# ---------------------------------------------------------------------------
# what a task asks for
# ---------------------------------------------------------------------------


def test_only_a_profile_that_declares_issue_asks_for_one():
    assert issue_mod.requested({"prompt": "x", "issue": 265}, RUNNER_PROFILES["claude-code"]) == 265
    assert issue_mod.requested({"prompt": "x", "issue": 265}, RUNNER_PROFILES["codex"]) == 265
    assert issue_mod.requested({"prompt": "x"}, RUNNER_PROFILES["claude-code"]) is None
    # The mock declares no `issue`, and `generic` has not declared yet: to
    # neither does the key mean anything, so the worker fetches nothing.
    assert issue_mod.requested({"prompt": "x", "issue": 265}, RUNNER_PROFILES["mock"]) is None
    assert issue_mod.requested({"issue": 265}, RUNNER_PROFILES["generic"]) is None


@pytest.mark.parametrize("value", [0, "265", True, 1.5])
def test_a_stored_issue_the_declaration_refuses_fails_as_an_unavailable_input(value):
    """A task written by a path that skipped the API still meets the declaration."""
    with pytest.raises(issue_mod.IssueUnavailable) as caught:
        issue_mod.requested({"issue": value}, RUNNER_PROFILES["claude-code"])
    assert isinstance(caught.value, InputUnavailable)
    assert "issue" in str(caught.value)


def test_issue_md_is_reserved_from_staging_only_when_an_issue_is_asked_for():
    cc = RUNNER_PROFILES["claude-code"]
    assert issue_mod.reserved_names({"issue": 1}, cc) == {"issue.md"}
    assert issue_mod.reserved_names({"prompt": "x"}, cc) == frozenset()
    assert issue_mod.reserved_names({"issue": 1}, RUNNER_PROFILES["mock"]) == frozenset()


# ---------------------------------------------------------------------------
# the fetch
# ---------------------------------------------------------------------------


def test_the_issue_and_every_page_of_its_comments_are_read_with_the_token(forge):
    page_one = [
        {"user": {"login": f"u{i}"}, "created_at": "2026-09-28", "body": f"comment {i}"}
        for i in range(issue_mod.COMMENTS_PER_PAGE)
    ]
    page_two = [{"user": {"login": "last"}, "created_at": "2026-09-29", "body": "the fix"}]
    fake = forge({
        "/repos/octo/widgets/issues/265": (200, _issue_doc(comments=101)),
        "/repos/octo/widgets/issues/265/comments?per_page=100&page=1": (200, page_one),
        "/repos/octo/widgets/issues/265/comments?per_page=100&page=2": (200, page_two),
    })
    ticks: list[int] = []

    issue = issue_mod.fetch_issue(
        repository_url=REPO, number=265, token=TOKEN, on_request=lambda: ticks.append(1)
    )

    assert issue.title == "A step cannot be pointed at an issue"
    assert issue.repository == "octo/widgets"
    assert issue.labels == ("enhancement",)
    assert len(issue.comments) == 101 and issue.comments[-1].body == "the fix"
    assert len(ticks) == 3, "the heartbeat is not proved between requests"
    assert len(fake.requests) == 3
    for req in fake.requests:
        assert req.full_url.startswith("https://api.github.com/repos/octo/widgets/issues/265")
        assert req.get_method() == "GET", "the fetch is read-only"
        assert req.get_header("Authorization") == f"Bearer {TOKEN}"


def test_without_a_token_no_authorization_header_is_sent(forge):
    fake = forge({"/repos/octo/widgets/issues/7": (200, _issue_doc(number=7))})

    issue_mod.fetch_issue(repository_url=REPO, number=7, token=None)

    (req,) = fake.requests
    assert req.get_header("Authorization") is None


def test_a_github_enterprise_repository_is_asked_at_its_own_host(forge):
    fake = forge({"/api/v3/repos/octo/widgets/issues/7": (200, _issue_doc(number=7))})

    issue_mod.fetch_issue(repository_url="https://ghe.example.com/octo/widgets", number=7, token=TOKEN)

    assert fake.requests[0].full_url == "https://ghe.example.com/api/v3/repos/octo/widgets/issues/7"


def test_a_missing_issue_is_named_with_its_repository(forge):
    forge({})

    with pytest.raises(issue_mod.IssueUnavailable) as caught:
        issue_mod.fetch_issue(repository_url=REPO, number=404, token=TOKEN)

    message = str(caught.value)
    assert "#404" in message and "octo/widgets" in message and "(404" in message


def test_a_pull_requests_number_is_refused(forge):
    forge({"/repos/octo/widgets/issues/9": (200, _issue_doc(number=9, pull_request={"url": "x"}))})

    with pytest.raises(issue_mod.IssueUnavailable) as caught:
        issue_mod.fetch_issue(repository_url=REPO, number=9, token=TOKEN)

    assert "pull request" in str(caught.value)


def test_a_comments_page_that_fails_fails_the_fetch(forge):
    forge({"/repos/octo/widgets/issues/265": (200, _issue_doc(comments=3))})

    with pytest.raises(issue_mod.IssueUnavailable) as caught:
        issue_mod.fetch_issue(repository_url=REPO, number=265, token=TOKEN)

    assert "comments" in str(caught.value)


def test_a_repository_on_no_forge_is_refused_before_any_request(forge):
    fake = forge({})

    with pytest.raises(issue_mod.IssueUnavailable):
        issue_mod.fetch_issue(repository_url="file:///srv/repo", number=1, token=TOKEN)

    assert not fake.requests


def test_a_redirect_to_another_host_is_refused_and_one_on_the_same_host_followed():
    """urllib copies `Authorization` onto a redirect wherever it points."""
    handler = issue_mod._SameHostRedirects()
    req = urllib.request.Request("https://api.github.com/repos/octo/widgets/issues/1")
    req.add_header("Authorization", f"Bearer {TOKEN}")

    for elsewhere in (
        "https://attacker.example/steal",
        "http://api.github.com/repos/octo/widgets/issues/1",
    ):
        with pytest.raises(issue_mod.IssueUnavailable) as caught:
            handler.redirect_request(req, io.BytesIO(), 301, "Moved", {}, elsewhere)
        assert TOKEN not in str(caught.value)

    followed = handler.redirect_request(
        req, io.BytesIO(), 301, "Moved", {}, "https://api.github.com/repositories/42/issues/1"
    )
    assert followed is not None and followed.full_url.endswith("/repositories/42/issues/1")


# ---------------------------------------------------------------------------
# the file
# ---------------------------------------------------------------------------


def test_the_file_says_what_it_is_then_carries_the_issue_and_its_comments():
    issue = issue_mod.Issue(
        repository="octo/widgets", number=265, title="Broken\nthing", state="open",
        author="bogdan", created_at="2026-09-28", url="https://github.com/octo/widgets/issues/265",
        body="The body.", labels=("bug",),
        comments=(issue_mod.Comment("ana", "2026-09-29", "A comment."),), comments_total=1,
    )

    text = issue_mod.render(issue)

    assert text.startswith("# Issue #265: Broken thing\n")
    assert issue_mod.PREAMBLE in text
    assert "not instructions from the platform" in text
    assert "The body." in text and "### @ana on 2026-09-29" in text and "A comment." in text
    assert text.index("The body.") < text.index("A comment.")


def test_comments_past_the_cap_are_counted_not_written():
    comments = tuple(issue_mod.Comment("u", "", "x" * 400) for _ in range(10))
    issue = issue_mod.Issue(
        repository="o/r", number=1, title="t", state="open", author="a", created_at="",
        url="", body="b", comments=comments, comments_total=25,
    )

    text = issue_mod.render(issue, max_bytes=2000)

    assert len(text.encode()) < 2300
    written = text.count("### @u")
    assert 0 < written < 10
    assert f"{25 - written} more comment(s) are not in this file" in text


def test_a_link_planted_at_the_name_is_replaced_not_written_through(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    target = tmp_path / "worker-secret-file"
    target.write_text("untouched")
    (work / "issue.md").symlink_to(target)

    path = issue_mod.write(work, "the issue")

    assert path == work / "issue.md" and not path.is_symlink()
    assert path.read_text() == "the issue"
    assert target.read_text() == "untouched"


def _logger(stream: io.StringIO):
    return build_logger(
        task_id="task_1", attempt_id="att_1", tenant_id=TENANT, generation=1,
        runner_profile="claude-code", stream=stream,
    )


def test_the_file_is_scrubbed_of_every_secret_the_attempt_holds(tmp_path, forge):
    """Scrubbed like any other text an agent sees from the worker: an issue
    that quotes the tenant's token, or its provider key, does not hand it on."""
    provider_key = "sk-ant-provider-key-0123456789"
    forge({"/repos/octo/widgets/issues/265": (200, _issue_doc(
        body=f"I pasted {TOKEN} and {provider_key} here by mistake"))})
    stream = io.StringIO()
    log = _logger(stream)
    log.register_secret(TOKEN)
    log.register_secret(provider_key)
    work = tmp_path / "work"
    work.mkdir()

    path = issue_mod.stage_issue(
        number=265, repository_url=REPO, token=TOKEN, refusal=None, work_dir=work,
        scrub=log.scrub_value, logger=log,
    )

    text = path.read_text()
    assert "I pasted" in text
    assert TOKEN not in text and provider_key not in text
    assert TOKEN not in stream.getvalue()
    # The log line says what was written, never the issue's text.
    assert "pasted" not in stream.getvalue()


def test_a_task_with_no_repository_is_refused_before_any_request(tmp_path, forge):
    fake = forge({})
    log = _logger(io.StringIO())

    with pytest.raises(issue_mod.IssueUnavailable) as caught:
        issue_mod.stage_issue(
            number=3, repository_url=None, token=TOKEN, refusal=None, work_dir=tmp_path,
            scrub=log.scrub_value, logger=log,
        )

    assert "no repository" in str(caught.value)
    assert not fake.requests
    assert not (tmp_path / "issue.md").exists()


def test_a_refused_token_is_named_when_the_fetch_fails(tmp_path, forge):
    forge({})
    log = _logger(io.StringIO())

    with pytest.raises(issue_mod.IssueUnavailable) as caught:
        issue_mod.stage_issue(
            number=3, repository_url=REPO, token=None,
            refusal="the entrypoint established no memory protection", work_dir=tmp_path,
            scrub=log.scrub_value, logger=log,
        )

    assert "without the tenant git token because the entrypoint" in str(caught.value)
    assert not (tmp_path / "issue.md").exists()


# ---------------------------------------------------------------------------
# the prompt
# ---------------------------------------------------------------------------

_RECORDING_CLI = r"""#!/usr/bin/env python3
import json, os, sys
with open(os.path.join(os.environ["SWARM_WORK_DIR"], "argv.json"), "w") as fh:
    json.dump(sys.argv, fh)
print(json.dumps({"result": "ok"}))
"""


def _prompt(tmp_path: Path, monkeypatch, payload: dict[str, Any], *, issue_text: str | None):
    from agent_worker.runners.cliagent import CliAgentSpec, run_cli_agent

    binary = tmp_path / "fake-claude"
    binary.write_text(_RECORDING_CLI)
    binary.chmod(0o755)
    monkeypatch.setenv("FAKE_KEY", "sk-value-0123456789")
    monkeypatch.setenv("FAKE_BIN", str(binary))
    work = tmp_path / "work"
    artifacts = tmp_path / "artifacts"
    work.mkdir(exist_ok=True)
    artifacts.mkdir(exist_ok=True)
    if issue_text is not None:
        (work / "issue.md").write_text(issue_text)
    ctx = RunnerContext(
        work_dir=work, artifacts_dir=artifacts, input_path=work / "input.json",
        result_path=work / "result.json", quota_path=work / "quota.json", payload=payload,
    )
    spec = CliAgentSpec(
        name="fake", provider="anthropic", binary_env="FAKE_BIN", binary_default="fake-claude",
        args_env="FAKE_ARGS", args_default=("--print",), key_env="FAKE_KEY", model_flag=None,
    )
    run_cli_agent(ctx, spec)
    return json.loads((work / "argv.json").read_text())[-1], work, artifacts


def test_the_prompt_names_the_issue_file_between_the_callers_prompt_and_the_instructions(
    tmp_path, monkeypatch
):
    prompt, work, artifacts = _prompt(
        tmp_path, monkeypatch, {"prompt": "Fix the issue.", "issue": 265}, issue_text="# Issue"
    )

    line = issue_mod.prompt_line(work / "issue.md")
    assert prompt.startswith(f"Fix the issue.\n\n{line}\n\n")
    assert str((work / "issue.md").resolve()) in prompt or str(work / "issue.md") in prompt
    assert prompt.endswith(
        f"Files written to {artifacts} ($SWARM_ARTIFACTS_DIR) are uploaded and shown in Artifacts."
    )
    # The number is in the file, not the prompt: a bare `429` in what a CLI
    # echoes reads as a rate limit (`cliagent._RATE_LIMIT_MARKERS`).
    assert "265" not in line


def test_a_task_that_asks_for_no_issue_gets_the_prompt_it_got_before(tmp_path, monkeypatch):
    prompt, _, artifacts = _prompt(tmp_path, monkeypatch, {"prompt": "do it"}, issue_text="stray")

    assert prompt == (
        f"do it\n\nFiles written to {artifacts} ($SWARM_ARTIFACTS_DIR) are uploaded and "
        "shown in Artifacts."
    )


def test_a_runner_whose_issue_file_is_missing_does_not_start_the_agent(tmp_path, monkeypatch):
    with pytest.raises(RunnerFailure) as caught:
        _prompt(tmp_path, monkeypatch, {"prompt": "Fix it.", "issue": 265}, issue_text=None)

    assert "issue.md" in str(caught.value)
    assert not (tmp_path / "work" / "argv.json").exists(), "the agent was started"


# ---------------------------------------------------------------------------
# end to end, through the real lifecycle
# ---------------------------------------------------------------------------

#: A stand-in for `claude` that records what it was handed: the prompt, whether
#: the issue file was there and what it held, and the names of its environment.
_LANE_AGENT = r"""#!/usr/bin/env python3
import json, os, sys
work = os.environ["SWARM_WORK_DIR"]
path = os.path.join(work, "issue.md")
record = {
    "prompt": sys.argv[-1],
    "cwd": os.getcwd(),
    "issue_md": open(path).read() if os.path.isfile(path) else None,
    "env": dict(os.environ),
}
with open(os.path.join(work, "agent-record.json"), "w") as fh:
    json.dump(record, fh)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done"}))
"""


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    )


@pytest.fixture
def origin(tmp_path: Path, monkeypatch) -> str:
    """A bare repository reachable as `file://`, and only it (as test_step_parity.py)."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--quiet", "--initial-branch=main", ".")
    (seed / "README.md").write_text("hello\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "--quiet", "-m", "base")
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(seed), str(bare)],
                   check=True, capture_output=True)
    real = gitops.validate_repository_url
    allowed = f"file://{tmp_path}"
    monkeypatch.setattr(
        gitops, "validate_repository_url",
        lambda url: url if url.startswith(allowed) else real(url),
    )
    return f"file://{bare}"


@pytest.fixture
def lane_agent(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "lane-claude"
    binary.write_text(_LANE_AGENT)
    binary.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CODE_BIN", str(binary))
    monkeypatch.delenv("CLAUDE_CODE_ARGS", raising=False)


def _seed(db, number: int = 265) -> None:
    seed_attempt(db, runner_profile="claude-code",
                 task_input={"prompt": "Fix the issue.", "issue": number})
    db.doc(f"tenants/{TENANT}")["credentials"] = ["anthropic"]


def _worker(worker_factory, monkeypatch, url: str, fetch):
    worker, _, _ = worker_factory(runner_profile="claude-code", repository_url=url)
    # The tenant's forge token, handed to the issue fetch and to nothing else
    # here: a `file://` clone needs none. Registered with the logger as
    # `secrets.resolve_git_token` registers the real one.
    worker.log.register_secret(TOKEN)
    monkeypatch.setattr(
        worker, "_git_token",
        lambda: TOKEN if worker.phases.current == "fetch_issue" else None,
    )
    monkeypatch.setattr(issue_mod, "fetch_issue", fetch)
    return worker


def _agent_record(tmp_path: Path) -> dict[str, Any] | None:
    found = list((tmp_path / "workspace").rglob("agent-record.json"))
    return json.loads(found[0].read_text()) if found else None


@pytest.fixture
def keep_workspace(monkeypatch):
    """The workspace is destroyed on exit; keep it so the test can read it."""
    from agent_worker import lifecycle

    monkeypatch.setattr(lifecycle.workspace_mod, "destroy", lambda ws: None)


@needs_git
def test_the_agent_reads_the_issue_and_never_the_token(
    db, store, worker_factory, monkeypatch, tmp_path, origin, lane_agent, keep_workspace
):
    asked: list[dict[str, Any]] = []

    def fetch(*, repository_url, number, token, on_request=None):
        asked.append({"url": repository_url, "number": number, "token": token})
        return issue_mod.Issue(
            repository="octo/widgets", number=number, title="Point a step at an issue",
            state="open", author="bogdan", created_at="", url="",
            body=f"The body names the token {TOKEN} by mistake.",
        )

    _seed(db)
    worker = _worker(worker_factory, monkeypatch, origin, fetch)

    code = worker.run()

    task = db.doc("tasks/task_1")
    assert code == ExitCode.OK, task
    assert task["state"] == TaskState.SUCCEEDED.value
    assert asked == [{"url": origin, "number": 265, "token": TOKEN}]

    record = _agent_record(tmp_path)
    assert record is not None, "the agent never started"
    assert record["issue_md"] is not None, "the agent found no issue.md"
    assert "# Issue #265: Point a step at an issue" in record["issue_md"]
    assert TOKEN not in record["issue_md"]
    assert "issue.md" in record["prompt"]
    assert TOKEN not in json.dumps(record["env"]), "the token reached the agent's environment"
    # In `work/`, beside the checkout: never in the repository the agent edits.
    assert record["cwd"].endswith(os.sep + "repo")
    assert not os.path.exists(os.path.join(record["cwd"], "issue.md"))

    # Never an artifact: nothing uploaded is named for it.
    names = [entry["name"] for entry in (task.get("result_summary") or {}).get("artifacts", [])]
    assert "issue.md" not in names, names
    assert not [key for key in store.list_keys(f"tenants/{TENANT}/") if key.endswith("/issue.md")]


@needs_git
def test_a_fetch_that_fails_fails_the_attempt_and_the_agent_never_starts(
    db, store, worker_factory, monkeypatch, tmp_path, origin, lane_agent, keep_workspace
):
    def fetch(*, repository_url, number, token, on_request=None):
        raise issue_mod.IssueUnavailable(f"could not fetch issue #{number} of octo/widgets: gone (410)")

    _seed(db, number=31)
    worker = _worker(worker_factory, monkeypatch, origin, fetch)

    code = worker.run()

    task = db.doc("tasks/task_1")
    assert code != ExitCode.OK
    assert task["state"] == TaskState.FAILED.value, task
    assert task.get("end_cause") == EndCause.INPUTS_UNAVAILABLE.value, task
    assert "#31" in (task.get("last_error") or ""), task
    assert _agent_record(tmp_path) is None, "the agent started without its issue"
