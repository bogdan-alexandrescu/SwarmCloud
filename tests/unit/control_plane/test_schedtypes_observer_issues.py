"""The `observer` files its proposals as one epic (docs/schedules.md §3.4; owner decision 2026-10-11).

WHAT IS HELD HERE
-----------------
* THE SCHEDULE NAMES THE REPOSITORY. `file_issues: true` needs
  `file_issues_repo_id`, a registration of the schedule's own tenant that the
  schedule's owner can write: refused at create, at edit, at take-ownership
  and again at fire time -- without one, for another tenant's registration,
  for a `read` grant, and for no grant while grants are enforced. Each is a
  NEW refusal, so it ships report-only: switched off the create goes
  through, and the firing still FILES NOTHING in a repository that failed
  its check.
* ONE EPIC PER REPORT, CLAUDE.md "Issues": the epic form's sections in its
  order, label `epic`, no task item in the body, one comment per proposal,
  each a `- [ ]` box. Written with the tenant's own forge credential, which
  appears in no body; the agent's text is masked and neutralised.
* EXACTLY ONCE: a retried firing writes nothing again; a firing that stopped
  half-way carries on from where it stopped; an issue and comments written
  but never recorded are found again by their markers, not written twice.
* A DRY RUN says what it would file and writes nothing.

No cloud and no emulator: the in-memory Firestore and GitHub as a fake
transport under the real `GitHubWriter`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api import forge
from swarm_api.main import create_app
from swarm_api.schedtypes import observer

from .conftest import api_settings, auth_header, seed_grant
from .test_schedtypes_issue_sweep import (  # noqa: F401 -- fixtures
    ALICE,
    REPOSITORY,
    SCHEDULE_ID,
    ctx,
    firing_for,
    github,
    register,
    tasks,
    writes,
)
from .test_schedtypes_observer import _artifacts, _earlier_report, watch

SWITCHES = ("REFUSAL_OBSERVER_FILE_ISSUES_REPO_REQUIRED",
            "REFUSAL_OBSERVER_FILE_ISSUES_REPO_NOT_REGISTERED",
            "REFUSAL_OBSERVER_FILE_ISSUES_REPO_NOT_WRITABLE")

PROPOSALS = [
    {"title": "Planner tasks wait 30 s to start", "finding": "p95 start latency is 30 s",
     "evidence": "latency.p95_s = 30", "suggestion": "warm one slot"},
    {"finding": "no title: not a proposal"},
    {"title": "Half the pull requests are red first", "finding": "ci.share = 0.5",
     "evidence": "ci.red_on_first_ci = 1 of 2"},
]


@pytest.fixture
def switched_on(monkeypatch) -> None:
    for name in SWITCHES:
        monkeypatch.setenv(name, "on")


@pytest.fixture
def switched_off(monkeypatch) -> None:
    for name in SWITCHES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def client(ctx) -> TestClient:
    return TestClient(create_app(ctx), raise_server_exceptions=False)


def body(**params: Any) -> dict[str, Any]:
    return {"name": "self-review", "type": "observer", "scope": {"mode": "all"},
            "cron": "0 6 * * *", "timezone": "UTC", "params": params}


def filing(db, repo_id: str | None, **extra: Any) -> dict[str, Any]:
    params = {"file_issues": True, **({"file_issues_repo_id": repo_id} if repo_id else {}), **extra}
    return watch(db, params=params)


def epics(writes) -> list[dict[str, Any]]:
    return [i for i in writes.issues.values() if "title" in i]


def reports(db) -> list[dict[str, Any]]:
    """The report tasks a firing submitted (not the earlier report seeded as input)."""
    return [t for t in tasks(db) if t["metadata"].get("observer") and t["id"] != "task_report1"]


def records(db) -> list[dict[str, Any]]:
    return [d for p, d in db.docs.items() if p.startswith(f"{observer.EPICS}/")]


# ---------------------------------------------------------------------------
# The repository the schedule names: at create, edit and take-ownership
# ---------------------------------------------------------------------------


def test_file_issues_without_a_repo_id_is_refused_at_create(client, switched_on) -> None:
    response = client.post("/v1/schedules", json=body(file_issues=True), headers=auth_header("alice"))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "observer_file_issues_repo_required"


def test_another_tenants_repository_is_refused_as_unregistered(db, client, switched_on) -> None:
    theirs = register(db, tenant_id="research")
    response = client.post("/v1/schedules", json=body(file_issues=True, file_issues_repo_id=theirs),
                           headers=auth_header("alice"))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "observer_file_issues_repo_not_registered"
    # The same answer as an id registered nowhere: never an oracle (§5.1).
    nowhere = client.post("/v1/schedules", json=body(file_issues=True, file_issues_repo_id="repo_" + "0" * 16),
                          headers=auth_header("alice"))
    assert nowhere.json()["code"] == "observer_file_issues_repo_not_registered"
    assert nowhere.json()["message"] == response.json()["message"]


def test_a_repository_the_owner_cannot_write_is_refused(db, ctx, client, switched_on) -> None:
    ours = register(db)
    seed_grant(db, "eng", ALICE, REPOSITORY, mode="read")
    response = client.post("/v1/schedules", json=body(file_issues=True, file_issues_repo_id=ours),
                           headers=auth_header("alice"))
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "observer_file_issues_repo_not_writable"

    seed_grant(db, "eng", ALICE, REPOSITORY, mode="write")
    ok = client.post("/v1/schedules", json=body(file_issues=True, file_issues_repo_id=ours),
                     headers=auth_header("alice"))
    assert ok.status_code == 201, ok.text
    assert ok.json()["schedule"]["params"]["file_issues_repo_id"] == ours


def test_no_grant_writes_only_while_grants_are_not_enforced(db, ctx, client, switched_on) -> None:
    ours = register(db)
    ctx.settings = api_settings(repository_grants_enforced=True)
    refused = client.post("/v1/schedules", json=body(file_issues=True, file_issues_repo_id=ours),
                          headers=auth_header("alice"))
    assert refused.json()["code"] == "observer_file_issues_repo_not_writable"

    ctx.settings = api_settings(repository_grants_enforced=False)
    ok = client.post("/v1/schedules", json=body(file_issues=True, file_issues_repo_id=ours),
                     headers=auth_header("alice"))
    assert ok.status_code == 201, ok.text


def test_an_edit_turning_file_issues_on_is_held_to_the_same_rule(db, client, switched_on) -> None:
    created = client.post("/v1/schedules", json=body(), headers=auth_header("alice"))
    assert created.status_code == 201, created.text
    doc = created.json()["schedule"]

    response = client.patch(f"/v1/schedules/{doc['schedule_id']}",
                            json={"revision": doc["revision"], "params": {"file_issues": True}},
                            headers=auth_header("alice"))

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "observer_file_issues_repo_required"


def test_taking_ownership_is_held_to_the_new_owners_grant(db, client, switched_on) -> None:
    ours = register(db)
    seed_grant(db, "eng", ALICE, REPOSITORY, mode="write")
    doc = client.post("/v1/schedules", json=body(file_issues=True, file_issues_repo_id=ours),
                      headers=auth_header("alice")).json()["schedule"]
    seed_grant(db, "eng", "root@saga.xyz", REPOSITORY, mode="read")

    response = client.post(f"/v1/schedules/{doc['schedule_id']}:take-ownership", headers=auth_header("root"))

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "observer_file_issues_repo_not_writable"


def test_switched_off_the_create_goes_through_and_nothing_is_filed(db, ctx, client, writes, monkeypatch,
                                                                   switched_off) -> None:
    theirs = register(db, tenant_id="research")
    created = client.post("/v1/schedules", json=body(file_issues=True, file_issues_repo_id=theirs),
                          headers=auth_header("alice"))
    assert created.status_code == 201, created.text

    _earlier_report(db)
    _artifacts(monkeypatch, ctx, {"task_report1": json.dumps(PROPOSALS)})
    work = observer.create(firing_for(ctx, filing(db, theirs)))

    assert len(work) == 1  # the report is still written
    assert epics(writes) == [] and writes.writes() == [] and records(db) == []


# ---------------------------------------------------------------------------
# Again at fire time
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case, code", [
    ("no_repo", "observer_file_issues_repo_required"),
    ("theirs", "observer_file_issues_repo_not_registered"),
    ("read_grant", "observer_file_issues_repo_not_writable"),
])
def test_the_firing_checks_again_before_it_writes(db, ctx, writes, monkeypatch, switched_on, case, code) -> None:
    ours = register(db)
    repo_id = {"no_repo": None, "theirs": register(db, tenant_id="research"), "read_grant": ours}[case]
    if case == "read_grant":
        # Granted at the edit, narrowed to read since.
        seed_grant(db, "eng", ALICE, REPOSITORY, mode="read")
    _earlier_report(db)
    _artifacts(monkeypatch, ctx, {"task_report1": json.dumps(PROPOSALS)})

    with pytest.raises(observer.ApiError) as caught:
        observer.create(firing_for(ctx, filing(db, repo_id)))

    assert caught.value.code == code
    assert reports(db) == [] and epics(writes) == [] and writes.writes() == []


# ---------------------------------------------------------------------------
# The epic
# ---------------------------------------------------------------------------


def _filed(db, ctx, writes, monkeypatch, proposals=PROPOSALS) -> tuple[dict[str, Any], str]:
    repo_id = register(db)
    _earlier_report(db)
    _artifacts(monkeypatch, ctx, {"task_report1": json.dumps(proposals)})
    schedule = filing(db, repo_id)
    observer.create(firing_for(ctx, schedule))
    return schedule, repo_id


def test_a_report_with_proposals_files_one_epic_and_one_comment_per_proposal(db, ctx, writes, monkeypatch,
                                                                              switched_on) -> None:
    _schedule, repo_id = _filed(db, ctx, writes, monkeypatch)

    (epic,) = epics(writes)
    assert epic["repository"] == REPOSITORY and epic["labels"] == [{"name": "epic"}]
    assert epic["title"].startswith("[epic] Wave ") and "observer findings" in epic["title"]
    text = epic["body"]
    assert text.startswith(observer.epic_marker("task_report1"))
    # The epic form's sections, in its order; and the body is not an index.
    sections = ["### Wave", "### Where these findings came from",
                "### Recorded elsewhere, deliberately not duplicated here"]
    assert [text.index(s) for s in sections] == sorted(text.index(s) for s in sections)
    assert "- [ ]" not in text and "- [x]" not in text.lower()

    comments = sorted(writes.on_issue(epic["number"]), key=lambda c: c["id"])
    assert len(comments) == 2  # the untitled entry is not a proposal
    for position, (comment, title) in enumerate(zip(comments, ["Planner tasks wait 30 s to start",
                                                               "Half the pull requests are red first"])):
        assert comment["body"].startswith(observer.proposal_marker("task_report1", position))
        assert f"- [ ] **{title}**" in comment["body"]
    assert "**Suggestion.** warm one slot" in comments[0]["body"]

    (record,) = records(db)
    assert record["tenant_id"] == "eng" and record["repo_id"] == repo_id and record["complete"] is True
    assert record["number"] == epic["number"] and set(record["comments"]) == {"0", "1"}
    # The report is still submitted, and the inbox still holds the proposals.
    assert len(reports(db)) == 1
    assert len([d for p, d in db.docs.items() if p.startswith("approvals/")]) == 2


def test_the_tenants_own_credential_writes_and_appears_in_no_body(db, ctx, writes, monkeypatch,
                                                                  switched_on) -> None:
    secret = ctx.forge_tokens.token_for(ctx.store.get_tenant("eng"))
    tricky = [{"title": "Ask @octocat; fixes #12", "finding": f"the token {secret} leaked",
               "evidence": "<script>x</script>"}]

    _filed(db, ctx, writes, monkeypatch, proposals=tricky)

    posts = [(u, h, b) for m, u, h, b in writes.calls if m == "POST"]
    assert posts and all(h.get("Authorization", "").endswith(secret) for _u, h, _b in posts)
    assert all(secret not in (b or b"").decode() for _u, _h, b in posts)
    (comment,) = writes.on_issue(epics(writes)[0]["number"])
    assert "@octocat" not in comment["body"] and "fixes #12" not in comment["body"]
    assert "<script>" not in comment["body"]


def test_a_retried_firing_files_nothing_twice(db, ctx, writes, monkeypatch, switched_on) -> None:
    schedule, _repo_id = _filed(db, ctx, writes, monkeypatch)
    before = list(writes.writes())

    observer.create(firing_for(ctx, schedule))  # the same firing, retried
    observer.create(firing_for(ctx, schedule, fid=f"{SCHEDULE_ID}:next"))  # and the next one

    assert len(epics(writes)) == 1 and writes.writes() == before


def test_a_firing_that_stopped_half_way_carries_on_where_it_stopped(db, ctx, writes, monkeypatch,
                                                                    switched_on) -> None:
    repo_id = register(db)
    _earlier_report(db)
    _artifacts(monkeypatch, ctx, {"task_report1": json.dumps(PROPOSALS)})
    schedule = filing(db, repo_id)
    writes.status = {"POST": 502}
    observer.create(firing_for(ctx, schedule))  # GitHub down: logged, the report still submitted
    assert epics(writes) == [] and records(db) == []
    assert len(reports(db)) == 1

    writes.status = {}
    original = writes.__call__.__func__

    class OneComment:
        """Let the epic and its first comment through, then fail."""

        def __init__(self) -> None:
            self.comments = 0

        def __call__(self, fake, method, url, headers, payload, timeout):
            if method == "POST" and url.endswith("/comments"):
                self.comments += 1
                if self.comments > 1:
                    fake.calls.append((method, url, dict(headers), payload))
                    return 502, b"{}"
            return original(fake, method, url, headers, payload, timeout)

    gate = OneComment()
    monkeypatch.setattr(type(writes), "__call__", lambda fake, *a: gate(fake, *a))
    observer.create(firing_for(ctx, schedule, fid=f"{SCHEDULE_ID}:second"))
    (record,) = records(db)
    assert record["complete"] is False and set(record["comments"]) == {"0"}

    monkeypatch.setattr(type(writes), "__call__", original)
    seen = len(writes.calls)
    observer.create(firing_for(ctx, schedule, fid=f"{SCHEDULE_ID}:third"))
    third = [(m, u.split("?")[0].rsplit("/", 1)[-1]) for m, u, _h, _b in writes.calls[seen:]]
    # The recorded comment is neither looked for nor written; the other is
    # looked for once (it may have been posted unrecorded), then written.
    assert third == [("GET", "comments"), ("POST", "comments")]

    (epic,) = epics(writes)
    assert len(writes.on_issue(epic["number"])) == 2
    (record,) = records(db)
    assert record["complete"] is True and set(record["comments"]) == {"0", "1"}


def test_an_epic_written_but_never_recorded_is_found_by_its_marker(db, ctx, writes, monkeypatch,
                                                                   switched_on) -> None:
    schedule, _repo_id = _filed(db, ctx, writes, monkeypatch)
    # The writes succeeded and the Firestore record was lost.
    for path in [p for p in db.docs if p.startswith(f"{observer.EPICS}/")]:
        del db.docs[path]
    before = list(writes.writes())

    observer.create(firing_for(ctx, schedule))

    assert len(epics(writes)) == 1 and writes.writes() == before
    (record,) = records(db)
    assert record["complete"] is True and set(record["comments"]) == {"0", "1"}


def test_a_report_without_proposals_files_no_epic(db, ctx, writes, monkeypatch, switched_on) -> None:
    _filed(db, ctx, writes, monkeypatch, proposals=[])
    assert epics(writes) == [] and records(db) == []
    assert ctx.forge_tokens.asked == []  # the credential is not even read


def test_another_tenants_record_is_never_continued(db, ctx, writes, monkeypatch, switched_on) -> None:
    db.docs[f"{observer.EPICS}/task_report1"] = {"tenant_id": "research", "number": 1, "complete": False}
    _filed(db, ctx, writes, monkeypatch)
    assert epics(writes) == [] and writes.writes() == []


def test_a_missing_credential_files_nothing_and_still_reports(db, ctx, writes, monkeypatch,
                                                              switched_on) -> None:
    ctx.forge_tokens = type(ctx.forge_tokens)(missing=(ctx.store.get_tenant("eng").secret_name(forge.GIT_PROVIDER),))
    _filed(db, ctx, writes, monkeypatch)
    assert epics(writes) == [] and len(reports(db)) == 1


def test_a_dry_run_says_what_it_would_file_and_writes_nothing(db, ctx, writes, monkeypatch, switched_on) -> None:
    repo_id = register(db)
    _earlier_report(db)
    _artifacts(monkeypatch, ctx, {"task_report1": json.dumps(PROPOSALS)})

    output = observer.dry_run(firing_for(ctx, filing(db, repo_id)))

    assert output["file_issues_repository"] == REPOSITORY and output["epics_to_file"] == 1
    assert output["file_issues_refused"] is None
    assert writes.calls == [] and records(db) == [] and reports(db) == []

    refused = observer.dry_run(firing_for(ctx, filing(db, None)))
    assert refused["file_issues_refused"] == "observer_file_issues_repo_required"
    assert refused["epics_to_file"] == 0
