"""The graph ticket for an additional repository (docs/design/multi-repository-
steps.md §4.9; lane MR-GRAPH, part of #1434).

A step that reads additional repositories asks for one graph ticket per
indexed repository. The body's optional `repo_id` names which; swarm-api mints
it only when that `repo_id` is one of the TASK's own signed
`additional_repositories` and its registration is this tenant's and still
`readable_by_tasks`. Anything else is the refusal an unregistered repository
gets, so the field never moves a ticket to a repository the step was not
given. A task without the list keeps today's request: naming a `repo_id` is a
422 (test_graph_ticket_api.py holds that too).

Drives the real route over the in-memory Firestore, as test_graph_ticket_api.py
does. Every key is built at runtime (CLAUDE.md).
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from swarm_api.graph_ticket import (
    AdditionalGraphTicketRequest,
    GraphTicketRequest,
    verify_graph_ticket,
)
from swarm_api.repositories import repo_id_for

from .test_graph_ticket_api import (
    DIGEST,
    GRAPH_KEY,
    OTHER_SHA,
    REPO_ID,
    SHA,
    URL,
    ask,
    refused,
    register,
    set_context,
    version,
)
from .test_live_api import (  # noqa: F401 - fixtures are used by name
    TASK,
    Attempt,
    clock,
    groups,
    ldb,
    make_client,
    people,
    relay,
    seed_running,
)

WEB_ID = repo_id_for("eng", "saga", "web")
SDK_ID = repo_id_for("eng", "saga", "sdk")
WEB_SHA = "c" * 40
WEB_COMMIT = "d" * 40


def entry(name: str, repo: str, **over: Any) -> dict[str, Any]:
    return {
        "name": name, "repo_id": repo_id_for("eng", "saga", repo),
        "repository": f"saga/{repo}", "repository_url": f"https://github.com/saga/{repo}",
        "ref": "main", "commit": WEB_COMMIT, "forge_credential": "git",
        "forge_credential_source": "tenant", "size_kib": 1024, **over,
    }


def register_extra(db, repo: str, *, tenant_id: str = "eng", **extra: Any) -> str:
    repo_id = repo_id_for("eng", "saga", repo)
    db.docs[f"repositories/{repo_id}"] = {
        "repo_id": repo_id, "tenant_id": tenant_id, "owner": "saga", "repo": repo,
        "repository_url": f"https://github.com/saga/{repo}", "readable_by_tasks": True,
        **extra,
    }
    return repo_id


@pytest.fixture
def make(ldb, people, groups, objects, clock, relay):
    def _make(**settings: Any):
        return make_client(ldb, people, groups, objects, clock, relay, **settings)
    return _make


@pytest.fixture
def multi(make, ldb):
    """A RUNNING attempt on the primary (indexed at SHA) whose signed list
    names `web` (registered, readable by tasks, indexed at WEB_SHA) and `sdk`
    (registered, readable, never indexed)."""
    client = make(graph_ticket_key=GRAPH_KEY)
    attempt = Attempt(generation=2)
    seed_running(client, ldb, attempt, repository_url=URL,
                 additional_repositories=[entry("web", "web"), entry("sdk", "sdk")])
    register(ldb)
    version(ldb)
    register_extra(ldb, "web")
    register_extra(ldb, "sdk")
    version(ldb, WEB_SHA, repo_id=WEB_ID)
    return client, attempt


def test_the_request_adds_only_an_optional_repo_id_to_todays():
    """The primary's body is the one a worker built before the field: the
    worker's REQUEST_FIELDS are held to GraphTicketRequest, unchanged."""
    assert set(AdditionalGraphTicketRequest.model_fields) == \
        set(GraphTicketRequest.model_fields) | {"repo_id"}
    assert AdditionalGraphTicketRequest.model_fields["repo_id"].default is None


def test_a_signed_additional_repository_gets_its_own_ticket(multi):
    client, attempt = multi
    response = ask(client, attempt, index_sha=WEB_SHA, repo_id=WEB_ID)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["index_sha"] == WEB_SHA and body["manifest_digest"] == DIGEST
    got = verify_graph_ticket([GRAPH_KEY], body["ticket"], now=time.time())
    assert got["repo_id"] == WEB_ID and got["index_sha"] == WEB_SHA
    assert got["tenant_id"] == "eng" and got["task_id"] == TASK and got["generation"] == 2


def test_leaving_repo_id_out_is_still_the_tasks_own_repository(multi):
    client, attempt = multi
    response = ask(client, attempt)
    assert response.status_code == 200, response.text
    got = verify_graph_ticket([GRAPH_KEY], response.json()["ticket"], now=time.time())
    assert got["repo_id"] == REPO_ID


def test_a_repo_id_not_in_the_signed_list_is_refused_even_when_registered_and_indexed(
        multi, ldb):
    """`gadgets` is this tenant's, readable by tasks and indexed; the task was
    never given it. The primary's own repo_id is not an entry either."""
    client, attempt = multi
    gadgets = register_extra(ldb, "gadgets")
    version(ldb, OTHER_SHA, repo_id=gadgets)
    body = refused(ask(client, attempt, index_sha=OTHER_SHA, repo_id=gadgets), 404)
    assert body["code"] == "no_graph"
    refused(ask(client, attempt, repo_id=REPO_ID), 404)


def test_a_task_without_the_list_cannot_name_a_repo_id(make, ldb):
    client = make(graph_ticket_key=GRAPH_KEY)
    attempt = Attempt(generation=2)
    seed_running(client, ldb, attempt, repository_url=URL)
    register(ldb)
    version(ldb)
    register_extra(ldb, "web")
    version(ldb, WEB_SHA, repo_id=WEB_ID)
    body = refused(ask(client, attempt, index_sha=WEB_SHA, repo_id=WEB_ID), 422)
    assert body["code"] == "validation_failed"


def test_a_registration_no_longer_readable_by_tasks_gets_no_ticket(multi, ldb):
    client, attempt = multi
    for off in (False, None, "true"):
        register_extra(ldb, "web", readable_by_tasks=off)
        refused(ask(client, attempt, index_sha=WEB_SHA, repo_id=WEB_ID), 404)
    register_extra(ldb, "web")
    assert ask(client, attempt, index_sha=WEB_SHA, repo_id=WEB_ID).status_code == 200


def test_another_tenants_registration_is_never_used(multi, ldb):
    client, attempt = multi
    register_extra(ldb, "web", tenant_id="research")
    refused(ask(client, attempt, index_sha=WEB_SHA, repo_id=WEB_ID), 404)


def test_an_entry_whose_repo_id_is_not_its_repositorys_reaches_nothing(make, ldb):
    """A list rewritten to pair `web`'s registration with another name: the
    repo_id is re-derived from `repository`, so nothing is minted."""
    client = make(graph_ticket_key=GRAPH_KEY)
    attempt = Attempt(generation=2)
    seed_running(client, ldb, attempt, repository_url=URL,
                 additional_repositories=[entry("web", "sdk", repo_id=WEB_ID)])
    register(ldb)
    register_extra(ldb, "web")
    version(ldb, WEB_SHA, repo_id=WEB_ID)
    refused(ask(client, attempt, index_sha=WEB_SHA, repo_id=WEB_ID), 404)


def test_a_malformed_list_mints_nothing(make, ldb):
    client = make(graph_ticket_key=GRAPH_KEY)
    attempt = Attempt(generation=2)
    seed_running(client, ldb, attempt, repository_url=URL,
                 additional_repositories=[entry("web", "web", forge_credential="git-r-x")])
    register(ldb)
    register_extra(ldb, "web")
    version(ldb, WEB_SHA, repo_id=WEB_ID)
    refused(ask(client, attempt, index_sha=WEB_SHA, repo_id=WEB_ID), 404)


def test_the_index_sha_must_be_a_version_of_the_named_repository(multi):
    """The primary's SHA is promoted for the primary only, and `sdk` has no
    index at all: neither is a graph of the repository asked for."""
    client, attempt = multi
    refused(ask(client, attempt, index_sha=SHA, repo_id=WEB_ID), 404)
    refused(ask(client, attempt, index_sha=WEB_SHA, repo_id=SDK_ID), 404)


def test_the_named_repositorys_context_setting_decides(multi, ldb):
    client, attempt = multi
    register_extra(ldb, "web", context="off")
    body = refused(ask(client, attempt, index_sha=WEB_SHA, repo_id=WEB_ID), 403)
    assert body["detail"]["reason"] == "context_disabled"
    assert ask(client, attempt).status_code == 200
    set_context(ldb, "off")
    register_extra(ldb, "web", context="on")
    assert ask(client, attempt, index_sha=WEB_SHA, repo_id=WEB_ID).status_code == 200


def test_a_malformed_repo_id_is_refused_by_shape(multi):
    client, attempt = multi
    for bad in ("repo_" + "A" * 16, "saga/web", "", 7):
        refused(ask(client, attempt, index_sha=WEB_SHA, repo_id=bad), 422)


def test_the_fence_still_comes_first(multi, ldb):
    client, attempt = multi
    ldb.docs[f"tasks/{TASK}"]["cancel_requested"] = True
    body = refused(ask(client, attempt, index_sha=WEB_SHA, repo_id=WEB_ID), 409)
    assert body["detail"]["reason"] == "fenced"
