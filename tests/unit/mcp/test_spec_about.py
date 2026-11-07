"""A spec's `about` reaches swarm-api as `metadata.about` (the owner, 2026-11-06).

The console's workflow page says what the work is for (purpose), what it
must achieve (goals) and what it will produce (expected output). A spec says
them at its top level as `about: {purpose?, goals?, output?}`, beside
`title` and `description`; the bridge sends it as `metadata.about`, the way
`title` goes as `metadata.title`, through `swarm_workflow` and the terminal's
`swarm workflow` alike, and refuses a malformed one by swarm-api's own rule,
naming the part, before anything is sent. A fake control plane RECORDS what
it was sent, so "nothing was sent" is a measurement.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from swarm_api import schemas
from swarm_mcp import cli, server, workflows
from swarm_mcp.client import SwarmError

REPO = "https://github.com/acme/web"
ABOUT = {
    "purpose": "Shoppers asked for size guidance on the product page.",
    "goals": ["A size chart on every product page", "Sizes read from the catalogue"],
    "output": "A pull request with the chart component and its tests.",
}


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


def test_about_and_description_are_spec_keys():
    assert {"about", "description"} <= workflows.SPEC_KEYS


def test_the_tool_sends_it_as_metadata():
    (body,) = _posted(_through_the_tool(_spec(about={**ABOUT, "purpose": f" {ABOUT['purpose']} "})))
    assert body["metadata"]["about"] == ABOUT
    assert body["metadata"]["title"] == "Size chart"
    assert "about" not in body
    # The body swarm-api would get is one it accepts, and keeps as sent.
    created = schemas.WorkflowCreate.model_validate(body)
    assert created.metadata["about"] == ABOUT and created.metadata["description_source"] == "submitter"


def test_a_description_and_an_about_go_together():
    (body,) = _posted(_through_the_tool(_spec(description="Adds a size chart.", about={"output": "A report."})))
    assert body["metadata"]["description"] == "Adds a size chart."
    assert body["metadata"]["about"] == {"output": "A report."}


def test_a_spec_without_it_sends_none():
    (body,) = _posted(_through_the_tool(_spec()))
    assert "about" not in body["metadata"]


def test_the_terminal_sends_it(tmp_path):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(_spec(about=ABOUT)))
    api = RecordingApi()
    args = cli.build_parser().parse_args(["workflow", str(path)])
    assert cli.cmd_workflow(api, args) == cli.EXIT_OK
    (body,) = _posted(api)
    assert body["metadata"]["about"] == ABOUT


@pytest.mark.parametrize(("about", "said"), [
    ({"purpose": "p" * 301}, "about.purpose is 301 characters and holds at most 300"),
    ({"output": "o" * 301}, "about.output is 301 characters and holds at most 300"),
    ({"goals": ["fine", "g" * 121]}, "about.goals[1] is 121 characters and holds at most 120"),
    ({"goals": ["a", "b", "c", "d"]}, "about.goals must be a list of 1 to 3"),
    ({"purpose": "Adds a <b>size</b> chart."}, "about.purpose holds markup"),
    ({"goals": ["See [it](https://github.com/acme/web/pull/2)"]}, "about.goals[0] holds markup"),
    ({"output": "Two\nlines"}, "about.output must be one line"),
    ({"purpose": ""}, "about.purpose must be non-empty text"),
    ({"summary": "x"}, "about must be {purpose?, goals?, output?}"),
    ("a sentence", "about must be {purpose?, goals?, output?}"),
])
def test_a_malformed_one_is_refused_with_the_field_named_before_anything_is_sent(about, said):
    api = RecordingApi()
    spec = _spec(about=about)
    with pytest.raises(SwarmError, match="Nothing was sent") as caught:
        server._call(api, "swarm_workflow", {"spec": spec, "spec_digest": workflows.spec_digest(spec)})
    assert said in str(caught.value)
    assert _posted(api) == []


def test_submit_checks_it_too():
    api = RecordingApi()
    with pytest.raises(SwarmError, match="about.purpose"):
        workflows.submit(api, steps=[{"step_id": "a", "input": {"prompt": "a"}}], about={"purpose": "x" * 301})
    assert _posted(api) == []


def test_the_bridge_holds_the_apis_caps():
    assert workflows.ABOUT_PURPOSE_MAX_CHARS == schemas.ABOUT_PURPOSE_MAX_CHARS
    assert workflows.ABOUT_GOAL_MAX_CHARS == schemas.ABOUT_GOAL_MAX_CHARS
    assert workflows.ABOUT_GOALS_MAX == schemas.ABOUT_GOALS_MAX
    assert workflows.ABOUT_OUTPUT_MAX_CHARS == schemas.ABOUT_OUTPUT_MAX_CHARS
