"""A review step is told what verdict.json must hold, and gets one turn to fix it (#3024).

MEASURED 2026-11-02 (task_3f1d937155f745c4a461): a review step owed
`verdict.json`; its prompt named the file and its path and nothing of its
shape, so the agent wrote `{"verdict": "changes_requested"}`. The runner's
repair turn checked only that the file existed, the worker refused it after
the session (`expected_outputs.verdict_problem`, cause `unreadable_verdict`),
and every retry started from the same prompt: three attempts and $9.79 lost.

Now `agent_instructions` says the shape and when each verdict applies,
wherever `verdict.json` is among the names it lists, and the runner's repair
loop reads a written verdict.json the way the worker does and gives ONE turn
(`VERDICT_REPAIR_MAX_TURNS`) naming the problem. The contract is unchanged: no
synonym reads as a verdict, so a file still refused after its turn fails the
attempt as it did.

The runner tests run the real claude-code runner against the fake CLI of
test_claude_repair_turns.py.
"""

from __future__ import annotations

import json

from agent_worker import expected_outputs, verdict
from agent_worker.runners import claude_code, cliagent
from test_claude_repair_turns import DONE, SESSION, _ctx, _install, _runs

NOT_A_VERDICT = json.dumps({"verdict": "changes_requested", "findings": ["x is wrong"]})
NOT_YET = json.dumps({"verdict": "NOT_YET", "findings": ["x is wrong"]})
MERGE = json.dumps({"verdict": "MERGE", "findings": []})


# ---------------------------------------------------------------------------
# What the prompt says
# ---------------------------------------------------------------------------


def test_a_review_steps_instructions_name_merge_and_not_yet(tmp_path):
    text = expected_outputs.agent_instructions(["verdict.json"], tmp_path)
    # MUTATION: drop the verdict line from `agent_instructions` and this is red.
    assert "MERGE" in text and "NOT_YET" in text
    assert verdict.EXPECTED_SHAPE in text
    assert expected_outputs.verdict_line() in text.splitlines()
    assert "when nothing must change" in text
    assert "when anything must change" in text
    assert "`findings`" in text


def test_the_shape_is_stated_once_from_the_review_verdicts():
    for word in verdict.REVIEW_VERDICTS:
        assert f'"{word}"' in verdict.EXPECTED_SHAPE
    assert verdict.EXPECTED_SHAPE == '{"verdict": "MERGE" | "NOT_YET", "findings": [...]}'


def test_the_verdict_line_carries_no_rate_limit_or_credential_marker():
    line = expected_outputs.verdict_line().lower()
    for marker in (*cliagent._RATE_LIMIT_MARKERS, *cliagent._CREDENTIAL_MARKERS):
        assert marker not in line, marker
    assert not cliagent._RATE_LIMIT_STATUS.search(line)


def test_the_verdict_line_follows_the_paths_wherever_verdict_json_is_listed(tmp_path):
    lines = expected_outputs.agent_instructions(
        ["notes.md", "verdict.json"], tmp_path, staged=("/w/scan-01.md",)
    ).splitlines()
    at = lines.index(expected_outputs.verdict_line())
    assert lines[at - 1].endswith("/verdict.json")
    assert lines.count(expected_outputs.verdict_line()) == 1


def test_a_non_review_steps_instructions_are_unchanged(tmp_path):
    text = expected_outputs.agent_instructions(["scan-01.md"], tmp_path)
    assert "MERGE" not in text and "NOT_YET" not in text
    assert text.splitlines() == [
        expected_outputs.deliverables_line(str(tmp_path)),
        expected_outputs.questions_line(),
        "Later steps of this workflow need these files from you: scan-01.md.",
        "Write each one there, which is outside the repository and outside "
        "your working directory. Files written anywhere else, the "
        "repository included, do not reach those steps.",
        f"- {tmp_path / 'scan-01.md'}",
    ]


# ---------------------------------------------------------------------------
# The repair turn
# ---------------------------------------------------------------------------


def _review(tmp_path, monkeypatch, runs: list[dict]):
    ctx = _ctx(tmp_path, {"prompt": "review it", "expected_outputs": ["verdict.json"]}, repo=False)
    _install(tmp_path, monkeypatch, ctx, runs)
    return ctx


def test_a_verdict_in_the_agents_own_words_gets_one_turn_naming_it(tmp_path, monkeypatch):
    ctx = _review(tmp_path, monkeypatch, [
        {"answer": "Reviewed.", "write": {"$ARTIFACTS/verdict.json": NOT_A_VERDICT}},
        {"answer": DONE, "write": {"$ARTIFACTS/verdict.json": NOT_YET}},
    ])
    out = claude_code.body(ctx)
    runs = _runs(ctx)
    # MUTATION: leave the verdict check out of the repair loop and the second
    # start never happens.
    assert [run["resume"] for run in runs] == [None, SESSION]
    prompt = runs[1]["prompt"]
    # The problem in the worker's words, which name the value written.
    problem = "verdict.json has `verdict` 'changes_requested', not MERGE or NOT_YET"
    assert problem in prompt
    assert expected_outputs.verdict_line() in prompt
    assert "Rewrite only that file, from the review you already did" in prompt
    assert out["repair_turns"] == 1
    (turn,) = out["repairs"]
    assert turn["verdict_problem"] == problem
    assert turn["verdict_fixed"] is True
    assert turn["missing_outputs"] == []
    assert out["repair_unresolved"] is None
    # The worker's own check, after the runner, reads it.
    assert expected_outputs.verdict_problem(ctx.artifacts_dir) is None


def test_a_verdict_written_wrong_again_is_not_resumed_again(tmp_path, monkeypatch):
    ctx = _review(tmp_path, monkeypatch, [
        {"answer": "Reviewed.", "write": {"$ARTIFACTS/verdict.json": NOT_A_VERDICT}},
    ])
    out = claude_code.body(ctx)
    # MUTATION: raise VERDICT_REPAIR_MAX_TURNS above 1 and a third start happens.
    assert cliagent.VERDICT_REPAIR_MAX_TURNS == 1
    assert len(_runs(ctx)) == 2
    assert out["repair_turns"] == 1
    assert out["repair_unresolved"]["verdict_problem"].startswith(
        "verdict.json has `verdict` 'changes_requested'"
    )
    assert out["exit_code"] == 0
    # No synonym reads: the worker's check still refuses it, as today.
    assert expected_outputs.verdict_problem(ctx.artifacts_dir) is not None


def test_a_verdict_never_written_keeps_the_missing_output_turn(tmp_path, monkeypatch):
    ctx = _review(tmp_path, monkeypatch, [
        {"answer": "Reviewed; the verdict is in my answer."},
        {"answer": DONE, "write": {"$ARTIFACTS/verdict.json": MERGE}},
    ])
    out = claude_code.body(ctx)
    runs = _runs(ctx)
    assert [run["resume"] for run in runs] == [None, SESSION]
    assert str((ctx.artifacts_dir / "verdict.json").resolve()) in runs[1]["prompt"]
    assert "does not read it as a verdict" not in runs[1]["prompt"]
    (turn,) = out["repairs"]
    assert turn["fixed_outputs"] == ["verdict.json"]
    assert turn["verdict_problem"] is None
    assert out["repair_unresolved"] is None


def test_a_readable_verdict_takes_no_turn(tmp_path, monkeypatch):
    ctx = _review(tmp_path, monkeypatch, [
        {"answer": DONE, "write": {"$ARTIFACTS/verdict.json": NOT_YET}},
    ])
    out = claude_code.body(ctx)
    assert len(_runs(ctx)) == 1
    assert out["repair_turns"] == 0
    assert out["repair_unresolved"] is None


def test_a_long_verdict_value_is_bounded_in_the_prompt(tmp_path, monkeypatch):
    long_word = "x" * 5000
    ctx = _review(tmp_path, monkeypatch, [
        {"answer": "Reviewed.", "write": {"$ARTIFACTS/verdict.json": json.dumps({"verdict": long_word})}},
        {"answer": DONE, "write": {"$ARTIFACTS/verdict.json": MERGE}},
    ])
    out = claude_code.body(ctx)
    runs = _runs(ctx)
    assert long_word not in runs[1]["prompt"]
    (turn,) = out["repairs"]
    assert len(turn["verdict_problem"]) <= cliagent.VERDICT_PROBLEM_CHARS + 3
