"""The titled run copies and the project record live OUTSIDE `.claude/`
(PLUGIN-RUNDIR; part of #1511).

Measured 2026-10-19 at 03:12Z on the installed sc plugin 0.5.44:
`swarm_workflow_launch` returned `<project>/.claude/sc-swarmcloud-runs/SC-...js`
and the Workflow tool refused it twice -- the second time after the session
had Read the file -- while the same bytes copied to the system temp folder
(an added working directory in that session) launched at once. Claude Code
treats `.claude/` as protected configuration, so SC-TITLES traded one refused
folder for another. Held here, offline:

* with a project known, the copies and `launched.jsonl` go to
  `<project>/.sc-swarmcloud-runs/` with its `.gitignore` of `*`, and nothing
  is created under `<project>/.claude/`;
* a record an older bridge left at `<project>/.claude/sc-swarmcloud-runs/`
  is moved beside the copies on the next write, and is still read where it
  is until then -- and after, should an older bridge append there again --
  so no session loses the record of what it launched;
* with no project known, the temp fallback still applies, for the copies and
  the record alike.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from swarm_mcp import launch, session

from test_plugin_agents_and_workflows import _PLUGIN

SESSION = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
def where(monkeypatch, tmp_path):
    monkeypatch.setenv(launch.PLUGIN_ROOT_ENV, str(_PLUGIN))
    project = tmp_path / "project"
    project.mkdir()
    system_tmp = tmp_path / "system-tmp"
    system_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(system_tmp))
    monkeypatch.setenv(session.SESSION_ENV, SESSION)
    return {"project": project, "tmp": system_tmp}


def _legacy_record(project: Path, *ids: str) -> Path:
    folder = project / ".claude" / "sc-swarmcloud-runs"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "launched.jsonl"
    with path.open("a", encoding="utf-8") as out:
        out.write("".join(json.dumps({"task_id": i, "kind": "task", "title": i}) + "\n" for i in ids))
    return path


def test_with_a_project_known_the_copies_go_outside_its_claude_folder(where, monkeypatch, capsys):
    monkeypatch.setenv(launch.PROJECT_DIR_ENV, str(where["project"]))
    script = launch.write_script("SC · t · 1 step", "d")
    folder = where["project"] / ".sc-swarmcloud-runs"
    assert script.parent == folder
    assert ".claude" not in script.relative_to(where["project"]).parts
    assert not (where["project"] / ".claude").exists(), "nothing is written under .claude/"
    assert (folder / ".gitignore").read_text().splitlines()[0] == "*"
    assert launch.record_path() == folder / launch.RECORD_FILE
    assert str(folder) in capsys.readouterr().err


def test_the_record_is_written_beside_the_copies_outside_claude(where, monkeypatch):
    monkeypatch.setenv(launch.PROJECT_DIR_ENV, str(where["project"]))
    launch.record("task", title="one", task_id="task_new")
    path = where["project"] / ".sc-swarmcloud-runs" / launch.RECORD_FILE
    assert [json.loads(line)["task_id"] for line in path.read_text().splitlines()] == ["task_new"]
    assert not (where["project"] / ".claude").exists()


def test_an_older_bridges_record_is_moved_beside_the_copies_on_the_next_write(where, monkeypatch):
    old = _legacy_record(where["project"], "task_before")
    monkeypatch.setenv(launch.PROJECT_DIR_ENV, str(where["project"]))
    launch.record("task", title="after", task_id="task_after")
    new = where["project"] / ".sc-swarmcloud-runs" / launch.RECORD_FILE
    ids = [json.loads(line)["task_id"] for line in new.read_text().splitlines()]
    assert ids == ["task_before", "task_after"]
    assert not old.exists()
    assert session.project_record().ids == {"task_before", "task_after"}


def test_an_older_bridges_record_is_still_read_before_anything_is_written(where, monkeypatch):
    _legacy_record(where["project"], "task_before")
    monkeypatch.setenv(launch.PROJECT_DIR_ENV, str(where["project"]))
    record = session.project_record()
    assert record.ids == {"task_before"}
    assert record.lines == 1
    assert not (where["project"] / ".sc-swarmcloud-runs").exists(), "a read creates nothing"


def test_both_records_are_read_when_an_older_bridge_appended_after_the_move(where, monkeypatch):
    monkeypatch.setenv(launch.PROJECT_DIR_ENV, str(where["project"]))
    launch.record("task", title="new", task_id="task_new")
    _legacy_record(where["project"], "task_from_an_older_bridge")
    record = session.project_record()
    assert record.ids == {"task_new", "task_from_an_older_bridge"}
    assert record.lines == 2


def test_with_no_project_known_the_temp_fallback_still_applies(where, capsys):
    script = launch.write_script("SC · t · 1 step", "d")
    assert script.parent == where["tmp"] / launch.RUN_DIR
    launch.record("task", title="t", task_id="task_tmp")
    assert launch.record_path() == where["tmp"] / launch.RUN_DIR / launch.RECORD_FILE
    assert session.project_record().ids == {"task_tmp"}
    assert "no project directory is known" in capsys.readouterr().err
