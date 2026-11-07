"""A spec's `description` and `feature` reach swarm-api as metadata (the owner, 2026-11-04).

The console's workflow page heads with what the work is meant to deliver and
the feature it belongs to. A spec says both at its top level, beside
`title`; the bridge sends them as `metadata.description` and
`metadata.feature`, the way `title` goes as `metadata.title`, through
`swarm_workflow` and the terminal's `swarm workflow` alike, and refuses a
malformed one by swarm-api's own rules before anything is sent. Every test
drives the real reader against a fake control plane that RECORDS what it was
sent, so "nothing was sent" is a measurement.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from swarm_api import schemas
from swarm_mcp import cli, server, workflows
from swarm_mcp.client import SwarmError

REPO = "https://github.com/acme/web"
DESCRIPTION = "Adds a size chart to the product page. Shoppers pick a size without leaving it."
FEATURE = {"title": "Size guidance", "url": "https://github.com/acme/web/issues/41"}


class RecordingApi:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, Any]] = []

    def request(self, method: str, path: str, *, payload: Any = None, timeout: int = 60) -> Any:
        self.sent.append((method, path, payload))
        if method == "POST" and path == "/v1/workflows":
            steps = [{"step_id": s["step_id"], "task_id": f"task_{s['step_id']}",
                      "runner_profile": s.get("runner_profile")} for s in payload["steps"]]
            return {"workflow": {"workflow_id": "wf_1", "tenant_id": "eng", "steps": steps},
                    "dispatch": {"strategy": "collect"}, "tenant_id": "eng"}
        raise SwarmError(f"{method} {path} -> 404: no such route", status=404)


def _spec(**extra: Any) -> dict[str, Any]:
    return {"repository_url": REPO, "title": "Size chart",
            "steps": [{"step_id": "a", "prompt": "add the size chart"}], **extra}


def _posted(api: RecordingApi) -> list[Any]:
    return [payload for method, path, payload in api.sent if (method, path) == ("POST", "/v1/workflows")]


def _through_the_tool(spec: dict[str, Any]) -> RecordingApi:
    api = RecordingApi()
    server._call(api, "swarm_workflow", {"spec": spec, "spec_digest": workflows.spec_digest(spec)})
    return api


def test_both_are_spec_keys():
    assert {"description", "feature"} <= workflows.SPEC_KEYS


def test_the_tool_sends_both_as_metadata():
    (body,) = _posted(_through_the_tool(_spec(description=f" {DESCRIPTION} ", feature=FEATURE)))
    assert body["metadata"]["description"] == DESCRIPTION
    assert body["metadata"]["feature"] == FEATURE
    assert body["metadata"]["title"] == "Size chart"
    assert "description" not in body and "feature" not in body
    # The body swarm-api would get is one it accepts.
    assert schemas.WorkflowCreate.model_validate(body).metadata["description_source"] == "submitter"


def test_a_spec_without_them_sends_neither():
    (body,) = _posted(_through_the_tool(_spec()))
    assert "description" not in body["metadata"] and "feature" not in body["metadata"]


def test_the_terminal_sends_both(tmp_path):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(_spec(description=DESCRIPTION, feature={"title": "Size guidance"})))
    api = RecordingApi()
    args = cli.build_parser().parse_args(["workflow", str(path)])
    assert cli.cmd_workflow(api, args) == cli.EXIT_OK
    (body,) = _posted(api)
    assert body["metadata"]["description"] == DESCRIPTION
    assert body["metadata"]["feature"] == {"title": "Size guidance"}


@pytest.mark.parametrize(("extra", "said"), [
    ({"description": "a" * 601}, "601 characters and holds at most 600"),
    ({"description": ""}, "description must be non-empty text"),
    ({"description": "Adds a <b>size</b> chart."}, "plain text"),
    ({"description": "Adds a **size** chart."}, "plain text"),
    ({"feature": "Size guidance"}, "feature must be {title, url?}"),
    ({"feature": {"title": "f" * 121}}, "holds at most 120"),
    ({"feature": {"title": "Size", "url": "https://gitlab.com/acme/web/issues/1"}}, "feature.url must be"),
    ({"feature": {"title": "Size", "url": "https://github.com/acme/other/pull/2"}}, "own repository"),
])
def test_a_malformed_one_is_refused_before_anything_is_sent(extra, said):
    api = RecordingApi()
    spec = _spec(**extra)
    with pytest.raises(SwarmError, match="Nothing was sent") as caught:
        server._call(api, "swarm_workflow", {"spec": spec, "spec_digest": workflows.spec_digest(spec)})
    assert said in str(caught.value)
    assert _posted(api) == []


def test_submit_checks_them_too():
    api = RecordingApi()
    with pytest.raises(SwarmError, match="Nothing was sent"):
        workflows.submit(api, steps=[{"step_id": "a", "input": {"prompt": "a"}}], description="x" * 601)
    assert _posted(api) == []


def test_the_bridge_holds_the_apis_caps():
    assert workflows.DESCRIPTION_MAX_CHARS == schemas.DESCRIPTION_MAX_CHARS
    assert workflows.FEATURE_TITLE_MAX_CHARS == schemas.FEATURE_TITLE_MAX_CHARS
