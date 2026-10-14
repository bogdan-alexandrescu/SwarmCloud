"""A live row's title survives quotes and every other character a person can type (H3-ROWS).

Measured 2026-10-11 in the operator session: attaching rows for a workflow
whose title held quotes produced a broken attach script. A title reaches the
per-run copy of run.js that swarm_mcp.launch writes in exactly two places --
`meta.name` and `meta.description`, spliced into JavaScript source -- and the
launch's `args`, which the session passes to the Workflow tool as a JSON value
and never as source. So every title below must, through each launch the
bridge offers (a spec, one attach, attach --all, single tasks):

* leave a copy that still PARSES as a workflow script,
* read back from that copy's `meta` exactly as it went in,
* change no line of run.js but the two it replaces, counted the way any
  line-based reader counts them -- U+2028 and U+2029 are line breaks to
  Python's `splitlines()` and to editors, and a raw one moved every line
  after it, and
* never fail the launch: a lone surrogate (a title JSON can carry as
  `\\ud800`) raised UnicodeEncodeError when the copy was written as UTF-8.

THE MUTATION THIS CATCHES: write either meta line with `json.dumps(...,
ensure_ascii=False)` as before (the surrogate and U+2028 cases go red), or
splice the title between quotes (every quote case goes red).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from swarm_mcp import launch, server, workflows

from test_plugin_agents_and_workflows import _PLUGIN, _RUN_JS, _node
from test_plugin_rows_match_console import _FIXTURE, _Recorder, _stored

_MANIFEST = _PLUGIN / ".claude-plugin" / "plugin.json"

#: The release before this lane: H3-ROWS changes the bridge the plugin pins.
_PREVIOUS_RELEASED_VERSION = (0, 5, 29)

#: Every character class that has ended a literal, opened a template or a
#: tag, or broken a line somewhere.
HOSTILE_TITLES = [
    "it's mine",
    'say "hi"',
    "both ' and \" at once",
    "back`tick` and `${x}`",
    "a\\b and a trailing \\",
    "${process.exit(3)}",
    "</script><script>alert(1)</script>",
    "line one\nline two\r\nthree",
    "tab\there",
    "ünïcödé · 中文 · 😀 · עברית",
    "line\u2028separator\u2029paragraph",
    "lone \ud800 surrogate",
    "nul \x00 and bell \x07",
    "', name: 'injected",
    '"}, x: (() => { throw 1 })(), y: {"',
]

#: Reads each copy's meta the way Claude Code does -- as the literal it is --
#: and compiles the whole copy as a workflow script body (top-level await and
#: return), without running it.
_READ = r"""
const fs = require('fs')
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const out = []
for (const path of JSON.parse(fs.readFileSync(process.argv[1], 'utf8'))) {
  const text = fs.readFileSync(path, 'utf8')
  const end = text.indexOf('\n}\n') + 2
  const meta = new Function(text.slice(0, end).replace('export const meta =', 'return'))()
  let parses = true
  try { new AsyncFunction(text.replace(/^export /, '')) } catch (error) { parses = String(error) }
  out.push({ name: meta.name, description: meta.description, parses: parses })
}
process.stdout.write(JSON.stringify(out))
"""


def _read(tmp_path: Path, scripts: list[Path]) -> list[dict]:
    listing = tmp_path / "scripts.json"
    listing.write_text(json.dumps([str(p) for p in scripts]))
    done = subprocess.run([_node(), "-e", _READ, str(listing)], capture_output=True, text=True,
                          timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)
    assert len(got) == len(scripts), "every copy was read"
    return got


def _only_meta_lines_differ(script: Path) -> None:
    copy = script.read_text(encoding="utf-8")
    original = _RUN_JS.read_text(encoding="utf-8")
    for split in (lambda t: t.split("\n"), str.splitlines):
        a, b = split(copy), split(original)
        assert len(a) == len(b), "a title added a line break to the copy"
        assert [i for i, (x, y) in enumerate(zip(a, b)) if x != y] == [1, 2]


@pytest.fixture
def _plugin_root(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARM_SC_PLUGIN_ROOT", str(_PLUGIN))
    monkeypatch.setattr(launch.tempfile, "tempdir", str(tmp_path))


def test_every_hostile_title_round_trips_through_the_copy_exactly(_plugin_root, tmp_path):
    scripts = [launch.write_script(title, "about " + title) for title in HOSTILE_TITLES]
    got = _read(tmp_path, scripts)
    for title, script, meta in zip(HOSTILE_TITLES, scripts, got):
        assert meta["parses"] is True, (title, meta["parses"])
        assert meta["name"] == title
        assert meta["description"] == "about " + title
        _only_meta_lines_differ(script)
    assert len(got) == len(HOSTILE_TITLES) == 15


def test_the_meta_lines_hold_no_raw_separator_or_surrogate_and_stay_readable(_plugin_root):
    for title in HOSTILE_TITLES:
        lines = launch.write_script(title, title).read_text(encoding="utf-8").split("\n")
        for line in lines[1:3]:
            assert not any(ch in "\u2028\u2029" or 0xD800 <= ord(ch) <= 0xDFFF for ch in line), title
    # Everything else is written as itself, so a person opening the copy reads the title.
    line = launch.write_script("ünï · 中文", "d").read_text(encoding="utf-8").split("\n")[1]
    assert line == '  name: "ünï · 中文",'


def test_a_spec_launch_with_a_hostile_title_parses_and_names_its_run(_plugin_root, tmp_path):
    replies = []
    for title in HOSTILE_TITLES:
        spec = {**_FIXTURE["chain"], "title": title}
        replies.append(json.loads(server._call(_Recorder(), "swarm_workflow_launch", {"spec": spec})))
    got = _read(tmp_path, [Path(r["script_path"]) for r in replies])
    for title, reply, meta in zip(HOSTILE_TITLES, replies, got):
        assert meta["parses"] is True, (title, meta["parses"])
        assert meta["name"] == reply["title"] == workflows.workflow_title(workflows.title_name(title=title), 3)


def test_an_attach_with_a_hostile_stored_title_parses_and_hands_the_name_back_as_an_arg(_plugin_root, tmp_path):
    replies = []
    for title in HOSTILE_TITLES:
        replies.append(json.loads(server._call(_Recorder(_stored(title, "a label")), "swarm_workflow_launch",
                                               {"attach": "wf_9"})))
    got = _read(tmp_path, [Path(r["script_path"]) for r in replies])
    for title, reply, meta in zip(HOSTILE_TITLES, replies, got):
        name = workflows.title_name(stored=title)
        assert meta["parses"] is True, (title, meta["parses"])
        assert reply["args"] == {"attach": "wf_9", "title": name}
        assert meta["name"] == reply["title"] == workflows.workflow_title(name, 2)


def test_attach_all_with_hostile_titles_writes_a_parsing_copy_per_workflow(_plugin_root, tmp_path, monkeypatch):
    from swarm_mcp import sc

    entries = [{"workflow_id": f"wf_{i}", "title": title, "label": None, "steps_total": 2, "console": None}
               for i, title in enumerate(HOSTILE_TITLES[: launch.MAX_ATTACHED_WORKFLOWS])]
    monkeypatch.setattr(sc, "running_workflows", lambda client: {"count": len(entries), "complete": True,
                                                                  "workflows": entries})
    monkeypatch.setattr(sc, "running_tasks", lambda client: {"count": 0, "tasks": []})
    reply = json.loads(server._call(_Recorder(), "swarm_workflow_launch", {"attach": "all"}))
    launches = reply["launches"]
    assert len(launches) == len(entries) == min(len(HOSTILE_TITLES), launch.MAX_ATTACHED_WORKFLOWS)
    got = _read(tmp_path, [Path(l["script_path"]) for l in launches])
    for entry, one, meta in zip(entries, launches, got):
        name = workflows.title_name(stored=entry["title"])
        assert meta["parses"] is True, (entry["title"], meta["parses"])
        assert one["args"] == {"attach": entry["workflow_id"], "title": name}
        assert meta["name"] == one["title"]


def test_single_task_rows_with_hostile_labels_parse_and_carry_each_label(_plugin_root, tmp_path):
    entries = [{"task_id": f"task_{i}", "label": title} for i, title in enumerate(HOSTILE_TITLES)]
    reply = launch.for_tasks(entries)
    meta = _read(tmp_path, [Path(reply["script_path"])])[0]
    assert meta["parses"] is True, meta["parses"]
    assert meta["name"] == reply["title"]
    rows = json.loads(json.dumps(reply["args"]))["attach_tasks"]
    assert [r["title"] for r in rows] == [workflows.title_name(stored=t) for t in
                                          HOSTILE_TITLES[: launch.MAX_ATTACHED_TASKS]]


def test_the_plugin_version_moved_past_the_release_before_h3_rows():
    manifest = json.loads(_MANIFEST.read_text())
    version = tuple(int(part) for part in manifest["version"].split("."))
    assert version > _PREVIOUS_RELEASED_VERSION, manifest["version"]
    pinned = manifest["mcpServers"]["swarmcloud"]["args"]
    assert any(f"@sc-v{manifest['version']}#subdirectory=apps/swarm-mcp" in arg for arg in pinned), pinned
