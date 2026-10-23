"""A QA target's `environments[]` (docs/design/qa-environments.md §1.1-§1.4,
lane QAENV-MODEL of §8; BGD's Q1 (a), 2026-10-23; part of #2136).

What these tests hold, over the real app and the in-memory Firestore:

  * a target stored before this lane (no `environments`) reads as a one-entry
    list, named `production` or `staging` by its old mark, and answers the
    API exactly as it did: no `environments` key, its mark `{id, revision}`;
  * the migration (`POST /v1/qa-targets:migrate`, a platform admin's) moves
    the per-leg fields into one entry, bumps the revision and appends one
    `qa_target_migrated` row; a second call changes nothing;
  * approving one entry's host leaves the other `pending` (§1.3);
  * a credential link is bound to its entry: a link minted for `staging` is
    refused by the PUT for `production`, and is not burned by the attempt;
  * an entry's credential is the secret map's `"<target_id>/<name>"`; the
    legacy key `<target_id>` is read until the entry's next write, which
    removes it; deleting the target removes every key (§1.4);
  * the map is held under 56 KiB: a new entry, or a value, that would pass it
    is refused with a sentence and nothing is written;
  * a run names its leg: the mark is `{id, revision, environment}` and the
    prompts carry that leg's origin, never the other's.

Every fake credential is built at run time.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

import pytest

from swarm_api import qatargets
from swarm_api.qatargets import QaTargets

from .conftest import auth_header
from .test_qa_target_credential import (  # noqa: F401 - fixtures are used by name
    OWN_LOGIN,
    FakeQaCredentials,
    secret_value,
    store,
)
from .test_qa_targets import (  # noqa: F401 - fixtures are used by name
    CAROL,
    CAROL_TENANT,
    TARGETS,
    allow_issues,
    api_context,
    approve,
    audit_rows,
    register,
    resolver,
    seeded,
    target_body,
)

MIGRATE = f"{TARGETS}:migrate"
STAGING_LOGIN = {"kind": "own_login", "login_path": "/sign-in", "signed_in_selector": "nav .me"}


def leg(name: str, base_url: str, **over: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": name, "base_url": base_url}
    entry.update(over)
    return entry


def two_legs(**over: Any) -> list[dict[str, Any]]:
    staging = leg("staging", "https://app.example.com", environment="non_production", sign_in=STAGING_LOGIN)
    production = leg("production", "https://shop.example.com", sign_in=OWN_LOGIN)
    staging.update(over.get("staging") or {})
    production.update(over.get("production") or {})
    return [staging, production]


def register_legs(client, environments: list[dict[str, Any]], user: str = "carol", **over: Any):
    body = target_body(**over)
    body.pop("base_url")
    body["environments"] = environments
    return client.post(TARGETS, json=body, headers=auth_header(user))


def stored(db: Any, target_id: str) -> dict[str, Any]:
    return db.docs[f"qa_targets/{target_id}"]


def entry_of(answer: dict[str, Any], name: str) -> dict[str, Any]:
    (entry,) = [e for e in answer["environments"] if e["name"] == name]
    return entry


def mint(client, target_id: str, environment: str | None = None, user: str = "carol"):
    body = {"environment": environment} if environment is not None else None
    return client.post(f"{TARGETS}/{target_id}/credential-links", json=body, headers=auth_header(user))


def token_of(response) -> str:
    return response.json()["url"].split("t=", 1)[1].split("&", 1)[0]


def put(client, target_id: str, token: str, username: str, password: str, *,
        environment: str | None = None, user: str = "carol"):
    body: dict[str, Any] = {"username": username, "password": password, "link_token": token}
    if environment is not None:
        body["environment"] = environment
    return client.put(f"{TARGETS}/{target_id}/credential", headers=auth_header(user), json=body)


def migrated_rows(db: Any) -> list[dict[str, Any]]:
    return [r for r in audit_rows(db) if r["action"] == "qa_target_migrated"]


# --------------------------------------------------------------------------
# The read of an unmigrated target, and the migration (§1.2)
# --------------------------------------------------------------------------


def test_an_unmigrated_target_reads_as_one_entry_and_answers_as_before(client, db, seeded, resolver):
    target = register(client).json()["qa_target"]
    staged = register(client, name="Shop staging", environment="non_production").json()["qa_target"]
    # Stored as today: the per-leg fields on the document.
    assert "environments" not in stored(db, target["id"])
    assert stored(db, target["id"])["base_url"] == "https://shop.example.com"
    targets = QaTargets(db, resolve=resolver)
    (entry,) = targets.find(CAROL_TENANT, target["id"])["environments"]
    # MUTATION: return the stored document from `QaTargets.find` unchanged and this is a KeyError.
    assert entry["name"] == "production"
    assert entry["base_url"] == "https://shop.example.com" and entry["host_state"] == "pending"
    # MUTATION: name every legacy entry `production` and this fails.
    assert [e["name"] for e in targets.find(CAROL_TENANT, staged["id"])["environments"]] == ["staging"]
    # The API answer of a one-entry, unmigrated target is the one it gave before this lane.
    # MUTATION: add `environments` to every `to_api` answer and this fails.
    assert "environments" not in target
    assert target["host"] == "shop.example.com" and target["host_state"] == "pending"


def test_the_migration_writes_environments_one_audit_row_and_a_new_revision(client, db, seeded):
    target = register(client, sign_in=OWN_LOGIN).json()["qa_target"]
    before = len(audit_rows(db))
    # MUTATION: let `migrate_targets` take `current_auth` and a member migrates.
    assert client.post(MIGRATE, headers=auth_header("carol")).status_code == 403
    response = client.post(MIGRATE, headers=auth_header("root"))
    assert response.status_code == 200, response.text
    assert response.json()["migrated"] == [target["id"]]
    doc = stored(db, target["id"])
    (entry,) = doc["environments"]
    assert entry["name"] == "production" and entry["base_url"] == "https://shop.example.com"
    assert entry["sign_in"]["login_path"] == "/login" and entry["host_state"] == "pending"
    # MUTATION: leave the per-leg fields on the document as well and this fails.
    assert not {"base_url", "host", "origin", "sign_in", "host_state", "credential"} & set(doc)
    # §1.4: the secret is not rewritten; the entry reads its old key until its next write.
    assert entry["legacy_map_key"] is True
    # MUTATION: drop the revision bump from `migrate` and a run started before it is still served.
    assert doc["revision"] == target["revision"] + 1
    rows = migrated_rows(db)
    # MUTATION: drop the `_audit` call from `migrate` and this is [].
    assert len(rows) == 1 and rows[0]["target_qa_target_id"] == target["id"]
    assert rows[0]["detail"] == {"environments": ["production"], "revision": doc["revision"]}
    assert len(audit_rows(db)) == before + 1
    # A second migration finds nothing to do and writes nothing.
    again = client.post(MIGRATE, headers=auth_header("root"))
    # MUTATION: migrate a document that already has `environments` and this names it again.
    assert again.json()["migrated"] == [] and len(migrated_rows(db)) == 1
    assert stored(db, target["id"])["revision"] == doc["revision"]
    # A migrated one-entry target answers its entry beside the flat fields.
    shown = client.get(f"{TARGETS}/{target['id']}", headers=auth_header("carol")).json()["qa_target"]
    assert [e["name"] for e in shown["environments"]] == ["production"]
    assert shown["host"] == "shop.example.com"


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------


@pytest.mark.parametrize("names", [["Staging"], ["a" * 21], ["staging", "staging"], ["st/aging"], [""]])
def test_an_entry_name_is_short_lower_case_and_unique(client, db, seeded, names):
    environments = [leg(name, "https://app.example.com") for name in names]
    response = register_legs(client, environments)
    # MUTATION: drop the name check from `_legs` and this is 201.
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == {"reason": "qa_target_invalid", "field": "environments"}
    assert not [p for p in db.docs if p.startswith("qa_targets/")]
    # The control: the same entry under a good name registers.
    assert register_legs(client, [leg("staging-2", "https://app.example.com")]).status_code == 201


def test_a_per_leg_field_beside_environments_is_refused(client, db, seeded):
    body = target_body()
    body["environments"] = two_legs()
    response = client.post(TARGETS, json=body, headers=auth_header("carol"))
    # MUTATION: let a body carry `base_url` beside `environments` and this is 201.
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == {"reason": "qa_target_invalid", "field": "environments"}
    assert "in each entry" in response.json()["message"]


def test_approving_one_entrys_host_leaves_the_other_pending(client, db, seeded, store):
    target = register_legs(client, two_legs()).json()["qa_target"]
    assert {e["name"]: e["host_state"] for e in target["environments"]} == {
        "staging": "pending", "production": "pending"}
    # On a target of two entries the approval names the entry.
    nameless = approve(client, target["id"], host="app.example.com")
    # MUTATION: approve the first entry when no environment is named and this is 200.
    assert nameless.status_code == 422, nameless.text
    # The host named must be that entry's.
    assert approve(client, target["id"], host="shop.example.com", environment="staging").status_code == 409
    response = approve(client, target["id"], host="app.example.com", environment="staging")
    assert response.status_code == 200, response.text
    answer = response.json()["qa_target"]
    # MUTATION: write the approval onto every entry and production reads approved.
    assert entry_of(answer, "staging")["host_state"] == "approved"
    assert entry_of(answer, "staging")["approved_origins"] == ["https://app.example.com"]
    assert entry_of(answer, "production")["host_state"] == "pending"
    assert entry_of(answer, "production")["approved_origins"] == []
    (row,) = [r for r in audit_rows(db) if r["action"] == "qa_target_host_approved"]
    assert row["detail"]["environment"] == "staging" and row["detail"]["host"] == "app.example.com"


def test_changing_one_entrys_host_resets_that_entry_only(client, db, seeded, store):
    target = register_legs(client, two_legs()).json()["qa_target"]
    approve(client, target["id"], host="app.example.com", environment="staging")
    approve(client, target["id"], host="shop.example.com", environment="production")
    moved = two_legs(staging={"base_url": "https://new.example.com"})
    response = client.patch(f"{TARGETS}/{target['id']}", json={"environments": moved},
                            headers=auth_header("carol"))
    assert response.status_code == 200, response.text
    answer = response.json()["qa_target"]
    # MUTATION: reset every entry's approval on a host change and production reads pending.
    assert entry_of(answer, "production")["host_state"] == "approved"
    assert entry_of(answer, "staging")["host_state"] == "pending"


# --------------------------------------------------------------------------
# The credential (§1.4)
# --------------------------------------------------------------------------


def test_a_link_minted_for_staging_cannot_set_productions_password(client, db, seeded, store):
    target = register_legs(client, two_legs()).json()["qa_target"]
    minted = mint(client, target["id"], "staging")
    assert minted.status_code == 201, minted.text
    token = token_of(minted)
    username, password = secret_value()
    refused = put(client, target["id"], token, username, password, environment="production")
    # MUTATION: drop `environment` from the link comparison in `consume_link` and this is 200.
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "qa_credential_link_invalid"
    assert store.writes == []
    # Not burned by the refused attempt: the entry it was minted for still takes it.
    accepted = put(client, target["id"], token, username, password, environment="staging")
    assert accepted.status_code == 200, accepted.text
    # MUTATION: key the map by target id alone and this is the bare id.
    assert set(store.values[CAROL_TENANT]) == {f"{target['id']}/staging"}
    shown = client.get(f"{TARGETS}/{target['id']}", headers=auth_header("carol")).json()["qa_target"]
    assert entry_of(shown, "staging")["credential"]["set"] is True
    assert entry_of(shown, "production")["credential"]["set"] is False
    assert password not in json.dumps(shown) and password not in json.dumps(db.docs, default=str)


def test_a_link_names_its_entry_when_the_target_has_two(client, db, seeded, store):
    target = register_legs(client, two_legs()).json()["qa_target"]
    # MUTATION: bind a nameless link to the first entry and this is 201.
    assert mint(client, target["id"]).status_code == 422
    assert mint(client, target["id"], "qa").status_code == 422
    assert not [p for p in db.docs if p.startswith("qa_target_credential_links/")]


def test_the_legacy_key_is_read_until_the_entrys_next_write_and_gone_after(client, db, seeded, store):
    target = register(client, sign_in=OWN_LOGIN).json()["qa_target"]
    first = secret_value()
    assert put(client, target["id"], token_of(mint(client, target["id"])), *first).status_code == 200
    # Before the migration the map is keyed by the target id, as it always was.
    assert set(store.values[CAROL_TENANT]) == {target["id"]}
    assert client.post(MIGRATE, headers=auth_header("root")).status_code == 200
    targets = QaTargets(db)
    migrated = targets.get(CAROL_TENANT, target["id"])
    (entry,) = migrated["environments"]
    # MUTATION: ignore `legacy_map_key` in `map_key` and the migrated entry reads a key nobody wrote.
    assert qatargets.map_key(migrated, entry) == target["id"]
    # The next write of that entry moves it to its own key in the same version.
    second = secret_value()
    minted = mint(client, target["id"], "production")
    response = put(client, target["id"], token_of(minted), *second, environment="production")
    assert response.status_code == 200, response.text
    values = store.values[CAROL_TENANT]
    # MUTATION: keep the legacy key after the write and this has two keys.
    assert set(values) == {f"{target['id']}/production"}
    assert values[f"{target['id']}/production"] == {"username": second[0], "password": second[1]}
    (entry,) = stored(db, target["id"])["environments"]
    assert entry["legacy_map_key"] is False
    assert len(store.writes) == 2


def test_deleting_a_target_removes_every_one_of_its_keys(client, db, seeded, store):
    target = register_legs(client, two_legs()).json()["qa_target"]
    for name in ("staging", "production"):
        assert put(client, target["id"], token_of(mint(client, target["id"], name)), *secret_value(),
                   environment=name).status_code == 200
    other = "qat_" + secrets.token_hex(8)
    store.values[CAROL_TENANT][target["id"]] = {"username": "old", "password": secrets.token_hex(8)}
    store.values[CAROL_TENANT][other] = {"username": "theirs", "password": secrets.token_hex(8)}
    response = client.delete(f"{TARGETS}/{target['id']}", headers=auth_header("carol"))
    assert response.status_code == 204, response.text
    # MUTATION: remove only `values[target_id]` in `delete` and the entries' keys outlive it.
    assert set(store.values[CAROL_TENANT]) == {other}


def test_removing_an_entry_removes_its_key(client, db, seeded, store):
    target = register_legs(client, two_legs()).json()["qa_target"]
    for name in ("staging", "production"):
        put(client, target["id"], token_of(mint(client, target["id"], name)), *secret_value(), environment=name)
    response = client.patch(f"{TARGETS}/{target['id']}", json={"environments": two_legs()[1:]},
                            headers=auth_header("carol"))
    assert response.status_code == 200, response.text
    # MUTATION: keep a removed entry's key in `patch` and staging's password outlives it.
    assert set(store.values[CAROL_TENANT]) == {f"{target['id']}/production"}


def _fill(store: FakeQaCredentials, tenant_id: str, room: int) -> None:
    """Fill the tenant's map until `room` bytes are left under the 56 KiB limit."""
    values: dict[str, Any] = {}
    index = 0
    while True:
        key = f"qat_{index:016x}"
        used = len(json.dumps(values, sort_keys=True).encode())
        left = qatargets.SECRET_MAP_MAX_BYTES - used - room
        if left <= 0:
            break
        values[key] = {"username": "u", "password": "p" * max(1, min(1000, left - 60))}
        index += 1
    store.values[tenant_id] = values


def test_a_new_entry_that_would_pass_56_kib_is_refused(client, db, seeded, store):
    target = register(client, sign_in=OWN_LOGIN).json()["qa_target"]
    _fill(store, CAROL_TENANT, room=1000)
    response = client.patch(f"{TARGETS}/{target['id']}",
                            json={"environments": two_legs(production={"name": "production"})},
                            headers=auth_header("carol"))
    # MUTATION: drop `_check_room` from `patch` and this is 200.
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "qa_secret_full"
    assert "environments" not in stored(db, target["id"])
    assert store.writes == []


def test_a_value_that_would_pass_56_kib_is_refused(client, db, seeded, store):
    target = register(client, sign_in=OWN_LOGIN).json()["qa_target"]
    _fill(store, CAROL_TENANT, room=200)
    username = "qa-" + secrets.token_hex(4)
    response = put(client, target["id"], token_of(mint(client, target["id"])), username,
                   secrets.token_hex(400))
    # MUTATION: drop the size check from `_write_values` and the store takes it.
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "qa_secret_full"
    assert store.writes == []
    assert stored(db, target["id"])["credential"]["set"] is False


# --------------------------------------------------------------------------
# The run names its leg (§1.5)
# --------------------------------------------------------------------------


def test_a_run_of_a_two_entry_target_marks_its_leg(client, db, seeded, store):
    public = leg("staging", "https://app.example.com", environment="non_production")
    target = register_legs(client, [public, leg("production", "https://shop.example.com")]).json()["qa_target"]
    approve(client, target["id"], host="app.example.com", environment="staging")
    allow_issues(db)
    nameless = client.post(f"{TARGETS}/{target['id']}:run", json={"file": "none"}, headers=auth_header("carol"))
    # MUTATION: run the first entry when no environment is named and this is 201.
    assert nameless.status_code == 422, nameless.text
    assert nameless.json()["detail"]["reason"] == "qa_target_environment_required"
    # Production's host is pending: its run is refused, staging's starts.
    pending = client.post(f"{TARGETS}/{target['id']}:run", json={"file": "none", "environment": "production"},
                          headers=auth_header("carol"))
    assert pending.status_code == 409 and pending.json()["code"] == "qa_target_host_pending"
    response = client.post(f"{TARGETS}/{target['id']}:run", json={"file": "none", "environment": "staging"},
                           headers=auth_header("carol"))
    assert response.status_code == 201, response.text
    answer = response.json()
    mark = {"id": target["id"], "revision": target["revision"], "environment": "staging"}
    # MUTATION: leave `environment` out of `qa_target_metadata` and this is {id, revision}.
    assert answer["qa_target"] == mark
    tasks = [d for p, d in db.docs.items() if p.startswith("tasks/") and d.get("workflow_id") == answer["workflow_id"]]
    marked = [t for t in tasks if (t.get("metadata") or {}).get("qa_target")]
    assert marked and all(t["metadata"]["qa_target"] == mark for t in marked)
    prompts = " ".join(t["input"]["prompt"] for t in tasks)
    # MUTATION: render from the first entry whatever the run names and this is shop.example.com.
    assert "https://app.example.com" in prompts and "shop.example.com" not in prompts
    run = db.docs[f"qa_target_runs/{answer['workflow_id']}"]
    assert run["environment"] == "staging" and run["target_id"] == target["id"]
