"""A name fits one row of the console (owner, 2026-11-07, part of #2688).

Measured at 1440 pixels on fixture data, the console's Workflows list holds
34 characters of a name, its Agents list 71: a name holds at most
`workflows.NAME_MAX_CHARS` (30), swarm-api's `schemas.NAME_MAX_CHARS`. The
plugin refuses -- never cuts -- a longer workflow spec `title` or `label`,
or a single task's `label`, before anything is sent, in one fixed sentence
naming the cap and saying the detail belongs in `description`. The run title
`SC · <name> · N steps` cuts an older stored name at the same cap.
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest

from swarm_api import schemas
from swarm_mcp import launch, server, workflows
from swarm_mcp.client import SwarmError, task_payload

CAP = workflows.NAME_MAX_CHARS
LONG = "n" * (CAP + 1)
STEP = {"step_id": "build", "runner_profile": "claude-code", "input": {"prompt": "build it"}}
SPEC_STEP = {"step_id": "build", "runner_profile": "claude-code", "prompt": "build it"}


def _sentence(field: str, where: str) -> str:
    return (f"{where}: {field} is {CAP + 1} characters and a name holds at most {CAP}, so it fits "
            "one row of the console; put the detail in `description`. Nothing was sent")


class RecordingApi:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, Any]] = []

    def request(self, method: str, path: str, *, payload: Any = None, timeout: int = 60) -> Any:
        self.sent.append((method, path, payload))
        if path == "/v1/stats":
            return {"limits": {"max_batch_size": 5}}
        raise SwarmError(f"{method} {path} -> 404: no such route", status=404)

    def dispatch(self, **fields: Any) -> dict[str, Any]:
        self.sent.append(("POST", "/v1/tasks", task_payload(**fields)))
        return {"id": f"task_{len(self.sent)}", "state": "QUEUED"}

    def dispatch_batch(self, payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.sent.append(("POST", "/v1/tasks/batch", {"tasks": list(payloads)}))
        return [{"id": f"task_b{i}", "state": "QUEUED"} for i, _ in enumerate(payloads, start=1)]


def test_the_cap_is_the_measured_one_and_swarm_apis():
    assert CAP == 30 == schemas.NAME_MAX_CHARS


@pytest.mark.parametrize("field", ["title", "label"])
def test_a_spec_name_over_the_cap_is_refused_in_the_fixed_sentence(field):
    with pytest.raises(SwarmError) as caught:
        workflows.read_spec({"steps": [SPEC_STEP], field: LONG})
    assert str(caught.value) == _sentence(field, "the workflow spec")


@pytest.mark.parametrize("field", ["title", "label"])
def test_submit_refuses_a_long_name_before_sending(field):
    api = RecordingApi()
    with pytest.raises(SwarmError, match=f"a name holds at most {CAP}"):
        workflows.submit(api, steps=[STEP], **{field: LONG})
    assert not api.sent


def test_a_name_at_the_cap_is_kept_whole():
    spec = workflows.read_spec({"steps": [SPEC_STEP], "title": "t" * CAP, "label": "l" * CAP})
    assert spec["title"] == "t" * CAP and spec["label"] == "l" * CAP


def test_a_single_tasks_label_over_the_cap_is_refused_before_any_request():
    api = RecordingApi()
    prompt = "summarise the source " + secrets.token_hex(6)
    with pytest.raises(SwarmError) as caught:
        server._call(api, "swarm_dispatch", {"prompt": prompt, "label": LONG})
    assert str(caught.value) == _sentence("label", "swarm_dispatch")
    with pytest.raises(SwarmError, match=f"a name holds at most {CAP}"):
        server._call(api, "swarm_dispatch", {"tasks": [{"prompt": prompt, "label": LONG}]})
    assert not [p for _, p, _ in api.sent if p.startswith("/v1/tasks")]


def test_the_run_title_cuts_an_older_name_at_the_cap():
    stored = " ".join(["headline"] * 10)
    title = workflows.workflow_title(stored, 3)
    name = title[len(workflows.TITLE_PREFIX):-len(" · 3 steps")]
    assert len(name) <= CAP and name.endswith("…")
    run = launch.workflow_run_title(stored, 3)
    assert len(run[len("SC ∴ "):-len(" · 3 steps")]) <= CAP
    assert launch.workflow_run_title("t" * CAP, 3) == f"SC ∴ {'t' * CAP} · 3 steps"
