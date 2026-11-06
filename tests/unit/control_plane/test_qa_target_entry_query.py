"""A leg's `entry_query` (QA-ENTRY-QUERY).

An app that picks its backend from the address (`?env=production`, else a tab
slot, else the browser's last leg, else Staging) lands a fresh browser on
Staging unless every URL says which leg. `base_url` refuses a query, so the
leg carries its pairs apart: registered and changed with the leg, checked as
plain `key=value` pairs, served on the gate's sign-in entry and the page it
leaves the agent on, and passed to every capture step as `--entry-query`. It is
not part of the host: a change of it alone keeps the leg's approval.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_api import qaspec, qatargets

from .conftest import auth_header
from .test_qa_target_credential import OWN_LOGIN, FakeQaCredentials, store  # noqa: F401 - fixtures
from .test_qa_target_environments import leg, register_legs
from .test_qa_targets import (  # noqa: F401 - fixtures are used by name
    TARGETS,
    api_context,
    approve,
    register,
    resolver,
    seeded,
    target_docs,
)

PRODUCTION_LOGIN = {"kind": "own_login", "login_path": "/", "signed_in_selector": "nav .me",
                    "extra_origin": "https://auth.example.com"}


def _legs(query: str | None = "env=production") -> list[dict[str, Any]]:
    production = leg("prod-user", "https://shop.example.com", sign_in=PRODUCTION_LOGIN)
    if query is not None:
        production["entry_query"] = query
    return [production, leg("staging", "https://app.example.com", environment="non_production")]


def _entry(answer: dict[str, Any], name: str) -> dict[str, Any]:
    return next(e for e in answer["environments"] if e["name"] == name)


def test_a_leg_registers_and_round_trips_its_entry_query(client, db, seeded, store):
    response = register_legs(client, _legs())
    assert response.status_code == 201, response.text
    target = response.json()["qa_target"]
    # MUTATION: leave `entry_query` out of LEG_SPEC_FIELDS (or `_entry_api`) and it is dropped.
    assert _entry(target, "prod-user")["entry_query"] == "env=production"
    assert "entry_query" not in _entry(target, "staging")
    read = client.get(f"{TARGETS}/{target['id']}", headers=auth_header("carol")).json()["qa_target"]
    assert _entry(read, "prod-user")["entry_query"] == "env=production"
    # A change of another field keeps it (the PATCH merges the stored spec).
    changed = client.patch(f"{TARGETS}/{target['id']}", json={"name": "Shop, renamed"},
                           headers=auth_header("carol"))
    assert changed.status_code == 200, changed.text
    assert _entry(changed.json()["qa_target"], "prod-user")["entry_query"] == "env=production"


def test_a_one_leg_target_takes_it_beside_its_base_url(client, db, seeded):
    response = register(client, entry_query="?env=production")
    assert response.status_code == 201, response.text
    # Stored without its leading `?`, as every URL merges it.
    assert response.json()["qa_target"]["entry_query"] == "env=production"
    (doc,) = target_docs(db).values()
    assert doc["entry_query"] == "env=production"


@pytest.mark.parametrize("query", [
    "env", "env=production&env=staging", "env=a b", "env=a%20b", "env=https://x/y", "token=abc",
    "api_key=abc", "session=1", "&".join(f"k{i}=v" for i in range(9)), "env=" + "x" * 300, 7,
], ids=["no-value", "twice", "space", "percent", "slash", "token", "key", "session", "nine", "long", "number"])
def test_a_malformed_entry_query_is_refused_and_nothing_is_written(client, db, seeded, store, query):
    response = register_legs(client, _legs(query))  # type: ignore[arg-type]
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["field"] == "entry_query"
    assert target_docs(db) == {}
    one = register(client, entry_query=query)
    assert one.status_code == 422, one.text


def test_a_change_of_the_entry_query_alone_keeps_the_approval(client, db, seeded, store):
    target = register_legs(client, _legs(None)).json()["qa_target"]
    approved = approve(client, target["id"], host="shop.example.com", environment="prod-user",
                       extra_origin="https://auth.example.com")
    assert approved.status_code == 200, approved.text
    response = client.patch(f"{TARGETS}/{target['id']}", json={"environments": _legs("env=production")},
                            headers=auth_header("carol"))
    assert response.status_code == 200, response.text
    entry = _entry(response.json()["qa_target"], "prod-user")
    assert entry["entry_query"] == "env=production"
    # MUTATION: count `entry_query` in `patch`'s host comparison and this is pending.
    assert entry["host_state"] == "approved"
    assert entry["approved_origins"] == ["https://shop.example.com", "https://auth.example.com"]


def test_the_gate_signs_in_and_lands_with_the_entry_query(client, db, seeded, store):
    target = register_legs(client, _legs()).json()["qa_target"]
    (doc,) = target_docs(db).values()
    view = qaspec.leg(qatargets._normal({**doc, "id": target["id"]}), "prod-user")
    served = qatargets.QaTargetIssuer._answer(view)
    # MUTATION: serve `login_url` and `base_url` without it and the gate signs
    # in on the app's default leg (Staging), as the run of 2026-11-03 did.
    assert served["sign_in"]["login_url"] == "https://shop.example.com/?env=production"
    assert served["base_url"] == "https://shop.example.com/?env=production"
    other = qatargets.QaTargetIssuer._answer(qaspec.leg(qatargets._normal({**doc, "id": target["id"]}), "staging"))
    assert other["base_url"] == "https://app.example.com"


def test_every_capture_step_passes_the_entry_query_to_the_tool():
    target = {
        "id": "qat_3f9a", "revision": 1, "name": "Studio", "tenant_id": "tenant-a",
        "base_url": "https://app.example.com", "repository": "https://github.com/example-org/studio",
        "sign_in": {"kind": "none"}, "screens": [{"id": "home", "path": "/"}], "flows": [],
        "environment": "production", "entry_query": "env=production&lang=en",
    }
    spec = qaspec.render(target)
    captures = [s for s in spec["steps"] if s["step_id"].startswith("capture-")]
    assert captures
    for step in captures:
        # MUTATION: leave the flag out of `_capture_prompt` and the capture opens Staging.
        assert "--entry-query 'env=production&lang=en'" in step["prompt"]
    plain = qaspec.render({**target, "entry_query": None})
    assert all("--entry-query" not in s["prompt"] for s in plain["steps"])


def test_with_query_merges_the_legs_keys_over_a_urls_own():
    assert qaspec.with_query("https://a.example.com/s?env=staging&tab=x#/r", "env=production") \
        == "https://a.example.com/s?tab=x&env=production#/r"
    assert qaspec.with_query("https://a.example.com", None) == "https://a.example.com"
