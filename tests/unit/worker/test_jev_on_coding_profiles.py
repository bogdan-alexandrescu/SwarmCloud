"""Jev on the coding profiles, the worker's half (lane jev-on-coding-profiles,
the owner's choice of 4 November 2026; part of #2688).

The jev_usage ledger for 3-4 November 2026 showed 377 Jev calls, every one
from a claude-code-browser step, and 902 attempts that settled a reservation
and made no call, 696 of them on claude-code and claude-code-large. All three
profiles run the same runner (`agent_worker.runners.claude_code`) in an image
built on the same base, which carries `swarm_jev`, so the client was there.
The cause on the worker: on the TypeSafe backend `judge.attempt.setup` read
only the key file `jevkey.prepare` writes for a LIVE-BROWSER attempt, and
asked for the workspace's key only under the workflow switch
`jev_judgments: on`. A coding step under the signed opt-in alone read
`no_key`: no `judge/config.json`, so no J0/J1 in the context pack, no J3 on
a review and no `jev_decide` tool -- while its reservation was still made
and settled empty.

Now an opted-in step on any profile resolves the WORKSPACE's key (no
submitter, as the quality gates resolve theirs) when it has neither the
browser's key file nor the forwarder's shim, so a coding step gets the same
config, ledger and budget a browser step gets.

The scheduler's half (the pod's forwarder or direct label):
tests/unit/control_plane/test_jev_on_coding_profiles.py.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

import pytest

from agent_worker.judge import attempt, register, settings

from test_quality_gates import _Db

TENANT = "eng"
URL = "https://github.com/acme/widgets"
TYPESAFE_ON = {"code_intelligence": {"jev": True, "judgment_backend": "typesafe"}}


def _setup(tmp_path: Path, workspace: dict[str, Any], *, key: str | None,
           verdict: str = "true", key_file: str | None = None,
           switch: str | None = None) -> tuple[dict[str, str], dict[str, Any], list[int]]:
    work = tmp_path / "att" / "work"
    work.mkdir(parents=True)
    if key_file is not None:
        private = tmp_path / "att" / register.KEY_DIR
        private.mkdir(parents=True, exist_ok=True)
        (private / attempt.TYPESAFE_KEY_FILE).write_text(key_file)
    asked: list[int] = []

    def resolve_key() -> str | None:
        asked.append(1)
        return key

    record: dict[str, Any] = {}
    env = attempt.setup(work=work, db=_Db({f"workspace_settings/{TENANT}": workspace}),
                        tenant_id=TENANT, repository_url=URL,
                        environ={attempt.VERDICT_ENV: verdict}, switch=switch,
                        resolve_key=resolve_key, record=record)
    return env, record, asked


def test_a_coding_step_opted_in_on_typesafe_gets_the_workspace_key(tmp_path):
    """No workflow switch, no live browser: the signed opt-in alone.
    MUTATION: put back `switch == ON and` before `resolve_key()` and this
    is {} with reason `no_key` -- the ledger's 696 empty coding attempts."""
    key = "ts-" + secrets.token_hex(24)
    env, record, asked = _setup(tmp_path, TYPESAFE_ON, key=key)
    assert asked == [1]
    assert attempt.CONFIG_ENV in env, record
    config_text = Path(env[attempt.CONFIG_ENV]).read_text()
    config = json.loads(config_text)
    assert config["backend"]["name"] == settings.TYPESAFE
    assert Path(config["key_file"]).read_text() == key
    assert key not in config_text
    assert record["on"] is True and record["source"] == "workspace"
    judge, why = attempt.load({attempt.CONFIG_ENV: env[attempt.CONFIG_ENV]})
    assert why == "on" and judge is not None
    attempt.finish(env)
    assert not Path(config["key_file"]).exists()


def test_a_coding_step_gets_the_same_judge_a_browser_step_gets(tmp_path):
    """The browser's key file and the coding step's resolved key produce the
    same config, budget included: same backend, same per-attempt caps, same
    ledger shape. Only the key's origin differs."""
    key = "ts-" + secrets.token_hex(24)
    browser_env, _, browser_asked = _setup(tmp_path / "browser", TYPESAFE_ON, key=None,
                                           key_file=key)
    coding_env, _, coding_asked = _setup(tmp_path / "coding", TYPESAFE_ON, key=key)
    assert browser_asked == [] and coding_asked == [1]
    browser = json.loads(Path(browser_env[attempt.CONFIG_ENV]).read_text())
    coding = json.loads(Path(coding_env[attempt.CONFIG_ENV]).read_text())

    def shape(config: dict[str, Any], root: Path) -> dict[str, Any]:
        return json.loads(json.dumps(config).replace(str(root), "<root>"))

    assert shape(browser, tmp_path / "browser") == shape(coding, tmp_path / "coding")


@pytest.mark.parametrize("workspace, verdict, reason", [
    pytest.param({"code_intelligence": {"judgment_backend": "typesafe"}}, "true",
                 "not opted in", id="not-opted-in"),
    pytest.param(TYPESAFE_ON, "false", "unsigned", id="unsigned-opt-in"),
    pytest.param({"code_intelligence": {"jev": True, "judgment_backend": "internal-jev"}},
                 "true", "internal backend carries no key", id="internal-jev"),
])
def test_no_key_is_resolved_where_the_step_would_not_judge_on_typesafe(
        tmp_path, workspace, verdict, reason):
    _, _, asked = _setup(tmp_path, workspace, key="ts-" + secrets.token_hex(8),
                         verdict=verdict)
    assert asked == [], reason


def test_the_browsers_key_file_is_used_and_the_workspace_key_not_read(tmp_path):
    env, _, asked = _setup(tmp_path, TYPESAFE_ON, key="ts-" + secrets.token_hex(8),
                           key_file="ts-" + secrets.token_hex(8))
    assert asked == [] and attempt.CONFIG_ENV in env


def test_the_switch_off_still_resolves_nothing(tmp_path):
    env, _, asked = _setup(tmp_path, TYPESAFE_ON, key="ts-" + secrets.token_hex(8),
                           switch="off")
    assert env == {} and asked == []
