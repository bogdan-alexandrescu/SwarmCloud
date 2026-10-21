"""A workflow spec's `after` reaches swarm-api, and `sc workflows` says what it waits for (SPEED-D3).

`after: {"pr": N}` (or a list of them) keeps the whole workflow PARKED until
each named pull request of the workflow's own repository has merged
(tests/unit/control_plane/test_workflow_after_pr.py holds the platform's
half). The bridge's spec reader refuses an unknown key, so until `after` was
one a spec carrying it was refused at the keyboard. Here:

  * `after` is a spec key; its shape -- one `{"pr": <int >= 1>}` or a
    non-empty list of them -- is checked here, so a typo is refused before
    anything is sent, and it is sent as the top-level `after` the API takes;
  * a spec without it sends none;
  * `sc workflows` prints the API's `after_pull_requests.text` under a
    workflow that is waiting, "waiting for PR #41 to merge".
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

from swarm_mcp import sc, workflows
from swarm_mcp.client import SwarmError


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict | None]] = []

    def request(self, method: str, path: str, payload=None, **_: object) -> dict:
        self.sent.append((method, path, payload))
        return {"workflow": {"workflow_id": "wf_1", "steps": []},
                "dispatch": {"strategy": "direct-pr"}}


SPEC = {
    "strategy": "direct-pr",
    "steps": [{"step_id": "implement", "prompt": "implement"}],
}


def _sent(spec: dict) -> dict:
    read = workflows.read_spec(spec)
    recorder = _Recorder()
    workflows.submit(recorder, steps=read["steps"], strategy=read["strategy"], after=read["after"])
    ((_method, _path, payload),) = recorder.sent
    return payload


def test_after_is_a_spec_key():
    assert "after" in workflows.SPEC_KEYS


def test_one_pull_request_is_sent_as_after():
    assert _sent({**SPEC, "after": {"pr": 41}})["after"] == [{"pr": 41}]


def test_a_list_of_pull_requests_is_sent_as_after():
    assert _sent({**SPEC, "after": [{"pr": 41}, {"pr": 42}]})["after"] == [{"pr": 41}, {"pr": 42}]


def test_a_spec_without_after_sends_none():
    assert "after" not in _sent(SPEC)


@pytest.mark.parametrize("after", [
    41, "41", {"pr": 0}, {"pr": "41"}, {"pr": True}, {"number": 41}, {"pr": 41, "repo": "a/b"},
    [], [41], [{"pr": 41}, "x"],
])
def test_a_malformed_after_is_refused_before_anything_is_sent(after):
    with pytest.raises(SwarmError) as caught:
        workflows.read_spec({**SPEC, "after": after})
    assert "after" in str(caught.value)


def test_sc_workflows_says_which_pull_request_a_workflow_waits_for(monkeypatch):
    entry = {
        "workflow_id": "wf_gated", "label": None, "state": "PARKED", "age_seconds": 60,
        "steps_total": 2, "current_steps": [],
        "waiting_for": "waiting for PR #41 to merge",
    }
    monkeypatch.setattr(sc, "running_workflows", lambda client, **_: {
        "workflows": [entry], "complete": True, "tenant_id": "eng"})
    monkeypatch.setattr(sc, "running_tasks", lambda client: {"tasks": [], "complete": True})
    out = io.StringIO()
    args = SimpleNamespace(json=False, width=200, color="never", ascii=True, session_start=False)

    sc.cmd_workflows(object(), args, out)

    assert "waiting for PR #41 to merge" in out.getvalue()


def test_the_wait_is_read_off_the_apis_workflow():
    workflow = {"after_pull_requests": {"state": "waiting", "pending": [41],
                                        "text": "waiting for PR #41 to merge"}}
    assert sc.pull_request_wait(workflow) == "waiting for PR #41 to merge"
    assert sc.pull_request_wait({"after_pull_requests": {"state": "merged", "text": "PR #41 merged"}}) is None
    assert sc.pull_request_wait({}) is None


def test_the_bridges_bound_is_the_apis():
    from swarm_api.schemas import MAX_AFTER_PRS

    assert workflows.MAX_AFTER_PRS == MAX_AFTER_PRS
