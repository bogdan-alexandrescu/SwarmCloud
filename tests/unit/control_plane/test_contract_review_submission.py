"""Jev's review of a work contract at submission (`swarm_api.contractreview`, part of #2688).

The design is docs/design/agent-quality/jev-contract.md section 4, with the
owner's answers of 3 November 2026: its own mode, SWARM_CONTRACT_REVIEW
(answer 7); its own signed workspace setting, `contract_review` (answer 9);
open and recorded `not_run` when the call fails (answer 2); amended
automatically as revision 2 by `reviewer` (answer 3). Held here:

  * `report` logs the 422 it would make and changes nothing;
  * `enforce` refuses with section 4.5's body, creating nothing;
  * a failed call is accepted open and recorded `not_run`;
  * the workspace setting off sends nothing to Jev;
  * the review's usage lands in the month as kind `contract_review`;
  * `off` (the default) reads nothing and records nothing.

Offline: FakeFirestore, a local spec signer, and a transport built here that
answers in the decision API's wire shape. No credentials, no network.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import contractjudge as cj
from swarm_api import contractreview as cr
from swarm_api import jevusage
from swarm_api import judgment
from swarm_api import workcontract as wc
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.objects import InMemoryObjectReader
from swarm_api.waker import NullWaker

from .conftest import PROJECT, api_settings, auth_header, seed_tenant
from .spec_signer import LocalSpecSigner

CODE = "claude-code"
NOW = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
#: The input tokens the fake decision API states it used, per call.
TOKENS = 240


class Jev:
    """The decision API, as a transport: answers each `c<k>`/`r<k>` from
    `classes` ({index: (class, reason, p)}), or fails as told."""

    def __init__(self, classes: dict[int, tuple[str, str, float]] | None = None, *,
                 status: int = 200, raises: bool = False) -> None:
        self.classes = classes or {}
        self.status = status
        self.raises = raises
        self.bodies: list[dict[str, Any]] = []
        self.timeouts: list[float] = []

    def __call__(self, url: str, body: bytes, headers: Any, timeout: float):
        self.bodies.append(json.loads(body))
        self.timeouts.append(timeout)
        if self.raises:
            raise TimeoutError("no answer")
        if self.status != 200:
            return self.status, b""
        answers = {}
        for name in self.bodies[-1]["questions"]:
            cls, reason, p = self.classes.get(int(name[1:]), ("mechanical", "none", 0.9))
            choice = cls if name[0] == "c" else reason
            answers[name] = {"choice": choice, "probabilities": {choice: p}, "confidence": p}
        return 200, json.dumps({"answers": answers,
                                "usage": {"input_tokens": TOKENS}}).encode()


@pytest.fixture
def context(db, tokens, group_map):
    seed_tenant(db, "eng")
    return build_context(
        settings=api_settings(), db=db, verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map), credentials=InMemoryCredentials(), waker=NullWaker(),
        metrics=ApiMetrics(), objects=InMemoryObjectReader(bucket=f"swarm-artifacts-{PROJECT}"),
        signer=LocalSpecSigner(),
    )


@pytest.fixture
def client(context) -> TestClient:
    return TestClient(create_app(context), raise_server_exceptions=False)


@pytest.fixture
def routed(monkeypatch):
    """Contracts on, and swarm-api's route to the internal backend rendered."""
    monkeypatch.setenv(wc.SWITCH_ENV, "on")
    monkeypatch.setenv("INTERNAL_DECISION_API_URL", judgment.INTERNAL_DECISION_ORIGIN)
    monkeypatch.setenv(judgment.INTERNAL_DECISION_API_ADDRESS_ENV, "10.20.0.7")


def _reviewer(context, db, jev: Jev, logs: list | None = None) -> cr.ContractReviewer:
    reviewer = cr.ContractReviewer(
        db, mac=judgment.SettingsMac(), now=lambda: NOW, transport=jev,
        sleep=lambda s: None,
        log=(lambda event, **f: logs.append({"event": event, **f})) if logs is not None
        else (lambda *a, **k: None))
    context.submissions.contract_reviewer = reviewer
    return reviewer


def _opt_in(db, enabled: bool = True, **extra: Any) -> None:
    db.collection("workspace_settings").document("eng").set(
        {"tenant_id": "eng", cr.FIELD: {cr.ENABLED: enabled}, **extra})


def _contract(**over: Any) -> dict[str, Any]:
    doc = {
        "schema": 1,
        "contract_id": "wc-sort-widgets",
        "tenant_id": "eng",
        "revision": 1,
        "strictness": "strict",
        "requirements": [
            {"id": "REQ-1", "text": "Widgets sort by name.", "verification": []},
            {"id": "REQ-2", "text": "It feels nicer.", "verification": []},
        ],
        "limits": {"runner_profile": CODE},
        "ownership": {"owner": "alice@example.com"},
    }
    doc.update(over)
    return doc


def _tasks(db) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith("tasks/") and p.count("/") == 1]


def _task(client, contract: dict[str, Any]):
    return client.post("/v1/tasks", headers=auth_header("alice"),
                       json={"runner_profile": CODE, "input": {"prompt": "hello"},
                             "metadata": {wc.METADATA_KEY: contract}})


UNENFORCEABLE = {1: ("unenforceable", "subjective", 0.83)}


# --- the mode ------------------------------------------------------------------


@pytest.mark.parametrize("raw, want", [
    (None, "off"), ("", "off"), ("off", "off"), ("report", "report"),
    (" Enforce ", "enforce"), ("on", "off"), ("enforced", "off"),
])
def test_the_mode_is_its_own_setting_and_anything_unknown_is_off(raw, want):
    environ = {} if raw is None else {cr.MODE_ENV: raw}
    assert cr.mode(environ) == want


def test_the_mode_is_not_a_refusals_switch():
    """Answer 7: outside `refusals.py`, whose switch is keyed on the code."""
    from swarm_api import refusals

    source = open(refusals.__file__, encoding="utf-8").read()
    assert cr.MODE_ENV not in source and "contract_review" not in source


def test_off_reads_nothing_sends_nothing_and_records_nothing(client, context, db, routed,
                                                            monkeypatch):
    monkeypatch.delenv(cr.MODE_ENV, raising=False)
    _opt_in(db)
    jev = Jev(UNENFORCEABLE)
    _reviewer(context, db, jev)
    response = _task(client, _contract())
    assert response.status_code == 201, response.text
    assert jev.bodies == []
    (task,) = _tasks(db)
    assert cr.DISPATCH_FIELD not in task["metadata"]["dispatch"]


# --- report: logs the 422 it would make -----------------------------------------


def test_report_logs_the_422_it_would_make_and_lets_the_contract_through(
        client, context, db, routed, monkeypatch):
    monkeypatch.setenv(cr.MODE_ENV, "report")
    _opt_in(db)
    logs: list[dict[str, Any]] = []
    jev = Jev(UNENFORCEABLE)
    _reviewer(context, db, jev, logs)
    response = _task(client, _contract())
    assert response.status_code == 201, response.text
    (would,) = [e for e in logs if e["event"] == "contract_review_would_refuse"]
    assert would["status"] == 422
    assert would["body"]["code"] == "validation_failed"
    assert would["body"]["detail"]["reason"] == "work_contract_not_enforceable"
    assert would["body"]["detail"]["jev"]["requirements"] == [
        {"id": "REQ-2", "class": "unenforceable", "reason": "subjective", "p": 0.83}]
    (task,) = _tasks(db)
    # Nothing about the contract changed; the review is recorded beside it.
    assert task["metadata"][wc.METADATA_KEY]["revision"] == 1
    record = task["metadata"]["dispatch"][cr.DISPATCH_FIELD]
    assert record["outcome"] == cj.REVIEW_REFUSED and record["mode"] == "report"
    assert record["digest"] == task["metadata"]["dispatch"][wc.DISPATCH_FIELD]["digest"]


def test_report_does_not_amend(client, context, db, routed, monkeypatch):
    monkeypatch.setenv(cr.MODE_ENV, "report")
    _opt_in(db)
    _reviewer(context, db, Jev({0: ("judgeable", "proof_kind_mismatch", 0.9)}))
    assert _task(client, _contract(strictness="advisory")).status_code == 201
    (task,) = _tasks(db)
    assert task["metadata"][wc.METADATA_KEY]["revision"] == 1
    assert task["metadata"]["dispatch"][cr.DISPATCH_FIELD]["outcome"] == cj.REVIEW_AMENDED


# --- enforce: section 4.5's 422 -------------------------------------------------


def test_enforce_refuses_with_section_4_5s_body_and_stores_nothing(client, context, db,
                                                                   routed, monkeypatch):
    monkeypatch.setenv(cr.MODE_ENV, "enforce")
    _opt_in(db)
    _reviewer(context, db, Jev(UNENFORCEABLE))
    sent = _contract()
    response = _task(client, sent)
    assert response.status_code == 422, response.text
    body = response.json()
    assert set(body) == {"code", "message", "detail"}
    assert body["code"] == "validation_failed"
    assert body["message"] == ("contract wc-sort-widgets is strict, and Jev found requirements "
                               "no worker could enforce: REQ-2")
    detail = body["detail"]
    assert set(detail) == {"reason", "contract_id", "revision", "digest", "unenforced", "jev"}
    assert detail["reason"] == "work_contract_not_enforceable"
    assert detail["contract_id"] == "wc-sort-widgets"
    assert detail["revision"] == 1
    assert detail["digest"] == wc.digest(wc.decode(sent))
    assert detail["unenforced"] == []
    assert set(detail["jev"]) == {"call_id", "backend", "requirements"}
    assert detail["jev"]["backend"] == judgment.INTERNAL_JEV
    assert detail["jev"]["call_id"]
    assert detail["jev"]["requirements"] == [
        {"id": "REQ-2", "class": "unenforceable", "reason": "subjective", "p": 0.83}]
    # A requirement's text is never echoed back.
    assert "feels nicer" not in response.text
    assert _tasks(db) == []


def test_enforce_amends_a_judgeable_requirement_as_revision_2_by_the_reviewer(
        client, context, db, routed, monkeypatch):
    monkeypatch.setenv(cr.MODE_ENV, "enforce")
    _opt_in(db)
    _reviewer(context, db, Jev({0: ("judgeable", "proof_kind_mismatch", 0.9)}))
    sent = _contract(strictness="advisory")
    assert _task(client, sent).status_code == 201
    (task,) = _tasks(db)
    stored = wc.decode(task["metadata"][wc.METADATA_KEY])
    assert stored.revision == 2
    assert stored.previous_digest == wc.digest(wc.decode(sent))
    assert stored.requirements[0].verification == ("judgment",)
    assert stored.requirements[1].verification == ()
    reference = task["metadata"]["dispatch"][wc.DISPATCH_FIELD]
    assert reference["revision"] == 2 and wc.verify_reference(stored, reference)
    record = task["metadata"]["dispatch"][cr.DISPATCH_FIELD]
    assert record["outcome"] == cj.REVIEW_AMENDED
    assert record["amended_to"] == {"revision": 2, "digest": wc.digest(stored),
                                    "previous_digest": wc.digest(wc.decode(sent)),
                                    "changed": ["REQ-1"]}


# --- answer 2: a failed call is open and not_run ---------------------------------


@pytest.mark.parametrize("jev, reason", [
    (Jev(status=503), "http_503"),
    (Jev(raises=True), "transport"),
])
def test_a_failed_call_is_accepted_open_and_recorded_not_run(client, context, db, routed,
                                                             monkeypatch, jev, reason):
    monkeypatch.setenv(cr.MODE_ENV, "enforce")
    _opt_in(db)
    _reviewer(context, db, jev)
    sent = _contract()
    assert _task(client, sent).status_code == 201
    (task,) = _tasks(db)
    assert wc.decode(task["metadata"][wc.METADATA_KEY]) == wc.decode(sent)
    record = task["metadata"]["dispatch"][cr.DISPATCH_FIELD]
    assert record["outcome"] == cj.REVIEW_NOT_RUN and record["reason"] == reason
    # One try and one retry, not the client's three tries.
    assert len(jev.bodies) == 2


def test_no_route_is_not_run_and_never_typesafe(client, context, db, monkeypatch):
    monkeypatch.setenv(wc.SWITCH_ENV, "on")
    monkeypatch.setenv(cr.MODE_ENV, "enforce")
    monkeypatch.setenv("INTERNAL_DECISION_API_URL", judgment.INTERNAL_DECISION_ORIGIN)
    monkeypatch.delenv(judgment.INTERNAL_DECISION_API_ADDRESS_ENV, raising=False)
    _opt_in(db)
    jev = Jev(UNENFORCEABLE)
    _reviewer(context, db, jev)
    assert _task(client, _contract()).status_code == 201
    assert jev.bodies == []
    (task,) = _tasks(db)
    record = task["metadata"]["dispatch"][cr.DISPATCH_FIELD]
    assert record["outcome"] == cj.REVIEW_NOT_RUN and record["reason"] == "no_route"


def test_the_deadline_bounds_every_dial_and_the_retry():
    now = [100.0]
    seen: list[float] = []

    def transport(url, body, headers, timeout):
        seen.append(timeout)
        now[0] += 9.0
        return 503, b""

    call = cr.bounded(transport, deadline_at=100.0 + cr.DEADLINE_SECONDS, clock=lambda: now[0])
    assert call("u", b"", {}, 10)[0] == 503
    assert call("u", b"", {}, 10)[0] == 503
    # The retry is spent: the last answer again, with no third dial.
    assert call("u", b"", {}, 10)[0] == 503
    assert seen == [10, 6.0]
    # Past the deadline nothing is dialled at all.
    late = cr.bounded(transport, deadline_at=now[0], clock=lambda: now[0])
    with pytest.raises(TimeoutError):
        late("u", b"", {}, 10)
    assert seen == [10, 6.0]


# --- answer 9: the workspace's own setting gates the call --------------------------


@pytest.mark.parametrize("settings", [
    {},
    {cr.FIELD: {cr.ENABLED: False}},
    {cr.FIELD: {cr.ENABLED: "true"}},
    # The setting that governs CODE is not this one.
    {"code_intelligence": {"jev": True}},
])
def test_the_workspace_setting_off_sends_nothing_to_jev(client, context, db, routed,
                                                        monkeypatch, settings):
    monkeypatch.setenv(cr.MODE_ENV, "enforce")
    db.collection("workspace_settings").document("eng").set({"tenant_id": "eng", **settings})
    jev = Jev(UNENFORCEABLE)
    _reviewer(context, db, jev)
    assert _task(client, _contract()).status_code == 201
    assert jev.bodies == []
    (task,) = _tasks(db)
    record = task["metadata"]["dispatch"][cr.DISPATCH_FIELD]
    assert record["outcome"] == cj.REVIEW_NOT_RUN and record["reason"] == cr.NOT_OPTED_IN


def test_the_setting_is_signed_and_a_forged_block_reads_off(db):
    key = secrets.token_hex(16)
    store = cr.ContractReviewStore(db, now=lambda: NOW,
                                   mac=judgment.SettingsMac(keys=(key,), tenants=("eng",)))
    store.put("eng", True, by="alice@example.com")
    doc = store.document("eng")
    assert doc[cr.FIELD]["mac"]["v"] == 1
    assert cr.stored_enabled(doc, tenant_id="eng", mac=store.mac) is True
    # The same block copied onto another workspace does not verify there.
    assert cr.stored_enabled(doc, tenant_id="research",
                             mac=judgment.SettingsMac(keys=(key,), tenants=("*",))) is False
    # A tenant worker's direct write, with no MAC, reads as off once cut over.
    forged = {cr.FIELD: {cr.ENABLED: True}}
    assert cr.stored_enabled(forged, tenant_id="eng", mac=store.mac) is False
    audits = [d for p, d in db.docs.items() if p.startswith("admin_audit/")]
    assert [a["action"] for a in audits] == [cr.AUDIT_ACTION]


def test_the_re_sign_pass_re_signs_the_block_after_a_key_rotation(db):
    """A rotation re-signs every stored block under the new key before the
    previous one is dropped (`scripts/resign-workspace-settings.sh`). The
    pass walked only the frozen `SETTINGS_MAC_FIELDS`, so a workspace's
    `contract_review` opt-in stayed signed under the old key and, once that
    key was dropped, read as off for a tenant cut over: the review stopped
    with nobody having turned it off."""
    old, new = secrets.token_hex(16), secrets.token_hex(16)
    cr.ContractReviewStore(db, now=lambda: NOW,
                           mac=judgment.SettingsMac(keys=(old,), tenants=("eng",))
                           ).put("eng", True, by="alice@example.com")
    plan = judgment.resign_workspace_settings(db, new, apply=False)
    assert {(b["block"], b["status"]) for b in plan["blocks"]} == {(cr.FIELD, "unsigned")}
    assert plan["blocks"][0]["covered"] == {cr.ENABLED: True}
    assert judgment.resign_workspace_settings(db, new, apply=True)["written"] == 1
    doc = cr.ContractReviewStore(db, now=lambda: NOW).document("eng")
    rotated = judgment.SettingsMac(keys=(new,), tenants=("eng",))
    assert cr.stored_enabled(doc, tenant_id="eng", mac=rotated) is True
    # Only the MAC was written, and a second run writes nothing.
    assert doc[cr.FIELD][cr.ENABLED] is True and doc[cr.FIELD]["set_by"] == "alice@example.com"
    again = judgment.resign_workspace_settings(db, new, apply=True)
    assert again["written"] == 0
    assert {b["status"] for b in again["blocks"]} == {"signed"}


def test_the_re_sign_pass_signs_an_unsigned_opt_in_only_as_stored(db):
    """A block stored before this deployment had a key is signed as it is
    stored, like every frozen block: the pass never turns a setting on."""
    _opt_in(db, enabled=False)
    key = secrets.token_hex(16)
    assert judgment.resign_workspace_settings(db, key, apply=True)["written"] == 1
    doc = cr.ContractReviewStore(db, now=lambda: NOW).document("eng")
    mac = judgment.SettingsMac(keys=(key,), tenants=("eng",))
    assert judgment.block_verifies("eng", cr.MAC_BLOCK, doc[cr.FIELD], mac.keys)
    assert cr.stored_enabled(doc, tenant_id="eng", mac=mac) is False


def test_the_signing_list_names_the_new_block_only():
    from swarm_common.specsign import SETTINGS_MAC_FIELDS

    assert dict(judgment.API_SETTINGS_MAC_FIELDS) == {"contract_review": ("enabled",)}
    assert not set(judgment.API_SETTINGS_MAC_FIELDS) & set(SETTINGS_MAC_FIELDS)


def test_the_route_reads_and_writes_the_setting(client, db, monkeypatch):
    monkeypatch.setenv(cr.MODE_ENV, "report")
    got = client.get("/v1/workspaces/eng/contract-review", headers=auth_header("alice"))
    assert got.status_code == 200, got.text
    assert got.json()[cr.FIELD] == {cr.ENABLED: False}
    assert got.json()["mode"] == "report"
    put = client.put("/v1/workspaces/eng/contract-review", headers=auth_header("root"),
                     json={"enabled": True})
    assert put.status_code == 200, put.text
    assert put.json()[cr.FIELD] == {cr.ENABLED: True}
    assert client.put("/v1/workspaces/eng/contract-review", headers=auth_header("root"),
                      json={"enabled": "yes"}).status_code == 422


# --- usage lands in the month ----------------------------------------------------------


def test_the_reviews_usage_lands_in_the_month_as_contract_review(client, context, db, routed,
                                                                 monkeypatch):
    monkeypatch.setenv(cr.MODE_ENV, "report")
    _opt_in(db)
    db.collection("jev_usage").document("eng_2026-11").set(
        {"tenant_id": "eng", "month": "2026-11", "input_tokens": 100})
    _reviewer(context, db, Jev())
    assert _task(client, _contract()).status_code == 201
    month = db.collection("jev_usage").document("eng_2026-11").get().to_dict()
    assert month["input_tokens"] == 100 + TOKENS
    row = db.collection("jev_usage").document("eng_contract_wc-sort-widgets_r1").get().to_dict()
    assert row["kind"] == jevusage.CONTRACT_REVIEW_KIND and row["tenant_id"] == "eng"
    assert row["calls"] == 1 and row["input_tokens"] == TOKENS and row["month"] == "2026-11"
    assert row["by_kind"]["contract_review"]["outcomes"] == {"ok": 1}
    page = jevusage.JevUsageReader(db).read("eng", now=NOW, rates={"input": None},
                                            cap_tokens=None)
    (kind,) = page["total"]["by_kind"]
    assert kind["kind"] == "contract_review" and kind["group"] == "contract_review"
    assert kind["calls"] == 1 and kind["input_tokens"] == TOKENS
    assert page["month"]["used_tokens"] == 100 + TOKENS


def test_a_month_over_its_cap_sends_nothing_and_is_not_run(client, context, db, routed,
                                                           monkeypatch):
    monkeypatch.setenv(cr.MODE_ENV, "enforce")
    _opt_in(db, code_intelligence={"jev_monthly_cap_tokens": 500})
    db.collection("jev_usage").document("eng_2026-11").set(
        {"tenant_id": "eng", "month": "2026-11", "input_tokens": 500})
    jev = Jev(UNENFORCEABLE)
    _reviewer(context, db, jev)
    assert _task(client, _contract()).status_code == 201
    assert jev.bodies == []
    (task,) = _tasks(db)
    record = task["metadata"]["dispatch"][cr.DISPATCH_FIELD]
    assert record["outcome"] == cj.REVIEW_NOT_RUN and record["reason"] == "budget_monthly"
    # A skipped call spent nothing: the month is unchanged.
    assert db.collection("jev_usage").document("eng_2026-11").get().to_dict()[
        "input_tokens"] == 500


def test_the_state_sent_never_names_the_tenant_the_owner_or_the_contract(
        client, context, db, routed, monkeypatch):
    monkeypatch.setenv(cr.MODE_ENV, "report")
    _opt_in(db)
    jev = Jev()
    _reviewer(context, db, jev)
    assert _task(client, _contract()).status_code == 201
    (sent,) = jev.bodies
    text = json.dumps(sent["state"])
    for never in ("eng", "alice@example.com", "wc-sort-widgets", "REQ-1"):
        assert never not in text


def test_a_strict_revision_2_no_worker_enforces_is_refused_not_degraded(
        client, context, db, routed, monkeypatch):
    """Adding `judgment` adds the `verification` component, which no worker
    enforces today, and the `judgment` one, which no worker enforces while
    JC2 does not run at the end of the step (the owner's pick "Gate on JC2"):
    a strict revision 2 is refused rather than carried advisory (a strict
    contract never silently degrades)."""
    monkeypatch.setenv(cr.MODE_ENV, "enforce")
    _opt_in(db)
    _reviewer(context, db, Jev({0: ("judgeable", "proof_kind_mismatch", 0.9)}))
    sent = _contract()
    response = _task(client, sent)
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "validation_failed"
    detail = body["detail"]
    assert set(detail) == {"reason", "contract_id", "revision", "digest", "unenforced", "jev"}
    assert detail["reason"] == "work_contract_not_enforceable"
    assert detail["contract_id"] == "wc-sort-widgets"
    assert detail["revision"] == 2
    assert detail["unenforced"] == ["judgment", "verification"]
    # The owner's answer 1 (4 November 2026): the digest is revision two's,
    # the one the reviewer made, never revision one's.
    revision_1 = wc.decode(sent)
    revision_2 = wc.decode({**sent, "revision": 2, "previous_digest": wc.digest(revision_1),
                            "requirements": [
                                {**sent["requirements"][0], "verification": ["judgment"]},
                                sent["requirements"][1]]})
    assert detail["digest"] == wc.digest(revision_2) != wc.digest(revision_1)
    assert set(detail["jev"]) == {"call_id", "backend", "requirements"}
    assert detail["jev"]["call_id"] and detail["jev"]["backend"] == judgment.INTERNAL_JEV
    assert detail["jev"]["requirements"] == [
        {"id": "REQ-1", "class": "judgeable", "reason": "proof_kind_mismatch", "p": 0.9}]
    assert "Widgets sort" not in response.text
    assert _tasks(db) == []


def _credential_shaped() -> str:
    """A GitHub-token-shaped string, built at runtime so no literal here looks
    like a credential."""
    return "ghp_" + secrets.token_hex(18)


def test_a_requirement_whose_text_the_sanitizer_drops_is_not_judged_and_never_asked(
        client, context, db, routed, monkeypatch):
    """Section 4.2: a requirement text the shared sanitizer drops is reported
    as not judged, never as enforceable. Before, the text was dropped but its
    `c<k>`/`r<k>` were still asked, so Jev's answer about a requirement it
    never read refused (or amended) the contract."""
    from swarm_jev import sanitize

    monkeypatch.setenv(cr.MODE_ENV, "enforce")
    _opt_in(db)
    token = _credential_shaped()
    assert sanitize.is_flagged(f"Deploy with {token}.")
    jev = Jev({0: ("unenforceable", "subjective", 0.9), 1: ("unenforceable", "subjective", 0.9)})
    _reviewer(context, db, jev)
    sent = _contract(requirements=[
        {"id": "REQ-1", "text": f"Deploy with {token}.", "verification": []},
        {"id": "REQ-2", "text": "It feels nicer.", "verification": []},
    ])
    response = _task(client, sent)
    assert response.status_code == 422, response.text
    # Refused on REQ-2 alone, the one Jev read; REQ-1 was never asked.
    assert [r["id"] for r in response.json()["detail"]["jev"]["requirements"]] == ["REQ-2"]
    (body,) = jev.bodies
    assert token not in json.dumps(body)
    assert set(body["questions"]) == {"c1", "r1"}
    state = body["state"] if isinstance(body["state"], dict) else json.loads(body["state"])
    assert [r["index"] for r in state["requirements"]] == [1]


def test_a_contract_whose_every_text_is_dropped_sends_nothing_and_is_not_judged(
        client, context, db, routed, monkeypatch):
    monkeypatch.setenv(cr.MODE_ENV, "enforce")
    _opt_in(db)
    jev = Jev({0: ("unenforceable", "subjective", 0.9)})
    _reviewer(context, db, jev)
    sent = _contract(requirements=[
        {"id": "REQ-1", "text": f"Deploy with {_credential_shaped()}.", "verification": []}])
    assert _task(client, sent).status_code == 201
    assert jev.bodies == []
    (task,) = _tasks(db)
    record = task["metadata"]["dispatch"][cr.DISPATCH_FIELD]
    assert record["outcome"] == cj.REVIEW_OK
    assert record["classes"] == [
        {"id": "REQ-1", "class": cj.NOT_JUDGED, "reason": None, "p": 0.0}]
    assert task["metadata"][wc.METADATA_KEY]["revision"] == 1


def test_the_blocks_bytes_are_the_frozen_functions_so_applying_request_138_re_signs_nothing(
        monkeypatch):
    """Contract request 138 moves `contract_review` into the frozen
    `SETTINGS_MAC_FIELDS`. swarm-api's own bytes for it must be exactly
    `canonical_settings_block`'s once the frozen list names it, so every MAC
    stored before the change still verifies after it."""
    from types import MappingProxyType

    from swarm_common import specsign

    fields = {cr.ENABLED: True, cr.SET_BY: "alice@example.com", "mac": {"v": 1}}
    ours = judgment._canonical_block("eng", cr.MAC_BLOCK, fields)
    monkeypatch.setattr(specsign, "SETTINGS_MAC_FIELDS", MappingProxyType(
        {**specsign.SETTINGS_MAC_FIELDS, cr.MAC_BLOCK: ("enabled",)}))
    assert ours == specsign.canonical_settings_block(tenant_id="eng", block=cr.MAC_BLOCK,
                                                     fields=fields)


@pytest.mark.parametrize(("jc2", "jev", "unenforced"), [
    (False, True, ["judgment", "verification"]),
    (True, False, ["judgment", "verification"]),
    (True, True, ["verification"]),
])
def test_judgment_is_enforced_for_revision_2_only_where_jc2_runs(
        client, context, db, routed, monkeypatch, jc2, jev, unenforced):
    """The owner's pick "Gate on JC2" (4 November 2026): `judgment` counts as
    enforced only where the worker's post-step runs JC2 and the workspace's
    signed `code_intelligence.jev` lets it send code, never merely because
    the submission review answered."""
    monkeypatch.setenv(cr.MODE_ENV, "enforce")
    monkeypatch.setattr(wc, "JC2_AT_END_OF_STEP", jc2)
    _opt_in(db, code_intelligence={"jev": jev})
    _reviewer(context, db, Jev({0: ("judgeable", "proof_kind_mismatch", 0.9)}))
    response = _task(client, _contract())
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["revision"] == 2 and detail["unenforced"] == unenforced
    assert _tasks(db) == []
