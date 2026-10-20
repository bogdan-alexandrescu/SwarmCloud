"""swarm-github-hooks, the process and its three outside calls (lane GS-HOOKS).

  * `create_receiver()` is a factory: importing the package builds no client and
    asks no credential, and `create_receiver` itself builds none until the first
    delivery needs one (CLAUDE.md: never a client at import time);
  * its settings are refused at start, by name, when they could send the
    secret read or the forwarded call anywhere but where terraform says;
  * the dedup is Firestore's `createDocument` with `documentId` -- a 409 is a
    redelivery, anything else unexpected is a failure, never a pass;
  * the forward is one `POST <SWARM_API_URL>/v1/admin/repositories/push` with
    `{"forge": "github", "full_name": ...}` and an ID token minted for
    swarm-api's audience, as the service's own (rollup sweeper) identity;
  * the secret is the latest version of swarm-github-app-webhook-secret.

Nothing here reaches the network: every session and token source is a fake.
"""

from __future__ import annotations

import base64
import json
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from swarm_github_hooks import main
from swarm_github_hooks.clients import ApiForwarder, FirestoreDeliveries, read_secret
from swarm_github_hooks.receiver import Receiver, StoreUnavailable

from hooks_fixtures import push

REPO = Path(__file__).resolve().parents[3]
APP = REPO / "apps" / "swarm-github-hooks"
VERSION = "projects/swarm-test-project/secrets/swarm-github-app-webhook-secret/versions/latest"
API = "https://swarm-api-abc-uc.a.run.app"

ENV = {
    main.SECRET_ENV: VERSION,
    main.API_URL_ENV: API,
    main.DATABASE_ENV: "swarm",
}


class Response:
    def __init__(self, status: int, body: dict | None = None) -> None:
        self.status_code = status
        self._body = body or {}

    def json(self) -> dict:
        return self._body


class Session:
    def __init__(self, *responses: Response) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def _answer(self, method: str, url: str, **kwargs) -> Response:
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)

    def get(self, url: str, **kwargs) -> Response:
        return self._answer("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> Response:
        return self._answer("POST", url, **kwargs)


# --- import and the factory --------------------------------------------------

def test_importing_the_package_builds_nothing_and_needs_no_credential():
    """A fresh interpreter, with google.auth.default made to fail: importing
    every module and calling create_receiver() must not reach it."""
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import google.auth\n"
        "def boom(*a, **k): raise SystemExit('google.auth.default was called')\n"
        "google.auth.default = boom\n"
        "import swarm_github_hooks.main as m, swarm_github_hooks.server, swarm_github_hooks.clients\n"
        "app = m.create_receiver(%r)\n"
        "print(type(app).__name__)\n"
    ) % (str(APP), ENV)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "Receiver"


def test_create_receiver_wires_the_injected_parts():
    store_calls: list[str] = []

    class Store:
        def create(self, delivery_id, expires_at):
            store_calls.append(delivery_id)
            return True

    forwarded: list[str] = []
    key = secrets.token_hex(16).encode()
    app = main.create_receiver(ENV, access=lambda version: key, deliveries=Store(),
                          forward=lambda name: forwarded.append(name) or 200)
    assert isinstance(app, Receiver)
    delivery = push(key)
    reply = app.handle("POST", "/github/events", delivery.headers(), lambda n: delivery.body[:n])
    assert reply.outcome == "forwarded"
    assert forwarded == ["Example-Org/Widgets"] and store_calls == [delivery.delivery]


@pytest.mark.parametrize("name,value,word", [
    (main.SECRET_ENV, "", main.SECRET_ENV),
    (main.SECRET_ENV, "projects/swarm-test-project/secrets/swarm-graph-ticket-key/versions/latest", main.SECRET_ENV),
    (main.SECRET_ENV, "projects/x/secrets/swarm-github-app-webhook-secret/versions/latest", main.SECRET_ENV),
    (main.API_URL_ENV, "", main.API_URL_ENV),
    (main.API_URL_ENV, "http://swarm-api-abc-uc.a.run.app", main.API_URL_ENV),
    (main.API_URL_ENV, "https://swarm-api.example/v1?x=1", main.API_URL_ENV),
    (main.DATABASE_ENV, "../x", main.DATABASE_ENV),
])
def test_a_setting_that_could_point_elsewhere_is_refused_by_name(name, value, word):
    env = {**ENV, name: value}
    with pytest.raises(RuntimeError, match=word):
        main.settings(env)


def test_the_settings_terraform_passes_are_accepted():
    s = main.settings(ENV)
    assert s.secret_version == VERSION
    assert s.project == "swarm-test-project"
    assert s.api_url == API
    # No custom audience: the token names swarm-api's URL, as the sweeper's
    # Cloud Scheduler jobs' tokens do (modules/scheduler jobs.tf).
    assert s.audience == API
    assert main.settings({**ENV, main.AUDIENCE_ENV: "https://swarm-api.dev.swarm.internal"}).audience \
        == "https://swarm-api.dev.swarm.internal"
    assert s.port == 8080
    assert main.settings({**ENV, "PORT": "9090"}).port == 9090


# --- Secret Manager ------------------------------------------------------------

def test_the_secret_is_read_from_its_latest_version():
    value = secrets.token_hex(20).encode()
    session = Session(Response(200, {"payload": {"data": base64.b64encode(value).decode()}}))
    assert read_secret(VERSION, session) == value
    ((method, url, _),) = session.calls
    assert method == "GET"
    assert url == "https://secretmanager.googleapis.com/v1/" + VERSION + ":access"


@pytest.mark.parametrize("response", [Response(404), Response(200, {}), Response(200, {"payload": {"data": ""}})])
def test_an_unreadable_or_empty_secret_raises_without_quoting_the_answer(response):
    with pytest.raises(RuntimeError) as err:
        read_secret(VERSION, Session(response))
    assert "payload" not in str(err.value) or "no payload" in str(err.value)


# --- Firestore: create() is the dedup ---------------------------------------------

def test_a_new_delivery_is_created_with_its_expiry():
    session = Session(Response(200, {"name": "x"}))
    store = FirestoreDeliveries("swarm-test-project", "swarm", lambda: session)
    expires = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
    assert store.create("72d3162e-cc78-11e3-81ab-4c9367dc0958", expires) is True
    ((method, url, kwargs),) = session.calls
    assert method == "POST"
    assert url == ("https://firestore.googleapis.com/v1/projects/swarm-test-project/databases/swarm"
                   "/documents/github_deliveries")
    assert kwargs["params"] == {"documentId": "72d3162e-cc78-11e3-81ab-4c9367dc0958"}
    assert kwargs["json"] == {"fields": {"expires_at": {"timestampValue": "2026-10-23T12:00:00Z"}}}


def test_an_existing_delivery_is_a_redelivery():
    session = Session(Response(409, {"error": {"status": "ALREADY_EXISTS"}}))
    store = FirestoreDeliveries("swarm-test-project", "swarm", lambda: session)
    assert store.create("72d3162e-cc78-11e3-81ab-4c9367dc0958", datetime.now(timezone.utc)) is False


@pytest.mark.parametrize("status", [400, 403, 404, 429, 500, 503])
def test_any_other_answer_is_a_failure_never_a_pass(status):
    store = FirestoreDeliveries("swarm-test-project", "swarm", lambda: Session(Response(status)))
    with pytest.raises(StoreUnavailable):
        store.create("72d3162e-cc78-11e3-81ab-4c9367dc0958", datetime.now(timezone.utc))


def test_the_session_is_built_once_and_only_when_first_needed():
    built: list[int] = []
    session = Session(Response(200), Response(200))

    def factory():
        built.append(1)
        return session

    store = FirestoreDeliveries("swarm-test-project", "swarm", factory)
    assert built == []
    store.create("a-1", datetime.now(timezone.utc))
    store.create("a-2", datetime.now(timezone.utc))
    assert built == [1]


# --- the forward to swarm-api --------------------------------------------------

def test_the_forward_is_one_post_of_the_name_with_an_id_token_for_swarm_api():
    token = "tok-" + secrets.token_hex(8)
    audiences: list[str] = []
    posts: list[tuple[str, dict]] = []

    def mint(audience: str) -> str:
        audiences.append(audience)
        return token

    def post(url: str, **kwargs):
        posts.append((url, kwargs))
        return Response(200, {"report": {}})

    forward = ApiForwarder(API, API, mint=mint, post=post)
    assert forward("Example-Org/Widgets") == 200
    assert audiences == [API]
    ((url, kwargs),) = posts
    assert url == API + "/v1/admin/repositories/push"
    assert kwargs["json"] == {"forge": "github", "full_name": "Example-Org/Widgets"}
    assert kwargs["headers"] == {"Authorization": "Bearer " + token}
    assert kwargs["timeout"] > 0
    assert kwargs.get("allow_redirects") is False


def test_the_forward_answers_swarm_apis_status():
    forward = ApiForwarder(API, API, mint=lambda a: "t", post=lambda url, **k: Response(403))
    assert forward("a/b") == 403


def test_the_forward_route_is_the_one_gs_pushapi_serves():
    text = (REPO / "apps" / "swarm-api" / "swarm_api" / "routes" / "admin.py").read_text()
    assert '@router.post("/repositories/push")' in text
    from swarm_github_hooks.clients import PUSH_ROUTE

    assert PUSH_ROUTE == "/v1/admin/repositories/push"


def test_the_forwarded_body_is_what_repository_push_accepts():
    """RepositoryPush is extra="forbid": the body the receiver sends must be
    exactly its fields, or every push is a 422."""
    sys.path.insert(0, str(REPO / "apps" / "swarm-api"))
    from swarm_api.repositories import RepositoryPush

    posts: list[dict] = []
    ApiForwarder(API, API, mint=lambda a: "t",
                 post=lambda url, **k: posts.append(k["json"]) or Response(200))("Example-Org/Widgets")
    model = RepositoryPush.model_validate(posts[0])
    assert model.full_name == "Example-Org/Widgets" and model.forge == "github"
    assert set(posts[0]) == set(RepositoryPush.model_fields)
    assert json.dumps(posts[0])
