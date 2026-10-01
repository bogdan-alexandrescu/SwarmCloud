"""`sc config` and `sc debug` (owner decisions 2026-10-01).

`sc config` is one read-only view over what `sc context` and `sc whoami`
already read, plus the S8 target and the two versions. `sc debug <task>` is
one task's diagnosis from the API's own reads. Both print a field that could
not be read WITH the reason, and neither prints a secret.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from swarm_mcp import config, sc, server
from swarm_mcp.client import SwarmError

_REPO = Path(__file__).resolve().parents[3]
_PLUGIN_VERSION = json.loads((_REPO / "plugin" / ".claude-plugin" / "plugin.json").read_text())["version"]

#: Unique, so its absence from the output is measurable -- and not shaped like
#: a real secret, so no scanner reads this file as holding one.
FAKE_SECRET = "not-a-real-client-secret-0000"
FAKE_TOKEN = "fake.id.token.for.tests"


class FakeDeploymentApi:
    def __init__(self, me=None, error=None):
        self.me = me if me is not None else {"tenant": {"tenant_id": "acme"}, "principal": {"email": "a@acme.test"}}
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def request(self, method, path, **kwargs):
        if self.error is not None:
            raise self.error
        assert (method, path) == ("GET", "/v1/tenants/me")
        return self.me


def _deployment():
    return SimpleNamespace(
        context="qa", url="https://swarm.example.test", source="current context in /tmp/config.json",
        current=True, client_id="", front_door=False, client_secret=FAKE_SECRET,
    )


@pytest.fixture
def deployed(monkeypatch):
    monkeypatch.setenv("SWARM_ID_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("SWARM_OAUTH_CLIENT_SECRET", FAKE_SECRET)
    monkeypatch.setenv(config.plugin_env(config.PLUGIN_CLIENT_SECRET), FAKE_SECRET)
    monkeypatch.setattr(sc, "_resolve", lambda args: _deployment())

    def use(api):
        monkeypatch.setattr(sc, "SwarmClient", lambda *a, **k: api)

    return use


def _config(argv=()):
    out = io.StringIO()
    args = sc.build_parser().parse_args(["config", *argv])
    code = args.func(None, args, out)
    return code, out.getvalue()


def test_config_shows_every_field_from_a_fake_deployment(deployed, monkeypatch):
    deployed(FakeDeploymentApi())
    monkeypatch.setenv(config.plugin_env(config.PLUGIN_TARGET), "cloud")
    code, text = _config(["--json"])
    shown = json.loads(text)
    assert code == sc.EXIT_OK, shown
    assert shown["endpoint"]["value"] == "https://swarm.example.test"
    assert shown["endpoint"]["context"] == "qa" and "current context" in shown["endpoint"]["source"]
    assert shown["tenant"]["value"] == "acme"
    assert shown["target"]["value"] == "cloud" and "default_target" in shown["target"]["source"]
    assert shown["plugin_version"]["value"] == _PLUGIN_VERSION
    assert shown["bridge_version"]["value"]


def test_config_text_names_each_field(deployed):
    deployed(FakeDeploymentApi())
    _, text = _config()
    for label in ("endpoint", "tenant", "target", "plugin", "bridge"):
        assert any(line.startswith(label) for line in text.splitlines()), text
    assert _PLUGIN_VERSION in text and "hybrid" in text


def test_the_target_override_is_shown_as_the_override(deployed):
    deployed(FakeDeploymentApi())
    _, text = _config(["--json", "--target", "local"])
    assert json.loads(text)["target"] == {"value": "local", "source": "--target"}


def test_an_unreadable_tenant_is_shown_as_not_read_with_the_reason(deployed):
    deployed(FakeDeploymentApi(error=SwarmError("GET /v1/tenants/me -> 503: unavailable", status=503)))
    code, text = _config(["--json"])
    shown = json.loads(text)
    assert code == sc.EXIT_FAIL
    assert shown["tenant"]["value"] is None and "503" in shown["tenant"]["not_read_because"]
    # The other fields still came back.
    assert shown["endpoint"]["value"] == "https://swarm.example.test"
    _, human = _config()
    assert any(line.startswith("tenant") and "not read" in line and "503" in line for line in human.splitlines()), human


def test_a_missing_plugin_manifest_is_shown_as_not_read(deployed, monkeypatch, tmp_path):
    deployed(FakeDeploymentApi())
    monkeypatch.setenv("SWARM_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    shown = json.loads(_config(["--json"])[1])
    assert shown["plugin_version"]["value"] is None and shown["plugin_version"]["not_read_because"]


def test_config_never_prints_a_secret_or_token(deployed):
    deployed(FakeDeploymentApi())
    for argv in ((), ("--json",)):
        _, text = _config(argv)
        assert FAKE_SECRET not in text and FAKE_TOKEN not in text


def test_an_invalid_target_is_refused_at_startup(monkeypatch, capsys):
    monkeypatch.setenv(config.plugin_env(config.PLUGIN_TARGET), "sideways")
    monkeypatch.setattr(sc, "SwarmClient", lambda *a, **k: pytest.fail("a client was built past a bad target"))
    assert sc.main(["whoami"]) == sc.EXIT_FAIL
    err = capsys.readouterr().err
    for target in config.TARGETS:
        assert target in err


def test_config_still_runs_with_an_invalid_target_and_shows_it_as_not_read(deployed, monkeypatch):
    """`sc config` is how an operator finds the bad setting, so start-up does
    not stop it; it shows the target as not read, with the same reason."""
    deployed(FakeDeploymentApi())
    monkeypatch.setenv(config.plugin_env(config.PLUGIN_TARGET), "sideways")
    out = io.StringIO()
    assert sc.main(["config", "--json"], out=out) == sc.EXIT_FAIL
    shown = json.loads(out.getvalue())
    assert shown["target"]["value"] is None
    for target in config.TARGETS:
        assert target in shown["target"]["not_read_because"]
    assert shown["endpoint"]["value"] == "https://swarm.example.test"


# ==========================================================================
# sc debug
# ==========================================================================


class FakeTaskApi:
    """The four reads `sc debug` makes, each of which can be made to fail."""

    def __init__(self, fail=()):
        self.fail = set(fail)
        self.calls: list[str] = []

    def _maybe(self, what):
        self.calls.append(what)
        if what in self.fail:
            raise SwarmError(f"GET {what} -> 503: unavailable", status=503)

    def task(self, task_id):
        self._maybe("task")
        return {
            "id": task_id, "state": "FAILED", "runner_profile": "claude-code",
            "last_error": "claude exited 1: token ********", "last_error_redaction_count": 1,
            "input_redaction_count": 2, "metadata_redaction_count": 0,
        }

    def attempts(self, task_id, *, limit=20):
        self._maybe("attempts")
        return [
            {"attempt_id": "att_2", "generation": 2, "started_at": "2026-10-01T10:05:00Z",
             "completed_at": "2026-10-01T10:09:00Z", "exit_code": 1,
             "error": "boom ********", "error_redaction_count": 1},
            {"attempt_id": "att_1", "generation": 1, "started_at": "2026-10-01T10:00:00Z",
             "completed_at": "2026-10-01T10:01:00Z", "exit_code": None,
             "error": "preempted", "error_redaction_count": 0},
        ]

    def events_page(self, task_id, *, limit=50, newest_first=False, page_token=None):
        self._maybe("events")
        assert newest_first
        events = [
            {"type": "failed", "at": "2026-10-01T10:09:00Z", "attempt_id": "att_2",
             "detail": {"error": "boom ********"}, "detail_redaction_count": 1},
            {"type": "started", "at": "2026-10-01T10:05:00Z", "attempt_id": "att_2",
             "detail": {}, "detail_redaction_count": 0},
        ]
        return events[:limit], None, True

    def logs(self, task_id, *, attempt_id=None, stream=None, source="auto", offset=0, limit_bytes=None, timeout=60):
        self._maybe("logs")
        assert attempt_id in (None, "att_2")
        text = "".join(f"line {i}\n" for i in range(100)) + "Authorization: Bearer ********\n"
        total = len(text.encode())
        window = text.encode()[offset: offset + (limit_bytes or total)].decode()
        return {
            "attempt_id": "att_2",
            "redaction": {"applied_at_read_time": True, "rules": 30},
            "streams": [{
                "stream": stream, "status": "ok", "content": window, "total_bytes": total,
                "offset": offset, "redaction_count": 1 if "Bearer" in window else 0,
            }],
        }


def _debug(api, *argv):
    out = io.StringIO()
    args = sc.build_parser().parse_args(["debug", "task_1", *argv])
    code = args.func(api, args, out)
    return code, out.getvalue()


def test_debug_prints_every_section_from_a_fake_client():
    code, text = _debug(FakeTaskApi())
    assert code == sc.EXIT_OK, text
    assert "FAILED" in text and "claude-code" in text
    assert "att_2" in text and "att_1" in text and "generation 2" in text
    assert "exit 1" in text and "exit not recorded" in text
    assert "claude exited 1: token ********" in text
    assert "failed" in text and "started" in text
    assert "line 99" in text and "line 0\n" not in text, "the tail, not the head"
    assert "masked" in text


def test_debug_json_carries_each_section():
    _, text = _debug(FakeTaskApi(), "--json")
    shown = json.loads(text)
    assert set(shown) >= {"task", "attempts", "events", "log_tail", "masked"}
    assert [a["attempt_id"] for a in shown["attempts"]["value"]] == ["att_2", "att_1"]
    assert shown["log_tail"]["value"]["lines"][-1] == "Authorization: Bearer ********"
    assert len(shown["log_tail"]["value"]["lines"]) <= sc.DEBUG_LOG_LINES


@pytest.mark.parametrize("failing", ["attempts", "events", "logs"])
def test_one_failing_read_does_not_hide_the_others(failing):
    code, text = _debug(FakeTaskApi(fail={failing}), "--json")
    shown = json.loads(text)
    assert code == sc.EXIT_FAIL
    section = {"attempts": "attempts", "events": "events", "logs": "log_tail"}[failing]
    assert shown[section]["value"] is None and "503" in shown[section]["not_read_because"]
    for other in {"task", "attempts", "events", "log_tail"} - {section}:
        assert shown[other]["value"] is not None, other


def test_an_unreadable_task_still_prints_the_rest():
    code, text = _debug(FakeTaskApi(fail={"task"}))
    assert code == sc.EXIT_FAIL
    assert "not read" in text and "att_2" in text


def test_a_credential_shaped_string_in_a_log_line_comes_out_masked():
    """The API masks at read time and says so; `sc debug` prints its masked copy
    and its count -- and a window that does NOT carry the API's statement that
    it was masked is withheld, never printed raw."""
    _, text = _debug(FakeTaskApi())
    assert "Bearer ********" in text

    class Unmasked(FakeTaskApi):
        def logs(self, task_id, **kwargs):
            served = super().logs(task_id, **kwargs)
            served.pop("redaction")
            served["streams"][0]["content"] = "Authorization: Bearer fake-unmasked-credential-000\n"
            return served

        def attempts(self, task_id, *, limit=20):
            rows = super().attempts(task_id, limit=limit)
            rows[0] = {**rows[0], "error": "fake-unmasked-credential-111"}
            rows[0].pop("error_redaction_count")
            return rows

    code, text = _debug(Unmasked())
    assert "fake-unmasked-credential" not in text
    assert "withheld" in text


def test_the_mcp_debug_tool_is_the_same_report():
    body = json.loads(server._call(FakeTaskApi(), "swarm_debug", {"task_id": "task_1"}))
    assert body["task"]["value"]["state"] == "FAILED"
    assert body["log_tail"]["value"]["lines"][-1] == "Authorization: Bearer ********"


def test_debug_reads_only_the_api():
    """Never GCS or Cloud Logging directly: the four client reads, and nothing else."""
    api = FakeTaskApi()
    _debug(api)
    assert set(api.calls) <= {"task", "attempts", "events", "logs"}
    import inspect

    source = inspect.getsource(sc.debug_report)
    for forbidden in ("storage.googleapis", "logging.googleapis", "gs://", "download("):
        assert forbidden not in source
