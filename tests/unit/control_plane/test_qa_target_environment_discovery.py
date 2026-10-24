"""Discovery per entry of a QA target's `environments[]` (docs/design/qa-environments.md
§1.1: `discovered` and `discovering` are a leg's; lane QAENV-MODEL, part of #2136).

What these tests hold, over the real app and the in-memory Firestore, with
test_qa_target_discovery's harness:

  * a discovery of a target of two entries names its leg, and its step is
    marked `{id, revision, environment}` and recorded for that leg;
  * the leg's `discovering` holds that leg only: the other leg may start its
    own discovery while the first runs;
  * when the step ends, what it found is stored on ITS entry, held to its own
    origin, and the other entry's `discovered` is untouched.
"""

from __future__ import annotations

from typing import Any

from .conftest import auth_header
from .test_qa_target_discovery import (  # noqa: F401 -- pytest fixtures, used by name
    DISCOVER,
    TARGETS,
    api_context,
    finish,
    group_map,
    resolver,
    route_list,
    seeded,
    tasks_of,
)
from .test_qa_target_environments import entry_of, leg, register_legs
from .test_qa_targets import approve


def crawl_legs(client) -> dict[str, Any]:
    """carol's target of two public legs, both approved, that discovers its screens."""
    legs = [leg("staging", "https://app.example.com", environment="non_production"),
            leg("production", "https://shop.example.com")]
    response = register_legs(client, legs, screens=None, discover=DISCOVER)
    assert response.status_code == 201, response.text
    target = response.json()["qa_target"]
    assert approve(client, target["id"], host="app.example.com", environment="staging").status_code == 200
    assert approve(client, target["id"], host="shop.example.com", environment="production").status_code == 200
    return target


def discover(client, target_id: str, environment: str | None):
    body = {"environment": environment} if environment is not None else {}
    return client.post(f"{TARGETS}/{target_id}:discover", json=body, headers=auth_header("carol"))


def test_a_discovery_names_its_leg_and_stores_on_that_entry_only(client, db, objects, seeded):
    target = crawl_legs(client)
    # MUTATION: discover the first entry when none is named and this is 201.
    assert discover(client, target["id"], None).status_code == 422
    response = discover(client, target["id"], "staging")
    assert response.status_code == 201, response.text
    answer = response.json()
    mark = {"id": target["id"], "revision": target["revision"], "environment": "staging"}
    assert answer["qa_target"] == mark
    [task] = tasks_of(db, answer["workflow_id"])
    assert task["metadata"]["qa_target"] == mark
    assert "https://app.example.com" in task["input"]["prompt"] and "shop.example.com" not in task["input"]["prompt"]
    assert db.docs[f"qa_target_runs/{answer['workflow_id']}"]["environment"] == "staging"
    # MUTATION: hold `discovering` on the target rather than the entry and production's is 409.
    other = discover(client, target["id"], "production")
    assert other.status_code == 201, other.text
    shown = client.get(f"{TARGETS}/{target['id']}", headers=auth_header("carol")).json()["qa_target"]
    assert entry_of(shown, "staging")["discovering"]["workflow_id"] == answer["workflow_id"]
    assert entry_of(shown, "production")["discovering"]["workflow_id"] == other.json()["workflow_id"]
    found = [{"id": "home", "path": "/"}, {"id": "billing", "path": "/billing"},
             {"id": "elsewhere", "path": "https://shop.example.com/x"}]
    finish(client, db, objects, answer["workflow_id"], document=route_list(found))
    shown = client.get(f"{TARGETS}/{target['id']}", headers=auth_header("carol")).json()["qa_target"]
    staging = entry_of(shown, "staging")
    # MUTATION: store the found list on every entry and production's is set too.
    assert staging["discovered"] == [{"id": "home", "path": "/"}, {"id": "billing", "path": "/billing"}]
    assert staging["discovering"] is None
    production = entry_of(shown, "production")
    assert production["discovered"] is None and production["discovering"] is not None
    # Neither an approval nor a stored list bumps the revision: a running leg's mark stays good.
    # MUTATION: bump `revision` in `settle_discovery` and production's running discovery is refused.
    assert shown["revision"] == target["revision"]
