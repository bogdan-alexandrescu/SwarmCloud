"""A codex step leases a ChatGPT plan from the "openai" pool (part of #2688).

BGD, 2026-11-01: codex runs on a shared pool of ChatGPT-plan accounts the way
claude-code runs on the pool of Claude subscriptions. The worker's half:

  * a codex step whose profile declares CODEX_PLAN_ACCESS_TOKEN and
    CODEX_PLAN_ACCOUNT_ID leases a provider "openai" account through the same
    `assign` claude-code uses, when the tenant supplies no OPENAI_API_KEY, and
    parks when the pool answers "no account" exactly as claude-code does;
  * the account's secret (`{base}`, written by the broker alone) holds the
    plan's current access token and account id, never the refresh token, and
    they reach the runner as those two variables and nothing else;
  * the runner's own stop before the token's hour is a scheduled reload: the
    account is re-read, the session resumed with `exec resume`, and no auth
    error lands on the account's health record.

The profile is the frozen catalogue's codex with the two plan names added, as
the contract lane's request declares them (`secrets_any_of=True`, exactly as
claude-code declares its two). Every token is built at runtime.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import secrets as pysecrets
import time
import uuid
from pathlib import Path

import pytest

from agent_worker import accountlease
from agent_worker.accountlease import (
    NO_ACCOUNT_AVAILABLE,
    NO_ACCOUNTS_REGISTERED,
    PLAN_ACCOUNT_ENV,
    PLAN_TOKEN_ENV,
    Assignment,
    NoAccount,
    NoAccountAvailable,
    credential_env_from_account,
)
from agent_worker.errors import ExitCode
from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.states import ParkReason, TaskState

from worker_seeds import TENANT, seed_attempt, seed_tenant
from fakes import FakeSecretClient

ACCOUNT_ID = f"{TENANT}:plan"
ACCOUNT_SECRET = f"swarm-account-{TENANT}--plan"
TENANT_SECRET = f"swarm-tenant-{TENANT}-openai"


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _access_token(expires_in: float = 3600.0) -> str:
    header = _b64({"alg": "RS256", "typ": "JWT"})
    claims = _b64({"exp": int(time.time() + expires_in), "jti": pysecrets.token_hex(8)})
    return f"{header}.{claims}.{pysecrets.token_hex(24)}"


def _plan_payload(token: str | None = None, account: str | None = None) -> str:
    return json.dumps({"access_token": token or _access_token(),
                       "account_id": account or str(uuid.uuid4())})


@pytest.fixture()
def plan_profile(monkeypatch):
    """codex as the contract request declares it: the plan's two names beside
    OPENAI_API_KEY, any one of them enough."""
    profile = dataclasses.replace(
        RUNNER_PROFILES["codex"],
        secrets=("OPENAI_API_KEY", PLAN_TOKEN_ENV, PLAN_ACCOUNT_ENV),
        secrets_any_of=True,
    )
    monkeypatch.setitem(RUNNER_PROFILES, "codex", profile)
    return profile


class FakeBroker:
    def __init__(self, outcome=None) -> None:
        self.outcome = outcome if outcome is not None else Assignment(
            account_id=ACCOUNT_ID,
            secret=ACCOUNT_SECRET,
            assignment_id="asg-1",
            account={"account_id": ACCOUNT_ID, "owner_tenant": TENANT, "label": "plan"},
        )
        self.assigns: list[str] = []
        self.releases: list[tuple[str, str]] = []
        self.swaps: list[dict] = []
        self.readings: list[tuple[str, dict]] = []

    def assign(self, provider, *, exclude=()):
        self.assigns.append(provider)
        return self.outcome

    def release(self, account_id, assignment_id, *, unusable=""):
        self.releases.append((account_id, unusable))
        return 0

    def swap(self, assignment, *, provider, reason, exclude=()):
        self.swaps.append({"from": assignment.account_id, "reason": reason})
        return NoAccount(reason=NO_ACCOUNT_AVAILABLE)

    def hold_status(self, assignment):
        return {"held": True, "move": None}

    def report_reading(self, assignment, windows):
        self.readings.append((assignment.account_id, dict(windows)))
        return True


def _worker(worker_factory, secrets, broker, tmp_path, **kwargs):
    from agent_worker import workspace as workspace_mod

    worker, config, exporter = worker_factory(
        runner_profile="codex", secret_client=secrets, **kwargs
    )
    worker._account_broker = broker
    worker.ws = workspace_mod.create(tmp_path / "ws", "att_1")
    return worker


# -- the provider choice ---------------------------------------------------------


def test_a_codex_step_leases_an_openai_account(db, worker_factory, tmp_path, plan_profile):
    token, account = _access_token(), str(uuid.uuid4())
    secrets = FakeSecretClient({ACCOUNT_SECRET: _plan_payload(token, account)})
    seed_attempt(db, runner_profile="codex")
    seed_tenant(db, credentials=[])
    broker = FakeBroker()
    worker = _worker(worker_factory, secrets, broker, tmp_path)

    env = worker._build_child_env()

    assert broker.assigns == ["openai"]
    assert env[PLAN_TOKEN_ENV] == token
    assert env[PLAN_ACCOUNT_ENV] == account
    assert "OPENAI_API_KEY" not in env
    assert secrets.accessed == [ACCOUNT_SECRET]
    # The channel, as for a claude-code attempt on an account.
    assert "SWARM_ACCOUNT_STREAM" in env


def test_a_tenant_that_supplies_an_api_key_runs_on_it(db, worker_factory, tmp_path, plan_profile):
    """The brief's condition: the pool serves codex when the tenant supplies
    no OPENAI_API_KEY. A tenant paying for metered API access keeps it."""
    key = "sk-" + "proj-" + pysecrets.token_hex(16)
    secrets = FakeSecretClient({TENANT_SECRET: key})
    seed_attempt(db, runner_profile="codex")
    seed_tenant(db, credentials=["openai"])
    broker = FakeBroker()
    worker = _worker(worker_factory, secrets, broker, tmp_path)

    env = worker._build_child_env()

    assert broker.assigns == []
    assert env["OPENAI_API_KEY"] == key
    assert secrets.accessed == [TENANT_SECRET]


def test_an_openai_pool_with_no_account_parks_as_claude_codes_does(
    db, worker_factory, tmp_path, plan_profile
):
    seed_attempt(db, runner_profile="codex")
    seed_tenant(db, credentials=[])
    broker = FakeBroker(NoAccount(reason=NO_ACCOUNT_AVAILABLE))
    worker = _worker(worker_factory, FakeSecretClient(), broker, tmp_path)

    with pytest.raises(NoAccountAvailable) as caught:
        worker._build_child_env()

    assert caught.value.provider == "openai"
    assert broker.assigns == ["openai"]


def test_no_openai_account_registered_falls_back_to_the_tenant(
    db, worker_factory, tmp_path, plan_profile
):
    """No pool for this provider: the tenant's credential, missing here, so
    the attempt parks as CREDENTIAL_MISSING exactly as it did before."""
    seed_attempt(db, runner_profile="codex", task_input={"prompt": "needs a plan"})
    seed_tenant(db, credentials=[])
    worker, _, _ = worker_factory(runner_profile="codex", secret_client=FakeSecretClient())
    worker._account_broker = FakeBroker(NoAccount(reason=NO_ACCOUNTS_REGISTERED))

    assert worker.run() == ExitCode.PARKED
    assert db.doc("tasks/task_1")["park_reason"] == ParkReason.CREDENTIAL_MISSING.value


def test_the_catalogue_codex_without_the_plan_names_never_asks_the_pool(
    db, worker_factory, tmp_path
):
    """Until the contract request lands, codex declares OPENAI_API_KEY alone:
    an account has no variable to put a plan in, and nothing changes."""
    assert PLAN_TOKEN_ENV not in RUNNER_PROFILES["codex"].secrets
    secrets = FakeSecretClient({TENANT_SECRET: "sk-" + "proj-" + pysecrets.token_hex(16)})
    seed_attempt(db, runner_profile="codex")
    seed_tenant(db, credentials=["openai"])
    broker = FakeBroker()
    worker = _worker(worker_factory, secrets, broker, tmp_path)

    worker._build_child_env()

    assert broker.assigns == []


def test_the_plan_token_never_reaches_the_logs(
    db, worker_factory, tmp_path, plan_profile, log_stream
):
    token = _access_token()
    secrets = FakeSecretClient({ACCOUNT_SECRET: _plan_payload(token)})
    seed_attempt(db, runner_profile="codex")
    seed_tenant(db, credentials=[])
    worker = _worker(worker_factory, secrets, FakeBroker(), tmp_path)

    worker._build_child_env()
    worker.log.info(f"about to run with {token}")

    assert token not in log_stream.getvalue()


# -- the account secret's shape ------------------------------------------------


def test_an_openai_account_supplies_the_plans_two_names_only():
    token, account = _access_token(), str(uuid.uuid4())
    env = credential_env_from_account(
        _plan_payload(token, account),
        secret_env_names=("OPENAI_API_KEY", PLAN_TOKEN_ENV, PLAN_ACCOUNT_ENV),
        secret_name=ACCOUNT_SECRET,
        provider="openai",
    )
    assert env == {PLAN_TOKEN_ENV: token, PLAN_ACCOUNT_ENV: account}


@pytest.mark.parametrize("payload", [
    "",
    "a-bare-token-with-no-account",
    json.dumps({"access_token": "x" * 40}),
    json.dumps({"account_id": str(uuid.uuid4())}),
    json.dumps({"access_token": "", "account_id": str(uuid.uuid4())}),
    json.dumps(["not", "an", "object"]),
])
def test_an_openai_account_secret_of_another_shape_is_refused(payload):
    with pytest.raises(ValueError):
        credential_env_from_account(
            payload,
            secret_env_names=("OPENAI_API_KEY", PLAN_TOKEN_ENV, PLAN_ACCOUNT_ENV),
            secret_name=ACCOUNT_SECRET,
            provider="openai",
        )


def test_an_openai_account_secret_never_carries_a_refresh_token():
    """The broker is the only writer and holder of the refresh token; a
    `{base}` that carried one would hand a pod the power to sign every other
    holder out. Refused, not silently dropped."""
    payload = json.dumps({"access_token": _access_token(), "account_id": str(uuid.uuid4()),
                          "refresh_token": pysecrets.token_hex(20)})
    with pytest.raises(ValueError, match="refresh"):
        credential_env_from_account(
            payload,
            secret_env_names=("OPENAI_API_KEY", PLAN_TOKEN_ENV, PLAN_ACCOUNT_ENV),
            secret_name=ACCOUNT_SECRET,
            provider="openai",
        )


def test_a_profile_without_the_plan_names_cannot_take_an_openai_account():
    with pytest.raises(ValueError):
        credential_env_from_account(
            _plan_payload(),
            secret_env_names=("OPENAI_API_KEY",),
            secret_name=ACCOUNT_SECRET,
            provider="openai",
        )


def test_the_anthropic_account_is_unchanged():
    token = "sk-ant-" + "oat01-" + pysecrets.token_hex(16)
    env = credential_env_from_account(
        token,
        secret_env_names=("ANTHROPIC_API_KEY", accountlease.ACCOUNT_TOKEN_ENV),
        secret_name=ACCOUNT_SECRET,
    )
    assert env == {accountlease.ACCOUNT_TOKEN_ENV: token}


def test_which_profiles_an_account_can_serve():
    assert accountlease.takes_account("anthropic", ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"))
    assert accountlease.takes_account("openai", ("OPENAI_API_KEY", PLAN_TOKEN_ENV, PLAN_ACCOUNT_ENV))
    assert not accountlease.takes_account("openai", ("OPENAI_API_KEY",))
    assert not accountlease.takes_account("openai", ("OPENAI_API_KEY", PLAN_TOKEN_ENV))
    assert not accountlease.takes_account("anthropic", ("ANTHROPIC_API_KEY",))
    assert not accountlease.takes_account(None, (PLAN_TOKEN_ENV, PLAN_ACCOUNT_ENV))


# -- the hour: a scheduled reload, end to end ---------------------------------------


#: A codex stand-in for the worker's real runner. `plan.json` lists one mode
#: per start: "expire" outlives the runner's stop before the token's hour,
#: "finish" answers. Each start records its argv's resume id and the token in
#: auth.json.
FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, pathlib, sys, time
plan_dir = pathlib.Path(PLAN_DIR)
plan = json.loads((plan_dir / "plan.json").read_text())
argv = sys.argv[1:]
resume = argv[argv.index("resume") + 1] if "resume" in argv else None
codex_home = pathlib.Path(os.environ["CODEX_HOME"])
auth = json.loads((codex_home / "auth.json").read_text())
with (plan_dir / "runs.jsonl").open("a") as f:
    f.write(json.dumps({"resume": resume, "token": auth["tokens"]["access_token"]}) + "\n")
runs = sum(1 for _ in (plan_dir / "runs.jsonl").open())
mode = plan["modes"][runs - 1]
session = resume or plan["session"]
day = codex_home / "sessions" / "2026" / "11" / "01"
day.mkdir(parents=True, exist_ok=True)
with (day / ("rollout-2026-11-01T20-00-00-%s.jsonl" % session)).open("a") as f:
    f.write(json.dumps({"type": "session_meta", "payload": {"id": session}}) + "\n")
sys.stdin.read()
print(json.dumps({"type": "thread.started", "thread_id": session}), flush=True)
if mode == "expire":
    time.sleep(45)
print(json.dumps({"type": "item.completed", "item": {"id": "i", "type": "agent_message",
                  "text": "done"}}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
'''


class _HourSecrets(FakeSecretClient):
    """The account's `{base}`: the first read holds a token inside the
    runner's margin of its hour (three seconds before the stop), every later
    read the successor the broker wrote."""

    def __init__(self, margin: float) -> None:
        super().__init__({})
        self.margin = margin
        self.account = str(uuid.uuid4())
        self.tokens: list[str] = []

    def access(self, secret_name, version="latest"):
        self.accessed.append(secret_name)
        expires_in = self.margin + 3 if not self.tokens else 3600.0
        self.tokens.append(_access_token(expires_in))
        return _plan_payload(self.tokens[-1], self.account)


def test_a_token_near_its_hour_is_reloaded_and_the_session_resumed(
    db, worker_factory, tmp_path, monkeypatch, plan_profile, log_stream
):
    from agent_worker.runners import codex

    plan = tmp_path / "plan"
    plan.mkdir()
    binary = tmp_path / "fake-codex"
    binary.write_text(FAKE_CODEX.replace("PLAN_DIR", repr(str(plan)), 1))
    binary.chmod(0o755)
    monkeypatch.setenv("CODEX_BIN", str(binary))
    monkeypatch.delenv("CODEX_ARGS", raising=False)
    session = str(uuid.uuid4())
    (plan / "plan.json").write_text(json.dumps({"modes": ["expire", "finish"],
                                                "session": session}))
    seed_attempt(db, runner_profile="codex", task_input={"prompt": "do the work"})
    seed_tenant(db, credentials=[])
    secrets = _HourSecrets(codex.EXPIRY_MARGIN_SECONDS)
    broker = FakeBroker()
    worker, _, _ = worker_factory(runner_profile="codex", secret_client=secrets,
                                  timeout_seconds=60)
    worker._account_broker = broker
    auth_errors: list[dict] = []
    monkeypatch.setattr(worker.control, "report_account_error",
                        lambda **kw: auth_errors.append(kw))

    code = worker.run()

    assert code == ExitCode.OK, log_stream.getvalue()[-3000:]
    runs = [json.loads(line) for line in (plan / "runs.jsonl").read_text().splitlines()]
    assert [r["resume"] for r in runs] == [None, session]
    assert [r["token"] for r in runs] == secrets.tokens[:2]
    assert broker.assigns == ["openai"], "re-read, never re-assigned"
    assert broker.swaps == [], "a scheduled reload does not move the attempt"
    assert auth_errors == [], "the hour is not an auth error on the account"
    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value
    for token in secrets.tokens:
        assert token not in log_stream.getvalue()
