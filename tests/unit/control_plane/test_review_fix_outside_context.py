"""Review and fix prompts see past the diff (context-quality.md §2.3, §3, §4.5, lane CTXQ-RV).

KG6 gave the review the call sites and covering tests of the plan's files, and
the CI fixer the code a failing test exercises. §2.3 measured what review
still missed, and §3 why fixers repeated each other:

  * #1181: `_audited` sorted Firestore rows in stream order while its sibling
    `audit_entries`, in the same file, already sorted by `(at, id)`. The
    review now lists SIBLINGS: existing code beside the plan's symbols that
    does the same kind of thing, so the reviewer compares the two ways.
  * Four fixers fixed the same time bomb four times (§3.2 point 5). The fixer
    is now shown the EARLIER FIXES of the failing test, from the index's
    history layer (layer H, `test_fixes`), and asked to read that commit
    first.
  * The fixer is told which code outside the plan's files calls the planned
    symbol the red test exercises, and which other tests cover it.

The control for each: a plan or a red test with nothing outside the diff
adds nothing -- the block is byte for byte what it was without this lane.

Graphs are written by the SHIPPED writer, context layers included, and read
through the real `RepoGraph`, as test_plan_context.py does. No credentials.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from swarm_api import issueci, reviewcontext
from swarm_api.validation import parse_issue_ref

from .conftest import TEST_REPOSITORIES, grant_members
from .repo_index_fakes import sha
from .test_plan_context import (  # noqa: F401 -- fixtures: the forge, the context, the writer
    CHARGE,
    LIST,
    ONE,
    REF,
    SORT,
    _edge,
    _promote,
    _register,
    _sym,
    api_context,
    github,
    graph_doc,
    writer,
)


@pytest.fixture(autouse=True)
def _members_hold_grants(db):
    grant_members(db, *TEST_REPOSITORIES)


def _document(symbols: list[str], edges: list[tuple[str, str]],
              tests: list[tuple[str, str]] = ()) -> dict:
    """A version-3 graph of `symbols` with `edges` and a symbol-test map, no terms."""
    rows = [_sym(*s.split("#", 1)) for s in symbols]
    return {
        "schema": "swarm.repo-graph/v1", "kind": "full", "commit_sha": ONE,
        "branch": "main", "base_sha": None, "languages": [], "truncated": [],
        "extractor": {"name": "swarm-repo-index", "version": "3"},
        "symbols": rows,
        "call_edges": [_edge(a, b) for a, b in edges],
        "symbol_test_map": [{"symbol": s, "test": t, "depth": 1, "confidence": 0.9}
                            for s, t in tests],
        "files": [{"path": r["path"]} for r in rows],
        "communities": [], "terms": [],
        "term_stats": {"documents": 0, "average_length": 0.0, "k1": 1.2, "b": 0.75,
                       "tokenizer": "1"},
        "signatures": [], "signature_changes": [], "flows": [],
    }


def _with_context(writer, context: dict | None):
    """The shipped writer, storing `context` (layer H and R) as an index run would."""
    return SimpleNamespace(
        LocalStore=writer.LocalStore,
        write_graph=lambda document, store, **kw: writer.write_graph(
            document, store, context=context, **kw),
    )


def _index(client, db, objects, writer, tmp_path, document: dict,
           context: dict | None = None) -> None:
    repo_id = _register(client)
    _promote(client, db, objects, _with_context(writer, context), tmp_path, repo_id, document)


def _run(plan: dict | None) -> SimpleNamespace:
    return SimpleNamespace(issue=parse_issue_ref(REF), plan=plan, id="r1", tenant_id="eng")


def _section(block: str, title: str) -> str:
    """The lines of `block` from the line starting `title` to the next blank line."""
    assert title in block, block
    return block.split(title, 1)[1].split("\n\n", 1)[0]


# --------------------------------------------------------------------------
# the review's SIBLINGS: #1181, replayed
# --------------------------------------------------------------------------

LIVE = "apps/swarm-api/swarm_api/live.py"
AUDIT_ENTRIES = f"{LIVE}#LiveService.audit_entries"
ADMIT = f"{LIVE}#LiveService.admit"
TASK = f"{LIVE}#LiveService._task"
ISO = f"{LIVE}#_iso"
TICKET = "apps/swarm-api/swarm_api/liveticket.py#verify_ticket"

#: #1181's plan, as its planner wrote it: `_audited` is new, so the index has
#: never seen it; `audit_entries` already reads the same collection and sorts.
PLAN_1181 = {
    "summary": "Say who took control of a live session in its log.",
    "requirements": ["The log names the person who took control."],
    "steps": [{
        "step_id": "audit-lines", "title": "Name the taker in the live log",
        "prompt": ("Add `LiveService._audited`, which reads the task's live_audit entries, "
                   "and have `audit_lines` tell `device_changed` from `rotated` by the "
                   "entry before each one."),
        "files": [LIVE], "tests": ["tests/unit/control_plane/test_live.py::test_taker"],
    }],
}


def test_1181s_sibling_sort_is_listed_for_its_review(api_context, client, db, objects,
                                                     writer, tmp_path):
    document = _document([AUDIT_ENTRIES, ADMIT, TASK, ISO, TICKET],
                         [(AUDIT_ENTRIES, TASK), (AUDIT_ENTRIES, ISO), (ADMIT, TASK),
                          (ADMIT, TICKET)])
    _index(client, db, objects, writer, tmp_path, document)
    block = reviewcontext.read_review_context(api_context, "eng", _run(PLAN_1181))
    assert block is not None
    siblings = _section(block, "SIBLINGS")
    # The new `_audited` is paired with the method that already does what it
    # does -- read live_audit and sort it -- by file and line, to be read.
    line = next(x for x in siblings.splitlines() if "`LiveService.audit_entries`" in x)
    assert "`LiveService._audited`" in line and f"`{LIVE}:1`" in line
    assert "new" in line  # the plan's symbol is not in the index yet
    # Told what to compare, and that a difference with no reason is a finding.
    assert "compare" in siblings and "findings" in siblings
    # A method in the same file that does something else is not a sibling.
    assert "admit" not in siblings
    assert len(block.encode()) <= reviewcontext.MAX_REVIEW_BYTES


SORT_KEY = "src/widgets/sort.py#_key_of"
RANK = "src/widgets/sort.py#rank_widgets"
BANNER = "src/widgets/sort.py#banner"


def test_a_sibling_of_an_indexed_symbol_is_found_by_what_both_call(
        api_context, client, db, objects, writer, tmp_path):
    document = _document([SORT, SORT_KEY, RANK, BANNER, LIST],
                         [(SORT, SORT_KEY), (RANK, SORT_KEY), (LIST, SORT)])
    _index(client, db, objects, writer, tmp_path, document)
    plan = {"summary": "Sort widgets by the requested key.",
            "steps": [{"step_id": "s", "title": "Honour the key",
                       "prompt": "Make sort_widgets use the key.",
                       "files": ["src/widgets/sort.py"]}]}
    block = reviewcontext.read_review_context(api_context, "eng", _run(plan))
    assert block is not None
    siblings = _section(block, "SIBLINGS")
    line = next(x for x in siblings.splitlines() if "`rank_widgets`" in x)
    assert "`sort_widgets`" in line and "`_key_of`" in line  # what both call
    assert "banner" not in siblings
    assert "new" not in line  # sort_widgets is in the index


def test_a_plan_with_nothing_beside_it_adds_nothing_to_the_review(
        api_context, client, db, objects, writer, tmp_path, monkeypatch):
    """The control: one function alone in its file, nobody calling it."""
    _index(client, db, objects, writer, tmp_path, graph_doc(writer))
    plan = {"summary": "Charge per order.",
            "steps": [{"step_id": "s", "title": "Bill it",
                       "prompt": "Make `charge_order` round to the cent.",
                       "files": ["src/billing/invoice.py"]}]}
    block = reviewcontext.read_review_context(api_context, "eng", _run(plan))
    assert block is not None and "SIBLINGS" not in block
    assert CHARGE not in block.split("CALL SITES", 1)[1].split("TESTS", 1)[0]
    monkeypatch.setattr(reviewcontext, "sibling_lines", lambda *_a, **_k: [])
    assert reviewcontext.read_review_context(api_context, "eng", _run(plan)) == block


# --------------------------------------------------------------------------
# the fixer's EARLIER FIXES: the second time-bomb fixer is shown the first fix
# --------------------------------------------------------------------------

WINDOW = "src/billing/window.py#window_open"
BOMB = "tests/billing/test_window.py#test_window_opens"
OTHER = "tests/billing/test_rates.py#test_rate"
FIRST_FIX = sha("first fix")


def _history(test_fixes: list[dict]) -> dict:
    return {"layers": {"test_fixes": test_fixes, "cannot_push": []},
            "meta": {"available": True, "reason": None}}


TIME_BOMB = _history([
    {"test": "tests/billing/test_window.py", "sha": FIRST_FIX, "pr": 1201,
     "subject": "The window fixture is dated from now, not from 2026-10-01 (#1201)",
     "date": "2026-10-11T09:00:00Z", "why": "fix subject", "fix_subjects": [],
     "files": ["tests/billing/test_window.py"]},
    {"test": "tests/billing/test_rates.py", "sha": sha("rates"), "pr": 1190,
     "subject": "Rates round half up (#1190)", "date": "2026-10-10T09:00:00Z",
     "why": "fix round", "fix_subjects": [], "files": ["tests/billing/test_rates.py"]},
])
BOMB_EXCERPT = ("CI is red at aaaaaaaaaaaa: unit\n## unit (failure)\n"
                "FAILED tests/billing/test_window.py::test_window_opens - AssertionError: "
                "the window closed on 2026-11-01")
WINDOW_PLAN = {"summary": "Bill per window.",
               "steps": [{"step_id": "s", "title": "Window", "prompt": "Bill per window.",
                          "files": ["src/billing/window.py"]}]}


def test_the_second_time_bomb_fixer_is_shown_the_first_fix(api_context, client, db, objects,
                                                           writer, tmp_path):
    document = _document([WINDOW, BOMB, OTHER], [(BOMB, WINDOW)], [(WINDOW, BOMB)])
    _index(client, db, objects, writer, tmp_path, document, TIME_BOMB)
    block = reviewcontext.read_fix_context(api_context, "eng", _run(WINDOW_PLAN),
                                           BOMB_EXCERPT)
    assert block is not None
    fixes = _section(block, "EARLIER FIXES")
    assert "#1201" in fixes and FIRST_FIX[:12] in fixes
    assert "dated from now, not from 2026-10-01" in fixes
    assert "`tests/billing/test_window.py`" in fixes
    # Another test's fix is not this test's.
    assert "#1190" not in block
    # Before the per-test lines, so the allowance never cuts it first.
    assert block.index("EARLIER FIXES") < block.index("test_window_opens` exercises")
    assert len(block.encode()) <= reviewcontext.MAX_FIX_BYTES
    # The round's prompt carries it, and asks for the earlier commit to be read first.
    round_ = SimpleNamespace(issue=parse_issue_ref(REF), pr_task_id="t1", pull_request={},
                             fix_rounds=3, id="r1")
    prompt = issueci.ci_fix_workflow(round_, 2, BOMB_EXCERPT, "a" * 40,
                                     block).model_dump()["steps"][0]["input"]["prompt"]
    assert "#1201" in prompt and "git show" in prompt


def test_no_earlier_fix_asks_for_none(api_context, client, db, objects, writer, tmp_path):
    document = _document([WINDOW, BOMB, OTHER], [(BOMB, WINDOW)], [(WINDOW, BOMB)])
    _index(client, db, objects, writer, tmp_path, document, _history([]))
    block = reviewcontext.read_fix_context(api_context, "eng", _run(WINDOW_PLAN),
                                           BOMB_EXCERPT)
    assert block is not None and "EARLIER FIXES" not in block
    round_ = SimpleNamespace(issue=parse_issue_ref(REF), pr_task_id="t1", pull_request={},
                             fix_rounds=3, id="r1")
    prompt = issueci.ci_fix_workflow(round_, 1, BOMB_EXCERPT, "a" * 40,
                                     block).model_dump()["steps"][0]["input"]["prompt"]
    assert "git show" not in prompt


# --------------------------------------------------------------------------
# the fixer's view outside the plan's files
# --------------------------------------------------------------------------

OTHER_SORT_TEST = "tests/widgets/test_list.py#test_list_sorted"


def test_the_fixer_is_told_who_calls_the_planned_code_from_outside_the_plan(
        api_context, client, db, objects, writer, tmp_path):
    test = "tests/widgets/test_sort.py#test_sort_widgets"
    document = _document([SORT, LIST, test, OTHER_SORT_TEST],
                         [(LIST, SORT), (test, SORT)],
                         [(SORT, test), (SORT, OTHER_SORT_TEST)])
    _index(client, db, objects, writer, tmp_path, document)
    plan = {"summary": "s", "steps": [{"step_id": "s", "title": "t", "prompt": "p",
                                       "files": ["src/widgets/sort.py"]}]}
    excerpt = "FAILED tests/widgets/test_sort.py::test_sort_widgets - AssertionError"
    block = reviewcontext.read_fix_context(api_context, "eng", _run(plan), excerpt)
    assert block is not None
    outside = _section(block, "OUTSIDE THE PLAN")
    assert f"`{SORT}`" in outside
    assert "`src/widgets/list.py`" in outside  # the caller the plan did not declare
    assert "`tests/widgets/test_list.py`" in outside  # another test that covers it
    assert "tests/widgets/test_sort.py" not in outside  # the red test itself


def test_a_red_test_with_nothing_outside_the_plan_adds_nothing(
        api_context, client, db, objects, writer, tmp_path, monkeypatch):
    """The control: every caller is planned, no other test, no earlier fix."""
    _index(client, db, objects, writer, tmp_path, graph_doc(writer), _history([]))
    plan = {"summary": "s", "steps": [{"step_id": "s", "title": "t", "prompt": "p",
                                       "files": ["src/widgets/sort.py",
                                                 "src/widgets/list.py"]}]}
    excerpt = "FAILED tests/widgets/test_sort.py::test_sort_widgets - AssertionError"
    block = reviewcontext.read_fix_context(api_context, "eng", _run(plan), excerpt)
    assert block is not None
    assert "OUTSIDE THE PLAN" not in block and "EARLIER FIXES" not in block
    monkeypatch.setattr(reviewcontext, "earlier_fixes", lambda *_a, **_k: {})
    monkeypatch.setattr(reviewcontext, "outside_lines", lambda *_a, **_k: [])
    again = reviewcontext.read_fix_context(api_context, "eng", _run(plan), excerpt)

    def nonce(text: str) -> str:
        return re.sub(r"TESTED CODE [0-9a-f]{16}", "TESTED CODE n", text)

    assert again is not None and nonce(again) == nonce(block)


def test_an_index_without_context_layers_still_gives_the_tested_code(
        api_context, client, db, objects, writer, tmp_path):
    """A graph from before CTXQ-H has no `context_shards`: no earlier fixes, no error."""
    _index(client, db, objects, writer, tmp_path, graph_doc(writer), None)
    excerpt = "FAILED tests/widgets/test_sort.py::test_sort_widgets - AssertionError"
    block = reviewcontext.read_fix_context(api_context, "eng", _run(None), excerpt)
    assert block is not None and f"`{SORT}`" in block and "EARLIER FIXES" not in block
    assert LIST not in block  # no plan: nothing is "outside" it
