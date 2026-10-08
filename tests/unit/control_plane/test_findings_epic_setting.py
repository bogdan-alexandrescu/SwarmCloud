"""The tenant's wave epic, `findings_epic`: the API half (#638, owner 2026-10-05).

  * An admin sets it per tenant (`PUT /v1/admin/tenants/{id}/findings-epic`),
    as an issue number, or null to stop filing. It is stored on the tenant's
    document beside the frozen `Tenant` fields, not in them.
  * A workflow submitted while it is set carries it as
    `metadata.dispatch.findings_epic` on the GATED step only -- the step that
    reads the review's verdict and files its minors -- inside the block the
    spec signature covers, because the worker reads no key the signature does
    not cover. Unset, no key is written and the block is what it was before.

The worker half is tests/unit/worker/test_findings_epic.py.
"""

from __future__ import annotations

import pytest

from typing import Any

from .conftest import auth_header, seed_tenant
from .test_allow_empty_diff_workflow import _post, _review_shape, _steps


def _set(client: Any, value: Any, user: str = "root", tenant: str = "eng") -> Any:
    return client.put(f"/v1/admin/tenants/{tenant}/findings-epic",
                      headers=auth_header(user), json={"findings_epic": value})


def test_an_admin_sets_and_clears_the_epic(client, db):
    seed_tenant(db, "eng")
    response = _set(client, 638)
    assert response.status_code == 200, response.text
    assert response.json() == {"tenant_id": "eng", "findings_epic": 638}
    assert db.docs["tenants/eng"]["findings_epic"] == 638

    read = client.get("/v1/admin/tenants/eng/findings-epic", headers=auth_header("root"))
    assert read.status_code == 200, read.text
    assert read.json() == {"tenant_id": "eng", "findings_epic": 638}

    cleared = _set(client, None)
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["findings_epic"] is None
    assert db.docs["tenants/eng"]["findings_epic"] is None


def test_a_value_that_is_not_an_issue_number_is_refused(client, db):
    seed_tenant(db, "eng")
    for bad in (0, -3, "638", 1.5, True):
        response = _set(client, bad)
        assert response.status_code == 422, (bad, response.text)
    assert "findings_epic" not in db.docs["tenants/eng"]


def test_only_an_admin_sets_it(client, db):
    seed_tenant(db, "eng")
    response = _set(client, 638, user="alice")
    assert response.status_code == 403, response.text
    assert "findings_epic" not in db.docs["tenants/eng"]


def test_an_unknown_tenant_is_not_found(client, db):
    response = _set(client, 638, tenant="nobody")
    assert response.status_code == 404, response.text


def test_the_gated_step_carries_the_epic(client, db):
    seed_tenant(db, "eng")
    assert _set(client, 638).status_code == 200
    response = _post(client, _review_shape())
    assert response.status_code == 201, response.text
    steps = _steps(response.json())

    fix = db.docs[f"tasks/{steps['fix']}"]["metadata"]["dispatch"]
    assert fix["findings_epic"] == 638
    for other in ("implement", "review"):
        assert "findings_epic" not in db.docs[f"tasks/{steps[other]}"]["metadata"]["dispatch"]


def test_no_epic_stores_no_key(client, db):
    seed_tenant(db, "eng")
    response = _post(client, _review_shape())
    assert response.status_code == 201, response.text
    fix = db.docs[f"tasks/{_steps(response.json())['fix']}"]["metadata"]["dispatch"]
    assert "findings_epic" not in fix


def test_the_epic_is_covered_by_the_spec_signature():
    from swarm_common.specsign import SIGNED_METADATA_KEYS

    from swarm_api.validation import DispatchOptions

    gated = DispatchOptions(strategy="integrate").with_routing(
        builds_on=None, gate_task_id="task_r", gate_verdicts=("NOT_YET",), findings_epic=9,
    )
    assert gated.to_metadata()["findings_epic"] == 9
    assert "dispatch" in SIGNED_METADATA_KEYS
    ungated = DispatchOptions(strategy="integrate").with_routing(
        builds_on=None, gate_task_id=None, findings_epic=9,
    )
    assert "findings_epic" not in ungated.to_metadata()



@pytest.fixture(autouse=True)
def _members_hold_grants(db):
    """#780 OB7: a person's task on GitHub needs their grant. This file is about
    something else, so its members hold one on every repository it names."""
    from .conftest import TEST_REPOSITORIES, grant_members

    grant_members(db, *TEST_REPOSITORIES)
