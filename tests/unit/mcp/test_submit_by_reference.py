"""`swarm_workflow` submits a spec by reference, so no model ever retypes it.

MEASURED, 2026-10-01. `/sc:swarmcloud` handed the whole spec to a haiku
`sc:workflow` relay, which retyped it into the `swarm_workflow` call. Specs of
11-44 KB came back altered and were refused by the digest check
(NOT_SUBMITTED) three times in one evening: B9 given by path, M1 and S1 given
as objects. The digest check did its job -- nothing changed was submitted --
but a check that refuses a third of the evening's work is a backstop doing the
main job.

So `swarm_workflow` takes `spec_path` (a local JSON file the bridge reads
itself) or `spec_ref` (the id of a spec the bridge already holds, handed out
by `swarm_workflow_spec`) in place of the inline `spec`. The bytes travel from
disk to the API without a model in between; `spec_digest` still checks them.
"""

from __future__ import annotations

import importlib.metadata
import json

import pytest

from swarm_mcp import server, workflows
from swarm_mcp.client import SwarmClient, SwarmError


class _Recorder:
    """The transport, stubbed at `request`, behind the real client methods."""

    dispatch = SwarmClient.dispatch

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict | None]] = []

    def request(self, method: str, path: str, payload=None, **_: object) -> dict:
        self.sent.append((method, path, payload))
        steps = [
            {"step_id": s["step_id"], "task_id": f"task_{s['step_id']}",
             "runner_profile": s["runner_profile"], "depends_on": s.get("depends_on") or []}
            for s in (payload or {}).get("steps", [])
        ]
        return {"workflow": {"workflow_id": "wf_1", "steps": steps}, "dispatch": {}}


def _big_spec(kilobytes: int = 50) -> dict:
    """A spec of about `kilobytes` KB: long prompts, the shape that came back altered."""
    filler = "Read the module, then write the change and its tests. " * 20
    steps = []
    index = 0
    while len(json.dumps({"steps": steps})) < kilobytes * 1024:
        steps.append({
            "step_id": f"s{index:02d}",
            "prompt": f"step {index}: " + filler + " `quotes` \"and\" back\\slashes, é 🚀",
            **({"depends_on": [f"s{index - 1:02d}"]} if index else {}),
        })
        index += 1
    return {"label": "big", "steps": steps}


def _submitted_steps(recorder: _Recorder) -> list[dict]:
    ((_, path, payload),) = recorder.sent
    assert path == "/v1/workflows"
    return payload["steps"]


def test_a_50_kb_spec_submits_through_spec_path_with_a_matching_digest(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_CHECKOUT_DIR", str(tmp_path))
    spec = _big_spec(50)
    target = tmp_path / "big.json"
    target.write_text(json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")
    assert target.stat().st_size >= 50 * 1024
    digest = workflows.spec_digest(spec)
    recorder = _Recorder()

    # The whole call a relay makes: a path and a digest, a few dozen bytes.
    arguments = {"spec_path": str(target), "spec_digest": digest}
    assert len(json.dumps(arguments)) < 300, "the call must not carry the spec"
    reply = json.loads(server._call(recorder, "swarm_workflow", arguments))

    assert reply["spec_digest"] == digest and reply["spec_digest_checked"] is True
    sent = _submitted_steps(recorder)
    assert [s["input"]["prompt"] for s in sent] == [s["prompt"] for s in spec["steps"]], "the bytes arrived changed"
    assert len(reply["steps"]) == len(spec["steps"])


def test_a_spec_path_whose_file_is_not_the_digested_spec_submits_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_CHECKOUT_DIR", str(tmp_path))
    spec = _big_spec(12)
    changed = json.loads(json.dumps(spec))
    changed["steps"][3]["prompt"] += " (tidied)"
    target = tmp_path / "spec.json"
    target.write_text(json.dumps(changed))
    recorder = _Recorder()

    with pytest.raises(SwarmError, match="NOTHING was submitted"):
        server._call(recorder, "swarm_workflow",
                     {"spec_path": str(target), "spec_digest": workflows.spec_digest(spec)})
    assert recorder.sent == []


def test_a_spec_ref_from_swarm_workflow_spec_submits_the_held_spec(tmp_path, monkeypatch):
    spec = _big_spec(20)
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "w.json").write_text(json.dumps(spec))
    monkeypatch.setenv("SWARM_CHECKOUT_DIR", str(tmp_path))

    read = json.loads(server._call(None, "swarm_workflow_spec", {"path": "specs/w.json"}))
    assert read["spec_ref"] and read["spec_digest"] == workflows.spec_digest(spec)
    # What a relay copies back instead of the spec: the outline.
    assert read["outline"]["steps"][1] == {"step_id": "s01", "depends_on": ["s00"], "stage": None}
    assert read["outline"]["label"] == "big"

    recorder = _Recorder()
    reply = json.loads(server._call(recorder, "swarm_workflow",
                                    {"spec_ref": read["spec_ref"], "spec_digest": read["spec_digest"]}))
    assert reply["spec_digest"] == read["spec_digest"]
    assert [s["input"]["prompt"] for s in _submitted_steps(recorder)] == [s["prompt"] for s in spec["steps"]]


def test_an_unknown_spec_ref_submits_nothing_and_says_where_refs_come_from():
    recorder = _Recorder()
    with pytest.raises(SwarmError, match="swarm_workflow_spec"):
        server._call(recorder, "swarm_workflow", {"spec_ref": "spec_0000000000000000"})
    assert recorder.sent == []


@pytest.mark.parametrize(
    "extra",
    [{"spec": {"steps": [{"step_id": "a", "prompt": "a"}]}}, {"spec_ref": "spec_x"}, {"steps": [{"step_id": "a", "prompt": "a"}]}],
    ids=["spec", "spec_ref", "steps"],
)
def test_one_spec_source_per_call(tmp_path, monkeypatch, extra):
    monkeypatch.setenv("SWARM_CHECKOUT_DIR", str(tmp_path))
    target = tmp_path / "s.json"
    target.write_text(json.dumps({"steps": [{"step_id": "a", "prompt": "a"}]}))
    recorder = _Recorder()
    with pytest.raises(SwarmError, match="one of"):
        server._call(recorder, "swarm_workflow", {"spec_path": str(target), **extra})
    assert recorder.sent == []


def test_an_inline_spec_still_submits():
    spec = {"steps": [{"step_id": "a", "prompt": "a"}]}
    recorder = _Recorder()
    reply = json.loads(server._call(recorder, "swarm_workflow",
                                    {"spec": spec, "spec_digest": workflows.spec_digest(spec)}))
    assert reply["workflow_id"] == "wf_1" and reply["spec_digest_checked"] is True


def test_the_reply_names_the_bridge_version():
    """`/sc:swarmcloud` names the bridge's version when a row's follow is
    refused, so the version has to arrive somewhere a relay reads."""
    spec = {"steps": [{"step_id": "a", "prompt": "a"}]}
    reply = json.loads(server._call(_Recorder(), "swarm_workflow", {"spec": spec}))
    assert reply["bridge_version"] == importlib.metadata.version("swarm-mcp")


def test_the_schema_offers_spec_path_and_spec_ref():
    (tool,) = [t for t in server.TOOLS if t["name"] == "swarm_workflow"]
    properties = tool["inputSchema"]["properties"]
    assert {"spec", "spec_path", "spec_ref", "spec_digest"} <= set(properties)
