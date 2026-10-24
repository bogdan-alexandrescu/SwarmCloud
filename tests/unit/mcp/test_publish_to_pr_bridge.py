"""`swarm_workflow` and `swarm workflow` pass `publish_to_pr` through (PTP-PLUGIN, part of #2183).

PR 2197 taught swarm-api `publish_to_pr`: a lane's commits are pushed onto an
existing pull request's branch instead of opening a replacement. BGD's answer
of 2026-10-24 02:17Z: the plugin's tools pass it through. The bridge sends it
TOP-LEVEL beside `steps`, only when given, as `merge_pr` is sent; and because
the API refuses it beside a merge step or `metadata.merge`, the bridge sends no
`metadata.merge` with it and refuses -- before any request -- a spec that
names both, in the API's own words. Every check of the pull request itself
(open, not a fork, a branch this tenant published) is the API's and the
worker's: the bridge reads no forge.

Every test drives the real entry points against a recorder of what was sent,
so "nothing was sent" is a measurement.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from swarm_mcp import cli, server, workflows
from swarm_mcp.client import SwarmError

REPO = "https://github.com/octo-org/widget-shop"
STEP = {"step_id": "fix", "prompt": "apply the review"}
#: The API's reason (`swarm_api.publishtopr.check_workflow_shape`), which the
#: bridge's refusal repeats.
API_REASON = "a merge step merges the pull request a step of its workflow opened"


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, Any]] = []

    def request(self, method: str, path: str, payload: Any = None, **_: Any) -> dict:
        self.sent.append((method, path, payload))
        steps = [{"step_id": s["step_id"], "task_id": f"task_{s['step_id']}",
                  "runner_profile": s.get("runner_profile"), "depends_on": s.get("depends_on") or []}
                 for s in (payload or {}).get("steps", [])]
        return {"workflow": {"workflow_id": "wf_1", "tenant_id": "eng", "steps": steps},
                "dispatch": {"strategy": "direct-pr"}, "tenant_id": "eng"}


def _body(recorder: _Recorder) -> dict:
    ((method, path, payload),) = recorder.sent
    assert (method, path) == ("POST", "/v1/workflows")
    return payload


def _spec(**top: Any) -> dict:
    return {"strategy": "direct-pr", "repository_url": REPO, "steps": [dict(STEP)], **top}


def _submit_spec(recorder: _Recorder, spec: dict, **beside: Any) -> dict:
    return json.loads(server._call(recorder, "swarm_workflow",
                                   {"spec": spec, "spec_digest": workflows.spec_digest(spec), **beside}))


# --------------------------------------------------------------------------
# Each accepted form
# --------------------------------------------------------------------------

#: What the caller writes, and what the bridge sends. A bare number (or
#: "#N") travels as the integer; a form naming a repository travels as
#: `owner/repo#N`, which swarm-api checks against the spec's own repository.
#: A URL is sent in that short form because the API reads `/pull/N` only at
#: the end, and a copied `.../pull/N/files` URL would be refused there.
FORMS = [
    (41, 41),
    ("41", 41),
    ("#41", 41),
    ("octo-org/widget-shop#41", "octo-org/widget-shop#41"),
    ("https://github.com/octo-org/widget-shop/pull/41", "octo-org/widget-shop#41"),
    ("https://github.com/octo-org/widget-shop/pull/41/files", "octo-org/widget-shop#41"),
]


@pytest.mark.parametrize("given, sent", FORMS)
def test_each_accepted_form_parses(given, sent):
    assert workflows.check_publish_to_pr(given) == sent


@pytest.mark.parametrize("given", [0, -3, True, 4.0, "", "pr", "octo-org/widget-shop",
                                   "https://gitlab.com/o/r/pull/1", "https://github.com/o/r/issues/4",
                                   ["41"], {"number": 41}])
def test_anything_else_is_refused_before_any_request(given):
    recorder = _Recorder()
    with pytest.raises(SwarmError, match="publish_to_pr"):
        _submit_spec(recorder, _spec(publish_to_pr=given))
    assert recorder.sent == []


# --------------------------------------------------------------------------
# Sent top-level, with no metadata.merge
# --------------------------------------------------------------------------

@pytest.mark.parametrize("given, sent", FORMS)
def test_a_spec_with_publish_to_pr_sends_it_top_level_and_no_merge(given, sent):
    recorder = _Recorder()
    reply = _submit_spec(recorder, _spec(publish_to_pr=given))
    body = _body(recorder)
    assert body["publish_to_pr"] == sent
    assert "merge" not in body["metadata"]
    assert "publish_to_pr" not in body["metadata"]
    for step in body["steps"]:
        assert "publish_to_pr" not in step
    # The answer says so: no merge was sent, and none will be appended.
    assert reply["publish_to_pr"]["pull_request"] == sent
    assert "no merge" in reply["publish_to_pr"]["merge"]


def test_merge_off_beside_publish_to_pr_asks_for_no_merge_and_is_not_sent():
    recorder = _Recorder()
    _submit_spec(recorder, _spec(publish_to_pr=41, merge="off"))
    body = _body(recorder)
    assert body["publish_to_pr"] == 41
    assert "merge" not in body["metadata"]


def test_the_explicit_tool_argument_sends_it_with_steps():
    recorder = _Recorder()
    reply = json.loads(server._call(recorder, "swarm_workflow", {
        "steps": [dict(STEP)], "strategy": "direct-pr", "repo": REPO,
        "publish_to_pr": "#41",
    }))
    assert _body(recorder)["publish_to_pr"] == 41
    assert reply["publish_to_pr"]["pull_request"] == 41


def test_the_explicit_tool_argument_beside_a_spec_that_names_none():
    recorder = _Recorder()
    _submit_spec(recorder, _spec(), publish_to_pr=41)
    assert _body(recorder)["publish_to_pr"] == 41


def test_the_argument_and_the_spec_naming_the_same_pull_request_agree():
    recorder = _Recorder()
    _submit_spec(recorder, _spec(publish_to_pr="#41"), publish_to_pr=41)
    assert _body(recorder)["publish_to_pr"] == 41


def test_the_argument_and_the_spec_naming_different_pull_requests_are_refused():
    recorder = _Recorder()
    with pytest.raises(SwarmError, match="publish_to_pr"):
        _submit_spec(recorder, _spec(publish_to_pr=41), publish_to_pr=42)
    assert recorder.sent == []


def test_a_spec_file_through_the_terminal_sends_it(tmp_path):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(_spec(publish_to_pr="octo-org/widget-shop#41")))
    recorder = _Recorder()
    args = cli.build_parser().parse_args(["workflow", str(path)])
    assert cli.cmd_workflow(recorder, args) == cli.EXIT_OK
    body = _body(recorder)
    assert body["publish_to_pr"] == "octo-org/widget-shop#41"
    assert "merge" not in body["metadata"]


def test_the_launch_path_reads_it_from_the_spec():
    """swarm_workflow_launch reads the spec with `read_spec` and /sc:swarmcloud
    submits it by `spec_handle`: a spec carrying the key must be read, not
    refused as an unknown key."""
    assert workflows.read_spec(_spec(publish_to_pr="#41"))["publish_to_pr"] == 41
    assert "publish_to_pr" in workflows.SPEC_KEYS


# --------------------------------------------------------------------------
# Without it, what is sent today
# --------------------------------------------------------------------------

def test_a_spec_without_it_sends_exactly_what_it_sends_today():
    recorder = _Recorder()
    reply = _submit_spec(recorder, _spec(merge="on", title="Review round"))
    body = _body(recorder)
    assert "publish_to_pr" not in body
    assert set(body) == {"steps", "metadata", "strategy", "repository_url"}
    assert body["metadata"]["merge"] == "on"
    assert body["metadata"]["title"] == "Review round"
    assert "publish_to_pr" not in reply


def test_submit_without_it_sends_no_key():
    recorder = _Recorder()
    workflows.submit(recorder, steps=[dict(STEP)], merge="on")
    body = _body(recorder)
    assert "publish_to_pr" not in body
    assert body["metadata"]["merge"] == "on"


# --------------------------------------------------------------------------
# Refused beside a merge, before any request
# --------------------------------------------------------------------------

@pytest.mark.parametrize("merge", ["on", "on_merge_verdict"])
def test_publish_to_pr_beside_a_merge_choice_is_refused_before_any_request(merge):
    recorder = _Recorder()
    with pytest.raises(SwarmError, match=API_REASON):
        _submit_spec(recorder, _spec(publish_to_pr=41, merge=merge))
    assert recorder.sent == []


def test_publish_to_pr_beside_a_merge_step_is_refused_before_any_request():
    recorder = _Recorder()
    spec = _spec(publish_to_pr=41, strategy="integrate", steps=[
        dict(STEP), {"step_id": "merge", "runner_profile": "merge", "depends_on": ["fix"]},
    ])
    with pytest.raises(SwarmError, match=API_REASON):
        _submit_spec(recorder, spec)
    assert recorder.sent == []


def test_the_tool_argument_beside_a_spec_that_merges_is_refused():
    recorder = _Recorder()
    with pytest.raises(SwarmError, match=API_REASON):
        _submit_spec(recorder, _spec(merge="on"), publish_to_pr=41)
    assert recorder.sent == []


def test_submit_itself_refuses_both():
    recorder = _Recorder()
    with pytest.raises(SwarmError, match=API_REASON):
        workflows.submit(recorder, steps=[dict(STEP)], merge="on", publish_to_pr=41)
    with pytest.raises(SwarmError, match="merge_pr"):
        workflows.submit(recorder, steps=[dict(STEP)], publish_to_pr=41,
                         merge_pr={"number": 41, "head_sha": "c" * 40})
    assert recorder.sent == []


def test_the_launch_refuses_both_before_writing_a_run():
    with pytest.raises(SwarmError, match=API_REASON):
        workflows.read_spec(_spec(publish_to_pr=41, merge="on"))


def test_merge_pr_beside_publish_to_pr_is_refused():
    recorder = _Recorder()
    with pytest.raises(SwarmError, match="merge_pr"):
        server._call(recorder, "swarm_workflow", {
            "merge_pr": {"number": 41, "head_sha": "c" * 40}, "publish_to_pr": 41,
        })
    assert recorder.sent == []


# --------------------------------------------------------------------------
# Advertised
# --------------------------------------------------------------------------

def test_swarm_workflow_advertises_publish_to_pr():
    tool = next(t for t in server.TOOLS if t["name"] == "swarm_workflow")
    assert "publish_to_pr" in tool["inputSchema"]["properties"]
    assert "publish_to_pr" in tool["description"]

