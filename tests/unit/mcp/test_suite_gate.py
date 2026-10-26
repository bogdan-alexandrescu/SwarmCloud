"""`swarm_suite_gate` renders a repository's Playwright suite as a sharded gate (PLAYWRIGHT-GATE, part of #2253).

BGD's decision of 2026-10-26: SwarmCloud runs a repository's whole Playwright
suite as N shard steps on `claude-code` and one `report` step that merges their
blob reports, so a project (first: Product Oracle, ~1,230 mock specs) can gate
a merge train in SwarmCloud instead of on a laptop. The caller names a
registered repository, a ref and a few parameters; the bridge renders the
workflow (invariant 10: a runner profile by name, never an image, a command
line or resources) and submits it through the same `workflows.submit` every
workflow goes through.

These drive `server._call` against a recorder of the wire, so "N steps",
"refused" and "nothing was sent" are measurements of the `POST /v1/workflows`
body, not of a helper's return value.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from swarm_mcp import server, suitegate
from swarm_mcp.client import SwarmError


class _Wire:
    """A tenant that registered `acme/shop`, and a recorder of what was sent."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, Any]] = []

    def registration(self, owner: str, repo: str) -> dict[str, Any] | None:
        self.sent.append(("GET", "/v1/repositories", None))
        if f"{owner}/{repo}".lower() == "acme/shop":
            return {"repo_id": "repo_" + "0" * 16, "owner": "acme", "repo": "shop"}
        return None

    def request(self, method: str, path: str, payload: Any = None, **_: Any) -> dict:
        self.sent.append((method, path, payload))
        steps = (payload or {}).get("steps") or []
        return {
            "workflow": {"workflow_id": "wf_gate", "state": "QUEUED",
                         "steps": [{"step_id": s["step_id"], "task_id": f"task_{i}"}
                                   for i, s in enumerate(steps)]},
            "dispatch": {"strategy": (payload or {}).get("strategy")},
        }


def _gate(wire: _Wire, **args: Any) -> dict:
    return json.loads(server._call(wire, "swarm_suite_gate", {
        "repository": "acme/shop", "ref": "main", "suite": "playwright", "working_dir": "web",
        **args,
    }))


def _body(wire: _Wire) -> dict:
    posts = [(path, payload) for method, path, payload in wire.sent if method == "POST"]
    ((path, payload),) = posts
    assert path == "/v1/workflows"
    return payload


def _posts(wire: _Wire) -> list:
    return [entry for entry in wire.sent if entry[0] == "POST"]


@pytest.mark.parametrize("shards", [1, 3, 8, 20])
def test_n_shards_render_n_steps_and_one_report_depending_on_all(shards):
    wire = _Wire()
    _gate(wire, shards=shards)
    steps = _body(wire)["steps"]
    shard_steps = [s for s in steps if s["step_id"] != "report"]
    (report,) = [s for s in steps if s["step_id"] == "report"]
    assert len(steps) == shards + 1
    assert len(shard_steps) == shards
    for k, step in enumerate(shard_steps, start=1):
        assert f"--shard {k}/{shards}" in step["input"]["prompt"]
        # MUTATION: a shard told another shard's slice runs it twice and one never.
        others = set(re.findall(r"--shard (\d+)/", step["input"]["prompt"]))
        assert others == {str(k)}, others
        assert step["depends_on"] == []
    assert sorted(report["depends_on"]) == sorted(s["step_id"] for s in shard_steps)
    assert set(report["input_from"]) == set(report["depends_on"])


def test_the_default_is_eight_shards():
    wire = _Wire()
    _gate(wire)
    assert len(_body(wire)["steps"]) == suitegate.DEFAULT_SHARDS + 1 == 9


@pytest.mark.parametrize("shards", [0, -1, 21, 100, "8", 2.5, True])
def test_a_shard_count_outside_one_to_twenty_is_refused_and_nothing_is_sent(shards):
    wire = _Wire()
    with pytest.raises(SwarmError, match="shards"):
        _gate(wire, shards=shards)
    assert _posts(wire) == []


def test_an_unregistered_repository_is_refused_and_nothing_is_sent():
    wire = _Wire()
    with pytest.raises(SwarmError, match="not registered"):
        _gate(wire, repository="acme/elsewhere")
    assert _posts(wire) == []


@pytest.mark.parametrize("field,value", [
    ("suite", "cypress"),
    ("ref", "main; rm -rf /"),
    ("ref", "--upload-pack=x"),
    ("working_dir", "../outside"),
    ("working_dir", "/abs"),
    ("working_dir", "web && curl x"),
    ("project", "chromium; echo"),
    ("env_file", "../.env"),
    ("env_file", "/etc/passwd"),
    ("env_file", "web/.env $(id)"),
    ("repository", "not-a-slug"),
])
def test_a_parameter_that_would_change_the_command_is_refused(field, value):
    """Every value lands in a shell line an agent runs: only plain names and
    relative paths are taken, so no caller can add a command to it."""
    wire = _Wire()
    with pytest.raises(SwarmError):
        _gate(wire, **{field: value})
    assert _posts(wire) == []


def test_the_spec_names_only_runner_profiles_never_an_image_or_resources():
    wire = _Wire()
    _gate(wire, shards=4, project="chromium", env_file="web/.env.mock")
    body = _body(wire)
    forbidden = {"image", "command", "args", "argv", "resources", "resource_class", "cpu",
                 "memory", "backend", "env", "timeout_seconds"}

    def keys(value: Any) -> set[str]:
        if isinstance(value, dict):
            return set(value) | set().union(*(keys(v) for v in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(v) for v in value)) if value else set()
        return set()

    assert keys(body) & forbidden == set()
    for step in body["steps"]:
        assert step["runner_profile"] == "claude-code"
        assert set(step["input"]) == {"prompt"}
        # The gate runs tests and commits nothing.
        assert step["allow_empty_diff"] is True
    assert body["strategy"] == "collect"


def test_the_report_prompt_names_every_shards_blob():
    wire = _Wire()
    _gate(wire, shards=5)
    steps = {s["step_id"]: s for s in _body(wire)["steps"]}
    report = steps.pop("report")
    blobs = report["input_from"]
    assert len(set(blobs.values())) == 5
    for step_id, blob in blobs.items():
        assert blob in report["input_from"].values()
        # MUTATION: a report prompt that drops one blob merges four shards and
        # reports the suite as passed.
        assert blob in report["input"]["prompt"], blob
        # The producer writes the very name the report stages.
        assert blob in steps[step_id]["input"]["prompt"]
    assert "npx playwright merge-reports" in report["input"]["prompt"]


def test_the_report_prompt_finds_the_blobs_in_the_checkouts_parent_not_its_root():
    # The worker stages input_from files into work/, the checkout's PARENT,
    # while the agent starts in work/repo (agent_worker/inputs.py, #226).
    # MUTATION: a prompt that sends the agent to the repository's root finds
    # none of the blobs, stops, and the step SUCCEEDS with no summary.
    wire = _Wire()
    _gate(wire, shards=3)
    prompt = {s["step_id"]: s for s in _body(wire)["steps"]}["report"]["input"]["prompt"]
    assert "staged into your working directory" not in prompt
    assert "from the repository's root into it" not in prompt
    assert "parent directory" in prompt
    assert "`..` from the repository's root" in prompt
    assert "Earlier steps of this workflow gave you these files" in prompt
    assert "absolute paths" in prompt


def test_each_shard_runs_the_named_commands_from_working_dir_and_never_fixes_code():
    wire = _Wire()
    _gate(wire, shards=2, project="chromium", env_file="web/.env.mock")
    shard = _body(wire)["steps"][0]["input"]["prompt"]
    for line in ("cd web", "npm ci", "npx playwright install chromium",
                 "npx playwright test --shard 1/2 --retries=1 --reporter=blob,line --project=chromium",
                 "web/.env.mock"):
        assert line in shard, line
    # The image carries Chromium's libraries (FIX-2253); the agent is not root.
    assert "install --with-deps" not in shard and "chromium --with-deps" not in shard
    assert "Never fix" in shard


def test_without_a_project_or_env_file_neither_is_named():
    wire = _Wire()
    _gate(wire, shards=2)
    shard = _body(wire)["steps"][0]["input"]["prompt"]
    assert "--project" not in shard
    assert "env file" not in shard.lower()


def test_the_reply_says_where_the_summary_is_read():
    wire = _Wire()
    reply = _gate(wire, shards=3)
    gate = reply["suite_gate"]
    assert gate["shards"] == 3
    assert gate["report_step"] == "report"
    assert "summary.md" in gate["read"] and "swarm_result" in gate["read"]
    assert _body(wire)["repository_url"] == "https://github.com/acme/shop"
    assert _body(wire)["repository_ref"] == "main"


def test_swarm_suite_gate_is_advertised_with_its_bounds():
    tool = next(t for t in server.TOOLS if t["name"] == "swarm_suite_gate")
    props = tool["inputSchema"]["properties"]
    assert props["shards"]["minimum"] == 1 and props["shards"]["maximum"] == 20
    assert props["shards"]["default"] == 8
    assert props["suite"]["enum"] == ["playwright"]
    assert set(tool["inputSchema"]["required"]) == {"repository", "ref", "suite"}


def test_the_plugin_moved_to_a_bridge_that_has_the_suite_gate():
    """Installed plugins fetch the bridge at the tag their version pins (0.5.70 was ARTIFACT-IMAGES)."""
    from pathlib import Path

    manifest = json.loads(
        (Path(__file__).resolve().parents[3] / "plugin" / ".claude-plugin" / "plugin.json").read_text()
    )
    # At least the bump this change made; a later bump moves it on (0.5.72,
    # SC-ROWS-PERSON-WAIT), and the tag the bridge is fetched at follows it.
    version = manifest["version"]
    assert tuple(int(p) for p in version.split(".")) >= (0, 5, 71), version  # MUTATION: no bump.
    assert any(f"@sc-v{version}#" in arg for arg in manifest["mcpServers"]["swarmcloud"]["args"])
