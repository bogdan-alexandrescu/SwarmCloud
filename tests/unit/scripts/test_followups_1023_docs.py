"""The 2026-10-23 lanes' out-of-territory follow-ups say what now holds (FOLLOWUPS-1023; part of #2150).

WHY THIS EXISTS. Four lanes of 2026-10-23 changed behaviour and left a
sentence elsewhere saying the opposite. Each check below reads the sentence
that was wrong and the one that replaced it:

  * contract request 114: swarm-api's startup CPU boost (PR 2132, #1816
    option (a)) is BGD's one recorded exception to invariant 7, and every other
    service and every worker stays as the invariant says. The invariant's own
    text in CLAUDE.md and CONTRACT.md is not softened by it;
  * design-system.md: the 560px phone block spares the Overview Pools list's
    tracks too (PR 2137, QAFIX6-OVERVIEW-2 item a);
  * two swarm-ui test comments no longer call V088's `align-items: start`
    deliberate (QAFIX6-CAPACITY-2 retired it for `stretch`);
  * the worker's merge docstrings: an answer that clears the hold wakes the
    merge, and the human-wait expiry spares it (PR 2151, QUESTION-WAKE);
  * the web-UI docs: an admin's `/v1/stats` carries `platform_tasks_by_state`
    only with `?scope=platform` (PR 2152, COUNTS-POLL).

Reads files only: no network, no credentials.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
REQUESTS = REPO / "docs" / "contract-change-requests.md"
DESIGN = REPO / "docs" / "web-ui" / "design-system.md"
UI_TESTS = REPO / "apps" / "swarm-ui" / "src" / "__tests__"
WORKER = REPO / "apps" / "agent-worker" / "agent_worker"
WEB_UI = REPO / "docs" / "web-ui"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Prose with its line breaks, blockquote marks and comment marks folded."""
    text = re.sub(r"^\s*(?:>|//|#)\s?", "", text, flags=re.M)
    return re.sub(r"\s+", " ", text)


def _entry(text: str, number: int) -> str:
    match = re.search(rf"^## {number}\. .*$", text, flags=re.M)
    assert match is not None, f"contract-change-requests.md has no entry {number}"
    rest = text[match.end():]
    nxt = re.search(r"^## \d+\. ", rest, flags=re.M)
    return match.group(0) + (rest[: nxt.start()] if nxt else rest)


# ---------------------------------------------------------------------------
# 1. The invariant-7 exception is an entry.


def test_the_swarm_api_cpu_boost_is_contract_request_114():
    text = _read(REQUESTS)
    raw = _entry(text, 114)
    body = _flat(raw)
    # MUTATION: delete entry 114, or retitle it away from the boost -> red.
    assert re.search(r"^## 114\. .*startup CPU boost.*\(API-CPU-BOOST, part of #1816\)$",
                     text, re.M)
    # MUTATION: drop who decided, when, or the PR that applied it -> red.
    for fact in ("BGD", "2026-10-23", "PR 2132", "option (a)", "startup_cpu_boost = true"):
        assert fact in body, f"entry 114 does not say {fact!r}"
    # MUTATION: leave out the scope of the exception -> red.
    assert "every other Cloud Run service" in body
    assert "every worker" in body
    assert "`cpu_idle`" in body
    for section in ("### What is true today", "### Why it was allowed", "### What it does not change",
                    "### Rollback", "### Invariants"):
        assert f"\n{section}\n" in raw, f"entry 114 has no {section!r}"


def test_request_114_has_one_index_row_that_says_it_is_applied():
    rows = [line for line in _read(REQUESTS).splitlines() if line.startswith("| 114 |")]
    # MUTATION: drop the index row, or file it twice -> red.
    assert len(rows) == 1, rows
    assert "API-CPU-BOOST" in rows[0] and "APPLIED" in rows[0] and "BGD" in rows[0]


@pytest.mark.parametrize("doc", ["CLAUDE.md", "CONTRACT.md"])
def test_the_invariant_7_text_is_not_softened_by_the_exception(doc):
    text = _read(REPO / doc)
    # MUTATION: write the boost exception into the invariant itself -> red.
    (item,) = re.findall(r"^7\. Memory and ephemeral storage.*?(?=^8\. )", text, re.M | re.S)
    assert "boost" not in item.lower(), item


# ---------------------------------------------------------------------------
# 2. The phone block spares the Overview Pools list's tracks.


def test_design_system_says_the_phone_spares_the_overview_pools_tracks():
    flat = _flat(_read(DESIGN))
    # MUTATION: restore "hides every track but a pool tile's;" alone -> red.
    assert "hides every track but a pool tile's;" not in flat
    assert "but a pool tile's and the Overview Pools list's" in flat
    # MUTATION: drop the selector or the decision it cites -> red.
    assert "`.ov-pools .ov-pl > .ctl-util-track`" in flat
    assert "Q3 of 2026-10-11" in flat and "PR 2137" in flat


# ---------------------------------------------------------------------------
# 3. V088's `start` is not called deliberate any more.


@pytest.mark.parametrize("name", ["qafix3.admin.test.tsx", "qafix5.capacity.test.tsx"])
def test_the_capacity_011_comments_say_stretch_now_holds(name):
    head = _flat(_read(UI_TESTS / name).split("import ", 1)[0])
    # MUTATION: restore "deliberate `align-items: start`" -> red.
    assert "deliberate `align-items: start`" not in head
    assert "deliberate `align-items:" not in head
    # MUTATION: drop what holds now -> red.
    assert "`align-items: stretch`" in head and "QAFIX6-CAPACITY-2" in head


# ---------------------------------------------------------------------------
# 5. The merge's owner-question hold wakes on its last answer.


def test_the_merge_module_docstring_says_an_answer_wakes_the_hold():
    flat = _flat(_read(WORKER / "merge.py").split('"""', 2)[1])
    # MUTATION: restore "ONLY a person's `human-ready` brings it back" -> red.
    assert "ONLY a person's `human-ready`" not in flat
    assert "dead-letters this park" not in flat
    assert "is an open owner question" not in flat
    # MUTATION: drop what QUESTION-WAKE built -> red.
    assert "owner_questions_answered" in flat and "PR 2151" in flat
    assert "spares this park" in flat


def test_the_lifecycle_park_docstring_says_an_answer_wakes_the_hold():
    text = _read(WORKER / "lifecycle.py")
    start = text.index("def _park_for_owner_questions(")
    doc = _flat(text[start: text.index('"""', text.index('"""', start) + 3)])
    # MUTATION: restore "An answer alone does not wake it" -> red.
    assert "An answer alone does not wake it" not in doc
    assert "expiry applies to it" not in doc
    assert "PR 2151" in doc and "spares it" in doc


# ---------------------------------------------------------------------------
# 6. An admin's /v1/stats carries the platform half only when asked.


@pytest.mark.parametrize("doc", ["01-reachability.md", "02-cluster-state.md",
                                 "05-alerts-errors.md", "08-operator-gaps.md"])
def test_every_platform_tasks_by_state_claim_names_scope_platform(doc):
    flat = _flat(_read(WEB_UI / doc))
    # MUTATION: remove `?scope=platform` from any of the four docs -> red.
    assert "?scope=platform" in flat, doc
    # MUTATION: restore an unconditional admin claim -> red.
    for stale in ("and `platform_tasks_by_state` for admins.",
                  "admin also gets `.platform_tasks_by_state` (`service.py:287`)",
                  "is added to `/v1/stats` only when `ctx.is_admin`",
                  "only adds `platform_tasks_by_state` for admins (`:287`)",
                  "an admin gets `platform_tasks_by_state` as well",
                  "only `platform_tasks_by_state` is admin-gated",
                  "`platform_tasks_by_state` (admin only).",
                  "24 for an admin**",
                  "(24 for an admin, because"):
        assert stale not in flat, f"{doc} still says {stale!r}"
