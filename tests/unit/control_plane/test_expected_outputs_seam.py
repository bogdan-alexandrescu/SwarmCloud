"""swarm-api and the worker mean the same thing by `metadata.expected_outputs`.

The key is a contract between two packages that never import each other, for
the reason tests/unit/worker/test_dispatch_contract_parity.py gives: a worker
that imported swarm-api would pull the control plane into every agent image. So
it is spelled twice, once on each side, and this file is what holds the two
spellings together (docs/mirrored-values.md, "Covered elsewhere").

The second test is built from what `POST /v1/workflows` really stores, not from
a hand-written block: hand-written fixtures are how the `dispatch` carrier
drifted between these same two packages for as long as it did. The PRODUCTION
worker still imports nothing from swarm-api; only this test does.

The imports sit inside each test so that, before either module existed, each
test failed on its own instead of one collection error hiding the rest.
"""

from __future__ import annotations

from pathlib import Path

from .conftest import auth_header


def test_the_api_and_the_worker_name_the_same_metadata_key():
    from agent_worker.expected_outputs import METADATA_KEY
    from swarm_api.expected_outputs import EXPECTED_OUTPUTS_METADATA_KEY

    assert EXPECTED_OUTPUTS_METADATA_KEY == METADATA_KEY


def test_what_the_api_stores_is_what_the_worker_tells_the_agent(client, db):
    from agent_worker.expected_outputs import declared_outputs, with_instructions

    response = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "steps": [
                {"step_id": "scan", "runner_profile": "mock"},
                {
                    "step_id": "report",
                    "runner_profile": "mock",
                    "depends_on": ["scan"],
                    "input_from": {"scan": "notes.md"},
                },
                {
                    "step_id": "index",
                    "runner_profile": "mock",
                    "depends_on": ["scan"],
                    "input_from": {"scan": "data.json"},
                },
            ]
        },
    )
    assert response.status_code == 201, response.text
    task_ids = {s["step_id"]: s["task_id"] for s in response.json()["workflow"]["steps"]}

    upstream = declared_outputs(db.docs[f"tasks/{task_ids['scan']}"]["metadata"])
    assert upstream.names == ("data.json", "notes.md")
    assert upstream.rejected == ()
    for leaf in ("report", "index"):
        declared = declared_outputs(db.docs[f"tasks/{task_ids[leaf]}"]["metadata"])
        assert declared.names == (), leaf

    told = with_instructions("scan it", upstream.names, Path("/w/att_1/artifacts"))
    assert told.startswith("scan it")
    assert "/w/att_1/artifacts/data.json" in told
    assert "/w/att_1/artifacts/notes.md" in told
