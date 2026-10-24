"""`swarm_dispatch`'s single-task form passes `publish_to_pr` through (PTP-DISPATCH, part of #2183).

PR 2197 taught swarm-api's `TaskCreate` a top-level `publish_to_pr`: one
task's commits are pushed onto an existing pull request's branch instead of
opening a replacement. BGD's answer of 2026-10-24 to PTP-PLUGIN's owner
question: "Add it to swarm_dispatch". The single-task form takes it in every
form `parse_pull_request` reads and sends it top-level in the `POST /v1/tasks`
body, only when given; the `tasks` form refuses it, because one pull request
takes one publishing task. Every check of the pull request itself (open, not a
fork, a branch this tenant published, `direct-pr`) stays the API's.

Every test drives `server._call` through the REAL `SwarmClient.dispatch` and
`task_payload` against a recorder of the wire, so "sent top-level" and
"nothing was sent" are measurements of the body.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from swarm_mcp import server
from swarm_mcp.client import SwarmClient, SwarmError

REPO = "https://github.com/octo-org/widget-shop"


class _Wire:
    """`SwarmClient.dispatch` over a recorded transport."""

    dispatch = SwarmClient.dispatch

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, Any]] = []

    def request(self, method: str, path: str, payload: Any = None, **_: Any) -> dict:
        self.sent.append((method, path, payload))
        if path == "/v1/stats":
            return {"limits": {"max_batch_size": 5}}
        return {"task": {"id": f"task_{len(self.sent)}", "state": "QUEUED"}}

    def dispatch_batch(self, payloads):  # pragma: no cover - must never be reached
        self.sent.append(("POST", "/v1/tasks/batch", payloads))
        return [{"id": "task_b", "state": "QUEUED"} for _ in payloads]


def _dispatch(wire: _Wire, **args: Any) -> dict:
    return json.loads(server._call(wire, "swarm_dispatch", {
        "prompt": "apply the review", "repo": REPO, "strategy": "direct-pr", **args,
    }))


def _body(wire: _Wire) -> dict:
    posts = [(path, payload) for method, path, payload in wire.sent if method == "POST"]
    ((path, payload),) = posts
    assert path == "/v1/tasks"
    return payload


#: What the caller writes, and what the bridge sends: `check_publish_to_pr`'s
#: forms, the ones swarm_workflow sends. A bare number travels as the integer;
#: a form naming a repository as `owner/repo#N`.
FORMS = [
    (41, 41),
    ("41", 41),
    ("#41", 41),
    ("octo-org/widget-shop#41", "octo-org/widget-shop#41"),
    ("https://github.com/octo-org/widget-shop/pull/41", "octo-org/widget-shop#41"),
    ("https://github.com/octo-org/widget-shop/pull/41/files", "octo-org/widget-shop#41"),
]


@pytest.mark.parametrize("given, sent", FORMS)
def test_a_single_dispatch_with_publish_to_pr_sends_it_top_level(given, sent):
    wire = _Wire()
    reply = _dispatch(wire, publish_to_pr=given)
    body = _body(wire)
    assert body["publish_to_pr"] == sent
    assert "publish_to_pr" not in body["input"]
    assert "publish_to_pr" not in body["metadata"]
    assert reply["publish_to_pr"] == {"pull_request": sent}


def test_without_it_the_body_is_unchanged():
    with_none, without = _Wire(), _Wire()
    _dispatch(with_none, publish_to_pr=None)
    _dispatch(without)
    assert "publish_to_pr" not in _body(without)
    assert _body(with_none) == _body(without)


def test_without_it_the_reply_names_none():
    assert "publish_to_pr" not in _dispatch(_Wire())


@pytest.mark.parametrize("given", [0, -3, True, 4.0, "", "pr", "octo-org/widget-shop",
                                   "https://gitlab.com/o/r/pull/1", "https://github.com/o/r/issues/4",
                                   ["41"], {"number": 41}])
def test_anything_else_is_refused_before_any_request(given):
    wire = _Wire()
    with pytest.raises(SwarmError, match="publish_to_pr"):
        _dispatch(wire, publish_to_pr=given)
    assert wire.sent == []


def test_the_same_prompt_onto_a_pull_request_is_not_a_repeat():
    wire = _Wire()
    _dispatch(wire)
    _dispatch(wire, publish_to_pr=41)
    posts = [payload for method, path, payload in wire.sent if path == "/v1/tasks"]
    assert ["publish_to_pr" in p for p in posts] == [False, True]


def test_the_several_tasks_form_refuses_it_before_any_request():
    wire = _Wire()
    with pytest.raises(SwarmError, match="one publishing task"):
        server._call(wire, "swarm_dispatch", {
            "tasks": [{"prompt": "a", "repo": REPO}, {"prompt": "b", "repo": REPO}],
            "publish_to_pr": 41,
        })
    assert wire.sent == []


def test_a_task_of_the_several_tasks_form_refuses_it_before_any_request():
    wire = _Wire()
    with pytest.raises(SwarmError, match="one publishing task"):
        server._call(wire, "swarm_dispatch", {
            "tasks": [{"prompt": "a", "repo": REPO, "publish_to_pr": 41}],
        })
    assert wire.sent == []


def test_swarm_dispatch_advertises_publish_to_pr():
    tool = next(t for t in server.TOOLS if t["name"] == "swarm_dispatch")
    assert "publish_to_pr" in tool["inputSchema"]["properties"]
    assert "publish_to_pr" in tool["description"]
    assert "publish_to_pr" not in server._BATCH_TASK_SCHEMA["properties"]
