"""`GET /v1/workflows?claude_session=&repository=` serves only the matching rows.

THE DEFECT (#1749, measured 2026-10-20 in tenant eng). The sc plugin's check,
attach-all and workflows views read at most 4 pages of 50 running workflows
and then stopped, so a session's work older than the tenant's newest 200
running workflows was never flagged. Owner decision 2026-10-20: the route
filters, on the server, by the Claude Code session and the repository a
caller scopes to, rather than the bridge paging to the end of everyone's work.

Neither is a field of the frozen `Workflow`: the session is the step tasks'
`metadata.claude_session` and the repository their `repository_url`. So the
route derives a page, keeps the rows whose step tasks match, and pages on
inside the request until a page of matches is full or the listing ends.

Offline: the real routes over FakeFirestore.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from swarm_api.codec import workflow_to_firestore
from swarm_api.routes import workflows as route
from swarm_common.models import Workflow, WorkflowStep
from swarm_common.states import TaskState

from .conftest import auth_header, seed_task, seed_tenant

T0 = datetime(2026, 10, 20, 20, 0, tzinfo=timezone.utc)
MINE = "0f0e0d0c-aaaa-4bbb-8ccc-000000000001"
THEIRS = "0f0e0d0c-aaaa-4bbb-8ccc-000000000002"
APP = "https://github.com/acme/app.git"
OTHER = "https://github.com/acme/other"


def _workflow(db, workflow_id: str, *, created_at: datetime, sid: str | None,
              repo: str | None, state: str = "RUNNING") -> None:
    task_id = f"task_{workflow_id}"
    doc = seed_task(db, task_id=task_id, tenant_id="eng", state=state,
                    created_at=created_at, workflow_id=workflow_id)
    doc["metadata"] = {"claude_session": sid} if sid else {}
    doc["repository_url"] = repo
    db.docs[f"tasks/{task_id}"] = doc
    workflow = Workflow(
        workflow_id=workflow_id, tenant_id="eng", created_at=created_at, updated_at=created_at,
        state=TaskState(state), submitted_by="alice@example.com",
        steps=[WorkflowStep(step_id="only", runner_profile="mock", input={},
                            depends_on=[], task_id=task_id)],
    )
    db.docs[f"workflows/{workflow_id}"] = workflow_to_firestore(workflow)


def _tenant(db) -> dict[str, str]:
    """120 running workflows of another session, newest first, with this
    session's three spread among them -- one of them the OLDEST of all --
    and one untagged and one of this session's in another repository."""
    seed_tenant(db, "eng")
    wanted = {"wf_mine_new": 5, "wf_mine_mid": 60, "wf_mine_old": 500}
    for i in range(120):
        _workflow(db, f"wf_theirs_{i:03d}", created_at=T0 - timedelta(minutes=i), sid=THEIRS, repo=APP)
    for wid, minutes in wanted.items():
        _workflow(db, wid, created_at=T0 - timedelta(minutes=minutes, seconds=30), sid=MINE, repo=APP)
    _workflow(db, "wf_untagged", created_at=T0 - timedelta(minutes=7), sid=None, repo=APP)
    _workflow(db, "wf_mine_elsewhere", created_at=T0 - timedelta(minutes=8), sid=MINE, repo=OTHER)
    _workflow(db, "wf_mine_done", created_at=T0 - timedelta(minutes=9), sid=MINE, repo=APP,
              state="SUCCEEDED")
    return wanted


def _list(client, **params):
    response = client.get("/v1/workflows", params={"active": "true", **params},
                          headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    return response.json()


def _walk(client, **params) -> tuple[list[str], list[dict]]:
    ids: list[str] = []
    bodies: list[dict] = []
    token = None
    while True:
        body = _list(client, **params, **({"page_token": token} if token else {}))
        bodies.append(body)
        ids += [w["workflow_id"] for w in body["workflows"]]
        token = body["next_page_token"]
        if token is None:
            return ids, bodies
        # MUTATION: a token that never ends (the route re-serving its own
        # token) would loop here; 20 pages is far past any honest walk.
        assert len(bodies) < 20, "the filtered listing never ended"


def test_the_session_filter_serves_only_that_sessions_running_workflows(db, client):
    _tenant(db)
    body = _list(client, claude_session=MINE, limit=50)
    # MUTATION: drop the session filter in `list_workflows` and the first page
    # is the 50 newest of the other session's workflows.
    assert [w["workflow_id"] for w in body["workflows"]] == [
        "wf_mine_new", "wf_mine_elsewhere", "wf_mine_mid", "wf_mine_old",
    ]
    # MUTATION: stop paging inside the request after one store page and the
    # oldest (behind 120 newer rows) comes back on a later page, not this one.
    assert body["next_page_token"] is None
    assert {w["claude_session"] for w in body["workflows"]} == {MINE}
    # MUTATION: echo nothing and a bridge cannot tell this deployment
    # filtered from one that ignored the parameter.
    assert body["filter"]["claude_session"] == MINE
    assert body["filter"]["repository"] is None
    # What was left out, counted (SC-SESSION-SCOPE reports it). MUTATION:
    # count an untagged row as another session's and these swap.
    assert body["filter"]["left_out"] == {"other_session": 120, "untagged": 1, "other_repository": 0}


def test_the_repository_filter_reads_any_spelling_of_one_repository(db, client):
    _tenant(db)
    spellings = ["acme/app", "github.com/acme/app", "https://github.com/ACME/app",
                 "git@github.com:acme/app.git", "ssh://git@github.com/acme/app"]
    for spelling in spellings:
        ids, bodies = _walk(client, repository=spelling, claude_session=MINE, limit=50)
        # MUTATION: compare the raw URL instead of its normalised key and
        # `https://github.com/acme/app.git` matches none of these spellings.
        assert ids == ["wf_mine_new", "wf_mine_mid", "wf_mine_old"], spelling
        assert bodies[0]["filter"]["repository"] == "github.com/acme/app"
        # MUTATION: drop the repository half and wf_mine_elsewhere is served.
        assert bodies[-1]["filter"]["left_out"]["other_repository"] == 1


def test_the_repository_filter_alone_keeps_every_session(db, client):
    _tenant(db)
    ids, _ = _walk(client, repository=OTHER, limit=50)
    # MUTATION: make the repository filter imply a session and this is empty.
    assert ids == ["wf_mine_elsewhere"]


def test_a_small_page_of_matches_is_a_page_and_its_token_continues_past_it(db, client):
    """`limit` caps the MATCHES, and a page that ends inside a store page
    hands back a token that continues strictly after the last row served:
    none twice, none skipped."""
    wanted = _tenant(db)
    ids, bodies = _walk(client, claude_session=MINE, repository="acme/app", limit=1)
    # MUTATION: hand back the store page's token after overshooting `limit`
    # and wf_mine_mid -- matched in the same store page as wf_mine_new -- is
    # skipped.
    assert ids == list(wanted)
    assert [len(b["workflows"]) for b in bodies] == [1, 1, 1]


def test_a_finished_workflow_of_the_session_is_still_left_out_by_active(db, client):
    _tenant(db)
    ids, _ = _walk(client, claude_session=MINE, limit=50)
    # MUTATION: filter on the session before the derived-state filter and
    # the SUCCEEDED one rides along.
    assert "wf_mine_done" not in ids


def test_the_route_scans_a_bounded_number_of_store_pages_per_request(db, client, monkeypatch):
    """A request reads at most `MAX_FILTER_PAGES` store pages of
    `FILTER_PAGE_SIZE`; past them it answers with what it matched and a token,
    and the walk goes on."""
    _tenant(db)
    monkeypatch.setattr(route, "MAX_FILTER_PAGES", 1)
    monkeypatch.setattr(route, "FILTER_PAGE_SIZE", 50)
    first = _list(client, claude_session=MINE, limit=50)
    # MUTATION: ignore the bound and the first answer holds the oldest too.
    assert [w["workflow_id"] for w in first["workflows"]] == ["wf_mine_new", "wf_mine_elsewhere"]
    assert first["next_page_token"] is not None
    ids, _ = _walk(client, claude_session=MINE, limit=50)
    assert ids[-1] == "wf_mine_old"


@pytest.mark.parametrize("params", [{"claude_session": "not a session"}, {"repository": "nothing"}])
def test_a_filter_that_names_nothing_is_refused(db, client, params):
    seed_tenant(db, "eng")
    response = client.get("/v1/workflows", params=params, headers=auth_header("alice"))
    # MUTATION: accept it and an unscoped page is served as though it matched.
    assert response.status_code == 422, response.text


def test_ids_takes_no_scope_filter(db, client):
    seed_tenant(db, "eng")
    response = client.get("/v1/workflows", params={"ids": "wf_x", "claude_session": MINE},
                          headers=auth_header("alice"))
    # MUTATION: accept it and `ids` serves rows the filter would have dropped.
    assert response.status_code == 422, response.text


SPELLINGS = [
    "acme/app", "github.com/acme/app", "https://github.com/acme/app",
    "https://github.com/Acme/App.git", "https://user@github.com/acme/app/",
    "git@github.com:acme/app.git", "ssh://git@github.com:22/acme/app.git",
    "https://gitlab.example.com:8443/group/proj", "gitlab.example.com/group/proj",
    "http://github.com/acme/app", "file:///tmp/app", "/tmp/app", "", "acme",
    "https://github.com/acme",
]


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_the_route_normalises_a_repository_as_the_bridge_does(spelling):
    from swarm_mcp.check import repo_key

    # MUTATION: keep the port, the `.git` or the case on either side and one
    # of these spellings scopes to a different repository than the bridge.
    assert route.repository_key(spelling) == repo_key(spelling)
