"""The browser benchmark's plan and summary, offline (DEVB-BENCH; part of #1030).

`scripts/browser-bench.sh` submits the same ten browser prompts with Playwright
MCP and with dev-browser and, later, reads the finished tasks back;
`scripts/browser_bench.py` makes every number. What is pinned here is what
would let the benchmark compare something other than the two tools:

  * the arms differ by the tool sentence and NOTHING else -- not the profile,
    not another input key, not a word of the prompt (invariant 10 too: the
    tool is chosen by prose, never by a new API input);
  * the dry run's plan is the size the doc says (10 x 2 x 3 = 60) and its
    estimate is that count times the measured median;
  * the summary counts a run as a success only when its answer passes the
    prompt's check AND the agent drove the live browser, says "not measured"
    where nothing was read rather than 0, and reads hand-offs, gate refusals
    and Google /sorry pages from what the API serves.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "scripts" / "browser_bench.py"
SCRIPT = ROOT / "scripts" / "browser-bench.sh"
DOC = ROOT / "docs" / "browser-bench.md"


def _load():
    spec = importlib.util.spec_from_file_location("browser_bench", HELPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["browser_bench"] = module
    spec.loader.exec_module(module)
    return module


bb = _load()


# ---------------------------------------------------------------------------
# The prompts and the arms.


def test_ten_prompts_cover_what_the_brief_asks():
    kinds = [p.kind for p in bb.PROMPTS]
    assert len(bb.PROMPTS) == 10
    assert len({p.id for p in bb.PROMPTS}) == 10
    assert kinds.count("handoff") >= 2
    assert kinds.count("google-search") >= 2
    assert kinds.count("multi-page-form") >= 2
    assert kinds.count("large-page") >= 1
    assert kinds.count("multi-tab") >= 1
    for prompt in bb.PROMPTS:
        assert re.fullmatch(r"[a-z0-9-]+", prompt.id), prompt.id
        assert prompt.expect, f"{prompt.id} has no machine-checkable success criterion"
        for pattern in prompt.expect:
            re.compile(pattern)
    assert all(p.handoff for p in bb.PROMPTS if p.kind == "handoff")


def test_the_tool_sentence_is_the_only_difference_between_arms():
    runs = bb.plan("b1")
    by_key: dict[tuple[str, int], dict[str, dict]] = {}
    for run in runs:
        by_key.setdefault((run.prompt_id, run.rep), {})[run.tool] = run.body
    assert by_key, "the plan is empty"
    for key, arms in by_key.items():
        assert set(arms) == {"playwright-mcp", "dev-browser"}, key
        mcp, devb = arms["playwright-mcp"], arms["dev-browser"]
        # Same keys at every level but the label carried in metadata.
        assert set(mcp) == set(devb) == {"runner_profile", "input", "metadata"}
        assert mcp["runner_profile"] == devb["runner_profile"] == "claude-code-browser"
        assert set(mcp["input"]) == set(devb["input"]) == {"prompt"}
        mcp_rest = mcp["input"]["prompt"].removeprefix(bb.TOOLS["playwright-mcp"])
        devb_rest = devb["input"]["prompt"].removeprefix(bb.TOOLS["dev-browser"])
        assert mcp_rest != mcp["input"]["prompt"], "the MCP sentence does not lead the prompt"
        assert devb_rest != devb["input"]["prompt"], "the dev-browser sentence does not lead the prompt"
        assert mcp_rest == devb_rest, f"{key}: the arms' prompts differ beyond the tool sentence"
        strip = lambda m: {k: v for k, v in m["bench"].items() if k not in ("tool", "label")}  # noqa: E731
        assert strip(mcp["metadata"]) == strip(devb["metadata"])


def test_the_tool_sentences_are_the_owners_words():
    assert bb.TOOLS["dev-browser"].startswith("Use the dev-browser CLI for every browser action")
    assert bb.TOOLS["playwright-mcp"].startswith(
        "Use the Playwright MCP browser tools for every browser action")


def test_labels_are_bench_tool_prompt_rep_and_unique():
    runs = bb.plan("b1")
    labels = [r.label for r in runs]
    assert len(set(labels)) == len(labels)
    for run in runs:
        assert run.label == f"bench-{run.tool}-{run.prompt_id}-{run.rep}"
        assert run.body["metadata"]["bench"]["batch"] == "b1"
        assert run.body["metadata"]["bench"]["label"] == run.label


def test_arms_alternate_so_egress_and_time_of_day_are_shared():
    runs = bb.plan("b1")
    for first, second in zip(runs[0::2], runs[1::2]):
        assert (first.prompt_id, first.rep) == (second.prompt_id, second.rep)
        assert first.tool != second.tool


# ---------------------------------------------------------------------------
# The dry run.


def test_default_plan_is_ten_by_two_by_three():
    assert len(bb.plan("b1")) == 60
    assert len(bb.plan("b1", repeats=1)) == 20
    assert len(bb.plan("b1", tools=["dev-browser"], repeats=2)) == 20
    assert len(bb.plan("b1", prompt_ids=[bb.PROMPTS[0].id], repeats=3)) == 6


def test_estimate_is_runs_times_the_measured_median():
    assert bb.MEDIAN_TASK_USD == 0.66
    assert bb.estimate_usd(60) == pytest.approx(39.60)
    assert bb.estimate_usd(20, 1.0) == pytest.approx(20.0)


def test_unknown_prompt_or_tool_is_refused():
    with pytest.raises(ValueError):
        bb.plan("b1", prompt_ids=["no-such-prompt"])
    with pytest.raises(ValueError):
        bb.plan("b1", tools=["selenium"])
    with pytest.raises(ValueError):
        bb.plan("b1", repeats=0)


def _bench_env(tmp_path: Path) -> dict[str, str]:
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "TERM": "dumb",
           "SWARM_BROWSER_BENCH_DIR": str(tmp_path / "bench"), "NO_COLOR": "1"}
    return env


def test_dry_run_prints_the_plan_and_cost_and_submits_nothing(tmp_path):
    proc = subprocess.run(["bash", str(SCRIPT), "--dry-run", "--batch", "t1"],
                          capture_output=True, text=True, env=_bench_env(tmp_path), timeout=120)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert len(re.findall(r"^bench-(playwright-mcp|dev-browser)-[a-z0-9-]+-[0-9]+\t", proc.stdout,
                          re.M)) == 60, out
    assert "60 run(s)" in out
    assert "$39.60" in out
    assert not (tmp_path / "bench" / "t1" / "runs.tsv").exists(), "a dry run recorded submissions"


def test_dry_run_repeats_flag_changes_the_count(tmp_path):
    proc = subprocess.run(["bash", str(SCRIPT), "--dry-run", "--repeats", "1", "--batch", "t2"],
                          capture_output=True, text=True, env=_bench_env(tmp_path), timeout=120)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "20 run(s)" in out and "$13.20" in out


# ---------------------------------------------------------------------------
# Collecting: canned task JSON -> TSV and markdown.


def _write(path: Path, data) -> None:
    path.write_text(json.dumps(data))


def _canned(tmp_path: Path) -> Path:
    """Four runs of one prompt and one hand-off prompt, both arms."""
    batch = tmp_path / "batch"
    tasks = batch / "tasks"
    tasks.mkdir(parents=True)
    search = next(p for p in bb.PROMPTS if p.kind == "google-search")
    handoff = next(p for p in bb.PROMPTS if p.kind == "handoff")
    good_search = search.example
    good_handoff = handoff.example
    rows = [
        ("playwright-mcp", search, "task_aaa1", "SUCCEEDED", f"done\nRESULT: {good_search}", 2.0, 30),
        ("dev-browser", search, "task_bbb2", "SUCCEEDED", f"done\nRESULT: {good_search}", 0.5, 12),
        ("playwright-mcp", handoff, "task_ccc3", "SUCCEEDED", f"RESULT: {good_handoff}", 1.0, 20),
        # Claimed the right answer, but drove a hidden browser: no gate action.
        ("dev-browser", handoff, "task_ddd4", "SUCCEEDED", f"RESULT: {good_handoff}", 0.25, 8),
    ]
    lines = []
    for tool, prompt, task_id, state, answer, cost, turns in rows:
        label = f"bench-{tool}-{prompt.id}-1"
        lines.append("\t".join([label, tool, prompt.id, "1", task_id]))
        _write(tasks / f"{task_id}.task.json", {
            "id": task_id, "state": state,
            "started_at": "2026-10-14T10:00:00Z", "completed_at": "2026-10-14T10:05:00Z",
            "result_summary": {"runner": {"usage": {"num_turns": turns}}},
        })
        _write(tasks / f"{task_id}.attempts.json", {"attempts": [
            {"attempt_id": "a1", "cost_usd": cost, "input_tokens": 100, "output_tokens": 10,
             "cache_read_input_tokens": 1000, "cache_creation_input_tokens": None},
        ]})
        _write(tasks / f"{task_id}.answer.json", {"status": "ok", "content": answer,
                                                  "num_turns": turns})
        events = [{"type": "running"}, {"type": "succeeded"}]
        if prompt.handoff:
            events[1:1] = [{"type": "human_handoff_requested"}, {"type": "human_control_taken"},
                           {"type": "human_control_returned"}, {"type": "human_handoff_resolved"}]
        _write(tasks / f"{task_id}.events.json", {"events": events, "next_page_token": None})
        if task_id != "task_ddd4":
            actions = [{"kind": "cdp", "method": "Page.navigate", "url": "https://www.google.com/search"},
                       {"kind": "cdp", "method": "Performance.enable", "outcome": "refused",
                        "cause": "method_not_allowed"}]
            if task_id == "task_aaa1":
                actions.append({"kind": "cdp", "method": "Page.navigate",
                                "url": "https://www.google.com/sorry/index"})
            _write(tasks / f"{task_id}.actions.json", {"attempt_id": "a1", "actions": actions,
                                                       "truncated": False})
        transcript = {"steps": [{"type": "text", "text": "fine"}], "stream": {"next_offset": None}}
        if task_id == "task_aaa1":
            transcript["steps"].append({"type": "tool_result",
                                        "text": "https://www.google.com/sorry/index?continue=x "
                                                "Our systems have detected unusual traffic"})
        _write(tasks / f"{task_id}.transcript.0.json", transcript)
    (batch / "runs.tsv").write_text("\n".join(lines) + "\n")
    return batch


def test_measure_reads_cost_turns_tokens_handoffs_refusals_and_sorry(tmp_path):
    batch = _canned(tmp_path)
    rows = bb.collect(batch)
    by_task = {r["task_id"]: r for r in rows}
    a = by_task["task_aaa1"]
    assert a["state"] == "SUCCEEDED"
    assert a["check"] is True and a["live_browser"] is True and a["success"] is True
    assert a["cost_usd"] == pytest.approx(2.0)
    assert a["turns"] == 30
    assert a["duration_s"] == pytest.approx(300.0)
    assert a["input_tokens"] == 100 and a["output_tokens"] == 10
    assert a["cache_read_input_tokens"] == 1000
    assert a["cache_creation_input_tokens"] is None, "not reported is not zero"
    assert a["cdp_method_refused"] == 1
    assert a["google_sorry"] >= 1
    c = by_task["task_ccc3"]
    assert c["handoffs_requested"] == 1 and c["handoffs_returned"] == 1
    d = by_task["task_ddd4"]
    assert d["check"] is True
    assert d["live_browser"] is False and d["success"] is False, \
        "an answer from a hidden browser is not a live-browser success"
    assert d["cdp_method_refused"] is None, "no action log is 'not measured', not 0"


def test_check_reads_the_last_result_line_only():
    prompt = next(p for p in bb.PROMPTS if p.kind == "google-search")
    assert bb.check(prompt, f"thinking\nRESULT: {prompt.example}")
    assert not bb.check(prompt, "RESULT: no idea")
    assert not bb.check(prompt, f"the answer may be {prompt.example}")
    assert not bb.check(prompt, None)
    assert bb.check(prompt, f"RESULT: wrong\nRESULT: {prompt.example}")


def test_every_example_passes_its_own_check():
    for prompt in bb.PROMPTS:
        assert bb.check(prompt, f"RESULT: {prompt.example}"), prompt.id


def test_summary_per_tool_and_tsv_and_markdown(tmp_path):
    batch = _canned(tmp_path)
    rows = bb.collect(batch)
    summary = bb.summarize(rows)
    mcp, devb = summary["playwright-mcp"], summary["dev-browser"]
    assert mcp["runs"] == 2 and mcp["successes"] == 2
    assert devb["runs"] == 2 and devb["successes"] == 1
    assert mcp["median_cost_usd"] == pytest.approx(1.5)
    assert devb["median_turns"] == pytest.approx(10.0)
    assert mcp["handoffs_requested"] == 1 and mcp["handoffs_returned"] == 1
    assert mcp["cdp_method_refused"] == 2
    assert devb["cdp_method_refused_measured"] == 1

    tsv = bb.render_tsv(rows).splitlines()
    header = tsv[0].split("\t")
    for column in ("label", "tool", "prompt", "rep", "task_id", "success", "cost_usd",
                   "duration_s", "turns", "input_tokens", "output_tokens",
                   "cache_read_input_tokens", "cache_creation_input_tokens",
                   "handoffs_requested", "handoffs_returned", "cdp_method_refused",
                   "google_sorry"):
        assert column in header, column
    assert len(tsv) == 1 + 4
    ddd = next(line for line in tsv if "task_ddd4" in line).split("\t")
    assert ddd[header.index("cdp_method_refused")] == "not measured"
    assert ddd[header.index("cache_creation_input_tokens")] == "not measured"

    md = bb.render_markdown(summary, rows)
    assert "playwright-mcp" in md and "dev-browser" in md
    assert "| tool |" in md


def test_collect_cli_writes_tsv_and_markdown_per_tool(tmp_path):
    batch = _canned(tmp_path)
    proc = subprocess.run([sys.executable, str(HELPER), "collect", "--dir", str(batch)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert (batch / "results.tsv").exists()
    assert (batch / "summary.md").exists()
    for tool in ("playwright-mcp", "dev-browser"):
        assert (batch / f"results-{tool}.tsv").exists(), tool
    assert "4 run(s) read" in proc.stderr + proc.stdout


def test_an_unfinished_task_is_not_a_failure_and_not_a_success(tmp_path):
    batch = _canned(tmp_path)
    _write(batch / "tasks" / "task_aaa1.task.json", {"id": "task_aaa1", "state": "RUNNING"})
    rows = bb.collect(batch)
    row = next(r for r in rows if r["task_id"] == "task_aaa1")
    assert row["finished"] is False and row["success"] is None
    summary = bb.summarize(rows)
    assert summary["playwright-mcp"]["unfinished"] == 1
    assert summary["playwright-mcp"]["runs"] == 1


def test_collecting_nothing_exits_two(tmp_path):
    batch = tmp_path / "empty"
    batch.mkdir()
    (batch / "runs.tsv").write_text("")
    proc = subprocess.run([sys.executable, str(HELPER), "collect", "--dir", str(batch)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2, "reading nothing is never a pass (CLAUDE.md, rule zero)"


# ---------------------------------------------------------------------------
# The script and the doc.


def test_script_house_style():
    text = SCRIPT.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    effective = [line for line in text.splitlines()[1:]
                 if line.strip() and not line.lstrip().startswith("#")]
    assert effective[0] == "set -euo pipefail"
    assert "lib/common.sh" in text
    assert SCRIPT.stat().st_mode & 0o111, "the script must be executable"
    assert "declare -A" not in text and "mapfile" not in text
    assert "run(s) visited" in text, "the script must print how many runs it visited"


def test_doc_lists_every_prompt_and_the_pass_bar():
    text = DOC.read_text()
    for prompt in bb.PROMPTS:
        assert f"`{prompt.id}`" in text, prompt.id
    assert "8/10" in text
    assert "0.66" in text and "39.60" in text
    assert "egress" in text.lower()
    assert "scripts/browser-bench.sh" in text
