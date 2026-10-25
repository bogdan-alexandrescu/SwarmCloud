"""BGD's three answers to SC-BROWSER-BUILD, built (SC-BROWSER-FOLLOWUP, #2318; recorded 2026-10-25).

1. SWITCHING LAYOUTS ALSO RE-ATTACHES ("Also re-attach automatically"):
   `/sc layout rows|pane` saves the choice, then queues ONE `/sc attach --all`
   chat turn, so rows in the new layout start beside the old ones, which keep
   running until they end. The engine half is plugin/hooks/pane.test.ts; this
   file holds the skill's and the command's words for the path without
   function hooks.
2. THE PANE OPENS AT SESSION START, UNREAD ("Yes, open it with no read"): in
   the pane layout the pane module opens the pane at session start drawing
   "not read yet" with Refresh, after ONE swarm_layout call and no list read.
   The engine half is pane.test.ts; the frame and the Refresh read are
   pane/model.ts's, run here under node.
3. SESSION START ATTACHES THE PROJECT'S RUNNING WORK (BGD's note): at session
   start only, the work still running for THIS project -- this checkout's
   origin repository, whichever Claude Code session started it -- is named
   and attached (rows layout: `/sc attach --all --project`; pane layout: the
   pane lists it once read). Another repository's work from another session
   is left out, and `/sc check` and the plain `/sc attach --all` stay this
   session's alone (owner decision 2026-10-14).
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from swarm_mcp import browse, check, details, launch, sc, server, session

from test_plugin_agents_and_workflows import _PLUGIN, _node
from test_sc_watch import APP, NOW, OTHER, SESSION, Api, task, workflow

_MODEL = _PLUGIN / "hooks" / "pane" / "model.ts"
_PANE = _PLUGIN / "hooks" / "pane.tsx"
_HOOK = _PLUGIN / "hooks" / "session-start.sh"
_SKILL = _PLUGIN / "skills" / "sc" / "SKILL.md"
_COMMAND = _PLUGIN / "commands" / "sc.md"
_MANIFEST = _PLUGIN / ".claude-plugin" / "plugin.json"
_MARKETPLACE = _PLUGIN.parent / ".claude-plugin" / "marketplace.json"

#: Another Claude Code session of the same person, in the same tenant.
THEIRS = "99999999-2222-4333-8444-555555555555"


@pytest.fixture(autouse=True)
def _project(tmp_path, monkeypatch):
    """This checkout is acme/app; the bridge's config and the project are the test's own."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", "git@github.com:acme/app.git"], check=True)
    monkeypatch.setenv("SWARM_CHECKOUT_DIR", str(checkout))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(checkout))
    monkeypatch.setenv("SWARM_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv(launch.PLUGIN_ROOT_ENV, str(_PLUGIN))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    monkeypatch.delenv(session.HOOK_ENV, raising=False)
    for name in ("SWARM_PLUGIN_MAX_LIVE_ROWS", "SWARM_PLUGIN_ROWS", "SWARM_PLUGIN_LAYOUT", "SWARM_SC_WATCH_MINUTES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(details, "account_labels", lambda _client: {})
    launch.forget_attached()
    yield
    launch.forget_attached()


def _one(wid: str, repo: str, owner: str | None) -> tuple[dict, dict]:
    """A running one-step workflow whose step clones `repo`, tagged `owner`."""
    tid = "task_" + wid
    return (workflow(wid, [("build", tid, "claude-code", [])], session=owner),
            task(tid, "RUNNING", repo=repo, session=owner))


def _world() -> Api:
    """Four running workflows and two running single tasks of the caller:
    this session's in acme/app; another session's and an untagged one in
    acme/app (the PROJECT'S); another session's in acme/other (not)."""
    made = [_one("wf_mine", APP, SESSION), _one("wf_theirs_app", APP, THEIRS),
            _one("wf_untagged_app", APP, None), _one("wf_theirs_other", OTHER, THEIRS)]
    singles = [task("task_single_app", "RUNNING", repo=APP, session=THEIRS),
               task("task_single_other", "RUNNING", repo=OTHER, session=THEIRS)]
    flows = [w for w, _t in made]
    return Api(running=flows, recent=flows, tasks=[t for _w, t in made], singles=singles)


PROJECT = {"wf_mine", "wf_theirs_app", "wf_untagged_app"}


def _session_start(api: Api, monkeypatch, sid: str = SESSION) -> str:
    """`sc workflows --session-start` as the SessionStart hook runs it."""
    monkeypatch.setenv(session.HOOK_ENV, sid)
    out = io.StringIO()
    assert sc.cmd_workflows(api, sc.build_parser().parse_args(["workflows", "--session-start"]), out) == 0
    text = out.getvalue()
    return json.loads(text)["hookSpecificOutput"]["additionalContext"] if text else ""


# --------------------------------------------------------------------------
# 3. Session start: the project's running work
# --------------------------------------------------------------------------


def test_session_start_names_the_projects_running_work_and_leaves_another_repositorys_out(monkeypatch):
    context = _session_start(_world(), monkeypatch)
    for wid in PROJECT:
        assert wid in context  # MUTATION: this session's only -- wf_theirs_app is never tracked here.
    assert "wf_theirs_other" not in context  # MUTATION: the whole tenant -- another repository's work too.
    assert "task_single_app" in context and "task_single_other" not in context
    assert "github.com/acme/app" in context
    # The rows layout: the hook asks the session to attach them, project-wide.
    assert "`/sc attach --all --project`" in context  # MUTATION: plain --all -- the project's work gets no row.


def test_in_the_pane_layout_session_start_says_the_pane_lists_the_projects_work(monkeypatch):
    monkeypatch.setenv("SWARM_PLUGIN_LAYOUT", "pane")
    context = _session_start(_world(), monkeypatch)
    assert "wf_theirs_app" in context and "wf_theirs_other" not in context
    assert "SwarmCloud pane" in context and "Refresh" in context  # MUTATION: no word of where it is listed.
    assert "--project" not in context  # MUTATION: a model row per project workflow in the zero-token layout.


def test_a_session_with_nothing_of_its_own_still_finds_the_projects_work(monkeypatch):
    world = _world()
    world.running = [w for w in world.running if w["workflow_id"] != "wf_mine"]
    context = _session_start(world, monkeypatch, sid="77777777-2222-4333-8444-555555555555")
    assert "wf_theirs_app" in context and "wf_untagged_app" in context  # MUTATION: own-only -- silence.
    assert "wf_theirs_other" not in context


def test_outside_a_checkout_with_an_origin_session_start_stays_this_sessions(tmp_path, monkeypatch):
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.setenv("SWARM_CHECKOUT_DIR", str(bare))
    context = _session_start(_world(), monkeypatch)
    assert "wf_mine" in context
    assert "wf_theirs_app" not in context  # MUTATION: no origin read as "every repository" -- the tenant's.


def _attached(reply: dict) -> tuple[set[str], set[str]]:
    flows = {l["workflow_id"] for l in reply["launches"] if l.get("workflow_id")}
    tasks = {t for l in reply["launches"] for t in l.get("task_ids") or (l.get("args") or {}).get("attach_tasks") or []}
    return flows, tasks


def test_attach_all_project_attaches_the_projects_running_work_and_not_another_repositorys(monkeypatch, tmp_path):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    reply = json.loads(server._call(_world(), "swarm_workflow_launch", {"attach": "all", "project": True}))
    flows, tasks = _attached(reply)
    assert flows == PROJECT  # MUTATION: session-scoped -- only wf_mine.
    assert "task_single_app" in json.dumps(reply) and "task_single_other" not in json.dumps(reply)
    assert reply["project"]["repository"] == "github.com/acme/app"
    assert reply["arm_watch"] is not None


def test_plain_attach_all_and_check_stay_this_sessions(monkeypatch, tmp_path):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    reply = json.loads(server._call(_world(), "swarm_workflow_launch", {"attach": "all"}))
    assert _attached(reply)[0] == {"wf_mine"}  # MUTATION: widen attach-all itself -- every later attach is project-wide.
    result = check.run(_world(), now=NOW, names=lambda _c: {}, repository=APP)
    assert [w["workflow_id"] for w in result["running"]["workflows"]] == ["wf_mine"]  # MUTATION: widen /sc check too.


def test_project_is_taken_only_with_attach_all():
    with pytest.raises(Exception, match="only with attach"):
        server._call(_world(), "swarm_workflow_launch", {"attach": "wf_mine", "project": True})


# --------------------------------------------------------------------------
# 3, pane layout: the pane's list
# --------------------------------------------------------------------------


def test_the_panes_project_read_lists_the_projects_running_work_beside_this_sessions():
    got = browse.read(_world(), {"level": "workflows", "project": True})
    assert {w["workflow_id"] for w in got["workflows"]} == PROJECT  # MUTATION: session only.
    assert [t["task_id"] for t in got["single_tasks"]] == ["task_single_app"]
    assert got["project"]["repository"] == "github.com/acme/app"
    plain = browse.read(_world(), {"level": "workflows"})
    assert {w["workflow_id"] for w in plain["workflows"]} == {"wf_mine"}  # the session read is unchanged


def test_swarm_browse_advertises_project():
    tool = next(t for t in server.TOOLS if t["name"] == "swarm_browse")
    assert tool["inputSchema"]["properties"]["project"]["type"] == "boolean"


def _js(tmp_path: Path, body: str) -> object:
    script = tmp_path / f"probe{len(list(tmp_path.glob('probe*.mjs')))}.mjs"
    script.write_text(f"const m = await import({json.dumps(_MODEL.as_uri())})\n{body}\n")
    done = subprocess.run([_node(), str(script)], capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_unread_pane_draws_not_read_yet_with_refresh_and_refresh_reads_the_project(tmp_path):
    got = _js(tmp_path, """
const state = { ...m.initialState(), project: true }
const lines = m.frame(state, { cols: 66, narrow: false, now: '' })
const calls = []
await m.act(state, 'refresh', async (tool, args) => { calls.push({ tool, args }); return { ok: true, data: { workflows: [] } } }, () => 'T')
process.stdout.write(JSON.stringify({
  text: lines.map((l) => l.map((s) => s.text).join('')),
  presses: lines.flat().filter((s) => s.press).map((s) => s.press),
  calls,
}))""")
    text = "\n".join(got["text"])
    assert "not read yet" in text.lower()  # MUTATION: draw the empty state -- "No SwarmCloud work" before any read.
    assert "No SwarmCloud work" not in text
    assert "refresh" in got["presses"]
    assert got["calls"] == [{"tool": "swarm_browse", "args": {"level": "workflows", "project": True}}]


def test_a_pane_opened_by_hand_still_reads_this_sessions_list(tmp_path):
    got = _js(tmp_path, """
const calls = []
await m.act(m.initialState(), 'open', async (tool, args) => { calls.push(args); return { ok: true, data: {} } }, () => 'T')
process.stdout.write(JSON.stringify(calls))""")
    assert got == [{"level": "workflows"}]


# --------------------------------------------------------------------------
# 1. The re-attach, and the words for 1-3
# --------------------------------------------------------------------------


def test_the_hook_hands_the_layout_option_to_the_bridge():
    loop = next(line for line in _HOOK.read_text().splitlines() if line.startswith("for key in "))
    assert "LAYOUT" in [word.strip(";") for word in loop.split()]  # MUTATION: the bridge never sees the option -- pane users told to attach rows.


def test_the_skill_and_the_command_say_a_switch_reattaches_and_which_attach_is_project_wide():
    skill = " ".join(_SKILL.read_text().split())
    command = " ".join(_COMMAND.read_text().split())
    assert "/sc attach --all --project" in skill and '"project": true' in skill.replace("project: true", '"project": true')
    assert "at session start only" in skill  # MUTATION: no word of when the widening applies.
    assert "`/sc check` and the watch stay scoped to this session" in skill
    for text in (skill, command):
        assert "then queue" in text.lower() or "queues" in text.lower()
        assert "not read yet" in skill


def test_the_version_moved_with_its_pin_and_the_marketplace():
    manifest = json.loads(_MANIFEST.read_text())
    assert manifest["version"] == "0.5.69"  # MUTATION: no bump -- installed copies keep the old hooks.
    assert any("@sc-v0.5.69#" in arg for arg in manifest["mcpServers"]["swarmcloud"]["args"])
    # The marketplace entry pins no version of its own (its source is ./plugin):
    # one it ever names must be this one, or `claude plugin tag` refuses.
    market = json.loads(_MARKETPLACE.read_text())
    assert [p.get("version", "0.5.69") for p in market["plugins"] if p["name"] == "sc"] == ["0.5.69"]


def test_the_pane_module_queues_the_reattach_by_rewriting_the_run_not_by_a_timer():
    pane = _PANE.read_text()
    assert "args: 'attach --all'" in pane  # MUTATION: save and stop -- the new layout starts no rows.
    assert pane.count("$.command.run(") == 1  # the pane's own Layout control, a press, outside any command hook
