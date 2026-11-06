"""Adoption: what an earlier install left, probed and taken over, never
recreated (DAYONE-ENGINE-3; design §10, D24; review finding 18)."""

from __future__ import annotations

import io

import pytest

from swarm_install import adopt
from swarm_install.events import Events
from swarm_install.steps.preflight import ApiError
from swarm_install.waits import WaitGaveUp

from .test_state_store import FakeClock

PROJECT = "p-dayone-test"
REGION = "europe-west1"
RING = f"projects/{PROJECT}/locations/{REGION}/keyRings/swarm-dev-specs"


class FakeGcp:
    def __init__(self) -> None:
        self.rings: dict[str, dict] = {}
        self.keys: dict[str, dict] = {}
        self.roles: list[dict] = []
        self.undelete_refused: set[str] = set()
        self.undeleted: list[str] = []
        self.databases: list[dict | None] = []

    def key_ring(self, project, location, name):
        return self.rings.get(f"projects/{project}/locations/{location}/keyRings/{name}")

    def crypto_key(self, ring_id, name):
        return self.keys.get(f"{ring_id}/cryptoKeys/{name}")

    def custom_roles(self, project):
        return list(self.roles)

    def undelete_role(self, name):
        if name.rsplit("/", 1)[-1] in self.undelete_refused:
            raise ApiError(400, "FAILED_PRECONDITION", "the role was deleted more than 7 days ago")
        self.undeleted.append(name)

    def firestore_database(self, project, database):
        return self.databases.pop(0) if len(self.databases) > 1 else self.databases[0]


def test_kms_ring_imported_never_recreated() -> None:
    gcp = FakeGcp()
    assert adopt.kms_imports(gcp, PROJECT, REGION, "dev", []) == [], "nothing to import in a new project"
    gcp.rings[RING] = {"name": RING}
    gcp.keys[f"{RING}/cryptoKeys/step-spec"] = {"name": "step-spec"}
    found = adopt.kms_imports(gcp, PROJECT, REGION, "dev", [])
    assert found == [
        ('google_kms_key_ring.step_spec["dev"]', RING),
        ('google_kms_crypto_key.step_spec["dev"]', f"{RING}/cryptoKeys/step-spec"),
    ]
    in_state = [address for address, _ in found]
    assert adopt.kms_imports(gcp, PROJECT, REGION, "dev", in_state) == [], "what is in state is not imported again"


def test_custom_roles_undeleted_within_7_days() -> None:
    gcp = FakeGcp()
    gcp.roles = [
        {"name": f"projects/{PROJECT}/roles/swarmDeployer", "deleted": True},
        {"name": f"projects/{PROJECT}/roles/swarmOld", "deleted": True},
        {"name": f"projects/{PROJECT}/roles/swarmLive"},
        {"name": f"projects/{PROJECT}/roles/otherTeamRole", "deleted": True},
    ]
    gcp.undelete_refused = {"swarmOld"}
    found = adopt.undelete_roles(gcp, PROJECT, ["swarmDeployer", "swarmOld", "swarmLive"])
    assert found.undeleted == ("swarmDeployer",)
    assert found.blocked == ("swarmOld",), "past 7 days IAM refuses the undelete: named, never recreated"
    assert gcp.undeleted == [f"projects/{PROJECT}/roles/swarmDeployer"]
    assert adopt.ROLE_UNDELETE_DAYS == 7


def test_desktop_client_adopted_from_the_record() -> None:
    """No API lists OAuth clients any more: the recorded id is adopted."""
    client = "123456789012-abc123.apps.googleusercontent.com"
    assert adopt.desktop_client({"desktop_client_id": client}) == client
    assert adopt.desktop_client({"desktop_client_id": "not-an-id"}) is None
    assert adopt.desktop_client({}) is None and adopt.desktop_client(None) is None


def test_github_app_reuse_or_new_name() -> None:
    taken = {"swarmcloud-dev", "swarmcloud-dev-2"}
    assert adopt.github_app_name("swarmcloud-dev", {"slug": "swarmcloud-dev"}, taken.__contains__) == (
        "swarmcloud-dev", True)
    assert adopt.github_app_name("swarmcloud-dev", None, taken.__contains__) == ("swarmcloud-dev-3", False)
    assert adopt.github_app_name("swarmcloud-dev", {"slug": "gone"}, taken.__contains__) == (
        "swarmcloud-dev-3", False), "a recorded App that no longer exists is not reused"


def test_firestore_id_waited_with_countdown() -> None:
    clock = FakeClock()
    events = Events(clock.now, json_lines=True, out=io.StringIO())
    gcp = FakeGcp()
    gcp.databases = [{"state": "DELETING"}, {"deleteTime": "2026-10-29T00:00:00Z"}, None]
    adopt.wait_firestore_id(gcp, PROJECT, "(default)", clock=clock, events=events)
    assert len(events.of_kind("wait_countdown")) == 2
    assert clock.sleeps and all(s == adopt.FIRESTORE_ID.interval_s for s in clock.sleeps)

    stuck = FakeGcp()
    stuck.databases = [{"state": "DELETING"}]
    with pytest.raises(WaitGaveUp):
        adopt.wait_firestore_id(stuck, PROJECT, "(default)", clock=clock, events=events)
    assert events.of_kind("wait_gave_up"), "past the bound it says where to look"


def test_remains_rederived_by_probe_without_cache() -> None:
    world = {"bucket": True, "ring": True}
    calls: list[str] = []

    def probe(name: str):
        return lambda: calls.append(name) or world[name]

    probes = {name: probe(name) for name in world}
    assert adopt.remains(probes) == ["bucket", "ring"]
    world["bucket"] = False
    assert adopt.remains(probes) == ["ring"], "every call probes again; nothing is cached"
    assert calls.count("bucket") == 2
