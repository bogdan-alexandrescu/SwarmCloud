"""The worker tells the runner, per step, whether it publishes and which
platform servers its agent is handed (ONE-PLACE; the owner's picks on archived pull request 3113,
`apply-worker-patch` and `explicit-list`).

`Worker._build_child_env` sets, on every runner start and never from `input`:

1. `SWARM_STEP_PUBLISHES`: "1" for a repository step whose branch is its
   deliverable (`_deliverable_branch`), "0" for one whose branch is not, and
   nothing for a step with no repository;
2. `SWARM_STEP_MCP_SERVERS`: the swarm-git / swarm-state list
   `stepservers.step_servers` reads from the signed step facts -- "none" for a
   build step, both for a review, a CI fix or a step onto a pull request.

And the runner registers swarm-git and swarm-state from that list only, every
offered server when the variable is unset (tests/unit/worker/
test_runner_instructions_once.py holds the runner's side).

MUTATIONS: drop either assignment from `_build_child_env` (1, 2); read
`_opens_pull_request` instead of `_deliverable_branch` (the contributor case);
drop a fact from `step_servers` (its parametrized case fails).
"""

from __future__ import annotations

import secrets

import pytest

from agent_worker import lifecycle, publishpr, stepservers
from agent_worker import workspace as workspace_mod
from agent_worker.runners import cliagent

from worker_seeds import seed_attempt

GIT = stepservers.GIT_SERVER
STATE = stepservers.STATE_SERVER


def _env(db, worker_factory, tmp_path, monkeypatch, *, task, repo=True, deliverable=""):
    seed_attempt(db, runner_profile="mock")
    worker, _, _ = worker_factory()
    worker.ws = workspace_mod.create(tmp_path / "ws", "att_1")
    worker._task = task
    worker._repo_url = "https://github.com/acme/widgets" if repo else None
    monkeypatch.setattr(lifecycle.Worker, "_deliverable_branch", lambda self: deliverable)
    return worker._build_child_env()


def test_the_publishes_name_is_the_runners():
    assert lifecycle.STEP_PUBLISHES_ENV == cliagent.STEP_PUBLISHES_ENV


@pytest.mark.parametrize("deliverable, expected", [
    ("the step was to open a pull request", "1"),
    ("the step is an 'integrate' contributor", "1"),
    ("", "0"),
])
def test_a_repository_step_is_told_whether_it_publishes(db, worker_factory, tmp_path, monkeypatch,
                                                        deliverable, expected):
    env = _env(db, worker_factory, tmp_path, monkeypatch, task={}, deliverable=deliverable)
    assert env[cliagent.STEP_PUBLISHES_ENV] == expected
    assert cliagent.step_publishes(env) is (expected == "1")


def test_a_step_with_no_repository_is_not_told(db, worker_factory, tmp_path, monkeypatch):
    env = _env(db, worker_factory, tmp_path, monkeypatch, task={}, repo=False)
    assert cliagent.STEP_PUBLISHES_ENV not in env


def test_a_build_step_is_handed_neither_server(db, worker_factory, tmp_path, monkeypatch):
    env = _env(db, worker_factory, tmp_path, monkeypatch,
               task={"step_id": "implement", "metadata": {"dispatch": {"strategy": "direct-pr"}}})
    assert env[stepservers.ENV] == stepservers.NONE
    assert stepservers.parse(env) == frozenset()


def test_a_review_step_is_handed_both(db, worker_factory, tmp_path, monkeypatch):
    env = _env(db, worker_factory, tmp_path, monkeypatch, task={"step_id": "review", "metadata": {}})
    assert stepservers.parse(env) == {GIT, STATE}


@pytest.mark.parametrize("facts, expected", [
    ({"step_id": "implement"}, ()),
    ({"step_id": "review"}, (GIT, STATE)),
    ({"step_id": "dod-data-rereview-2"}, (GIT, STATE)),
    ({"step_id": "fix", "metadata": {"expected_outputs": ["verdict.json"]}}, (GIT, STATE)),
    ({"dispatch": {"ci_fix_head": {"pull_request": 7, "head_sha": "a" * 40}}}, (GIT, STATE)),
    ({"onto_pull_request": True}, (GIT, STATE)),
    ({"dispatch": {"conflict_round": True}}, (GIT,)),
    ({"pr_role": "reader"}, (GIT,)),
    ({"pr_role": "amender"}, (GIT,)),
    ({"pr_role": "author"}, ()),
    ({"step_id": "supervisor"}, (STATE,)),
    ({"step_id": "review-ui"}, ()),
])
def test_step_servers(facts, expected):
    args = {"step_id": None, "metadata": None, "dispatch": {}, "pr_role": "",
            "onto_pull_request": False, **facts}
    assert stepservers.step_servers(**args) == expected


def test_render_and_parse_round_trip():
    assert stepservers.render(()) == stepservers.NONE
    assert stepservers.parse({stepservers.ENV: stepservers.render((GIT, STATE))}) == {GIT, STATE}
    assert stepservers.parse({}) is None


# -- SWARM_PUBLISH_ONTO_PR (issue 472, task_3162ecbd, 2026-11-25) -------------
#
# A step that publishes onto an existing pull request's branch is refused at
# publish when its change touches .github/workflows or .github/actions; the
# worker tells the runner it is such a step, so the agent can be told before
# its turn ends. MUTATION: drop the assignment -> the first test fails; set it
# for every repository step -> the controls fail.

#: A signed `publish_to_pr`, its task id built at runtime.
ONTO = {"number": 356, "task_id": "task_" + secrets.token_hex(10)}


def _dispatch(**block):
    return {"step_id": "fix", "metadata": {"dispatch": block}}


def test_the_onto_pr_name_is_the_one_issue_472_names():
    assert publishpr.ONTO_PR_ENV == "SWARM_PUBLISH_ONTO_PR"


def test_a_step_that_opens_the_pull_request_onto_one_is_told(
    db, worker_factory, tmp_path, monkeypatch
):
    env = _env(db, worker_factory, tmp_path, monkeypatch,
               task=_dispatch(strategy="direct-pr", publish_to_pr=ONTO))
    assert env[publishpr.ONTO_PR_ENV] == "1"


@pytest.mark.parametrize("task, repo", [
    (_dispatch(strategy="direct-pr"), True),
    (_dispatch(strategy="single-pr", pr_role="author", publish_to_pr=ONTO), True),
    (_dispatch(strategy="integrate", role="contributor", publish_to_pr=ONTO), True),
    (_dispatch(strategy="direct-pr", publish_to_pr={"number": 0, "task_id": "x"}), True),
    (_dispatch(strategy="direct-pr", publish_to_pr=ONTO), False),
], ids=["no-publish-to-pr", "single-pr-role", "contributor", "unsignable", "no-repository"])
def test_every_other_step_is_not_told(db, worker_factory, tmp_path, monkeypatch, task, repo):
    env = _env(db, worker_factory, tmp_path, monkeypatch, task=task, repo=repo)
    assert publishpr.ONTO_PR_ENV not in env
