"""The SwarmCloud merge train gathers before it composes (lane 5, docs/design/merging-without-a-laptop.md §7).

A train composes once `gather_min` riders are eligible, or once the oldest
has waited `gather_wait_minutes`, so a lone change is never held long and a
busy repository still halves its CI runs. The four new settings are bounded
scalars on the workspace merge setting, with a per-repository override of
the same keys; a malformed value reads as its default, and the frozen MAC
does not cover them yet, so a tenant cut over reads them as defaults.

This module also holds the fake GitHub the other platform-train tests
(parity, boarding a `ready` pull request, coexistence) run over: the
coordinator's fake plus the reads and writes those lanes make.

No credentials, no network, no emulator. Every token is built at runtime.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

import pytest

from swarm_api import mergesettings, mergetrain
from swarm_api.errors import ValidationFailed
from swarm_api.judgment import SettingsMac, block_mac

from .test_merge_train_coordinator import (  # noqa: F401 - the fixtures are used by name
    API, CHECK, NOW, OWNER, REPO, TrainGitHub, _entry, _ride, _tick, clock, ctx,
)

REPO_ID = "repo_widgets"
JOB = 7001
RUN = 5001


class PlatformGitHub(TrainGitHub):
    """The coordinator's fake, plus: the base's required contexts, the
    repository variable `MERGE_TRAIN`, Actions re-runs, commit statuses
    posted, the open pull request list with labels, issue events,
    collaborator permissions, comments and label removal."""

    def __init__(self) -> None:
        super().__init__()
        self.required: list[str] = [CHECK]
        self.variable: str | None = None
        self.reruns: list[int] = []
        self.on_rerun: Any = None
        self.posted: list[tuple[str, str]] = []
        self.events: dict[int, list[dict[str, Any]]] = {}
        self.permissions: dict[str, str] = {}
        self.comments: list[tuple[int, str]] = []
        #: Contexts that never report on any commit: a check that skips drafts.
        self.silent: set[str] = set()
        #: The base's own required checks are red at its head.
        self.base_red = False

    def label(self, number: int, by: str, *, minute: int = 0, event: str = "labeled") -> None:
        pull = self.pulls[number]
        names = {label["name"] for label in pull.setdefault("labels", [])}
        if event == "labeled" and "ready" not in names:
            pull["labels"].append({"name": "ready"})
        if event == "unlabeled":
            pull["labels"] = [label for label in pull["labels"] if label["name"] != "ready"]
        stamp = (NOW + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z")
        self.events.setdefault(number, []).append(
            {"event": event, "label": {"name": "ready"}, "actor": {"login": by},
             "created_at": stamp})

    def _runs(self, sha: str) -> list[dict[str, Any]]:
        numbers = self.contents.get(sha, frozenset())
        verdict = self.verdict(numbers)
        if self.base_red and sha == self.branches.get("main"):
            verdict = "failure"
        runs = []
        for context in self.required:
            if context in self.silent or context == "merge-train":
                continue
            run = {"id": JOB, "name": context,
                   "details_url": f"https://github.com/{OWNER}/{REPO}/actions/runs/{RUN}/job/{JOB}",
                   "status": "completed" if verdict else "in_progress",
                   "conclusion": verdict}
            runs.append(run)
        return runs

    def _route(self, method: str, path: str, payload: Any) -> tuple[int, Any]:
        rest = path[len(API):] if path.startswith(API) else None
        if rest is None:
            return 404, {"message": "Not Found"}
        if method == "GET" and rest == "/rules/branches/main":
            return 200, [{"type": "required_status_checks", "parameters": {
                "required_status_checks": [{"context": c} for c in self.required]}}]
        if method == "GET" and rest.startswith("/commits/") and rest.endswith("/check-runs"):
            runs = self._runs(rest.split("/")[2])
            return 200, {"total_count": len(runs), "check_runs": runs}
        if method == "GET" and rest == "/actions/variables/MERGE_TRAIN":
            if self.variable is None:
                return 404, {"message": "Not Found"}
            return 200, {"name": "MERGE_TRAIN", "value": self.variable}
        if method == "POST" and rest == f"/actions/runs/{RUN}/rerun-failed-jobs":
            self.reruns.append(RUN)
            if self.on_rerun is not None:
                self.on_rerun()
            return 201, {}
        if method == "POST" and rest.startswith("/statuses/"):
            self.posted.append((rest.split("/")[2], payload["context"]))
            return 201, {"state": payload["state"], "context": payload["context"]}
        if method == "GET" and rest == "/pulls":
            return 200, [p for n, p in sorted(self.pulls.items())
                         if n < 900 and p["state"] == "open"]
        if method == "GET" and rest.startswith("/issues/") and rest.endswith("/events"):
            return 200, self.events.get(int(rest.split("/")[2]), [])
        if method == "GET" and rest.startswith("/collaborators/"):
            login = rest.split("/")[2]
            if login not in self.permissions:
                return 404, {"message": "Not Found"}
            return 200, {"permission": self.permissions[login]}
        if method == "POST" and rest.startswith("/issues/") and rest.endswith("/comments"):
            self.comments.append((int(rest.split("/")[2]), payload["body"]))
            return 201, {"id": len(self.comments), "html_url": "https://example.invalid/c"}
        if method == "DELETE" and rest.startswith("/issues/") and "/labels/" in rest:
            number = int(rest.split("/")[2])
            name = rest.split("/")[-1]
            self.pulls[number]["labels"] = [
                label for label in self.pulls[number].get("labels", []) if label["name"] != name]
            return 200, []
        return super()._route(method, path, payload)


@pytest.fixture
def github() -> PlatformGitHub:
    return PlatformGitHub()


def settings(db, tenant_id: str = "eng", **values: Any) -> None:
    """The workspace in train mode: these tests name their own gather values."""
    db.docs[f"{mergesettings.COLLECTION}/{tenant_id}"] = {mergesettings.FIELD: {
        "tenant_id": tenant_id, "mode": "train",
        "train_size": mergesettings.TRAIN_SIZE_DEFAULT,
        "max_train_age_minutes": mergesettings.MAX_TRAIN_AGE_DEFAULT, **values,
    }}


def register(db, *, tenant_id: str = "eng", **fields: Any) -> dict[str, Any]:
    """`OWNER/REPO` registered in `tenant_id`, its default branch main."""
    doc = {"repo_id": REPO_ID, "tenant_id": tenant_id, "forge": "github", "owner": OWNER,
           "repo": REPO, "repository_url": f"https://github.com/{OWNER}/{REPO}",
           "default_branch": "main", **fields}
    db.docs[f"repositories/{REPO_ID}"] = doc
    return doc


def at(clock, minutes: float) -> None:
    clock.now = NOW + timedelta(minutes=minutes)


def tick(ctx, clock):
    """One tick at the clock's time, which does not move."""
    return mergetrain.tick_tenant(ctx, "eng", limit=50)


# --------------------------------------------------------------------------
# gathering
# --------------------------------------------------------------------------

def test_one_rider_waits_until_the_gather_wait(ctx, db, github, clock):
    settings(db, gather_min=2, gather_wait_minutes=10)
    entry_id, _ = _ride(db, github, 11, minute=0)

    at(clock, 0)
    report = tick(ctx, clock)
    assert report.composed == 0 and report.waiting == 1 and report.active
    assert _entry(db, entry_id)["state"] == mergetrain.QUEUED
    assert not github.path_calls("POST", f"{API}/git/refs")

    at(clock, 9)
    assert tick(ctx, clock).composed == 0

    at(clock, 10)
    assert tick(ctx, clock).composed == 1
    assert _entry(db, entry_id)["state"] == mergetrain.RIDING


def test_gather_min_riders_board_at_once(ctx, db, github, clock):
    settings(db, gather_min=2, gather_wait_minutes=60)
    rides = [_ride(db, github, n, minute=0) for n in (21, 22)]
    at(clock, 0)
    report = tick(ctx, clock)
    assert report.composed == 1
    assert len(github.path_calls("POST", f"{API}/merges")) == 2
    assert all(_entry(db, e)["state"] == mergetrain.RIDING for e, _ in rides)


def test_a_bisected_half_rides_without_gathering(ctx, db, github, clock):
    settings(db, gather_min=2, gather_wait_minutes=60)
    github.verdict = lambda numbers: "failure" if 32 in numbers else "success"
    github.on_rerun = None
    rides = {n: _ride(db, github, n, minute=0) for n in (31, 32)}
    for minute in range(0, 20, 2):
        at(clock, minute)
        tick(ctx, clock)
    # Red as a pair, retried once, then each half rode alone at once.
    assert github.main_landed == [31]
    assert _entry(db, rides[32][0])["state"] == mergetrain.EJECTED


def test_malformed_gather_values_read_as_defaults():
    read = mergesettings.settings_of({
        "train_size": 4, "gather_min": "3", "gather_wait_minutes": 61,
        "required_start_minutes": 4, "base_red_wait_minutes": True,
    })
    assert read.gather_min == mergesettings.GATHER_MIN_DEFAULT == 2
    assert read.gather_wait_minutes == mergesettings.GATHER_WAIT_DEFAULT == 10
    assert read.required_start_minutes == mergesettings.REQUIRED_START_DEFAULT == 15
    assert read.base_red_wait_minutes == mergesettings.BASE_RED_WAIT_DEFAULT == 60
    # Above the train size is malformed too: never more than a train carries.
    assert mergesettings.settings_of({"train_size": 3, "gather_min": 5}).gather_min == 2
    assert mergesettings.settings_of({"train_size": 1, "gather_min": 5}).gather_min == 1
    assert mergesettings.settings_of({"train_size": 6, "gather_min": 5}).gather_min == 5
    # A registration's malformed override inherits.
    assert mergesettings.repo_train_of({"merge_train": {"gather_min": 0,
                                                        "gather_wait_minutes": 5}}) == {
        "gather_wait_minutes": 5}
    assert mergesettings.repo_train_of({"merge_train": "x"}) == {}


def test_a_malformed_stored_gather_min_holds_a_lone_rider_as_the_default_does(
        ctx, db, github, clock):
    settings(db, gather_min="one", gather_wait_minutes=10)
    _ride(db, github, 41, minute=0)
    at(clock, 0)
    assert tick(ctx, clock).composed == 0
    at(clock, 10)
    assert tick(ctx, clock).composed == 1


def test_a_repository_override_wins_over_the_workspace(ctx, db, github, clock):
    settings(db, gather_min=2, gather_wait_minutes=60)
    register(db, merge_train={"gather_min": 1})
    _ride(db, github, 51, minute=0)
    at(clock, 0)
    assert tick(ctx, clock).composed == 1


# --------------------------------------------------------------------------
# the setting: written, bounded, audited; not acted on unsigned when cut over
# --------------------------------------------------------------------------

def _store(db, clock, mac: SettingsMac | None = None) -> mergesettings.MergeSettingsStore:
    return mergesettings.MergeSettingsStore(db, now=clock, mac=mac or SettingsMac())


def test_the_gather_settings_are_written_and_audited(db, clock):
    store = _store(db, clock)
    after = store.put("eng", mergesettings.MergeSettingsPut(
        gather_min=3, gather_wait_minutes=0, required_start_minutes=20,
        base_red_wait_minutes=120), by="admin@example.com")
    assert after.train_dict() == {"gather_min": 3, "gather_wait_minutes": 0,
                                  "required_start_minutes": 20, "base_red_wait_minutes": 120}
    assert store.get("eng") == after
    audits = [d for p, d in db.docs.items() if p.startswith("admin_audit/")]
    assert audits and audits[-1]["detail"]["to"]["gather_min"] == 3


def test_gather_min_above_the_train_size_is_refused(db, clock):
    store = _store(db, clock)
    with pytest.raises(ValidationFailed):
        store.put("eng", mergesettings.MergeSettingsPut(train_size=2, gather_min=3), by="a")
    with pytest.raises(ValueError):
        mergesettings.MergeSettingsPut(gather_wait_minutes=61)
    with pytest.raises(ValueError):
        mergesettings.MergeSettingsPut(required_start_minutes=4)


def test_a_repository_train_override_is_written_and_cleared(db, clock):
    register(db)
    store = _store(db, clock)
    store.put("eng", mergesettings.MergeSettingsPut(
        repository_train={REPO_ID: {"gather_min": 1, "gather_wait_minutes": 0}}), by="a")
    assert db.docs[f"repositories/{REPO_ID}"]["merge_train"] == {
        "gather_min": 1, "gather_wait_minutes": 0}
    store.put("eng", mergesettings.MergeSettingsPut(
        repository_train={REPO_ID: {"gather_min": None}}), by="a")
    assert db.docs[f"repositories/{REPO_ID}"]["merge_train"] == {"gather_wait_minutes": 0}


def test_another_tenants_registration_is_never_given_an_override(db, clock):
    register(db, tenant_id="research")
    with pytest.raises(Exception) as raised:
        _store(db, clock).put("eng", mergesettings.MergeSettingsPut(
            repository_train={REPO_ID: {"gather_min": 1}}), by="a")
    assert getattr(raised.value, "code", "") == "not_found"
    assert "merge_train" not in db.docs[f"repositories/{REPO_ID}"]


def test_a_cut_over_tenant_reads_the_unsigned_train_settings_as_defaults(db, clock):
    key = secrets.token_hex(16)
    mac = SettingsMac(keys=(key,), tenants=("eng",))
    covered = {"mode": "train", "train_size": 4, "max_train_age_minutes": 90}
    block = {"tenant_id": "eng", **covered, "gather_min": 1, "gather_wait_minutes": 0,
             "mac": block_mac("eng", mergesettings.MAC_BLOCK, covered, (key,))}
    db.docs[f"{mergesettings.COLLECTION}/eng"] = {mergesettings.FIELD: block}
    read = _store(db, clock, mac).get("eng")
    assert read.mode == "train"
    assert read.gather_min == mergesettings.GATHER_MIN_DEFAULT
    assert read.gather_wait_minutes == mergesettings.GATHER_WAIT_DEFAULT
    # Not cut over: read as stored.
    assert _store(db, clock).get("eng").gather_min == 1
