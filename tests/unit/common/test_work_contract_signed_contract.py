"""Contract request 137, the work contract signed with the step, as applied (part of #2688).

Accepted by the owner 4 November 2026 ("Sign the key"):

  * `"work_contract"` is in `specsign.SIGNED_METADATA_KEYS`;
  * spec format 8, format 7's fields plus `metadata.work_contract`, at which a
    task is signed only when its metadata carries a contract;
  * formats 1-7 keep exactly the bytes they had, so every stored signature
    still verifies during a rolling deploy, and a document at one of them that
    carries a contract has no canonical form (a contract added to a task
    signed without one fails its worker's check).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarm_common import specsign
from swarm_common.specsign import SpecNotCanonical, canonical_step_spec, signing_format, spec_digest

REQUESTS = Path(__file__).resolve().parents[3] / "docs" / "contract-change-requests.md"

DOC = {
    "tenant_id": "eng",
    "runner_profile": "claude-code",
    "resource_class": "standard",
    "timeout_seconds": 900,
    "max_attempts": 3,
    "input": {"prompt": "x"},
    "submitted_by": "alice@example.com",
    "metadata": {"dispatch": {"strategy": "direct-pr"}, "label": "mine"},
}

CONTRACT = {
    "schema": 1,
    "contract_id": "wc-sort-widgets",
    "tenant_id": "eng",
    "revision": 1,
    "strictness": "advisory",
    "requirements": [{"id": "REQ-1", "text": "Widgets sort by name."}],
    "capabilities": {"plugins": {"gstack": {"mode": "unattended", "skills": ["review"]}}},
}


def _with(contract) -> dict:
    return {**DOC, "metadata": {**DOC["metadata"], "work_contract": contract}}


#: `DOC`'s digest at every format before format 8 existed, derived from
#: specsign.py as it was at c9c4c43. Each must be unchanged by format 8.
PRE_FORMAT_8_DIGESTS = {
    1: "21ecb5f24b2a7d8f8dbfc3622f9e6ac85bc8beb696681e403b3e65cb2ff553a3",
    2: "773e8be93b04a65dbbc88b13f3c2e1f49291214936bed88513afa66c8e253687",
    3: "118e2bf71a8e4c151926ae285ac2799f94af93326145eedaebf5fb41f2a47c45",
    4: "22a48ed395e069ca479528a407c983f170aa8acb96cbe59d29b20e2a4e18c83b",
    5: "026c9566297df41f463f485a82017486c71123c21fee691dd3af2037f2d06b69",
    6: "7e0ae60c26d4891f4ab077cdc32f347208c8dc1087e153097aa6ced04bf7cd37",
    7: "3909a8e1fbfc383e34c806b79d85b65f999f5dbe6acdc07ca4977ae75c7e8b88",
}


def test_the_key_is_signed_and_the_format_is_8():
    assert "work_contract" in specsign.SIGNED_METADATA_KEYS
    assert specsign.SPEC_FORMAT == 8 and specsign.SPEC_FORMATS == (1, 2, 3, 4, 5, 6, 7, 8)


def test_a_contract_is_signed_at_format_8_and_only_a_contract_is():
    # MUTATION: leave `signing_format` without format 8 and this is 1.
    assert signing_format(_with(CONTRACT)) == 8
    assert signing_format({**_with(CONTRACT), "max_turns": 30}) == 8
    assert signing_format(DOC) == 1, "a task with no contract keeps its old format"
    assert signing_format(_with(None)) == 1
    assert signing_format({**DOC, "max_turns": 30}) == 7
    assert signing_format({**DOC, "metadata": None}) == 1


def test_the_contract_is_inside_format_8s_bytes():
    signed = canonical_step_spec(_with(CONTRACT), task_id="task_1", spec_format=8)
    assert json.loads(signed)["metadata"]["work_contract"] == CONTRACT
    widened = {**CONTRACT, "capabilities": {"plugins": {
        "gstack": {"mode": "unattended", "skills": ["review", "ship"]}}}}
    rewritten = canonical_step_spec(_with(widened), task_id="task_1", spec_format=8)
    removed = canonical_step_spec(DOC, task_id="task_1", spec_format=8)
    assert spec_digest(signed) != spec_digest(rewritten)
    assert spec_digest(signed) != spec_digest(removed)
    # An uncovered caller label is still outside the bytes at format 8.
    relabelled = {**_with(CONTRACT), "metadata": {**_with(CONTRACT)["metadata"], "label": "x"}}
    assert canonical_step_spec(relabelled, task_id="task_1", spec_format=8) == signed


@pytest.mark.parametrize("older", [1, 2, 3, 4, 5, 6, 7])
def test_a_contract_has_no_canonical_form_below_format_8(older):
    """Adding a contract to a task signed without one must fail verification."""
    with pytest.raises(SpecNotCanonical, match="metadata.work_contract"):
        canonical_step_spec(_with(CONTRACT), task_id="task_1", spec_format=older)
    canonical_step_spec(_with(None), task_id="task_1", spec_format=older)


@pytest.mark.parametrize("older", sorted(PRE_FORMAT_8_DIGESTS))
def test_older_formats_bytes_do_not_change(older):
    """A rolling deploy: every task signed before format 8 keeps verifying."""
    canonical = canonical_step_spec(DOC, task_id="task_1", spec_format=older)
    assert spec_digest(canonical).hex() == PRE_FORMAT_8_DIGESTS[older]
    assert b'"work_contract"' not in canonical


def test_the_request_is_marked_accepted_and_applied():
    text = REQUESTS.read_text()
    row = next(line for line in text.splitlines() if line.startswith("| 137 |"))
    assert "ACCEPTED by the owner 4 November 2026" in row and "APPLIED" in row
    section = text.split("\n## 137. ", 1)[1].split("\n## ", 1)[0]
    assert section.splitlines()[2].startswith("**Status:** ACCEPTED by the owner")
    assert "digest-verified allowance list" in section
