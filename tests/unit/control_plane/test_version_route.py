"""U25: `GET /v1/version` says which build is serving; no route serves headroom.

`scripts/build-images.sh` passes `GIT_SHA` and `BUILD_TIME` as build args, and
the swarm-api image now sets both as ENV; Cloud Run sets `K_REVISION` and
`K_SERVICE`. Each identity field is null when its variable is unset -- never
"unknown", never a guess.

`limits` is the CONFIGURED limits, the same object `/v1/stats` serves, built by
one helper (`served_limits.configured_limits`). No remaining or headroom figure
exists on either payload (owner decision, 2026-10-01): the limiter is a token
bucket per principal PER INSTANCE, so such a figure is wrong for the next
request.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from .conftest import auth_header, seed_tenant

REPO = Path(__file__).resolve().parents[3]
IDENTITY = {
    "GIT_SHA": "git_sha",
    "BUILD_TIME": "build_time",
    "K_REVISION": "revision",
    "K_SERVICE": "service",
}


def _get(client, path: str, user: str = "alice") -> dict[str, Any]:
    response = client.get(path, headers=auth_header(user))
    assert response.status_code == 200, f"{path} -> {response.status_code}: {response.text}"
    return response.json()


def _keys(value: Any) -> list[str]:
    if isinstance(value, dict):
        out = list(value)
        for nested in value.values():
            out.extend(_keys(nested))
        return out
    if isinstance(value, list):
        return [k for item in value for k in _keys(item)]
    return []


def test_a_set_environment_is_served(client, db, monkeypatch):
    seed_tenant(db, "eng")
    values = {
        "GIT_SHA": "0c2354f",
        "BUILD_TIME": "2026-10-02T09:00:00Z",
        "K_REVISION": "swarm-api-00042-abc",
        "K_SERVICE": "swarm-api",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    body = _get(client, "/v1/version")
    for env, field in IDENTITY.items():
        assert body[field] == values[env], field


@pytest.mark.parametrize("unset", ["absent", "empty"])
def test_an_unset_environment_is_null_never_a_placeholder(client, db, monkeypatch, unset):
    seed_tenant(db, "eng")
    for name in IDENTITY:
        if unset == "absent":
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, "")
    body = _get(client, "/v1/version")
    for field in IDENTITY.values():
        assert body[field] is None, f"{field} served {body[field]!r} with its variable {unset}"


def test_the_route_needs_a_caller(client):
    assert client.get("/v1/version").status_code == 401


def test_limits_on_version_equal_limits_on_stats(client, db, api_context):
    seed_tenant(db, "eng")
    version = _get(client, "/v1/version")["limits"]
    stats = _get(client, "/v1/stats")["limits"]
    assert version == stats
    assert version["rate_limit_burst_per_instance"] == api_context.settings.rate_limit_burst
    assert version["requests_per_second_per_instance"] == (
        api_context.settings.core.requests_per_second
    )


def test_no_payload_carries_a_remaining_or_headroom_figure(client, db):
    seed_tenant(db, "eng")
    pattern = re.compile(r"remaining|headroom", re.IGNORECASE)
    for path, user in (("/v1/version", "alice"), ("/v1/stats", "alice"), ("/v1/stats", "root")):
        offending = [k for k in _keys(_get(client, path, user)) if pattern.search(k)]
        assert not offending, f"{path} as {user} serves {offending}"


def test_the_image_declares_the_build_args_and_sets_them_as_env():
    text = (REPO / "images/swarm-api/Dockerfile").read_text()
    for name in ("GIT_SHA", "BUILD_TIME"):
        assert re.search(rf"^ARG {name}\b", text, re.M), f"ARG {name} is not declared"
        assert re.search(rf"\b{name}=\${{{name}}}", text), f"{name} is not set as ENV"
