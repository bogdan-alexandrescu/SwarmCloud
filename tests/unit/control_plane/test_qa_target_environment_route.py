"""The worker's route serves only the mark's entry (docs/design/qa-environments.md
§1.5, lane QAENV-MODEL of §8; part of #2136).

What these tests hold, over the real app, the in-memory Firestore and a real
attempt key (test_live_api's harness, as test_qa_target_attempt_route.py):

  * an agent marked for one leg is answered that leg's base URL, origins,
    sign-in, password and challenge bypass value, and NOTHING of the other
    leg's: its origin, password and bypass value, each built at run time, are
    in no answer, no Firestore write and no log line;
  * a mark that names no entry of the target, a nameless mark on a target of
    two entries, and a mark whose run record names another leg each answer
    exactly as a missing target;
  * `mutating` is false on a `non_production` entry that shares an origin
    with a `production` entry, whatever its flow declares (§1.5's
    shared-origin rule, AI Studio's case under F-1), and true on one that
    shares none;
  * a run started before the migration is refused: the migration bumps the
    revision;
  * the migrated entry's credential is read from the legacy key until its
    next write, and from its own key after it.
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any

import pytest

from swarm_api.qatargets import QaTargets

from .test_live_api import (  # noqa: F401 - fixtures are used by name
    ATTEMPT,
    TASK,
    WORKER_TOKEN,
    Attempt,
    _b64url,
    clock,
    groups,
    ldb,
    make_client,
    people,
    relay,
    seed_running,
)
from .test_qa_target_attempt_route import (  # noqa: F401 - fixtures are used by name
    NOW,
    TARGET,
    WORKFLOW,
    ask,
    client,
    issued_rows,
    running,
    seed_target,
    stamp,
    store,
)

STAGING = "https://staging.example.com"
SHOP = "https://shop.example.com"
VERSION = "projects/p/secrets/swarm-tenant-eng-qa/versions/1"
FLOWS = [{"id": "checkout", "goal": "Buy the cheapest item", "start_path": "/", "mutating": True}]


def status(is_set: bool = True) -> dict[str, Any]:
    return {"set": is_set, "set_at": NOW if is_set else None, "set_by": "root@example.com" if is_set else None,
            "version": VERSION if is_set else None}


def entry(name: str, base: str, *, environment: str = "production", bypass: bool = True) -> dict[str, Any]:
    host = base.split("://", 1)[1]
    out: dict[str, Any] = {
        "name": name, "base_url": base, "origin": base, "host": host, "environment": environment,
        "sign_in": {"kind": "own_login", "login_path": f"/{name}-login", "signed_in_selector": "nav .me"},
        "host_state": "approved", "approved_origins": [base], "approved_by": "root@example.com",
        "approved_at": NOW, "approved_idp_token_path": None,
        "credential": status(), "challenge_bypass_value": status(bypass), "legacy_map_key": False,
    }
    if bypass:
        out["challenge_bypass"] = {"kind": "header", "name": "X-QA-Bypass"}
    return out


def seed_legs(db: Any, *, staging_base: str = STAGING, flows: list[dict[str, Any]] | None = None,
              revision: int = 1) -> dict[str, Any]:
    doc = {
        "id": TARGET, "tenant_id": "eng", "name": "Shop", "revision": revision,
        "repository": "https://github.com/example-org/shop", "repo_id": "repo_0000000000000000",
        "screens": [{"id": "home", "path": "/"}],
        "environments": [entry("staging", staging_base, environment="non_production"), entry("production", SHOP)],
    }
    if flows is not None:
        doc["flows"] = flows
    db.docs[f"qa_targets/{TARGET}"] = doc
    return doc


def legs_secret() -> dict[str, dict[str, str]]:
    """Both legs' entries, each value made at run time."""
    return {name: {"username": "qa-" + secrets.token_hex(4), "password": "pw-" + secrets.token_hex(8),
                   "challenge_bypass": "bp-" + secrets.token_hex(12)} for name in ("staging", "production")}


def mark(db: Any, task: dict[str, Any], *, environment: str | None, run_environment: str | None = "same",
         revision: int = 1, step_id: str | None = None) -> None:
    stamp(db, task, revision=revision, step_id=step_id)
    if environment is not None:
        task["metadata"]["qa_target"]["environment"] = environment
    recorded = environment if run_environment == "same" else run_environment
    if recorded is not None:
        db.docs[f"qa_target_runs/{WORKFLOW}"]["environment"] = recorded


def test_an_agent_marked_for_staging_is_served_staging_and_nothing_of_production(
        client, ldb, store, running, caplog):
    caplog.set_level(logging.DEBUG)
    attempt, doc = running
    seed_legs(ldb)
    values = legs_secret()
    store.values["eng"] = {f"{TARGET}/{name}": value for name, value in values.items()}
    mark(ldb, doc, environment="staging")
    response = ask(client, attempt)
    assert response.status_code == 200, response.text
    body = response.json()
    # MUTATION: serve entries[-1] whatever the mark names and this is shop.example.com.
    assert body["base_url"] == STAGING and body["origins"] == [STAGING]
    assert body["environment"] == "non_production" and body["environment_name"] == "staging"
    assert body["sign_in"]["login_url"] == f"{STAGING}/staging-login"
    assert (body["sign_in"]["username"], body["sign_in"]["password"]) == (
        values["staging"]["username"], values["staging"]["password"])
    assert body["challenge_bypass"]["value"] == values["staging"]["challenge_bypass"]
    production = values["production"]
    for value in (production["password"], production["challenge_bypass"], production["username"], SHOP,
                  "shop.example.com"):
        # MUTATION: answer every entry's settings in `_answer` and this fails.
        assert value not in response.text
    for value in (*values["staging"].values(), *production.values()):
        # MUTATION: log the answer in `issue` and this fails.
        assert value not in caplog.text
        # MUTATION: put the answer in the live_audit row and this fails.
        assert value not in json.dumps(ldb.docs, default=str)
    (row,) = issued_rows(ldb)
    assert row["detail"]["environment"] == "staging" and row["detail"]["target_id"] == TARGET


def test_an_agent_marked_for_production_is_served_production(client, ldb, store, running):
    attempt, doc = running
    seed_legs(ldb)
    values = legs_secret()
    store.values["eng"] = {f"{TARGET}/{name}": value for name, value in values.items()}
    mark(ldb, doc, environment="production")
    body = ask(client, attempt).json()
    # MUTATION: serve entries[0] whatever the mark names and this is staging.
    assert body["base_url"] == SHOP and body["origins"] == [SHOP]
    assert body["sign_in"]["password"] == values["production"]["password"]
    assert values["staging"]["password"] not in json.dumps(body)


@pytest.mark.parametrize("environment, run_environment", [
    (None, None),            # a nameless mark on a target of two entries
    ("qa", "qa"),            # a mark naming no entry
    ("staging", "production"),  # the run record names another leg
])
def test_a_mark_that_does_not_name_one_entry_answers_as_missing(
        client, ldb, store, running, environment, run_environment):
    attempt, doc = running
    seed_legs(ldb)
    store.values["eng"] = {f"{TARGET}/{name}": value for name, value in legs_secret().items()}
    mark(ldb, doc, environment=environment, run_environment=run_environment)
    response = ask(client, attempt)
    # MUTATION: fall back to the first entry for a nameless mark and the first case is 200.
    # MUTATION: drop the run record's `environment` comparison in `_run_names` and the third is 200.
    assert response.status_code == 404, response.text
    assert issued_rows(ldb) == []


def test_mutating_is_false_on_a_non_production_entry_sharing_an_origin_with_production(
        client, ldb, store, running):
    attempt, doc = running
    # AI Studio's case (F-1): both legs on one origin, the chooser's toggle picks the backend.
    seed_legs(ldb, staging_base=SHOP, flows=FLOWS)
    store.values["eng"] = {f"{TARGET}/{name}": value for name, value in legs_secret().items()}
    mark(ldb, doc, environment="staging", step_id="flow-checkout")
    body = ask(client, attempt).json()
    assert body["environment"] == "non_production"
    # MUTATION: drop the shared-origin rule from `_mutating` and this is True.
    assert body["mutating"] is False


def test_mutating_is_true_on_a_non_production_entry_on_its_own_origin(client, ldb, store, running):
    attempt, doc = running
    seed_legs(ldb, flows=FLOWS)
    store.values["eng"] = {f"{TARGET}/{name}": value for name, value in legs_secret().items()}
    mark(ldb, doc, environment="staging", step_id="flow-checkout")
    # The control: the rule turns the permission off only where an origin is shared.
    # MUTATION: answer False for every entry of a target with a production entry and this fails.
    assert ask(client, attempt).json()["mutating"] is True


def test_mutating_is_false_on_the_production_entry(client, ldb, store, running):
    attempt, doc = running
    seed_legs(ldb, flows=FLOWS)
    store.values["eng"] = {f"{TARGET}/{name}": value for name, value in legs_secret().items()}
    mark(ldb, doc, environment="production", step_id="flow-checkout")
    # MUTATION: read the environment of entries[0] in `_mutating` and this is True.
    assert ask(client, attempt).json()["mutating"] is False


def test_a_run_started_before_the_migration_is_refused(client, ldb, store, running):
    attempt, doc = running
    seed_target(ldb)
    stamp(ldb, doc)
    store.values["eng"] = {TARGET: {"username": "qa-" + secrets.token_hex(4), "password": "pw-" + secrets.token_hex(8)}}
    assert ask(client, attempt).status_code == 200
    assert QaTargets(ldb).migrate(TARGET, by="root@example.com") is True
    response = ask(client, attempt)
    # MUTATION: keep the revision in `migrate` and the old run is served the migrated target.
    assert response.status_code == 409, response.text


def test_a_migrated_entry_reads_the_legacy_key_until_its_next_write(client, ldb, store, running):
    attempt, doc = running
    seed_target(ldb)
    legacy = {"username": "qa-" + secrets.token_hex(4), "password": "pw-" + secrets.token_hex(8)}
    store.values["eng"] = {TARGET: dict(legacy)}
    targets = QaTargets(ldb)
    targets.migrate(TARGET, by="root@example.com")
    mark(ldb, doc, environment="production", revision=2)
    body = ask(client, attempt).json()
    # MUTATION: read `<target_id>/<name>` for a legacy entry and this is 503.
    assert body["sign_in"]["password"] == legacy["password"]
    link = targets.mint_link("eng", TARGET, email="root@example.com", console=None, environment="production")
    token = link["path"].split("t=", 1)[1].split("&", 1)[0]
    fresh = ("qa-" + secrets.token_hex(4), "pw-" + secrets.token_hex(8))
    targets.set_credential("eng", TARGET, username=fresh[0], password=fresh[1], link_token=token,
                           by="root@example.com", credentials=store, environment="production")
    assert set(store.values["eng"]) == {f"{TARGET}/production"}
    body = ask(client, attempt).json()
    # MUTATION: keep reading the legacy key after the write and this is the old password.
    assert body["sign_in"]["password"] == fresh[1]
