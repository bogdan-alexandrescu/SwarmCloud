"""What a workflow is about, and where it came from (the owner, 2026-11-04).

Opening a workflow in the console, the owner could not tell what feature or
repository it works on, nor read what it is meant to deliver. A submission's
metadata now carries `description` (plain text, at most 600 characters,
trimmed, no markup) and `feature` (`{title}` of at most 120 characters, plus
an optional `url` naming an issue or pull request of the workflow's OWN
GitHub repository), and swarm-api records `description_source: "submitter"`
beside a description. Every workflow read serves them -- `GET
/v1/workflows/{id}`, and each `GET /v1/workflows` row in the full and the
summary view -- with the repository, the title and `started_from`, one word
for where the workflow came from, read off what the platform already writes.

Offline: the real routes over FakeFirestore, and `WorkflowCreate` itself.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from swarm_api import wfdescription
from swarm_api.routes.workflows import STARTED_FROM, _started_from
from swarm_api.schemas import DESCRIPTION_MAX_CHARS, FEATURE_TITLE_MAX_CHARS, WorkflowCreate

from .conftest import auth_header

REPO = "https://github.com/octo-org/widget-shop.git"
DESCRIPTION = ("Adds a size chart to the product page. Shoppers pick a size without leaving it, "
               "and the chart reads from the catalogue.")
FEATURE = {"title": "Size guidance", "url": "https://github.com/octo-org/widget-shop/issues/41"}
ABOUT_KEYS = ("repository_url", "title", "description", "description_source", "feature", "started_from")


def _spec(**metadata: Any) -> dict[str, Any]:
    return {
        "repository_url": REPO,
        "strategy": "collect",
        "steps": [{"step_id": "build", "runner_profile": "mock", "input": {"prompt": "build it"}}],
        "metadata": metadata,
    }


def _create(**metadata: Any) -> WorkflowCreate:
    return WorkflowCreate.model_validate(_spec(**metadata))


def _refusal(**metadata: Any) -> str:
    with pytest.raises(ValidationError) as caught:
        _create(**metadata)
    return str(caught.value)


# --------------------------------------------------------------------------
# Accepted, capped and refused at submission
# --------------------------------------------------------------------------


def test_a_description_is_trimmed_and_recorded_as_the_submitters():
    created = _create(description=f"  {DESCRIPTION}\n", feature=FEATURE)
    assert created.metadata["description"] == DESCRIPTION
    assert created.metadata["description_source"] == "submitter"
    assert created.metadata["feature"] == FEATURE


def test_a_description_at_the_cap_is_accepted_and_one_past_it_refused():
    assert _create(description="a" * DESCRIPTION_MAX_CHARS).metadata["description"] == "a" * 600
    message = _refusal(description="a" * (DESCRIPTION_MAX_CHARS + 1))
    assert "601 characters and holds at most 600" in message


@pytest.mark.parametrize("text", [
    "Adds a <b>size</b> chart.",
    "See [the issue](https://github.com/octo-org/widget-shop/issues/41).",
    "## Size chart\nAdds one.",
    "Adds a **size** chart.",
    "```\ncode\n```",
])
def test_a_description_with_markup_is_refused(text):
    assert "holds markup" in _refusal(description=text)


@pytest.mark.parametrize("value", ["", "   ", 7, ["a"]])
def test_an_empty_or_non_text_description_is_refused(value):
    assert "metadata.description" in _refusal(description=value)


def test_a_callers_description_source_is_never_kept():
    # Overwritten beside a description, dropped without one: a caller cannot
    # mark its own text as generated, nor claim a description it never sent.
    assert _create(description=DESCRIPTION, description_source="generated").metadata[
        "description_source"] == "submitter"
    assert "description_source" not in _create(description_source="submitter").metadata


def test_a_validated_spec_validates_again_unchanged():
    # A custom-prompt schedule stores the validated spec's dump and validates
    # it again at every firing: the dump must not be refused.
    created = _create(description=DESCRIPTION, feature=FEATURE)
    again = WorkflowCreate.model_validate(created.model_dump(mode="json"))
    assert again.metadata == created.metadata


def test_a_feature_title_at_the_cap_is_accepted_and_one_past_it_refused():
    assert _create(feature={"title": "f" * FEATURE_TITLE_MAX_CHARS}).metadata["feature"] == {"title": "f" * 120}
    assert "holds at most 120" in _refusal(feature={"title": "f" * (FEATURE_TITLE_MAX_CHARS + 1)})


@pytest.mark.parametrize("feature", [
    "Size guidance",
    {"url": FEATURE["url"]},
    {"title": ""},
    {"title": "two\nlines"},
    {"title": "Size guidance", "owner": "someone"},
])
def test_a_malformed_feature_is_refused(feature):
    assert "metadata.feature" in _refusal(feature=feature)


@pytest.mark.parametrize("url", [
    "http://github.com/octo-org/widget-shop/issues/41",
    "https://gitlab.com/octo-org/widget-shop/issues/41",
    "https://github.com/octo-org/widget-shop/commit/abc",
    "https://github.com/octo-org/widget-shop/issues/0",
    "https://github.com/octo-org/widget-shop/issues/41#comment",
])
def test_a_feature_url_that_is_not_a_github_issue_or_pull_request_is_refused(url):
    assert "must be an https://github.com/" in _refusal(feature={"title": "Size", "url": url})


def test_a_feature_url_of_another_repository_is_refused():
    message = _refusal(feature={"title": "Size", "url": "https://github.com/octo-org/other-shop/pull/9"})
    assert "octo-org/other-shop" in message and "own repository" in message


def test_a_feature_url_of_the_own_repository_in_any_spelling_is_accepted():
    url = "https://github.com/Octo-Org/Widget-Shop/pull/9"
    for repository in (REPO, "https://github.com/octo-org/widget-shop", "git@github.com:octo-org/widget-shop.git",
                       "ssh://git@github.com/octo-org/widget-shop.git"):
        spec = {**_spec(feature={"title": "Size", "url": url}), "repository_url": repository}
        assert WorkflowCreate.model_validate(spec).metadata["feature"]["url"] == url


def test_a_feature_url_with_no_repository_is_refused():
    spec = _spec(feature=FEATURE)
    spec.pop("repository_url")
    with pytest.raises(ValidationError, match="not a GitHub repository"):
        WorkflowCreate.model_validate(spec)


def test_a_step_cannot_set_the_workflows_description_or_feature():
    # A step's metadata is merged over the workflow's onto its task, so a
    # step's own would reach the store unchecked.
    spec = _spec()
    spec["steps"][0]["metadata"] = {"description": "x" * 2000}
    with pytest.raises(ValidationError, match="the workflow's, sent in its own metadata"):
        WorkflowCreate.model_validate(spec)


# --------------------------------------------------------------------------
# Served on read and list
# --------------------------------------------------------------------------


def _post(client, **metadata: Any) -> dict[str, Any]:
    response = client.post("/v1/workflows", headers=auth_header("alice"), json=_spec(**metadata))
    assert response.status_code == 201, response.text
    return response.json()["workflow"]


def test_a_description_over_the_cap_is_a_422_naming_the_rule(client):
    response = client.post("/v1/workflows", headers=auth_header("alice"),
                           json=_spec(description="a" * (DESCRIPTION_MAX_CHARS + 1)))
    assert response.status_code == 422
    assert "holds at most 600" in response.text


def test_the_read_serves_what_the_workflow_is_about(client):
    created = _post(client, title="Size chart", description=DESCRIPTION, feature=FEATURE)
    assert created["description"] == DESCRIPTION
    read = client.get(f"/v1/workflows/{created['workflow_id']}", headers=auth_header("alice"))
    assert read.status_code == 200, read.text
    workflow = read.json()["workflow"]
    assert {k: workflow[k] for k in ABOUT_KEYS} == {
        "repository_url": REPO,
        "title": "Size chart",
        "description": DESCRIPTION,
        "description_source": "submitter",
        "feature": FEATURE,
        "started_from": "console",
    }


@pytest.mark.parametrize("view", [None, "full", "summary"])
def test_every_list_row_serves_it_in_both_views(client, view):
    _post(client, description=DESCRIPTION, feature={"title": "Size guidance"})
    params = {"limit": 10, **({"view": view} if view else {})}
    response = client.get("/v1/workflows", headers=auth_header("alice"), params=params)
    assert response.status_code == 200, response.text
    (row,) = response.json()["workflows"]
    assert row["repository_url"] == REPO
    assert row["description"] == DESCRIPTION
    assert row["description_source"] == "submitter"
    assert row["feature"] == {"title": "Size guidance"}
    assert row["started_from"] == "console"


def test_a_workflow_without_them_serves_nulls_and_a_generated_description(client, api_context, monkeypatch):
    # No feature and no title are made up. The description is written for
    # it after the submission returns, marked generated (`wfdescription`),
    # with the switch as Terraform ships it.
    service = api_context.submissions
    monkeypatch.setattr(service, "_settings", replace(service._settings, workflow_description_model="off"))
    workflow = _post(client)
    assert wfdescription.wait_pending(10)
    read = client.get(f"/v1/workflows/{workflow['workflow_id']}", headers=auth_header("alice")).json()
    served = read["workflow"]
    assert served["description"] == "build it." and served["description_source"] == "generated"
    assert served["feature"] is None and served["title"] is None


def test_a_workflow_without_them_serves_nulls(client, monkeypatch):
    # What the submission itself stores: the generated description is a
    # separate write after it returns, held by the test above.
    monkeypatch.setattr(wfdescription, "after_submission", lambda *a, **k: None)
    workflow = _post(client)
    read = client.get(f"/v1/workflows/{workflow['workflow_id']}", headers=auth_header("alice")).json()
    served = read["workflow"]
    assert served["description"] is None and served["description_source"] is None
    assert served["feature"] is None and served["title"] is None


def test_a_claude_code_submission_reads_as_claude_code(client):
    workflow = _post(client, origin="swarm-mcp")
    read = client.get(f"/v1/workflows/{workflow['workflow_id']}", headers=auth_header("alice")).json()
    assert read["workflow"]["started_from"] == "claude_code"


# --------------------------------------------------------------------------
# Where it came from: the field that decides each
# --------------------------------------------------------------------------


def _task(**metadata: Any) -> Any:
    # `_started_from` reads a step task's metadata and nothing else.
    return SimpleNamespace(metadata=metadata)


@pytest.mark.parametrize(("metadata", "expected"), [
    ({"schedule": {"firing_id": "f1"}, "claude_session": "a" * 8}, "schedule"),
    ({"qa_target": {"target_id": "qat_1"}, "origin": "qa-target"}, "quality_check"),
    ({"issue_run": "run_1", "claude_session": "a" * 8}, "issue_run"),
    ({"claude_session": "abcd1234"}, "claude_code"),
    ({"origin": "swarm-mcp"}, "claude_code"),
    ({"origin": "qa-target"}, "console"),
    ({}, "console"),
])
def test_started_from_reads_the_platforms_own_marks(metadata, expected):
    assert _started_from([_task(), _task(**metadata)]) == expected
    assert expected in STARTED_FROM
