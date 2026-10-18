"""The merge train coordinator over a fake GitHub (TRAIN-WS, docs/merge-trains.md §4-§5).

A merge step in train mode registers its pull request in
`merge_train_entries` and parks; this coordinator -- one lane per (tenant,
repository, base), run on the per-tenant `merge_wake` tick -- composes a train
of up to N of them on a `swarm-train/...` branch from the base's head, opens a
DRAFT pull request so the base's required checks run on the composed head,
and when they settle:

  * green: squash-merges each pull request in order, pinned to the head it was
    registered at (`sha`), then reads that the base's tree is the tested tree;
  * red, several: bisects by halves (the first half rides next);
  * red, one: ejects it, with the failing checks named;
  * older than the workspace's max train age: treated as red;

and marks each step's park so the scheduler wakes it to read its outcome.
A waiting train is documents only: nothing runs between ticks (invariant 1).

The fake: a commit's "tree" is the set of pull requests it contains, so a
squash of #1 then #2 onto main has the same tree as a train branch that
merged #1 and #2 -- exactly the equality the coordinator checks.

No credentials, no network. Every token is built at runtime.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from swarm_api import forgewrite, mergesettings, mergetrain, mergewake
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker
from swarm_common.states import ParkReason

from . import forge_fakes
from .conftest import api_settings, seed_task, seed_tenant

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
OWNER, REPO = "saga-xyz", "widgets"
REPO_URL = f"https://github.com/{OWNER}/{REPO}"
API = f"/repos/{OWNER}/{REPO}"
CHECK = "ci / unit"
SWEEPER = "swarm-rollup-sweeper@saga-agents-staging.iam.gserviceaccount.com"


def _sha(*parts: Any) -> str:
    return hashlib.sha1(json.dumps([str(p) for p in parts]).encode()).hexdigest()


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class TrainGitHub:
    """One repository: main, pull requests, branches, merges, checks.

    `verdict(numbers)` is CI on a commit containing those pull requests:
    "success", "failure" or None (still running)."""

    def __init__(self) -> None:
        self.contents: dict[str, frozenset[int]] = {}
        self.main_landed: list[int] = []
        self.branches: dict[str, str] = {"main": self._commit(frozenset())}
        self.pulls: dict[int, dict[str, Any]] = {}
        self.conflicts: set[frozenset[int]] = set()
        self.refuse_land: dict[int, tuple[int, str]] = {}
        self.verdict: Callable[[frozenset[int]], str | None] = lambda _n: "success"
        self.calls: list[tuple[str, str, Any]] = []
        self.next_pull = 900

    def _commit(self, numbers: frozenset[int], salt: Any = "") -> str:
        sha = _sha(sorted(numbers), salt)
        self.contents[sha] = numbers
        return sha

    def tree(self, sha: str) -> str:
        return _sha("tree", sorted(self.contents[sha]))

    def open_pull(self, number: int) -> str:
        head = self._commit(frozenset({number}), salt=f"pr{number}")
        self.pulls[number] = {
            "number": number, "state": "open", "merged": False, "draft": False,
            "title": f"Change {number}", "mergeable": True, "mergeable_state": "clean",
            "head": {"ref": f"swarm/task_{number}", "sha": head,
                     "repo": {"full_name": f"{OWNER}/{REPO}"}},
            "base": {"ref": "main", "repo": {"full_name": f"{OWNER}/{REPO}"}},
        }
        return head

    def path_calls(self, method: str, prefix: str) -> list[tuple[str, str, Any]]:
        return [c for c in self.calls if c[0] == method and c[1].startswith(prefix)]

    # -- the transport GitHubWriter sends through ---------------------------
    def __call__(self, method, url, headers, body, timeout):
        path = urlparse(url).path
        payload = json.loads(body) if body else None
        self.calls.append((method, path, payload))
        status, answer = self._route(method, path, payload)
        return status, (json.dumps(answer).encode() if answer is not None else b"")

    def _route(self, method: str, path: str, payload: Any) -> tuple[int, Any]:
        if not path.startswith(API):
            return 404, {"message": "Not Found"}
        rest = path[len(API):]
        if method == "GET" and rest.startswith("/git/ref/heads/"):
            branch = rest[len("/git/ref/heads/"):]
            if branch not in self.branches:
                return 404, {"message": "Not Found"}
            return 200, {"object": {"sha": self.branches[branch]}}
        if method == "POST" and rest == "/git/refs":
            branch = payload["ref"][len("refs/heads/"):]
            if branch in self.branches:
                return 422, {"message": "Reference already exists"}
            self.branches[branch] = payload["sha"]
            return 201, {"ref": payload["ref"], "object": {"sha": payload["sha"]}}
        if method == "DELETE" and rest.startswith("/git/refs/heads/"):
            self.branches.pop(rest[len("/git/refs/heads/"):], None)
            return 204, None
        if method == "GET" and rest.startswith("/git/commits/"):
            sha = rest[len("/git/commits/"):]
            return 200, {"sha": sha, "tree": {"sha": self.tree(sha)}}
        if method == "POST" and rest == "/merges":
            base, head = payload["base"], payload["head"]
            have = self.contents[self.branches[base]]
            add = self.contents[head]
            if add <= have:
                return 204, None
            for pair in self.conflicts:
                if pair <= (have | add):
                    return 409, {"message": "Merge conflict"}
            sha = self._commit(have | add, salt=base)
            self.branches[base] = sha
            return 201, {"sha": sha}
        if method == "POST" and rest == "/pulls":
            self.next_pull += 1
            number = self.next_pull
            head = self.branches[payload["head"]]
            self.pulls[number] = {
                "number": number, "state": "open", "merged": False,
                "draft": payload.get("draft", False), "title": payload["title"],
                "head": {"ref": payload["head"], "sha": head,
                         "repo": {"full_name": f"{OWNER}/{REPO}"}},
                "base": {"ref": payload["base"], "repo": {"full_name": f"{OWNER}/{REPO}"}},
            }
            return 201, self.pulls[number]
        if rest.startswith("/pulls/"):
            parts = rest.split("/")
            number = int(parts[2])
            pull = self.pulls.get(number)
            if pull is None:
                return 404, {"message": "Not Found"}
            if method == "GET" and len(parts) == 3:
                return 200, pull
            if method == "PATCH" and len(parts) == 3:
                pull.update({k: v for k, v in payload.items() if k in ("state", "title", "body")})
                return 200, pull
            if method == "PUT" and parts[3:] == ["merge"]:
                if number in self.refuse_land:
                    status, message = self.refuse_land[number]
                    return status, {"message": message}
                if payload.get("sha") != pull["head"]["sha"]:
                    return 409, {"message": "Head branch was modified."}
                assert payload.get("merge_method") == "squash"
                self.main_landed.append(number)
                sha = self._commit(frozenset(self.main_landed), salt="main")
                self.branches["main"] = sha
                pull.update(merged=True, state="closed", merge_commit_sha=sha)
                return 200, {"merged": True, "sha": sha, "message": "merged"}
        if method == "GET" and rest == "/rules/branches/main":
            return 200, [{"type": "required_status_checks", "parameters": {
                "required_status_checks": [{"context": CHECK}]}}]
        if method == "GET" and rest.startswith("/commits/") and rest.endswith("/check-runs"):
            sha = rest.split("/")[2]
            verdict = self.verdict(self.contents.get(sha, frozenset()))
            runs = [] if verdict is None else [
                {"name": CHECK, "status": "completed", "conclusion": verdict}]
            if verdict is None:
                runs = [{"name": CHECK, "status": "in_progress", "conclusion": None}]
            return 200, {"total_count": len(runs), "check_runs": runs}
        if method == "GET" and rest.startswith("/commits/") and rest.endswith("/status"):
            return 200, {"state": "pending", "statuses": []}
        return 404, {"message": f"unrouted {method} {rest}"}


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def github() -> TrainGitHub:
    return TrainGitHub()


@pytest.fixture
def ctx(db, tokens, group_map, objects, github, clock):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    tokens = dict(tokens)
    tokens["token-sweeper"] = {"email": SWEEPER, "email_verified": True, "sub": "sub-sweeper"}
    return build_context(
        settings=api_settings(rollup_sweeper_users=(SWEEPER,)),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        now=clock,
        forge_tokens=forge_fakes.AnyTenantTokens(),
        forge_writer=forgewrite.GitHubWriter(send=github),
    )


def _settings(db, tenant_id="eng", **values):
    db.docs[f"{mergesettings.COLLECTION}/{tenant_id}"] = {mergesettings.FIELD: {
        "tenant_id": tenant_id, "mode": "train",
        "train_size": mergesettings.TRAIN_SIZE_DEFAULT,
        "max_train_age_minutes": mergesettings.MAX_TRAIN_AGE_DEFAULT, **values,
    }}


def _ride(db, github, number, *, tenant_id="eng", minute=0, state="PARKED", code=None):
    """A merge step in train mode, parked on its registered entry."""
    head = github.open_pull(number) if number not in github.pulls else github.pulls[number]["head"]["sha"]
    task_id = f"task_m{number}" if tenant_id == "eng" else f"task_{tenant_id}_{number}"
    doc = seed_task(db, task_id=task_id, tenant_id=tenant_id, state=state,
                    runner_profile="merge",
                    park_reason=ParkReason.CI_PENDING.value if state == "PARKED" else None)
    doc["repository_url"] = REPO_URL
    doc["metadata"] = {"merge_wait": {
        "code": code or mergetrain.TRAIN_WAITING, "head": head, "pull_request": number}}
    entry_id = mergetrain.entry_id_for(tenant_id, task_id)
    db.docs[f"{mergetrain.ENTRIES_COLLECTION}/{entry_id}"] = {
        "entry_id": entry_id, "tenant_id": tenant_id, "task_id": task_id,
        "repository": f"{OWNER}/{REPO}", "repository_url": REPO_URL, "base": "main",
        "number": number, "head": head, "title": f"Change {number}",
        "message": f"Merged by SwarmCloud task {task_id}",
        "state": mergetrain.QUEUED, "returns": 0,
        "enqueued_at": NOW + timedelta(minutes=minute), "updated_at": NOW,
    }
    return entry_id, task_id


def _entry(db, entry_id) -> dict:
    return db.docs[f"{mergetrain.ENTRIES_COLLECTION}/{entry_id}"]


def _woken(db, task_id) -> bool:
    return bool(db.docs[f"tasks/{task_id}"]["metadata"]["merge_wait"].get(mergewake.WAKE_MARKER))


def _tick(ctx, clock, minutes: float = 2):
    report = mergetrain.tick_tenant(ctx, "eng", limit=50)
    clock.now += timedelta(minutes=minutes)
    return report


def _run_until_quiet(ctx, clock, ticks=30):
    for _ in range(ticks):
        report = _tick(ctx, clock)
        if not report.active:
            return


# --------------------------------------------------------------------------
# a green train
# --------------------------------------------------------------------------

def test_a_green_train_lands_each_pull_request_in_order_pinned_to_its_head(ctx, db, github, clock):
    _settings(db)
    rides = [_ride(db, github, n, minute=i) for i, n in enumerate((11, 12, 13))]

    report = _tick(ctx, clock)
    assert report.composed == 1
    # A draft pull request from a swarm-train branch into main, carrying all three.
    (opened,) = github.path_calls("POST", f"{API}/pulls")
    assert opened[2]["draft"] is True and opened[2]["base"] == "main"
    assert opened[2]["head"].startswith(mergetrain.BRANCH_PREFIX)
    merged_in = [c[2]["head"] for c in github.path_calls("POST", f"{API}/merges")]
    assert merged_in == [github.pulls[n]["head"]["sha"] for n in (11, 12, 13)]
    assert all(_entry(db, e)["state"] == mergetrain.RIDING for e, _ in rides)
    assert not github.path_calls("PUT", f"{API}/pulls/")

    _tick(ctx, clock)
    landed = github.path_calls("PUT", f"{API}/pulls/")
    assert [c[1] for c in landed] == [f"{API}/pulls/{n}/merge" for n in (11, 12, 13)]
    assert [c[2]["sha"] for c in landed] == [github.pulls[n]["head"]["sha"] for n in (11, 12, 13)]
    assert github.main_landed == [11, 12, 13]
    for entry_id, task_id in rides:
        entry = _entry(db, entry_id)
        assert entry["state"] == mergetrain.LANDED
        assert entry["merge_commit"]
        assert _woken(db, task_id)
    lane = next(doc for path, doc in db.docs.items()
                if path.startswith(f"{mergetrain.LANES_COLLECTION}/"))
    assert lane["train"] is None
    assert lane["last"]["outcome"] == "landed"
    assert lane["last"]["tree_verified"] is True
    # The draft is closed and the branch deleted.
    draft = github.pulls[max(github.pulls)]
    assert draft["state"] == "closed"
    assert not any(b.startswith(mergetrain.BRANCH_PREFIX) for b in github.branches)


def test_a_train_waits_for_pending_checks_and_costs_nothing_meanwhile(ctx, db, github, clock):
    _settings(db)
    github.verdict = lambda _n: None
    (entry_id, task_id), = [_ride(db, github, 21)]
    _tick(ctx, clock)
    for _ in range(5):
        report = _tick(ctx, clock)
        assert report.waiting == 1
    assert _entry(db, entry_id)["state"] == mergetrain.RIDING
    assert not _woken(db, task_id)
    assert github.main_landed == []
    # Nothing is created but documents and a branch: no task, no lease.
    assert db.docs[f"tasks/{task_id}"]["state"] == "PARKED"


def test_the_train_size_comes_from_the_workspace_settings(ctx, db, github, clock):
    _settings(db, train_size=2)
    for i, n in enumerate((31, 32, 33)):
        _ride(db, github, n, minute=i)
    _tick(ctx, clock)
    assert len(github.path_calls("POST", f"{API}/merges")) == 2
    _run_until_quiet(ctx, clock)
    assert github.main_landed == [31, 32, 33]


# --------------------------------------------------------------------------
# red: bisect, eject
# --------------------------------------------------------------------------

def test_a_red_train_is_bisected_and_only_the_culprit_is_ejected(ctx, db, github, clock):
    _settings(db)
    github.verdict = lambda numbers: "failure" if 43 in numbers else "success"
    rides = {n: _ride(db, github, n, minute=i) for i, n in enumerate((41, 42, 43, 44))}
    _run_until_quiet(ctx, clock)
    assert github.main_landed == [41, 42, 44]
    culprit, culprit_task = rides[43]
    entry = _entry(db, culprit)
    assert entry["state"] == mergetrain.EJECTED
    assert entry["reason"] == "train_red"
    assert CHECK in entry["detail"]
    assert _woken(db, culprit_task)
    for n in (41, 42, 44):
        assert _entry(db, rides[n][0])["state"] == mergetrain.LANDED


def test_a_train_past_its_max_age_is_treated_as_red(ctx, db, github, clock):
    _settings(db, max_train_age_minutes=30)
    github.verdict = lambda _n: None
    entry_id, task_id = _ride(db, github, 51)
    _tick(ctx, clock)
    clock.now += timedelta(minutes=31)
    _tick(ctx, clock)
    entry = _entry(db, entry_id)
    assert entry["state"] == mergetrain.EJECTED and entry["reason"] == "train_timed_out"
    assert _woken(db, task_id)
    assert github.main_landed == []


# --------------------------------------------------------------------------
# composition and landing refusals
# --------------------------------------------------------------------------

def test_a_pull_request_that_conflicts_with_the_train_is_returned_to_its_step(ctx, db, github, clock):
    _settings(db)
    a = _ride(db, github, 61, minute=0)
    b = _ride(db, github, 62, minute=1)
    github.conflicts.add(frozenset({61, 62}))
    _tick(ctx, clock)
    returned = _entry(db, b[0])
    assert returned["state"] == mergetrain.RETURNED and returned["reason"] == "train_conflict"
    assert returned["returns"] == 1
    assert _woken(db, b[1])
    assert _entry(db, a[0])["state"] == mergetrain.RIDING
    _tick(ctx, clock)
    assert github.main_landed == [61]


def test_a_pull_request_returned_too_often_is_ejected(ctx, db, github, clock):
    _settings(db)
    _ride(db, github, 63, minute=0)
    entry_id, _ = _ride(db, github, 64, minute=1)
    _entry(db, entry_id)["returns"] = mergetrain.MAX_RETURNS - 1
    github.conflicts.add(frozenset({63, 64}))
    _tick(ctx, clock)
    entry = _entry(db, entry_id)
    assert entry["state"] == mergetrain.EJECTED and entry["reason"] == "train_conflict"


def test_a_pull_request_whose_head_moved_is_returned_not_composed(ctx, db, github, clock):
    _settings(db)
    entry_id, task_id = _ride(db, github, 71)
    github.pulls[71]["head"]["sha"] = "f" * 40
    _tick(ctx, clock)
    entry = _entry(db, entry_id)
    assert entry["state"] == mergetrain.RETURNED and entry["reason"] == "head_moved"
    assert not github.path_calls("POST", f"{API}/merges")


def test_a_refused_landing_returns_that_pull_request_and_requeues_the_rest(ctx, db, github, clock):
    _settings(db)
    rides = [_ride(db, github, n, minute=i) for i, n in enumerate((81, 82, 83))]
    github.refuse_land[82] = (405, "Required status check is expected.")
    _tick(ctx, clock)
    _tick(ctx, clock)
    assert github.main_landed == [81]
    assert _entry(db, rides[0][0])["state"] == mergetrain.LANDED
    refused = _entry(db, rides[1][0])
    assert refused["state"] == mergetrain.RETURNED and refused["reason"] == "land_refused"
    assert "Required status check" in refused["detail"]
    assert _entry(db, rides[2][0])["state"] == mergetrain.QUEUED


# --------------------------------------------------------------------------
# what may ride
# --------------------------------------------------------------------------

def test_an_entry_whose_step_is_not_parked_on_the_train_does_not_ride_yet(ctx, db, github, clock):
    _settings(db)
    entry_id, _ = _ride(db, github, 91, state="RUNNING")
    _tick(ctx, clock)
    assert _entry(db, entry_id)["state"] == mergetrain.QUEUED
    assert not github.path_calls("POST", f"{API}/merges")


def test_an_entry_whose_step_ended_is_dropped(ctx, db, github, clock):
    _settings(db)
    entry_id, _ = _ride(db, github, 92, state="CANCELLED")
    _tick(ctx, clock)
    entry = _entry(db, entry_id)
    assert entry["state"] == mergetrain.EJECTED and entry["reason"] == "step_ended"
    assert not github.path_calls("POST", f"{API}/merges")


def test_another_tenants_entries_never_ride_on_this_tenants_tick(ctx, db, github, clock):
    _settings(db)
    _settings(db, tenant_id="research")
    theirs, _ = _ride(db, github, 95, tenant_id="research")
    _tick(ctx, clock)
    assert _entry(db, theirs)["state"] == mergetrain.QUEUED
    assert not github.path_calls("POST", f"{API}/merges")


# --------------------------------------------------------------------------
# the tick
# --------------------------------------------------------------------------

def test_the_merge_wake_route_runs_the_train_and_skips_train_parks(ctx, db, github, clock):
    _settings(db)
    _ride(db, github, 99)
    client = TestClient(create_app(ctx), raise_server_exceptions=False)
    response = client.post("/v1/admin/merges/wake", params={"tenant_id": "eng"},
                           headers={"Authorization": "Bearer token-sweeper"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["merge_train"]["composed"] == 1
    # The CI wake read nothing for the train park: it is the coordinator's.
    assert body["report"]["visited"] == 0
