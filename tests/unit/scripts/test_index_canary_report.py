"""scripts/index_canary_report.py: the index canary over the index-runs read.

Held here, with no credentials: the build ratio counts what runs built, the
rebuild reasons are grouped, the phase figures count every measured run (a
failed one too) and keep unknowns out of the percentiles, lag is over the
promoted runs, a window cut by run retention says so, and reading nothing
exits 2. The last test feeds it a run swarm-api itself measured
(`repoindex.run_metrics`, served by `run_to_api`), so the two agree on names.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "index_canary_report.py"
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
SHA = "ab" * 20


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("index_canary_report", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["index_canary_report"] = module
    spec.loader.exec_module(module)
    return module


canary = _module()


def _phases(**seconds: float | None) -> dict[str, float | None]:
    return {name: seconds.get(name) for name in canary.PHASES}


def _run(n: int, *, hours_ago: float = 1.0, built: str | None = "incremental",
         requested: str = "incremental", reason: str | None = None,
         outcome: str | None = "promoted", state: str = "SUCCEEDED",
         phases: dict | None = None, files: dict | None = None, lag: float | None = 600.0,
         at_head: bool | None = True, metrics: bool = True) -> dict[str, Any]:
    ended = NOW - timedelta(hours=hours_ago)
    run: dict[str, Any] = {
        "task_id": f"task_{n}", "repo_id": "repo_a", "commit_sha": SHA, "kind": requested,
        "state": state, "queued_at": (ended - timedelta(minutes=30)).isoformat(),
        "ended_at": ended.isoformat(),
        "promotion": {"outcome": outcome} if outcome else None, "metrics": None,
    }
    if metrics:
        run["metrics"] = {
            "requested_kind": requested, "built_kind": built, "rebuild_reason": reason,
            "phases": phases or _phases(extraction=100.0, judgment=50.0, upload=20.0,
                                        promotion=5.0),
            "files": files, "lag_seconds": lag, "at_head": at_head,
        }
    return run


def _doc(*runs: dict[str, Any], repo_id: str = "repo_a") -> dict[str, Any]:
    return {"repo_id": repo_id, "tenant_id": "eng", "runs": list(runs)}


def _report(*docs: dict[str, Any], hours: float = 168) -> dict[str, Any]:
    return canary.report(list(docs), now=NOW, window=timedelta(hours=hours))


def test_the_ratio_counts_what_was_built_and_the_reasons_are_grouped():
    figures = _report(_doc(
        _run(1, built="full", requested="full", reason="the weekly full run is due"),
        _run(2, built="full", reason=f"the extractor did not carry the base: base {SHA} cut"),
        _run(3, built="full", reason=f"the extractor did not carry the base: base {'cd' * 20} cut"),
        _run(4), _run(5),
        _run(6, built=None, outcome=None, state="FAILED", lag=None, at_head=None),
    ))
    assert figures["built"] == {"full": 3, "incremental": 2}
    # MUTATION: dividing by every run, the failed one too, reads 0.333.
    assert figures["incremental_share"] == 0.4
    assert figures["requested"] == {"full": 1, "incremental": 5}
    # Two reasons that differ only in a sha are one reason.
    assert figures["rebuild_reasons"] == [
        {"reason": "the extractor did not carry the base: base <sha> cut", "runs": 2},
        {"reason": "the weekly full run is due", "runs": 1},
    ]
    assert figures["states"] == {"FAILED": 1, "SUCCEEDED": 5}


def test_every_measured_run_costs_and_an_unknown_is_never_zero():
    figures = _report(_doc(
        _run(1, phases=_phases(extraction=100.0, judgment=40.0)),
        _run(2, phases=_phases(extraction=300.0, judgment=None)),
        # A failed run's extraction ran and is counted.
        _run(3, built=None, outcome=None, state="FAILED",
             phases=_phases(extraction=720.0)),
        _run(4, metrics=False),
    ))
    extraction = figures["phases"]["extraction"]
    # MUTATION: leaving the failed run out of the phases under-counts the cost.
    assert extraction == {"n": 3, "unknown": 0, "p50": 300.0, "p90": 720.0, "max": 720.0}
    judgment = figures["phases"]["judgment"]
    # MUTATION: reading None as 0.0 and the p50 is 0.
    assert judgment == {"n": 1, "unknown": 2, "p50": 40.0, "p90": 40.0, "max": 40.0}
    assert figures["phases"]["language_servers"]["n"] == 0
    assert figures["phases"]["language_servers"]["p50"] is None
    assert figures["measured"] == 3 and figures["unmeasured"] == 1
    assert figures["phases_by_kind"]["incremental"]["extraction"]["n"] == 2


def test_lag_is_over_promoted_runs_and_files_are_summed_or_unknown():
    figures = _report(_doc(
        _run(1, lag=60.0, files={"parsed": 100, "reused": 90, "recomputed": 10}),
        _run(2, lag=1800.0, at_head=False,
             files={"parsed": 100, "reused": 95, "recomputed": 5}),
        _run(3, outcome="superseded", lag=99999.0, at_head=False),
    ))
    assert figures["lag_seconds"]["n"] == 2 and figures["lag_seconds"]["max"] == 1800.0
    assert figures["promoted"] == 2 and figures["promoted_at_head"] == 1
    # The superseded run said nothing about its files: the totals are of the
    # runs that did, and the one that did not is counted apart.
    assert figures["files"] == {"runs_known": 2, "runs_unknown": 1, "parsed": 200,
                                "reused": 185, "recomputed": 15}
    unknown = _report(_doc(_run(1, files={"parsed": 1, "reused": None, "recomputed": 1})))
    # MUTATION: summing a None as 0.
    assert unknown["files"]["reused"] is None


def test_the_window_and_a_cut_by_run_retention():
    inside = [_run(n, hours_ago=1 + n) for n in range(canary.KEPT_RUNS)]
    figures = _report(_doc(*inside), hours=168)
    assert figures["runs"] == canary.KEPT_RUNS
    # MUTATION: a report over a cut window that does not say so.
    assert [row["repo_id"] for row in figures["cut_by_run_retention"]] == ["repo_a"]
    # A response that reaches back past the window's start covers all of it.
    covered = _report(_doc(*inside[:-1], _run(999, hours_ago=200)), hours=168)
    assert covered["cut_by_run_retention"] == []
    whole = _report(_doc(_run(1), _run(2, hours_ago=500)))
    assert whole["runs"] == 1 and whole["cut_by_run_retention"] == []
    # The same run read twice is one run.
    assert _report(_doc(_run(1)), _doc(_run(1)))["runs"] == 1


def _main(stdin: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), "--now", NOW.isoformat(), *args],
                          input=stdin, capture_output=True, text=True, timeout=60)


def test_reading_nothing_is_not_a_result():
    assert _main("").returncode == 2
    assert _main(json.dumps(_doc())).returncode == 2
    old = _main(json.dumps(_doc(_run(1, hours_ago=500))))
    assert old.returncode == 2 and "none in the window" in old.stderr
    bad = _main("{\"runs\": 3}")
    assert bad.returncode == 2 and "runs" in bad.stderr


def test_the_printed_report():
    stdin = json.dumps(_doc(_run(1, built="full", requested="full", reason="weekly"),
                            _run(2))) + "\n" + json.dumps(_doc(_run(3), repo_id="repo_b"))
    done = _main(stdin)
    assert done.returncode == 0, done.stderr
    out = done.stdout
    assert "runs in window: 3 of 3 read" in out
    assert "built: full 1, incremental 2 (incremental 67%)" in out
    assert "     1  weekly" in out
    assert any(line.startswith("extraction") and " 3 " in line for line in out.splitlines())
    as_json = json.loads(_main(stdin, "--json").stdout)
    assert as_json["built"] == {"full": 1, "incremental": 2}


def test_it_reads_what_swarm_api_measures():
    from swarm_api import repoindex

    run = {"task_id": "task_1", "kind": "incremental", "queued_at": NOW - timedelta(hours=2),
           "state": "SUCCEEDED", "ended_at": NOW - timedelta(hours=1),
           "promotion": {"outcome": "promoted"}}
    run["metrics"] = repoindex.run_metrics(
        {"repo_index_phases": [{"phase": "extract", "status": "ok", "seconds": 120.0},
                               {"phase": "agent", "status": "ok", "seconds": 60.0}]},
        run, built_kind="incremental",
        manifest={"extractor": {"work": {"parsed": 10, "recomputed": 2, "reused": 8}}},
        ended_at=NOW - timedelta(hours=1, minutes=1), promoted_at=NOW - timedelta(hours=1),
        at_head=True)
    served = json.loads(json.dumps({"runs": [repoindex.run_to_api(run)]}, default=str))

    figures = _report(served)

    assert set(canary.PHASES) == set(repoindex.METRIC_PHASE_NAMES)
    assert figures["built"] == {"full": 0, "incremental": 1}
    assert figures["phases"]["extraction"]["p50"] == 120.0
    assert figures["phases"]["judgment"]["p50"] == 60.0
    assert figures["phases"]["promotion"]["p50"] == 60.0
    assert figures["files"]["reused"] == 8
    assert figures["lag_seconds"]["p50"] == 3600.0


@pytest.mark.parametrize("value", [None, "", "yesterday"])
def test_an_unreadable_time_is_none(value):
    assert canary.parse_time(value) is None
