"""The bucket's clone-bundle lifecycle rule, the worker and the doc name the same suffixes (#940).

WHY: the rule that expires clone bundles is bucket-wide and keyed only on
two suffixes (`terraform/modules/storage/main.tf`), because a prefix list
cannot reach the personal `u-<slug>` tenants the API creates at runtime. If
the worker's `clonebundle.BUNDLE_SUFFIX`/`HEAD_SUFFIX` and Terraform's
`matches_suffix` drift apart, bundles stop expiring on the 7-day clock and
sit under the 180-day customTime rule instead, and nothing fails. The doc
(`docs/clone-bundles.md`) is what an operator reads to find them, so it is
held to the same strings and to the key layout.

Offline: Terraform and the doc are read as text; nothing runs terraform.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_worker import clonebundle

REPO = Path(__file__).resolve().parents[3]
STORAGE_TF = REPO / "terraform" / "modules" / "storage" / "main.tf"
DOC = REPO / "docs" / "clone-bundles.md"


def _suffix_lists(tf: str) -> list[list[str]]:
    """Every `matches_suffix = [...]` list in the file, as strings, in order."""
    found = []
    for match in re.finditer(r"matches_suffix\s*=\s*\[(.*?)\]", tf, flags=re.S):
        found.append(re.findall(r'"([^"]*)"', match.group(1)))
    return found


def test_the_lifecycle_rule_suffixes_equal_the_workers():
    lists = _suffix_lists(STORAGE_TF.read_text(encoding="utf-8"))
    bundle_lists = [
        values for values in lists
        if any(value.startswith(".swarm-clone.") for value in values)
    ]
    assert len(bundle_lists) == 1, (
        f"expected exactly one clone-bundle matches_suffix list in {STORAGE_TF.name}, "
        f"found {bundle_lists!r}"
    )
    assert sorted(bundle_lists[0]) == sorted(
        [clonebundle.BUNDLE_SUFFIX, clonebundle.HEAD_SUFFIX]
    )
    # Distinctive on purpose: a bare `.bundle` would catch an agent artifact.
    for suffix in bundle_lists[0]:
        assert suffix.startswith(".swarm-clone."), suffix


def test_the_doc_names_the_key_layout_and_both_suffixes():
    doc = DOC.read_text(encoding="utf-8")
    assert clonebundle.BUNDLE_SUFFIX in doc
    assert clonebundle.HEAD_SUFFIX in doc
    assert f"tenants/<tenant>/bundles/<repo_id>/<sha>{clonebundle.BUNDLE_SUFFIX}" in doc
    assert (
        f"tenants/<tenant>/bundles/<repo_id>/{clonebundle.HEADS_SEGMENT}/"
        f"<sha256(ref)[:32]>{clonebundle.HEAD_SUFFIX}"
    ) in doc
    # The kill switch an operator reaches for, and the acceptance field.
    assert "SWARM_CLONE_BUNDLES=0" in doc
    assert "total_seconds" in doc
