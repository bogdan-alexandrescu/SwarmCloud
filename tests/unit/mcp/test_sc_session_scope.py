"""The sc plugin's watch, check and attach candidates are only THIS Claude Code
session's work (SC-SESSION-SCOPE, owner decision 2026-10-14).

"It should filter any potential workflow that is not owned by this claude
session. Even if using the same tenant or workspace in swarmcloud, only the
tasks related to the work this claude code session is doing should pass as
valid candidates to attach if not attached already. Same goes for watching and
checking for staleness." Held here, with no real Claude Code:

* the session id is read from Claude Code's environment, kept across
  /compact and a resume, different for a new session, and moved by /clear
  through the SessionStart hook's record -- driven through the real hook
  script with stdin fixtures;
* every submission path -- swarm_dispatch (one and a batch), swarm_workflow
  (spec, spec_path, spec_ref, merge_pr) and swarm_run_issue -- carries
  `metadata.claude_session` (a run: `claude_session`), and the real swarm-api
  stores it and serves it back;
* `/sc check`, `/sc attach --all` (and the check's `new_only` attach) and the
  SessionStart context return only this session's work, each beside a CONTROL
  from another session and an untagged one that are left out;
* `/sc attach <wf_id>` by hand still attaches another session's workflow, and
  says it is not this session's; `/sc workflows` lists all and marks ours;
* a swarm_dispatch (one and a batch), a SUBMISSION_UNKNOWN or
  SUBMITTED_UNVERIFIED run, and an attach of a finished workflow arm the watch.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

from swarm_mcp import check, launch, sc, server, session, workflows
from swarm_mcp.client import SwarmClient, SwarmError

from test_against_the_real_api import AUTH, DAG, api, db, swarm  # noqa: F401
from test_plugin_agents_and_workflows import _SPEC, _run as _run_js

_REPO = Path(__file__).resolve().parents[3]
_PLUGIN = _REPO / "plugin"
_HOOK = _PLUGIN / "hooks" / "session-start.sh"
_SKILL = _PLUGIN / "skills" / "sc" / "SKILL.md"

HEAD = "a" * 40


def _sid() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    """Launch copies have somewhere to go, and the registry starts empty."""
    monkeypatch.setenv(launch.PLUGIN_ROOT_ENV, str(_PLUGIN))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    launch.forget_attached()
    yield
    launch.forget_attached()


# --------------------------------------------------------------------------
# 1. The session id
# --------------------------------------------------------------------------


def test_the_session_id_is_claude_codes_and_nothing_else_is_one(monkeypatch):
    sid = _sid()
    monkeypatch.setenv(session.SESSION_ENV, sid)
    assert session.current() == sid
    assert session.source() == session.SESSION_ENV
    assert session.tag() == {"claude_session": sid}
    monkeypatch.delenv(session.SESSION_ENV)
    assert session.current() is None and session.tag() == {}
    for junk in ("", "   ", "short", "has space in it", "x" * 200, "../../etc/passwd"):
        monkeypatch.setenv(session.SESSION_ENV, junk)
        assert session.current() is None, junk


def test_the_hooks_own_id_wins_for_the_cli_it_runs(monkeypatch):
    started, handed = _sid(), _sid()
    monkeypatch.setenv(session.SESSION_ENV, started)
    monkeypatch.setenv(session.HOOK_ENV, handed)
    assert session.current() == handed


def _hook(tmp_path: Path, stdin: dict, *, started: str | None, deployment: bool = False,
          uv_body: str = "exit 0") -> subprocess.CompletedProcess:
    """The plugin's real SessionStart hook, with Claude Code's stdin and env
    faked: no Claude Code runs."""
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover
        pytest.skip("bash is not installed")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    uv = bin_dir / "uv"
    uv.write_text("#!/usr/bin/env bash\n" + uv_body + "\n")
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
        "SWARM_CONFIG_DIR": os.environ["SWARM_CONFIG_DIR"],
        "CLAUDE_PROJECT_DIR": str(tmp_path),
        "SWARM_ENV_LOG": str(tmp_path / "uv-env"),
    }
    if started:
        env[session.SESSION_ENV] = started
    if deployment:
        env["CLAUDE_PLUGIN_OPTION_DEPLOYMENT_URL"] = "https://swarm.example.test"
    return subprocess.run([bash, str(_HOOK)], input=json.dumps(stdin), capture_output=True,
                          text=True, timeout=60, env=env, check=False)


def _bridge_sees(started: str) -> str | None:
    """What a bridge started by the Claude Code process `started` reads."""
    return session.current({session.SESSION_ENV: started, "SWARM_CONFIG_DIR": os.environ["SWARM_CONFIG_DIR"]})


def test_the_id_is_stable_across_compact_and_resume_and_new_for_a_new_session(tmp_path):
    first = _sid()
    # A new session: Claude Code starts the bridge and the hook with its id.
    done = _hook(tmp_path, {"session_id": first, "source": "startup"}, started=first)
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    assert _bridge_sees(first) == first
    # /compact: the same session, the same id, nothing recorded.
    _hook(tmp_path, {"session_id": first, "source": "compact"}, started=first)
    assert _bridge_sees(first) == first
    assert not session.record_path(first).exists()
    # `claude --resume <first>`: a new process continuing the same session.
    _hook(tmp_path, {"session_id": first, "source": "resume"}, started=first)
    assert _bridge_sees(first) == first
    # Another `claude` in the same workspace: another session, another id.
    second = _sid()
    _hook(tmp_path, {"session_id": second, "source": "startup"}, started=second)
    assert _bridge_sees(second) == second != first
    assert _bridge_sees(first) == first, "one session's start moved another's id"


def test_clear_moves_the_running_bridge_to_the_new_id_and_a_resume_moves_it_back(tmp_path):
    started, cleared = _sid(), _sid()
    _hook(tmp_path, {"session_id": started, "source": "startup"}, started=started)
    # /clear: the MCP server keeps the environment it started with; the hook
    # fires with the new id on stdin and records it for that starting id.
    done = _hook(tmp_path, {"session_id": cleared, "source": "clear"}, started=started)
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    assert _bridge_sees(started) == cleared
    assert "hook's record" in session.source({session.SESSION_ENV: started,
                                               "SWARM_CONFIG_DIR": os.environ["SWARM_CONFIG_DIR"]})
    # A process started as another session never reads that record.
    other = _sid()
    assert _bridge_sees(other) == other
    # Resuming the first session in that process removes the record.
    _hook(tmp_path, {"session_id": started, "source": "resume"}, started=started)
    assert _bridge_sees(started) == started
    assert not session.record_path(started).exists()


def test_the_matcher_fires_on_clear_too():
    hooks = json.loads((_PLUGIN / "hooks" / "hooks.json").read_text())
    (entry,) = hooks["hooks"]["SessionStart"]
    assert set(entry["matcher"].split("|")) >= {"startup", "resume", "clear"}


def test_the_hook_hands_the_bridge_its_session_id(tmp_path):
    sid = _sid()
    done = _hook(tmp_path, {"session_id": sid, "source": "startup"}, started=sid, deployment=True,
                 uv_body='env > "$SWARM_ENV_LOG"; exit 0')
    assert (done.returncode, done.stdout) == (0, "")
    assert f"SWARM_SC_SESSION_ID={sid}" in (tmp_path / "uv-env").read_text()
    # No id on stdin: none is handed over, and nothing breaks.
    done = _hook(tmp_path, {}, started=None, deployment=True, uv_body='env > "$SWARM_ENV_LOG"; exit 0')
    assert done.returncode == 0
    assert "SWARM_SC_SESSION_ID" not in (tmp_path / "uv-env").read_text()


# --------------------------------------------------------------------------
# 2. Every submission carries the tag, and swarm-api keeps it
# --------------------------------------------------------------------------


def test_a_dispatch_a_batch_and_a_workflow_are_tagged_stored_and_served_back(swarm, api, db, claude_session):  # noqa: F811
    one = json.loads(server._call(swarm, "swarm_dispatch", {"prompt": "one", "profile": "mock"}))
    batch = json.loads(server._call(swarm, "swarm_dispatch", {"tasks": [
        {"prompt": "two", "runner_profile": "mock"}, {"prompt": "three", "runner_profile": "mock"}]}))
    created = json.loads(server._call(swarm, "swarm_workflow", json.loads(json.dumps(DAG))))

    task_ids = [one["task_id"], *batch["task_ids"], *[s["task_id"] for s in created["steps"]]]
    assert len(task_ids) == 5
    for task_id in task_ids:
        assert db.docs[f"tasks/{task_id}"]["metadata"]["claude_session"] == claude_session, task_id
        served = api.get(f"/v1/tasks/{task_id}", headers=AUTH).json()["task"]
        assert served["claude_session"] == claude_session
    state = db.docs[f"tasks/{one['task_id']}"]["state"]
    summary = api.get(f"/v1/tasks?view=summary&state={state}", headers=AUTH).json()["tasks"]
    # The summary's metadata is the title alone (SUMMARY-TITLE).
    assert summary and "claude_session" not in summary[0]["metadata"]
    assert {t["claude_session"] for t in summary} == {claude_session}, \
        "the summary view drops the rest of metadata, so the tag is served beside it"
    listed = api.get("/v1/workflows", headers=AUTH).json()["workflows"]
    assert [w["claude_session"] for w in listed] == [claude_session]
    # Each reply says which session it was tagged with.
    assert one["claude_session"] == batch["claude_session"] == created["claude_session"] == claude_session


def test_untagged_work_is_served_as_untagged_not_left_out(swarm, api, db):  # noqa: F811
    created = json.loads(server._call(swarm, "swarm_workflow", json.loads(json.dumps(DAG))))
    for step in created["steps"]:
        assert "claude_session" not in db.docs[f"tasks/{step['task_id']}"]["metadata"]
    (row,) = api.get("/v1/workflows", headers=AUTH).json()["workflows"]
    assert "claude_session" in row and row["claude_session"] is None
    assert created["claude_session"] is None and "does not know" in created["claude_session_note"]


class _Recorder:
    """The transport, stubbed at `request`, behind the real client methods."""

    tenant = None
    dispatch = SwarmClient.dispatch

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict | None]] = []

    def request(self, method, path, payload=None, **_):
        self.sent.append((method, path, payload))
        if method == "GET":
            raise SwarmError(f"GET {path} -> 404")
        steps = [{"step_id": s["step_id"], "task_id": f"task_{s['step_id']}",
                  "runner_profile": s.get("runner_profile"), "depends_on": []}
                 for s in (payload or {}).get("steps", [])]
        return {"workflow": {"workflow_id": "wf_1", "steps": steps}, "dispatch": {}}

    def body(self) -> dict:
        ((method, path, payload),) = [s for s in self.sent if s[0] == "POST"]
        assert (method, path) == ("POST", "/v1/workflows")
        return payload


def test_spec_path_spec_ref_and_merge_pr_submissions_are_tagged(tmp_path, monkeypatch, claude_session):
    monkeypatch.setenv("SWARM_CHECKOUT_DIR", str(tmp_path))
    spec = {"label": "scoped", "steps": [{"step_id": "a", "prompt": "do a"}]}
    (tmp_path / "w.json").write_text(json.dumps(spec))

    by_path = _Recorder()
    server._call(by_path, "swarm_workflow", {"spec_path": str(tmp_path / "w.json"),
                                              "spec_digest": workflows.spec_digest(spec)})
    held = json.loads(server._call(None, "swarm_workflow_spec", {"path": "w.json"}))
    by_ref = _Recorder()
    server._call(by_ref, "swarm_workflow", {"spec_ref": held["spec_ref"], "spec_digest": held["spec_digest"]})
    inline = _Recorder()
    server._call(inline, "swarm_workflow", {"spec": spec})
    merge = _Recorder()
    server._call(merge, "swarm_workflow", {"merge_pr": {"number": 41, "head_sha": HEAD}})

    for recorder in (by_path, by_ref, inline, merge):
        assert recorder.body()["metadata"]["claude_session"] == claude_session
        assert recorder.body()["metadata"]["origin"] == "swarm-mcp"


class _RunApi:
    """`POST /v1/runs`, recorded; `refuse_field` plays a deployment that
    predates `claude_session` and refuses it as an extra."""

    tenant = None

    def __init__(self, *, refuse_field: bool = False) -> None:
        self.sent: list[dict] = []
        self.refuse_field = refuse_field

    def request(self, method, path, payload=None, **_):
        if method == "POST" and path == "/v1/runs":
            self.sent.append(dict(payload))
            if self.refuse_field and "claude_session" in payload:
                raise SwarmError("POST /v1/runs -> 422: body.claude_session: Extra inputs are not "
                                 "permitted", status=422)
            return {"run": {"id": "run_1", "state": "PLANNING", "terminal": False, "workflow_id": None,
                            "plan_approval": payload["plan_approval"], "ci_fix_workflows": [],
                            "claude_session": payload.get("claude_session")}}
        raise SwarmError(f"{method} {path} -> 404")


def _client(api_: _RunApi) -> SwarmClient:
    client = object.__new__(SwarmClient)
    client.request = api_.request  # type: ignore[method-assign]
    client.tenant = None
    return client


def test_an_issue_run_carries_the_session_and_says_so(claude_session):
    runs = _RunApi()
    reply = json.loads(server._call(_client(runs), "swarm_run_issue", {"issue": "acme/app#5"}))
    assert runs.sent[0]["claude_session"] == claude_session
    assert reply["claude_session"] == claude_session


def test_a_deployment_that_refuses_the_runs_tag_still_starts_the_run_untagged_and_says_so(claude_session):
    runs = _RunApi(refuse_field=True)
    reply = json.loads(server._call(_client(runs), "swarm_run_issue", {"issue": "acme/app#5"}))
    assert [("claude_session" in sent) for sent in runs.sent] == [True, False]
    assert "untagged" in reply["session_tag"]
    assert reply["claude_session"] is None


def test_with_no_session_known_nothing_is_tagged():
    runs = _RunApi()
    server._call(_client(runs), "swarm_run_issue", {"issue": "acme/app#5"})
    assert "claude_session" not in runs.sent[0]
    recorder = _Recorder()
    server._call(recorder, "swarm_workflow", {"spec": {"steps": [{"step_id": "a", "prompt": "a"}]}})
    assert "claude_session" not in recorder.body()["metadata"]


# --------------------------------------------------------------------------
# 3. The scope: own work only, with the controls left out
# --------------------------------------------------------------------------


def _three_sessions(swarm, monkeypatch, *, dispatch: bool = True) -> dict[str, dict]:  # noqa: F811
    """One workflow (and one single task) each from THIS session, ANOTHER
    session in the same tenant, and NO session (untagged), all running."""
    mine, theirs = _sid(), _sid()
    made: dict[str, dict] = {}
    for who, sid in (("mine", mine), ("theirs", theirs), ("untagged", None)):
        if sid:
            monkeypatch.setenv(session.SESSION_ENV, sid)
        else:
            monkeypatch.delenv(session.SESSION_ENV, raising=False)
        wf = json.loads(server._call(swarm, "swarm_workflow", json.loads(json.dumps(DAG))))
        task = (json.loads(server._call(swarm, "swarm_dispatch", {"prompt": f"single {who}", "profile": "mock"}))
                if dispatch else None)
        made[who] = {"sid": sid, "workflow_id": wf["workflow_id"], "task_id": task and task["task_id"]}
    monkeypatch.setenv(session.SESSION_ENV, mine)
    return made


def _attach_ids(reply: dict) -> tuple[list[str], list[str]]:
    workflow_ids = [l["workflow_id"] for l in reply["launches"] if l.get("workflow_id")]
    task_ids = [t for l in reply["launches"] for t in l.get("task_ids") or []]
    return workflow_ids, task_ids


def test_attach_all_attaches_only_this_sessions_work(plugin, swarm, api, db, monkeypatch):  # noqa: F811
    made = _three_sessions(swarm, monkeypatch)
    reply = json.loads(server._call(swarm, "swarm_workflow_launch", {"attach": "all"}))
    assert _attach_ids(reply) == ([made["mine"]["workflow_id"]], [made["mine"]["task_id"]])
    assert reply["session"]["session_id"] == made["mine"]["sid"]
    assert reply["session"]["left_out"] == {
        "workflows": {"other_session": 1, "untagged": 1},
        "single_tasks": {"other_session": 1, "untagged": 1},
    }
    assert reply["arm_watch"] is not None


def test_the_checks_new_only_attach_takes_only_this_sessions_work(plugin, swarm, api, db, monkeypatch):  # noqa: F811
    made = _three_sessions(swarm, monkeypatch)
    # swarm_dispatch's own rows registered the tasks; a new session's check
    # starts with nothing attached.
    launch.forget_attached()
    args = {"attach": "all", "repository": "all", "new_only": True}
    first = json.loads(server._call(swarm, "swarm_workflow_launch", args))
    assert _attach_ids(first) == ([made["mine"]["workflow_id"]], [made["mine"]["task_id"]])
    again = json.loads(server._call(swarm, "swarm_workflow_launch", args))
    assert _attach_ids(again) == ([], [])
    assert again["arm_watch"] is None


def test_check_reports_only_this_sessions_work(swarm, api, db, monkeypatch):  # noqa: F811
    made = _three_sessions(swarm, monkeypatch)
    result = check.run(swarm, repository="all", names=lambda _c: {})
    assert [w["workflow_id"] for w in result["running"]["workflows"]] == [made["mine"]["workflow_id"]]
    assert [t["task_id"] for t in result["running"]["single_tasks"]] == [made["mine"]["task_id"]]
    assert result["session"]["left_out"] == {
        "workflows": {"other_session": 1, "untagged": 1},
        "single_tasks": {"other_session": 1, "untagged": 1},
    }
    flagged_ids = {i.get("workflow_id") for i in result["flagged"]} | {i.get("task_id") for i in result["flagged"]}
    for who in ("theirs", "untagged"):
        assert made[who]["workflow_id"] not in flagged_ids and made[who]["task_id"] not in flagged_ids


def test_a_check_with_no_session_known_has_no_candidates_and_says_why(swarm, api, db, monkeypatch):  # noqa: F811
    _three_sessions(swarm, monkeypatch)
    monkeypatch.delenv(session.SESSION_ENV)
    result = check.run(swarm, repository="all", names=lambda _c: {})
    assert result["running"] == {"workflows": [], "single_tasks": []}
    assert "does not know" in result["session"]["scope"]
    assert result["session"]["left_out"]["workflows"] == {"other_session": 2, "untagged": 1}


def _session_start(client, sid: str | None, monkeypatch) -> str:
    """`sc workflows --session-start` as the hook runs it, handed `sid`."""
    monkeypatch.delenv(session.SESSION_ENV, raising=False)
    if sid:
        monkeypatch.setenv(session.HOOK_ENV, sid)
    out = io.StringIO()
    code = sc.cmd_workflows(client, sc.build_parser().parse_args(["workflows", "--session-start"]), out)
    assert code == 0
    return out.getvalue()


def test_the_session_start_context_names_only_this_sessions_work(swarm, api, db, monkeypatch):  # noqa: F811
    made = _three_sessions(swarm, monkeypatch)
    context = json.loads(_session_start(swarm, made["mine"]["sid"], monkeypatch))[
        "hookSpecificOutput"]["additionalContext"]
    assert made["mine"]["workflow_id"] in context and made["mine"]["task_id"] in context
    for who in ("theirs", "untagged"):
        assert made[who]["workflow_id"] not in context and made[who]["task_id"] not in context
    assert "1 SwarmCloud workflow is running" in context
    assert "`/sc watch`" in context, "work of this session runs: the context arms the watch"


def test_a_new_session_is_told_nothing_about_another_sessions_work(swarm, api, db, monkeypatch):  # noqa: F811
    _three_sessions(swarm, monkeypatch)
    assert _session_start(swarm, _sid(), monkeypatch) == ""
    assert _session_start(swarm, None, monkeypatch) == ""


def test_sc_workflows_lists_the_whole_tenant_and_marks_this_sessions(swarm, api, db, monkeypatch):  # noqa: F811
    made = _three_sessions(swarm, monkeypatch)
    out = io.StringIO()
    sc.cmd_workflows(swarm, sc.build_parser().parse_args(["workflows", "--json"]), out)
    payload = json.loads(out.getvalue())
    marks = {w["workflow_id"]: w["this_session"] for w in payload["workflows"]}
    assert marks == {made["mine"]["workflow_id"]: True, made["theirs"]["workflow_id"]: False,
                     made["untagged"]["workflow_id"]: False}
    tasks = {t["task_id"]: t["this_session"] for t in payload["single_tasks"]["tasks"]}
    assert tasks[made["mine"]["task_id"]] is True and tasks[made["theirs"]["task_id"]] is False
    out = io.StringIO()
    sc.cmd_workflows(swarm, sc.build_parser().parse_args(["workflows"]), out)
    marked = [line for line in out.getvalue().splitlines() if "[this session]" in line and " wf_" in line]
    assert len(marked) == 1 and made["mine"]["workflow_id"] in marked[0]


# --------------------------------------------------------------------------
# 4. By hand, any workflow the tenant may read -- and the reply says whose
# --------------------------------------------------------------------------


def test_attaching_another_sessions_workflow_by_hand_works_and_says_so(plugin, swarm, api, db, monkeypatch):  # noqa: F811
    made = _three_sessions(swarm, monkeypatch, dispatch=False)
    theirs = json.loads(server._call(swarm, "swarm_workflow_launch", {"attach": made["theirs"]["workflow_id"]}))
    assert theirs["workflow_id"] == made["theirs"]["workflow_id"] and theirs["script_path"]
    assert theirs["this_session"] is False and made["theirs"]["sid"] in theirs["not_this_session"]
    untagged = json.loads(server._call(swarm, "swarm_workflow_launch", {"attach": made["untagged"]["workflow_id"]}))
    assert untagged["this_session"] is False and "no Claude Code session tag" in untagged["not_this_session"]
    mine = json.loads(server._call(swarm, "swarm_workflow_launch", {"attach": made["mine"]["workflow_id"]}))
    assert mine["this_session"] is True and "not_this_session" not in mine


# --------------------------------------------------------------------------
# 5. What arms the watch
# --------------------------------------------------------------------------


def _assert_arms(reply: dict) -> None:
    arm = reply["arm_watch"]
    assert arm is not None and arm["tool"] == "CronCreate", reply
    assert arm["args"]["prompt"] == "/sc check"


def test_a_single_dispatch_and_a_batch_arm_the_watch(plugin, swarm, api, db, claude_session):  # noqa: F811
    _assert_arms(json.loads(server._call(swarm, "swarm_dispatch", {"prompt": "hi", "profile": "mock"})))
    _assert_arms(json.loads(server._call(swarm, "swarm_dispatch", {"tasks": [
        {"prompt": "a", "runner_profile": "mock"}]})))


def test_a_dispatch_with_the_watch_off_arms_nothing_and_says_why(plugin, swarm, api, db, claude_session):  # noqa: F811
    server._call(None, "swarm_check_schedule", {"off": True})
    reply = json.loads(server._call(swarm, "swarm_dispatch", {"prompt": "hi", "profile": "mock"}))
    assert reply["arm_watch"] is None and "/sc watch off" in reply["arm_watch_off"]


def test_attaching_a_finished_workflow_arms_the_watch(plugin, swarm, api, db, claude_session):  # noqa: F811
    created = json.loads(server._call(swarm, "swarm_workflow", json.loads(json.dumps(DAG))))
    for step in created["steps"]:
        db.docs[f"tasks/{step['task_id']}"]["state"] = "SUCCEEDED"
    reply = json.loads(server._call(swarm, "swarm_workflow_launch", {"attach": created["workflow_id"]}))
    _assert_arms(reply)


@pytest.mark.parametrize("answer", [None, {"__throw": "StructuredOutput failed validation after 5 attempts"}],
                         ids=["stopped", "threw"])
def test_a_submission_of_unknown_outcome_arms_the_watch(tmp_path, answer):
    got = _run_js(tmp_path, _SPEC, {"SUBMIT": answer})
    assert got["result"]["state"] == "SUBMISSION_UNKNOWN"
    assert "arm_watch" in got["result"]["arm_watch"] and "CronList" in got["result"]["arm_watch"]


def test_a_submitted_unverified_workflow_arms_the_watch(tmp_path):
    from test_plugin_agents_and_workflows import _ANSWERS

    submitted = json.loads(json.dumps(_ANSWERS["SUBMIT"]))
    submitted["steps"] = submitted["steps"][:-1]
    got = _run_js(tmp_path, _SPEC, {**_ANSWERS, "SUBMIT": submitted})
    assert got["result"]["state"] == "SUBMITTED_UNVERIFIED"
    assert "arm_watch" in got["result"]["arm_watch"]


def test_a_refused_submission_arms_nothing(tmp_path):
    refused = {"workflow_id": None, "steps": [], "repository": None, "spec_digest": None,
               "error": "the spec was refused"}
    got = _run_js(tmp_path, _SPEC, {"SUBMIT": refused})
    assert got["result"]["state"] == "NOT_SUBMITTED"
    assert "arm_watch" not in got["result"]


# --------------------------------------------------------------------------
# 6. The skill says all of it
# --------------------------------------------------------------------------


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n### ", start + len(heading))
    return " ".join(text[start: end if end != -1 else len(text)].split())


def test_the_skill_scopes_check_watch_attach_all_and_workflows_to_this_session():
    text = _SKILL.read_text()
    for heading in ("### `check`", "### `attach --all`", "### `watch [interval]`"):
        assert "this Claude Code session" in _section(text, heading), heading
    assert "not this session's" in _section(text, "### `attach <wf_id>`")
    assert "[this session]" in _section(text, "### `workflows`")
    arming = _section(text, "### Arming the watch")
    for named in ("swarm_dispatch", "SUBMISSION_UNKNOWN", "SUBMITTED_UNVERIFIED", "already finished"):
        assert named in arming, named


def test_the_bridge_and_the_api_agree_on_the_tag():
    """docs/mirrored-values.md: the key and the id shape, in three copies."""
    from swarm_api import sessiontag
    from swarm_mcp import client

    assert session.METADATA_KEY == sessiontag.KEY == client.SESSION_KEY
    assert session._SHAPE.pattern == sessiontag.PATTERN


# --------------------------------------------------------------------------
# 7. The /sc:swarmcloud fallback attaches only what swarm_workflows marks
# --------------------------------------------------------------------------


def test_the_list_tool_marks_this_sessions_work(swarm, api, db, monkeypatch):  # noqa: F811
    made = _three_sessions(swarm, monkeypatch)
    reply = json.loads(server._call(swarm, "swarm_workflows", {}))
    assert {w["workflow_id"]: w["this_session"] for w in reply["workflows"]} == {
        made["mine"]["workflow_id"]: True, made["theirs"]["workflow_id"]: False,
        made["untagged"]["workflow_id"]: False}
    assert {t["task_id"]: t["this_session"] for t in reply["single_tasks"]}[made["mine"]["task_id"]] is True
    assert reply["session"]["session_id"] == made["mine"]["sid"]


def test_run_js_attach_all_attaches_only_entries_marked_this_session(tmp_path):
    listed = {"count": 3, "error": None, "single_tasks_error": None, "workflows": [
        {"workflow_id": "wf_mine", "label": "mine", "title": None, "state": "RUNNING", "console": None,
         "this_session": True},
        {"workflow_id": "wf_theirs", "label": "theirs", "title": None, "state": "RUNNING", "console": None,
         "this_session": False},
        {"workflow_id": "wf_unmarked", "label": "unmarked", "title": None, "state": "RUNNING", "console": None},
    ], "single_tasks": [
        {"task_id": "task_mine", "label": None, "state": "RUNNING", "console": None, "this_session": True},
        {"task_id": "task_theirs", "label": None, "state": "RUNNING", "console": None, "this_session": False},
    ]}
    got = _run_js(tmp_path, {"attach": "all"}, {"LIST": listed})
    result = got["result"]
    assert [c["attach"] for c in result["attach_calls"]] == ["wf_mine"]
    assert [t["task_id"] for t in result["task_call"]["attach_tasks"]] == ["task_mine"]
    assert any("not this session's" in line for line in got["logs"]), got["logs"]
