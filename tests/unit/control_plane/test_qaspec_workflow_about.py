"""A quality-check run says which leg it is and what it does (BGD, 2026-11-04).

Every leg of one target was titled `Visual QA: <target>`, so in the console
the reviewer and moderator legs read alike, and none said what it was meant
to deliver. Each leg is now titled with its role and environment (`Visual QA:
ai-studio, moderator, staging`), and carries `metadata.description` -- the
target, the leg's role and environment, how many capture groups, what the
report is -- and `metadata.feature`, `Quality check of <target>`, which the
API accepts as any submitter's (`schemas.check_workflow_about`).

Offline: `qaspec` and `qatargets.render_run`, pure.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_api import qaspec
from swarm_api.qatargets import render_run
from swarm_api.schemas import DESCRIPTION_MAX_CHARS, WorkflowCreate


def _login() -> dict[str, Any]:
    return {"kind": "own_login", "login_path": "/", "signed_in_selector": "nav .me",
            "start_clicks": [{"text": "Sign in"}]}


def _target(name: str = "ai-studio") -> dict[str, Any]:
    return {
        "id": "qat_3f9a", "revision": 4, "name": name, "tenant_id": "tenant-a",
        "repository": "https://github.com/example-org/studio",
        "screens": [{"id": "home", "path": "/"}, {"id": "pricing", "path": "/pricing"}],
        "environments": [
            {"name": "stg-reviewer", "base_url": "https://staging.example.com", "environment": "non_production",
             "sign_in": _login()},
            {"name": "stg-moderator", "base_url": "https://staging.example.com", "environment": "non_production",
             "sign_in": _login()},
            {"name": "production", "base_url": "https://studio.example.com", "environment": "production",
             "sign_in": _login()},
        ],
    }


def _leg(name: str, target: dict[str, Any] | None = None) -> dict[str, Any]:
    return qaspec.leg(target or _target(), name)


@pytest.mark.parametrize(("leg", "title"), [
    ("stg-moderator", "Visual QA: ai-studio, moderator, staging"),
    ("stg-reviewer", "Visual QA: ai-studio, reviewer, staging"),
    ("production", "Visual QA: ai-studio, production"),
])
def test_each_leg_is_titled_with_its_role_and_environment(leg, title):
    assert qaspec.render(_leg(leg), file="issues")["title"] == title


def test_a_target_that_is_its_own_one_leg_names_its_environment():
    flat = {k: v for k, v in _target().items() if k != "environments"}
    flat.update(base_url="https://staging.example.com", environment="non_production", sign_in={"kind": "none"})
    assert qaspec.render(flat, file="none")["title"] == "Visual QA: ai-studio, staging"


def test_two_legs_of_one_target_read_apart():
    titles = {qaspec.render(_leg(n), file="none")["title"] for n in ("stg-reviewer", "stg-moderator")}
    descriptions = {qaspec.render(_leg(n), file="none")["description"] for n in ("stg-reviewer", "stg-moderator")}
    assert len(titles) == 2 and len(descriptions) == 2


def test_a_leg_carries_a_description_and_its_feature():
    spec = qaspec.render(_leg("stg-moderator"), file="issues")
    description = spec["description"]
    assert description.startswith("Visual QA of ai-studio signed in as moderator on staging.")
    assert "1 capture group screenshot its screens" in description
    assert "files the new ones as GitHub issues" in description
    assert spec["feature"] == {"title": "Quality check of ai-studio"}
    assert "nothing is filed" in qaspec.render(_leg("stg-moderator"), file="none")["description"]


def test_a_discovery_run_is_titled_and_described_by_its_leg():
    target = _target()
    target["environments"][1]["discover"] = {"max_pages": 50, "max_depth": 3}
    target["discover"] = {"max_pages": 50, "max_depth": 3}
    spec = qaspec.render_discovery(_leg("stg-moderator", target))
    assert spec["title"] == "Visual QA discovery: ai-studio, moderator, staging"
    assert "lists the same-origin screens" in spec["description"]
    assert spec["feature"] == {"title": "Quality check of ai-studio"}


def test_the_run_body_carries_them_and_the_api_accepts_it():
    body, _mark, _browser = render_run(_leg("stg-moderator"), screens=None, file="none")
    metadata = body["metadata"]
    assert metadata["title"] == "Visual QA: ai-studio, moderator, staging"
    assert metadata["feature"] == {"title": "Quality check of ai-studio"}
    created = WorkflowCreate.model_validate(body)
    assert created.metadata["description"] == metadata["description"]
    assert created.metadata["description_source"] == "submitter"


def test_a_target_name_with_markup_still_renders_a_description_the_api_accepts():
    # The API refuses markup in a description; the target's name is spaced
    # out of it there, and kept as written in the title and the feature.
    name = "a" * 80 + " <b>__x__</b> [y](z)"
    body, _mark, _browser = render_run(_leg("stg-moderator", _target(name[:100])), screens=None, file="none")
    created = WorkflowCreate.model_validate(body)
    assert len(created.metadata["description"]) <= DESCRIPTION_MAX_CHARS
    assert created.metadata["feature"]["title"] == f"Quality check of {name[:100]}"
