"""CR 33's #364 amendment and CR 35's status must say what is actually built.

Both status paragraphs said items 2 and 3 of the amendment -- the
`secret_manager` accessor override and the refresh exclusion -- were "not
applied yet (lane M2)" long after `terraform/modules/secret_manager` carried
both and `tests/terraform/merge_step_iam.tftest.hcl` tested them. A reader
following that record would think the build was still gated (#364).

The premise test holds the doc assertions to the Terraform: if the module ever
loses the override or the exclusion, the record saying "applied" is what fails
here, rather than going quietly false the other way.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CRS = REPO / "docs" / "contract-change-requests.md"
MERGE_STEP = REPO / "docs" / "merge-step.md"
SECRET_MANAGER = REPO / "terraform" / "modules" / "secret_manager"

AMENDMENT_HEADING = "### Amendment (proposed 2026-09-30, #364)"


def _flat(text: str) -> str:
    """Collapse line wrapping, so a phrase split across lines still matches."""
    return re.sub(r"\s+", " ", text)


def _section(text: str, start: str) -> str:
    begin = text.index(start)
    end = text.find("\n## ", begin + len(start))
    return text[begin : end if end != -1 else len(text)]


def test_the_secret_manager_module_carries_both_items():
    main = (SECRET_MANAGER / "main.tf").read_text()
    variables = (SECRET_MANAGER / "variables.tf").read_text()
    assert "accessor_overrides" in main and "accessor_overrides" in variables
    assert 'variable "action_providers"' in variables
    assert "refreshable = !contains(var.action_providers, provider)" in _flat(main)
    assert "v.refreshable" in main, "the -refresh twins and refresher grants filter on it"


def test_cr33_amendment_records_items_2_and_3_as_applied():
    section = _flat(_section(CRS.read_text(), AMENDMENT_HEADING))
    assert "not applied yet" not in section
    assert "Items 2 and 3 APPLIED" in section
    assert "`accessor_overrides`" in section
    assert "tests/terraform/merge_step_iam.tftest.hcl" in section


def test_cr35_status_records_the_companions_as_applied():
    section = _flat(_section(CRS.read_text(), "## 35."))
    assert "are not yet (lane M2)" not in section
    assert "refresh exclusion are not yet" not in section
    assert "refresh exclusion are APPLIED" in section
    assert "tests/terraform/merge_step_iam.tftest.hcl" in section


def test_the_index_row_for_33_says_the_amendment_is_applied_in_full():
    rows = [line for line in CRS.read_text().splitlines() if line.startswith("| 33 |")]
    assert len(rows) == 1
    row = rows[0]
    assert "items 1-3" in row, row
    assert "item 1 applied with it" not in row, row


def test_merge_step_keeps_the_r8_correction():
    text = _flat(MERGE_STEP.read_text())
    assert "Corrected (2026-09-30, #364)" in text
    assert "R8" in text
    assert "carries no portable secret" not in text
