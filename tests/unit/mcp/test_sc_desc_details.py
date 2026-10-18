"""Every `/sc:swarmcloud` run's expanded description says who started it, where it runs and on which LLM (SC-DESC-DETAILS).

Owner, 2026-10-14 ~22:00 PDT, verbatim: "we also said that workflows in here
should not have the [Engineering] or [eng@saga.xyz] prefix in front of the
names. We do want to have a section in the workflow or task description when
you expand it in claude, that will provide more details like user that
created the job, tenant or workspace that is using and maybe even what LLM
subscription or config is using all shown here in claude code under each
workflow or task expanded description". Held here:

* THE DETAILS. A run's meta.description starts with one line, six fields in
  this order -- Workspace, Started by, Profiles, LLM, Browser tool, Session --
  each read off what the API serves (`submitted_by`, `runner_profile`, the
  task's `account` and `model`, the account's label from `GET /v1/accounts`,
  `input.browser_tool`, `claude_session`) and `not reported` when it serves
  nothing. The workspace stays first, so DESCRIPTION_CHARS clips the end.
* EACH ROW. A step or single-task row's prompt ends with its own `details:`
  line -- what Claude Code shows when the row is expanded -- written by the
  bridge into the copy's args, or built by run.js from what it knows.
* NO WORKSPACE IN ANY NAME. No run title, Result title, row label or
  SessionStart notice entry starts with `[<workspace>]`.
* LLM ONCE. A row whose task had no account when it started says
  `LLM: <account>` on the first progress line after the assignment, once.
* NOTHING SECRET-SHAPED. Only allow-listed fields are read, and a value that
  looks like a credential is withheld: a fixture whose account label, account
  object and metadata carry a token-like value leaks none of it.

THE MUTATIONS THESE CATCH: put `workspace_tag` back into a label or the title
(the no-prefix tests go red); drop a field or reorder them (the order test
goes red); omit a field the API did not serve instead of saying `not
reported` (the fallback test goes red); read the account object whole (the
leak test goes red); append the details after the base description (the clip
test goes red); say the LLM on every line (the once test goes red).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from swarm_mcp import compact, details, launch, sc, workflows

from test_auto_attach import _BY_PROMPT
from test_plugin_agents_and_workflows import _PLUGIN, _RUN_JS, _node, _step

_MANIFEST = _PLUGIN / ".claude-plugin" / "plugin.json"
_SKILL = _PLUGIN / "skills" / "sc" / "SKILL.md"
_README = _PLUGIN / "README.md"

#: The release this lane follows (SC-WS-DESC, PR 1323).
_PREVIOUS_RELEASED_VERSION = (0, 5, 38)

SESSION = "11111111-2222-4333-8444-555555555555"
OTHER_SESSION = "99999999-2222-4333-8444-555555555555"

FIELDS = ("Workspace", "Started by", "Profiles", "LLM", "Browser tool", "Session")


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv(launch.PLUGIN_ROOT_ENV, str(_PLUGIN))
    monkeypatch.setattr(launch.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    for name in ("SWARM_PLUGIN_MAX_LIVE_ROWS", "CLAUDE_PLUGIN_OPTION_MAX_LIVE_ROWS", "SWARM_SC_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    launch.forget_attached()


def _meta(script: str) -> tuple[str, str]:
    lines = Path(script).read_text(encoding="utf-8").split("\n")
    return json.loads(lines[1][len("  name: "):-1]), json.loads(lines[2][len("  description: "):-1])


def _fields(text: str) -> dict[str, str]:
    """`{field: value}` of a details line, in the order found."""
    out: dict[str, str] = {}
    for name in FIELDS:
        match = re.search(re.escape(name) + r": (.*?)(?: · (?:" + "|".join(map(re.escape, FIELDS)) + r"): | · SC · |$)", text)
        if match:
            out[name] = match.group(1)
    return out


def _token() -> str:
    """A credential-shaped value, built at runtime so no literal looks like one."""
    return "sk-" + "ant-" + "Q" * 44


class _Api:
    """A client serving `/v1/tenants/me`, `/v1/accounts` and tasks."""

    def __init__(self, *, email="dev@example.com", tenant="eng", shown="eng@saga.xyz", accounts=None, tasks=None):
        self.email, self.tenant, self.shown = email, tenant, shown
        self.accounts = accounts if accounts is not None else [{"account_id": "acct_max1", "label": "Team Max"}]
        self.tasks = tasks or {}

    def request(self, method, path, **_):
        if path == "/v1/tenants/me":
            return {"tenant": {"tenant_id": self.tenant, "display_name": self.shown},
                    "principal": {"email": self.email, "groups": []}}
        if path == "/v1/accounts":
            return {"accounts": self.accounts, "tenant_id": self.tenant}
        raise AssertionError(path)

    def tenants_mine(self):
        return [{"tenant_id": self.tenant, "display_name": self.shown}]

    def task(self, task_id):
        return self.tasks[task_id]


def _spec() -> dict:
    return {"label": "gate", "steps": [
        {"step_id": "implement", "prompt": "p", "runner_profile": "claude-code"},
        {"step_id": "verify", "prompt": "p", "runner_profile": "claude-code-browser",
         "inputs": {"browser_tool": "jev"}, "depends_on": ["implement"]},
    ]}


def _task(name, *, account=None, model="claude-opus-5-5", submitted_by="dev@example.com", **more):
    """A step task named `name` (a single task with `step_id=None`)."""
    doc = {"id": f"t_{name}", "tenant_id": "eng", "step_id": name, "state": "RUNNING",
           "runner_profile": "claude-code", "submitted_by": submitted_by, "model": model,
           "account": account, "claude_session": SESSION, "input": {"prompt": "p"}}
    doc.update(more)
    return doc


def _envelope(tasks) -> dict:
    return {
        "workflow": {"workflow_id": "wf_a", "tenant_id": "eng", "submitted_by": "dev@example.com",
                     "claude_session": SESSION,
                     "steps": [{"step_id": t["step_id"], "task_id": t["id"], "runner_profile": t["runner_profile"],
                                "input": t.get("input")} for t in tasks]},
        "tasks": tasks,
    }


ASSIGNED = {"status": "assigned", "account_id": "acct_max1", "provider": "anthropic"}


# --------------------------------------------------------------------------
# The run's description: every field, in order
# --------------------------------------------------------------------------


def test_a_submitted_specs_description_holds_every_field_in_order():
    out = launch.for_spec(_spec(), _Api())
    _, description = _meta(out["script_path"])
    assert description.startswith("Workspace: eng@saga.xyz (eng) · Started by: dev@example.com · ")
    positions = [description.index(f"{name}: ") for name in FIELDS]
    assert positions == sorted(positions), description
    got = _fields(description)
    assert got["Profiles"] == "claude-code, claude-code-browser"
    assert got["LLM"] == "not assigned yet", "nothing is assigned before the submission"
    assert got["Browser tool"] == "jev"
    assert got["Session"] == f"this session ({SESSION})"
    # Each step row's own line comes back for the session to hand on.
    rows = out["row_details"]["steps"]
    assert _fields(rows["implement"])["Profiles"] == "claude-code"
    assert _fields(rows["implement"])["Browser tool"] == "not used"
    assert _fields(rows["verify"])["Browser tool"] == "jev"


def test_an_attached_workflows_description_names_the_account_label_and_model(monkeypatch):
    tasks = [_task("implement", account=ASSIGNED),
             _task("verify", runner_profile="claude-code-browser", account={"status": "not_assigned_yet"},
                   input={"prompt": "p"})]
    monkeypatch.setattr(workflows, "fetch", lambda client, wid: _envelope(tasks))
    out = launch.for_attach(_Api(), "wf_a")
    _, description = _meta(out["script_path"])
    got = _fields(description)
    assert got["Workspace"] == "eng@saga.xyz (eng)"
    assert got["Started by"] == "dev@example.com"
    assert got["LLM"] == "Team Max (anthropic), model claude-opus-5-5"
    assert got["Browser tool"] == "auto", "browser_tool absent on claude-code-browser is auto"
    # Each step row's line, in the args the run hands run.js.
    steps = out["args"]["details"]["steps"]
    assert _fields(steps["implement"])["LLM"] == "Team Max (anthropic), model claude-opus-5-5"
    assert _fields(steps["verify"])["LLM"] == "not assigned yet, model claude-opus-5-5"


def test_a_field_the_api_does_not_serve_says_not_reported():
    """The control: a spec whose tenant read fails, and a task document with
    none of the fields, say `not reported` for each rather than leaving it out."""
    class _Down:
        def request(self, *_a, **_k):
            raise RuntimeError("down")

    out = launch.for_spec({"label": "gate", "steps": [{"step_id": "s", "prompt": "p"}]}, _Down())
    got = _fields(_meta(out["script_path"])[1])
    assert got["Workspace"] == "not reported" and got["Started by"] == "not reported"
    out = launch.for_tasks([{"task_id": "task_a", "label": "lane", "task": {"id": "task_a"}}])
    got = _fields(_meta(out["script_path"])[1])
    assert set(got) == set(FIELDS), got
    for name in ("Workspace", "Started by", "Profiles", "LLM", "Browser tool", "Session"):
        assert got[name] == "not reported", (name, got)


def test_another_sessions_workflow_says_whose_it_is(monkeypatch):
    tasks = [_task("implement", claude_session=OTHER_SESSION)]
    envelope = _envelope(tasks)
    envelope["workflow"]["claude_session"] = OTHER_SESSION
    monkeypatch.setattr(workflows, "fetch", lambda client, wid: envelope)
    got = _fields(_meta(launch.for_attach(_Api(), "wf_a")["script_path"])[1])
    assert got["Session"] == f"another Claude Code session ({OTHER_SESSION})"


def test_a_dispatched_tasks_row_and_run_carry_its_details():
    task = _task("x", step_id=None, account=None)
    out = launch.for_tasks([{"task_id": "t_x", "label": "lane-a", "workspace": "eng", "task": task}])
    (row,) = out["args"]["attach_tasks"]
    assert "task" not in row, "the task document is read, never copied into the args"
    got = _fields(row["details"])
    assert got["Started by"] == "dev@example.com" and got["Profiles"] == "claude-code"
    assert got["Session"] == f"this session ({SESSION})"
    assert _meta(out["script_path"])[1].startswith("Workspace: eng · Started by: dev@example.com · ")


def test_attach_all_gives_every_run_and_task_row_its_details(monkeypatch):
    monkeypatch.setattr(sc, "running_workflows", lambda client: {"count": 1, "complete": True, "workflows": [
        {"workflow_id": "wf_a", "workspace": "eng", "title": "flow", "label": None, "steps_total": 1,
         "claude_session": SESSION}]})
    monkeypatch.setattr(sc, "running_tasks", lambda client: {"count": 1, "tasks": [
        {"task_id": "t_x", "label": "lane", "workspace": "eng", "claude_session": SESSION}]})
    monkeypatch.setattr(workflows, "fetch", lambda client, wid: _envelope([_task("implement", account=ASSIGNED)]))
    api = _Api(tasks={"t_x": _task("x", step_id=None, account=ASSIGNED)})
    out = launch.for_all(api)
    flow, tasks = out["launches"]
    assert _fields(_meta(flow["script_path"])[1])["LLM"] == "Team Max (anthropic), model claude-opus-5-5"
    assert _fields(flow["args"]["details"]["steps"]["implement"])["Started by"] == "dev@example.com"
    assert _fields(tasks["args"]["attach_tasks"][0]["details"])["LLM"] == "Team Max (anthropic), model claude-opus-5-5"


# --------------------------------------------------------------------------
# Clipping keeps the workspace
# --------------------------------------------------------------------------


def test_a_clipped_description_keeps_the_workspace_and_clips_the_end(monkeypatch):
    tasks = [_task(f"step-{n:02d}", runner_profile=f"profile-{n:02d}-" + "p" * 30) for n in range(8)]
    monkeypatch.setattr(workflows, "fetch", lambda client, wid: _envelope(tasks))
    out = launch.for_attach(_Api(), "wf_a")
    _, description = _meta(out["script_path"])
    assert len(description) == launch.DESCRIPTION_CHARS and description.endswith("…"), "it was clipped"
    assert description.startswith("Workspace: eng@saga.xyz (eng) · Started by: dev@example.com · Profiles: ")


# --------------------------------------------------------------------------
# Nothing secret-shaped
# --------------------------------------------------------------------------


def test_no_token_like_value_reaches_a_description_or_a_row(monkeypatch):
    secret = _token()
    leaky_account = {**ASSIGNED, "access_token": secret, "refresh_token": secret}
    tasks = [_task("implement", account=leaky_account, metadata={"note": secret}, input={"prompt": secret},
                   last_error=secret)]
    monkeypatch.setattr(workflows, "fetch", lambda client, wid: _envelope(tasks))
    api = _Api(accounts=[{"account_id": "acct_max1", "label": secret, "oauth": secret}])
    out = launch.for_attach(api, "wf_a")
    text = Path(out["script_path"]).read_text(encoding="utf-8") + json.dumps(out)
    assert secret not in text and "Q" * 20 not in text
    got = _fields(_meta(out["script_path"])[1])
    assert got["LLM"].startswith("acct_max1 (anthropic)"), "a credential-shaped label falls back to the id"
    # A submitter or model that is itself credential-shaped is withheld, said as such.
    assert details.shown(secret) == details.WITHHELD
    assert details.shown("dev@example.com") == "dev@example.com"
    assert details.shown(SESSION) == SESSION, "a session UUID is an id, not a credential"


# --------------------------------------------------------------------------
# No `[<workspace>]` in any title or label
# --------------------------------------------------------------------------


def _run(tmp_path, args, answers) -> dict:
    import subprocess

    harness = tmp_path / "harness.cjs"
    harness.write_text(_BY_PROMPT)
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"args": args, "answers": answers}))
    done = subprocess.run([_node(), str(harness), str(_RUN_JS), str(fixture)],
                          capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)
    assert "error" not in got, got.get("error")
    return got


def _attached() -> dict:
    return {
        "workflow_id": "wf_a", "workspace": "u-bogdan", "title": "gate", "label": None, "console": None,
        "state": "RUNNING", "state_note": None, "bridge_version": "0.1.0", "follow_error": None, "error": None,
        "steps": [{"step_id": "implement", "task_id": "t1", "depends_on": [], "state": "RUNNING", "console": None}],
    }


_STATUS = {"state": "SUCCEEDED", "state_note": None, "console": None, "steps": []}


def test_no_run_js_label_or_title_carries_the_workspace(tmp_path):
    got = _run(tmp_path, {"attach": "wf_a", "title": "gate", "workspace": "u-bogdan"}, {
        "ATTACH\nworkflow_id: wf_a": _attached(), "step:implement": _step("SUCCEEDED"), "STATUS": _STATUS,
    })
    labels = [c["label"] for c in got["calls"]]
    assert labels == [
        "[SwarmCloud] gate · setup · attach",
        "[SwarmCloud] gate · stage 1 · implement",
        "[SwarmCloud] gate · 1 step · wf_a · setup · result",
    ]
    assert got["result"]["title"] == "SC · gate · 1 step" == workflows.workflow_title("gate", 1)
    assert got["result"]["workspace"] == "u-bogdan", "the result object still says where it ran"
    assert not any(line.startswith("[u-bogdan]") for line in got["logs"]), got["logs"]
    tasks = _run(tmp_path, {"attach_tasks": [{"task_id": "task_a", "title": "lane-a", "workspace": "u-bogdan"}]},
                 {"task_id: task_a": _step("SUCCEEDED")})
    assert tasks["calls"][0]["label"] == "[SwarmCloud] lane-a · task"
    assert not any(line.startswith("[u-bogdan]") for line in tasks["logs"]), tasks["logs"]


def test_no_launch_title_carries_the_workspace(monkeypatch):
    monkeypatch.setattr(workflows, "fetch", lambda client, wid: _envelope([_task("implement")]))
    titles = [
        launch.for_spec(_spec(), _Api())["title"],
        launch.for_attach(_Api(), "wf_a")["title"],
        launch.for_tasks([{"task_id": "t", "label": "lane", "workspace": "eng@saga.xyz"}])["title"],
    ]
    for title in titles:
        assert "[" not in title and "eng" not in title, title
    with pytest.raises(TypeError):
        workflows.workflow_title("gate", 1, "eng")  # type: ignore[call-arg]


def test_the_session_start_notice_puts_no_workspace_in_front_of_a_name():
    text = sc.session_start_context(
        {"workflows": [{"workflow_id": "wf_1", "workspace": "Engineering", "state": "RUNNING", "current_steps": []}]},
        {"tasks": [{"task_id": "task_a", "workspace": "eng@saga.xyz", "state": "RUNNING"}]},
    )
    assert "[Engineering]" not in text and "[eng@saga.xyz]" not in text, text
    assert "wf_1 (RUNNING;" in text and "workspace Engineering)" in text
    assert "workspace eng@saga.xyz)" in text


# --------------------------------------------------------------------------
# Each row's expanded view: its details line
# --------------------------------------------------------------------------


def test_each_step_row_prompt_ends_with_the_details_the_bridge_wrote(tmp_path):
    line = "Workspace: u-bogdan · Started by: dev@example.com · Profiles: claude-code · LLM: Team Max (anthropic) · Browser tool: not used · Session: this session"
    got = _run(tmp_path, {"attach": "wf_a", "title": "gate", "details": {"steps": {"implement": line}}}, {
        "ATTACH\nworkflow_id: wf_a": _attached(), "step:implement": _step("SUCCEEDED"), "STATUS": _STATUS,
    })
    (row,) = [c for c in got["calls"] if c["agentType"] == "sc:step"]
    assert row["prompt"].split("\n")[-1] == "details: " + line


def test_a_row_launched_without_details_says_what_it_knows(tmp_path):
    spec = {"label": "gate", "steps": [{"step_id": "implement", "prompt": "p", "runner_profile": "claude-code-browser",
                                       "inputs": {"browser_tool": "playwright"}}]}
    got = _run(tmp_path, {"spec": spec}, {
        "SUBMIT": {"workflow_id": "wf_s", "workspace": "u-bogdan", "console": None,
                   "steps": [{"step_id": "implement", "task_id": "t1", "depends_on": [], "console": None}],
                   "repository": None, "repository_notes": [], "spec_digest": workflows.spec_digest(spec),
                   "bridge_version": "0.1.0", "follow_error": None, "error": None},
        "step:implement": _step("SUCCEEDED"), "STATUS": _STATUS,
    })
    (row,) = [c for c in got["calls"] if c["agentType"] == "sc:step"]
    assert row["prompt"].split("\n")[-1] == (
        "details: Workspace: u-bogdan · Started by: not reported · Profiles: claude-code-browser · "
        "LLM: not reported · Browser tool: playwright · Session: not reported"
    )
    assert row["label"] == "[SwarmCloud] gate · stage 1 · implement"


def test_a_task_rows_prompt_ends_with_its_details(tmp_path):
    got = _run(tmp_path, {"attach_tasks": [{"task_id": "task_a", "title": "lane-a", "workspace": "eng",
                                            "details": "Workspace: eng · Started by: dev@example.com"}]},
               {"task_id: task_a": _step("SUCCEEDED")})
    assert got["calls"][0]["prompt"].split("\n")[-1] == "details: Workspace: eng · Started by: dev@example.com"


# --------------------------------------------------------------------------
# LLM: <account>, once, after the assignment
# --------------------------------------------------------------------------


class _Row:
    """A task that is RUNNING with no account, then gets one."""

    def __init__(self):
        self.doc = {"id": "t1", "state": "RUNNING", "step_id": "implement", "attempt_count": 1, "max_attempts": 3,
                    "account": {"status": "not_assigned_yet"}, "model": "claude-opus-5-5"}

    def task(self, task_id):
        return dict(self.doc)

    def attempts(self, task_id):
        return []

    def events_page(self, task_id, **_):
        return [], None, False


def test_the_first_progress_line_after_the_assignment_says_the_llm_once():
    row = _Row()
    first = compact.watch_progress(row, ["t1"], step_id="implement")
    assert not any("LLM:" in line for line in first["progress"]), first["progress"]
    row.doc["account"] = {**ASSIGNED, "access_token": _token()}
    second = compact.watch_progress(row, ["t1"], since=first["since"], step_id="implement")
    (line,) = second["progress"]
    assert line.endswith("LLM: acct_max1 (anthropic), model claude-opus-5-5") or "· LLM: acct_max1 (anthropic), model claude-opus-5-5" in line
    assert _token() not in json.dumps(second)
    third = compact.watch_progress(row, ["t1"], since=second["since"], step_id="implement")
    assert not any("LLM:" in line for line in third["progress"]), "said once, never again"


# --------------------------------------------------------------------------
# Docs and release
# --------------------------------------------------------------------------


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_the_skill_and_readme_state_the_details_and_no_workspace_prefix():
    for doc in (_SKILL, _README):
        text = _flat(doc.read_text(encoding="utf-8"))
        assert "[SwarmCloud] [<workspace>]" not in text, doc
        assert "SC · [<workspace>]" not in text, doc
        assert ("`Workspace: <workspace> (<tenant>) · Started by: <submitted_by> · Profiles: <runner profiles> · "
                "LLM: <pool account> (<provider>), model <model> · Browser tool: <browser_tool> · "
                "Session: <claude_session>`") in text, doc
        assert "not reported" in text and "LLM: <account>" in text, doc


def test_the_plugin_is_bumped_and_pins_the_bridge_at_its_own_tag():
    manifest = json.loads(_MANIFEST.read_text())
    version = tuple(int(part) for part in manifest["version"].split("."))
    assert version > _PREVIOUS_RELEASED_VERSION, manifest["version"]
    pinned = manifest["mcpServers"]["swarmcloud"]["args"]
    assert any(f"@sc-v{manifest['version']}#subdirectory=apps/swarm-mcp" in arg for arg in pinned), pinned


def test_the_agents_read_the_details_line_as_display_only():
    for name in ("step", "task"):
        text = _flat((_PLUGIN / "agents" / f"{name}.md").read_text(encoding="utf-8"))
        assert "`details: ...`, is DISPLAY ONLY" in text, name


def test_the_details_module_reads_no_whole_document():
    """The allow-list: only these keys of an account leave `details`."""
    assert details.account_fields({"status": "assigned", "account_id": "a", "provider": "p", "extra": 1}) == {
        "account_id": "a", "provider": "p", "status": "assigned"}
