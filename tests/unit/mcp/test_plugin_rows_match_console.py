"""A SwarmCloud workflow reads the same in Claude Code's /workflows as in the console.

Owner request, 2026-10-04: "if I see a workflow with 2/4 or 4/6 or more steps
in the workflow here in claude code, why is that shown different in sc ui ...
they should be identically structured either here or there to match". Owner
chose steps-only counting and a 1:1 mapping (lane P4, functionality wave 4).

What this file holds:

* STEP ROWS ARE THE CONSOLE'S STEPS. One `sc:step` row per SwarmCloud step,
  labelled `stage N · <step_id>` with the console's stage number, in the
  console's order -- derived here by running the console's own `levelsOf`
  (apps/swarm-ui/src/dag.ts) over the same spec, from a fixture both read.
  The console never sees a spec's `stage` key (the bridge drops it as
  display-only, `workflows.DISPLAY_ONLY_STEP_KEYS`), so its Stage is the DAG
  level and nothing else; a row that showed the spec's `stage` would differ.
* EVERY OTHER ROW SAYS `setup`. At most two of them for a spec the plugin
  launches: the submit (which probes the follow) and the Result.
* THE RUN IS NAMED AFTER THE WORKFLOW. `SwarmCloud · <name> · N steps`, at
  most 100 characters: the spec's `title`, else its `label` cut at a word with
  `…`, else (attach) the title or label SwarmCloud stored, else the id. `meta`
  is a literal, so the bridge writes a per-run copy of run.js with it filled in
  (`swarm_workflow_launch`).
* ATTACH --ALL IS ONE RUN PER WORKFLOW, never one counter over several.
* ROWS SURVIVE A MISSING OR UNRESPONSIVE BRIDGE: five tries 30 s apart, then
  UNKNOWN ending `resume: /sc attach <workflow_id>`; the Result re-attaches the
  unfinished UNKNOWN steps once while SwarmCloud says the workflow still runs.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

from swarm_mcp import cli, server, workflows

from test_plugin_agents_and_workflows import _HARNESS, _PLUGIN, _RUN_JS, _node, _step

_REPO = Path(__file__).resolve().parents[3]
_DAG_TS = _REPO / "apps" / "swarm-ui" / "src" / "dag.ts"
_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "console_stages.json").read_text())
_MANIFEST = _PLUGIN / ".claude-plugin" / "plugin.json"
_SKILL = _PLUGIN / "skills" / "sc" / "SKILL.md"

_NOT_CONNECTED = (
    "the sc plugin's SwarmCloud MCP server is not connected in this session, so this row "
    "cannot read its task; the task itself is unaffected"
)
_NOT_RESPONDING = "the sc plugin's SwarmCloud MCP server is not responding; cannot read task"


# --------------------------------------------------------------------------
# The harness: run.js under node, with answers that may change per call
# --------------------------------------------------------------------------

def _replace(text: str, old: str, new: str) -> str:
    assert old in text, f"the run.js harness changed shape; re-point {old!r}"
    return text.replace(old, new)


#: `_HARNESS`, with three additions: a reply keyed by the whole prompt (attach
#: asks `ATTACH` once per workflow), a LIST of replies answered one per call
#: (the last repeats), and a `setTimeout` that records its delay and fires at
#: once, so a test of five tries 30 s apart takes no time.
_SEQ = _HARNESS
_SEQ = _replace(_SEQ, "const calls = []", "const calls = []\nconst asked = {}\nconst delays = []\n"
                "function fakeTimer(fn, ms) { delays.push(ms); fn() }")
_SEQ = _replace(
    _SEQ,
    "const answer = fixture.answers[key]",
    "let answer = fixture.answers[prompt] !== undefined ? fixture.answers[prompt] : fixture.answers[key]\n"
    "  if (Array.isArray(answer)) { asked[key] = (asked[key] || 0) + 1; "
    "answer = answer[Math.min(asked[key], answer.length) - 1] }",
)
_SEQ = _replace(_SEQ, "'log', 'args'", "'log', 'setTimeout', 'args'")
_SEQ = _replace(_SEQ, "run(agent, pipeline, parallel, phase, log, fixture.args",
                "run(agent, pipeline, parallel, phase, log, fakeTimer, fixture.args")
_SEQ = _replace(_SEQ, "JSON.stringify({ result, calls, logs, phases })",
                "JSON.stringify({ result, calls, logs, phases, delays })")


def _run(tmp_path, args, answers, script: Path = _RUN_JS) -> dict:
    harness = tmp_path / "harness.cjs"
    harness.write_text(_SEQ)
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"args": args, "answers": answers}))
    done = subprocess.run(
        [_node(), str(harness), str(script), str(fixture)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)
    assert "error" not in got, got.get("error")
    return got


def _submitted(spec: dict, workflow_id: str = "wf_1", order=None) -> dict:
    """The SUBMIT relay's answer for `spec`, its steps in `order` (default: reversed,
    so nothing can take the rows' order from the reply)."""
    steps = [
        {"step_id": s["step_id"], "task_id": "task_" + s["step_id"],
         "depends_on": list(s.get("depends_on") or []), "console": None}
        for s in spec["steps"]
    ]
    steps = list(reversed(steps)) if order is None else [next(s for s in steps if s["step_id"] == i) for i in order]
    return {"workflow_id": workflow_id, "console": None, "steps": steps,
            "repository": None, "repository_notes": [], "spec_digest": workflows.spec_digest(spec),
            "bridge_version": "0.1.0", "follow_error": None, "error": None}


def _answers(spec: dict, **extra) -> dict:
    out = {"SUBMIT": _submitted(spec), "STATUS": {"state": "SUCCEEDED", "state_note": None, "console": None, "steps": []}}
    for step in spec["steps"]:
        out["step:" + step["step_id"]] = _step("SUCCEEDED")
    out.update(extra)
    return out


def _step_rows(got) -> list[dict]:
    return [c for c in got["calls"] if c["agentType"] == "sc:step"]


def _setup_rows(got) -> list[dict]:
    return [c for c in got["calls"] if c["agentType"] != "sc:step"]


def _row_tail(label: str) -> str:
    """`stage N · <step_id>`: what a step row's label ends with (no link in these fixtures)."""
    parts = label.split(" · ")
    return " · ".join(parts[-2:])


# --------------------------------------------------------------------------
# The console's own stage derivation
# --------------------------------------------------------------------------

def _console_levels(steps: list[dict]) -> list[list[str]]:
    """The console's `levelsOf`, run: dag.ts's function, its type annotations
    stripped, over the steps as the API serves them (every step carries a
    `depends_on` list). Each inner list is one Stage, in the console's order."""
    source = _DAG_TS.read_text()
    start = source.index("export function levelsOf(")
    end = source.index("\n}\n", start) + 3
    body = source[start:end]
    for typed, plain in (
        ("export function", "function"),
        ("steps: readonly WorkflowStep[]): WorkflowStep[][]", "steps)"),
        ("new Map<string, number>()", "new Map()"),
        ("(id: string, seen: Set<string>): number =>", "(id, seen) =>"),
    ):
        body = _replace(body, typed, plain)
    served = [{"step_id": s["step_id"], "depends_on": list(s.get("depends_on") or [])} for s in steps]
    program = body + "\nprocess.stdout.write(JSON.stringify(levelsOf(" + json.dumps(served) + ").map((l) => l.map((s) => s.step_id))))\n"
    done = subprocess.run([_node(), "-e", program], capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _console_rows(spec: dict) -> list[str]:
    return [f"stage {index + 1} · {step_id}"
            for index, level in enumerate(_console_levels(spec["steps"])) for step_id in level]


def test_the_console_derivation_this_file_runs_is_the_consoles():
    """The derivation itself, held to what the console draws for the fixture:
    the spec's `stage` key plays no part, the join is Stage 2."""
    assert _console_rows(_FIXTURE["parallel"]) == [
        "stage 1 · scan-01", "stage 1 · scan-02", "stage 1 · scan-03",
        "stage 2 · join", "stage 3 · report",
    ]


def test_a_three_step_spec_yields_three_step_rows_as_the_console_shows_them(tmp_path):
    spec = _FIXTURE["chain"]
    got = _run(tmp_path, {"spec": spec}, _answers(spec))
    steps = _step_rows(got)
    assert [_row_tail(c["label"]) for c in steps] == _console_rows(spec)
    assert len(steps) == 3
    assert [c["phase"] for c in steps] == ["Stage 1", "Stage 2", "Stage 3"]
    setup = _setup_rows(got)
    assert len(setup) <= 2, [c["label"] for c in setup]
    for call in setup:
        assert call["label"].startswith("[SwarmCloud] ") and " · setup · " in call["label"], call["label"]
    assert len(got["calls"]) == len(spec["steps"]) + 2


def test_parallel_steps_in_one_stage_map_to_the_consoles_stage_numbers(tmp_path):
    spec = _FIXTURE["parallel"]
    got = _run(tmp_path, {"spec": spec}, _answers(spec))
    assert [_row_tail(c["label"]) for c in _step_rows(got)] == _console_rows(spec)
    # The spec's own `stage` names are not what the console shows, so not here either.
    assert not any("Report" in c["label"] or "Scans" in c["label"] for c in _step_rows(got))


def test_attached_rows_keep_the_stage_the_whole_workflow_gives_them(tmp_path):
    """An attach starts rows for the unfinished steps only; their stage is still
    their level in the WHOLE workflow, as the console draws it."""
    spec = _FIXTURE["chain"]
    attached = {
        "workflow_id": "wf_9", "console": None, "state": "RUNNING", "state_note": None,
        "bridge_version": "0.1.0", "follow_error": None, "error": None, "title": None, "label": spec["label"],
        "steps": [
            {"step_id": "implement", "task_id": "t1", "depends_on": [], "state": "SUCCEEDED", "console": None},
            {"step_id": "review", "task_id": "t2", "depends_on": ["implement"], "state": "RUNNING", "console": None},
            {"step_id": "fix", "task_id": "t3", "depends_on": ["review"], "state": "PARKED", "console": None},
        ],
    }
    got = _run(tmp_path, {"attach": "wf_9"}, {
        "ATTACH": attached, "step:review": _step("SUCCEEDED"), "step:fix": _step("SUCCEEDED"),
        "STATUS": {"state": "SUCCEEDED", "state_note": None, "console": None, "steps": []},
    })
    assert [_row_tail(c["label"]) for c in _step_rows(got)] == _console_rows(spec)[1:]


# --------------------------------------------------------------------------
# Setup rows and the workflow's name in the rows
# --------------------------------------------------------------------------

def test_a_spec_given_as_an_object_has_no_read_row_and_names_its_steps_and_id(tmp_path):
    spec = _FIXTURE["chain"]
    got = _run(tmp_path, {"spec": spec, "spec_path": "/abs/chain.json"}, _answers(spec))
    submit, *_, result = got["calls"]
    assert submit["prompt"].startswith("SUBMIT") and "READ SPEC" not in json.dumps(got["calls"])
    assert submit["label"] == "[SwarmCloud] implement, review, fix · 3 steps · setup · submit"
    assert result["label"] == "[SwarmCloud] implement, review, fix · 3 steps · wf_1 · setup · result"
    assert got["result"]["title"] == "SwarmCloud · implement, review, fix · 3 steps"


def test_the_list_and_attach_rows_are_setup_rows(tmp_path):
    got = _run(tmp_path, {"attach": "all"}, {"LIST": {"count": 0, "workflows": [], "error": None}})
    (listed,) = got["calls"]
    assert listed["label"] == "[SwarmCloud] running workflows · setup · list"


# --------------------------------------------------------------------------
# The title
# --------------------------------------------------------------------------

_LONG_LABEL = " ".join(["refactor"] * 40)[:300]


def test_the_title_is_at_most_100_characters_and_ends_at_a_word():
    assert len(_LONG_LABEL) == 300
    name = workflows.title_name(label=_LONG_LABEL)
    title = workflows.workflow_title(name, 3)
    assert len(title) <= workflows.TITLE_CHARS == 100
    assert title.startswith("SwarmCloud · refactor ") and title.endswith(" · 3 steps")
    cut = title[len("SwarmCloud · "):-len(" · 3 steps")]
    assert cut.endswith("refactor…"), cut
    assert _LONG_LABEL.startswith(cut[:-1])


def test_run_js_cuts_the_title_exactly_as_the_bridge_does(tmp_path):
    spec = {**_FIXTURE["chain"], "label": _LONG_LABEL}
    got = _run(tmp_path, {"spec": spec}, _answers(spec))
    assert got["result"]["title"] == workflows.workflow_title(workflows.title_name(label=_LONG_LABEL), 3)


def test_a_spec_title_wins_over_its_label(tmp_path):
    assert workflows.title_name(title="nightly", label="a much longer label") == "nightly"
    spec = {**_FIXTURE["chain"], "title": "nightly"}
    got = _run(tmp_path, {"spec": spec}, _answers(spec))
    assert got["result"]["title"] == "SwarmCloud · nightly · 3 steps"
    assert got["calls"][0]["label"] == "[SwarmCloud] nightly · 3 steps · setup · submit"


def test_an_attach_takes_the_title_swarmcloud_stored(tmp_path):
    assert workflows.title_name(stored="nightly", workflow_id="wf_9") == "nightly"
    assert workflows.title_name(workflow_id="wf_9") == "wf_9"
    attached = {
        "workflow_id": "wf_9", "console": None, "state": "RUNNING", "state_note": None,
        "bridge_version": "0.1.0", "follow_error": None, "error": None, "title": "nightly", "label": "x",
        "steps": [{"step_id": "a", "task_id": "t1", "depends_on": [], "state": "RUNNING", "console": None}],
    }
    got = _run(tmp_path, {"attach": "wf_9"}, {
        "ATTACH": attached, "step:a": _step("SUCCEEDED"),
        "STATUS": {"state": "SUCCEEDED", "state_note": None, "console": None, "steps": []},
    })
    assert got["result"]["title"] == "SwarmCloud · nightly · 1 step"
    assert _step_rows(got)[0]["label"] == "[SwarmCloud] nightly · stage 1 · a"


class _Recorder:
    def __init__(self, reply=None) -> None:
        self.sent: list[tuple[str, str, dict | None]] = []
        self.reply = reply

    def request(self, method: str, path: str, payload=None, **_: object) -> dict:
        self.sent.append((method, path, payload))
        if self.reply is not None:
            return self.reply
        steps = [{"step_id": s["step_id"], "task_id": "task_" + s["step_id"], "depends_on": s.get("depends_on") or []}
                 for s in (payload or {}).get("steps", [])]
        return {"workflow": {"workflow_id": "wf_1", "steps": steps}, "dispatch": {}}


def test_the_bridge_accepts_a_spec_title_and_sends_it_as_metadata_title():
    spec = {**_FIXTURE["chain"], "title": "nightly"}
    assert workflows.read_spec(spec)["title"] == "nightly"
    recorder = _Recorder()
    server._call(recorder, "swarm_workflow", {"spec": spec, "spec_digest": workflows.spec_digest(spec)})
    ((_, _, payload),) = recorder.sent
    assert payload["metadata"] == {"origin": "swarm-mcp", "unit": spec["label"], "title": "nightly"}
    assert "title" not in payload, "WorkflowCreate is extra=forbid: the title travels in metadata only"


def test_a_spec_without_a_title_submits_as_before():
    spec = _FIXTURE["chain"]
    recorder = _Recorder()
    server._call(recorder, "swarm_workflow", {"spec": spec})
    ((_, _, payload),) = recorder.sent
    assert payload["metadata"] == {"origin": "swarm-mcp", "unit": spec["label"]}


def test_a_title_that_is_not_text_is_refused():
    with pytest.raises(workflows.SwarmError, match="title"):
        workflows.read_spec({**_FIXTURE["chain"], "title": 7})


def test_the_cli_sends_a_spec_title_as_metadata_title(tmp_path, capsys):
    target = tmp_path / "spec.json"
    target.write_text(json.dumps({**_FIXTURE["chain"], "title": "nightly"}))
    recorder = _Recorder()
    namespace = argparse.Namespace(spec=str(target), strategy=None, carrier=None, repo=None, ref=None,
                                   label=None, title=None, json=True)
    cli.cmd_workflow(recorder, namespace)
    ((_, _, payload),) = recorder.sent
    assert payload["metadata"]["title"] == "nightly"
    # And --title, beside --label, overrides the spec's.
    recorder = _Recorder()
    cli.cmd_workflow(recorder, argparse.Namespace(**{**vars(namespace), "title": "other"}))
    assert recorder.sent[0][2]["metadata"]["title"] == "other"
    capsys.readouterr()


# --------------------------------------------------------------------------
# The per-run copy of run.js: the run's own name in Claude Code
# --------------------------------------------------------------------------

def _meta_of(script: Path) -> dict:
    program = (
        "const fs = require('fs')\n"
        "const text = fs.readFileSync(" + json.dumps(str(script)) + ", 'utf8')\n"
        "const end = text.indexOf('\\n}\\n') + 2\n"
        "const meta = new Function(text.slice(0, end).replace('export const meta =', 'return'))()\n"
        "process.stdout.write(JSON.stringify(meta))\n"
    )
    done = subprocess.run([_node(), "-e", program], capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.fixture
def _plugin_root(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARM_SC_PLUGIN_ROOT", str(_PLUGIN))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


def test_a_launch_writes_a_copy_of_run_js_named_after_the_workflow(_plugin_root, tmp_path):
    spec = {**_FIXTURE["chain"], "label": _LONG_LABEL}
    reply = json.loads(server._call(_Recorder(), "swarm_workflow_launch", {"spec": spec}))
    script = Path(reply["script_path"])
    meta = _meta_of(script)
    assert meta["name"] == reply["title"] == workflows.workflow_title(workflows.title_name(label=_LONG_LABEL), 3)
    assert len(meta["name"]) <= 100
    assert meta["name"] in meta["description"]
    assert meta["phases"] == _meta_of(_RUN_JS)["phases"]
    # Everything but the name and the description is run.js, byte for byte.
    copy, original = script.read_text().splitlines(), _RUN_JS.read_text().splitlines()
    assert len(copy) == len(original)
    assert [i for i, (a, b) in enumerate(zip(copy, original)) if a != b] == [1, 2]
    # And it runs.
    got = _run(tmp_path, {"spec": spec}, _answers(spec), script=script)
    assert got["result"]["title"] == meta["name"]


def test_a_launch_by_spec_path_reads_the_title_from_the_file(_plugin_root, tmp_path):
    target = tmp_path / "spec.json"
    target.write_text(json.dumps({**_FIXTURE["chain"], "title": "nightly"}))
    reply = json.loads(server._call(_Recorder(), "swarm_workflow_launch", {"spec_path": str(target)}))
    assert reply["title"] == "SwarmCloud · nightly · 3 steps"
    assert _meta_of(Path(reply["script_path"]))["name"] == "SwarmCloud · nightly · 3 steps"


def _stored(title=None, unit=None) -> dict:
    metadata = {"origin": "swarm-mcp", **({"unit": unit} if unit else {}), **({"title": title} if title else {})}
    return {
        "workflow": {"workflow_id": "wf_9", "steps": [
            {"step_id": "a", "task_id": "t1", "depends_on": []},
            {"step_id": "b", "task_id": "t2", "depends_on": ["a"]},
        ]},
        "tasks": [{"task_id": "t1", "metadata": metadata}, {"task_id": "t2", "metadata": metadata}],
    }


def test_an_attach_launch_takes_the_stored_title_else_the_unit_else_the_id(_plugin_root):
    for stored, name in ((_stored("nightly", "a long label"), "nightly"), (_stored(None, "a label"), "a label"),
                         (_stored(), "wf_9")):
        reply = json.loads(server._call(_Recorder(stored), "swarm_workflow_launch", {"attach": "wf_9"}))
        assert reply["title"] == f"SwarmCloud · {name} · 2 steps"
        assert reply["args"] == {"attach": "wf_9", "title": name}
        assert _meta_of(Path(reply["script_path"]))["name"] == reply["title"]


def test_the_status_read_carries_the_stored_title_and_label():
    reply = json.loads(server._call(_Recorder(_stored("nightly", "a label")), "swarm_workflow_status",
                                    {"workflow_id": "wf_9"}))
    assert (reply["title"], reply["label"]) == ("nightly", "a label")


# --------------------------------------------------------------------------
# attach --all: one Claude Code run per SwarmCloud workflow
# --------------------------------------------------------------------------

def test_attach_all_in_run_js_lists_and_hands_back_one_attach_call_per_workflow(tmp_path):
    listed = {"count": 2, "error": None, "workflows": [
        {"workflow_id": "wf_a", "label": "alpha", "title": None, "state": "RUNNING", "console": None},
        {"workflow_id": "wf_b", "label": "beta", "title": "Beta!", "state": "RUNNING", "console": None},
    ]}
    got = _run(tmp_path, {"attach": "all"}, {"LIST": listed})
    assert [c["prompt"] for c in got["calls"]] == ["LIST"], "no workflow is attached inside this one run"
    result = got["result"]
    assert result["state"] == "LISTED"
    assert result["attach_calls"] == [{"attach": "wf_a", "title": "alpha"}, {"attach": "wf_b", "title": "Beta!"}]


def test_attach_all_through_the_bridge_writes_one_run_per_workflow(_plugin_root, monkeypatch):
    from swarm_mcp import sc

    listing = {"count": 2, "complete": True, "workflows": [
        {"workflow_id": "wf_a", "label": "alpha", "title": None, "steps_total": 3, "console": None},
        {"workflow_id": "wf_b", "label": "beta", "title": "Beta!", "steps_total": 2, "console": None},
    ]}
    monkeypatch.setattr(sc, "running_workflows", lambda client: listing)
    reply = json.loads(server._call(_Recorder(), "swarm_workflow_launch", {"attach": "all"}))
    launches = reply["launches"]
    assert [l["args"] for l in launches] == [{"attach": "wf_a", "title": "alpha"}, {"attach": "wf_b", "title": "Beta!"}]
    assert [l["title"] for l in launches] == ["SwarmCloud · alpha · 3 steps", "SwarmCloud · Beta! · 2 steps"]
    paths = {l["script_path"] for l in launches}
    assert len(paths) == 2, "two runs, two scripts: never one counter over both"
    assert [_meta_of(Path(p))["name"] for p in (l["script_path"] for l in launches)] == [l["title"] for l in launches]


def test_the_sc_skill_launches_attach_all_as_one_run_per_workflow():
    text = " ".join(_SKILL.read_text().split())
    assert "swarm_workflow_launch" in text
    section = text[text.index("### `attach --all`"):]
    section = section[:section.index("### ", 5)]
    assert "one Workflow call per" in section and "scriptPath" in section


# --------------------------------------------------------------------------
# Rows survive a missing or unresponsive bridge
# --------------------------------------------------------------------------

def _down(error=_NOT_CONNECTED) -> dict:
    return _step("UNKNOWN", last_error=error)


def _row_result(got, step_id) -> dict:
    return next(r for r in got["result"]["steps"] if r["step_id"] == step_id)


def test_a_row_whose_bridge_is_down_twice_then_answers_ends_with_the_real_state(tmp_path):
    spec = _FIXTURE["chain"]
    got = _run(tmp_path, {"spec": spec}, _answers(spec, **{
        "step:review": [_down(), _down(_NOT_RESPONDING), _step("SUCCEEDED", duration_s=60)],
    }))
    review = [c for c in _step_rows(got) if "step_id: review" in c["prompt"]]
    assert len(review) == 3
    assert len({c["label"] for c in review}) == 1, "a retry is the same row, by the same label"
    assert _row_result(got, "review")["state"] == "SUCCEEDED"
    assert got["delays"] == [30000, 30000]


def test_five_bridge_failures_end_unknown_with_the_resume_line(tmp_path):
    spec = _FIXTURE["chain"]
    got = _run(tmp_path, {"spec": spec}, _answers(spec, **{"step:review": [_down()]}))
    review = [c for c in _step_rows(got) if "step_id: review" in c["prompt"]]
    assert len(review) == 5
    assert got["delays"] == [30000] * 4
    row = _row_result(got, "review")
    assert row["state"] == "UNKNOWN"
    assert row["last_error"].startswith(_NOT_CONNECTED)
    assert row["last_error"].endswith("resume: /sc attach wf_1")


def test_a_result_over_a_running_workflow_reattaches_an_unknown_row_once(tmp_path):
    spec = _FIXTURE["chain"]
    running = {"state": "RUNNING", "state_note": None, "console": None, "steps": [
        {"step_id": "implement", "state": "SUCCEEDED"}, {"step_id": "review", "state": "RUNNING"},
        {"step_id": "fix", "state": "PARKED"},
    ]}
    got = _run(tmp_path, {"spec": spec}, _answers(spec, **{
        "step:review": [_down()] * 5 + [_step("SUCCEEDED")],
        "step:fix": [_down()] * 5 + [_step("SUCCEEDED")],
        "STATUS": [running, {"state": "SUCCEEDED", "state_note": None, "console": None, "steps": []}],
    }))
    first = [c["prompt"].split("\n")[0] for c in got["calls"]]
    assert first.count("STATUS") == 2
    review = [c for c in _step_rows(got) if "step_id: review" in c["prompt"]]
    fix = [c for c in _step_rows(got) if "step_id: fix" in c["prompt"]]
    assert len(review) == 6 and len(fix) == 6
    assert len([c for c in _step_rows(got) if "step_id: implement" in c["prompt"]]) == 1, "a finished step is not re-attached"
    assert _row_result(got, "review")["state"] == "SUCCEEDED"
    assert got["result"]["state"] == "SUCCEEDED"
    assert got["result"]["reattached"] == ["review", "fix"]
    assert any("re-attach" in line for line in got["logs"]), got["logs"]


def test_the_reattach_happens_once_however_the_rows_end(tmp_path):
    spec = _FIXTURE["chain"]
    running = {"state": "RUNNING", "state_note": None, "console": None,
               "steps": [{"step_id": "review", "state": "RUNNING"}, {"step_id": "fix", "state": "PARKED"}]}
    got = _run(tmp_path, {"spec": spec}, _answers(spec, **{
        "step:review": [_down()], "step:fix": [_down()], "STATUS": [running],
    }))
    first = [c["prompt"].split("\n")[0] for c in got["calls"]]
    assert first.count("STATUS") == 2
    assert len([c for c in _step_rows(got) if "step_id: review" in c["prompt"]]) == 10
    assert _row_result(got, "review")["last_error"].endswith("resume: /sc attach wf_1")


def test_no_reattach_when_the_workflow_has_finished(tmp_path):
    spec = _FIXTURE["chain"]
    got = _run(tmp_path, {"spec": spec}, _answers(spec, **{"step:review": [_down()]}))
    assert [c["prompt"].split("\n")[0] for c in got["calls"]].count("STATUS") == 1
    assert "reattached" not in got["result"]


# --------------------------------------------------------------------------
# The version and the pin move together
# --------------------------------------------------------------------------

def test_the_plugin_version_and_its_bridge_pin_moved_together():
    manifest = json.loads(_MANIFEST.read_text())
    version = manifest["version"]
    assert tuple(int(part) for part in version.split(".")) > (0, 5, 13)
    pinned = json.dumps(manifest["mcpServers"])
    assert f"@sc-v{version}#subdirectory=apps/swarm-mcp" in pinned
    assert re.findall(r"@sc-v([0-9.]+)#", pinned) == [version]


def test_the_bridge_is_told_where_the_plugin_is():
    env = json.loads(_MANIFEST.read_text())["mcpServers"]["swarmcloud"]["env"]
    assert env.get("SWARM_SC_PLUGIN_ROOT") == "${CLAUDE_PLUGIN_ROOT}"
