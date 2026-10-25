"""ACCOUNT-429-SCOPE's follow-ups, in the quota broker (part of #2302).

BGD answered three questions on 2026-10-25, and each is pinned here:

  1. ASSIGNMENT HONOURS `limited_until`. ACCOUNT-429-SCOPE (PR 2305) stopped
     an account's 429 halving the tenant and recorded it on the account's
     health record, but `choose()` still looked only at the window readings,
     so a 429 the readings did not explain landed the next agent on the same
     account. The report's transaction now mirrors the record's
     `limited_until` onto the account document, and `choose()` (assign and
     swap) skips the account until it passes, or a reading of the refusing
     window shows that window reset. A far-future value from a bad report is
     bounded by the window it names, dated for the parked tasks, and an
     operator's resume clears it.
  2. THE BROKER REDACTS HEALTH MESSAGES ON WRITE: a credential-shaped run in a
     message is masked before it is stored, and the broker's image carries
     swarm-redaction.
  3. THE BROKER'S HARD-MAX ROUTE AUDITS WHO AND WHY: `quota_audit` gets an
     entry with `requested_by` and `reason`, and swarm-api forwards the
     admin's verified email from the token, never from the body.

No credentials, no network: the broker runs over the shared FakeFirestore.
"""

from __future__ import annotations

import secrets
import subprocess
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from quota_broker import accountstore
from quota_broker.accounts import Account, AccountState, WindowReading, choose, eligibility
from quota_broker.accountstore import AccountStore
from quota_broker.main import create_app
from quota_broker.service import QUOTA_AUDIT, QuotaBroker
from quota_broker.settings import BrokerSettings

from .conftest import core_settings
from .fakes import FakeFirestore

REPO = Path(__file__).resolve().parents[3]
ENG = "eng"
PROVIDER = "anthropic"
FIRST = f"{ENG}:first"
SECOND = f"{ENG}:second"
HARD_MAX = 40
T0 = datetime(2026, 10, 25, 4, 40, 57, tzinfo=timezone.utc)


def _token() -> str:
    """A credential-shaped value, built at runtime (CLAUDE.md: no literal)."""
    return "-".join(("sk", "ant", "oat01", secrets.token_hex(24)))


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _Identity:
    def __init__(self) -> None:
        self.tenant: str | None = ENG
        self.platform = False

    def resolve(self, authorization):  # noqa: ANN001 - the broker's duck type
        return (None, True) if self.platform else (self.tenant, False)

    def verified_email(self, authorization):  # noqa: ANN001
        return "swarm-api@example.iam.gserviceaccount.com"


def _windows(now: datetime, used: float = 0.2) -> dict[str, WindowReading]:
    return {
        "five_hour": WindowReading(used, now + timedelta(hours=3)),
        "seven_day": WindowReading(used, now + timedelta(days=3)),
    }


def _account(label: str, now: datetime, **fields: Any) -> Account:
    fields.setdefault("windows", _windows(now))
    return Account(
        account_id=f"{ENG}:{label}",
        owner_tenant=ENG,
        label=label,
        observed_at=now,
        **fields,
    )


@pytest.fixture()
def db() -> FakeFirestore:
    return FakeFirestore()


@pytest.fixture()
def store(db) -> AccountStore:
    s = AccountStore(db)
    s.register(ENG, "first")
    s.register(ENG, "second")
    for account_id in (FIRST, SECOND):
        s.record_reading(account_id, _windows(_now()), _now())
    return s


@pytest.fixture()
def broker(db) -> QuotaBroker:
    return QuotaBroker(
        db, settings=BrokerSettings(core=core_settings(), default_hard_max=HARD_MAX)
    )


@pytest.fixture()
def client(broker, store):
    identity = _Identity()
    c = TestClient(
        create_app(broker, identity=identity, account_store=store),
        raise_server_exceptions=False,
    )
    c.identity = identity
    return c


def _hold(client, task: str) -> dict[str, Any]:
    r = client.post(
        "/v1/accounts/assign",
        json={"provider": PROVIDER, "task_id": task, "attempt_id": f"att-{task}"},
    )
    assert r.status_code == 200 and r.json()["account_id"], r.text
    return {
        "account_id": r.json()["account_id"],
        "assignment_id": r.json()["assignment_id"],
        "task_id": task,
        "attempt_id": f"att-{task}",
    }


def _rate_limit(client, account: dict[str, Any], **body: Any):
    return client.post(f"/v1/quota/{PROVIDER}/{ENG}/rate-limit",
                       json={"account": account, **body})


# -- 1. choose() honours limited_until -------------------------------------------


def test_an_account_whose_recorded_429_has_not_reset_is_not_chosen_and_is_once_it_passes():
    until = T0 + timedelta(minutes=10)
    # `first` has MORE headroom, so without the hold it is the one chosen.
    limited = _account("first", T0, limited_until=until, limited_window="other")
    other = _account("second", T0, windows=_windows(T0, used=0.5))

    assert choose([limited, other], ENG, T0).account_id == SECOND
    assert choose([limited], ENG, T0) is None, "the hold refuses it as a window would"
    assert choose([limited, other], ENG, until + timedelta(seconds=1)).account_id == FIRST, (
        "the time passing clears it"
    )


def test_a_pool_held_only_by_429s_is_dated_by_their_reset():
    """Parked tasks wake when the hold ends, not on some other clock or never."""
    until = T0 + timedelta(minutes=10)
    accounts = [_account("first", T0, limited_until=until, limited_window="other")]

    reason, when = eligibility(accounts, ENG, T0)

    assert when == until, reason


def test_the_report_transaction_writes_limited_until_on_the_account(client, store, db):
    held = _hold(client, "task_1")
    before = _now()

    r = _rate_limit(client, held, retry_after_seconds=600)

    assert r.status_code == 200 and r.json()["scope"] == "account", r.text
    raw = db.docs[f"accounts/{held['account_id']}"]
    assert isinstance(raw.get("limited_until"), datetime), raw.keys()
    assert raw["limited_until"] == store.health(held["account_id"])["limited_until"], (
        "the account mirrors the health record's value"
    )
    assert before + timedelta(seconds=590) <= raw["limited_until"] <= _now() + timedelta(seconds=610)
    assert store.get(held["account_id"]).is_limited(_now())


def test_assign_skips_the_account_after_its_429(client):
    held = _hold(client, "task_1")
    assert _rate_limit(client, held, retry_after_seconds=600).json()["scope"] == "account"

    for task in ("task_2", "task_3", "task_4"):
        assert _hold(client, task)["account_id"] != held["account_id"], (
            "a 429 the readings do not explain must not land the next agent on the account"
        )


def test_a_reading_of_the_refusing_window_after_it_reset_clears_the_hold(client, store):
    held = _hold(client, "task_1")
    account = store.get(held["account_id"])
    five = account.windows["five_hour"].resets_at
    r = _rate_limit(client, held, reset_at=five.isoformat())
    assert r.status_code == 200, r.text
    limited = store.get(held["account_id"])
    assert limited.limited_window == "five_hour" and limited.is_limited(_now())

    # The same window instance, read again: still held.
    store.record_reading(held["account_id"], {"five_hour": WindowReading(0.3, five)}, _now())
    assert store.get(held["account_id"]).is_limited(_now())

    # The window rolled over: its reset is the provider's word that it reset.
    store.record_reading(
        held["account_id"],
        {"five_hour": WindowReading(0.01, five + timedelta(hours=5))},
        _now() + timedelta(seconds=1),
    )
    cleared = store.get(held["account_id"])
    assert cleared.limited_until is None and not cleared.is_limited(_now())
    assert store.health(held["account_id"]).get("limited_until") is None, (
        "the record agrees, so the next report does not resurrect the old hold"
    )


def test_a_bad_far_future_report_on_a_five_hour_window_holds_at_most_five_hours(client, store):
    held = _hold(client, "task_1")
    account = store.get(held["account_id"])
    # Readings block the five-hour window, so it is the one the report names.
    store.record_reading(
        held["account_id"],
        {"five_hour": WindowReading(0.99, account.windows["five_hour"].resets_at)},
        _now(),
    )
    far = _now() + timedelta(days=30)

    assert _rate_limit(client, held, reset_at=far.isoformat()).status_code == 200

    limited = store.get(held["account_id"])
    assert limited.limited_window == "five_hour"
    assert limited.limited_until <= _now() + timedelta(hours=5)


def test_an_operator_resume_clears_the_hold(client, store):
    held = _hold(client, "task_1")
    _rate_limit(client, held, reset_at=(_now() + timedelta(days=6)).isoformat())
    assert store.get(held["account_id"]).is_limited(_now())

    store.set_state(held["account_id"], AccountState.AVAILABLE, "resumed by an operator")

    assert not store.get(held["account_id"]).is_limited(_now())
    assert store.health(held["account_id"]).get("limited_until") is None


def test_the_listing_says_the_429_hold_refuses_it(client, store):
    held = _hold(client, "task_1")
    _rate_limit(client, held, retry_after_seconds=600)
    client.identity.platform = True

    rows = {a["account_id"]: a for a in client.get("/v1/accounts").json()["accounts"]}

    row = rows[held["account_id"]]
    assert row["can_take_new_agent"] is False
    assert any("limited_until" in b for b in row["blocked_by"]), row["blocked_by"]


# -- 2. the broker redacts health messages on write ------------------------------


def test_a_health_message_holding_a_credential_is_stored_redacted(store, db):
    token = _token()

    assert store.record_health(FIRST, "auth", message=f"refused with Authorization: Bearer {token}")

    stored = db.docs[f"accounts/{FIRST}/health/record"]["events"][0]["message"]
    assert token not in stored
    assert token[:20] not in stored, "masked before the cap, so no prefix survives either"
    assert stored, "a redacted message is still a message"


def test_a_credential_cut_by_the_cap_is_still_redacted(store, db):
    token = _token()
    padding = "x" * (accountstore.HEALTH_MESSAGE_MAX - 30)

    store.record_health(FIRST, "auth", message=f"{padding} token={token}")

    stored = db.docs[f"accounts/{FIRST}/health/record"]["events"][0]["message"]
    assert token[:12] not in stored


def test_the_broker_depends_on_swarm_redaction_and_its_image_ships_it():
    project = tomllib.loads((REPO / "apps/quota-broker/pyproject.toml").read_text())
    assert "swarm-redaction" in project["project"]["dependencies"]
    lock = {p["name"]: p for p in tomllib.loads((REPO / "uv.lock").read_text())["package"]}
    assert {"name": "swarm-redaction"} in lock["swarm-quota-broker"]["dependencies"]
    dockerfile = (REPO / "images/swarm-quota-broker/Dockerfile").read_text()
    assert "COPY apps/redaction/ /src/apps/redaction/" in dockerfile
    assert "--prune swarm-redaction" not in dockerfile


def test_the_workspace_apply_render_path_imports_without_swarm_redaction():
    """`kubernetes/render.py` puts apps/common, apps/quota-broker and
    apps/scheduler on its path and nothing else (the workspace-apply image
    copies exactly those), then imports `scheduler.dispatch`, which reaches
    `quota_broker.accountstore`. The broker's redaction must not be a
    module-level import on that path."""
    script = (
        "import builtins, sys\n"
        f"sys.path[:0] = [{str(REPO / 'apps/common')!r}, {str(REPO / 'apps/quota-broker')!r},"
        f" {str(REPO / 'apps/scheduler')!r}]\n"
        "real = builtins.__import__\n"
        "def guard(name, *a, **k):\n"
        "    if name.split('.')[0] == 'swarm_redaction':\n"
        "        raise ImportError('swarm_redaction is not in the workspace-apply image')\n"
        "    return real(name, *a, **k)\n"
        "builtins.__import__ = guard\n"
        "import scheduler.dispatch, quota_broker.accountstore\n"
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]


# -- 3. the hard-max route audits who and why ------------------------------------


def _hard_max_audits(db: FakeFirestore) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items()
            if p.startswith(f"{QUOTA_AUDIT}/") and d.get("action") == "hard_max_set"]


def test_a_hard_max_change_writes_a_quota_audit_entry_with_who_and_why(client, db):
    client.identity.platform = True

    r = client.put(f"/v1/quota/{PROVIDER}/{ENG}/hard-max",
                   json={"hard_max": 30, "requested_by": "Root@Example.com",
                         "reason": "eng asked for less"})

    assert r.status_code == 200, r.text
    [audit] = _hard_max_audits(db)
    assert audit["requested_by"] == "root@example.com"
    assert audit["reason"] == "eng asked for less"
    assert audit["caller"] == "swarm-api@example.iam.gserviceaccount.com"
    assert audit["from_hard_max"] == HARD_MAX and audit["to_hard_max"] == 30
    assert audit["provider"] == PROVIDER and audit["tenant_id"] == ENG


def test_a_hard_max_change_without_who_or_why_is_still_audited(client, db):
    client.identity.platform = True

    assert client.put(f"/v1/quota/{PROVIDER}/{ENG}/hard-max", json={"hard_max": 30}).status_code == 200

    [audit] = _hard_max_audits(db)
    assert audit["requested_by"] is None and audit["reason"] is None
