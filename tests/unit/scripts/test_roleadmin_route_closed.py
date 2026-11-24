"""The roleAdmin route (archived issue 79) is recorded as closed AND applied, not pending, in the terraform.

WHY THIS EXISTS. The owner's decision of 2026-09-25 on archived issue 79 took
roles/iam.roleAdmin off the CI deployer and moved every custom role into
owner-applied terraform/bootstrap. The owner's bootstrap apply of 2026-09-28
23:21Z destroyed the live binding, and on 2026-09-29 the deployer held no
roleAdmin (docs/runbooks/custom-roles-to-bootstrap.md, "What actually
happened"). The comment in terraform/bootstrap/verify_logs.tf still said route 2 was open
until that apply. These tests pin it to the applied state.

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
VERIFY_LOGS = REPO / "terraform" / "bootstrap" / "verify_logs.tf"
#: A deployment's real bootstrap terraform.tfvars is private (the state bucket's config/ prefix); a placeholder deployment's stands in (tests/fixtures/tfvars/).
TFVARS = REPO / "tests" / "fixtures" / "tfvars" / "bootstrap.tfvars.fixture"
VARIABLES = REPO / "terraform" / "bootstrap" / "variables.tf"

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


def test_variables_still_refuse_roleadmin():
    """Validation control: the record is never 'closed' over a reverted guard."""
    assert '!contains(var.deployer_roles, "roles/iam.roleAdmin")' in _text(VARIABLES)
