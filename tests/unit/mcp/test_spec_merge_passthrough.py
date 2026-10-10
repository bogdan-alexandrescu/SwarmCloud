"""A workflow spec's `merge` reaches swarm-api as `metadata.merge` (WF-MERGE-API, part of #295).

The plugin's implement -> review -> fix specs ended at the pull request on
2026-10-09 because nothing let a spec ask for a merge. `merge` is a spec key
now ("on" | "on_merge_verdict" | "off"); the bridge passes it through
unchecked, and swarm-api decides what it means
(tests/unit/control_plane/test_merge_verdict_workflows.py). A spec that does
not say sends no `metadata.merge`, so the repository's `merge_policy` applies.
"""

from __future__ import annotations

from swarm_mcp import workflows


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict | None]] = []

    def request(self, method: str, path: str, payload=None, **_: object) -> dict:
        self.sent.append((method, path, payload))
        return {"workflow": {"workflow_id": "wf_1", "steps": []},
                "dispatch": {"strategy": "integrate"}}


SPEC = {
    "strategy": "integrate",
    "steps": [
        {"step_id": "implement", "prompt": "implement"},
        {"step_id": "review", "prompt": "review", "depends_on": ["implement"]},
    ],
}


def _metadata(spec: dict) -> dict:
    read = workflows.read_spec(spec)
    recorder = _Recorder()
    workflows.submit(recorder, steps=read["steps"], strategy=read["strategy"], merge=read["merge"])
    ((_method, _path, payload),) = recorder.sent
    return payload["metadata"]


def test_a_specs_merge_is_sent_as_metadata_merge():
    assert _metadata({**SPEC, "merge": "on_merge_verdict"})["merge"] == "on_merge_verdict"


def test_a_spec_without_merge_sends_none():
    assert "merge" not in _metadata(SPEC)


def test_merge_is_a_spec_key():
    assert "merge" in workflows.SPEC_KEYS
