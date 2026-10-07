"""terraform/bootstrap keeps its state in the state bucket, and an empty one is refused (#827).

WHY THIS FILE EXISTS
--------------------
Until 2026-10 terraform/bootstrap had only `backend.tf.example`, so its state
was a local terraform.tfstate in one laptop checkout. On 2026-10-07
`scripts/bootstrap.sh` run from a git worktree of main planned "9 to import, 91
to add" against an empty state: the live deployer service account, every WIF
binding and every deployer role. Only the typed "apply" stopped it.

THE PROPERTY
------------
* the root has a gcs backend at prefix "bootstrap", and common.sh's
  TF_BOOTSTRAP_STATE_PREFIX is the same value;
* bootstrap.sh initialises it with the bucket passed as -backend-config;
* with an EMPTY bootstrap state it refuses to plan while swarm-tf-deployer
  exists, while a local terraform.tfstate holding resources is in the checkout,
  and while gcloud cannot say whether the deployer exists. It plans from empty
  only on NOT_FOUND;
* `--migrate-state` refuses without a local state file, refuses over an
  existing remote state, never takes --yes or SWARM_ASSUME_YES as the answer,
  and -- given the typed word on a terminal -- runs `init -migrate-state`
  against the bucket and checks the copy's instance count.

terraform and gcloud are fakes on PATH. Nothing is planned, applied or
migrated, no credentials are used, and the real repository is never written to.
"""

from __future__ import annotations

import json
import os
import pty
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "scripts"

PROJECT = "swarm-test-project"
BUCKET = f"swarm-tfstate-{PROJECT}"
DEPLOYER = f"swarm-tf-deployer@{PROJECT}.iam.gserviceaccount.com"
STATE_URL = f"gs://{BUCKET}/bootstrap/default.tfstate"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("curl") is None,
    reason="bootstrap.sh needs jq and curl",
)

# Records every call, one per line, arguments joined by a unit separator.
# `state list` answers from FAKE_TF_STATE_LIST (a file; absent means "no state
# file", the way terraform answers a backend with no object yet).
FAKE_TERRAFORM = """#!/usr/bin/env bash
set -euo pipefail
{
  first=1
  for a in "$@"; do
    if [[ $first -eq 0 ]]; then printf '\\037'; fi
    printf '%s' "$a"
    first=0
  done
  printf '\\n'
} >> "${FAKE_TF_LOG}"
case "$*" in
  *"state list"*)
    if [[ -f "${FAKE_TF_STATE_LIST}" ]]; then
      cat "${FAKE_TF_STATE_LIST}"
    else
      echo "No state file was found!" >&2
      exit 1
    fi
    ;;
  *"-migrate-state"*)
    # What a successful migration leaves behind: the copy, listable.
    cp "${FAKE_TF_MIGRATED_LIST}" "${FAKE_TF_STATE_LIST}"
    ;;
esac
exit 0
"""

# FAKE_DEPLOYER and FAKE_REMOTE_STATE: present | absent | denied.
FAKE_GCLOUD = f"""#!/usr/bin/env bash
set -euo pipefail
answer() {{
  case "$1" in
    present) echo "found" ;;
    absent)  echo "ERROR: (gcloud) NOT_FOUND: not found: 404" >&2; exit 1 ;;
    *)       echo "ERROR: (gcloud) PERMISSION_DENIED: caller lacks permission" >&2; exit 1 ;;
  esac
}}
case "$*" in
  *"iam service-accounts describe"*) answer "${{FAKE_DEPLOYER}}" ;;
  *"storage objects describe"*)      answer "${{FAKE_REMOTE_STATE}}" ;;
  *versioning_enabled*)              echo "True" ;;
  *"storage buckets describe"*)      echo "{BUCKET}" ;;
  *) : ;;
esac
exit 0
"""

LOCAL_STATE = {
    "version": 4,
    "serial": 118,
    "resources": [
        {"mode": "managed", "type": "google_service_account", "name": "deployer",
         "instances": [{"index_key": 0, "attributes": {}}]},
        {"mode": "managed", "type": "google_project_iam_member", "name": "deployer_roles",
         "instances": [{"index_key": "a", "attributes": {}}, {"index_key": "b", "attributes": {}}]},
    ],
}
LOCAL_ADDRESSES = [
    "google_service_account.deployer[0]",
    'google_project_iam_member.deployer_roles["a"]',
    'google_project_iam_member.deployer_roles["b"]',
]


class Sandbox:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.repo = tmp / "repo"
        self.repo.mkdir()
        shutil.copytree(SCRIPTS, self.repo / "scripts")
        (self.repo / ".env").write_text("")
        self.bootstrap = self.repo / "terraform" / "bootstrap"
        self.bootstrap.mkdir(parents=True)
        shutil.copy(REPO / "terraform" / "bootstrap" / "backend.tf", self.bootstrap / "backend.tf")
        (self.repo / "terraform" / "infra").mkdir(parents=True)
        tfvars = self.repo / "terraform" / "environments" / "dev"
        tfvars.mkdir(parents=True)
        (tfvars / "dev.tfvars").write_text("")

        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "gcloud").write_text(FAKE_GCLOUD)
        (bin_dir / "gcloud").chmod(0o755)
        self.terraform = bin_dir / "terraform-recorder"
        self.terraform.write_text(FAKE_TERRAFORM)
        self.terraform.chmod(0o755)
        self.log = tmp / "terraform-calls"
        self.log.write_text("")
        self.state_list = tmp / "state-list"
        self.migrated_list = tmp / "migrated-list"

        env_file = tmp / "env"
        env_file.write_text(
            f"PROJECT_ID={PROJECT}\nREGION=us-central1\nENVIRONMENT=dev\nFIRESTORE_DATABASE=swarm\n"
        )
        env_file.chmod(0o600)
        self.env = dict(os.environ)
        self.env.pop("SWARM_ASSUME_YES", None)
        self.env.update(
            PATH=f"{bin_dir}{os.pathsep}{self.env['PATH']}",
            SWARM_ENV_FILE=str(env_file),
            SWARM_TERRAFORM=str(self.terraform),
            FAKE_TF_LOG=str(self.log),
            FAKE_TF_STATE_LIST=str(self.state_list),
            FAKE_TF_MIGRATED_LIST=str(self.migrated_list),
            FAKE_DEPLOYER="present",
            FAKE_REMOTE_STATE="absent",
            NO_COLOR="1",
        )

    def remote_state(self, addresses: list[str] | None) -> None:
        """None: the backend has no state object. []: an object with no resources."""
        if addresses is None:
            self.state_list.unlink(missing_ok=True)
        else:
            self.state_list.write_text("".join(f"{a}\n" for a in addresses))

    def local_state(self) -> None:
        (self.bootstrap / "terraform.tfstate").write_text(json.dumps(LOCAL_STATE))

    def run(self, *args: str, stdin_text: str | None = None) -> subprocess.CompletedProcess:
        cmd = [str(self.repo / "scripts" / "bootstrap.sh"), "--skip-prereq", *args]
        if stdin_text is None:
            return subprocess.run(cmd, cwd=self.repo, env=self.env, capture_output=True,
                                  text=True, timeout=180, stdin=subprocess.DEVNULL)
        # A real terminal on stdin, so the typed confirmation's `[[ -t 0 ]]` holds.
        master, slave = pty.openpty()
        try:
            os.write(master, stdin_text.encode())
            return subprocess.run(cmd, cwd=self.repo, env=self.env, capture_output=True,
                                  text=True, timeout=180, stdin=slave)
        finally:
            os.close(slave)
            os.close(master)

    def calls(self) -> list[list[str]]:
        return [line.split("\x1f") for line in self.log.read_text().splitlines() if line]

    def verbs(self) -> list[str]:
        return [next(a for a in c if not a.startswith("-")) for c in self.calls()]


@pytest.fixture
def box(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


def _out(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout + proc.stderr


# --- the backend -------------------------------------------------------------


def test_the_bootstrap_root_has_a_gcs_backend_at_the_prefix_common_sh_names() -> None:
    backend = (REPO / "terraform" / "bootstrap" / "backend.tf").read_text()
    code = "\n".join(line for line in backend.splitlines() if not line.lstrip().startswith("#"))
    assert re.search(r'backend\s+"gcs"\s*\{', code), "terraform/bootstrap has no gcs backend block"
    prefix = re.search(r'prefix\s*=\s*"([^"]+)"', code)
    assert prefix and prefix.group(1) == "bootstrap", code
    assert not re.search(r"^\s*bucket\s*=", code, re.M), (
        "the bucket is passed at init with -backend-config, as for terraform/infra; "
        "a bucket in backend.tf names one project for every checkout"
    )
    assert not (REPO / "terraform" / "bootstrap" / "backend.tf.example").exists()

    common = (SCRIPTS / "lib" / "common.sh").read_text()
    named = re.search(r'^\s*TF_BOOTSTRAP_STATE_PREFIX="([^"]+)"', common, re.M)
    assert named, "common.sh no longer names TF_BOOTSTRAP_STATE_PREFIX"
    assert named.group(1) == prefix.group(1), (
        f"common.sh says {named.group(1)!r}, backend.tf says {prefix.group(1)!r}: "
        "the migration's existence check would look for the wrong object"
    )


def test_bootstrap_init_passes_the_state_bucket(box: Sandbox) -> None:
    box.remote_state(LOCAL_ADDRESSES)
    proc = box.run("--yes")
    assert proc.returncode == 0, _out(proc)
    inits = [c for c in box.calls() if "init" in c and any("bootstrap" in a for a in c)]
    assert inits, f"terraform/bootstrap was never initialised: {box.calls()}"
    assert f"-backend-config=bucket={BUCKET}" in inits[0], inits[0]
    assert "-reconfigure" in inits[0], inits[0]


# --- the empty-state guard -----------------------------------------------------


@pytest.mark.parametrize("empty", [None, []], ids=["no-state-object", "empty-state-object"])
def test_an_empty_state_is_refused_while_the_deployer_exists(box: Sandbox, empty) -> None:
    box.remote_state(empty)
    box.env["FAKE_DEPLOYER"] = "present"
    proc = box.run("--yes")
    out = _out(proc)
    assert proc.returncode != 0, out
    assert "plan" not in box.verbs(), f"planned against an empty state: {box.calls()}"
    assert "apply" not in box.verbs()
    assert DEPLOYER in out, out
    assert "--migrate-state" in out, f"the refusal must say to migrate first:\n{out}"


def test_an_empty_state_is_refused_while_this_checkout_holds_a_local_one(box: Sandbox) -> None:
    box.remote_state(None)
    box.local_state()
    box.env["FAKE_DEPLOYER"] = "absent"
    proc = box.run("--yes")
    out = _out(proc)
    assert proc.returncode != 0, out
    assert "plan" not in box.verbs(), box.calls()
    assert "--migrate-state" in out, out


def test_an_empty_state_is_refused_when_gcloud_cannot_say_whether_the_deployer_exists(box: Sandbox) -> None:
    box.remote_state(None)
    box.env["FAKE_DEPLOYER"] = "denied"
    proc = box.run("--yes")
    assert proc.returncode != 0, _out(proc)
    assert "plan" not in box.verbs(), box.calls()


def test_an_empty_state_plans_on_a_project_never_bootstrapped(box: Sandbox) -> None:
    box.remote_state(None)
    box.env["FAKE_DEPLOYER"] = "absent"
    proc = box.run("--yes")
    assert proc.returncode == 0, _out(proc)
    assert box.verbs().count("plan") == 1, box.calls()


def test_a_populated_state_plans_with_the_deployer_present(box: Sandbox) -> None:
    box.remote_state(LOCAL_ADDRESSES)
    box.env["FAKE_DEPLOYER"] = "present"
    proc = box.run("--yes")
    assert proc.returncode == 0, _out(proc)
    assert box.verbs().count("plan") == 1, box.calls()


def test_a_state_that_cannot_be_listed_is_not_read_as_empty(box: Sandbox) -> None:
    box.terraform.write_text(FAKE_TERRAFORM.replace(
        '  *"state list"*)\n',
        '  *"state list"*)\n    echo "Error: Failed to get existing workspaces: 403" >&2; exit 1\n',
    ))
    box.env["FAKE_DEPLOYER"] = "absent"
    proc = box.run("--yes")
    assert proc.returncode != 0, _out(proc)
    assert "plan" not in box.verbs(), box.calls()


# --- the one-time migration ----------------------------------------------------


def test_migrate_state_refuses_without_a_local_state_file(box: Sandbox) -> None:
    proc = box.run("--migrate-state", stdin_text="migrate\n")
    out = _out(proc)
    assert proc.returncode != 0, out
    assert "terraform.tfstate" in out, out
    assert not [c for c in box.calls() if "-migrate-state" in c], box.calls()


def test_migrate_state_refuses_over_an_existing_remote_state(box: Sandbox) -> None:
    box.local_state()
    box.env["FAKE_REMOTE_STATE"] = "present"
    proc = box.run("--migrate-state", stdin_text="migrate\n")
    assert proc.returncode != 0, _out(proc)
    assert STATE_URL in _out(proc)
    assert not [c for c in box.calls() if "-migrate-state" in c], box.calls()


def test_migrate_state_refuses_when_the_remote_state_cannot_be_checked(box: Sandbox) -> None:
    box.local_state()
    box.env["FAKE_REMOTE_STATE"] = "denied"
    proc = box.run("--migrate-state", stdin_text="migrate\n")
    assert proc.returncode != 0, _out(proc)
    assert not [c for c in box.calls() if "-migrate-state" in c], box.calls()


@pytest.mark.parametrize("how", ["--yes", "env"])
def test_migrate_state_ignores_assume_yes(box: Sandbox, how: str) -> None:
    box.local_state()
    args = ["--migrate-state"]
    if how == "--yes":
        args.append("--yes")
    else:
        box.env["SWARM_ASSUME_YES"] = "1"
    proc = box.run(*args)   # no terminal: the typed confirmation cannot be answered
    out = _out(proc)
    assert proc.returncode != 0, out
    assert "ignoring" in out and "SWARM_ASSUME_YES" in out, out
    assert not [c for c in box.calls() if "-migrate-state" in c], box.calls()


def test_migrate_state_refuses_a_wrong_answer(box: Sandbox) -> None:
    box.local_state()
    proc = box.run("--migrate-state", stdin_text="yes\n")
    assert proc.returncode != 0, _out(proc)
    assert not [c for c in box.calls() if "-migrate-state" in c], box.calls()


def test_migrate_state_moves_the_local_state_into_the_bucket_and_stops(box: Sandbox) -> None:
    box.local_state()
    box.migrated_list.write_text("".join(f"{a}\n" for a in LOCAL_ADDRESSES))
    proc = box.run("--migrate-state", stdin_text="migrate\n")
    out = _out(proc)
    assert proc.returncode == 0, out

    migrations = [c for c in box.calls() if "-migrate-state" in c]
    assert len(migrations) == 1, box.calls()
    assert f"-backend-config=bucket={BUCKET}" in migrations[0], migrations[0]
    assert "plan" not in box.verbs() and "apply" not in box.verbs(), (
        f"--migrate-state must stop after the copy: {box.calls()}"
    )
    backups = list((box.repo / "build").glob("bootstrap-local-*.tfstate"))
    assert len(backups) == 1, "the local state was not backed up before terraform touched it"
    assert json.loads(backups[0].read_text())["serial"] == 118


def test_migrate_state_fails_when_the_copy_does_not_match(box: Sandbox) -> None:
    box.local_state()
    box.migrated_list.write_text(f"{LOCAL_ADDRESSES[0]}\n")
    proc = box.run("--migrate-state", stdin_text="migrate\n")
    out = _out(proc)
    assert proc.returncode != 0, out
    assert "Do NOT plan or apply" in out, out
