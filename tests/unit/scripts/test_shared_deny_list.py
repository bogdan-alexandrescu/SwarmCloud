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

`oracle-api@` WAS among them and is not any more: it is also a principal of
this platform (the service tenant u-or-9a0792, a front-door IAP member, in
ALLOWED_USERS), and BGD decided on 2026-10-27 to remove it (#1835). The Oracle
team's Cloud Run service named `oracle-api` is a different resource and stays.

Offline: sources common.sh in bash and calls the function, nothing else.
"""

from __future__ import annotations

import re
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


#: The service account removed on 2026-10-27 (#1835): a SwarmCloud principal.
ORACLE_API_ACCOUNT = f"oracle-api@{PROJECT}.iam.gserviceaccount.com"

#: SHARED_DENY_LIST as it must read after #1835, written out by hand in
#: common.sh's order: the 2026-10-26 list less the oracle-api service account
#: and nothing else. Not derived from common.sh, for the reason above.
EXPECTED_AFTER_1835 = [
    "agents-staging",
    "agents-staging-vpc",
    "agents-staging-subnet",
    "gke-agents-staging-88280d02-pe-subnet",
    "default",
    "saga-agents-crawled-media-staging",
    "saga-agents-files-staging",
    "saga-agents-terraform-state-staging",
    "ai-studio-avatars-staging",
    "promptlab-avatars-staging",
    f"{PROJECT}_cloudbuild",
    f"api-service@{PROJECT}.iam.gserviceaccount.com",
    f"publisher@{PROJECT}.iam.gserviceaccount.com",
    "209012342332-compute@developer.gserviceaccount.com",
    f"promptlab-deployer@{PROJECT}.iam.gserviceaccount.com",
    f"promptlab-runner@{PROJECT}.iam.gserviceaccount.com",
    f"aipipeline@{PROJECT}.iam.gserviceaccount.com",
    f"saga-storage-ro@{PROJECT}.iam.gserviceaccount.com",
    f"saga-storage-rw@{PROJECT}.iam.gserviceaccount.com",
    f"external-secrets@{PROJECT}.iam.gserviceaccount.com",
    f"staging-gke-nodes@{PROJECT}.iam.gserviceaccount.com",
    f"crawler@{PROJECT}.iam.gserviceaccount.com",
    f"tournament-digest@{PROJECT}.iam.gserviceaccount.com",
    f"ai-studio-api@{PROJECT}.iam.gserviceaccount.com",
    f"ai-studio-deployer@{PROJECT}.iam.gserviceaccount.com",
    f"ai-studio-runner@{PROJECT}.iam.gserviceaccount.com",
    f"oracle-ticker@{PROJECT}.iam.gserviceaccount.com",
    f"oracle-web@{PROJECT}.iam.gserviceaccount.com",
    f"promptlab-api@{PROJECT}.iam.gserviceaccount.com",
    "oracle-api",
    "oracle-web",
]


def _deny_list_from_common_sh() -> list[str]:
    block = re.search(r"^SHARED_DENY_LIST=\(\n(.*?)^\)", COMMON.read_text(), re.MULTILINE | re.DOTALL)
    assert block, "scripts/lib/common.sh no longer declares SHARED_DENY_LIST=( ... )"
    return re.findall(r'^\s*"([^"]+)"', block.group(1), re.MULTILINE)


def test_the_list_found_on_2026_10_21_is_the_eleven_still_protected() -> None:
    # MUTATION: drop an entry from FOUND_2026_10_21 and the parametrised case
    # below silently stops checking it; this count fails instead. Twelve were
    # read; oracle-api@ left on 2026-10-27 (#1835).
    assert len(FOUND_2026_10_21) == len(set(FOUND_2026_10_21)) == 11
    assert ORACLE_API_ACCOUNT not in FOUND_2026_10_21


def test_the_deny_list_is_exactly_the_old_list_less_the_oracle_api_account() -> None:
    # Removing oracle-api@ must drop nothing else: every other team's resource
    # stays protected, and nothing is added in passing.
    # MUTATION: delete any other entry from SHARED_DENY_LIST (the Cloud Run
    # service "oracle-api" included), or put oracle-api@ back, and this fails.
    assert _deny_list_from_common_sh() == EXPECTED_AFTER_1835
    assert ORACLE_API_ACCOUNT not in EXPECTED_AFTER_1835 and len(EXPECTED_AFTER_1835) == 31


@pytest.mark.parametrize("name", EXPECTED_AFTER_1835)
def test_every_other_entry_is_still_shared(name: str) -> None:
    # MUTATION: break is_shared_resource for one entry and this fails for it alone.
    assert _is_shared(name), f"{name} belongs to another team but is_shared_resource says it is ours"


def test_the_oracle_api_account_is_a_platform_principal_not_a_shared_resource() -> None:
    # BGD's decision of 2026-10-27 (#1835): the account is the service tenant
    # u-or-9a0792's principal, so register-tenant.sh's S1 must not refuse it.
    # MUTATION: re-add oracle-api@ to SHARED_DENY_LIST and this fails.
    assert not _is_shared(ORACLE_API_ACCOUNT)


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
