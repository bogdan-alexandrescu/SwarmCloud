"""The identity that runs the verification gate holds the two grants it needs.

Owner decisions, 2026-09-24 (recorded in
docs/audits/2026-09-22/race-test-needs-a-write.md):

1. `swarm-verify` passes IAP on the front door. Before this, the load balancer
   answered 403 "Access denied. For user swarm-verify@..." -- IAP knew who the
   caller was and it was not on the list, because `domain:saga.xyz` does not
   cover a `*.iam.gserviceaccount.com` identity. PR #15's end-to-end check and
   every operator script run with SWARM_IMPERSONATE_SA go through that door.

   The list is `frontend_iap_members` in terraform/bootstrap/terraform.tfvars,
   NOT the environment's tfvars. #23 moved it there on 2026-09-24 (the release's
   deployer could not be given an IAP role scoped to our backends), and it is
   applied by the owner rather than by the release. This test first read
   dev.tfvars, and after that move it would have gone on "failing as expected"
   under its strict xfail for the wrong reason -- the variable was no longer in
   the file, not the member missing from the list.

2. race-test narrows `runner:mock` through `PUT /v1/admin/limits/runner/mock`
   instead of a raw Firestore PATCH, so `swarm-verify` needs that one route.

   CORRECTED 2026-09-24 by the owner. The first form of this decision made
   swarm-verify a FULL platform admin (`admin_users`), and review showed that
   one boolean also lets it disable any tenant through
   `PUT /v1/admin/tenants/{id}/limits` and rewrite any tenant's workflow state.
   It is now on `admin_pool_users` instead: swarm-api lets that list call an
   explicit allow-list of admin routes -- the runner ceiling and nothing else
   -- and `admin_users` is back to operators. What the narrow capability can
   and cannot reach is asserted over the whole router in
   tests/unit/control_plane/test_pool_admin_is_narrow.py; this file asserts
   that the deployment puts the gate on the right list.

WHAT IS ASSERTED IS THE IDENTITY, NOT A STRING. The email is DERIVED here the way
terraform derives it -- `account_id` from `google_service_account.verify` and
`project_id` from the environment's tfvars -- so renaming the account or moving
the project fails this test instead of leaving both lists naming an identity
that no longer exists, which is the silent version of the 403 this fixes.

And each list needs its OWN spelling of that identity, which is the mistake
this shape invites:

  * `frontend_iap_members` is IAM: `serviceAccount:<email>`. The module refuses
    anything without a member-type prefix, and `user:` on a service account is
    a different principal that IAP will never match.
  * `admin_pool_users`, like `admin_users`, is compared by swarm-api against
    the BARE email from the verified token (`auth.py`). A prefixed entry there
    is accepted by terraform and matches nobody.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEV_TFVARS = ROOT / "terraform" / "environments" / "dev" / "dev.tfvars"
#: Where the IAP accessor list lives since #23; scripts/lib/common.sh's IAP
#: remedy sends the reader to the same file.
BOOTSTRAP_TFVARS = ROOT / "terraform" / "bootstrap" / "terraform.tfvars"
VERIFY_TF = ROOT / "terraform" / "infra" / "verify.tf"
LOCALS_TF = ROOT / "terraform" / "infra" / "locals.tf"
VARIABLES_TF = ROOT / "terraform" / "infra" / "variables.tf"
API_SETTINGS = ROOT / "apps" / "swarm-api" / "swarm_api" / "settings.py"


def _strip_comments(text: str) -> str:
    # tfvars comments here are `#` to end of line; none of the values these
    # tests read contain a `#`.
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _string_list(path: Path, name: str) -> list[str]:
    """The string elements of a top-level `name = [ ... ]` assignment."""
    body = _strip_comments(path.read_text())
    m = re.search(rf"^{re.escape(name)}\s*=\s*\[(.*?)\]", body, re.S | re.M)
    assert m, f"{name} is not assigned as a list in {path.relative_to(ROOT)}"
    return re.findall(r'"([^"]+)"', m.group(1))


def _string_value(path: Path, name: str) -> str:
    m = re.search(
        rf'^{re.escape(name)}\s*=\s*"([^"]+)"', _strip_comments(path.read_text()), re.M
    )
    assert m, f"{name} is not assigned in {path.relative_to(ROOT)}"
    return m.group(1)


def _verify_identity() -> str:
    """swarm-verify's email, derived exactly as terraform derives it."""
    verify = _strip_comments(VERIFY_TF.read_text())
    m = re.search(
        r'resource\s+"google_service_account"\s+"verify"\s*\{[^}]*?account_id\s*=\s*"([^"]+)"',
        verify,
        re.S,
    )
    assert m, "google_service_account.verify has no literal account_id in verify.tf"
    project = _string_value(DEV_TFVARS, "project_id")
    return f"{m.group(1)}@{project}.iam.gserviceaccount.com"


def test_the_derived_identity_is_the_one_swarm_api_admits() -> None:
    """Guard on the derivation itself: the tenant this identity owns, and the
    ALLOWED_USERS wiring, must describe the same principal the tests below
    look for -- otherwise they would be asserting grants for a stranger."""
    email = _verify_identity()
    assert re.search(
        rf'^\s*principal\s*=\s*"{re.escape(email)}"',
        _strip_comments(DEV_TFVARS.read_text()),
        re.M,
    ), f"no tenant in dev.tfvars has the derived identity {email} as its principal"
    assert re.search(
        r"ALLOWED_USERS\s*=\s*google_service_account\.verify\.email",
        LOCALS_TF.read_text(),
    ), "swarm-api no longer admits google_service_account.verify through ALLOWED_USERS"


#: No xfail any more. This was `xfail(strict=True)` while decision 1 was decided
#: and not yet in the branch. It landed through #23 -- in
#: terraform/bootstrap/terraform.tfvars, which #23 reports applied and read back
#: on both backends -- and the marker came off in the merge of main that brought
#: it here. A strict xfail over a moved variable is the trap this file now
#: avoids: it kept passing because the old file no longer SET the list at all.
def test_the_verify_identity_can_pass_iap_on_the_front_door() -> None:
    """Decision 1: IAP admits swarm-verify, as a serviceAccount member."""
    email = _verify_identity()
    # The bootstrap root grants on the front door of ITS project. If that ever
    # stopped being the project swarm-verify lives in, the member below would
    # be admitted to somebody else's load balancer and still 403 on ours.
    bootstrap_project = _string_value(BOOTSTRAP_TFVARS, "project_id")
    dev_project = _string_value(DEV_TFVARS, "project_id")
    assert bootstrap_project == dev_project, (
        f"terraform/bootstrap grants IAP in {bootstrap_project}, but swarm-verify "
        f"and dev's front door are in {dev_project}"
    )
    members = _string_list(BOOTSTRAP_TFVARS, "frontend_iap_members")

    assert f"serviceAccount:{email}" in members, (
        f"frontend_iap_members is {members}; IAP answers 403 'Access denied. For "
        f"user {email}' until serviceAccount:{email} is in it"
    )
    # The coarse grant for people is unchanged: adding the gate must not have
    # replaced the domain.
    assert "domain:saga.xyz" in members, members


def test_the_verify_identity_holds_the_narrow_pool_capability_in_dev() -> None:
    """Decision 2, as corrected: ADMIN_POOL_USERS names swarm-verify, bare."""
    email = _verify_identity()
    pool_admins = _string_list(DEV_TFVARS, "admin_pool_users")

    assert email in pool_admins, (
        f"admin_pool_users is {pool_admins}; PUT /v1/admin/limits/runner/mock "
        f"answers 403 to {email} until it is in it, and race-test cannot narrow "
        "the pool"
    )
    prefixed = [a for a in pool_admins if ":" in a]
    assert not prefixed, (
        f"{prefixed} carry an IAM member prefix. swarm-api compares the bare email "
        "from the verified token against this list, so a prefixed entry matches "
        "nobody -- the grant would plan, apply and do nothing."
    )


def test_the_verify_identity_is_not_a_platform_admin_in_dev() -> None:
    """The owner's correction, 2026-09-24: NOT a full admin.

    Every entry in `admin_users` opens every /v1/admin route, tenant disable
    included. Matched on the address anywhere in an entry, so a prefixed or
    differently-cased copy of the gate cannot slip back in unnoticed.
    """
    email = _verify_identity()
    admins = _string_list(DEV_TFVARS, "admin_users")

    reinstated = [a for a in admins if email.lower() in a.lower()]
    assert not reinstated, (
        f"admin_users names the verification gate again ({reinstated}). That makes "
        "it a full platform admin -- it could disable any tenant -- which the "
        "owner reversed on 2026-09-24. The gate's one admin route comes from "
        "admin_pool_users."
    )


def _live_lines(path: Path) -> list[str]:
    return [line for line in _strip_comments(path.read_text()).splitlines() if line.strip()]


def test_admin_pool_users_reaches_swarm_api_under_the_name_it_reads() -> None:
    """The tfvars list is only a grant if it arrives as the variable swarm-api
    reads. The name is DERIVED from settings.py rather than restated here, so a
    rename on either side fails this test instead of the list silently reaching
    nobody -- the WAKE_TOPIC/DISPATCH_TOPIC class of defect
    (scripts/lib/check-env-parity.sh catches a read nobody sets; this catches a
    set that carries the wrong list).
    """
    m = re.search(
        r"admin_pool_users\s*=\s*_csv\(\s*\"([A-Z0-9_]+)\"\s*\)", API_SETTINGS.read_text()
    )
    assert m, "ApiSettings.from_env does not read admin_pool_users from the environment"
    env_name = m.group(1)

    setters = [
        line for line in _live_lines(LOCALS_TF)
        if re.match(rf"^\s*{re.escape(env_name)}\s*=", line)
    ]
    assert setters, f"terraform/infra/locals.tf never sets {env_name}"
    for line in setters:
        assert "var.admin_pool_users" in line, (
            f"{env_name} is set from something other than var.admin_pool_users: {line.strip()}"
        )
        # Derived the way ADMIN_USERS is -- from the environment's tfvars AS IS
        # -- and never by appending google_service_account.verify here, which
        # would hand the capability to the gate in every environment at once.
        assert "google_service_account" not in line, (
            f"{env_name} appends a service account in locals.tf: {line.strip()}"
        )
    assert re.search(
        r'^variable\s+"admin_pool_users"', VARIABLES_TF.read_text(), re.M
    ), "terraform/infra/variables.tf declares no admin_pool_users"
