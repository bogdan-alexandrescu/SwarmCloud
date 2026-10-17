"""Closest to finish first, everywhere running workflows are listed (BOARD-ORDER).

Owner decisions, 2026-10-16 (#1437):

* THE ORDER is steps done / steps total, descending; then the current step's
  age, descending (the step that has run longest first); then the workflow id,
  so the order is total. It replaces the "newest first" half of SC-ROWS.
* THE WORKSPACE BLOCKS STAY: one block per workspace, the blocks in the order
  of each block's closest-to-finish workflow, that order inside each block.
* EVERY running workflow is read in detail -- its progress is what orders it --
  at most eight reads at a time; before, only the newest 30 were.
* `sc workflows` prints, per workflow: workspace, id, name, done/total, the
  current step and its age, `p50 <m>m` and `eta ~<m>m` when known, the console.
* run.js's `groupByWorkspace` (the fallback path's mirror) orders the same way.

THE MUTATIONS THESE CATCH: order newest first again, or by fraction alone, or
by age ascending (the order tests go red); order the blocks by their first
listed entry instead of their closest (the block test goes red); put the
detail-read cap back (the 60-workflow test goes red); drop `done/total` or
the medians from the printer (the printer test goes red); let run.js keep the
order it was handed (the parity test goes red).
"""

from __future__ import annotations

import io
import json
import threading
import time
import types
from datetime import datetime, timedelta, timezone

import pytest

from swarm_mcp import board, sc
from swarm_mcp.client import SwarmClient

from test_plugin_agents_and_workflows import _RUN_JS, _node

NOW = datetime(2026, 10, 16, 20, 0, tzinfo=timezone.utc)


def _iso(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


@pytest.fixture(autouse=True)
def _no_remembered_medians():
    board.forget_medians()
    yield
    board.forget_medians()


def envelope(workflow_id: str, states: list[str], *, started_minutes_ago: int = 10,
             chain: bool = True, tenant: str = "eng", profile: str = "claude-code") -> dict:
    """A workflow of len(states) steps `s0..sN`, each a chain on the one before
    (or all side by side), whose unfinished started steps began
    `started_minutes_ago` before NOW."""
    steps, tasks = [], []
    for i, state in enumerate(states):
        task_id = f"task_{workflow_id}_{i}"
        steps.append({"step_id": f"s{i}", "task_id": task_id, "runner_profile": profile,
                      "depends_on": [f"s{i - 1}"] if chain and i else []})
        task = {"id": task_id, "state": state, "runner_profile": profile, "step_id": f"s{i}",
                "created_at": _iso(NOW - timedelta(hours=2))}
        if state in ("RUNNING", "STARTING", "SUCCEEDED"):
            task["started_at"] = _iso(NOW - timedelta(minutes=started_minutes_ago))
        if state == "SUCCEEDED":
            task["started_at"] = _iso(NOW - timedelta(minutes=90))
            task["completed_at"] = _iso(NOW - timedelta(minutes=60))
        tasks.append(task)
    return {"workflow": {"workflow_id": workflow_id, "tenant_id": tenant, "state": "RUNNING",
                         "steps": steps}, "tasks": tasks}


class FakeAPI:
    """The routes `running_workflows` reads, counted: the list, each workflow's
    detail, and the SUCCEEDED tasks the medians come from."""

    workflows = SwarmClient.workflows

    def __init__(self, envelopes: list[dict], succeeded: list[dict] | None = None,
                 fail_medians: bool = False, delay: float = 0.0) -> None:
        self.envelopes = {e["workflow"]["workflow_id"]: e for e in envelopes}
        self.order = [e["workflow"]["workflow_id"] for e in envelopes]
        self.succeeded = succeeded or []
        self.fail_medians = fail_medians
        self.delay = delay
        self.paths: list[str] = []
        self.base_url = "https://fake.test"
        self.tenant = None
        self._lock = threading.Lock()
        self.in_flight = 0
        self.most_in_flight = 0

    def request(self, method, path, **_):
        with self._lock:
            self.paths.append(path)
        if path.startswith("/v1/workflows?"):
            rows = [{"workflow_id": wid, "tenant_id": self.envelopes[wid]["workflow"]["tenant_id"],
                     "state": "RUNNING", "steps": self.envelopes[wid]["workflow"]["steps"],
                     "created_at": _iso(NOW - timedelta(minutes=n))}
                    for n, wid in enumerate(self.order)]
            return {"tenant_id": "eng", "workflows": rows, "next_page_token": None,
                    "filter": {"active": True, "stored_states": ["RUNNING"]}}
        if path.startswith("/v1/workflows/"):
            with self._lock:
                self.in_flight += 1
                self.most_in_flight = max(self.most_in_flight, self.in_flight)
            time.sleep(self.delay)
            with self._lock:
                self.in_flight -= 1
            return self.envelopes[path.rsplit("/", 1)[-1]]
        if path.startswith("/v1/tasks?"):
            if self.fail_medians:
                from swarm_mcp.client import SwarmError
                raise SwarmError("429 too many requests")
            return {"tasks": self.succeeded, "next_page_token": None, "tenant_id": "eng"}
        raise AssertionError(f"unexpected read {path}")

    def detail_reads(self) -> int:
        return sum(1 for p in self.paths if p.startswith("/v1/workflows/"))


# --------------------------------------------------------------------------
# progress()
# --------------------------------------------------------------------------


def test_progress_counts_finished_steps_and_ages_the_current_one():
    p = board.progress(envelope("wf_a", ["SUCCEEDED", "RUNNING", "QUEUED"], started_minutes_ago=12), NOW)
    assert (p["done"], p["total"], p["fraction"]) == (1, 3, round(1 / 3, 4))
    (current,) = p["current"]
    assert (current["step_id"], current["task_id"], current["state"]) == ("s1", "task_wf_a_1", "RUNNING")
    assert current["age_seconds"] == 12 * 60 and current["age_basis"] == "started_at"
    assert [s["step_id"] for s in p["ahead"]] == ["s2"], "a step waiting on an unfinished parent is ahead"
    assert p["age_seconds"] == 12 * 60


def test_a_step_free_to_run_but_not_started_counts_from_its_parents_end():
    p = board.progress(envelope("wf_a", ["SUCCEEDED", "QUEUED"]), NOW)
    (current,) = p["current"]
    assert current["age_basis"] == "parents_completed_at" and current["age_seconds"] == 60 * 60


def test_every_terminal_state_counts_as_done_as_the_listing_already_counted_it():
    """FAILED under `on_step_failure: continue`, CANCELLED, DEAD_LETTERED: the
    same terminal set `sc._read_detail` leaves out of `current_steps`."""
    p = board.progress(envelope("wf_a", ["FAILED", "CANCELLED", "DEAD_LETTERED", "RUNNING"], chain=False), NOW)
    assert (p["done"], p["total"]) == (3, 4)
    assert board.FINISHED == sc._TERMINAL_VALUES


# --------------------------------------------------------------------------
# order()
# --------------------------------------------------------------------------


def _entry(wid: str, done: int, total: int, age: int | None, workspace: str = "eng") -> dict:
    return {"workflow_id": wid, "workspace": workspace,
            "progress": {"done": done, "total": total, "age_seconds": age}}


def test_the_order_is_fraction_then_age_then_id():
    entries = [
        _entry("wf_new", 0, 4, 60),       # newest, furthest
        _entry("wf_half_young", 2, 4, 60),
        _entry("wf_b", 3, 4, 300),
        _entry("wf_half_old", 1, 2, 900),
        _entry("wf_a", 3, 4, 300),         # ties wf_b: the id decides
        _entry("wf_unknown_age", 2, 4, None),
    ]
    assert [e["workflow_id"] for e in board.order(entries)] == [
        "wf_a", "wf_b", "wf_half_old", "wf_half_young", "wf_unknown_age", "wf_new",
    ]


def test_the_order_is_total_whatever_order_it_was_handed():
    entries = [_entry(f"wf_{n}", n % 3, 3, (n * 37) % 11) for n in range(12)]
    once = [e["workflow_id"] for e in board.order(entries)]
    assert [e["workflow_id"] for e in board.order(list(reversed(entries)))] == once


def test_a_workflow_whose_progress_was_not_read_sorts_below_every_read_one():
    entries = [{"workflow_id": "wf_unread", "workspace": "eng", "age_seconds": 99999},
               _entry("wf_zero", 0, 5, 1)]
    assert [e["workflow_id"] for e in board.order(entries)] == ["wf_zero", "wf_unread"]


def test_single_tasks_are_ordered_by_their_own_state_age():
    tasks = [{"task_id": "task_young", "state_age_seconds": 10},
             {"task_id": "task_old", "state_age_seconds": 900},
             {"task_id": "task_mid", "state_age_seconds": 300}]
    assert [t["task_id"] for t in board.order(tasks)] == ["task_old", "task_mid", "task_young"]


def test_the_blocks_come_in_the_order_of_each_blocks_closest_workflow():
    """`eng` is listed first and holds the newest, but `u-bogdan` holds the
    workflow closest to finish: its block comes first, and inside each block
    the progress order holds."""
    entries = [
        _entry("wf_1", 0, 3, 50, "eng"),
        _entry("wf_2", 2, 3, 50, "u-bogdan"),
        _entry("wf_3", 1, 3, 50, "eng"),
        _entry("wf_4", 1, 3, 70, "u-bogdan"),
    ]
    assert [e["workflow_id"] for e in sc.group_by_workspace(entries)] == ["wf_2", "wf_4", "wf_3", "wf_1"]


# --------------------------------------------------------------------------
# running_workflows(): every one read, in board order
# --------------------------------------------------------------------------


def test_every_running_workflow_is_read_eight_at_a_time_and_listed_closest_first():
    envelopes = [envelope(f"wf_{n:02d}", ["SUCCEEDED"] * (n % 4) + ["RUNNING"] + ["QUEUED"] * (3 - n % 4),
                          started_minutes_ago=n + 1) for n in range(60)]
    api = FakeAPI(envelopes, delay=0.005)
    listing = sc.running_workflows(api, now=NOW)
    assert api.detail_reads() == 60, "every running workflow is read: its progress orders it"
    assert api.most_in_flight <= sc.WORKFLOW_DETAIL_CONCURRENCY == 8
    assert not any("steps_unread_because" in e for e in listing["workflows"])
    keys = [(e["progress"]["done"] / e["progress"]["total"], e["progress"]["age_seconds"])
            for e in listing["workflows"]]
    assert keys == sorted(keys, key=lambda k: (-k[0], -k[1]))
    assert listing["workflows"][0]["workflow_id"] == "wf_59", "3 of 4 done, its step the oldest of those"


# --------------------------------------------------------------------------
# `sc workflows`
# --------------------------------------------------------------------------


def test_sc_workflows_prints_progress_the_current_step_and_the_medians(monkeypatch):
    entry = {
        "workflow_id": "wf_1", "workspace": "u-bogdan", "label": "gate", "state": "RUNNING", "age_seconds": 5400,
        "console": "https://console.test/w/wf_1", "current_steps": [],
        "progress": {"done": 3, "total": 5, "fraction": 0.6, "age_seconds": 720, "ahead": [],
                     "current": [{"step_id": "review", "task_id": "t", "state": "RUNNING",
                                  "age_seconds": 720, "median_seconds": 1200}],
                     "remaining_median_seconds": 600, "eta_seconds": 1080},
    }
    monkeypatch.setattr(sc, "running_workflows", lambda client: {
        "tenant_id": "eng", "count": 1, "complete": True, "workflows": [entry]})
    monkeypatch.setattr(sc, "running_tasks", lambda client: {"tenant_id": "eng", "count": 0, "complete": True,
                                                             "tasks": []})
    out = io.StringIO()
    args = types.SimpleNamespace(json=False, width=240, color="never", ascii=True, session_start=False)
    sc.cmd_workflows(types.SimpleNamespace(tenants_mine=lambda: []), args, out)
    line = next(l for l in out.getvalue().splitlines() if "wf_1" in l)
    assert line.startswith("  [u-bogdan] wf_1  gate  RUNNING  3/5  now: review 12m  p50 20m  eta ~18m"), line
    assert "console: https://console.test/w/wf_1" in out.getvalue()


# --------------------------------------------------------------------------
# run.js agrees
# --------------------------------------------------------------------------


_JS_HARNESS = r"""
const fs = require('fs')
const src = fs.readFileSync(process.argv[2], 'utf8')
const pick = (name) => {
  const at = src.indexOf('function ' + name + '(')
  let depth = 0, i = src.indexOf('{', at)
  for (; i < src.length; i++) { if (src[i] === '{') depth++; else if (src[i] === '}' && --depth === 0) break }
  return src.slice(at, i + 1)
}
const flatText = (v) => typeof v === 'string' && v.trim() ? v.trim() : null
eval(pick('boardOrder') + '\n' + pick('groupByWorkspace') + '\nglobalThis.g = groupByWorkspace')
const entries = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'))
process.stdout.write(JSON.stringify(g(entries).map((e) => e.workflow_id || e.task_id)))
"""


def _js_group(tmp_path, entries: list[dict]) -> list[str]:
    import subprocess

    harness = tmp_path / "group.cjs"
    harness.write_text(_JS_HARNESS)
    given = tmp_path / "entries.json"
    given.write_text(json.dumps(entries))
    done = subprocess.run([_node(), str(harness), str(_RUN_JS), str(given)],
                          capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_run_js_groups_and_orders_exactly_as_the_bridge(tmp_path):
    entries = [_entry(f"wf_{n:02d}", n % 4, 4, (n * 53) % 17 * 60, ("eng", "u-bogdan", "ops")[n % 3])
               for n in range(24)]
    shuffled = entries[7:] + entries[:7]
    bridge = [e["workflow_id"] for e in sc.group_by_workspace(shuffled)]
    assert _js_group(tmp_path, shuffled) == bridge


def test_run_js_keeps_the_handed_order_when_no_progress_was_relayed(tmp_path):
    """attach_tasks rows, and a bridge older than the rule, carry no progress:
    the order they were handed is the bridge's, and is kept."""
    tasks = [{"task_id": "task_z", "workspace": "eng"}, {"task_id": "task_a", "workspace": "eng"}]
    assert _js_group(tmp_path, tasks) == ["task_z", "task_a"]
