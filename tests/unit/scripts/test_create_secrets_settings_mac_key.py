"""`scripts/create-secrets.sh --settings-mac-key`: the key swarm-api MACs every
`workspace_settings` block with (#1457; docs/design/tenant-settings-integrity.md
§5 request B; lane ISO-SETTINGS-MAC).

Run for real against test_create_secrets_egress.py's fake `gcloud`. What it
holds:

  * the key is generated, never read, never shown, and never accepted on the
    command line;
  * the secret is never created here: a missing one is refused, because its
    container and its two readers are terraform's;
  * a key that already has an enabled version is only added to after a TYPED
    confirmation, the secret's own name; SWARM_ASSUME_YES does not count;
  * it is not combined with another platform key's slot;
  * its name is the one swarm-api reads (`swarm_api.judgment.SETTINGS_KEY_SECRET_ID`).
"""

from __future__ import annotations

import os
import subprocess

import pytest

from .test_create_secrets_egress import SCRIPT, _adds, _run, pytestmark  # noqa: F401

KEY = "swarm-settings-key"


def test_the_slot_names_the_secret_swarm_api_reads():
    # Imported here, not at module top: collection of tests/unit/scripts must not
    # load swarm_api (test_collection_stays_cheap.py).
    from swarm_api.judgment import SETTINGS_KEY_SECRET_ID

    assert SETTINGS_KEY_SECRET_ID == KEY
    assert f'SETTINGS_KEY_NAME="{KEY}"' in SCRIPT.read_text()


def test_a_first_key_is_generated_and_never_shown(tmp_path):
    rc, out, calls = _run(tmp_path, "--settings-mac-key", existing=KEY)
    assert rc == 0, out
    adds = _adds(calls)
    assert len(adds) == 1 and adds[0]["args"][3] == KEY
    assert len(adds[0]["data"]) >= 32
    assert adds[0]["data"] not in out


@pytest.mark.parametrize("argv", [("--settings-mac-key=VALUE",),
                                  ("--settings-mac-key", "--stdin"),
                                  ("--settings-mac-key", "--from-file", "/dev/null"),
                                  ("--settings-mac-key", "--tenant", "eng"),
                                  ("--settings-mac-key", "--disable-previous"),
                                  ("--settings-mac-key", "--typesafe-fwd-key"),
                                  ("--settings-mac-key", "--workspace-grant-key")])
def test_the_key_takes_no_value_no_tenant_and_no_other_slot(tmp_path, argv):
    rc, out, calls = _run(tmp_path, *argv, existing=KEY)
    assert rc != 0, out
    assert _adds(calls) == []
    assert "VALUE" not in out.replace("--settings-mac-key=VALUE", "")


def test_a_missing_container_is_refused_not_created(tmp_path):
    rc, out, calls = _run(tmp_path, "--settings-mac-key", existing="")
    assert rc != 0
    assert f"{KEY} does not exist" in out and "terraform" in out
    assert _adds(calls) == [] and not any(c["args"][:2] == ["secrets", "create"] for c in calls)


def test_an_enabled_key_needs_its_typed_name(tmp_path):
    rc, out, calls = _run(tmp_path, "--settings-mac-key", existing=KEY, enabled=KEY,
                          assume_yes=True)
    assert rc != 0, out
    assert _adds(calls) == []
    assert f"--overwrite {KEY}" in out
    rc, out, calls = _run(tmp_path, "--settings-mac-key", "--overwrite", "swarm-egress-key",
                          existing=KEY, enabled=KEY)
    assert rc != 0 and _adds(calls) == []
    rc, out, calls = _run(tmp_path, "--settings-mac-key", "--overwrite", KEY,
                          existing=KEY, enabled=KEY)
    assert rc == 0, out
    assert [a["args"][3] for a in _adds(calls)] == [KEY]


def test_help_names_the_slot_and_the_re_sign_job():
    proc = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True,
                          timeout=30, check=False,
                          env={**os.environ, "SWARM_ENV_FILE": "/nonexistent", "NO_COLOR": "1"})
    assert proc.returncode == 0, proc.stderr
    assert "--settings-mac-key" in proc.stdout and KEY in proc.stdout
    assert "scripts/resign-workspace-settings.sh" in proc.stdout
