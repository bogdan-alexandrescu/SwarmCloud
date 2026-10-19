"""The plugin names a step's model class BY NAME on a task, a step and a run (MODEL-PLUGIN, part of #1435).

docs/design/model-per-step.md §4.8, lane MODEL-PLUGIN:

* `swarm_profiles` lists each profile's `model_classes`, its
  `default_model_class` and the class's `models`, from the platform
  (`GET /v1/runtimes`) and, when the platform cannot be read, from the
  bundled copy, which knows the classes but not the IDs (Terraform pins them);
* `swarm_dispatch` (one task and a batch), `swarm_workflow` (`steps` and a
  whole `spec`) and `swarm_run_issue` send `model_class` as given, and a
  name the frozen vocabulary does not hold, or one the profile does not
  accept, is refused before any request;
* `swarm dispatch --model-class` exists, and `--model`'s help still says it is
  attribution and does "not select" the model.

Every tool test drives `server._call` against a fake control plane that
RECORDS what it was sent, so "nothing was sent" is a measurement.
"""

from __future__ import annotations

import argparse
import secrets
from typing import Any

import pytest

from swarm_common.profiles import MODEL_CLASSES, RUNNER_PROFILES
from swarm_mcp import cli, profiles, server, workflows
from swarm_mcp.client import SwarmClient, SwarmError, task_payload

#: What swarm-api's WORKER_CLASS_MODELS serves today (terraform/infra/locals.tf
#: `runner_models`). Only the platform half of these tests reads it.
PINS = {
    "opus": "claude-opus-5-5",
    "fable": "claude-fable-5-1",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
}


def _prompt() -> str:
    # Unique per call: the bridge refuses a repeat of a dispatch this session made.
    return "summarise the source " + secrets.token_hex(6)


class RecordingApi:
    """Records every body; a single dispatch goes through the real `task_payload`."""

    def __init__(self, runtimes: Any = None) -> None:
        self.sent: list[tuple[str, str, Any]] = []
        self.runtimes = runtimes
        self.runs: list[dict[str, Any]] = []

    def request(self, method: str, path: str, *, payload: Any = None, timeout: int = 60) -> Any:
        self.sent.append((method, path, payload))
        if path == "/v1/stats":
            return {"limits": {"max_batch_size": 5}}
        if path == "/v1/runtimes" and self.runtimes is not None:
            return {"runtimes": self.runtimes}
        if method == "POST" and path == "/v1/workflows":
            steps = [{"step_id": s["step_id"], "task_id": f"task_{s['step_id']}",
                      "runner_profile": s.get("runner_profile")} for s in payload["steps"]]
            return {"workflow": {"workflow_id": "wf_1", "tenant_id": "eng", "steps": steps},
                    "dispatch": {"strategy": "collect"}, "tenant_id": "eng"}
        if method == "POST" and path == "/v1/runs":
            return {"run": {"id": "run_1", "state": "PLANNING", "issue": payload["issue"]}}
        raise SwarmError(f"{method} {path} -> 404: no such route", status=404)

    def dispatch(self, **fields: Any) -> dict[str, Any]:
        payload = task_payload(**fields)
        self.sent.append(("POST", "/v1/tasks", payload))
        return {"id": f"task_{len(self.sent)}", "state": "QUEUED"}

    def dispatch_batch(self, payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.sent.append(("POST", "/v1/tasks/batch", {"tasks": list(payloads)}))
        return [{"id": f"task_b{i}", "state": "QUEUED"} for i, _ in enumerate(payloads, start=1)]

    def create_run(self, **fields: Any) -> dict[str, Any]:
        # The real method, on this recorder: what is asserted is its body.
        return SwarmClient.create_run(self, **fields)  # type: ignore[arg-type]


def _posted(api: RecordingApi, path: str) -> list[Any]:
    return [payload for method, path_, payload in api.sent if method == "POST" and path_ == path]


# --------------------------------------------------------------------------
# swarm_profiles: the classes and their models
# --------------------------------------------------------------------------


def _entry(listing: dict[str, Any], name: str) -> dict[str, Any]:
    (entry,) = [e for e in listing["profiles"] if e["name"] == name]
    return entry


def test_the_bundled_copy_lists_each_profile_s_classes_and_default():
    listing = profiles.served_catalogue(RecordingApi())  # /v1/runtimes answers 404
    assert listing["catalogue_source"] != profiles.CATALOGUE_FROM_PLATFORM
    for name, profile in RUNNER_PROFILES.items():
        entry = _entry(listing, name)
        assert entry["model_classes"] == list(profile.model_classes), name
        assert entry["default_model_class"] == profile.default_model_class, name
    code = _entry(listing, "claude-code")
    assert code["model_classes"] == list(MODEL_CLASSES)
    assert code["default_model_class"] == "opus"
    codex = _entry(listing, "codex")
    assert codex["model_classes"] == [] and codex["default_model_class"] is None
    assert codex["models"] == {}


def test_the_bundled_copy_says_the_model_ids_are_the_platform_s():
    """The copy knows each class's family, never its pin: an ID here would be a
    second copy of Terraform's `runner_models`, stale the day it is re-pinned."""
    code = _entry(profiles.served_catalogue(RecordingApi()), "claude-code")
    assert code["models"] == {}, "the bundled copy must not invent the pinned IDs"
    note = code["models_note"]
    assert "GET /v1/runtimes" in note
    for name, cls in MODEL_CLASSES.items():
        assert f"{name}: claude-{cls.family}-" in note, note


def test_the_platform_s_classes_and_models_pass_through():
    served = {
        "claude-code": {
            "name": "claude-code", "available": True, "resolved_backend": "gke",
            "resource_class": "standard", "provider": "anthropic", "timeout_seconds": 3600,
            "resources": {"cpu": 4, "memory_gib": 8, "disk_gib": 4},
            "model_classes": ["opus", "haiku"], "default_model_class": "opus",
            "models": {"opus": PINS["opus"], "haiku": PINS["haiku"]},
        },
        "mock": {
            "name": "mock", "available": True, "resolved_backend": "cloud_run",
            "resource_class": "standard", "provider": None, "timeout_seconds": 600,
            "resources": {"cpu": 4, "memory_gib": 8, "disk_gib": 4},
            "model_classes": [], "default_model_class": None, "models": {},
        },
    }
    listing = profiles.served_catalogue(RecordingApi(runtimes=served))
    assert listing["catalogue_source"] == profiles.CATALOGUE_FROM_PLATFORM
    code = _entry(listing, "claude-code")
    # The platform's answer, not the copy's: it serves two classes here.
    assert code["model_classes"] == ["opus", "haiku"]
    assert code["default_model_class"] == "opus"
    assert code["models"] == {"opus": PINS["opus"], "haiku": PINS["haiku"]}
    assert "models_note" not in code
    mock = _entry(listing, "mock")
    assert mock["model_classes"] == [] and mock["models"] == {}


def test_a_platform_older_than_the_classes_is_said_not_guessed():
    served = {"claude-code": {
        "name": "claude-code", "available": True, "resolved_backend": "gke",
        "resource_class": "standard", "provider": "anthropic", "timeout_seconds": 3600,
        "resources": {"cpu": 4, "memory_gib": 8, "disk_gib": 4},
    }}
    code = _entry(profiles.served_catalogue(RecordingApi(runtimes=served)), "claude-code")
    assert code["model_classes"] == list(RUNNER_PROFILES["claude-code"].model_classes)
    assert code["models"] == {}
    assert "does not serve" in code["models_note"]


# --------------------------------------------------------------------------
# Sent on a task, a step and a run
# --------------------------------------------------------------------------


def test_the_client_sends_model_class_on_a_task_and_none_when_not_named():
    assert task_payload(prompt="x", model_class="haiku")["model_class"] == "haiku"
    assert "model_class" not in task_payload(prompt="x")


def test_swarm_dispatch_sends_model_class():
    api = RecordingApi()
    server._call(api, "swarm_dispatch", {"prompt": _prompt(), "model_class": "sonnet"})
    (body,) = _posted(api, "/v1/tasks")
    assert body["model_class"] == "sonnet"
    assert "model_class" not in body["input"]


def test_a_dispatch_naming_no_class_sends_no_key():
    api = RecordingApi()
    server._call(api, "swarm_dispatch", {"prompt": _prompt()})
    (body,) = _posted(api, "/v1/tasks")
    assert "model_class" not in body


def test_a_batch_task_sends_its_own_class():
    api = RecordingApi()
    server._call(api, "swarm_dispatch", {"tasks": [
        {"prompt": _prompt(), "model_class": "haiku"},
        {"prompt": _prompt()},
    ]})
    (batch,) = _posted(api, "/v1/tasks/batch")
    first, second = batch["tasks"]
    assert first["model_class"] == "haiku"
    assert "model_class" not in second


@pytest.mark.parametrize(
    ("args", "match"),
    [
        ({"model_class": "claude-haiku-5-5"}, r"no model class called 'claude-haiku-5-5'"),
        ({"model_class": "gpt"}, r"no model class called 'gpt'"),
        ({"model_class": "haiku", "profile": "mock"}, r"mock runs no Claude model"),
    ],
)
def test_a_class_the_profile_cannot_run_is_refused_before_any_request(args, match):
    api = RecordingApi()
    with pytest.raises(SwarmError, match=match):
        server._call(api, "swarm_dispatch", {"prompt": _prompt(), **args})
    assert not _posted(api, "/v1/tasks")


def test_swarm_workflow_steps_send_each_step_s_class():
    api = RecordingApi()
    server._call(api, "swarm_workflow", {"steps": [
        {"step_id": "plan", "prompt": "plan it", "model_class": "fable"},
        {"step_id": "build", "prompt": "build it", "depends_on": ["plan"]},
        {"step_id": "tidy", "prompt": "reformat it", "depends_on": ["build"],
         "model_class": "haiku"},
    ]})
    (body,) = _posted(api, "/v1/workflows")
    steps = {s["step_id"]: s for s in body["steps"]}
    assert steps["plan"]["model_class"] == "fable"
    assert "model_class" not in steps["build"], "a step naming none runs its profile's default"
    assert steps["tidy"]["model_class"] == "haiku"


def test_a_whole_spec_carries_its_steps_classes():
    spec = {"steps": [
        {"step_id": "a", "prompt": "a", "model_class": "sonnet"},
        {"step_id": "b", "prompt": "b", "depends_on": ["a"]},
    ]}
    api = RecordingApi()
    server._call(api, "swarm_workflow", {"spec": spec, "spec_digest": workflows.spec_digest(spec)})
    (body,) = _posted(api, "/v1/workflows")
    assert body["steps"][0]["model_class"] == "sonnet"
    assert "model_class" not in body["steps"][1]


def test_a_step_class_the_profile_cannot_run_submits_nothing():
    api = RecordingApi()
    with pytest.raises(SwarmError, match=r"step 'a'.*mock runs no Claude model"):
        server._call(api, "swarm_workflow", {"steps": [
            {"step_id": "a", "prompt": "a", "runner_profile": "mock", "model_class": "haiku"},
        ]})
    assert not _posted(api, "/v1/workflows")


def test_swarm_run_issue_sends_the_planner_s_class():
    api = RecordingApi()
    server._call(api, "swarm_run_issue", {"issue": "acme/api#7", "model_class": "fable"})
    (body,) = _posted(api, "/v1/runs")
    assert body["model_class"] == "fable"


def test_a_run_naming_no_class_sends_no_key():
    api = RecordingApi()
    server._call(api, "swarm_run_issue", {"issue": "acme/api#8"})
    (body,) = _posted(api, "/v1/runs")
    assert "model_class" not in body


def test_a_run_naming_an_unknown_class_sends_nothing():
    api = RecordingApi()
    with pytest.raises(SwarmError, match=r"no model class called 'gpt'"):
        server._call(api, "swarm_run_issue", {"issue": "acme/api#9", "model_class": "gpt"})
    assert not _posted(api, "/v1/runs")


def test_the_tools_offer_the_classes_by_name():
    tools = {tool["name"]: tool["inputSchema"]["properties"] for tool in server.TOOLS}
    for offered in (
        tools["swarm_dispatch"]["model_class"],
        tools["swarm_dispatch"]["tasks"]["items"]["properties"]["model_class"],
        tools["swarm_workflow"]["steps"]["items"]["properties"]["model_class"],
        tools["swarm_run_issue"]["model_class"],
    ):
        assert offered["enum"] == list(MODEL_CLASSES)


# --------------------------------------------------------------------------
# The terminal: --model-class selects, --model records
# --------------------------------------------------------------------------


def _dispatch_parser() -> argparse.ArgumentParser:
    parser = cli.build_parser()
    (subparsers,) = [
        a for a in parser._actions  # noqa: SLF001 -- argparse exposes no public walk
        if isinstance(a, argparse._SubParsersAction)  # noqa: SLF001
    ]
    return subparsers.choices["dispatch"]


def _help(flag: str) -> str:
    for action in _dispatch_parser()._actions:  # noqa: SLF001
        if flag in action.option_strings:
            return " ".join((action.help or "").lower().split())
    raise AssertionError(f"`swarm dispatch` has no {flag} flag")


def test_dispatch_has_model_class_and_model_still_only_records():
    assert _help("--model-class")
    model = _help("--model")
    assert "attribution" in model and "not select" in model, model
    assert "--model-class" in model, "the help must say how a model IS chosen"


def test_model_class_reaches_the_task_from_the_terminal():
    api = RecordingApi()
    args = cli.build_parser().parse_args(
        ["dispatch", _prompt(), "--model-class", "haiku"]
    )
    assert cli.cmd_dispatch(api, args) == cli.EXIT_OK
    (body,) = _posted(api, "/v1/tasks")
    assert body["model_class"] == "haiku"


def test_the_terminal_refuses_a_model_id_as_a_class():
    api = RecordingApi()
    args = cli.build_parser().parse_args(
        ["dispatch", _prompt(), "--model-class", "claude-opus-5-5"]
    )
    with pytest.raises(SwarmError, match=r"no model class called 'claude-opus-5-5'"):
        cli.cmd_dispatch(api, args)
    assert not api.sent


def test_the_launch_reads_a_spec_whose_steps_name_classes(monkeypatch):
    """`/sc run <spec>` titles the run with `swarm_workflow_launch` first, and
    `launch.for_spec` reads the spec with `workflows.read_spec`, which holds no
    `model_class`: a spec naming a class must reach it without the key, and
    one naming a class its profile cannot run must be refused before it."""
    from swarm_mcp import launch

    seen: list[Any] = []

    def for_spec(document: Any, client: Any = None) -> dict[str, Any]:
        workflows.read_spec(document)  # what the real one calls first
        seen.append(document)
        return {"steps": len(document["steps"])}

    monkeypatch.setattr(launch, "for_spec", for_spec)
    spec = {"steps": [{"step_id": "a", "prompt": "a", "model_class": "haiku"}]}
    server._call(RecordingApi(), "swarm_workflow_launch", {"spec": spec})
    assert seen and "model_class" not in seen[0]["steps"][0]
    assert spec["steps"][0]["model_class"] == "haiku", "the caller's spec is not edited"
    bad = {"steps": [{"step_id": "a", "prompt": "a", "runner_profile": "mock",
                      "model_class": "haiku"}]}
    with pytest.raises(SwarmError, match=r"mock runs no Claude model"):
        server._call(RecordingApi(), "swarm_workflow_launch", {"spec": bad})
    assert len(seen) == 1


def test_a_spec_file_through_the_terminal_carries_its_steps_classes(tmp_path):
    import json

    spec = {"steps": [
        {"step_id": "a", "prompt": "a", "model_class": "fable"},
        {"step_id": "b", "prompt": "b", "depends_on": ["a"]},
    ]}
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    api = RecordingApi()
    args = cli.build_parser().parse_args(["workflow", str(path)])

    assert cli.cmd_workflow(api, args) == cli.EXIT_OK

    (body,) = _posted(api, "/v1/workflows")
    assert body["steps"][0]["model_class"] == "fable"
    assert "model_class" not in body["steps"][1]
