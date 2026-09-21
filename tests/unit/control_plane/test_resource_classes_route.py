"""`GET /v1/resource-classes` -- the REQUESTED side of "requested vs utilised".

Every attempt document has recorded `peak_rss_bytes` and `peak_disk_bytes`
since the first day, and nothing served the ceiling those numbers should be
read against. So "peak RSS 6.1 GiB" was a figure with no scale: on this
platform `requests == limits`, so 6.1 GiB is comfortable on `large` and one
chatty prompt from an OOM kill on `standard`, and no screen could tell which.

These tests exist mainly to pin the two properties that make the route worth
having rather than harmful:

  * it serves the FROZEN catalogue, byte for byte, so the web UI never has to
    hand-copy it -- `check-contract-parity.sh` does not cover TypeScript, so a
    copy there would drift the first time a class is resized and nothing would
    notice;
  * it is authenticated like every other /v1 route, so adding it does not open
    the first unauthenticated hole in the API surface.
"""

from __future__ import annotations

from swarm_common.profiles import RESOURCE_CLASSES

from .conftest import auth_header


def test_route_serves_the_frozen_catalogue_exactly(client):
    body = client.get("/v1/resource-classes", headers=auth_header("alice")).json()
    served = body["resource_classes"]

    assert set(served) == set(RESOURCE_CLASSES), "a class the catalogue has and the route drops is invisible to every caller"
    for name, rc in RESOURCE_CLASSES.items():
        assert served[name] == {
            "name": rc.name,
            "cpu": rc.cpu,
            "memory_gib": rc.memory_gib,
            "disk_gib": rc.disk_gib,
            "units": rc.units,
        }, f"{name} drifted from the frozen catalogue"


def test_requires_authentication_like_every_other_v1_route(client):
    assert client.get("/v1/resource-classes").status_code == 401


def test_available_to_a_non_admin(client):
    # It is the catalogue a caller already picks from by name when submitting,
    # so gating it behind admin would hide the ceiling from the only person who
    # can act on it -- the one whose agent is running near it.
    assert client.get("/v1/resource-classes", headers=auth_header("carol")).status_code == 200
