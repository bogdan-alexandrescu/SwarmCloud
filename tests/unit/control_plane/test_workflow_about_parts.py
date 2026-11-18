"""A workflow's description in three labelled parts (the owner, 2026-11-06).

The owner, opening a workflow in the console, asked for what the work is for,
what it must achieve and what it will produce. A spec sends
`metadata.about` `{purpose, goals, output}`, each optional, checked like
`metadata.description` (plain text, a cap a part, no markup), recorded
`description_source: "submitter"`; `routes.workflows.workflow_about` serves
it as `about`, and a bare `metadata.description` as its purpose.

Offline: the real routes over FakeFirestore.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from swarm_api import wfdescription
from swarm_api.schemas import (
    ABOUT_GOAL_MAX_CHARS,
    ABOUT_GOALS_MAX,
    ABOUT_OUTPUT_MAX_CHARS,
    ABOUT_PURPOSE_MAX_CHARS,
    WorkflowCreate,
)

from .conftest import auth_header

REPO = "https://github.com/octo-org/widget-shop.git"
ABOUT = {
    "purpose": "Shoppers asked for size guidance on the product page.",
    "goals": ["A size chart on every product page", "Sizes read from the catalogue"],
    "output": "A pull request with the chart component and its tests.",
}


def _spec(**metadata: Any) -> dict[str, Any]:
    return {
        "repository_url": REPO,
        "strategy": "collect",
        "steps": [{"step_id": "build", "runner_profile": "mock", "input": {"prompt": "build it"}}],
        "metadata": metadata,
    }


def _refusal(**metadata: Any) -> str:
    with pytest.raises(ValidationError) as caught:
        WorkflowCreate.model_validate(_spec(**metadata))
    return str(caught.value)


@pytest.fixture(autouse=True)
def no_generated(monkeypatch):
    # What the submitter sent, read alone: nothing is generated beside it.
    monkeypatch.setattr(wfdescription, "after_submission", lambda *a, **k: None)


def _served(client, **metadata: Any) -> dict[str, Any]:
    created = client.post("/v1/workflows", headers=auth_header("alice"), json=_spec(**metadata))
    assert created.status_code == 201, created.text
    workflow_id = created.json()["workflow"]["workflow_id"]
    read = client.get(f"/v1/workflows/{workflow_id}", headers=auth_header("alice"))
    assert read.status_code == 200, read.text
    return read.json()["workflow"]


def test_a_submitters_about_is_kept_and_served(client):
    served = _served(client, about={**ABOUT, "purpose": f"  {ABOUT['purpose']} "})
    assert served["about"] == ABOUT
    assert served["description_source"] == "submitter"
    assert served["description"] is None


def test_each_part_is_optional(client):
    served = _served(client, about={"output": "Mock-up frames of the checkout."})
    assert served["about"] == {"purpose": None, "goals": [], "output": "Mock-up frames of the checkout."}
    assert served["description_source"] == "submitter"


def test_a_description_alone_is_served_as_the_purpose(client):
    served = _served(client, description="Adds a size chart. It reads the catalogue.")
    assert served["about"] == {"purpose": "Adds a size chart. It reads the catalogue.", "goals": [], "output": None}
    assert served["description_source"] == "submitter"


def test_none_served_is_null(client):
    served = _served(client)
    assert served["about"] is None and served["description"] is None and served["description_source"] is None


def test_a_listed_workflow_serves_it_too(client):
    _served(client, about=ABOUT)
    rows = client.get("/v1/workflows", headers=auth_header("alice")).json()["workflows"]
    assert [row["about"] for row in rows] == [ABOUT]


@pytest.mark.parametrize(("part", "value", "named"), [
    ("purpose", "p" * (ABOUT_PURPOSE_MAX_CHARS + 1), "metadata.about.purpose is 301 characters"),
    ("output", "o" * (ABOUT_OUTPUT_MAX_CHARS + 1), "metadata.about.output is 301 characters"),
    ("goals", ["fine", "g" * (ABOUT_GOAL_MAX_CHARS + 1)], "metadata.about.goals[1] is 121 characters"),
    ("goals", ["a"] * (ABOUT_GOALS_MAX + 1), "metadata.about.goals must be a list of 1 to 3"),
    ("goals", "one goal", "metadata.about.goals must be a list"),
    ("purpose", "", "metadata.about.purpose must be non-empty text"),
    ("purpose", "Two lines.\nOf purpose.", "metadata.about.purpose must be one line"),
])
def test_an_over_long_or_malformed_part_is_refused_with_the_field_named(part, value, named):
    assert named in _refusal(about={part: value})


@pytest.mark.parametrize(("part", "value", "named"), [
    ("purpose", "Adds a <b>size</b> chart.", "metadata.about.purpose holds markup"),
    ("output", "See [the PR](https://github.com/octo-org/widget-shop/pull/4).", "metadata.about.output holds markup"),
    ("goals", ["**Bold** goal"], "metadata.about.goals[0] holds markup"),
])
def test_a_marked_up_part_is_refused_with_the_field_named(part, value, named):
    assert named in _refusal(about={part: value})


@pytest.mark.parametrize("about", [{}, {"summary": "x"}, "a sentence", ["a"]])
def test_an_about_with_no_known_part_is_refused(about):
    assert "metadata.about must be {purpose?, goals?, output?}" in _refusal(about=about)


def test_a_step_may_not_carry_its_own_about():
    spec = _spec()
    spec["steps"][0]["metadata"] = {"about": ABOUT}
    with pytest.raises(ValidationError) as caught:
        WorkflowCreate.model_validate(spec)
    assert "sets metadata about" in str(caught.value)


def test_a_validated_about_validates_again_unchanged():
    created = WorkflowCreate.model_validate(_spec(about=ABOUT))
    again = WorkflowCreate.model_validate(created.model_dump(mode="json"))
    assert again.metadata == created.metadata == {"about": ABOUT, "description_source": "submitter"}
