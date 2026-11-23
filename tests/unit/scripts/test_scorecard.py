"""scripts/lib/scorecard.py: every measure, the 'not read' rule, no workspace name.

Recorded-shape fixtures built at runtime: task records as the Firestore read
decodes them, the API's /artifacts/content answer for verdict.json, and gh
pages as the shell saves them. No cloud, no emulator, no network.
"""

from __future__ import annotations

import importlib.util
import json
import os
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "scripts" / "lib" / "scorecard.py"
SCRIPT = ROOT / "scripts" / "scorecard.sh"

SINCE = "2026-11-09T00:00:00Z"
UNTIL = "2026-11-23T00:00:00Z"  # two weeks: 11-09..11-16 and 11-16..11-23
W2 = "2026-11-18"


def _load():
    spec = importlib.util.spec_from_file_location("scorecard", HELPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sc = _load()

# Workspace names, made at runtime so none is a literal anywhere in the repo.
MAIN = f"team-{secrets.token_hex(4)}"
PERSONAL = f"u-{secrets.token_hex(4)}"
ACCEPT = f"acc-{secrets.token_hex(4)}"


def task(tid, step, *, state="SUCCEEDED", tenant=None, profile="claude-code", cost=1.0,
         turns=10, created=f"{W2}T10:00:00Z", started=f"{W2}T10:05:00Z",
         completed=f"{W2}T11:05:00Z", workflow="wf_a", model="model-big", model_class="opus",
         pr=None, end_cause=None, usage=None, prepublish=None, **extra):
    u = {"input_tokens": 100, "cache_read_input_tokens": 9000, "cache_creation_input_tokens": 900,
         "output_tokens": 500, "num_turns": turns, "duration_api_ms": 1_800_000,
         "total_cost_usd": cost}
    if usage is not None:
        u = usage
    summary: dict = {"runner": {"usage": u}}
    if prepublish is not None:
        summary["runner"]["output"] = {"prepublish": prepublish}
    if pr is not None:
        summary["git"] = {"pull_request": {"number": pr}}
    record = {"id": tid, "step_id": step, "state": state, "tenant_id": tenant or MAIN,
              "runner_profile": profile, "created_at": created, "started_at": started,
              "completed_at": completed, "workflow_id": workflow, "model": model,
              "model_class": model_class, "attempt_count": 1, "end_cause": end_cause,
              "attempt_totals": {"cost_usd_total": cost}, "result_summary": summary}
    record.update(extra)
    return record


def write_records(path: Path, records) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def report_for(records, *, verdicts=None, verdict_meta=None, github=None, since=SINCE, until=UNTIL):
    start, end = sc.resolve_window(since, until, None)
    meta = verdict_meta if verdict_meta is not None else sc.NotRead("verdicts were not read (--no-verdicts)")
    gh = github if github is not None else sc.read_github(None)
    return sc.build_report(records, sc.week_buckets(start, end), verdicts or {}, meta, gh,
                           (ACCEPT,), "a file")


def last_week(report):
    return report["weeks"][-1]["measures"]


def gh_dir(tmp_path: Path, *, comments=(), merged=(), release=(), accept=(), prci=(), failed=()):
    d = tmp_path / "gh"
    d.mkdir()
    status = []
    for source, items, key in (("comments", comments, None), ("merged", merged, "items"),
                               ("release", release, "workflow_runs"), ("accept", accept, "workflow_runs"),
                               ("prci", prci, "workflow_runs")):
        if source in failed:
            status.append(f"{source}\tgh api failed: HTTP 403")
            continue
        body = list(items) if key is None else {key: list(items)}
        (d / f"{source}-1.json").write_text(json.dumps(body))
        status.append(f"{source}\tok")
    (d / "status.tsv").write_text("\n".join(status) + "\n")
    (d / "calls").write_text("5\n")
    return d


# --- role_for_step -----------------------------------------------------------


@pytest.mark.parametrize("step, role", [
    ("implement", "build"), ("build", "build"), ("lane-12-build", "build"), ("impl", "build"),
    ("review", "review"), ("security-review", "review"), ("lane-3-review", "review"),
    ("review-fix:1", "fix"), ("fix", "fix"),
    ("re-review:2", "re-review"), ("re-review", "re-review"),
    ("ci-fix:1", "ci-fix"), ("conflict-fix", "conflict-fix"),
    ("planner", "planner"), ("plan", "planner"),
    ("docs", "other"), ("", "other"), (None, "other"),
])
def test_role_for_step(step, role):
    assert sc.role_for_step(step) == role


def test_workspace_class():
    assert sc.workspace_class(MAIN, (ACCEPT,)) == "main workspace"
    assert sc.workspace_class(PERSONAL, (ACCEPT,)) == "personal"
    assert sc.workspace_class(ACCEPT, (ACCEPT,)) == "acceptance"


# --- the window --------------------------------------------------------------


def test_weeks_end_at_until_and_the_oldest_may_be_short():
    start, end = sc.resolve_window("2026-11-01T00:00:00Z", UNTIL, None)
    buckets = sc.week_buckets(start, end)
    assert buckets[-1][1] == end and buckets[0][0] == start
    # 22 days: three whole weeks and one short day at the old end.
    assert len(buckets) == 4
    assert (buckets[0][1] - buckets[0][0]).days == 1
    assert all((b[1] - b[0]).days == 7 for b in buckets[1:])


def test_weeks_option_is_n_seven_day_buckets():
    now = datetime(2026, 11, 23, tzinfo=timezone.utc)
    start, end = sc.resolve_window(None, None, 4, now=now)
    assert len(sc.week_buckets(start, end)) == 4


# --- task measures -----------------------------------------------------------


def test_counts_success_cost_and_tokens():
    records = [
        task("task_a1", "implement", cost=4.0),
        task("task_a2", "review", cost=1.0),
        task("task_a3", "review-fix:1", state="FAILED", cost=3.0),
        task("task_a4", "merge", profile="merge", cost=None, usage={}),  # not an agent task
        task("task_old", "implement", created="2026-11-10T10:00:00Z", cost=2.0),
    ]
    m = last_week(report_for(records))
    assert m["agent_tasks"] == 3
    assert m["succeeded_pct"] == pytest.approx(66.7)
    assert m["cost_usd"] == 8.0
    assert m["tokens"] == {"cache_read": 27000, "cache_creation": 2700, "output": 1500, "input": 300}
    first = report_for(records)["weeks"][0]["measures"]
    assert first["agent_tasks"] == 1 and first["cost_usd"] == 2.0


def test_cost_share_by_role_and_model():
    records = [
        task("task_b1", "implement", cost=6.0, model="model-big"),
        task("task_b2", "review", cost=2.0, model="model-small"),
        task("task_b3", "re-review:1", cost=1.0, model="model-small"),
        task("task_b4", "ci-fix:1", cost=1.0, model="model-big"),
    ]
    m = last_week(report_for(records))
    roles = m["cost_share_by_role_pct"]
    assert roles["build"] == 60.0 and roles["review"] == 20.0
    assert roles["re-review"] == 10.0 and roles["ci-fix"] == 10.0 and roles["planner"] == 0.0
    assert m["cost_share_by_model_pct"] == {"model-big": 70.0, "model-small": 30.0}


def test_turns_context_and_long_tasks():
    records = [task(f"task_c{i}", "implement", turns=t, cost=c)
               for i, (t, c) in enumerate([(10, 1.0), (20, 1.0), (30, 1.0), (150, 7.0)])]
    m = last_week(report_for(records))
    assert m["turns"] == {"median": 20, "p90": 150, "n": 4}
    assert m["over_100_turns_pct"] == 25.0
    assert m["over_100_turns_cost_pct"] == 70.0
    # context per turn = (input + cache read + cache creation) / turns
    assert m["context_tokens_per_turn"]["median"] == round(10000 / 30)


def test_model_time_versus_wall_clock():
    m = last_week(report_for([task("task_d1", "implement")]))
    # 30 min of model time in a 60 min attempt
    assert m["model_time_vs_wall_clock"] == {"model_h": 0.5, "wall_h": 1.0, "model_pct": 50.0}


def test_queue_wait_splits_standalone_from_workflow_steps():
    records = [task("task_e1", "implement", workflow=None, started=f"{W2}T10:01:00Z"),
               task("task_e2", "implement", started=f"{W2}T10:10:00Z")]
    m = last_week(report_for(records))
    assert m["queue_wait_s"] == {"median": 60.0, "p90": 60.0, "n": 1}
    assert m["step_created_to_started_s"] == {"median": 600.0, "p90": 600.0, "n": 1}


def test_merge_steps_refused():
    records = [task("task_f1", "merge", profile="merge", state="FAILED", end_cause="merge_refused"),
               task("task_f2", "merge", profile="merge"),
               task("task_f3", "merge", profile="merge"),
               task("task_f4", "merge", profile="merge", state="RUNNING")]
    assert last_week(report_for(records))["merge_steps_refused_pct"] == pytest.approx(33.3)


def test_prepublish_gate_outcomes():
    budget = {"rounds": [[{"name": "guards", "status": "passed", "left_to_ci": ["g1", "g2"]}]]}
    clean = {"rounds": [[{"name": "graph-pytest", "status": "passed"}]]}
    skipped = {"skipped": "budget", "rounds": []}
    records = [task("task_g1", "implement", prepublish=budget),
               task("task_g2", "implement", prepublish=clean),
               task("task_g3", "implement", prepublish=skipped),
               task("task_g4", "implement")]
    m = last_week(report_for(records))
    assert m["gate_runs"] == 3
    assert m["gate_at_budget_pct"] == pytest.approx(66.7)
    assert m["gate_guards_left_to_ci"] == 2


# --- workflows and GitHub ----------------------------------------------------


def test_workflow_latency_and_per_merged_pr(tmp_path):
    records = [
        task("task_h1", "implement", created=f"{W2}T08:00:00Z", completed=f"{W2}T09:00:00Z", pr=41, cost=3.0),
        task("task_h2", "review", created=f"{W2}T08:00:00Z", completed=f"{W2}T10:00:00Z", cost=1.0),
        task("task_h3", "merge", profile="merge", created=f"{W2}T08:00:00Z", cost=None, usage={}),
    ]
    merged = [{"number": 41, "pull_request": {"merged_at": f"{W2}T12:00:00Z"}},
              {"number": 42, "pull_request": {"merged_at": f"{W2}T13:00:00Z"}}]
    m = last_week(report_for(records, github=sc.read_github(gh_dir(tmp_path, merged=merged))))
    assert m["merged_prs"] == 2
    assert m["cost_usd_per_merged_pr"] == 2.0
    assert m["tokens_per_merged_pr"]["output"] == 500
    assert m["workflows_merged"] == 1
    assert m["workflow_submit_to_merged_h"]["median"] == 4.0
    assert m["workflow_last_review_or_fix_to_merged_h"]["median"] == 2.0


def test_merge_step_completion_stands_in_when_github_is_off():
    records = [task("task_i1", "review", created=f"{W2}T08:00:00Z", completed=f"{W2}T09:00:00Z"),
               task("task_i2", "merge", profile="merge", created=f"{W2}T08:00:00Z",
                    completed=f"{W2}T11:00:00Z")]
    m = last_week(report_for(records))
    assert m["workflow_submit_to_merged_h"]["median"] == 3.0


def test_ejections_by_cause(tmp_path):
    sh_body = ("**Ejected from the merge train (rule 3, bisect), and `ready` removed.** Train 7 failed:\n\n"
               "- [unit (3)](https://example.invalid/1)\n- [lint](https://example.invalid/2)\n")
    api_body = "<!-- m -->\n**Ejected from the merge train of `main`** (conflict): it no longer merges."
    comments = [{"created_at": f"{W2}T10:00:00Z", "body": sh_body},
                {"created_at": f"{W2}T11:00:00Z", "body": api_body},
                {"created_at": f"{W2}T12:00:00Z", "body": "unrelated comment"},
                {"created_at": "2026-11-01T12:00:00Z", "body": api_body}]
    merged = [{"number": n, "pull_request": {"merged_at": f"{W2}T12:00:00Z"}} for n in (1, 2, 3, 4)]
    m = last_week(report_for([task("task_j1", "implement")],
                             github=sc.read_github(gh_dir(tmp_path, comments=comments, merged=merged))))
    assert m["train_ejections"] == 2
    assert m["train_ejections_per_merged_pr"] == 0.5
    assert m["train_ejections_by_cause"] == {"lint, unit (3)": 1, "conflict": 1}


def test_release_acceptance_and_pr_ci(tmp_path):
    def run(conclusion, minutes, created=f"{W2}T10:00:00Z"):
        return {"status": "completed", "conclusion": conclusion, "created_at": created,
                "run_started_at": created, "updated_at": f"{W2}T10:{minutes:02d}:00Z"}
    gh = sc.read_github(gh_dir(tmp_path, release=[run("success", 20), run("failure", 40)],
                               accept=[run("success", 30), run("success", 30), run("cancelled", 5)],
                               prci=[run("success", 10), run("failure", 12), run("success", 14)]))
    m = last_week(report_for([task("task_k1", "implement")], github=gh))
    assert m["release_runs"] == 2 and m["release_success_pct"] == 50.0
    assert m["release_duration_min"] == {"median": 20.0, "p90": 40.0, "n": 2}
    assert m["acceptance_success_pct"] == 100.0  # a cancelled run decides nothing
    assert m["pr_ci_duration_min"]["median"] == 12.0


# --- review verdicts ---------------------------------------------------------


def _verdict_answer(verdict, **extra):
    return json.dumps({"status": "ok", "content": json.dumps(dict(verdict=verdict, **extra))})


def test_review_not_yet_rate_by_model_class(tmp_path):
    d = tmp_path / "verdicts"
    d.mkdir()
    (d / "task_v1.json").write_text(_verdict_answer("MERGE"))
    (d / "task_v2.json").write_text(_verdict_answer("not_yet", findings=["x"]))
    (d / "task_v3.json").write_text(_verdict_answer("NOT_YET"))
    (d / "task_v4.json").write_text(_verdict_answer("SKIPPED", skipped="size gate"))
    (d / "task_v5.json").write_text("")  # 404: no verdict.json
    (d / "meta.json").write_text(json.dumps({"candidates": 9, "sampled": 5, "cap": 5}))
    verdicts, meta = sc.read_verdicts(d)
    records = [task("task_v1", "review", model_class="sonnet"),
               task("task_v2", "review", model_class="sonnet"),
               task("task_v3", "re-review:1", model_class="opus"),
               task("task_v4", "review", model_class="opus"),
               task("task_v5", "review", model_class="opus")]
    report = report_for(records, verdicts=verdicts, verdict_meta=meta)
    by_class = last_week(report)["review_verdicts_by_model_class"]
    assert by_class["sonnet"]["not_yet_pct"] == 50.0
    assert by_class["opus"] == {"MERGE": 0, "NOT_YET": 1, "SKIPPED": 1, "unreadable": 1, "not_yet_pct": 100.0}
    assert "5 sampled of 9 (cap 5)" in sc.render_text(report)


def test_reviews_lists_newest_succeeded_review_tasks_up_to_the_cap(tmp_path):
    records = [task("task_r1", "review", created=f"{W2}T01:00:00Z"),
               task("task_r2", "re-review:1", created=f"{W2}T02:00:00Z"),
               task("task_r3", "review", state="FAILED"),
               task("task_r4", "implement")]
    path = write_records(tmp_path / "r.jsonl", records)
    out = subprocess.run([sys.executable, str(HELPER), "reviews", "--records", str(path), "--max", "1"],
                         capture_output=True, text=True, check=True).stdout.splitlines()
    assert out == ["candidates 2", f"task task_r2 {MAIN}"]


# --- not read ----------------------------------------------------------------


def test_unread_sources_say_not_read_never_zero(tmp_path):
    text = sc.render_text(report_for([task("task_n1", "implement", cost=None, usage={})]))
    for label in ("cost USD", "merged pull requests", "cost USD per merged PR", "train ejections",
                  "review verdicts by model class", "release success %", "acceptance pass %",
                  "PR CI duration min", "turns", "pre-publish gate runs", "model time vs wall-clock"):
        line = next(ln for ln in text.splitlines() if ln.strip().startswith(label))
        assert "not read: " in line, line
    assert "not read: GitHub was not read (--no-github)" in text
    assert "not read: verdicts were not read (--no-verdicts)" in text


def test_a_failed_github_source_names_why(tmp_path):
    gh = sc.read_github(gh_dir(tmp_path, failed=("release",)))
    m = last_week(report_for([task("task_o1", "implement")], github=gh))
    assert m["release_success_pct"].why == "gh api failed: HTTP 403"
    assert m["merged_prs"] == 0
    assert m["cost_usd_per_merged_pr"].why == "no pull request merged in the week"


def test_an_empty_week_is_zero_tasks_and_not_read_rates():
    report = report_for([])
    m = last_week(report)
    assert m["agent_tasks"] == 0
    assert isinstance(m["succeeded_pct"], sc.NotRead)
    assert json.dumps(sc.to_json(report))  # every value serialises


# --- workspace names never appear --------------------------------------------


def test_workspace_names_never_appear(tmp_path):
    records = [task("task_p1", "implement", tenant=MAIN, workflow=f"wf_{MAIN}"),
               task("task_p2", "review", tenant=PERSONAL),
               task("task_p3", "implement", tenant=ACCEPT),
               task("task_p4", "implement", tenant=PERSONAL)]
    path = write_records(tmp_path / "records.jsonl", records)
    for extra in ([], ["--json"]):
        out = subprocess.run(
            [sys.executable, str(HELPER), "report", "--since", SINCE, "--until", UNTIL,
             "--records", str(path), "--acceptance-tenant", ACCEPT, *extra],
            capture_output=True, text=True, check=True).stdout
        for name in (MAIN, PERSONAL, ACCEPT):
            assert name not in out
        assert "main workspace" in out and "personal" in out and "acceptance" in out
    m = last_week(report_for(records))
    assert m["agent_tasks_by_workspace"] == {"main workspace": 1, "acceptance": 1, "personal": 2}


# --- Firestore ---------------------------------------------------------------


def test_fs_query_masks_fields_and_pages_on_created_at_and_name():
    start, end = sc.resolve_window(SINCE, UNTIL, None)
    body = sc.fs_query_body(start, end, {"created_at": {"timestampValue": "t"}, "name": "n"})
    q = body["structuredQuery"]
    assert {f["fieldPath"] for f in q["select"]["fields"]} == set(sc.FIELD_MASK)
    assert [o["field"]["fieldPath"] for o in q["orderBy"]] == ["created_at", "__name__"]
    assert q["startAt"] == {"before": False, "values": [{"timestampValue": "t"}, {"referenceValue": "n"}]}
    ops = [f["fieldFilter"]["op"] for f in q["where"]["compositeFilter"]["filters"]]
    assert ops == ["GREATER_THAN_OR_EQUAL", "LESS_THAN"]


def test_fs_decode_reads_typed_values(tmp_path):
    name = "projects/p/databases/d/documents/tasks/task_q1"
    answer = [{"document": {"name": name, "fields": {
        "state": {"stringValue": "SUCCEEDED"},
        "attempt_count": {"integerValue": "2"},
        "created_at": {"timestampValue": f"{W2}T10:00:00.123456789Z"},
        "attempt_totals": {"mapValue": {"fields": {"cost_usd_total": {"doubleValue": 1.5}}}},
        "end_cause": {"nullValue": None}}}}, {"readTime": "x"}]
    records, cursor = sc.fs_decode(answer)
    assert records == [{"id": "task_q1", "state": "SUCCEEDED", "attempt_count": 2,
                        "created_at": f"{W2}T10:00:00.123456789Z",
                        "attempt_totals": {"cost_usd_total": 1.5}, "end_cause": None}]
    assert cursor["name"] == name
    assert sc.parse_time(records[0]["created_at"]) is not None


def test_wrapper_is_read_only_and_bounded():
    text = SCRIPT.read_text()
    assert text.splitlines()[0] == "#!/usr/bin/env bash"
    assert "source \"$(cd -- \"$(dirname -- \"${BASH_SOURCE[0]}\")\" && pwd)/lib/common.sh\"" in text
    for flag in ("--from-file", "--no-github", "--max-gh-calls", "--max-verdicts", "--weeks", "--json"):
        assert flag in text
    # Nothing written: no PATCH, DELETE or POST except Firestore's read-only runQuery.
    assert "fs_patch" not in text and "fs_delete" not in text
    assert "-X DELETE" not in text and "-X POST" not in text
    assert text.count("fs_request POST") == 1 and ":runQuery" in text
    assert "| redact" in text


def test_wrapper_reports_an_extract_offline(tmp_path):
    """The shell end to end on --from-file, with GitHub and verdicts off: no
    network, the placeholder deny list, and still no workspace name."""
    config = tmp_path / "config" / "dev"
    config.mkdir(parents=True)
    example = json.loads((ROOT / "scripts" / "lib" / "deny-list.example.json").read_text())
    example["project"] = "example-project"
    (config / "deny-list.json").write_text(json.dumps(example))
    records = write_records(tmp_path / "x.jsonl", [task("task_w1", "implement", tenant=PERSONAL)])
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", str(tmp_path)), "TMPDIR": str(tmp_path),
           "SWARM_DEPLOY_CONFIG_DIR": str(tmp_path / "config"), "XDG_CACHE_HOME": str(tmp_path / "cache"),
           "ENVIRONMENT": "dev", "PROJECT_ID": "example-project", "REGION": "example-region"}
    done = subprocess.run(["bash", str(SCRIPT), "--from-file", str(records), "--no-github", "--no-verdicts",
                           "--until", UNTIL], capture_output=True, text=True, env=env, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    assert "agent tasks by workspace" in done.stdout and "personal 1" in done.stdout
    assert "not read: GitHub was not read (--no-github)" in done.stdout
    assert PERSONAL not in done.stdout + done.stderr


# --- sessions per role: tool calls, turns, ceiling stops ---------------------


def sessioned(tid, step, *, turns, families, sessions, stops=None, ceiling=True, **kw):
    """An agent task with the session fields the runner records: the attempt's
    tool calls per family (`metrics.tool_calls`), its session count
    (`metrics.sessions`) and the ceiling record (`output.context_ceiling`)."""
    record = task(tid, step, turns=turns, **kw)
    runner = record["result_summary"]["runner"]
    runner["metrics"] = {"tool_calls": {"read": True, "complete": True, "families": families},
                         "sessions": {"count": sessions, "usage_unread": [], "cost_complete": True}}
    if ceiling:
        runner["output"] = {"context_ceiling": {
            "limits": {"tokens": 200_000, "tool_calls": 150}, "max_fresh_sessions": 4,
            "fresh_sessions": len(stops or []), "stops": list(stops or []), "skipped": None}}
    return record


def stop(turn, calls, why="tool_calls"):
    return {"at": "turn_boundary", "why": why, "turn": turn, "context_tokens": 90_000,
            "peak_context_tokens": 95_000, "tool_calls": calls, "fresh_session": 1}


def test_sessions_per_role_from_the_recorded_fields():
    records = [
        # One session each: the attempt's numbers are the session's.
        sessioned("task_s1", "implement", turns=40, families={"bash": 30, "read": 10}, sessions=1),
        sessioned("task_s2", "implement", turns=60, families={"bash": 50}, sessions=1),
        # Two sessions, the first stopped at the ceiling: its own numbers are the
        # stop's; the second session's own numbers are not recorded.
        sessioned("task_s3", "implement", turns=200, families={"bash": 170, "edit": 20},
                  sessions=2, stops=[stop(120, 151)]),
        sessioned("task_s4", "review:1", turns=12, families={"read": 8}, sessions=1),
    ]
    m = last_week(report_for(records))
    assert m["attempt_tool_calls_by_role"] == {
        "build": {"median": 50, "p90": 190, "p99": 190, "n": 3},
        "review": {"median": 8, "p90": 8, "p99": 8, "n": 1}}
    assert m["attempt_turns_by_role"]["build"] == {"median": 60, "p90": 200, "p99": 200, "n": 3}
    assert m["session_tool_calls_by_role"]["build"] == {"median": 50, "p90": 151, "p99": 151, "n": 3}
    assert m["session_turns_by_role"]["build"] == {"median": 60, "p90": 120, "p99": 120, "n": 3}
    assert m["session_turns_by_role"]["review"] == {"median": 12, "p90": 12, "p99": 12, "n": 1}
    # 1 stop of 4 build sessions; 0 of 1 review session.
    assert m["sessions_at_ceiling_pct_by_role"] == {"build": 25.0, "review": 0.0}
    assert m["fresh_sessions_per_attempt_by_role"]["build"] == {
        "median": 0, "p90": 1, "p99": 1, "n": 3}
    assert m["sessions_without_own_numbers"] == 1


def test_session_measures_say_not_read_without_the_fields():
    m = last_week(report_for([task("task_n1", "implement")]))
    for key in ("attempt_tool_calls_by_role", "session_tool_calls_by_role",
                "session_turns_by_role", "sessions_at_ceiling_pct_by_role",
                "fresh_sessions_per_attempt_by_role", "sessions_without_own_numbers"):
        assert isinstance(m[key], sc.NotRead), key
    # Turns per attempt are recorded without the ceiling's fields.
    assert m["attempt_turns_by_role"] == {"build": {"median": 10, "p90": 10, "p99": 10, "n": 1}}


def test_a_cut_ceiling_record_is_not_read_as_no_stop():
    # The runner's output is cut at 8,000 characters (`lifecycle._truncate_json`):
    # the ceiling record is then unread, never "no stop".
    record = sessioned("task_t1", "fix", turns=300, families={"bash": 400}, sessions=3,
                       ceiling=False)
    record["result_summary"]["runner"]["output"] = {"truncated": True, "preview": "{"}
    m = last_week(report_for([record]))
    assert isinstance(m["sessions_at_ceiling_pct_by_role"], sc.NotRead)
    assert isinstance(m["session_tool_calls_by_role"], sc.NotRead)
    assert m["sessions_without_own_numbers"] == 3
    assert m["fresh_sessions_per_attempt_by_role"] == {"fix": {"median": 2, "p90": 2, "p99": 2, "n": 1}}


def test_session_fields_are_in_the_mask_and_the_table():
    for field in ("result_summary.runner.output.context_ceiling",
                  "result_summary.runner.metrics.sessions",
                  "result_summary.runner.metrics.tool_calls"):
        assert field in sc.FIELD_MASK
    keys = {key for key, _ in sc.ROWS}
    assert {"attempt_tool_calls_by_role", "attempt_turns_by_role", "session_tool_calls_by_role",
            "session_turns_by_role", "sessions_at_ceiling_pct_by_role",
            "fresh_sessions_per_attempt_by_role", "sessions_without_own_numbers"} <= keys
    text = sc.render_text(report_for([
        sessioned("task_r1", "implement", turns=40, families={"bash": 30}, sessions=1)]))
    assert "build: median 30, p90 30, p99 30 (n=1)" in text
