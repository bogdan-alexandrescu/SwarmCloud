"""`iam_policy_binds_member` in scripts/lib/common.sh, against real policies.

The predicate decides whether scripts/register-tenant.sh ADDS an IAM binding or
reports it as already present. It used to be two greps over the policy document,
and on the one policy it is asked about -- the SHARED artifact bucket, which
carries every tenant's bindings -- neither grep could return false:

    grep -q "${BUCKET_METADATA_ROLE_ID}"       # true once ANY tenant holds it
    grep -q "serviceAccount:${GSA_EMAIL}"      # true once THIS member holds any

terraform/modules/tenancy/main.tf binds swarmBucketMetadataReader on that bucket
for `for_each = var.tenants`, so in dev the tenants eng, smoke and u-bogdan are
all in the policy before this script ever runs. Registering a tenant that is not
in var.tenants -- which is exactly what this script is for -- matched one of
their bindings and printed "already granted" for a grant nobody had made. The
worker then cannot read the bucket's own metadata, which Cloud Storage FUSE
needs to mount at all, and the transcript says the opposite.

Nothing here touches the network: the policies are fixtures on disk and the
predicate is sourced out of common.sh and called directly.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
COMMON = REPO / "scripts" / "lib" / "common.sh"

PROJECT = "swarm-test-project"
METADATA_ROLE = f"projects/{PROJECT}/roles/swarmBucketMetadataReader"
OBJECT_ROLE = "roles/storage.objectUser"

# The tenant this script is registering, and the tenants terraform already put
# in the bucket policy. `research` is deliberately NOT one of them.
THIS_TENANT = f"serviceAccount:swarm-agent-worker-research@{PROJECT}.iam.gserviceaccount.com"
OTHER_TENANTS = [
    f"serviceAccount:swarm-agent-worker-{t}@{PROJECT}.iam.gserviceaccount.com"
    for t in ("eng", "smoke", "u-bogdan")
]

pytestmark = pytest.mark.skipif(
    not COMMON.exists() or shutil.which("jq") is None or shutil.which("bash") is None,
    reason="common.sh, jq and bash are all required",
)


def _condition(tenant: str) -> dict[str, str]:
    """The prefix condition terraform attaches to each tenant's object grant."""
    return {
        "title": "tenant_prefix_only",
        "expression": (
            "resource.name.startsWith('projects/_/buckets/b/objects/"
            f"tenants/{tenant}/')"
        ),
    }


def _shared_bucket_policy() -> dict[str, object]:
    """The artifact bucket's policy with three OTHER tenants bound, as in dev."""
    bindings: list[dict[str, object]] = [
        {"role": METADATA_ROLE, "members": list(OTHER_TENANTS)},
    ]
    for member, tenant in zip(OTHER_TENANTS, ("eng", "smoke", "u-bogdan")):
        bindings.append(
            {"role": OBJECT_ROLE, "members": [member], "condition": _condition(tenant)}
        )
    return {"kind": "storage#policy", "version": 3, "etag": "CAE=", "bindings": bindings}


def _binds(tmp_path: Path, policy: object, role: str, member: str) -> bool:
    """Source common.sh and ask the real predicate.

    `policy` is written as JSON unless it is already a str, which lets a test
    hand over a malformed document on purpose.
    """
    policy_file = tmp_path / "policy.json"
    if isinstance(policy, str):
        policy_file.write_text(policy)
    else:
        policy_file.write_text(json.dumps(policy))

    # common.sh sources an env file as shell and refuses one that is
    # group-writable, so give it a private one rather than whatever is in the
    # checkout. No value in it reaches this predicate; load_env just has to run.
    env_file = tmp_path / "env"
    env_file.write_text(f"PROJECT_ID={PROJECT}\nENVIRONMENT=dev\n")
    env_file.chmod(0o600)

    env = dict(os.environ)
    env["SWARM_ENV_FILE"] = str(env_file)
    env["NO_COLOR"] = "1"

    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{COMMON}"; iam_policy_binds_member "$1" "$2" "$3"',
            "bash",
            str(policy_file),
            role,
            member,
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # Anything other than "bound" / "not bound" is a broken predicate, not a
    # policy answer: jq missing, common.sh failing to source, a syntax error.
    assert proc.returncode in (0, 1, 5), (
        f"iam_policy_binds_member exited {proc.returncode}\n{proc.stdout}{proc.stderr}"
    )
    return proc.returncode == 0


def test_another_tenants_binding_on_the_same_role_is_not_this_tenants(tmp_path) -> None:
    """The reproduction: eng/smoke/u-bogdan hold the role, research does not.

    This is the whole finding. `grep -q swarmBucketMetadataReader` over this same
    policy is true, so register-tenant.sh skipped the grant and said it existed.
    """
    policy = _shared_bucket_policy()
    assert _binds(tmp_path, policy, METADATA_ROLE, OTHER_TENANTS[0]), (
        "eng IS bound to the metadata role in this fixture; the predicate must see it"
    )
    assert not _binds(tmp_path, policy, METADATA_ROLE, THIS_TENANT), (
        "research is bound to nothing in this policy, but the predicate reported "
        "the metadata-role grant as present -- which is how the grant gets skipped"
    )


def test_the_member_holding_a_different_role_is_not_this_grant(tmp_path) -> None:
    """The other half of the same shape, on the object grant.

    A tenant already carrying the metadata role -- a re-run that got that far and
    no further -- matched `grep -q "serviceAccount:${GSA_EMAIL}"` and had its
    storage.objectUser binding skipped: a worker that can mount the bucket and
    not read the artifacts it writes.
    """
    policy = {
        "bindings": [{"role": METADATA_ROLE, "members": [THIS_TENANT]}],
    }
    assert _binds(tmp_path, policy, METADATA_ROLE, THIS_TENANT)
    assert not _binds(tmp_path, policy, OBJECT_ROLE, THIS_TENANT), (
        "the member is in the policy under a DIFFERENT role; its object grant is "
        "still missing"
    )


def test_a_role_prefix_is_not_the_role(tmp_path) -> None:
    """Role ids are compared whole.

    CUSTOM_ROLE_SUFFIX exists so two environments can coexist in one project, so
    `swarmBucketMetadataReader` and `swarmBucketMetadataReader_dev` are both real
    role ids here and one is a prefix of the other. A substring test grants the
    second by finding the first.
    """
    policy = {
        "bindings": [
            {"role": f"{METADATA_ROLE}_dev", "members": [THIS_TENANT]},
        ],
    }
    assert _binds(tmp_path, policy, f"{METADATA_ROLE}_dev", THIS_TENANT)
    assert not _binds(tmp_path, policy, METADATA_ROLE, THIS_TENANT)


def test_an_unreadable_policy_answers_not_bound(tmp_path) -> None:
    """Every failure mode has to fall towards adding the binding again.

    An expired credential, a bucket in another project, a gcloud that printed an
    error: the file is empty or is not a policy. Adding a binding that already
    exists is idempotent; skipping one that does not is the silent missing grant
    this predicate exists to stop.
    """
    assert not _binds(tmp_path, "", METADATA_ROLE, THIS_TENANT), "empty policy"
    assert not _binds(tmp_path, "ERROR: (gcloud) PERMISSION_DENIED", METADATA_ROLE, THIS_TENANT), (
        "a gcloud error message is not a policy"
    )
    assert not _binds(tmp_path, [], METADATA_ROLE, THIS_TENANT), "a JSON array is not a policy"
    assert not _binds(tmp_path, {}, METADATA_ROLE, THIS_TENANT), "a policy with no bindings"
    assert not _binds(tmp_path, {"bindings": []}, METADATA_ROLE, THIS_TENANT), "no bindings"


def test_a_missing_policy_file_answers_not_bound(tmp_path) -> None:
    """The file may never have been created -- same fallback, no crash."""
    env_file = tmp_path / "env"
    env_file.write_text(f"PROJECT_ID={PROJECT}\n")
    env_file.chmod(0o600)
    env = dict(os.environ)
    env["SWARM_ENV_FILE"] = str(env_file)
    env["NO_COLOR"] = "1"

    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{COMMON}"; iam_policy_binds_member "$1" "$2" "$3"',
            "bash",
            str(tmp_path / "does-not-exist.json"),
            METADATA_ROLE,
            THIS_TENANT,
        ],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode != 0, "a missing policy file must not read as 'already granted'"
    assert proc.returncode < 126, (
        f"the predicate crashed rather than answering:\n{proc.stdout}{proc.stderr}"
    )
