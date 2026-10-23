"""A CI-fix round runs with its lane's declared territory (CIFIX-TERRITORY, part of #1499).

Measured 2026-10-17 on PR 1485 (lane ISO-WORKER-CORE): a PR-side CI-fix
round edited `apps/swarm-mcp/swarm_mcp/sc.py`, outside the lane, and turned
`key set`'s refusal of a key typed on the command line into exit 0. CI caught
it. The fixer had been told nothing about the lane's territory, and its task
declared none, so the worker's TERRITORY-WARN check (#1468) had nothing to
compare its push with.

BGD's decision for lanes (#1468, 2026-10-19) is a loud warning, never a
refusal, and it holds for a round too. So `cifix.stamp` copies the lane's
`inputs.territory` -- read from the lane's own task record, never from the
failing log or anything an agent wrote -- onto the round's step, and the
round's prompt names it and says to ask in questions.json rather than edit
outside it. The worker's publish then warns on the round's push as it does on
a lane's.

No credentials, no network, no emulator.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_api import cifix, laneterritory
from swarm_api.continuation import Continuation

from .conftest import auth_header
from .test_ci_fix_postback import (  # noqa: F401 - fixtures
    HEAD,
    PR,
    REPO_URL,
    _fix_task,
    _members_hold_grants,
    _original,
    _red_run_payload,
    _submit,
    clock,
    fix_client,
    github,
    tenant_tokens,
)

TERRITORY = ["apps/agent-worker/agent_worker/lifecycle.py", "tests/unit/worker/"]
OUTSIDE = "apps/swarm-mcp/swarm_mcp/sc.py"


def _lane(client, territory: list[str] | None) -> str:
    """A `direct-pr` lane task whose `swarm/<id>` pull request went red,
    declaring `territory` in its input (none when None)."""
    payload: dict[str, Any] = {
        "runner_profile": "mock", "strategy": "direct-pr", "repository_url": REPO_URL,
    }
    if territory is not None:
        payload["input"] = {"prompt": "the lane's brief", "territory": territory}
    response = client.post("/v1/tasks", headers=auth_header("alice"), json=payload)
    assert response.status_code == 201, response.text
    return response.json()["task"]["id"]


def _round_payload(continues: str, key: str, *, prompt: str = "fix it",
                   step_input: dict[str, Any] | None = None) -> dict[str, Any]:
    """A merge step's fix round, in the shape mergewake submits it."""
    return {
        "steps": [{"step_id": "fix", "runner_profile": "mock",
                   "input": {"prompt": prompt, **(step_input or {})}}],
        "strategy": "direct-pr",
        "continues_task": continues,
        "metadata": {key: {"merge_task": "task_" + "a" * 20, "round": 1,
                           "head_sha": HEAD, "pull_request": PR}},
    }


def _round(fix_client, db, payload: dict[str, Any]) -> dict[str, Any]:
    response = _submit(fix_client, payload)
    assert response.status_code == 201, response.text
    return _fix_task(db, response)


@pytest.mark.parametrize("kind", ["ci_fix", "merge_fix_round"])
def test_a_round_on_a_lane_that_declares_a_territory_runs_with_the_same_one(fix_client, db, kind):
    lane = _lane(fix_client, TERRITORY)
    payload = _red_run_payload(lane) if kind == "ci_fix" else _round_payload(lane, kind)

    task = _round(fix_client, db, payload)

    # MUTATION: leave `stamp` copying nothing -- the round's task declares no
    # territory, and the worker's warning has nothing to compare its push with
    # (PR 1485's `sc.py` edit published with no label and no list).
    assert task["input"].get("territory") == TERRITORY, task["input"]
    # MUTATION: copy the territory onto the workflow's metadata instead of the
    # step's input -- the worker reads `task["input"]` only.
    assert "territory" not in (task.get("metadata") or {})


def test_a_round_on_a_lane_with_no_territory_declares_none(fix_client, db):
    lane = _lane(fix_client, None)

    task = _round(fix_client, db, _red_run_payload(lane))

    # MUTATION: give a round with no lane territory an empty list or `["**"]`
    # -- the first is refused at submit, the second hides every later warning.
    assert "territory" not in task["input"], task["input"]
    # MUTATION: append the territory paragraph even with no territory -- a
    # round with nothing to keep inside is told about a list that is not there.
    assert task["input"]["prompt"] == "CI failed. Fix exactly this failure."


def test_the_territory_is_the_lanes_even_when_the_failing_log_names_other_paths(fix_client, db):
    lane = _lane(fix_client, TERRITORY)
    payload = _red_run_payload(lane)
    payload["steps"][0]["input"]["prompt"] = (
        "CI failed. Fix exactly this failure.\n"
        f"FAILED tests/unit/mcp/test_provider_keys_bridge.py - SystemExit: 0\n  at {OUTSIDE}:212\n"
    )

    task = _round(fix_client, db, payload)

    # MUTATION: build the territory from the paths the log names -- `sc.py`
    # becomes the round's own territory and its edit is never flagged.
    assert task["input"]["territory"] == TERRITORY
    assert not any(OUTSIDE in entry for entry in task["input"]["territory"])


def test_a_round_cannot_widen_its_lanes_territory(fix_client, db):
    lane = _lane(fix_client, TERRITORY)
    payload = _round_payload(lane, "merge_fix_round", step_input={"territory": ["**"]})

    task = _round(fix_client, db, payload)

    # MUTATION: keep a caller's territory over the lane's -- a fixer that
    # sends `**` publishes anything without a warning.
    assert task["input"]["territory"] == TERRITORY


def test_another_tenants_lane_record_is_never_read_for_a_territory(fix_client, db):
    lane = _lane(fix_client, TERRITORY)
    continuation = Continuation(root_task_id=lane, repository_url=REPO_URL)
    assert cifix.lane_territory(db, "eng", continuation) == TERRITORY

    # MUTATION: drop the tenant check on the lane's record -- one tenant's
    # territory (its file layout) is copied into another's task.
    assert cifix.lane_territory(db, "other", continuation) is None


def test_the_rounds_prompt_names_the_territory_and_the_ask_instead_rule(fix_client, db):
    lane = _lane(fix_client, TERRITORY)

    task = _round(fix_client, db, _red_run_payload(lane))
    prompt = task["input"]["prompt"]

    # MUTATION: replace the prompt instead of appending to it -- the fixer
    # loses the failure it was sent to fix.
    assert prompt.startswith("CI failed. Fix exactly this failure.")
    # MUTATION: leave an entry out of the prompt -- the fixer is held to a
    # territory it was never shown.
    for entry in TERRITORY:
        assert f"- `{entry}`" in prompt, prompt
    # MUTATION: drop the ask-instead rule -- a fixer whose failure's cause is
    # outside edits it, as on PR 1485.
    assert "questions.json" in prompt
    assert "outside" in prompt.lower()
    # MUTATION: name another label than the one the worker sets.
    assert laneterritory.OUT_OF_TERRITORY_LABEL in prompt


def test_the_round_brief_is_one_block_naming_every_entry():
    brief = laneterritory.round_brief(TERRITORY)
    # MUTATION: list the entries without their code spans -- a `*` in a glob
    # renders as emphasis and the fixer reads another path.
    assert [line for line in brief.splitlines() if line.startswith("- ")] == [
        f"- `{entry}`" for entry in TERRITORY
    ]
    assert "questions.json" in brief


def test_a_conflict_round_is_left_without_a_territory(fix_client, db):
    """A conflict round merges the base into the branch: its push changes every
    path the base changed since the branch left it, so the lane's territory
    would flag main's own work on every conflict round."""
    lane = _lane(fix_client, TERRITORY)

    task = _round(fix_client, db, _round_payload(lane, "merge_conflict_round"))

    # MUTATION: copy the territory onto every ROUND_KINDS round -- each
    # conflict round's pull request is labelled for files main changed.
    assert "territory" not in task["input"]
    assert task["metadata"][cifix.FIX_HEAD_FIELD]["kind"] == "merge_conflict_round"
