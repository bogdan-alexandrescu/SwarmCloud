"""`swarm upgrade` runs release V's own engine, reads the GitHub App as the App,
and finds V on the deployment's channel when no `--to` is given (audit 4e F3,
F4, F6; lane DAYONE-UPGRADE-2, part of #2688).

No test needs credentials or an emulator: the release home, uv, `execve`,
Secret Manager and GitHub are fakes, and the App's RSA key is generated here.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from swarm_install import upgrade as up
from swarm_install.exitcodes import ExitCode
from swarm_install.state import InstallState, StateStore
from swarm_install.steps.preflight import RELEASE_HOME
from swarm_install.verbs import upgrade as verb

PROJECT = "acme-swarm-1"
OLD, NEW = "2026.10.1", "2026.11.1"


def args_for(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    verb.add_arguments(parser)
    return parser.parse_args(list(argv))


# -- F6: which release ------------------------------------------------------------------


def test_to_is_optional() -> None:
    args = args_for("--project", PROJECT)
    assert args.to is None


def test_channel_release_reads_the_deployments_channel() -> None:
    read: list[str] = []

    def read_public(url: str) -> bytes:
        read.append(url)
        return b"2026.11.3-rc.1\n" if url.endswith("/rc") else f"{NEW}\n".encode()

    assert verb.channel_release(read_public, OLD) == NEW
    assert verb.channel_release(read_public, "2026.11.3-rc.0") == "2026.11.3-rc.1"
    assert read == [f"{RELEASE_HOME}/channels/stable", f"{RELEASE_HOME}/channels/rc"]
    with pytest.raises(up.UpgradeRefused, match="not a release"):
        verb.channel_release(lambda url: b"<html>", OLD)


# -- F3: V's engine ---------------------------------------------------------------------


def test_wheel_version_is_install_shs_mapping() -> None:
    assert verb.wheel_version("2026.01.2") == "2026.1.2"
    assert verb.wheel_version("2026.11.3-rc.2") == "2026.11.3rc2"
    assert verb.wheel_version("2026.11.1") == "2026.11.1"


class FakeRelease:
    """`steps.bootstrap.Release`: `sums` stands for the cosign-verified SHA256SUMS."""

    def __init__(self, version: str, *, tamper: str | None = None) -> None:
        self.files = {f"{dist}-{verb.wheel_version(version)}-py3-none-any.whl": f"{dist} wheel".encode()
                      for dist in verb.WHEELS}
        self.sums_ = {name: hashlib.sha256(data).hexdigest() for name, data in self.files.items()}
        if tamper:
            self.files[tamper] = b"something else"
        self.version = version
        self.sums_calls = 0

    def sums(self, version: str, where: Path) -> dict[str, str]:
        assert version == self.version
        self.sums_calls += 1
        return dict(self.sums_)

    def read_public(self, url: str) -> bytes:
        prefix = f"{RELEASE_HOME}/releases/{self.version}/"
        assert url.startswith(prefix), url
        return self.files[url[len(prefix):]]


class FakeUv:
    def __init__(self, bindir: Path, *, fail: bool = False) -> None:
        self.bindir = bindir
        self.fail = fail
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if argv[1:3] == ["tool", "install"]:
            if self.fail:
                return subprocess.CompletedProcess(argv, 2, "", "no python")
            self.bindir.mkdir(parents=True, exist_ok=True)
            (self.bindir / "swarm").write_text("#!/bin/sh\n")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[1:] == ["tool", "dir", "--bin"]:
            return subprocess.CompletedProcess(argv, 0, f"{self.bindir}\n", "")
        raise AssertionError(argv)


class Exec:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], dict[str, str]]] = []

    def __call__(self, path: str, argv: list[str], env: dict[str, str]) -> None:
        self.calls.append((path, list(argv), dict(env)))


def swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, installed: str | None, answer: str,
         environ: dict[str, str] | None = None, release: FakeRelease | None = None,
         confirm_plan: bool = False, uv: FakeUv | None = None,
         args: argparse.Namespace | None = None) -> tuple[Exec, FakeUv, list[str], list[str]]:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    (tmp_path / "uv").write_text("")
    runner = uv or FakeUv(tmp_path / "bin")
    execve = Exec()
    prompts: list[str] = []
    said: list[str] = []

    def confirm(prompt: str) -> str:
        prompts.append(prompt)
        return answer

    verb.ensure_engine(NEW, args or args_for("--to", NEW, "--project", PROJECT, "--json-events"),
                       installed=installed, release=release or FakeRelease(NEW), confirm=confirm,
                       confirm_plan=confirm_plan, environ=environ or {"PATH": "/usr/bin"},
                       which=lambda name: str(tmp_path / "uv"), run_=runner, execve=execve, say=said.append)
    return execve, runner, prompts, said


def test_the_engine_of_the_target_release_goes_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    execve, uv, prompts, _ = swap(tmp_path, monkeypatch, installed=NEW, answer="")
    assert execve.calls == [] and uv.calls == [] and prompts == []


def test_an_older_engine_installs_the_targets_and_runs_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = FakeRelease(NEW)
    execve, uv, prompts, _ = swap(tmp_path, monkeypatch, installed=OLD, answer=PROJECT, release=release,
                                  confirm_plan=True)
    assert len(prompts) == 1 and OLD in prompts[0] and NEW in prompts[0] and PROJECT in prompts[0]
    assert release.sums_calls == 1, "SHA256SUMS is verified with the pinned key once"
    install = uv.calls[0]
    assert install[:4] == [str(tmp_path / "uv"), "tool", "install", "--force"]
    wheels = [part.split("file://", 1)[1] for part in install if "file://" in part]
    assert [Path(w).name for w in wheels] == [f"{d}-{NEW}-py3-none-any.whl" for d in verb.WHEELS]
    assert all(Path(w).read_bytes() == release.files[Path(w).name] for w in wheels)
    swarm = str(tmp_path / "bin" / "swarm")
    [(path, argv, env)] = execve.calls
    assert path == swarm
    assert argv == [swarm, "upgrade", "--to", NEW, "--project", PROJECT, "--env", "prod", "--json-events"]
    assert env[verb.ENGINE_ENV] == NEW and env[verb.CONFIRM_PLAN_ENV] == "1" and env["PATH"] == "/usr/bin"


def test_an_older_engine_without_approval_refuses_exit_5(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(up.UpgradeRefused, match="nothing was applied") as refused:
        swap(tmp_path, monkeypatch, installed=OLD, answer="yes")
    assert verb.exit_code_for(refused.value) == ExitCode.REFUSED
    assert "SWARMCLOUD_VERSION=" + NEW in str(refused.value)


def test_a_tampered_wheel_is_never_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    uv = FakeUv(tmp_path / "bin")
    release = FakeRelease(NEW, tamper=f"swarm_install-{NEW}-py3-none-any.whl")
    with pytest.raises(up.UpgradeRefused, match="does not match its signed SHA256SUMS"):
        swap(tmp_path, monkeypatch, installed=OLD, answer=PROJECT, release=release, uv=uv)
    assert uv.calls == []


def test_a_reexec_that_is_still_not_the_target_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    uv = FakeUv(tmp_path / "bin")
    with pytest.raises(up.UpgradeRefused, match="installed for this upgrade"):
        swap(tmp_path, monkeypatch, installed=OLD, answer=PROJECT, environ={verb.ENGINE_ENV: NEW}, uv=uv)
    assert uv.calls == []


def test_a_failed_uv_install_is_a_failed_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(up.UpgradeFailed, match="uv tool install failed"):
        swap(tmp_path, monkeypatch, installed=OLD, answer=PROJECT, uv=FakeUv(tmp_path / "bin", fail=True))


# -- the verb end to end, with every cloud call faked --------------------------------


class FakeLease:
    def __init__(self, **kwargs: Any) -> None:
        self.released = False

    def acquire(self, confirm: Any = None) -> None:
        return None

    def heartbeat_due(self) -> bool:
        return False

    def renew(self) -> None:
        return None

    def release(self) -> None:
        self.released = True


class FakeBucket:
    def __init__(self, name: str) -> None:
        self.name = name

    def read(self, path: str) -> bytes | None:
        return b"{}"


class FakeLiveWorld:
    channel = NEW

    def __init__(self, *args: Any) -> None:
        self.read: list[str] = []

    def read_public(self, url: str) -> bytes:
        self.read.append(url)
        return f"{self.channel}\n".encode()

    def fetch(self, version: str, name: str) -> bytes:
        assert (version, name) == (NEW, "RELEASE.md")
        return b"# SwarmCloud 2026.11.1\n\nThe scheduler learns to count.\n"

    def _release(self) -> object:
        return object()

    def close_reach(self) -> None:
        return None


def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, release: str = OLD,
         flow_run: Any = None) -> dict[str, Any]:
    from swarm_install.steps import preflight
    from swarm_install.verbs import install

    seen: dict[str, Any] = {"engine": [], "flows": []}
    local = tmp_path / "state.json"
    real = StateStore(project=PROJECT, environment="prod", local=local)
    real.save(InstallState(project=PROJECT, environment="prod", release=release, region="us-central1"))
    monkeypatch.setattr(verb, "GcsObjectStore", FakeBucket)
    monkeypatch.setattr(verb, "StateStore", lambda **kw: StateStore(project=PROJECT, environment="prod",
                                                                    local=local))
    monkeypatch.setattr(verb, "Lease", FakeLease)
    monkeypatch.setattr(verb, "Engine", lambda *a, **kw: object())
    monkeypatch.setattr(verb, "LiveWorld", FakeLiveWorld)
    monkeypatch.setattr(install, "holder_identity", lambda: "ada@acme.com")
    monkeypatch.setattr(install, "_typed", lambda prompt: "")
    monkeypatch.setattr(preflight, "installed_release", lambda: NEW)
    monkeypatch.setattr(verb, "ensure_engine", lambda target, args, **kw: seen["engine"].append((target, kw)))

    class Flow:
        def __init__(self, **kw: Any) -> None:
            seen["flows"].append(kw)

        def run(self, target: str, *, allow_downgrade: bool = False) -> ExitCode:
            seen["target"] = target
            if flow_run is not None:
                return flow_run()
            return ExitCode.OK

    monkeypatch.setattr(verb, "Upgrade", Flow)
    return seen


def test_without_to_the_channels_release_is_shown_with_its_notes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                   capsys: pytest.CaptureFixture[str]) -> None:
    seen = live(tmp_path, monkeypatch)
    assert verb.run(args_for("--project", PROJECT)) == ExitCode.OK
    out = capsys.readouterr().out
    assert f"release {NEW} is available" in out and "The scheduler learns to count." in out
    assert seen["target"] == NEW
    assert seen["engine"][0][0] == NEW and seen["engine"][0][1]["confirm_plan"] is True
    assert seen["flows"][0]["confirm_plan"] is True


def test_without_to_on_the_channels_release_there_is_nothing_to_do(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch,
                                                                     capsys: pytest.CaptureFixture[str]) -> None:
    seen = live(tmp_path, monkeypatch, release=NEW)
    assert verb.run(args_for("--project", PROJECT)) == ExitCode.OK
    assert "nothing to do" in capsys.readouterr().out
    assert seen["engine"] == [] and seen["flows"] == []


def test_with_to_the_engine_is_checked_before_the_upgrade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = live(tmp_path, monkeypatch)
    assert verb.run(args_for("--to", NEW, "--project", PROJECT)) == ExitCode.OK
    assert seen["engine"][0][0] == NEW and seen["engine"][0][1]["installed"] == NEW
    assert seen["engine"][0][1]["confirm_plan"] is False and seen["target"] == NEW


def test_a_downgrade_is_refused_before_any_engine_is_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                               capsys: pytest.CaptureFixture[str]) -> None:
    seen = live(tmp_path, monkeypatch, release=NEW)
    assert verb.run(args_for("--to", OLD, "--project", PROJECT)) == ExitCode.REFUSED
    assert "--allow-downgrade" in capsys.readouterr().err
    assert seen["engine"] == [] and seen["flows"] == []
    assert verb.run(args_for("--to", OLD, "--project", PROJECT, "--allow-downgrade")) == ExitCode.OK
    assert seen["engine"][0][0] == OLD


def test_an_api_error_is_one_line_never_a_traceback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    import requests

    def boom() -> ExitCode:
        raise requests.HTTPError("404 Client Error: Not Found for url: https://api.github.com/apps/acme-swarm")

    live(tmp_path, monkeypatch, flow_run=boom)
    code = verb.run(args_for("--to", NEW, "--project", PROJECT))
    assert code == int(ExitCode.TROUBLE)
    err = capsys.readouterr().err
    assert "swarm upgrade: HTTPError: 404" in err and "Traceback" not in err


# -- F4: the App, read as itself ------------------------------------------------------


@pytest.fixture(scope="module")
def app_key() -> tuple[str, Any]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                            serialization.NoEncryption()).decode()
    return pem, key.public_key()


def _verify(token: str, public: Any) -> dict[str, Any]:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    def unb64(part: str) -> bytes:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))

    header, claims, signature = token.split(".")
    public.verify(unb64(signature), f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
    assert json.loads(unb64(header))["alg"] == "RS256"
    return dict(json.loads(unb64(claims)))


def test_app_jwt_is_rs256_signed_by_the_apps_key(app_key: tuple[str, Any]) -> None:
    pem, public = app_key
    claims = _verify(verb.app_jwt("12345", pem, 1_000_000.0), public)
    assert claims == {"iat": 1_000_000 - 60, "exp": 1_000_000 + 540, "iss": "12345"}
    with pytest.raises(up.UpgradeFailed, match="not an RSA private key") as bad:
        verb.app_jwt("12345", "not a key", 1_000_000.0)
    assert "not a key" not in str(bad.value)


class FakeGcp:
    def __init__(self, pem: str) -> None:
        self.pem = pem
        self.calls: list[str] = []

    def _call(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(url)
        return {"payload": {"data": base64.b64encode(self.pem.encode()).decode()}}


class FakeResponse:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self.body = body

    def json(self) -> Any:
        return self.body


def app_world(tmp_path: Path, pem: str, *, console: dict[str, Any] | None = None) -> verb.LiveWorld:
    store = StateStore(project=PROJECT, environment="prod", local=tmp_path / "state.json")
    state = InstallState(project=PROJECT, environment="prod", release=OLD, region="us-central1")
    state.outputs["github_org"] = "acme"
    state.console["github_app"] = console if console is not None else {"id": 12345, "slug": "acme-swarm",
                                                                       "installed_on": "acme"}
    store.save(state)
    world = object.__new__(verb.LiveWorld)
    world.project = PROJECT
    world.store = store
    world._key = None
    world._installation_id = None
    gcp = FakeGcp(pem)
    world.deps = type("Deps", (), {"gcp": staticmethod(lambda: gcp)})()
    return world


def test_a_private_app_is_read_as_the_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                          app_key: tuple[str, Any]) -> None:
    import requests

    pem, public = app_key
    asked: list[tuple[str, dict[str, str]]] = []
    answers = {
        "https://api.github.com/app": FakeResponse(200, {
            "slug": "acme-swarm", "owner": {"login": "acme", "type": "Organization"},
            "permissions": {"contents": "write", "metadata": "read"}}),
        "https://api.github.com/app/installations?per_page=100": FakeResponse(200, [
            {"id": 7, "account": {"login": "someone-else", "type": "User"}},
            {"id": 4242, "account": {"login": "Acme", "type": "Organization"}}]),
        "https://api.github.com/app/installations/4242": FakeResponse(200, {
            "id": 4242, "account": {"login": "acme", "type": "Organization"},
            "permissions": {"contents": "write"}}),
    }

    def get(url: str, **kwargs: Any) -> FakeResponse:
        asked.append((url, dict(kwargs["headers"])))
        return answers[url]

    monkeypatch.setattr(requests, "get", get)
    world = app_world(tmp_path, pem)
    assert world.app() == up.GithubApp(slug="acme-swarm", owner="acme", owner_is_org=True,
                                       permissions={"contents": "write", "metadata": "read"})
    assert world.installation() == up.AppInstallation(id=4242, account="acme", account_is_org=True,
                                                      permissions={"contents": "write"})
    assert world.installation() is not None
    assert [url for url, _ in asked] == [
        "https://api.github.com/app", "https://api.github.com/app/installations?per_page=100",
        "https://api.github.com/app/installations/4242", "https://api.github.com/app/installations/4242"]
    for _, headers in asked:
        scheme, token = headers["Authorization"].split(" ", 1)
        assert scheme == "Bearer" and _verify(token, public)["iss"] == "12345"
    assert world.deps.gcp().calls == [
        f"https://secretmanager.googleapis.com/v1/projects/{PROJECT}/secrets/swarm-github-app-private-key"
        "/versions/latest:access"], "the key is read once"


def test_a_github_error_is_a_failed_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                         app_key: tuple[str, Any]) -> None:
    import requests

    monkeypatch.setattr(requests, "get", lambda url, **kw: FakeResponse(404, {"message": "Not Found"}))
    with pytest.raises(up.UpgradeFailed, match="GitHub answered 404 to GET /app as App 12345: Not Found"):
        app_world(tmp_path, app_key[0]).app()


def test_no_app_yet_reads_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                  app_key: tuple[str, Any]) -> None:
    import requests

    monkeypatch.setattr(requests, "get", lambda url, **kw: pytest.fail("GitHub was asked"))
    world = app_world(tmp_path, app_key[0], console={})
    assert world.app() is None and world.installation() is None
