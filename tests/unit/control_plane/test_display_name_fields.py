"""A person's display name, served beside every email the §4.4 routes serve.

docs/design/user-profile.md §4.4 (Q5, Q8); lane PROFILE-NAMES-API. Each route
of the table gains a `*_name` sibling of each email field it serves, and no
name for an email it does not serve; a page asks for its names in ONE
`get_all`; a lookup that fails serves every name as null on a 200; and a
People row carries the name and nothing else from the person's preferences.

The child key is built at runtime, like every key in these tests.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import approvals
from swarm_api.auth import StaticTokenVerifier
from swarm_api.codec import workflow_to_firestore
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.people import People
from swarm_api.preferences import COLLECTION, Preferences
from swarm_api.store import Store
from swarm_api.waker import NullWaker
from swarm_common.models import Workflow, WorkflowStep
from swarm_common.states import TaskState

from .conftest import NoForgeTokens, api_settings, auth_header, seed_task, seed_tenant
from .test_people_admin import CAROL, FakePool, FakePublisher, _personal_tenant, _record

ADA = "ada@saga.xyz"
BOB = "bob@saga.xyz"
EVE = "eve@saga.xyz"  # submits, never sets a name
UNSEEN = "unseen@saga.xyz"  # has a name, appears in no response

NAMES = {ADA: "Ada Lovelace", BOB: "Bob Smith", UNSEEN: "Una Seen", CAROL: "Carol Danvers"}


#: Who `_get` calls as. Since #1653 the production Authenticator reads the
#: caller's OWN preferences (their saved default workspace) on every request;
#: that one-person read is authentication's, not the name lookup these tests
#: count, so the recorder neither counts it nor fails it.
CALLER = "alice@saga.xyz"


class CountingFirestore:
    """The test's `FakeFirestore`, with every preferences `get_all` counted,
    and made to fail when `fail` is set -- except the caller's own read for
    authentication (`skip`), which is neither."""

    def __init__(self, db, skip: frozenset[str] = frozenset()) -> None:
        self.db = db
        self.skip = skip
        self.calls: list[list[str]] = []
        self.fail = False
        self._get_all = db.get_all
        db.get_all = self.get_all  # type: ignore[method-assign]

    def get_all(self, references, *args: Any, **kwargs: Any):
        refs = list(references)
        paths = {r.path for r in refs}
        if paths and paths <= self.skip:
            return self._get_all(refs, *args, **kwargs)
        if any(r.path.startswith(COLLECTION + "/") for r in refs):
            self.calls.append([r.path for r in refs])
            if self.fail:
                raise RuntimeError("firestore is unavailable in this test")
        return self._get_all(refs, *args, **kwargs)


@pytest.fixture
def child_key() -> str:
    return secrets.token_hex(32)


@pytest.fixture
def named(db, child_key) -> CountingFirestore:
    prefs = Preferences(db, [child_key])
    for email, name in NAMES.items():
        prefs.save(email, display_name=name, default_workspace=None, muted=[],
                   reminder="as_set", revision=0)
    caller = frozenset(f"{COLLECTION}/{i}" for i in prefs.ids(CALLER))
    return CountingFirestore(db, skip=caller)


@pytest.fixture
def api_context(db, tokens, group_map, objects, child_key):
    return build_context(
        settings=api_settings(child_key=child_key),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=NoForgeTokens(),
    )


@pytest.fixture
def client(api_context) -> TestClient:
    app = create_app(api_context)
    app.state.workspace_publisher = FakePublisher()
    app.state.account_pool = FakePool()
    return TestClient(app, raise_server_exceptions=False)


def _get(client, path: str, user: str = "alice") -> dict:
    response = client.get(path, headers=auth_header(user))
    assert response.status_code == 200, response.text
    return response.json()


def _names_only_beside_their_emails(body: Any) -> None:
    """Every `*_name` (and `approver_names`) sits beside the email field it
    names, and no name of a person the response does not serve appears."""

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in node:
                if key == "approver_names":
                    assert "approvers" in node, node
                elif key.endswith("_by_name"):
                    assert key[: -len("_name")] in node, (key, node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(body)
    assert NAMES[UNSEEN] not in repr(body)


def _seed_tasks(db) -> dict[str, str]:
    """Ten tasks in eng by three people: ada 4, bob 3, eve 3."""
    seed_tenant(db, "eng")
    by: dict[str, str] = {}
    base = datetime.now(timezone.utc)
    for i, email in enumerate([ADA] * 4 + [BOB] * 3 + [EVE] * 3):
        task_id = f"task_{i:02d}"
        seed_task(db, task_id=task_id, tenant_id="eng", created_at=base - timedelta(seconds=i))
        db.docs[f"tasks/{task_id}"]["submitted_by"] = email
        by[task_id] = email
    return by


def _expected(email: str) -> str | None:
    return NAMES.get(email)


# -- tasks ---------------------------------------------------------------------------


@pytest.mark.parametrize("view", ["full", "summary"])
def test_a_page_of_ten_tasks_by_three_people_is_named_from_one_get_all_of_three_ids(
    db, client, named, view
) -> None:
    by = _seed_tasks(db)

    body = _get(client, f"/v1/tasks?view={view}")

    rows = body["tasks"]
    assert len(rows) == 10
    for row in rows:
        assert row["submitted_by"] == by[row["id"]]
        assert row["submitted_by_name"] == _expected(row["submitted_by"])
    assert len(named.calls) == 1, named.calls
    assert len(named.calls[0]) == 3, named.calls
    _names_only_beside_their_emails(body)


def test_a_task_read_serves_its_submitters_name(db, client, named) -> None:
    _seed_tasks(db)

    task = _get(client, "/v1/tasks/task_00")["task"]

    assert (task["submitted_by"], task["submitted_by_name"]) == (ADA, NAMES[ADA])
    _names_only_beside_their_emails(task)


def test_a_failed_lookup_serves_every_name_as_null_and_the_page_is_a_200(db, client, named) -> None:
    _seed_tasks(db)
    named.fail = True

    rows = _get(client, "/v1/tasks")["tasks"]

    assert len(rows) == 10
    assert all(row["submitted_by_name"] is None for row in rows)
    assert all(row["submitted_by"] for row in rows)
    assert len(named.calls) == 1


def test_without_a_child_key_every_name_is_null_and_nothing_is_read(
    db, tokens, group_map, objects, named
) -> None:
    ctx = build_context(
        settings=api_settings(), db=db, verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map), credentials=InMemoryCredentials(), waker=NullWaker(),
        metrics=ApiMetrics(), objects=objects, forge_tokens=NoForgeTokens(),
    )
    _seed_tasks(db)

    rows = _get(TestClient(create_app(ctx)), "/v1/tasks")["tasks"]

    assert {row["submitted_by_name"] for row in rows} == {None}
    assert named.calls == []


# -- workflows -----------------------------------------------------------------------


def _seed_workflow(db, workflow_id: str = "wf_named") -> None:
    seed_tenant(db, "eng")
    now = datetime.now(timezone.utc)
    task_id = f"task_of_{workflow_id}"
    seed_task(db, task_id=task_id, tenant_id="eng", state="SUCCEEDED", created_at=now,
              workflow_id=workflow_id)
    db.docs[f"tasks/{task_id}"]["submitted_by"] = BOB
    workflow = Workflow(
        workflow_id=workflow_id, tenant_id="eng", created_at=now, updated_at=now,
        state=TaskState.SUCCEEDED, submitted_by=ADA,
        steps=[WorkflowStep(step_id="a", runner_profile="mock", input={}, depends_on=[],
                            task_id=task_id)],
    )
    db.docs[f"workflows/{workflow_id}"] = workflow_to_firestore(workflow)


def test_the_workflow_list_names_each_workflows_submitter(db, client, named) -> None:
    _seed_workflow(db)

    body = _get(client, "/v1/workflows")

    [row] = body["workflows"]
    assert (row["submitted_by"], row["submitted_by_name"]) == (ADA, NAMES[ADA])
    assert len(named.calls) == 1
    _names_only_beside_their_emails(body)


def test_a_workflow_read_names_the_workflow_and_its_step_tasks_from_one_lookup(
    db, client, named
) -> None:
    _seed_workflow(db)

    body = _get(client, "/v1/workflows/wf_named")

    assert body["workflow"]["submitted_by_name"] == NAMES[ADA]
    [task] = body["tasks"]
    assert (task["submitted_by"], task["submitted_by_name"]) == (BOB, NAMES[BOB])
    assert len(named.calls) == 1
    assert len(named.calls[0]) == 2
    _names_only_beside_their_emails(body)


def test_the_workflows_read_by_ids_names_workflows_and_tasks(db, client, named) -> None:
    _seed_workflow(db)

    body = _get(client, "/v1/workflows?ids=wf_named")

    assert body["workflows"][0]["submitted_by_name"] == NAMES[ADA]
    assert body["tasks"][0]["submitted_by_name"] == NAMES[BOB]
    assert len(named.calls) == 1
    _names_only_beside_their_emails(body)


# -- approvals -----------------------------------------------------------------------


def _approval(db, key: str, approvers: Any) -> str:
    doc = approvals.request(
        db, datetime.now(timezone.utc), tenant_id="eng", kind=approvals.PROPOSAL, key=key,
        subject={"proposal": key}, digest=f"digest-{key}", summary=f"proposal {key}",
        approvers=approvers, ttl=24,
    )
    return doc["approval_id"]


def test_the_inbox_names_each_listed_approver_and_no_one_for_a_rule(db, client, named) -> None:
    seed_tenant(db, "eng")
    listed = _approval(db, "listed", [ADA, EVE])
    rule = _approval(db, "rule", "members")

    body = _get(client, "/v1/approvals")

    items = {i["approval_id"]: i for i in body["approvals"]}
    assert items[listed]["approver_names"] == [NAMES[ADA], None]
    assert items[listed]["decided_by_name"] is None
    assert items[rule]["approvers"] == "members"
    assert items[rule]["approver_names"] is None
    assert len(named.calls) == 1
    _names_only_beside_their_emails(body)


def test_a_decided_approval_names_who_decided_it(db, client, named) -> None:
    seed_tenant(db, "eng")
    item = _approval(db, "decided", [BOB])
    db.docs[f"{approvals.COLLECTION}/{item}"].update(
        {"state": approvals.APPROVED, "decided_by": BOB, "decided_at": datetime.now(timezone.utc)}
    )

    approval = _get(client, f"/v1/approvals/{item}")["approval"]

    assert (approval["decided_by"], approval["decided_by_name"]) == (BOB, NAMES[BOB])
    assert approval["approver_names"] == [NAMES[BOB]]
    _names_only_beside_their_emails(approval)


def test_an_expired_approval_names_no_one_for_the_expiry_actor(db, client, named) -> None:
    seed_tenant(db, "eng")
    item = _approval(db, "expired", [BOB])
    db.docs[f"{approvals.COLLECTION}/{item}"].update(
        {"state": approvals.EXPIRED, "decided_by": approvals.EXPIRY_ACTOR}
    )

    approval = _get(client, f"/v1/approvals/{item}")["approval"]

    assert approval["decided_by_name"] is None


# -- Admin › People ------------------------------------------------------------------


def test_a_people_row_serves_the_persons_display_name_beside_their_email(db, named, child_key) -> None:
    _record(db, state="ready")
    _personal_tenant(db)
    lookup = Preferences(db, [child_key]).names.names_for
    people = People(db, publisher=FakePublisher(), now=lambda: datetime.now(timezone.utc),
                    names=lookup)

    rows = people.everyone(store=Store(db))["people"]

    [row] = [r for r in rows if r["email"] == CAROL]
    assert row["display_name"] == NAMES[CAROL]
    assert len(named.calls) == 1


def test_a_people_row_serves_null_when_the_lookup_fails(db, named, child_key) -> None:
    _record(db, state="ready")
    _personal_tenant(db)
    named.fail = True
    lookup = Preferences(db, [child_key]).names.names_for
    people = People(db, publisher=FakePublisher(), now=lambda: datetime.now(timezone.utc),
                    names=lookup)

    rows = people.everyone(store=Store(db))["people"]

    assert [r["display_name"] for r in rows if r["email"] == CAROL] == [None]


def test_a_people_row_carries_no_default_workspace_mute_or_reminder(db, client, child_key) -> None:
    """Q8: those are the person's alone, and Admin › People reads none of them."""
    _record(db, state="ready")
    _personal_tenant(db)
    workspace = "team-" + secrets.token_hex(4)
    Preferences(db, [child_key]).save(
        CAROL, display_name="Carol Danvers", default_workspace=workspace,
        muted=["failed"], reminder="daily", revision=0,
    )

    body = _get(client, "/v1/admin/people", user="root")

    [row] = [r for r in body["people"] if r["email"] == CAROL]
    # The route's own People is given the lookup, not only a test's.
    assert row["display_name"] == "Carol Danvers"
    for field in ("default_workspace", "muted", "reminder", "revision", "preferences"):
        assert field not in row, field
    people_text = repr(body["people"])
    assert workspace not in people_text
    assert "daily" not in people_text
