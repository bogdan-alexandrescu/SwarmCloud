"""Where the API is, and which credential it takes, when the bridge is not in a checkout.

Since 0.4.1 the `sc` plugin installs the bridge with `uv tool run --from
'swarm-mcp @ git+https://...#subdirectory=apps/swarm-mcp'`, so the running
`client.py` is not in this repository. It is in uv's cache:

    <uv cache>/archive-v0/<hash>/lib/python3.11/site-packages/swarm_mcp/client.py

`_repo_root()` was `Path(__file__).parents[3]`, which there is
`<hash>/lib`, and there are no tfvars under it. Two defects followed, and a
reviewer of #62 found both:

  1. `is_front_door()` knew the load balancer only from `API_HOST` or from the
     tfvars. A user who followed the README and exported
     `SWARM_API_URL=https://<the front door>` got a client that classed that
     address as Cloud Run and presented a Google ID token -- which IAP refuses
     as `Invalid JWT audience`, and which a laptop on user credentials cannot
     even mint for an audience. The README recommended exactly that variable.
  2. The developer escape hatch, `SWARM_MCP_FROM=<checkout>/apps/swarm-mcp`,
     is a NON-editable install into the same cache. It ran the working copy's
     code without seeing the working copy's repository, so it lost the
     front door that `uv run` in the same checkout finds.

Offline: the installed layout is simulated by pointing `client.__file__` into
`tmp_path` and by standing in for the install's PEP 610 record
(`direct_url.json`). CI's install step checks the same two facts against a
real `uv tool run` install (tests/unit/mcp/test_plugin_bridge_install.py).
"""

from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path

import pytest

from swarm_mcp import auth, client
from swarm_mcp.client import SwarmClient

#: Every variable that can decide the address or the credential, cleared for
#: the same reason test_front_door.py clears them: a CI shell that exports one
#: would answer these questions for us, and the answer would look like code.
_VARS = (
    "SWARM_API_URL",
    "API_URL",
    "SWARM_API_HOST",
    "API_HOST",
    "SWARM_REPO_ROOT",
    "ENVIRONMENT",
    "PROJECT_ID",
    "REGION",
    "API_SERVICE",
    "SWARM_ID_TOKEN",
    "SWARM_IAP_CLIENT_ID",
    "SWARM_IMPERSONATE_SA",
    "SWARM_ACCESS_TOKEN",
    "API_AUDIENCE",
    "K_SERVICE",
    "CLOUD_RUN_JOB",
)

#: What uv records for `swarm-mcp @ git+https://...@<ref>#subdirectory=...`:
#: PEP 610's VCS form. There is no directory on this machine to read.
_FROM_GIT = {
    "url": "https://github.com/bogdan-alexandrescu/SwarmCloud",
    "vcs_info": {
        "vcs": "git",
        "requested_revision": "sc-v0.4.1",
        "commit_id": "a83b26893b47690211b02695d70866f8333a7dca",
    },
    "subdirectory": "apps/swarm-mcp",
}

_FRONT_DOOR = "swarm.example.com"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in _VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(auth, "_metadata_available", lambda timeout=0.3: False)


class _Installed:
    """The one method of `importlib.metadata.Distribution` that is read."""

    def __init__(self, record: dict | None) -> None:
        self._record = record

    def read_text(self, filename: str) -> str | None:
        if filename == "direct_url.json" and self._record is not None:
            return json.dumps(self._record)
        return None


def _installed_into_uv_cache(monkeypatch, tmp_path: Path, record: dict | None) -> Path:
    """Make `client` look like a `uv tool run --from ...` install.

    Returns the directory `parents[3]` resolves to, which is what the old
    `_repo_root()` answered with.
    """
    site_packages = (
        tmp_path / "uv-cache" / "archive-v0" / "Xq3bVf0" / "lib" / "python3.11" / "site-packages"
    )
    monkeypatch.setattr(client, "__file__", str(site_packages / "swarm_mcp" / "client.py"))

    def _distribution(name: str) -> _Installed:
        assert name.replace("_", "-").lower() == "swarm-mcp", name
        return _Installed(record)

    monkeypatch.setattr(importlib.metadata, "distribution", _distribution)
    return site_packages.parents[1]


def _checkout(tmp_path: Path, *, hostname: str = _FRONT_DOOR) -> Path:
    """A checkout, as far as the bridge reads one: its own package and the tfvars."""
    root = tmp_path / "checkout"
    bridge = root / "apps" / "swarm-mcp"
    bridge.mkdir(parents=True)
    (bridge / "pyproject.toml").write_text('[project]\nname = "swarm-mcp"\n')
    environment = root / "terraform" / "environments" / "dev"
    environment.mkdir(parents=True)
    (environment / "dev.tfvars").write_text(
        f'enable_frontend   = true\nfrontend_hostname = "{hostname}"\n'
    )
    return root


# -- 1. an explicit front-door URL, with nothing to read it from ----------------


def test_swarm_api_url_at_the_front_door_gets_the_access_token_outside_a_checkout(
    monkeypatch, tmp_path
):
    """THE REVIEWER'S FAILURE SCENARIO. A team user installs the plugin, opens
    Claude Code in their own project and exports the front door as
    SWARM_API_URL. Before the fix `is_front_door` answered False -- it knew the
    load balancer only from API_HOST or tfvars, and a git install has neither --
    so `credential()` minted a Google ID token for the URL, which IAP answers
    `401 Invalid IAP credentials: Invalid JWT audience`.

    THE MUTATION THIS CATCHES: drop the no-declared-front-door branch from
    `is_front_door`.
    """
    _installed_into_uv_cache(monkeypatch, tmp_path, _FROM_GIT)
    monkeypatch.setenv("SWARM_API_URL", f"https://{_FRONT_DOOR}")
    monkeypatch.setattr(SwarmClient, "access_token", lambda self: "ACCESS")
    monkeypatch.setattr(SwarmClient, "_id_token", lambda self: "IDTOKEN")

    # The precondition, asserted so this cannot pass by finding tfvars somewhere.
    assert client.front_door_host() == "", "the simulated git install found a front door"

    c = SwarmClient()
    assert c.base_url == f"https://{_FRONT_DOOR}"
    assert c.front_door is True, (
        "an https address that is not *.run.app, with no front door declared "
        "anywhere, is the load balancer -- nothing else in this platform serves "
        "the API over https at another name"
    )
    assert c.credential() == "ACCESS"


@pytest.mark.parametrize(
    "url",
    [
        # Cloud Run's two URL forms: the hashed one and the deterministic one.
        "https://swarm-api-xyz123-uc.a.run.app",
        "https://swarm-api-123456789012.us-central1.run.app",
    ],
)
def test_a_run_app_address_still_gets_an_id_token_outside_a_checkout(monkeypatch, tmp_path, url):
    """The in-VPC caller names the *.run.app address ON PURPOSE -- that is what
    terraform/infra/verify.tf does -- and Cloud Run takes an ID token. The
    fallback must not turn that into an access token."""
    _installed_into_uv_cache(monkeypatch, tmp_path, _FROM_GIT)
    monkeypatch.setenv("SWARM_API_URL", url)
    monkeypatch.setattr(SwarmClient, "access_token", lambda self: "ACCESS")
    monkeypatch.setattr(SwarmClient, "_id_token", lambda self: "IDTOKEN")

    c = SwarmClient()
    assert c.front_door is False
    assert c.credential() == "IDTOKEN"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080",  # `gcloud run services proxy`, or a local API
        "http://swarm.example.com",  # IAP never serves plain http
        "",
    ],
)
def test_nothing_that_is_not_https_is_a_front_door(monkeypatch, tmp_path, url):
    """The proxy's own address is `http://127.0.0.1:<port>` and gcloud supplies
    that credential; an access token there would be a second one."""
    _installed_into_uv_cache(monkeypatch, tmp_path, _FROM_GIT)
    assert client.is_front_door(url) is False


def test_a_declared_front_door_still_decides_alone(monkeypatch, tmp_path):
    """When the front door IS declared, only that host is it -- the guard
    test_front_door.py holds against a lookalike (`swarm.example.com.evil.test`)
    must survive the fallback. An access token is a broader credential than an
    audience-bound ID token, so it goes only where a front door was named."""
    _installed_into_uv_cache(monkeypatch, tmp_path, _FROM_GIT)
    monkeypatch.setenv("API_HOST", _FRONT_DOOR)
    assert client.is_front_door(f"https://{_FRONT_DOOR}") is True
    assert client.is_front_door(f"https://{_FRONT_DOOR}.evil.test") is False
    assert client.is_front_door("https://another.example.com") is False


# -- 2. the escape hatch reads the checkout it was built from ------------------


def test_the_escape_hatch_reads_the_tfvars_of_the_checkout_it_was_built_from(
    monkeypatch, tmp_path
):
    """`SWARM_MCP_FROM=<checkout>/apps/swarm-mcp` is a NON-editable install:
    the code is the working copy's, the file is in uv's cache. uv records where
    it came from in the install's `direct_url.json` (PEP 610, `dir_info`), and
    that record is how the bridge finds its checkout -- so the escape hatch
    reaches the same front door `uv run swarm-mcp` in that checkout does.

    THE MUTATION THIS CATCHES: `_repo_root()` back to `parents[3]` alone.
    """
    root = _checkout(tmp_path)
    cache_lib = _installed_into_uv_cache(
        monkeypatch,
        tmp_path,
        {"url": (root / "apps" / "swarm-mcp").as_uri(), "dir_info": {}},
    )

    assert client._repo_root() != cache_lib, "the escape hatch read uv's cache as the repository"
    assert Path(client._repo_root()).resolve() == root.resolve()
    assert client.front_door_host() == _FRONT_DOOR


def test_a_git_install_has_no_checkout_and_reads_no_tfvars(monkeypatch, tmp_path):
    """A VCS record names a repository on GitHub, not a directory here. There
    is nothing to read, and the answer stays "no front door declared" -- never
    an error, since a solo deployment has none and must keep working."""
    _installed_into_uv_cache(monkeypatch, tmp_path, _FROM_GIT)
    assert not (Path(client._repo_root()) / "apps" / "swarm-mcp" / "pyproject.toml").exists()
    assert client.front_door_host() == ""


def test_a_directory_record_that_is_not_this_repository_is_not_read(monkeypatch, tmp_path):
    """A record naming some other directory -- a copy of apps/swarm-mcp on its
    own, say -- has no tfvars two levels up, and must not be taken for a
    checkout because its path happens to end in `swarm-mcp`."""
    stray = tmp_path / "elsewhere" / "swarm-mcp"
    stray.mkdir(parents=True)
    (stray / "pyproject.toml").write_text('[project]\nname = "swarm-mcp"\n')
    _installed_into_uv_cache(monkeypatch, tmp_path, {"url": stray.as_uri(), "dir_info": {}})
    assert client.front_door_host() == ""


def test_swarm_repo_root_still_overrides_everything(monkeypatch, tmp_path):
    """The explicit override the README documents wins over the install record."""
    recorded = _checkout(tmp_path, hostname="recorded.example.com")
    named = tmp_path / "named"
    (named / "terraform" / "environments" / "dev").mkdir(parents=True)
    (named / "terraform" / "environments" / "dev" / "dev.tfvars").write_text(
        'frontend_hostname = "named.example.com"\n'
    )
    _installed_into_uv_cache(
        monkeypatch,
        tmp_path,
        {"url": (recorded / "apps" / "swarm-mcp").as_uri(), "dir_info": {}},
    )
    monkeypatch.setenv("SWARM_REPO_ROOT", str(named))
    assert client.front_door_host() == "named.example.com"
