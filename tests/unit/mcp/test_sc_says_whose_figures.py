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
