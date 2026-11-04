"""The control plane's re-ask of a step's acceptance judgment (`swarm_api.contractreask`).

The owner's answer 5 of 3 November 2026 (docs/design/agent-quality/
jev-contract.md sections 8.2 and 14): when the worker's judgment comes back
`not_run` or `cannot_judge`, the control plane re-asks Jev from the published
diff three times over 30 minutes, then sends the step to the owner. Held here:
at most three asks, all inside the window; the first verdict stops it; a
newer generation supersedes the record with nothing written; two passes never
ask twice on one try; no pass sleeps (the clock is a value). Offline: the
judge is a function, the store the in-memory Firestore.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from swarm_api import contractevidence as ce
from swarm_api import contractjudge as cj
from swarm_api import contractreask as cr
from swarm_api import jevaccount, jevusage
from swarm_api import workcontract as wc

from .conftest import api_settings, seed_task
from .spec_signer import KEY_VERSION, LocalSpecSigner
from .test_contract_verdict import BASE, COMMIT, contract_metadata

SIGNER = LocalSpecSigner()
TASK = "task_reask_1"
T0 = datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc)


def settings():
    return api_settings(spec_signing_key_version=KEY_VERSION,
                        spec_verify_keys=json.dumps({KEY_VERSION: SIGNER.public_pem()}))


def seed_pending(db, *, generation: int = 1) -> wc.WorkContract:
    contract, metadata = contract_metadata()
    doc = seed_task(db, task_id=TASK, tenant_id="eng", state="SUCCEEDED")
    doc.update({"metadata": metadata, "current_generation": generation,
                "repository_url": "https://github.com/acme/widgets.git"})
    SIGNER.sign_document(doc, TASK)
    doc[cr.ACCEPTANCE_FIELD] = {
        "status": cj.JUDGMENT_PENDING, "waiting_on": cr.WAITING_REASK, "owner_reason": None,
        "attempt_id": "att_1", "lease_id": "lease_1", "generation": generation,
        "digest": wc.digest(contract), "commit": COMMIT, "base": BASE,
        "outcome": "not_run", "reason": "transport", "verdicts": [], "changed": [],
        "mechanical": [], "reask": cr.pending_schedule(T0), "at": T0,
    }
    return contract


class Judge:
    """Answers from a script, one per ask, recording when it was asked."""

    def __init__(self, *answers: cr.ReaskResult) -> None:
        self.answers = list(answers)
        self.asked: list[datetime] = []
        self.clock: Clock | None = None

    def __call__(self, task, contract, acc) -> cr.ReaskResult:
        assert self.clock is not None
        self.asked.append(self.clock.now)
        if self.answers:
            return self.answers.pop(0)
        return cr.ReaskResult(cr.OUTCOME_NOT_RUN, reason="transport")


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def reask(db, judge: Judge, clock: Clock) -> cr.ContractReask:
    judge.clock = clock
    return cr.ContractReask(db=db, settings=settings(), judge=judge, now=clock)


def minutes(db, judge: Judge, clock: Clock, upto: int) -> None:
    """One pass a minute, as the scheduled tick runs it, for `upto` minutes."""
    for minute in range(upto + 1):
        clock.now = T0 + timedelta(minutes=minute)
        reask(db, judge, clock).run()


def met(*rids: str) -> cr.ReaskResult:
    return cr.ReaskResult(cr.OUTCOME_JUDGED, verdicts=tuple(
        cj.Verdict(r, cj.MET, "satisfied", "i0", 0.9) for r in rids), call_id="c1",
        changed=("src/login/form.py",), state_sha256="d" * 64)


def acceptance(db) -> dict[str, Any]:
    return db.docs[f"tasks/{TASK}"][cr.ACCEPTANCE_FIELD]


def evidence(db) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith(f"{ce.COLLECTION}/")]


def test_it_asks_at_most_three_times_inside_thirty_minutes_then_hands_to_the_owner(db):
    seed_pending(db)
    judge, clock = Judge(), Clock()
    minutes(db, judge, clock, 60)
    assert len(judge.asked) == cr.REASK_TRIES == 3
    assert all(T0 <= at < T0 + timedelta(minutes=30) for at in judge.asked)
    assert judge.asked == [T0 + offset for offset in cr.REASK_OFFSETS]
    acc = acceptance(db)
    assert acc["status"] == cj.JUDGMENT_PENDING and acc["waiting_on"] == cr.WAITING_OWNER
    assert acc["owner_reason"] == cr.OWNER_REASK_EXHAUSTED
    assert acc["reask"]["tries"] == 3 and acc["reask"]["last"]["reason"] == "transport"
    assert evidence(db) == []


def test_nothing_is_asked_before_the_first_try_is_due(db):
    seed_pending(db)
    judge, clock = Judge(), Clock()
    minutes(db, judge, clock, 4)
    assert judge.asked == []
    assert acceptance(db)["reask"]["tries"] == 0


def test_the_first_verdict_stops_it(db):
    seed_pending(db)
    judge, clock = Judge(cr.ReaskResult(cr.OUTCOME_NOT_RUN, reason="http_503"),
                         met("REQ-1", "REQ-2")), Clock()
    minutes(db, judge, clock, 60)
    assert len(judge.asked) == 2
    acc = acceptance(db)
    assert acc["status"] == cj.JUDGED_MET and acc["waiting_on"] is None
    rows = evidence(db)
    assert {(r["requirement_id"], r["passed"], r["source"]) for r in rows} == {
        ("REQ-1", True, ce.SOURCE_REASK), ("REQ-2", True, ce.SOURCE_REASK)}
    assert all(r["commit"] == COMMIT and r["path"] == "src/login/form.py" for r in rows)


def test_an_unmet_from_the_reask_goes_to_the_owner_with_its_evidence(db):
    """No live attempt to send back: the owner decides, with the verdict."""
    seed_pending(db)
    unmet = cr.ReaskResult(cr.OUTCOME_JUDGED, verdicts=(
        cj.Verdict("REQ-1", cj.UNMET, "not_in_diff", "i0", 0.9),
        cj.Verdict("REQ-2", cj.MET, "satisfied", "i0", 0.9)), call_id="c2",
        changed=("src/login/form.py",))
    judge, clock = Judge(unmet), Clock()
    minutes(db, judge, clock, 60)
    assert len(judge.asked) == 1
    acc = acceptance(db)
    assert acc["waiting_on"] == cr.WAITING_OWNER
    assert acc["owner_reason"] == cr.OWNER_UNMET_AFTER_ATTEMPT
    assert db.docs[f"tasks/{TASK}"]["state"] == "SUCCEEDED"
    assert {r["requirement_id"]: r["passed"] for r in evidence(db)} == {
        "REQ-1": False, "REQ-2": True}


def test_cannot_judge_keeps_it_pending_until_the_tries_are_spent(db):
    seed_pending(db)
    unsure = cr.ReaskResult(cr.OUTCOME_JUDGED, verdicts=(
        cj.Verdict("REQ-1", cj.CANNOT_JUDGE, None, None, 0.0),
        cj.Verdict("REQ-2", cj.MET, "satisfied", "i0", 0.9)), changed=("a.py",))
    judge, clock = Judge(unsure, unsure, unsure), Clock()
    minutes(db, judge, clock, 60)
    assert len(judge.asked) == 3
    assert acceptance(db)["owner_reason"] == cr.OWNER_REASK_EXHAUSTED


def test_a_newer_generation_supersedes_the_record_with_nothing_written(db):
    seed_pending(db)
    db.docs[f"tasks/{TASK}"]["current_generation"] = 2
    before = copy.deepcopy(db.docs)
    judge, clock = Judge(met("REQ-1", "REQ-2")), Clock()
    minutes(db, judge, clock, 40)
    assert judge.asked == []
    assert db.docs == before


def test_superseded_records_never_starve_a_live_one_out_of_the_pass(db):
    seed_pending(db)
    live = db.docs[f"tasks/{TASK}"]
    for n in range(cr.PASS_LIMIT + 1):
        stale = copy.deepcopy(live)
        stale["current_generation"] = 2
        # Due earlier than the live record, so they sort to the front.
        stale[cr.ACCEPTANCE_FIELD]["reask"]["next_at"] = T0 - timedelta(minutes=1)
        db.docs[f"tasks/task_stale_{n}"] = stale
    before = {p: copy.deepcopy(d) for p, d in db.docs.items() if "task_stale_" in p}
    judge, clock = Judge(met("REQ-1", "REQ-2")), Clock()
    clock.now = T0 + cr.REASK_OFFSETS[0]
    reask(db, judge, clock).run()
    assert len(judge.asked) == 1
    assert acceptance(db)["status"] != cj.JUDGMENT_PENDING
    assert {p: d for p, d in db.docs.items() if "task_stale_" in p} == before


def test_a_generation_that_moves_while_jev_is_asked_writes_no_verdict(db):
    seed_pending(db)

    class Moving(Judge):
        def __call__(self, task, contract, acc):
            db.docs[f"tasks/{TASK}"]["current_generation"] = 2
            return super().__call__(task, contract, acc)

    judge, clock = Moving(met("REQ-1", "REQ-2")), Clock()
    clock.now = T0 + cr.REASK_OFFSETS[0]
    reask(db, judge, clock).run()
    assert len(judge.asked) == 1
    assert evidence(db) == []
    assert acceptance(db)["status"] == cj.JUDGMENT_PENDING
    assert acceptance(db)["reask"]["last"] is None


def test_two_passes_in_one_minute_ask_once(db):
    seed_pending(db)
    judge, clock = Judge(), Clock()
    clock.now = T0 + cr.REASK_OFFSETS[0]
    first = reask(db, judge, clock).run()
    second = reask(db, judge, clock).run()
    assert len(judge.asked) == 1
    assert first["asked"] == 1 and second["asked"] == 0


def test_an_amended_contract_hands_the_record_to_the_owner_without_asking(db):
    seed_pending(db)
    acceptance(db)["digest"] = "e" * 64
    judge, clock = Judge(met("REQ-1", "REQ-2")), Clock()
    minutes(db, judge, clock, 10)
    assert judge.asked == []
    assert acceptance(db)["owner_reason"] == cr.OWNER_CONTRACT_SUPERSEDED


def test_a_judge_that_raises_is_a_try_without_a_verdict(db):
    seed_pending(db)

    class Broken(Judge):
        def __call__(self, task, contract, acc):
            super().__call__(task, contract, acc)
            raise RuntimeError("boom")

    judge, clock = Broken(), Clock()
    minutes(db, judge, clock, 60)
    assert len(judge.asked) == 3
    assert acceptance(db)["reask"]["last"]["reason"] == "judge_error"
    assert evidence(db) == []


def test_the_forge_judge_asks_nothing_for_a_workspace_that_sends_no_code(db):
    """JC2 sends code, so it stays on the signed `code_intelligence.jev`."""
    contract = seed_pending(db)
    ctx = SimpleNamespace(db=db, settings=settings(), now=lambda: T0, store=None,
                          forge=None, forge_tokens=None)
    result = cr.ForgeJudge(ctx)(db.docs[f"tasks/{TASK}"], contract, acceptance(db))
    assert result.outcome == cr.OUTCOME_NOT_RUN and result.reason == "not_opted_in"


# --- the re-ask's Jev calls are counted and capped (owner, 4 November 2026) ---


def _cap(db, cap: int | None) -> None:
    db.docs["workspace_settings/eng"] = {
        "tenant_id": "eng", "code_intelligence": {"jev": True, "jev_monthly_cap_tokens": cap}}


def _ledger(db) -> jevaccount.ReaskLedger:
    return jevaccount.ReaskLedger(db, settings(), lambda: T0, tenant_id="eng", task_id=TASK,
                                  generation=1, attempt_id="att_1")


def _month(db) -> dict[str, Any]:
    return db.docs.get(f"{jevusage.COLLECTION}/eng_{T0:%Y-%m}") or {}


def _ask(db, transport) -> Any:
    from swarm_jev import JudgeClient

    contract = seed_pending(db)
    state = cj.judgment_state(contract, commit=COMMIT, changed=[
        {"path": "src/login/form.py", "status": "modified", "hunk": "+ok"}], mechanical=[])
    backend = SimpleNamespace(name="internal-jev", url="http://jev.internal/decide",
                              model="jev-small", stringify_state=True, key=None)
    ledger = _ledger(db)
    decision = JudgeClient(backend=backend, ledger=ledger, transport=transport,
                           sleep=lambda _s: None).decide(
        cr.JUDGMENT_KIND, state, cj.judgment_questions(state))
    return decision, ledger


def test_a_reask_call_is_counted_in_the_months_jev_usage(db):
    _cap(db, None)
    _month_before = 1_000
    db.docs[f"{jevusage.COLLECTION}/eng_{T0:%Y-%m}"] = {"tenant_id": "eng",
                                                        "input_tokens": _month_before}

    def transport(url, body, headers, timeout):
        return 200, json.dumps({"answers": {}, "usage": {"input_tokens": 4_000}}).encode()

    decision, ledger = _ask(db, transport)
    assert ledger.refused is None and len(ledger.rows) == 1
    row = db.docs[f"{jevusage.COLLECTION}/{jevaccount.reask_row_id('eng', TASK, 1)}"]
    assert row["kind"] == jevaccount.ATTEMPT_KIND and row["tenant_id"] == "eng"
    assert row["calls"] == 1 and row["input_tokens"] == 4_000
    assert row["by_kind"][cr.JUDGMENT_KIND]["calls"] == 1
    assert _month(db)["input_tokens"] == _month_before + 4_000
    # Its own row: the worker's attempt row, which the task's limits read, is untouched.
    assert f"{jevusage.COLLECTION}/{jevaccount.row_id('eng', TASK, 1)}" not in db.docs
    assert jevusage.KIND_GROUPS[cr.JUDGMENT_KIND] == "control_plane_reask"


def test_a_recorded_call_is_charged_once(db):
    _cap(db, None)
    ledger = _ledger(db)
    call = {"call_id": "abc123", "kind": cr.JUDGMENT_KIND, "input_tokens": 3_000,
            "outcome": "ok"}
    ledger.record(dict(call))
    _ledger(db).record(dict(call))
    row = db.docs[f"{jevusage.COLLECTION}/{jevaccount.reask_row_id('eng', TASK, 1)}"]
    assert row["calls"] == 1 and _month(db)["input_tokens"] == 3_000


def test_the_monthly_cap_refuses_a_reask_call_and_nothing_is_sent(db):
    _cap(db, 10_000)
    db.docs[f"{jevusage.COLLECTION}/eng_{T0:%Y-%m}"] = {"tenant_id": "eng",
                                                        "input_tokens": 9_000}
    sent: list[bytes] = []

    def transport(url, body, headers, timeout):
        sent.append(body)
        return 200, b"{}"

    decision, ledger = _ask(db, transport)
    assert sent == [] and decision.outcome == "skipped"
    assert decision.reason == ledger.refused == "budget_monthly"
    row = db.docs[f"{jevusage.COLLECTION}/{jevaccount.reask_row_id('eng', TASK, 1)}"]
    assert row["calls"] == 0 and row["input_tokens"] == 0
    assert row["by_kind"][cr.JUDGMENT_KIND]["outcomes"] == {"skipped:budget_monthly": 1}
    assert _month(db)["input_tokens"] == 9_000


def test_the_cap_leaves_the_worker_attempts_reservations_alone(db):
    _cap(db, 100_000)
    db.docs[f"{jevusage.COLLECTION}/eng_reservations"] = {
        "kind": jevaccount.RESERVATIONS_KIND, "tenant_id": "eng",
        "held": {"task_other_g1": {"task_id": "task_other", "generation": 1,
                                   "remaining_tokens": 99_000}}}
    assert _ledger(db).admit(estimate=10) == "budget_monthly"
    del db.docs[f"{jevusage.COLLECTION}/eng_reservations"]
    assert _ledger(db).admit(estimate=10) is None


def test_a_ledger_admits_one_call(db):
    _cap(db, None)
    ledger = _ledger(db)
    assert ledger.admit(estimate=10) is None
    ledger.record({"call_id": "c1", "kind": cr.JUDGMENT_KIND, "input_tokens": 3_000,
                   "outcome": "ok"})
    assert ledger.admit(estimate=10) == "budget_calls"
