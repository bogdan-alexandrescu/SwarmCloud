"""An abbreviated commit sha as `repository_ref` is refused at submission (F4).

THE DEFECT. task_531f0eeb was submitted with `repository_ref: b7bda42e`. The
worker reads any 7-40 character lowercase hex ref as a commit sha
(`agent_worker.gitops._SHA_RE`) and fetches it by sha, which a forge serves
only for a FULL sha -- so the task was admitted, held capacity, started a
container and failed in the clone. The submission now answers 422 with the
fix in the sentence: a full 40-character sha, or a branch or tag name.

Offline: the real routes over FakeFirestore.
"""

from __future__ import annotations

import pytest

from swarm_api.errors import ValidationFailed
from swarm_api.validation import check_repository_ref

from .conftest import auth_header

REPO = "https://github.com/saga-xyz/example.git"
SHORT = "b7bda42e"
FULL = "b7bda42e" + "0123456789abcdef" * 2


def _task(client, ref: str):
    return client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={"runner_profile": "mock", "repository_url": REPO, "repository_ref": ref},
    )


def _workflow(client, ref: str):
    return client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "repository_url": REPO,
            "repository_ref": ref,
            "steps": [{"step_id": "a", "runner_profile": "mock"}],
        },
    )


@pytest.mark.parametrize("submit", [_task, _workflow], ids=["task", "workflow"])
@pytest.mark.parametrize("ref", [SHORT, "b7bda42", FULL[:39], FULL[:12]])
def test_a_short_sha_ref_is_a_422_naming_the_fix(client, db, submit, ref):
    response = submit(client, ref)

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "validation_failed"
    message = body["message"]
    assert ref in message
    assert "40" in message and "branch" in message and "tag" in message
    assert not any(k.startswith("tasks/") for k in db.docs), "nothing may be written"


@pytest.mark.parametrize("submit", [_task, _workflow], ids=["task", "workflow"])
@pytest.mark.parametrize("ref", [FULL, "main", "release/2026-10", "v1.2.3", "feature-b7bda42e"])
def test_a_full_sha_or_a_branch_or_tag_is_accepted(client, db, submit, ref):
    response = submit(client, ref)

    assert response.status_code == 201, response.text
    stored = [d for k, d in db.docs.items() if k.startswith("tasks/") and k.count("/") == 1]
    assert stored and all(d["repository_ref"] == ref for d in stored)


def test_no_ref_is_still_accepted(client):
    response = client.post(
        "/v1/tasks", headers=auth_header("alice"),
        json={"runner_profile": "mock", "repository_url": REPO},
    )

    assert response.status_code == 201, response.text


def test_the_rule_matches_what_the_worker_reads_as_a_sha():
    """Uppercase hex and anything under 7 characters are branch names to the
    worker (`--branch`), so they are not refused here."""
    for ref in ("B7BDA42E", "abc123", "deadbe", None, ""):
        assert check_repository_ref(ref) == ref
    with pytest.raises(ValidationFailed):
        check_repository_ref("deadbeef")
