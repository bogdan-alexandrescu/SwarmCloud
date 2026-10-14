"""The context pack: what a step is told about its repository before it starts
(docs/design/context-quality.md §4.3-§4.5; lane CTXQ-P).

The fixture is `repo_index_fixtures.PYTHON_APP` (a route calling `load_user`,
which calls `store.fetch`, with tests), committed with two later changes that
carry pull request numbers, a CLAUDE.md rule and a design doc that name
paths, and two guard tests that police `src/pkg/store.py` by naming it. Its
snapshot is built by KG2's own extractor and writer, so every assertion is
against the graph the indexer writes, laid out as the stager (CTXQ-S) stages
it: `work/.swarm-context/snapshot/`.

What the design's "Tests prove" column asks, each a test below:
  * per task type -- all six of implement, review, fix, test-writing, design
    and plan (owner decision 2026-10-14: every type in phase 1) -- the seeds
    produce the expected rows on the fixture snapshot;
  * in-flight and cannot-push rows are never cut, however much else competes;
  * the 16 KiB cap holds (owner decision Q2), and what it pages out is
    counted with the swarm-graph tool that pages it, never silently cut;
  * rows on a path changed since the index are marked stale;
  * CONTEXT.md is outside the checkout and the checkout stays clean.

MUTATIONS, each turning a named test red: drop the always-section in
`render.render_markdown` (never-cut); render ranked rows without the budget
check (cap); skip `_mark_stale` in `build` (stale); resolve the context
directory inside the checkout (outside); make `rank.select` ignore `MIN_PER_KIND`
(quotas); return "implement" first in `tasktype.classify` (per-type rows);
remove the prompt line from `run_cli_agent` (wiring).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import graphmcp_fixtures as gf
import pytest
import repo_index_fixtures as fx

from agent_worker import contextpack
from agent_worker.contextpack import checklists, rank, tasktype
from agent_worker.contextpack.build import build_pack
from agent_worker.contextpack.retrieve import Candidate

LOAD_USER = "src/pkg/users.py#load_user"
GET_USER = "src/pkg/users.py#get_user"
FETCH = "src/pkg/store.py#fetch"
TEST_LOAD = "tests/test_users.py#test_load_user"

FILES = {
    **fx.PYTHON_APP,
    "CLAUDE.md": (
        "# Working here\n\n"
        "## The store\n\n"
        "Every change to `src/pkg/store.py` keeps `fetch` free of network calls.\n"
    ),
    "docs/design/users.md": (
        "# Users\n\n"
        "## Loading and normalising users\n\n"
        "`src/pkg/users.py` loads users through the store and normalises each row.\n"
    ),
    "tests/test_store_guard.py": (
        "from pathlib import Path\n\n\n"
        "def test_store_makes_no_network_call():\n"
        "    text = Path(\"src/pkg/store.py\").read_text()\n"
        "    assert \"requests\" not in text\n"
    ),
    "tests/test_import_cheap.py": (
        "def test_store_imports_nothing_heavy():\n"
        "    assert \"src/pkg/store.py\"\n"
    ),
}

#: Two later commits, each a squash subject with its pull request number.
HISTORY = (
    ("store.fetch returns a copy (#101)", {
        "src/pkg/store.py": FILES["src/pkg/store.py"] + "\n\ndef copy(row):\n    return dict(row)\n",
        "tests/test_store_guard.py": FILES["tests/test_store_guard.py"] + "\n",
    }),
    ("Fix the flaky users fixture in test_users (#102)", {
        "tests/test_users.py": FILES["tests/test_users.py"] + "\n",
        "src/pkg/users.py": FILES["src/pkg/users.py"] + "\n",
    }),
)


def _commit(repo: Path, subject: str, changes: dict[str, str], when: int) -> None:
    env = fx.git_env(repo.parent / (repo.name + "-home"))
    fx.write_files(repo, changes)
    stamp = f"@{when} +0000"
    dated = {**env, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    subprocess.run(["git", "-C", str(repo), "add", "-A"], env=env, check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", subject], env=dated,
                   check=True, capture_output=True)


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> tuple[Path, Path]:
    """(a committed checkout with history, its staged snapshot)."""
    root = tmp_path_factory.mktemp("contextpack")
    repo = fx.build_repo(root / "repo", FILES)
    for n, (subject, changes) in enumerate(HISTORY, start=1):
        _commit(repo, subject, changes, 1_780_000_000 + n * 86_400)
    snapshot = gf.write_snapshot(gf.graph_of(repo), root / "snapshot")
    return repo, snapshot


@pytest.fixture
def work(built, tmp_path) -> Path:
    """`work/` with the checkout at `work/repo` and the snapshot staged beside it."""
    repo, snapshot = built
    work = tmp_path / "work"
    work.mkdir()
    shutil.copytree(repo, work / "repo", symlinks=True)
    shutil.copytree(snapshot, work / contextpack.CONTEXT_DIR_NAME / contextpack.SNAPSHOT_DIR)
    return work


def _stage(work: Path, **fields) -> None:
    path = work / contextpack.CONTEXT_DIR_NAME / contextpack.STAGE_FILE
    path.write_text(json.dumps(fields), encoding="utf-8")


def _pack(work: Path, payload: dict, **kwargs) -> dict:
    return build_pack(work / contextpack.CONTEXT_DIR_NAME, checkout=work / "repo",
                      work_dir=work, payload=payload, **kwargs)


def _rows(pack: dict, kind: str | None = None) -> list[dict]:
    return [r for r in pack["rows"] if kind is None or r["kind"] == kind]


def _refs(pack: dict, kind: str) -> set[str]:
    return {r["ref"] for r in _rows(pack, kind)}


def _step_prompt(files: list[str], tests: list[str] = ()) -> str:
    """An issue-run implementer step, as `issueruns.compile_plan` words it."""
    text = ("You are doing step 1 of 1 of the approved plan for GitHub issue o/r#7.\n\n"
            "This step -- make fetch return a copy:\nChange fetch.\n\n")
    if files:
        text += "Files this step is planned to touch: " + ", ".join(files) + "\n"
    if tests:
        text += "Tests this step adds -- write them first:\n" + "".join(
            f"- {t}\n" for t in tests)
    return text


# --- the task type ------------------------------------------------------------

def test_every_task_type_has_a_checklist_and_the_owner_s_five_are_all_there():
    """Owner decision 2026-10-14: all five types in phase 1 (fix and test-writing
    are one of the owner's five and two pipelines of the design's)."""
    assert set(contextpack.TASK_TYPES) == {"implement", "review", "fix", "test", "design",
                                           "plan"}
    for kind in contextpack.TASK_TYPES:
        items = checklists.checklist(kind)
        assert len(items) >= 3, kind
        assert all(isinstance(i, str) and i.strip() for i in items)
    assert any("fail without the change" in i for i in checklists.checklist("test"))
    assert any("requirement" in i for i in checklists.checklist("review"))
    assert any("requirement" in i for i in checklists.checklist("plan"))
    assert any("overlap" in i for i in checklists.checklist("plan"))
    assert any("earlier fix" in i for i in checklists.checklist("fix"))
    assert any("owner decision" in i for i in checklists.checklist("design"))
    assert any("cannot-push" in i for i in checklists.checklist("implement"))


def test_the_task_type_comes_from_the_step_s_role_never_a_guess(work):
    issue = {"prompt": "Plan the work for GitHub issue o/r#7 (https://x/7). The issue's "
                       "title, body and comments are in the issue file", "issue": 7}
    assert tasktype.classify(issue, work) == "plan"
    review = {"prompt": "Review the change on this branch for GitHub issue o/r#7. Write "
                        "$SWARM_ARTIFACTS_DIR/verdict.json", "staged_inputs":
              [{"path": "swarm-work.patch"}]}
    assert tasktype.classify(review, work) == "review"
    fix = {"prompt": "Fix every finding in verdict.json for GitHub issue o/r#7.",
           "staged_inputs": [{"path": "verdict.json"}]}
    assert tasktype.classify(fix, work) == "fix"
    ci = {"prompt": "PR #9 is red. This is CI fix round 1 of at most 2.\n"
                    "=== FAILING CHECKS 0a1b2c3d4e5f6a7b ===\nFAILED x\n"
                    "=== FAILING CHECKS 0a1b2c3d4e5f6a7b ===\n"}
    assert tasktype.classify(ci, work) == "fix"
    assert tasktype.classify({"prompt": "Write tests for load_user."}, work) == "test"
    assert tasktype.classify({"prompt": "Design how users are cached."}, work) == "design"
    assert tasktype.classify({"prompt": "Make fetch return a copy."}, work) == "implement"
    # The stager's stored decision outranks every inference; a value it does
    # not know is ignored rather than trusted.
    _stage(work, type="design")
    assert tasktype.classify({"prompt": "Make fetch return a copy."}, work) == "design"
    _stage(work, type="anything-else")
    assert tasktype.classify({"prompt": "Make fetch return a copy."}, work) == "implement"


# --- per type, the seeds produce the expected rows ------------------------------

def test_implement_lists_callers_two_hops_tests_guards_rules_history_and_cannot_push(work):
    pack = _pack(work, {"prompt": _step_prompt(["src/pkg/store.py"])})
    assert pack["task_type"] == "implement"
    assert pack["seeds"]["files"] == ["src/pkg/store.py"]
    callers = {r["ref"]: r["hops"] for r in _rows(pack, "caller")}
    assert callers[LOAD_USER] == 1 and callers[GET_USER] == 2
    assert {FETCH, "src/pkg/store.py#save"} <= _refs(pack, "symbol")
    tests = _refs(pack, "test")
    assert "tests/test_store.py" in tests and TEST_LOAD in tests
    assert {"tests/test_store_guard.py", "tests/test_import_cheap.py"} <= _refs(pack, "guard")
    assert any(r["path"] == "CLAUDE.md" and "The store" in r["ref"]
               for r in _rows(pack, "rule"))
    assert any(r.get("pr") == 101 for r in _rows(pack, "similar_pr"))
    assert ".github/workflows/**" in _refs(pack, "cannot_push")
    text = (work / contextpack.CONTEXT_DIR_NAME / contextpack.CONTEXT_FILE).read_text()
    assert LOAD_USER in text and "tests/test_store_guard.py" in text


def test_review_names_callers_outside_the_diff_and_the_sibling_idiom(work):
    (work / "swarm-work.patch").write_text(
        "diff --git a/src/pkg/store.py b/src/pkg/store.py\n"
        "--- a/src/pkg/store.py\n+++ b/src/pkg/store.py\n"
        "@@ -1,2 +1,2 @@\n def fetch(key):\n-    return {\"key\": key}\n"
        "+    return dict(key=key)\n", encoding="utf-8")
    payload = {"prompt": "Review the change on this branch for GitHub issue o/r#7 against "
                         "the approved plan. Write $SWARM_ARTIFACTS_DIR/verdict.json",
               "staged_inputs": [{"path": "swarm-work.patch"}]}
    pack = _pack(work, payload)
    assert pack["task_type"] == "review"
    assert pack["seeds"]["symbols"] == [FETCH]
    caller = next(r for r in _rows(pack, "caller") if r["ref"] == LOAD_USER)
    assert "outside the diff" in caller["why"]
    assert TEST_LOAD in _refs(pack, "test")
    assert any("sibling" in r["why"] for r in _rows(pack, "symbol"))


def test_a_ci_fix_is_given_the_red_test_s_code_and_the_earlier_fix_of_that_test(work):
    payload = {"prompt": ("PR #9 is red. This is CI fix round 1 of at most 2.\n"
                          "=== FAILING CHECKS 0a1b2c3d4e5f6a7b ===\n"
                          "FAILED tests/test_users.py::test_load_user - AssertionError\n"
                          "=== FAILING CHECKS 0a1b2c3d4e5f6a7b ===\n")}
    pack = _pack(work, payload)
    assert pack["task_type"] == "fix"
    assert pack["seeds"]["tests"] == ["tests/test_users.py"]
    tested = next(r for r in _rows(pack, "symbol") if r["ref"] == LOAD_USER)
    assert "red test" in tested["why"]
    earlier = next(r for r in _rows(pack, "similar_pr") if r.get("pr") == 102)
    assert earlier["fix"] is True


def test_a_review_fix_is_seeded_by_the_findings_files(work):
    (work / "verdict.json").write_text(json.dumps({
        "verdict": "NOT_YET",
        "findings": [{"summary": "s", "file": "src/pkg/users.py", "line": 9}]}))
    pack = _pack(work, {"prompt": "Fix every finding in verdict.json for issue o/r#7.",
                        "staged_inputs": [{"path": "verdict.json"}]})
    assert pack["task_type"] == "fix"
    assert pack["seeds"]["files"] == ["src/pkg/users.py"]
    assert LOAD_USER in _refs(pack, "symbol")


def test_test_writing_lists_the_existing_tests_and_the_sibling_test_layout(work):
    _stage(work, type="test")
    pack = _pack(work, {"prompt": "Add tests for load_user in src/pkg/users.py."})
    assert pack["task_type"] == "test"
    assert TEST_LOAD in _refs(pack, "test")
    assert any(r["path"] == "tests/test_store.py" and "layout" in r["why"]
               for r in _rows(pack, "test"))


def test_design_is_doc_heavy_from_the_brief_s_terms(work):
    pack = _pack(work, {"prompt": "Design how users are loaded and normalised."})
    assert pack["task_type"] == "design"
    assert pack["seeds"]["files"] == []
    assert LOAD_USER in _refs(pack, "symbol")
    assert any(r["path"] == "docs/design/users.md" for r in _rows(pack, "doc"))
    assert rank.QUOTAS["design"]["max"]["doc"] > rank.QUOTAS["implement"]["max"]["doc"]


def test_plan_lists_parallel_work_on_the_issue_s_files_and_past_changes(work):
    (work / "issue.md").write_text(
        "# o/r#7: load_user returns stale rows\n\n`src/pkg/store.py` caches rows.\n")
    (work / contextpack.CONTEXT_DIR_NAME / contextpack.INFLIGHT_FILE).write_text(json.dumps({
        "rows": [{"title": "Lane STORE-2: store cache", "ref": "PR #120",
                  "files": ["src/pkg/store.py"]},
                 {"title": "Unrelated", "ref": "PR #121", "files": ["lib/c.py"]}]}))
    pack = _pack(work, {"prompt": "Plan the work for GitHub issue o/r#7 (https://x/7).",
                        "issue": 7})
    assert pack["task_type"] == "plan"
    assert "src/pkg/store.py" in pack["seeds"]["files"]
    in_flight = _rows(pack, "in_flight")
    assert [r["ref"] for r in in_flight] == ["PR #120"]
    assert "src/pkg/store.py" in in_flight[0]["why"]
    assert LOAD_USER in _refs(pack, "caller")
    assert any(r.get("pr") == 101 for r in _rows(pack, "similar_pr"))


# --- never cut, the cap, quotas -------------------------------------------------

def test_in_flight_is_unknown_never_none_when_the_view_was_not_staged(work):
    pack = _pack(work, {"prompt": _step_prompt(["src/pkg/store.py"])})
    assert pack["in_flight"] == "UNKNOWN"
    text = (work / contextpack.CONTEXT_DIR_NAME / contextpack.CONTEXT_FILE).read_text()
    assert "UNKNOWN" in text and "nothing in flight" not in text.lower()


def test_in_flight_and_cannot_push_rows_are_never_cut_and_the_cap_holds(work, monkeypatch):
    many = [{"title": f"Lane L{i}: work on the store", "ref": f"PR #{1000 + i}",
             "files": ["src/pkg/store.py"]} for i in range(40)]
    (work / contextpack.CONTEXT_DIR_NAME / contextpack.INFLIGHT_FILE).write_text(
        json.dumps({"rows": many}))
    _stage(work, hard_stop_paths=["terraform/bootstrap/**", "CODEOWNERS"])
    # Swamp the ranked section: 400 guard tests and 60 docs naming the seed
    # path. Guards have no maximum, so only the byte budget can stop them.
    for i in range(400):
        (work / "repo" / "tests" / f"test_guard_{i:03}.py").write_text(
            f"def test_guard_{i:03}():\n    assert 'src/pkg/store.py'\n")
    for i in range(60):
        (work / "repo" / "docs" / f"note{i:03}.md").write_text(
            f"# Note {i}\n\n## Section\n\n`src/pkg/store.py`\n")
    subprocess.run(["git", "-C", str(work / "repo"), "add", "-A"], check=True,
                   capture_output=True)
    pack = _pack(work, {"prompt": _step_prompt(["src/pkg/store.py"])})
    path = work / contextpack.CONTEXT_DIR_NAME / contextpack.CONTEXT_FILE
    text = path.read_text()
    assert len(text.encode()) <= contextpack.BUDGET_BYTES == 16 * 1024
    for row in many:
        assert row["ref"] in text, row["ref"]
    for glob in (".github/workflows/**", "terraform/bootstrap/**", "CODEOWNERS"):
        assert glob in text
    # What did not fit is counted, with the tool that pages it.
    assert pack["paged_out"]["guard"] > 0 and pack["paged_out"]["doc"] > 0
    assert f"{pack['paged_out']['guard']} more guard rows: swarm-graph `rules_for`" in text
    # And pack.json keeps every row, ranked or not: nothing is silently cut.
    assert len(_rows(pack, "guard")) >= 400 and len(_rows(pack, "doc")) >= 60
    assert sum(1 for r in pack["rows"] if not r["shown"]) == sum(pack["paged_out"].values())


def test_quotas_keep_the_minimum_tests_and_guards_and_cap_docs_and_similar_prs():
    def cand(kind: str, n: int, score: float) -> list[Candidate]:
        return [Candidate(kind=kind, ref=f"{kind}{i}", path=f"{kind}/{i}.py", why="w",
                          score=score) for i in range(n)]

    pool = (cand("doc", 20, 9.0) + cand("similar_pr", 20, 8.0) + cand("caller", 30, 7.0)
            + cand("test", 5, 0.01) + cand("guard", 4, 0.01))
    chosen = rank.select(pool, "implement", budget_rows=12)
    kinds = [c.kind for c in chosen]
    assert kinds.count("test") >= 3 and kinds.count("guard") >= 2
    assert kinds.count("doc") <= 5 and kinds.count("similar_pr") <= 5
    assert len(chosen) == 12


def test_a_guard_names_a_path_by_itself_or_by_a_distinctive_base_name():
    """As `changed-guards.sh` selects: the path, or its base name, never a longer name."""
    from agent_worker.contextpack.retrieve import _needles

    needles = _needles(["apps/w/agent_worker/children.py", "src/pkg/__init__.py"])
    labels = [label for label, _ in needles]
    assert labels == ["apps/w/agent_worker/children.py", "children.py", "src/pkg/__init__.py"]
    base = dict(needles)["children.py"]
    assert base.search("reads agent_worker/children.py as text")
    assert base.search("`children.py`")
    assert not base.search("tests/test_children.py") and not base.search("children.pyc")


def test_the_local_score_is_the_design_s_weighted_sum_and_deterministic():
    near = Candidate(kind="caller", ref="a#f", path="a.py", why="w", hops=1, confidence=0.9)
    far = Candidate(kind="caller", ref="b#g", path="b.py", why="w", hops=2, confidence=0.9)
    assert rank.local_score(near) > rank.local_score(far)
    lsp = Candidate(kind="caller", ref="c#h", path="c.py", why="w", hops=1, confidence=0.9)
    naming = Candidate(kind="caller", ref="d#i", path="d.py", why="w", hops=1,
                       confidence=0.3)
    assert rank.local_score(lsp) > rank.local_score(naming)
    recent = Candidate(kind="similar_pr", ref="#1", path="x", why="w", age_days=0)
    old = Candidate(kind="similar_pr", ref="#2", path="x", why="w", age_days=28)
    assert rank.local_score(recent) - rank.local_score(old) == pytest.approx(
        rank.WEIGHTS["recency"] * 0.75)


def test_two_builds_of_the_same_step_write_the_same_pack(work):
    payload = {"prompt": _step_prompt(["src/pkg/store.py"])}
    first = _pack(work, payload)
    text = (work / contextpack.CONTEXT_DIR_NAME / contextpack.CONTEXT_FILE).read_text()
    assert _pack(work, payload) == first
    assert (work / contextpack.CONTEXT_DIR_NAME / contextpack.CONTEXT_FILE).read_text() == text


# --- stale, freshness, Jev ------------------------------------------------------

def test_rows_on_a_path_changed_since_the_index_are_marked_stale(work):
    snapshot = work / contextpack.CONTEXT_DIR_NAME / contextpack.SNAPSHOT_DIR
    sha = json.loads((snapshot / "manifest.json").read_text())["commit_sha"]
    (snapshot / "freshness.json").write_text(json.dumps({
        "index_sha": sha, "base_sha": "f" * 40, "behind_by": 3,
        "changed": ["src/pkg/users.py"]}))
    pack = _pack(work, {"prompt": _step_prompt(["src/pkg/store.py"])})
    assert pack["freshness"]["behind_by"] == 3
    load = next(r for r in _rows(pack, "caller") if r["ref"] == LOAD_USER)
    assert load["stale"] == "changed since the index"
    fetch = next(r for r in _rows(pack, "symbol") if r["ref"] == FETCH)
    assert "stale" not in fetch
    text = (work / contextpack.CONTEXT_DIR_NAME / contextpack.CONTEXT_FILE).read_text()
    assert "behind_by 3" in text and "STALE" in text


def test_the_pack_says_jev_is_off_and_why_and_spends_nothing(work):
    _stage(work, jev={"state": "off", "reason": "workspace not opted in"})
    pack = _pack(work, {"prompt": _step_prompt(["src/pkg/store.py"])})
    assert pack["jev"] == {"state": "off", "reason": "workspace not opted in"}
    assert pack["jev_calls"] == 0 and pack["jev_input_tokens"] == 0
    text = (work / contextpack.CONTEXT_DIR_NAME / contextpack.CONTEXT_FILE).read_text()
    assert "Jev: off (workspace not opted in)" in text


def test_the_summary_line_names_the_arm_rows_and_tokens(work):
    _stage(work, arm="B")
    pack = _pack(work, {"prompt": _step_prompt(["src/pkg/store.py"])})
    assert pack["arm"] == "B"
    assert pack["summary"].startswith("Context pack: arm B, ")
    assert " rows, " in pack["summary"] and pack["summary"].endswith(" tokens")


# --- outside the checkout, never committed ------------------------------------

def test_the_pack_is_outside_the_checkout_and_the_checkout_stays_clean(work):
    _pack(work, {"prompt": _step_prompt(["src/pkg/store.py"])})
    context = work / contextpack.CONTEXT_DIR_NAME
    for name in (contextpack.CONTEXT_FILE, contextpack.PACK_FILE):
        assert (context / name).is_file()
        assert not (context / name).resolve().is_relative_to((work / "repo").resolve())
    status = subprocess.run(["git", "-C", str(work / "repo"), "status", "--porcelain",
                             "--untracked-files=all"], capture_output=True, text=True,
                            check=True).stdout
    assert status == ""


def test_a_context_directory_inside_the_checkout_is_refused(work):
    inside = work / "repo" / contextpack.CONTEXT_DIR_NAME
    shutil.copytree(work / contextpack.CONTEXT_DIR_NAME, inside)
    with pytest.raises(contextpack.ContextPackError, match="inside the checkout"):
        build_pack(inside, checkout=work / "repo", work_dir=work,
                   payload={"prompt": _step_prompt(["src/pkg/store.py"])})
    assert not (inside / contextpack.CONTEXT_FILE).exists()


def test_a_link_planted_at_the_pack_s_name_is_replaced_never_written_through(work, tmp_path):
    target = tmp_path / "elsewhere.md"
    target.write_text("untouched")
    (work / contextpack.CONTEXT_DIR_NAME / contextpack.CONTEXT_FILE).symlink_to(target)
    _pack(work, {"prompt": _step_prompt(["src/pkg/store.py"])})
    assert target.read_text() == "untouched"
    assert not (work / contextpack.CONTEXT_DIR_NAME / contextpack.CONTEXT_FILE).is_symlink()


# --- the prompt line and the runner -------------------------------------------

def _ctx(work: Path, tmp_path: Path, payload: dict):
    from agent_worker.runners.base import RunnerContext

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(exist_ok=True)
    return RunnerContext(work_dir=work, artifacts_dir=artifacts,
                         input_path=work / "input.json", result_path=work / "result.json",
                         quota_path=work / "quota.json", payload=payload,
                         repo_dir=work / "repo")


def test_prepare_writes_the_pack_and_the_artifact_and_returns_the_one_line(
        work, tmp_path, monkeypatch):
    monkeypatch.delenv(contextpack.CONTEXT_ENV, raising=False)
    ctx = _ctx(work, tmp_path, {"prompt": _step_prompt(["src/pkg/store.py"])})
    line = contextpack.prepare(ctx, work / "repo")
    context_file = work / contextpack.CONTEXT_DIR_NAME / contextpack.CONTEXT_FILE
    assert line == contextpack.prompt_line(context_file)
    assert str(context_file) in line and "swarm-graph" in line and "\n" not in line
    artifact = json.loads((ctx.artifacts_dir / contextpack.ARTIFACT_NAME).read_text())
    assert artifact["task_type"] == "implement"


def test_no_staged_context_means_no_line(tmp_path, monkeypatch):
    monkeypatch.delenv(contextpack.CONTEXT_ENV, raising=False)
    work = tmp_path / "work"
    (work / "repo").mkdir(parents=True)
    assert contextpack.prepare(_ctx(work, tmp_path, {"prompt": "x"}), work / "repo") is None


def test_a_repository_the_stager_skipped_says_so_in_one_line(tmp_path, monkeypatch):
    monkeypatch.delenv(contextpack.CONTEXT_ENV, raising=False)
    work = tmp_path / "work"
    (work / "repo").mkdir(parents=True)
    (work / contextpack.CONTEXT_DIR_NAME).mkdir()
    _stage(work, staged=False, reason="the index is format 2\nand stale")
    line = contextpack.prepare(_ctx(work, tmp_path, {"prompt": "x"}), work / "repo")
    assert line is not None and "\n" not in line
    assert "No repository context was staged" in line and "format 2" in line


def test_a_broken_snapshot_never_fails_the_step(work, tmp_path, monkeypatch):
    monkeypatch.delenv(contextpack.CONTEXT_ENV, raising=False)
    snapshot = work / contextpack.CONTEXT_DIR_NAME / contextpack.SNAPSHOT_DIR
    (snapshot / "manifest.json").write_text("not json")
    line = contextpack.prepare(_ctx(work, tmp_path, {"prompt": "x"}), work / "repo")
    assert line is not None and "could not be built" in line


def test_the_cli_runner_appends_the_context_line_before_the_platform_s(
        work, tmp_path, monkeypatch):
    from agent_worker.runners import claude_code, cliagent

    cli = tmp_path / "fake-cli"
    cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['HOME'], 'stdin.txt').write_text(sys.stdin.read())\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,"
        " 'session_id': 's', 'result': 'done', 'total_cost_usd': 0.0, 'num_turns': 1}))\n")
    cli.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CODE_BIN", str(cli))
    for name in ("CLAUDE_CODE_ARGS", "CLAUDE_CODE_OAUTH_TOKEN", "MODEL", "SWARM_CHILDREN",
                 cliagent.ACCOUNT_STREAM_ENV, cliagent.RESUME_SESSION_ENV,
                 cliagent.RESUME_REASON_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "api03-" + os.urandom(16).hex())
    monkeypatch.setenv(contextpack.CONTEXT_ENV, str(work / contextpack.CONTEXT_DIR_NAME))
    ctx = _ctx(work, tmp_path, {"prompt": _step_prompt(["src/pkg/store.py"])})

    claude_code.body(ctx)

    stdin = (work / "stdin.txt").read_text()
    line = contextpack.prompt_line(work / contextpack.CONTEXT_DIR_NAME
                                   / contextpack.CONTEXT_FILE)
    assert line in stdin
    assert stdin.index(line) < stdin.index(cliagent.PUBLISH_PARAGRAPH)
