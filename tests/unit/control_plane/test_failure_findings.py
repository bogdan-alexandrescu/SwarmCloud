"""A tenant's failure findings, read side (lane FR-4 FINDINGS; part of #1933).

docs/design/failure-review.md §1: the `failure-review` schedule writes a
`failure_findings/{id}` document into the tenant whose OWN repository caused
an agent's failure (`schedtypes/failure_review.py` `Filing.report`, FR-3).
What is held here, against an in-memory Firestore with no credentials:

* a tenant reads only its own findings (invariant 9);
* another tenant's finding id answers exactly as a missing one;
* the bell item names the agent and carries the cause sentence;
* no platform-tenant text reaches a tenant: not the cause's fingerprint, the
  platform's schedule or firing, the investigator's proposed fix, nor the
  digest's references to other failures (`f-002`) and tenants (`t2`).

`alice` is in eng, `bob` is in research (conftest).
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from swarm_api import notifications as notif
from swarm_api.schedtypes import failure_review
from swarm_api.store import Store

from .conftest import auth_header, seed_task, seed_tenant

ALICE = "alice@example.com"
BOB = "bob@example.com"
#: The platform tenant's own identifiers, built at runtime so each run's are
#: unique and a leak of any of them is unmistakable in a response.
FINGERPRINT = "fr-" + secrets.token_hex(8)
SCHEDULE = "sch_" + secrets.token_hex(6)
FIRING = "fir_" + secrets.token_hex(6)
FIX = "Patch platform-internal " + secrets.token_hex(6) + " in the scheduler"
CAUSE = "The repository's test_login fails on a missing fixture, as f-002 in t2 also did"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _tenants(db):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")


def _failed(db, task_id: str, *, tenant_id: str = "eng", by: str = ALICE,
            step_id: str | None = None) -> dict[str, Any]:
    doc = seed_task(db, task_id=task_id, tenant_id=tenant_id, state="FAILED",
                    created_at=_now() - timedelta(hours=1))
    doc["submitted_by"] = by
    doc["completed_at"] = doc["updated_at"] = _now() - timedelta(minutes=30)
    doc["step_id"] = step_id
    return doc


def _finding(db, task_id: str, *, tenant_id: str = "eng", state: str = "open",
             cause: str = CAUSE, created_at: datetime | None = None) -> str:
    """A finding exactly as `Filing.report` writes one."""
    fid = failure_review.finding_id(FINGERPRINT, tenant_id, task_id)
    db.docs[f"{failure_review.FINDINGS}/{fid}"] = {
        "finding_id": fid, "tenant_id": tenant_id, "kind": "agent", "task_id": task_id,
        "failure_id": task_id, "fingerprint": FINGERPRINT, "cause": cause, "fix": FIX,
        "evidence": {"kind": "agent", "state": "FAILED", "profile": "claude-code",
                     "end_cause": "agent_failed", "last_error": "pytest exited 1"},
        "schedule_id": SCHEDULE, "firing_id": FIRING, "state": state,
        "created_at": created_at or _now() - timedelta(minutes=10),
    }
    return fid


def _get(client, path: str, user: str = "alice"):
    return client.get(path, headers=auth_header(user))


def _platform_text(body: Any) -> list[str]:
    text = json.dumps(body, default=str)
    found = [t for t in (FINGERPRINT, SCHEDULE, FIRING, FIX, "firing_id", "schedule_id",
                         "fingerprint", '"fix"') if t in text]
    return found + re.findall(r"\b(?:f-002|t2)\b", text)


# -- a tenant reads only its own ------------------------------------------------

def test_a_tenant_lists_only_its_own_findings(client, db):
    _failed(db, "task_eng")
    _failed(db, "task_res", tenant_id="research", by=BOB)
    mine = _finding(db, "task_eng")
    theirs = _finding(db, "task_res", tenant_id="research")
    response = _get(client, "/v1/failure-findings")
    assert response.status_code == 200, response.text
    ids = [f["finding_id"] for f in response.json()["findings"]]
    # MUTATION: drop the `tenant_id ==` filter in `tenant_findings` and research's finding is listed.
    assert ids == [mine]
    # MUTATION: serve eng's findings for every caller and bob reads alice's.
    assert [f["finding_id"] for f in _get(client, "/v1/failure-findings", "bob").json()["findings"]] \
        == [theirs]


def test_findings_are_narrowed_to_one_agent_by_task_id(client, db):
    _failed(db, "task_a")
    _failed(db, "task_b")
    _finding(db, "task_a")
    b = _finding(db, "task_b")
    found = _get(client, "/v1/failure-findings?task_id=task_b").json()["findings"]
    # MUTATION: ignore `task_id` in `tenant_findings` and task_a's finding is listed too.
    assert [f["finding_id"] for f in found] == [b]


def test_one_finding_of_the_tenant_is_served(client, db):
    _failed(db, "task_eng")
    fid = _finding(db, "task_eng")
    response = _get(client, f"/v1/failure-findings/{fid}")
    assert response.status_code == 200, response.text
    body = response.json()
    # MUTATION: serve `fix` as `cause` and the cause sentence is not the investigator's.
    assert body["cause"].startswith("The repository's test_login fails on a missing fixture")
    # MUTATION: drop `task_id` from `tenant_view` and the card cannot name its agent.
    assert body["task_id"] == "task_eng" and body["state"] == "open"


def test_a_cause_stored_plain_is_served_exactly_as_written(client, db):
    """`Filing.report` stores plain text (FAILURE-STORE-PLAIN, part of #2391):
    the console escapes it once through React, so the API serves it untouched.
    A document written before keeps its entities: nothing migrates them."""
    _failed(db, "task_new")
    _failed(db, "task_old")
    new = _finding(db, "task_new", cause="implement -> review")
    old = _finding(db, "task_old", cause="implement -&gt; review")
    # MUTATION: escape `<`/`>` in `tenant_text` and this reads 'implement -&gt; review'.
    assert _get(client, f"/v1/failure-findings/{new}").json()["cause"] == "implement -> review"
    assert _get(client, f"/v1/failure-findings/{old}").json()["cause"] == "implement -&gt; review"


# -- another tenant's id answers as missing ------------------------------------

def test_another_tenants_finding_answers_exactly_as_a_missing_one(client, db):
    _failed(db, "task_res", tenant_id="research", by=BOB)
    theirs = _finding(db, "task_res", tenant_id="research")
    other = _get(client, f"/v1/failure-findings/{theirs}")
    missing = _get(client, "/v1/failure-findings/ff_" + "0" * 20)
    # MUTATION: drop the `tenant_id != tenant_id` check in `get_failure_finding` and alice reads it (200).
    assert other.status_code == 404
    # MUTATION: answer another tenant's id with 403 and the id is confirmed to exist.
    assert missing.status_code == 404
    assert other.json()["code"] == missing.json()["code"]
    # MUTATION: word the other tenant's 404 differently and the two are told apart.
    assert other.json()["message"].replace(theirs, "<id>") \
        == missing.json()["message"].replace("ff_" + "0" * 20, "<id>")
    # MUTATION: echo the stored document in the 404's detail and research's cause leaks.
    assert "test_login" not in other.text


# -- no platform-tenant text reaches a tenant ----------------------------------

def test_no_platform_tenant_field_reaches_the_tenant(client, db):
    _failed(db, "task_eng")
    fid = _finding(db, "task_eng")
    listed = _get(client, "/v1/failure-findings").json()
    one = _get(client, f"/v1/failure-findings/{fid}").json()
    # MUTATION: serve the stored document (`return doc`) and the fingerprint, firing, schedule and fix leak.
    assert _platform_text(listed) == []
    assert _platform_text(one) == []
    # MUTATION: drop the allow-list in `tenant_view` and every evidence field passes.
    assert set(one["evidence"]) <= set(("kind", "state", "profile", "end_cause", "last_error"))


def test_the_digests_references_to_other_failures_and_tenants_are_replaced(client, db):
    _failed(db, "task_eng")
    fid = _finding(db, "task_eng")
    cause = _get(client, f"/v1/failure-findings/{fid}").json()["cause"]
    # MUTATION: drop `_FAILURE_REF.sub` in `tenant_text` and `f-002` reaches the tenant.
    assert "f-002" not in cause and "a failure" in cause
    # MUTATION: drop `_TENANT_LABEL.sub` in `tenant_text` and the tenant label `t2` reaches the tenant.
    assert "t2" not in cause.split() and "a tenant" in cause


def test_a_credential_in_the_cause_is_redacted_before_it_is_served(client, db):
    _failed(db, "task_eng")
    secret = "ghp_" + "x" * 36
    fid = _finding(db, "task_eng", cause=f"The repository's CI printed {secret} and failed")
    response = _get(client, f"/v1/failure-findings/{fid}")
    assert response.status_code == 200, response.text
    # MUTATION: drop `redact_detail` in `tenant_text` and the token is served.
    assert secret not in response.text and "failed" in response.json()["cause"]


# -- the bell ------------------------------------------------------------------

def _viewer(email: str = ALICE, tenant_id: str = "eng") -> notif.Viewer:
    return notif.Viewer(email=email, tenant_id=tenant_id)


def _bell(db, viewer: notif.Viewer) -> list[dict[str, Any]]:
    return notif.failure_findings(notif.Reading(db=db, store=Store(db), viewer=viewer, now=_now()))


def test_the_bell_item_names_the_agent_and_the_cause_sentence(db):
    _failed(db, "task_eng", step_id="impl-login")
    fid = _finding(db, "task_eng")
    items = _bell(db, _viewer())
    # MUTATION: remove `("failure_review", failure_findings)` from SOURCES and nothing rings.
    assert ("failure_review", notif.failure_findings) in notif.SOURCES
    # MUTATION: skip findings whose state is "open" and the item is not derived.
    assert [i["id"] for i in items] == [f"failure_review:{fid}"]
    item = items[0]
    # MUTATION: title the item without `task.step_id` and the bell does not name the agent.
    assert "impl-login" in item["title"] and "task_eng" in item["title"]
    # MUTATION: put the evidence's `last_error` in the detail and the cause sentence is gone.
    assert item["detail"].startswith("The repository's test_login fails on a missing fixture")
    # MUTATION: mark it `needs_you=False` and it is only an update.
    assert item["needs_you"] is True and item["topic"] in notif.TOPICS
    # MUTATION: link it to `/agents/recent/<id>` without the anchor and the card is not opened.
    assert item["href"] == "/agents/recent/task_eng#failure-findings"


def test_an_agent_without_a_step_is_named_by_its_profile(db):
    _failed(db, "task_eng")
    _finding(db, "task_eng")
    title = _bell(db, _viewer())[0]["title"]
    # MUTATION: drop the `or task.runner_profile` fallback and the title says "None".
    assert "None" not in title and "task_eng" in title


def test_the_bell_rings_only_the_submitter_in_the_findings_tenant(db):
    _failed(db, "task_eng")
    _failed(db, "task_carols", by="carol@example.com")
    _failed(db, "task_res", tenant_id="research", by=BOB)
    _finding(db, "task_eng")
    _finding(db, "task_carols")
    _finding(db, "task_res", tenant_id="research")
    ids = [i["id"] for i in _bell(db, _viewer())]
    # MUTATION: skip the submitter check and carol's finding rings alice.
    assert ids == [f"failure_review:{failure_review.finding_id(FINGERPRINT, 'eng', 'task_eng')}"]
    # MUTATION: read findings without the tenant filter and research's rings in eng.
    assert all("task_res" not in i["title"] for i in _bell(db, _viewer()))
    # The same submitter in another tenant hears nothing of eng's finding.
    assert _bell(db, _viewer(ALICE, "research")) == []


def test_a_closed_or_old_finding_does_not_ring(db):
    _failed(db, "task_closed")
    _failed(db, "task_old")
    _finding(db, "task_closed", state="resolved")
    _finding(db, "task_old", created_at=_now() - notif.WINDOW - timedelta(hours=1))
    # MUTATION: drop the `state == "open"` or the `_recent` check and one of them rings.
    assert _bell(db, _viewer()) == []


def test_the_bell_item_carries_no_platform_tenant_text(db):
    _failed(db, "task_eng")
    _finding(db, "task_eng")
    item = _bell(db, _viewer())[0]
    # MUTATION: build the detail from the stored `cause` without `tenant_view` and `f-002`/`t2` ring.
    assert _platform_text(item) == []


def test_the_feed_route_serves_the_finding(client, db):
    _failed(db, "task_eng")
    fid = _finding(db, "task_eng")
    body = _get(client, "/v1/notifications").json()
    # MUTATION: leave the source out of SOURCES and the feed does not carry it.
    assert f"failure_review:{fid}" in [i["id"] for i in body["items"]]
    assert _platform_text([i for i in body["items"] if i["id"].startswith("failure_review:")]) == []
