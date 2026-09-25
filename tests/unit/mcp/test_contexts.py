"""Which SwarmCloud a command talks to -- chosen by the USER, never by this repo.

THE DEFECT, as the owner put it on 2026-09-25: "the cluster deployment is now
set to my account but could be different for other deployments. Shouldn't the
plugin be configured by the user with the urls and other credentials needed?"

It was. `client.front_door_host()` found https://swarm.saga.xyz by reading
`terraform/environments/<env>/<env>.tfvars` out of whatever checkout the bridge
was installed from. Anyone installing the `sc` plugin against their OWN
deployment got Saga's load balancer, and anyone outside a checkout got nothing.

What replaces it is the shape `gh` and `kubectl` have: named CONTEXTS in a
per-user config file, one of them current, with a strict override order --

    --context NAME            the command line, most explicit of all
    SWARM_URL (+ legacy)      CI: an environment variable beats every file
    SWARM_CONTEXT             a named context, from the environment
    SWARM_MCP_CONFIG_FROM=repo  developer mode: this checkout's tfvars
    plugin userConfig         what /plugin install prompted for
    the current context       what `sc context use` last chose

-- and the repository is consulted ONLY in developer mode. That last clause is
asserted the strong way below: an audit hook watches every file the process
opens, and a tfvars file that is opened at all, outside developer mode, fails
the test, whether or not the read "succeeded".

Everything here is offline: a config directory in `tmp_path` (the conftest
gives every test its own), a fake credential store, no network.
"""

from __future__ import annotations

import base64
import importlib
import io
import json
import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from swarm_mcp import auth, client, sc
from swarm_mcp.client import SwarmClient, SwarmError

_REPO = Path(__file__).resolve().parents[3]


def _config():
    """The contexts module, imported per test so a missing module fails each
    test on its own rather than the whole file at collection."""
    return importlib.import_module("swarm_mcp.config")


def _credentials():
    return importlib.import_module("swarm_mcp.credentials")


class FakeStore:
    """An in-memory credential store: the keyring, without the keyring."""

    name = "fake"

    def __init__(self) -> None:
        self.items: dict[str, str] = {}

    def describe(self) -> str:
        return "a fake store"

    def get(self, key: str) -> str | None:
        return self.items.get(key)

    def set(self, key: str, value: str) -> None:
        self.items[key] = value

    def delete(self, key: str) -> bool:
        return self.items.pop(key, None) is not None


#: Everything that can pick an address, cleared so a CI shell cannot answer for
#: the test. The conftest clears the new variables; these are the legacy ones.
_LEGACY = (
    "SWARM_API_URL",
    "API_URL",
    "SWARM_API_HOST",
    "API_HOST",
    "SWARM_REPO_ROOT",
    "ENVIRONMENT",
    "PROJECT_ID",
    "SWARM_ID_TOKEN",
    "SWARM_IAP_CLIENT_ID",
    "SWARM_IMPERSONATE_SA",
    "SWARM_ACCESS_TOKEN",
    "API_AUDIENCE",
    "K_SERVICE",
    "CLOUD_RUN_JOB",
)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    for name in _LEGACY:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(auth, "_metadata_available", lambda timeout=0.3: False)
    # A gcloud call from any test here is a test that reached for the machine.
    monkeypatch.setattr(
        client, "_run", lambda *a, **k: (_ for _ in ()).throw(AssertionError(f"gcloud: {a}"))
    )


@pytest.fixture()
def store(monkeypatch) -> FakeStore:
    fake = FakeStore()
    monkeypatch.setattr(_credentials(), "store", lambda environ=None: fake)
    return fake


def _run_sc(argv: list[str], *, stdin: str | None = None, monkeypatch=None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    if stdin is not None and monkeypatch is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    old_err = sys.stderr
    sys.stderr = err
    try:
        code = sc.main(argv, out=out)
    finally:
        sys.stderr = old_err
    return code, out.getvalue(), err.getvalue()


# ==========================================================================
# Context CRUD
# ==========================================================================


def test_add_list_use_remove_round_trip(store):
    config = _config()
    config.add_context("saga-dev", "https://swarm.example.test/", client_id="123-abc.apps.googleusercontent.com")
    config.add_context("other", "https://swarm.other.test")

    loaded = config.load()
    assert sorted(loaded.contexts) == ["other", "saga-dev"]
    # The trailing slash is normalised away: it is joined onto `/v1/...` later
    # and a double slash is a different path to a load balancer's URL map.
    assert loaded.contexts["saga-dev"].url == "https://swarm.example.test"
    assert loaded.contexts["saga-dev"].oauth_client_id == "123-abc.apps.googleusercontent.com"
    # The FIRST context becomes current -- a user who has exactly one
    # deployment should not need a second command to use it.
    assert loaded.current == "saga-dev"

    config.use_context("other")
    assert config.load().current == "other"

    store.set("other/refresh-token", "1//a-refresh-token")
    store.set("other/oauth-client-secret", "GOCSPX-secret")
    config.remove_context("other")
    after = config.load()
    assert sorted(after.contexts) == ["saga-dev"]
    assert after.current is None, "removing the current context leaves none current, not a guess"
    assert store.items == {}, "removing a context must remove the credentials keyed by it"


def test_using_an_unknown_context_names_the_ones_that_exist(store):
    config = _config()
    config.add_context("saga-dev", "https://swarm.example.test")
    with pytest.raises(SwarmError) as caught:
        config.use_context("sage-dev")
    assert "saga-dev" in str(caught.value)


@pytest.mark.parametrize("bad", ["", "has space", "a/b", "../up", "-leading-dash", "x" * 80])
def test_a_context_name_is_refused_when_it_could_not_key_a_credential(store, bad):
    """The name is the credential store key. A slash would make one context's
    refresh token another's path, and a leading dash is a flag to a CLI."""
    with pytest.raises(SwarmError):
        _config().add_context(bad, "https://swarm.example.test")


@pytest.mark.parametrize(
    "url",
    ["http://swarm.example.test", "swarm.example.test", "https://", "ftp://swarm.example.test",
     "https://swarm.example.test/?x=1", "https://swarm.example.test/#frag"],
)
def test_a_url_that_would_send_a_bearer_in_the_clear_or_nowhere_is_refused(store, url):
    """An ID token over plain http to anything but this machine is a
    credential on the wire. Loopback is allowed for local development."""
    with pytest.raises(SwarmError):
        _config().add_context("dev", url)


def test_loopback_http_is_allowed_for_a_local_api(store):
    context, _ = _config().add_context("local", "http://127.0.0.1:8080")
    assert context.url == "http://127.0.0.1:8080"


def test_the_config_file_is_private_and_carries_no_secret(store, monkeypatch):
    """`sc context add --client-secret-stdin` must land the secret in the
    credential store and NOWHERE in the config file, which is plain JSON a
    user may well paste into an issue."""
    code, out, err = _run_sc(
        ["context", "add", "saga-dev", "--url", "https://swarm.example.test",
         "--client-id", "123-abc.apps.googleusercontent.com", "--client-secret-stdin"],
        stdin="GOCSPX-do-not-leak\n",
        monkeypatch=monkeypatch,
    )
    assert code == 0, err
    config = _config()
    path = config.config_path()
    text = path.read_text()
    assert "GOCSPX-do-not-leak" not in text
    assert "GOCSPX-do-not-leak" not in out + err
    assert store.items["saga-dev/oauth-client-secret"] == "GOCSPX-do-not-leak"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # Only non-secret keys are ever written, whatever a caller hands over.
    body = json.loads(text)
    assert set(body["contexts"]["saga-dev"]) <= {"url", "oauth_client_id"}


def test_the_cli_lists_contexts_and_marks_the_current_one(store, monkeypatch):
    _config().add_context("a", "https://a.example.test")
    _config().add_context("b", "https://b.example.test", client_id="1-x.apps.googleusercontent.com")
    store.set("b/refresh-token", "1//r")

    code, out, _ = _run_sc(["context", "list", "--json"])
    assert code == 0
    listed = {row["name"]: row for row in json.loads(out)["contexts"]}
    assert listed["a"]["current"] is True and listed["b"]["current"] is False
    assert listed["b"]["signed_in"] is True and listed["a"]["signed_in"] is False
    assert "1//r" not in out, "a listing says WHETHER you are signed in, never with what"

    code, out, _ = _run_sc(["context", "use", "b"])
    assert code == 0
    code, out, _ = _run_sc(["context", "list"])
    line_b = next(line for line in out.splitlines() if " b " in f" {line} ")
    assert line_b.lstrip().startswith("*"), out


def test_the_cli_removes_a_context(store):
    _config().add_context("a", "https://a.example.test")
    code, _, err = _run_sc(["context", "remove", "a"])
    assert code == 0, err
    assert _config().load().contexts == {}
    code, _, err = _run_sc(["context", "remove", "a"])
    assert code == 1 and "a" in err


def test_the_config_lives_in_the_users_config_dir(monkeypatch, tmp_path):
    """XDG first on any platform (a mac user who set it meant it), then
    ~/Library/Application Support on macOS, then ~/.config elsewhere. Never
    the working directory, and never the repository."""
    config = _config()
    monkeypatch.delenv("SWARM_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    monkeypatch.setattr(sys, "platform", "darwin")
    assert config.config_dir() == tmp_path / "Library" / "Application Support" / "SwarmCloud"

    monkeypatch.setattr(sys, "platform", "linux")
    assert config.config_dir() == tmp_path / ".config" / "swarmcloud"

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(sys, "platform", "darwin")
    assert config.config_dir() == tmp_path / "xdg" / "swarmcloud"


def test_a_corrupt_config_file_is_an_error_not_an_empty_config(store):
    """"No contexts" and "could not read your contexts" are different facts,
    and the second one read as the first would send every call nowhere."""
    config = _config()
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json")
    with pytest.raises(SwarmError) as caught:
        config.load()
    assert str(path) in str(caught.value)


# ==========================================================================
# Override order
# ==========================================================================


def _layers(monkeypatch, tmp_path) -> None:
    """Every layer present at once; each test below peels them off in order."""
    config = _config()
    config.add_context("file-current", "https://current.example.test", client_id="1-a.apps.googleusercontent.com")
    config.add_context("named", "https://named.example.test")
    config.add_context("flagged", "https://flagged.example.test")
    config.use_context("file-current")
    monkeypatch.setenv("SWARM_PLUGIN_DEPLOYMENT_URL", "https://plugin.example.test")
    monkeypatch.setenv("SWARM_PLUGIN_OAUTH_CLIENT_ID", "2-b.apps.googleusercontent.com")
    tfvars = tmp_path / "repo" / "terraform" / "environments" / "dev"
    tfvars.mkdir(parents=True)
    (tfvars / "dev.tfvars").write_text('enable_frontend = true\nfrontend_hostname = "repo.example.test"\n')
    monkeypatch.setenv("SWARM_REPO_ROOT", str(tmp_path / "repo"))
    monkeypatch.setenv("SWARM_MCP_CONFIG_FROM", "repo")
    monkeypatch.setenv("SWARM_CONTEXT", "named")
    monkeypatch.setenv("SWARM_URL", "https://env.example.test")


def test_the_override_order_is_flag_env_named_repo_plugin_current(monkeypatch, tmp_path, store):
    config = _config()
    _layers(monkeypatch, tmp_path)

    assert config.resolve(context="flagged").url == "https://flagged.example.test"
    assert config.resolve().url == "https://env.example.test"

    monkeypatch.delenv("SWARM_URL")
    assert config.resolve().url == "https://named.example.test"

    monkeypatch.delenv("SWARM_CONTEXT")
    assert config.resolve().url == "https://repo.example.test"

    monkeypatch.delenv("SWARM_MCP_CONFIG_FROM")
    resolved = config.resolve()
    assert resolved.url == "https://plugin.example.test"
    assert resolved.client_id == "2-b.apps.googleusercontent.com"

    monkeypatch.delenv("SWARM_PLUGIN_DEPLOYMENT_URL")
    resolved = config.resolve()
    assert resolved.url == "https://current.example.test"
    assert resolved.context == "file-current"
    assert resolved.client_id == "1-a.apps.googleusercontent.com"

    config.remove_context("file-current")
    assert config.resolve() is None, "nothing configured must resolve to nothing, not to a default"


def test_the_legacy_api_url_variables_still_override(monkeypatch, store):
    """`terraform/infra/verify.tf` sets API_URL for the in-VPC job and `swarm
    init` writes SWARM_API_URL into .env. Both are explicit, so both still win
    over any file."""
    _config().add_context("c", "https://current.example.test")
    monkeypatch.setenv("API_URL", "https://swarm-api-xyz-uc.a.run.app")
    resolved = _config().resolve()
    assert resolved.url == "https://swarm-api-xyz-uc.a.run.app"
    assert resolved.front_door is False, "a run.app address is Cloud Run, not IAP"


def test_an_env_url_borrows_the_sign_in_of_a_context_with_the_same_url(monkeypatch, store):
    """SWARM_URL on a laptop that already signed in to that deployment must
    use that sign-in: credentials are keyed by the context, and the context is
    found by the address."""
    _config().add_context("saga-dev", "https://swarm.example.test", client_id="1-a.apps.googleusercontent.com")
    monkeypatch.setenv("SWARM_URL", "https://swarm.example.test/")
    resolved = _config().resolve()
    assert resolved.context == "saga-dev"
    assert resolved.client_id == "1-a.apps.googleusercontent.com"


def test_the_client_id_and_secret_from_the_environment_override_the_files(monkeypatch, store):
    _config().add_context("c", "https://swarm.example.test", client_id="1-a.apps.googleusercontent.com")
    monkeypatch.setenv("SWARM_OAUTH_CLIENT_ID", "9-z.apps.googleusercontent.com")
    monkeypatch.setenv("SWARM_OAUTH_CLIENT_SECRET", "GOCSPX-from-env")
    resolved = _config().resolve()
    assert resolved.client_id == "9-z.apps.googleusercontent.com"
    assert resolved.client_secret == "GOCSPX-from-env"
    assert "GOCSPX-from-env" not in repr(resolved), "a secret must not survive a repr into a log"


@pytest.mark.parametrize("unset", ["", "${user_config.oauth_client_id}"])
def test_an_unset_plugin_option_is_unset_whatever_claude_code_substituted(monkeypatch, store, unset):
    """Claude Code's docs do not say what an unset option becomes inside
    `env`: an empty string, or the reference left as written. Both must read
    as "not set", or the literal `${user_config...}` becomes a client id."""
    monkeypatch.setenv("SWARM_PLUGIN_DEPLOYMENT_URL", "https://plugin.example.test")
    monkeypatch.setenv("SWARM_PLUGIN_OAUTH_CLIENT_ID", unset)
    monkeypatch.setenv("SWARM_PLUGIN_OAUTH_CLIENT_SECRET", "${user_config.oauth_client_secret}")
    resolved = _config().resolve()
    assert resolved.client_id == ""
    assert resolved.client_secret == ""


def test_the_plugin_seeds_a_context_the_terminal_can_find(monkeypatch, store):
    """The MCP server sees the plugin's values; a terminal running `sc login`
    does not. Seeding writes the non-secret half into the config file and the
    secret into the credential store, so `sc login` in any shell finds both."""
    monkeypatch.setenv("SWARM_PLUGIN_DEPLOYMENT_URL", "https://plugin.example.test")
    monkeypatch.setenv("SWARM_PLUGIN_OAUTH_CLIENT_ID", "2-b.apps.googleusercontent.com")
    monkeypatch.setenv("SWARM_PLUGIN_OAUTH_CLIENT_SECRET", "GOCSPX-plugin")
    config = _config()
    seeded = config.seed_from_plugin()
    assert seeded is not None

    loaded = config.load()
    context = loaded.contexts[seeded.context]
    assert context.url == "https://plugin.example.test"
    assert context.oauth_client_id == "2-b.apps.googleusercontent.com"
    assert loaded.current == seeded.context, "with no current context the plugin's becomes current"
    assert store.items[f"{seeded.context}/oauth-client-secret"] == "GOCSPX-plugin"
    assert "GOCSPX-plugin" not in config.config_path().read_text()


def test_the_plugin_overrides_a_context_with_its_url_but_never_steals_current(monkeypatch, store):
    """/plugin configure is where the user changes the client id; the context
    with that URL takes the new value. It does NOT move `current` off a
    context the user chose with `sc context use` -- a background process
    switching the terminal's deployment is how a dispatch lands on the wrong
    cluster."""
    config = _config()
    config.add_context("mine", "https://plugin.example.test", client_id="1-old.apps.googleusercontent.com")
    config.add_context("elsewhere", "https://elsewhere.example.test")
    config.use_context("elsewhere")
    monkeypatch.setenv("SWARM_PLUGIN_DEPLOYMENT_URL", "https://plugin.example.test")
    monkeypatch.setenv("SWARM_PLUGIN_OAUTH_CLIENT_ID", "2-new.apps.googleusercontent.com")

    seeded = config.seed_from_plugin()
    loaded = config.load()
    assert seeded.context == "mine"
    assert loaded.contexts["mine"].oauth_client_id == "2-new.apps.googleusercontent.com"
    assert loaded.current == "elsewhere"
    # ...but inside the plugin's own process the plugin's deployment is used.
    assert config.resolve().url == "https://plugin.example.test"


# ==========================================================================
# No repository coupling
# ==========================================================================

#: Paths opened while `_WATCHING` is set. An audit hook cannot be removed once
#: added, so it is added once per process and switched by this flag.
_OPENED: list[str] = []
_WATCHING = threading.Event()


def _audit(event: str, args: tuple) -> None:
    if event == "open" and _WATCHING.is_set() and args and isinstance(args[0], (str, bytes, os.PathLike)):
        path = os.fsdecode(args[0])
        if path.endswith(".tfvars"):
            _OPENED.append(path)


sys.addaudithook(_audit)


@pytest.fixture()
def watch_tfvars():
    _OPENED.clear()
    _WATCHING.set()
    try:
        yield _OPENED
    finally:
        _WATCHING.clear()


def _unreadable_repo(tmp_path: Path) -> Path:
    """A checkout whose tfvars names a front door -- and cannot be read.

    Unreadable as well as watched: chmod alone proves nothing to a test running
    as root (CI containers often do), and a read that fails is caught and
    treated as "no front door" by design, so only the audit hook can tell "never
    looked" from "looked and failed". Both are applied, and the hook decides.
    """
    directory = tmp_path / "repo" / "terraform" / "environments" / "dev"
    directory.mkdir(parents=True)
    tfvars = directory / "dev.tfvars"
    tfvars.write_text('enable_frontend = true\nfrontend_hostname = "saga-only.example.test"\n')
    tfvars.chmod(0)
    return tmp_path / "repo"


def test_the_bridge_never_opens_a_tfvars_file_outside_developer_mode(
    monkeypatch, tmp_path, store, watch_tfvars
):
    """THE DEFECT, pinned. Before this change, constructing a client in a
    checkout read Saga's tfvars and pointed every user at swarm.saga.xyz.

    THE MUTATION THIS CATCHES: remove the `repo_mode()` gate from
    `client.front_door_host`. The hook then records the open and this fails,
    even though the read itself raises PermissionError and is swallowed.
    """
    repo = _unreadable_repo(tmp_path)
    monkeypatch.setenv("SWARM_REPO_ROOT", str(repo))
    _config().add_context("mine", "https://mine.example.test", client_id="1-a.apps.googleusercontent.com")
    # THE SETUP WROTE THE FILE, and a write is an `open`. Forget it: only what
    # the bridge opens from here on is the question. (Measured: without this
    # line the test failed against the FIXED bridge too -- CI run 36093266742
    # -- so its first red, 36093035420, proved nothing about the gate.)
    watch_tfvars.clear()

    try:
        built = SwarmClient()
        assert built.base_url == "https://mine.example.test"
        assert client.front_door_host() == ""
        assert client.is_front_door("https://saga-only.example.test") is False
        _config().resolve()
        # `swarm doctor` is the command most likely to go looking; it must not.
        out = io.StringIO()
        monkeypatch.setattr(
            "swarm_mcp.cli.SwarmClient",
            lambda *a, **k: (_ for _ in ()).throw(SwarmError("no api here")),
        )
        from swarm_mcp import cli

        monkeypatch.setenv("PROJECT_ID", "a-project")
        import contextlib

        with contextlib.redirect_stdout(out):
            cli.cmd_doctor(None, None)
    finally:
        (repo / "terraform" / "environments" / "dev" / "dev.tfvars").chmod(0o644)

    assert watch_tfvars == [], f"tfvars opened outside developer mode: {watch_tfvars}"
    assert "saga-only.example.test" not in out.getvalue()


def test_developer_mode_does_read_the_repository(monkeypatch, tmp_path, store, watch_tfvars):
    """The positive control. Without it the test above passes just as well
    against a bridge that can no longer read tfvars at all -- which would
    break `make smoke`-style in-repo use for the owner."""
    repo = _unreadable_repo(tmp_path)
    (repo / "terraform" / "environments" / "dev" / "dev.tfvars").chmod(0o644)
    monkeypatch.setenv("SWARM_REPO_ROOT", str(repo))
    monkeypatch.setenv("SWARM_MCP_CONFIG_FROM", "repo")
    watch_tfvars.clear()  # the setup's own write, as above

    assert client.front_door_host() == "saga-only.example.test"
    assert watch_tfvars, "developer mode read no tfvars file"
    resolved = _config().resolve()
    assert resolved.url == "https://saga-only.example.test"
    assert resolved.front_door is True


def test_nothing_configured_says_how_to_configure_it(monkeypatch, store):
    """A plugin user with no context and no PROJECT_ID must be told the two
    commands that fix it, not handed a gcloud error about a proxy."""
    monkeypatch.setattr(
        auth,
        "Proxy",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("a gcloud proxy was started")),
    )
    with pytest.raises(SwarmError) as caught:
        SwarmClient()
    message = str(caught.value)
    assert "sc context add" in message
    assert "SWARM_URL" in message


# ==========================================================================
# Which door a context is
# ==========================================================================


def test_a_context_with_a_client_id_is_an_iap_front_door(store):
    _config().add_context("team", "https://swarm.example.test", client_id="1-a.apps.googleusercontent.com")
    assert _config().resolve().front_door is True


def test_a_context_on_a_run_app_address_is_cloud_run(store):
    """A solo deployment is reached at its *.run.app address; nothing there
    takes an IAP credential."""
    _config().add_context("solo", "https://swarm-api-xyz-uc.a.run.app")
    assert _config().resolve().front_door is False


def _jwt(claims: dict) -> str:
    """An unsigned JWT: three base64url parts, the middle one these claims."""

    def _part(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{_part({'alg': 'RS256'})}.{_part(claims)}.c2lnbmF0dXJl"


class _Recorded(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _recording_opener(sent: list, body: dict):
    def _opener(request, timeout=None):  # noqa: ARG001
        sent.append((request.full_url, request.get_header("Authorization")))
        return _Recorded(json.dumps(body).encode())

    return _opener


def test_a_solo_context_on_user_credentials_reaches_cloud_run_as_the_developer(monkeypatch, store):
    """REVIEW OF PR #61, 2026-09-25: a solo deployment could not be used at all.

    The plugin makes `deployment_url` required and tells a solo user to enter
    their *.run.app address and leave the client id empty. That resolves to a
    Deployment, so `connect()` skips the gcloud proxy -- and on ordinary user
    credentials (the PROXY tier) `credential()` then asked
    `auth.id_token_for(tier=PROXY)`, which ALWAYS raises "user credentials
    cannot mint a token for a specific audience; this tier reaches the API
    through a local proxy instead" -- about a proxy nothing had started. Every
    tool and every `sc` failed, and nothing the user could configure fixed it.
    `test_a_context_on_a_run_app_address_is_cloud_run` above passed throughout:
    it asserted `front_door is False` and never sent a request.

    Cloud Run documents the developer's path directly: "curl -H
    "Authorization: Bearer $(gcloud auth print-identity-token)" SERVICE_URL",
    for an account holding run.routes.invoke
    (docs.cloud.google.com/run/docs/authenticating/developers). So this sends
    ONE request down that path and checks what arrives: gcloud's own identity
    token, minted once, with no audience flag (a user account cannot set one)
    and no impersonation, at the run.app address -- and no proxy started.
    """
    _config().add_context("solo", "https://swarm-api-xyz-uc.a.run.app")
    monkeypatch.setattr(
        auth, "Proxy", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a gcloud proxy was started"))
    )
    token = _jwt({"email": "dev@example.test", "exp": int(time.time()) + 3600})
    minted: list[list[str]] = []
    monkeypatch.setattr(client, "_run", lambda argv, **kw: minted.append(list(argv)) or token)
    sent: list = []
    monkeypatch.setattr(client, "_open", _recording_opener(sent, {"tenant_id": "solo-tenant"}))

    built = SwarmClient()
    assert built.tier is auth.Tier.PROXY, "the fixture leaves this machine on user credentials"
    assert built.front_door is False
    assert built.request("GET", "/v1/tenants/me") == {"tenant_id": "solo-tenant"}
    built.request("GET", "/v1/stats")

    assert [url for url, _ in sent] == [
        "https://swarm-api-xyz-uc.a.run.app/v1/tenants/me",
        "https://swarm-api-xyz-uc.a.run.app/v1/stats",
    ]
    assert [auth_header for _, auth_header in sent] == [f"Bearer {token}"] * 2
    assert minted == [["gcloud", "auth", "print-identity-token"]], (
        "one mint for two requests, with no --audiences (gcloud refuses one for a "
        f"user account) and no impersonation: {minted}"
    )


def test_gclouds_identity_token_is_reused_only_until_shortly_before_it_expires(monkeypatch, store):
    """gcloud hands back the identity token it already holds, which may have
    minutes left. Reusing it for the client's usual 45 minutes would turn a
    long `swarm tail` into a 401 at the moment it expires, so its own `exp`
    decides."""
    _config().add_context("solo", "https://swarm-api-xyz-uc.a.run.app")
    now = int(time.time())
    minted: list[list[str]] = []
    monkeypatch.setattr(
        client, "_run", lambda argv, **kw: minted.append(list(argv)) or _jwt({"exp": now + 120})
    )
    sent: list = []
    monkeypatch.setattr(client, "_open", _recording_opener(sent, {}))

    built = SwarmClient()
    built.request("GET", "/v1/stats")
    built.request("GET", "/v1/stats")
    assert len(minted) == 2, "a token two minutes from expiry must not be reused"


def test_a_context_on_any_other_host_is_a_load_balancer(store):
    """Outside the VPC the only address a team deployment serves is its load
    balancer, and this platform's load balancer is always behind IAP
    (terraform/modules/frontend turns it on unconditionally). So a context on
    a non-run.app host is a front door even before a client id is set -- which
    is what lets CI reach it with SWARM_IMPERSONATE_SA and an access token."""
    _config().add_context("team", "https://swarm.example.test")
    assert _config().resolve().front_door is True
