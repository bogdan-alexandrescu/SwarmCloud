"""`GET /v1/admin/tenants` names each tenant's workspace kind and `w-` id (WK-ADMIN-LIST).

docs/design/workspace-kinds.md §4.2.2 and §4.9: Admin › Tenants must tell the
tenants the in-cloud job made (a `workspaces/{tenant_id}` record) from the ones
Terraform made (no record). Each row gains the record's kind and `w-` id, or
nulls when there is no record.

The served names are `workspace_kind` and `workspace_id`, not `kind`: every
tenant row already serves `kind` as the frozen `Tenant.kind` (`user` or
`group`), which the console's Kind column and every `kind === 'user'` check
read. A service workspace is filed as a `user` tenant (`tenant_kind_for`), so
the two answers differ and one cannot replace the other.

The records are read with ONE `get_all` per page, never one read per tenant,
and a failed read serves the list without the two fields, flagged
`workspaces_unread`, never a 500.

Offline: the real route over FakeFirestore.
"""

from __future__ import annotations

import secrets
from typing import Any

from swarm_api.codec import tenant_to_api

from .conftest import auth_header, seed_tenant
from .fakes import FakeDocumentRef


def _workspace_id() -> str:
    return "w-" + secrets.token_hex(3)


def _record(db: Any, tenant_id: str, principal: str, kind: str | None) -> str:
    workspace_id = _workspace_id()
    record = {
        "tenant_id": tenant_id,
        "workspace_id": workspace_id,
        "principal": principal,
        "state": "ready",
    }
    if kind is not None:
        record["kind"] = kind
    db.docs[f"workspaces/{tenant_id}"] = record
    return workspace_id


def _rows(client: Any) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    response = client.get("/v1/admin/tenants", headers=auth_header("root"))
    assert response.status_code == 200, response.text
    body = response.json()
    return body, {row["tenant_id"]: row for row in body["tenants"]}


def _workspace_refs(references: Any) -> tuple[list[Any], list[Any]]:
    refs = list(references)
    return refs, [ref for ref in refs if ref.path.startswith("workspaces/")]


def test_a_group_and_a_service_record_are_served_with_their_kind_and_w_id(db, client):
    seed_tenant(db, "g-research")
    seed_tenant(db, "s-reports")
    seed_tenant(db, "u-alice")
    group_id = _record(db, "g-research", "research@example.com", "group")
    service_id = _record(db, "s-reports", "reports-bot@example.iam.gserviceaccount.com", "service")
    # A record written before kinds existed carries no `kind`: it is a person's.
    personal_id = _record(db, "u-alice", "alice@example.com", None)

    body, rows = _rows(client)

    assert rows["g-research"]["workspace_kind"] == "group"
    assert rows["g-research"]["workspace_id"] == group_id
    assert rows["s-reports"]["workspace_kind"] == "service"
    assert rows["s-reports"]["workspace_id"] == service_id
    assert rows["u-alice"]["workspace_kind"] == "personal"
    assert rows["u-alice"]["workspace_id"] == personal_id
    # The frozen tenant kind is still served beside it, unchanged.
    assert rows["g-research"]["kind"] == "group"
    assert body["workspaces_unread"] is False


def test_the_record_adds_no_principal_and_no_other_field(db, client):
    """Only the two fields: the record's principal, state and the rest stay out."""
    tenant = seed_tenant(db, "s-reports")
    _record(db, "s-reports", "reports-bot@example.iam.gserviceaccount.com", "service")

    _, rows = _rows(client)

    assert set(rows["s-reports"]) == set(tenant_to_api(tenant)) | {"workspace_kind", "workspace_id"}
    assert "reports-bot@example.iam.gserviceaccount.com" not in str(rows["s-reports"])


def test_a_terraform_tenant_with_no_record_gets_nulls(db, client):
    seed_tenant(db, "eng")

    body, rows = _rows(client)

    assert "workspace_kind" in rows["eng"] and rows["eng"]["workspace_kind"] is None
    assert "workspace_id" in rows["eng"] and rows["eng"]["workspace_id"] is None
    assert body["workspaces_unread"] is False


def test_a_page_of_fifty_reads_the_records_in_one_batch(db, client, monkeypatch):
    tenant_ids = [f"t-{i:02d}" for i in range(50)]
    for tenant_id in tenant_ids:
        seed_tenant(db, tenant_id)
    for tenant_id in tenant_ids[::2]:
        _record(db, tenant_id, f"{tenant_id}@example.com", "group")

    batches: list[int] = []
    single_reads: list[str] = []
    in_batch: list[bool] = []
    real_get_all = db.get_all
    real_get = FakeDocumentRef.get

    def counting_get_all(references, *args: Any, **kwargs: Any):
        refs, workspace_refs = _workspace_refs(references)
        if workspace_refs:
            batches.append(len(workspace_refs))
        # The fake's get_all reads each reference itself: those are the
        # batch, not single reads, so they are drained while it is marked.
        in_batch.append(True)
        try:
            return iter(list(real_get_all(refs, *args, **kwargs)))
        finally:
            in_batch.pop()

    def counting_get(self, *args: Any, **kwargs: Any):
        if self.path.startswith("workspaces/") and not in_batch:
            single_reads.append(self.path)
        return real_get(self, *args, **kwargs)

    monkeypatch.setattr(db, "get_all", counting_get_all)
    monkeypatch.setattr(FakeDocumentRef, "get", counting_get)

    _, rows = _rows(client)

    assert batches == [50], f"expected one batched read of 50 records, got {batches}"
    assert single_reads == [], f"records read one at a time: {single_reads[:5]}"
    assert all(tenant_id in rows for tenant_id in tenant_ids)
    assert rows["t-00"]["workspace_kind"] == "group"
    assert rows["t-01"]["workspace_kind"] is None


def test_a_failed_read_keeps_the_list_and_says_so(db, client, monkeypatch):
    seed_tenant(db, "eng")
    seed_tenant(db, "g-research")
    _record(db, "g-research", "research@example.com", "group")
    real_get_all = db.get_all

    def failing_get_all(references, *args: Any, **kwargs: Any):
        refs, workspace_refs = _workspace_refs(references)
        if workspace_refs:
            raise RuntimeError("firestore unavailable")
        return real_get_all(refs, *args, **kwargs)

    monkeypatch.setattr(db, "get_all", failing_get_all)

    body, rows = _rows(client)

    assert body["workspaces_unread"] is True
    assert set(rows) == {"eng", "g-research"}
    for row in rows.values():
        assert "workspace_kind" not in row
        assert "workspace_id" not in row
