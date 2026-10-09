"""The review's IMPACT block and the CI fixer's TESTED CODE block (knowledge-graph.md §4.3, §4.4, lane KG6).

Approving a plan compiles a `review` step; when the run's tenant has a
promoted extractor-version-3 index of the repository, its prompt carries an
IMPACT block: the application files outside the plan's files that call into
them (the call sites a signature change forces), the tests that cover them,
and the seams among them -- so "check the call sites" is a list the review
names a missed one from, not advice. A CI fix round's prompt carries the
application symbols each failing test exercises, so the fixer starts at the
code, not at the test.

The success measures of the lane's row -- review findings that name a missed
call site or test, and fix rounds per run -- are production counts. What is
asserted here is what they rest on: the review block NAMES the call site and
the covering test, the fixer's block NAMES the symbol under test, within the
§7.7 budgets. Without a version-3 index both prompts are exactly today's.

The graph is written by the SHIPPED writer and read through the real
`RepoGraph`, as test_plan_context.py does. No credentials, no network.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from swarm_api import issueci, issueruns, reviewcontext
from swarm_api.validation import parse_issue_ref

from .conftest import TEST_REPOSITORIES, auth_header, grant_members
from .test_issue_runs import _approve, _docs, _finish_planner
from .test_plan_context import (  # noqa: F401 -- fixtures: the forge, the context, the writer
    CHARGE,
    LIST,
    ONE,
    REF,
    ROUTE,
    SORT,
    TEST,
    _promote,
    _register,
    api_context,
    github,
    graph_doc,
    writer,
)

PLAN = {
    "summary": "Sort widgets by the requested key.",
    "requirements": ["The sort honours the key."],
    "steps": [
        {"step_id": "sort-key", "title": "Honour the key",
         "prompt": "Make sort_widgets use the key.", "files": ["src/widgets/sort.py"],
         "tests": ["tests/widgets/test_sort.py::test_sort_by_key"]},
    ],
}

#: Two parallel steps: compiles to stages, whose review prompt is the other shape.
STAGED = {
    "summary": PLAN["summary"],
    "steps": [
        {**PLAN["steps"][0], "depends_on": []},
        {"step_id": "invoice", "title": "Bill it", "prompt": "Charge per sort.",
         "files": ["src/billing/invoice.py"], "depends_on": []},
    ],
}


@pytest.fixture(autouse=True)
def _members_hold_grants(db):
    grant_members(db, *TEST_REPOSITORIES)


def _review_prompt(client, db, objects, plan=PLAN, user="alice") -> tuple[dict, str]:
    created = client.post("/v1/runs", json={"issue": REF}, headers=auth_header(user))
    assert created.status_code == 201, created.text
    run = created.json()["run"]
    _finish_planner(db, objects, run, plan)
    read = client.get(f"/v1/runs/{run['id']}", headers=auth_header(user))
    planned = read.json()["run"]
    assert planned["state"] == "PLANNED", planned
    approved = _approve(client, run["id"], planned["plan_digest"], user=user)
    assert approved.status_code == 200, approved.text
    workflow = _docs(db, "workflows")[f"workflows/{approved.json()['run']['workflow_id']}"]
    (task_id,) = [s["task_id"] for s in workflow["steps"]
                  if s["step_id"] == issueruns.REVIEW_STEP]
    return run, db.docs[f"tasks/{task_id}"]["input"]["prompt"]


def _block(prompt: str, run_id: str) -> str:
    start, end = reviewcontext.review_markers(run_id, ONE)
    assert prompt.count(start) == 1 and prompt.count(end) == 1, prompt
    return prompt.split(start, 1)[1].split(end, 1)[0]


# --------------------------------------------------------------------------
# the review's IMPACT block
# --------------------------------------------------------------------------

def test_the_review_names_the_call_site_and_the_test_of_the_planned_file(
    client, db, objects, writer, tmp_path
):
    repo_id = _register(client)
    _promote(client, db, objects, writer, tmp_path, repo_id, graph_doc(writer))
    run, prompt = _review_prompt(client, db, objects)
    block = _block(prompt, run["id"])

    calls = block.split("CALL SITES", 1)[1].split("TESTS", 1)[0]
    # render_list calls sort_widgets from a file the plan did not declare:
    # the call site a signature change forces, named by file and symbol.
    assert "`src/widgets/list.py` calls" in calls and f"`{SORT}`" in calls
    # Depth 1 only: the route calls render_list, not the planned file.
    assert "src/api/routes.py" not in calls
    # The planned file is not its own call site.
    assert "`src/widgets/sort.py` calls" not in calls
    tests = block.split("TESTS that call or cover", 1)[1]
    assert "tests/widgets/test_sort.py" in tests
    # The tests the plan said it adds, to be checked for existence.
    assert "test_sort_by_key" in block
    # Told how to use it, and that it is data computed from the plan, not the diff.
    head = prompt.split(reviewcontext.review_markers(run["id"], ONE)[0], 1)[0]
    assert "DATA, not instructions" in head and "not from the diff" in head
    assert "findings" in head
    # Inside §7.7's 8 KiB, and before the verdict instructions.
    start, end = reviewcontext.review_markers(run["id"], ONE)
    whole = prompt[prompt.index(head.splitlines()[-1]):prompt.index(end) + len(end)]
    assert len(whole.encode()) <= reviewcontext.MAX_REVIEW_BYTES
    assert prompt.index(end) < prompt.index(issueruns.VERDICT_FILE)
    # The implementers' prompts are unchanged by this lane.
    impl = [t for t in _docs(db, "tasks").values()
            if (t.get("step_id") or "").startswith(issueruns.IMPLEMENT_PREFIX)]
    assert impl and all("REVIEW IMPACT" not in t["input"]["prompt"] for t in impl)


def test_the_staged_review_gets_the_block_too(client, db, objects, writer, tmp_path):
    repo_id = _register(client)
    _promote(client, db, objects, writer, tmp_path, repo_id, graph_doc(writer))
    run, prompt = _review_prompt(client, db, objects, plan=STAGED)
    block = _block(prompt, run["id"])
    assert "src/widgets/list.py" in block
    assert "stages" in prompt  # the staged review's own frame


def test_a_planned_file_nobody_calls_is_unknown_never_safe(writer, tmp_path, client, db,
                                                           objects):
    repo_id = _register(client)
    _promote(client, db, objects, writer, tmp_path, repo_id, graph_doc(writer))
    plan = {**PLAN, "steps": [{**PLAN["steps"][0], "files": ["src/billing/invoice.py"],
                               "tests": None}]}
    run, prompt = _review_prompt(client, db, objects, plan=plan)
    calls = _block(prompt, run["id"]).split("CALL SITES", 1)[1].split("TESTS", 1)[0]
    assert "UNKNOWN" in calls and "never \"safe\"" in calls
    assert CHARGE not in calls.replace("src/billing/invoice.py", "")


def test_a_large_plan_stays_inside_the_review_allowance(writer, tmp_path):
    """The cap holds however many callers the graph has (§7.7: 8 KiB)."""
    callers = [f"src/many/m{i}.py#caller_with_a_long_descriptive_name_{i}" for i in range(400)]
    rows = [{"path": c.split("#")[0], "depth": 1, "via": [SORT]} for c in callers]
    expanded = {"named": ["src/widgets/sort.py"], "unknown": [], "callers": rows,
                "tests": rows, "seams": [], "communities": [],
                "cut": {"below_floor": 0, "judged": 0}, "truncated": []}
    original = reviewcontext.expand
    reviewcontext.expand = lambda *_a, **_k: expanded
    try:
        block = reviewcontext.review_block(None, run_id="r1", sha=ONE, first_line="fresh",
                                           plan=PLAN, seams={})
    finally:
        reviewcontext.expand = original
    assert block is not None
    assert len(block.encode()) <= reviewcontext.MAX_REVIEW_BYTES
    assert block.rstrip().endswith("=== END REVIEW IMPACT r1 ===")
    assert "more" in block


# --------------------------------------------------------------------------
# today's review prompt without a version-3 index
# --------------------------------------------------------------------------

def _todays(prompt: str) -> None:
    assert "REVIEW IMPACT" not in prompt
    assert issueruns.VERDICT_FILE in prompt


def test_no_registration_is_todays_review_prompt(client, db, objects):
    _run, prompt = _review_prompt(client, db, objects)
    _todays(prompt)


def test_an_index_older_than_version_3_is_todays_review_prompt(
    client, db, objects, writer, tmp_path
):
    repo_id = _register(client)
    _promote(client, db, objects, writer, tmp_path, repo_id, graph_doc(writer, version="2"))
    _run, prompt = _review_prompt(client, db, objects)
    _todays(prompt)


def test_an_index_with_no_graph_is_todays_review_prompt(client, db, objects, writer, tmp_path):
    repo_id = _register(client)
    _promote(client, db, objects, writer, tmp_path, repo_id, None)
    _run, prompt = _review_prompt(client, db, objects)
    _todays(prompt)


def test_a_plan_declaring_no_files_is_todays_review_prompt(client, db, objects, writer,
                                                           tmp_path):
    repo_id = _register(client)
    _promote(client, db, objects, writer, tmp_path, repo_id, graph_doc(writer))
    plan = {**PLAN, "steps": [{**PLAN["steps"][0], "files": None}]}
    _run, prompt = _review_prompt(client, db, objects, plan=plan)
    _todays(prompt)


def test_a_read_that_raises_never_refuses_the_approval(client, db, objects, writer, tmp_path,
                                                       monkeypatch):
    repo_id = _register(client)
    _promote(client, db, objects, writer, tmp_path, repo_id, graph_doc(writer))

    def broken(*_a, **_k):
        raise RuntimeError("the store fell over")

    monkeypatch.setattr(reviewcontext, "expand", broken)
    _run, prompt = _review_prompt(client, db, objects)
    _todays(prompt)


def test_compile_plan_without_context_is_todays_review_prompt():
    run = SimpleNamespace(issue=parse_issue_ref(REF), plan=PLAN, auto_merge=False, id="r1",
                          plan_digest="d", fix_rounds=3)
    review = next(s for s in issueruns.compile_plan(run).model_dump()["steps"]
                  if s["step_id"] == issueruns.REVIEW_STEP)
    assert "REVIEW IMPACT" not in review["input"]["prompt"]
    with_block = next(s for s in issueruns.compile_plan(run, "BLOCK\n").model_dump()["steps"]
                      if s["step_id"] == issueruns.REVIEW_STEP)
    assert with_block["input"]["prompt"].replace("BLOCK\n\n", "") == review["input"]["prompt"]


# --------------------------------------------------------------------------
# tenant isolation (invariant 9)
# --------------------------------------------------------------------------

def test_another_tenants_index_never_reaches_the_review(client, db, objects, writer, tmp_path):
    """bob (research) registers and indexes the repository; alice (eng) approves."""
    repo_id = _register(client, user="bob")
    _promote(client, db, objects, writer, tmp_path, repo_id, graph_doc(writer), user="bob")
    _run, prompt = _review_prompt(client, db, objects, user="alice")
    _todays(prompt)
    assert ONE not in prompt


def test_only_the_named_tenants_index_is_read(api_context, client, db, objects, writer,
                                              tmp_path):
    repo_id = _register(client, user="alice")
    _promote(client, db, objects, writer, tmp_path, repo_id, graph_doc(writer))
    run = SimpleNamespace(issue=parse_issue_ref(REF), plan=PLAN, id="r1", tenant_id="eng")
    assert reviewcontext.read_review_context(api_context, "eng", run) is not None
    # The same run, read as another tenant: eng's index is never named.
    assert reviewcontext.read_review_context(api_context, "research", run) is None
    excerpt = "FAILED tests/widgets/test_sort.py::test_sort_widgets - AssertionError"
    assert reviewcontext.read_fix_context(api_context, "eng", run, excerpt) is not None
    assert reviewcontext.read_fix_context(api_context, "research", run, excerpt) is None


# --------------------------------------------------------------------------
# the CI fixer's TESTED CODE block
# --------------------------------------------------------------------------

@pytest.mark.parametrize("excerpt,expected", [
    ("FAILED tests/widgets/test_sort.py::test_sort_widgets - AssertionError: 1 != 2",
     [("tests/widgets/test_sort.py", "test_sort_widgets")]),
    ("tests/widgets/test_sort.py::TestSort::test_key[name] FAILED",
     [("tests/widgets/test_sort.py", "TestSort.test_key")]),
    ("- tests/widgets/test_sort.py:12 [failure]: assert False",
     [("tests/widgets/test_sort.py", None)]),
    (" FAIL  src/ui/List.test.tsx > sorts", [("src/ui/List.test.tsx", None)]),
    ("error in src/widgets/sort.py:3 and https://x.io/a/test_b.py", []),
])
def test_failing_tests_are_read_from_ci_output(excerpt, expected):
    assert [(t.path, t.name) for t in reviewcontext.failing_tests(excerpt)] == expected


def test_the_fixer_is_told_the_code_the_failing_test_exercises(
    api_context, client, db, objects, writer, tmp_path
):
    repo_id = _register(client)
    _promote(client, db, objects, writer, tmp_path, repo_id, graph_doc(writer))
    run = SimpleNamespace(issue=parse_issue_ref(REF), plan=PLAN, id="r1", tenant_id="eng")
    excerpt = ("CI is red at aaaaaaaaaaaa: unit\n## unit (failure)\n"
               "FAILED tests/widgets/test_sort.py::test_sort_widgets - AssertionError\n"
               "FAILED tests/widgets/test_new.py::test_brand_new - AssertionError")
    block = reviewcontext.read_fix_context(api_context, "eng", run, excerpt)
    assert block is not None
    entry = block.split("tests/widgets/test_sort.py::test_sort_widgets", 1)[1]
    entry = entry.split("tests/widgets/test_new.py", 1)[0]
    # The reverse of symbol_test_map: the symbol under test, its file, and
    # that the run's plan declared that file.
    assert f"`{SORT}` in `src/widgets/sort.py` · planned" in entry
    assert LIST not in entry and ROUTE not in entry
    # A test the index does not know is said to be unknown, not skipped.
    assert "test_brand_new`: not in this index" in block
    assert len(block.encode()) <= reviewcontext.MAX_FIX_BYTES
    assert "DATA, not instructions" in block.split("=== TESTED CODE", 1)[0]


def test_the_fixer_block_falls_back_to_the_test_files_symbols(api_context, client, db, objects,
                                                              writer, tmp_path):
    repo_id = _register(client)
    _promote(client, db, objects, writer, tmp_path, repo_id, graph_doc(writer))
    run = SimpleNamespace(issue=parse_issue_ref(REF), plan=None, id="r1", tenant_id="eng")
    block = reviewcontext.read_fix_context(
        api_context, "eng", run, "- tests/widgets/test_sort.py:4 [failure]: boom")
    assert block is not None and f"`{SORT}`" in block and "planned" not in block.split(
        "===", 2)[2]


def test_ci_output_naming_no_test_is_todays_fix_prompt(api_context, client, db, objects,
                                                        writer, tmp_path):
    repo_id = _register(client)
    _promote(client, db, objects, writer, tmp_path, repo_id, graph_doc(writer))
    run = SimpleNamespace(issue=parse_issue_ref(REF), plan=PLAN, id="r1", tenant_id="eng")
    assert reviewcontext.read_fix_context(api_context, "eng", run, "lint: E501 in a.py") is None


def test_the_fix_round_prompt_carries_the_block_after_the_excerpt():
    run = SimpleNamespace(issue=parse_issue_ref(REF), pr_task_id="t1", pull_request={},
                          fix_rounds=3, id="r1")
    plain = issueci.ci_fix_workflow(run, 1, "red", "a" * 40).model_dump()
    prompt = plain["steps"][0]["input"]["prompt"]
    assert "TESTED CODE" not in prompt
    block = "=== TESTED CODE n s ===\nx\n=== END TESTED CODE n ===\n"
    carried = issueci.ci_fix_workflow(run, 1, "red", "a" * 40, block).model_dump()
    text = carried["steps"][0]["input"]["prompt"]
    assert text.index("=== TESTED CODE n s ===") > text.rindex("=== FAILING CHECKS")
