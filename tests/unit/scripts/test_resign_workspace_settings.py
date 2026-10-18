"""`scripts/resign-workspace-settings.sh` signs the stored `workspace_settings`
blocks through `swarm_api.judgment.resign_workspace_settings`, dry run by
default (#1457; docs/design/tenant-settings-integrity.md §5 request B; lane
ISO-SETTINGS-MAC).

The harness runs the REAL script and the REAL swarm_api with no credentials,
as test_workspace_migrate_record.py does: a `sitecustomize` on PYTHONPATH
swaps `google.cloud.firestore.Client` for the in-memory FakeFirestore, loaded
from and saved back to a pickle, and `secretmanager.SecretManagerServiceClient`
for one that answers the runtime-built key. The typed confirmation is
answered through a pseudo-terminal, because the script refuses anything else.
"""

from __future__ import annotations

import os
import pickle
import pty
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "resign-workspace-settings.sh"
FAKES_DIR = REPO / "tests" / "unit" / "control_plane"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")

SITECUSTOMIZE = f"""
import atexit, os, pickle, sys
_world = os.environ.get("SWARM_TEST_FIRESTORE")
if _world:
    sys.path.insert(0, {str(FAKES_DIR)!r})
    import fakes
    from google.cloud import firestore, secretmanager
    _db = fakes.FakeFirestore()
    with open(_world, "rb") as fh:
        _db.docs = pickle.load(fh)
    firestore.Client = lambda *a, **k: _db

    class _Payload:
        data = open(os.environ["SWARM_TEST_KEY_FILE"], "rb").read()

    class _Version:
        payload = _Payload()

    class _Secrets:
        def __init__(self, *a, **k):
            pass

        def access_secret_version(self, name):
            with open(os.environ["SWARM_TEST_KEY_ASKED"], "a") as fh:
                fh.write(name + "\\n")
            return _Version()

    secretmanager.SecretManagerServiceClient = _Secrets

    def _save():
        with open(_world, "wb") as fh:
            pickle.dump(_db.docs, fh)

    atexit.register(_save)
"""


def _legacy() -> dict:
    return {
        "workspace_settings/eng": {
            "tenant_id": "eng", "judgment_backend": "typesafe",
            "merge": {"mode": "train", "train_size": 4, "max_train_age_minutes": 90},
        },
        "workspace_settings/research": {
            "tenant_id": "research",
            "code_intelligence": {"jev": True, "judgment_backend": "typesafe"},
        },
    }


class World:
    def __init__(self, tmp: Path, docs: dict) -> None:
        self.file = tmp / "firestore.pickle"
        self.file.write_bytes(pickle.dumps(docs))
        self.key = "settings-" + secrets.token_hex(24)
        key_file = tmp / "key"
        key_file.write_text(self.key)
        self.asked = tmp / "asked"
        site = tmp / "site"
        site.mkdir()
        (site / "sitecustomize.py").write_text(SITECUSTOMIZE)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("SWARM_", "CLOUDSDK_", "GOOGLE_", "PROJECT_ID",
                                    "ENVIRONMENT", "FIRESTORE_"))}
        env.update(
            SWARM_ENV_FILE=str(tmp / "absent.env"),
            SWARM_TEST_FIRESTORE=str(self.file),
            SWARM_TEST_KEY_FILE=str(key_file),
            SWARM_TEST_KEY_ASKED=str(self.asked),
            PYTHONPATH=os.pathsep.join(filter(None, [str(site), env.get("PYTHONPATH", "")])),
            PROJECT_ID="saga-agents-staging",
            ENVIRONMENT="dev",
            NO_COLOR="1",
            TMPDIR=str(tmp),
        )
        self.env = env

    def read(self) -> dict:
        return pickle.loads(self.file.read_bytes())

    def run(self, *args: str, extra_env: dict | None = None,
            typed: str | None = None) -> subprocess.CompletedProcess:
        env = {**self.env, **(extra_env or {})}
        command = [str(SCRIPT), *args]
        if typed is None:
            return subprocess.run(command, env=env, cwd=REPO, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=300, check=False)
        controller, terminal = pty.openpty()
        try:
            os.write(controller, typed.encode() + b"\n")
            proc = subprocess.run(command, env=env, cwd=REPO, stdin=terminal,
                                  capture_output=True, text=True, timeout=300, check=False)
        finally:
            os.close(terminal)
            os.close(controller)
        return proc


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path, _legacy())


def test_the_script_is_executable_and_starts_with_set_euo_pipefail() -> None:
    assert os.access(SCRIPT, os.X_OK)
    effective = [line for line in SCRIPT.read_text().splitlines()[1:]
                 if line.strip() and not line.lstrip().startswith("#")]
    assert effective[0] == "set -euo pipefail"


def test_the_mac_is_not_restated_in_the_script() -> None:
    text = "\n".join(line for line in SCRIPT.read_text().splitlines()
                     if not line.lstrip().startswith("#"))
    assert "resign_workspace_settings(" in text and "SETTINGS_KEY_SECRET_ID" in text
    for restated in ("hmac", "canonical_settings_block", "SETTINGS_MAC_FIELDS", "sha256"):
        assert restated not in text


def test_a_dry_run_prints_the_plan_and_writes_nothing(world) -> None:
    # swarm_api is imported inside the test, not at module top: collection of
    # tests/unit/scripts must not load it (test_collection_stays_cheap.py).
    from swarm_api import judgment

    before = world.read()
    proc = world.run()
    assert proc.returncode == 0, proc.stderr
    assert world.read() == before
    assert "dry run: nothing was written" in proc.stderr
    assert "unsigned\teng\ttop" in proc.stderr and "held\tresearch\tcode_intelligence" in proc.stderr
    assert judgment.key_id(world.key) in proc.stderr
    assert world.key not in proc.stderr + proc.stdout
    assert world.asked.read_text().strip() == (
        "projects/saga-agents-staging/secrets/swarm-settings-key/versions/latest")


def test_apply_signs_after_the_typed_word_and_a_second_run_writes_nothing(world) -> None:
    # swarm_api is imported inside the test, not at module top: collection of
    # tests/unit/scripts must not load it (test_collection_stays_cheap.py).
    from swarm_api import judgment

    proc = world.run("--apply", typed="sign")
    assert proc.returncode == 0, proc.stderr
    docs = world.read()
    eng = docs["workspace_settings/eng"]
    keys = (world.key,)
    assert judgment.block_verifies("eng", "top", eng, keys)
    assert judgment.block_verifies("eng", "merge", eng["merge"], keys)
    assert "mac" not in docs["workspace_settings/research"]["code_intelligence"]
    assert world.key not in proc.stderr + proc.stdout
    # No terminal: a run that reached the confirmation would die for want of one.
    again = world.run("--apply")
    assert again.returncode == 0, again.stderr
    assert "nothing to write" in again.stderr
    assert world.read() == docs


def test_swarm_assume_yes_does_not_skip_the_typed_word(world) -> None:
    before = world.read()
    proc = world.run("--apply", extra_env={"SWARM_ASSUME_YES": "1"})
    assert proc.returncode != 0
    assert "ignoring SWARM_ASSUME_YES" in proc.stderr
    assert world.read() == before
    wrong = world.run("--apply", typed="yes")
    assert wrong.returncode != 0
    assert world.read() == before


def test_one_tenant_at_a_time(world) -> None:
    proc = world.run("--tenant", "eng", "--apply", typed="sign")
    assert proc.returncode == 0, proc.stderr
    docs = world.read()
    assert "mac" in docs["workspace_settings/eng"]
    assert "research" not in proc.stderr


@pytest.mark.parametrize("argv", [("--apply", "--dry-run"), ("--tenant", "Eng;x"), ("--what",)])
def test_a_bad_command_line_writes_nothing(world, argv) -> None:
    before = world.read()
    proc = world.run(*argv)
    assert proc.returncode != 0
    assert world.read() == before
