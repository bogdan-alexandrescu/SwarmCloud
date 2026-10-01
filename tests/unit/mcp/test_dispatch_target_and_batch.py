"""Where a dispatch goes (S8), N tasks in one request (S7), and collecting them.

BUILD_PROMPT_V2 2.4 names `swarm.dispatch(tasks[])` and `swarm.collect(ids)`;
2.5 names a session default `target` and a per-call override. Every test here
drives `server._call` against a fake control plane that RECORDS what it was
sent, so "nothing was sent" is a measurement: the fake's lists are empty.
"""

from __future__ import annotations

import json

import pytest

from swarm_mcp import config, server
from swarm_mcp.client import SwarmClient, SwarmError, task_payload

TARGET_ENV = config.plugin_env(config.PLUGIN_TARGET)


class FakeApi:
    """Answers `/v1/stats`, a single dispatch and a batch; counts each."""

    def __init__(self, *, max_batch_size=5, tasks=None, answers=None, stats_error=None):
        self.max_batch_size = max_batch_size
        self.stats_error = stats_error
        self.tasks = tasks or {}
        self.answers = answers or {}
        self.dispatched: list[dict] = []
        self.batches: list[list[dict]] = []
        self.requests: list[tuple[str, str]] = []
        self.artifact_reads: list[str] = []

    def request(self, method, path, **kwargs):
        self.requests.append((method, path))
        if path == "/v1/stats":
            if self.stats_error is not None:
                raise self.stats_error
            return {"limits": {"max_batch_size": self.max_batch_size}}
        raise SwarmError(f"{method} {path} -> 500: no such route", status=500)

    def dispatch(self, **kwargs):
        self.dispatched.append(kwargs)
        return {"id": f"task_{len(self.dispatched)}", "state": "QUEUED"}

    def dispatch_batch(self, payloads):
        self.batches.append(list(payloads))
        return [{"id": f"task_b{i}", "state": "QUEUED"} for i, _ in enumerate(payloads, start=1)]

    def task(self, task_id):
        if task_id not in self.tasks:
            raise SwarmError(f"GET /v1/tasks/{task_id} -> 404: no such task", status=404)
        return self.tasks[task_id]

    def attempts(self, task_id, *, limit=20):
        return []

    def artifacts(self, task_id):
        self.artifact_reads.append(task_id)
        return {"artifacts": [], "complete": True}


def _sent_nothing(api: FakeApi) -> bool:
    return not api.dispatched and not api.batches and not api.requests


def _call(api, name, args):
    return json.loads(server._call(api, name, args))


# ==========================================================================
# S8: the session default and the per-call target
# ==========================================================================


def test_local_refuses_and_sends_nothing(monkeypatch):
    monkeypatch.setenv(TARGET_ENV, "local")
    api = FakeApi()
    with pytest.raises(SwarmError) as refused:
        server._call(api, "swarm_dispatch", {"prompt": "fix it", "profile": "claude-code"})
    text = str(refused.value)
    assert "run locally in this session" in text
    assert "local" in text and "default_target" in text, text
    assert _sent_nothing(api)


@pytest.mark.parametrize("tool,args", [
    ("swarm_dispatch", {"prompt": "fix it"}),
    ("swarm_dispatch", {"tasks": [{"prompt": "a"}, {"prompt": "b"}]}),
    ("swarm_workflow", {"steps": [{"step_id": "a", "prompt": "a"}]}),
])
def test_every_dispatching_tool_honours_a_per_call_local(tool, args):
    api = FakeApi()
    with pytest.raises(SwarmError) as refused:
        server._call(api, tool, {**args, "target": "local"})
    assert "this call's `target`" in str(refused.value)
    assert _sent_nothing(api)


def test_hybrid_with_needs_local_refuses_naming_the_reason():
    api = FakeApi()
    with pytest.raises(SwarmError) as refused:
        server._call(api, "swarm_dispatch", {
            "prompt": "use my keychain", "profile": "claude-code",
            "target": "hybrid", "needs_local": ["keychain"],
        })
    text = str(refused.value)
    assert "keychain" in text and "hybrid" in text
    assert _sent_nothing(api)


def test_hybrid_with_a_profile_and_no_needs_local_dispatches():
    api = FakeApi()
    body = _call(api, "swarm_dispatch", {"prompt": "fix it", "profile": "claude-code", "target": "hybrid"})
    assert body["task_id"] == "task_1"
    assert body["target"]["applied"] == "hybrid"
    assert body["target"]["sent_to"] == "cloud"
    assert len(api.dispatched) == 1


def test_the_per_call_target_overrides_the_session_default(monkeypatch):
    monkeypatch.setenv(TARGET_ENV, "local")
    api = FakeApi()
    body = _call(api, "swarm_dispatch", {"prompt": "fix it", "target": "cloud"})
    assert body["target"] == {"applied": "cloud", "from": "this call's `target`", "sent_to": "cloud"}
    assert len(api.dispatched) == 1


def test_the_answer_names_the_session_default_when_the_call_says_nothing(monkeypatch):
    monkeypatch.setenv(TARGET_ENV, "cloud")
    body = _call(FakeApi(), "swarm_dispatch", {"prompt": "fix it"})
    assert body["target"]["applied"] == "cloud"
    assert "default_target" in body["target"]["from"]


def test_with_nothing_set_the_default_is_hybrid():
    body = _call(FakeApi(), "swarm_dispatch", {"prompt": "fix it"})
    assert body["target"]["applied"] == "hybrid"
    assert "built-in" in body["target"]["from"]


def test_cloud_sends_exactly_what_it_sent_before_the_target_existed():
    """`cloud` is today's behaviour, byte for byte: the request is unchanged."""
    api = FakeApi()
    _call(api, "swarm_dispatch", {"prompt": "fix it", "profile": "mock", "label": "u1", "target": "cloud"})
    assert api.dispatched == [{
        "prompt": "fix it", "runner_profile": "mock", "repository_url": None,
        "repository_ref": None, "metadata": {"unit": "u1"}, "inputs": None, "strategy": None,
    }]


@pytest.mark.parametrize("bad", ["sideways", "", "CLOUDY"])
def test_an_invalid_per_call_target_is_refused_naming_the_three(bad):
    api = FakeApi()
    with pytest.raises(SwarmError) as refused:
        server._call(api, "swarm_dispatch", {"prompt": "x", "target": bad or " "})
    for allowed in config.TARGETS:
        assert allowed in str(refused.value)
    assert _sent_nothing(api)


def test_an_unknown_needs_local_entry_is_refused():
    api = FakeApi()
    with pytest.raises(SwarmError) as refused:
        server._call(api, "swarm_dispatch", {"prompt": "x", "needs_local": ["vibes"]})
    assert "filesystem" in str(refused.value)
    assert _sent_nothing(api)


def test_an_invalid_session_default_is_refused_at_startup(monkeypatch, capsys):
    monkeypatch.setenv(TARGET_ENV, "sideways")
    monkeypatch.setattr(server, "serve", lambda *a, **k: pytest.fail("served with an invalid target"))
    monkeypatch.setattr(server, "seed_plugin_config", lambda: None)
    assert server.main() != 0
    err = capsys.readouterr().err
    for allowed in config.TARGETS:
        assert allowed in err


def test_the_config_file_sets_the_default_for_a_terminal_and_survives_a_rewrite(tmp_path, monkeypatch):
    environ = {"SWARM_CONFIG_DIR": str(tmp_path)}
    (tmp_path / "config.json").write_text(json.dumps({"default_target": "local", "contexts": {}}))
    assert config.session_target(environ).value == "local"
    config.add_context("dev", "https://swarm.example.test", environ=environ)
    assert config.session_target(environ).value == "local", "a context rewrite dropped the setting"
    assert config.session_target(environ, override="cloud").source == "--target"


def test_the_plugin_setting_outranks_the_config_file(tmp_path):
    environ = {"SWARM_CONFIG_DIR": str(tmp_path), TARGET_ENV: "cloud"}
    (tmp_path / "config.json").write_text(json.dumps({"default_target": "local", "contexts": {}}))
    assert config.session_target(environ).value == "cloud"
    # An unsubstituted reference is unset, not a value.
    environ[TARGET_ENV] = "${user_config.default_target}"
    assert config.session_target(environ).value == "local"


# ==========================================================================
# S7: a batch is one request
# ==========================================================================


def test_a_three_task_batch_is_one_post_returning_three_ids_in_order():
    api = FakeApi()
    body = _call(api, "swarm_dispatch", {"tasks": [
        {"prompt": "one", "runner_profile": "mock"},
        {"prompt": "two", "runner_profile": "claude-code", "label": "second"},
        {"prompt": "three"},
    ]})
    assert body["task_ids"] == ["task_b1", "task_b2", "task_b3"]
    assert len(api.batches) == 1 and not api.dispatched
    sent = api.batches[0]
    assert [p["input"]["prompt"] for p in sent] == ["one", "two", "three"]
    assert [p["runner_profile"] for p in sent] == ["mock", "claude-code", "claude-code"]
    assert sent[1]["metadata"]["unit"] == "second"
    assert body["target"]["applied"] == "hybrid"


def test_the_batch_payload_is_the_single_dispatch_payload():
    """One builder for both shapes, so a field added to one reaches the other."""
    seen = []

    class _Self:
        def request(self, method, path, payload=None, **kwargs):
            seen.append((method, path, payload))
            return {"tasks": [{"task": {"id": "t1", "state": "QUEUED"}}]}

    payload = task_payload(prompt="p", runner_profile="mock", strategy="direct-pr", repository_url="https://g/x.git")
    created = SwarmClient.dispatch_batch(_Self(), [payload])
    assert created == [{"id": "t1", "state": "QUEUED"}]
    assert seen == [("POST", "/v1/tasks/batch", {"tasks": [payload]})]


def test_a_batch_over_the_apis_limit_is_refused_before_sending():
    api = FakeApi(max_batch_size=2)
    with pytest.raises(SwarmError) as refused:
        server._call(api, "swarm_dispatch", {"tasks": [{"prompt": str(i)} for i in range(3)]})
    assert "max_batch_size" in str(refused.value) and "2" in str(refused.value)
    assert not api.batches and not api.dispatched


def test_an_empty_batch_is_refused():
    api = FakeApi()
    with pytest.raises(SwarmError):
        server._call(api, "swarm_dispatch", {"tasks": []})
    assert not api.batches


def test_a_repeat_inside_a_batch_is_refused_before_sending():
    api = FakeApi()
    with pytest.raises(SwarmError) as refused:
        server._call(api, "swarm_dispatch", {"tasks": [{"prompt": "same"}, {"prompt": "same"}]})
    assert "already" in str(refused.value) or "twice" in str(refused.value)
    assert not api.batches


def test_a_batch_task_already_dispatched_singly_is_refused():
    api = FakeApi()
    _call(api, "swarm_dispatch", {"prompt": "once", "profile": "mock"})
    with pytest.raises(SwarmError):
        server._call(api, "swarm_dispatch", {"tasks": [{"prompt": "new"}, {"prompt": "once", "runner_profile": "mock"}]})
    assert not api.batches


def test_a_batch_is_not_mixed_with_the_single_task_fields():
    api = FakeApi()
    with pytest.raises(SwarmError) as refused:
        server._call(api, "swarm_dispatch", {"prompt": "x", "tasks": [{"prompt": "y"}]})
    assert "tasks" in str(refused.value)
    assert _sent_nothing(api)


def test_a_batch_task_refuses_a_field_dispatch_does_not_take():
    api = FakeApi()
    with pytest.raises(SwarmError):
        server._call(api, "swarm_dispatch", {"tasks": [{"prompt": "x", "image": "evil:latest"}]})
    assert not api.batches


# ==========================================================================
# S7: collect
# ==========================================================================


def _task(task_id, state, **extra):
    return {"id": task_id, "state": state, "runner_profile": "mock", **extra}


def test_collect_reports_finished_tasks_with_results_and_running_ones_as_not_finished():
    api = FakeApi(tasks={
        "t_done": _task("t_done", "SUCCEEDED"),
        "t_run": _task("t_run", "RUNNING"),
        "t_failed": _task("t_failed", "FAILED", last_error="boom"),
    })
    body = _call(api, "swarm_collect", {"task_ids": ["t_done", "t_run", "t_failed"], "wait_seconds": 0})
    rows = {row["task_id"]: row for row in body["tasks"]}
    assert [row["task_id"] for row in body["tasks"]] == ["t_done", "t_run", "t_failed"]
    assert rows["t_done"]["finished"] is True and rows["t_done"]["state"] == "SUCCEEDED"
    assert "outputs" in rows["t_done"]["result"]
    assert rows["t_run"]["finished"] is False and rows["t_run"]["state"] == "RUNNING"
    assert "result" not in rows["t_run"], "an unfinished task must never be reported as a result"
    assert rows["t_failed"]["finished"] is True and rows["t_failed"]["result"]["state"] == "FAILED"
    assert body["still_running"] == ["t_run"]
    # Only the finished tasks' artifacts were read.
    assert sorted(api.artifact_reads) == ["t_done", "t_failed"]


def test_collect_reports_an_unreadable_task_as_unread_not_finished():
    body = _call(FakeApi(), "swarm_collect", {"task_ids": ["nope"], "wait_seconds": 0})
    row = body["tasks"][0]
    assert row["finished"] is None and row["state"] is None and "404" in row["read_error"]


def test_collect_reads_a_non_numeric_wait_as_the_default_not_a_crash():
    body = _call(FakeApi(), "swarm_collect", {"task_ids": ["nope"], "wait_seconds": "soon"})
    assert body["not_read"] == ["nope"]
