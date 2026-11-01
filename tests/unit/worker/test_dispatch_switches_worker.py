"""The workflow switches, the worker's half (the owner's experiment, 2026-11-01).

A step's signed `metadata.dispatch` may carry `code_intelligence`,
`quality_gates` and `jev_judgments`, each `on` or `off`. Each only narrows
what the stored settings allow, except that `code_intelligence: on` takes
the step out of the A/B and `jev_judgments: on` runs the judgments on the
step's own profile with the WORKSPACE's TypeSafe key. Every attempt records
the switch, its value and the deciding source.

The swarm-api half: tests/unit/control_plane/test_dispatch_switches.py.
"""

from __future__ import annotations

import json
import secrets
import stat
from pathlib import Path
from typing import Any

import pytest

from agent_worker import graphstage, indexrun, jevkey, qualitygates
from agent_worker.judge import attempt, register, settings

from test_graphstage import (  # noqa: F401 -- `clone` is a fixture
    CLAUDE, GKE, SERVICE_URL, TENANT, URL, _FakeTicket, _promote, clone,
)
from test_quality_gates import _Db, _logger, _world

INTERNAL_URL = "http://decide.internal:8080/v1/decide"


def _arm_task(arm: str) -> str:
    return next(f"task_{i}" for i in range(1000) if graphstage.assign_arm(f"task_{i}") == arm)


def _decide(*, override: Any = None, context: Any = None, ab: Any = "ab",
            task_id: str = "task_1", switch: str | None = None) -> graphstage.Decision:
    code_intel: dict[str, Any] = {"context_ab": ab}
    if context is not None:
        code_intel["context"] = context
    return graphstage.decide(repository_override=override,
                             workspace_settings={"code_intelligence": code_intel},
                             task_id=task_id, switch=switch)


# --------------------------------------------------------------------------
# code_intelligence: decide
# --------------------------------------------------------------------------


@pytest.mark.parametrize("arm", ["A", "B"])
def test_on_stages_on_both_arms_and_says_the_switch_decided(arm):
    decision = _decide(task_id=_arm_task(arm), switch="on")
    # MUTATION: drop the `switch == ON` branch and arm A stages nothing.
    assert decision.stage is True and decision.arm is None
    assert decision.source == "workflow switch"


@pytest.mark.parametrize("arm", ["A", "B"])
@pytest.mark.parametrize("context, ab", [("on", "off"), ("on", "ab"), (None, "ab")])
def test_off_never_stages(arm, context, ab):
    decision = _decide(context=context, ab=ab, task_id=_arm_task(arm), switch="off")
    assert decision.stage is False and decision.source == "workflow switch"


@pytest.mark.parametrize("override, context, source", [
    ("off", "on", "repository"), (None, "off", "workspace"), ("off", None, "repository"),
])
def test_on_never_stages_over_a_stored_off(override, context, source):
    decision = _decide(override=override, context=context, task_id=_arm_task("B"), switch="on")
    assert decision.stage is False and decision.source == source


@pytest.mark.parametrize("switch", [None, "ON", "yes", 1])
def test_no_switch_is_todays_decision(switch):
    for arm in ("A", "B"):
        task = _arm_task(arm)
        assert _decide(task_id=task, switch=switch) == _decide(task_id=task)
    assert _decide(task_id=_arm_task("A"), switch=switch).source == "default"


def test_switch_of_reads_only_the_two_words():
    assert graphstage.switch_of({"code_intelligence": "on"}, "code_intelligence") == "on"
    assert graphstage.switch_of({"code_intelligence": "off"}, "code_intelligence") == "off"
    assert graphstage.switch_of({"code_intelligence": True}, "code_intelligence") is None
    assert graphstage.switch_of(None, "code_intelligence") is None


# --------------------------------------------------------------------------
# code_intelligence: the settings document, and no graph tool under off
# --------------------------------------------------------------------------


def _stage(db: Any, store: Any, checkout: Path, base: str, *, task_id: str,
           switch: str | None = None) -> graphstage.StageResult:
    return graphstage.stage(db=db, store=store, tenant_id=TENANT, repository_url=URL,
                            task_id=task_id, checkout=checkout, base_sha=base,
                            context_dir=checkout.parent / graphstage.CONTEXT_DIR_NAME,
                            switch=switch)


def test_the_context_fields_are_read_from_workspace_settings(db, store, clone):
    """swarm-api's Code intelligence screen writes `workspace_settings`; a
    `tenant_settings` document is never read (the settings bug, 2026-11-01)."""
    checkout, first, second = clone
    _promote(db, store, first)
    db.seed(f"tenant_settings/{TENANT}", {"code_intelligence": {"context": "on",
                                                                "context_ab": "off"}})
    db.seed(f"workspace_settings/{TENANT}", {"code_intelligence": {"context": "off"}})
    result = _stage(db, store, checkout, second, task_id=_arm_task("B"))
    # MUTATION: read `tenant_settings` and this stages.
    assert result.staged is False
    assert result.decision is not None and result.decision.source == "workspace"
    assert graphstage.SETTINGS_COLLECTION == "workspace_settings"


def test_the_workspace_ab_reaches_the_worker_and_the_switch_overrides_it(db, store, clone):
    checkout, first, second = clone
    _promote(db, store, first)
    db.seed(f"workspace_settings/{TENANT}", {"code_intelligence": {"context_ab": "ab"}})
    task = _arm_task("A")
    assert _stage(db, store, checkout, second, task_id=task).staged is False
    assert _stage(db, store, checkout, second, task_id=task, switch="on").staged is True


def _prepare(db: Any, store: Any, checkout: Path, base: str, *, ticket: Any,
             dispatch: dict[str, Any] | None, jev_key: Any = None,
             task_id: str = "task_b") -> dict[str, str]:
    private = checkout.parent.parent / "private"
    private.mkdir(mode=0o700, exist_ok=True)
    return graphstage.prepare(
        checkout.parent, profile_name="claude-code", runner_argv=CLAUDE, db=db, store=store,
        tenant_id=TENANT, repository_url=URL, task_id=task_id, checkout=checkout,
        base_sha=base, graph_ticket=ticket, private_dir=private, backend=GKE,
        service_url=SERVICE_URL, probe=lambda url, path: None, dispatch=dispatch,
        jev_key=jev_key)


def _stage_record(checkout: Path) -> dict[str, Any]:
    return json.loads((checkout.parent / graphstage.CONTEXT_DIR_NAME
                       / graphstage.STAGE_RECORD_NAME).read_text())


def test_off_gets_no_graph_tool_neither_staged_files_nor_the_service(db, store, clone):
    checkout, first, second = clone
    _promote(db, store, first)
    db.seed(f"workspace_settings/{TENANT}", {"code_intelligence": {
        "context": "on", "context_ab": "off", "graph_delivery": "service"}})
    ticket = _FakeTicket()
    env = _prepare(db, store, checkout, second, ticket=ticket,
                   dispatch={"code_intelligence": "off"})
    assert ticket.started == []
    for name in (graphstage.SNAPSHOT_ENV, graphstage.PIN_ENV, graphstage.TICKET_FILE_ENV,
                 graphstage.URL_ENV, graphstage.CONTEXT_ENV):
        assert name not in env
    context_dir = checkout.parent / graphstage.CONTEXT_DIR_NAME
    assert not (context_dir / graphstage.SNAPSHOT_DIR_NAME).exists()
    record = _stage_record(checkout)
    assert record[graphstage.SWITCHES_FIELD]["code_intelligence"] == {
        "value": "off", "source": "workflow switch", "graph": False,
        "reason": record["reason"]}


def test_on_records_the_switch_and_the_source_in_stage_json(db, store, clone):
    checkout, first, second = clone
    _promote(db, store, first)
    db.seed(f"workspace_settings/{TENANT}", {"code_intelligence": {"context_ab": "ab"}})
    env = _prepare(db, store, checkout, second, ticket=_FakeTicket(),
                   dispatch={"code_intelligence": "on"}, task_id=_arm_task("A"))
    assert graphstage.SNAPSHOT_ENV in env
    line = _stage_record(checkout)[graphstage.SWITCHES_FIELD]["code_intelligence"]
    assert line["value"] == "on" and line["source"] == "workflow switch" and line["graph"]
    summary = graphstage.switches_summary(checkout.parent, {"code_intelligence": "on"})
    assert summary is not None and summary["code_intelligence"] == line


def test_no_switch_adds_nothing_to_the_attempts_summary(tmp_path):
    assert graphstage.switches_summary(tmp_path, {"strategy": "collect"}) is None
    assert graphstage.switches_summary(tmp_path, {"jev_judgments": "off"}) == {
        "code_intelligence": {"value": None}, "jev_judgments": {"value": "off"}}


# --------------------------------------------------------------------------
# jev_judgments
# --------------------------------------------------------------------------


def _judged(tmp_path: Path, workspace: dict[str, Any] | None, *, switch: str | None,
            key: str | None = None, override: str | None = None) -> tuple[dict, dict, list]:
    work = tmp_path / "att" / "work"
    work.mkdir(parents=True)
    docs: dict[str, dict] = {}
    if workspace is not None:
        docs[f"workspace_settings/{TENANT}"] = workspace
    if override is not None:
        where = indexrun.target(TENANT, URL)
        docs[f"{indexrun.REPOSITORIES_COLLECTION}/{where.repo_id}"] = {
            "tenant_id": TENANT, "jev": override}
    asked: list[int] = []

    def resolve_key() -> str | None:
        asked.append(1)
        return key

    record: dict[str, Any] = {}
    env = attempt.setup(work=work, db=_Db(docs), tenant_id=TENANT, repository_url=URL,
                        environ={settings.INTERNAL_URL_ENV: INTERNAL_URL}, switch=switch,
                        resolve_key=resolve_key, record=record)
    return env, record, asked


TYPESAFE_ON = {"code_intelligence": {"jev": True, "judgment_backend": "typesafe"}}


def test_on_runs_the_judgments_on_claude_code_with_the_workspace_key(tmp_path):
    key = "ts-" + secrets.token_hex(24)
    env, record, asked = _judged(tmp_path, TYPESAFE_ON, switch="on", key=key)
    # MUTATION: drop the `resolve_key()` call and this is {} (`no_key`).
    assert attempt.CONFIG_ENV in env and asked == [1]
    config = json.loads(Path(env[attempt.CONFIG_ENV]).read_text())
    assert config["backend"]["name"] == settings.TYPESAFE
    key_file = Path(config["key_file"])
    assert key_file == tmp_path / "att" / register.KEY_DIR / register.KEY_FILE
    assert key_file.read_text() == key
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert key not in Path(env[attempt.CONFIG_ENV]).read_text()
    assert record["switch"] == "jev_judgments" and record["value"] == "on"
    assert record["source"] == "workflow switch" and record["on"] is True
    assert record["backend"] == settings.TYPESAFE
    judge, why = attempt.load({attempt.CONFIG_ENV: env[attempt.CONFIG_ENV]})
    assert why == "on" and judge is not None
    attempt.finish(env)
    assert not key_file.exists()


def test_without_the_switch_no_workspace_key_is_resolved(tmp_path):
    """Today's behaviour: only a live-browser attempt's key file is read."""
    env, record, asked = _judged(tmp_path, TYPESAFE_ON, switch=None,
                                 key="ts-" + secrets.token_hex(8))
    assert env == {} and asked == []
    assert record["value"] is None and record["on"] is False


@pytest.mark.parametrize("workspace", [
    TYPESAFE_ON, {"code_intelligence": {"jev": True}},
])
def test_off_runs_none_whatever_the_workspace_allows(tmp_path, workspace):
    env, record, asked = _judged(tmp_path, workspace, switch="off",
                                 key="ts-" + secrets.token_hex(8))
    assert env == {} and asked == []
    assert not (tmp_path / "att" / register.JUDGE_DIR).exists()
    assert record == {"switch": "jev_judgments", "value": "off", "source": "workflow switch",
                      "on": False, "reason": "jev judgments are off (workflow switch)"}


@pytest.mark.parametrize("workspace, override, source", [
    ({"code_intelligence": {"judgment_backend": "typesafe"}}, None, "default"),
    ({"code_intelligence": {"jev": False, "judgment_backend": "typesafe"}}, None, "workspace"),
    (TYPESAFE_ON, "off", "repository"),
])
def test_on_never_runs_where_the_workspace_does_not_let_jev_see_code(
        tmp_path, workspace, override, source):
    env, record, asked = _judged(tmp_path, workspace, switch="on",
                                 key="ts-" + secrets.token_hex(8), override=override)
    assert env == {} and asked == []
    assert record["source"] == source and record["on"] is False


def test_on_with_no_workspace_key_is_off_and_says_why(tmp_path):
    env, record, asked = _judged(tmp_path, TYPESAFE_ON, switch="on", key=None)
    assert env == {} and asked == [1]
    assert record["on"] is False and record["reason"]


def test_the_workspace_key_is_resolved_with_no_submitter(monkeypatch):
    seen: list[dict[str, Any]] = []
    key = "ts-" + secrets.token_hex(12)

    def fake_resolve(**kw: Any) -> tuple[str, str, str]:
        seen.append(kw)
        return key, "workspace", "swarm-tenant-eng-typesafe"

    monkeypatch.setattr(jevkey, "resolve", fake_resolve)
    log, stream = _logger()
    assert jevkey.resolve_workspace(db=object(), client=object(), tenant_id="eng",
                                    logger=log) == key
    assert seen[0]["submitted_by"] is None and seen[0]["tenant_id"] == "eng"
    assert key not in stream.getvalue()
    assert jevkey.resolve_workspace(db=None, client=object(), tenant_id="eng",
                                    logger=log) is None


# --------------------------------------------------------------------------
# quality_gates
# --------------------------------------------------------------------------


class _Says:
    """A `GatesVerdict` built from the scheduler's word, as the job carries it."""

    def __new__(cls, word: str | None) -> qualitygates.GatesVerdict:
        environ = {} if word is None else {qualitygates.VERDICT_ENV: word}
        return qualitygates.GatesVerdict(environ)


def _gates(tmp_path: Path, stored: Any, *, switch: str | None, word: str | None
           ) -> tuple[dict[str, str], qualitygates.GatesVerdict]:
    db, client = _world(gates=stored, tenant_key="ts-" + secrets.token_hex(24))
    private = tmp_path / "private"
    private.mkdir(exist_ok=True)
    log, _stream = _logger()
    verdict = _Says(word)
    env = qualitygates.prepare(db=db, client=client, tenant_id="eng", private_dir=private,
                               logger=log, verdict=verdict, switch=switch)
    return env, verdict


@pytest.mark.parametrize("stored, switch, word, on, source", [
    # On request: only a step whose switch says on.
    ("on_request", "on", "on_request", True, "workflow switch"),
    ("on_request", None, "on_request", False, "workspace"),
    ("on_request", "off", "on_request", False, "workflow switch"),
    # The scheduler vouched for a different word: refused.
    ("on_request", "on", "true", False, "workflow switch"),
    # Only where the signed setting allows.
    (True, "on", "true", True, "workflow switch"),
    (False, "on", None, False, "workspace"),
    (None, "on", None, False, "default"),
    # A true setting with the switch off runs none.
    (True, "off", "true", False, "workflow switch"),
])
def test_quality_gates_on_runs_only_where_the_signed_setting_allows(
        tmp_path, stored, switch, word, on, source):
    env, verdict = _gates(tmp_path, stored, switch=switch, word=word)
    assert (qualitygates.CONFIG_ENV in env) is on
    assert verdict.record["switch"] == "quality_gates"
    assert verdict.record["value"] == switch
    assert verdict.record["source"] == source and verdict.record["on"] is on


def test_no_switch_and_a_true_setting_is_todays_gates_with_no_record(tmp_path):
    env, verdict = _gates(tmp_path, True, switch=None, word="true")
    assert qualitygates.CONFIG_ENV in env and verdict.record == {}


@pytest.mark.parametrize("stored, expected", [
    ({"quality_gates": {"jev": True}}, True),
    ({"quality_gates": {"jev": "on_request"}}, "on_request"),
    ({"quality_gates": {"jev": "on-request"}}, False),
    ({"quality_gates": {"jev": False}}, False),
    ({"quality_gates": {}}, None),
    (None, None),
])
def test_the_stored_value_reads_the_third_word_and_nothing_else(stored, expected):
    assert qualitygates.stored_value(stored) == expected
    assert qualitygates.switched_on(stored) is (expected is True)
