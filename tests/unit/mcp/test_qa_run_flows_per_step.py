"""`swarm_qa_run`'s `flows_per_step` (QA-FLOWS-CAP, part of #2136).

BGD, 2026-10-26: a leg's flows run in batches, several a live-browser step,
the batch size chosen when a run starts. The plugin's `swarm_qa_run` sends
the same optional `flows_per_step` the API's `:run` takes (1-30), refuses
anything else before a request, and sends no such key when the caller names
none, so the workspace's `qa_flows_per_step` decides. Its bounds are the
API's: `qaspec` states them, `swarm_mcp.qatargets` restates them (the plugin
does not import swarm-api), and this file holds the two equal
(docs/mirrored-values.md).

Offline: no credentials, no emulator.
"""

from __future__ import annotations

import json

import pytest

from swarm_api import qaspec
from swarm_api.routes.qatargets import RunBody
from swarm_mcp import qatargets, server
from swarm_mcp.client import SwarmError

LIST_PATH = "/v1/qa-targets"
APPROVED_ID = "qat_" + "a1" * 8


class FakeApi:
    """`:run`, recording every request. An id is resolved without one."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, object]] = []

    def request(self, method, path, payload=None, **_kwargs):
        self.sent.append((method, path, payload))
        if method == "POST" and path.endswith(":run"):
            return {"group_id": "qrg_" + "f6" * 8, "workflow_id": "wf_" + "c3" * 8, "file": "issues",
                    "qa_target": {"id": APPROVED_ID, "revision": 2}, "browser_steps": ["capture-g1"],
                    "legs": []}
        raise SwarmError(f"{method} {path} -> 500: the fake has no such route", status=500)


def _tool(name: str) -> dict:
    return next(t for t in server.TOOLS if t["name"] == name)


def test_a_run_that_names_flows_per_step_sends_it_in_the_run_body():
    api = FakeApi()
    json.loads(server._call(api, "swarm_qa_run", {"target": APPROVED_ID, "flows_per_step": 12}))
    # MUTATION: drop `flows_per_step` from the payload, or send it under another key.
    assert api.sent == [("POST", f"{LIST_PATH}/{APPROVED_ID}:run", {"file": "issues", "flows_per_step": 12})]


def test_a_run_that_names_none_sends_no_such_key():
    api = FakeApi()
    server._call(api, "swarm_qa_run", {"target": APPROVED_ID})
    # MUTATION: default it to 15 here and the workspace's setting never decides.
    assert "flows_per_step" not in api.sent[-1][2]


@pytest.mark.parametrize("value", [0, 31, "12", 2.5, True, -3])
def test_a_value_outside_1_to_30_is_refused_before_any_request(value):
    api = FakeApi()
    with pytest.raises(SwarmError) as caught:
        server._call(api, "swarm_qa_run", {"target": APPROVED_ID, "flows_per_step": value})
    # MUTATION: check after resolving the target (a GET is sent), or not at all.
    assert api.sent == []
    assert "flows_per_step" in str(caught.value) and "30" in str(caught.value)


def test_the_schema_bounds_it_and_gives_it_no_default():
    field = _tool("swarm_qa_run")["inputSchema"]["properties"]["flows_per_step"]
    assert field["type"] == "integer"
    assert (field["minimum"], field["maximum"]) == (1, 30)
    # A default would be read as the caller's choice over the workspace's.
    assert "default" not in field
    assert "flows_per_step" not in _tool("swarm_qa_run")["inputSchema"].get("required", [])
    assert "qa_flows_per_step" in _tool("swarm_qa_run")["description"]


def test_the_plugins_bounds_are_the_apis():
    # MUTATION: change either side's bound alone and this fails.
    assert (qatargets.MIN_FLOWS_PER_STEP, qatargets.MAX_FLOWS_PER_STEP) == \
        (qaspec.MIN_FLOWS_PER_STEP, qaspec.MAX_FLOWS_PER_STEP)
    meta = {type(m).__name__: m for m in RunBody.model_fields["flows_per_step"].metadata}
    assert meta["Ge"].ge == qaspec.MIN_FLOWS_PER_STEP
    assert meta["Le"].le == qaspec.MAX_FLOWS_PER_STEP
