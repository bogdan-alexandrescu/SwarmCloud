"""The roleAdmin route (#79) is recorded as closed AND applied, not pending.

WHY THIS EXISTS. The owner's decision of 2026-09-25 on #79 took
roles/iam.roleAdmin off the CI deployer and moved every custom role into
owner-applied terraform/bootstrap. The owner's bootstrap apply of 2026-09-28
23:21Z destroyed the live binding, and on 2026-09-29 the deployer held no
roleAdmin (docs/runbooks/custom-roles-to-bootstrap.md, "What actually
happened"). Three records still said the route was open until that apply:
route 2 of docs/ci.md's log-reading table, route 2 of the comment in
terraform/bootstrap/verify_logs.tf, and the sentence under that table listing
2 among the routes not yet applied. A reader at 3am would believe CI can still
widen a custom role. These tests pin each record to the applied state.

The tfvars "NOT CLOSED BY IT" bullet is held too, both to the past tense and
to the issue's own requirement: a hasOnly condition stops a direct grant, not
one made through a custom-role update. That sentence is why roleAdmin had to
go, and it must survive the correction.

Two controls keep the assertions honest. Route 3's own "Open until" sentence
in verify_logs.tf is still found, so the bounding of route 2's item cannot
silently match nothing. And variables.tf still refuses roleAdmin by name, so
this record is never kept "closed" over a reverted validation.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CI = REPO / "docs" / "ci.md"
VERIFY_LOGS = REPO / "terraform" / "bootstrap" / "verify_logs.tf"
TFVARS = REPO / "terraform" / "bootstrap" / "terraform.tfvars"
VARIABLES = REPO / "terraform" / "bootstrap" / "variables.tf"
RUNBOOK = REPO / "docs" / "runbooks" / "custom-roles-to-bootstrap.md"

APPLIED = "2026-09-28"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _between(text: str, start: str, end: str) -> str:
    begin = text.index(start)
    return text[begin: text.index(end, begin + len(start))]


def _flat(text: str) -> str:
    """A comment block with its `#` prefixes and line breaks collapsed."""
    lines = [line.lstrip().lstrip("#").strip() for line in text.splitlines()]
    return " ".join(line for line in lines if line)


def _ci_route_row(number: int, starts: str) -> str:
    prefix = f"| {number} | {starts}"
    rows = [line for line in _text(CI).splitlines() if line.startswith(prefix)]
    assert len(rows) == 1, f"expected one route-{number} row in docs/ci.md, found {len(rows)}"
    return rows[0]


def test_ci_route_2_row_records_the_apply():
    row = _ci_route_row(2, "`roles/iam.roleAdmin`")
    assert "open until the owner's bootstrap apply destroys the live binding" not in row
    assert APPLIED in row
    assert "runbooks/custom-roles-to-bootstrap.md" in row


def test_ci_unapplied_routes_sentence_does_not_list_route_2():
    text = _text(CI)
    assert "The same held for 2, 3, 5 and 6 until each is applied." not in text
    sentence = _between(text, "What bounds routes 1 and 4 today", "Closing 1 means")
    assert "The same held for 3, 5 and 6 until each is applied" in sentence
    assert APPLIED in sentence


def test_verify_logs_route_2_records_the_apply():
    text = _text(VERIFY_LOGS)
    item = _flat(_between(text, "#   2. roles/iam.roleAdmin (UNSCOPABLE)", "#   3."))
    assert "Open until the owner's bootstrap apply destroys the live binding" not in item
    assert APPLIED in item


def test_verify_logs_route_3_bounding_control():
    """Route 3's own sentence is still there, so route 2's item is bounded."""
    text = _text(VERIFY_LOGS)
    item = _flat(_between(text, "#   3. roles/iam.serviceAccountAdmin (UNSCOPABLE)", "#   4."))
    assert "Open until the owner's bootstrap apply destroys the project-wide binding" in item


def test_tfvars_roleadmin_bullet_is_past_tense_and_keeps_the_reason():
    text = _text(TFVARS)
    block = _between(text, "# NOT CLOSED BY IT", "# APPLY, in this exact order")
    bullet = _flat(_between(block, "#   * roles/iam.roleAdmin", "#   * hasOnly limits"))
    assert "BYPASSES THIS CONDITION while it is on the deployer" not in bullet
    assert "swarmSecretProvisioner today" not in bullet
    assert "bypassed this condition while it was on the deployer" in bullet
    assert "#79" in bullet
    assert APPLIED in bullet
    assert "stops a direct grant, not one made through a custom-role update" in bullet


def test_runbook_release_after_apply_is_recorded():
    section = _between(_text(RUNBOOK), "## What actually happened (dates)", "## Why")
    assert "still pending as of this PR" not in section
    assert "36514377555" in section


def test_variables_still_refuse_roleadmin():
    """Validation control: the record is never 'closed' over a reverted guard."""
    assert '!contains(var.deployer_roles, "roles/iam.roleAdmin")' in _text(VARIABLES)
