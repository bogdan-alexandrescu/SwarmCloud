"""The §4.4 hard stops on an issue run, at plan (docs/schedules.md §4.4-§4.6, lane S5).

What these tests hold, each against the real app and the in-memory Firestore:

  1. A planner plan naming `.github/workflows/` ends the run FAILED with
     WORKFLOWS_PATH: no plan stored, no workflow submitted, and the error the
     status comment carries says why. A plan edit adding such a path is 422.
  2. A plan touching `terraform/bootstrap/` in a `platform: true` repository
     is held NEEDS_OWNER. A member's `plan:approve` -- the route
     `swarm_plan_approve` and `sc plan approve` call -- is 403
     `hold_approver_required`; PLATFORM_OWNER's succeeds.
  3. The same plan elsewhere is held NEEDS_SECOND_MEMBER: its creator's
     approve is 403 and another member's succeeds. A one-person tenant
     approves its own only with the typed confirmation.
  4. A `plan_approval: auto` run with a security-class issue, or an IAM plan
     with the switch on, is NOT auto-approved by the tick: still PLANNED,
     holding its hold, nothing submitted.
  5. An edit that removes the held path leaves the hold in place.
  6. Report-only: with the switch off an IAM hold is recorded and logged and
     the approval goes ahead; the security-class hold is enforced anyway.

The merge point's half (a held run's auto_merge waits for a `merge`
approval) is in test_approvals.py, with the inbox. No credentials, no
network, no emulator.
"""

from __future__ import annotations

import copy
import json
import logging

import pytest
from fastapi.testclient import TestClient

from swarm_api import forge, forgewrite, issueruns, refusals
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.repositories import repo_id_for
from swarm_api.waker import NullWaker

from . import forge_fakes
from .conftest import ENG_GROUP, api_settings, auth_header, seed_grant
from .test_issue_runs import PLAN, _create, _docs, _finish_planner, _run

OWNER = "root@saga.xyz"
SWITCH = refusals.env_name("hold_approver_required")
SWEEPER = "swarm-rollup-sweeper@saga-agents-staging.iam.gserviceaccount.com"


def _plan(*files: str, prompt: str = "Change what the issue asks.") -> dict:
    plan = copy.deepcopy(PLAN)
    plan["steps"][0]["files"] = list(files)
    plan["steps"][0]["prompt"] = prompt
    return plan


BOOTSTRAP_PLAN = _plan("terraform/bootstrap/main.tf")
WORKFLOW_PLAN = _plan(".github/workflows/ci.yml", "src/widgets/list.py")
PLAIN_PLAN = _plan("src/widgets/list.py")


# --------------------------------------------------------------------------
# fixtures: a second eng member, PLATFORM_OWNER, and the switch
# --------------------------------------------------------------------------

@pytest.fixture
def group_map():
    return {
        "alice@saga.xyz": (ENG_GROUP,),
        "dave@saga.xyz": (ENG_GROUP,),
        "carol@saga.xyz": (),
        # PLATFORM_OWNER, and a member of eng: the owner approves NEEDS_OWNER
        # holds as a member of the platform's tenant, never across tenants.
        OWNER: ("swarm-admins@saga.xyz", ENG_GROUP),
    }


@pytest.fixture
def labels():
    return []


ISSUE_URL = "https://api.github.com/repos/saga-xyz/widgets/issues/42"


class _Forge:
    """The open-work lists from `forge_fakes.GitHub`, and the one issue's read
    (`forge.preview`), whose labels are what makes an issue security-class."""

    def __init__(self, labels: list[str]) -> None:
        self.lists = forge_fakes.GitHub(issues=[forge_fakes.issue(42)], pulls=[], files={})
        self.issue = {
            "number": 42, "title": "Widgets cannot be sorted", "body": "Steps: open the list.\n",
            "state": "open", "comments": 0, "labels": [{"name": name} for name in labels],
            "html_url": "https://github.com/saga-xyz/widgets/issues/42",
        }

    def __call__(self, url: str, headers: dict[str, str], timeout: float):
        if url == ISSUE_URL:
            return 200, json.dumps(self.issue).encode()
        return self.lists(url, headers, timeout)


@pytest.fixture
def github(labels):
    return _Forge(labels)


@pytest.fixture
def api_context(db, tokens, group_map, objects, github):
    tokens = dict(tokens)
    tokens["token-sweeper"] = {"email": SWEEPER, "email_verified": True, "sub": "sub-sweeper"}
    return build_context(
        settings=api_settings(platform_owner=OWNER, rollup_sweeper_users=(SWEEPER,)),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=forge_fakes.AnyTenantTokens(),
        forge=forge.GitHubIssues(send=github),
        forge_writer=forgewrite.GitHubWriter(send=forge_fakes.GitHubWrites()),
    )


@pytest.fixture
def client(api_context) -> TestClient:
    return TestClient(create_app(api_context), raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _grants(db):
    from .conftest import TEST_REPOSITORIES, grant_members

    grant_members(db, *TEST_REPOSITORIES)
    for email in ("dave@saga.xyz", OWNER):
        seed_grant(db, "eng", email, "saga-xyz/widgets")


@pytest.fixture
def switch_on(monkeypatch):
    monkeypatch.setenv(SWITCH, "on")


def _register(db, *, platform: bool, hard_stop_paths: list[str] | None = None, tenant_id: str = "eng") -> None:
    repo_id = repo_id_for(tenant_id, "saga-xyz", "widgets")
    db.docs[f"repositories/{repo_id}"] = {
        "repo_id": repo_id, "tenant_id": tenant_id, "owner": "saga-xyz", "repo": "widgets",
        "platform": platform, **({"hard_stop_paths": hard_stop_paths} if hard_stop_paths else {}),
    }


def _planned(client, db, objects, plan, user="alice", **body) -> dict:
    created = _create(client, user, **body)
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    _finish_planner(db, objects, run, plan)
    read = _run(client, run["id"], user)
    assert read.status_code == 200, read.text
    return read.json()["run"]


def _approve(client, run_id, digest, user="alice", confirm=None):
    url = f"/v1/runs/{run_id}/plan:approve"
    if confirm is not None:
        url += "?" + __import__("urllib.parse").parse.urlencode({"confirm": confirm})
    return client.post(url, headers=auth_header(user), json={"plan_digest": digest})


def _workflows(db) -> dict:
    return _docs(db, "workflows")


def _tick(client, tenant_id="eng"):
    response = client.post(f"/v1/admin/runs/advance?tenant_id={tenant_id}",
                           headers={"Authorization": "Bearer token-sweeper"})
    assert response.status_code == 200, response.text
    return response.json()["report"]


# --------------------------------------------------------------------------
# the pure part: matching, detection, widening
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path, pattern, expected", [
    ("terraform/bootstrap/main.tf", "terraform/bootstrap/**", True),
    ("terraform/bootstrap", "terraform/bootstrap/**", True),  # a directory the plan names
    ("terraform/bootstrapper/main.tf", "terraform/bootstrap/**", False),
    ("terraform/modules/x/iam.tf", "**/iam*.tf", True),
    ("iam_bindings.tf", "**/iam*.tf", True),
    ("terraform/modules/x/main.tf", "**/iam*.tf", False),
    ("CODEOWNERS", "CODEOWNERS", True),
    (".github/CODEOWNERS", "CODEOWNERS", True),  # no slash: any depth, as gitignore
    ("docs/CODEOWNERS.md", "CODEOWNERS", False),
    (".github/workflows/ci.yml", ".github/workflows/**", True),
    ("./apps/common/swarm_common/models.py", "apps/common/swarm_common/**", True),
    ("apps/common/swarm_commonx/models.py", "apps/common/swarm_common/**", False),
])
def test_a_protected_path_matches_as_a_gitignore_glob(path, pattern, expected):
    assert issueruns.path_matches(path, pattern) is expected


def test_the_hold_names_its_stops_and_who_decides_it():
    owner = issueruns.plan_hold(BOOTSTRAP_PLAN, platform=True, hard_stop_paths=[])
    assert owner["code"] == "NEEDS_OWNER" and owner["approvers"] == "owner_only"
    assert owner["reasons"] == ["iam"] and owner["matched"] == ["terraform/bootstrap/main.tf"]
    member = issueruns.plan_hold(BOOTSTRAP_PLAN, platform=False, hard_stop_paths=[])
    assert member["code"] == "NEEDS_SECOND_MEMBER" and member["approvers"] == "second_member"
    assert issueruns.plan_hold(PLAIN_PLAN, platform=True, hard_stop_paths=["src/secret/**"]) is None


def test_the_contract_is_held_only_in_a_platform_repository():
    plan = _plan("apps/common/swarm_common/models.py")
    assert issueruns.plan_hold(plan, platform=False, hard_stop_paths=[]) is None
    held = issueruns.plan_hold(plan, platform=True, hard_stop_paths=[])
    assert held["reasons"] == ["contract"] and held["code"] == "NEEDS_OWNER"


def test_a_prompt_naming_a_path_or_an_iam_resource_is_held_though_files_are_silent():
    """The plan's files are advisory: the prompts are read for the paths too."""
    by_path = _plan(prompt="Also edit terraform/bootstrap/deployer.tf to add the account.")
    assert issueruns.plan_hold(by_path, platform=False, hard_stop_paths=[])["reasons"] == ["iam"]
    by_resource = _plan(prompt="Add a google_project_iam_member for the new account.")
    held = issueruns.plan_hold(by_resource, platform=False, hard_stop_paths=[])
    assert held["matched"] == ["google_project_iam_member"]


def test_a_registrations_protected_paths_hold_but_the_workflow_pattern_refuses_instead():
    plan = _plan("db/migrations/0001.sql")
    held = issueruns.plan_hold(plan, platform=False, hard_stop_paths=["db/migrations/**"])
    assert held["reasons"] == ["protected_path"]
    assert issueruns.plan_hold(WORKFLOW_PLAN, platform=False,
                               hard_stop_paths=[".github/workflows/**"]) is None
    assert issueruns.workflow_paths(WORKFLOW_PLAN) == [".github/workflows/ci.yml"]


@pytest.mark.parametrize("issue_read, expected", [
    ({"labels": ["Security"]}, ["label:security"]),
    ({"labels": [], "body": "### Severity\n\nS0 — a platform guarantee at risk"}, ["severity:S0"]),
    ({"labels": [], "body": "### Severity\n\nS1 — blocked"}, []),
    (None, []),
])
def test_a_security_class_issue_is_the_label_or_severity_s0(issue_read, expected):
    assert issueruns.is_security_issue(issue_read) == expected


def test_widening_never_clears_a_hold_and_owner_outranks_second_member():
    member = issueruns.plan_hold(BOOTSTRAP_PLAN, platform=False, hard_stop_paths=[])
    assert issueruns.widen_hold(member, None) == member
    owner = issueruns.plan_hold(_plan("apps/common/swarm_common/x.py"), platform=True, hard_stop_paths=[])
    wide = issueruns.widen_hold(member, owner)
    assert wide["code"] == "NEEDS_OWNER" and wide["reasons"] == ["iam", "contract"]


# --------------------------------------------------------------------------
# 1. `.github/workflows/` is refused at plan
# --------------------------------------------------------------------------

def test_a_planner_plan_naming_a_workflow_file_fails_the_run_and_submits_nothing(client, db, objects):
    run = _planned(client, db, objects, WORKFLOW_PLAN)

    assert run["state"] == "FAILED"
    assert run["error"].startswith("WORKFLOWS_PATH: ")
    assert ".github/workflows/ci.yml" in run["error"]
    assert "made by a person or split out of the issue" in run["error"]
    assert run["plan"] is None and run["plan_digest"] is None
    assert not _workflows(db)
    assert [h["to"] for h in run["history"]] == ["PLANNING", "FAILED"]


def test_a_plan_edit_adding_a_workflow_file_is_a_422_naming_it(client, db, objects):
    run = _planned(client, db, objects, PLAIN_PLAN)
    response = client.post(
        f"/v1/runs/{run['id']}/plan:edit", headers=auth_header("alice"),
        json={"plan_digest": run["plan_digest"], "plan": WORKFLOW_PLAN},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_plan"
    assert ".github/workflows/ci.yml" in response.json()["message"]
    assert db.docs[f"issue_runs/{run['id']}"]["plan_digest"] == run["plan_digest"]


# --------------------------------------------------------------------------
# 2-3. who decides a hold
# --------------------------------------------------------------------------

def test_a_bootstrap_plan_in_a_platform_repository_needs_the_owner(client, db, objects, switch_on):
    _register(db, platform=True)
    run = _planned(client, db, objects, BOOTSTRAP_PLAN)
    assert run["state"] == "PLANNED"
    assert run["approval_hold"]["code"] == "NEEDS_OWNER"
    assert run["approval_hold"]["approvers"] == "owner_only"

    for member in ("alice", "dave"):
        refused = _approve(client, run["id"], run["plan_digest"], user=member)
        assert refused.status_code == 403, refused.text
        assert refused.json()["code"] == "hold_approver_required"
        assert refused.json()["detail"]["hold"] == "NEEDS_OWNER"
    assert not _workflows(db)

    approved = _approve(client, run["id"], run["plan_digest"], user="root")
    assert approved.status_code == 200, approved.text
    assert approved.json()["run"]["state"] == "RUNNING"
    assert approved.json()["run"]["approved_by"] == OWNER


def test_the_same_plan_elsewhere_needs_a_second_member(client, db, objects, switch_on):
    _register(db, platform=False)
    run = _planned(client, db, objects, BOOTSTRAP_PLAN)
    assert run["approval_hold"]["code"] == "NEEDS_SECOND_MEMBER"

    refused = _approve(client, run["id"], run["plan_digest"], user="alice")
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "hold_approver_required"
    assert not _workflows(db)

    approved = _approve(client, run["id"], run["plan_digest"], user="dave")
    assert approved.status_code == 200, approved.text
    assert approved.json()["run"]["state"] == "RUNNING"


def test_an_unregistered_repository_holds_iam_for_a_second_member(client, db, objects, switch_on):
    run = _planned(client, db, objects, BOOTSTRAP_PLAN)
    assert run["approval_hold"]["code"] == "NEEDS_SECOND_MEMBER"


def test_the_last_plan_editor_is_not_a_second_member(client, db, objects, switch_on):
    run = _planned(client, db, objects, BOOTSTRAP_PLAN)
    edited = copy.deepcopy(BOOTSTRAP_PLAN)
    edited["summary"] = "Make the widget list sortable, carefully."
    response = client.post(
        f"/v1/runs/{run['id']}/plan:edit", headers=auth_header("dave"),
        json={"plan_digest": run["plan_digest"], "plan": edited},
    )
    assert response.status_code == 200, response.text
    digest = response.json()["run"]["plan_digest"]
    refused = _approve(client, run["id"], digest, user="dave")
    assert refused.status_code == 403 and refused.json()["code"] == "hold_approver_required"


def test_a_one_person_tenant_approves_its_own_hold_only_with_the_typed_paths(client, db, objects, switch_on):
    from .conftest import seed_grant as grant
    from swarm_common.identity import tenant_id_for_user

    personal = tenant_id_for_user("carol@saga.xyz")
    grant(db, personal, "carol@saga.xyz", "saga-xyz/widgets")
    run = _planned(client, db, objects, BOOTSTRAP_PLAN, user="carol")
    assert run["approval_hold"]["code"] == "NEEDS_SECOND_MEMBER"

    bare = _approve(client, run["id"], run["plan_digest"], user="carol")
    assert bare.status_code == 403, bare.text
    assert bare.json()["detail"]["confirm"] == "terraform/bootstrap/main.tf"
    wrong = _approve(client, run["id"], run["plan_digest"], user="carol", confirm="yes")
    assert wrong.status_code == 403

    typed = _approve(client, run["id"], run["plan_digest"], user="carol",
                     confirm="terraform/bootstrap/main.tf")
    assert typed.status_code == 200, typed.text
    assert typed.json()["run"]["state"] == "RUNNING"


# --------------------------------------------------------------------------
# 4. the tick never auto-approves a held run
# --------------------------------------------------------------------------

@pytest.mark.parametrize("labels", [["security"]])
def test_an_auto_run_on_a_security_issue_is_not_auto_approved(client, db, objects, labels):
    """Enforced from its first day: the switch is OFF here, and still nothing moves."""
    created = _create(client, plan_approval="auto").json()["run"]
    _finish_planner(db, objects, created, PLAIN_PLAN)

    _tick(client)
    _tick(client)

    stored = db.docs[f"issue_runs/{created['id']}"]
    assert stored["state"] == "PLANNED"
    assert stored["approval_hold"]["reasons"] == ["security"]
    assert stored["approval_hold"]["matched"] == ["label:security"]
    assert not _workflows(db)
    assert all(t["id"] == created["planner_task_id"] for t in _docs(db, "tasks").values())


def test_an_auto_run_with_an_iam_plan_is_not_auto_approved_with_the_switch_on(client, db, objects, switch_on):
    created = _create(client, plan_approval="auto").json()["run"]
    _finish_planner(db, objects, created, BOOTSTRAP_PLAN)

    _tick(client)

    stored = db.docs[f"issue_runs/{created['id']}"]
    assert stored["state"] == "PLANNED" and stored["approval_hold"]["reasons"] == ["iam"]
    assert not _workflows(db)


# --------------------------------------------------------------------------
# 5. an edit never clears a hold
# --------------------------------------------------------------------------

def test_an_edit_that_removes_the_held_path_leaves_the_hold(client, db, objects, switch_on):
    _register(db, platform=True)
    run = _planned(client, db, objects, BOOTSTRAP_PLAN)
    response = client.post(
        f"/v1/runs/{run['id']}/plan:edit", headers=auth_header("alice"),
        json={"plan_digest": run["plan_digest"], "plan": PLAIN_PLAN},
    )
    assert response.status_code == 200, response.text
    edited = response.json()["run"]
    assert edited["approval_hold"]["code"] == "NEEDS_OWNER"
    assert edited["approval_hold"]["matched"] == ["terraform/bootstrap/main.tf"]
    refused = _approve(client, run["id"], edited["plan_digest"], user="alice")
    assert refused.status_code == 403 and refused.json()["code"] == "hold_approver_required"


def test_an_edit_can_add_a_hold(client, db, objects, switch_on):
    run = _planned(client, db, objects, PLAIN_PLAN)
    assert run["approval_hold"] is None
    response = client.post(
        f"/v1/runs/{run['id']}/plan:edit", headers=auth_header("dave"),
        json={"plan_digest": run["plan_digest"], "plan": BOOTSTRAP_PLAN},
    )
    assert response.json()["run"]["approval_hold"]["code"] == "NEEDS_SECOND_MEMBER"


def test_rejecting_a_held_run_is_anyones(client, db, objects, switch_on):
    _register(db, platform=True)
    run = _planned(client, db, objects, BOOTSTRAP_PLAN)
    response = client.post(f"/v1/runs/{run['id']}/plan:reject", headers=auth_header("alice"),
                           json={"reason": "not now"})
    assert response.status_code == 200 and response.json()["run"]["state"] == "REJECTED"


# --------------------------------------------------------------------------
# 6. report-only
# --------------------------------------------------------------------------

def test_with_the_switch_off_an_iam_hold_is_recorded_logged_and_approved(client, db, objects, caplog):
    assert not refusals.enforced("hold_approver_required", {})
    run = _planned(client, db, objects, BOOTSTRAP_PLAN)
    assert run["approval_hold"]["code"] == "NEEDS_SECOND_MEMBER"

    with caplog.at_level(logging.WARNING, logger="swarm_api.refusals"):
        approved = _approve(client, run["id"], run["plan_digest"], user="alice")

    assert approved.status_code == 200, approved.text
    assert approved.json()["run"]["state"] == "RUNNING"
    assert "refusal report-only code=hold_approver_required" in caplog.text


@pytest.mark.parametrize("labels", [["security"]])
def test_the_security_hold_is_enforced_with_the_switch_off(client, db, objects, labels):
    run = _planned(client, db, objects, PLAIN_PLAN)
    refused = _approve(client, run["id"], run["plan_digest"], user="alice")
    assert refused.status_code == 403 and refused.json()["code"] == "hold_approver_required"
    approved = _approve(client, run["id"], run["plan_digest"], user="dave")
    assert approved.status_code == 200, approved.text


def test_a_run_stored_before_the_hold_reads_as_unheld(client, db, objects):
    run = _planned(client, db, objects, PLAIN_PLAN)
    stored = db.docs[f"issue_runs/{run['id']}"]
    for key in ("approval_hold", "merge_approval", "merge_approved", "metadata"):
        stored.pop(key, None)
    read = _run(client, run["id"]).json()["run"]
    assert read["approval_hold"] is None and read["schedule"] is None
    assert _approve(client, run["id"], run["plan_digest"]).status_code == 200


def test_a_hold_that_needs_the_owner_logs_the_alert_line_once(client, db, objects, caplog):
    """§4.9: S4's `schedule_needs_owner` metric counts exactly this message,
    labelled by the `tenant_id` extra field."""
    from swarm_api import approvals

    _register(db, platform=True)
    with caplog.at_level(logging.WARNING, logger="swarm_api.approvals"):
        run = _planned(client, db, objects, BOOTSTRAP_PLAN)
        edited = copy.deepcopy(BOOTSTRAP_PLAN)
        edited["summary"] = "Sort, and nothing else."
        client.post(f"/v1/runs/{run['id']}/plan:edit", headers=auth_header("alice"),
                    json={"plan_digest": run["plan_digest"], "plan": edited})
    lines = [r for r in caplog.records if r.getMessage() == approvals.NEEDS_OWNER_LINE]
    assert len(lines) == 1
    assert lines[0].tenant_id == "eng" and lines[0].approval_id == f"run:{run['id']}"
    assert "terraform" not in lines[0].getMessage()

