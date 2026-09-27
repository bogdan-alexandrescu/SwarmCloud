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

The second property, added later and asserted by the last two tests:

    "Is this grant already there?" is a question about a (role, member) PAIR, and on
    the artifact bucket it cannot be answered any other way. That bucket is one
    bucket with one policy for every tenant -- terraform/modules/tenancy binds
    swarmBucketMetadataReader and a conditioned storage.objectUser for
    `for_each = var.tenants` -- so a substring test over the policy document is
    answered by whichever tenant happens to be in it. Both checks here used to be
    exactly that, and both stopped being able to return false after the first tenant
    was provisioned. A tenant this script registers -- one that is NOT in
    var.tenants, which is the only reason to run the script -- was therefore told
    two bindings existed that nobody had made, and its worker cannot mount the
    bucket without them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from swarm_common.identity import tenant_id_for_group

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
  # The bucket policy the script reads to decide whether a grant already
  # exists. FAKE_BUCKET_POLICY is the whole point of this fake: it lets a test
  # say "these OTHER tenants are already bound here" and see what the script
  # then concludes about the tenant it is registering.
  *"storage buckets get-iam-policy"*) cat "${FAKE_BUCKET_POLICY:-/dev/null}" ;;
  *"get-iam-policy"*)                 : ;;   # no stale conditional binding
  *)                                  : ;;
esac
exit 0
"""

# The script only reads Firestore through the REST API here, and in --dry-run it
# writes nothing, so an empty document is the whole contract. stdin is drained
# because common.sh pipes the auth config in and the caller runs with pipefail.
# The real fs_request invokes curl as `-o <file> -w '%{http_code}'`, so stdout
# is the STATUS and the body goes to the file. This fake used to print the body
# on stdout and ignore both flags, which made `status` the literal string `{}`.
#
# That was invisible until 9c639af ("An HTTP error is not an empty result")
# taught fs_request to check the status instead of assuming success, at which
# point every test in this file errored in its fixture -- and stayed that way
# for 95 commits, because `make test` did not run tests/integration. The
# production change was right; the fixture was lying about what curl does.
#
# fs_database_exists calls curl WITHOUT -o and reads the body from stdout, so
# both shapes have to work.
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
# Drain any -K config on stdin so the caller's pipe does not block.
cat >/dev/null 2>&1 || true
body='{}'
if [[ -n "${out}" ]]; then
  printf '%s' "${body}" > "${out}"
  [[ "${want_status}" -eq 1 ]] && printf '200'
else
  printf '%s' "${body}"
fi
"""


def _register(
    tmp: Path,
    group: str,
    bucket_policy: str | None = None,
    extra: tuple[str, ...] = (),
) -> str:
    """Drive the real script under --dry-run and return everything it printed.

    `bucket_policy` is the JSON the fake gcloud hands back for
    `storage buckets get-iam-policy`; None means the policy is empty, which is a
    bucket nobody is bound on yet. `extra` is appended to the command line.
    """
    proc = _run_register(tmp, group, bucket_policy, extra)
    transcript = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"register-tenant.sh --dry-run failed:\n{transcript}"
    return transcript


def _run_register(
    tmp: Path,
    group: str,
    bucket_policy: str | None = None,
    extra: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """`_register` without the success assertion, for the runs that must refuse."""
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

    policy_file = tmp / "bucket-policy.json"
    policy_file.write_text(bucket_policy or "")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["SWARM_ENV_FILE"] = str(env_file)
    env["NO_COLOR"] = "1"
    env["FAKE_BUCKET_POLICY"] = str(policy_file)

    return subprocess.run(
        [str(SCRIPT), "--group", group, "--dry-run", "--skip-k8s", *extra],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=180,
    )


@pytest.fixture(scope="module")
def dry_run_transcript(tmp_path_factory) -> str:
    return _register(tmp_path_factory.mktemp("register-tenant"), "eng@saga.xyz")


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


def _gsa(group: str) -> str:
    """The identity a worker for `group` runs as, spelled as an IAM member.

    Derived through swarm_common.identity rather than by slugging the address
    here: the script derives it that way too, and the one thing this test must
    not do is agree with the script about a tenant id the API would resolve
    differently.
    """
    tenant = tenant_id_for_group(group)
    return f"serviceAccount:swarm-agent-worker-{tenant}@{PROJECT}.iam.gserviceaccount.com"


def _shared_bucket_policy(*groups: str) -> str:
    """The artifact bucket's policy with `groups` already bound, as terraform leaves it.

    terraform/modules/tenancy/main.tf binds BOTH of these for
    `for_each = var.tenants` -- one shared bucket, one policy, every managed
    tenant in it.
    """
    metadata_role = f"projects/{PROJECT}/roles/swarmBucketMetadataReader"
    bindings: list[dict[str, object]] = [
        {"role": metadata_role, "members": [_gsa(g) for g in groups]},
    ]
    for group in groups:
        tenant = tenant_id_for_group(group)
        bindings.append({
            "role": "roles/storage.objectUser",
            "members": [_gsa(group)],
            "condition": {
                "title": "tenant_prefix_only",
                "expression": (
                    f"resource.name.startsWith('projects/_/buckets/{BUCKET}"
                    f"/objects/tenants/{tenant}/')"
                ),
            },
        })
    return json.dumps(
        {"kind": "storage#policy", "version": 3, "etag": "CAE=", "bindings": bindings}
    )


def test_another_tenants_bucket_bindings_do_not_count_as_this_tenants(tmp_path) -> None:
    """Register a tenant terraform does not manage, into a policy full of ones it does.

    The reproduction, end to end: `make infra` in dev leaves eng, smoke and
    u-bogdan bound on the single shared artifact bucket, and
    `make register-tenant GROUP=research@saga.xyz` then registers a tenant that
    is in nobody's var.tenants. Both "already granted?" checks used to be a grep
    over that whole policy -- one for the role id, one for the member -- so both
    matched somebody else and the script printed success for two bindings it
    never made. The worker that follows has no storage.buckets.get, so Cloud
    Storage FUSE cannot mount the bucket at all, and no objectUser, so it could
    not read its own artifacts if it did.
    """
    transcript = _register(
        tmp_path,
        "research@saga.xyz",
        _shared_bucket_policy("eng@saga.xyz", "smoke@saga.xyz", "u-bogdan@saga.xyz"),
    )
    member = _gsa("research@saga.xyz")

    metadata = _would_run(transcript, "roles/swarmBucketMetadataReader")
    assert any(f"--member {member}" in line for line in metadata), (
        "register-tenant.sh did not grant swarmBucketMetadataReader to the tenant it "
        "was registering. Another tenant's binding on the SAME custom role, on the "
        "SAME shared bucket, was read as this tenant's:\n" + transcript
    )

    objects = [
        line for line in _would_run(transcript, "roles/storage.objectUser")
        if f"--member {member}" in line
    ]
    assert objects, (
        "register-tenant.sh did not grant storage.objectUser to the tenant it was "
        "registering:\n" + transcript
    )

    assert "already granted" not in transcript, (
        "the script reported a grant as already present while registering a tenant "
        "that holds none of them:\n" + transcript
    )


def test_this_tenants_own_bindings_are_still_recognised(tmp_path) -> None:
    """The fix must not turn the idempotency check off.

    Re-running the script is advertised as harmless, and it stops being harmless
    the moment it re-adds a conditioned binding: gcloud reads this policy at
    version 1, the conditions make it version 3, and the write is refused --
    which aborts the run before the tenant document is written.
    """
    transcript = _register(
        tmp_path, "eng@saga.xyz", _shared_bucket_policy("eng@saga.xyz", "smoke@saga.xyz")
    )
    member = _gsa("eng@saga.xyz")

    assert not [
        line for line in _would_run(transcript, "add-iam-policy-binding")
        if "gs://" in line and f"--member {member}" in line
    ], "a binding this tenant already holds was added again:\n" + transcript
    assert "storage access already granted" in transcript, transcript
    assert "swarmBucketMetadataReader already granted" in transcript, transcript


# ---------------------------------------------------------------------------
# The tenant pool's ceiling
# ---------------------------------------------------------------------------
#
# `max_active` and `capacity_units` bound ONE count: the units the tenant's
# running work holds, where every task costs at least one. So the API writes
# the tenant pool's hard limit as min(max_active, capacity_units) --
# swarm_api/store.py, in `ensure_tenant` and on every `set_tenant_limits` --
# and the console's Tenants screen prints that minimum as the ceiling
# admission enforces (AH-12 in #86).
#
# This script wrote CAPACITY_UNITS. At its own defaults (20 and 40) that is a
# pool admitting 40 units for a tenant whose record, the API and the console
# all say is capped at 20. Found in review of #161.


@pytest.mark.parametrize(
    ("max_active", "capacity_units", "ceiling"),
    [
        ("20", "40", 20),  # the defaults: capacity_units is the larger
        ("10", "4", 4),  # capacity_units is the smaller
        ("7", "7", 7),
    ],
)
def test_the_tenant_pool_is_capped_at_the_smaller_limit(
    tmp_path, max_active: str, capacity_units: str, ceiling: int
) -> None:
    transcript = _register(
        tmp_path,
        "eng@saga.xyz",
        extra=("--max-active", max_active, "--capacity-units", capacity_units),
    )
    pool = f"pools/tenant:{tenant_id_for_group('eng@saga.xyz')}"
    assert f"would write {pool} with hard_limit={ceiling}" in transcript, (
        f"max_active={max_active} capacity_units={capacity_units} must write the "
        f"tenant pool at min(...) = {ceiling}, as swarm_api/store.py does:\n{transcript}"
    )


@pytest.mark.parametrize("value", ["forty", "-5", "1e3", "4 + 4"])
def test_a_limit_that_is_not_a_whole_number_is_refused(tmp_path, value: str) -> None:
    """The minimum is computed in shell arithmetic, which evaluates what it is given.

    `$(( ... ))` reads a bare word as a variable name and `4 + 4` as a sum, so
    an unchecked limit would become a ceiling nobody typed. jq's `--argjson`
    used to be the only check, and it accepted a negative number.
    """
    proc = _run_register(tmp_path, "eng@saga.xyz", extra=("--max-active", value))
    transcript = proc.stdout + proc.stderr
    assert proc.returncode != 0, f"--max-active {value!r} was accepted:\n{transcript}"
    assert "--max-active" in transcript, transcript
