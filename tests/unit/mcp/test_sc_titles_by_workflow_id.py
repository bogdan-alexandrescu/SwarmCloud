"""A workflow this session submits is recorded by its workflow id, and the
plugin hands the bridge the project directory explicitly (SC-TITLES-B, part
of #1511).

SC-TITLES recorded a submission before SwarmCloud had named the workflow, as
`{workflow_id: null, name}`, and `session.recorded` matched it by name: two
of this session's workflows with one name then both counted as launched from
this project. Held here, offline:

* a submission through `swarm_workflow` (`server._workflow_created`) writes
  one record line carrying the workflow id SwarmCloud answered with, and the
  launch that precedes it writes none;
* of two same-named workflows, only the one submitted here is kept;
* a by-name line written before this change still matches, as before;
* plugin.json hands the bridge `CLAUDE_PROJECT_DIR`, and an empty or
  unexpanded `${...}` value falls back to the bridge's cwd rule.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from swarm_mcp import launch, sc, server, session

from test_plugin_agents_and_workflows import _PLUGIN
from test_plugin_rows_match_console import _FIXTURE, _Recorder
from test_sc_titles_project_folder import SESSION, _in_project, _lines, _record_line, where  # noqa: F401

FAR = "https://github.com/acme/ci-runners"
_MANIFEST = _PLUGIN / ".claude-plugin" / "plugin.json"


class _Named(_Recorder):
    """Answers a submission with `workflow_id`."""

    def __init__(self, workflow_id: str) -> None:
        super().__init__()
        self.workflow_id = workflow_id

    def request(self, method: str, path: str, payload=None, **kw: object) -> dict:
        reply = super().request(method, path, payload, **kw)
        reply["workflow"]["workflow_id"] = self.workflow_id
        return reply


def _submit(spec: dict, workflow_id: str) -> dict:
    """The /sc:swarmcloud flow: the titled copy, then the submission."""
    server._call(_Recorder(), "swarm_workflow_launch", {"spec": spec})
    return json.loads(server._call(_Named(workflow_id), "swarm_workflow", {"spec": spec}))


def test_a_submission_is_recorded_by_the_workflow_id_swarmcloud_named(where, monkeypatch):
    folder = _in_project(monkeypatch, where["project"])
    spec = {**_FIXTURE["chain"], "title": "nightly", "repository_url": FAR}
    reply = _submit(spec, "wf_mine")
    assert reply["workflow_id"] == "wf_mine"
    (line,) = _lines(folder)
    assert line == {"workflow_id": "wf_mine", "kind": "submit", "title": "SC · nightly · 3 steps",
                    "claude_session": SESSION, "repository": "github.com/acme/ci-runners",
                    "launched_at": line["launched_at"], "name": "nightly"}


def test_a_merge_pr_submission_is_recorded_by_its_id(where, monkeypatch):
    folder = _in_project(monkeypatch, where["project"])
    server._workflow_created({"workflow": {"workflow_id": "wf_merge", "steps": []}},
                             placed={}, repository={"url": FAR}, name="merge 12")
    (line,) = _lines(folder)
    assert (line["workflow_id"], line["kind"], line["name"]) == ("wf_merge", "submit", "merge 12")


def test_a_record_that_cannot_be_written_never_fails_the_answer(where, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(launch, "record", broken)
    reply = json.loads(server._workflow_created({"workflow": {"workflow_id": "wf_x", "steps": []}},
                                                placed={}, repository={"url": FAR}))
    assert reply["workflow_id"] == "wf_x"


def test_two_same_named_workflows_only_the_one_submitted_here_is_kept(where, monkeypatch):
    _in_project(monkeypatch, where["project"])
    _submit({**_FIXTURE["chain"], "title": "nightly", "repository_url": FAR}, "wf_mine")

    entries = [{"workflow_id": wid, "title": "nightly", "steps_total": 3, "claude_session": SESSION,
                "repository_url": FAR, "workspace": "eng"} for wid in ("wf_mine", "wf_twin")]
    monkeypatch.setattr(sc, "running_workflows", lambda client: {"count": 2, "complete": True, "workflows": entries})
    monkeypatch.setattr(sc, "running_tasks", lambda client: {"count": 0, "tasks": []})
    reply = launch.for_all(_Recorder(), repository="acme/app")
    assert [l["workflow_id"] for l in reply["launches"] if l.get("workflow_id")] == ["wf_mine"]

    record = session.project_record()
    assert record.ids == frozenset({"wf_mine"}) and record.submitted == frozenset()
    assert session.recorded(record, "wf_mine", "nightly", SESSION)
    assert not session.recorded(record, "wf_twin", "nightly", SESSION)


def test_a_by_name_line_written_before_this_change_still_matches(where, monkeypatch):
    folder = _in_project(monkeypatch, where["project"])
    _record_line(folder, workflow_id=None, kind="submit", claude_session=SESSION, name="nightly")
    record = session.project_record()
    assert session.recorded(record, "wf_old", "nightly", SESSION)
    assert not session.recorded(record, "wf_old", "other", SESSION)


# --------------------------------------------------------------------------
# CLAUDE_PROJECT_DIR, handed over by the plugin
# --------------------------------------------------------------------------


def test_the_plugin_hands_the_bridge_the_project_directory():
    env = json.loads(_MANIFEST.read_text())["mcpServers"]["swarmcloud"]["env"]
    assert env.get(launch.PROJECT_DIR_ENV) == "${CLAUDE_PROJECT_DIR:-}"


@pytest.mark.parametrize("value", ["", "   ", "${CLAUDE_PROJECT_DIR:-}", "${CLAUDE_PROJECT_DIR}"])
def test_an_empty_or_unexpanded_project_dir_falls_back_to_the_cwd_rule(where, monkeypatch, tmp_path, value):
    if not shutil.which("git"):
        pytest.skip("git is not installed")
    tree = where["project"]
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    monkeypatch.setenv("SWARM_CHECKOUT_DIR", str(tree))
    # A folder literally named as the unexpanded value, under the process's
    # working directory: taken as a relative path, it would be "a directory".
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    if value.strip():
        (cwd / value).mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv(launch.PROJECT_DIR_ENV, value)

    assert launch.project_dir_given() is None
    script = launch.write_script("SC · t · 1 step", "d")
    assert script.parent == tree / launch.REPO_RUN_DIR


def test_an_unexpanded_project_dir_and_no_work_tree_falls_back_to_temp(where, monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(launch.PROJECT_DIR_ENV, "${CLAUDE_PROJECT_DIR:-}")
    (tmp_path / "${CLAUDE_PROJECT_DIR:-}").mkdir()
    script = launch.write_script("SC · t · 1 step", "d")
    assert script.parent == where["tmp"] / launch.RUN_DIR
    assert "was not expanded" in capsys.readouterr().err


def test_an_expanded_project_dir_is_the_project(where, monkeypatch):
    folder = _in_project(monkeypatch, where["project"])
    assert launch.project_dir_given() == str(where["project"])
    assert launch.write_script("SC · t · 1 step", "d").parent == folder

