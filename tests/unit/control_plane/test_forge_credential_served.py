"""DAYONE-FORGE-CRED-RECORD (part of #2688), the API half.

The worker records, in an attempt's result summary, the KIND of GitHub
credential the run cloned and pushed with and the secret NAME it came from
(`result_summary.forge_credential`: `{"kind": "app_installation", "source":
"swarm-tenant-<tenant>-git"}`). Stage 14 of `swarm install` reads it through
`swarm result <task> --json` to check lesson 43 (an App installation token,
never a PAT). swarm-api serves it where it already serves the summary, the
task read, to a caller who may read the task -- unchanged by the masking a
summary goes through, and never to another tenant's caller.

Offline: the fake Firestore, no credentials.
"""

from __future__ import annotations

import pytest

from .conftest import auth_header, seed_task, seed_tenant

RECORD = {"kind": "app_installation", "source": "swarm-tenant-eng-git"}


def _seed(db, record) -> None:
    seed_tenant(db, "eng")
    doc = seed_task(db, task_id="task_cred", tenant_id="eng", state="SUCCEEDED")
    doc["result_summary"] = {"summary": "done", "forge_credential": record}


@pytest.mark.parametrize("record", [
    RECORD,
    {"kind": "pat", "source": "swarm-tenant-eng-git"},
    {"kind": "mixed", "kinds": ["app_installation", "pat"], "source": "swarm-tenant-eng-git"},
    {"kind": "none", "source": None},
])
def test_the_task_read_serves_the_recorded_kind_unmasked(client, db, record):
    _seed(db, record)
    response = client.get("/v1/tasks/task_cred", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    task = response.json()["task"]
    assert task["result_summary"]["forge_credential"] == record
    assert task["result_summary_redaction_count"] == 0


def test_another_tenants_caller_is_served_nothing(client, db):
    _seed(db, RECORD)
    response = client.get("/v1/tasks/task_cred", headers=auth_header("bob"))
    assert response.status_code in (403, 404)
    assert "forge_credential" not in response.text
