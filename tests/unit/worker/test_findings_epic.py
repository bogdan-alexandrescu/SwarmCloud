"""A review's MINOR findings are filed on the tenant's wave epic (#638, owner 2026-10-05).

    implement ──> review ──> fix (when: review verdict_in [NOT_YET])

The review writes `verdict.json`; a finding that is an object with
`"severity": "minor"` is a minor. The step gated on that verdict (the fix,
the one step that reads it) posts each minor ONCE as its own comment on the
issue named by the signed `dispatch.findings_epic`, in CLAUDE.md's epic shape:

    - [ ] **<the defect>** · `<file>` `<call site>` · found by review `<task>` ...

de-duplicated by (file, call site, text) against the comments already there,
with the TENANT's git token, read by the worker after the agent ended. Blockers
and majors are not filed: they are what the fix step fixes. With no epic
configured nothing is posted and `result_summary.findings_epic` says so.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

import pytest

from agent_worker import findings_epic as epic_mod
from agent_worker import forge as forge_mod
from agent_worker import verdict as verdict_mod
from agent_worker.errors import ExitCode
from swarm_common.states import TaskState

from conftest import seed_attempt
from test_input_from import run_upstream

REPO = "https://github.com/acme/widgets.git"
EPIC = 77


def _token() -> str:
    # Built at runtime: nothing token-shaped is a literal in this file.
    return "ghp" + "_" + secrets.token_hex(18)


MINOR_A = {"severity": "minor", "summary": "The docstring names a retired flag",
           "file": "apps/x/a.py", "call_site": "run()", "evidence": "read by lane MIN"}
MINOR_B = {"severity": "Minor", "summary": "A log line repeats its own field",
           "file": "apps/x/b.py", "call_site": "Worker.finish()"}
# The shape the review steps' briefs prescribe, and the shape of the minors
# #638 counted: `problem` is the defect, `where` the call site.
MINOR_BRIEF = {"severity": "minor", "file": "apps/x/e.py", "where": "Worker.park()",
               "problem": "The park reason is logged twice",
               "fix": "Log it once in park()"}
MAJOR_BRIEF = {"severity": "major", "file": "apps/x/f.py", "where": "lease()",
               "problem": "The lease outlives its generation", "fix": "Fence it"}
MAJOR = {"severity": "major", "summary": "The lease is released twice",
         "file": "apps/x/c.py", "call_site": "release()"}
BLOCKER = {"severity": "blocker", "summary": "A token reaches the log",
           "file": "apps/x/d.py", "call_site": "log()"}


class FakeGitHub:
    """`forge._request` for one repository's issue comments."""

    def __init__(self) -> None:
        self.comments: dict[int, list[dict[str, Any]]] = {}
        self.posted: list[tuple[int, str]] = []
        self.tokens: set[str] = set()
        self.next_id = 1000

    def __call__(self, url: str, *, token: str, method: str = "GET",
                 payload: dict[str, Any] | None = None) -> tuple[int, Any]:
        self.tokens.add(token)
        assert url.startswith("https://api.github.com/repos/acme/widgets/issues/"), url
        number = int(url.split("/issues/")[1].split("/")[0])
        if number not in self.comments:
            return 404, {"message": "Not Found"}
        if method == "POST":
            assert payload is not None
            self.next_id += 1
            self.comments[number].append({"id": self.next_id, "body": payload["body"]})
            self.posted.append((number, payload["body"]))
            return 201, {"id": self.next_id, "body": payload["body"]}
        page = int(url.split("page=")[-1])
        rows = self.comments[number][(page - 1) * 100: page * 100]
        return 200, list(rows)


@pytest.fixture
def github(monkeypatch) -> FakeGitHub:
    fake = FakeGitHub()
    fake.comments[EPIC] = []
    monkeypatch.setattr(forge_mod, "_request", fake)
    return fake


# --------------------------------------------------------------------------
# reading the minors out of a verdict
# --------------------------------------------------------------------------

def test_only_findings_marked_minor_are_minors(tmp_path):
    path = tmp_path / "verdict.json"
    path.write_text(json.dumps({
        "verdict": "NOT_YET",
        "findings": ["a plain string is a blocker by convention", MINOR_A, MAJOR, BLOCKER,
                     MINOR_B, {"severity": "minor"}],
    }))
    read = verdict_mod.read_verdict(path, task_id="task_up", filename="verdict.json")
    assert [m.text for m in read.minors] == [MINOR_A["summary"], MINOR_B["summary"]]
    assert read.minors[0].file == "apps/x/a.py"
    assert read.minors[0].call_site == "run()"
    # The findings the PR body and the fix agent see are unchanged.
    assert len(read.findings) == 6


def test_a_minor_in_the_review_brief_shape_is_read(tmp_path):
    path = tmp_path / "verdict.json"
    path.write_text(json.dumps({"verdict": "NOT_YET",
                                "findings": [MAJOR_BRIEF, MINOR_BRIEF]}))
    read = verdict_mod.read_verdict(path, task_id="task_up", filename="verdict.json")
    assert len(read.minors) == 1
    minor = read.minors[0]
    assert minor.text == MINOR_BRIEF["problem"]
    assert minor.file == "apps/x/e.py"
    assert minor.call_site == "Worker.park()"
    assert minor.evidence == "suggested fix: Log it once in park()"


def test_call_site_and_evidence_win_over_where_and_fix():
    (minor,) = _minors({**MINOR_BRIEF, "call_site": "park()", "evidence": "read by lane MIN"})
    assert minor.call_site == "park()"
    assert minor.evidence == "read by lane MIN"


def test_a_comment_names_the_defect_the_file_and_the_call_site():
    line = epic_mod.render_comment(
        verdict_mod.MinorFinding(text="It is wrong", file="a.py", call_site="f()"),
        found_by="found by review `task_up`",
        clean=lambda s: s,
    )
    assert line == "- [ ] **It is wrong** · `a.py` `f()` · found by review `task_up`"
    assert epic_mod.keys_in(line) == {epic_mod.finding_key("It is wrong", "a.py", "f()")}


def test_agent_text_cannot_break_the_line_or_the_key():
    finding = verdict_mod.MinorFinding(text="bold **break** here", file="a`b.py",
                                       call_site="f()")
    line = epic_mod.render_comment(finding, found_by="x", clean=lambda s: s)
    assert "\n" not in line
    assert epic_mod.keys_in(line) == {epic_mod.key_of(finding)}


# --------------------------------------------------------------------------
# filing
# --------------------------------------------------------------------------

def _file(minors, epic=EPIC, token=None) -> dict[str, Any]:
    ref = forge_mod.parse_repo(REPO)
    token = token or _token()
    return epic_mod.file_minors(
        minors=minors, epic=epic, ref=ref,
        list_comments=lambda n: forge_mod.list_issue_comments(ref=ref, token=token, number=n),
        post_comment=lambda n, body: forge_mod.create_issue_comment(
            ref=ref, token=token, number=n, body=body),
        found_by="found by review `task_up`",
        clean=lambda s: s,
    )


def _minors(*items: dict[str, Any]) -> tuple[verdict_mod.MinorFinding, ...]:
    return verdict_mod.minor_findings(list(items))[0]


def test_each_minor_is_posted_once_as_its_own_comment(github):
    result = _file(_minors(MINOR_A, MINOR_B, MINOR_A))
    assert len(github.posted) == 2
    assert all(n == EPIC for n, _ in github.posted)
    assert github.posted[0][1].startswith("- [ ] **The docstring names a retired flag** · "
                                          "`apps/x/a.py` `run()` · ")
    assert [f["text"] for f in result["filed"]] == [MINOR_A["summary"], MINOR_B["summary"]]
    assert result["already_filed"] == 1
    assert all(isinstance(f["comment_id"], int) for f in result["filed"])


def test_a_rerun_posts_nothing_new(github):
    _file(_minors(MINOR_A, MINOR_B))
    again = _file(_minors(MINOR_B, MINOR_A))
    assert len(github.posted) == 2
    assert again["filed"] == []
    assert again["already_filed"] == 2


def test_a_review_brief_minor_is_posted_once_and_deduped_on_rerun(github):
    first = _file(_minors(MINOR_BRIEF, MAJOR_BRIEF))
    assert len(github.posted) == 1
    assert github.posted[0][1] == (
        "- [ ] **The park reason is logged twice** · `apps/x/e.py` `Worker.park()` · "
        "found by review `task_up`; suggested fix: Log it once in park()"
    )
    assert [f["text"] for f in first["filed"]] == [MINOR_BRIEF["problem"]]
    again = _file(_minors(MINOR_BRIEF))
    assert again["filed"] == []
    assert again["already_filed"] == 1
    assert len(github.posted) == 1


def test_a_finding_a_person_already_filed_by_hand_is_not_filed_again(github):
    github.comments[EPIC].append({"id": 1, "body": (
        "- [x] **the docstring names a retired   flag** · `apps/x/a.py` `run()` · lane P2"
    )})
    result = _file(_minors(MINOR_A))
    assert github.posted == []
    assert result["already_filed"] == 1


def test_the_dedup_reads_every_page_of_the_epic(github):
    github.comments[EPIC] = [{"id": i, "body": f"note {i}"} for i in range(250)]
    github.comments[EPIC].append({"id": 999, "body": epic_mod.render_comment(
        _minors(MINOR_A)[0], found_by="earlier", clean=lambda s: s)})
    _file(_minors(MINOR_A))
    assert github.posted == []


def test_an_epic_that_cannot_be_read_files_nothing_and_says_why(github):
    result = _file(_minors(MINOR_A), epic=404404)
    assert github.posted == []
    assert result["filed"] == []
    assert "404" in result["not_filed"]


# --------------------------------------------------------------------------
# the step that reads the verdict files them
# --------------------------------------------------------------------------

def _seed_fix(db: Any, *, epic: Any = EPIC, n: int = 2) -> None:
    seed_attempt(
        db, task_id=f"task_{n}", attempt_id=f"att_{n}", lease_id=f"lease_{n}",
        task_input={"prompt": "fix the findings", "steps": 1, "sleep_seconds": 0.01},
    )
    dispatch: dict[str, Any] = {
        "strategy": "collect", "carrier": "checkpoints",
        "verdict_gate": {"task_id": "task_up", "verdict_in": ["NOT_YET"]},
    }
    if epic is not None:
        dispatch["findings_epic"] = epic
    db.doc(f"tasks/task_{n}")["metadata"] = {
        "input_from": {"task_up": "verdict.json"},
        "dispatch": dispatch,
    }


def _run_fix(db, worker_factory, monkeypatch, verdict: dict[str, Any] | None, token: str,
             epic: Any = EPIC, n: int = 2) -> dict[str, Any]:
    if verdict is not None:
        run_upstream(db, worker_factory, artifact_name="verdict.json",
                     artifact_text=json.dumps(verdict))
    _seed_fix(db, epic=epic, n=n)
    worker, _config, _ = worker_factory(task_id=f"task_{n}", attempt_id=f"att_{n}",
                                        lease_id=f"lease_{n}")
    # The repository this step cloned. No clone runs here: the step is
    # `collect` with no repository, so this stands in for `_maybe_clone`.
    worker._repo_url = REPO
    monkeypatch.setattr(worker, "_git_token", lambda: token)
    assert worker.run() == ExitCode.OK
    task = db.doc(f"tasks/task_{n}")
    assert task["state"] == TaskState.SUCCEEDED.value, task.get("last_error")
    return task["result_summary"]


def test_the_gated_step_files_the_minors_and_lists_them(
    db, worker_factory, monkeypatch, github, log_stream
):
    token = _token()
    summary = _run_fix(db, worker_factory, monkeypatch, {
        "verdict": "MERGE", "findings": [MINOR_A, MAJOR, BLOCKER, MINOR_B],
    }, token)

    filed = summary["findings_epic"]
    assert filed["epic"] == EPIC
    assert filed["repository"] == "acme/widgets"
    assert [f["text"] for f in filed["filed"]] == [MINOR_A["summary"], MINOR_B["summary"]]
    # Majors and blockers are the fix step's, never the epic's.
    bodies = " ".join(body for _, body in github.posted)
    assert MAJOR["summary"] not in bodies and BLOCKER["summary"] not in bodies
    assert len(github.posted) == 2
    # The tenant's token went to the forge, and nowhere a reader sees.
    assert github.tokens == {token}
    assert token not in log_stream.getvalue()
    assert token not in json.dumps(db.doc("tasks/task_2"), default=str)


def test_the_gated_step_does_not_file_twice_on_a_rerun(
    db, worker_factory, monkeypatch, github
):
    verdict = {"verdict": "NOT_YET", "findings": [MINOR_A, MAJOR]}
    first = _run_fix(db, worker_factory, monkeypatch, verdict, _token())
    assert len(first["findings_epic"]["filed"]) == 1
    # The same review read again by a second fix task.
    second = _run_fix(db, worker_factory, monkeypatch, None, _token(), n=3)
    assert second["findings_epic"]["filed"] == []
    assert second["findings_epic"]["already_filed"] == 1
    assert len(github.posted) == 1


def test_the_gated_step_files_a_review_brief_minor_once(
    db, worker_factory, monkeypatch, github, log_stream
):
    token = _token()
    verdict = {"verdict": "NOT_YET", "findings": [MINOR_BRIEF, MAJOR_BRIEF]}
    first = _run_fix(db, worker_factory, monkeypatch, verdict, token)
    assert [f["text"] for f in first["findings_epic"]["filed"]] == [MINOR_BRIEF["problem"]]
    assert [f["call_site"] for f in first["findings_epic"]["filed"]] == ["Worker.park()"]
    second = _run_fix(db, worker_factory, monkeypatch, None, _token(), n=3)
    assert second["findings_epic"]["filed"] == []
    assert second["findings_epic"]["already_filed"] == 1
    assert len(github.posted) == 1
    assert MAJOR_BRIEF["problem"] not in github.posted[0][1]
    assert token not in log_stream.getvalue()


def test_no_epic_configured_posts_nothing_and_says_so(
    db, worker_factory, monkeypatch, github
):
    summary = _run_fix(db, worker_factory, monkeypatch, {
        "verdict": "MERGE", "findings": [MINOR_A, MINOR_B],
    }, _token(), epic=None)
    assert github.posted == []
    assert github.tokens == set()
    filed = summary["findings_epic"]
    assert filed["epic"] is None
    assert filed["filed"] == []
    assert filed["minors"] == 2
    assert "findings_epic" in filed["not_filed"]


def test_a_malformed_epic_posts_nothing(db, worker_factory, monkeypatch, github):
    summary = _run_fix(db, worker_factory, monkeypatch, {
        "verdict": "MERGE", "findings": [MINOR_A],
    }, _token(), epic=True)
    assert github.posted == []
    assert summary["findings_epic"]["epic"] is None
    assert summary["findings_epic"]["not_filed"]
