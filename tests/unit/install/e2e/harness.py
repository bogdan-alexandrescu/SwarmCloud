"""Wiring the fake world under the real `swarm install`.

Nothing here replaces a step, the engine, the manifest or a renderer: the
seams are the ones a real install crosses to reach the outside (the HTTP
session Google's REST APIs ride on, the GCS and Firestore clients, the
commands it runs, the clock, the terminal and the browser).
"""

from __future__ import annotations

import io
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from .world import REPO, Response, World, token


class FakeCredentials:
    """Application-default or impersonated credentials; `principal` is who they act as."""

    def __init__(self, world: World, principal: str, *, source: FakeCredentials | None = None) -> None:
        self.world = world
        self.principal = principal
        self.source = source
        self.token: str | None = None
        self.valid = False
        self.expired = False
        self.quota_project_id: str | None = None
        self.service_account_email = principal

    def refresh(self, _request: Any = None) -> None:
        if self.source is not None:
            self.world.generate_access_token(self.source.principal, self.principal)
        self.token = token()
        self.valid = True

    def with_quota_project(self, project: str) -> FakeCredentials:
        self.quota_project_id = project
        return self

    def with_scopes(self, _scopes: Any) -> FakeCredentials:
        return self


class FakeSession:
    """`AuthorizedSession`: every request goes to the world, as `credentials.principal`."""

    def __init__(self, credentials: FakeCredentials, *_a: Any, **_kw: Any) -> None:
        self.credentials = credentials

    def request(self, method: str, url: str, data: Any = None, headers: Any = None, json: Any = None,
                params: Any = None, timeout: Any = None, **_kw: Any) -> Response:
        if self.credentials.source is not None and self.credentials.token is None:
            self.credentials.refresh()
        return self.world_call(method, url, json if json is not None else data, params, headers)

    def world_call(self, method: str, url: str, body: Any, params: Any, headers: Any) -> Response:
        world = self.credentials.world
        world.principal = self.credentials.principal
        try:
            return world.http(method, url, body, params, headers)
        finally:
            world.principal = world.installer

    def get(self, url: str, **kw: Any) -> Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw: Any) -> Response:
        return self.request("POST", url, **kw)

    def close(self) -> None:
        return None


def _impersonated(world: World) -> type:
    class Impersonated(FakeCredentials):
        def __init__(self, *, source_credentials: FakeCredentials, target_principal: str,
                     target_scopes: Any = None, lifetime: int = 3600, **_kw: Any) -> None:
            super().__init__(world, target_principal, source=source_credentials)

    return Impersonated


def install(world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every seam, patched for one walk."""
    import google.auth
    import google.auth.transport.requests as transport
    import requests
    from google.auth import impersonated_credentials

    from swarm_install import tools, waits
    from swarm_install.steps import bootstrap, preflight
    from swarm_install.verbs import install as install_verb

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # A display, so every page the install sends the person to opens in the
    # "browser": the scripted person acts on it there (World.browse).
    monkeypatch.setenv("DISPLAY", ":0")
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **kw: world.browse(url))
    for name in ("SWARM_NO_BROWSER", "SWARM_CHANNEL", "SWARM_ASSUME_YES", "GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_CONFIG"):
        monkeypatch.delenv(name, raising=False)

    # The clock: every wait and every sleep runs on the world's clock.
    monkeypatch.setattr(waits.SystemClock, "now", lambda self: world.clock.now())
    monkeypatch.setattr(waits.SystemClock, "sleep", lambda self, s: world.clock.sleep(s))
    monkeypatch.setattr(waits.time, "sleep", world.clock.sleep)
    monkeypatch.setattr(bootstrap.DEPS, "sleep", world.clock.sleep)
    monkeypatch.setattr(bootstrap.DEPS, "monotonic", world.clock.monotonic)

    # Google's credentials and the HTTP session every REST client rides on.
    def default(scopes: Any = None, **_kw: Any) -> tuple[FakeCredentials, str | None]:
        if not world.signed_in:
            from google.auth.exceptions import DefaultCredentialsError

            raise DefaultCredentialsError("the person has not signed in (fake world)")
        return FakeCredentials(world, world.installer), None

    monkeypatch.setattr(google.auth, "default", default)
    monkeypatch.setattr(transport, "AuthorizedSession", FakeSession)
    monkeypatch.setattr(impersonated_credentials, "Credentials", _impersonated(world))

    def plain(method: str) -> Any:
        def call(url: str, *a: Any, **kw: Any) -> Response:
            return world.http(method, url, kw.get("json") if kw.get("json") is not None else kw.get("data"),
                              kw.get("params"), kw.get("headers"))

        return call

    class PlainSession:
        """`requests.Session()`: no ambient credentials, only the headers the caller sets."""

        trust_env = True

        def __enter__(self) -> PlainSession:
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def request(self, method: str, url: str, headers: Any = None, json: Any = None, params: Any = None,
                    data: Any = None, **_kw: Any) -> Response:
            return world.http(method, url, json if json is not None else data, params, headers)

        def get(self, url: str, **kw: Any) -> Response:
            return self.request("GET", url, **kw)

        def post(self, url: str, **kw: Any) -> Response:
            return self.request("POST", url, **kw)

        def close(self) -> None:
            return None

    monkeypatch.setattr(requests, "Session", PlainSession)
    monkeypatch.setattr(requests, "get", plain("GET"))
    monkeypatch.setattr(requests, "post", plain("POST"))

    # GCS and Firestore clients.
    from google.cloud import firestore, storage

    monkeypatch.setattr(storage, "Client", lambda *a, **kw: world.storage_client())
    monkeypatch.setattr(firestore, "Client", lambda *a, **kw: world.firestore_client(kw.get("project"),
                                                                                    kw.get("database")))

    # Commands: gcloud, kubectl, terraform and the rest, all through the world.
    def run(argv: Sequence[str], *a: Any, **kw: Any) -> subprocess.CompletedProcess[str]:
        code, out, err = world.command(list(argv), kw.get("input"), kw.get("env"), kw.get("cwd"))
        return subprocess.CompletedProcess(list(argv), code, out, err)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **kw: world.which(name))
    monkeypatch.setattr(tools, "ensure", lambda name, *a, **kw: world.ensure(name))

    # The person: every question, typed secret and typed confirmation.
    from swarm_install import secret_entry

    def ask(ctx: Any, step_id: str, question: str, *, default: str | None = None, **_kw: Any) -> str:
        return world.person.answer(step_id, question, default)

    def type_secret(ctx: Any, *, step_id: str, what: str, prompt: str, use: Any, **_kw: Any) -> Any:
        return use(world.person.secret(step_id, what, prompt).encode())

    def enter_secret(ctx: Any, *, step_id: str, project: str, secret_id: str, prompt: str, store: Any,
                     also: Any = None, **_kw: Any) -> int:
        value = world.person.secret(step_id, secret_id, prompt).encode()
        version = store.add_secret_version(project, secret_id, value)
        if also is not None:
            also(value)
        return int(version)

    monkeypatch.setattr(secret_entry, "ask", ask)
    monkeypatch.setattr(secret_entry, "type_secret", type_secret)
    monkeypatch.setattr(secret_entry, "enter_secret", enter_secret)
    monkeypatch.setattr(bootstrap, "_confirm_typed", lambda prompt: world.person.confirm(prompt))
    monkeypatch.setattr(install_verb, "_typed", lambda prompt: world.person.confirm(prompt))
    monkeypatch.setattr("builtins.input", lambda prompt="": world.person.typed(prompt))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr(preflight.Gcp, "resolve", lambda self, hostname: world.resolve(hostname))

    # The release's module: the repository's own Terraform roots, copied out of the tree.
    roots = tmp_path / "release"
    for root in ("bootstrap", "infra"):
        target = roots / root
        target.mkdir(parents=True)
        for tf in (REPO / "terraform" / root).glob("*.tf"):
            shutil.copy(tf, target / tf.name)
    monkeypatch.setattr(bootstrap.DEPS, "root_dir", lambda ctx, root: roots / root)
    world.module_roots = roots

    # Each lazy client is built once per process: start this walk with none.
    from swarm_install.steps import cluster, identity, verify

    for lazy in (preflight.GCP, cluster.CLUSTER_GCP, identity.IDENTITY_GCP, verify.VERIFY_GCP):
        monkeypatch.setattr(lazy, "made", [])
    monkeypatch.setattr(preflight.GCP, "quota_project", None)
    monkeypatch.setattr(bootstrap.DEPS, "release", None)
    monkeypatch.setattr(bootstrap.DEPS, "gcp", bootstrap._lazy_gcp())
