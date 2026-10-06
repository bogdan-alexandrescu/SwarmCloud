"""The merge step as its own workflow step, design lane MS0: the docs say what the owner asked.

WHY THIS EXISTS. Owner decision 2026-10-06 (epic #352, #295): merging becomes
a distinct workflow step -- implement -> review -> fix -> merge -- that parks
while CI runs, holding no capacity (invariant 4), is woken when the checks
complete, updates a branch that is behind, squash-merges with the tenant's
`-git` token, closes the issues its pull request names, and replaces
`.github/workflows/auto-merge.yml` for SwarmCloud's own pull requests once it
has merged about ten cleanly. Lane MS0 writes the DESIGN AND THE BUILD PLAN
only, in docs/merge-step.md, and re-triages every box of epic #352 against it.

A revision that lost one of the brief's parts -- the lifecycle and its wake
signal, a refusal, the token's exact permissions, what the new design
supersedes, an invariant, the retirement gate, a lane of the build plan, a box
of the epic -- would be built without it. So each part is held here, in the
section that carries it. Citations in the new section are symbols or anchors
that must resolve, never line numbers (lane CITD).
"""

from __future__ import annotations

import re
from pathlib import Path

from .test_docs_spec_amendments import (
    _ANCHOR_CITE,
    _LINE_CITE,
    _SYMBOL_CITE,
    _cited_anchor,
    _cited_symbol,
)

REPO = Path(__file__).resolve().parents[3]
DESIGN = REPO / "docs" / "merge-step.md"
WORKFLOWS = REPO / "docs" / "workflows.md"
REQUESTS = REPO / "docs" / "contract-change-requests.md"

REVISION = "## Revised 2026-10-06 (owner): merging is its own step, parked while CI runs"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The body under `heading`, up to the next heading of the same or a higher level."""
    assert heading in text, f"no heading {heading!r}"
    start = text.index(heading)
    level = len(heading) - len(heading.lstrip("#"))
    rest = text[start + len(heading):]
    nxt = re.search(rf"^#{{1,{level}}} ", rest, flags=re.M)
    return rest[: nxt.start()] if nxt else rest


def _subsection(prefix: str) -> str:
    revision = _section(_text(DESIGN), REVISION)
    match = re.search(rf"^{re.escape(prefix)}.*$", revision, flags=re.M)
    assert match, f"the 2026-10-06 revision has no heading starting {prefix!r}"
    return _section(revision, match.group(0))


# --------------------------------------------------------------------------
# docs/merge-step.md
# --------------------------------------------------------------------------


def test_the_revision_leads_the_document_and_the_status_line_names_it():
    text = _text(DESIGN)
    head = "\n".join(text.splitlines()[:24])
    assert "2026-10-06" in head, "the status line must say the design was revised on 2026-10-06"
    assert REVISION in text
    assert text.index(REVISION) < text.index("## Revised 2026-10-04 (owner)"), (
        "the newest revision comes first: a reader stops at the first section that answers them"
    )
    revision = _section(text, REVISION)
    for ref in ("#352", "#295", "MS0", "auto-merge.yml"):
        assert ref in revision, f"the revision must name {ref}"
    assert "DESIGN" in revision and "nothing in this section is built" in revision.lower()


def test_lifecycle_parks_on_ci_pending_and_says_which_signal_wakes_it_and_why():
    life = _subsection("### 1. Lifecycle")
    for word in ("PARKED", "`CI_PENDING`", "no capacity", "READY", "SUCCEEDED", "MERGE_REFUSED"):
        assert word in life, f"the lifecycle must name {word}"
    # The wake signal: a webhook is considered and rejected, the periodic tick chosen.
    assert "webhook" in life.lower()
    assert "/v1/admin/merges/wake" in life, "the wake route is named"
    assert "`terraform/modules/scheduler/jobs.tf` (`resource \"google_cloud_scheduler_job\" \"issue_run_advance\"`)" in life
    assert "apps/swarm-api/swarm_api/forge.py::SecretManagerForgeTokens" in life
    assert "_promote_child_awaits" in life, "the scheduler sweep it is modelled on"
    # Each wake outcome.
    assert "update-branch" in life and "expected_head_sha" in life, "a branch behind is updated, pinned"
    assert "squash" in life.lower()
    assert "closingIssuesReferences" in life and "scripts/close-merged-issues.sh" in life
    assert "CI-fix loop" in life and "checks_failed" in life


def test_every_refusal_the_owner_listed_ends_merge_refused_with_its_code():
    life = _subsection("### 1. Lifecycle")
    for code in ("`from_fork`", "`base_not_default`", "`title_placeholder`",
                 "`protection_refused`", "`merge_conflict`", "`checks_timeout`",
                 "`behind_too_often`"):
        assert code in life, f"the refusal {code} must be listed"
    assert "[swarm] task_" in life


def test_identity_is_the_tenants_git_token_with_exact_permissions():
    ident = _subsection("### 2. Identity")
    assert "swarm-tenant-<tenant>-git" in ident
    for perm in ("Contents: write", "Pull requests: write", "Issues: write", "Checks: read",
                 "Commit statuses: read", "Workflows: write"):
        assert perm in ident, f"the token's permission {perm!r} must be stated"
    assert "branch protection" in ident.lower() and "ruleset" in ident.lower()
    assert "bypass" in ident.lower(), "an admin token's bypass is the residual to name"


def test_superseded_names_each_retired_piece_and_keeps_signed_specs_with_a_reason():
    sup = _subsection("### 3. What this supersedes")
    for piece in ("review App", "merge App", "`-git-merge`", "`-git-review`", "`single-pr`",
                  "`post-verdict`", "`claude-code-review`", "§9", "R8"):
        assert piece in sup, f"what is superseded must name {piece}"
    assert "Kept" in sup and "signed step spec" in sup.lower()


def test_every_invariant_one_to_ten_is_answered():
    inv = _subsection("### 4. Invariants")
    for n in range(1, 11):
        assert re.search(rf"^\* \*\*{n}\.", inv, flags=re.M), f"invariant {n} must have its own bullet"


def test_retirement_plan_for_auto_merge_names_the_gate_and_the_files():
    ret = _subsection("### 5. Retiring")
    assert "auto-merge.yml" in ret
    assert "10" in ret and "merged_by_this_task" in ret
    for path in ("tests/unit/scripts/test_auto_merge_workflow.py", "docs/runbooks/merge-app.md",
                 "CLAUDE.md", "docs/ci.md"):
        assert path in ret, f"the retirement must name {path}"


def test_build_plan_has_every_lane_with_territory_dependencies_and_tests():
    plan = _subsection("### 6. Build plan")
    for lane in ("MS1", "MS2", "MS3", "MS4", "MS5", "MS6", "MS7"):
        match = re.search(rf"^#### {lane}\b.*$", plan, flags=re.M)
        assert match, f"the build plan must have lane {lane}"
        body = _section(plan, match.group(0))
        for field in ("**Territory:**", "**Depends on:**", "**Tests:**"):
            assert field in body, f"{lane} must state {field}"


def test_epic_352_is_retriaged_box_by_box():
    tri = _subsection("### 7. Epic #352")
    rows = [line for line in tri.splitlines() if line.startswith("| ") and not line.startswith("| box")
            and not set(line) <= set("|- ")]
    assert len(rows) == 10, f"epic #352 has 10 boxes; the triage lists {len(rows)}"
    for row in rows:
        assert re.search(r"kept as MS\d|superseded because|still needed separately|done", row), row


def test_the_revision_cites_symbols_and_anchors_that_resolve_and_no_line_numbers():
    revision = _section(_text(DESIGN), REVISION)
    assert not _LINE_CITE.findall(revision), "cite a symbol or an anchor, never a line number"
    symbols = _SYMBOL_CITE.findall(revision)
    assert len(symbols) >= 8, "the revision ties its claims to code"
    for path, qualname in symbols:
        assert (REPO / path).is_file(), f"cites {path}, which does not exist"
        _cited_symbol(path, qualname)
    for path, anchor in _ANCHOR_CITE.findall(revision):
        _cited_anchor(path, anchor)


# --------------------------------------------------------------------------
# docs/workflows.md and docs/contract-change-requests.md
# --------------------------------------------------------------------------


def test_workflows_merge_section_points_at_the_revision_and_keeps_what_is_built():
    section = _section(_text(WORKFLOWS), "## Ending in a merge: the `merge` step")
    assert "2026-10-06" in section and "CI_PENDING" in section
    assert "merge-step.md#revised-2026-10-06-owner-merging-is-its-own-step-parked-while-ci-runs" in section
    assert "MERGE_STEP_MAX_ATTEMPTS" in section, "what runs today stays described until MS2 replaces it"


def test_contract_requests_mark_what_the_new_design_supersedes():
    text = _text(REQUESTS)
    for heading in (
        "## 33. `profiles.py` / `models.py`: a merge profile that runs no agent, and two end causes for it",
        "## 35. `profiles.py` / `models.py`: the `post-verdict` worker-action profile, and its own end causes",
        "## 36. `profiles.py`: the `claude-code-review` profile, and a typed `never_restore_checkpoint`",
    ):
        body = _section(text, heading)
        head = "\n".join(body.strip().splitlines()[:30])
        assert "Superseded" in head and "2026-10-06" in head, f"{heading[:8]} must say what 2026-10-06 superseded"
