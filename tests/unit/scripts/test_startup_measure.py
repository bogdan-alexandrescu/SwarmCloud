"""The start-up measurement's arithmetic and its verdict, offline (START-MEASURE).

`scripts/startup-measure.sh` reads tasks' `startup_progress` events through the
API and samples the browser pods' CPU and memory with `kubectl top` on
`swarm-autopilot`; `scripts/startup_measure.py` turns both into the table and
the verdict that decide two owner questions of 2026-10-13
(docs/browser-startup-speed.md §9 decisions 3 and 6): are warm slots worth an
amendment to invariant 1, and would a 4 vCPU / 8 GiB browser class fit.

So the rules most worth pinning are the ones that would let it answer either
question with a number it did not measure:

  * each segment is the documented pair of steps of ONE attempt at ONE
    generation, at each step's earliest time -- a fenced worker's late write
    or a second attempt's pod is not this start-up (invariant 5);
  * a start is cold only when its node wait is OVER the threshold, and a
    start whose pod never got scheduled is neither cold nor warm;
  * percentiles are nearest-rank (benchstat's), so every number printed is
    a start that happened;
  * overlapping pages are not double-counted, and the window is the
    attempt's `dispatched` time;
  * reading no start-up exits 2, and sampling no pod is NOT MEASURED, never
    "fits" (CLAUDE.md, rule zero);
  * the candidate class and the measured class are read from the frozen
    catalogue, never restated here or in the helper.

WHAT THIS CANNOT PROVE: that the API and `kubectl top` answer in exactly these
shapes in the deployed environment. The event shape is `_event_to_api` in
apps/swarm-api/swarm_api/routes/tasks.py; the `kubectl top pods -A
--containers --no-headers` columns are kubectl's. Only the operator's run
against `swarm-autopilot` exercises them.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from swarm_common.profiles import RESOURCE_CLASSES

ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "scripts" / "startup_measure.py"
SCRIPT = ROOT / "scripts" / "startup-measure.sh"
DOC = ROOT / "docs" / "browser-startup-speed.md"

NOW = "2026-10-13T12:00:00+00:00"
DAY = "2026-10-13"
PROFILE = "claude-code-browser"
GIB = 1024**3


def _load():
    spec = importlib.util.spec_from_file_location("startup_measure", HELPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["startup_measure"] = module
    spec.loader.exec_module(module)
    return module


sm = _load()


# ---------------------------------------------------------------------------
# Fixtures: API response bodies exactly as `_event_to_api` serves them.


def _at(seconds: float, *, hour: int = 11) -> str:
    """A UTC timestamp `seconds` after 11:20:00 on the measured day."""
    millis = round(seconds * 1000)
    whole, frac = divmod(millis, 1000)
    minutes, sec = divmod(20 * 60 + whole, 60)
    hh, mm = divmod(minutes, 60)
    return f"{DAY}T{hour + hh:02d}:{mm:02d}:{sec:02d}.{frac:03d}+00:00"


def event(task_id, attempt_id, kind, at, *, step=None, generation=1):
    detail = {"step": step} if step else {}
    return {
        "event_id": f"evt_{secrets.token_hex(6)}",
        "task_id": task_id,
        "type": kind,
        "at": at,
        "attempt_id": attempt_id,
        "lease_id": f"lease_{attempt_id}",
        "generation": generation,
        "detail": detail,
        "detail_redaction_count": 0,
    }


def startup(task_id, attempt_id, offsets, *, base=0.0, generation=1, hour=11):
    """One attempt's `dispatched` plus the given steps, at `base + offset` seconds."""
    events = [event(task_id, attempt_id, "dispatched", _at(base, hour=hour), generation=generation)]
    for step, offset in offsets.items():
        events.append(
            event(
                task_id,
                attempt_id,
                "startup_progress",
                _at(base + offset, hour=hour),
                step=step,
                generation=generation,
            )
        )
    return events


#: The start docs/browser-startup-speed.md §1 measured: 38 s node wait, 42 s
#: pull, ~88 s in all.
MEASURED = {
    "pod_scheduled": 38.2,
    "image_pulling": 38.2,
    "image_pulled": 80.2,
    "container_started": 87.9,
    "browser_launching": 81.9,
    "browser_ready": 84.2,
    "first_frame": 88.2,
}

WARM = {
    "pod_scheduled": 2.0,
    "image_pulling": 2.0,
    "image_pulled": 4.0,
    "container_started": 9.0,
    "browser_launching": 10.0,
    "browser_ready": 12.0,
    "first_frame": 14.0,
}


def tasks_page(*rows, token=None):
    return {
        "tasks": [
            {
                "id": task_id,
                "runner_profile": profile,
                "resource_class": rc,
                "created_at": f"{DAY}T11:00:00+00:00",
                "state": "SUCCEEDED",
            }
            for task_id, profile, rc in rows
        ],
        "next_page_token": token,
    }


def events_page(task_id, events, token=None):
    return {"task_id": task_id, "events": events, "next_page_token": token}


def report(docs, top_text=None, **kwargs):
    kwargs.setdefault("since", "7d")
    kwargs.setdefault("now", NOW)
    return sm.build_report(docs, top_text=top_text, **kwargs)


def row(result, profile=PROFILE, rc="browser", start="all"):
    for r in result["rows"]:
        if r["runner_profile"] == profile and r["resource_class"] == rc and r["start"] == start:
            return r
    raise AssertionError(f"no row {profile}/{rc}/{start} in {result['rows']}")


# ---------------------------------------------------------------------------
# The segments.


def test_the_measured_start_decomposes_into_the_documented_segments():
    docs = [
        tasks_page(("task_a", PROFILE, "browser")),
        events_page("task_a", startup("task_a", "att_a", MEASURED)),
    ]
    r = row(report(docs))
    assert r["n"] == 1
    assert r["segments"]["node_wait"]["p50"] == pytest.approx(38.2)
    assert r["segments"]["pull"]["p50"] == pytest.approx(42.0)
    # container_started -> first_frame: the browser's own time after the worker started.
    assert r["segments"]["browser"]["p50"] == pytest.approx(0.3)
    assert r["segments"]["total"]["p50"] == pytest.approx(88.2)


def test_a_start_is_cold_only_over_the_threshold_and_a_never_scheduled_pod_is_neither():
    exactly = dict(WARM, pod_scheduled=10.0, image_pulling=10.0)
    unscheduled = {}  # dispatched, then a pod that was never bound
    docs = [
        tasks_page(
            ("task_cold", PROFILE, "browser"),
            ("task_edge", PROFILE, "browser"),
            ("task_warm", PROFILE, "browser"),
            ("task_none", PROFILE, "browser"),
        ),
        events_page("task_cold", startup("task_cold", "att_c", MEASURED)),
        events_page("task_edge", startup("task_edge", "att_e", exactly)),
        events_page("task_warm", startup("task_warm", "att_w", WARM)),
        events_page(
            "task_none",
            startup("task_none", "att_n", unscheduled)
            + [event("task_none", "att_n", "startup_progress", _at(5), step="image_pulling")],
        ),
    ]
    result = report(docs)
    everything = row(result)
    assert everything["n"] == 4
    assert everything["cold"] == 1
    assert everything["classified"] == 3
    assert everything["cold_share"] == pytest.approx(1 / 3)
    assert row(result, start="cold")["n"] == 1
    # Exactly 10 s is not "over 10 s".
    assert row(result, start="warm")["n"] == 2


def test_the_threshold_is_an_option():
    docs = [
        tasks_page(("task_w", PROFILE, "browser")),
        events_page("task_w", startup("task_w", "att_w", WARM)),
    ]
    assert row(report(docs, cold_seconds=1.0))["cold"] == 1
    assert row(report(docs))["cold"] == 0


def test_percentiles_are_nearest_rank_over_the_starts_that_reached_both_steps():
    events = []
    rows = []
    for index, wait in enumerate((1, 2, 3, 4, 5, 6, 7, 8, 9, 40)):
        task_id = f"task_{index}"
        rows.append((task_id, PROFILE, "browser"))
        events.append(events_page(task_id, startup(task_id, f"att_{index}", {"pod_scheduled": float(wait)})))
    result = report([tasks_page(*rows), *events])
    node = row(result)["segments"]["node_wait"]
    assert (node["n"], node["p50"], node["p95"]) == (10, 5.0, 40.0)
    # No start reached the pull: no number, never a zero.
    assert row(result)["segments"]["pull"] == {"n": 0, "p50": None, "p95": None}


def test_only_the_dispatching_generation_counts_and_each_step_at_its_earliest_time():
    events = startup("task_g", "att_g", MEASURED, generation=3)
    # A fenced worker's late write under an older generation, and a re-read
    # of the same step later: neither moves the start.
    events.append(event("task_g", "att_g", "startup_progress", _at(1.0), step="pod_scheduled", generation=2))
    events.append(event("task_g", "att_g", "startup_progress", _at(200.0), step="first_frame", generation=3))
    events.append(event("task_g", "att_g", "startup_progress", _at(85.0), step="first_frame", generation=3))
    docs = [tasks_page(("task_g", PROFILE, "browser")), events_page("task_g", events)]
    r = row(report(docs))
    assert r["segments"]["node_wait"]["p50"] == pytest.approx(38.2)
    assert r["segments"]["total"]["p50"] == pytest.approx(85.0)


def test_each_attempt_is_its_own_start():
    events = startup("task_r", "att_1", MEASURED) + startup("task_r", "att_2", WARM, base=600.0)
    docs = [tasks_page(("task_r", PROFILE, "browser")), events_page("task_r", events)]
    result = report(docs)
    assert row(result)["n"] == 2
    assert row(result, start="cold")["n"] == 1
    assert row(result, start="warm")["segments"]["node_wait"]["p50"] == pytest.approx(2.0)


def test_overlapping_pages_are_not_counted_twice():
    events = startup("task_o", "att_o", MEASURED)
    docs = [
        tasks_page(("task_o", PROFILE, "browser")),
        events_page("task_o", events),
        events_page("task_o", events),
    ]
    assert row(report(docs))["n"] == 1


def test_the_window_is_the_attempts_dispatched_time():
    old = startup("task_old", "att_old", MEASURED, hour=9)
    docs = [
        tasks_page(("task_old", PROFILE, "browser"), ("task_new", PROFILE, "browser")),
        events_page("task_old", old),
        events_page("task_new", startup("task_new", "att_new", WARM)),
    ]
    result = report(docs, since="2h")
    assert row(result)["n"] == 1
    assert result["visited"]["outside_window"] == 1


def test_starts_are_grouped_by_profile_and_class_and_an_unlisted_task_is_unknown():
    docs = [
        tasks_page(("task_b", PROFILE, "browser"), ("task_s", "claude-code", "standard")),
        events_page("task_b", startup("task_b", "att_b", MEASURED)),
        events_page("task_s", startup("task_s", "att_s", WARM)),
        events_page("task_x", startup("task_x", "att_x", WARM)),
    ]
    result = report(docs)
    assert row(result)["n"] == 1
    assert row(result, profile="claude-code", rc="standard")["n"] == 1
    assert row(result, profile="unknown", rc="unknown")["n"] == 1


def test_an_attempt_with_no_startup_step_is_counted_as_visited_not_as_a_start():
    # A Cloud Run attempt: dispatched, starting, running, and no pod steps.
    events = [
        event("task_cr", "att_cr", "dispatched", _at(0)),
        event("task_cr", "att_cr", "starting", _at(30)),
        event("task_cr", "att_cr", "running", _at(31)),
    ]
    docs = [
        tasks_page(("task_cr", "claude-code", "standard"), ("task_b", PROFILE, "browser")),
        events_page("task_cr", events),
        events_page("task_b", startup("task_b", "att_b", MEASURED)),
    ]
    result = report(docs)
    assert result["visited"]["attempts_without_steps"] == 1
    assert all(r["runner_profile"] != "claude-code" for r in result["rows"])


# ---------------------------------------------------------------------------
# The pods' real use, from `kubectl top pods -A --containers --no-headers`.


def top(*samples):
    """One `# sample` marker per sample, then kubectl's five columns per container."""
    lines = []
    for index, containers in enumerate(samples):
        lines.append(f"# sample {DAY}T12:0{index}:00Z")
        for pod, name, cpu, memory in containers:
            lines.append(f"swarm-tenant-eng   {pod}   {name}   {cpu}   {memory}")
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize(
    "text, cores",
    [("250m", 0.25), ("2", 2.0), ("1500000000n", 1.5), ("750000u", 0.75)],
)
def test_cpu_quantities(text, cores):
    assert sm.parse_cpu(text) == pytest.approx(cores)


@pytest.mark.parametrize(
    "text, size",
    [("512Mi", 512 * 1024**2), ("2Gi", 2 * GIB), ("1000Ki", 1024000), ("1G", 10**9), ("4096", 4096)],
)
def test_memory_quantities(text, size):
    assert sm.parse_memory(text) == size


def test_a_pods_use_is_the_sum_of_its_containers_in_one_sample_and_its_peak_across_samples():
    text = top(
        [("p1", "display", "1200m", "2Gi"), ("p1", "worker", "800m", "1Gi")],
        [("p1", "display", "2500m", "3Gi"), ("p1", "worker", "500m", "1536Mi")],
    )
    usage = sm.pod_usage(text)
    assert usage["samples"] == 2
    assert usage["pods"] == 1
    assert usage["peak_cpu"] == pytest.approx(3.0)
    assert usage["peak_memory_bytes"] == int(4.5 * GIB)
    assert usage["containers"]["display"]["peak_cpu"] == pytest.approx(2.5)
    assert usage["containers"]["worker"]["peak_memory_bytes"] == int(1.5 * GIB)


def _sized(text):
    docs = [
        tasks_page(("task_b", PROFILE, "browser")),
        events_page("task_b", startup("task_b", "att_b", MEASURED)),
    ]
    return report(docs, top_text=text)


def test_the_candidate_and_the_measured_class_come_from_the_frozen_catalogue():
    sizing = _sized(top([("p1", "display", "1", "2Gi")]))["sizing"]
    candidate = RESOURCE_CLASSES["standard"]
    current = RESOURCE_CLASSES["browser"]
    assert (sizing["candidate"]["cpu"], sizing["candidate"]["memory_gib"]) == (candidate.cpu, candidate.memory_gib)
    assert (sizing["current"]["cpu"], sizing["current"]["memory_gib"]) == (current.cpu, current.memory_gib)


def test_a_pod_well_inside_four_by_eight_fits():
    sizing = _sized(top([("p1", "display", "1500m", "3Gi"), ("p1", "worker", "500m", "1Gi")]))["sizing"]
    assert sizing["verdict"] == "FITS"


def test_memory_over_the_headroom_does_not_fit_even_when_cpu_does():
    # 7 GiB of an 8 GiB limit: requests == limits, so there is no burst, and
    # an overshoot between two samples is an OOM kill.
    sizing = _sized(top([("p1", "display", "1", "5Gi"), ("p1", "worker", "500m", "2Gi")]))["sizing"]
    assert sizing["verdict"] == "DOES_NOT_FIT"
    assert "memory" in sizing["reason"]


def test_cpu_over_the_headroom_does_not_fit():
    sizing = _sized(top([("p1", "display", "3500m", "2Gi"), ("p1", "worker", "500m", "1Gi")]))["sizing"]
    assert sizing["verdict"] == "DOES_NOT_FIT"
    assert "cpu" in sizing["reason"].lower()


def test_no_pod_sampled_is_not_measured_never_fits():
    sizing = _sized(top([], []))["sizing"]
    assert sizing["verdict"] == "NOT_MEASURED"
    assert sizing["pods"] == 0


# ---------------------------------------------------------------------------
# The command line.


def _main(argv, stdin, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(stdin))
    code = sm.main(argv)
    return code, capsys.readouterr()


def _stream(*docs):
    return "\n".join(json.dumps(d) for d in docs) + "\n"


def test_reading_no_startup_step_exits_2(monkeypatch, capsys):
    stream = _stream(tasks_page(("task_a", PROFILE, "browser")), events_page("task_a", []))
    code, out = _main(["report", "--now", NOW], stream, monkeypatch, capsys)
    assert code == 2
    assert "no startup_progress" in out.err


def test_the_report_prints_a_table_and_a_verdict(monkeypatch, capsys, tmp_path):
    stream = _stream(
        tasks_page(("task_a", PROFILE, "browser"), ("task_w", PROFILE, "browser")),
        events_page("task_a", startup("task_a", "att_a", MEASURED)),
        events_page("task_w", startup("task_w", "att_w", WARM)),
    )
    top_file = tmp_path / "top.txt"
    top_file.write_text(top([("p1", "display", "1500m", "3Gi"), ("p1", "worker", "500m", "1Gi")]))
    code, out = _main(["report", "--now", NOW, "--top", str(top_file)], stream, monkeypatch, capsys)
    assert code == 0, out.err
    assert re.search(r"profile\s+class\s+start\s+n", out.out)
    assert re.search(rf"{PROFILE}\s+browser\s+all\s+2\s+1/2 \(50%\)", out.out)
    assert "warm slots:" in out.out
    assert "4 vCPU / 8 GiB" in out.out
    assert "FITS" in out.out


def test_a_report_without_a_pod_sample_exits_1(monkeypatch, capsys, tmp_path):
    stream = _stream(
        tasks_page(("task_a", PROFILE, "browser")),
        events_page("task_a", startup("task_a", "att_a", MEASURED)),
    )
    top_file = tmp_path / "top.txt"
    top_file.write_text(top([]))
    code, out = _main(["report", "--now", NOW, "--top", str(top_file)], stream, monkeypatch, capsys)
    assert code == 1
    assert "NOT_MEASURED" in out.out


def test_the_json_report_is_json(monkeypatch, capsys):
    stream = _stream(
        tasks_page(("task_a", PROFILE, "browser")),
        events_page("task_a", startup("task_a", "att_a", MEASURED)),
    )
    code, out = _main(["report", "--now", NOW, "--json"], stream, monkeypatch, capsys)
    assert code == 0
    assert json.loads(out.out)["rows"][0]["n"] == 1


def test_the_window_walk_stops_at_the_first_older_task_and_passes_the_cursor():
    token = "tok/" + secrets.token_hex(4)
    page = tasks_page(("task_a", PROFILE, "browser"), token=token)
    assert sm.window_lines(page, since="7d", now=NOW) == ["task task_a", f"next {token.replace('/', '%2F')}"]
    page["tasks"].append({"id": "task_old", "created_at": "2026-09-01T00:00:00+00:00"})
    assert sm.window_lines(page, since="7d", now=NOW) == ["task task_a", "older"]


# ---------------------------------------------------------------------------
# The shell script: only reads, only through the repository's own guards.


def _effective_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def test_the_script_is_executable_and_strict():
    assert SCRIPT.exists()
    assert os.access(SCRIPT, os.X_OK)
    text = SCRIPT.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    assert _effective_lines(text)[0] == "set -euo pipefail"
    assert 'lib/common.sh"' in text


def test_the_script_reaches_kubectl_only_through_the_guard_and_only_to_read():
    code = "\n".join(_effective_lines(SCRIPT.read_text()))
    # Never a bare kubectl: the old clients on PATH drop fields, and the
    # context check is what keeps it off agents-staging.
    assert not re.search(r"(^|[\s;|(])kubectl\s", code)
    assert "assert_kube_context" in code
    assert code.index("assert_kube_context") < code.index("kc ")
    verbs = set(re.findall(r"\bkc\s+(?:--context\s+\S+\s+)?([a-z-]+)", code))
    assert verbs == {"top"}, verbs
    # Only GETs against the API.
    assert set(re.findall(r"api_request\s+([A-Z]+)", code)) == {"GET"}


def test_the_doc_says_how_to_measure():
    text = DOC.read_text()
    assert "scripts/startup-measure.sh" in text


@pytest.mark.skipif(not os.path.exists("/bin/bash"), reason="needs bash")
def test_the_script_answers_help_without_credentials():
    done = subprocess.run(
        ["bash", str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "SWARM_ENV_FILE": "/nonexistent/no.env"},
    )
    assert done.returncode == 0, done.stderr
    assert "startup_progress" in done.stdout
