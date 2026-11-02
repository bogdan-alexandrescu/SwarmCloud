"""The worker's Jev judgments actually run, under the signed setting and one budget
(lane JEV-JUDGMENTS-RUN; the owner chose on 2026-11-02 to build every proposal
in CLD's Jev report).

* THE SIGNED SETTING. `code_intelligence.jev` counts only when the scheduler,
  which holds the settings key, vouched for its MAC (`attempt.VERDICT_ENV`);
  unsigned or mismatched is off (tenant-settings-integrity.md row 7).
* THE SETUP ORDER. `jevkey.prepare` writes `private/jev-typesafe.key` before
  `graphstage.prepare` runs `judge.attempt.setup`, which reads it.
* THE MONTHLY CAP AND THE PER-TASK COUNT. `setup` starts the ledger with the
  month's use and the task's earlier attempts' calls; `settle` writes one
  `jev_usage` row per attempt and the month's total, fenced by the generation
  (invariant 5).

Every Jev refusal leaves the output byte-identical to the local-only run
(context-quality.md §5.6): the J1 rerank below is compared with `judge=None`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_worker import lifecycle
from agent_worker.contextpack import rank
from agent_worker.contextpack.retrieve import Candidate
from agent_worker.errors import ExitCode
from agent_worker.judge import attempt, budget, register, settings

from fakes import FakeFirestore, FakeTransactionRunner
from test_jev_wiring import INTERNAL_URL, URL, FakeJev
from worker_seeds import TENANT, seed_attempt

NOW = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)
MONTH = "2026-11"
TASK = "task_jev"
SIGNED = {attempt.VERDICT_ENV: "true"}


def _env(**extra: str) -> dict[str, str]:
    return {settings.INTERNAL_URL_ENV: INTERNAL_URL, **SIGNED,
            attempt.TASK_ID_ENV: TASK, attempt.GENERATION_ENV: "2", **extra}


def _db(block: dict[str, Any]) -> FakeFirestore:
    db = FakeFirestore()
    db.seed("workspace_settings/acme", {"code_intelligence": block})
    return db


def _setup(tmp_path: Path, db: Any, environ: dict[str, str],
           record: dict[str, Any] | None = None) -> dict[str, str]:
    work = tmp_path / "att" / "work"
    work.mkdir(parents=True, exist_ok=True)
    return attempt.setup(work=work, db=db, tenant_id="acme", repository_url=URL,
                         environ=environ, record=record, now=lambda: NOW)


def _rows() -> list[Candidate]:
    return [Candidate(kind="symbol", ref="src/a.py#one", path="src/a.py", why="seed",
                      score=2.0, evidence="def one(): return 1"),
            Candidate(kind="symbol", ref="src/b.py#two", path="src/b.py", why="seed",
                      score=1.0, evidence="def two(): return 2")]


def _ledger(env: dict[str, str]) -> dict[str, Any]:
    config = json.loads(Path(env[attempt.CONFIG_ENV]).read_text())
    return json.loads(Path(config["ledger"]).read_text())


# -- the signed setting -----------------------------------------------------------


@pytest.mark.parametrize("environ, why", [
    pytest.param({settings.INTERNAL_URL_ENV: INTERNAL_URL}, "no_verdict", id="no-verdict"),
    pytest.param({settings.INTERNAL_URL_ENV: INTERNAL_URL, attempt.VERDICT_ENV: "false",
                  attempt.VERDICT_REFUSED_ENV: "unsigned"}, "unsigned", id="unsigned"),
    pytest.param({settings.INTERNAL_URL_ENV: INTERNAL_URL, attempt.VERDICT_ENV: "false",
                  attempt.VERDICT_REFUSED_ENV: "mac_mismatch"}, "mac_mismatch", id="mismatch"),
])
def test_unsigned_settings_turn_judgments_off(tmp_path, environ, why):
    record: dict[str, Any] = {}
    assert _setup(tmp_path, _db({"jev": True}), environ, record) == {}
    assert not (tmp_path / "att" / register.JUDGE_DIR).exists()
    assert record["on"] is False and why in record["reason"]


def test_signed_settings_turn_judgments_on(tmp_path):
    env = _setup(tmp_path, _db({"jev": True}), _env())
    assert attempt.CONFIG_ENV in env


def test_the_verdict_names_are_the_schedulers():
    from scheduler import judgment

    assert attempt.VERDICT_ENV == judgment.JEV_JUDGMENTS_ENV
    assert attempt.VERDICT_REFUSED_ENV == judgment.JEV_JUDGMENTS_REFUSED_ENV


# -- the setup order ------------------------------------------------------------


def test_the_typesafe_key_file_exists_before_the_judge_is_set_up(
        db, worker_factory, monkeypatch):
    """`attempt.setup` reads `private/jev-typesafe.key` (`_read_key`), which
    `jevkey.prepare` writes for a live-browser attempt. Red before: the
    lifecycle ran the graph context, and so the judge's setup, first."""
    seen: list[bool] = []

    def fake_jevkey(**kwargs: Any) -> dict[str, str]:
        private = Path(kwargs["private_dir"])
        private.mkdir(parents=True, exist_ok=True)
        (private / attempt.TYPESAFE_KEY_FILE).write_text("k", encoding="utf-8")
        return {}

    def fake_graph(*_args: Any, **kwargs: Any) -> dict[str, str]:
        seen.append((Path(kwargs["private_dir"]) / attempt.TYPESAFE_KEY_FILE).is_file())
        return {}

    monkeypatch.setattr(lifecycle.jevkey_mod, "prepare", fake_jevkey)
    monkeypatch.setattr(lifecycle.graphstage_mod, "prepare", fake_graph)
    seed_attempt(db)
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.OK, db.doc("tasks/task_1").get("last_error")
    assert seen and all(seen)


# -- the monthly cap ----------------------------------------------------------------


def test_a_month_over_its_cap_skips_with_budget_monthly_and_changes_nothing(tmp_path):
    db = _db({"jev": True, "jev_monthly_cap_tokens": 10_000})
    db.seed(f"{budget.MONTHLY_COLLECTION}/acme_{MONTH}",
            {"tenant_id": "acme", "month": MONTH, "input_tokens": 9_000})
    env = _setup(tmp_path, db, _env())
    assert _ledger(env)["month_used_tokens"] == 9_000
    fake = FakeJev()
    judge, why = attempt.load({attempt.CONFIG_ENV: env[attempt.CONFIG_ENV]},
                              transport=fake, sleep=lambda s: None)
    assert why == "on" and judge is not None
    local = rank.rerank(_rows(), "implement", "brief", None)
    judged = rank.rerank(_rows(), "implement", "brief", judge)
    assert fake.requests == []
    assert repr(judged) == repr(local)
    assert [row["outcome"] for row in _ledger(env)["audit"]] == ["skipped:budget_monthly"]


def test_under_the_cap_the_call_is_made(tmp_path):
    db = _db({"jev": True, "jev_monthly_cap_tokens": 1_000_000})
    db.seed(f"{budget.MONTHLY_COLLECTION}/acme_{MONTH}", {"input_tokens": 9_000})
    env = _setup(tmp_path, db, _env())
    fake = FakeJev()
    judge, _ = attempt.load({attempt.CONFIG_ENV: env[attempt.CONFIG_ENV]},
                            transport=fake, sleep=lambda s: None)
    rank.rerank(_rows(), "implement", "brief", judge)
    assert len(fake.requests) == 1


def test_with_no_cap_the_month_is_not_read(tmp_path):
    db = _db({"jev": True})
    db.seed(f"{budget.MONTHLY_COLLECTION}/acme_{MONTH}", {"input_tokens": 10 ** 9})
    env = _setup(tmp_path, db, _env())
    assert _ledger(env)["month_used_tokens"] == 0


# -- the per-task count -------------------------------------------------------------


def test_a_tasks_second_attempt_counts_the_first_attempts_calls(tmp_path):
    db = _db({"jev": True})
    first = _setup(tmp_path / "one", db, _env(**{attempt.GENERATION_ENV: "1"}))
    ledger = budget.Ledger(Path(json.loads(Path(first[attempt.CONFIG_ENV]).read_text())["ledger"]))
    for _ in range(5):
        assert ledger.admit(estimate=0) is None
        ledger.record({"input_tokens": 1_000, "outcome": "ok"}, succeeded=True)
    assert attempt.settle(first, db=db, run_transaction=FakeTransactionRunner(db).run,
                          tenant_id="acme", task_id=TASK, generation=1,
                          now=lambda: NOW) == (5, 5_000)
    second = _setup(tmp_path / "two", db, _env())
    state = _ledger(second)
    assert state["calls"] == 5 and state["input_tokens"] == 5_000
    # The carried spend was charged by the first attempt: never again.
    assert state["settled_calls"] == 5 and state["settled_tokens"] == 5_000
    again = budget.Ledger(Path(json.loads(Path(second[attempt.CONFIG_ENV]).read_text())["ledger"]))
    for _ in range(3):
        assert again.admit(estimate=0) is None
    assert again.admit(estimate=0) == "budget_calls"
    # Another task, or another tenant's rows, carry nothing.
    other = _setup(tmp_path / "three", db, _env(**{attempt.TASK_ID_ENV: "task_other"}))
    assert _ledger(other)["calls"] == 0


# -- the settle -------------------------------------------------------------------


def _spent_attempt(tmp_path: Path, db: FakeFirestore, calls: int, tokens: int) -> dict[str, str]:
    env = _setup(tmp_path, db, _env())
    ledger = budget.Ledger(Path(json.loads(Path(env[attempt.CONFIG_ENV]).read_text())["ledger"]))
    for _ in range(calls):
        assert ledger.admit(estimate=0) is None
        ledger.record({"input_tokens": tokens // calls, "outcome": "ok"}, succeeded=True)
    return env


def test_settle_writes_one_jev_usage_row_per_attempt_and_the_month(tmp_path):
    db = _db({"jev": True})
    env = _spent_attempt(tmp_path, db, 2, 3_000)
    run = FakeTransactionRunner(db).run
    kwargs = dict(db=db, run_transaction=run, tenant_id="acme", task_id=TASK, generation=2,
                  now=lambda: NOW)
    assert attempt.settle(env, **kwargs) == (2, 3_000)
    assert attempt.settle(env, **kwargs) == (0, 0)  # once
    rows = {path: doc for path, doc in db.documents.items()
            if path.startswith(budget.MONTHLY_COLLECTION + "/")}
    assert rows[f"jev_usage/acme_{TASK}_g2"] == {
        "kind": "attempt", "tenant_id": "acme", "task_id": TASK, "generation": 2,
        "month": MONTH, "calls": 2, "input_tokens": 3_000}
    assert rows[f"jev_usage/acme_{MONTH}"]["input_tokens"] == 3_000
    assert len(rows) == 2


def test_a_stale_generation_settles_nothing(tmp_path, db, worker_factory):
    """The real fence: `ControlPlane._fenced_task`, through the transaction,
    on a task whose generation moved on. Nothing is written, and the spend
    stays unsettled."""
    seed_attempt(db, generation=1, task_generation=2)
    worker, _, _ = worker_factory()
    db.seed("workspace_settings/acme", {"code_intelligence": {"jev": True}})
    env = _spent_attempt(tmp_path, db, 1, 1_000)
    settled = attempt.settle(env, db=db, run_transaction=worker.control._run_transaction,
                             tenant_id=TENANT, task_id="task_1", generation=1,
                             fence=lambda txn: worker.control._fenced_task(txn, write="jev usage"),
                             now=lambda: NOW)
    assert settled is None
    assert not [p for p in db.documents if p.startswith(budget.MONTHLY_COLLECTION + "/")]
    assert _ledger(env)["settled_calls"] == 0


def test_the_lifecycle_settles_the_attempt_after_the_agent(db, worker_factory, monkeypatch,
                                                           tmp_path):
    seed_attempt(db)
    home = tmp_path / "judge-att" / register.JUDGE_DIR
    home.mkdir(parents=True)
    ledger = budget.Ledger.create(home / register.LEDGER_FILE)
    assert ledger.admit(estimate=0) is None
    ledger.record({"input_tokens": 700, "outcome": "ok"}, succeeded=True)
    config = home / register.CONFIG_FILE
    config.write_text(json.dumps({"ledger": str(ledger.path)}))
    monkeypatch.setattr(lifecycle.graphstage_mod, "prepare",
                        lambda *a, **k: {attempt.CONFIG_ENV: str(config)})
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.OK, db.doc("tasks/task_1").get("last_error")
    rows = [doc for path, doc in db.documents.items()
            if path.startswith(f"{budget.MONTHLY_COLLECTION}/") and doc.get("kind") == "attempt"]
    assert len(rows) == 1
    assert rows[0]["task_id"] == "task_1" and rows[0]["calls"] == 1
    assert rows[0]["input_tokens"] == 700 and rows[0]["tenant_id"] == TENANT


def test_a_failed_settle_changes_no_output_and_is_retried(tmp_path):
    db = _db({"jev": True})
    env = _spent_attempt(tmp_path, db, 1, 400)

    def down(fn: Any) -> Any:
        raise RuntimeError("firestore down")

    assert attempt.settle(env, db=db, run_transaction=down, tenant_id="acme", task_id=TASK,
                          generation=2, now=lambda: NOW) is None
    assert attempt.settle(env, db=db, run_transaction=FakeTransactionRunner(db).run,
                          tenant_id="acme", task_id=TASK, generation=2,
                          now=lambda: NOW) == (1, 400)
