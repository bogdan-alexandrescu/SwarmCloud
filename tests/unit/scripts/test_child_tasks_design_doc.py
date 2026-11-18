"""Child tasks, phase 1: the design and the contract request say what was decided.

WHY THIS EXISTS. Owner decisions OD-B15-1 to OD-B15-4 (2026-10-02) settle how
an agent's helpers ("main agent with helpers", docs/design/dispatch-and-
integration.md section 4.1) are built: CR 14 accepted WITH its cancel and
capacity rules, submission THROUGH the worker against the live lease, an
`await` that parks holding no capacity, depth 1 with a fan-out cap, and a
parent's cancel cascading to its children. Phase 2 builds it from
docs/design/child-tasks.md and from CR 14's amendment, so a design that lost
an invariant, a limit's reason or a failure case would be built without it.

Nothing here reads the code the design describes beyond checking that every
citation still resolves. Line NUMBERS are not cited at all (lane CITD):
lifecycle.py, loop.py and store.py are edited by other lanes every wave, so a
citation is `path::qualname` or `path` (`anchor text`), which fails only when
the symbol or the text is gone, not when a merge above it moves it. Where in the requests file requests
40-43 sit is not pinned either; the owner may refile them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from .test_docs_spec_amendments import _assert_cites_resolve, _cited_anchor

REPO = Path(__file__).resolve().parents[3]
DESIGN = REPO / "docs" / "design" / "child-tasks.md"
REQUESTS = REPO / "docs" / "contract-change-requests.md"

#: The contract requests this design files. CLAUDE.md lane brief: new numbers
#: start at 40.
NEW_REQUESTS = (40, 41, 42, 43)

def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The body under `heading`, up to the next heading of the same or a higher level."""
    start = text.index(heading)
    level = len(heading) - len(heading.lstrip("#"))
    rest = text[start + len(heading):]
    nxt = re.search(rf"^#{{1,{level}}} ", rest, flags=re.M)
    return rest[: nxt.start()] if nxt else rest


def _cr14() -> str:
    text = _text(REQUESTS)
    heading = next(
        line for line in text.splitlines() if line.startswith("## 14. ")
    )
    return _section(text, heading)


# --------------------------------------------------------------------------
# the design document
# --------------------------------------------------------------------------


def test_the_design_document_exists():
    assert DESIGN.is_file(), "docs/design/child-tasks.md is the phase-1 deliverable"


@pytest.mark.parametrize(
    "heading",
    [
        "## 1. Why",
        "## 2. The owner's decisions",
        "## 3. The flows",
        "## 4. The invariants",
        "## 5. Failure cases",
        "## 6. Routes and fields",
        "## 7. Limits and their defaults",
        "## 8. The frozen contract",
        "## 9. Phase 2",
    ],
)
def test_the_design_has_every_part_the_brief_names(heading):
    assert re.search(rf"^{re.escape(heading)}", _text(DESIGN), flags=re.M), heading


@pytest.mark.parametrize("number", range(1, 11))
def test_every_invariant_is_addressed_by_its_own_heading(number):
    """Invariants 1-10, each by name: a missing one is one nobody decided."""
    text = _text(DESIGN)
    match = re.search(rf"^### Invariant {number}\b.*$", text, flags=re.M)
    assert match, f"invariant {number} has no heading in section 4"
    body = _section(text, match.group(0))
    assert len(body.split()) >= 40, f"invariant {number} is named but not argued"


@pytest.mark.parametrize("decision", ["OD-B15-1", "OD-B15-2", "OD-B15-3", "OD-B15-4"])
def test_every_owner_decision_is_answered_in_both_documents(decision):
    assert decision in _text(DESIGN), decision
    assert decision in _cr14(), decision


@pytest.mark.parametrize(
    "needle",
    [
        "POST /v1/tasks/{parent_task_id}/children",
        "GET /v1/tasks?parent_task_id=",
        "parent_task_id",
        "parent_attempt_id",
        "(tenant_id, parent_task_id, created_at)",
        "CHILDREN_INCOMPLETE",
    ],
)
def test_the_routes_fields_and_index_are_named(needle):
    assert needle in _text(DESIGN), needle


def test_the_route_checks_the_live_lease_task_attempt_and_generation():
    body = _section(_text(DESIGN), "## 6. Routes and fields")
    for field in ("task_id", "attempt_id", "generation", "lease_id"):
        assert field in body, field


def test_the_design_does_not_pretend_the_agent_cannot_mint_its_tenants_token():
    """security.md concedes an agent can act as its tenant's service account.

    A design that called the tenant identity "the worker identity" and stopped
    would let any agent call the route directly. The attempt proof is what
    makes "only the worker" true; the route's own limits are what bound an
    agent that gets past the worker anyway.
    """
    text = _text(DESIGN)
    assert "security.md#cloud-metadata-abuse" in text
    assert "attempt proof" in text
    assert "enforced at the route" in text


def test_the_design_does_not_pretend_the_agent_cannot_read_the_environment():
    """tini is PID 1, holds the container environment and is not non-dumpable.

    hardening.py says the worker's prctl changes nothing at /proc/1/environ.
    A proof delivered as an environment override (the first draft of this
    design) is therefore the agent's too. What is allowed through the
    environment is a nonce the worker spends before the agent exists, and the
    key that signs requests lives only in the worker's protected heap.
    """
    body = _section(_text(DESIGN), "### 3.2 What makes")
    assert "/proc/1/environ" in body
    # Cited by its text, not a line number (lane CITD): the anchor fails if the paragraph goes.
    hardening, anchor = "apps/agent-worker/agent_worker/hardening.py", "WHAT IT DOES NOT COVER"
    assert f"`{hardening}` (`{anchor}`)" in body
    _cited_anchor(hardening, anchor)
    source = _text(REPO / hardening)
    assert "/proc/1/environ" in source[source.index(anchor):].split("\n\n", 1)[0]
    assert "before the agent exists" in body
    assert "first-wins" in body or "once per generation" in body
    assert "tombstone" in body, "an unprotected worker must still spend the nonce"
    assert "attest" in body, "the registration must not be a tenant-writable fact"
    assert "residual" in body, "the residual must be stated, not hidden"


def _limit_rows() -> list[list[str]]:
    body = _section(_text(DESIGN), "## 7. Limits and their defaults")
    rows = []
    for line in body.splitlines():
        if not line.startswith("| `"):
            continue
        rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


def test_every_limit_has_a_default_and_a_reason():
    rows = _limit_rows()
    assert len(rows) >= 6, "the limits table lost rows"
    for name, default, where, reason in (r[:4] for r in rows):
        assert default, f"{name} has no default"
        assert where, f"{name} does not say where it is enforced"
        assert len(reason.split()) >= 12, f"{name}'s reason is not a reason: {reason!r}"


def test_depth_is_one_and_the_fan_out_cap_fits_a_new_tenant():
    """Depth 1 is OD-B15-3. The fan-out cap plus the parent stays within a new
    tenant's `default_tenant_max_active`, so a fresh tenant can run one full
    fan-out with no admin action -- the reason the table gives for the value."""
    rows = {r[0]: r for r in _limit_rows()}
    assert rows["`max_child_depth`"][1] == "1"
    fan_out = int(rows["`max_children_per_task`"][1])
    config = _text(REPO / "apps" / "common" / "swarm_common" / "config.py")
    tenant_max = int(re.search(r"default_tenant_max_active: int = (\d+)", config).group(1))
    assert fan_out + 1 <= tenant_max


def test_the_failure_cases_cover_the_ones_that_strand_or_leak():
    body = _section(_text(DESIGN), "## 5. Failure cases")
    rows = [line for line in body.splitlines() if re.match(r"^\| F\d+ ", line)]
    assert len(rows) >= 12, "the failure table lost rows"
    for needle in (
        "crashes after",       # submitted, response lost: idempotent resubmit
        "fenced",              # stale parent generation
        "cancel",              # parent cancelled with live children
        "DEAD_LETTERED",       # parent ends non-success with live children
        "deadline",            # a child that never ends
        "another tenant",      # a rewritten parent_task_id
        "memory protection",   # the proof in an unprotected heap
    ):
        assert needle in body, needle


@pytest.mark.parametrize("doc", [DESIGN, REQUESTS], ids=lambda p: p.name)
def test_every_cited_file_exists(doc: Path):
    """Every citation names a file that exists and resolves as a symbol or an anchor, never a line (lane CITD)."""
    _assert_cites_resolve(doc)


# --------------------------------------------------------------------------
# contract-change-requests.md: CR 14's amendment, and the requests it files
# --------------------------------------------------------------------------


def test_cr14_records_the_acceptance_and_that_it_is_applied():
    """Phase 2 (2026-10-02) applied it: the status says so, with the acceptance
    still on the record, and the index row agrees."""
    body = _cr14()
    status = next(line for line in body.splitlines() if line.startswith("**Status:"))
    assert "ACCEPTED 2026-10-02" in status
    assert "OD-B15-1" in status
    assert "APPLIED 2026-10-02" in status
    index = re.search(r"^\| 14 \| .*$", _text(REQUESTS), flags=re.M).group(0)
    assert "APPLIED 2026-10-02" in index and "OD-B15-1" in index


def test_cr14_amendment_adds_the_cancel_and_capacity_rules():
    body = _cr14()
    assert "### Amendment, 2026-10-02" in body
    amendment = _section(body, "### Amendment, 2026-10-02")
    for needle in ("Cancellation", "Capacity", "docs/design/child-tasks.md"):
        assert needle in amendment, needle


@pytest.mark.parametrize("number", NEW_REQUESTS)
def test_each_new_request_is_filed_once_with_its_parts_and_an_index_row(number):
    text = _text(REQUESTS)
    headings = re.findall(rf"^#+ {number}\. .*$", text, flags=re.M)
    assert len(headings) == 1, f"request {number} filed {len(headings)} times"
    body = _section(text, headings[0])
    # Applied with request 14 in phase 2 (2026-10-02).
    assert re.search(r"^\*\*Status:\*\* APPLIED 2026-10-02", body, flags=re.M), (
        f"request {number} status"
    )
    for part in (
        "What is true today",
        "The requested change",
        "What it would break if accepted",
        "If it is declined",
    ):
        assert part in body, f"request {number} lacks {part!r}"
    assert re.search(rf"^\| {number} \| .* \| APPLIED 2026-10-02", text, flags=re.M), (
        f"no index row for {number}"
    )


def test_each_new_request_points_back_to_cr14s_amendment():
    """They exist because CR 14 was accepted; a reader of either must find the other."""
    text = _text(REQUESTS)
    amendment = _section(_cr14(), "### Amendment, 2026-10-02")
    for number in NEW_REQUESTS:
        heading = re.search(rf"^#+ {number}\. .*$", text, flags=re.M).group(0)
        assert "request 14's amendment" in _section(text, heading), number
        assert f"[{number}](#{number}-" in amendment, f"CR 14's amendment does not link {number}"
