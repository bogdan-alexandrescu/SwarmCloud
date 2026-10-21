"""The sc plugin says why a waiting task waits, in the API's own words (WAIT-PLUGIN, part of #1782).

WAIT-API serves `waiting` on a task -- `{reason, summary, next_check_at,
resumes_when, detail, history}` -- built only from what the platform recorded.
Before this lane the plugin printed the bare park reason: every merge step of
the 2026-10-20/21 CI stall read "waiting for the pull request's checks
(CI_PENDING)", whether its checks were queued, failing or done.

What holds now, each pinned below:

* `swarm_status` carries the summary and the next check, and `waiting_line`
  is the one line `/sc status <task>` prints after the state;
* a followed row (`format: "lines"` and `format: "progress"`) writes the
  summary instead of the bare park reason, and writes a new line only when
  that summary changes -- never on every poll;
* `swarm_check`'s `parked` and `waiting_for_capacity` flags carry the summary
  in their evidence;
* an API older than WAIT-API (no `waiting`) gets every line exactly as before.

Offline: the real application over an in-memory Firestore, as
`test_follow_cursor.py` wires it. No credentials, no emulator, no network.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from swarm_mcp import check, compact, follow, progress, server

from test_follow_cursor import World, swarm, world  # noqa: F401 - fixtures

_CI_SUMMARY = "CI on PR #1779: 5 checks pending; re-checked 2 times; next check 02:46Z"
_CI_LATER = "CI on PR #1779: 3 checks pending; re-checked 3 times; next check 03:01Z"


def _parked(world, task_id: str = "task_a", *, reason: str = "CI_PENDING") -> None:  # noqa: F811
    world.task(task_id, state="PARKED")
    doc = world.db.docs[f"tasks/{task_id}"]
    doc["step_id"] = "merge"
    doc["park_reason"] = reason
    doc["next_eligible_at"] = datetime.now(timezone.utc) + timedelta(minutes=10)


def _serve(swarm, monkeypatch, waiting: dict) -> None:  # noqa: F811 - fixture name
    """Every task read carries `waiting` as `waiting["now"]` holds it; absent
    when it holds None -- an API older than WAIT-API."""
    original = swarm.task

    def task(task_id):
        doc = dict(original(task_id))
        if waiting["now"] is None:
            doc.pop("waiting", None)
        else:
            doc["waiting"] = waiting["now"]
        return doc

    monkeypatch.setattr(swarm, "task", task)


def _waiting(summary: str, next_check_at: str | None = "2026-10-21T02:46:00Z") -> dict:
    return {"reason": "CI_PENDING", "summary": summary, "next_check_at": next_check_at,
            "resumes_when": "its pull request's checks settle", "detail": {"head": "1968b677"},
            "history": [{"kind": "park"}], "history_read": True}


# --------------------------------------------------------------------------
# swarm_status and /sc status <task>
# --------------------------------------------------------------------------


def test_status_carries_the_summary_and_the_next_check(swarm, world, monkeypatch):
    _parked(world)
    _serve(swarm, monkeypatch, {"now": _waiting(_CI_SUMMARY)})
    (row,) = json.loads(server._call(swarm, "swarm_status", {"task_ids": ["task_a"]}))
    assert row["waiting"] == {  # MUTATION: drop `waiting` from the status row -- KeyError.
        "reason": "CI_PENDING",
        "summary": _CI_SUMMARY,
        "next_check_at": "2026-10-21T02:46:00Z",
    }  # MUTATION: copy the whole object (detail, history) -- extra keys.
    # The summary already names its next check, so the line does not repeat it.
    assert row["waiting_line"] == _CI_SUMMARY  # MUTATION: append "; next check" again -- differs.


def test_status_line_adds_the_next_check_the_summary_does_not_name(swarm, world, monkeypatch):
    _parked(world, reason="PROVIDER_QUOTA_EXHAUSTED")
    _serve(swarm, monkeypatch, {"now": {**_waiting("Quota exhausted on anthropic"),
                                         "reason": "PROVIDER_QUOTA_EXHAUSTED",
                                         "next_check_at": "2026-10-21T03:10:00Z"}})
    (row,) = json.loads(server._call(swarm, "swarm_status", {"task_ids": ["task_a"]}))
    line = row["waiting_line"]
    assert line.startswith("Quota exhausted on anthropic; next check ")  # MUTATION: summary only -- no next check.
    assert line.endswith("03:10Z")  # MUTATION: print the raw ISO stamp -- ends "00Z".


def test_status_carries_what_the_real_api_serves_for_a_ci_park(swarm, world):
    _parked(world)
    (row,) = json.loads(server._call(swarm, "swarm_status", {"task_ids": ["task_a"]}))
    assert row["waiting"]["reason"] == "CI_PENDING"  # MUTATION: read `waiting_for` instead -- None.
    assert row["waiting"]["summary"].startswith("CI on its pull request")  # MUTATION: the bare reason -- differs.
    assert row["waiting"]["next_check_at"]  # MUTATION: drop next_check_at -- None.


def test_status_of_an_older_api_is_unchanged(swarm, world, monkeypatch):
    _parked(world)
    _serve(swarm, monkeypatch, {"now": None})
    (row,) = json.loads(server._call(swarm, "swarm_status", {"task_ids": ["task_a"]}))
    assert "waiting" not in row  # MUTATION: serve `waiting: null` to an older API -- present.
    assert "waiting_line" not in row  # MUTATION: a line built from the bare reason -- present.
    assert set(row) == {"task_id", "workspace", "state", "masked"}  # MUTATION: any new key -- differs.


def test_a_running_task_carries_no_waiting(swarm, world):
    world.task("task_a", state="RUNNING")
    (row,) = json.loads(server._call(swarm, "swarm_status", {"task_ids": ["task_a"]}))
    assert "waiting" not in row  # MUTATION: serve the API's `waiting: null` as a key -- present.


def test_the_skill_prints_the_waiting_line_after_the_state():
    from pathlib import Path

    skill = (Path(__file__).resolve().parents[3] / "plugin" / "skills" / "sc" / "SKILL.md").read_text()
    section = skill.split("### `status <wf_id|task_id>`", 1)[1].split("\n### ", 1)[0]
    flat = " ".join(section.split())
    assert "`waiting_line`" in flat  # MUTATION: leave the status section as it was -- absent.
    assert "[<workspace>] <id> · <state> · <waiting_line>" in flat  # MUTATION: a different order -- absent.


# --------------------------------------------------------------------------
# The rows: one line per summary, never one per poll
# --------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def clock(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def _waiting_lines(reply: dict) -> list[str]:
    return [line for line in reply["lines"] if " waiting · " in line]


def test_a_lines_row_writes_the_summary_once_per_change(swarm, world, monkeypatch):
    _parked(world)
    served = {"now": _waiting(_CI_SUMMARY)}
    _serve(swarm, monkeypatch, served)

    first = progress.watch(swarm, ["task_a"])
    (line,) = _waiting_lines(first)
    assert _CI_SUMMARY in line  # MUTATION: keep the bare park reason -- absent.
    assert "waiting for the pull request's checks" not in line  # MUTATION: write both -- present.
    assert line.endswith("(CI_PENDING); holds no capacity")  # MUTATION: drop the reason code -- differs.

    ticks = _Clock()
    again = progress.watch(swarm, ["task_a"], since=first["since"], wait_seconds=120,
                           sleep=ticks.sleep, clock=ticks.clock)
    assert again["polls"] > 1  # the window did poll more than once
    assert _waiting_lines(again) == []  # MUTATION: write the line on every poll -- one per poll.

    served["now"] = _waiting(_CI_LATER, "2026-10-21T03:01:00Z")
    changed = progress.watch(swarm, ["task_a"], since=again["since"])
    (line,) = _waiting_lines(changed)  # MUTATION: key on the state only -- no line.
    assert _CI_LATER in line

    quiet = progress.watch(swarm, ["task_a"], since=changed["since"])
    assert _waiting_lines(quiet) == []  # MUTATION: forget the summary said -- written again.


def test_a_lines_row_of_an_older_api_is_unchanged(swarm, world, monkeypatch):
    _parked(world)
    _serve(swarm, monkeypatch, {"now": None})
    first = progress.watch(swarm, ["task_a"])
    (line,) = _waiting_lines(first)
    assert line.startswith("[merge ")
    assert line.endswith(  # MUTATION: any change to the line an older API gets -- differs.
        "] waiting · PARKED · waiting for the pull request's checks (CI_PENDING); holds no capacity"
    )
    assert "w" not in progress._payload(first["since"])  # MUTATION: always write the summary map -- present.
    second = progress.watch(swarm, ["task_a"], since=first["since"])
    assert _waiting_lines(second) == []  # MUTATION: a line per poll -- one.


def test_a_progress_row_writes_the_summary_once_per_change(swarm, world, monkeypatch):
    _parked(world)
    served = {"now": _waiting(_CI_SUMMARY)}
    _serve(swarm, monkeypatch, served)

    first = compact.watch_progress(swarm, ["task_a"])
    (line,) = first["progress"]
    assert line.endswith(f"waits: {_CI_SUMMARY}")  # MUTATION: keep "waits: pull request checks" -- differs.

    second = compact.watch_progress(swarm, ["task_a"], since=first["since"])
    assert second["progress"] == []  # MUTATION: key on the elapsed time -- a line per poll.

    served["now"] = _waiting(_CI_LATER, "2026-10-21T03:01:00Z")
    third = compact.watch_progress(swarm, ["task_a"], since=second["since"])
    (line,) = third["progress"]  # MUTATION: leave the summary out of the key -- no line.
    assert line.endswith(f"waits: {_CI_LATER}")


def test_a_progress_row_of_an_older_api_is_unchanged(swarm, world, monkeypatch):
    _parked(world)
    _serve(swarm, monkeypatch, {"now": None})
    first = compact.watch_progress(swarm, ["task_a"])
    (line,) = first["progress"]
    assert line.endswith("waits: pull request checks")  # MUTATION: change the older line -- differs.
    assert compact.decode_since(first["since"])[1]["task_a"] == "PARKED:CI_PENDING|"  # MUTATION: a new key -- differs.


def test_waiting_brief_ignores_what_is_not_a_summary():
    assert follow.waiting_brief({"waiting": None}) is None  # MUTATION: a brief of nothing -- a dict.
    assert follow.waiting_brief({"waiting": {"reason": "CI_PENDING", "summary": "  "}}) is None  # MUTATION: blank -- a dict.
    assert follow.waiting_brief({}) is None  # MUTATION: KeyError on an older API.


# --------------------------------------------------------------------------
# swarm_check's evidence
# --------------------------------------------------------------------------


class _Reads:
    def events(self, task_id):  # noqa: ARG002
        return []

    def task(self, task_id):  # noqa: ARG002
        return None

    def capacity(self):
        return None, "not read in this test"


_LONG_AGO = datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc)
_NOW = _LONG_AGO + timedelta(hours=3)


def _task(state: str, **extra) -> dict:
    return {"id": "task_a", "state": state, "created_at": _LONG_AGO.isoformat(),
            "runner_profile": "merge", **extra}


def test_the_parked_flag_carries_the_summary():
    task = _task("PARKED", park_reason="CI_PENDING", waiting={"reason": "CI_PENDING", "summary": _CI_SUMMARY})
    flag = check.classify_task(task, _Reads(), now=_NOW, window=30)
    assert flag["rule"] == "parked"
    assert flag["evidence"]["waiting_summary"] == _CI_SUMMARY  # MUTATION: leave it out of the evidence -- KeyError.


def test_the_failed_dependency_flag_carries_the_summary():
    summary = "Waiting on 1 upstream steps: build (FAILED)"
    task = _task("PARKED", park_reason="DEPENDENCY_INCOMPLETE",
                 waiting={"reason": "DEPENDENCY_INCOMPLETE", "summary": summary})
    flag = check.classify_task(task, _Reads(), now=_NOW, window=30, dependencies={"build": "FAILED"})
    assert flag["evidence"]["waiting_summary"] == summary  # MUTATION: only the CI branch carries it -- KeyError.


def test_the_capacity_flag_carries_the_summary():
    summary = "Ready; capacity binds on runner:merge (4 of 4 units)"
    task = _task("READY", waiting={"reason": "READY_CAPACITY", "summary": summary},
                 waiting_for={"lead": {"pool": "runner:merge", "limit": 4, "active": 4}})
    flag = check.classify_task(task, _Reads(), now=_NOW, window=30)
    assert flag["rule"] == "waiting_for_capacity"
    assert flag["evidence"]["waiting_summary"] == summary  # MUTATION: leave it out of the evidence -- KeyError.


@pytest.mark.parametrize("state, extra", [
    ("PARKED", {"park_reason": "CI_PENDING"}),
    ("READY", {"waiting_for": {"lead": {"pool": "runner:merge", "limit": 4, "active": 4}}}),
])
def test_the_flags_of_an_older_api_are_unchanged(state, extra):
    flag = check.classify_task(_task(state, **extra), _Reads(), now=_NOW, window=30)
    assert "waiting_summary" not in flag["evidence"]  # MUTATION: a null summary key -- present.


# --------------------------------------------------------------------------
# The version
# --------------------------------------------------------------------------

_VERSION_BEFORE = (0, 5, 48)


def test_the_plugin_version_moved_one_patch_with_its_pin():
    from pathlib import Path

    manifest = json.loads(
        (Path(__file__).resolve().parents[3] / "plugin" / ".claude-plugin" / "plugin.json").read_text()
    )
    version = tuple(int(part) for part in manifest["version"].split("."))
    assert version == (_VERSION_BEFORE[0], _VERSION_BEFORE[1], _VERSION_BEFORE[2] + 1)  # MUTATION: no bump -- 0.5.48.
    pinned = manifest["mcpServers"]["swarmcloud"]["args"]
    assert any(f"@sc-v{manifest['version']}#" in arg for arg in pinned)  # MUTATION: bump the version, not the pin -- absent.
