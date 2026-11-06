"""The graph's answer leads the pack, and one brief line names the graph's
lookup (BGD's pick of 6 November 2026 after the 48-hour code intelligence
report; part of #2688).

The report: 62 of 903 steps that had the graph ever called it; when a step
did, the graph named a file the step then changed in 35 of 56 answers. So:

1. a delivered graph's answer for the step's brief -- the files it ranks most
   relevant, each with its one-line reason -- leads CONTEXT.md, inside its
   16 KiB bound, before the local scores; never displacing the never-cut rows;
2. an undelivered answer adds nothing, and the pack says why;
3. pack.json (and the artifact) records whether the pack carried graph files
   and how many, so the next report tells use from delivery;
4. an implement, review or fix step that has the graph gets ONE brief line
   naming the graph tool for "where is this symbol defined or used"; a step
   without the graph, or of another type, gets none.

The fixture is test_contextpack's: `repo_index_fixtures.PYTHON_APP` and its
snapshot, built by the indexer. Offline: no CLI, no cloud, no emulator.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import test_agents_use_tools as runner_harness
from agent_worker import contextpack, graphstage
from agent_worker.contextpack import answer as answer_mod
from agent_worker.contextpack.render import render_markdown
from agent_worker.contextpack.retrieve import Candidate
from agent_worker.runners import claude_code, codex

from test_agents_use_tools import _graph_env, _run, _served_env
from test_contextpack import _ctx, _pack, _stage, built, work  # noqa: F401 -- fixtures

BRIEF = "Change how load_user normalises the users the store fetches."
SECTION = "## The graph's answer for this brief"


def _text(work: Path) -> str:  # noqa: F811
    return (work / contextpack.CONTEXT_DIR_NAME / contextpack.CONTEXT_FILE).read_text()


# --- 1. the graph's answer leads the pack ----------------------------------------

def test_a_delivered_graph_s_top_files_lead_the_pack_before_the_local_scores(work):  # noqa: F811
    pack = _pack(work, {"prompt": BRIEF})
    text = _text(work)
    # MUTATION: render without the section and this is red.
    assert SECTION in text
    assert text.index(SECTION) < text.index("## Never cut") < text.index("## Ranked context")
    files = pack["graph_answer"]["files"]
    assert files and files[0]["path"] == "src/pkg/users.py"
    section = text[text.index(SECTION):text.index("## Never cut")]
    listed = [line for line in section.splitlines() if line.startswith("- ")]
    # Best first, each with the graph's one-line reason naming what matched.
    assert [line.split("`")[1] for line in listed] == [f["path"] for f in files]
    assert "load_user" in listed[0] and "swarm-graph `search`" in section
    for line in listed:
        assert "\n" not in line and len(line.encode()) <= 400


def test_the_answer_fits_the_bound_and_never_displaces_the_never_cut_rows(work):  # noqa: F811
    many = [{"title": f"Lane L{i}: work on the store", "ref": f"PR #{1000 + i}",
             "files": ["src/pkg/store.py"]} for i in range(60)]
    (work / contextpack.CONTEXT_DIR_NAME / contextpack.INFLIGHT_FILE).write_text(
        json.dumps({"rows": many}))
    _stage(work, hard_stop_paths=[f"area{i}/**" for i in range(40)])
    pack = _pack(work, {"prompt": BRIEF + " Files: src/pkg/store.py"})
    text = _text(work)
    assert len(text.encode()) <= contextpack.BUDGET_BYTES
    for glob in (f"area{i}/**" for i in range(40)):
        assert glob in text
    assert SECTION in text
    # What did not fit is counted, never silently cut.
    shown = pack["graph_answer"]["shown"]
    assert pack["graph_files"] == shown <= len(pack["graph_answer"]["files"])


def test_render_trims_the_answer_to_what_fits_and_says_how_many_more():
    header = {"task_type": "implement", "jev": {"state": "off", "reason": "x"},
              "freshness": {"index_sha": "a" * 40, "base_sha": "b" * 40, "behind_by": 0,
                            "changed_since_index": 0},
              "seeds": {"files": [f"src/seed{i:02}/" + "s" * 100 + ".py" for i in range(12)],
                        "tests": [], "symbols": [],
                        "unknown": [f"new{i}/" + "u" * 110 for i in range(8)]}}
    # A never-cut section just under its own cap, in full.
    always = [Candidate(kind="cannot_push", ref=f"zone{i:03}/" + "x" * 90 + "/**",
                        path=f"zone{i:03}", why="a human must approve " + "y" * 80)
              for i in range(46)]
    graph = answer_mod.GraphAnswer(files=[
        {"path": f"src/mod{i:02}/" + "p" * 140 + ".py", "score": 10.0 - i,
         "why": "defines `thing` " + "w" * 250} for i in range(answer_mod.GRAPH_FILES)])
    text, _shown, _paged = render_markdown(header, always, [], 0, answer=graph)
    assert len(text.encode()) <= contextpack.BUDGET_BYTES
    for cand in always:
        assert cand.ref in text and cand.why in text
    assert 0 < graph.shown < answer_mod.GRAPH_FILES
    assert f"{answer_mod.GRAPH_FILES - graph.shown} more in pack.json" in text


# --- 2. an undelivered answer adds nothing, and says why ---------------------------

def test_a_brief_the_graph_cannot_answer_adds_no_file_and_the_pack_says_why(work):  # noqa: F811
    pack = _pack(work, {"prompt": "Qwxzv plorf."})
    text = _text(work)
    section = text[text.index(SECTION):text.index("## Never cut")]
    assert not [line for line in section.splitlines() if line.startswith("- ")]
    assert "None:" in section and "matched none of the brief's terms" in section
    assert pack["graph_answer"] == {"carried": False, "files": [], "shown": 0,
                                    "reason": pack["graph_answer"]["reason"]}
    assert "matched none" in pack["graph_answer"]["reason"]
    assert pack["graph_files"] == 0


def test_a_graph_without_search_adds_no_file_and_the_pack_says_why(work, monkeypatch):  # noqa: F811
    from agent_worker.graphmcp.snapshot import Snapshot, SnapshotError

    def refuse(self, query, limit):
        raise SnapshotError("this snapshot has no term index (a format-2 graph)")

    monkeypatch.setattr(Snapshot, "search", refuse)
    pack = _pack(work, {"prompt": BRIEF})
    assert pack["graph_files"] == 0 and pack["graph_answer"]["carried"] is False
    assert "no term index" in pack["graph_answer"]["reason"]
    assert "no term index" in _text(work)


def test_an_undelivered_graph_is_one_line_saying_why_and_no_pack(tmp_path, monkeypatch):
    work_dir = tmp_path / "w"
    (work_dir / contextpack.CONTEXT_DIR_NAME).mkdir(parents=True)
    (work_dir / contextpack.CONTEXT_DIR_NAME / contextpack.STAGE_FILE).write_text(
        json.dumps({"staged": False, "reason": "the repository has no index yet"}))
    monkeypatch.delenv(contextpack.CONTEXT_ENV, raising=False)
    ctx = _ctx(work_dir, tmp_path, {"prompt": BRIEF})
    line = contextpack.prepare(ctx, work_dir / "repo")
    assert line == ("No repository context was staged for this step: "
                    "the repository has no index yet.")
    assert not (work_dir / contextpack.CONTEXT_DIR_NAME / contextpack.CONTEXT_FILE).exists()
    assert not (ctx.artifacts_dir / contextpack.ARTIFACT_NAME).exists()


# --- 3. the record: carried or not, and how many ----------------------------------

def test_the_artifact_records_whether_the_pack_carried_graph_files(work, tmp_path):  # noqa: F811
    ctx = _ctx(work, tmp_path, {"prompt": BRIEF})
    assert contextpack.prepare(ctx, work / "repo")
    record = json.loads((ctx.artifacts_dir / contextpack.ARTIFACT_NAME).read_text())
    assert record["graph_answer"]["carried"] is True
    assert record["graph_files"] == record["graph_answer"]["shown"] > 0
    assert f"{record['graph_files']} graph files" in record["summary"]


def test_a_pack_that_failed_records_no_graph_files(work, tmp_path, monkeypatch):  # noqa: F811
    from agent_worker.contextpack import build

    def broken(*args, **kwargs):
        raise build.SnapshotError("broken")

    monkeypatch.setattr(build, "build_pack", broken)
    ctx = _ctx(work, tmp_path, {"prompt": BRIEF})
    contextpack.prepare(ctx, work / "repo")
    record = json.loads((ctx.artifacts_dir / contextpack.ARTIFACT_NAME).read_text())
    assert record["built"] is False and record["graph_files"] == 0


# --- 4. the brief line: only with the graph, only for implement, review and fix ---

REVIEW = {"prompt": "Review the change on this branch for GitHub issue o/r#7. Write "
                    "$SWARM_ARTIFACTS_DIR/verdict.json",
          "staged_inputs": [{"path": "swarm-work.patch"}]}
FIX = {"prompt": "Fix every finding in verdict.json for GitHub issue o/r#7.",
       "staged_inputs": [{"path": "verdict.json"}]}
PLAN = {"prompt": "Plan the work for GitHub issue o/r#7 (https://x/7)."}
DESIGN = {"prompt": "Design how users are cached."}
TESTS = {"prompt": "Write tests for load_user."}


@pytest.mark.parametrize("payload", [{"prompt": BRIEF}, REVIEW, FIX],
                         ids=["implement", "review", "fix"])
def test_implement_review_and_fix_get_the_line_naming_the_tool(tmp_path, payload):
    line = contextpack.lookup_line(payload, tmp_path, tool=contextpack.GRAPH_LOOKUP_TOOL)
    assert line is not None and "\n" not in line and len(line) < 300
    # An instruction with its moment, naming the tool.
    assert line.startswith("Before you search widely for where a symbol is defined or used")
    assert "swarm-graph `context`" in line


@pytest.mark.parametrize("payload", [PLAN, DESIGN, TESTS], ids=["plan", "design", "test"])
def test_other_step_types_get_no_line(tmp_path, payload):
    assert contextpack.lookup_line(payload, tmp_path, tool=contextpack.GRAPH_LOOKUP_TOOL) is None


@pytest.mark.parametrize("env_of", [_graph_env, _served_env], ids=["staged", "served"])
def test_a_claude_code_step_with_the_graph_gets_the_line_once(monkeypatch, tmp_path, env_of):
    seen = _run(monkeypatch, tmp_path, env_of(tmp_path))
    line = claude_code.graph_lookup_line({"prompt": runner_harness.PROMPT},
                                         tmp_path / "work", shape=graphstage.SHAPE_SKILL)
    assert line and f"`{claude_code.GRAPH_SKILL_NAME}` skill" in line
    # MUTATION: drop the line from `body` and this is red.
    assert seen["prompt"].count(line) == 1
    # The graph is named once: the exploration line keeps only its other clause.
    assert claude_code.EXPLORATION_SKILL_CLAUSE not in seen["prompt"]


def test_the_tools_shape_names_the_context_tool(monkeypatch, tmp_path):
    seen = _run(monkeypatch, tmp_path, {**_graph_env(tmp_path),
                                        graphstage.SHAPE_ENV: graphstage.SHAPE_TOOLS})
    line = claude_code.graph_lookup_line({"prompt": runner_harness.PROMPT},
                                         tmp_path / "work", shape=graphstage.SHAPE_TOOLS)
    assert line and "swarm-graph `context`" in line and line in seen["prompt"]
    assert claude_code.EXPLORATION_GRAPH_CLAUSE not in seen["prompt"]


def test_a_step_without_the_graph_gets_no_line(monkeypatch, tmp_path):
    seen = _run(monkeypatch, tmp_path, {})
    assert "Before you search widely" not in seen["prompt"]


def test_a_plan_step_with_the_graph_keeps_the_exploration_clause_and_gets_no_line(
        monkeypatch, tmp_path):
    monkeypatch.setattr(runner_harness, "PROMPT", PLAN["prompt"])
    seen = _run(monkeypatch, tmp_path, _graph_env(tmp_path))
    assert "Before you search widely" not in seen["prompt"]
    assert claude_code.EXPLORATION_SKILL_CLAUSE in seen["prompt"]


def test_a_codex_step_with_the_graph_gets_the_line(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_run(ctx, spec, *, extra_args=(), extra_env=None):
        captured["prompt"] = ctx.payload.get("prompt")
        return {"summary": "", "stdout_truncated": False, "metrics": {}}

    monkeypatch.setattr(codex, "run_cli_agent", fake_run)
    monkeypatch.setattr(codex, "plan_credential", lambda: None)
    monkeypatch.setattr(codex, "finish_log", lambda ctx, token=None: None)
    for name, value in _graph_env(tmp_path).items():
        monkeypatch.setenv(name, value)
    work = tmp_path / "work"
    (work / "repo").mkdir(parents=True)
    from agent_worker.runners.base import RunnerContext

    ctx = RunnerContext(work_dir=work, artifacts_dir=work / "artifacts",
                        input_path=work / "input.json", result_path=work / "result.json",
                        quota_path=work / "quota.json", payload={"prompt": BRIEF},
                        repo_dir=work / "repo")
    codex.body(ctx)
    line = contextpack.lookup_line({"prompt": BRIEF}, work, tool=contextpack.GRAPH_LOOKUP_TOOL)
    assert line and captured["prompt"].count(line) == 1
