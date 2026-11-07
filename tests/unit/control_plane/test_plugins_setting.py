"""Agent plugins on swarm-api: the closed catalogue, a workspace's allowed list, and a step's plugins at submission.

    GET  /v1/plugins                          the selected workspace's allowed plugins
    GET  /v1/workspaces/{tenant_id}/plugins   the same, with the catalogue
    PUT  /v1/workspaces/{tenant_id}/plugins   `{allowed}`, catalogue names; audited

The owner chose plugins per step, gstack first, unattended until the question
bridge lands (docs/design/agent-plugins.md, 3 November 2026). A step names
them in its work contract's capabilities section; submission accepts only
catalogue names its runner profile loads, in unattended mode, that the
workspace allowed under a signed settings block, and the contract's digest is
signed with the step. Offline: FakeFirestore, a local spec signer.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import plugincatalogue, plugins, workcontract as wc
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.judgment import SettingsMac, block_mac
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.objects import InMemoryObjectReader
from swarm_api.waker import NullWaker

from .conftest import PROJECT, api_settings, auth_header, seed_tenant
from .spec_signer import LocalSpecSigner
from .test_settings_mac import configure, new_key, reference_verifies
from .test_qa_targets import (  # noqa: F401 - fixtures are used by name
    CAROL_TENANT,
    api_context,
    resolver,
    seeded,
)


def _url(tenant_id: str) -> str:
    return f"/v1/workspaces/{tenant_id}/plugins"


def _put(client, tenant_id: str, user: str, body: dict[str, Any]):
    return client.put(_url(tenant_id), json=body, headers=auth_header(user))


def _audit(db: Any) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith("admin_audit/")
            and d.get("action") == plugins.AUDIT_ACTION]


# -- the catalogue ----------------------------------------------------------------


def test_the_catalogue_offers_gstack_pinned_on_the_six_claude_code_profiles_without_cso():
    entry = plugincatalogue.CATALOGUE["gstack"]
    assert entry.version == "1.92.55.0"
    assert entry.install_path == "/opt/swarm/plugins/gstack"
    assert entry.runner_profiles == ("claude-code", "claude-code-large", "claude-code-medium",
                                     "claude-code-review", "claude-code-browser", "claude-code-lightpanda")
    # MUTATION: keep `cso` in the unattended list and this fails.
    assert all("cso" not in skills for skills in entry.skills.values())
    assert "cso" not in wc.PLUGIN_CATALOGUE["gstack"]["unattended"]
    assert wc.PLUGIN_CATALOGUE == plugincatalogue.offered_skills()


def test_a_contract_naming_cso_is_not_a_contract():
    doc = {
        "schema": 1, "contract_id": "wc-sec", "tenant_id": "eng", "revision": 1,
        "strictness": "advisory",
        "requirements": [{"id": "REQ-1", "text": "Review it."}],
        "capabilities": {"plugins": {"gstack": {"mode": "unattended", "skills": ["cso"]}}},
        "limits": {"runner_profile": "claude-code"},
        "ownership": {"owner": "alice@example.com"},
    }
    with pytest.raises(wc.ContractInvalid):
        wc.decode(doc)


def test_the_worker_enforces_plugins_so_a_strict_contract_naming_them_compiles():
    # MUTATION: leave plugins out of WORKER_ENFORCED_TODAY and a strict
    # contract with plugins is refused as not enforceable.
    assert wc.COMPONENT_PLUGINS in wc.WORKER_ENFORCED_TODAY


# -- the workspace's allowed plugins ------------------------------------------------


def test_none_are_allowed_until_set(client, db, seeded):
    response = client.get(_url("eng"), headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["plugins"] == {"allowed": []}
    assert body["defaults"] == {"plugins": {"allowed": []}}
    assert body["can_edit"] is False
    served = {entry["name"]: entry for entry in body["catalogue"]}
    assert sorted(served) == ["gstack", "security-audit"]
    gstack = served["gstack"]
    assert gstack["version"] == "1.92.55.0"
    assert gstack["modes"]["unattended"]["available"] is True
    assert gstack["modes"]["planner"]["available"] is False
    assert "question bridge" in gstack["modes"]["planner"]["reason"]
    # The second plugin (owner, 4 November 2026): one skill, unattended only.
    audit = served["security-audit"]
    assert audit["version"] == "c1c8a8c1471069fb0e188eeaff69b8e8db6564a8"
    assert audit["modes"] == {"unattended": {"skills": ["security-audit"], "available": True}}
    # Never a path from the image, nor how its folders are laid out.
    for entry in served.values():
        assert "install_path" not in entry and "skill_folders" not in entry
    selected = client.get("/v1/plugins", headers={**auth_header("alice"), "X-Swarm-Tenant": "eng"})
    assert selected.status_code == 200, selected.text
    assert selected.json()["plugins"] == {"allowed": []}


def test_an_admin_allows_gstack_signed_and_audited_once(client, db, seeded, api_context):
    key = new_key()
    configure(api_context, settings_key=key)
    response = _put(client, "eng", "root", {"allowed": ["gstack", "gstack"]})
    assert response.status_code == 200, response.text
    assert response.json()["plugins"] == {"allowed": ["gstack"]}
    assert response.json()["can_edit"] is True
    block = db.docs["workspace_settings/eng"]["plugins"]
    assert block["allowed"] == ["gstack"]
    # MUTATION: drop the writer's block_mac and there is nothing to verify.
    assert reference_verifies(key, "eng", "plugins", block)
    assert not reference_verifies(key, "research", "plugins", block)
    (row,) = _audit(db)
    # MUTATION: drop the audit write and this is empty.
    assert row["target_tenant_id"] == "eng" and row["by"] == "root@example.com"
    assert row["detail"] == {"tenant_id": "eng", "setting": "plugins.allowed",
                             "from": [], "to": ["gstack"]}
    # The same list again writes no second row.
    assert _put(client, "eng", "root", {"allowed": ["gstack"]}).status_code == 200
    assert len(_audit(db)) == 1
    # None again is a second row.
    assert _put(client, "eng", "root", {"allowed": []}).json()["plugins"] == {"allowed": []}
    assert len(_audit(db)) == 2


def test_the_write_leaves_the_other_settings_alone(client, db, seeded, api_context):
    configure(api_context, settings_key=new_key())
    db.docs["workspace_settings/eng"] = {"tenant_id": "eng", "quality_gates": {"jev": True},
                                         "qa_flows_per_step": 7}
    assert _put(client, "eng", "root", {"allowed": ["gstack"]}).status_code == 200
    doc = db.docs["workspace_settings/eng"]
    assert doc["quality_gates"] == {"jev": True} and doc["qa_flows_per_step"] == 7


@pytest.mark.parametrize("body", [
    {"allowed": ["gstack", "superpowers"]},
    {"allowed": ["/opt/swarm/plugins/gstack"]},
    {"allowed": "gstack"},
    {"allowed": ["gstack"], "version": "1.0"},
])
def test_anything_but_catalogue_names_is_refused_and_nothing_written(client, db, seeded, body):
    response = _put(client, "eng", "root", body)
    # MUTATION: skip `normalise` and an unknown name or a path is stored.
    assert response.status_code == 422, response.text
    assert "workspace_settings/eng" not in db.docs
    assert _audit(db) == []


def test_a_member_who_is_not_its_admin_is_refused_and_nothing_written(client, db, seeded):
    response = _put(client, "eng", "alice", {"allowed": ["gstack"]})
    # MUTATION: let any member change it and this is 200.
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "role_admin_required"
    assert "workspace_settings/eng" not in db.docs
    assert _audit(db) == []


def test_a_caller_outside_the_workspace_is_answered_not_found(client, db, seeded):
    assert client.get(_url("eng"), headers=auth_header("bob")).status_code == 404
    assert _put(client, "eng", "bob", {"allowed": ["gstack"]}).status_code in (403, 404)
    assert "workspace_settings/eng" not in db.docs


def test_the_owner_of_a_personal_workspace_allows_it(client, db, seeded, api_context):
    configure(api_context, settings_key=new_key())
    response = _put(client, CAROL_TENANT, "carol", {"allowed": ["gstack"]})
    assert response.status_code == 200, response.text
    assert response.json()["plugins"] == {"allowed": ["gstack"]}


def test_an_unsigned_or_forged_list_allows_nothing(client, db, seeded, api_context):
    key = new_key()
    configure(api_context, settings_key=key)
    # A direct Firestore write: no MAC.
    db.docs["workspace_settings/eng"] = {"tenant_id": "eng", "plugins": {"allowed": ["gstack"]}}
    body = client.get(_url("eng"), headers=auth_header("alice")).json()
    # MUTATION: read the block as stored and a forged list is served as allowed.
    assert body["plugins"] == {"allowed": []}
    mac = SettingsMac(keys=(key,))
    assert plugins.allowed_plugins("eng", db.docs["workspace_settings/eng"], mac) == ()
    # Another workspace's signed block, copied over, does not verify here.
    signed = {"allowed": ["gstack"]}
    signed["mac"] = block_mac("research", "plugins", signed, (key,))
    assert plugins.allowed_plugins("eng", {"plugins": signed}, mac) == ()
    assert plugins.allowed_plugins("research", {"plugins": signed}, mac) == ("gstack",)


def test_a_deployment_with_no_key_stores_the_list_unsigned_and_allows_nothing(client, db, seeded):
    assert _put(client, "eng", "root", {"allowed": ["gstack"]}).status_code == 200
    block = db.docs["workspace_settings/eng"]["plugins"]
    assert "mac" not in block
    assert client.get(_url("eng"), headers=auth_header("alice")).json()["plugins"] == {"allowed": []}


# -- a step's plugins at submission ---------------------------------------------------


@pytest.fixture
def settings_key() -> str:
    return new_key()


@pytest.fixture
def sub_client(db, tokens, group_map, settings_key) -> TestClient:
    seed_tenant(db, "eng")
    context = build_context(
        settings=api_settings(settings_key=settings_key), db=db,
        verifier=StaticTokenVerifier(tokens), groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=InMemoryObjectReader(bucket=f"swarm-artifacts-{PROJECT}"),
        signer=LocalSpecSigner(),
    )
    return TestClient(create_app(context), raise_server_exceptions=False)


@pytest.fixture
def switch_on(monkeypatch):
    monkeypatch.setenv(wc.SWITCH_ENV, "on")


def _allow(db, settings_key: str, tenant_id: str = "eng", names=("gstack",)) -> None:
    block: dict[str, Any] = {"allowed": list(names)}
    block["mac"] = block_mac(tenant_id, "plugins", block, (settings_key,))
    db.docs[f"workspace_settings/{tenant_id}"] = {"tenant_id": tenant_id, "plugins": block}


def _contract(profile: str = "claude-code", **plugin: Any) -> dict[str, Any]:
    chosen = {"mode": "unattended", "skills": ["review", "qa"], **plugin}
    return {
        "schema": 1, "contract_id": "wc-review-widgets", "tenant_id": "eng", "revision": 1,
        "strictness": "strict",
        "requirements": [{"id": "REQ-1", "text": "Review the widgets."}],
        "capabilities": {"plugins": {"gstack": chosen}},
        "limits": {"runner_profile": profile},
        "ownership": {"owner": "alice@example.com"},
    }


def _submit(client, contract: dict[str, Any], profile: str = "claude-code"):
    return client.post("/v1/tasks", headers=auth_header("alice"), json={
        "runner_profile": profile, "input": {"prompt": "review"},
        "metadata": {wc.METADATA_KEY: contract}})


def _tasks(db) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith("tasks/") and p.count("/") == 1]


def test_an_allowed_step_stores_its_plugins_signed_with_the_step(
        sub_client, db, switch_on, settings_key):
    _allow(db, settings_key)
    response = _submit(sub_client, _contract())
    assert response.status_code == 201, response.text
    (task,) = _tasks(db)
    stored = task["metadata"][wc.METADATA_KEY]
    assert stored["capabilities"]["plugins"] == {"gstack": {"mode": "unattended",
                                                            "skills": ["qa", "review"]}}
    reference = task["metadata"]["dispatch"][wc.DISPATCH_FIELD]
    # The digest inside the signed dispatch block covers the plugins.
    from swarm_common.workcontract import contract_digest
    assert reference["digest"] == contract_digest(stored)
    assert task["spec_signature"]
    forged = copy.deepcopy(stored)
    forged["capabilities"]["plugins"]["gstack"]["skills"].append("ship")
    assert contract_digest(forged) != reference["digest"]


@pytest.mark.parametrize("why, contract, profile, words", [
    ("not allowed", _contract(), "claude-code", "has not allowed gstack"),
    ("planner", _contract(mode="planner", skills=["spec"]), "claude-code", "question bridge"),
    ("designer", _contract(mode="designer", skills=["design-html"]), "claude-code",
     "question bridge"),
    ("codex", _contract("codex"), "codex", "runs only on the profiles"),
])
def test_a_step_the_platform_cannot_run_is_refused_and_nothing_stored(
        sub_client, db, switch_on, settings_key, why, contract, profile, words):
    if why != "not allowed":
        _allow(db, settings_key)
    response = _submit(sub_client, contract, profile)
    # MUTATION: skip `plugins.check_step` in `_build_task` and each is 201.
    assert response.status_code == 422, response.text
    body = response.json()
    assert words in body["message"]
    assert body["detail"]["plugins"] == "plugin_refused"
    assert _tasks(db) == []


def test_a_forged_allowed_list_is_refused(sub_client, db, switch_on):
    db.docs["workspace_settings/eng"] = {"tenant_id": "eng", "plugins": {"allowed": ["gstack"]}}
    response = _submit(sub_client, _contract())
    assert response.status_code == 422, response.text
    assert "has not allowed gstack" in response.json()["message"]
    assert _tasks(db) == []


def test_a_workflow_step_with_plugins_is_checked_too(sub_client, db, switch_on, settings_key):
    step = {"step_id": "review", "runner_profile": "claude-code",
            "input": {"prompt": "review"},
            "metadata": {wc.METADATA_KEY: _contract(mode="planner", skills=["spec"])}}
    _allow(db, settings_key)
    response = sub_client.post("/v1/workflows", headers=auth_header("alice"), json={
        "on_step_failure": "continue", "strategy": "direct-pr",
        "repository_url": "https://github.com/acme/widgets.git", "steps": [step]})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["step_id"] == "review"
    assert _tasks(db) == []
