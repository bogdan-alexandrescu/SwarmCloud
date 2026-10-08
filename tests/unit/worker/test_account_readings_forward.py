"""What a worker on a pool account tells the broker, and how often (S15).

  * THE FORWARDER THROTTLES: each window at most once per 60 s while the run
    goes on, every unsent window once at the end, and a window whose reset has
    passed never.
  * IT SURVIVES THE BROKER: a 5xx (or anything else) is logged and the
    attempt carries on; nothing raises out of it.
  * THE CLIENT'S BODIES carry the hold's proof and figures, nothing else -- a
    reading never carries a token.
  * THE RUNNER'S WATCHER finds the session id, the readings and the turn
    boundaries in the stream as it is written, and a resumed start masks the
    session id in the argv it logs.
"""

from __future__ import annotations

import json
import secrets as pysecrets
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_worker.accountlease import (
    AccountBroker,
    Assignment,
    BrokerUnavailable,
    NoAccount,
    ReadingForwarder,
)


class _Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, dict]] = []

    def warning(self, message, **fields):
        self.lines.append((message, fields))

    info = warning


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _soon(hours: float = 2) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _forwarder(sent, clock, *, fail=None):
    def send(windows):
        sent.append(dict(windows))
        if fail is not None:
            raise fail

    return ReadingForwarder(send, logger=_Log(), clock=clock)


# -- the throttle -----------------------------------------------------------


def test_each_window_is_forwarded_at_most_once_a_minute():
    sent, clock = [], _Clock()
    f = _forwarder(sent, clock)
    reset = _soon()

    assert f.offer({"five_hour": {"utilization": 0.1, "resets_at": reset}}) == ["five_hour"]
    for step in range(1, 60):
        clock.t += 1
        f.offer({"five_hour": {"utilization": 0.1 + step / 1000, "resets_at": reset}})
    assert len(sent) == 1, "59 changed readings inside a minute sent nothing more"

    clock.t += 1
    assert f.offer({"five_hour": {"utilization": 0.5, "resets_at": reset}}) == ["five_hour"]
    assert sent[-1]["five_hour"]["utilization"] == 0.5, "the LATEST reading, not the first"


def test_the_windows_are_throttled_separately_and_sent_together():
    sent, clock = [], _Clock()
    f = _forwarder(sent, clock)
    f.offer({"five_hour": {"utilization": 0.1, "resets_at": _soon()}})
    clock.t += 10
    assert f.offer({"seven_day": {"utilization": 0.3, "resets_at": _soon(90)}}) == ["seven_day"]
    assert sent[-1] == {"seven_day": {"utilization": 0.3, "resets_at": sent[-1]["seven_day"][
        "resets_at"]}}


def test_an_unchanged_reading_is_not_sent_again():
    sent, clock = [], _Clock()
    f = _forwarder(sent, clock)
    reading = {"five_hour": {"utilization": 0.1, "resets_at": _soon()}}
    f.offer(reading)
    clock.t += 600
    assert f.offer(reading) == []


def test_the_end_of_the_run_sends_what_the_throttle_held_back():
    sent, clock = [], _Clock()
    f = _forwarder(sent, clock)
    reset = _soon()
    f.offer({"five_hour": {"utilization": 0.1, "resets_at": reset}})
    clock.t += 5
    f.offer({"five_hour": {"utilization": 0.9, "resets_at": reset}})
    assert len(sent) == 1

    assert f.offer({}, final=True) == ["five_hour"]
    assert sent[-1]["five_hour"]["utilization"] == 0.9


def test_a_window_whose_reset_has_passed_is_never_sent():
    sent, clock = [], _Clock()
    f = _forwarder(sent, clock)
    assert f.offer({"five_hour": {"utilization": 0.9, "resets_at": _soon(-1)}}, final=True) == []
    assert sent == []


def test_a_broker_5xx_is_logged_and_never_raised():
    sent, clock = [], _Clock()
    f = _forwarder(sent, clock, fail=BrokerUnavailable("the quota broker answered 503"))
    reading = {"five_hour": {"utilization": 0.2, "resets_at": _soon()}}

    assert f.offer(reading) == []
    clock.t += 5
    f.offer(reading)
    assert len(sent) == 1, "a failed window waits out the interval before it is retried"
    clock.t += 60
    f.offer(reading)
    assert len(sent) == 2, "and is retried after it"
    assert f._log.lines and "could not forward" in f._log.lines[0][0]


def test_anything_but_a_reading_is_ignored():
    sent, clock = [], _Clock()
    f = _forwarder(sent, clock)
    f.offer({"five_hour": {"utilization": 7, "resets_at": _soon()},
             "seven_day": {"utilization": True, "resets_at": _soon()},
             "x": "nope"})
    f.offer("not a map")
    assert sent == []


# -- the client's bodies ------------------------------------------------------


class _Response:
    def __init__(self, body: str) -> None:
        self._body = body.encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture(monkeypatch, body: str) -> list:
    seen: list = []

    def fake_urlopen(req, timeout=None):
        seen.append((req.full_url, json.loads(req.data.decode())))
        return _Response(body)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return seen


def _broker() -> AccountBroker:
    return AccountBroker("https://broker.example", logger=_Log(),
                         token_fetcher=lambda aud: "id-token", task_id="task_1",
                         attempt_id="att_1")


HELD = Assignment(account_id="eng:first", secret="swarm-account-eng--first",
                  assignment_id="asg-1", account={})


def test_a_reading_carries_the_holds_proof_and_two_fields_per_window(monkeypatch):
    seen = _capture(monkeypatch, '{"recorded": true}')
    reset = _soon()

    assert _broker().report_reading(
        HELD, {"five_hour": {"utilization": 0.4, "resets_at": reset, "extra": "dropped"}}
    ) is True

    url, body = seen[0]
    assert url == "https://broker.example/v1/accounts/eng%3Afirst/readings"
    assert body == {
        "assignment_id": "asg-1", "task_id": "task_1", "attempt_id": "att_1",
        "windows": {"five_hour": {"utilization": 0.4, "resets_at": reset}},
    }


def test_a_swap_names_the_hold_it_leaves_and_the_reason(monkeypatch):
    seen = _capture(monkeypatch, json.dumps({
        "swapped": True, "account_id": "eng:second", "assignment_id": "asg-2",
        "secret": "swarm-account-eng--second", "account": {"label": "second"},
    }))

    moved = _broker().swap(HELD, provider="anthropic", reason="drain", exclude=["eng:x"])

    url, body = seen[0]
    assert url == "https://broker.example/v1/accounts/swap"
    assert body == {
        "assignment_id": "asg-1", "task_id": "task_1", "attempt_id": "att_1",
        "account_id": "eng:first", "reason": "drain", "provider": "anthropic",
        "exclude": ["eng:x"],
    }
    assert (moved.account_id, moved.assignment_id, moved.secret) == (
        "eng:second", "asg-2", "swarm-account-eng--second",
    )


def test_no_other_account_is_a_result_not_an_exception(monkeypatch):
    _capture(monkeypatch, json.dumps({
        "swapped": False, "reason": "no_account_available",
        "next_reset_at": "2026-10-02T18:00:00+00:00",
    }))

    out = _broker().swap(HELD, provider="anthropic", reason="exhausted")

    assert out == NoAccount(reason="no_account_available",
                            next_reset_at="2026-10-02T18:00:00+00:00")


def test_a_swap_answer_that_names_no_hold_is_an_outage(monkeypatch):
    _capture(monkeypatch, json.dumps({"swapped": True, "account_id": "eng:second"}))
    with pytest.raises(BrokerUnavailable):
        _broker().swap(HELD, provider="anthropic", reason="exhausted")


def test_the_hold_status_read_is_a_post_with_the_proof(monkeypatch):
    seen = _capture(monkeypatch, '{"held": true, "move": "drain"}')

    assert _broker().hold_status(HELD)["move"] == "drain"
    url, body = seen[0]
    assert url.endswith("/v1/accounts/eng%3Afirst/hold-status")
    assert body == {"assignment_id": "asg-1", "task_id": "task_1", "attempt_id": "att_1"}


# -- the runner's watcher -----------------------------------------------------


def _write(path: Path, events) -> None:
    with path.open("a") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def test_the_watcher_reads_the_stream_as_it_grows(tmp_path):
    from agent_worker.runners.cliagent import AccountStreamWatcher

    stdout, channel, move = tmp_path / "out", tmp_path / "chan.json", tmp_path / "move"
    stdout.write_text("")
    sid = str(uuid.uuid4())
    w = AccountStreamWatcher(stdout, channel, move)
    reset = int(datetime.now(timezone.utc).timestamp()) + 3600

    _write(stdout, [{"type": "system", "subtype": "init", "session_id": sid}])
    with stdout.open("a") as f:
        f.write('{"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", ')
    assert w.poll() is None
    assert json.loads(channel.read_text())["session_id"] == sid
    with stdout.open("a") as f:
        f.write('"rateLimitType": "five_hour", "resetsAt": %d, "utilization": 0.3}}\n' % reset)
    _write(stdout, [{"type": "user", "parent_tool_use_id": "sub-agent"}])
    assert w.poll() is None
    data = json.loads(channel.read_text())
    assert data["readings"]["five_hour"]["utilization"] == 0.3, "a line split across reads"
    assert data["turns"] == 0, "a subagent's user event is not the session's turn"

    move.write_text("{}")
    _write(stdout, [{"type": "user", "parent_tool_use_id": None}])
    assert w.poll() == "drain"
    assert json.loads(channel.read_text())["stopped_for"] == "drain"


def test_a_rejected_reading_stops_at_the_next_turn_boundary(tmp_path):
    from agent_worker.runners.cliagent import AccountStreamWatcher

    stdout, channel = tmp_path / "out", tmp_path / "chan.json"
    stdout.write_text("")
    w = AccountStreamWatcher(stdout, channel, None)
    _write(stdout, [{"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "rateLimitType": "five_hour",
        "resetsAt": int(datetime.now(timezone.utc).timestamp()) + 600}}])
    assert w.poll() is None, "not mid-turn"
    _write(stdout, [{"type": "user"}])
    assert w.poll() == "exhausted"
    assert json.loads(channel.read_text())["readings"]["five_hour"]["utilization"] == 1.0


def test_a_capture_tap_sees_every_byte_past_the_cap(tmp_path):
    import io

    from agent_worker.procman import StreamCapture

    seen: list[bytes] = []
    stream = b"".join(b"line %05d\n" % i for i in range(5000))
    capture = StreamCapture(tmp_path / "out", 4096, keep_tail=True, on_chunk=seen.append)
    capture.pump(io.BufferedReader(io.BytesIO(stream)))

    assert b"".join(seen) == stream, "the tap gets the stream, not the capped file"
    assert capture.truncated and (tmp_path / "out").stat().st_size < len(stream)


def test_a_capture_tap_that_raises_never_breaks_the_capture(tmp_path):
    import io

    from agent_worker.procman import StreamCapture

    def broken(_chunk: bytes) -> None:
        raise RuntimeError("tap failed")

    capture = StreamCapture(tmp_path / "out", 1 << 20, keep_tail=True, on_chunk=broken)
    capture.pump(io.BufferedReader(io.BytesIO(b"a\nb\n")))
    assert (tmp_path / "out").read_bytes() == b"a\nb\n"


def test_a_tapped_watcher_reads_the_pipe_and_not_the_file(tmp_path):
    from agent_worker.runners.cliagent import AccountStreamWatcher

    stdout, channel = tmp_path / "out", tmp_path / "chan.json"
    stdout.write_text("")
    w = AccountStreamWatcher(stdout, channel, None)
    feed = w.tap()
    reset = int(datetime.now(timezone.utc).timestamp()) + 600
    rejected = json.dumps({"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "rateLimitType": "five_hour", "resetsAt": reset}})
    # Split across two chunks, and never written to the (capped) file.
    feed((rejected[:20]).encode())
    assert w.poll() is None
    feed((rejected[20:] + "\n" + json.dumps({"type": "user"}) + "\n").encode())
    assert w.poll() == "exhausted"
    assert stdout.read_text() == ""


def _fake_cli(tmp_path: Path) -> Path:
    script = tmp_path / "fake-cli"
    script.write_text(
        "#!" + sys.executable + "\n"
        "import json, sys\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,"
        " 'result': 'argv=' + json.dumps([*sys.argv[1:], sys.stdin.read()])}))\n"
    )
    script.chmod(0o755)
    return script


def test_a_resumed_start_passes_resume_and_logs_the_session_masked(
    tmp_path, monkeypatch, capsys
):
    from agent_worker.runners.base import RunnerContext
    from agent_worker.runners.cliagent import RESUME_PROMPT, CliAgentSpec, run_cli_agent

    sid = str(uuid.uuid4())
    monkeypatch.setenv("FAKE_BIN", str(_fake_cli(tmp_path)))
    monkeypatch.setenv("FAKE_KEY", "k-" + pysecrets.token_hex(8))
    monkeypatch.setenv("SWARM_RESUME_SESSION", sid)
    work, artifacts = tmp_path / "work", tmp_path / "artifacts"
    work.mkdir()
    artifacts.mkdir()
    ctx = RunnerContext(work_dir=work, artifacts_dir=artifacts, input_path=work / "input.json",
                        result_path=work / "result.json", quota_path=work / "quota.json",
                        payload={"prompt": "the original prompt"})
    spec = CliAgentSpec(name="fake", provider="anthropic", binary_env="FAKE_BIN",
                        binary_default="fake", args_env="FAKE_ARGS", args_default=(),
                        key_env="FAKE_KEY", model_flag=None, resume_flag="--resume")

    out = run_cli_agent(ctx, spec)

    # The fake appends what it read on stdin after its argv: the message is
    # the resume prompt, delivered on stdin after `--resume <id>`.
    received = json.loads(out["summary"].split("argv=", 1)[1])
    assert received[-3:] == ["--resume", sid, RESUME_PROMPT]
    assert "the original prompt" not in json.dumps(received), "the session already holds it"
    err = capsys.readouterr().err
    assert sid not in err and "<session>" in err


def test_a_spec_that_cannot_resume_refuses_a_resume(tmp_path, monkeypatch):
    from agent_worker.runners.base import RunnerContext, RunnerFailure
    from agent_worker.runners.cliagent import CliAgentSpec, run_cli_agent

    monkeypatch.setenv("FAKE_BIN", str(_fake_cli(tmp_path)))
    monkeypatch.setenv("FAKE_KEY", "k-" + pysecrets.token_hex(8))
    monkeypatch.setenv("SWARM_RESUME_SESSION", str(uuid.uuid4()))
    work = tmp_path / "work"
    work.mkdir()
    ctx = RunnerContext(work_dir=work, artifacts_dir=tmp_path / "a", input_path=work / "i",
                        result_path=work / "r", quota_path=work / "q",
                        payload={"prompt": "p"})
    (tmp_path / "a").mkdir()
    spec = CliAgentSpec(name="fake", provider="anthropic", binary_env="FAKE_BIN",
                        binary_default="fake", args_env="FAKE_ARGS", args_default=(),
                        key_env="FAKE_KEY", model_flag=None)

    with pytest.raises(RunnerFailure):
        run_cli_agent(ctx, spec)


def test_both_cli_runners_can_resume():
    from agent_worker.runners.claude_code import SPEC
    from agent_worker.runners.codex import SPEC as CODEX

    assert SPEC.resume_flag == "--resume"
    # codex resumes too since #626 (`codex exec resume <id>`), for a
    # credential reload; it still never holds a pool account, so it never
    # gets the channel and never moves (`accountlease.ACCOUNT_TOKEN_ENV`).
    assert CODEX.resume_flag == "resume"
    assert CODEX.session_locator is not None, "codex's stdout names no session"


def test_a_session_id_containing_429_does_not_make_a_failure_a_rate_limit(tmp_path, monkeypatch):
    """`429` is matched anywhere, and about one uuid in 115 contains it. A
    failed streamed run whose events carried such an id was parked as
    rate-limited -- and on a pool account, moved as exhausted."""
    from agent_worker.runners.base import QuotaExhaustedSignal, RunnerFailure
    from agent_worker.runners.base import RunnerContext
    from agent_worker.runners.cliagent import CliAgentSpec, run_cli_agent

    sid = "a4290000-0000-4000-8000-" + "0" * 12
    script = tmp_path / "fake-cli"
    script.write_text(
        "#!" + sys.executable + "\n"
        "import json, sys\n"
        f"print(json.dumps({{'type': 'system', 'subtype': 'init', 'session_id': {sid!r}}}))\n"
        f"print(json.dumps({{'type': 'result', 'subtype': 'error_during_execution', "
        f"'is_error': True, 'result': 'the build failed', 'session_id': {sid!r}, "
        f"'uuid': {sid!r}}}))\n"
        "sys.exit(1)\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("FAKE_BIN", str(script))
    monkeypatch.setenv("FAKE_KEY", "k-" + pysecrets.token_hex(8))
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / "a").mkdir()
    ctx = RunnerContext(work_dir=work, artifacts_dir=tmp_path / "a", input_path=work / "i",
                        result_path=work / "r", quota_path=work / "q",
                        payload={"prompt": "p"})
    spec = CliAgentSpec(name="fake", provider="anthropic", binary_env="FAKE_BIN",
                        binary_default="fake", args_env="FAKE_ARGS", args_default=(),
                        key_env="FAKE_KEY", model_flag=None)

    with pytest.raises(RunnerFailure) as caught:
        run_cli_agent(ctx, spec)
    assert not isinstance(caught.value, QuotaExhaustedSignal)
