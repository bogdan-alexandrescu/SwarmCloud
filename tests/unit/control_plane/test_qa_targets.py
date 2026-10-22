"""QA targets: registration, the host rule, the host approval and `:run`
(docs/design/visual-qa-any-app.md §1, §1.2, §5, §6; lane VQ-2 TARGETS of
docs/design/agent-autonomy.md §6, part of #1933).

What these tests hold, over the real app and the in-memory Firestore:

  * a member cannot register, change or delete a target, and nothing is
    written when they try; a workspace admin (a personal workspace's person,
    a platform admin) can, and each change's `admin_audit` row commits with it;
  * `http`, an IP literal, userinfo, an internal or single-label host, and a
    host that resolves to a private address are each refused, and the
    internal-suffix list is swarm_egress.policy's;
  * a repository that is not one of the workspace's registrations is refused;
  * a new target is `pending`; a platform admin, and no one else, approves
    the host they name; changing the host (or the login's other origin)
    resets the approval, and changing anything else keeps it;
  * `:run` on a pending host is 409 `qa_target_host_pending`; with the
    registration's `agents_may_file_issues` off it is 403 `issue_writes_off`
    unless `file: "none"`; a run's browser step tasks carry
    `metadata.qa_target = {id, revision}`, written by swarm-api, and no step's
    input carries the target;
  * another workspace's target answers exactly as a missing one.

The resolver is a fake: no test here touches DNS.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from swarm_api import qatargets
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context

from swarm_api.groups import StaticGroups
from swarm_api.metrics import ApiMetrics
from swarm_api.repositories import repo_id_for
from swarm_api.waker import NullWaker
from swarm_common.identity import tenant_id_for_user

from .conftest import NoForgeTokens, api_settings, auth_header, seed_tenant

CAROL = "carol@example.com"
CAROL_TENANT = tenant_id_for_user(CAROL)
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
ENG_WIDGETS = repo_id_for("eng", "example-org", "widgets")
CAROL_SHOP = repo_id_for(CAROL_TENANT, "example-org", "shop")
RESEARCH_NOTES = repo_id_for("research", "example-org", "notes")
#: A public unicast address the fake resolver answers with.
PUBLIC = "93.184.215.14"
TARGETS = "/v1/qa-targets"
CONSOLE = "https://console.example.com"


#: The deployment's pinned model per class (WORKER_CLASS_MODELS): the rendered
#: QA steps name haiku, sonnet and opus, and a class with no pin is refused at
#: submission. Model ids only, built for the test.
CLASS_MODELS = {name: f"claude-{name}-test" for name in ("opus", "fable", "sonnet", "haiku")}


@pytest.fixture
def api_context(db, tokens, group_map, objects):
    """conftest's context, with every model class pinned."""
    return build_context(
        settings=api_settings(worker_class_models=dict(CLASS_MODELS), console_url=CONSOLE),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=NoForgeTokens(),
    )


class FakeResolver:
    """host -> addresses; an unknown host does not resolve."""

    def __init__(self) -> None:
        self.answers: dict[str, list[str]] = {}
        self.asked: list[str] = []

    def __call__(self, host: str) -> list[str]:
        self.asked.append(host)
        if host not in self.answers:
            raise OSError("no such host")
        return self.answers[host]


def seed_repositories(db: Any) -> None:
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    seed_tenant(db, CAROL_TENANT)
    db.docs[f"tenants/{CAROL_TENANT}"].update({"kind": "user", "principal": CAROL})
    for tenant_id, repo_id, repo in (("eng", ENG_WIDGETS, "widgets"),
                                     (CAROL_TENANT, CAROL_SHOP, "shop"),
                                     ("research", RESEARCH_NOTES, "notes")):
        db.docs[f"repositories/{repo_id}"] = {
            "repo_id": repo_id, "tenant_id": tenant_id, "owner": "example-org", "repo": repo,
            "default_branch": "main", "created_at": NOW, "index": {},
        }


@pytest.fixture
def resolver(client) -> FakeResolver:
    fake = FakeResolver()
    for host in ("app.example.com", "shop.example.com", "auth.example.com", "new.example.com"):
        fake.answers[host] = [PUBLIC]
    client.app.state.qa_target_resolver = fake
    return fake


@pytest.fixture
def seeded(db, resolver):
    seed_repositories(db)
    return db


def target_body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "Shop",
        "base_url": "https://shop.example.com",
        "repository": "example-org/shop",
        "screens": [{"id": "home", "path": "/"}, {"id": "pricing", "path": "/pricing"}],
        "authorized_by": "The shop's owner, by e-mail on 2026-10-22",
    }
    body.update(over)
    return body


def register(client, user: str = "carol", **over: Any):
    return client.post(TARGETS, json=target_body(**over), headers=auth_header(user))


def audit_rows(db: Any) -> list[dict[str, Any]]:
    return [doc for path, doc in db.docs.items() if path.startswith("admin_audit/")]


def target_docs(db: Any) -> dict[str, dict[str, Any]]:
    return {path: doc for path, doc in db.docs.items() if path.startswith("qa_targets/")}


def approve(client, target_id: str, host: str = "shop.example.com", user: str = "root", **extra: Any):
    return client.post(f"{TARGETS}/{target_id}:approve-host", json={"host": host, **extra},
                       headers=auth_header(user))


def allow_issues(db: Any, repo_id: str = CAROL_SHOP) -> None:
    db.docs[f"repositories/{repo_id}"]["agents_may_file_issues"] = True


# --------------------------------------------------------------------------
# Who registers
# --------------------------------------------------------------------------


def test_a_member_cannot_register_and_nothing_is_written(client, db, seeded):
    response = client.post(TARGETS, json=target_body(repository="example-org/widgets"),
                           headers=auth_header("alice"))
    # MUTATION: drop `require_workspace_admin` from `create_target` and this is 201.
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"
    assert target_docs(db) == {}
    assert audit_rows(db) == []


def test_a_member_cannot_change_or_delete_a_target(client, db, seeded):
    created = register(client, "root", repository="example-org/widgets").json()["qa_target"]
    before = dict(db.docs[f"qa_targets/{created['id']}"])
    # MUTATION: drop `require_workspace_admin` from `patch_target` and this is 200.
    response = client.patch(f"{TARGETS}/{created['id']}", json={"name": "Renamed"},
                            headers=auth_header("alice"))
    assert response.status_code == 403, response.text
    # MUTATION: drop `require_workspace_admin` from `delete_target` and this is 204.
    response = client.delete(f"{TARGETS}/{created['id']}", headers=auth_header("alice"))
    assert response.status_code == 403, response.text
    assert db.docs[f"qa_targets/{created['id']}"] == before
    # CONTROL: the member reads it.
    assert client.get(f"{TARGETS}/{created['id']}", headers=auth_header("alice")).status_code == 200


def test_a_personal_workspaces_person_registers_pending_and_audited(client, db, seeded):
    response = register(client)
    assert response.status_code == 201, response.text
    target = response.json()["qa_target"]
    # MUTATION: create the document with host_state approved and this fails.
    assert target["host_state"] == "pending"
    assert target["host"] == "shop.example.com"
    assert target["repository"] == "https://github.com/example-org/shop"
    assert target["credential"] == {"set": False, "set_at": None, "set_by": None, "version": None}
    rows = audit_rows(db)
    # MUTATION: drop the `_audit` call from `QaTargets.create` and this is [].
    assert [r["action"] for r in rows] == ["qa_target_created"]
    assert rows[0]["by"] == CAROL and rows[0]["target_tenant_id"] == CAROL_TENANT
    assert rows[0]["detail"]["host"] == "shop.example.com"


def test_a_registration_and_its_audit_row_commit_together(client, db, seeded, monkeypatch):
    original = qatargets._audit

    def failing(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(qatargets, "_audit", failing)
    response = register(client)
    assert response.status_code == 500
    # MUTATION: write the target outside the transaction and it survives here.
    assert target_docs(db) == {}
    assert audit_rows(db) == []


def test_a_platform_admin_registers_in_a_group_workspace(client, db, seeded):
    response = register(client, "root", repository="example-org/widgets")
    assert response.status_code == 201, response.text
    assert response.json()["qa_target"]["tenant_id"] == "eng"


def test_the_name_is_unique_in_the_workspace(client, db, seeded):
    assert register(client).status_code == 201
    response = register(client, name="shop")
    # MUTATION: drop the `_name_taken` check and this is 201.
    assert response.status_code == 409, response.text


# --------------------------------------------------------------------------
# The host rule (§1)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("base_url", [
    "http://shop.example.com",
    "https://93.184.215.14",
    "https://[2606:4700::6810:85e5]",
    "https://user@shop.example.com",
    "https://user:pw@shop.example.com",
    "https://shop.example.com/?q=1",
], ids=["http", "ipv4", "ipv6", "userinfo", "userinfo-password", "query"])
def test_a_base_url_that_is_not_a_plain_https_host_is_refused(client, db, seeded, base_url):
    response = register(client, base_url=base_url)
    # MUTATION: skip `qaspec.base_url` in `_build` and http or an IP literal is 201.
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["field"] == "base_url"
    assert target_docs(db) == {}


@pytest.mark.parametrize("host", [
    "localhost", "intranet", "metadata.google.internal", "api.svc", "db.cluster.local",
    "printer.lan", "nas.home.arpa",
])
def test_an_internal_host_is_refused_before_it_is_resolved(client, db, seeded, resolver, host):
    resolver.answers[host] = [PUBLIC]
    response = register(client, base_url=f"https://{host}")
    # MUTATION: drop the INTERNAL_SUFFIXES test in `check_public_host` and these are 201.
    assert response.status_code == 422, response.text
    assert host not in resolver.asked


@pytest.mark.parametrize("address", ["10.0.0.7", "127.0.0.1", "169.254.169.254", "192.168.1.1",
                                     "100.64.0.1", "fd00::1", "224.0.0.1"])
def test_a_host_resolving_to_a_private_address_is_refused(client, db, seeded, resolver, address):
    resolver.answers["shop.example.com"] = [PUBLIC, address]
    response = register(client)
    # MUTATION: check only the first resolved address and these are 201.
    assert response.status_code == 422, response.text
    assert "private" in response.json()["message"]


def test_a_host_that_does_not_resolve_is_refused(client, db, seeded, resolver):
    del resolver.answers["shop.example.com"]
    response = register(client)
    # MUTATION: treat a resolver error as "no addresses, nothing private" and this is 201.
    assert response.status_code == 422, response.text


def test_an_extra_origin_is_held_to_the_same_rule(client, db, seeded, resolver):
    sign_in = {"kind": "own_login", "login_path": "/login", "signed_in_selector": "nav .me",
               "extra_origin": "https://sso.internal"}
    response = register(client, sign_in=sign_in)
    # MUTATION: store `extra_origin` unchecked and this is 201.
    assert response.status_code == 422, response.text


def test_the_internal_suffixes_are_swarm_egress_policys():
    app = str(Path(__file__).resolve().parents[3] / "apps" / "swarm-egress")
    if app not in sys.path:
        sys.path.insert(0, app)
    from swarm_egress import policy

    # MUTATION: drop a suffix from qatargets.INTERNAL_SUFFIXES and this fails.
    assert qatargets.INTERNAL_SUFFIXES == policy.INTERNAL_SUFFIXES


def test_an_unregistered_repository_is_refused(client, db, seeded):
    response = register(client, repository="example-org/elsewhere")
    # MUTATION: skip the `Repositories.find` check in `_repository` and this is 201.
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["field"] == "repository"
    # Another workspace's registration is not this one's either.
    assert register(client, repository="example-org/notes").status_code == 422


def test_an_own_login_without_a_signed_in_selector_is_refused(client, db, seeded):
    response = register(client, sign_in={"kind": "own_login", "login_path": "/login"})
    # MUTATION: make `signed_in_selector` optional in `_sign_in` and this is 201.
    assert response.status_code == 422, response.text


def test_a_mutating_flow_on_a_production_target_is_refused_at_registration(client, db, seeded):
    flows = [{"id": "buy", "goal": "buy a plan", "start_path": "/pricing", "mutating": True}]
    response = register(client, flows=flows)
    # MUTATION: drop `check_spec` from `_build` and this is 201, refused only at :run.
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "qa_mutating_flow_on_production"


# --------------------------------------------------------------------------
# The approval (§1.2, VQ-Q1)
# --------------------------------------------------------------------------


def test_only_a_platform_admin_approves_and_it_is_audited(client, db, seeded):
    target = register(client).json()["qa_target"]
    # carol administers her own workspace, not the platform.
    response = approve(client, target["id"], user="carol")
    assert response.status_code == 403, response.text
    assert db.docs[f"qa_targets/{target['id']}"]["host_state"] == "pending"
    response = approve(client, target["id"])
    assert response.status_code == 200, response.text
    approved = response.json()["qa_target"]
    assert approved["host_state"] == "approved"
    assert approved["approved_by"] == "root@example.com"
    rows = [r for r in audit_rows(db) if r["action"] == "qa_target_host_approved"]
    # MUTATION: drop the `_audit` call from `approve_host` and this is [].
    assert len(rows) == 1
    assert rows[0]["detail"]["host"] == "shop.example.com"
    assert rows[0]["detail"]["registrant"] == CAROL


def test_an_approval_naming_another_host_approves_nothing(client, db, seeded):
    target = register(client).json()["qa_target"]
    response = approve(client, target["id"], host="other.example.com")
    # MUTATION: ignore the named host in `approve_host` and this is 200.
    assert response.status_code == 409, response.text
    assert db.docs[f"qa_targets/{target['id']}"]["host_state"] == "pending"


def test_an_extra_origin_is_approved_only_when_named(client, db, seeded):
    sign_in = {"kind": "own_login", "login_path": "/login", "signed_in_selector": "nav .me",
               "extra_origin": "https://auth.example.com"}
    target = register(client, sign_in=sign_in).json()["qa_target"]
    approved = approve(client, target["id"]).json()["qa_target"]
    # MUTATION: approve every declared origin with the host and this lists two.
    assert approved["approved_origins"] == ["https://shop.example.com"]
    approved = approve(client, target["id"], extra_origin="https://auth.example.com").json()["qa_target"]
    assert approved["approved_origins"] == ["https://shop.example.com", "https://auth.example.com"]


def test_changing_the_host_resets_the_approval(client, db, seeded):
    target = register(client).json()["qa_target"]
    assert approve(client, target["id"]).status_code == 200
    response = client.patch(f"{TARGETS}/{target['id']}", json={"base_url": "https://new.example.com"},
                            headers=auth_header("carol"))
    assert response.status_code == 200, response.text
    changed = response.json()["qa_target"]
    # MUTATION: drop the `host_reset` update in `QaTargets.patch` and this stays approved.
    assert changed["host_state"] == "pending"
    assert changed["approved_by"] is None and changed["approved_origins"] == []
    assert changed["revision"] == 2
    row = [r for r in audit_rows(db) if r["action"] == "qa_target_changed"][0]
    assert row["detail"]["host_reset"] is True and "base_url" in row["detail"]["fields"]


def test_changing_the_extra_origin_resets_the_approval(client, db, seeded):
    sign_in = {"kind": "own_login", "login_path": "/login", "signed_in_selector": "nav .me",
               "extra_origin": "https://auth.example.com"}
    target = register(client, sign_in=sign_in).json()["qa_target"]
    approve(client, target["id"], extra_origin="https://auth.example.com")
    moved = {**sign_in, "extra_origin": "https://app.example.com"}
    changed = client.patch(f"{TARGETS}/{target['id']}", json={"sign_in": moved},
                           headers=auth_header("carol")).json()["qa_target"]
    # MUTATION: compare only the host in `QaTargets.patch` and this stays approved.
    assert changed["host_state"] == "pending"


def test_changing_anything_else_keeps_the_approval(client, db, seeded):
    target = register(client).json()["qa_target"]
    approve(client, target["id"])
    changed = client.patch(f"{TARGETS}/{target['id']}",
                           json={"base_url": "https://shop.example.com/app", "name": "Shop app"},
                           headers=auth_header("carol")).json()["qa_target"]
    # CONTROL for the two tests above: a path on the same host is not a new host.
    assert changed["host_state"] == "approved"
    assert changed["name"] == "Shop app"


def test_a_delete_is_audited(client, db, seeded):
    target = register(client).json()["qa_target"]
    response = client.delete(f"{TARGETS}/{target['id']}", headers=auth_header("carol"))
    assert response.status_code == 204, response.text
    assert target_docs(db) == {}
    # MUTATION: drop the `_audit` call from `QaTargets.delete` and this fails.
    assert [r["action"] for r in audit_rows(db)][-1] == "qa_target_deleted"


def test_another_workspaces_target_answers_as_a_missing_one(client, db, seeded):
    target = register(client).json()["qa_target"]
    theirs = client.get(f"{TARGETS}/{target['id']}", headers=auth_header("alice"))
    missing = client.get(f"{TARGETS}/qat_{'0' * 16}", headers=auth_header("alice"))
    # MUTATION: drop the tenant check in `QaTargets.find` and the first is 200.
    assert theirs.status_code == missing.status_code == 404
    assert theirs.json() == missing.json()
    assert client.get(TARGETS, headers=auth_header("alice")).json()["qa_targets"] == []


# --------------------------------------------------------------------------
# :run (§5, §6)
# --------------------------------------------------------------------------


def run(client, target_id: str, user: str = "carol", **body: Any):
    return client.post(f"{TARGETS}/{target_id}:run", json=body, headers=auth_header(user))


def test_a_run_on_a_pending_host_is_refused(client, db, seeded):
    allow_issues(db)
    target = register(client).json()["qa_target"]
    response = run(client, target["id"])
    # MUTATION: drop the host_state check in `runnable` and this is 201.
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "qa_target_host_pending"
    assert not [p for p in db.docs if p.startswith("workflows/")]


def test_a_run_that_files_with_the_flag_off_is_issue_writes_off(client, db, seeded):
    target = register(client).json()["qa_target"]
    approve(client, target["id"])
    response = run(client, target["id"])
    # MUTATION: skip the `agents_may_file_issues_of` check in `runnable` and this is 201.
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "issue_writes_off"
    assert "agents_may_file_issues" in response.json()["message"]
    assert not [p for p in db.docs if p.startswith("workflows/")]


def test_a_report_only_run_needs_no_flag_and_stamps_its_browser_steps(client, db, seeded):
    target = register(client).json()["qa_target"]
    approve(client, target["id"])
    response = run(client, target["id"], file="none")
    # CONTROL for the test above: `file: "none"` is the documented way past the flag.
    assert response.status_code == 201, response.text
    answer = response.json()
    assert answer["qa_target"] == {"id": target["id"], "revision": 1}
    tasks = [d for p, d in db.docs.items() if p.startswith("tasks/")
             and d.get("workflow_id") == answer["workflow_id"]]
    assert tasks
    stamped = {t["step_id"] for t in tasks if (t.get("metadata") or {}).get("qa_target")}
    # MUTATION: stamp no step in `render_run` and this is empty.
    assert stamped == set(answer["browser_steps"]) == {"capture-g1"}
    for task in tasks:
        if task["step_id"] in stamped:
            assert task["metadata"]["qa_target"] == {"id": target["id"], "revision": 1}
        else:
            # MUTATION: stamp every step and a review step carries the mark.
            assert "qa_target" not in (task.get("metadata") or {})
        # §1.1: the target travels as metadata, never in a step's input.
        assert set(task["input"]) <= {"prompt", "browser_tool"}
        assert "file" not in {t["step_id"] for t in tasks}
    run_doc = db.docs[f"qa_target_runs/{answer['workflow_id']}"]
    # MUTATION: drop `record_run` and the worker's route serves this run nothing.
    assert run_doc["target_id"] == target["id"] and run_doc["tenant_id"] == CAROL_TENANT
    assert sorted(run_doc["task_ids"]) == sorted(t["id"] for t in tasks if t["step_id"] in stamped)


def test_a_run_with_the_flag_on_files(client, db, seeded):
    allow_issues(db)
    target = register(client).json()["qa_target"]
    approve(client, target["id"])
    response = run(client, target["id"])
    assert response.status_code == 201, response.text
    steps = {d["step_id"] for p, d in db.docs.items() if p.startswith("tasks/")
             and d.get("workflow_id") == response.json()["workflow_id"]}
    assert "file" in steps


def test_a_run_cannot_name_a_target_field(client, db, seeded):
    target = register(client).json()["qa_target"]
    approve(client, target["id"])
    response = run(client, target["id"], file="none", base_url="https://elsewhere.example.com")
    # Invariant 10: a caller names the target and nothing about it.
    assert response.status_code == 422, response.text


def test_a_run_of_another_workspaces_target_is_404(client, db, seeded):
    target = register(client).json()["qa_target"]
    approve(client, target["id"])
    response = run(client, target["id"], user="alice", file="none")
    # MUTATION: read the target by id alone in `runnable` and this starts carol's run as alice.
    assert response.status_code == 404, response.text


def test_a_run_body_is_logged_nowhere_as_a_prompt(client, db, seeded):
    target = register(client).json()["qa_target"]
    approve(client, target["id"])
    answer = run(client, target["id"], file="none").json()
    # The answer names the run, the mark and the steps: not the rendered spec.
    assert set(answer) == {"workflow_id", "qa_target", "file", "browser_steps"}
    assert "shop.example.com" not in json.dumps(answer)
