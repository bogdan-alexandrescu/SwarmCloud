"""The workflow-rollup job seals each tenant's ended outcome days (COUNTS-SEAL, #2149).

WHY THIS EXISTS
---------------
BGD's decision, 2026-10-23: Admin › Platform counts reads agents over 12
months from `outcome_days`, "sealed nightly by the existing rollup job". Before
this change nothing sealed a day unless somebody happened to read it, so a cold
12-month view spent its 5,000-read derive budget and came back mostly
`unread: derive_budget`.

The existing `workflow-rollup-<tenant>` Cloud Scheduler job
(terraform/modules/scheduler/jobs.tf) now calls
`POST /v1/admin/workflows/rollup?tenant_id=T&seal_outcomes=true`, as the
rollup-sweeper identity it already presents. No new job, no new route on the
sweeper's allow-list, no new IAM row. What these tests hold:

* a call after a UTC day's 15-minute grace writes that day as `sealed` for the
  tenant it names, and never writes or rewrites TODAY's live day;
* a day still inside its grace is left live, not sealed early;
* a second call changes nothing (the seal is idempotent);
* without `seal_outcomes` the route writes no outcome day (an operator's
  manual workflow sweep is unchanged);
* one call is bounded by the derive budget, and what it did not reach is
  `deferred`, never a zero;
* it logs one count line per tenant.

The fixture is test_outcomes_route.py's week: eng and research, 19-25 Sep 2026.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.outcomes import (
    DERIVE_VERSION,
    OUTCOMES_SEAL_LOOKBACK_DAYS,
    Outcomes,
)
from swarm_api.waker import NullWaker

from .conftest import api_settings, auth_header
from .test_outcomes_route import NOW, WEEK, Clock, at, seed_week, task

UTC = timezone.utc

#: The address jobs.tf's rollup jobs mint their OIDC token as.
SWEEPER = "swarm-rollup-sweeper@saga-agents-staging.iam.gserviceaccount.com"
SWEEPER_HEADERS = {"Authorization": "Bearer token-sweeper"}

#: 26 Sep, five minutes past the grace that seals 25 Sep.
AFTER_GRACE = datetime(2026, 9, 26, 0, 20, tzinfo=UTC)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def ctx(db, tokens, group_map, clock):
    seed_week(db)
    tokens = dict(tokens)
    tokens["token-sweeper"] = {"email": SWEEPER, "email_verified": True, "sub": "sub-sweeper"}
    return build_context(
        settings=api_settings(rollup_sweeper_users=(SWEEPER,)),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=None,
        now=clock,
    )


@pytest.fixture
def api(ctx) -> TestClient:
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def sweep(api: TestClient, tenant: str, **params: Any) -> dict[str, Any]:
    response = api.post(
        "/v1/admin/workflows/rollup",
        params={"tenant_id": tenant, **params},
        headers=SWEEPER_HEADERS,
    )
    assert response.status_code == 200, response.text
    return response.json()


def outcome_days(db) -> dict[str, dict[str, Any]]:
    return {k: deepcopy(v) for k, v in db.docs.items() if k.startswith("outcome_days/")}


def test_the_job_seals_yesterday_for_each_tenant_and_leaves_today_alone(api, db, clock):
    # The live day of 25 Sep, as a reader's GET left it at 11:10.
    response = api.get("/v1/outcomes", params=WEEK, headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    assert db.docs["outcome_days/eng_2026-09-25"]["sealed"] is False

    clock.now = AFTER_GRACE
    # Today's live day as some reader left it: the seal must neither write it
    # nor rewrite it.
    task(db, "s6", state="SUCCEEDED", created=at(26, 0, 1), completed=at(26, 0, 5))
    today_live = {"tenant_id": "eng", "day": "2026-09-26", "sealed": False, "marker": "untouched"}
    db.docs["outcome_days/eng_2026-09-26"] = dict(today_live)

    for tenant in ("eng", "research"):
        body = sweep(api, tenant, seal_outcomes="true")
        # MUTATION: drop the seal call from rollup_workflows -> no `outcomes` key.
        assert body["outcomes"]["tenant_id"] == tenant
        # MUTATION: start the lookback at today instead of yesterday -> today is examined.
        assert body["outcomes"]["report"]["examined"] == OUTCOMES_SEAL_LOOKBACK_DAYS
        assert body["outcomes"]["report"]["unknown"] == 0
        # The workflow sweep still answers as it did.
        assert body["tenant_id"] == tenant and "report" in body and "drifted" in body

    for tenant in ("eng", "research"):
        sealed = db.docs[f"outcome_days/{tenant}_2026-09-25"]
        # MUTATION: write the day with sealed=False -> still live.
        assert sealed["sealed"] is True, tenant
        assert sealed["derive_version"] == DERIVE_VERSION
        # MUTATION: seal from the live cut instead of a full derive -> built_through is 11:08.
        assert sealed["built_through"] == datetime(2026, 9, 26, tzinfo=UTC)
    # A full derive: eng's 25 Sep holds s4, which ended that day.
    assert db.docs["outcome_days/eng_2026-09-25"]["n_ended"] == 1

    # MUTATION: include today in the window -> the live doc is replaced.
    assert db.docs["outcome_days/eng_2026-09-26"] == today_live
    # MUTATION: include today in the window -> research gets a doc for today.
    assert "outcome_days/research_2026-09-26" not in db.docs


def test_a_day_inside_its_grace_is_left_live(api, db, clock):
    api.get("/v1/outcomes", params=WEEK, headers=auth_header("alice"))
    clock.now = datetime(2026, 9, 26, 0, 14, 59, tzinfo=UTC)
    report = sweep(api, "eng", seal_outcomes="true")["outcomes"]["report"]
    # MUTATION: drop the `now < _seal_at(day)` check -> 25 Sep is sealed 1 s early.
    assert db.docs["outcome_days/eng_2026-09-25"]["sealed"] is False
    assert report["not_yet"] == 1
    # The days before it were sealed by the reader's GET at 11:10.
    assert report["sealed"] == 0
    assert report["already_sealed"] == OUTCOMES_SEAL_LOOKBACK_DAYS - 1

    clock.now = datetime(2026, 9, 26, 0, 15, tzinfo=UTC)
    report = sweep(api, "eng", seal_outcomes="true")["outcomes"]["report"]
    assert db.docs["outcome_days/eng_2026-09-25"]["sealed"] is True
    assert report["sealed"] == 1 and report["not_yet"] == 0


def test_a_second_run_changes_nothing(api, db, clock):
    clock.now = AFTER_GRACE
    first = sweep(api, "eng", seal_outcomes="true")["outcomes"]["report"]
    assert first["sealed"] == OUTCOMES_SEAL_LOOKBACK_DAYS
    written = outcome_days(db)
    assert len(written) == OUTCOMES_SEAL_LOOKBACK_DAYS, "a control: the first run wrote the days"

    clock.now = AFTER_GRACE + timedelta(minutes=15)
    second = sweep(api, "eng", seal_outcomes="true")["outcomes"]["report"]
    # MUTATION: re-derive and rewrite a day that is already sealed -> built_at moves.
    assert outcome_days(db) == written
    assert second["sealed"] == 0
    assert second["already_sealed"] == OUTCOMES_SEAL_LOOKBACK_DAYS
    # Only the stored-day lookup: no derive was spent on a sealed day.
    # MUTATION: derive every day on every run -> reads grow past the lookups.
    assert second["reads"] == OUTCOMES_SEAL_LOOKBACK_DAYS


def test_without_seal_outcomes_the_route_writes_no_outcome_day(api, db, clock):
    clock.now = AFTER_GRACE
    body = sweep(api, "eng")
    # MUTATION: seal on every call, flag or not -> days are written.
    assert outcome_days(db) == {}
    assert "outcomes" not in body


def test_an_unknown_tenant_is_not_sealed_and_the_sweep_still_answers(api, db, clock):
    clock.now = AFTER_GRACE
    body = sweep(api, "nope", seal_outcomes="true")
    # MUTATION: drop the tenant check -> a rollup is built for a tenant nobody has.
    assert outcome_days(db) == {}
    assert body["outcomes"] == {"tenant_id": "nope", "skipped": "unknown_tenant"}


def test_one_call_is_bounded_by_the_derive_budget(ctx, db, clock):
    clock.now = AFTER_GRACE
    outcomes = Outcomes(store=ctx.store, rollups=ctx.rollups, now=clock, derive_read_budget=1)
    report = outcomes.seal_recent(tenant_id="eng")["report"]
    # The newest day is derived (the budget is checked before each derive);
    # the rest wait for the next run, and are never written as zeros.
    # MUTATION: drop the budget check -> every day is sealed in one call.
    assert report["sealed"] == 1
    assert report["deferred"] == OUTCOMES_SEAL_LOOKBACK_DAYS - 1
    assert list(outcome_days(db)) == ["outcome_days/eng_2026-09-25"]

    again = Outcomes(store=ctx.store, rollups=ctx.rollups, now=clock).seal_recent(tenant_id="eng")
    assert again["report"]["sealed"] == OUTCOMES_SEAL_LOOKBACK_DAYS - 1
    assert again["report"]["already_sealed"] == 1


def test_the_lookback_stays_inside_the_backfills_31_day_cap():
    # MUTATION: raise the lookback past 31 -> one call walks more days than the backfill may.
    assert 1 <= OUTCOMES_SEAL_LOOKBACK_DAYS <= 31


def test_it_logs_one_count_line_per_tenant(api, clock, caplog):
    clock.now = AFTER_GRACE
    with caplog.at_level(logging.INFO, logger="swarm_api.outcomes"):
        sweep(api, "eng", seal_outcomes="true")
        sweep(api, "research", seal_outcomes="true")
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("outcome seal ")]
    # MUTATION: drop the log line -> no count per tenant.
    assert len(lines) == 2
    assert lines[0].startswith("outcome seal tenant=eng sealed=")
    assert lines[1].startswith("outcome seal tenant=research sealed=")
    assert NOW < AFTER_GRACE, "a control: the clock moved past the seal"
