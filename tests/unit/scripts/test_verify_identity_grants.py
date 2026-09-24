"""The identity that runs the verification gate holds the two grants it needs.

Owner decisions, 2026-09-24 (recorded in
docs/audits/2026-09-22/race-test-needs-a-write.md):

1. `swarm-verify` passes IAP on the front door. Before this, the load balancer
   answered 403 "Access denied. For user swarm-verify@..." -- IAP knew who the
   caller was and it was not on the list, because `domain:saga.xyz` does not
   cover a `*.iam.gserviceaccount.com` identity. PR #15's end-to-end check and
   every operator script run with SWARM_IMPERSONATE_SA go through that door.

2. `swarm-verify` is a platform admin in dev, so race-test narrows
   `runner:mock` through `PUT /v1/admin/limits/runner/mock` instead of a raw
   Firestore PATCH.

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
  * `admin_users` is compared by swarm-api against the BARE email from the
    verified token (`auth.py`: `email.lower() in admin_users`). A prefixed
    entry there is accepted by terraform and matches nobody.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEV_TFVARS = ROOT / "terraform" / "environments" / "dev" / "dev.tfvars"
VERIFY_TF = ROOT / "terraform" / "infra" / "verify.tf"
LOCALS_TF = ROOT / "terraform" / "infra" / "locals.tf"


def _strip_comments(text: str) -> str:
    # tfvars comments here are `#` to end of line; none of the values these
    # tests read contain a `#`.
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _string_list(text: str, name: str) -> list[str]:
    """The string elements of a top-level `name = [ ... ]` assignment."""
    body = _strip_comments(text)
    m = re.search(rf"^{re.escape(name)}\s*=\s*\[(.*?)\]", body, re.S | re.M)
    assert m, f"{name} is not assigned as a list in {DEV_TFVARS.relative_to(ROOT)}"
    return re.findall(r'"([^"]+)"', m.group(1))


def _string_value(text: str, name: str) -> str:
    m = re.search(rf'^{re.escape(name)}\s*=\s*"([^"]+)"', _strip_comments(text), re.M)
    assert m, f"{name} is not assigned in {DEV_TFVARS.relative_to(ROOT)}"
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
    project = _string_value(DEV_TFVARS.read_text(), "project_id")
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


def test_the_verify_identity_can_pass_iap_on_the_front_door() -> None:
    """Decision 1: IAP admits swarm-verify, as a serviceAccount member."""
    email = _verify_identity()
    members = _string_list(DEV_TFVARS.read_text(), "frontend_iap_members")

    assert f"serviceAccount:{email}" in members, (
        f"frontend_iap_members is {members}; IAP answers 403 'Access denied. For "
        f"user {email}' until serviceAccount:{email} is in it"
    )
    # The coarse grant for people is unchanged: adding the gate must not have
    # replaced the domain.
    assert "domain:saga.xyz" in members, members


def test_the_verify_identity_is_a_platform_admin_in_dev() -> None:
    """Decision 2: ADMIN_USERS names swarm-verify, as a bare email."""
    email = _verify_identity()
    admins = _string_list(DEV_TFVARS.read_text(), "admin_users")

    assert email in admins, (
        f"admin_users is {admins}; PUT /v1/admin/limits/runner/mock answers 403 "
        f"to {email} until it is in it, and race-test cannot narrow the pool"
    )
    prefixed = [a for a in admins if ":" in a]
    assert not prefixed, (
        f"{prefixed} carry an IAM member prefix. swarm-api compares the bare email "
        "from the verified token against this list, so a prefixed entry matches "
        "nobody -- the grant would plan, apply and do nothing."
    )
