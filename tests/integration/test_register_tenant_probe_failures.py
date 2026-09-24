"""`scripts/register-tenant.sh` must not call a failed lookup an absence.

The swallowed-stderr sweep (docs/audits/2026-09-18/13-swallowed-stderr-sweep.md)
fanned fourteen files out to fourteen agents on 2026-09-19. Thirteen landed.
The fourteenth was this script -- its agent was reclaimed three times and never
ran -- so its three findings were still open at b0fff1b, five days later:

  * `iam roles describe ... >/dev/null 2>&1` for BOTH custom roles, so a denied
    `iam.roles.get` or a dead session printed "custom role ... does not exist;
    run 'make infra'" -- sending an operator to a terraform apply against a
    shared project to fix a permission;
  * the artifact bucket's IAM policy read with `2>/dev/null`, so a denied read
    silently became "nothing is bound here" and the reason was lost;
  * `kubectl get --raw=/readyz >/dev/null 2>&1`, so a swarm context whose API
    server could not be reached (the master allowlist, a dropped connection)
    printed "not connected to the swarm cluster" and sent the operator to
    configure-kubectl.sh, which cannot fix any of those.

Each case below makes one probe fail the way gcloud or kubectl really fails and
asserts two things: the real reason reaches the operator, and the absence
message does not. A third test keeps the fix from over-reaching -- a role that
genuinely is not there must still say so.

Driven the same way as test_register_tenant_grants.py: the real script, a fake
`gcloud`, `curl` and `kubectl` on PATH, `--dry-run`, nothing created.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "register-tenant.sh"

PROJECT = "swarm-test-project"
BUCKET = "swarm-test-artifacts"

pytestmark = pytest.mark.skipif(
    not SCRIPT.exists() or shutil.which("jq") is None,
    reason="register-tenant.sh and jq are both required",
)

# gcloud's own wording for a denied read. "(or it may not exist)" is gcloud's
# hedge, and it is exactly why this is not a NOT_FOUND: the caller was refused
# before anything was looked up.
DENIED = (
    "ERROR: (gcloud.{cmd}) PERMISSION_DENIED: Permission '{perm}' denied on "
    "resource (or it may not exist)."
)

FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
args="$*"
fail() { printf '%s\n' "$1" >&2; exit 1; }
case "${args}" in
  *"auth print-access-token"*)        echo "fake-access-token" ;;
  *"firestore databases describe"*)   echo "projects/x/databases/swarm" ;;
  *"identity groups describe"*)       echo "groups/1" ;;
  *"iam service-accounts describe"*)  echo "sa@example.iam.gserviceaccount.com" ;;
  *"iam roles describe"*swarmTenantWorkerFirestore*)
    [[ -z "${FAKE_FIRESTORE_ROLE_ERR:-}" ]] || fail "${FAKE_FIRESTORE_ROLE_ERR}"
    echo "roles/custom" ;;
  *"iam roles describe"*swarmBucketMetadataReader*)
    [[ -z "${FAKE_METADATA_ROLE_ERR:-}" ]] || fail "${FAKE_METADATA_ROLE_ERR}"
    echo "roles/custom" ;;
  *"storage buckets describe"*)       echo "swarm-test-artifacts" ;;
  *"storage buckets get-iam-policy"*)
    [[ -z "${FAKE_BUCKET_POLICY_ERR:-}" ]] || fail "${FAKE_BUCKET_POLICY_ERR}"
    echo '{"bindings":[]}' ;;
  *) : ;;
esac
exit 0
"""

# Same contract as the fake in test_register_tenant_grants.py: with `-o` the
# body goes to the file and stdout is the status; without it, stdout is the body.
FAKE_CURL = r"""#!/usr/bin/env bash
set -euo pipefail
out=""
want_status=0
prev=""
for arg in "$@"; do
  case "${prev}" in
    -o) out="${arg}" ;;
    -w) [[ "${arg}" == *http_code* ]] && want_status=1 ;;
  esac
  prev="${arg}"
done
cat >/dev/null 2>&1 || true
body='{}'
if [[ -n "${out}" ]]; then
  printf '%s' "${body}" > "${out}"
  [[ "${want_status}" -eq 1 ]] && printf '200'
else
  printf '%s' "${body}"
fi
"""

# A kubectl new enough for kubectl_bin, pointed at the swarm's own context,
# whose API server cannot be reached. The error text is kubectl's own.
FAKE_KUBECTL = r"""#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  *"version --client"*)       echo "Client Version: v1.36.3" ;;
  *"config current-context"*) echo "swarm-dev" ;;
  *"get --raw=/readyz"*)
    echo "Unable to connect to the server: dial tcp 34.9.8.7:443: i/o timeout" >&2
    exit 1 ;;
  *) : ;;
esac
"""


def _register(tmp: Path, *, skip_k8s: bool = True, **fake_env: str) -> subprocess.CompletedProcess:
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    for name, body in (("gcloud", FAKE_GCLOUD), ("curl", FAKE_CURL), ("kubectl", FAKE_KUBECTL)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)

    env_file = tmp / "env"
    env_file.write_text(
        f"PROJECT_ID={PROJECT}\n"
        "REGION=us-central1\n"
        "ENVIRONMENT=dev\n"
        "FIRESTORE_DATABASE=swarm\n"
        f"ARTIFACT_BUCKET={BUCKET}\n"
    )
    env_file.chmod(0o600)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["SWARM_ENV_FILE"] = str(env_file)
    env["SWARM_KUBECTL"] = str(bin_dir / "kubectl")
    env["KUBECONFIG"] = str(tmp / "kubeconfig")
    env["NO_COLOR"] = "1"
    env.update(fake_env)

    args = [str(SCRIPT), "--group", "eng@saga.xyz", "--dry-run"]
    if skip_k8s:
        args.append("--skip-k8s")
    return subprocess.run(args, cwd=REPO, env=env, capture_output=True, text=True, timeout=180)


def _transcript(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout + proc.stderr


def test_a_denied_firestore_role_lookup_is_not_reported_as_a_missing_role(tmp_path) -> None:
    """The one that ends the run. It must end it with the right sentence.

    Stopping is correct -- without the narrowed role the only alternative is
    roles/datastore.user, which hands this tenant entities.delete over every
    other tenant's documents. What was wrong is the reason it gave.
    """
    err = DENIED.format(cmd="iam.roles.describe", perm="iam.roles.get")
    proc = _register(tmp_path, FAKE_FIRESTORE_ROLE_ERR=err)
    out = _transcript(proc)

    assert proc.returncode != 0, f"the script carried on without its Firestore role:\n{out}"
    assert "PERMISSION_DENIED" in out, (
        "gcloud's actual answer never reached the operator; they are left to guess "
        f"why the lookup failed:\n{out}"
    )
    assert "does not exist" not in out, (
        "a DENIED lookup was reported as a missing role, which sends the operator to "
        f"`make infra` to fix a permission:\n{out}"
    )


def test_a_role_that_really_is_missing_still_says_so(tmp_path) -> None:
    """The fix must not turn every lookup failure into 'could not tell'.

    A NOT_FOUND is an answer. It is the one case `make infra` does fix.
    """
    err = (
        "ERROR: (gcloud.iam.roles.describe) NOT_FOUND: The role named "
        f"projects/{PROJECT}/roles/swarmTenantWorkerFirestore was not found."
    )
    proc = _register(tmp_path, FAKE_FIRESTORE_ROLE_ERR=err)
    out = _transcript(proc)

    assert proc.returncode != 0, out
    assert "does not exist" in out, out
    assert "make infra" in out, out


def test_a_denied_metadata_role_lookup_is_not_reported_as_a_missing_role(tmp_path) -> None:
    """Warn-and-continue, as before -- but with the reason, and without the advice.

    Without storage.buckets.get the worker cannot mount the bucket, so this
    warning is the only thing standing between the operator and a tenant whose
    every task dies at mount time. It has to say why.
    """
    err = DENIED.format(cmd="iam.roles.describe", perm="iam.roles.get")
    proc = _register(tmp_path, FAKE_METADATA_ROLE_ERR=err)
    out = _transcript(proc)

    assert proc.returncode == 0, out
    assert "PERMISSION_DENIED" in out, (
        f"the reason the metadata role could not be looked up was discarded:\n{out}"
    )
    assert "does not exist yet" not in out, (
        f"a DENIED lookup was reported as a role that does not exist:\n{out}"
    )


def test_an_unreadable_bucket_policy_says_so_before_granting(tmp_path) -> None:
    """A denied policy read used to become an empty policy in silence.

    Granting anyway is still the right move -- the grant either lands or fails
    out loud -- but "nothing is bound here" was never established, and the
    operator must be told that the check did not happen and why.
    """
    err = DENIED.format(
        cmd="storage.buckets.get-iam-policy", perm="storage.buckets.getIamPolicy"
    )
    proc = _register(tmp_path, FAKE_BUCKET_POLICY_ERR=err)
    out = _transcript(proc)

    assert proc.returncode == 0, out
    assert "storage.buckets.getIamPolicy" in out, (
        f"a denied read of the bucket policy was swallowed:\n{out}"
    )


def test_an_unreachable_swarm_cluster_is_not_reported_as_the_wrong_context(tmp_path) -> None:
    """The context IS the swarm cluster; only the API server did not answer.

    "not connected to the swarm cluster" plus "run configure-kubectl.sh" is the
    diagnosis for a wrong context. For an allowlist miss or a dropped
    connection, configure-kubectl.sh rewrites a kubeconfig that was already
    right, and the operator sees the same message again.
    """
    proc = _register(tmp_path, skip_k8s=False)
    out = _transcript(proc)

    assert "i/o timeout" in out, (
        f"kubectl's own error was discarded, so the operator cannot tell which failure this is:\n{out}"
    )
    assert "not connected to the swarm cluster" not in out, (
        f"a reachable-context, unreachable-server failure was reported as a wrong context:\n{out}"
    )
