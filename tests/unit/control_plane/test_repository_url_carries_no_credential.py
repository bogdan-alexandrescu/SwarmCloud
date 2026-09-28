"""A repository URL carrying a credential is refused, and one stored before is served masked.

THE PR #229 REVIEW. `repository_url` is caller-supplied, and the usual way to
hand a tool a private repository is to put the token in it. `_repo_scheme`
checked the scheme only, so

    swarm dispatch --repo https://x-access-token:ghp_...@github.com/o/r

stored the token, `GET /v1/tasks/{id}` served it whole with
`input_redaction_count: 0`, and Details drew it in the `repo` fact directly
above the masked prompt. A failed clone then echoed it into stderr, and so
into `last_error`.

The platform's own path for a private repository is the tenant's git secret,
`swarm-tenant-<tenant>-git`, which the worker hands to git in a 0600 file at
clone time. So: userinfo that can carry a credential is a 422 that names that
path, on a task and on a workflow; an ssh login name and the scp form are
not credentials and are accepted; and a URL stored before the refusal is
served with its userinfo masked and counted.

MUTATIONS: drop the userinfo check from `validation.check_repository_url`;
restate it in one schema only; refuse an ssh login name; serve
`task.repository_url` as stored from `task_to_api`.
"""

from __future__ import annotations

import pytest

from swarm_api import validation
from swarm_api.redaction import MASK

from .conftest import auth_header, seed_task, seed_tenant

#: `github_token`-shaped. Not a real token.
TOKEN = "ghp_AbCdEf0123456789AbCdEf0123456789abcd"
#: No recognisable prefix: only the userinfo rule can catch it.
PASSWORD = "correct-horse-battery-staple"

REFUSED = [
    f"https://x-access-token:{TOKEN}@github.com/o/r",
    f"https://{TOKEN}@github.com/o/r.git",
    f"https://bob:{PASSWORD}@git.example.com/team/r",
    f"ssh://git:{PASSWORD}@github.com/o/r.git",
]
ACCEPTED = [
    "https://github.com/o/r",
    "https://github.com/o/r.git?ref=main#readme",
    "ssh://git@github.com/o/r.git",
    "git@github.com:o/r.git",
    # An `@` after the authority is a path, not userinfo.
    "https://github.com/o/r/tree/main@v1",
]


@pytest.mark.parametrize("url", REFUSED)
def test_a_url_with_credential_userinfo_is_refused_by_the_one_rule(url):
    assert validation.repository_userinfo(url) is not None
    with pytest.raises(ValueError, match="swarm-tenant-<tenant>-git"):
        validation.check_repository_url(url)


@pytest.mark.parametrize("url", ACCEPTED)
def test_a_url_with_no_credential_is_accepted_as_written(url):
    assert validation.repository_userinfo(url) is None
    assert validation.check_repository_url(url) == url


@pytest.mark.parametrize("url", REFUSED)
def test_a_task_submitted_with_one_is_a_422_that_names_the_git_secret_and_stores_nothing(
    client, db, url
):
    seed_tenant(db, "eng")
    before = set(db.paths("tasks/"))
    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={"runner_profile": "mock", "input": {"prompt": "go"}, "repository_url": url},
    )
    assert response.status_code == 422, response.text
    assert "swarm-tenant-<tenant>-git" in response.text
    assert TOKEN not in response.text and PASSWORD not in response.text, (
        "the refusal echoed the credential it refused"
    )
    assert set(db.paths("tasks/")) == before, "a refused submission wrote a task"


def test_a_workflow_submitted_with_one_is_refused_by_the_same_rule(client, db):
    seed_tenant(db, "eng")
    response = client.post(
        "/v1/workflows",
        headers=auth_header("alice"),
        json={
            "steps": [{"step_id": "a", "runner_profile": "mock", "input": {"prompt": "go"}}],
            "repository_url": REFUSED[0],
        },
    )
    assert response.status_code == 422, response.text
    assert "swarm-tenant-<tenant>-git" in response.text
    assert TOKEN not in response.text


def test_an_ssh_login_is_accepted_and_served_as_written(client, db):
    """The control: the refusal is about credentials, not about `@`."""
    seed_tenant(db, "eng")
    response = client.post(
        "/v1/tasks",
        headers=auth_header("alice"),
        json={"runner_profile": "mock", "input": {"prompt": "go"},
              "repository_url": "ssh://git@github.com/o/r.git"},
    )
    assert response.status_code == 201, response.text
    task = response.json()["task"]
    assert task["repository_url"] == "ssh://git@github.com/o/r.git"
    assert task["repository_url_redaction_count"] == 0


@pytest.mark.parametrize(
    "stored,served",
    [
        (f"https://x-access-token:{TOKEN}@github.com/o/r", f"https://{MASK}@github.com/o/r"),
        (f"https://bob:{PASSWORD}@git.example.com/team/r", f"https://{MASK}@git.example.com/team/r"),
        (f"ssh://git:{PASSWORD}@github.com/o/r.git", f"ssh://{MASK}@github.com/o/r.git"),
    ],
)
def test_a_url_stored_before_the_refusal_is_served_with_its_userinfo_masked(
    client, db, stored, served
):
    seed_tenant(db, "eng")
    doc = seed_task(db, task_id="task_old", tenant_id="eng", state="SUCCEEDED")
    doc["repository_url"] = stored

    for path in ("/v1/tasks/task_old", "/v1/tasks?limit=50"):
        response = client.get(path, headers=auth_header("alice"))
        assert response.status_code == 200, response.text
        assert TOKEN not in response.text and PASSWORD not in response.text, path
    task = client.get("/v1/tasks/task_old", headers=auth_header("alice")).json()["task"]
    assert task["repository_url"] == served
    assert task["repository_url_redaction_count"] == 1
