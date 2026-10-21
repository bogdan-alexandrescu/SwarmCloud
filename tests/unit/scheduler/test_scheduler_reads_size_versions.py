"""The scheduler's account can read every collection of database `swarm`,
including `runner_size_versions` (SIZES-SCHED-2, docs/design/runner-sizes.md
§4.3, §6; part of #1689).

Admission now reads `control/runner_sizes` and
`runner_size_versions/<version>` on every pass. Today
terraform/modules/iam/bindings.tf grants `roles/datastore.user` to each of
`local.firestore_accounts`, which is `keys(local.platform_accounts)`, under a
condition scoped to the DATABASE, so this passes at once. It exists so that a
later narrowing of that grant -- the scheduler dropped from the accounts, a
narrower role, or a condition naming collections under the database -- fails a
test instead of failing admission in production with a PermissionDenied.

Parsed from the HCL text, offline: no Terraform, no credentials.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
IAM = REPO / "terraform/modules/iam"
BINDINGS = IAM / "bindings.tf"
SERVICE_ACCOUNTS = IAM / "service_accounts.tf"
ACCOUNT_IDS = REPO / "terraform/modules/service_account_ids/main.tf"

SCHEDULER = "swarm-scheduler"


def _block(text: str, opener: str) -> str:
    """The brace-balanced block that starts at the first `opener`."""
    start = text.index(opener)
    depth = 0
    for i in range(text.index("{", start), len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise AssertionError(f"unbalanced block after {opener!r}")


def _code(path: Path) -> str:
    """The file with `#` comments removed, so a comment cannot satisfy a check."""
    return "\n".join(line.split("#", 1)[0] for line in path.read_text().splitlines())


def test_the_scheduler_is_a_platform_account():
    platform = _block(_code(ACCOUNT_IDS), "platform = {")
    # MUTATION: remove "swarm-scheduler" from service_account_ids' `platform`
    # map and the scheduler has no Firestore grant at all.
    assert re.search(rf'^\s*"{SCHEDULER}"\s*=\s*{{', platform, re.M), platform
    # MUTATION: point `platform_accounts` anywhere but that map and the
    # scheduler's membership above proves nothing about the grant.
    assert re.search(r"platform_accounts\s*=\s*module\.service_account_ids\.platform\b",
                     _code(SERVICE_ACCOUNTS))


def test_every_platform_account_holds_the_firestore_grant():
    code = _code(BINDINGS)
    # MUTATION: build `firestore_accounts` from a hand-written list, or
    # filter the scheduler out of it, and the grant below misses it.
    assert re.search(r"firestore_accounts\s*=\s*keys\(local\.platform_accounts\)\s*$", code, re.M)
    grant = _block(code, 'resource "google_project_iam_member" "firestore"')
    assert re.search(r"for_each\s*=\s*toset\(local\.firestore_accounts\)", grant), grant
    # MUTATION: narrow the role to one that cannot read documents (or to a
    # custom role scoped to `control`) and admission's version read is denied.
    assert re.search(r'role\s*=\s*"roles/datastore\.user"', grant), grant


def test_the_firestore_condition_names_the_database_not_a_collection():
    code = _code(BINDINGS)
    condition = _block(code, "firestore_condition = ")
    (expression,) = re.findall(r'expression\s*=\s*"((?:[^"\\]|\\.)*)"', condition)
    # MUTATION: scope the condition to `.../databases/swarm/documents/control`
    # (or any collection) and `runner_size_versions` is outside it.
    assert "databases/${var.firestore_database}" in expression, expression
    assert re.search(r'databases/\$\{var\.firestore_database\}\\?"?$', expression), expression
    for narrower in ("/documents", "collection", "runner_size", "control"):
        assert narrower not in expression, (narrower, expression)
