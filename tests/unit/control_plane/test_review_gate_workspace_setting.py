"""A workspace's default review gate is settable as its other defaults are (#2524).

    GET /v1/workspaces/{tenant_id}/review-gate
    PUT /v1/workspaces/{tenant_id}/review-gate   `{enabled, min_lines, risk_paths}`

BGD 2026-10-28: the gate applies to every review step by default, "settable
per workspace the way other workflow defaults are". What these hold: the
owner or admin (here a platform admin) sets it, audited; a member who is not
its admin is refused 403 and nothing is written; a malformed value is a 422;
what is stored is what `verdictpublish` reads (`reviewgate.stored_setting`),
and a value no swarm-api path wrote is read as no setting.

No credentials, no network: an in-memory Firestore and the real app.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarm_api import reviewgate

from .conftest import auth_header
from .test_qa_targets import (  # noqa: F401 - fixtures are used by name
    api_context,
    resolver,
    seeded,
)

URL = "/v1/workspaces/eng/review-gate"


def _put(client, user: str, body: Any):
    return client.put(URL, json=body, headers=auth_header(user))


def _audit(db) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith("admin_audit/")
            and d.get("action") == reviewgate.AUDIT_ACTION]


def test_the_default_is_the_platforms(client, seeded):
    response = client.get(URL, headers=auth_header("root"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["review_gate"] == {"enabled": True, "min_lines": 300, "risk_paths": []}
    assert ".github/workflows/" in body["defaults"]["risk_paths"]


def test_an_admin_sets_it_audited_and_the_decision_reads_it(client, db, seeded):
    response = _put(client, "root", {"min_lines": 500, "risk_paths": ["docs/"]})
    assert response.status_code == 200, response.text
    assert response.json()["review_gate"]["min_lines"] == 500
    stored = reviewgate.stored_setting(db.docs["workspace_settings/eng"])
    assert stored == {"min_lines": 500, "risk_paths": ["docs/"]}
    gate = reviewgate.policy_of({"review_gate": {"default": True}}, stored)
    # The workspace's list adds to the platform's, never replaces it.
    assert gate.min_lines == 500 and set(reviewgate.DEFAULT_RISK_PATHS) < set(gate.risk_paths)
    assert len(_audit(db)) == 1

    # Switched off, then back to the platform's default: the map is replaced whole.
    assert _put(client, "root", {"enabled": False}).status_code == 200
    assert reviewgate.stored_setting(db.docs["workspace_settings/eng"]) == {"enabled": False}
    assert _put(client, "root", {}).status_code == 200
    assert reviewgate.stored_setting(db.docs["workspace_settings/eng"]) == {}
    assert len(_audit(db)) == 3


def test_a_member_who_is_not_its_admin_is_refused_and_nothing_written(client, db, seeded):
    response = _put(client, "alice", {"min_lines": 5})
    assert response.status_code == 403, response.text
    assert "review_gate" not in db.docs.get("workspace_settings/eng", {})
    assert _audit(db) == []


@pytest.mark.parametrize("body", [
    {"min_lines": -1}, {"min_lines": "300"}, {"risk_paths": []}, {"risk_paths": ["/etc"]},
    {"enabled": "no"}, {"paths": ["x/"]},
])
def test_a_malformed_value_is_a_422(client, db, seeded, body):
    response = _put(client, "root", body)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "invalid_review_gate"
    assert _audit(db) == []


def test_a_value_no_swarm_api_path_wrote_is_no_setting():
    assert reviewgate.stored_setting({"review_gate": {"min_lines": True}}) == {}
    assert reviewgate.stored_setting({"review_gate": {"enabled": "off"}}) == {}
    assert reviewgate.stored_setting({"review_gate": {"other": 1}}) == {}
