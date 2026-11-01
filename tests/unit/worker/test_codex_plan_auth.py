"""codex on a ChatGPT plan from the shared pool (part of #2688, BGD 2026-11-01).

The worker hands a plan's ACCESS token and account id to the codex runner as
CODEX_PLAN_ACCESS_TOKEN and CODEX_PLAN_ACCOUNT_ID. The runner writes them as a
private CODEX_HOME/auth.json in the shape codex 0.154.0 reads -- measured in
agent-runtime-base: `codex login status` answers "Logged in using ChatGPT" for
it -- with NO refresh token, points codex's refresh at a port nothing listens
on, so the broker stays the only writer of the plan's sign-in, and removes the
file when the run ends.

These run the real runner against a fake codex that records what it saw.
Every token is built at runtime: no credential-shaped literal in this file.

MUTATIONS: write the refresh token into auth.json (the no-refresh-token test
fails); drop the chmod (the mode test fails); drop the cleanup (the removed
test fails); drop the refusal readers in `codex.body` (each refusal test
fails); drop the expiry timer (the expiry test runs to the CLI's end and
raises RunnerFailure instead of CredentialRevokedSignal).
"""

from __future__ import annotations

import base64
import json
import os
import secrets as pysecrets
import time
import uuid
from pathlib import Path

import pytest

from agent_worker.runners import cliagent, codex
from agent_worker.runners.base import (
    CredentialRevokedSignal,
    QuotaExhaustedSignal,
    RunnerContext,
    RunnerFailure,
)


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _access_token(expires_in: float = 3600.0) -> str:
    """A JWT-shaped access token with an `exp`, as the plan's are. Runtime-built."""
    header = _b64({"alg": "RS256", "typ": "JWT"})
    claims = _b64({"exp": int(time.time() + expires_in), "jti": pysecrets.token_hex(8)})
    return f"{header}.{claims}.{pysecrets.token_hex(24)}"


def _account_id() -> str:
    return str(uuid.uuid4())


def _ctx(tmp_path: Path) -> RunnerContext:
    work = tmp_path / "work"
    artifacts = tmp_path / "artifacts"
    work.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    return RunnerContext(
        work_dir=work,
        artifacts_dir=artifacts,
        input_path=work / "input.json",
        result_path=work / "result.json",
        quota_path=work / "quota.json",
        payload={"prompt": "do the work"},
    )


#: A codex stand-in. Records argv, stdin, CODEX_HOME, the refresh override,
#: the credential variables it was given and auth.json's text and mode in
#: `$HOME/runs.jsonl`; writes a rollout under CODEX_HOME/sessions naming
#: `$HOME/session`, with a token_count carrying rate limits; then behaves as
#: `$HOME/mode` says.
FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, pathlib, stat, sys, time
home = pathlib.Path(os.environ["HOME"])
codex_home = pathlib.Path(os.environ.get("CODEX_HOME") or (home / ".codex"))
auth = codex_home / "auth.json"
record = {
    "argv": sys.argv[1:],
    "stdin": sys.stdin.read(),
    "codex_home": os.environ.get("CODEX_HOME"),
    "refresh_url": os.environ.get("CODEX_REFRESH_TOKEN_URL_OVERRIDE"),
    "env": sorted(k for k in os.environ if k in (
        "OPENAI_API_KEY", "CODEX_PLAN_ACCESS_TOKEN", "CODEX_PLAN_ACCOUNT_ID")),
    "auth": auth.read_text() if auth.exists() else None,
    "auth_mode": stat.S_IMODE(auth.stat().st_mode) if auth.exists() else None,
}
with (home / "runs.jsonl").open("a") as f:
    f.write(json.dumps(record) + "\n")
mode = (home / "mode").read_text().strip() if (home / "mode").exists() else "ok"
session = (home / "session").read_text().strip()
day = codex_home / "sessions" / "2026" / "11" / "01"
day.mkdir(parents=True, exist_ok=True)
rollout = day / ("rollout-2026-11-01T20-00-00-%s.jsonl" % session)
reset = int(time.time()) + 7200
used = 100.0 if mode == "usage" else 42.0
with rollout.open("a") as f:
    f.write(json.dumps({"type": "session_meta", "payload": {"id": session}}) + "\n")
    f.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": None,
        "rate_limits": {"primary": {"used_percent": used, "window_minutes": 300,
                                    "resets_at": reset},
                        "secondary": {"used_percent": 7.5, "window_minutes": 10080,
                                      "resets_at": reset + 86400}}}}) + "\n")
print(json.dumps({"type": "thread.started", "thread_id": session}), flush=True)
if mode == "model":
    print(json.dumps({"type": "error", "message": "{\"detail\":\"The 'gpt-4o' model is not "
                      "supported when using Codex with a ChatGPT account.\"}"}), flush=True)
    sys.exit(1)
if mode == "usage":
    print(json.dumps({"type": "error", "message": "You've hit your usage limit. Upgrade to "
                      "Pro or try again later."}), flush=True)
    sys.exit(1)
if mode == "401":
    print("ERROR codex_api: unexpected status 401 Unauthorized: token expired", file=sys.stderr)
    sys.exit(1)
if mode == "403":
    print("ERROR codex_api: unexpected status 403 Forbidden: account deactivated", file=sys.stderr)
    sys.exit(1)
if mode == "sleep":
    time.sleep(30)
print(json.dumps({"type": "item.completed", "item": {"id": "item_1",
                  "type": "agent_message", "text": "done"}}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
'''


def _install(tmp_path: Path, monkeypatch, ctx: RunnerContext, mode: str = "ok") -> str:
    cli = tmp_path / "fake-codex"
    cli.write_text(FAKE_CODEX)
    cli.chmod(0o755)
    monkeypatch.setenv("CODEX_BIN", str(cli))
    session = str(uuid.uuid4())
    (ctx.work_dir / "session").write_text(session)
    (ctx.work_dir / "mode").write_text(mode)
    for name in (
        "CODEX_ARGS", "OPENAI_API_KEY", "MODEL", "SWARM_CHILDREN", "SWARM_GIT",
        "CODEX_HOME", "CODEX_REFRESH_TOKEN_URL_OVERRIDE",
        codex.PLAN_TOKEN_ENV, codex.PLAN_ACCOUNT_ENV,
        cliagent.ACCOUNT_STREAM_ENV, cliagent.ACCOUNT_MOVE_ENV,
        cliagent.RESUME_SESSION_ENV, cliagent.RESUME_REASON_ENV, cliagent.SESSION_FILE_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    return session


def _plan(monkeypatch, *, expires_in: float = 3600.0) -> tuple[str, str]:
    token, account = _access_token(expires_in), _account_id()
    monkeypatch.setenv(codex.PLAN_TOKEN_ENV, token)
    monkeypatch.setenv(codex.PLAN_ACCOUNT_ENV, account)
    return token, account


def _channel(tmp_path: Path, monkeypatch) -> Path:
    """The account channel the worker names for an attempt holding an account."""
    private = tmp_path / "private"
    private.mkdir(exist_ok=True)
    stream = private / "account-stream.json"
    monkeypatch.setenv(cliagent.ACCOUNT_STREAM_ENV, str(stream))
    monkeypatch.setenv(cliagent.ACCOUNT_MOVE_ENV, str(private / "account-move.json"))
    return stream


def _runs(ctx: RunnerContext) -> list[dict]:
    path = ctx.work_dir / "runs.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# -- auth.json ---------------------------------------------------------------


def test_the_plan_credential_is_written_as_the_auth_json_codex_reads(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _install(tmp_path, monkeypatch, ctx)
    token, account = _plan(monkeypatch)

    codex.body(ctx)

    (run,) = _runs(ctx)
    auth = json.loads(run["auth"])
    assert auth["auth_mode"] == "chatgpt"
    assert auth["OPENAI_API_KEY"] is None
    assert auth["tokens"]["access_token"] == token
    assert auth["tokens"]["account_id"] == account
    # codex 0.154.0 parses id_token as a JWT: three non-empty parts, the
    # plan's account in its auth claim. An empty signature part is refused
    # ("invalid ID token format"), measured.
    parts = auth["tokens"]["id_token"].split(".")
    assert len(parts) == 3 and all(parts)
    claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    assert claims["https://api.openai.com/auth"]["chatgpt_account_id"] == account
    # `last_refresh` now: codex does not refresh a token it believes fresh.
    assert auth["last_refresh"].endswith("Z")


def test_auth_json_holds_no_refresh_token(tmp_path, monkeypatch):
    """The broker is the ONLY writer of the plan's sign-in: each refresh
    replaces the refresh token, so a pod that refreshed would sign every
    other holder of the account out."""
    ctx = _ctx(tmp_path)
    _install(tmp_path, monkeypatch, ctx)
    _plan(monkeypatch)

    codex.body(ctx)

    (run,) = _runs(ctx)
    assert json.loads(run["auth"])["tokens"]["refresh_token"] == ""


def test_auth_json_is_private_to_the_attempt(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _install(tmp_path, monkeypatch, ctx)
    _channel(tmp_path, monkeypatch)
    _plan(monkeypatch)

    codex.body(ctx)

    (run,) = _runs(ctx)
    assert run["auth_mode"] == 0o600
    home = Path(run["codex_home"])
    # In the worker's private scratch beside the channel, never in `work/`,
    # which a checkpoint archives and the harvest reads.
    assert home.parent == tmp_path / "private"
    assert ctx.work_dir not in home.parents


def test_codex_cannot_refresh_the_token_itself(tmp_path, monkeypatch):
    """codex refreshes on a 401 (measured). Its refresh URL points at a
    loopback port nothing listens on, so the attempt fails there and the
    worker's reload asks the broker instead."""
    ctx = _ctx(tmp_path)
    _install(tmp_path, monkeypatch, ctx)
    _plan(monkeypatch)

    codex.body(ctx)

    (run,) = _runs(ctx)
    assert run["refresh_url"] == codex.REFUSED_REFRESH_URL
    assert run["refresh_url"].startswith("http://127.0.0.1:")


def test_auth_json_is_removed_when_the_run_ends(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _install(tmp_path, monkeypatch, ctx, mode="401")
    _channel(tmp_path, monkeypatch)
    _plan(monkeypatch)

    with pytest.raises(CredentialRevokedSignal):
        codex.body(ctx)

    (run,) = _runs(ctx)
    assert not Path(run["codex_home"]).exists()


def test_the_session_codex_writes_lands_where_the_resume_finds_it(tmp_path, monkeypatch):
    """CODEX_HOME moves, the rollouts do not: `sessions/` is the workspace's
    `.codex/sessions`, which the checkpoint keeps and `latest_session` reads."""
    ctx = _ctx(tmp_path)
    session = _install(tmp_path, monkeypatch, ctx)
    _plan(monkeypatch)

    codex.body(ctx)

    assert codex.latest_session(ctx.work_dir, 0) == session


def test_a_plan_run_hands_codex_no_api_key(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _install(tmp_path, monkeypatch, ctx)
    _plan(monkeypatch)

    codex.body(ctx)

    (run,) = _runs(ctx)
    assert "OPENAI_API_KEY" not in run["env"]
    assert "CODEX_PLAN_ACCOUNT_ID" not in run["env"]


def test_an_api_key_run_writes_no_auth_json(tmp_path, monkeypatch):
    """The tenant's own key is unchanged: no CODEX_HOME, no auth.json."""
    ctx = _ctx(tmp_path)
    _install(tmp_path, monkeypatch, ctx)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-" + "proj-" + pysecrets.token_hex(16))

    codex.body(ctx)

    (run,) = _runs(ctx)
    assert run["codex_home"] is None
    assert run["auth"] is None


def test_a_bare_tenant_secret_projected_into_every_name_stays_an_api_key(tmp_path, monkeypatch):
    """A tenant's bare secret fills every declared name with the same string;
    that is the tenant's key, never a plan sign-in."""
    ctx = _ctx(tmp_path)
    _install(tmp_path, monkeypatch, ctx)
    key = "sk-" + "proj-" + pysecrets.token_hex(16)
    for name in ("OPENAI_API_KEY", codex.PLAN_TOKEN_ENV, codex.PLAN_ACCOUNT_ENV):
        monkeypatch.setenv(name, key)

    codex.body(ctx)

    (run,) = _runs(ctx)
    assert run["auth"] is None
    assert run["env"] == ["OPENAI_API_KEY"]


# -- the plan's refusals -----------------------------------------------------


def test_a_model_the_plan_refuses_fails_with_the_plans_sentence(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _install(tmp_path, monkeypatch, ctx, mode="model")
    _plan(monkeypatch)

    with pytest.raises(RunnerFailure) as caught:
        codex.body(ctx)

    assert not isinstance(caught.value, (QuotaExhaustedSignal, CredentialRevokedSignal))
    assert str(caught.value) == (
        "The 'gpt-4o' model is not supported when using Codex with a ChatGPT account."
    )


def test_a_usage_limit_parks_or_swaps_with_a_reading_for_the_account(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    session = _install(tmp_path, monkeypatch, ctx, mode="usage")
    stream = _channel(tmp_path, monkeypatch)
    _plan(monkeypatch)

    with pytest.raises(QuotaExhaustedSignal) as caught:
        codex.body(ctx)

    assert caught.value.provider == "openai"
    channel = json.loads(stream.read_text())
    assert channel["readings"]["five_hour"]["utilization"] == 1.0
    assert channel["readings"]["seven_day"]["utilization"] == pytest.approx(0.075)
    # The session, so the worker's swap resumes it on another account.
    assert channel["session_id"] == session
    assert caught.value.reset_at == channel["readings"]["five_hour"]["resets_at"]


def test_a_401_is_a_refused_credential(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _install(tmp_path, monkeypatch, ctx, mode="401")
    _plan(monkeypatch)

    with pytest.raises(CredentialRevokedSignal) as caught:
        codex.body(ctx)
    assert caught.value.provider == "openai"


def test_a_403_is_a_refused_credential(tmp_path, monkeypatch):
    """The worker releases a refused account as unusable once a reload brings
    back the same token (`lifecycle._dead_credential`), so the broker marks
    it; a 403 must reach that path, not fail as an ordinary error."""
    ctx = _ctx(tmp_path)
    _install(tmp_path, monkeypatch, ctx, mode="403")
    _plan(monkeypatch)

    with pytest.raises(CredentialRevokedSignal) as caught:
        codex.body(ctx)
    assert caught.value.marker == codex.FORBIDDEN_MARKER


# -- the rate-limit report becomes a reading ------------------------------------


def test_codex_rate_limits_become_readings_on_the_channel(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    session = _install(tmp_path, monkeypatch, ctx)
    stream = _channel(tmp_path, monkeypatch)
    _plan(monkeypatch)

    codex.body(ctx)

    channel = json.loads(stream.read_text())
    assert channel["readings"]["five_hour"]["utilization"] == pytest.approx(0.42)
    assert channel["readings"]["seven_day"]["utilization"] == pytest.approx(0.075)
    assert channel["session_id"] == session


def test_a_rate_limit_window_of_another_length_is_named_by_its_minutes():
    reading = codex.readings_of({"primary": {"used_percent": 10, "window_minutes": 60,
                                             "resets_at": int(time.time()) + 60}})
    assert list(reading) == ["window_60m"]


def test_a_malformed_rate_limit_report_is_no_reading():
    assert codex.readings_of({"primary": {"used_percent": "lots", "window_minutes": 300,
                                          "resets_at": 1}}) == {}
    assert codex.readings_of({"primary": {"used_percent": 150, "window_minutes": 300,
                                          "resets_at": 1}}) == {}
    assert codex.readings_of(None) == {}


# -- the hour: reload and resume ----------------------------------------------


def test_a_token_near_its_hour_stops_codex_for_a_reload(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    session = _install(tmp_path, monkeypatch, ctx, mode="sleep")
    stream = _channel(tmp_path, monkeypatch)
    monkeypatch.setattr(codex, "EXPIRY_MARGIN_SECONDS", 60.0)
    _plan(monkeypatch, expires_in=62.0)

    started = time.monotonic()
    with pytest.raises(CredentialRevokedSignal) as caught:
        codex.body(ctx)

    assert time.monotonic() - started < 20, "stopped before the CLI's own end"
    assert caught.value.marker == codex.EXPIRY_MARKER
    assert ctx.stop_requested is False, "the runner's own stop is not a SIGTERM"
    # The session goes on the channel: the worker's reload resumes it.
    assert json.loads(stream.read_text())["session_id"] == session


def test_a_token_already_inside_the_margin_is_not_stopped_at_once(tmp_path, monkeypatch):
    """No loop: a token the broker has not replaced yet runs to its 401, which
    the ordinary refused-credential path answers."""
    ctx = _ctx(tmp_path)
    _install(tmp_path, monkeypatch, ctx)
    _channel(tmp_path, monkeypatch)
    _plan(monkeypatch, expires_in=30.0)

    codex.body(ctx)

    assert len(_runs(ctx)) == 1


def test_the_reloaded_run_resumes_with_the_new_token(tmp_path, monkeypatch):
    """The path PR 725 built: the worker re-reads the account, restarts the
    runner with SWARM_RESUME_SESSION, and codex continues with `exec resume`
    -- reading a fresh auth.json holding the broker's new token."""
    ctx = _ctx(tmp_path)
    session = _install(tmp_path, monkeypatch, ctx)
    _channel(tmp_path, monkeypatch)
    token, _ = _plan(monkeypatch)
    monkeypatch.setenv(cliagent.RESUME_SESSION_ENV, session)
    monkeypatch.setenv(cliagent.RESUME_REASON_ENV, cliagent.RESUME_RELOADED)

    codex.body(ctx)

    (run,) = _runs(ctx)
    assert run["argv"] == [*codex.SPEC.args_default, "resume", session, "-"]
    assert json.loads(run["auth"])["tokens"]["access_token"] == token


def test_the_token_never_reaches_the_runner_artifacts(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _install(tmp_path, monkeypatch, ctx, mode="401")
    _channel(tmp_path, monkeypatch)
    token, _ = _plan(monkeypatch)

    with pytest.raises(CredentialRevokedSignal):
        codex.body(ctx)

    for path in ctx.artifacts_dir.rglob("*"):
        if path.is_file():
            assert token not in path.read_text(errors="replace"), path.name
