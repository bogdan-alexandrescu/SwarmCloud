"""The `merge` step at submission: platform default, per job, and the forge (contract request 47).

Owner decisions of 2026-10-04 (#295): the merge is an OPT-IN final step,
configurable as a platform default (`merge_by_default`, served and set by
`/v1/admin/settings`, default off) or per job (`metadata.merge`: "on" |
"off"), defaulting to the platform setting when the job says nothing. When
the effective choice is on, the spec states no merge step and the workflow
opens a pull request, swarm-api appends ONE merge step -- depending on the
step that opens it and on the review when there is one -- BEFORE signing. A
spec's own merge step is honoured unless `metadata.merge` is "off". A host no
`ForgeMerger` serves is refused here, at submission, never at merge time.

No credentials, no network, no emulator: every agent step is `mock`.
"""

from __future__ import annotations

import pytest

from swarm_api.validation import MERGE_STEP_MAX_ATTEMPTS

from .conftest import auth_header

REPO = "https://github.com/octo-org/widget-shop.git"


def _review_shape(**overrides) -> dict:
    """implement -> review -> fix (gated, the integrator), under `integrate`."""
    spec = {
        "strategy": "integrate",
        "repository_url": REPO,
        "steps": [
            {"step_id": "implement", "runner_profile": "mock",
             "input": {"prompt": "implement the change"}},
            {"step_id": "review", "runner_profile": "mock", "depends_on": ["implement"],
             "builds_on": "implement", "input_from": {"implement": "swarm-work.patch"},
             "input": {"prompt": "review; write verdict.json"}},
            {"step_id": "fix", "runner_profile": "mock", "depends_on": ["review"],
             "builds_on": "implement", "input_from": {"review": "verdict.json"},
             "when": {"step": "review", "verdict_in": ["NOT_YET"]},
             "input": {"prompt": "fix every finding"}},
        ],
    }
    spec.update(overrides)
    return spec


def _post(client, spec):
    return client.post("/v1/workflows", headers=auth_header("alice"), json=spec)


def _set_default(client, value: bool):
    response = client.put("/v1/admin/settings", headers=auth_header("root"),
                          json={"merge_by_default": value})
    assert response.status_code == 200, response.text
    return response.json()


def _merge_steps(body: dict) -> list[dict]:
    return [s for s in body["workflow"]["steps"] if s["runner_profile"] == "merge"]


def _task(db, task_id: str) -> dict:
    return db.docs[f"tasks/{task_id}"]


# --------------------------------------------------------------------------
# The admin setting
# --------------------------------------------------------------------------

def test_merge_by_default_is_off_until_an_admin_turns_it_on(client):
    response = client.get("/v1/admin/settings", headers=auth_header("root"))
    assert response.status_code == 200, response.text
    assert response.json()["merge_by_default"] is False
    assert response.json()["settings_document"] == "missing"

    body = _set_default(client, True)
    assert body["merge_by_default"] is True
    assert body["updated_by"] == "root@saga.xyz"
    again = client.get("/v1/admin/settings", headers=auth_header("root")).json()
    assert again["merge_by_default"] is True and again["settings_document"] == "present"


def test_only_an_admin_reads_or_sets_the_platform_settings(client):
    assert client.get("/v1/admin/settings", headers=auth_header("alice")).status_code == 403
    response = client.put("/v1/admin/settings", headers=auth_header("alice"),
                          json={"merge_by_default": True})
    assert response.status_code == 403
    assert client.get("/v1/admin/settings",
                      headers=auth_header("root")).json()["merge_by_default"] is False


def test_a_settings_change_naming_nothing_or_an_unknown_setting_is_refused(client):
    assert client.put("/v1/admin/settings", headers=auth_header("root"),
                      json={}).status_code == 422
    assert client.put("/v1/admin/settings", headers=auth_header("root"),
                      json={"merge_everything": True}).status_code == 422


def test_pausing_dispatch_does_not_erase_the_setting(client):
    _set_default(client, True)
    client.post("/v1/admin/dispatch/pause", headers=auth_header("root"), json={})
    client.post("/v1/admin/dispatch/resume", headers=auth_header("root"), json={})
    assert client.get("/v1/admin/settings",
                      headers=auth_header("root")).json()["merge_by_default"] is True


# --------------------------------------------------------------------------
# Platform default and per job
# --------------------------------------------------------------------------

def test_default_off_and_no_choice_appends_no_merge_step(client):
    response = _post(client, _review_shape())
    assert response.status_code == 201, response.text
    assert _merge_steps(response.json()) == []


def test_default_on_appends_one_merge_step_after_the_integrator_and_the_review(client, db):
    _set_default(client, True)
    response = _post(client, _review_shape())
    assert response.status_code == 201, response.text
    body = response.json()
    (merge,) = _merge_steps(body)
    assert merge["step_id"] == "merge"
    assert merge["depends_on"] == ["fix", "review"]
    assert merge["input_from"] == {"review": "verdict.json"}
    # The integrator is still the fix, and it integrates only the implementer.
    assert body["dispatch"]["integrator_step_id"] == "fix"
    by_id = {s["step_id"]: s for s in body["workflow"]["steps"]}
    fix = _task(db, by_id["fix"]["task_id"])
    assert fix["metadata"]["dispatch"]["integrates"] == [by_id["implement"]["task_id"]]
    # The merge step's signed dispatch block names, by TASK id, the pull
    # request's task and the review, and the verdict file it stages.
    task = _task(db, merge["task_id"])
    block = task["metadata"]["dispatch"]
    assert block["merge_target"] == {
        "pull_request": by_id["fix"]["task_id"],
        "review": by_id["review"]["task_id"],
        "verdict_file": "verdict.json",
    }
    assert "role" not in block, "the merge step was given an integrate role"
    assert task["metadata"]["input_from"] == {by_id["review"]["task_id"]: "verdict.json"}
    assert task["runner_profile"] == "merge"
    assert task["provider"] == "git"
    assert task["max_attempts"] == MERGE_STEP_MAX_ATTEMPTS
    # Inside `metadata.dispatch`, which the step-spec signature covers.
    from swarm_common.specsign import SIGNED_METADATA_KEYS

    assert "dispatch" in SIGNED_METADATA_KEYS


def test_default_on_but_the_job_says_off_appends_none(client):
    _set_default(client, True)
    response = _post(client, _review_shape(metadata={"merge": "off"}))
    assert response.status_code == 201, response.text
    assert _merge_steps(response.json()) == []


def test_default_off_but_the_job_says_on_appends_one(client):
    response = _post(client, _review_shape(metadata={"merge": "on"}))
    assert response.status_code == 201, response.text
    (merge,) = _merge_steps(response.json())
    assert merge["depends_on"] == ["fix", "review"]


def test_a_stated_merge_step_is_honoured_when_the_job_says_nothing(client):
    spec = _review_shape()
    spec["steps"].append({"step_id": "land", "runner_profile": "merge",
                          "depends_on": ["fix", "review"],
                          "input_from": {"review": "verdict.json"}})
    response = _post(client, spec)
    assert response.status_code == 201, response.text
    assert [s["step_id"] for s in _merge_steps(response.json())] == ["land"]


def test_a_stated_merge_step_with_the_job_saying_off_is_refused(client):
    spec = _review_shape(metadata={"merge": "off"})
    spec["steps"].append({"step_id": "land", "runner_profile": "merge",
                          "depends_on": ["fix", "review"],
                          "input_from": {"review": "verdict.json"}})
    response = _post(client, spec)
    assert response.status_code == 422, response.text
    assert "'off'" in response.json()["message"]


def test_a_stated_merge_step_with_the_job_saying_on_is_not_doubled(client):
    spec = _review_shape(metadata={"merge": "on"})
    spec["steps"].append({"step_id": "land", "runner_profile": "merge",
                          "depends_on": ["fix", "review"],
                          "input_from": {"review": "verdict.json"}})
    response = _post(client, spec)
    assert response.status_code == 201, response.text
    assert len(_merge_steps(response.json())) == 1


def test_a_stated_merge_step_missing_the_review_is_refused(client):
    spec = _review_shape()
    spec["steps"].append({"step_id": "land", "runner_profile": "merge", "depends_on": ["fix"]})
    response = _post(client, spec)
    assert response.status_code == 422, response.text
    assert "review" in response.json()["message"]


def test_a_merge_step_something_depends_on_is_refused(client):
    spec = _review_shape()
    spec["steps"].append({"step_id": "land", "runner_profile": "merge",
                          "depends_on": ["fix", "review"],
                          "input_from": {"review": "verdict.json"}})
    spec["steps"].append({"step_id": "after", "runner_profile": "mock", "depends_on": ["land"]})
    response = _post(client, spec)
    assert response.status_code == 422, response.text


def test_a_one_step_direct_pr_workflow_gets_a_merge_step_on_that_step(client, db):
    spec = {"strategy": "direct-pr", "repository_url": REPO, "metadata": {"merge": "on"},
            "steps": [{"step_id": "work", "runner_profile": "mock", "input": {"prompt": "x"}}]}
    response = _post(client, spec)
    assert response.status_code == 201, response.text
    (merge,) = _merge_steps(response.json())
    assert merge["depends_on"] == ["work"]
    assert merge["input_from"] == {}
    by_id = {s["step_id"]: s for s in response.json()["workflow"]["steps"]}
    block = _task(db, merge["task_id"])["metadata"]["dispatch"]
    assert block["merge_target"] == {"pull_request": by_id["work"]["task_id"]}


def test_a_direct_pr_workflow_of_several_prs_saying_on_is_refused(client):
    spec = {"strategy": "direct-pr", "repository_url": REPO, "metadata": {"merge": "on"},
            "steps": [{"step_id": "a", "runner_profile": "mock"},
                      {"step_id": "b", "runner_profile": "mock"}]}
    response = _post(client, spec)
    assert response.status_code == 422, response.text
    assert "integrate" in response.json()["message"]


def test_the_default_does_not_apply_to_a_workflow_that_opens_no_single_pull_request(client):
    _set_default(client, True)
    several = {"strategy": "direct-pr", "repository_url": REPO,
               "steps": [{"step_id": "a", "runner_profile": "mock"},
                         {"step_id": "b", "runner_profile": "mock"}]}
    collect = {"steps": [{"step_id": "a", "runner_profile": "mock"}]}
    for spec in (several, collect):
        response = _post(client, spec)
        assert response.status_code == 201, response.text
        assert _merge_steps(response.json()) == []


def test_collect_saying_on_is_refused(client):
    response = _post(client, {"metadata": {"merge": "on"},
                              "steps": [{"step_id": "a", "runner_profile": "mock"}]})
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("value", ["yes", True, 1, "ON"])
def test_a_merge_choice_that_is_not_on_or_off_is_refused(client, value):
    response = _post(client, _review_shape(metadata={"merge": value}))
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["accepted"] == ["on", "off"]


# --------------------------------------------------------------------------
# The forge, refused at submission
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://gitlab.com/octo-org/widget-shop.git",
    "https://github.example.com/octo-org/widget-shop.git",
    "https://github.com:8443/octo-org/widget-shop.git",
])
def test_an_unsupported_forge_host_is_refused_at_submission(client, db, url):
    before = len([k for k in db.docs if k.startswith("tasks/")])
    response = _post(client, _review_shape(repository_url=url, metadata={"merge": "on"}))
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["supported_forge_hosts"] == ["github.com", "www.github.com"]
    assert len([k for k in db.docs if k.startswith("tasks/")]) == before, "a task was written"


def test_an_unsupported_forge_with_a_stated_merge_step_is_refused_too(client):
    spec = _review_shape(repository_url="https://gitlab.com/o/r.git")
    spec["steps"].append({"step_id": "land", "runner_profile": "merge",
                          "depends_on": ["fix", "review"],
                          "input_from": {"review": "verdict.json"}})
    response = _post(client, spec)
    assert response.status_code == 422, response.text
    assert "github.com" in response.json()["message"]


def test_an_unsupported_forge_without_a_merge_is_accepted_as_before(client):
    response = _post(client, _review_shape(repository_url="https://gitlab.com/o/r.git"))
    assert response.status_code == 201, response.text


# --------------------------------------------------------------------------
# The profile, and where it may run
# --------------------------------------------------------------------------

def test_a_merge_task_on_its_own_is_refused_as_misplaced_not_disabled(client):
    response = client.post("/v1/tasks", headers=auth_header("alice"),
                           json={"runner_profile": "merge", "input": {}})
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail.get("disabled") is not True
    assert detail["merge_strategies"] == ["direct-pr", "integrate"]


@pytest.mark.parametrize("url", [
    "https://gitlab.com/octo-org/widget-shop.git",
    "https://github.example.com/octo-org/widget-shop.git",
])
def test_the_platform_default_appends_nothing_on_a_host_no_merger_serves(client, url):
    """Review of the merge step: an admin turning the default on must not 422
    every tenant on another host who never asked for a merge. The worker
    harvests a patch there and opens no pull request, so there is nothing to
    merge; an explicit "on" or a stated step is still refused (above)."""
    _set_default(client, True)
    response = _post(client, _review_shape(repository_url=url))
    assert response.status_code == 201, response.text
    assert _merge_steps(response.json()) == []
    one = _post(client, {"strategy": "direct-pr", "repository_url": url,
                         "steps": [{"step_id": "only", "runner_profile": "mock",
                                    "input": {"prompt": "change it"}}]})
    assert one.status_code == 201, one.text
    assert _merge_steps(one.json()) == []


# --------------------------------------------------------------------------
# A merge-only continuation: how an issue run merges once CI is green
# --------------------------------------------------------------------------

def _direct_pr_task(client, user: str = "alice") -> str:
    response = client.post("/v1/tasks", headers=auth_header(user), json={
        "runner_profile": "mock", "strategy": "direct-pr", "repository_url": REPO,
    })
    assert response.status_code == 201, response.text
    return response.json()["task"]["id"]


def test_a_merge_only_continuation_targets_the_continued_task_by_id(client, db):
    continued = _direct_pr_task(client)
    response = _post(client, {
        "strategy": "direct-pr", "continues_task": continued,
        "steps": [{"step_id": "merge", "runner_profile": "merge"}],
    })
    assert response.status_code == 201, response.text
    (merge,) = _merge_steps(response.json())
    assert merge["depends_on"] == []
    task = _task(db, merge["task_id"])
    assert task["metadata"]["dispatch"]["merge_target"] == {"pull_request": continued}
    assert task["provider"] == "git"
    assert task["max_attempts"] == MERGE_STEP_MAX_ATTEMPTS


def test_a_continuation_of_no_step_but_a_non_merge_one_is_still_one_step(client):
    continued = _direct_pr_task(client)
    response = _post(client, {
        "strategy": "direct-pr", "continues_task": continued,
        "steps": [{"step_id": "a", "runner_profile": "mock", "input": {"prompt": "x"}},
                  {"step_id": "b", "runner_profile": "mock", "input": {"prompt": "y"}}],
    })
    assert response.status_code == 422, response.text


def test_a_merge_only_continuation_on_another_tenants_task_is_refused(client):
    continued = _direct_pr_task(client, user="bob")
    response = _post(client, {
        "strategy": "direct-pr", "continues_task": continued,
        "steps": [{"step_id": "merge", "runner_profile": "merge"}],
    })
    assert response.status_code == 422, response.text
    assert "not a task in your tenant" in response.json()["message"]
