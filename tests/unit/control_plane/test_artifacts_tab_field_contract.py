"""Every FIELD the Artifacts tab and the CPU rows read is one the API sends, and back (#184).

WHY THIS FILE. The Artifacts tab (archived pull request 187) and its routes
(archived pull request 188) were written in two lanes at once, against a
contract neither could run, and a parity pass that read both diffs side by
side found the client short of what the server sends:

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

HOW IT IS HELD NOW (part of #72). This file once read `types.ts` as text and
compared its interfaces with these payloads by regex. The shapes are now
models in `swarm_api.wire_types` (`ArtifactListing`, `ArtifactEntry`,
`ArtifactContent`, `TaskLogs`, `LogAttempt`, `LogStream`, `TaskTranscript`,
`TranscriptStream`, `TranscriptStep`, `TaskAnswer`, `TaskInputCopy`,
`MaskedText`, `MaskedMetadata`, and `AttemptRow`), documented on their routes
(`responses=` only), and the console's interfaces EXTEND the types
`scripts/gen_api_types.py` generates from them, so `tsc` holds the console to
the models and the drift check (`gen_api_types.py --check`, in CI and
`tests/unit/scripts/test_api_types_drift.py`) holds the generated file to the
models. What is left here is the Python half: each payload the real routes
serve over one populated task is walked against its model at EVERY depth,
both directions -- every served key declared, every required field served --
and validates against it, so a null id or a missing count is a type error
here, not a regex miss. The fourth finding was a meaning, not a name, and
`details.cpu.test.tsx` holds it.

Offline: FakeFirestore, InMemoryObjectReader, StaticTokenVerifier.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import BaseModel

from swarm_api import wire_types
from swarm_api.inspect import parse_tail_header

from .conftest import PROJECT, auth_header, seed_task, seed_tenant
from .test_wire_types import nested_models, walk

BUCKET = f"swarm-artifacts-{PROJECT}"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
BASE = "tenants/eng/tasks/task_a/attempts/att_1"


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
        # A call whose id is not a string and that has no name, and a result
        # with no id and a non-boolean `is_error`: each is served null.
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": 7, "input": {"file_path": "README.md"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "is_error": "no", "content": "# Read me"},
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
        # One file the worker wrote and did not upload, so the skipped list
        # is checked against a row, not against `[]`.
        "artifacts_skipped": [{"name": "huge.bin", "cause": "cap"}],
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


#: Every shape the tab reads, and the model the console's type is generated from.
MODELS: dict[str, type[BaseModel]] = {
    name: getattr(wire_types, name) for name in (
        "ArtifactListing", "ArtifactEntry", "ArtifactContent", "TaskLogs", "LogAttempt",
        "LogStream", "TaskTranscript", "TranscriptStream", "TranscriptStep", "TaskAnswer",
        "AttemptRow", "TaskInputCopy", "MaskedText", "MaskedMetadata",
    )
}


def test_every_shape_the_tab_reads_has_a_model_and_a_payload(served):
    assert set(served) == set(MODELS), sorted(set(served) ^ set(MODELS))
    empty = sorted(name for name, payload in served.items() if not payload)
    assert not empty, f"the populated task served no {empty}; their checks would be vacuous"


@pytest.mark.parametrize("shape", sorted(MODELS))
def test_each_payload_is_exactly_its_model_at_every_depth(served, shape):
    """Both directions: a key the route serves and the model lacks (the
    console's generated type could not see it), and a field the model
    requires and the route does not send (the screen would draw it absent on
    every read), at every depth the model describes."""
    problems = walk(MODELS[shape], served[shape], shape, set())
    assert not problems, (
        f"{problems}: change `wire_types.{shape}` (or the route) and run "
        "`uv run python scripts/gen_api_types.py`"
    )


@pytest.mark.parametrize("shape", sorted(MODELS))
def test_each_payload_validates_against_its_model(served, shape):
    """The scalar types too: a count served as a string, or a null where the
    model says a value, is a generated type that lies."""
    MODELS[shape].model_validate(served[shape])


def test_every_nested_shape_is_reached_with_a_value(served):
    """No part of a shape is checked only against null or `[]`: the inline
    objects (`tool`, `tool_result`, `artifact`, `object`, `redaction`)
    included. A `tail_window` is served only for a live read of a running
    task, so it is walked from the one function that builds it for both the
    logs and the transcript (`inspect.parse_tail_header`)."""
    seen: set[str] = set()
    tail_window, _ = parse_tail_header(b"#swarm-tail offset=0 size=4 at=2026-09-25T12:00:00Z\nabcd")
    assert tail_window is not None
    assert not walk(wire_types.TailWindow, tail_window, "tail_window", seen)
    wire_types.TailWindow.model_validate(tail_window)
    for shape in ("ArtifactListing", "ArtifactContent", "TaskLogs", "TaskTranscript",
                  "TaskAnswer", "TaskInputCopy"):
        assert not walk(MODELS[shape], served[shape], shape, seen)
    reachable = set().union(*(nested_models(MODELS[s]) for s in (
        "ArtifactListing", "ArtifactContent", "TaskLogs", "TaskTranscript", "TaskAnswer",
        "TaskInputCopy")))
    unreached = sorted(reachable - seen)
    assert not unreached, f"the populated task served no {unreached} with a value"


@pytest.mark.parametrize(("kind", "field", "model"), [
    ("tool_call", "tool", wire_types.TranscriptTool),
    ("tool_result", "tool_result", wire_types.TranscriptToolResult),
])
def test_a_steps_inline_objects_are_exactly_what_the_server_builds(served, kind, field, model):
    step = next(s for s in served["TaskTranscript"]["steps"] if s["kind"] == kind)
    payload = step[field]
    assert payload is not None, f"the populated transcript's {kind} step carries no {field}"
    assert set(payload) == set(model.model_fields), (
        f"wire_types.{model.__name__} declares {sorted(model.model_fields)}; "
        f"the server builds {sorted(payload)}"
    )


def test_a_step_id_the_server_sends_as_null_is_typed_nullable(served):
    """`transcript._Scrubber.text` returns None for anything that is not a string.

    So `tool.id`, `tool.name`, `tool_result.tool_use_id` are `string | null`
    and `is_error` is `boolean | null` on the wire. Typed as always present,
    the client joined two null ids as a call and its result. The fixture's
    second call has a numeric id and no name, and its result no id and a
    string `is_error`: each is served null, and each validates.
    """
    steps = served["TaskTranscript"]["steps"]
    call = next(s for s in steps if s["kind"] == "tool_call" and s["tool"]["id"] is None)
    result = next(s for s in steps if s["kind"] == "tool_result"
                  and s["tool_result"]["tool_use_id"] is None)
    assert call["tool"]["name"] is None
    assert result["tool_result"]["is_error"] is None
    wire_types.TranscriptStep.model_validate(call)
    wire_types.TranscriptStep.model_validate(result)


def test_the_tab_routes_are_documented_with_their_models(client):
    """The models reach the console only through the OpenAPI document, so each
    route documents its own: a route that served the shape undocumented would
    leave the generated type unused."""
    document = client.app.openapi()
    for path, model in (
        ("/v1/tasks/{task_id}/artifacts", "ArtifactListing"),
        ("/v1/tasks/{task_id}/artifacts/content", "ArtifactContent"),
        ("/v1/tasks/{task_id}/logs", "TaskLogs"),
        ("/v1/tasks/{task_id}/transcript", "TaskTranscript"),
        ("/v1/tasks/{task_id}/answer", "TaskAnswer"),
        ("/v1/tasks/{task_id}/input", "TaskInputCopy"),
    ):
        schema = document["paths"][path]["get"]["responses"]["200"]["content"][
            "application/json"]["schema"]
        assert schema.get("$ref") == f"#/components/schemas/{model}", (path, schema)


def test_the_interim_usage_shape_is_gone_with_its_route(client):
    """`AttemptUsage` described `attempts?include=usage`, which the typed
    fields replaced. Neither the models nor the OpenAPI document (so neither
    the generated console type) carries it."""
    assert not hasattr(wire_types, "AttemptUsage")
    assert "AttemptUsage" not in client.app.openapi()["components"]["schemas"]
