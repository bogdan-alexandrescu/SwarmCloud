"""Jev's usage and cost, per workspace and day (JEV-USAGE-PAGE, part of #2688).

The owner chose on 2 November 2026 to build a Jev usage and cost page. These
tests hold the read behind it (`swarm_api.jevusage`, `GET /v1/jev-usage`) and
its rate setting (`jev_token_rates`):

  * a fixture of `jev_usage` attempt rows is summed per UTC day and kind, and
    the 30-day total is the days' sum;
  * a skipped call shows its reason;
  * dollars are null, never 0, while no rate is set, and priced once it is;
  * another workspace's rows never appear, even one whose id the query
    could be fooled by, and another workspace's read is a 404;
  * a row's task id, generation and any other field never reach the answer;
  * the month's use against `code_intelligence.jev_monthly_cap_tokens`.

MUTATION: drop the `tenant_id` check in `summarise` and
`test_a_row_filed_under_another_tenant_is_dropped_in_code` fails; drop it
there and the query's `tenant_id` filter too and
`test_another_workspaces_rows_never_appear` fails; make `Tally.usd` return 0
without a rate and `test_dollars_are_blank_not_zero_without_a_rate` fails;
key `summarise`'s days by month and the per-day test fails.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import TENANT_HEADER, StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.jevusage import (
    COLLECTION,
    UNATTRIBUTED,
    UsageRow,
    rate_problem,
    row_day,
    summarise,
)
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import NoForgeTokens, api_settings, auth_header, seed_tenant
from .fakes import FakeFirestore

NOW = datetime(2026, 11, 2, 9, 30, tzinfo=timezone.utc)


def row(tenant: str, task: str, gen: int, *, settled_at: datetime | None,
        by_kind: dict | None = None, calls: int = 0, input_tokens: int = 0,
        month: str = "2026-11", **extra) -> tuple[str, dict]:
    data = {"kind": "attempt", "tenant_id": tenant, "task_id": task, "generation": gen,
            "month": month, "calls": calls, "input_tokens": input_tokens, **extra}
    if settled_at is not None:
        data["settled_at"] = settled_at
    if by_kind is not None:
        data["by_kind"] = by_kind
    return f"{COLLECTION}/{tenant}_{task}_g{gen}", data


def seed_rows(db: FakeFirestore, *rows: tuple[str, dict]) -> None:
    for path, data in rows:
        db.docs[path] = data


@pytest.fixture
def client(db, tokens, group_map, objects) -> TestClient:
    ctx = build_context(
        settings=api_settings(), db=db, verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map), credentials=InMemoryCredentials(),
        waker=NullWaker(), metrics=ApiMetrics(), objects=objects,
        forge_tokens=NoForgeTokens(), now=lambda: NOW,
    )
    return TestClient(create_app(ctx), raise_server_exceptions=False)


@pytest.fixture
def seeded(db: FakeFirestore) -> FakeFirestore:
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    nov2 = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    nov1 = datetime(2026, 11, 1, 23, 59, tzinfo=timezone.utc)
    oct20 = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
    seed_rows(
        db,
        row("eng", "task_a", 1, settled_at=nov2, calls=3, input_tokens=9000, by_kind={
            "J1": {"calls": 1, "input_tokens": 3000, "outcomes": {"ok": 1}},
            "J4": {"calls": 2, "input_tokens": 6000,
                   "outcomes": {"ok": 1, "failed": 1, "skipped:budget_calls": 1}},
        }),
        row("eng", "task_b", 2, settled_at=nov2, calls=1, input_tokens=2000, by_kind={
            "J1": {"calls": 1, "input_tokens": 2000, "output_tokens": 150,
                   "outcomes": {"ok": 1, "skipped:oversize": 2}},
        }),
        row("eng", "task_c", 1, settled_at=nov1, calls=1, input_tokens=2500, by_kind={
            "agent": {"calls": 1, "input_tokens": 2500, "outcomes": {"ok": 1}},
        }),
        row("eng", "task_d", 1, settled_at=oct20, month="2026-10", calls=2, input_tokens=4000),
        # another workspace's rows, one carrying eng's task id
        row("research", "task_a", 1, settled_at=nov2, calls=7, input_tokens=70_000, by_kind={
            "J1": {"calls": 7, "input_tokens": 70_000, "outcomes": {"ok": 7}},
        }),
    )
    db.docs[f"{COLLECTION}/eng_2026-11"] = {"tenant_id": "eng", "month": "2026-11",
                                            "input_tokens": 13_500}
    db.docs[f"{COLLECTION}/research_2026-11"] = {"tenant_id": "research", "month": "2026-11",
                                                 "input_tokens": 70_000}
    return db


def read(client: TestClient, who: str = "alice", tenant: str = "eng") -> dict:
    headers = {**auth_header(who), TENANT_HEADER: tenant}
    response = client.get("/v1/jev-usage", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def day_of(body: dict, day: str) -> dict:
    return next(d for d in body["days"] if d["day"] == day)


def kind_of(day: dict, kind: str) -> dict:
    return next(k for k in day["kinds"] if k["kind"] == kind)


def test_the_read_sums_usage_records_per_day_and_kind(client, seeded):
    body = read(client)
    assert [d["day"] for d in body["days"]] == ["2026-11-02", "2026-11-01", "2026-10-20"]
    nov2 = day_of(body, "2026-11-02")
    assert [k["kind"] for k in nov2["kinds"]] == ["J1", "J4"]
    j1 = kind_of(nov2, "J1")
    assert (j1["group"], j1["calls"], j1["input_tokens"]) == ("worker_judgment", 2, 5000)
    assert j1["output_tokens"] == 150
    assert j1["outcomes"]["ok"] == 2
    j4 = kind_of(nov2, "J4")
    assert (j4["group"], j4["calls"], j4["input_tokens"]) == ("quality_gate", 2, 6000)
    assert (j4["outcomes"]["ok"], j4["outcomes"]["failed"]) == (1, 1)
    assert (nov2["calls"], nov2["input_tokens"]) == (4, 11_000)
    nov1 = day_of(body, "2026-11-01")
    assert kind_of(nov1, "agent")["group"] == "decision_tool"
    # a row with no split is counted whole, never guessed apart
    oct20 = day_of(body, "2026-10-20")
    assert [(k["kind"], k["calls"], k["input_tokens"]) for k in oct20["kinds"]] == [
        (UNATTRIBUTED, 2, 4000)]
    assert oct20["kinds"][0]["output_tokens"] is None
    total = body["total"]
    assert (total["calls"], total["input_tokens"]) == (4 + 1 + 2, 11_000 + 2500 + 4000)
    assert {k["kind"]: k["calls"] for k in total["by_kind"]} == {
        "J1": 2, "J4": 2, "agent": 1, UNATTRIBUTED: 2}
    assert body["window"] == {"from": "2026-10-04", "to": "2026-11-02", "days": 30}


def test_skipped_calls_show_their_reason(client, seeded):
    body = read(client)
    nov2 = day_of(body, "2026-11-02")
    assert kind_of(nov2, "J1")["outcomes"]["skipped_by_reason"] == {"oversize": 2}
    assert kind_of(nov2, "J4")["outcomes"]["skipped_by_reason"] == {"budget_calls": 1}
    assert nov2["outcomes"]["skipped"] == 3
    assert body["total"]["outcomes"]["skipped_by_reason"] == {"budget_calls": 1, "oversize": 2}


def test_dollars_are_blank_not_zero_without_a_rate(client, seeded):
    body = read(client)
    assert body["rate"] == {"input_usd_per_million": None, "output_usd_per_million": None,
                            "set": False}
    assert body["total"]["usd"] is None
    assert all(d["usd"] is None for d in body["days"])
    assert all(k["usd"] is None for d in body["days"] for k in d["kinds"])


def test_dollars_are_priced_once_the_rate_is_set(client, seeded):
    response = client.put("/v1/admin/settings", headers=auth_header("root"),
                          json={"jev_token_rates": {"input": 2.0, "output": 10.0}})
    assert response.status_code == 200, response.text
    assert response.json()["jev_token_rates"] == {"input": 2.0, "output": 10.0}
    body = read(client)
    assert body["rate"]["set"] is True
    nov2 = day_of(body, "2026-11-02")
    # 5000 in at $2/M + 150 out at $10/M
    assert kind_of(nov2, "J1")["usd"] == pytest.approx(0.0115)
    assert kind_of(nov2, "J4")["usd"] == pytest.approx(0.012)
    assert body["total"]["usd"] == pytest.approx(17_500 * 2 / 1e6 + 150 * 10 / 1e6)


def test_without_an_output_rate_a_figure_with_output_tokens_is_blank(client, seeded):
    client.put("/v1/admin/settings", headers=auth_header("root"),
               json={"jev_token_rates": {"input": 2.0}})
    nov2 = day_of(read(client), "2026-11-02")
    assert kind_of(nov2, "J1")["usd"] is None
    assert kind_of(nov2, "J4")["usd"] == pytest.approx(0.012)


def test_another_workspaces_rows_never_appear(client, seeded):
    eng = read(client)
    assert eng["tenant_id"] == "eng"
    assert eng["total"]["input_tokens"] == 17_500
    assert eng["month"]["used_tokens"] == 13_500
    research = read(client, "bob", "research")
    assert research["total"]["input_tokens"] == 70_000
    assert [d["day"] for d in research["days"]] == ["2026-11-02"]
    # alice is not in research: its usage is not hers to read, nor to learn exists
    response = client.get("/v1/workspaces/research/jev-usage",
                          headers={**auth_header("alice"), TENANT_HEADER: "eng"})
    assert response.status_code == 404


def test_a_row_filed_under_another_tenant_is_dropped_in_code():
    rows = [UsageRow({"kind": "attempt", "tenant_id": "research", "calls": 5,
                      "settled_at": NOW}),
            UsageRow({"kind": "attempt", "tenant_id": "eng", "calls": 1,
                      "settled_at": NOW})]
    body = summarise("eng", rows, now=NOW, rates={"input": None, "output": None},
                     month_used_tokens=0, cap_tokens=None)
    assert body["total"]["calls"] == 1


def test_no_payload_or_identifier_reaches_the_answer(client, db, seeded):
    marker = secrets.token_hex(12)
    seed_rows(db, row("eng", f"task_{marker}", 1, settled_at=NOW, calls=1, input_tokens=10,
                      state=f"payload-{marker}", by_kind={
                          f"weird kind {marker}": {"calls": 1, "input_tokens": 10,
                                                    "outcomes": {f"skipped:{marker} x": 1}}}))
    response = client.get("/v1/jev-usage", headers={**auth_header("alice"), TENANT_HEADER: "eng"})
    assert response.status_code == 200
    assert marker not in response.text
    assert "task_a" not in response.text
    nov2 = day_of(response.json(), "2026-11-02")
    other = kind_of(nov2, "other")
    assert other["outcomes"]["skipped_by_reason"] == {"other": 1}


def test_the_months_use_against_its_cap(client, db, seeded):
    assert read(client)["month"] == {"month": "2026-11", "used_tokens": 13_500,
                                     "cap_tokens": None, "used_fraction": None}
    db.docs["workspace_settings/eng"] = {"code_intelligence": {"jev_monthly_cap_tokens": 50_000}}
    assert read(client)["month"] == {"month": "2026-11", "used_tokens": 13_500,
                                     "cap_tokens": 50_000, "used_fraction": 0.27}


def test_a_row_with_no_settled_at_counts_on_its_last_write():
    written = datetime(2026, 10, 30, 4, 0, tzinfo=timezone.utc)
    assert str(row_day({}, written)) == "2026-10-30"
    body = summarise("eng", [UsageRow({"kind": "attempt", "tenant_id": "eng", "calls": 2,
                                       "input_tokens": 600}, written),
                             UsageRow({"kind": "attempt", "tenant_id": "eng", "calls": 1,
                                       "input_tokens": 50})],
                     now=NOW, rates={"input": None, "output": None},
                     month_used_tokens=0, cap_tokens=None)
    assert [d["day"] for d in body["days"]] == ["2026-10-30"]
    assert body["undated"]["calls"] == 1


def test_the_rate_setting_has_no_default_and_refuses_a_bad_value(client, seeded):
    settings = client.get("/v1/admin/settings", headers=auth_header("root"))
    assert settings.status_code == 200
    assert settings.json()["jev_token_rates"] == {}
    bad = client.put("/v1/admin/settings", headers=auth_header("root"),
                     json={"jev_token_rates": {"input": -1}})
    assert bad.status_code == 422
    member = client.put("/v1/admin/settings", headers=auth_header("alice"),
                        json={"jev_token_rates": {"input": 1}})
    assert member.status_code == 403
    assert rate_problem({"input": 1, "currency": "EUR"}) is not None
    assert rate_problem({"input": True}) is not None
    cleared = client.put("/v1/admin/settings", headers=auth_header("root"),
                         json={"jev_token_rates": {}})
    assert cleared.status_code == 200
    assert cleared.json()["jev_token_rates"] == {}
