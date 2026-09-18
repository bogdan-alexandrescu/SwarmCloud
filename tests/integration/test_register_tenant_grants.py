"""What `scripts/register-tenant.sh` actually grants a tenant worker.

The script is driven end to end with a fake `gcloud` and a fake `curl` on PATH and
`--dry-run`, so the assertions are about the commands it really builds rather than
about text in the file. Nothing is created and no credentials are used.

The property under test, and why production breaks without it:

    Firestore does not evaluate IAM Conditions on the DATA PLANE. Conditions govern
    administrative operations -- creating a database, indexes, backups -- and nothing
    else, so attaching one to a worker's Firestore role does not narrow document
    access, it removes it. Verified live on 2026-09-16: every identity carrying
    `resource.name.endsWith('/databases/swarm')` reported

        {"status":"not-ready","detail":"firestore unavailable: PermissionDenied"}

    terraform/modules/tenancy already knows this -- `scope_firestore_to_database`
    defaults to false -- so a conditioned binding here also made the two provisioning
    paths grant different authority for the same tenant. A tenant registered by this
    script would have had a worker that could not read its own task, its own lease or
    its own attempt: every task of that tenant dead on its first control-plane read,
    with an error that looks like a missing role rather than a present condition.

The GCS assertions are here to stop the fix being over-applied: the object-prefix
condition IS load-bearing (Cloud Storage evaluates conditions on the data plane) and
removing it would expose every other tenant's artifacts.
"""

from __future__ import annotations

import os
import re
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


FAKE_GCLOUD = r"""#!/usr/bin/env bash
# Enough gcloud to let register-tenant.sh reach its IAM section offline.
# Every "describe" answers yes so no branch is skipped for the wrong reason.
set -euo pipefail
args="$*"
case "${args}" in
  *"auth print-access-token"*)        echo "fake-access-token" ;;
  *"firestore databases describe"*)   echo "projects/x/databases/swarm" ;;
  *"identity groups describe"*)       echo "groups/1" ;;
  *"iam service-accounts describe"*)  echo "sa@example.iam.gserviceaccount.com" ;;
  *"iam roles describe"*)             echo "roles/custom" ;;
  *"storage buckets describe"*)       echo "swarm-test-artifacts" ;;
  *"get-iam-policy"*)                 : ;;   # no stale conditional binding
  *)                                  : ;;
esac
exit 0
"""

# The script only reads Firestore through the REST API here, and in --dry-run it
# writes nothing, so an empty document is the whole contract. stdin is drained
# because common.sh pipes the auth config in and the caller runs with pipefail.
FAKE_CURL = r"""#!/usr/bin/env bash
set -euo pipefail
cat >/dev/null 2>&1 || true
printf '{}'
"""


@pytest.fixture(scope="module")
def dry_run_transcript(tmp_path_factory) -> str:
    tmp = tmp_path_factory.mktemp("register-tenant")

    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    for name, body in (("gcloud", FAKE_GCLOUD), ("curl", FAKE_CURL)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)

    # common.sh refuses to source a group- or world-writable env file, and it is
    # right to: it sources it as shell.
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
    env["NO_COLOR"] = "1"

    proc = subprocess.run(
        [str(SCRIPT), "--group", "eng@saga.xyz", "--dry-run", "--skip-k8s"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=180,
    )
    transcript = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"register-tenant.sh --dry-run failed:\n{transcript}"
    return transcript


def _would_run(transcript: str, needle: str) -> list[str]:
    """The `would run:` lines of a --dry-run transcript that mention `needle`."""
    return [
        line.strip()
        for line in transcript.splitlines()
        if "would run:" in line and needle in line
    ]


def test_the_firestore_grant_carries_no_iam_condition(dry_run_transcript: str) -> None:
    """A conditioned Firestore binding denies the data plane instead of scoping it.

    Without this, a tenant registered by the script gets a worker whose every
    Firestore read returns PermissionDenied -- it cannot load the task it was
    dispatched for -- while the operator sees a binding that reads like a boundary.
    """
    bindings = _would_run(dry_run_transcript, "projects add-iam-policy-binding")
    firestore = [b for b in bindings if "swarmTenantWorkerFirestore" in b]
    assert firestore, f"no Firestore role binding in the transcript:\n{dry_run_transcript}"

    for binding in firestore:
        assert "expression=" not in binding, (
            "the Firestore binding carries an IAM condition. Firestore ignores "
            "conditions on the data plane, so this DENIES document access rather "
            f"than scoping it:\n  {binding}"
        )
        assert "/databases/" not in binding, (
            f"the Firestore binding still tries to scope to a database:\n  {binding}"
        )
        # Not the same as omitting the flag: with a conditional binding already in
        # the policy, gcloud asks which one is meant, and --quiet turns that into
        # a failure.
        assert re.search(r"--condition\s+None\b", binding), (
            f"the Firestore binding must say `--condition None` explicitly:\n  {binding}"
        )


def test_the_role_granted_is_the_narrowed_custom_role(dry_run_transcript: str) -> None:
    """roles/datastore.user would add entities.delete and entities.list.

    Firestore IAM cannot scope below the database, so those two apply to every other
    tenant's documents: enumerate every prompt and repository URL, or delete the
    `pools/*` documents the whole platform admits against.
    """
    bindings = _would_run(dry_run_transcript, "projects add-iam-policy-binding")
    assert not any("roles/datastore.user" in b for b in bindings), (
        "a tenant worker was granted roles/datastore.user:\n"
        + "\n".join(bindings)
    )
    assert any(
        f"--role projects/{PROJECT}/roles/swarmTenantWorkerFirestore" in b
        for b in bindings
    ), "the narrowed custom role is no longer granted:\n" + "\n".join(bindings)


def test_the_gcs_grant_keeps_its_prefix_condition(dry_run_transcript: str) -> None:
    """Cloud Storage DOES evaluate conditions, so this one is the artifact boundary.

    Both clauses matter. The first covers get/create/delete on an object path; the
    second covers LIST, which carries no object name at all -- without
    `objectListPrefix` a worker can enumerate every tenant's object names even
    though it cannot open them.
    """
    bindings = _would_run(dry_run_transcript, "storage buckets add-iam-policy-binding")
    objects = [b for b in bindings if "roles/storage.objectUser" in b]
    assert objects, "the tenant's object grant disappeared:\n" + dry_run_transcript

    # The expression moved into a file: gcloud parses --condition as
    # comma-separated key=value pairs, and this expression contains a comma
    # inside api.getAttribute(..., ''), so gcloud split it mid-expression and
    # refused the fragment. The script prints the condition it is applying --
    # which an operator should see anyway, since that condition IS the
    # isolation -- so the assertions look at the transcript rather than at the
    # command line.
    for binding in objects:
        assert "--condition-from-file" in binding, (
            "the prefix condition must be passed by file; passing it inline is "
            f"silently truncated by gcloud at the first comma:\n  {binding}"
        )
        assert "roles/storage.objectAdmin" not in binding, (
            "objectAdmin adds storage.objects.setIamPolicy, which lets a compromised "
            f"worker share its own objects with anyone:\n  {binding}"
        )

    assert "objects/tenants/eng/" in dry_run_transcript, (
        "the tenant's object prefix is not in the condition:\n" + dry_run_transcript
    )
    assert "listing prefix tenants/eng/" in dry_run_transcript, (
        "the LIST clause is gone; a worker could enumerate every tenant's object "
        "names even though it could not open them:\n" + dry_run_transcript
    )
