"""The discovery app: `GET /.well-known/swarmcloud` and nothing else.

docs/design/day-one-plan.md §6 (lane DAYONE-DISCOVERY-1, part of #2688) and
docs/design/day-one-install.md §9.2, §9.3. The service is unauthenticated (a
non-IAP backend, invoker check disabled), so what it serves is what anyone on
the internet can read: these tests hold that it serves exactly the public set
and that the Desktop client secret is served only when the Q1 switch is on, and
then from Secret Manager, never from an environment variable.

No credentials and no emulator: Secret Manager is a fake reader passed to
`create_app`, the same seam the production entrypoint fills with the real one.
"""

from __future__ import annotations

import json
import re
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from swarm_redaction import redact

from swarm_api import discovery

PATH = "/.well-known/swarmcloud"

#: Design §9.2 / plan §6.2: the public keys, `plugin` holding `min_version`.
PUBLIC_KEYS = {
    "schema",
    "name",
    "version",
    "console_url",
    "api_url",
    "oauth_client_id",
    "iap_audience",
    "plugin",
}

_HOST = "swarm.example.test"


def _environ(**extra: str) -> dict[str, str]:
    env = {
        "PROJECT_ID": "example-project",
        "DISCOVERY_NAME": "Example deployment",
        "DISCOVERY_VERSION": "2026.10.1",
        "DISCOVERY_HOST": _HOST,
        "DISCOVERY_OAUTH_CLIENT_ID": "123456789012-example.apps.googleusercontent.com",
        "DISCOVERY_IAP_AUDIENCE": "/projects/123456789012/global/backendServices/4242",
        "DISCOVERY_PLUGIN_MIN_VERSION": "2026.10.0",
    }
    env.update(extra)
    return env


class FakeSecrets:
    """Records every secret name asked for; answers with one value."""

    def __init__(self, value: str) -> None:
        self.value = value
        self.asked: list[str] = []

    def __call__(self, name: str) -> str:
        self.asked.append(name)
        return self.value


def _planted() -> dict[str, str]:
    """Credential-shaped values put where a careless reader would find them.
    Built at runtime so nothing in this file looks like a credential."""
    return {
        "DISCOVERY_OAUTH_CLIENT_SECRET": "GOCSPX-" + secrets.token_hex(14),
        "OAUTH_CLIENT_SECRET": "GOCSPX-" + secrets.token_hex(14),
        "GOOGLE_OAUTH_ACCESS_TOKEN": "ya29." + secrets.token_hex(24),
        "SWARM_API_KEY": "AIza" + secrets.token_hex(16),
    }


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _get(app, path: str = PATH):
    with TestClient(app) as client:
        return client.get(path)


def test_document_keys_are_exactly_the_public_set() -> None:
    planted = _planted()
    reader = FakeSecrets("GOCSPX-" + secrets.token_hex(14))
    app = discovery.create_app(environ=_environ(**planted), read_secret=reader)

    response = _get(app)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    document = response.json()
    assert set(document) == PUBLIC_KEYS
    assert set(document["plugin"]) == {"min_version"}
    assert document == {
        "schema": 1,
        "name": "Example deployment",
        "version": "2026.10.1",
        "console_url": f"https://{_HOST}/",
        "api_url": f"https://{_HOST}/v1",
        "oauth_client_id": "123456789012-example.apps.googleusercontent.com",
        "iap_audience": "/projects/123456789012/global/backendServices/4242",
        "plugin": {"min_version": "2026.10.0"},
    }
    # Nothing credential-shaped: not the planted values, not any value the
    # platform's own redaction rules would mask, and Secret Manager not asked.
    for value in _strings(document):
        assert not redact(value).any, f"{value!r} matches a credential pattern"
    for value in planted.values():
        assert value not in response.text
    assert not re.search(r"GOCSPX-|ya29\.|AIza", response.text)
    assert reader.asked == []


def test_every_other_path_404s() -> None:
    app = discovery.create_app(environ=_environ(), read_secret=FakeSecrets("unused"))
    with TestClient(app) as client:
        for path in (
            "/",
            "/v1/tasks",
            "/v1/me",
            "/.well-known/other",
            "/.well-known/swarmcloud/extra",
            "/.well-known/",
            "/docs",
            "/redoc",
            "/openapi.json",
            "/healthz",
        ):
            response = client.get(path)
            assert response.status_code == 404, f"{path} answered {response.status_code}"
            assert "oauth_client_id" not in response.text
        # The one path answers GET only.
        assert client.post(PATH).status_code == 405


@pytest.mark.parametrize("switch", [None, "false", "False", ""])
def test_client_secret_absent_unless_switched(switch: str | None) -> None:
    planted = _planted()
    extra = dict(planted)
    if switch is not None:
        extra["DISCOVERY_SERVE_CLIENT_SECRET"] = switch
    reader = FakeSecrets("GOCSPX-" + secrets.token_hex(14))
    app = discovery.create_app(environ=_environ(**extra), read_secret=reader)

    document = _get(app).json()

    assert "oauth_client_secret" not in document
    assert reader.asked == []


def test_client_secret_only_when_switched() -> None:
    planted = _planted()
    served = "GOCSPX-" + secrets.token_hex(14)
    reader = FakeSecrets(served)
    app = discovery.create_app(
        environ=_environ(DISCOVERY_SERVE_CLIENT_SECRET="true", **planted), read_secret=reader
    )

    response = _get(app)

    document = response.json()
    assert set(document) == PUBLIC_KEYS | {"oauth_client_secret"}
    # From Secret Manager, the one secret the switch grants, and never an env var.
    assert document["oauth_client_secret"] == served
    assert reader.asked == [
        "projects/example-project/secrets/swarm-iap-desktop-client-secret/versions/latest"
    ]
    for value in planted.values():
        assert value not in response.text
    # Read once, at start: a second request asks Secret Manager nothing more.
    with TestClient(app) as client:
        client.get(PATH)
        client.get(PATH)
    assert len(reader.asked) == 1


def test_switch_on_with_an_unreadable_secret_refuses_to_start() -> None:
    def unreadable(name: str) -> str:
        raise discovery.DiscoveryConfigError(f"{name} could not be read")

    with pytest.raises(discovery.DiscoveryConfigError):
        discovery.create_app(
            environ=_environ(DISCOVERY_SERVE_CLIENT_SECRET="true"), read_secret=unreadable
        )
    with pytest.raises(discovery.DiscoveryConfigError):
        discovery.create_app(
            environ=_environ(DISCOVERY_SERVE_CLIENT_SECRET="true"), read_secret=FakeSecrets("  ")
        )


@pytest.mark.parametrize(
    "change",
    [
        {"DISCOVERY_OAUTH_CLIENT_ID": ""},
        {"DISCOVERY_HOST": "https://swarm.example.test"},
        {"DISCOVERY_HOST": "swarm.example.test/v1"},
        {"DISCOVERY_SERVE_CLIENT_SECRET": "yes"},
        {"PROJECT_ID": "", "DISCOVERY_SERVE_CLIENT_SECRET": "true"},
    ],
)
def test_a_bad_environment_refuses_to_start(change: dict[str, str]) -> None:
    """A document with a hole would configure every plugin wrongly; a revision
    that fails to start is seen at deploy instead."""
    with pytest.raises(discovery.DiscoveryConfigError):
        discovery.create_app(environ=_environ(**change), read_secret=FakeSecrets("x" * 20))


def test_a_missing_variable_is_named() -> None:
    env = _environ()
    del env["DISCOVERY_IAP_AUDIENCE"]
    with pytest.raises(discovery.DiscoveryConfigError, match="DISCOVERY_IAP_AUDIENCE"):
        discovery.create_app(environ=env, read_secret=FakeSecrets("unused"))


def test_no_firestore_at_import() -> None:
    """Importing the module, and building the app with the switch off, loads
    no Firestore and no Secret Manager client library at all."""
    root = Path(__file__).resolve().parents[3]
    script = (
        "import json, sys\n"
        "from swarm_api import discovery\n"
        "before = sorted(m for m in sys.modules if m.startswith(('google.cloud', 'swarm_api.')))\n"
        f"discovery.create_app(environ={json.dumps(_environ())})\n"
        "after = sorted(m for m in sys.modules if m.startswith(('google.cloud', 'swarm_api.')))\n"
        "print(json.dumps({'before': before, 'after': after}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=root, capture_output=True, text=True, check=True
    )
    loaded = json.loads(result.stdout.strip().splitlines()[-1])
    for phase in ("before", "after"):
        assert not [m for m in loaded[phase] if m.startswith("google.cloud")], loaded[phase]
        # Nothing of the API's own: not its store, settings or main app.
        assert loaded[phase] == ["swarm_api.discovery"], loaded[phase]
