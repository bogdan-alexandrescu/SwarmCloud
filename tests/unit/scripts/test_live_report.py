"""The live-browser report's parsing, arithmetic and verdicts, offline (LIVE-MEASURE).

`scripts/live_report.py` reads what `scripts/live-report.sh` fetched -- the
display sidecar's `live_viewer_metrics` lines and swarm-api's funnel lines as
`gcloud logging read --format json` prints them, and each task's
`/startup` and `/live/actions` bodies -- and judges the Phase-2 targets the
owner set on 2026-10-13. The rules pinned here are the ones that would let it
report a pass it did not measure: a connection counted twice, the recorder
counted as a person, percentiles averaged, and nothing read exiting zero.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "scripts" / "live_report.py"
SCRIPT = ROOT / "scripts" / "live-report.sh"
NOW = "2026-10-14T12:00:00+00:00"
RECENT = "2026-10-14T10:00:00Z"
BOUNDS = [50, 100, 150, 200, 300, 500, 1000, 2000, 5000]


def _load():
    spec = importlib.util.spec_from_file_location("live_report", HELPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lr = _load()


def gate_entry(task="task_aa11", conn="v-1", *, pod="swarm-aa11-1-xyz", final=True, at=RECENT,
               **fields):
    line = {
        "event": "live_viewer_metrics", "conn": conn,
        "role": "recorder" if conn.startswith("rec-") else "viewer", "final": final,
        "frames_captured": 300, "frames_changed": 120, "updates": 110,
        "changing_seconds": 10, "updates_while_changing": 120, "fps_while_changing": 12.0,
        "longest_gap_ms": 180.0, "blank_ms": 0.0, "unavailable_ms": 0.0, "page_blanked_ms": 0.0,
        "input_to_frame_bounds_ms": BOUNDS,
        "input_to_frame_hist": [10, 5, 3, 2, 0, 0, 0, 0, 0, 0],
        "keys_read": 20, "keys": 20, "pointer": 9,
        "severity": "INFO", "message": "live viewer metrics",
    }
    line.update(fields)
    return {
        "timestamp": at,
        "resource": {"type": "k8s_container",
                     "labels": {"cluster_name": "swarm-autopilot", "container_name": "display",
                                "pod_name": pod}},
        "labels": {"k8s-pod/swarm-task": task.replace("_", "-")},
        "jsonPayload": line,
    }


def funnel_entry(task="task_aa11", request="signin-1", *, outcome="returned", viewer=12,
                 control=17, typed=20, resolved=40, at=RECENT):
    return {"timestamp": at, "jsonPayload": {
        "event": "live_handoff_funnel", "task_id": task, "tenant_id": "eng",
        "attempt_id": "att_1", "request_id": request, "outcome": outcome,
        "first_viewer_s": viewer, "control_taken_s": control, "first_input_s": typed,
        "resolved_s": resolved, "person_present": viewer is not None}}


def ready_entry(task="task_aa11", attempt="att_2", seconds=14.2, at=RECENT):
    return {"timestamp": at, "jsonPayload": {
        "event": "live_ready_first_frame", "task_id": task, "tenant_id": "eng",
        "attempt_id": attempt, "seconds": seconds}}


def startup_body(attempt="att_2", *, node=8, pull=0.4, frame=25):
    t0 = "2026-10-14T10:00:00+00:00"

    def at(s):
        return f"2026-10-14T10:{int(s) // 60:02d}:{s % 60:06.3f}+00:00"

    return {"attempt_id": attempt, "generation": 2, "done": True, "steps": [
        {"step": "dispatched", "at": t0},
        {"step": "pod_scheduled", "at": at(node)},
        {"step": "image_pulling", "at": at(node + 1)},
        {"step": "image_pulled", "at": at(node + 1 + pull)},
        {"step": "first_frame", "at": at(frame)},
    ]}


def _write(tmp_path, gate=(), api=(), tasks=None):
    g, a = tmp_path / "gate.json", tmp_path / "api.json"
    g.write_text(json.dumps(list(gate)))
    a.write_text(json.dumps(list(api)))
    d = tmp_path / "tasks"
    d.mkdir(exist_ok=True)
    for name, body in (tasks or {}).items():
        (d / name).write_text(json.dumps(body))
    return g, a, d


def _report(tmp_path, **kw):
    g, a, d = _write(tmp_path, **kw)
    gate = lr.viewer_lines(lr._entries(g))
    funnels = lr.api_lines(lr._entries(a), lr.FUNNEL_EVENT)
    readies = lr.api_lines(lr._entries(a), lr.READY_EVENT)
    return lr.build(gate, funnels, readies, d)


def _target(report, prefix):
    (t,) = [t for t in report["targets"] if t["target"].startswith(prefix)]
    return t


# ---------------------------------------------------------------------------
# Reading.


def test_the_event_names_are_the_ones_the_gate_and_swarm_api_write():
    from agent_worker.livegate import rfb
    from swarm_api import handoff

    assert lr.GATE_EVENT == rfb.VIEWER_METRICS_EVENT
    assert lr.FUNNEL_EVENT == handoff.FUNNEL_EVENT
    assert lr.READY_EVENT == handoff.READY_FRAME_EVENT
    assert lr.TARGET_INPUT_P95_MS in rfb.INPUT_TO_FRAME_BOUNDS_MS


def test_the_pod_label_is_read_back_to_the_task_id():
    assert lr.task_from_label("task-1d1785b21d5b4e1f894e") == "task_1d1785b21d5b4e1f894e"
    assert lr.task_from_label("not-a-task") is None
    assert lr.task_from_label(None) is None


def test_a_connection_is_counted_once_from_its_close_line():
    entries = [
        gate_entry(final=False, at="2026-10-14T10:01:00Z", keys=3),
        gate_entry(final=False, at="2026-10-14T10:02:00Z", keys=7),
        gate_entry(final=True, at="2026-10-14T10:02:30Z", keys=9),
    ]
    (line,) = lr.viewer_lines(entries)
    assert line["keys"] == 9 and line["task_id"] == "task_aa11"


def test_a_connection_with_no_close_line_is_read_from_its_latest():
    entries = [gate_entry(final=False, at="2026-10-14T10:01:00Z", keys=3),
               gate_entry(final=False, at="2026-10-14T10:02:00Z", keys=7)]
    (line,) = lr.viewer_lines(entries)
    assert line["keys"] == 7


def test_the_same_conn_id_on_two_pods_is_two_connections():
    entries = [gate_entry(pod="p-1"), gate_entry(pod="p-2")]
    assert len(lr.viewer_lines(entries)) == 2


def test_lines_outside_the_window_and_other_events_are_not_read():
    old = gate_entry(at="2026-10-12T10:00:00Z")
    other = gate_entry()
    other["jsonPayload"]["event"] = "something_else"
    cutoff = lr.parse_time(NOW) - lr.timedelta(hours=24)
    assert lr.viewer_lines([old, other], cutoff=cutoff) == []


def test_a_hand_off_logged_twice_is_one_hand_off():
    entries = [funnel_entry(), funnel_entry()]
    assert len(lr.api_lines(entries, lr.FUNNEL_EVENT)) == 1


def test_startup_is_split_cold_and_warm_on_node_allocation_and_pull():
    warm = lr.startup_of(startup_body(node=6, pull=0.3, frame=22))
    assert (warm["class"], warm["node_s"], warm["pull_s"], warm["startup_s"]) == (
        "warm", 6.0, 0.3, 22.0)
    assert lr.startup_of(startup_body(node=75, pull=0.3, frame=100))["class"] == "cold"
    assert lr.startup_of(startup_body(node=6, pull=40, frame=60))["class"] == "cold"
    assert lr.startup_of(None)["class"] == "unknown"
    assert lr.startup_of({"steps": []})["class"] == "unknown"


def test_google_sorry_hits_are_counted_from_the_action_log():
    body = {"actions": [
        {"kind": "navigate", "url": "https://www.google.com/search?q=x"},
        {"kind": "navigate", "url": "https://www.google.com/sorry/index?continue=x"},
        {"kind": "navigate", "url": "https://google.co.uk/sorry/"},
        {"kind": "navigate", "url": "https://example.com/sorry/"},
        {"kind": "control", "event": "control_taken"},
    ]}
    assert lr.sorry_hits(body) == (2, 3)
    assert lr.sorry_hits(None) == (0, 0)


# ---------------------------------------------------------------------------
# The targets.


def test_a_healthy_window_passes_every_target(tmp_path):
    report = _report(
        tmp_path,
        gate=[gate_entry(), gate_entry(conn="rec-1", updates_while_changing=0, keys_read=0, keys=0)],
        api=[funnel_entry(), funnel_entry(request="r-2"), funnel_entry(request="r-3"),
             funnel_entry(request="r-4", outcome="expired"),
             ready_entry(attempt="att_2", seconds=14.2)],
        tasks={"task_aa11.startup.json": startup_body("att_2", node=6)},
    )
    verdicts = {t["target"]: t["verdict"] for t in report["targets"]}
    # No cold resume was measured: that target says so rather than passing.
    cold = _target(report, "I'm ready -> visible p90 < 60")
    assert cold["verdict"] == "NO DATA"
    assert [v for k, v in verdicts.items() if k != cold["target"]] == ["PASS"] * 5
    assert lr.exit_code(report) == 3
    (row,) = report["tasks"]
    assert (row["viewers"], row["recorders"]) == (1, 1)
    assert row["class"] == "warm" and row["ready_to_frame_s"] == [14.2]


def test_the_recorder_is_not_a_person_watching():
    """The recorder sees the stream too; its rate must not lift a stalled person's."""
    person = gate_entry(changing_seconds=60, updates_while_changing=60)
    recorder = gate_entry(conn="rec-1", changing_seconds=60, updates_while_changing=1800)
    lines = lr.viewer_lines([person, recorder])
    report = lr.build(lines, [], [], None)
    fps = _target(report, "viewer fps")
    assert fps["value"] == 1.0 and fps["verdict"] == "FAIL"
    assert lr.exit_code(report) == 1


def test_fps_is_pooled_over_changing_seconds_not_averaged_per_connection():
    a = gate_entry(conn="v-a", changing_seconds=100, updates_while_changing=1500)
    b = gate_entry(conn="v-b", changing_seconds=2, updates_while_changing=2)
    report = lr.build(lr.viewer_lines([a, b]), [], [], None)
    # (1500 + 2) / 102, not the mean of 15 and 1.
    assert _target(report, "viewer fps")["value"] == pytest.approx(14.73, abs=0.01)


def test_input_p95_is_read_from_the_pooled_histogram_at_200_ms():
    fast = gate_entry(conn="v-a", input_to_frame_hist=[90, 5, 0, 0, 0, 0, 0, 0, 0, 0])
    slow = gate_entry(conn="v-b", input_to_frame_hist=[0, 0, 0, 0, 0, 0, 0, 0, 0, 5])
    report = lr.build(lr.viewer_lines([fast]), [], [], None)
    assert _target(report, "input_to_frame")["verdict"] == "PASS"
    report = lr.build(lr.viewer_lines([fast, slow]), [], [], None)
    t = _target(report, "input_to_frame")
    # 95 of 100 within 200 ms is exactly p95: still a pass.
    assert t["verdict"] == "PASS" and t["n"] == 100
    worse = gate_entry(conn="v-c", input_to_frame_hist=[0, 0, 0, 0, 1, 0, 0, 0, 0, 0])
    report = lr.build(lr.viewer_lines([fast, slow, worse]), [], [], None)
    t = _target(report, "input_to_frame")
    assert t["verdict"] == "FAIL" and t["value"] == "<=300"


def test_a_lost_key_fails_the_keys_target():
    report = lr.build(lr.viewer_lines([gate_entry(keys_read=41, keys=40)]), [], [], None)
    t = _target(report, "every key")
    assert t["verdict"] == "FAIL" and t["value"] == "40/41"


def test_ready_to_visible_is_judged_by_the_resumed_attempts_start_up(tmp_path):
    report = _report(
        tmp_path,
        api=[ready_entry(task="task_aa11", attempt="att_2", seconds=48.0),
             ready_entry(task="task_bb22", attempt="att_5", seconds=55.0),
             ready_entry(task="task_cc33", attempt="att_9", seconds=500.0)],
        tasks={"task_aa11.startup.json": startup_body("att_2", node=80, frame=100),
               "task_bb22.startup.json": startup_body("att_5", node=5, frame=20),
               # task_cc33's current attempt is a later one: not this measurement's.
               "task_cc33.startup.json": startup_body("att_10", node=5)},
    )
    assert _target(report, "I'm ready -> visible p90 < 60")["verdict"] == "PASS"   # 48 s cold
    assert _target(report, "I'm ready -> visible p90 < 20")["verdict"] == "FAIL"   # 55 s warm
    assert report["read"]["ready_unclassified"] == 1


def test_expiry_counts_only_hand_offs_a_person_was_present_for(tmp_path):
    report = _report(tmp_path, api=[
        funnel_entry(request="r-1", outcome="expired", viewer=None, control=None, typed=None),
        funnel_entry(request="r-2", outcome="expired", viewer=None, control=None, typed=None),
        funnel_entry(request="r-3", outcome="returned"),
        funnel_entry(request="r-4", outcome="expired", control=None, typed=None),
    ])
    t = _target(report, "hand-off expiry")
    # 1 of the 2 with a person present expired: 50%.
    assert t["n"] == 2 and t["value"] == 0.5 and t["verdict"] == "FAIL"
    f = report["funnel"]
    assert f["requested"] == 4 and f["first_viewer_s"]["n"] == 2
    assert f["outcomes"] == {"expired": 3, "returned": 1}


def test_listed_tasks_with_no_line_still_get_a_row(tmp_path):
    g, a, d = _write(tmp_path, tasks={"task_dd44.startup.json": startup_body("att_1", node=90)})
    report = lr.build([], [], [], d, ["task_dd44"])
    (row,) = report["tasks"]
    assert row["task_id"] == "task_dd44" and row["class"] == "cold" and row["viewers"] == 0


# ---------------------------------------------------------------------------
# The CLI.


def _cli(*args, cwd=None):
    return subprocess.run([sys.executable, str(HELPER), *args], capture_output=True, text=True,
                          cwd=cwd)


def test_reading_nothing_exits_2_and_never_passes(tmp_path):
    g, a, d = _write(tmp_path)
    run = _cli("report", "--gate", str(g), "--api", str(a), "--tasks-dir", str(d), "--now", NOW)
    assert run.returncode == 2, run.stdout + run.stderr
    assert "NOTHING READ" in run.stdout and "PASS " not in run.stdout


def test_the_table_and_the_targets_are_printed(tmp_path):
    g, a, d = _write(tmp_path, gate=[gate_entry()], api=[funnel_entry()],
                     tasks={"task_aa11.startup.json": startup_body("att_2")})
    run = _cli("report", "--gate", str(g), "--api", str(a), "--tasks-dir", str(d), "--now", NOW)
    assert run.returncode == 3, run.stdout + run.stderr
    assert "task_aa11" in run.stdout and "Phase-2 targets:" in run.stdout
    assert "Hand-off funnel" in run.stdout
    for word in ("PASS", "NO DATA"):
        assert word in run.stdout


def test_json_output_is_one_document_with_its_exit(tmp_path):
    g, a, d = _write(tmp_path, gate=[gate_entry(keys_read=3, keys=2)])
    run = _cli("report", "--gate", str(g), "--api", str(a), "--tasks-dir", str(d), "--now", NOW,
               "--json")
    doc = json.loads(run.stdout)
    assert doc["exit"] == run.returncode == 1
    assert {"read", "tasks", "funnel", "targets"} <= set(doc)


def test_a_malformed_log_file_is_exit_2(tmp_path):
    g = tmp_path / "gate.json"
    g.write_text('{"not": "an array"}')
    run = _cli("report", "--gate", str(g), "--now", NOW)
    assert run.returncode == 2


def test_tasks_lists_every_task_the_lines_name(tmp_path):
    g, a, _ = _write(tmp_path, gate=[gate_entry(task="task_aa11")],
                     api=[funnel_entry(task="task_bb22")])
    run = _cli("tasks", "--gate", str(g), "--api", str(a), "--now", NOW)
    assert run.stdout.split() == ["task_aa11", "task_bb22"]


def test_window_stops_at_the_first_task_older_than_the_window():
    page = {"tasks": [{"id": "task_new", "created_at": "2026-10-14T11:00:00Z"},
                      {"id": "task_old", "created_at": "2026-10-12T11:00:00Z"}],
            "next_page_token": "x"}
    assert lr.window_lines(page, hours=24, now=NOW) == ["task task_new", "older"]
    page["tasks"].pop()
    assert lr.window_lines(page, hours=24, now=NOW) == ["task task_new", "next x"]


def test_the_live_profiles_are_the_catalogues():
    assert "claude-code-browser" in lr.live_profiles()


# ---------------------------------------------------------------------------
# The shell script's shape.


def _effective_lines(text):
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            yield stripped


def test_script_carries_set_euo_pipefail_first_and_sources_common():
    text = SCRIPT.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    assert next(_effective_lines(text)) == "set -euo pipefail"
    assert re.search(r"^source .*lib/common\.sh\"?$", text, re.MULTILINE)
    assert SCRIPT.stat().st_mode & 0o111, "the script must be executable"
    assert "| redact" in text


def test_script_is_read_only():
    text = SCRIPT.read_text()
    for verb in ("POST", "PUT", "PATCH", "DELETE"):
        assert not re.search(rf"\b{verb}\b", text), verb
    assert "api_post" not in text and "fs_" not in text
    assert "kubectl" not in text and "kc " not in text
    calls = re.findall(r"^\s*gcloud ([a-z-]+ [a-z-]+)", text, re.MULTILINE)
    assert calls == ["logging read"], calls


def test_script_reads_our_cluster_only_and_refuses_the_shared_one():
    text = SCRIPT.read_text()
    assert 'resource.labels.cluster_name=\\"${GKE_CLUSTER}\\"' in text
    assert 'is_shared_resource "${GKE_CLUSTER}"' in text
    assert "agents-staging" not in "\n".join(_effective_lines(text))


def test_script_avoids_bash4_only_constructs():
    text = SCRIPT.read_text()
    assert "mapfile" not in text and "readarray" not in text
    assert "declare -A" not in text
    assert ",,}" not in text and "^^}" not in text
