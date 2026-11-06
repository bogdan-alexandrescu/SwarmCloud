"""Let the bridge's tests reach the control plane's fixtures.

`test_against_the_real_api.py` builds the REAL swarm-api application rather
than a fake of it, and the assembly for that -- settings, the in-memory
Firestore, the tenant seed -- already exists once in
`tests/unit/control_plane/conftest.py`. A second copy here would be a second
statement of what a tenant document looks like, which is the drift this
repository keeps finding.

`tests/unit/control_plane` is a package, so pytest puts `tests/unit` on
`sys.path` when it collects anything from it -- but only then. Running
`pytest tests/unit/mcp` on its own would not, and a suite that passes in one
invocation and fails in another is worse than either. So it is put there
explicitly.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

_UNIT = str(Path(__file__).resolve().parents[1])
if _UNIT not in sys.path:
    sys.path.insert(0, _UNIT)


#: Every variable that picks WHICH deployment the bridge talks to, or where it
#: keeps what it knows about one. The bridge became a deployment-agnostic client
#: on 2026-09-25: it reads a per-user config file and a per-user credential
#: store, and a Claude Code plugin hands it values through its environment. A
#: suite that inherited any of that from the machine running it would answer
#: with the developer's own deployment -- and the answer would look like a code
#: change. So every test starts with none of it.
_DEPLOYMENT_VARS = (
    "SWARM_URL",
    "SWARM_CONTEXT",
    "SWARM_OAUTH_CLIENT_ID",
    "SWARM_OAUTH_CLIENT_SECRET",
    "SWARM_MCP_CONFIG_FROM",
    "SWARM_PLUGIN_DEPLOYMENT_URL",
    "SWARM_PLUGIN_OAUTH_CLIENT_ID",
    "SWARM_PLUGIN_OAUTH_CLIENT_SECRET",
    "SWARM_PLUGIN_DEFAULT_TARGET",
    # Which of the caller's tenants to act as (#447): set on the machine, it
    # would put X-Swarm-Tenant on every request a test makes.
    "SWARM_TENANT",
    "XDG_CONFIG_HOME",
    # Which Claude Code session the bridge serves (SC-SESSION-SCOPE): set on
    # the machine -- every Claude Code session sets the first -- it would tag
    # every submission a test makes and scope every attach and check to it.
    "CLAUDE_CODE_SESSION_ID",
    "SWARM_SC_SESSION_ID",
    # The session's project (SC-TITLES): set on the machine, every launch a
    # test makes would write its run copy and its launched.jsonl line into
    # the developer's own project.
    "CLAUDE_PROJECT_DIR",
)


@pytest.fixture(autouse=True)
def _isolated_deployment_config(monkeypatch, tmp_path_factory):
    """A private config directory and a FILE credential store, for every test.

    SWARM_CONFIG_DIR points at a fresh directory so no test reads or writes
    the real `~/Library/Application Support/SwarmCloud/config.json`, and
    SWARM_CREDENTIAL_STORE=file keeps every test away from the real macOS
    Keychain or Secret Service -- a test that wrote a refresh token into the
    developer's keychain would be a test with a side effect nobody could see.
    Tests about a specific store replace it explicitly.
    """
    for name in _DEPLOYMENT_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SWARM_CONFIG_DIR", str(tmp_path_factory.mktemp("swarm-config")))
    monkeypatch.setenv("SWARM_CREDENTIAL_STORE", "file")
    # `swarm_dispatch` and `swarm_workflow` infer the repository from the git
    # checkout the bridge runs in when a caller names none (swarm_mcp.checkout).
    # A test runs in THIS checkout -- in CI a detached merge commit, which the
    # inference refuses -- so every test starts in an empty directory that is
    # no checkout at all, and the tests about inference build their own.
    monkeypatch.setenv("SWARM_CHECKOUT_DIR", str(tmp_path_factory.mktemp("not-a-checkout")))
    # With no project known, every launch -- a swarm_dispatch's row included --
    # writes its run copy and its launched.jsonl line (SC-TITLES) to the
    # system temp directory. A private one per test, so no test reads
    # another's record and none is left in the machine's.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path_factory.mktemp("system-tmp")))


@pytest.fixture(autouse=True)
def _fresh_since_handles():
    """A fresh store of `since` handles for every test.

    The bridge keeps each follower's position for the life of its process
    (`progress.HANDLES`), and a follow WITHOUT `since` for tasks it already
    reported holds instead of answering at once. Shared across tests, one
    test's `task_a` would turn the next test's first call into a hold.
    """
    from swarm_mcp import progress

    handles = getattr(progress, "HANDLES", None)
    if handles is not None:
        handles.clear()
    yield
    if handles is not None:
        handles.clear()


@pytest.fixture
def claude_session(monkeypatch) -> str:
    """This test's Claude Code session (SC-SESSION-SCOPE): set where Claude
    Code sets it, so the bridge tags what it submits and scopes what it
    attaches and checks to it. A fresh id per test."""
    import uuid

    sid = str(uuid.uuid4())
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
    return sid



@pytest.fixture(autouse=True)
def _no_github_reads(monkeypatch):
    """`swarm_check` reads a refused merge step's PR state from GitHub's
    public API (`check.github_pull_state`, epic #1334). A unit test runs with
    no network, so every test starts with that read failing -- the check's
    own "PR state not read" path -- and the tests about the read put their
    own answer there."""
    from swarm_mcp import check
    from swarm_mcp.client import SwarmError

    def offline(key, number):
        raise SwarmError("unit tests read no network")

    monkeypatch.setattr(check, "github_pull_state", offline, raising=False)


@pytest.fixture(autouse=True)
def _no_automatic_build_board(monkeypatch):
    """No test refreshes the build board behind its own back (BOARD-WIRE).

    The stdio loop refreshes the board after a submission, a check or a
    follow (`server._answered`), in a background thread, into the session's
    project -- which, with CLAUDE_PROJECT_DIR unset above, is the checkout
    the suite runs in. Every test that speaks the protocol would write a
    board there and read the fake control plane once more behind its back.
    This refresher starts nothing; the tests of the refresh build their own.
    """
    from swarm_mcp import buildboard

    monkeypatch.setattr(buildboard, "AUTO", buildboard.AutoRefresh(start=lambda run: None))


@pytest.fixture(autouse=True)
def _no_discovery_reads(monkeypatch):
    """`sc whoami` and `sc doctor` read the deployment's discovery document
    for the version-skew line (DAYONE-PLUGIN-1B, plan §6.5). A unit test reads
    no network, so the read fails the way an unreachable deployment's does --
    the line's own "not checked" path -- and the tests about the skew serve
    their own document through `discovery.CACHE`."""
    from swarm_mcp import discovery

    def offline(url, timeout):
        raise discovery.DiscoveryUnreachable(f"cannot read {url} (unit tests read no network)")

    monkeypatch.setattr(discovery, "http_get", offline)
