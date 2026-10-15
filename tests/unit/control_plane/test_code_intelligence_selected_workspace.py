"""GET /v1/code-intelligence: the console's read of its selected workspace (CTXQ-UI over CTXQ-SET).

The console has no tenant id in hand when it draws Settings > Workspace > Code
intelligence; it asks for the SELECTED workspace, as it does for the judgment
backend. The answer is exactly the path form's
(`GET /v1/workspaces/{id}/code-intelligence`, test_code_intelligence.py): one
implementation serves both. These tests hold that the two cannot disagree,
that the console's write reads back, and that each path is registered once
(two routers for one path would be served silently by whichever came first).
"""

from __future__ import annotations

import pytest

from .conftest import auth_header, seed_tenant


@pytest.fixture
def seeded(db):
    seed_tenant(db, "eng")


def test_the_selected_workspace_read_is_the_path_reads_answer(client, db, seeded):
    selected = client.get("/v1/code-intelligence", headers=auth_header("alice"))
    assert selected.status_code == 200, selected.text
    by_path = client.get("/v1/workspaces/eng/code-intelligence", headers=auth_header("alice"))
    assert selected.json() == by_path.json()
    body = selected.json()
    assert body["tenant_id"] == "eng"
    assert body["jev"] is False
    assert body["can_edit"] is False


def test_what_the_console_writes_it_reads_back(client, db, seeded):
    put = client.put("/v1/workspaces/eng/code-intelligence", headers=auth_header("root"), json={"jev": True})
    assert put.status_code == 200, put.text
    assert put.json()["jev"] is True and put.json()["can_edit"] is True
    again = client.get("/v1/code-intelligence", headers=auth_header("alice"))
    assert again.json()["jev"] is True
    assert again.json()["updated_by"] == "root@saga.xyz"


def test_each_code_intelligence_path_is_registered_once(client):
    seen = sorted(
        (method, route.path)
        for route in client.app.routes
        for method in getattr(route, "methods", None) or ()
        if "code-intelligence" in route.path
    )
    assert seen == [
        ("GET", "/v1/code-intelligence"),
        ("GET", "/v1/workspaces/{tenant_id}/code-intelligence"),
        ("PUT", "/v1/workspaces/{tenant_id}/code-intelligence"),
    ]
