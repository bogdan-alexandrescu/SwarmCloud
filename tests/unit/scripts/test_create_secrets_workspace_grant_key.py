"""`scripts/create-secrets.sh --workspace-grant-key`: the workspace-secret
grant attestation key (docs/design/workspace-secrets.md, SEC-GRANT-ATTEST;
part of #1347).

terraform/infra/workspace_grant_key.tf creates `swarm-workspace-grant-key`
with no version; this slot is the one way its value is made. Run for real
against test_create_secrets_egress.py's fake `gcloud`. What it holds:

  * the key is generated, never read, never shown, and never accepted on the
    command line;
  * the secret is never created here: a missing one is refused, naming the
    terraform file that creates it;
  * a key that already has an enabled version is only added to after a TYPED
    confirmation, the secret's own name; SWARM_ASSUME_YES does not count;
  * it is not combined with another platform key's slot.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from .test_create_secrets_egress import SCRIPT, _adds, _run, pytestmark  # noqa: F401

KEY = "swarm-workspace-grant-key"


def test_a_first_key_is_generated_and_never_shown(tmp_path):
    rc, out, calls = _run(tmp_path, "--workspace-grant-key", existing=KEY)
    assert rc == 0, out
    adds = _adds(calls)
    assert len(adds) == 1 and adds[0]["args"][3] == KEY
    assert len(adds[0]["data"]) >= 32
    assert adds[0]["data"] not in out


@pytest.mark.parametrize("argv", [("--workspace-grant-key=VALUE",),
                                  ("--workspace-grant-key", "--stdin"),
                                  ("--workspace-grant-key", "--from-file", "/dev/null"),
                                  ("--workspace-grant-key", "--tenant", "eng"),
                                  ("--workspace-grant-key", "--egress-key"),
                                  ("--workspace-grant-key", "--typesafe-fwd-key")])
def test_the_key_takes_no_value_no_tenant_and_no_other_slot(tmp_path, argv):
    rc, out, calls = _run(tmp_path, *argv, existing=KEY)
    assert rc != 0, out
    assert _adds(calls) == []
    assert "VALUE" not in out.replace("--workspace-grant-key=VALUE", "")


def test_a_missing_container_is_refused_not_created(tmp_path):
    rc, out, calls = _run(tmp_path, "--workspace-grant-key", existing="")
    assert rc != 0
    assert "terraform/infra/workspace_grant_key.tf" in out
    assert _adds(calls) == [] and not any(c["args"][:2] == ["secrets", "create"] for c in calls)


def test_an_enabled_key_needs_its_typed_name(tmp_path):
    rc, out, calls = _run(tmp_path, "--workspace-grant-key", existing=KEY, enabled=KEY,
                          assume_yes=True)
    assert rc != 0, out
    assert _adds(calls) == []
    assert f"--overwrite {KEY}" in out
    rc, out, calls = _run(tmp_path, "--workspace-grant-key", "--overwrite", "swarm-egress-key",
                          existing=KEY, enabled=KEY)
    assert rc != 0 and _adds(calls) == []
    rc, out, calls = _run(tmp_path, "--workspace-grant-key", "--overwrite", KEY,
                          existing=KEY, enabled=KEY)
    assert rc == 0, out
    assert [a["args"][3] for a in _adds(calls)] == [KEY]


def test_help_names_the_slot():
    proc = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True,
                          timeout=30, check=False,
                          env={**os.environ, "SWARM_ENV_FILE": "/nonexistent", "NO_COLOR": "1"})
    assert proc.returncode == 0, proc.stderr
    assert "--workspace-grant-key" in proc.stdout and KEY in proc.stdout


def _declared_previous_version_settings() -> set[str]:
    """Every `<variable>.previous_version` terraform/infra declares as an
    object attribute, plus every bare `*_previous_version` variable."""
    import re

    from .test_create_secrets_egress import ROOT

    declared: set[str] = set()
    for tf in (ROOT / "terraform" / "infra").glob("*.tf"):
        text = tf.read_text()
        for block in re.finditer(r'variable\s+"([a-z0-9_]+)"\s*\{(.*?)\n\}', text, re.S):
            name, body = block.group(1), block.group(2)
            if name.endswith("previous_version"):
                declared.add(name)
            if re.search(r"^\s*previous_version\s*=", body, re.M):
                declared.add(f"{name}.previous_version")
    return declared


def test_the_rotation_line_names_a_setting_terraform_declares(tmp_path):
    # #1334: the dim after the mint (and the header --help prints) told the
    # operator to set `workspace_grant_key_previous_version`, which no
    # variable declares; terraform refuses an undeclared tfvars key.
    import re

    declared = _declared_previous_version_settings()
    assert "workspace_grant_attestation.previous_version" in declared
    rc, out, _calls = _run(tmp_path, "--workspace-grant-key", existing=KEY)
    assert rc == 0, out
    helped = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True,
                            timeout=30, check=False,
                            env={**os.environ, "SWARM_ENV_FILE": "/nonexistent",
                                 "NO_COLOR": "1"}).stdout
    for printed in (out, helped):
        named = set(re.findall(r"[a-z0-9_.]*previous_version", printed))
        assert "workspace_grant_attestation.previous_version" in named, printed
        assert named <= declared | {"previous_version"}, named - declared
