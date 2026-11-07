"""A step whose brief says it changes nothing must declare `allow_empty_diff`.

The measured failure (2026-11-07, part of #2688): a workflow of read-only
analysis steps, each told to change nothing in the repository and to write
its result as artifacts, was submitted with strategy `direct-pr` and no
`allow_empty_diff`. Every step ran clean and FAILED `published_nothing:`.

So swarm-api refuses such a step at submission, 422
`step_declares_no_change`, with a fixed sentence and the step id in `detail`
(swarm_api/nochange.py). An explicit `true` or `false` is a declaration and
is accepted; `collect` and a reader (a review another step reads) are never
refused; and phrases a committing step writes too do not match. The refusal
is new, so it ships report-only (`refusals.SWITCHES`): these tests turn it on.

MUTATIONS: drop the call from `SubmissionService` -- the refused tests see
201. Read `allow_empty_diff`'s value instead of `model_fields_set` -- the
explicit-false test is refused. Drop a phrase from `NO_CHANGE_PHRASES` -- its
parametrised case sees 201. Match "read-only" alone -- its excluded case is
refused.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_api import refusals
from swarm_api.nochange import NO_CHANGE_PHRASES, NO_CHANGE_UNDECLARED_MESSAGE

from .conftest import auth_header

REPO = "https://github.com/example-org/example.git"
SWITCH = refusals.env_name("step_declares_no_change")


@pytest.fixture(autouse=True)
def _switched_on(monkeypatch):
    monkeypatch.setenv(SWITCH, "on")


def _post(client: Any, spec: dict[str, Any]) -> Any:
    return client.post("/v1/workflows", headers=auth_header("alice"), json=spec)


def _analysis(prompt: str, *, strategy: str = "direct-pr", **step: Any) -> dict[str, Any]:
    return {
        "strategy": strategy,
        "repository_url": REPO,
        "steps": [{
            "step_id": "triage", "runner_profile": "mock",
            "input": {"prompt": prompt}, **step,
        }],
    }


BRIEF = "classify every branch; write triage.tsv and a summary; change nothing"


def _stored_dispatch(db: Any, response: Any, step_id: str) -> dict[str, Any]:
    steps = {s["step_id"]: s["task_id"] for s in response.json()["workflow"]["steps"]}
    return db.docs[f"tasks/{steps[step_id]}"]["metadata"]["dispatch"]


def _workflows(db: Any) -> list[str]:
    return [key for key in db.docs if key.startswith("workflows/")]


def test_an_undeclared_analysis_step_is_refused_naming_the_step_in_detail(client, db):
    response = _post(client, _analysis(BRIEF))
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "step_declares_no_change"
    assert body["message"] == NO_CHANGE_UNDECLARED_MESSAGE
    assert body["detail"]["step_id"] == "triage"
    # Never interpolated into the sentence.
    assert "triage" not in body["message"]
    # Refused before anything is stored.
    assert _workflows(db) == []


def test_the_sentence_is_the_fixed_one():
    assert NO_CHANGE_UNDECLARED_MESSAGE == (
        "a step whose brief says it changes nothing must say allow_empty_diff: true "
        "(its deliverable is its artifacts) or allow_empty_diff: false (it must commit)"
    )


def test_an_integrate_integrator_that_says_it_changes_nothing_is_refused(client, db):
    spec = _analysis("gather the notes", strategy="integrate")
    spec["steps"].append({
        "step_id": "summary", "runner_profile": "mock", "depends_on": ["triage"],
        "input": {"prompt": "summarise the notes as artifacts; commit nothing"},
    })
    response = _post(client, spec)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["step_id"] == "summary"


def test_explicit_true_is_accepted_and_stored(client, db):
    response = _post(client, _analysis(BRIEF, allow_empty_diff=True))
    assert response.status_code == 201, response.text
    assert _stored_dispatch(db, response, "triage")["allow_empty_diff"] is True


def test_explicit_false_is_accepted(client, db):
    response = _post(client, _analysis(BRIEF, allow_empty_diff=False))
    assert response.status_code == 201, response.text
    assert "allow_empty_diff" not in _stored_dispatch(db, response, "triage")


def test_collect_is_never_refused(client, db):
    response = _post(client, _analysis(BRIEF, strategy="collect"))
    assert response.status_code == 201, response.text


def test_a_review_another_step_reads_is_accepted(client, db):
    spec = _analysis("review the change; write review.md; change nothing")
    spec["steps"][0]["step_id"] = "review"
    spec["steps"].append({
        "step_id": "fix", "runner_profile": "mock", "depends_on": ["review"],
        "input_from": {"review": "review.md"},
        "input": {"prompt": "fix every finding in review.md"},
    })
    response = _post(client, spec)
    assert response.status_code == 201, response.text


def test_report_only_while_the_switch_is_off(client, db, monkeypatch, caplog):
    monkeypatch.setenv(SWITCH, "off")
    with caplog.at_level("WARNING", logger="swarm_api.refusals"):
        response = _post(client, _analysis(BRIEF))
    assert response.status_code == 201, response.text
    assert "code=step_declares_no_change" in caplog.text


@pytest.mark.parametrize("prompt", [
    "implement the fix; do not commit secrets",
    "if the bug is already fixed, finish without changes",
    "use the read-only token to read the issue, then implement it",
    "rename the helper; no changes outside apps/example",
])
def test_phrases_a_committing_step_writes_are_accepted(client, db, prompt):
    response = _post(client, _analysis(prompt))
    assert response.status_code == 201, response.text


@pytest.mark.parametrize("phrase", NO_CHANGE_PHRASES)
def test_each_listed_phrase_is_matched_case_insensitively(client, db, phrase):
    response = _post(client, _analysis(f"Classify the branches. {phrase.upper()}."))
    assert response.status_code == 422, (phrase, response.text)
    assert response.json()["code"] == "step_declares_no_change"


def test_the_phrase_list_is_the_briefs():
    assert NO_CHANGE_PHRASES == (
        "change nothing", "changes nothing", "commit nothing", "make no change",
        "make no changes", "nothing in the repository is changed",
        "do not change the repository", "do not modify the repository",
        "leave the repository unchanged", "read-only step", "read-only task",
    )


def test_a_phrase_inside_a_longer_word_is_not_matched(client, db):
    response = _post(client, _analysis("the exchange nothingness report"))
    assert response.status_code == 201, response.text
