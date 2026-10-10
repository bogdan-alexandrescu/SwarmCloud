"""The egress-ready report's arithmetic and its verdict, offline (#939).

`scripts/egress_ready_report.py` reads the workers' `egress_ready` and
`clone_timed` startup marks, as the API serves them, and says how long a new
worker waited for its path to the forge, per backend. The issue's acceptance
bar is read off the same tool: Cloud Run p50 under 5 s over at least 30 steps.
So the rules most worth pinning are the ones that would let it report a pass
it did not measure: a step that never connected must not vanish from the
count, too few steps must not pass, an unknown backend must not be dropped,
and reading nothing must not exit zero.
"""

from __future__ import annotations

import importlib.util
import json
import re
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from swarm_common.profiles import Backend

ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "scripts" / "egress_ready_report.py"
SCRIPT = ROOT / "scripts" / "egress-ready-report.sh"

CLOUD_RUN = Backend.CLOUD_RUN_JOB.value
GKE = Backend.GKE_AUTOPILOT.value
NOW = "2026-10-09T12:00:00+00:00"
RECENT = "2026-10-08T12:00:00+00:00"


def _load():
    spec = importlib.util.spec_from_file_location("egress_ready_report", HELPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


err = _load()


def _event(task_id, attempt_id, cause, detail, *, at=RECENT):
    return {
        "event_id": f"evt_{secrets.token_hex(6)}",
        "task_id": task_id,
        "type": "RUNNING",
        "at": at,
        "attempt_id": attempt_id,
        "detail": {"cause": cause, **detail},
    }


def egress_mark(task_id, attempt_id, seconds, *, ended="ready", at=RECENT):
    return _event(
        task_id,
        attempt_id,
        "egress_ready",
        {
            "egress": {
                "egress_ready_seconds": seconds,
                "probe_attempts": 3,
                "ended": ended if seconds is not None else "cap",
                "cap_seconds": 120.0,
                "targets": [
                    {
                        "host": "github.com",
                        "port": 443,
                        "ready": seconds is not None,
                        "egress_ready_seconds": seconds,
                        "probe_attempts": 3,
                        "peer": "140.82.112.3",
                    }
                ],
            }
        },
        at=at,
    )


def clone_mark(task_id, attempt_id, seconds):
    return _event(
        task_id, attempt_id, "clone_timed", {"clone": {"seconds": seconds, "ok": True}}
    )


def task_docs(backend, egress_seconds, *, clone_seconds=None, prefix="tsk"):
    """One events page and one attempts page per task, as the API serves them."""
    docs = []
    for i, seconds in enumerate(egress_seconds):
        task_id = f"{prefix}_{backend.lower()}_{i}"
        attempt_id = f"att_{task_id}"
        events = [egress_mark(task_id, attempt_id, seconds)]
        if clone_seconds is not None:
            events.append(clone_mark(task_id, attempt_id, clone_seconds[i]))
        docs.append({"task_id": task_id, "events": events, "next_page_token": None})
        docs.append(
            {
                "task_id": task_id,
                "attempts": [{"attempt_id": attempt_id, "backend": backend}],
            }
        )
    return docs


def stream(docs):
    return "\n".join(json.dumps(d) for d in docs)


def report(docs, **kwargs):
    kwargs.setdefault("now", NOW)
    return err.build_report(err.read_documents(stream(docs)), **kwargs)


def row(result, backend):
    return next(r for r in result["rows"] if r["backend"] == backend)


# ---------------------------------------------------------------------------
# The arithmetic.


def test_percentiles_over_a_fixture_are_nearest_rank():
    seconds = [float(s) for s in range(1, 11)]  # 1..10
    result = report(task_docs(CLOUD_RUN, seconds, clone_seconds=[s + 2 for s in seconds]))
    cr = row(result, CLOUD_RUN)
    assert cr["n"] == 10
    assert cr["n_none"] == 0
    assert cr["p50"] == 5.0
    assert cr["p90"] == 9.0
    assert cr["max"] == 10.0
    assert cr["clone_p50"] == 7.0


def test_a_none_counts_in_n_none_and_not_in_the_percentiles():
    result = report(task_docs(CLOUD_RUN, [1.0, 2.0, 3.0, None, None]))
    cr = row(result, CLOUD_RUN)
    assert cr["n"] == 5
    assert cr["n_none"] == 2
    # Over the three that connected, not five with two zeros or two caps.
    assert cr["p50"] == 2.0
    assert cr["max"] == 3.0


def test_gke_and_cloud_run_are_kept_apart():
    docs = task_docs(CLOUD_RUN, [40.0, 70.0, 36.0]) + task_docs(GKE, [0.5, 0.7])
    result = report(docs)
    assert row(result, CLOUD_RUN)["n"] == 3
    assert row(result, CLOUD_RUN)["p50"] == 40.0
    assert row(result, GKE)["n"] == 2
    assert row(result, GKE)["max"] == 0.7


def test_the_apis_lowercase_event_type_is_read():
    # GET /v1/tasks/{id}/events serves "type": "running" (lowercase); a report
    # that only matched "RUNNING" read zero of 167 live marks on 2026-10-09.
    docs = task_docs(CLOUD_RUN, [4.0, 6.0])
    for doc in docs:
        for event in doc.get("events", []):
            event["type"] = "running"
    result = report(docs)
    assert row(result, CLOUD_RUN)["n"] == 2
    assert row(result, CLOUD_RUN)["max"] == 6.0


def test_an_unknown_backend_is_counted_under_unknown_not_dropped():
    docs = task_docs("SOMETHING_NEW", [3.0])
    # And a mark whose attempt the attempts page never listed.
    orphan = egress_mark("tsk_orphan", "att_missing", 4.0)
    docs.append({"task_id": "tsk_orphan", "events": [orphan]})
    result = report(docs)
    unknown = row(result, "unknown")
    assert unknown["n"] == 2
    assert result["visited"]["egress_ready_marks"] == 2


def test_a_mark_older_than_since_is_not_read():
    old = egress_mark("tsk_old", "att_old", 1.0, at="2026-09-01T00:00:00+00:00")
    docs = task_docs(CLOUD_RUN, [2.0]) + [
        {"task_id": "tsk_old", "events": [old]},
        {"task_id": "tsk_old", "attempts": [{"attempt_id": "att_old", "backend": CLOUD_RUN}]},
    ]
    result = report(docs, since="7d")
    assert row(result, CLOUD_RUN)["n"] == 1


def test_an_event_repeated_across_pages_is_read_once():
    docs = task_docs(CLOUD_RUN, [2.0])
    docs.append(docs[0])  # the same events page, served twice
    assert row(report(docs), CLOUD_RUN)["n"] == 1


def test_masked_token_shaped_text_in_a_detail_is_not_read_as_a_number():
    # The API masks credential-shaped strings; a detail that carries one must
    # still parse, and the string must never count as a time.
    docs = task_docs(CLOUD_RUN, [2.0])
    fake = "ghp_" + secrets.token_hex(18)
    docs[0]["events"][0]["detail"]["egress"]["targets"][0]["last_error"] = fake
    assert row(report(docs), CLOUD_RUN)["p50"] == 2.0


# ---------------------------------------------------------------------------
# The verdict.


def test_cloud_run_under_five_seconds_over_thirty_steps_passes():
    result = report(task_docs(CLOUD_RUN, [1.0] * 30))
    assert result["verdict"]["result"] == "PASS"


def test_fewer_than_thirty_cloud_run_marks_is_insufficient_n_not_pass():
    result = report(task_docs(CLOUD_RUN, [1.0] * 29))
    assert result["verdict"]["result"] == "FAIL"
    assert "insufficient n" in result["verdict"]["reason"]


def test_a_slow_cloud_run_p50_fails():
    result = report(task_docs(CLOUD_RUN, [36.0] * 30))
    assert result["verdict"]["result"] == "FAIL"
    assert "insufficient n" not in result["verdict"]["reason"]


def test_fast_gke_does_not_pass_the_cloud_run_bar():
    docs = task_docs(GKE, [0.5] * 40) + task_docs(CLOUD_RUN, [40.0] * 30)
    assert report(docs)["verdict"]["result"] == "FAIL"


def test_steps_that_never_connected_cannot_carry_a_pass():
    # 16 fast, 14 never connected: the connected ones' p50 is 1 s, but over
    # the 30 steps the median step did not have its path within 5 s.
    result = report(task_docs(CLOUD_RUN, [1.0] * 14 + [None] * 16))
    assert result["verdict"]["result"] == "FAIL"


def test_cli_exit_codes_follow_the_verdict(tmp_path):
    passing = stream(task_docs(CLOUD_RUN, [1.0] * 30))
    failing = stream(task_docs(CLOUD_RUN, [1.0] * 3))
    run = lambda text: subprocess.run(  # noqa: E731
        [sys.executable, str(HELPER), "report", "--now", NOW],
        input=text, capture_output=True, text=True, check=False,
    )
    ok = run(passing)
    assert ok.returncode == 0, ok.stderr
    assert "PASS" in ok.stdout
    bad = run(failing)
    assert bad.returncode == 1
    assert "FAIL" in bad.stdout and "insufficient n" in bad.stdout
    assert "visited: 3 task(s), 3 egress_ready mark(s)" in bad.stdout


def test_json_output_is_one_document():
    proc = subprocess.run(
        [sys.executable, str(HELPER), "report", "--now", NOW, "--json"],
        input=stream(task_docs(CLOUD_RUN, [1.0] * 30)),
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["verdict"]["result"] == "PASS"
    assert doc["rows"][0]["backend"] == CLOUD_RUN


# ---------------------------------------------------------------------------
# Reading nothing is not a result.


EMPTY_INPUTS = {
    "nothing": lambda: "",
    "no-marks": lambda: stream([{"task_id": "tsk_x", "events": [], "next_page_token": None}]),
    # A mark that --backend cloud-run filters out below.
    "filtered-out": lambda: stream(task_docs(GKE, [1.0])),
}


@pytest.mark.parametrize("case", sorted(EMPTY_INPUTS))
def test_empty_input_exits_non_zero_and_never_passes(case):
    text = EMPTY_INPUTS[case]()
    proc = subprocess.run(
        [sys.executable, str(HELPER), "report", "--now", NOW, "--backend", "cloud-run"],
        input=text, capture_output=True, text=True, check=False,
    )
    assert proc.returncode not in (0, 1)
    assert "no egress_ready marks read" in proc.stderr
    assert "PASS" not in proc.stdout


# ---------------------------------------------------------------------------
# Paging helpers the shell script uses.


def test_window_lists_recent_tasks_and_says_when_older_ones_begin():
    page = {
        "tasks": [
            {"id": "tsk_new", "created_at": RECENT},
            {"id": "tsk_old", "created_at": "2026-09-01T00:00:00+00:00"},
        ],
        "next_page_token": "a b/c",
    }
    lines = err.window_lines(page, since="7d", now=NOW)
    assert lines == ["task tsk_new", "older"]
    page["tasks"].pop()
    assert err.window_lines(page, since="7d", now=NOW) == ["task tsk_new", "next a%20b%2Fc"]


def test_duration_parsing():
    assert err.parse_duration("7d") == 7 * 86400
    assert err.parse_duration("36h") == 36 * 3600
    with pytest.raises(ValueError):
        err.parse_duration("seven days")


# ---------------------------------------------------------------------------
# The shell script's shape.


def _effective_lines(text):
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        yield stripped


def test_script_carries_set_euo_pipefail_first_and_sources_common():
    text = SCRIPT.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    assert next(_effective_lines(text)) == "set -euo pipefail"
    assert re.search(r"^source .*lib/common\.sh\"?$", text, re.MULTILINE)
    assert SCRIPT.stat().st_mode & 0o111, "the script must be executable"
    assert "| redact" in text


def test_script_makes_no_mutating_request():
    text = SCRIPT.read_text()
    for verb in ("POST", "PUT", "PATCH", "DELETE"):
        assert not re.search(rf"\b{verb}\b", text), verb
    assert "api_post" not in text
    assert "fs_" not in text, "read through the API, never Firestore directly"


def test_script_avoids_bash4_only_constructs():
    text = SCRIPT.read_text()
    assert "mapfile" not in text and "readarray" not in text
    assert "declare -A" not in text
    assert ",,}" not in text and "^^}" not in text


# ---------------------------------------------------------------------------
# Clone bundles (#940): the same marks, read by `clone.source`.


def bundle_mark(task_id, source, seconds, *, ok=True, hit=None, miss_reason=None, at=RECENT):
    """A `clone_timed` mark in the shape `Worker._mark_clone_timed` writes since #940."""
    bundle = {"hit": source != "forge" if hit is None else hit, "miss_reason": miss_reason}
    clone = {"seconds": seconds, "total_seconds": seconds, "ok": ok, "source": source, "bundle": bundle}
    return _event(task_id, f"att_{task_id}", "clone_timed", {"clone": clone}, at=at)


def bundle_docs(*marks):
    return [{"task_id": m["task_id"], "events": [m], "next_page_token": None} for m in marks]


def bundled(n, seconds, *, prefix="b"):
    return [bundle_mark(f"tsk_{prefix}{i}", "bundle", seconds) for i in range(n)]


def fallback(n=1, *, ok=True, reason="miss"):
    return [
        bundle_mark(f"tsk_f{i}", "forge", 40.0, ok=ok, hit=False, miss_reason=reason) for i in range(n)
    ]


def bundle_report(marks, **kwargs):
    kwargs.setdefault("now", NOW)
    return err.build_bundle_report(err.read_documents(stream(bundle_docs(*marks))), **kwargs)


def test_thirty_fast_bundle_clones_with_a_working_fallback_pass():
    result = bundle_report(bundled(30, 2.0) + fallback(2))
    assert result["verdict"]["result"] == "PASS", result["verdict"]
    bundle = next(r for r in result["rows"] if r["source"] == "bundle")
    assert (bundle["n"], bundle["p50"]) == (30, 2.0)
    assert result["fallback"] == {"n": 2, "n_ok": 2}


def test_fewer_than_thirty_bundle_clones_is_insufficient_n_not_pass():
    verdict = bundle_report(bundled(29, 1.0) + fallback())["verdict"]
    assert verdict["result"] == "FAIL" and "insufficient n (29" in verdict["reason"]


def test_a_slow_bundle_p50_fails_and_other_sources_do_not_count_toward_it():
    marks = bundled(30, 6.0) + [bundle_mark(f"tsk_d{i}", "bundle+delta", 1.0) for i in range(40)]
    verdict = bundle_report(marks + fallback())["verdict"]
    assert verdict["result"] == "FAIL" and "bundle p50 6.00 s" in verdict["reason"]


def test_bundle_clones_that_did_not_land_cannot_carry_a_pass():
    marks = bundled(14, 1.0) + [bundle_mark(f"tsk_x{i}", "bundle", 0.5, ok=False) for i in range(16)]
    result = bundle_report(marks + fallback())
    assert result["verdict"]["result"] == "FAIL", result["verdict"]
    assert next(r for r in result["rows"] if r["source"] == "bundle")["n_failed"] == 16


def test_a_fallback_that_never_ran_is_not_one_that_works():
    """A forge clone with no bundle lookup (the kill switch: `disabled`) is not the fallback."""
    disabled = fallback(3, reason="disabled")
    verdict = bundle_report(bundled(30, 1.0) + disabled)["verdict"]
    assert verdict["result"] == "FAIL" and "not exercised" in verdict["reason"]


@pytest.mark.parametrize("reason", ["miss", "no_key", "bundle_error"])
def test_a_fallback_clone_that_did_not_land_fails(reason):
    verdict = bundle_report(bundled(30, 1.0) + fallback(1) + fallback(1, ok=False, reason=reason))["verdict"]
    assert verdict["result"] == "FAIL" and "did not land" in verdict["reason"]


def test_a_mark_from_a_worker_older_than_940_is_unknown_not_forge():
    old = clone_mark("tsk_old", "att_tsk_old", 37.0)
    result = err.build_bundle_report(err.read_documents(stream(bundle_docs(old))), now=NOW)
    assert [r["source"] for r in result["rows"]] == ["unknown"]
    assert result["fallback"]["n"] == 0


def test_a_bundle_mark_older_than_since_is_not_read():
    marks = bundled(30, 1.0) + [bundle_mark("tsk_old", "bundle", 99.0, at="2026-09-01T00:00:00+00:00")]
    result = bundle_report(marks + fallback(), since="7d")
    assert result["visited"]["clone_timed_marks"] == 31


def test_bundle_report_cli_exit_codes(tmp_path):
    run = lambda text, *extra: subprocess.run(  # noqa: E731
        [sys.executable, str(HELPER), "bundle-report", "--now", NOW, *extra],
        input=text, capture_output=True, text=True, check=False,
    )
    ok = run(stream(bundle_docs(*bundled(30, 2.0), *fallback())))
    assert ok.returncode == 0, ok.stderr
    assert "PASS" in ok.stdout and "visited: 31 task(s), 31 clone_timed mark(s)" in ok.stdout
    assert run(stream(bundle_docs(*bundled(3, 2.0)))).returncode == 1
    as_json = run(stream(bundle_docs(*bundled(30, 2.0), *fallback())), "--json")
    assert json.loads(as_json.stdout)["verdict"]["result"] == "PASS"
    nothing = run(stream([{"task_id": "tsk_x", "events": [], "next_page_token": None}]))
    assert nothing.returncode == 2 and "no clone_timed marks read" in nothing.stderr
    assert "PASS" not in nothing.stdout


def test_script_routes_clone_bundles_to_the_bundle_report():
    text = SCRIPT.read_text()
    assert '--clone-bundles) SUBCOMMAND="bundle-report"' in text
    assert '"${HELPER}" "${SUBCOMMAND}"' in text
    assert "--backend does not apply to --clone-bundles" in text
