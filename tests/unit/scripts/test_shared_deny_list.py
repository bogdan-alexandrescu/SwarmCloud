"""`is_shared_resource` knows every resource the other teams hold in saga-agents-staging.

`SHARED_DENY_LIST` in scripts/lib/common.sh is the one list `destroy.sh`,
`plan-guard.sh`, the kubectl guard and the workspace call guard judge by. It said
"Verified against the live project on 2026-09-15". The operator read the live
project again on 2026-10-21 at 09:45Z (service accounts, buckets, Cloud Run
services; read-only) and found twelve resources that are not this platform's and
were missing from it (epic #1822, lane DENY-LIST-REFRESH). A name missing from
the list is a name every guard above lets through.

`qa-console@` is NOT among them: it is this platform's own
(terraform/bootstrap/qa_identity.tf), and a platform `swarm-` name must still
answer false, or the guards would refuse this platform's own resources.

Offline: sources common.sh in bash and calls the function, nothing else.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
COMMON = REPO / "scripts" / "lib" / "common.sh"
PROJECT = "saga-agents-staging"

#: Read on 2026-10-21 at 09:45Z by the operator, not derived from common.sh:
#: deriving it from the list under test would make the test agree with any list.
FOUND_2026_10_21 = [
    # service accounts
    f"ai-studio-api@{PROJECT}.iam.gserviceaccount.com",
    f"ai-studio-deployer@{PROJECT}.iam.gserviceaccount.com",
    f"ai-studio-runner@{PROJECT}.iam.gserviceaccount.com",
    f"oracle-api@{PROJECT}.iam.gserviceaccount.com",
    f"oracle-ticker@{PROJECT}.iam.gserviceaccount.com",
    f"oracle-web@{PROJECT}.iam.gserviceaccount.com",
    f"promptlab-api@{PROJECT}.iam.gserviceaccount.com",
    # buckets
    "ai-studio-avatars-staging",
    "promptlab-avatars-staging",
    f"{PROJECT}_cloudbuild",
    # Cloud Run services
    "oracle-api",
    "oracle-web",
]


def _is_shared(name: str) -> bool:
    proc = subprocess.run(
        ["bash", "-c", 'source "$1"; is_shared_resource "$2"', "_", str(COMMON), name],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode in (0, 1), proc.stderr
    return proc.returncode == 0


def test_the_list_found_on_2026_10_21_is_the_twelve_the_operator_read() -> None:
    # MUTATION: drop an entry from FOUND_2026_10_21 and the parametrised case
    # below silently stops checking it; this count fails instead.
    assert len(FOUND_2026_10_21) == len(set(FOUND_2026_10_21)) == 12


@pytest.mark.parametrize("name", FOUND_2026_10_21)
def test_a_resource_found_on_2026_10_21_is_shared(name: str) -> None:
    # MUTATION: delete this name from SHARED_DENY_LIST in common.sh and this
    # case fails for it alone.
    assert _is_shared(name), f"{name} belongs to another team but is_shared_resource says it is ours"


@pytest.mark.parametrize("name", [
    f"swarm-api@{PROJECT}.iam.gserviceaccount.com",
    "swarm-api",
    f"qa-console@{PROJECT}.iam.gserviceaccount.com",
])
def test_a_platform_name_is_not_shared(name: str) -> None:
    # MUTATION: make is_shared_resource `return 0` unconditionally, or add a
    # platform name (a swarm- account, qa-console@) to SHARED_DENY_LIST, and
    # this fails.
    assert not _is_shared(name), f"{name} is this platform's own but is_shared_resource refuses it"
