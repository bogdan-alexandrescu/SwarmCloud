"""`scripts/register-tenant.sh` refuses a worker account somebody else made first.

#334 security review, 2026-09-29. terraform/modules/tenancy ADOPTS an existing
`swarm-agent-worker-<tenant>` (create_ignore_already_exists), because this
script creates that account before the release that adds the tenant, and the
owner grants the release deployer serviceAccountAdmin on it in between. Adoption
is also a way in: whoever made the account first chose its IAM policy and may
hold a key to it, and the platform would then give it the tenant's provider
keys, artifacts and Firestore access.

So when the account ALREADY EXISTED, step 2b reads it and refuses to go on if

  * its IAM policy has any (role, member) pair beyond the ones this platform
    makes -- the deployer's serviceAccountAdmin and serviceAccountUser, the
    scheduler's and reconciler's serviceAccountUser, and workloadIdentityUser
    for the tenant namespace's two Kubernetes service accounts -- or any
    conditioned binding; or
  * it has a user-managed key.

The script is driven end to end with a fake gcloud and curl under --dry-run,
as in test_register_tenant_grants.py. The control runs show the refusal is
about the account's contents, not about it existing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "register-tenant.sh"

PROJECT = "swarm-test-project"
BUCKET = "swarm-test-artifacts"
GSA = f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com"
DEPLOYER = f"serviceAccount:swarm-tf-deployer@{PROJECT}.iam.gserviceaccount.com"

pytestmark = pytest.mark.skipif(
    not SCRIPT.exists() or shutil.which("jq") is None,
    reason="register-tenant.sh and jq are both required",
)

FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
args="$*"
case "${args}" in
  *"auth print-access-token"*)        echo "fake-access-token" ;;
  *"firestore databases describe"*)   echo "projects/x/databases/swarm" ;;
  *"identity groups describe"*)       echo "groups/1" ;;
  *"iam service-accounts describe"*)
    [[ -z "${FAKE_SA_MISSING:-}" ]] || exit 1
    echo "sa@example.iam.gserviceaccount.com" ;;
  *"iam service-accounts get-iam-policy"*) cat "${FAKE_SA_POLICY}" ;;
  *"iam service-accounts keys list"*)      cat "${FAKE_SA_KEYS}" ;;
  *"iam roles describe"*)             echo "roles/custom" ;;
  *"storage buckets describe"*)       echo "swarm-test-artifacts" ;;
  *) : ;;
esac
exit 0
"""

# Same contract as test_register_tenant_grants.py's fake: with `-o` the body goes
# to the file and stdout is the status; without it, stdout is the body.
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

EXPECTED_POLICY = {
    "bindings": [
        {"role": "roles/iam.serviceAccountAdmin", "members": [DEPLOYER]},
        {
            "role": "roles/iam.serviceAccountUser",
            "members": [
                DEPLOYER,
                f"serviceAccount:swarm-scheduler@{PROJECT}.iam.gserviceaccount.com",
                f"serviceAccount:swarm-reconciler@{PROJECT}.iam.gserviceaccount.com",
            ],
        },
        {
            "role": "roles/iam.workloadIdentityUser",
            "members": [
                f"serviceAccount:{PROJECT}.svc.id.goog[swarm-tenant-eng/swarm-agent-worker]",
                f"serviceAccount:{PROJECT}.svc.id.goog[swarm-tenant-eng/swarm-worker]",
            ],
        },
    ]
}


def _run(tmp: Path, *, policy: dict | None, keys: str = "", missing: bool = False) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    for name, body in (("gcloud", FAKE_GCLOUD), ("curl", FAKE_CURL)):
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

    policy_file = tmp / "sa-policy.json"
    policy_file.write_text("" if policy is None else json.dumps(policy))
    keys_file = tmp / "sa-keys.txt"
    keys_file.write_text(keys)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["SWARM_ENV_FILE"] = str(env_file)
    env["NO_COLOR"] = "1"
    env["FAKE_SA_POLICY"] = str(policy_file)
    env["FAKE_SA_KEYS"] = str(keys_file)
    if missing:
        env["FAKE_SA_MISSING"] = "1"

    return subprocess.run(
        [str(SCRIPT), "--group", "eng@saga.xyz", "--dry-run", "--skip-k8s"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=180,
    )


def _out(proc: subprocess.CompletedProcess[str]) -> str:
    return proc.stdout + proc.stderr


def test_an_existing_account_with_a_stranger_in_its_policy_is_refused(tmp_path: Path) -> None:
    policy = json.loads(json.dumps(EXPECTED_POLICY))
    policy["bindings"].append(
        {"role": "roles/iam.serviceAccountTokenCreator", "members": ["user:mallory@example.com"]}
    )
    proc = _run(tmp_path, policy=policy)
    out = _out(proc)
    assert proc.returncode != 0, f"a squatted account was accepted:\n{out}"
    assert "mallory@example.com" in out, f"the refusal does not name the unexpected member:\n{out}"
    # It stops at 2b: nothing is granted to the account afterwards.
    assert "Control plane" not in out, f"the script went on past the refusal:\n{out}"


def test_an_expected_member_under_an_unexpected_role_is_refused(tmp_path: Path) -> None:
    policy = json.loads(json.dumps(EXPECTED_POLICY))
    policy["bindings"].append({"role": "roles/iam.serviceAccountTokenCreator", "members": [DEPLOYER]})
    proc = _run(tmp_path, policy=policy)
    assert proc.returncode != 0, f"tokenCreator for the deployer was accepted:\n{_out(proc)}"


def test_a_conditioned_binding_is_refused(tmp_path: Path) -> None:
    policy = json.loads(json.dumps(EXPECTED_POLICY))
    policy["bindings"][0]["condition"] = {"title": "t", "expression": "true"}
    proc = _run(tmp_path, policy=policy)
    assert proc.returncode != 0, f"a conditioned binding was accepted:\n{_out(proc)}"


def test_an_existing_account_with_a_user_managed_key_is_refused(tmp_path: Path) -> None:
    proc = _run(
        tmp_path,
        policy=EXPECTED_POLICY,
        keys=f"projects/{PROJECT}/serviceAccounts/{GSA}/keys/0123456789abcdef\n",
    )
    out = _out(proc)
    assert proc.returncode != 0, f"an account with a user-managed key was accepted:\n{out}"
    assert "key" in out.lower(), out
    assert "Control plane" not in out, f"the script went on past the refusal:\n{out}"


def test_an_existing_account_holding_only_what_the_platform_grants_is_accepted(tmp_path: Path) -> None:
    """The control: the refusal is about the account's contents, not its existence."""
    proc = _run(tmp_path, policy=EXPECTED_POLICY)
    out = _out(proc)
    assert proc.returncode == 0, f"an account holding only the platform's own grants was refused:\n{out}"
    assert "Control plane" in out, out


def test_an_existing_account_with_an_empty_policy_is_accepted(tmp_path: Path) -> None:
    proc = _run(tmp_path, policy=None)
    assert proc.returncode == 0, _out(proc)


def test_a_new_account_is_not_inspected(tmp_path: Path) -> None:
    """Created by this run, it has no history to distrust -- and in --dry-run it does not exist."""
    policy = json.loads(json.dumps(EXPECTED_POLICY))
    policy["bindings"].append({"role": "roles/owner", "members": ["user:mallory@example.com"]})
    proc = _run(tmp_path, policy=policy, missing=True)
    assert proc.returncode == 0, _out(proc)
