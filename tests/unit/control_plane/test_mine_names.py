"""`GET /v1/tenants/mine` serves each group workspace's STORED name
(MINE-NAMES, part of #2157).

BGD, 2026-10-25: a workspace its admins rename (`PUT /v1/workspaces/{id}/name`,
WORKSPACE-ADMIN-FIELDS) is renamed in everyone's switcher at once, not only
once they next act in it. So `/mine` reads every listed group workspace's
`tenants/{id}` -- in ONE `get_all` per call, never a point read per workspace
-- and serves its `display_name`, with the registered group email beside it
as `group`. A workspace whose document is missing, unreadable or holds a name
the rename route would refuse keeps today's fallback, the group email, and is
never dropped from the list.

`dave` is in eng AND research; `root` is a platform admin in eng only.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.routes import tenants

from .conftest import ADMIN_GROUP, ENG_GROUP, RESEARCH_GROUP, auth_header, seed_tenant
from .fakes import FakeFirestore


class ReadCountingFirestore(FakeFirestore):
    """Records every `get_all` (the paths it asked for) and every point read
    of a `tenants/` document made outside one."""

    def __init__(self) -> None:
        super().__init__()
        self.batches: list[list[str]] = []
        self.point_reads: list[str] = []
        self.fail_batches = False
        self._in_batch = False

    def get_all(self, references, field_paths=None, transaction=None):  # type: ignore[override]
        refs = list(references)
        self.batches.append([ref.path for ref in refs])
        if self.fail_batches:
            raise RuntimeError("firestore unavailable")
        self._in_batch = True
        try:
            yield from super().get_all(refs, field_paths, transaction)
        finally:
            self._in_batch = False

    def document(self, path: str):  # type: ignore[override]
        return _Counted(super().document(path), self)

    def collection(self, path: str):  # type: ignore[override]
        return _CountedCollection(super().collection(path), self)


class _Counted:
    def __init__(self, ref: Any, db: ReadCountingFirestore) -> None:
        self._ref, self._db = ref, db

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ref, name)

    def get(self, *args: Any, **kwargs: Any) -> Any:
        if not self._db._in_batch and self._ref.path.startswith("tenants/"):
            self._db.point_reads.append(self._ref.path)
        return self._ref.get(*args, **kwargs)


class _CountedCollection:
    def __init__(self, ref: Any, db: ReadCountingFirestore) -> None:
        self._ref, self._db = ref, db

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ref, name)

    def document(self, *args: Any, **kwargs: Any) -> Any:
        return _Counted(self._ref.document(*args, **kwargs), self._db)


@pytest.fixture
def group_map() -> dict[str, tuple[str, ...]]:
    return {
        "dave@example.com": (ENG_GROUP, RESEARCH_GROUP),
        "root@example.com": (ADMIN_GROUP, ENG_GROUP),
    }


@pytest.fixture
def db() -> ReadCountingFirestore:
    return ReadCountingFirestore()


def mine(client: TestClient, user: str = "dave") -> list[dict[str, Any]]:
    response = client.get("/v1/tenants/mine", headers=auth_header(user))
    assert response.status_code == 200, response.text
    return response.json()


def names(listed: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    return {e["tenant_id"]: (e["display_name"], e.get("group")) for e in listed}


def group_batches(db: ReadCountingFirestore) -> list[list[str]]:
    return [paths for paths in db.batches if any(p.startswith("tenants/") for p in paths)]


def test_a_renamed_workspace_is_served_by_its_new_name_in_one_batched_read(client, db):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    db.docs["tenants/eng"]["display_name"] = "Engineering Platform"
    db.docs["tenants/research"]["display_name"] = "Research Lab"
    db.batches.clear()
    db.point_reads.clear()

    listed = mine(client)

    assert names(listed) == {
        "eng": ("Engineering Platform", ENG_GROUP),
        "research": ("Research Lab", RESEARCH_GROUP),
    }
    # ONE get_all for both workspaces, and no point read of either.
    assert group_batches(db) == [["tenants/eng", "tenants/research"]]
    assert "tenants/eng" not in db.point_reads
    assert "tenants/research" not in db.point_reads


def test_a_missing_document_keeps_the_group_email_and_the_entry(client, db):
    seed_tenant(db, "eng")
    db.docs["tenants/eng"]["display_name"] = "Engineering Platform"
    # research has never been submitted to: no tenant document at all.
    listed = mine(client)
    assert [e["tenant_id"] for e in listed] == ["eng", "research"]
    assert names(listed)["research"] == (RESEARCH_GROUP, RESEARCH_GROUP)
    assert names(listed)["eng"] == ("Engineering Platform", ENG_GROUP)


def test_an_unreadable_batch_keeps_every_entry_on_its_group_email(client, db):
    seed_tenant(db, "eng")
    db.docs["tenants/eng"]["display_name"] = "Engineering Platform"
    db.fail_batches = True
    listed = mine(client)
    assert names(listed) == {
        "eng": (ENG_GROUP, ENG_GROUP),
        "research": (RESEARCH_GROUP, RESEARCH_GROUP),
    }
    assert [e["is_default"] for e in listed] == [True, False]


@pytest.mark.parametrize("stored", [
    None, "", "   ", 7, "Your workspace", "Eng‮ineering", "x" * 61,
])
def test_a_name_the_rename_route_would_refuse_is_not_served(client, db, stored):
    """The tenant's own worker can rewrite its document; the switcher must
    not draw a name the rename route would have refused for it."""
    seed_tenant(db, "eng")
    db.docs["tenants/eng"]["display_name"] = stored
    assert names(mine(client))["eng"] == (ENG_GROUP, ENG_GROUP)


def test_a_platform_admins_reach_entry_is_named_in_the_same_batch(api_context, db):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    db.docs["tenants/research"]["display_name"] = "Research Lab"
    auth = api_context.authenticator.authenticate("Bearer token-root", None)
    auth = replace(auth, platform_reach=(("research", RESEARCH_GROUP),))
    db.batches.clear()

    listed = tenants.get_mine(auth=auth, ctx=api_context)

    reach = [e for e in listed if e["tenant_id"] == "research"]
    assert reach == [{
        "tenant_id": "research", "display_name": "Research Lab", "group": RESEARCH_GROUP,
        "kind": "group", "is_default": False, "via": "platform_admin",
        "role": {"name": "admin", "source": "platform_admin"},
    }]
    assert group_batches(db) == [["tenants/eng", "tenants/research"]]


def test_a_caller_with_only_a_personal_workspace_reads_no_names(api_context, db):
    auth = api_context.authenticator.authenticate("Bearer token-root", None)
    auth = replace(auth, tenant_choices=(("u-root", auth.email),), platform_reach=())
    db.batches.clear()
    listed = tenants.get_mine(auth=auth, ctx=api_context)
    assert [e["kind"] for e in listed] == ["user"]
    assert "group" not in listed[0]
    assert group_batches(db) == []
