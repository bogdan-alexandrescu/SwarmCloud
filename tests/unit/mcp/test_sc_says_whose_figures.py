"""What `sc` prints about whose figures they are, how many, and what can run.

The `sc` half of the 2026-09-25 plugin QA (#88). Each test names its box
(SC-Fn) and was committed before its fix, so the first CI run on the branch
shows it red for the reason it names.

The listing tests run against the REAL swarm-api -- the defect there was a
disagreement with the route (which tasks it returns, and in what shape the
identity route nests the tenant), and a fake would agree with whoever wrote it.
The rest are `render.py`, which is pure and is tested with data.

`tests/unit` is on sys.path via this directory's conftest, which says why.
"""

from __future__ import annotations

import io
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker
from swarm_common.profiles import RUNNER_PROFILES

from swarm_mcp import render, sc
from swarm_mcp import client as mcp_client
from swarm_mcp.client import SwarmClient
from swarm_mcp.render import Style

from control_plane.conftest import api_settings, seed_task, seed_tenant
from control_plane.fakes import FakeFirestore

AUTH = {"Authorization": "Bearer token-alice"}
TENANT = "eng"
WIDE = Style(width=100, color=False, unicode=True)
NOW = datetime(2026, 9, 25, 4, 0, tzinfo=timezone.utc)
LONG_AGO = datetime(2026, 9, 1, tzinfo=timezone.utc)


class _Response(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200) -> None:
        super().__init__(body)
        self.status = status
        self.code = status

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@pytest.fixture()
def db() -> FakeFirestore:
    return FakeFirestore()


@pytest.fixture()
def swarm(db, monkeypatch) -> SwarmClient:
    """A real SwarmClient whose socket is the real application."""
    ctx = build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier(
            {"token-alice": {"email": "alice@saga.xyz", "email_verified": True,
                             "sub": "sub-alice", "hd": "saga.xyz"}}
        ),
        groups=StaticGroups({"alice@saga.xyz": (f"{TENANT}@saga.xyz",)}),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
    )
    seed_tenant(db, TENANT)
    api = TestClient(create_app(ctx), raise_server_exceptions=False)

    def _opener(req, timeout=None):  # noqa: ARG001
        path = req.full_url[len("http://api.invalid"):]
        headers = {**dict(req.headers), **AUTH}
        response = api.request(req.get_method(), path, content=req.data, headers=headers)
        if response.status_code >= 400:
            raise urllib.error.HTTPError(
                req.full_url, response.status_code, "error", response.headers,
                io.BytesIO(response.content),
            )
        return _Response(response.content, response.status_code)

    monkeypatch.setenv("SWARM_ID_TOKEN", "test.id.token")
    monkeypatch.setattr(mcp_client, "_open", _opener, raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", _opener)
    return SwarmClient(base_url="http://api.invalid")


def _seed(db, task_id: str, state: str, minutes: int) -> None:
    seed_task(db, task_id=task_id, tenant_id=TENANT, state=state,
              created_at=LONG_AGO + timedelta(minutes=minutes))


def text(lines) -> str:
    return "\n".join(lines)


# ==========================================================================
# SC-F4: the header shows the tenant the identity route answered
# ==========================================================================


def test_the_header_shows_the_tenant_the_identity_route_answered(swarm):
    """The header read `tenant_id` off the top level; the route nests it, so a
    tenant that WAS read showed as the not-measured mark."""
    snap = sc.collect(swarm, ("tenant",))
    assert snap.tenant_error is None, snap.tenant_error

    line = render.render_header(snap, WIDE)[0]

    assert line.split(" · ")[1] == TENANT, line


# ==========================================================================
# SC-F5: tenant-scoped sections say whose; shared pools say they are shared
# ==========================================================================


def test_the_agents_section_names_the_tenant_its_tasks_belong_to(swarm, db):
    _seed(db, "task_0000000000000000run1", "RUNNING", 1)
    snap = sc.collect(swarm, sc.NEEDS["agents"])
    assert snap.tasks_error is None, snap.tasks_error

    assert f"tenant {TENANT}" in render.agents_subtitle(snap.tasks, WIDE)


def test_the_accounts_section_names_the_tenant_its_pool_belongs_to():
    class _Accounts:
        tier = "explicit"
        base_url = "https://swarm.example.test"

        def request(self, method, path, **kwargs):  # noqa: ARG002
            return {"accounts": [], "tenant_id": "acme"}

    accounts = sc.fetch_accounts(_Accounts())

    assert "tenant acme" in render.accounts_subtitle(accounts, WIDE)


def _pool(name: str, *, limit: int, active: int) -> dict:
    return {"name": name, "enabled": True, "effective_limit": limit,
            "active": active, "available": max(0, limit - active)}


CAPACITY = {
    "tenant_id": "acme",
    "pools": [_pool("global", limit=40, active=8), _pool("tenant:acme", limit=20, active=0)],
    "runner_profiles": {
        "mock": {"resource_class": "standard", "backend": "CLOUD_RUN_JOB", "provider": None,
                 "units": 1, "pools": ["global", "tenant:acme"]},
    },
}


@pytest.mark.parametrize("profiles_only", [True, False])
def test_the_capacity_section_says_a_shared_pool_counts_every_tenant(profiles_only):
    """`claude-code 8/40` was another tenant's work, beside AGENTS "0 running"."""
    out = text(render.render_capacity(CAPACITY, WIDE, profiles_only=profiles_only))
    assert "every tenant" in out, out
    assert "tenant:acme" in out


def test_a_trouble_finding_about_tasks_names_the_tenant():
    snap = render.Snapshot(
        tenant={"tenant": {"tenant_id": "acme"}},
        stats={"dispatch_paused": False},
        capacity={"pools": [], "runner_profiles": {}},
        accounts=[],
        tasks=[{"id": "t1", "state": "PARKED", "park_reason": "CREDENTIAL_MISSING"}],
        now=NOW,
    )
    parked = [f.what for f in render.find_trouble(snap, WIDE) if f.where == "parked"]
    assert parked and "tenant acme" in parked[0], parked


# ==========================================================================
# SC-F11: every live task, not the newest hundred
# ==========================================================================


def test_sc_reads_a_live_task_older_than_the_newest_hundred(swarm, db):
    """The tenant had 324 tasks and `sc agents` read the newest 100."""
    _seed(db, "task_00000000000000stuck1", "RUNNING", 0)
    for n in range(110):
        _seed(db, f"task_done{n:016d}", "SUCCEEDED", 60 + n)

    tasks = sc.fetch_tasks(swarm)

    assert any(t.get("state") == "RUNNING" for t in tasks), [t.get("state") for t in tasks][:5]


def test_sc_follows_the_page_token_for_a_long_queue(swarm, db):
    for n in range(130):
        _seed(db, f"task_queue{n:015d}", "QUEUED", n)

    tasks = sc.fetch_tasks(swarm)

    assert sum(1 for t in tasks if t.get("state") == "QUEUED") == 130


def test_a_listing_that_stopped_short_says_so():
    """A floor is printed as a floor, never as a total."""
    tasks = render.Listing(
        [{"id": "t1", "state": "QUEUED"}],
        tenant_id="acme",
        incomplete=["more than 2000 QUEUED tasks exist; sc read the newest 2000 and stopped"],
    )
    assert "incomplete" in render.agents_subtitle(tasks, WIDE)
    assert "more than 2000 QUEUED tasks exist" in text(render.render_agents(tasks, WIDE, NOW))


# ==========================================================================
# SC-F13: a catalogue-disabled profile has no ROOM
# ==========================================================================


def _disabled_profile() -> str:
    disabled = sorted(name for name, profile in RUNNER_PROFILES.items() if not profile.available)
    if not disabled:
        pytest.skip("the frozen catalogue disables no profile today")
    return disabled[0]


def _capacity_with(name: str, spec: dict) -> dict:
    return {
        "tenant_id": "acme",
        "pools": [_pool("global", limit=10, active=1)],
        "runner_profiles": {name: spec},
    }


def test_capacity_marks_a_catalogue_disabled_profile_disabled_with_no_room():
    """`sc capacity` showed ROOM for codex, which `swarm profiles` and every
    dispatch refuse."""
    name = _disabled_profile()
    capacity = _capacity_with(name, {"resource_class": "standard", "backend": "CLOUD_RUN_JOB",
                                     "units": 1, "pools": ["global"]})

    lines = render.render_capacity(capacity, WIDE, profiles_only=True)

    row = next(line for line in lines if line.strip().startswith(name))
    assert "disabled" in row, row
    assert not re.search(r"\b9\b", row), row


def test_capacity_honours_an_api_that_serves_availability():
    capacity = _capacity_with("future", {"available": False, "units": 1, "pools": ["global"]})

    row = next(
        line for line in render.render_capacity(capacity, WIDE, profiles_only=True)
        if line.strip().startswith("future")
    )
    assert "disabled" in row, row


# ==========================================================================
# SC-F14: the headline counts what is running as running
# ==========================================================================


def test_the_headline_does_not_call_dispatched_tasks_running():
    tasks = [{"id": "a", "state": "DISPATCHED"}, {"id": "b", "state": "DISPATCHED"}]
    assert "2 active (2 dispatched, 0 running)" in render.agents_subtitle(tasks, WIDE)


# ==========================================================================
# SC-F15 / SC-F19: one truncation, a step's name, one casing, "never started"
# ==========================================================================


def test_a_task_id_is_shortened_one_way():
    assert render.short_id("task_0123456789ab4674b39f") == "4674b39f"


def test_the_agents_table_names_a_workflow_step_and_spells_the_state_as_the_api_does():
    tasks = [{"id": "task_0123456789ab4674b39f", "step_id": "b", "state": "DISPATCHED",
              "runner_profile": "mock", "created_at": NOW.isoformat()}]
    out = text(render.render_agents(tasks, WIDE, NOW))
    assert "b 4674b39f" in out, out
    assert "DISPATCHED" in out, out


def test_sc_task_says_never_started_rather_than_a_dash():
    task = {"id": "task_0123456789ab4674b39f", "state": "CANCELLED", "runner_profile": "mock",
            "created_at": NOW.isoformat(), "started_at": None, "attempt_count": 0,
            "max_attempts": 3}
    out = text(render.render_task(task, WIDE, NOW))
    assert "never started" in out, out
    assert "started —" not in out, out


# ==========================================================================
# #190: a count taken over a window names the window
# ==========================================================================
#
# `sc trouble` printed `failed  5 task(s) of tenant eng in the window listed`
# and listed no window anywhere. The count is right; it is taken over the
# newest `sc.TASK_PAGE` tasks by creation time, which only a docstring said.
# Firestore on 2026-09-25 20:28Z: the newest 100 `eng` tasks were 58 SUCCEEDED,
# 37 CANCELLED and 5 FAILED, created between 03:28Z and 18:58Z.


def _findings(swarm, where: str) -> list[str]:
    snap = render.Snapshot(tasks=sc.fetch_tasks(swarm), now=NOW)
    return [f.what for f in render.find_trouble(snap, WIDE) if f.where == where]


def test_the_failed_count_names_the_newest_n_and_when_they_were_created(swarm, db):
    """110 tasks: the window is the newest 100, created from minute 10 on."""
    for n in range(110):
        state = "FAILED" if n in (100, 101, 102, 103, 104) else "SUCCEEDED"
        _seed(db, f"task_window{n:015d}", state, n)

    failed = _findings(swarm, "failed")

    assert failed == [
        f"5 of the newest 100 tasks of tenant {TENANT} failed "
        "(created since 2026-09-01 00:10Z)"
    ], failed


def test_the_dead_lettered_count_names_the_same_window(swarm, db):
    for n in range(110):
        state = "DEAD_LETTERED" if n == 108 else "SUCCEEDED"
        _seed(db, f"task_letter{n:015d}", state, n)

    dead = _findings(swarm, "dead-lettered")

    assert dead == [
        f"1 of the newest 100 tasks of tenant {TENANT} gave up after every attempt "
        "(created since 2026-09-01 00:10Z)"
    ], dead


def test_a_window_that_holds_every_task_says_all(swarm, db):
    """Seven tasks fit in one page: the count is over all of them, and saying
    "the newest 7" would imply there are older ones it left out."""
    for n in range(7):
        _seed(db, f"task_small{n:016d}", "FAILED" if n < 2 else "SUCCEEDED", n)

    failed = _findings(swarm, "failed")

    assert failed == [
        f"2 of all 7 tasks of tenant {TENANT} failed (created since 2026-09-01 00:00Z)"
    ], failed


def test_the_window_rides_on_the_listing_for_the_json_dump(swarm, db):
    """`sc --json` is what a script reads; the window is part of the answer."""
    for n in range(3):
        _seed(db, f"task_json{n:017d}", "SUCCEEDED", n)

    window = render.listing_window(sc.fetch_tasks(swarm))

    assert window is not None
    assert (window.count, window.whole) == (3, True)


# ==========================================================================
# #192: USED is units, ROOM is agents, and the screen says which
# ==========================================================================
#
# `sc capacity` showed a pool's `active/limit` -- UNITS, since admission adds a
# task's `units` on every pool it takes (`swarm_common/admission.py`) -- beside
# ROOM, which is `available // units`, a count of AGENTS. Nothing said so, and
# the note said "USED on a shared pool counts every tenant's agents". So two
# browser agents on `resource:browser` read `4/10`, and the note said that
# meant four agents.

_BROWSER_CAPACITY = {
    "tenant_id": "eng",
    "pools": [
        _pool("resource:browser", limit=10, active=4),
        _pool("runner:generic", limit=10, active=0),
    ],
    "runner_profiles": {
        "browser": {"resource_class": "browser", "backend": "GKE_AUTOPILOT", "provider": None,
                    "units": 2, "pools": ["resource:browser"]},
        "generic": {"resource_class": "standard", "backend": "CLOUD_RUN_JOB", "provider": None,
                    "units": 1, "pools": ["runner:generic"]},
    },
}


@pytest.mark.parametrize("profiles_only", [True, False])
def test_the_capacity_table_labels_units_and_says_room_counts_agents(profiles_only):
    lines = render.render_capacity(_BROWSER_CAPACITY, WIDE, profiles_only=profiles_only)
    out = text(lines)

    header = next(line for line in lines if "PROFILE" in line)
    assert "UNITS" in header, header
    assert " USED" not in header, header
    browser = next(line for line in lines if line.strip().startswith("browser"))
    assert re.search(r"\b4/10\b", browser) and browser.rstrip().endswith("3"), browser
    assert "every tenant's agents" not in out, "the note still calls units agents"
    flat = " ".join(out.split())
    assert "ROOM" in flat and "agents" in flat, flat


def test_the_note_gives_each_classes_units_from_the_catalogue():
    """Read from `swarm_common.profiles.RESOURCE_CLASSES`, so the note cannot
    go on saying 2 the day the catalogue says 3."""
    from swarm_common.profiles import RESOURCE_CLASSES

    flat = " ".join(text(render.render_capacity(_BROWSER_CAPACITY, WIDE)).split())
    for name, resource in RESOURCE_CLASSES.items():
        assert f"{name} {resource.units}" in flat, (name, flat)


def test_the_tightest_line_says_what_it_ranks_by():
    """`tightest: resource:browser 0/10` named an empty pool the tightest,
    because it ranks by agents of a profile that still fit and printed units."""
    subtitle = render.capacity_subtitle(_BROWSER_CAPACITY, WIDE)

    assert subtitle == (
        "tightest: browser, 3 more agents fit on resource:browser (4/10 units)"
    ), subtitle


def test_a_full_pool_finding_counts_units_not_agents():
    capacity = dict(_BROWSER_CAPACITY, pools=[_pool("resource:browser", limit=10, active=10)])
    snap = render.Snapshot(capacity=capacity, now=NOW)

    full = [f.what for f in render.find_trouble(snap, WIDE) if f.where == "resource:browser"]

    assert full == ["full at 10/10 units, counting every tenant's"], full
