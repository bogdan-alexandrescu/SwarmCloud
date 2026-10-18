"""`sc` and the sc plugin act in the tenant SWARM_TENANT names (SC-TENANT).

Owner decision, 2026-10-12, answering TENANT-SELF (#1204): "sc reads
SWARM_TENANT". #1204 made a person's ready personal workspace their default
tenant when no `X-Swarm-Tenant` is sent, so a tool that sends none moves off
the group it used to act in. The bridge and `swarm` already honoured
SWARM_TENANT; `sc` never did, and the plugin had no way to set it.

These hold:

* `sc` with SWARM_TENANT=eng sends `X-Swarm-Tenant: eng` on EVERY request it
  makes, through the real `SwarmClient` and the real wire; unset, it sends none.
* `sc whoami` and `sc config` say which tenant, and which setting chose it.
* The plugin's `tenant` option reaches the bridge as SWARM_TENANT, and a
  SWARM_TENANT already in Claude Code's environment wins over it.
* The SessionStart hook's `sc workflows` runs in that same tenant.
* The plugin's version moved, so the change reaches installed sessions.

And, since the default became the person's own (PROFILE-SC,
docs/design/user-profile.md §4.5 and Q9):

* with no SWARM_TENANT, `sc whoami` / `sc config` say which default applied,
  from the `default_source` `GET /v1/tenants/mine` reports on its default
  entry -- Settings › Profile, the personal workspace, or "your preferences
  could not be read; name a workspace";
* a 503 `preferences_unavailable` is printed with the API's message and a
  non-zero exit, and nothing is retried in another workspace.

Offline: the transport is `client._open`, replaced; nothing leaves the process.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import stat
import subprocess
import urllib.error
from pathlib import Path

import pytest

from swarm_mcp import client as mcp_client
from swarm_mcp import sc
from swarm_mcp.client import chosen_tenant

_REPO = Path(__file__).resolve().parents[3]
_PLUGIN = _REPO / "plugin"
_MANIFEST = _PLUGIN / ".claude-plugin" / "plugin.json"
_HOOK = _PLUGIN / "hooks" / "session-start.sh"

#: The version main carried before this change (PROFILE-SC). A plugin change
#: that leaves the version where it was never reaches an installed session.
_VERSION_BEFORE = (0, 5, 43)


class _Response:
    status = 200

    def __init__(self, payload) -> None:
        self._raw = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


@pytest.fixture()
def wire(monkeypatch):
    """Every request `sc` sends through the real client, headers lowercased."""
    # A *.run.app address, never an invented host: any other host is read as an
    # IAP front door, whose credential is an access token minted by shelling
    # out to gcloud. CI has no gcloud account, so the request was never sent.
    monkeypatch.setenv("SWARM_URL", "https://swarm-api-test.a.run.app")
    # An explicit token, built at runtime: the tier needs one and no test here
    # is about which.
    monkeypatch.setenv("SWARM_ID_TOKEN", ".".join(["test", "id", "tok"]))
    sent: list[dict] = []

    def _opener(req, timeout=None):  # noqa: ARG001
        headers = {k.lower(): v for k, v in req.header_items()}
        sent.append({"url": req.full_url, "headers": headers})
        if "/v1/tenants/me" in req.full_url:
            chosen = headers.get("x-swarm-tenant") or "personal-dev"
            return _Response({"tenant": {"tenant_id": chosen}, "principal": {"email": "dev@example.com"}})
        if "/v1/tenants/mine" in req.full_url and _MINE in wire_state:
            return _Response(wire_state[_MINE])
        if _REFUSE in wire_state and "x-swarm-tenant" not in headers:
            # What PROFILE-DEFAULT's API answers a headerless request whose
            # preferences it could not read: every route but the three exempt.
            raise _http_error(req.full_url, 503, wire_state[_REFUSE])
        return _Response({"items": [], "tasks": [], "next_page_token": None})

    wire_state.clear()
    monkeypatch.setattr(mcp_client, "_open", _opener)
    return sent


#: What the `wire` fixture serves beyond its defaults, set by a test before it
#: runs `sc`: `_MINE` is the `GET /v1/tenants/mine` body, `_REFUSE` the message
#: of a 503 `preferences_unavailable` for every headerless non-exempt request.
wire_state: dict[str, object] = {}
_MINE = "mine"
_REFUSE = "refuse"

#: The API's own sentence (swarm_api/errors.py PREFERENCES_UNAVAILABLE_MESSAGE),
#: which `sc` must print word for word.
_PREFS_MESSAGE = (
    "Your SwarmCloud preferences could not be read, so your default workspace is "
    "not known and nothing was submitted. Name a workspace and retry: choose one "
    "in the console's workspace switcher, set SWARM_TENANT or the /sc plugin's "
    "tenant option, or pass --tenant to swarm."
)


def _http_error(url: str, status: int, message: str) -> urllib.error.HTTPError:
    body = json.dumps({"code": "preferences_unavailable", "message": message}).encode()
    return urllib.error.HTTPError(url, status, "unavailable", {}, io.BytesIO(body))


def _mine(source: str) -> list[dict]:
    """A `tenants/mine` body as PROFILE-DEFAULT's API answers it."""
    return [
        {"tenant_id": "personal-dev", "display_name": "Your workspace (personal-dev)",
         "kind": "user", "is_default": True, "default_source": source},
        {"tenant_id": "eng", "display_name": "eng@example.com", "kind": "group",
         "is_default": False},
    ]


def _run(argv) -> tuple[int, str]:
    out = io.StringIO()
    code = sc.main(list(argv), out=out)
    return code, out.getvalue()


_VERBS = [[], ["whoami"], ["config"], ["workflows"]]


@pytest.mark.parametrize("argv", _VERBS, ids=lambda a: " ".join(a) or "sc")
def test_sc_sends_the_tenant_swarm_tenant_names_on_every_call(wire, monkeypatch, argv):
    monkeypatch.setenv("SWARM_TENANT", "eng")
    _run(argv)
    assert wire, f"`sc {' '.join(argv)}` sent nothing, so this proved nothing"
    named = [(s["url"], s["headers"].get("x-swarm-tenant")) for s in wire]
    assert all(tenant == "eng" for _, tenant in named), named


@pytest.mark.parametrize("argv", _VERBS, ids=lambda a: " ".join(a) or "sc")
def test_sc_without_swarm_tenant_sends_no_tenant_header(wire, monkeypatch, argv):
    monkeypatch.delenv("SWARM_TENANT", raising=False)
    _run(argv)
    assert wire, f"`sc {' '.join(argv)}` sent nothing, so this proved nothing"
    assert not [s["url"] for s in wire if "x-swarm-tenant" in s["headers"]]


def test_a_blank_swarm_tenant_is_no_choice_for_sc(wire, monkeypatch):
    monkeypatch.setenv("SWARM_TENANT", "  ")
    _run(["whoami"])
    assert wire and not [s for s in wire if "x-swarm-tenant" in s["headers"]]


def test_sc_chooses_the_tenant_with_the_bridges_own_helper():
    """No restatement: the rule lives once, in `client.chosen_tenant`."""
    assert sc.chosen_tenant is chosen_tenant
    source = Path(sc.__file__).read_text()
    assert 'environ.get("SWARM_TENANT")' not in source
    assert "os.environ[\"SWARM_TENANT\"]" not in source


def test_whoami_prints_the_tenant_and_that_swarm_tenant_chose_it(wire, monkeypatch):
    monkeypatch.setenv("SWARM_TENANT", "eng")
    code, text = _run(["whoami"])
    assert code == sc.EXIT_OK, text
    line = next(row for row in text.splitlines() if row.startswith("tenant "))
    assert "eng" in line and "SWARM_TENANT" in line, text


def test_whoami_says_the_default_was_used_when_nothing_was_chosen(wire, monkeypatch):
    monkeypatch.delenv("SWARM_TENANT", raising=False)
    code, text = _run(["whoami"])
    assert code == sc.EXIT_OK, text
    line = next(row for row in text.splitlines() if row.startswith("tenant "))
    assert "personal-dev" in line and "default" in line and "SWARM_TENANT" in line, text


def test_whoami_json_carries_the_choice(wire, monkeypatch):
    monkeypatch.setenv("SWARM_TENANT", "eng")
    _, text = _run(["whoami", "--json"])
    shown = json.loads(text)
    assert shown["tenant"] == "eng"
    assert shown["tenant_chosen"] == "eng"
    assert "SWARM_TENANT" in shown["tenant_source"]


def test_config_prints_the_tenant_and_which_setting_chose_it(wire, monkeypatch):
    monkeypatch.setenv("SWARM_TENANT", "eng")
    _, text = _run(["config"])
    line = next(row for row in text.splitlines() if row.startswith("tenant "))
    assert "eng" in line and "SWARM_TENANT" in line, text
    monkeypatch.delenv("SWARM_TENANT")
    _, text = _run(["config", "--json"])
    tenant = json.loads(text)["tenant"]
    assert tenant["value"] == "personal-dev"
    assert "default" in tenant["source"], tenant


# --------------------------------------------------------------------------
# Which default applied (PROFILE-SC)
# --------------------------------------------------------------------------


def _tenant_line(text: str) -> str:
    return next(row for row in text.splitlines() if row.startswith("tenant "))


def test_whoami_says_the_default_came_from_settings_profile(wire, monkeypatch):
    monkeypatch.delenv("SWARM_TENANT", raising=False)
    wire_state[_MINE] = _mine("profile")
    code, text = _run(["whoami"])
    assert code == sc.EXIT_OK, text
    line = _tenant_line(text)
    assert "personal-dev" in line and "your default, from Settings › Profile" in line, text


@pytest.mark.parametrize("source", ["personal", None])
def test_whoami_says_the_personal_workspace_otherwise(wire, monkeypatch, source):
    """`personal`, and an API that reports no `default_source` at all."""
    monkeypatch.delenv("SWARM_TENANT", raising=False)
    mine = _mine(source or "personal")
    if source is None:
        for entry in mine:
            entry.pop("is_default"), entry.pop("default_source", None)
    wire_state[_MINE] = mine
    code, text = _run(["whoami"])
    assert code == sc.EXIT_OK, text
    line = _tenant_line(text)
    assert "personal workspace" in line, text
    if source is not None:
        assert "from Settings › Profile" not in line, text


def test_whoami_says_unreadable_preferences_mean_name_a_workspace(wire, monkeypatch):
    monkeypatch.delenv("SWARM_TENANT", raising=False)
    wire_state[_MINE] = _mine("unavailable")
    _, text = _run(["whoami"])
    assert "your preferences could not be read; name a workspace" in _tenant_line(text), text
    _, text = _run(["whoami", "--json"])
    shown = json.loads(text)
    assert shown["default_source"] == "unavailable"
    assert "your preferences could not be read; name a workspace" in shown["tenant_source"]


def test_config_says_which_default_applied(wire, monkeypatch):
    monkeypatch.delenv("SWARM_TENANT", raising=False)
    wire_state[_MINE] = _mine("profile")
    _, text = _run(["config", "--json"])
    tenant = json.loads(text)["tenant"]
    assert tenant["value"] == "personal-dev"
    assert "your default, from Settings › Profile" in tenant["source"], tenant


def test_swarm_tenant_still_wins_over_a_profile_default(wire, monkeypatch):
    monkeypatch.setenv("SWARM_TENANT", "eng")
    wire_state[_MINE] = _mine("profile")
    code, text = _run(["whoami"])
    assert code == sc.EXIT_OK, text
    line = _tenant_line(text)
    assert "eng" in line and "chosen by SWARM_TENANT" in line, text
    assert "Settings › Profile" not in line, text


@pytest.mark.parametrize("argv", [["runs"], ["agents"]], ids=" ".join)
def test_preferences_unavailable_is_the_apis_message_and_a_failure(wire, monkeypatch, capsys, argv):
    monkeypatch.delenv("SWARM_TENANT", raising=False)
    wire_state[_REFUSE] = _PREFS_MESSAGE
    code, out = _run(argv)
    err = capsys.readouterr().err
    assert code != sc.EXIT_OK, (out, err)
    # A screen view wraps its lines; the words, in order, are the API's.
    assert _PREFS_MESSAGE in " ".join((out + err).split()), (out, err)
    assert "swarm doctor" not in err, "the API answered; this is no connection failure"
    refused = [s for s in wire if "/v1/tenants/" not in s["url"]]
    assert refused, "nothing was asked, so this proved nothing"
    # NEVER RETRIED IN ANOTHER WORKSPACE: no request named one.
    assert not [s["url"] for s in wire if "x-swarm-tenant" in s["headers"]]


def test_the_refusal_is_printed_as_the_gate_prints_its_own(wire, monkeypatch, capsys):
    monkeypatch.delenv("SWARM_TENANT", raising=False)
    wire_state[_REFUSE] = _PREFS_MESSAGE
    code, _ = _run(["runs"])
    err = capsys.readouterr().err
    assert code == sc.EXIT_FAIL
    assert err.strip() == f"✕ 503 preferences_unavailable: {_PREFS_MESSAGE}", err


# --------------------------------------------------------------------------
# The plugin: userConfig.tenant -> SWARM_TENANT, the environment winning
# --------------------------------------------------------------------------


def _manifest() -> dict:
    return json.loads(_MANIFEST.read_text())


def _claude_code_expands(value: str, options: dict, environ: dict) -> str:
    """What Claude Code does to a plugin MCP server's env value, in its order:
    `${user_config.KEY}` first, then `${VAR}` / `${VAR:-default}` from its own
    environment, where a variable that is SET (even empty) beats the default."""
    value = re.sub(r"\$\{user_config\.([^}]+)\}", lambda m: str(options[m.group(1)]), value)

    def _env(match: re.Match) -> str:
        name, _, default = match.group(1).partition(":-")
        if name in environ:
            return environ[name]
        return default if ":-" in match.group(1) else match.group(0)

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*(?::-[^}]*)?)\}", _env, value)


def test_the_plugin_offers_a_tenant_option():
    option = _manifest()["userConfig"].get("tenant")
    assert option is not None, "plugin.json offers no tenant setting"
    assert option["type"] == "string"
    assert not option.get("required")
    assert option.get("sensitive") is False
    described = option["description"]
    assert "the tenant every call acts in" in described.lower()
    assert "empty uses your default" in described
    assert "Settings › Profile" in described, "the default is the person's own now (PROFILE-SC)"
    assert "personal workspace" in described
    assert "SWARM_TENANT" in described and "wins" in described


def test_the_tenant_option_reaches_the_bridge_as_swarm_tenant():
    env = _manifest()["mcpServers"]["swarmcloud"]["env"]
    assert "SWARM_TENANT" in env, "the tenant option never reaches the bridge"
    assert "${user_config.tenant}" in env["SWARM_TENANT"]


@pytest.mark.parametrize(
    ("option", "environ", "expected"),
    [
        ("eng", {}, "eng"),
        ("", {}, None),
        ("eng", {"SWARM_TENANT": "ops"}, "ops"),
        ("", {"SWARM_TENANT": "ops"}, "ops"),
    ],
)
def test_the_bridge_acts_in_the_option_unless_the_environment_names_one(option, environ, expected):
    raw = _manifest()["mcpServers"]["swarmcloud"]["env"]["SWARM_TENANT"]
    expanded = _claude_code_expands(raw, {"tenant": option}, environ)
    assert chosen_tenant(None, {"SWARM_TENANT": expanded}) == expected, expanded


def test_the_plugin_version_moved():
    manifest = _manifest()
    version = tuple(int(part) for part in manifest["version"].split("."))
    assert version > _VERSION_BEFORE, manifest["version"]
    pinned = manifest["mcpServers"]["swarmcloud"]["args"]
    assert any(f"@sc-v{manifest['version']}#" in arg for arg in pinned)


# --------------------------------------------------------------------------
# The SessionStart hook: its `sc workflows` runs in the same tenant
# --------------------------------------------------------------------------


def _hook(tmp_path, **env: str) -> str | None:
    """Run the hook against a fake uv; what SWARM_TENANT the bridge saw."""
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover
        pytest.skip("bash is not installed")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    seen = tmp_path / "seen-tenant"
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/usr/bin/env bash\n"
        'if [ -n "${SWARM_TENANT+x}" ]; then printf "%s" "$SWARM_TENANT" > "$SWARM_SEEN"; fi\n'
    )
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    base = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
        "CLAUDE_PLUGIN_OPTION_DEPLOYMENT_URL": "https://swarm.example.test",
        "SWARM_SEEN": str(seen),
    }
    base.update(env)
    done = subprocess.run(
        [bash, str(_HOOK)], input="{}", capture_output=True, text=True,
        timeout=60, env=base, check=False,
    )
    assert done.returncode == 0, done.stderr
    return seen.read_text() if seen.exists() else None


def test_the_hook_runs_sc_workflows_in_the_plugins_tenant(tmp_path):
    assert _hook(tmp_path, CLAUDE_PLUGIN_OPTION_TENANT="eng") == "eng"


def test_the_hook_lets_swarm_tenant_win_over_the_option(tmp_path):
    assert _hook(tmp_path, CLAUDE_PLUGIN_OPTION_TENANT="eng", SWARM_TENANT="ops") == "ops"


def test_the_hook_names_no_tenant_when_neither_is_set(tmp_path):
    assert _hook(tmp_path, CLAUDE_PLUGIN_OPTION_TENANT="") is None
    assert _hook(tmp_path) is None
