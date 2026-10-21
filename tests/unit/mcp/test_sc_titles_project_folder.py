"""Every bridge writes its titled run copies into the session's project, and
records what this project launched (SC-TITLES, #1511, owner decision 2026-10-18).

Measured 2026-10-18: the installed plugin's bridge wrote each titled copy to
`tempfile.gettempdir()/sc-swarmcloud-runs/`; the Workflow tool refuses a script
path outside the session's allowed directories, so the session fell back to
`sc:swarmcloud` by name and 46 of 56 runs in one session were labelled
`swarmcloud`. Held here, offline:

* with a project directory known the copy is written under
  `<project>/.sc-swarmcloud-runs/` (PLUGIN-RUNDIR) with its `.gitignore`, named
  `SC-<slug>-<12 hex>.js`, and its meta name is the title; with none known
  (or its folder unwritable), the temp fallback, and stderr says which;
* pruning removes old copies of both name patterns and nothing else;
* every launch path appends exactly one `launched.jsonl` line (a spec's at
  its submission, by workflow id: SC-TITLES-B), with no prompt
  and no credential in it, and two writers at once never corrupt a line;
* `/sc attach --all` and `/sc check` keep this session's work in the record
  whatever repository it clones, fall back to the repository filter for this
  session's work the record does not hold, and never take another project's
  record or another session's work;
* run.js launched by name with a `title` says, first, why its label is
  `swarmcloud` and which copy it would have run from.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from swarm_mcp import check, launch, sc, server, session, workflows

from test_plugin_agents_and_workflows import _HARNESS, _PLUGIN, _node
from test_plugin_rows_match_console import _FIXTURE, _Recorder, _meta_of, _stored

SESSION = "11111111-2222-4333-8444-555555555555"
OTHER = "99999999-8888-4777-8666-555555555555"
REPO = "https://github.com/acme/app"
FAR = "https://github.com/acme/ci-runners"

_COPY = re.compile(r"^SC-(?P<slug>[A-Za-z0-9-]+)-[0-9a-f]{12}\.js$")


@pytest.fixture
def where(monkeypatch, tmp_path):
    """The plugin's run.js, a project directory and a separate system temp
    directory, with this session's id set."""
    monkeypatch.setenv(launch.PLUGIN_ROOT_ENV, str(_PLUGIN))
    project = tmp_path / "project"
    project.mkdir()
    system_tmp = tmp_path / "system-tmp"
    system_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(system_tmp))
    monkeypatch.setenv(session.SESSION_ENV, SESSION)
    launch.forget_attached()
    yield {"project": project, "tmp": system_tmp}
    launch.forget_attached()


def _in_project(monkeypatch, project: Path) -> Path:
    monkeypatch.setenv(launch.PROJECT_DIR_ENV, str(project))
    return project / launch.REPO_RUN_DIR


def _lines(folder: Path) -> list[dict]:
    path = folder / launch.RECORD_FILE
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


# --------------------------------------------------------------------------
# Where the copy goes, and its name
# --------------------------------------------------------------------------


def test_with_a_project_known_the_copy_is_written_under_its_run_folder(where, monkeypatch, capsys):
    folder = _in_project(monkeypatch, where["project"])
    spec = {**_FIXTURE["chain"], "title": "nightly"}
    reply = json.loads(server._call(_Recorder(), "swarm_workflow_launch", {"spec": spec}))
    script = Path(reply["script_path"])
    assert script.parent == folder
    assert (folder / ".gitignore").read_text().splitlines()[0] == "*"
    assert _COPY.match(script.name) and _COPY.match(script.name)["slug"] == "nightly-3-steps"
    assert _meta_of(script)["name"] == reply["title"] == "SC · nightly · 3 steps"
    assert not (where["tmp"] / launch.RUN_DIR).exists(), "nothing goes to the system temp dir"
    assert str(folder) in capsys.readouterr().err


def test_the_project_folder_is_ignored_by_git(where, monkeypatch):
    if not shutil.which("git"):
        pytest.skip("git is not installed")
    subprocess.run(["git", "init", "-q", str(where["project"])], check=True)
    _in_project(monkeypatch, where["project"])
    launch.write_script("SC · t · 1 step", "d")
    status = subprocess.run(["git", "-C", str(where["project"]), "status", "--porcelain", "--untracked-files=all"],
                            capture_output=True, text=True, check=True).stdout
    assert status == "", status


def test_the_bridges_working_directory_is_the_project_when_it_is_a_git_work_tree(where, monkeypatch):
    if not shutil.which("git"):
        pytest.skip("git is not installed")
    subprocess.run(["git", "init", "-q", str(where["project"])], check=True)
    session_dir = where["project"] / "sub"
    session_dir.mkdir()
    monkeypatch.setenv("SWARM_CHECKOUT_DIR", str(session_dir))
    script = launch.write_script("SC · t · 1 step", "d")
    # The session's directory itself, never the top level above it: that is
    # outside the session's allowed directories.
    assert script.parent == session_dir / launch.REPO_RUN_DIR


def test_with_no_project_known_the_copy_goes_to_the_temp_fallback_and_says_why(where, capsys):
    script = launch.write_script("SC · t · 1 step", "d")
    assert script.parent == where["tmp"] / launch.RUN_DIR
    assert _COPY.match(script.name)
    err = capsys.readouterr().err
    assert str(where["tmp"] / launch.RUN_DIR) in err and "no project directory is known" in err


def test_a_project_folder_that_cannot_be_written_falls_back_to_temp(where, monkeypatch, capsys):
    _in_project(monkeypatch, where["project"])
    (where["project"] / launch.REPO_RUN_DIR).write_text("a file where the folder would go")
    script = launch.write_script("SC · t · 1 step", "d")
    assert script.parent == where["tmp"] / launch.RUN_DIR
    assert "could not be written" in capsys.readouterr().err


@pytest.mark.parametrize("title", [
    "SC · it's \"quoted\" / with\\slashes · 2 steps",
    "SC · ünïcödé 中文 😀 · 1 step",
    "SC · lone \ud800 surrogate · 1 step",
    "SC · ../../etc/passwd · 1 step",
    "SC · " + "x" * 400 + " · 1 step",
    "中文",
])
def test_any_title_gives_a_valid_ascii_slug(title):
    slug = launch.slug(title)
    assert re.fullmatch(r"[A-Za-z0-9]+(-[A-Za-z0-9]+)*", slug), slug
    assert len(slug) <= launch.SLUG_CHARS


def test_pruning_removes_old_copies_of_both_name_patterns_and_nothing_else(where, monkeypatch):
    folder = _in_project(monkeypatch, where["project"])
    folder.mkdir(parents=True)
    week_ago = time.time() - launch.RUN_COPY_MAX_AGE_S - 60
    old_new = folder / f"SC-nightly-3-steps-{'a' * 12}.js"
    old_old = folder / f"swarmcloud-{'b' * 12}.js"
    fresh_new = folder / f"SC-fresh-{'c' * 12}.js"
    fresh_old = folder / f"swarmcloud-{'d' * 12}.js"
    keep = [folder / "SC-notes.js", folder / "notes.txt", folder / launch.RECORD_FILE, folder / ".gitignore"]
    for path in (old_new, old_old, fresh_new, fresh_old, *keep):
        path.write_text("x")
    for path in (old_new, old_old, *keep):
        os.utime(path, (week_ago, week_ago))
    script = launch.write_script("SC · t · 1 step", "d")
    assert not old_new.exists() and not old_old.exists()
    assert fresh_new.exists() and fresh_old.exists() and script.exists()
    assert all(path.exists() for path in keep)


# --------------------------------------------------------------------------
# The project record
# --------------------------------------------------------------------------


def test_every_launch_path_appends_exactly_one_record_line(where, monkeypatch):
    folder = _in_project(monkeypatch, where["project"])
    token = secrets.token_hex(16)
    prompt = "do the thing " + secrets.token_hex(8)

    spec = {**_FIXTURE["chain"], "title": "nightly", "repository_url": f"https://x-access-token:{token}@github.com/acme/ci-runners.git"}
    server._call(_Recorder(), "swarm_workflow_launch", {"spec": spec})
    # No workflow exists yet: the submission is recorded by its id (SC-TITLES-B).
    assert len(_lines(folder)) == 0
    server._call(_Recorder(), "swarm_workflow", {"spec": spec})
    assert len(_lines(folder)) == 1

    server._call(_Recorder(_stored("attached one")), "swarm_workflow_launch", {"attach": "wf_9"})
    assert len(_lines(folder)) == 2

    entry = {"workflow_id": "wf_all", "title": "all one", "steps_total": 2, "claude_session": SESSION,
             "repository_url": REPO}
    monkeypatch.setattr(sc, "running_workflows", lambda client, **_: {"count": 1, "complete": True, "workflows": [entry]})
    monkeypatch.setattr(sc, "running_tasks", lambda client: {"count": 0, "tasks": []})
    server._call(_Recorder(), "swarm_workflow_launch", {"attach": "all"})
    assert len(_lines(folder)) == 3

    task = {"id": "task_d", "state": "QUEUED", "input": {"prompt": prompt}, "metadata": {"unit": "lane-d"},
            "repository_url": f"https://x-access-token:{token}@github.com/acme/app.git"}
    server._dispatch_rows([("task_d", "lane-d", task)])
    lines = _lines(folder)
    assert len(lines) == 4

    submit, attach, attach_all, dispatch = lines
    assert submit == {"workflow_id": "wf_1", "kind": "submit", "title": "SC · nightly · 3 steps",
                      "claude_session": SESSION, "repository": "github.com/acme/ci-runners",
                      "launched_at": submit["launched_at"], "name": "nightly"}
    assert (attach["workflow_id"], attach["kind"], attach["title"]) == ("wf_9", "attach", "SC · attached one · 2 steps")
    assert (attach_all["workflow_id"], attach_all["kind"], attach_all["repository"]) == ("wf_all", "attach", "github.com/acme/app")
    assert (dispatch["task_id"], dispatch["kind"], dispatch["title"], dispatch["repository"]) == \
        ("task_d", "dispatch", "SC · lane-d · task", "github.com/acme/app")
    for line in lines:
        assert line["claude_session"] == SESSION
        datetime.strptime(line["launched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    text = (folder / launch.RECORD_FILE).read_text()
    assert token not in text and prompt not in text and "x-access-token" not in text
    assert "step a" not in text and '"steps"' not in text, "no spec body"


def test_a_dispatch_whose_copy_cannot_be_written_is_still_recorded(where, monkeypatch):
    folder = _in_project(monkeypatch, where["project"])
    monkeypatch.setenv(launch.PLUGIN_ROOT_ENV, str(where["project"] / "no-plugin"))
    rows = server._dispatch_rows([("task_e", "lane-e", {"id": "task_e"})])
    assert "error" in rows
    assert [line["task_id"] for line in _lines(folder)] == ["task_e"]


def test_two_writers_at_once_never_corrupt_a_line(where, monkeypatch):
    folder = _in_project(monkeypatch, where["project"])
    folder.mkdir(parents=True)

    def write(n: int) -> None:
        for i in range(40):
            launch.record("dispatch", title="SC · " + "t" * 140 + " · task", task_id=f"task_{n}_{i}")

    threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    lines = _lines(folder)
    assert len(lines) == 320
    assert {line["task_id"] for line in lines} == {f"task_{n}_{i}" for n in range(8) for i in range(40)}


# --------------------------------------------------------------------------
# The scope: this session's work, within the project
# --------------------------------------------------------------------------


def _record_line(folder: Path, **line) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / launch.RECORD_FILE).open("a") as out:
        out.write(json.dumps(line) + "\n")


def _workflows(monkeypatch) -> None:
    def wf(wid, sid, repo, title=None):
        return {"workflow_id": wid, "title": title or wid, "steps_total": 2, "claude_session": sid,
                "repository_url": repo, "workspace": "eng"}

    entries = [
        wf("wf_recorded_far", SESSION, FAR),        # in the record, another repository: kept
        wf("wf_submitted_far", SESSION, FAR, "nightly"),  # recorded by its submission's name: kept
        wf("wf_old_here", SESSION, REPO),           # not recorded, this repository: the fallback keeps it
        wf("wf_old_far", SESSION, FAR),             # not recorded, another repository: left out
        wf("wf_other_project", SESSION, FAR),       # in ANOTHER project's record only: left out
        wf("wf_theirs", OTHER, REPO),               # another session's, though recorded here: left out
    ]
    monkeypatch.setattr(sc, "running_workflows", lambda client, **_: {"count": len(entries), "complete": True,
                                                                  "workflows": entries})
    monkeypatch.setattr(sc, "running_tasks", lambda client: {"count": 0, "tasks": []})


def _records(where, monkeypatch) -> None:
    here = _in_project(monkeypatch, where["project"])
    _record_line(here, workflow_id="wf_recorded_far", kind="attach", claude_session=SESSION)
    # A submission recorded by name, as a bridge before SC-TITLES-B wrote it.
    _record_line(here, workflow_id=None, kind="submit", claude_session=SESSION, name="nightly")
    _record_line(here, workflow_id="wf_theirs", kind="attach", claude_session=OTHER)
    with (here / launch.RECORD_FILE).open("a") as out:
        out.write("not json\n[1, 2]\n")
    elsewhere = where["project"].parent / "another-project" / launch.REPO_RUN_DIR
    _record_line(elsewhere, workflow_id="wf_other_project", kind="submit", claude_session=SESSION)


def test_attach_all_keeps_this_sessions_recorded_work_and_falls_back_to_the_repository(where, monkeypatch):
    _records(where, monkeypatch)
    _workflows(monkeypatch)
    reply = launch.for_all(_Recorder(), repository="acme/app")
    assert sorted(l["workflow_id"] for l in reply["launches"] if l.get("workflow_id")) == [
        "wf_old_here", "wf_recorded_far", "wf_submitted_far"]
    assert reply["session"]["left_out"]["workflows"] == {"other_session": 1, "untagged": 0, "not_this_project": 2}


def test_with_no_record_attach_all_is_the_repository_filter_as_before(where, monkeypatch):
    _in_project(monkeypatch, where["project"])
    _workflows(monkeypatch)
    reply = launch.for_all(_Recorder(), repository="acme/app")
    assert [l["workflow_id"] for l in reply["launches"] if l.get("workflow_id")] == ["wf_old_here"]


def test_another_projects_record_never_attaches_here(where, monkeypatch):
    _records(where, monkeypatch)
    _workflows(monkeypatch)
    for scope in ("acme/app", "acme/website"):
        reply = launch.for_all(_Recorder(), repository=scope)
        assert "wf_other_project" not in [l.get("workflow_id") for l in reply["launches"]]


def _check(monkeypatch) -> dict:
    def listed(wid, sid):
        return {"workflow_id": wid, "state": "RUNNING", "claude_session": sid, "steps": []}

    rows = {
        "wf_recorded_far": (SESSION, FAR, None),
        "wf_submitted_far": (SESSION, FAR, "nightly"),
        "wf_old_here": (SESSION, REPO, None),
        "wf_old_far": (SESSION, FAR, None),
        "wf_other_project": (SESSION, FAR, None),
        "wf_theirs": (OTHER, REPO, None),
    }
    running = [listed(wid, sid) for wid, (sid, _, _) in rows.items()]
    monkeypatch.setattr(check, "_workflow_lists", lambda client, scoped=None: (running, [], {"tenant_id": "eng"}))
    monkeypatch.setattr(check, "_single_tasks", lambda client: ([
        {"id": "task_recorded_far", "state": "RUNNING", "claude_session": SESSION, "repository_url": FAR},
        {"id": "task_old_far", "state": "RUNNING", "claude_session": SESSION, "repository_url": FAR},
    ], None))
    monkeypatch.setattr(check, "classify_task", lambda *a, **k: None)

    def fetch(client, wid):
        sid, repo, title = rows[wid]
        metadata = {"claude_session": sid, **({"title": title} if title else {})}
        return {"workflow": {"workflow_id": wid, "state": "RUNNING",
                             "steps": [{"step_id": "a", "task_id": f"t_{wid}"}]},
                "tasks": [{"id": f"t_{wid}", "state": "RUNNING", "repository_url": repo, "metadata": metadata}]}

    monkeypatch.setattr(workflows, "fetch", fetch)
    return check.run(_Recorder(), repository="acme/app", names=lambda _c: {})


def test_check_keeps_this_sessions_recorded_work_and_falls_back_to_the_repository(where, monkeypatch):
    _records(where, monkeypatch)
    _record_line(where["project"] / launch.REPO_RUN_DIR, task_id="task_recorded_far",
                 kind="dispatch", claude_session=SESSION)
    result = _check(monkeypatch)
    assert sorted(w["workflow_id"] for w in result["running"]["workflows"]) == [
        "wf_old_here", "wf_recorded_far", "wf_submitted_far"]
    assert [t["task_id"] for t in result["running"]["single_tasks"]] == ["task_recorded_far"]
    assert result["out_of_scope"] == {"workflows": 2, "single_tasks": 1}
    assert result["session"]["left_out"]["workflows"] == {"other_session": 1, "untagged": 0}


def test_the_rule_lives_in_session_ownership():
    record = session.Record(frozenset({"wf_r"}), frozenset({(SESSION, "nightly")}))
    own = dict(record=record, in_repository=False)
    assert session.ownership(SESSION, SESSION, ident="wf_r", **own) == "own"
    assert session.ownership(SESSION, SESSION, ident="wf_x", name="nightly", **own) == "own"
    assert session.ownership(SESSION, SESSION, ident="wf_x", **own) == "not_this_project"
    assert session.ownership(SESSION, SESSION, ident="wf_x", record=record, in_repository=True) == "own"
    assert session.ownership(OTHER, SESSION, ident="wf_r", **own) == "other_session"
    assert session.ownership(None, SESSION, ident="wf_r", **own) == "untagged"
    # Another session's submission of the same name is not this session's.
    assert session.ownership(OTHER, OTHER, ident="wf_x", name="nightly", **own) == "not_this_project"
    # No record given: the tag alone, as before.
    assert session.ownership(SESSION, SESSION) == "own"


# --------------------------------------------------------------------------
# run.js says why its label is generic
# --------------------------------------------------------------------------


def _logs(tmp_path: Path, script: Path, args) -> list[str]:
    harness = tmp_path / "harness.cjs"
    harness.write_text(_HARNESS)
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"args": args, "answers": {}}))
    done = subprocess.run([_node(), str(harness), str(script), str(fixture)],
                          capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)["logs"]


def test_run_js_launched_by_name_with_a_title_says_why_first(where, monkeypatch, tmp_path):
    _in_project(monkeypatch, where["project"])
    reply = launch.for_attach(_Recorder(_stored("nightly")), "wf_9")
    assert reply["args"]["titled_copy"] == reply["script_path"]
    by_name = _logs(tmp_path, _PLUGIN / "workflows" / "run.js", reply["args"])
    assert by_name, "run.js logged nothing"
    assert 'labelled "swarmcloud"' in by_name[0] and reply["script_path"] in by_name[0]
    # A title alone (an older bridge) still says why, without a path.
    assert 'labelled "swarmcloud"' in _logs(tmp_path, _PLUGIN / "workflows" / "run.js",
                                            {"attach": "wf_9", "title": "nightly"})[0]


def test_the_titled_copy_and_a_plain_launch_by_name_say_nothing_of_it(where, monkeypatch, tmp_path):
    _in_project(monkeypatch, where["project"])
    reply = launch.for_attach(_Recorder(_stored("nightly")), "wf_9")
    from_copy = _logs(tmp_path, Path(reply["script_path"]), reply["args"])
    assert not any("labelled" in line for line in from_copy), from_copy
    plain = _logs(tmp_path, _PLUGIN / "workflows" / "run.js", {"attach": "wf_9"})
    assert not any("labelled" in line for line in plain), plain
