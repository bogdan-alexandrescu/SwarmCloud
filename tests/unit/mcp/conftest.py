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
    "XDG_CONFIG_HOME",
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
