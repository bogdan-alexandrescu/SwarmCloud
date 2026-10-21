"""The build board keeps itself current (BOARD-WIRE, part of #1773).

Owner requirements, 2026-10-20 (#1773): the board is "updated automatically
when work is dispatched, and as it progresses", and opened with
`/sc build-board`. The lane's brief turns that into:

* REFRESH IN THE BACKGROUND, never delaying or failing the tool that
  triggered it: errors are swallowed and logged, triggers coalesce.
* ALWAYS after a successful submission (`swarm_workflow`, `swarm_dispatch`
  for one task or several, `swarm_run_issue`), after `swarm_check`, and after
  `swarm_workflow_launch` -- a submission's refresh is never dropped by the
  follow throttle.
* AFTER A ROW'S FOLLOW SEES A STATE CHANGE, at most once per 2 minutes per
  project; a change inside the window is written when the window closes.
* AT SESSION START, once, silently, inside the hook's time budget, only in a
  project that has a board folder or a run record.

Every assertion carries the MUTATION it catches.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

from swarm_mcp import buildboard, server
from test_plugin_skills import _load

_REPO = Path(__file__).resolve().parents[3]
_PLUGIN = _REPO / "plugin"
_MANIFEST = _PLUGIN / ".claude-plugin" / "plugin.json"
_HOOK = _PLUGIN / "hooks" / "session-start.sh"
_SKILL = _PLUGIN / "skills" / "sc" / "SKILL.md"
_README = _PLUGIN / "README.md"

#: The version BOARD-CORE (#1794) left the plugin at; this lane ships one patch past it.
_VERSION_BEFORE = (0, 5, 47)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Rig:
    """One `AutoRefresh` whose refresh, project, clock, thread and timer are
    the test's: a refresh runs synchronously, and a deferred one is held in
    `armed` until the test fires it."""

    def __init__(self, project: Path, *, refresh=None, start=None) -> None:
        self.project = project
        self.clock = _Clock()
        self.writes: list[tuple[object, Path, float]] = []
        self.armed: list[tuple[float, object]] = []
        self.auto = buildboard.AutoRefresh(
            refresh=refresh or self._refresh,
            project=lambda: self.project,
            clock=self.clock,
            start=start or (lambda run: run()),
            after=lambda seconds, run: self.armed.append((seconds, run)),
        )

    def _refresh(self, client, project):
        self.writes.append((client, project, self.clock.now))
        return {"path": str(project / ".claude" / "build-board" / "index.html")}


def _follow(*rows: tuple[str, str], **extra) -> str:
    return json.dumps({"tasks": [{"task_id": t, "state": s} for t, s in rows], **extra})


# --------------------------------------------------------------------------
# The refresher
# --------------------------------------------------------------------------


def test_a_submission_writes_the_board_even_inside_the_throttle_window(tmp_path):
    rig = _Rig(tmp_path)
    client = object()
    rig.auto.followed(client, _follow(("task_1", "QUEUED")))
    rig.clock.now += 10
    rig.auto.followed(client, _follow(("task_1", "RUNNING")))
    assert len(rig.writes) == 1  # MUTATION: no refresh on a state change -- 0.
    rig.clock.now += 20
    rig.auto.submitted(client)
    assert len(rig.writes) == 2  # MUTATION: throttle submissions like follows -- still 1.
    assert rig.writes[-1][:2] == (client, tmp_path)  # MUTATION: refresh another project or client -- differs.
    rig.clock.now += 5
    rig.auto.submitted(client)
    assert len(rig.writes) == 3  # MUTATION: throttle a second submission -- still 2.


def test_two_follow_state_changes_within_two_minutes_write_once(tmp_path):
    rig = _Rig(tmp_path)
    client = object()
    rig.auto.followed(client, _follow(("task_1", "QUEUED"), ("task_2", "QUEUED")))
    assert rig.writes == []  # MUTATION: count a first sighting as a change -- a write.
    rig.clock.now += 10
    rig.auto.followed(client, _follow(("task_1", "RUNNING")))
    rig.clock.now += 60
    rig.auto.followed(client, _follow(("task_1", "SUCCEEDED")))
    assert len(rig.writes) == 1  # MUTATION: no throttle -- 2 writes.
    assert len(rig.armed) == 1 and rig.armed[0][0] == pytest.approx(60.0)  # MUTATION: drop the change -- nothing armed for the window's end.
    rig.clock.now += 1
    rig.auto.followed(client, _follow(("task_2", "RUNNING")))
    assert len(rig.armed) == 1  # MUTATION: arm a timer per change -- 2.
    rig.clock.now += 59
    rig.armed[0][1]()
    assert len(rig.writes) == 2  # MUTATION: the deferred refresh never writes -- still 1.


def test_a_follow_without_a_change_writes_nothing(tmp_path):
    rig = _Rig(tmp_path)
    client = object()
    rig.auto.followed(client, _follow(("task_1", "RUNNING")))
    rig.clock.now += 300
    rig.auto.followed(client, _follow(("task_1", "RUNNING")))
    rig.auto.followed(client, "not json at all")
    rig.auto.followed(client, json.dumps({"tasks": "nope"}))
    assert rig.writes == []  # MUTATION: refresh on every follow -- writes.
    rig.auto.followed(client, _follow(("task_9", "RUNNING"), transitions=["[a] QUEUED → RUNNING"]))
    assert len(rig.writes) == 1  # MUTATION: ignore the reply's own transitions -- 0.


def test_a_deferred_refresh_a_newer_one_already_covered_writes_nothing(tmp_path):
    rig = _Rig(tmp_path)
    client = object()
    rig.auto.followed(client, _follow(("task_1", "QUEUED")))
    rig.auto.followed(client, _follow(("task_1", "RUNNING")))
    rig.clock.now += 30
    rig.auto.followed(client, _follow(("task_1", "SUCCEEDED")))
    rig.clock.now += 10
    rig.auto.submitted(client)
    rig.clock.now += 80
    rig.armed[0][1]()
    assert len(rig.writes) == 2  # MUTATION: write again what the submission's refresh already showed -- 3.


def test_triggers_while_a_refresh_runs_coalesce_into_one_more(tmp_path):
    held: list = []
    rig = _Rig(tmp_path, start=held.append)
    client = object()
    rig.auto.submitted(client)
    rig.auto.submitted(client)
    rig.auto.submitted(client)
    assert len(held) == 1  # MUTATION: a thread per trigger -- 3.
    assert rig.writes == []  # MUTATION: refresh in the caller's thread -- written already.
    held[0]()
    assert len(rig.writes) == 1  # MUTATION: one refresh per trigger -- 3.
    rig.auto.submitted(client)
    assert len(held) == 2  # MUTATION: never start again after the first drain -- 1.


def test_a_raising_refresh_is_logged_and_the_next_trigger_still_runs(tmp_path, capsys):
    calls: list = []

    def boom(client, project):
        calls.append(project)
        raise RuntimeError("the disk went away")

    rig = _Rig(tmp_path, refresh=boom)
    rig.auto.submitted(object())
    assert "the disk went away" in capsys.readouterr().err  # MUTATION: swallow silently -- nothing logged.
    rig.auto.submitted(object())
    assert len(calls) == 2  # MUTATION: leave the refresher marked busy after a raise -- 1.


def test_a_failed_refresh_is_logged(tmp_path, capsys):
    rig = _Rig(tmp_path, refresh=lambda client, project: {"error": "HTTP 503 from swarm-api", "path": None})
    rig.auto.submitted(object())
    assert "HTTP 503" in capsys.readouterr().err  # MUTATION: drop the refresh's error -- nothing logged.


# --------------------------------------------------------------------------
# The bridge's trigger points
# --------------------------------------------------------------------------


class _Spy:
    def __init__(self) -> None:
        self.forced: list = []
        self.follows: list = []

    def submitted(self, client):
        self.forced.append(client)

    def followed(self, client, reply):
        self.follows.append(reply)


@pytest.mark.parametrize("name", ["swarm_workflow", "swarm_dispatch", "swarm_workflow_launch",
                                  "swarm_run_issue", "swarm_check"])
def test_every_submission_check_and_launch_refreshes_unthrottled(name, monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(buildboard, "AUTO", spy)
    monkeypatch.setattr(server, "_call", lambda client, n, args, **_: '{"ok": true}')
    server._answered(object(), name, {})
    assert len(spy.forced) == 1  # MUTATION: leave this tool out of the forced set -- 0.


def test_a_follow_hands_its_reply_to_the_throttled_path_and_a_read_triggers_nothing(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(buildboard, "AUTO", spy)
    monkeypatch.setattr(server, "_call", lambda client, n, args, **_: _follow(("task_1", "RUNNING")))
    server._answered(object(), "swarm_follow", {})
    assert spy.follows == [_follow(("task_1", "RUNNING"))] and spy.forced == []  # MUTATION: force a follow's refresh -- forced.
    server._answered(object(), "swarm_status", {})
    server._answered(object(), "swarm_build_board", {})
    assert len(spy.follows) == 1 and spy.forced == []  # MUTATION: refresh after every tool -- more.


def test_a_tool_that_fails_triggers_no_refresh(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(buildboard, "AUTO", spy)

    def refused(client, n, args, **_):
        raise server.SwarmError("nothing was sent")

    monkeypatch.setattr(server, "_call", refused)
    with pytest.raises(server.SwarmError):
        server._answered(object(), "swarm_dispatch", {})
    assert spy.forced == []  # MUTATION: refresh after a refused submission -- 1.


def test_a_raising_refresh_leaves_the_tools_reply_unchanged(tmp_path, monkeypatch, capsys):
    reply = json.dumps({"workflow_id": "wf_1", "steps": []}, indent=2)
    monkeypatch.setattr(server, "_call", lambda client, n, args, **_: reply)

    def boom(client, project):
        raise OSError(28, "No space left on device")

    rig = _Rig(tmp_path, refresh=boom)
    monkeypatch.setattr(buildboard, "AUTO", rig.auto)
    assert server._answered(object(), "swarm_workflow", {}) == reply  # MUTATION: let the refresh's error replace the reply -- raises.
    assert "No space left" in capsys.readouterr().err  # MUTATION: swallow without logging -- empty.

    def broken(client, name, text):
        raise RuntimeError("the trigger itself broke")

    monkeypatch.setattr(buildboard, "after_tool", broken)
    assert server._answered(object(), "swarm_workflow", {}) == reply  # MUTATION: no guard around the trigger -- raises.


def test_the_refresh_never_delays_the_reply(tmp_path, monkeypatch):
    reply = '{"task_id": "task_1"}'
    monkeypatch.setattr(server, "_call", lambda client, n, args, **_: reply)
    held: list = []
    rig = _Rig(tmp_path, start=held.append)
    monkeypatch.setattr(buildboard, "AUTO", rig.auto)
    assert server._answered(object(), "swarm_dispatch", {}) == reply  # MUTATION: answer with the refresh's result -- differs.
    assert rig.writes == [] and len(held) == 1  # MUTATION: refresh before answering -- written already.


def test_the_stdio_loop_answers_through_the_trigger_point():
    import inspect

    assert "_answered(" in inspect.getsource(server.serve)  # MUTATION: serve calls _call directly -- no refresh ever runs.


def test_the_real_refresher_runs_in_a_daemon_thread(tmp_path):
    done = []
    auto = buildboard.AutoRefresh(refresh=lambda client, project: done.append(project) or {},
                                  project=lambda: tmp_path)
    auto.submitted(object())
    deadline = time.monotonic() + 5
    while not done and time.monotonic() < deadline:
        time.sleep(0.01)
    assert done == [tmp_path]  # MUTATION: never start the thread -- nothing refreshed.


# --------------------------------------------------------------------------
# Session start
# --------------------------------------------------------------------------

#: The fake bridge: every invocation's argv on one line of the log; the board
#: refresh does whatever SWARM_BOARD_BODY says, the listing prints nothing.
_FAKE_UV = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$SWARM_ARGS_LOG"
case "$*" in
  *build-board*) eval "${SWARM_BOARD_BODY:-exit 0}" ;;
  *) eval "${SWARM_LIST_BODY:-exit 0}" ;;
esac
"""

_CONTEXT = json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                              "additionalContext": "1 SwarmCloud workflow is running: wf_a."}})


def _hook(tmp_path: Path, project: Path, **env: str) -> subprocess.CompletedProcess:
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover
        pytest.skip("bash is not installed")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    uv = bin_dir / "uv"
    uv.write_text(_FAKE_UV)
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    base = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
        "CLAUDE_PLUGIN_OPTION_DEPLOYMENT_URL": "https://swarm.example.test",
        "CLAUDE_PROJECT_DIR": str(project),
        "SWARM_ARGS_LOG": str(tmp_path / "uv-args"),
    }
    base.update(env)
    return subprocess.run(
        [bash, str(_HOOK)],
        input=json.dumps({"hook_event_name": "SessionStart", "source": "startup", "cwd": str(project)}),
        capture_output=True, text=True, timeout=60, env=base, check=False,
    )


def _calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "uv-args"
    return log.read_text().splitlines() if log.exists() else []


def _project(tmp_path: Path, kind: str) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    if kind == "board":
        (project / ".claude" / "build-board").mkdir(parents=True)
    elif kind == "record":
        (project / ".sc-swarmcloud-runs").mkdir()
        (project / ".sc-swarmcloud-runs" / "launched.jsonl").write_text("{}\n")
    elif kind == "legacy-record":
        (project / ".claude" / "sc-swarmcloud-runs").mkdir(parents=True)
        (project / ".claude" / "sc-swarmcloud-runs" / "launched.jsonl").write_text("{}\n")
    return project


@pytest.mark.parametrize("kind", ["board", "record", "legacy-record"])
def test_the_hook_refreshes_the_board_in_a_project_with_a_board_or_a_record(tmp_path, kind):
    project = _project(tmp_path, kind)
    done = _hook(tmp_path, project, SWARM_LIST_BODY=f"cat <<'EOF'\n{_CONTEXT}\nEOF")
    assert (done.returncode, done.stderr) == (0, "")  # MUTATION: let the board's stderr through -- shown to the session.
    boards = [c for c in _calls(tmp_path) if c.endswith("sc build-board --no-open")]
    assert len(boards) == 1  # MUTATION: no session-start refresh, or one that opens a browser -- 0.
    assert json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"].startswith("1 SwarmCloud")  # MUTATION: the board's refresh replaces the context -- differs.


def test_the_hook_refreshes_nothing_in_a_project_with_neither(tmp_path):
    project = _project(tmp_path, "none")
    done = _hook(tmp_path, project)
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")  # MUTATION: print anything for a project with no work -- output.
    assert not any("build-board" in c for c in _calls(tmp_path))  # MUTATION: refresh in every project -- called.
    assert not (project / ".claude").exists()  # MUTATION: create a board where there was no work -- a folder.


@pytest.mark.parametrize("off", ["false", "0"])
def test_the_board_refresh_does_not_depend_on_auto_attach(tmp_path, off):
    project = _project(tmp_path, "board")
    done = _hook(tmp_path, project, CLAUDE_PLUGIN_OPTION_AUTO_ATTACH=off,
                 SWARM_LIST_BODY=f"cat <<'EOF'\n{_CONTEXT}\nEOF")
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")  # MUTATION: print the context with auto_attach off -- output.
    calls = _calls(tmp_path)
    assert any(c.endswith("sc build-board --no-open") for c in calls)  # MUTATION: tie the board to auto_attach -- not called.
    assert not any("workflows" in c for c in calls)  # MUTATION: list workflows with auto_attach off -- called.


@pytest.mark.parametrize("failure", [
    "echo 'sc: GET /v1/workflows -> 503' >&2; exit 1",
    "echo 'some noise on stdout'; exit 3",
])
def test_a_failing_board_refresh_says_nothing_and_changes_nothing(tmp_path, failure):
    project = _project(tmp_path, "board")
    page = project / ".claude" / "build-board" / "index.html"
    page.write_text("<!doctype html><p>before</p>")
    done = _hook(tmp_path, project, SWARM_BOARD_BODY=failure)
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")  # MUTATION: pass the refresh's output through -- printed.
    assert page.read_text() == "<!doctype html><p>before</p>"  # MUTATION: clear the page on failure -- changed.


def test_a_hanging_board_refresh_is_cut_at_the_hooks_time_budget(tmp_path):
    project = _project(tmp_path, "board")
    started = time.monotonic()
    done = _hook(tmp_path, project, SWARM_BOARD_BODY="sleep 30", SWARM_SESSION_START_TIMEOUT="1",
                 SWARM_LIST_BODY=f"cat <<'EOF'\n{_CONTEXT}\nEOF")
    assert time.monotonic() - started < 15  # MUTATION: wait for the board without the watchdog -- 30 s.
    assert done.returncode == 0 and done.stderr == ""  # MUTATION: report the killed job -- stderr.
    assert "wf_a" in done.stdout  # MUTATION: a slow board costs the session its context -- empty.


# --------------------------------------------------------------------------
# The verb, the version and the README
# --------------------------------------------------------------------------


def _section(body: str, heading: str) -> str:
    at = body.index(heading)
    rest = body[at + len(heading):]
    stop = rest.find("\n## ")
    nxt = rest.find("\n### ")
    ends = [i for i in (stop, nxt) if i >= 0]
    return " ".join((rest[: min(ends)] if ends else rest).split())


def test_build_board_is_a_verb_of_sc():
    fields, body = _load(_SKILL)
    assert "build-board" in str(fields["argument-hint"])  # MUTATION: leave it out of the hint -- absent.
    assert "| `build-board` |" in body  # MUTATION: no row in the verb table -- absent.
    section = _section(body, "### `build-board`")  # MUTATION: no section -- ValueError.
    assert "ONE `swarm_build_board` call" in section  # MUTATION: run sc build-board instead -- absent.
    assert "`open " in section and "`xdg-open " in section  # MUTATION: one platform only -- absent.
    assert "No lanes yet" in section and "not an error" in section  # MUTATION: report an empty board as a failure -- absent.
    assert "written_at" in section and "counts" in section and "one line" in section  # MUTATION: report otherwise -- absent.


def test_the_skill_is_granted_only_the_exact_open_commands():
    fields, _ = _load(_SKILL)
    opens = sorted(e for e in fields["allowed-tools"] if "open" in e.split("(", 1)[-1].split(" ", 1)[0])
    assert opens == [  # MUTATION: grant `open:*` or any other open -- differs.
        "Bash(open /*/.claude/build-board/index.html)",
        "Bash(xdg-open /*/.claude/build-board/index.html)",
    ]


def test_the_plugin_version_moved_one_patch_with_its_pin():
    manifest = json.loads(_MANIFEST.read_text())
    version = tuple(int(part) for part in manifest["version"].split("."))
    assert version == (_VERSION_BEFORE[0], _VERSION_BEFORE[1], _VERSION_BEFORE[2] + 1)  # MUTATION: no bump -- 0.5.47.
    pinned = manifest["mcpServers"]["swarmcloud"]["args"]
    assert any(f"@sc-v{manifest['version']}#" in arg for arg in pinned)  # MUTATION: bump the version, not the pin -- absent.


def test_the_readme_documents_the_board():
    readme = " ".join(_README.read_text().split())
    assert "/sc build-board" in readme  # MUTATION: no docs -- absent.
    assert "`<project>/.claude/build-board/index.html`" in readme  # MUTATION: say not where it lives -- absent.
    assert "`.gitignore` of `*`" in readme and "never committed" in readme  # MUTATION: leave out how it stays out of git -- absent.
    assert "independent of the Claude account" in readme  # MUTATION: leave out that it is local -- absent.
    assert "Waiting on someone" in readme and "By feature" in readme  # MUTATION: say not what it shows -- absent.


def test_the_hook_documents_the_board_refresh():
    text = _HOOK.read_text()
    assert "build-board" in text and "BOARD-WIRE" in text  # MUTATION: an undocumented refresh -- absent.
    assert os.access(_HOOK, os.X_OK)  # MUTATION: lose the executable bit -- not executable.
