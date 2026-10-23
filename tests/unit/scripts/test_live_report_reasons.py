"""live_report.py reports each task's unavailable time by reason, not only `blank_s` (FOLLOWUPS-1023; part of #2150).

WHY THIS EXISTS. The gate's `live_viewer_metrics` line has carried
`unavailable_ms_by_reason` and `unavailable_longest_ms_by_reason` since the
unavailable time was split by reason (agent_worker/livegate/rfb.py,
`UNAVAILABLE_REASON_KEYS`), but the report printed only `blank_s`, so #1344's
2026-10-23 reading needed raw `gcloud logging read` calls to tell "no page is
open" from "the page has not reported its layout". So, per task:

  * the seconds each reason held, summed over the task's viewer connections,
    and its longest stretch, the longest any one connection logged;
  * a task whose lines predate those keys reads "not recorded", never 0: a
    zero would claim the frames were never unavailable, which nobody measured;
  * a task some of whose lines carry them says on how many connections.

Fixture lines only: no network, no credentials.
"""

from __future__ import annotations

import json

from .test_live_report import NOW, _cli, _report, _write, gate_entry


def _row(report, task):
    (row,) = [r for r in report["tasks"] if r["task_id"] == task]
    return row


def _with_reasons(task="task_aa11", conn="v-1", **kw):
    return gate_entry(task, conn, unavailable_ms=21000.0, blank_ms=21000.0,
                      unavailable_ms_by_reason={"no_page": 18000.0, "not_reported": 3000.0},
                      unavailable_longest_ms_by_reason={"no_page": 18000.0, "not_reported": 1500.0},
                      **kw)


def _without_reasons(task="task_bb22", conn="v-9"):
    # A line from before the split: `unavailable_ms` but neither by-reason key.
    return gate_entry(task, conn, unavailable_ms=4000.0, blank_ms=4000.0)


def test_a_line_with_reasons_reports_seconds_and_longest_stretch_by_reason(tmp_path):
    row = _row(_report(tmp_path, gate=[_with_reasons()]), "task_aa11")
    # MUTATION: drop the by-reason sum from build()'s row -> KeyError/None -> red.
    assert row["unavailable_s_by_reason"] == {"no_page": 18.0, "not_reported": 3.0}
    # MUTATION: report the longest in ms, or sum it instead of taking the max -> red.
    assert row["unavailable_longest_s_by_reason"] == {"no_page": 18.0, "not_reported": 1.5}
    # MUTATION: count lines without the keys as recorded -> red on the next test, and
    # a wrong count here -> red.
    assert row["unavailable_reasons_recorded"] == 1


def test_a_line_from_before_the_keys_reads_not_recorded_never_zero(tmp_path):
    row = _row(_report(tmp_path, gate=[_without_reasons()]), "task_bb22")
    # MUTATION: default a missing key to {} or 0 -> red.
    assert row["unavailable_s_by_reason"] is None
    assert row["unavailable_longest_s_by_reason"] is None
    assert row["unavailable_reasons_recorded"] == 0
    # blank_s is unchanged beside it.
    assert row["blank_s"] == 4.0


def test_reasons_sum_across_connections_and_the_longest_is_the_longest_one(tmp_path):
    second = gate_entry("task_aa11", "v-2", unavailable_ms=10000.0, blank_ms=10000.0,
                        unavailable_ms_by_reason={"no_page": 4000.0, "hidden": 6000.0},
                        unavailable_longest_ms_by_reason={"no_page": 4000.0, "hidden": 6000.0})
    row = _row(_report(tmp_path, gate=[_with_reasons(), second]), "task_aa11")
    # MUTATION: keep only one connection's dict -> red.
    assert row["unavailable_s_by_reason"] == {"hidden": 6.0, "no_page": 22.0, "not_reported": 3.0}
    # MUTATION: sum the longest stretches (22.0 for no_page) -> red.
    assert row["unavailable_longest_s_by_reason"] == {"hidden": 6.0, "no_page": 18.0,
                                                      "not_reported": 1.5}


def test_an_empty_reason_map_is_recorded_as_no_unavailable_time(tmp_path):
    line = gate_entry(unavailable_ms_by_reason={}, unavailable_longest_ms_by_reason={})
    row = _row(_report(tmp_path, gate=[line]), "task_aa11")
    # MUTATION: treat an empty dict like a missing key -> None -> red.
    assert row["unavailable_s_by_reason"] == {}
    assert row["unavailable_reasons_recorded"] == 1


def test_the_recorder_is_not_counted_in_the_reasons(tmp_path):
    recorder = _with_reasons(conn="rec-1")
    row = _row(_report(tmp_path, gate=[_without_reasons("task_aa11"), recorder]), "task_aa11")
    # MUTATION: read the reasons from every gate line, recorders included -> red.
    assert row["unavailable_s_by_reason"] is None


def test_the_printed_report_names_each_reason_and_not_recorded(tmp_path):
    g, a, d = _write(tmp_path, gate=[_with_reasons(), _without_reasons()])
    run = _cli("report", "--gate", str(g), "--api", str(a), "--tasks-dir", str(d), "--now", NOW)
    out = run.stdout
    # MUTATION: drop the section from render() -> red.
    assert "Unavailable time by reason" in out, out
    (aa,) = [ln for ln in out.splitlines() if ln.startswith("  task_aa11 ")]
    (bb,) = [ln for ln in out.splitlines() if ln.startswith("  task_bb22 ")]
    # MUTATION: print the total without the longest stretch, or the other way round -> red.
    assert "no_page 18s (longest 18s)" in aa and "not_reported 3s (longest 1.5s)" in aa, aa
    # MUTATION: render None as "0" or "-" -> red.
    assert "not recorded" in bb and "0s" not in bb, bb


def test_a_task_partly_recorded_says_on_how_many_connections(tmp_path):
    g, a, d = _write(tmp_path, gate=[_with_reasons(), _without_reasons("task_aa11")])
    run = _cli("report", "--gate", str(g), "--api", str(a), "--tasks-dir", str(d), "--now", NOW)
    (aa,) = [ln for ln in run.stdout.splitlines() if ln.startswith("  task_aa11 ")]
    # MUTATION: print a partial sum as if it covered every connection -> red.
    assert "recorded on 1 of 2 connections" in aa, aa


def test_json_carries_the_reasons_and_null_for_not_recorded(tmp_path):
    g, a, d = _write(tmp_path, gate=[_with_reasons(), _without_reasons()])
    run = _cli("report", "--gate", str(g), "--api", str(a), "--tasks-dir", str(d), "--now", NOW,
               "--json")
    rows = {r["task_id"]: r for r in json.loads(run.stdout)["tasks"]}
    # MUTATION: drop the keys from the row -> KeyError -> red.
    assert rows["task_aa11"]["unavailable_s_by_reason"]["no_page"] == 18.0
    assert rows["task_bb22"]["unavailable_s_by_reason"] is None
