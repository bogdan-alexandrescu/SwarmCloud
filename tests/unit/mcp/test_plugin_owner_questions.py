"""A lane's questions for the owner reach the session that launched it (PLUGIN-OWNER-QUESTIONS, part of #1904).

MEASURED, 2026-10-22, plugin sc 0.5.53. A lane's agent asks the owner in its
`questions.json`; the bridge serves them whole in every step row's `result`
(`compact.step_result`) and in the summary follow's `outcome.questions`. The
run never handed them on:

  1. `stepAnswer()` built the flat step result from state, answer_excerpt,
     cost_usd, duration_s, pr_url, artifacts, last_error and console, and
     dropped `questions` -- NOVPC-JOBS (wf_268e5f8cad9649789fba) and
     QAFIX4-AGENT-PANES (wf_ac9eed2e58b647cba4f1) wrote questions.json and
     their rows' results had no questions field;
  2. the summary row returned a COUNT, and the run logged "N question(s) for
     the owner" and nothing more;
  3. nothing told the session to put them to the person.

What these tests hold: a step or task row's questions survive stepAnswer
(the documented shape and PLUGIN-ROW-NEST's nested one), validated and
capped; a run's result carries `owner_questions`, one entry per step or task
that asked, with each question's 0-based `index` for swarm_task_answer; one
log line per such step when the run ends; the summary row's questions whole;
and the skill and the workflow's own text telling the session to ask the
person with AskUserQuestion and record the pick with swarm_task_answer.
"""

from __future__ import annotations

import json
import re
import subprocess

from test_auto_attach import _BY_PROMPT
from test_plugin_agents_and_workflows import _ANSWERS, _PLUGIN, _RUN_JS, _SPEC, _meta, _node, _run

_SKILL = _PLUGIN / "skills" / "sc" / "SKILL.md"
_LINK = "https://c.test/agents/task_1"


def _question(n: int, **extra) -> dict:
    """One question as the bridge serves it in a row's `result`: no null."""
    return {
        "question": f"Should the park wake on a hold release? ({n})",
        "options": [
            {"label": "wake", "description": "wake every no-account park"},
            {"label": "wait", "description": "leave them to the next tick"},
        ],
        "recommended": "wake",
        "context": f"QUOTA-PARK-RECHECK, #1885 ({n})",
        **extra,
    }


def _fields(state: str, questions: list) -> dict:
    return {"state": state, "answer_excerpt": "done", "cost_usd": 1.5, "duration_s": 600.0,
            "pr_url": "https://github.com/acme/widgets/pull/9", "artifacts": ["questions.json"],
            "last_error": "", "console": _LINK, "questions": questions}


def _status_all(state: str = "SUCCEEDED") -> dict:
    return {"state": state, "state_note": None, "console": None,
            "steps": [{"step_id": s["step_id"], "state": state} for s in _SPEC["steps"]]}


def _per_step(tmp_path, answers: dict) -> dict:
    got = _run(tmp_path, _SPEC, {**_ANSWERS, "STATUS": _status_all(), **answers})
    assert "error" not in got, got.get("error")
    return got


def _expected(question: dict, index: int) -> dict:
    return {"index": index, "question": question["question"], "options": question["options"],
            "recommended": question["recommended"], "context": question["context"]}


# --------------------------------------------------------------------------
# 1. per-step rows
# --------------------------------------------------------------------------


def test_a_step_rows_questions_survive_step_answer_and_reach_owner_questions(tmp_path):
    asked = [_question(1), _question(2)]
    got = _per_step(tmp_path, {
        "step:scan-01": {"state": "SUCCEEDED", "result": _fields("SUCCEEDED", asked)},
        "step:scan-02": {"state": "SUCCEEDED", "result": _fields("SUCCEEDED", [])},
    })
    rows = {row["step_id"]: row for row in got["result"]["steps"]}

    # MUTATION: stepAnswer() leaving `questions` out of the flat result (the
    # #1904 defect) loses them here: the row carries none.
    assert rows["scan-01"]["questions"] == [_expected(asked[0], 0), _expected(asked[1], 1)], rows["scan-01"]
    # MUTATION: setting `questions: []` on every row puts an empty list on the
    # step that asked nothing; it stays absent, as `console` does.
    assert "questions" not in rows["scan-02"], rows["scan-02"]

    # MUTATION: not building `owner_questions` (or building it only in summary
    # mode) leaves the run's result without the questions at top level.
    assert got["result"]["owner_questions"] == [{
        "step_id": "scan-01", "task_id": "task_1", "workflow_id": "wf_1", "console": _LINK,
        "questions": [_expected(asked[0], 0), _expected(asked[1], 1)],
    }], got["result"].get("owner_questions")


def test_the_nested_shape_plugin_row_nest_unwraps_keeps_its_questions(tmp_path):
    asked = [_question(3)]
    got = _per_step(tmp_path, {
        "step:join": {"result": {"state": "SUCCEEDED", "result": _fields("SUCCEEDED", asked)}},
    })
    rows = {row["step_id"]: row for row in got["result"]["steps"]}
    # MUTATION: reading `questions` from the middle object instead of the
    # unwrapped inner one loses them on the nested row.
    assert rows["join"]["questions"] == [_expected(asked[0], 0)], rows["join"]
    assert [entry["step_id"] for entry in got["result"]["owner_questions"]] == ["join"]


def test_a_run_whose_step_asked_logs_one_line_for_it(tmp_path):
    asked = [_question(1), _question(2)]
    got = _per_step(tmp_path, {"step:scan-01": {"state": "SUCCEEDED", "result": _fields("SUCCEEDED", asked)}})
    lines = [line for line in got["logs"] if " asks the owner " in line]
    # MUTATION: no closing line (or one per question) -- zero, or two, here.
    assert lines == [
        "scan-01 asks the owner 2 question(s): '" + asked[0]["question"] + "' · answer them in this "
        "session, or in the console " + _LINK
    ], got["logs"]
    # MUTATION: logging it before the run's own final line ends the run on
    # the question line instead of `wf_1 <state>`... it comes after it.
    assert got["logs"].index(lines[0]) > got["logs"].index("wf_1 SUCCEEDED")


def test_a_run_with_no_questions_carries_an_empty_owner_questions(tmp_path):
    got = _per_step(tmp_path, {})
    # MUTATION: adding `owner_questions` only when there are some makes a
    # reader tell "none asked" from "not reported" by a missing key.
    assert got["result"]["owner_questions"] == []
    assert not any(" asks the owner " in line for line in got["logs"])


def test_questions_are_validated_and_capped_and_keep_their_served_index(tmp_path):
    long_text = "q" * 5000
    asked = [
        "not a question",
        _question(1, question=long_text, context="c" * 5000,
                  options=[{"label": f"opt{i}", "description": "d" * 2000} for i in range(30)]),
        {"question": "", "options": [{"label": "", "description": ""}], "recommended": "", "context": ""},
        _question(2, options="not a list", recommended=7),
    ] + [_question(i) for i in range(40)]
    got = _per_step(tmp_path, {"step:scan-01": {"state": "SUCCEEDED", "result": _fields("SUCCEEDED", asked)}})
    kept = {row["step_id"]: row for row in got["result"]["steps"]}["scan-01"]["questions"]

    # MUTATION: renumbering after dropping the non-object entry makes index 0
    # point at a question the agent did not ask first: swarm_task_answer
    # would record the pick on the wrong question.
    assert kept[0]["index"] == 1 and kept[1]["index"] == 2 and kept[2]["index"] == 3, kept[:3]
    # MUTATION: no cap on a question's text, its context or an option.
    assert len(kept[0]["question"]) <= 2000 and len(kept[0]["context"]) <= 2000, len(kept[0]["question"])
    assert len(kept[0]["options"]) <= 10 and all(len(o["description"]) <= 500 for o in kept[0]["options"])
    # MUTATION: keeping "" for an absent text: the session would show an
    # empty recommendation. It is null, as stepAnswer reads every empty text.
    assert kept[1] == {"index": 2, "question": None, "options": [{"label": None, "description": None}],
                       "recommended": None, "context": None}, kept[1]
    # MUTATION: passing through a non-list `options` or a non-text `recommended`.
    assert kept[2]["options"] == [] and kept[2]["recommended"] is None, kept[2]
    # MUTATION: no cap on the number of questions a row hands on.
    assert len(kept) <= 20, len(kept)


# --------------------------------------------------------------------------
# 2. single-task rows
# --------------------------------------------------------------------------


def test_a_task_rows_questions_reach_owner_questions_under_its_title(tmp_path):
    asked = [_question(5)]
    harness = tmp_path / "harness.cjs"
    harness.write_text(_BY_PROMPT)
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({
        "args": {"attach_tasks": [{"task_id": "task_q", "title": "QUOTA-PARK-RECHECK"},
                                  {"task_id": "task_n", "title": "NOTHING-ASKED"}]},
        "answers": {"task_id: task_q": {"state": "SUCCEEDED", "result": _fields("SUCCEEDED", asked)},
                    "task_id: task_n": {"state": "SUCCEEDED", "result": _fields("SUCCEEDED", [])}},
    }))
    done = subprocess.run([_node(), str(harness), str(_RUN_JS), str(fixture)],
                          capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)
    assert "error" not in got, got.get("error")

    # MUTATION: building owner_questions only from `steps` misses a task batch.
    assert got["result"]["owner_questions"] == [{
        "title": "QUOTA-PARK-RECHECK", "task_id": "task_q", "console": _LINK,
        "questions": [_expected(asked[0], 0)],
    }], got["result"].get("owner_questions")
    assert ("QUOTA-PARK-RECHECK asks the owner 1 question(s): '" + asked[0]["question"]
            + "' · answer them in this session, or in the console " + _LINK) in got["logs"]


# --------------------------------------------------------------------------
# 3. the summary row
# --------------------------------------------------------------------------


_WF_LINK = "https://console.example.test/workflows/wf_s"


def _attached() -> dict:
    steps = [
        {"step_id": "a", "task_id": "task_a", "depends_on": [], "state": "RUNNING", "console": None},
        {"step_id": "b", "task_id": "task_b", "depends_on": [], "state": "RUNNING", "console": "https://c.test/agents/task_b"},
        {"step_id": "c", "task_id": "task_c", "depends_on": [], "state": "RUNNING", "console": None},
    ]
    return {"workflow_id": "wf_s", "state": "RUNNING", "state_note": None, "title": "scan", "label": None,
            "console": _WF_LINK, "bridge_version": "0.1.0", "follow_error": None, "error": None, "steps": steps}


def _told(task_id: str, **extra) -> dict:
    return {"task_id": task_id, "state": "SUCCEEDED", "park_reason": None, "pr_url": None, "cost_usd": 0.1,
            "questions": 0, "last_error": None, **extra}


def _summary(tmp_path, tasks: list[dict]) -> dict:
    answers = {"ATTACH": _attached(), "SUMMARY": {"state": "FINISHED", "last_error": None, "tasks": tasks},
               "STATUS": {"state": "SUCCEEDED", "state_note": None, "console": _WF_LINK, "steps": []}}
    got = _run(tmp_path, {"attach": "wf_s", "title": "scan", "rows": "summary"}, answers)
    assert "error" not in got, got.get("error")
    return got


def test_a_summary_run_fills_the_questions_whole(tmp_path):
    asked = [_question(1), _question(2), _question(3)]
    got = _summary(tmp_path, [
        _told("task_a"),
        _told("task_b", questions=asked, answered=[1]),
        _told("task_c", questions=2),
    ])
    result = got["result"]
    steps = {s["step_id"]: s for s in result["steps"]}

    # MUTATION: summaryResult() keeping only a count (the #1904 defect) drops
    # the questions the row handed back whole.
    assert steps["b"]["questions"] == [_expected(q, i) for i, q in enumerate(asked)], steps["b"]
    entries = {e["step_id"]: e for e in result["owner_questions"]}
    # MUTATION: ignoring `answered` re-asks the question the owner already
    # answered in the console (index 1).
    assert entries["b"] == {"step_id": "b", "task_id": "task_b", "workflow_id": "wf_s",
                            "console": "https://c.test/agents/task_b",
                            "questions": [_expected(asked[0], 0), _expected(asked[2], 2)]}, entries["b"]
    # A row on an older agent returns only a count: the entry still says so,
    # and how to read them, rather than vanishing.
    # MUTATION: listing only array questions drops the counted step `c`.
    assert entries["c"]["questions"] == [] and entries["c"]["questions_count"] == 2, entries["c"]
    assert "swarm_result" in entries["c"]["questions_unread"], entries["c"]
    assert "a" not in entries
    # MUTATION: the closing line only in per-step mode -- absent here.
    assert ("b asks the owner 2 question(s): '" + asked[0]["question"]
            + "' · answer them in this session, or in the console https://c.test/agents/task_b") in got["logs"]
    # The in-run notice still counts every question the agent asked.
    assert "b · 3 question(s) for the owner · console: https://c.test/agents/task_b" in got["logs"], got["logs"]


def test_a_summary_task_batch_carries_owner_questions_by_title(tmp_path):
    asked = [_question(9)]
    answers = {"SUMMARY": {"state": "FINISHED", "last_error": None,
                           "tasks": [_told("task_q", questions=asked)]}}
    got = _run(tmp_path, {"attach_tasks": [{"task_id": "task_q", "title": "LANE-Q", "console": _LINK}],
                          "rows": "summary"}, answers)
    assert "error" not in got, got.get("error")
    # MUTATION: summarizeTasks() not building owner_questions.
    assert got["result"]["owner_questions"] == [{
        "title": "LANE-Q", "task_id": "task_q", "console": _LINK, "questions": [_expected(asked[0], 0)],
    }], got["result"].get("owner_questions")


# --------------------------------------------------------------------------
# 4. the text that tells the session what to do with them
# --------------------------------------------------------------------------


def _section(text: str, heading: str) -> str:
    start = text.index(heading + "\n")
    rest = text[start + len(heading) + 1:]
    found = re.search(r"^#{2,3} ", rest, flags=re.MULTILINE)
    return rest[: found.start()] if found else rest


_DUTY = ("owner_questions", "AskUserQuestion", "swarm_task_answer")


def test_the_skills_check_and_run_sections_say_to_ask_the_person():
    skill = _SKILL.read_text()
    for heading in ("### `check`", "### `run <spec path|JSON>`", "### Answers given here are recorded on the task"):
        section = _section(skill, heading)
        # MUTATION: leaving any of these out of a section -- the session reads
        # a check or a run with questions and never puts them to the person.
        missing = [word for word in _DUTY if word not in section]
        assert not missing, f"{heading} does not name {missing}"


def test_the_workflows_own_text_says_to_ask_the_person():
    meta = _meta()
    text = meta["description"] + " " + meta["whenToUse"]
    # MUTATION: a whenToUse/description that never names them: a session
    # reading only the workflow's own text leaves the questions in the result.
    assert all(word in text for word in _DUTY), text
