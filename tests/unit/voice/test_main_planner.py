"""swarm-voice's process wires the planner into its sessions
(docs/design/voice-it.md §3.3, §10 phase 2; swarm_voice/main.py).

`build_sessions` is what the app's start builds: with `SWARM_API_URL` set (the
manifest sets swarm-api's in-cluster address) it hands `Sessions` the planner,
built by `planner.build_planner` over the same HTTP client, so a delegation
reaches swarm-api through the session grant; without it no session offers
delegation. Nothing here mints a token or reads a secret: the planner's ID
token and Secret Manager reads are lazy, and none happens while building.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

_APP = str(Path(__file__).resolve().parents[3] / "apps" / "swarm-voice")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from swarm_voice import main as voice_main  # noqa: E402
from swarm_voice.planner import Planner  # noqa: E402

API_URL = "http://swarm-api.swarm-system.svc.cluster.local:8080"


@pytest.fixture(autouse=True)
def broker(monkeypatch):
    # Every session leases from the quota broker's Voice it pool; nothing is called while building.
    monkeypatch.setenv("QUOTA_BROKER_URL", "https://swarm-quota-broker.test")


@pytest.fixture
def http():
    client = httpx.AsyncClient()
    yield client
    asyncio.run(client.aclose())


def test_the_session_builder_hands_the_planner_to_sessions(monkeypatch, http):
    monkeypatch.setenv("PROJECT_ID", "swarm-test")
    monkeypatch.setenv("SWARM_API_URL", API_URL)

    sessions = voice_main.build_sessions(http)

    planner = sessions._planner  # noqa: SLF001
    assert isinstance(planner, Planner)
    assert planner._api_base == API_URL and planner._http is http  # noqa: SLF001


def test_the_planner_reads_the_anthropic_key_from_the_deployments_project(monkeypatch, http):
    monkeypatch.setenv("PROJECT_ID", "swarm-test")
    monkeypatch.setenv("SWARM_API_URL", API_URL)

    planner = voice_main.build_sessions(http)._planner  # noqa: SLF001

    assert planner._keys._project == "swarm-test"  # noqa: SLF001


def test_without_swarm_apis_address_no_session_offers_delegation(monkeypatch, http):
    monkeypatch.setenv("PROJECT_ID", "swarm-test")
    monkeypatch.delenv("SWARM_API_URL", raising=False)

    assert voice_main.build_sessions(http)._planner is None  # noqa: SLF001
