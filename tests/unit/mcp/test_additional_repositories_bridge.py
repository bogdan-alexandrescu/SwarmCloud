"""The sc plugin's tools send a step's additional repositories by NAME (MR-BRIDGE, part of #1434).

docs/design/multi-repository-steps.md §6, lane MR-BRIDGE: `swarm_dispatch`
(one task and a batch) and `swarm_workflow` (`steps`, and a whole `spec`)
send `additional_repositories` as the `owner/repo` names the API takes;
`infer` chooses the primary repository and never rewrites that list; and a
URL is refused before any request. Every test drives `server._call` against a
fake control plane that RECORDS what it was sent, so "nothing was sent" is a
measurement.

The count is deliberately NOT checked here: the limit is the platform setting
`max_additional_repositories`, editable live, which swarm-api reads at each
submission and refuses `too_many_additional_repositories` against.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

import pytest

from swarm_mcp import checkout, server, workflows
from swarm_mcp.client import SwarmError, task_payload

WEB = {"repository": "acme/web", "ref": "main", "name": "web"}
SDK = {"repository": "acme/sdk"}


class RecordingApi:
    """Records every body. A single dispatch goes through the real
    `task_payload`, so what is asserted is the body the API would get."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, Any]] = []

    def request(self, method: str, path: str, *, payload: Any = None, timeout: int = 60) -> Any:
        self.sent.append((method, path, payload))
        if path == "/v1/stats":
            return {"limits": {"max_batch_size": 5}}
        if method == "POST" and path == "/v1/workflows":
            steps = [{"step_id": s["step_id"], "task_id": f"task_{s['step_id']}",
                      "runner_profile": s.get("runner_profile")} for s in payload["steps"]]
            return {"workflow": {"workflow_id": "wf_1", "tenant_id": "eng", "steps": steps},
                    "dispatch": {"strategy": "collect"}, "tenant_id": "eng"}
        raise SwarmError(f"{method} {path} -> 404: no such route", status=404)

    def dispatch(self, **fields: Any) -> dict[str, Any]:
        payload = task_payload(**fields)
        self.sent.append(("POST", "/v1/tasks", payload))
        return {"id": f"task_{len(self.sent)}", "state": "QUEUED"}

    def dispatch_batch(self, payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.sent.append(("POST", "/v1/tasks/batch", {"tasks": list(payloads)}))
        return [{"id": f"task_b{i}", "state": "QUEUED"} for i, _ in enumerate(payloads, start=1)]


def _posted(api: RecordingApi, path: str) -> list[Any]:
    return [payload for method, path_, payload in api.sent if method == "POST" and path_ == path]


def _inferred(monkeypatch) -> dict[str, Any]:
    """`infer: true` resolving to a checkout's pushed branch, pinned; records
    what `resolve` was asked, so the test can show the list never reached it."""
    asked: dict[str, Any] = {}

    def resolve(**kwargs: Any) -> checkout.Repository:
        asked.update(kwargs)
        if kwargs.get("infer"):
            return checkout.Repository(url="https://github.com/acme/api", ref="a" * 40,
                                       source="checkout", commit="a" * 40)
        return checkout.Repository(url=None, ref=None, source="none")

    monkeypatch.setattr(server.checkout, "resolve", resolve)
    return asked


# --------------------------------------------------------------------------
# Sent as names
# --------------------------------------------------------------------------


def test_swarm_dispatch_sends_additional_repositories_as_names():
    api = RecordingApi()
    server._call(api, "swarm_dispatch", {"prompt": "trace the call",
                                         "additional_repositories": [WEB, SDK]})
    (body,) = _posted(api, "/v1/tasks")
    assert body["additional_repositories"] == [WEB, SDK]


def test_a_dispatch_naming_none_sends_no_key():
    api = RecordingApi()
    server._call(api, "swarm_dispatch", {"prompt": "trace the call"})
    (body,) = _posted(api, "/v1/tasks")
    assert "additional_repositories" not in body


def test_a_batch_task_sends_its_own_list():
    api = RecordingApi()
    server._call(api, "swarm_dispatch", {"tasks": [
        {"prompt": "a", "additional_repositories": [WEB]},
        {"prompt": "b"},
    ]})
    (batch,) = _posted(api, "/v1/tasks/batch")
    first, second = batch["tasks"]
    assert first["additional_repositories"] == [WEB]
    assert "additional_repositories" not in second


def test_additional_repositories_beside_tasks_is_refused():
    api = RecordingApi()
    with pytest.raises(SwarmError, match=r"given `tasks` AND \['additional_repositories'\]"):
        server._call(api, "swarm_dispatch", {"tasks": [{"prompt": "a"}],
                                             "additional_repositories": [WEB]})
    assert not api.sent


def test_the_count_is_left_to_the_platform_setting():
    """Eleven entries reach the API: the operative limit is the live platform
    setting swarm-api reads, never a copy of today's value here."""
    many = [{"repository": f"acme/r{i}"} for i in range(11)]
    api = RecordingApi()
    server._call(api, "swarm_dispatch", {"prompt": "x", "additional_repositories": many})
    (body,) = _posted(api, "/v1/tasks")
    assert body["additional_repositories"] == many


def test_the_same_prompt_over_other_repositories_is_not_a_repeat():
    api = RecordingApi()
    server._call(api, "swarm_dispatch", {"prompt": "same", "additional_repositories": [WEB]})
    server._call(api, "swarm_dispatch", {"prompt": "same", "additional_repositories": [SDK]})
    assert len(_posted(api, "/v1/tasks")) == 2
    with pytest.raises(SwarmError):
        server._call(api, "swarm_dispatch", {"prompt": "same", "additional_repositories": [SDK]})


def test_swarm_workflow_steps_send_the_default_and_a_step_list_replaces_it():
    api = RecordingApi()
    server._call(api, "swarm_workflow", {
        "additional_repositories": [WEB],
        "steps": [
            {"step_id": "read", "prompt": "read"},
            {"step_id": "own", "prompt": "own", "additional_repositories": [SDK]},
            {"step_id": "none", "prompt": "none", "additional_repositories": []},
        ],
    })
    (body,) = _posted(api, "/v1/workflows")
    assert body["additional_repositories"] == [WEB]
    steps = {s["step_id"]: s for s in body["steps"]}
    assert "additional_repositories" not in steps["read"]
    assert steps["own"]["additional_repositories"] == [SDK]
    # `[]` is "none", replacing the workflow's list: it must travel as `[]`.
    assert steps["none"]["additional_repositories"] == []


def test_a_workflow_naming_none_sends_no_key():
    api = RecordingApi()
    server._call(api, "swarm_workflow", {"steps": [{"step_id": "a", "prompt": "a"}]})
    (body,) = _posted(api, "/v1/workflows")
    assert "additional_repositories" not in body
    assert "additional_repositories" not in body["steps"][0]


def test_a_whole_spec_carries_both_lists():
    spec = {"additional_repositories": [WEB], "steps": [
        {"step_id": "a", "prompt": "a"},
        {"step_id": "b", "prompt": "b", "additional_repositories": []},
    ]}
    api = RecordingApi()
    server._call(api, "swarm_workflow", {"spec": spec, "spec_digest": workflows.spec_digest(spec)})
    (body,) = _posted(api, "/v1/workflows")
    assert body["additional_repositories"] == [WEB]
    assert body["steps"][1]["additional_repositories"] == []
    assert workflows.read_spec(spec)["additional_repositories"] == [WEB]


def test_a_list_beside_a_spec_is_two_answers_and_is_refused():
    api = RecordingApi()
    with pytest.raises(SwarmError, match=r"`spec` AND \['additional_repositories'\]"):
        server._call(api, "swarm_workflow", {"spec": {"steps": [{"step_id": "a", "prompt": "a"}]},
                                             "additional_repositories": [WEB]})
    assert not _posted(api, "/v1/workflows")


# --------------------------------------------------------------------------
# `infer` never rewrites them
# --------------------------------------------------------------------------


def test_infer_picks_the_primary_and_leaves_a_dispatch_list_as_named(monkeypatch):
    asked = _inferred(monkeypatch)
    api = RecordingApi()
    server._call(api, "swarm_dispatch", {"prompt": "x", "infer": True,
                                         "additional_repositories": [WEB, SDK]})
    (body,) = _posted(api, "/v1/tasks")
    assert body["repository_url"] == "https://github.com/acme/api"
    assert body["additional_repositories"] == [WEB, SDK]
    assert "additional_repositories" not in asked


def test_infer_leaves_a_workflow_list_as_named(monkeypatch):
    _inferred(monkeypatch)
    api = RecordingApi()
    server._call(api, "swarm_workflow", {
        "infer": True, "additional_repositories": [WEB],
        "steps": [{"step_id": "a", "prompt": "a", "additional_repositories": [SDK]}],
    })
    (body,) = _posted(api, "/v1/workflows")
    assert body["repository_url"] == "https://github.com/acme/api"
    assert body["additional_repositories"] == [WEB]
    assert body["steps"][0]["additional_repositories"] == [SDK]


# --------------------------------------------------------------------------
# A URL is refused before any request
# --------------------------------------------------------------------------


def _token_url() -> str:
    return "https://x-access-token:" + secrets.token_hex(20) + "@github.com/acme/web"


@pytest.mark.parametrize("repository", [
    "https://github.com/acme/web",
    "git@github.com:acme/web.git",
    "ssh://git@github.com/acme/web",
    "github.com/acme/web",
    "acme/web/tree/main",
    "acme",
    "-acme/web",
    "",
])
@pytest.mark.parametrize("tool,wrap", [
    ("swarm_dispatch", lambda extra: {"prompt": "x", "additional_repositories": extra}),
    ("swarm_dispatch", lambda extra: {"tasks": [{"prompt": "x", "additional_repositories": extra}]}),
    ("swarm_workflow", lambda extra: {"steps": [{"step_id": "a", "prompt": "a"}],
                                      "additional_repositories": extra}),
    ("swarm_workflow", lambda extra: {"steps": [{"step_id": "a", "prompt": "a",
                                                 "additional_repositories": extra}]}),
])
def test_a_url_or_non_name_is_refused_before_any_request(tool, wrap, repository):
    api = RecordingApi()
    with pytest.raises(SwarmError, match="owner/repo"):
        server._call(api, tool, wrap([{"repository": repository}]))
    assert not [s for s in api.sent if s[0] == "POST"], api.sent


def test_a_refused_url_is_never_repeated():
    url = _token_url()
    api = RecordingApi()
    with pytest.raises(SwarmError) as refused:
        server._call(api, "swarm_dispatch", {"prompt": "x",
                                             "additional_repositories": [{"repository": url}]})
    assert "repository must be owner/repo" in str(refused.value)
    assert url.split("@")[0] not in str(refused.value)
    assert not api.sent


@pytest.mark.parametrize("entry,named", [
    ({"repository": "acme/web", "url": "https://github.com/acme/web"}, "url"),
    ({"repository": "acme/web", "commit": "b" * 40}, "commit"),
    ({"repository": "acme/web", "forge_credential_source": "tenant"}, "forge_credential_source"),
    ("acme/web", "not an object"),
    ({"repository": "acme/web", "ref": ""}, "ref must be a non-empty string"),
    ({"repository": "acme/web", "name": 3}, "name must be a non-empty string"),
])
def test_only_names_are_taken(entry, named):
    api = RecordingApi()
    with pytest.raises(SwarmError, match=named):
        server._call(api, "swarm_dispatch", {"prompt": "x", "additional_repositories": [entry]})
    assert not api.sent


def test_a_list_that_is_not_a_list_is_refused():
    with pytest.raises(SwarmError, match="must be a list"):
        workflows.build_steps([{"step_id": "a", "prompt": "a",
                                "additional_repositories": {"repository": "acme/web"}}])


# --------------------------------------------------------------------------
# Advertised
# --------------------------------------------------------------------------


def _schema(name: str) -> dict[str, Any]:
    return next(t for t in server.TOOLS if t["name"] == name)["inputSchema"]


def test_the_tools_advertise_the_field():
    dispatch = _schema("swarm_dispatch")["properties"]
    workflow = _schema("swarm_workflow")["properties"]
    for where in (
        dispatch["additional_repositories"],
        dispatch["tasks"]["items"]["properties"]["additional_repositories"],
        workflow["additional_repositories"],
        workflow["steps"]["items"]["properties"]["additional_repositories"],
    ):
        assert where["type"] == "array"
        item = where["items"]
        assert set(item["properties"]) == {"repository", "ref", "name"}
        assert item["additionalProperties"] is False
        assert "URL" in where["description"]
    assert "REPLACING" in workflow["steps"]["items"]["properties"]["additional_repositories"]["description"]
    json.dumps(server.TOOLS)  # the schema is still plain JSON
