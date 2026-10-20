"""The `model-prices-check` schedule type: the nightly observed-rate check (#1481).

WHAT IS HELD HERE
-----------------
* THE COMPARISON. Per model, the finished attempts' REPORTED cost is set
  against what their reported token counts cost at the price setting. Within
  10% passes; beyond it, in either direction, is ONE finding per model,
  however many attempts it has.
* WHAT IS COUNTED: a finished attempt, in the window, that names its model and
  reported its cost and its four token counts. An attempt whose cost the
  platform ESTIMATED from the setting (`cost_estimated`) is not, because it
  would agree with the setting by construction. A model with fewer than
  `MIN_ATTEMPTS` is not judged, and a model the setting does not price is
  listed, never compared against an invented figure.
* THE ISSUE: one per disagreeing model, in the registered repository the
  schedule names, with the owning tenant's credential, found again by its
  marker so a nightly run does not open it twice. Aggregates only: no tenant,
  task or person is named.
* A DRY RUN reads and writes nothing.

No cloud and no emulator: the in-memory Firestore, GitHub as a fake transport
under the real client.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from swarm_common.models import Attempt

from swarm_api import modelprices, schedulefire, scheduletypes, schedules
from swarm_api.schedtypes import model_prices_check as check

from .conftest import seed_task, seed_tenant
from .test_schedtypes_issue_sweep import (  # noqa: F401 -- fixtures
    NOW,
    REPOSITORY,
    ctx,
    firing_for,
    github,
    make_schedule,
    register,
    writes,
)

OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5"
HAIKU = "claude-haiku-4-5"
PRICES = {name: dict(modelprices.SEED[name]) for name in (OPUS, SONNET, HAIKU)}
SINCE, UNTIL = NOW - timedelta(hours=24), NOW

#: One attempt's usage: 1M input, 100k output, 2M cache reads, 200k cache writes.
USAGE = {"input_tokens": 1_000_000, "output_tokens": 100_000,
         "cache_read_input_tokens": 2_000_000, "cache_creation_input_tokens": 200_000}


def at_setting(model: str) -> float:
    """What `USAGE` costs at the setting's price for `model`."""
    return modelprices.estimate_cost({model: USAGE}, PRICES)


def make_attempt(attempt_id: str, model: str | None, cost: float | None, *, tenant_id: str = "eng",
                 hours_ago: float = 2, finished: bool = True, **counts: Any) -> Attempt:
    created = NOW - timedelta(hours=hours_ago)
    return Attempt(
        attempt_id=attempt_id, task_id=f"t_{attempt_id}", tenant_id=tenant_id, generation=1,
        lease_id=f"l_{attempt_id}", backend="CLOUD_RUN_JOB", created_at=created,
        completed_at=created + timedelta(minutes=30) if finished else None,
        cost_usd=cost, model=model, **{**USAGE, **counts},
    )


def attempts_at(model: str, ratio: float, n: int = 3, **kwargs: Any) -> list[Attempt]:
    """`n` finished attempts of `model` whose reported cost is `ratio` x the setting's."""
    return [make_attempt(f"{model}-{i}", model, round(at_setting(model) * ratio, 6), **kwargs)
            for i in range(n)]


def judge(attempts: list[Attempt], tasks: dict | None = None,
          prices: dict | None = None) -> tuple[dict[str, dict], list[dict]]:
    rows = check.observe(attempts, tasks or {}, prices or PRICES, since=SINCE, until=UNTIL)
    return rows, check.compare(rows)


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


def test_within_ten_percent_passes() -> None:
    rows, findings = judge(attempts_at(OPUS, 1.09) + attempts_at(SONNET, 0.92) + attempts_at(HAIKU, 1.0))

    assert findings == []
    assert {name: row["status"] for name, row in rows.items()} == {
        OPUS: check.AGREES, SONNET: check.AGREES, HAIKU: check.AGREES}
    assert abs(rows[OPUS]["deviation"] - 0.09) < 1e-6


def test_beyond_ten_percent_is_one_finding_per_model() -> None:
    # Opus 25% above, Sonnet 30% below, Haiku in line: two models, two findings,
    # however many attempts each has.
    rows, findings = judge(attempts_at(OPUS, 1.25, n=7) + attempts_at(SONNET, 0.70, n=4)
                           + attempts_at(HAIKU, 1.02))

    assert sorted(f["model"] for f in findings) == [OPUS, SONNET]
    by_model = {f["model"]: f for f in findings}
    assert abs(by_model[OPUS]["deviation"] - 0.25) < 1e-6 and by_model[OPUS]["attempts"] == 7
    assert abs(by_model[SONNET]["deviation"] + 0.30) < 1e-6
    assert rows[HAIKU]["status"] == check.AGREES
    # The rates are per million tokens, observed against the setting's.
    opus = by_model[OPUS]
    assert abs(opus["observed_usd_per_mtok"] / opus["setting_usd_per_mtok"] - 1.25) < 1e-6


def test_the_rates_are_per_model_not_per_attempt_mix() -> None:
    # Two Opus attempts far off in opposite directions average out to the setting.
    rows, findings = judge([make_attempt("a", OPUS, at_setting(OPUS) * 1.5),
                            make_attempt("b", OPUS, at_setting(OPUS) * 0.5),
                            make_attempt("c", OPUS, at_setting(OPUS))])
    assert findings == [] and rows[OPUS]["attempts"] == 3


def test_a_dated_model_id_is_judged_against_its_priced_name() -> None:
    _rows, findings = judge(attempts_at("claude-haiku-4-5-20251001", 1.5))
    assert [f["model"] for f in findings] == [HAIKU]


def test_too_few_attempts_are_not_judged() -> None:
    rows, findings = judge(attempts_at(OPUS, 2.0, n=check.MIN_ATTEMPTS - 1))
    assert findings == [] and rows[OPUS]["status"] == check.TOO_FEW


def test_a_model_without_a_price_is_listed_never_compared() -> None:
    rows, findings = judge(attempts_at(OPUS, 1.0) + [
        make_attempt(f"x{i}", "claude-sonnet-5-5", 3.0) for i in range(3)])
    assert findings == []
    assert rows["claude-sonnet-5-5"]["status"] == check.UNPRICED
    assert rows["claude-sonnet-5-5"]["setting_usd"] is None


def test_only_finished_reported_attempts_of_a_named_model_in_the_window_count() -> None:
    estimated = attempts_at(SONNET, 3.0)
    tasks = {a.task_id: SimpleNamespace(result_summary={"cost_estimated": True}) for a in estimated}
    rows, findings = judge(
        attempts_at(OPUS, 1.0)
        # Each of these is far off, and none may count:
        + [make_attempt("running", OPUS, 99.0, finished=False),
           make_attempt("no-cost", OPUS, None),
           make_attempt("no-model", None, 99.0),
           make_attempt("no-counts", OPUS, 99.0, output_tokens=None),
           make_attempt("too-old", OPUS, 99.0, hours_ago=30)]
        + estimated,
        tasks=tasks,
    )
    assert findings == []
    assert rows[OPUS]["attempts"] == 3
    # The estimated ones agree with the setting by construction: not counted.
    assert SONNET not in rows


# ---------------------------------------------------------------------------
# The firing
# ---------------------------------------------------------------------------


def schedule(db, **params: Any) -> dict[str, Any]:
    return make_schedule(db, type_="model-prices-check", scope_mode="platform",
                         params={"issue_repository": REPOSITORY, **params},
                         cron="30 2 * * *", max_concurrent=2)


def seed(db, attempts: list[Attempt]) -> None:
    for tenant_id in sorted({a.tenant_id for a in attempts}):
        seed_tenant(db, tenant_id)
    for a in attempts:
        seed_task(db, task_id=a.task_id, tenant_id=a.tenant_id, runner_profile="claude-code",
                  state="SUCCEEDED", created_at=a.created_at)
        doc = {k: v for k, v in vars(a).items() if v is not None}
        db.docs[f"attempts/{a.attempt_id}"] = doc


def test_the_type_is_available_because_its_file_exists() -> None:
    entry = scheduletypes.get("model-prices-check")
    assert scheduletypes.availability(entry) == (True, "")
    assert schedulefire.load_executor(entry) is check
    assert sorted(entry.creators) == ["platform"] and entry.profile is None
    assert schedules.resolve_budget(entry, None)["per_run_usd"] > 0


def test_a_firing_opens_one_issue_per_disagreeing_model_and_never_twice(db, ctx, writes) -> None:
    register(db)
    seed(db, attempts_at(OPUS, 1.25) + attempts_at(SONNET, 1.0, tenant_id="research")
         + attempts_at(HAIKU, 0.5, tenant_id="research"))
    ctx.store.set_platform_settings({"model_prices": PRICES}, by="admin@saga.xyz")

    work = check.create(firing_for(ctx, schedule(db), room=2))

    outcomes = {w["id"]: w["outcome"] for w in work}
    assert outcomes == {check.REPORT: "succeeded", check.ISSUES: "succeeded"}
    report = {w["id"]: w["detail"] for w in work}[check.REPORT]
    assert sorted(f["model"] for f in report["findings"]) == [HAIKU, OPUS]
    opened = sorted(writes.issues.values(), key=lambda i: i["number"])
    assert len(opened) == 2
    assert {i["body"].splitlines()[0] for i in opened} == {
        check.marker(OPUS, PRICES[OPUS]), check.marker(HAIKU, PRICES[HAIKU])}
    for issue in opened:
        # Aggregates only: no tenant, task or attempt is named.
        for word in ("eng", "research", "t_claude", "claude-opus-5-5-0"):
            assert word not in issue["body"] and word not in issue["title"]
        assert issue["labels"] == [{"name": "bug"}]
    assert "25% above" in next(i["title"] for i in opened if OPUS in i["title"])

    # The next night, the same disagreement: found by its marker, not opened again.
    again = check.create(firing_for(ctx, schedule(db), room=2, fid="next"))
    assert len(writes.issues) == 2
    issues = {w["id"]: w["detail"] for w in again}[check.ISSUES]["models"]
    assert {row["already_open"] for row in issues.values()} == {True}


def test_no_disagreement_opens_nothing(db, ctx, writes) -> None:
    register(db)
    seed(db, attempts_at(OPUS, 1.05))

    work = check.create(firing_for(ctx, schedule(db), room=2))

    assert [(w["id"], w["outcome"]) for w in work] == [(check.REPORT, "succeeded")]
    assert writes.calls == []


def test_an_unregistered_repository_is_not_written_and_the_firing_says_so(db, ctx, writes) -> None:
    seed(db, attempts_at(OPUS, 2.0))

    work = check.create(firing_for(ctx, schedule(db, issue_repository="someone/else"), room=2))

    issues = {w["id"]: w for w in work}[check.ISSUES]
    assert issues["outcome"] == "failed" and issues["detail"]["code"] == check.REPOSITORY_NOT_REGISTERED
    assert writes.calls == []


def test_without_an_issue_repository_a_finding_fails_the_firing(db, ctx, writes) -> None:
    seed(db, attempts_at(OPUS, 2.0))
    no_repo = make_schedule(db, type_="model-prices-check", scope_mode="platform", max_concurrent=2)

    work = check.create(firing_for(ctx, no_repo, room=2))

    issues = {w["id"]: w for w in work}[check.ISSUES]
    assert issues["outcome"] == "failed" and issues["detail"]["code"] == check.NO_ISSUE_REPOSITORY
    assert writes.calls == []


def test_a_refused_issue_is_a_failed_item(db, ctx, writes) -> None:
    register(db)
    seed(db, attempts_at(OPUS, 2.0))
    writes.status = {"POST": 403}

    work = check.create(firing_for(ctx, schedule(db), room=2))

    issues = {w["id"]: w for w in work}[check.ISSUES]
    assert issues["outcome"] == "failed"
    assert issues["detail"]["models"][OPUS]["code"] == "writeback_forbidden"


def test_a_dry_run_writes_nothing(db, ctx, writes) -> None:
    register(db)
    seed(db, attempts_at(OPUS, 2.0))

    out = check.dry_run(firing_for(ctx, schedule(db), room=2))

    assert [f["model"] for f in out["report"]["findings"]] == [OPUS]
    assert out["issues"] == {"repository": REPOSITORY, "would": "open_or_find", "models": [OPUS]}
    assert writes.calls == []
