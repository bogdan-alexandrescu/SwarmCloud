"""`swarm merge <pr>` and `swarm_workflow`'s `merge_pr` (#352, owner decision 2026-10-07).

The caller names a pull request and the head sha it means; the bridge sends
the one merge step, `strategy: direct-pr` and `merge_pr: {number, head_sha}`,
and no repository unless one was named -- swarm-api takes the tenant's
registered repository then (`continuation.resolve_merge_pr`). Every check of
the pull request itself is the API's: the bridge reads no forge.
"""

from __future__ import annotations

import argparse
import json

import pytest

from swarm_mcp import cli, server, workflows
from swarm_mcp.cli import build_parser
from swarm_mcp.client import SwarmError

HEAD = "c" * 40


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict | None]] = []

    def request(self, method: str, path: str, payload=None, **_: object) -> dict:
        self.sent.append((method, path, payload))
        steps = [
            {"step_id": s["step_id"], "task_id": f"task_{s['step_id']}",
             "runner_profile": s["runner_profile"], "depends_on": s.get("depends_on") or []}
            for s in (payload or {}).get("steps", [])
        ]
        return {"workflow": {"workflow_id": "wf_1", "steps": steps},
                "dispatch": {"strategy": "direct-pr"}}


def _body(recorder: _Recorder) -> dict:
    ((method, path, payload),) = recorder.sent
    assert (method, path) == ("POST", "/v1/workflows")
    return payload


@pytest.mark.parametrize("text, repository, number", [
    ("41", None, 41),
    ("#41", None, 41),
    ("octo-org/widget-shop#41", "https://github.com/octo-org/widget-shop", 41),
    ("https://github.com/octo-org/widget-shop/pull/41", "https://github.com/octo-org/widget-shop", 41),
    ("https://github.com/octo-org/widget-shop/pull/41/files", "https://github.com/octo-org/widget-shop", 41),
])
def test_a_pull_request_is_read_from_a_number_a_short_ref_or_its_url(text, repository, number):
    assert workflows.parse_pull_request(text) == (repository, number)


@pytest.mark.parametrize("text", ["", "0", "-3", "pr", "octo-org/widget-shop", "https://gitlab.com/o/r/pull/1",
                                  "https://github.com/o/r/issues/4"])
def test_anything_else_is_refused_before_anything_is_sent(text):
    with pytest.raises(SwarmError):
        workflows.parse_pull_request(text)


def test_submit_merge_pr_sends_the_one_merge_step_and_no_repository_unless_named():
    recorder = _Recorder()
    workflows.submit_merge_pr(recorder, number=41, head_sha=HEAD)
    body = _body(recorder)
    assert body["steps"] == [{"step_id": "merge", "runner_profile": "merge"}]
    assert body["strategy"] == "direct-pr"
    assert body["merge_pr"] == {"number": 41, "head_sha": HEAD}
    assert "repository_url" not in body and "repository_ref" not in body
    named = _Recorder()
    workflows.submit_merge_pr(named, number=41, head_sha=HEAD,
                              repository_url="https://github.com/octo-org/widget-shop")
    assert _body(named)["repository_url"] == "https://github.com/octo-org/widget-shop"


@pytest.mark.parametrize("sha", ["C" * 40, "c" * 39, "main", ""])
def test_a_head_sha_that_is_not_a_full_sha_is_refused_here(sha):
    with pytest.raises(SwarmError, match="head"):
        workflows.submit_merge_pr(_Recorder(), number=41, head_sha=sha)


def test_the_cli_merge_verb_parses_and_submits(capsys):
    args = build_parser().parse_args(["merge", "octo-org/widget-shop#41", "--sha", HEAD])
    assert args.func is cli.cmd_merge
    recorder = _Recorder()
    assert cli.cmd_merge(recorder, args) == cli.EXIT_OK
    body = _body(recorder)
    assert body["merge_pr"] == {"number": 41, "head_sha": HEAD}
    assert body["repository_url"] == "https://github.com/octo-org/widget-shop"
    printed = capsys.readouterr().out
    assert "wf_1" in printed and "task_merge" in printed


def test_the_cli_merge_verb_requires_the_sha():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["merge", "41"])


def test_a_repo_flag_and_a_url_naming_another_repository_are_refused():
    args = argparse.Namespace(pull_request="octo-org/widget-shop#41", sha=HEAD,
                              repo="https://github.com/octo-org/gadget-shop", json=False,
                              title=None)
    recorder = _Recorder()
    with pytest.raises(SwarmError, match="two repositories"):
        cli.cmd_merge(recorder, args)
    assert recorder.sent == []


def test_swarm_workflow_takes_merge_pr_and_sends_the_one_merge_step():
    recorder = _Recorder()
    reply = json.loads(server._call(recorder, "swarm_workflow", {
        "merge_pr": {"number": 41, "head_sha": HEAD},
    }))
    body = _body(recorder)
    assert body["merge_pr"] == {"number": 41, "head_sha": HEAD}
    assert body["steps"] == [{"step_id": "merge", "runner_profile": "merge"}]
    assert reply["workflow_id"] == "wf_1"


@pytest.mark.parametrize("extra", [
    {"steps": [{"step_id": "a", "prompt": "x"}]},
    {"spec": {"steps": [{"step_id": "a", "prompt": "x"}]}},
    {"strategy": "integrate"},
    {"ref": "main"},
    {"infer": True},
])
def test_swarm_workflow_refuses_merge_pr_beside_anything_that_would_add_work(extra):
    recorder = _Recorder()
    with pytest.raises(SwarmError, match="merge_pr"):
        server._call(recorder, "swarm_workflow",
                     {"merge_pr": {"number": 41, "head_sha": HEAD}, **extra})
    assert recorder.sent == []


def test_swarm_workflow_advertises_merge_pr():
    schema = next(t for t in server.TOOLS if t["name"] == "swarm_workflow")["inputSchema"]
    merge_pr = schema["properties"]["merge_pr"]
    assert set(merge_pr["required"]) == {"number", "head_sha"}
