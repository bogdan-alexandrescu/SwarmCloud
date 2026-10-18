"""swarm-api reads its settings key and the MAC cutover list from its
settings, like the rest of its configuration (#1457, lane ISO-API-SETTINGS;
docs/design/tenant-settings-integrity.md §4.5).

ISO-SETTINGS-MAC had `swarm_api.judgment` read SWARM_SETTINGS_KEY,
SWARM_SETTINGS_KEY_PREVIOUS and SETTINGS_MAC_TENANTS from the environment
whenever a reader asked. They are now `ApiSettings.settings_key`,
`settings_key_previous` and `settings_mac_tenants`, read once at start from
the same variables, and every construction of the three readers
(`JudgmentSettings`, `MergeSettingsStore`, `CodeIntelligenceStore`) hands
them in as `judgment.settings_mac(settings)`. What these tests hold:

  * `ApiSettings.from_env` reads the three variables, the keys stay out of
    the repr, and unset they are no key and nobody cut over;
  * `settings_mac` carries them to a reader, current key first;
  * every construction of a reader in swarm_api passes the settings', and a
    reader cannot be built without them;
  * the environment alone, with settings that do not carry them, signs
    nothing and cuts nobody over -- the value reaches a reader only through
    the settings.

Every key is built at runtime (CLAUDE.md).
"""

from __future__ import annotations

import ast
import secrets
from pathlib import Path

import pytest

from swarm_api import codeintel, judgment, mergesettings
from swarm_api.auth import TENANT_HEADER
from swarm_api.settings import ApiSettings

from .conftest import api_settings, auth_header, seed_tenant

SWARM_API = Path(judgment.__file__).resolve().parent
READERS = ("JudgmentSettings", "MergeSettingsStore", "CodeIntelligenceStore")


def new_key() -> str:
    return "settings-" + secrets.token_hex(24)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", "swarm-test-project")
    monkeypatch.setenv("ENVIRONMENT", "test")
    for name in (judgment.SETTINGS_KEY_ENV, judgment.SETTINGS_KEY_PREVIOUS_ENV,
                 judgment.SETTINGS_MAC_TENANTS_ENV):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_the_settings_read_the_key_the_previous_key_and_the_cutover_list(env):
    current, previous = new_key(), new_key()
    env.setenv(judgment.SETTINGS_KEY_ENV, f" {current} ")
    env.setenv(judgment.SETTINGS_KEY_PREVIOUS_ENV, previous)
    env.setenv(judgment.SETTINGS_MAC_TENANTS_ENV, " eng, ,research ")
    settings = ApiSettings.from_env()
    assert settings.settings_key == current
    assert settings.settings_key_previous == previous
    assert settings.settings_mac_tenants == ("eng", "research")
    assert current not in repr(settings) and previous not in repr(settings)
    mac = judgment.settings_mac(settings)
    assert mac == judgment.SettingsMac(keys=(current, previous), tenants=("eng", "research"))
    assert current not in repr(mac)


def test_unset_the_settings_are_no_key_and_nobody_cut_over(env):
    settings = ApiSettings.from_env()
    assert (settings.settings_key, settings.settings_key_previous) == ("", "")
    assert settings.settings_mac_tenants == ()
    assert judgment.settings_mac(settings) == judgment.SettingsMac()
    fields = ApiSettings.__dataclass_fields__
    assert fields["settings_key"].repr is False
    assert fields["settings_key_previous"].repr is False
    assert fields["settings_mac_tenants"].default == ()


def test_the_cutover_list_takes_everyone():
    settings = api_settings(settings_mac_tenants=("*",))
    assert judgment.settings_mac(settings).tenants == ("*",)
    assert judgment.mac_enforced("anyone", judgment.settings_mac(settings).tenants)


def test_a_blank_key_is_no_key_and_the_previous_alone_still_verifies():
    previous = new_key()
    mac = judgment.settings_mac(api_settings(settings_key=" ", settings_key_previous=previous))
    assert mac.keys == (previous,)
    assert judgment.settings_mac(object()) == judgment.SettingsMac()


def _reader_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else \
            getattr(node.func, "id", None)
        if name in READERS:
            yield name, node


def test_every_reader_in_swarm_api_is_built_with_the_settings_mac():
    sites: list[str] = []
    for path in sorted(SWARM_API.rglob("*.py")):
        for name, call in _reader_calls(ast.parse(path.read_text(), str(path))):
            where = f"{path.relative_to(SWARM_API)}:{call.lineno} {name}"
            sites.append(where)
            mac = [k.value for k in call.keywords if k.arg == "mac"]
            assert mac, f"{where} builds a reader without the settings' mac"
            value = mac[0]
            assert isinstance(value, ast.Call) and getattr(value.func, "id", None) == \
                "settings_mac", f"{where} passes a mac not built from its settings"
    # live, mergetrain, service (twice), graph_ticket, typesafe_ticket and the
    # three routes (judgment twice): a count that drops means the scan missed one.
    assert len(sites) >= 10, sites


def test_a_reader_cannot_be_built_without_the_settings_mac(db):
    for build in (judgment.JudgmentSettings, mergesettings.MergeSettingsStore,
                  codeintel.CodeIntelligenceStore):
        with pytest.raises(TypeError):
            build(db, now=lambda: None)


def _get_backend(client) -> str:
    headers = auth_header("root")
    headers[TENANT_HEADER] = "eng"
    response = client.get("/v1/workspaces/eng/judgment-backend", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["judgment_backend"]


def test_the_environment_alone_signs_nothing_and_cuts_nobody_over(client, db, monkeypatch):
    # The app's settings carry no key and no cutover; the variables are set
    # on the process anyway. A reader acts on what its settings say.
    monkeypatch.setenv(judgment.SETTINGS_KEY_ENV, new_key())
    monkeypatch.setenv(judgment.SETTINGS_MAC_TENANTS_ENV, "*")
    seed_tenant(db, "eng")
    headers = auth_header("root")
    headers[TENANT_HEADER] = "eng"
    response = client.put("/v1/workspaces/eng/judgment-backend", headers=headers,
                          json={"judgment_backend": "typesafe"})
    assert response.status_code == 200, response.text
    assert "mac" not in db.docs["workspace_settings/eng"]
    response = client.put("/v1/workspaces/eng/merge-settings", headers=auth_header("root"),
                          json={"mode": "train"})
    assert response.status_code == 200, response.text
    response = client.put("/v1/workspaces/eng/code-intelligence", headers=auth_header("root"),
                          json={"jev": True})
    assert response.status_code == 200, response.text
    stored = db.docs["workspace_settings/eng"]
    assert "mac" not in stored["merge"] and "mac" not in stored["code_intelligence"]
    # Unsigned, and read as it was stored: nobody is cut over.
    assert _get_backend(client) == "typesafe"


def test_the_settings_key_reaches_every_route_writer(api_context, client, db):
    key = new_key()
    api_context.settings = api_settings(settings_key=key)
    seed_tenant(db, "eng")
    headers = auth_header("root")
    headers[TENANT_HEADER] = "eng"
    assert client.put("/v1/workspaces/eng/judgment-backend", headers=headers,
                      json={"judgment_backend": "typesafe"}).status_code == 200
    assert client.put("/v1/workspaces/eng/merge-settings", headers=auth_header("root"),
                      json={"mode": "train"}).status_code == 200
    assert client.put("/v1/workspaces/eng/code-intelligence", headers=auth_header("root"),
                      json={"jev": True}).status_code == 200
    stored = db.docs["workspace_settings/eng"]
    ident = judgment.key_id(key)
    assert stored["mac"]["key"] == ident
    assert stored["merge"]["mac"]["key"] == ident
    assert stored["code_intelligence"]["mac"]["key"] == ident
