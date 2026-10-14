"""An `sc:step` row ends cleanly on a skipped step and on a merge parked on CI.

MEASURED, 2026-10-12, in the operator's Claude Code session (ROW-SKIP, part
of #1194). Several `[SwarmCloud]` step rows of `/sc:swarmcloud attach` runs
ended with `row_error` "agent({schema}): StructuredOutput retry cap (5)
exceeded ... root: must have required property 'state', /result: must have
required property 'answer_excerpt', 'pr_url', 'artifacts', 'last_error',
'console'", or "subagent completed without calling StructuredOutput" --
always on re-review and merge steps, mostly ones whose agent was skipped (no
answer, no artifacts) or that were PARKED on CI. The workflow itself was fine;
the rows were not:

* a skipped step finishes with nothing to copy, and the relay answered with
  the empty fields left out or null -- which the schema refused five times;
* a merge PARKED on `CI_PENDING` is woken and re-parked for as long as the
  pull request's CI runs, so its row followed hold after hold, and ended at
  its turn budget without ever answering;
* a reply that says nothing new (`changed: false`) was taken by the relay as
  the end of its work, so it stopped without calling StructuredOutput.

So the bridge hands a step row a ready-made PARKED `result` for a CI park and
`stop: true`; the row's schema takes a skipped step's empty answer however the
relay writes it; and the row's instructions say what to do in all three cases.
"""

from __future__ import annotations

import importlib
import json
import re
import subprocess
from pathlib import Path

import pytest

from test_follow_cursor import NOW, World, swarm, world  # noqa: F401 - fixtures
from test_plugin_agents_and_workflows import (
    _ANSWERS,
    _PLUGIN,
    _RUN_JS,
    _SPEC,
    _load,
    _node,
    _run,
)

#: Every field a step row's `result` must carry (`STEP_FIELDS` in run.js).
RESULT_TEXTS = ("state", "answer_excerpt", "pr_url", "last_error", "console")


@pytest.fixture()
def compact():
    return importlib.import_module("swarm_mcp.compact")


def _no_null(value) -> bool:
    if value is None:
        return False
    if isinstance(value, dict):
        return all(_no_null(item) for item in value.values())
    if isinstance(value, list):
        return all(_no_null(item) for item in value)
    return True


def _merge_parked_on_ci(world: World, task_id: str = "task_m", step_id: str = "merge") -> None:
    world.task(task_id, state="PARKED")
    doc = world.db.docs[f"tasks/{task_id}"]
    doc["step_id"] = step_id
    doc["park_reason"] = "CI_PENDING"
    doc["metadata"] = {
        "merge_wait": {"pull_request": 231, "pending": ["unit", "lint"], "code": "CI_PENDING", "head": "abc1234"},
    }


def _skipped(world: World, task_id: str = "task_r", step_id: str = "re-review") -> None:
    """A step whose agent was skipped: it SUCCEEDED with no answer and no artifacts."""
    world.task(task_id, state="SUCCEEDED")
    doc = world.db.docs[f"tasks/{task_id}"]
    doc["step_id"] = step_id
    doc["completed_at"] = NOW
    doc["result_summary"] = {}


# --------------------------------------------------------------------------
# The bridge: a CI park ends a STEP row with a ready-made PARKED result
# --------------------------------------------------------------------------


def test_a_merge_parked_on_ci_stops_its_step_row_with_a_parked_result(swarm, world, compact):
    _merge_parked_on_ci(world)

    got = compact.watch_progress(swarm, ["task_m"], step_id="merge", wait_seconds=1800)

    assert got["stop"] is True, "a CI-parked merge must end its row, not hold it hold after hold"
    assert got["tasks"][0]["park_reason"] == "CI_PENDING"
    result = got["result"]
    assert result["state"] == "PARKED"
    for name in RESULT_TEXTS:
        assert isinstance(result[name], str), name
    assert result["artifacts"] == [] and result["questions"] == []
    assert _no_null(result), result
    # The park reason, what it waits on, that nothing was lost, and how to
    # pick it up again -- all in the one field the narrator prints.
    why = result["last_error"]
    assert "CI_PENDING" in why and "check" in why, why
    assert "#231" in why and "unit" in why, why
    assert "swarm follow task_m" in why, why


def test_a_ci_park_stops_on_a_later_call_too(swarm, world, compact):
    """The row was following the merge while it ran; the park ends the next hold."""
    world.task("task_m", state="RUNNING")
    world.db.docs["tasks/task_m"]["step_id"] = "merge"
    first = compact.watch_progress(swarm, ["task_m"], step_id="merge")
    assert first["stop"] is False

    _merge_parked_on_ci(world)
    got = compact.watch_progress(swarm, ["task_m"], step_id="merge", since=first["since"])

    assert got["stop"] is True and got["result"]["state"] == "PARKED"


def test_a_ci_park_does_not_stop_a_follow_that_names_no_step(swarm, world, compact):
    """Only a STEP row is ended by a CI park: an operator's own follow, or a
    single task's row, keeps following it as before."""
    _merge_parked_on_ci(world)

    got = compact.watch_progress(swarm, ["task_m"])

    assert got["stop"] is False
    assert "result" not in got


def test_a_dependency_park_still_holds_the_step_row(swarm, world, compact):
    world.task("task_m", state="PARKED")
    doc = world.db.docs["tasks/task_m"]
    doc["step_id"] = "merge"
    doc["park_reason"] = "DEPENDENCY_WAIT"

    got = compact.watch_progress(swarm, ["task_m"], step_id="merge")

    assert got["stop"] is False and "result" not in got


def test_a_skipped_step_finishes_with_an_empty_answer_and_no_null(swarm, world, compact):
    _skipped(world)

    got = compact.watch_progress(swarm, ["task_r"], step_id="re-review")

    assert got["stop"] is True
    result = got["result"]
    assert result["state"] == "SUCCEEDED"
    assert result["answer_excerpt"] == "" and result["artifacts"] == []
    for name in RESULT_TEXTS:
        assert isinstance(result[name], str), name
    assert _no_null(result), result


# --------------------------------------------------------------------------
# run.js: the row's schema, and what the workflow does with these answers
# --------------------------------------------------------------------------


def _schema(name: str) -> dict:
    """run.js's `name` schema object, evaluated by node exactly as written."""
    source = _RUN_JS.read_text()
    start = source.index("const STEP_FIELDS = {")
    end = source.index("\n}\n", source.index("const STEP_RESULT = {")) + 3
    script = source[start:end] + f"\nprocess.stdout.write(JSON.stringify({name}))\n"
    done = subprocess.run([_node(), "-e", script], capture_output=True, text=True, timeout=30, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool, "null": type(None),
    "number": (int, float),
}


def _errors(schema: dict, value, where: str = "root") -> list[str]:
    """The JSON-schema subset run.js uses: type, properties, required, items."""
    found: list[str] = []
    kinds = schema.get("type")
    if kinds is not None:
        kinds = kinds if isinstance(kinds, list) else [kinds]
        fits = any(
            isinstance(value, _TYPES[kind]) and not (kind == "number" and isinstance(value, bool))
            for kind in kinds
        )
        if not fits:
            return [f"{where}: must be {kinds}"]
    if isinstance(value, dict):
        for name in schema.get("required") or []:
            if name not in value:
                found.append(f"{where}: must have required property {name!r}")
        for name, sub in (schema.get("properties") or {}).items():
            if name in value:
                found += _errors(sub, value[name], f"{where}/{name}")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            found += _errors(schema["items"], item, f"{where}/{index}")
    return found


#: What the measured relays sent for a skipped re-review step: no root
#: `state`, and the empty fields null or left out.
_SKIPPED_AS_SENT = [
    {"result": {"state": "SUCCEEDED"}},
    {"result": {"state": "SUCCEEDED", "answer_excerpt": None, "pr_url": None, "artifacts": None,
                "last_error": None, "console": None, "questions": None}},
    {"state": "SUCCEEDED", "result": {"state": "SUCCEEDED", "answer_excerpt": "", "pr_url": "",
                                      "artifacts": [], "last_error": "", "console": "", "questions": []}},
]


@pytest.mark.parametrize("answer", _SKIPPED_AS_SENT)
def test_the_step_schema_takes_a_skipped_steps_answer_however_the_relay_writes_it(answer):
    assert _errors(_schema("STEP_RESULT"), answer) == []


def test_the_step_schema_takes_an_answer_with_no_state():
    """SPEED-D5 (2026-10-14) loosened `state` too: rows on 0.5.27 still ran out
    of their attempts on it. A stateless answer is read from the workflow
    status instead (test_server_added_steps_and_tolerant_rows.py)."""
    assert _errors(_schema("STEP_RESULT"), {"result": {"answer_excerpt": ""}}) == []


def test_a_skipped_step_reads_back_as_succeeded_with_nothing_produced(tmp_path):
    answers = {**_ANSWERS, "step:report": {"result": {"state": "SUCCEEDED", "answer_excerpt": None, "artifacts": None}}}

    got = _run(tmp_path, _SPEC, answers)

    assert "error" not in got, got.get("error")
    report = {row["step_id"]: row for row in got["result"]["steps"]}["report"]
    assert report["state"] == "SUCCEEDED" and "row_error" not in report
    assert report["answer_excerpt"] is None and report["artifacts"] == []
    assert any(line.startswith("report SUCCEEDED · ") and "produced no artifacts" in line for line in got["logs"]), got["logs"]


def test_a_ci_parked_merge_reads_back_as_parked_with_its_reason_and_is_not_reattached(tmp_path):
    why = "PARKED on CI_PENDING: waiting for the pull request's checks · resume: swarm follow task_4"
    answers = {
        **_ANSWERS,
        "step:report": {"state": "PARKED", "result": {
            "state": "PARKED", "answer_excerpt": "", "pr_url": "", "artifacts": [], "last_error": why,
            "console": "", "questions": []}},
        "STATUS": {"state": "RUNNING", "state_note": None, "steps": [{"step_id": "report", "state": "PARKED"}]},
    }

    got = _run(tmp_path, _SPEC, answers)

    assert "error" not in got, got.get("error")
    report = {row["step_id"]: row for row in got["result"]["steps"]}["report"]
    assert report["state"] == "PARKED" and report["last_error"] == why
    assert "row_error" not in report
    assert any(line.startswith("report PARKED · ") and "CI_PENDING" in line for line in got["logs"]), got["logs"]
    # PARKED is an answer, not a lost row: no second row is started for it.
    assert "reattached" not in got["result"]
    assert sum(1 for call in got["calls"] if call["label"].endswith("· report")) == 1


# --------------------------------------------------------------------------
# step.md: what the relay does in each case
# --------------------------------------------------------------------------


def _flat_step_md() -> str:
    _, body = _load(_PLUGIN / "agents" / "step.md")
    return " ".join(body.split())


def test_the_step_row_answers_a_ci_park_with_parked_and_stops():
    flat = _flat_step_md()
    assert "`CI_PENDING`" in flat
    assert "`tasks[0].park_reason`" in flat
    assert re.search(r"`state: \"PARKED\"`", flat), "step.md must answer a CI park as PARKED"
    assert "never wait" in flat.lower() or "do not wait" in flat.lower()


def test_the_step_row_never_ends_on_a_reply_with_nothing_new():
    lower = _flat_step_md().lower()
    assert "not the end of this row" in lower
    assert "make the next call at once" in lower
    assert "every way this row ends is a `structuredoutput` call" in lower


def test_the_step_row_answers_a_skipped_step_with_empty_fields_not_nulls():
    flat = _flat_step_md()
    assert "skipped" in flat.lower()
    assert '`answer_excerpt: ""`' in flat and "`artifacts: []`" in flat


def test_the_plugin_version_moved_past_the_one_these_rows_failed_on():
    manifest = json.loads((Path(_PLUGIN) / ".claude-plugin" / "plugin.json").read_text())
    major, minor, patch = (int(part) for part in manifest["version"].split("."))
    assert (major, minor, patch) > (0, 5, 30), manifest["version"]
