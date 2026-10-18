"""The task profile's registered secrets reach Jev's sanitizer (lane JEV-SCRUB, part of #1336).

context-quality.md §5.4 step 4: a string holding one of the task's registered
secrets is dropped before anything is sent to Jev, an external service. The
names are `RunnerProfile.secrets`, the ones the lifecycle already resolves
into the runner's environment. They travel

    lifecycle._graph_context_env -> graphstage.prepare -> judge.attempt.setup
    -> register.prepare (config.json's `secret_env_names`)
    -> judge.attempt.load / JevServer.from_config (the values, from the env)

and the client drops every string holding one of those values. A value that
does not look like a credential is the case only this rule catches: rules 2
and 3 (the credential scan and the redaction rules) pass it, so each test
first asserts that they do.
"""

from __future__ import annotations

import dataclasses
import json
import secrets
from pathlib import Path
from typing import Any

from agent_worker import config as config_mod
from agent_worker import graphstage, lifecycle
from agent_worker.contextpack import rank
from agent_worker.contextpack.retrieve import Candidate
from agent_worker.errors import ExitCode
from agent_worker.judge import attempt, register, sanitize, settings
from swarm_common.profiles import RUNNER_PROFILES

from test_jev_wiring import INTERNAL_URL, URL, FakeJev, _Db
from worker_seeds import seed_attempt

SECRET_NAME = "WIDGET_STORE_PASSPHRASE"
OPTED_IN = {"code_intelligence": {"jev": True}}


def _passphrase() -> str:
    """Plain words and a number: nothing a credential scan would flag."""
    return f"orange harbor lantern {secrets.randbelow(900_000) + 100_000}"


def _rows(value: str) -> list[Candidate]:
    return [Candidate(kind="symbol", ref="src/store.py#connect", path="src/store.py",
                      why="seed", score=2.0, evidence=f"# the staging store answers to {value}"),
            Candidate(kind="symbol", ref="src/api.py#serve", path="src/api.py",
                      why="seed", score=1.0, evidence="def serve(): return store.connect()")]


def _j1(tmp_path: Path, value: str, **setup_kwargs: Any) -> FakeJev:
    """Setup in the worker, load in the runner (whose environment holds the
    profile's secret), then J1's rerank with the value inside an excerpt."""
    work = tmp_path / "att" / "work"
    work.mkdir(parents=True)
    env = attempt.setup(work=work, db=_Db({"workspace_settings/acme": OPTED_IN}),
                        tenant_id="acme", repository_url=URL,
                        environ={settings.INTERNAL_URL_ENV: INTERNAL_URL}, **setup_kwargs)
    assert attempt.CONFIG_ENV in env
    fake = FakeJev()
    judge, why = attempt.load({attempt.CONFIG_ENV: env[attempt.CONFIG_ENV], SECRET_NAME: value},
                              transport=fake, sleep=lambda s: None)
    assert why == "on" and judge is not None
    rank.rerank(_rows(value), "implement", "brief", judge)
    assert len(fake.requests) == 1
    return fake


def test_the_passphrase_is_not_credential_shaped():
    """The control: rules 2 and 3 alone would send it."""
    value = _passphrase()
    assert not sanitize.is_flagged(f"# the staging store answers to {value}")


def test_a_profile_secret_inside_a_j1_excerpt_is_dropped_before_the_jev_call(tmp_path):
    value = _passphrase()
    fake = _j1(tmp_path, value, secret_env_names=(SECRET_NAME,))
    sent = json.dumps(fake.requests)
    assert value not in sent and "orange harbor lantern" not in sent
    first, second = fake.states()[0]["candidates"]
    assert first["path"] == "src/store.py" and "excerpt" not in first
    assert second["excerpt"] == "def serve(): return store.connect()"
    config = json.loads((tmp_path / "att" / register.JUDGE_DIR / register.CONFIG_FILE).read_text())
    assert config["secret_env_names"] == [SECRET_NAME]
    assert value not in json.dumps(config)  # names only, never the value


def test_without_the_names_the_same_excerpt_is_sent(tmp_path):
    """The measurement can come out the other way: a setup that names no
    secret sends the passphrase, so the test above is held by the threading."""
    value = _passphrase()
    fake = _j1(tmp_path, value)
    assert value in json.dumps(fake.requests)


def test_graphstage_hands_the_profile_secret_names_to_jev_setup(tmp_path, monkeypatch):
    seen: list[dict] = []

    def fake_setup(**kwargs: Any) -> dict[str, str]:
        seen.append(kwargs)
        return {}

    monkeypatch.setattr(attempt, "setup", fake_setup)
    monkeypatch.setattr(graphstage, "stage", lambda **kw: graphstage.StageResult(False, "no index"))
    work = tmp_path / "work"
    (work / "repo").mkdir(parents=True)
    graphstage.prepare(work, profile_name="claude-code",
                       runner_argv=["python", "-m", "agent_worker.runners.claude_code"],
                       db=object(), store=object(), tenant_id="acme", repository_url=URL,
                       task_id="t", checkout=work / "repo", base_sha="a" * 40,
                       secret_env_names=(SECRET_NAME, "WIDGET_SIGNING_SEED"))
    assert [tuple(s["secret_env_names"]) for s in seen] == [(SECRET_NAME, "WIDGET_SIGNING_SEED")]


def test_the_lifecycle_passes_the_task_profiles_secrets_to_graphstage(
        db, worker_factory, monkeypatch):
    """The one lifecycle call site: `profile.secrets`, the names the
    credentials step resolved into the runner's environment."""
    # The mock profile with a registered secret; provider None, so the
    # credentials step resolves nothing and the attempt runs as it does.
    profile = dataclasses.replace(RUNNER_PROFILES["mock"], secrets=(SECRET_NAME,))
    monkeypatch.setattr(config_mod.WorkerConfig, "profile", property(lambda self: profile))
    seen: list[Any] = []

    def fake_prepare(*_args: Any, **kwargs: Any) -> dict[str, str]:
        seen.append(kwargs.get("secret_env_names"))
        return {}

    monkeypatch.setattr(lifecycle.graphstage_mod, "prepare", fake_prepare)
    seed_attempt(db)
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.OK, db.doc("tasks/task_1").get("last_error")
    assert seen and all(tuple(names) == (SECRET_NAME,) for names in seen)
