"""`GET /v1/tenants/mine` serves each group workspace's stored name
(MINE-NAMES, part of #2157).

BGD, 2026-10-25: a workspace's admins rename it (`PUT /v1/workspaces/{id}/name`,
WORKSPACE-ADMIN-FIELDS) and everyone's switcher shows the new name at once,
not only after they next act in that workspace. So /mine reads the caller's
group workspaces' `tenants/{id}` documents -- ONE batched `get_all` per call,
never a read per workspace -- and serves a stored name in `display_name`, with
the group email beside it as `group`. A workspace whose document is missing,
unreadable or still named by its id keeps today's entry, group email and all,
and is never dropped.

`dave` is in eng and research; `root` is a platform admin in eng only.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import ADMIN_GROUP, ENG_GROUP, RESEARCH_GROUP, api_settings, auth_header, seed_tenant
from .fakes import FakeDocumentRef, FakeFirestore

GROUP_DOCS = {"tenants/eng", "tenants/research"}


class RecordingFirestore(FakeFirestore):
    """Records every `get_all` (the paths it named) so "one batched read" is
    measurable; `fail_get_all` makes the batch raise, as an outage would."""

    def __init__(self) -> None:
        super().__init__()
        self.batches: list[list[str]] = []
        self.fail_get_all = False
        #: True while the batch reads, so its own reads are not counted as single ones.
        self.in_batch = False

    def get_all(self, references, field_paths=None, transaction=None):  # type: ignore[override]
        refs = list(references)
        self.batches.append([ref.path for ref in refs])
        if self.fail_get_all:
            raise RuntimeError("firestore unavailable")
        self.in_batch = True
        try:
            return iter(list(super().get_all(refs, field_paths, transaction)))
        finally:
            self.in_batch = False

    def tenant_batches(self) -> list[list[str]]:
        return [b for b in self.batches if any(p.startswith("tenants/") for p in b)]


@pytest.fixture
def group_map() -> dict[str, tuple[str, ...]]:
    return {
        "alice@example.com": (ENG_GROUP,),
        "carol@example.com": (),
        "dave@example.com": (ENG_GROUP, RESEARCH_GROUP),
        "root@example.com": (ADMIN_GROUP, ENG_GROUP),
    }


@pytest.fixture
def db() -> RecordingFirestore:
    return RecordingFirestore()


@pytest.fixture
def single_gets(monkeypatch, db) -> list[str]:
    """Every single-document read, by path: a read per workspace shows here."""
    seen: list[str] = []
    original = FakeDocumentRef.get

    def get(self: FakeDocumentRef, *args: Any, **kwargs: Any):
        if not db.in_batch:
            seen.append(self.path)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(FakeDocumentRef, "get", get)
    return seen


def renamed(db: FakeFirestore, tenant_id: str, name: str) -> None:
    seed_tenant(db, tenant_id)
    db.docs[f"tenants/{tenant_id}"]["display_name"] = name


def mine(client: TestClient, user: str) -> list[dict[str, Any]]:
    response = client.get("/v1/tenants/mine", headers=auth_header(user))
    assert response.status_code == 200, response.text
    return response.json()


def names(entries: list[dict[str, Any]]) -> dict[str, tuple[str, str | None]]:
    return {e["tenant_id"]: (e["display_name"], e.get("group")) for e in entries}


def test_a_renamed_workspace_is_served_by_its_new_name_in_one_batched_read(
        client, db, single_gets):
    renamed(db, "eng", "Engineering")
    renamed(db, "research", "Research Lab")
    before = len(single_gets)

    entries = mine(client, "dave")

    assert [e["tenant_id"] for e in entries] == ["eng", "research"]
    assert names(entries) == {"eng": ("Engineering", ENG_GROUP),
                              "research": ("Research Lab", RESEARCH_GROUP)}
    # One batch naming exactly the caller's group workspaces, and no
    # per-workspace read of either document beside it.
    assert [set(b) for b in db.tenant_batches()] == [GROUP_DOCS]
    assert not GROUP_DOCS & set(single_gets[before:])


def test_a_second_rename_shows_on_the_next_call_without_acting_there(client, db):
    renamed(db, "research", "Research Lab")
    assert names(mine(client, "dave"))["research"] == ("Research Lab", RESEARCH_GROUP)
    db.docs["tenants/research"]["display_name"] = "Applied Research"
    assert names(mine(client, "dave"))["research"] == ("Applied Research", RESEARCH_GROUP)
    assert len(db.tenant_batches()) == 2


def test_a_missing_document_keeps_the_group_email_and_the_entry(client, db):
    renamed(db, "eng", "Engineering")
    # research has no tenant document yet: a first submit creates it.
    entries = mine(client, "dave")
    assert names(entries) == {"eng": ("Engineering", ENG_GROUP),
                              "research": (RESEARCH_GROUP, None)}
    assert entries[1]["is_default"] is False


def test_an_unreadable_batch_keeps_every_entry_with_its_group_email(client, db):
    renamed(db, "eng", "Engineering")
    renamed(db, "research", "Research Lab")
    db.fail_get_all = True
    entries = mine(client, "dave")
    assert names(entries) == {"eng": (ENG_GROUP, None), "research": (RESEARCH_GROUP, None)}
    assert entries[0]["is_default"] is True


@pytest.mark.parametrize("stored", [
    "eng",                        # the id a first submit names it by: no name was chosen
    "",
    "   ",
    None,
    42,
    "Your workspace",             # the switcher's label for the caller's own workspace
    "Eng‮ering",             # a bidi override that makes one name read as another
    "x" * 61,                     # longer than a rename accepts
])
def test_a_stored_name_a_rename_could_not_have_written_is_not_served(client, db, stored):
    seed_tenant(db, "eng")
    db.docs["tenants/eng"]["display_name"] = stored
    assert names(mine(client, "alice")) == {"eng": (ENG_GROUP, None)}


def test_a_caller_in_no_group_reads_no_tenant_documents(client, db):
    assert mine(client, "carol") == []
    assert db.tenant_batches() == []


def test_a_platform_admins_reach_entries_are_named_in_the_same_batch(db, tokens, group_map):
    ctx = build_context(
        settings=api_settings(platform_admin_reach=True), db=db,
        verifier=StaticTokenVerifier(tokens), groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(), objects=None,
    )
    client = TestClient(create_app(ctx), raise_server_exceptions=False)
    renamed(db, "research", "Research Lab")

    entries = mine(client, "root")

    reach = [e for e in entries if e.get("via") == "platform_admin"]
    assert [(e["tenant_id"], e["display_name"], e["group"]) for e in reach] == [
        ("research", "Research Lab", RESEARCH_GROUP)]
    assert names(entries)["eng"] == (ENG_GROUP, None)
    assert [set(b) for b in db.tenant_batches()] == [GROUP_DOCS]
