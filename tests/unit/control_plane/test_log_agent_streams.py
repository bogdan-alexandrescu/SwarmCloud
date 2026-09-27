"""`GET /v1/tasks/{id}/logs` serves the AGENT's streams too, says how old a live read is,
and never splices two versions of a republished tail.

#184. The drawer's "Output, as the agent wrote it" panel showed `stdout` and
`stderr` -- the RUNNER process's streams, its own JSON log lines -- and the
agent's own output sat in `artifacts/` as file names. What is pinned here:

  * `stream` repeats, and names `agent_stdout` / `agent_stderr` besides the
    runner's two; naming none is the runner's two, exactly as before;
  * the agent streams are read from `logs/agent_*.log`, the live
    `logs/live/agent_*.tail.log` mid-run, and -- for an attempt made before
    the worker published them -- the manifest entry the runner wrote, as
    `source: "artifact"`, only for the attempt the manifest describes;
  * a runner with no agent CLI answers `not_applicable`, a fourth answer;
  * every read carries `read_at`, and every stream its object's
    `object_updated_at` and `age_seconds` on the server's clock; a tail's
    header `at=` is `tail_window.published_at`, and a header without it
    still parses;
  * a live tail replaced mid-read is read once more, and on a second
    replacement is unreadable, never a window spliced from two objects.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from swarm_api.objects import InMemoryObjectReader

from .conftest import PROJECT, auth_header, seed_task, seed_tenant

BUCKET = f"swarm-artifacts-{PROJECT}"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def _attempt(db, attempt_id, *, minutes_ago=0, completed=False, task_id="task_a"):
    created = NOW - timedelta(minutes=minutes_ago)
    db.collection("attempts").document(attempt_id).set({
        "attempt_id": attempt_id,
        "task_id": task_id,
        "tenant_id": "eng",
        "generation": 1,
        "lease_id": f"lease_{attempt_id}",
        "backend": "CLOUD_RUN_JOB",
        "created_at": created,
        "started_at": created + timedelta(seconds=5),
        "completed_at": created + timedelta(minutes=1) if completed else None,
        "exit_code": 0 if completed else None,
        "checkpoints": [],
    })


def base(attempt="att_1", task="task_a") -> str:
    return f"tenants/eng/tasks/{task}/attempts/{attempt}"


def final_key(stream, **kw) -> str:
    return f"{base(**kw)}/logs/{stream}.log"


def live_key(stream, **kw) -> str:
    return f"{base(**kw)}/logs/live/{stream}.tail.log"


def get(client, query="", task="task_a", user="alice"):
    return client.get(f"/v1/tasks/{task}/logs{query}", headers=auth_header(user))


def stream_of(body, name):
    return next(s for s in body["streams"] if s["stream"] == name)


def a_task(db, *, state="RUNNING", runner_profile="claude-code", summary=None):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng", state=state, runner_profile=runner_profile)
    if summary is not None:
        db.docs["tasks/task_a"]["result_summary"] = summary


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


# --------------------------------------------------------------------------
# Which streams
# --------------------------------------------------------------------------

def test_no_stream_named_is_still_the_runners_two(client, db, objects):
    a_task(db, state="SUCCEEDED")
    _attempt(db, "att_1", completed=True)
    objects.put(final_key("stdout"), "runner out\n")
    objects.put(final_key("stderr"), '{"message":"child started"}\n')
    objects.put(final_key("agent_stdout"), '{"type":"result"}\n')

    body = get(client).json()
    assert [s["stream"] for s in body["streams"]] == ["stdout", "stderr"]
    assert body["read_at"], "every read says when it was made"


def test_stream_repeats_and_names_the_agents_own(client, db, objects):
    a_task(db, state="SUCCEEDED")
    _attempt(db, "att_1", completed=True)
    objects.put(final_key("stderr"), '{"message":"child started"}\n')
    objects.put(final_key("agent_stdout"), '{"type":"assistant"}\n{"type":"result"}\n')
    objects.put(final_key("agent_stderr"), "agent warning\n")

    body = get(client, "?stream=agent_stdout&stream=stderr&stream=agent_stderr").json()
    assert [s["stream"] for s in body["streams"]] == ["agent_stdout", "stderr", "agent_stderr"]
    out = stream_of(body, "agent_stdout")
    assert (out["status"], out["source"]) == ("ok", "final")
    assert out["content"] == '{"type":"assistant"}\n{"type":"result"}\n'
    assert out["key"] == final_key("agent_stdout")
    assert stream_of(body, "agent_stderr")["content"] == "agent warning\n"
    assert "child started" in stream_of(body, "stderr")["content"]


def test_a_stream_named_twice_is_served_once(client, db, objects):
    a_task(db, state="SUCCEEDED")
    _attempt(db, "att_1", completed=True)
    objects.put(final_key("agent_stdout"), "x\n")

    body = get(client, "?stream=agent_stdout&stream=agent_stdout").json()
    assert [s["stream"] for s in body["streams"]] == ["agent_stdout"]


def test_an_unknown_stream_is_refused(client, db, objects):
    a_task(db)
    _attempt(db, "att_1")
    assert get(client, "?stream=agent_transcript").status_code == 422
    assert get(client, "?stream=stdout&stream=syslog").status_code == 422


def test_the_agents_live_tail_is_served_mid_run_with_its_age(client, db, objects):
    a_task(db)
    _attempt(db, "att_1", minutes_ago=2)
    published = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=7)
    objects.put(
        live_key("agent_stdout"),
        f"#swarm-tail offset=1200 size=1300 at={published.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        '{"type":"assistant"}\n',
        updated=published,
    )

    body = get(client, "?stream=agent_stdout").json()
    out = stream_of(body, "agent_stdout")
    assert (out["status"], out["source"]) == ("ok", "live")
    assert out["content"] == '{"type":"assistant"}\n'
    assert out["tail_window"] == {
        "object_offset": 1200,
        "stream_size": 1300,
        "published_at": published.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    assert _parse(out["object_updated_at"]) == published
    age = (_parse(body["read_at"]) - published).total_seconds()
    assert out["age_seconds"] == round(age, 3)
    assert out["age_seconds"] >= 7


def test_a_header_without_at_still_parses(client, db, objects):
    a_task(db)
    _attempt(db, "att_1")
    objects.put(live_key("agent_stdout"), "#swarm-tail offset=0 size=4\nabc\n")

    out = stream_of(get(client, "?stream=agent_stdout").json(), "agent_stdout")
    assert out["tail_window"] == {"object_offset": 0, "stream_size": 4, "published_at": None}
    assert out["content"] == "abc\n"


def test_an_object_with_no_time_has_no_age_rather_than_age_zero(client, db, objects):
    a_task(db, state="SUCCEEDED")
    _attempt(db, "att_1", completed=True)
    objects.put(final_key("stdout"), "x\n")  # the in-memory store records no time

    out = stream_of(get(client).json(), "stdout")
    assert out["object_updated_at"] is None
    assert out["age_seconds"] is None


# --------------------------------------------------------------------------
# Attempts made before #184, and runners with no agent CLI
# --------------------------------------------------------------------------

def _legacy_summary(attempt="att_1", *, stdout='{"type":"result","result":"hi"}\n'):
    key = f"{base(attempt=attempt)}/artifacts/claude-code.stdout.log"
    return {
        "artifacts": [
            {"name": "claude-code.stdout.log", "bytes": len(stdout), "uri": f"gs://{BUCKET}/{key}"},
        ],
        "artifact_bytes": len(stdout),
        "logs": {"stderr": f"gs://{BUCKET}/{final_key('stderr', attempt=attempt)}"},
    }, key, stdout


def test_a_pre_change_attempt_serves_the_agents_stdout_from_its_artifact(client, db, objects):
    """task_73b5f4d9ca3641fbb914: no `logs/agent_stdout.log`, no live tail --
    the agent's stdout is `artifacts/claude-code.stdout.log`."""
    summary, key, stdout = _legacy_summary()
    a_task(db, state="SUCCEEDED", summary=summary)
    _attempt(db, "att_1", completed=True)
    objects.put(key, stdout)

    out = stream_of(get(client, "?stream=agent_stdout").json(), "agent_stdout")
    assert (out["status"], out["source"]) == ("ok", "artifact")
    assert out["content"] == stdout
    assert out["key"] == key


def test_the_artifact_fallback_is_only_for_the_attempt_the_manifest_describes(
    client, db, objects
):
    """The manifest is the FINAL attempt's. An earlier attempt must not be shown
    the final attempt's output as its own."""
    summary, key, stdout = _legacy_summary(attempt="att_2")
    a_task(db, state="SUCCEEDED", summary=summary)
    _attempt(db, "att_1", minutes_ago=30, completed=True)
    _attempt(db, "att_2", minutes_ago=10, completed=True)
    objects.put(key, stdout)

    earlier = stream_of(
        get(client, "?stream=agent_stdout&attempt_id=att_1").json(), "agent_stdout"
    )
    assert earlier["status"] == "absent"
    assert earlier["content"] is None
    latest = stream_of(get(client, "?stream=agent_stdout").json(), "agent_stdout")
    assert latest["source"] == "artifact"


def test_the_final_copy_wins_over_the_artifact(client, db, objects):
    summary, key, _stdout = _legacy_summary()
    a_task(db, state="SUCCEEDED", summary=summary)
    _attempt(db, "att_1", completed=True)
    objects.put(key, "the artifact\n")
    objects.put(final_key("agent_stdout"), "the final copy\n")

    out = stream_of(get(client, "?stream=agent_stdout").json(), "agent_stdout")
    assert (out["source"], out["content"]) == ("final", "the final copy\n")


def test_a_runner_that_declared_no_agent_cli_is_not_applicable(client, db, objects):
    a_task(db, state="SUCCEEDED", runner_profile="mock",
           summary={"artifacts": [], "logs": {}, "agent_streams": None})
    _attempt(db, "att_1", completed=True)

    body = get(client, "?stream=agent_stdout&stream=stdout").json()
    out = stream_of(body, "agent_stdout")
    assert out["status"] == "not_applicable"
    assert out["content"] is None
    assert out["detail"]
    assert stream_of(body, "stdout")["status"] == "absent"


def test_a_running_mock_task_is_not_applicable_before_it_has_declared_anything(
    client, db, objects
):
    """No summary yet, and the profile is one known to start no agent CLI."""
    a_task(db, runner_profile="mock")
    _attempt(db, "att_1")

    out = stream_of(get(client, "?stream=agent_stderr").json(), "agent_stderr")
    assert out["status"] == "not_applicable"


def test_a_running_claude_task_with_nothing_yet_is_absent_not_not_applicable(
    client, db, objects
):
    a_task(db)
    _attempt(db, "att_1")

    out = stream_of(get(client, "?stream=agent_stdout").json(), "agent_stdout")
    assert out["status"] == "absent"
    assert "neither the completed log nor a live tail" in out["detail"]


def test_another_tenants_agent_stream_is_a_404(client, db, objects):
    a_task(db)
    _attempt(db, "att_1")
    objects.put(live_key("agent_stdout"), "#swarm-tail offset=0 size=6\nsecret\n")

    response = get(client, "?stream=agent_stdout", user="bob")
    assert response.status_code == 404, response.text
    assert "secret" not in response.text


def test_an_agent_stream_is_redacted_at_read_time(client, db, objects):
    token = "ghp_" + "Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9Jj0KkLlMm"
    a_task(db, state="SUCCEEDED")
    _attempt(db, "att_1", completed=True)
    objects.put(final_key("agent_stdout"), f'{{"type":"user","content":"{token}"}}\n')

    out = stream_of(get(client, "?stream=agent_stdout").json(), "agent_stdout")
    assert token not in out["content"]
    assert out["redacted"] is True


# --------------------------------------------------------------------------
# A republished tail is never spliced
# --------------------------------------------------------------------------

class ReplacingReader(InMemoryObjectReader):
    """Reports the live tail replaced mid-read `times` times, then reads it."""

    def __init__(self, *args, times: int, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.times = times
        self.reads = 0

    def read_range(self, key, *, offset, length):
        from swarm_api.objects import ObjectReplaced

        if "/logs/live/" in key and self.times > 0:
            self.times -= 1
            self.reads += 1
            raise ObjectReplaced(key)
        return super().read_range(key, offset=offset, length=length)


def _client_over(reader, db, tokens, group_map):
    from fastapi.testclient import TestClient

    from swarm_api.auth import StaticTokenVerifier
    from swarm_api.credentials import InMemoryCredentials
    from swarm_api.deps import build_context
    from swarm_api.groups import StaticGroups
    from swarm_api.main import create_app
    from swarm_api.metrics import ApiMetrics
    from swarm_api.waker import NullWaker

    from .conftest import api_settings

    ctx = build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=reader,
    )
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def test_a_tail_replaced_once_mid_read_is_read_again(db, tokens, group_map):
    reader = ReplacingReader(bucket=BUCKET, times=1)
    client = _client_over(reader, db, tokens, group_map)
    a_task(db)
    _attempt(db, "att_1")
    reader.put(live_key("agent_stdout"), "#swarm-tail offset=0 size=6\nfresh\n")

    out = stream_of(get(client, "?stream=agent_stdout").json(), "agent_stdout")
    assert (out["status"], out["content"]) == ("ok", "fresh\n")
    assert reader.reads == 1


def test_a_tail_replaced_twice_is_unreadable_never_spliced(db, tokens, group_map):
    reader = ReplacingReader(bucket=BUCKET, times=2)
    client = _client_over(reader, db, tokens, group_map)
    a_task(db)
    _attempt(db, "att_1")
    reader.put(live_key("agent_stdout"), "#swarm-tail offset=0 size=6\nfresh\n")

    out = stream_of(get(client, "?stream=agent_stdout").json(), "agent_stdout")
    assert out["status"] == "unreadable"
    assert out["content"] is None
    assert "replaced while it was read" in out["detail"]
