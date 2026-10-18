"""The MCP servers list serves the egress addresses and each server's last use (MCP-API-B).

Design: docs/design/registered-mcp-servers.md §4.9 (the NAT addresses a
server's operator allowlists, "read from a Terraform output rendered into
swarm-api's settings") and §4.11 ("last used" from `mcp_attach_releases`),
part of #1433. Settings > MCP servers (lane MCP-UI) reads both fields.

What is held here:

  * `egress_addresses` is MCP_EGRESS_ADDRESSES, as swarm-api's settings read
    it, on GET /v1/mcp-servers; unset or empty serves `[]`.
  * `last_attached_at` is the newest `mcp_attach_releases` row of THIS
    tenant for that server, on the list and on GET /{name}; `null` is never
    attached. Another tenant's row for a server of the same name never
    counts, even one forged to carry this tenant's `server_id`.
  * The list reads the releases ONCE, tenant-filtered and limited, however
    many servers it lists, and not at all when it lists none.

Offline: the in-memory Firestore, no credentials, no emulator.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from swarm_api.mcp_servers import McpServers, server_id
from swarm_api.routes.attempts_mcp import ATTACH_ROWS_COLLECTION
from swarm_api.settings import ApiSettings

from .conftest import auth_header, seed_tenant
from .fakes import FakeQuery
from .test_child_tasks_api import _client, worker_tokens  # noqa: F401  (a fixture)
from .test_mcp_servers import on  # noqa: F401  (a fixture)

INDEXES_TF = Path(__file__).resolve().parents[3] / "terraform/modules/firestore/indexes.tf"

T0 = datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)
ADDRESSES = ("203.0.113.7", "198.51.100.9")


def _register(db, tenant_id: str, name: str) -> None:
    McpServers(db, now=lambda: T0).register(
        tenant_id=tenant_id, name=name, url=f"https://{name}.example.com/mcp",
        description="", auth={"mode": "none"}, allowed_profiles=[], allowed_tools=[],
        limits=None, enabled=True, by="root@saga.xyz")


def _attached(db, tenant_id: str, name: str, at: datetime, *, attempt: str,
              sid: str | None = None) -> None:
    sid = sid or server_id(tenant_id, name)
    db.docs[f"{ATTACH_ROWS_COLLECTION}/{attempt}~{sid}"] = {
        "at": at, "expire_at": at + timedelta(days=90), "task_id": f"task_{attempt}",
        "attempt_id": attempt, "generation": 1, "tenant_id": tenant_id,
        "server_id": sid, "name": name, "revision": 1, "auth_mode": "none",
        "secret_id": None, "version": None, "submitted_by": "alice@saga.xyz",
    }


@pytest.fixture
def make_client(db, worker_tokens, group_map, objects, on):  # noqa: F811
    seed_tenant(db, "eng")
    seed_tenant(db, "research")

    def make(**settings: Any):
        return _client(db, worker_tokens, group_map, objects, **settings)
    return make


@pytest.fixture
def release_reads(monkeypatch) -> list[FakeQuery]:
    """Every query the request runs against `mcp_attach_releases`."""
    seen: list[FakeQuery] = []
    rows = FakeQuery._rows

    def spy(self):
        if self._path == ATTACH_ROWS_COLLECTION:
            seen.append(self)
        return rows(self)
    monkeypatch.setattr(FakeQuery, "_rows", spy)
    return seen


# --------------------------------------------------------------------------
# egress_addresses (§4.9)
# --------------------------------------------------------------------------


def test_the_list_serves_the_configured_egress_addresses(make_client):
    client = make_client(mcp_egress_addresses=ADDRESSES)
    listed = client.get("/v1/mcp-servers", headers=auth_header("alice"))
    assert listed.status_code == 200, listed.text
    assert listed.json()["egress_addresses"] == list(ADDRESSES)


def test_unset_egress_addresses_serve_an_empty_list(make_client):
    listed = make_client().get("/v1/mcp-servers", headers=auth_header("alice"))
    assert listed.status_code == 200, listed.text
    assert listed.json()["egress_addresses"] == []


def test_settings_read_mcp_egress_addresses(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", "swarm-test-project")
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("MCP_EGRESS_ADDRESSES", " 203.0.113.7, ,198.51.100.9 ")
    assert ApiSettings.from_env().mcp_egress_addresses == ADDRESSES
    monkeypatch.setenv("MCP_EGRESS_ADDRESSES", "")
    assert ApiSettings.from_env().mcp_egress_addresses == ()
    monkeypatch.delenv("MCP_EGRESS_ADDRESSES")
    assert ApiSettings.from_env().mcp_egress_addresses == ()


# --------------------------------------------------------------------------
# last_attached_at (§4.11)
# --------------------------------------------------------------------------


def test_last_attached_at_is_this_tenants_newest_release(make_client, db):
    _register(db, "eng", "kb")
    _register(db, "eng", "notes")
    _register(db, "research", "kb")
    _attached(db, "eng", "kb", T0 - timedelta(hours=3), attempt="att_a")
    _attached(db, "eng", "kb", T0 - timedelta(hours=1), attempt="att_b")
    # Another tenant's server of the same name, attached later: never counts.
    _attached(db, "research", "kb", T0, attempt="att_c")
    # A row forged to carry eng's server_id under research: never counts.
    _attached(db, "research", "kb", T0 + timedelta(hours=1), attempt="att_d",
              sid=server_id("eng", "kb"))

    client = make_client()
    listed = client.get("/v1/mcp-servers", headers=auth_header("root"))
    assert listed.status_code == 200, listed.text
    served = {s["name"]: s["last_attached_at"] for s in listed.json()["servers"]}
    assert served == {"kb": (T0 - timedelta(hours=1)).isoformat(), "notes": None}

    one = client.get("/v1/mcp-servers/kb", headers=auth_header("root"))
    assert one.status_code == 200, one.text
    assert one.json()["server"]["last_attached_at"] == (T0 - timedelta(hours=1)).isoformat()
    never = client.get("/v1/mcp-servers/notes", headers=auth_header("root"))
    assert never.json()["server"]["last_attached_at"] is None

    # The control: research's own member sees research's own release.
    theirs = client.get("/v1/mcp-servers/kb", headers=auth_header("bob"))
    assert theirs.json()["server"]["last_attached_at"] == T0.isoformat()


def test_the_list_reads_the_releases_once_tenant_filtered_and_bounded(
        make_client, db, release_reads):
    for n in range(5):
        _register(db, "eng", f"kb{n}")
        _attached(db, "eng", f"kb{n}", T0 - timedelta(minutes=n), attempt=f"att_{n}")

    listed = make_client().get("/v1/mcp-servers", headers=auth_header("root"))
    assert listed.status_code == 200, listed.text
    assert len(listed.json()["servers"]) == 5
    assert all(s["last_attached_at"] for s in listed.json()["servers"])
    assert len(release_reads) == 1, "one read per list, never one per server"
    [query] = release_reads
    assert ("tenant_id", "==", "eng") in query._filters
    assert query._limit is not None and query._limit > 0


def test_the_release_read_has_its_composite_index(make_client, db, release_reads):
    """An equality on tenant_id ordered by `at` is served only by a composite
    index; the in-memory fake has no index check, so without this the list
    is green here and FAILED_PRECONDITION (a 500) against real Firestore."""
    _register(db, "eng", "kb")
    assert make_client().get("/v1/mcp-servers", headers=auth_header("root")).status_code == 200
    [query] = release_reads
    shape = ([f for f, op, _ in query._filters if op == "=="],
             [(f, d.upper()) for f, d in query._orders])
    declared = []
    for block in re.findall(r'"[^"]+"\s*=\s*\{(.*?)\n    \}', INDEXES_TF.read_text(), re.S):
        if f'collection  = "{ATTACH_ROWS_COLLECTION}"' not in block:
            continue
        fields = re.findall(r'field_path = "([^"]+)", order = "([A-Z]+)"', block)
        declared.append(([f for f, _ in fields[:-1]], [fields[-1]]))
    assert shape in declared, f"no index in indexes.tf serves {shape}; found {declared}"


def test_a_tenant_with_no_servers_reads_no_releases(make_client, db, release_reads):
    _attached(db, "eng", "kb", T0, attempt="att_a")
    listed = make_client().get("/v1/mcp-servers", headers=auth_header("root"))
    assert listed.status_code == 200, listed.text
    assert listed.json()["servers"] == []
    assert release_reads == []


def test_a_cut_off_read_says_so(make_client, db, monkeypatch):
    from swarm_api.routes import mcp_servers as route
    _register(db, "eng", "kb")
    _register(db, "eng", "old")
    _attached(db, "eng", "kb", T0, attempt="att_a")
    _attached(db, "eng", "kb", T0 - timedelta(hours=1), attempt="att_b")
    _attached(db, "eng", "old", T0 - timedelta(days=30), attempt="att_c")
    client = make_client()

    monkeypatch.setattr(route, "LAST_ATTACHED_SCAN", 2)
    listed = client.get("/v1/mcp-servers", headers=auth_header("root")).json()
    assert {s["name"]: s["last_attached_at"] for s in listed["servers"]} == {
        "kb": T0.isoformat(), "old": None}
    assert listed["last_attached_complete"] is False

    # The control: a bound above the rows reads them all, and says so.
    monkeypatch.setattr(route, "LAST_ATTACHED_SCAN", 3 + 1)
    listed = client.get("/v1/mcp-servers", headers=auth_header("root")).json()
    assert {s["name"]: s["last_attached_at"] for s in listed["servers"]} == {
        "kb": T0.isoformat(), "old": (T0 - timedelta(days=30)).isoformat()}
    assert listed["last_attached_complete"] is True
