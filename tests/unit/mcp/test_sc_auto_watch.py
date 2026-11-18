"""The sc plugin arms its `/sc check` watch by itself (SC-AUTO-WATCH, owner
request 2026-10-14).

"/sc watch should automatically be set if there is at least one workflow being
worked on in sc that is attached here or if and when we start a new workflow,
this should be checked. Also this should run when we attach manually either
one or --all". Held here:

* every verb that puts SwarmCloud work in front of this session -- a spec
  launched or submitted, an issue run whose workflow exists, `attach <id>`,
  `attach --all` and `/sc check`'s attach -- answers `arm_watch`, the exact
  CronCreate call, at the project's saved interval or the 30-minute default;
* an explicit `/sc watch off` is SAVED, and suppresses every one of them until
  `/sc watch` again (the control: the same calls with the preference on);
* nothing attached, or no workflow yet, is no arming;
* the SessionStart hook asks to arm only when something of the caller's runs,
  and never when the project's watch is off;
* the skill says CronList comes first, so a second job is never created.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

from swarm_mcp import check, launch, sc, server, watch
from swarm_mcp.client import SwarmError

from test_sc_watch import APP, NOW, OTHER, SESSION, Api, task, workflow

_REPO = Path(__file__).resolve().parents[3]
_PLUGIN = _REPO / "plugin"
_HOOK = _PLUGIN / "hooks" / "session-start.sh"
_SKILL = _PLUGIN / "skills" / "sc" / "SKILL.md"


@pytest.fixture(autouse=True)
def _this_session(monkeypatch):
    """The session the fixtures were submitted by (SC-SESSION-SCOPE)."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / "sub").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    monkeypatch.setenv("SWARM_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root / "sub"))
    monkeypatch.setenv(launch.PLUGIN_ROOT_ENV, str(_PLUGIN))
    monkeypatch.delenv("SWARM_MCP_CONFIG_FROM", raising=False)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    launch.forget_attached()
    yield root
    launch.forget_attached()


def _armed(minutes: int) -> dict:
    return {"cron": watch.cron_for(minutes), "prompt": "/sc check", "recurring": True}


def _assert_arms(reply: dict, minutes: int = 30) -> None:
    arm = reply["arm_watch"]
    assert arm is not None, reply
    assert arm["tool"] == "CronCreate"
    assert arm["args"] == _armed(minutes)
    assert arm["save_with"] == {"tool": "swarm_check_schedule", "args": {"interval": f"{minutes}m"}}
    assert "CronList" in arm["unless"] and "/sc check" in arm["unless"]


def _off() -> None:
    reply = json.loads(server._call(None, "swarm_check_schedule", {"off": True}))
    assert reply["watch"] == "off"


def _project_api() -> Api:
    ours = workflow("wf_ours", [("a", "task_ours", "claude-code", [])])
    theirs = workflow("wf_theirs", [("a", "task_theirs", "claude-code", [])])
    return Api(running=[ours, theirs],
               tasks=[task("task_ours", "RUNNING"), task("task_theirs", "RUNNING", repo=OTHER)],
               singles=[task("task_mine", "RUNNING")])


class _Recorder:
    """A create that answers a workflow with one step per step sent."""

    tenant = None

    def request(self, method, path, payload=None, **_):
        if method == "GET":
            raise SwarmError(f"GET {path} -> 404")
        steps = [{"step_id": s["step_id"], "task_id": "task_" + s["step_id"], "depends_on": []}
                 for s in (payload or {}).get("steps", [])]
        return {"workflow": {"workflow_id": "wf_new", "steps": steps}, "dispatch": {}}


_SPEC = {"label": "auto watch", "repository_url": APP, "steps": [{"step_id": "a", "prompt": "do the thing"}]}


# --------------------------------------------------------------------------
# Each verb's reply carries the arming call
# --------------------------------------------------------------------------


def test_attach_all_arms_the_watch_at_the_default_interval(project):
    reply = json.loads(server._call(_project_api(), "swarm_workflow_launch", {"attach": "all"}))
    assert reply["launches"]
    _assert_arms(reply)


def test_attach_all_arms_at_the_projects_saved_interval(project):
    server._call(None, "swarm_check_schedule", {"interval": "15m"})
    reply = json.loads(server._call(_project_api(), "swarm_workflow_launch", {"attach": "all"}))
    _assert_arms(reply, 15)


def test_the_checks_attach_arms_only_when_it_attached_something(project):
    args = {"attach": "all", "repository": APP, "new_only": True}
    first = json.loads(server._call(_project_api(), "swarm_workflow_launch", args))
    assert first["launches"]
    _assert_arms(first)
    # The same check again attaches nothing new: nothing new to arm for.
    second = json.loads(server._call(_project_api(), "swarm_workflow_launch", args))
    assert second["launches"] == []
    assert second["arm_watch"] is None


def test_attach_all_with_nothing_running_does_not_arm(project):
    reply = json.loads(server._call(Api(), "swarm_workflow_launch", {"attach": "all"}))
    assert reply["launches"] == [] and reply["arm_watch"] is None


def test_attach_one_workflow_arms_the_watch(project):
    reply = json.loads(server._call(_project_api(), "swarm_workflow_launch", {"attach": "wf_ours"}))
    assert reply["workflow_id"] == "wf_ours"
    _assert_arms(reply)


def test_launching_a_spec_arms_the_watch(project):
    reply = json.loads(server._call(_Recorder(), "swarm_workflow_launch", {"spec": _SPEC}))
    assert reply["script_path"]
    _assert_arms(reply)


def test_submitting_a_workflow_arms_the_watch(project):
    reply = json.loads(server._call(_Recorder(), "swarm_workflow", {"spec": _SPEC, "target": "cloud"}))
    assert reply["workflow_id"] == "wf_new"
    _assert_arms(reply)


class _Runs:
    """`create_run` and the plan routes, answering a run in the state given."""

    tenant = None

    def __init__(self, run: dict) -> None:
        self.run = run

    def create_run(self, **_):
        return self.run

    def approve_plan(self, run_id, **_):
        return self.run

    def request(self, method, path, payload=None, **_):
        raise SwarmError(f"{method} {path} -> 503")


def _issue_run(state: str, workflow_id: str | None) -> dict:
    return {"id": "run_1", "state": state, "terminal": False, "plan_approval": "auto",
            "workflow_id": workflow_id, "ci_fix_workflows": []}


def test_an_issue_run_arms_the_watch_once_its_workflow_exists(project):
    running = json.loads(server._call(_Runs(_issue_run("RUNNING", "wf_issue")), "swarm_run_issue",
                                      {"issue": "acme/app#5", "plan_approval": "auto"}))
    assert running["attach_with"] == "/sc attach wf_issue"
    _assert_arms(running)
    approved = json.loads(server._call(_Runs(_issue_run("RUNNING", "wf_issue")), "swarm_plan_approve",
                                       {"run_id": "run_1", "plan_digest": "sha256:" + "a" * 64}))
    _assert_arms(approved)


def test_an_issue_run_with_no_workflow_yet_does_not_arm(project):
    planning = json.loads(server._call(_Runs(_issue_run("PLANNING", None)), "swarm_run_issue",
                                       {"issue": "acme/app#5"}))
    assert planning["arm_watch"] is None


# --------------------------------------------------------------------------
# `/sc watch off` wins, and is saved
# --------------------------------------------------------------------------


def test_watch_off_suppresses_every_verbs_arming_until_watch_again(project):
    _off()
    replies = [
        json.loads(server._call(_project_api(), "swarm_workflow_launch", {"attach": "all"})),
        json.loads(server._call(_project_api(), "swarm_workflow_launch", {"attach": "wf_ours"})),
        json.loads(server._call(_Recorder(), "swarm_workflow_launch", {"spec": _SPEC})),
        json.loads(server._call(_Recorder(), "swarm_workflow", {"spec": _SPEC, "target": "cloud"})),
        json.loads(server._call(_Runs(_issue_run("RUNNING", "wf_issue")), "swarm_run_issue",
                                {"issue": "acme/app#5"})),
    ]
    for reply in replies:
        assert reply["arm_watch"] is None, reply
        assert "/sc watch off" in reply["arm_watch_off"]

    # `/sc watch` again lifts it.
    server._call(None, "swarm_check_schedule", {"interval": "30m"})
    _assert_arms(json.loads(server._call(_project_api(), "swarm_workflow_launch", {"attach": "wf_ours"})))


def test_watch_off_is_saved_as_off_not_forgotten(project):
    _off()
    status = json.loads(server._call(None, "swarm_check_schedule", {}))
    assert status["watch"] == "off" and status["explicitly_off"] is True
    assert watch.preference(watch.project_dir()) == "off"
    assert watch.read(watch.project_dir()) is None
    assert watch.arm_call() is None


def test_a_project_never_set_is_armed_at_the_default(project):
    assert watch.preference(watch.project_dir()) is None
    status = json.loads(server._call(None, "swarm_check_schedule", {}))
    assert status["watch"] == "off" and status.get("explicitly_off") is False
    assert watch.arm_call()["args"] == _armed(30)


# --------------------------------------------------------------------------
# `/sc check` with nothing running says so, and leaves the job armed
# --------------------------------------------------------------------------


def test_a_check_that_finds_nothing_running_says_so_and_keeps_the_watch():
    result = check.run(Api(), repository=APP, now=NOW, names=lambda _c: {})
    assert "nothing" in result["nothing_running"] and "github.com/acme/app" in result["nothing_running"]
    assert "stays armed" in result["nothing_running"]


def test_a_check_with_work_running_has_no_nothing_running_note():
    result = check.run(_project_api(), repository=APP, now=NOW, names=lambda _c: {})
    assert result["running"]["workflows"]
    assert "nothing_running" not in result


# --------------------------------------------------------------------------
# The SessionStart hook
# --------------------------------------------------------------------------

_LISTING = {"tenant_id": "eng", "workflows": [{"workflow_id": "wf_a", "state": "RUNNING", "steps_total": 1,
                                               "current_steps": [], "age_seconds": 60}]}


def test_the_session_start_context_asks_to_arm_when_something_runs():
    text = sc.session_start_context(_LISTING, None)
    assert "/sc attach --all" in text
    assert "`/sc watch`" in text and "CronList" in text
    singles = {"tenant_id": "eng", "tasks": [{"task_id": "task_a", "state": "RUNNING", "age_seconds": 60}]}
    assert "`/sc watch`" in sc.session_start_context(None, singles)


def test_the_session_start_context_does_not_ask_to_arm_when_off_or_idle():
    off = sc.session_start_context(_LISTING, None, watch_off=True)
    assert "/sc attach --all" in off and "/sc watch" not in off
    assert sc.session_start_context(None, None) is None
    assert sc.session_start_context(None, None, watch_off=True) is None


def test_a_saved_interval_is_re_armed_at_that_interval_not_the_default():
    text = sc.session_start_context(_LISTING, None, watch_minutes=15)
    assert "`/sc watch 15m`" in text and "`/sc watch`" not in text


def test_the_bridge_reads_off_from_the_hook(monkeypatch):
    monkeypatch.setenv(watch.REARM_ENV, "off")
    assert watch.handed_off() is True and watch.rearm_minutes() is None
    monkeypatch.setenv(watch.REARM_ENV, "30")
    assert watch.handed_off() is False


def _hook(tmp_path: Path, project: Path, uv_body: str, **env: str) -> subprocess.CompletedProcess:
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover
        pytest.skip("bash is not installed")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    uv = bin_dir / "uv"
    uv.write_text("#!/usr/bin/env bash\n" + uv_body + "\n")
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    base = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
        "CLAUDE_PLUGIN_OPTION_DEPLOYMENT_URL": "https://swarm.example.test",
        "SWARM_CONFIG_DIR": os.environ["SWARM_CONFIG_DIR"],
        "CLAUDE_PROJECT_DIR": str(project / "sub"),
        "SWARM_ENV_LOG": str(tmp_path / "uv-env"),
    }
    base.update(env)
    return subprocess.run([bash, str(_HOOK)], input="{}", capture_output=True, text=True,
                          timeout=60, env=base, check=False)


def test_the_hook_hands_the_bridge_an_explicit_off(tmp_path, project):
    _off()
    done = _hook(tmp_path, project, 'env > "$SWARM_ENV_LOG"; exit 0')
    assert (done.returncode, done.stdout) == (0, "")
    assert "SWARM_SC_WATCH_MINUTES=off" in (tmp_path / "uv-env").read_text()


def test_the_hook_hands_nothing_for_a_project_never_set(tmp_path, project):
    done = _hook(tmp_path, project, 'env > "$SWARM_ENV_LOG"; exit 0')
    assert (done.returncode, done.stdout) == (0, "")
    assert "SWARM_SC_WATCH_MINUTES" not in (tmp_path / "uv-env").read_text()


def test_the_hook_passes_the_bridges_arming_context_through(tmp_path, project):
    context = sc.session_start_context(_LISTING, None)
    payload = json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}})
    script = tmp_path / "payload.json"
    script.write_text(payload + "\n")
    done = _hook(tmp_path, project, f'cat "{script}"')
    got = json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "`/sc watch`" in got


# --------------------------------------------------------------------------
# The skill
# --------------------------------------------------------------------------


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n### ", start + len(heading))
    return text[start: end if end != -1 else len(text)]


def test_the_skill_makes_arming_the_final_step_of_every_attaching_verb():
    text = _SKILL.read_text()
    arming = _section(text, "### Arming the watch")
    assert arming.index("CronList") < arming.index("CronCreate"), "CronList must come first"
    for named in ("arm_watch", "swarm_check_schedule", "never a second", "/sc watch off"):
        assert named in arming, named
    for heading in ("### `attach <wf_id>`", "### `attach --all`", "### `check`",
                    "### `run <spec path|JSON>`", "### `run --issue owner/repo#N`"):
        assert "Arming the watch" in _section(text, heading), heading


def test_the_skills_watch_section_says_it_arms_itself():
    watch_section = _section(_SKILL.read_text(), "### `watch [interval]`")
    assert "arms itself" in watch_section
    off = _section(_SKILL.read_text(), "### `watch off`")
    assert "until" in off and "`/sc watch`" in off
