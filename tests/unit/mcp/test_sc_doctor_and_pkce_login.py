"""`sc login --pkce-only`, and `sc doctor`/`sc whoami` naming a version skew
(DAYONE-PLUGIN-1B, part of #2688; plan §6.3 and §6.5).

`--pkce-only` is GATE 0b's Q1 probe: the Desktop client's sign-in with PKCE
and no client secret, so the CLI must hand `pkce_only=True` to
`signin.login` and read no secret. `sc doctor` and `sc whoami` print
`discovery.Checker.status()` and say, in plain words, whether the plugin and
the deployment agree -- and when they do not, which of the two to upgrade.
"""

from __future__ import annotations

import io
import json
import secrets
from types import SimpleNamespace
from typing import Any

import pytest

from swarm_mcp import discovery, sc, signin
from swarm_mcp.client import SwarmError

URL = "https://swarm.example.test"


@pytest.fixture
def deployment(monkeypatch):
    chosen = SimpleNamespace(
        context="qa", url=URL, source="test", current=True, client_id="", front_door=False, skew="",
    )
    monkeypatch.setattr(sc, "_resolve", lambda args: chosen)
    return chosen


def _bundle(tmp_path, monkeypatch, *, plugin: str, low: str, high: str) -> None:
    root = tmp_path / "plugin"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "sc", "version": plugin}))
    (root / "compat.json").write_text(
        json.dumps({"plugin": plugin, "deployments": {"min": low, "max_exclusive": high}})
    )
    monkeypatch.setenv(discovery.PLUGIN_ROOT_ENV, str(root))


def _serve(monkeypatch, *, version: str, min_plugin: str = "") -> None:
    document = {
        "schema": 1,
        "name": "example",
        "version": version,
        "oauth_client_id": f"{secrets.randbelow(10**9)}-{secrets.token_hex(6)}.apps.googleusercontent.com",
        "plugin": {"min_version": min_plugin},
    }
    monkeypatch.setattr(
        discovery, "CACHE", discovery.Cache(get=lambda url, timeout: (200, json.dumps(document).encode()))
    )


def _doctor(*extra: str) -> tuple[int, str]:
    out = io.StringIO()
    code = sc.main(["doctor", *extra], out=out)
    return code, out.getvalue()


def _line(text: str, label: str) -> str:
    return next(row for row in text.splitlines() if row.startswith(f"{label} "))


# -- sc login --pkce-only -------------------------------------------------------


def test_login_pkce_only_asks_signin_for_no_secret(monkeypatch, deployment):
    asked: list[dict[str, Any]] = []

    def login(chosen, **kwargs):
        asked.append(kwargs)
        raise SwarmError("stopped after the call this test reads")

    monkeypatch.setattr(signin, "login", login)
    # MUTATION: a parser without the flag exits 2 here; a cmd_login that does
    # not forward it calls login without pkce_only=True.
    assert sc.main(["login", "--pkce-only"], out=io.StringIO()) == sc.EXIT_FAIL
    assert len(asked) == 1 and asked[0].get("pkce_only") is True, asked
    assert asked[0].get("client_secret") is None, "a PKCE-only sign-in reads no secret"


def test_login_without_the_flag_is_not_pkce_only(monkeypatch, deployment):
    asked: list[dict[str, Any]] = []

    def login(chosen, **kwargs):
        asked.append(kwargs)
        raise SwarmError("stopped after the call this test reads")

    monkeypatch.setattr(signin, "login", login)
    sc.main(["login"], out=io.StringIO())
    assert asked and not asked[0].get("pkce_only"), asked


# -- sc doctor ------------------------------------------------------------------


def test_doctor_names_a_newer_deployment_and_says_upgrade_the_plugin(tmp_path, monkeypatch, deployment):
    _bundle(tmp_path, monkeypatch, plugin="1.2.0", low="1.0.0", high="1.3.0")
    _serve(monkeypatch, version="1.4.0")
    code, text = _doctor()
    assert code == sc.EXIT_TROUBLE, text
    versions = _line(text, "versions")
    assert "plugin 1.2.0" in versions and "deployment 1.4.0" in versions, text
    verdict = _line(text, "compatible")
    assert verdict.split()[1] == "no:", verdict
    assert "newer" in verdict and "Upgrade the plugin" in verdict, verdict
    assert discovery.UPDATE_PLUGIN in verdict, verdict


def test_doctor_names_an_older_deployment_and_says_upgrade_the_deployment(tmp_path, monkeypatch, deployment):
    _bundle(tmp_path, monkeypatch, plugin="1.2.0", low="1.1.0", high="1.3.0")
    _serve(monkeypatch, version="1.0.5")
    code, text = _doctor()
    assert code == sc.EXIT_TROUBLE, text
    assert "deployment 1.0.5" in _line(text, "versions"), text
    verdict = _line(text, "compatible")
    assert "older" in verdict and "Upgrade the deployment" in verdict, verdict
    assert "swarm upgrade --to 1.2.0" in verdict, verdict


def test_doctor_names_a_plugin_the_deployment_no_longer_accepts(tmp_path, monkeypatch, deployment):
    _bundle(tmp_path, monkeypatch, plugin="1.2.0", low="1.0.0", high="2.0.0")
    _serve(monkeypatch, version="1.5.0", min_plugin="1.4.0")
    code, text = _doctor("--json")
    assert code == sc.EXIT_TROUBLE, text
    versions = json.loads(text)["versions"]
    assert versions["compatible"] is False and versions["upgrade"] == "plugin", versions
    assert versions["plugin"] == "1.2.0" and versions["deployment"] == "1.5.0", versions


def test_doctor_says_yes_when_they_agree(tmp_path, monkeypatch, deployment):
    _bundle(tmp_path, monkeypatch, plugin="1.2.0", low="1.0.0", high="1.3.0")
    _serve(monkeypatch, version="1.2.4", min_plugin="1.1.0")
    code, text = _doctor()
    assert code == sc.EXIT_OK, text
    assert _line(text, "compatible").split()[1] == "yes:", text


def test_doctor_never_calls_an_unread_version_fine(tmp_path, monkeypatch, deployment):
    _bundle(tmp_path, monkeypatch, plugin="1.2.0", low="1.0.0", high="1.3.0")
    monkeypatch.setattr(discovery, "CACHE", discovery.Cache(get=lambda url, timeout: (404, b"")))
    code, text = _doctor()
    assert code == sc.EXIT_TROUBLE, text
    assert "deployment version unknown" in _line(text, "versions"), text
    verdict = _line(text, "compatible")
    assert "not checked" in verdict and "(404)" in verdict, verdict


# -- sc whoami ------------------------------------------------------------------


def test_whoami_prints_the_skew_line(tmp_path, monkeypatch, deployment):
    _bundle(tmp_path, monkeypatch, plugin="1.2.0", low="1.0.0", high="1.3.0")
    _serve(monkeypatch, version="1.4.0")

    class _Api:
        def request(self, method, path, **_kwargs):
            if path == "/v1/tenants/me":
                return {"tenant": {"tenant_id": "eng"}, "principal": {"email": "carol@example.com"}}
            return []

        def tenants_mine(self):
            return []

    class _Ctx:
        def __enter__(self):
            return _Api()

        def __exit__(self, *exc):
            return None

    monkeypatch.setattr(sc, "SwarmClient", lambda *a, **k: _Ctx())
    monkeypatch.setenv("SWARM_TENANT", "eng")
    monkeypatch.setenv("SWARM_ID_TOKEN", ".".join(["test", "id", "tok"]))
    out = io.StringIO()
    # The skew never decides whoami's exit code: whoami answers who.
    assert sc.cmd_whoami(None, SimpleNamespace(json=False, context=None), out) == sc.EXIT_OK
    text = out.getvalue()
    assert "deployment 1.4.0" in _line(text, "versions"), text
    assert "Upgrade the plugin" in _line(text, "compatible"), text
