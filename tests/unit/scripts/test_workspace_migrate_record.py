"""`scripts/workspace-migrate-record.sh` writes §3.3's record through
`Workspaces.migrate`, dry run by default (docs/workspaces.md §3.3; W9, #847).

The harness runs the REAL script and the REAL `swarm_api.workspaces` with no
credentials: a `sitecustomize` on PYTHONPATH swaps `google.cloud.firestore.
Client` for tests/unit/control_plane/fakes.FakeFirestore, loaded from and saved
back to a pickle, so what the script's python wrote is read here afterwards.
A fake kubectl answers the one quota read the live path makes, and the fake
gcloud of tests/unit/scripts/fixtures/workspace_world.py (the one the
workspace job's tests run behind) holds the forge slot pair. The typed
confirmation is answered through a pseudo-terminal, because the script refuses
anything else.
"""

from __future__ import annotations

import hashlib
import json
import os
import pickle
import pty
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "workspace-migrate-record.sh"
FAKES_DIR = REPO / "tests" / "unit" / "control_plane"
WORLD_PY = Path(__file__).resolve().parent / "fixtures" / "workspace_world.py"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")

TENANT = "u-bogdan"
PRINCIPAL = "bogdan@saga.xyz"
KEPT_ID = "w-752763"
REQUEST_ID = "6f1c2d3e-0000-4000-8000-000000000001"
PROJECT = "saga-agents-staging"
# The names A6 makes and A9 checks (scripts/lib/workspace-guard.sh expect).
SLOT = f"swarm-tenant-{TENANT}-git-u-" + hashlib.sha256(PRINCIPAL.encode()).hexdigest()[:16]
TWIN = SLOT + "-refresh"
WORKER = f"swarm-agent-worker-{TENANT}@{PROJECT}.iam.gserviceaccount.com"
READER = "roles/secretmanager.secretAccessor"

SITECUSTOMIZE = f"""
import atexit, os, pickle, sys
_world = os.environ.get("SWARM_TEST_FIRESTORE")
if _world:
    sys.path.insert(0, {str(FAKES_DIR)!r})
    import fakes
    from google.cloud import firestore
    _db = fakes.FakeFirestore()
    with open(_world, "rb") as fh:
        _db.docs = pickle.load(fh)
    firestore.Client = lambda *a, **k: _db

    def _save():
        with open(_world, "wb") as fh:
            pickle.dump(_db.docs, fh)

    atexit.register(_save)
"""

FAKE_KUBECTL = """#!/bin/sh
case "$*" in
  "version --client"*) echo "Client Version: v1.31.0" ;;
  "config current-context") echo "gke_saga-agents-staging_us-central1_swarm-autopilot" ;;
  "get resourcequota swarm-tenant-quota -n swarm-tenant-u-bogdan -o json")
    echo '{"spec": {"hard": {"pods": "100", "requests.cpu": "400", "requests.memory": "800Gi"}}}' ;;
  *) echo "unexpected kubectl call: $*" >&2; exit 9 ;;
esac
"""


def _tenant_doc() -> dict:
    return {"tenant_id": TENANT, "kind": "user", "principal": PRINCIPAL,
            "max_active": 80, "capacity_units": 80, "credentials": ["anthropic"],
            "enabled": True}


def _requested() -> dict:
    return {
        "tenant_id": TENANT, "workspace_id": KEPT_ID, "principal": PRINCIPAL,
        "state": "requested", "request_id": REQUEST_ID,
        "requested_at": datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc),
        "requested_via": "console", "decision": None, "history": [],
        "limits": {"max_active": 8, "capacity_units": 8, "quota_pods": 16, "quota_cpu": 64},
        "run": None, "steps": {}, "failure": None, "ready_at": None, "migrated": False,
    }


class World:
    def __init__(self, tmp: Path, docs: dict) -> None:
        self.tmp = tmp
        self.file = tmp / "firestore.pickle"
        self.write(docs)
        site = tmp / "site"
        site.mkdir()
        (site / "sitecustomize.py").write_text(SITECUSTOMIZE)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        self.kubectl = bin_dir / "kubectl"
        self.kubectl.write_text(FAKE_KUBECTL)
        self.kubectl.chmod(0o755)
        gcloud = bin_dir / "gcloud"
        gcloud.write_text(f'#!/bin/sh\nFAKE_TOOL=gcloud exec "{sys.executable}" "{WORLD_PY}" "$@"\n')
        gcloud.chmod(0o755)
        self.cloud = tmp / "cloud.json"
        self.cloud.write_text(json.dumps({"project": PROJECT, "accounts": {}, "secrets": {}, "fail": []}))
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("SWARM_", "CLOUDSDK_", "GOOGLE_", "KUBECONFIG",
                                    "PROJECT_ID", "ENVIRONMENT", "FIRESTORE_"))}
        env.update(
            SWARM_ENV_FILE=str(tmp / "absent.env"),
            SWARM_TEST_FIRESTORE=str(self.file),
            SWARM_KUBECTL=str(self.kubectl),
            FAKE_WORLD=str(self.cloud),
            PATH=os.pathsep.join([str(bin_dir), env.get("PATH", "/usr/bin:/bin")]),
            KUBECONFIG=str(tmp / "kubeconfig"),
            PYTHONPATH=os.pathsep.join(filter(None, [str(site), env.get("PYTHONPATH", "")])),
            PROJECT_ID="saga-agents-staging",
            ENVIRONMENT="dev",
            NO_COLOR="1",
            TMPDIR=str(tmp),
        )
        self.env = env

    def write(self, docs: dict) -> None:
        self.file.write_bytes(pickle.dumps(docs))

    def read(self) -> dict:
        return pickle.loads(self.file.read_bytes())

    def secrets(self) -> dict:
        return json.loads(self.cloud.read_text())["secrets"]

    def edit_cloud(self, change) -> None:
        cloud = json.loads(self.cloud.read_text())
        change(cloud)
        self.cloud.write_text(json.dumps(cloud))

    def changes(self) -> list[list[str]]:
        """gcloud calls that change something."""
        calls = json.loads(self.cloud.read_text()).get("calls", [])
        return [c["argv"] for c in calls
                if any(w in ("create", "add-iam-policy-binding", "set-iam-policy", "delete")
                       for w in c["argv"][:3])]

    def run(self, *args: str, extra_env: dict | None = None, typed: str | None = None,
            ) -> subprocess.CompletedProcess:
        env = {**self.env, **(extra_env or {})}
        command = [str(SCRIPT), *args]
        if typed is None:
            return subprocess.run(command, env=env, cwd=REPO, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=300, check=False)
        # The typed confirmation reads a terminal: give it one.
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
def pending(tmp_path) -> World:
    """The 2026-10-09 state: a console request already exists."""
    return World(tmp_path, {
        f"tenants/{TENANT}": _tenant_doc(),
        f"workspaces/{TENANT}": _requested(),
        f"workspace_ids/{KEPT_ID}": {"tenant_id": TENANT},
    })


QUOTA = ("--quota-pods", "100", "--quota-cpu", "400")


def test_the_script_is_executable_and_starts_with_set_euo_pipefail() -> None:
    assert os.access(SCRIPT, os.X_OK)
    effective = [line for line in SCRIPT.read_text().splitlines()[1:]
                 if line.strip() and not line.lstrip().startswith("#")]
    assert effective[0] == "set -euo pipefail"


def test_the_record_shape_is_not_restated_in_the_script() -> None:
    """The write is Workspaces.migrate; the script names none of its fields."""
    text = SCRIPT.read_text()
    assert "Workspaces(" in text and ".migrate(" in text
    for field in ('"migrated"', '"decision"', '"limits"', '"providers"', "workspace_ids/{"):
        assert field not in text


def test_a_dry_run_prints_before_and_after_and_writes_nothing(pending) -> None:
    before = pending.read()

    proc = pending.run(TENANT, *QUOTA)

    assert proc.returncode == 0, proc.stderr
    assert pending.read() == before
    assert "before (workspaces/u-bogdan)" in proc.stderr and "after:" in proc.stderr
    assert '"state": "requested"' in proc.stderr and '"state": "ready"' in proc.stderr
    assert f"keeping workspace id {KEPT_ID}" in proc.stderr
    assert "dry run: nothing was written" in proc.stderr


def test_apply_updates_the_pending_record_in_place_and_keeps_its_id(pending) -> None:
    proc = pending.run(TENANT, *QUOTA, "--apply", typed=TENANT)

    assert proc.returncode == 0, proc.stderr
    docs = pending.read()
    record = docs[f"workspaces/{TENANT}"]
    assert record["workspace_id"] == KEPT_ID
    assert record["request_id"] == REQUEST_ID
    assert record["state"] == "ready" and record["migrated"] is True
    assert record["providers"] == ["anthropic"]
    assert record["limits"] == {"max_active": 80, "capacity_units": 80,
                                "quota_pods": 100, "quota_cpu": 400}
    assert record["decision"]["by"] == "migration"
    assert record["decision"]["reason"] == "Terraform-era tenant moved by W9"
    assert sorted(k for k in docs if k.startswith("workspace_ids/")) == [f"workspace_ids/{KEPT_ID}"]
    audits = [v for k, v in docs.items() if k.startswith("admin_audit/")]
    assert [(a["action"], a["target_workspace_id"]) for a in audits] == [("migrate", KEPT_ID)]
    assert f"--workspace {KEPT_ID} --mode verify" in proc.stderr


def test_apply_with_no_record_creates_one_with_a_fresh_id(tmp_path) -> None:
    world = World(tmp_path, {f"tenants/{TENANT}": _tenant_doc()})

    proc = world.run(TENANT, *QUOTA, "--apply", typed=TENANT)

    assert proc.returncode == 0, proc.stderr
    docs = world.read()
    record = docs[f"workspaces/{TENANT}"]
    assert record["workspace_id"].startswith("w-") and record["workspace_id"] != KEPT_ID
    assert docs[f"workspace_ids/{record['workspace_id']}"] == {"tenant_id": TENANT}
    assert record["state"] == "ready" and record["migrated"] is True


def test_a_second_apply_is_a_no_op_and_asks_nothing(pending) -> None:
    assert pending.run(TENANT, *QUOTA, "--apply", typed=TENANT).returncode == 0
    after_first = pending.read()

    # No terminal: a run that reached the confirmation would die for want of one.
    proc = pending.run(TENANT, *QUOTA, "--apply")

    assert proc.returncode == 0, proc.stderr
    assert "already ready and migrated" in proc.stderr
    assert pending.read() == after_first


def test_swarm_assume_yes_does_not_skip_the_typed_confirmation(pending) -> None:
    before = pending.read()

    proc = pending.run(TENANT, *QUOTA, "--apply", extra_env={"SWARM_ASSUME_YES": "1"})

    assert proc.returncode != 0
    assert "ignoring SWARM_ASSUME_YES" in proc.stderr
    assert pending.read() == before


def test_a_wrong_typed_answer_writes_nothing(pending) -> None:
    before = pending.read()

    proc = pending.run(TENANT, *QUOTA, "--apply", typed="u-someone")

    assert proc.returncode != 0
    assert "confirmation did not match" in proc.stderr
    assert pending.read() == before


def test_a_record_a_build_may_be_making_is_refused(pending) -> None:
    docs = pending.read()
    docs[f"workspaces/{TENANT}"]["state"] = "approved"
    # A dispatch attempt is what makes an approved record one a build may hold;
    # an approved record never dispatched is migrated (#847, 2026-10-10).
    docs[f"workspaces/{TENANT}"]["dispatch"] = {"attempts": 1}
    pending.write(docs)

    proc = pending.run(TENANT, *QUOTA, "--apply", typed=TENANT)

    assert proc.returncode == 1
    assert "refused" in proc.stderr and "approved" in proc.stderr
    assert pending.read() == docs


def test_without_quota_flags_the_live_resourcequota_is_read(pending) -> None:
    proc = pending.run(TENANT, "--apply", typed=TENANT)

    assert proc.returncode == 0, proc.stderr
    assert "read from swarm-tenant-u-bogdan" in proc.stderr
    limits = pending.read()[f"workspaces/{TENANT}"]["limits"]
    assert (limits["quota_pods"], limits["quota_cpu"]) == (100, 400)


def test_a_cluster_context_that_is_not_the_swarms_is_refused_before_any_read(pending) -> None:
    pending.kubectl.write_text(FAKE_KUBECTL.replace(
        "gke_saga-agents-staging_us-central1_swarm-autopilot",
        "gke_saga-agents-staging_us-central1-a_agents-staging"))
    before = pending.read()

    proc = pending.run(TENANT)

    assert proc.returncode == 1
    assert "not the swarm cluster" in proc.stderr
    assert pending.read() == before


@pytest.mark.parametrize("args", [
    ("eng",),
    ("u-bogdan", "--quota-pods", "100"),
    ("u-bogdan", "--quota-pods", "100", "--quota-cpu", "400m"),
    ("u-bogdan", "--apply", "--dry-run"),
])
def test_bad_arguments_are_refused_before_anything_is_read(pending, args) -> None:
    before = pending.read()

    proc = pending.run(*args)

    assert proc.returncode != 0
    assert pending.read() == before



# ---------------------------------------------------------------------------
# The forge slot pair (owner decision 2026-10-10: the migration creates it)
# ---------------------------------------------------------------------------


def _readers(policy: dict) -> set[str]:
    return {m for b in policy.get("bindings", []) if b["role"] == READER for m in b["members"]}


def test_apply_creates_the_empty_slot_pair_and_binds_the_slot_only(pending) -> None:
    proc = pending.run(TENANT, *QUOTA, "--apply", typed=TENANT)

    assert proc.returncode == 0, proc.stderr
    secrets = pending.secrets()
    assert set(secrets) == {SLOT, TWIN}
    for name in (SLOT, TWIN):
        assert secrets[name]["labels"]["tenant"] == TENANT
        assert secrets[name]["labels"]["managed-by"] == "swarm-api"
    assert secrets[SLOT]["labels"]["provider"] == SLOT.removeprefix(f"swarm-tenant-{TENANT}-")
    assert _readers(secrets[SLOT]["policy"]) == {f"serviceAccount:{WORKER}"}
    assert _readers(secrets[TWIN]["policy"]) == set()
    # Empty: the value arrives when the person connects GitHub.
    assert not any("versions" in c for c in pending.changes())
    assert f"created {SLOT}" in proc.stderr and f"created {TWIN}" in proc.stderr
    assert f"secretAccessor on {SLOT} to {WORKER}" in proc.stderr


def test_a_rerun_with_the_slot_present_changes_nothing(pending) -> None:
    assert pending.run(TENANT, *QUOTA, "--apply", typed=TENANT).returncode == 0
    secrets = pending.secrets()
    made = len(pending.changes())

    proc = pending.run(TENANT, *QUOTA, "--apply")

    assert proc.returncode == 0, proc.stderr
    assert pending.secrets() == secrets
    assert len(pending.changes()) == made
    assert f"{SLOT} is present; kept" in proc.stderr


def test_a_rerun_finishes_a_pair_an_earlier_run_left_incomplete(pending) -> None:
    pending.edit_cloud(lambda c: c["fail"].append(
        {"tool": "gcloud", "words": ["secrets", "add-iam-policy-binding"], "times": 1}))
    first = pending.run(TENANT, *QUOTA, "--apply", typed=TENANT)
    assert first.returncode == 1
    assert "the record is written but the forge slot pair is not complete" in first.stderr
    assert pending.read()[f"workspaces/{TENANT}"]["migrated"] is True

    # No terminal: the record is migrated, so finishing the pair asks nothing.
    proc = pending.run(TENANT, *QUOTA, "--apply")

    assert proc.returncode == 0, proc.stderr
    assert _readers(pending.secrets()[SLOT]["policy"]) == {f"serviceAccount:{WORKER}"}


def test_a_dry_run_prints_the_slot_it_would_make_and_makes_nothing(pending) -> None:
    proc = pending.run(TENANT, *QUOTA)

    assert proc.returncode == 0, proc.stderr
    assert pending.secrets() == {}
    assert pending.changes() == []
    assert f"would create {SLOT}" in proc.stderr and f"would create {TWIN}" in proc.stderr
    assert f"would grant roles/secretmanager.secretAccessor on {SLOT}" in proc.stderr


def test_a_slot_labelled_for_another_tenant_is_not_adopted(pending) -> None:
    pending.edit_cloud(lambda c: c["secrets"].update({SLOT: {
        "labels": {"tenant": "u-someone"}, "policy": {"version": 1}}}))

    proc = pending.run(TENANT, *QUOTA, "--apply", typed=TENANT)

    assert proc.returncode == 1
    assert "labelled for 'u-someone'" in proc.stderr
    assert set(pending.secrets()) == {SLOT}
    assert pending.changes() == []


def test_the_slot_is_made_by_the_one_shared_function_never_a_copy() -> None:
    """A6 and the migration source scripts/lib/forge-slot.sh; neither restates it."""
    lib = REPO / "scripts" / "lib" / "forge-slot.sh"
    register = REPO / "scripts" / "register-tenant.sh"
    assert "gcloud secrets create" in lib.read_text()
    for script in (SCRIPT, register):
        text = script.read_text()
        assert "lib/forge-slot.sh" in text and "forge_slot_ensure_pair" in text
        assert "gcloud secrets create" not in text
    # register-tenant.sh's operator path binds other secrets; the migration none.
    assert "add-iam-policy-binding" not in SCRIPT.read_text()
