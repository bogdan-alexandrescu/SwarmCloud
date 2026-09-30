"""Every FIELD the Artifacts tab and the CPU rows read is one the API sends, and back (#184).

WHY A SECOND FILE. `test_ui_api_field_contract.py` holds `types.ts` to the
payloads it was written for -- tasks, attempts, leases, capacity -- and none of
#184's. The Artifacts tab (#187) and its routes (#188) were written in two
lanes at once, against a contract neither could run, and a parity pass that
read both diffs side by side found the client short of what the server sends:

  * `/transcript` and `/answer` send `capture_truncated`; `TaskTranscript` and
    `TaskAnswer` did not declare it, so a transcript cut at the capture cap was
    drawn as a whole one;
  * `/logs` streams and `/artifacts/content` send `invalid_utf8_bytes`;
    neither `LogStream` nor `ArtifactContent` declared it;
  * a step's `tool.id`, `tool.name`, `tool_result.tool_use_id` and `is_error`
    can be null, and the client typed them as always present -- which is how
    two null ids came to be joined as a call and its result;
  * `AttemptUsage.cpu_limit_cores` was documented, and read, as the cgroup's
    limit, when `cpu_limit_source` says it can be the class's.

The first three are exactly what this file checks mechanically: BOTH
DIRECTIONS, as the first file does -- every required client field is served,
and every served field is declared -- for every shape the tab reads, from the
real routes over one populated task. The fourth was a meaning, not a name,
and `details.cpu.test.tsx` held it.

THE FOLLOW-UP (#184, owner decisions of 2026-09-25). `AttemptUsage` is gone:
contract request #15 was accepted, the CPU figures are typed `Attempt` fields
served on every attempt row, and `test_ui_api_field_contract.py` holds
`AttemptRow` both ways. What is new here is `TaskInputCopy`, the masked copy
of the task's input that Inputs and Details draw (`GET /v1/tasks/{id}/input`),
and its three texts' inline shape.

The helpers are the first file's, imported rather than copied, so there is one
reader of `types.ts` interfaces. `extends` is resolved here because
`ArtifactEntry extends ArtifactRef`, and the first file has no such interface.

Offline: FakeFirestore, InMemoryObjectReader, StaticTokenVerifier.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from .conftest import PROJECT, auth_header, seed_task, seed_tenant
from .test_ui_api_field_contract import TYPES_TS, _interface_body, _source

BUCKET = f"swarm-artifacts-{PROJECT}"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
BASE = "tenants/eng/tasks/task_a/attempts/att_1"


def _fields(name: str) -> dict[str, bool]:
    """Field -> optional, for one interface, including what it `extends`."""
    source = _source(TYPES_TS)
    header = re.search(rf"export interface {name}(?: extends (\w+))? \{{", source)
    assert header, f"interface {name} is not in types.ts; this check would be vacuous"
    parent = header.group(1)
    body = (
        _interface_body(source.replace(header.group(0), f"export interface {name} {{", 1), name)
        if parent
        else _interface_body(source, name)
    )
    found = dict(re.findall(r"^  (\w+)(\??):", body, flags=re.MULTILINE))
    assert found, f"no fields parsed out of interface {name}; the check would be vacuous"
    own = {field: mark == "?" for field, mark in found.items()}
    return {**_fields(parent), **own} if parent else own


def _inline_fields(interface: str, field: str) -> set[str]:
    """The member names of an inline object type, `field: { a: T; b: U } | null`."""
    body = _interface_body(_source(TYPES_TS), interface)
    match = re.search(rf"^  {field}\??: \{{([^}}]*)\}}", body, flags=re.MULTILINE)
    assert match, f"{interface}.{field} is not an inline object type in types.ts"
    return set(re.findall(r"(\w+)\??:", match.group(1)))


# --------------------------------------------------------------------------
# One finished claude-code task, with every object the tab reads
# --------------------------------------------------------------------------

STREAM_JSON = "\n".join(
    json.dumps(event)
    for event in (
        {"type": "system", "subtype": "init", "model": "claude-opus-5", "tools": ["Read"]},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Reading the contract."},
            {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "CONTRACT.md"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "is_error": False, "content": "# The contract"},
        ]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "# Findings\n\nDone.",
         "num_turns": 3, "stop_reason": "end_turn", "terminal_reason": "completed"},
    )
) + "\n"

REPORT = "# Report\n\nThe body.\n"


@pytest.fixture
def served(client, db, objects) -> dict[str, dict[str, Any]]:
    """Every #184 payload, from the real routes, keyed by the interface that reads it."""
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng", state="SUCCEEDED", runner_profile="claude-code")
    artifacts = {"report.md": REPORT, "claude-code.stdout.log": STREAM_JSON}
    db.docs["tasks/task_a"]["current_lease_id"] = None
    db.docs["tasks/task_a"]["result_summary"] = {
        "artifacts": [
            {"name": name, "bytes": len(body), "uri": f"gs://{BUCKET}/{BASE}/artifacts/{name}"}
            for name, body in artifacts.items()
        ],
        "artifact_bytes": sum(len(b) for b in artifacts.values()),
        "logs": {"stdout": f"gs://{BUCKET}/{BASE}/logs/stdout.log"},
        "agent_streams": {
            "stdout": "claude-code.stdout.log", "stderr": None, "transcript": None,
            "transcript_skipped": None, "stdout_truncated": False, "stderr_truncated": False,
        },
    }
    # A prompt, and something else beside it, so every text of the copy is
    # populated: the prompt, the rest, and the whole.
    db.docs["tasks/task_a"]["input"] = {"prompt": "Audit the capacity code.", "steps": 2}
    db.docs["attempts/att_1"] = {
        "attempt_id": "att_1", "task_id": "task_a", "tenant_id": "eng", "generation": 1,
        "lease_id": "lease_att_1", "backend": "CLOUD_RUN_JOB", "created_at": NOW,
        "started_at": NOW + timedelta(seconds=5), "completed_at": NOW + timedelta(minutes=2),
        "exit_code": 0, "checkpoints": [],
        # Contract request #15: typed, on the attempt, where the interim path
        # put them on a HEARTBEAT event.
        "cpu_seconds": 42.5, "peak_cpu_cores": 1.875, "mean_cpu_cores": 0.472,
        "cpu_limit_cores": 2.0,
        # Contract request #26: dated and sourced, so the served row carries
        # all three of its fields populated.
        "cpu_measured_at": NOW + timedelta(minutes=2), "cpu_limit_source": "cgroup",
    }
    # Caller metadata beside a platform key, so the masked block has both.
    db.docs["tasks/task_a"]["metadata"] = {"dispatch": {"strategy": "collect"}, "note": "a label"}
    for name, body in artifacts.items():
        objects.put(f"{BASE}/artifacts/{name}", body)
    objects.put(f"{BASE}/logs/agent_stdout.log", STREAM_JSON)
    objects.put(f"{BASE}/logs/stdout.log", '{"message": "child started"}\n')

    def get(path: str) -> dict[str, Any]:
        response = client.get(f"/v1/tasks/task_a{path}", headers=auth_header("alice"))
        assert response.status_code == 200, f"{path} -> {response.status_code}: {response.text}"
        return response.json()

    listing = get("/artifacts?limit=200")
    logs = get("/logs?stream=agent_stdout&stream=stdout")
    transcript = get("/transcript")
    attempts = get("/attempts")
    task_input = get("/input")

    # Each shape is asserted to be the populated one, so a comparison is never
    # made against an absent row whose optional fields prove nothing.
    assert listing["artifacts"], "the listing served no entry"
    assert [s["status"] for s in logs["streams"]] == ["ok", "ok"], logs["streams"]
    assert transcript["stream"]["status"] == "ok" and transcript["steps"], transcript
    assert attempts["attempts"][0]["peak_cpu_cores"] == 1.875, attempts
    assert task_input["prompt"] is not None and task_input["rest"] is not None, task_input

    return {
        "ArtifactListing": listing,
        "ArtifactEntry": listing["artifacts"][0],
        "ArtifactContent": get("/artifacts/content?name=report.md"),
        "TaskLogs": logs,
        "LogAttempt": logs["attempt"],
        "LogStream": logs["streams"][0],
        "TaskTranscript": transcript,
        "TranscriptStream": transcript["stream"],
        "TranscriptStep": next(s for s in transcript["steps"] if s["kind"] == "tool_call"),
        "TaskAnswer": get("/answer"),
        "AttemptRow": attempts["attempts"][0],
        "TaskInputCopy": task_input,
        "MaskedText": task_input["prompt"],
        # The task's metadata, masked (the owner's "mask it everywhere",
        # 2026-09-26). `.get`: an API without the block fails this shape by
        # name rather than failing every shape in the fixture.
        "MaskedMetadata": task_input.get("metadata"),
    }


SHAPES = (
    "ArtifactListing", "ArtifactEntry", "ArtifactContent", "TaskLogs", "LogAttempt",
    "LogStream", "TaskTranscript", "TranscriptStream", "TranscriptStep", "TaskAnswer",
    "AttemptRow", "TaskInputCopy", "MaskedText", "MaskedMetadata",
)


def test_the_interim_usage_type_is_gone_with_its_route():
    """`AttemptUsage` described `attempts?include=usage`, which the typed
    fields replaced. A client type for a payload nobody sends is a screen
    that can only ever draw `not served`."""
    assert "export interface AttemptUsage" not in _source(TYPES_TS)


@pytest.mark.parametrize("shape", SHAPES)
def test_every_required_client_field_is_served(served, shape):
    payload = served[shape]
    missing = sorted(f for f, optional in _fields(shape).items() if not optional and f not in payload)
    assert not missing, (
        f"types.ts {shape} requires {missing}, which the API does not send; the "
        "screen would draw them as absent on every read"
    )


@pytest.mark.parametrize("shape", SHAPES)
def test_every_field_the_api_serves_is_declared_by_the_client(served, shape):
    undeclared = sorted(set(served[shape]) - set(_fields(shape)))
    assert not undeclared, (
        f"the API sends {undeclared} in {shape} and types.ts does not declare them; "
        "a field no client declares is a finding no screen can show"
    )


@pytest.mark.parametrize(("kind", "field"), [("tool_call", "tool"), ("tool_result", "tool_result")])
def test_a_steps_inline_objects_match_what_the_server_builds(served, kind, field):
    step = next(s for s in served["TaskTranscript"]["steps"] if s["kind"] == kind)
    payload = step[field]
    assert payload is not None, f"the populated transcript's {kind} step carries no {field}"
    declared = _inline_fields("TranscriptStep", field)
    assert declared == set(payload), (
        f"types.ts TranscriptStep.{field} declares {sorted(declared)}; the server builds {sorted(payload)}"
    )


def test_a_step_id_the_server_may_send_as_null_is_typed_nullable():
    """`transcript._Scrubber.text` returns None for anything that is not a string.

    So `tool.id`, `tool.name`, `tool_result.tool_use_id` are `string | null`
    and `is_error` is `boolean | null` on the wire. Typed as always present,
    the client joined two null ids as a call and its result.
    """
    body = _interface_body(_source(TYPES_TS), "TranscriptStep")
    tool = re.search(r"^  tool: \{([^}]*)\}", body, flags=re.MULTILINE)
    result = re.search(r"^  tool_result: \{([^}]*)\}", body, flags=re.MULTILINE)
    assert tool and result
    for name in ("id", "name"):
        assert re.search(rf"\b{name}: string \| null", tool.group(1)), f"tool.{name} is not nullable"
    assert re.search(r"\btool_use_id: string \| null", result.group(1)), "tool_use_id is not nullable"
    assert re.search(r"\bis_error: boolean \| null", result.group(1)), "is_error is not nullable"
