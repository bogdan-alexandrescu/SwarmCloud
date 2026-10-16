"""`scripts/create-secrets.sh --graph-ticket-key`: the shared graph service's
ticket MAC key (docs/design/graph-service.md §3.2; lane GS-INFRA, part of
#1384).

terraform/infra/graph_service.tf creates `swarm-graph-ticket-key` with no
version; this slot is the one way its value is made. Run for real against
test_create_secrets_egress.py's fake `gcloud`, as
test_create_secrets_typesafe_fwd.py runs --typesafe-fwd-key. What it holds:

  * the key is generated, never read, never shown, and never accepted on the
    command line;
  * the secret is never created here: a missing one is refused, naming the
    terraform file that creates it;
  * a key that already has an enabled version is only added to after a TYPED
    confirmation, the secret's own name; SWARM_ASSUME_YES does not count;
  * it is one slot: combined with another platform key, nothing is stored.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from .test_create_secrets_egress import SCRIPT, _adds, _run, pytestmark  # noqa: F401

KEY = "swarm-graph-ticket-key"


def test_a_first_key_is_generated_and_never_shown(tmp_path):
    rc, out, calls = _run(tmp_path, "--graph-ticket-key", existing=KEY)
    assert rc == 0, out
    adds = _adds(calls)
    assert len(adds) == 1 and adds[0]["args"][3] == KEY
    assert len(adds[0]["data"]) >= 32
    assert adds[0]["data"] not in out


@pytest.mark.parametrize("argv", [("--graph-ticket-key=VALUE",),
                                  ("--graph-ticket-key", "--stdin"),
                                  ("--graph-ticket-key", "--from-file", "/dev/null"),
                                  ("--graph-ticket-key", "--tenant", "eng"),
                                  ("--graph-ticket-key", "--egress-key"),
                                  ("--graph-ticket-key", "--typesafe-fwd-key"),
                                  ("--graph-ticket-key", "--workspace-grant-key"),
                                  ("--typesafe-fwd-key", "--graph-ticket-key"),
                                  ("--workspace-grant-key", "--graph-ticket-key")])
def test_the_key_takes_no_value_no_tenant_and_no_other_slot(tmp_path, argv):
    rc, out, calls = _run(tmp_path, *argv, existing=KEY)
    assert rc != 0, out
    assert _adds(calls) == []
    assert "VALUE" not in out.replace("--graph-ticket-key=VALUE", "")


def test_a_missing_container_is_refused_not_created(tmp_path):
    rc, out, calls = _run(tmp_path, "--graph-ticket-key", existing="")
    assert rc != 0
    assert "terraform/infra/graph_service.tf" in out
    assert _adds(calls) == [] and not any(c["args"][:2] == ["secrets", "create"] for c in calls)


def test_an_enabled_key_needs_its_typed_name(tmp_path):
    rc, out, calls = _run(tmp_path, "--graph-ticket-key", existing=KEY, enabled=KEY,
                          assume_yes=True)
    assert rc != 0, out
    assert _adds(calls) == []
    assert f"--overwrite {KEY}" in out
    rc, out, calls = _run(tmp_path, "--graph-ticket-key", "--overwrite", "swarm-egress-key",
                          existing=KEY, enabled=KEY)
    assert rc != 0 and _adds(calls) == []
    rc, out, calls = _run(tmp_path, "--graph-ticket-key", "--overwrite", KEY,
                          existing=KEY, enabled=KEY)
    assert rc == 0, out
    assert [a["args"][3] for a in _adds(calls)] == [KEY]


def test_help_names_the_slot():
    proc = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True,
                          timeout=30, check=False,
                          env={**os.environ, "SWARM_ENV_FILE": "/nonexistent", "NO_COLOR": "1"})
    assert proc.returncode == 0, proc.stderr
    assert "--graph-ticket-key" in proc.stdout and KEY in proc.stdout
