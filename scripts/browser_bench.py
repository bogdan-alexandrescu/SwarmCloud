#!/usr/bin/env python3
"""The browser benchmark's plan and its numbers (DEVB-BENCH; part of #1030).

Owner decision 2026-10-14: dev-browser is built opt-in beside the Playwright
MCP, which stays the default, and "a benchmark decides any default flip
later". This is that benchmark's arithmetic. `scripts/browser-bench.sh`
submits and fetches; everything it decides is decided here, and unit-tested
offline (tests/unit/scripts/test_browser_bench.py). See docs/browser-bench.md
for the prompts, the pass bar and what a run costs.

THE ARMS DIFFER BY ONE SENTENCE. Both arms are `claude-code-browser` with the
same prompt; the tool is chosen only by the sentence `TOOLS` puts in front of
it. No new API input selects a tool (invariant 10: a caller picks a profile by
name and sends a prompt, never a backend parameter), and nothing else may
differ, or the benchmark measures that instead.

A SUCCESS IS CHECKED, NOT CLAIMED. The agent ends its answer with
`RESULT: <value>`; the value must fully match one of the prompt's `expect`
patterns, the task must have SUCCEEDED, the gate's action log must show the
agent drove the LIVE browser (at least one forwarded CDP method -- an answer
from a hidden browser of the agent's own, task_7cda0d5335f049b4b442, is not a
live-browser success), and a hand-off prompt must show the hand-off come back
(`human_control_returned`). "Not reported" is never 0: a figure the API did not
serve is None here and "not measured" in the TSV.

JEV IS A THIRD ARM, OPT-IN (JEV-RUNNER, owner decision 2026-10-14). The
`jev` arm is chosen by its own sentence like the other two; it is not in a
default plan, because its tools exist only for a submitter or tenant with a
TypeSafe key and every run spends TypeSafe credits as well as the task's.
Name it: `--tools playwright-mcp,dev-browser,jev`. Each challenger is held to
the same bar against Playwright MCP, with its own verdict line.

    browser_bench.py plan --batch B [--repeats 3] [--prompts a,b] [--tools a,b]
                          [--usd-per-task 0.66] [--bodies DIR]
    browser_bench.py collect --dir BATCH_DIR
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_report import sorry_hits  # noqa: E402  (Google /sorry in an action log, one rule)

#: The one live-browser profile (swarm_common.profiles, contract request 71).
PROFILE = "claude-code-browser"

#: The ONLY difference between the arms, word for word the owner's.
TOOLS: dict[str, str] = {
    "playwright-mcp": "Use the Playwright MCP browser tools for every browser action.",
    "dev-browser": "Use the dev-browser CLI for every browser action.",
    "jev": ("Use the jev browser tools (browser_do, browser_read and the other `jev` "
            "tools) for every browser action."),
}

#: The arms a plan has when `--tools` is not given: jev is opt-in (docstring).
DEFAULT_TOOLS: tuple[str, ...] = ("playwright-mcp", "dev-browser")

#: The arm every other one is measured against: today's default.
BASELINE = "playwright-mcp"

#: The measured median cost of one task, in USD (docs/browser-bench.md shows
#: the arithmetic). An estimate, never a measurement: `--collect` reports what
#: each run actually cost.
MEDIAN_TASK_USD = 0.66

DEFAULT_REPEATS = 3

#: What every prompt ends with, so the check has one line to read.
RESULT_INSTRUCTION = (
    "When you are done, end your answer with exactly one line of the form "
    "`RESULT: <answer>`, in the format asked for above, and nothing after it. "
    "If you could not finish, write `RESULT: FAILED <why>`."
)

#: Task states after which nothing changes (swarm_common.states).
TERMINAL_STATES = ("SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTERED")

#: A Google rate-limit page or a 429 named in the agent's transcript.
_GOOGLE_LIMITED = re.compile(
    r"google\.[a-z.]{2,12}/sorry"
    r"|unusual traffic from your computer"
    r"|our systems have detected unusual traffic"
    r"|\b429 too many requests\b"
    r"|\bhttp(?:/[0-9.]+)? 429\b",
    re.IGNORECASE,
)

_RESULT_LINE = re.compile(r"^\s*RESULT:\s*(.*?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Prompt:
    id: str
    kind: str
    text: str
    #: Patterns the RESULT value must FULLY match (any one of them).
    expect: tuple[str, ...]
    #: A value that passes, for the doc and the tests.
    example: str
    #: Needs a person: success also needs the hand-off to come back.
    handoff: bool = False


PROMPTS: tuple[Prompt, ...] = (
    Prompt(
        id="google-python-release",
        kind="google-search",
        text=("Go to https://www.google.com and search for: python 3.12.0 release date. "
              "From the results (open one if you need to), give the date Python 3.12.0 was "
              "released, as YYYY-MM-DD."),
        expect=(r"2023-10-02",),
        example="2023-10-02",
    ),
    Prompt(
        id="google-rfc-http-semantics",
        kind="google-search",
        text=("Go to https://www.google.com and search for: RFC HTTP Semantics. "
              "From the results, give the number of the RFC titled \"HTTP Semantics\", "
              "as RFC <number>."),
        expect=(r"(?i)rfc ?9110",),
        example="RFC 9110",
    ),
    Prompt(
        id="saucedemo-checkout",
        kind="multi-page-form",
        text=("Open https://www.saucedemo.com. Sign in as the standard user, with the user "
              "name and the shared password the login page itself prints. Add \"Sauce Labs "
              "Backpack\" to the cart, check out with first name Bench, last name Run and "
              "postal code 94105, note the Item total on the overview page, and finish the "
              "order. Report the heading of the final page and the item total, as "
              "<heading> | <item total>."),
        expect=(r"(?i)thank you for your order!?\s*\|\s*(item total:\s*)?\$?29\.99",),
        example="Thank you for your order! | $29.99",
    ),
    Prompt(
        id="httpbin-pizza-form",
        kind="multi-page-form",
        text=("Open https://httpbin.org/forms/post. Fill in Customer name \"Bench Runner\", "
              "Telephone 555-0100, E-mail bench@example.com, Pizza Size Medium and the "
              "toppings Bacon and Onion, and submit the order. On the page the form posts "
              "to, read the values the server echoes back for custname and size, and report "
              "them as custname=<value>; size=<value>."),
        expect=(r"(?i)custname=bench runner;\s*size=medium",),
        example="custname=Bench Runner; size=medium",
    ),
    Prompt(
        id="rfc9110-large-page",
        kind="large-page",
        text=("Open https://www.rfc-editor.org/rfc/rfc9110.html, one page of about half a "
              "megabyte. Find the section numbers of the status code \"418 (Unused)\" and of "
              "the header field \"Content-Length\", and report them as <418 section>; "
              "<Content-Length section>."),
        expect=(r"15\.5\.19\s*;\s*8\.6",),
        example="15.5.19; 8.6",
    ),
    Prompt(
        id="herokuapp-new-tab",
        kind="multi-tab",
        text=("Open https://the-internet.herokuapp.com/windows and click \"Click Here\", which "
              "opens a second tab. Read the heading of the new tab, switch back to the first "
              "tab and read its heading, and report both as <new tab heading> / <first tab "
              "heading>."),
        expect=(r"(?i)new window\s*/\s*opening a new window",),
        example="New Window / Opening a new window",
    ),
    Prompt(
        id="github-signin-handoff",
        kind="handoff",
        text=("Open https://github.com/login. Do not type any user name or password yourself: "
              "ask a person to sign in, through the live-browser hand-off (swarm-handoff), and "
              "wait until they return control. Then open https://github.com/settings/profile "
              "and report the signed-in account's user name, as login=<user name>."),
        expect=(r"login=[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}",),
        example="login=octocat",
        handoff=True,
    ),
    Prompt(
        id="recaptcha-demo-handoff",
        kind="handoff",
        text=("Open https://www.google.com/recaptcha/api2/demo. Fill in the form's fields with "
              "Bench, Run and bench@example.com. Do not try to solve the CAPTCHA yourself: ask "
              "a person to solve it through the live-browser hand-off (swarm-handoff) and wait "
              "until they return control. Then submit the form and report the text the page "
              "shows after submitting."),
        expect=(r"(?i)verification success\W*hooray!?",),
        example="Verification Success... Hooray!",
        handoff=True,
    ),
    Prompt(
        id="pypi-requests-license",
        kind="lookup",
        text=("Open https://pypi.org/project/requests/ and report the license the project "
              "page lists in its Meta section, as written there."),
        expect=(r"(?i)(license:\s*)?apache[- ]?(software license|2\.0|license 2\.0)",),
        example="Apache-2.0",
    ),
    Prompt(
        id="herokuapp-dynamic-loading",
        kind="dynamic-page",
        text=("Open https://the-internet.herokuapp.com/dynamic_loading/2, click Start, wait "
              "until the loading bar has finished, and report the text that appears."),
        expect=(r"(?i)hello world!?",),
        example="Hello World!",
    ),
)

PROMPTS_BY_ID = {p.id: p for p in PROMPTS}


# ---------------------------------------------------------------------------
# The plan.


@dataclass(frozen=True)
class Run:
    label: str
    tool: str
    prompt_id: str
    rep: int
    body: dict[str, Any]


def label(tool: str, prompt_id: str, rep: int) -> str:
    return f"bench-{tool}-{prompt_id}-{rep}"


def prompt_for(tool: str, prompt: Prompt) -> str:
    """The tool's sentence, then the prompt -- the same prompt for every arm."""
    return f"{TOOLS[tool]}\n\n{prompt.text}\n\n{RESULT_INSTRUCTION}"


def request_body(batch: str, tool: str, prompt: Prompt, rep: int) -> dict[str, Any]:
    """A `POST /v1/tasks` body: a profile by name, a prompt, and a label in
    free-form metadata. Nothing that selects an image, a command or a tool."""
    name = label(tool, prompt.id, rep)
    return {
        "runner_profile": PROFILE,
        "input": {"prompt": prompt_for(tool, prompt)},
        "metadata": {"bench": {"batch": batch, "label": name, "tool": tool,
                               "prompt": prompt.id, "rep": rep}},
    }


def plan(batch: str, *, prompt_ids: Iterable[str] | None = None,
         tools: Iterable[str] | None = None, repeats: int = DEFAULT_REPEATS) -> list[Run]:
    """Every run, ARMS INTERLEAVED: for each repeat and prompt, one run per
    tool back to back, so the two arms share the egress, the hour and the
    sites' moods. Running one arm after the other would compare Tuesday
    morning with Tuesday night."""
    if repeats < 1:
        raise ValueError("--repeats must be at least 1")
    chosen = list(prompt_ids) if prompt_ids is not None else [p.id for p in PROMPTS]
    unknown = [p for p in chosen if p not in PROMPTS_BY_ID]
    if unknown:
        raise ValueError(f"unknown prompt(s) {unknown}; the prompts are {sorted(PROMPTS_BY_ID)}")
    arms = list(tools) if tools is not None else list(DEFAULT_TOOLS)
    bad = [t for t in arms if t not in TOOLS]
    if bad or not arms:
        raise ValueError(f"unknown tool(s) {bad}; the tools are {sorted(TOOLS)}")
    runs = []
    for rep in range(1, repeats + 1):
        for prompt_id in chosen:
            prompt = PROMPTS_BY_ID[prompt_id]
            for tool in arms:
                runs.append(Run(label(tool, prompt_id, rep), tool, prompt_id, rep,
                                request_body(batch, tool, prompt, rep)))
    return runs


def estimate_usd(runs: int, usd_per_task: float = MEDIAN_TASK_USD) -> float:
    return round(runs * usd_per_task, 2)


# ---------------------------------------------------------------------------
# Reading one finished run.


def result_value(answer: str | None) -> str | None:
    """The value on the LAST `RESULT:` line, without surrounding backticks."""
    if not isinstance(answer, str):
        return None
    found = _RESULT_LINE.findall(answer)
    if not found:
        return None
    return found[-1].strip().strip("`").strip()


def check(prompt: Prompt, answer: str | None) -> bool:
    value = result_value(answer)
    if value is None:
        return False
    return any(re.fullmatch(pattern, value) for pattern in prompt.expect)


def _json(path: Path) -> Any:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _attempt_sum(attempts: Any, field: str) -> float | None:
    """Summed across attempts; None when no attempt reported it (not 0)."""
    values = [_number(a.get(field)) for a in (attempts or {}).get("attempts") or []
              if isinstance(a, dict)]
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def _turns(task: dict[str, Any], answer: Any) -> int | None:
    turns = _number((answer or {}).get("num_turns")) if isinstance(answer, dict) else None
    if turns is None:
        usage = (((task.get("result_summary") or {}).get("runner") or {}).get("usage") or {})
        turns = _number(usage.get("num_turns")) if isinstance(usage, dict) else None
    return int(turns) if turns is not None else None


def _transcript_sorry(windows: list[Any]) -> int | None:
    """Transcript steps naming a Google /sorry page or a 429. None when no
    window was parsed into steps (a `text` transcript, or none read)."""
    steps = [s for w in windows if isinstance(w, dict) and isinstance(w.get("steps"), list)
             for s in w["steps"]]
    if not any(isinstance(w, dict) and isinstance(w.get("steps"), list) for w in windows):
        return None
    return sum(1 for s in steps if _GOOGLE_LIMITED.search(json.dumps(s, ensure_ascii=False)))


def measure(tasks_dir: Path, run: dict[str, Any]) -> dict[str, Any]:
    task_id = run["task_id"]
    prompt = PROMPTS_BY_ID.get(run["prompt"])
    task = _json(tasks_dir / f"{task_id}.task.json") or {}
    attempts = _json(tasks_dir / f"{task_id}.attempts.json")
    events = _json(tasks_dir / f"{task_id}.events.json") or {}
    actions = _json(tasks_dir / f"{task_id}.actions.json")
    answer = _json(tasks_dir / f"{task_id}.answer.json")
    windows = [_json(p) for p in sorted(tasks_dir.glob(f"{task_id}.transcript.*.json"),
                                        key=lambda p: int(p.name.split(".")[-2]))]

    state = task.get("state")
    finished = state in TERMINAL_STATES
    content = answer.get("content") if isinstance(answer, dict) else None
    types = [e.get("type") for e in events.get("events") or [] if isinstance(e, dict)]
    lines = [a for a in (actions or {}).get("actions") or [] if isinstance(a, dict)]
    cdp = [a for a in lines if a.get("kind") == "cdp"]
    forwarded = [a for a in cdp if a.get("outcome") != "refused"]
    started, completed = _time(task.get("started_at")), _time(task.get("completed_at"))

    row: dict[str, Any] = {
        **run,
        "state": state,
        "finished": finished,
        "result": result_value(content),
        "check": check(prompt, content) if prompt else False,
        # No action log is no live session: the agent never drove the gate.
        "live_browser": bool(forwarded),
        "cdp_actions": len(forwarded) if actions is not None else None,
        "cost_usd": _attempt_sum(attempts, "cost_usd"),
        "duration_s": (completed - started).total_seconds() if started and completed else None,
        "turns": _turns(task, answer),
        "input_tokens": _attempt_sum(attempts, "input_tokens"),
        "output_tokens": _attempt_sum(attempts, "output_tokens"),
        "cache_read_input_tokens": _attempt_sum(attempts, "cache_read_input_tokens"),
        "cache_creation_input_tokens": _attempt_sum(attempts, "cache_creation_input_tokens"),
        "handoffs_requested": types.count("human_handoff_requested") if events else None,
        "handoffs_returned": types.count("human_control_returned") if events else None,
        "cdp_method_refused": (sum(1 for a in cdp if a.get("outcome") == "refused")
                               if actions is not None else None),
        "google_sorry": _transcript_sorry(windows),
        "google_sorry_live": sorry_hits(actions)[0] if actions is not None else None,
    }
    if not finished:
        row["success"] = None
    else:
        row["success"] = bool(
            state == "SUCCEEDED" and row["check"] and row["live_browser"]
            and (not (prompt and prompt.handoff) or (row["handoffs_returned"] or 0) >= 1))
    return row


def read_runs(batch_dir: Path) -> list[dict[str, Any]]:
    """runs.tsv: label, tool, prompt, rep, task_id -- what the submit wrote."""
    path = batch_dir / "runs.tsv"
    runs = []
    if not path.exists():
        return runs
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) != 5 or not re.fullmatch(r"task_[a-z0-9]+", parts[4]):
            continue
        runs.append({"label": parts[0], "tool": parts[1], "prompt": parts[2],
                     "rep": int(parts[3]), "task_id": parts[4]})
    return runs


def collect(batch_dir: Path) -> list[dict[str, Any]]:
    return [measure(batch_dir / "tasks", run) for run in read_runs(batch_dir)]


# ---------------------------------------------------------------------------
# Per tool.


def _median(values: Iterable[Any]) -> float | None:
    present = [v for v in values if v is not None]
    return statistics.median(present) if present else None


def _total(values: Iterable[Any]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def summarize(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for tool in [t for t in TOOLS if any(r["tool"] == t for r in rows)]:
        mine = [r for r in rows if r["tool"] == tool]
        done = [r for r in mine if r["finished"]]
        prompts_passed = 0
        prompts_seen = 0
        for prompt in PROMPTS:
            reps = [r for r in done if r["prompt"] == prompt.id]
            if not reps:
                continue
            prompts_seen += 1
            # A prompt is done when MOST of its repeats succeeded.
            if sum(1 for r in reps if r["success"]) * 2 > len(reps):
                prompts_passed += 1
        refused = [r["cdp_method_refused"] for r in done if r["cdp_method_refused"] is not None]
        out[tool] = {
            "runs": len(done),
            "unfinished": len(mine) - len(done),
            "successes": sum(1 for r in done if r["success"]),
            "success_rate": (sum(1 for r in done if r["success"]) / len(done)) if done else None,
            "live_browser_runs": sum(1 for r in done if r["live_browser"]),
            "prompts_passed": prompts_passed,
            "prompts_seen": prompts_seen,
            "median_cost_usd": _median(r["cost_usd"] for r in done),
            "total_cost_usd": _total(r["cost_usd"] for r in done),
            "median_turns": _median(r["turns"] for r in done),
            "median_duration_s": _median(r["duration_s"] for r in done),
            "input_tokens": _total(r["input_tokens"] for r in done),
            "output_tokens": _total(r["output_tokens"] for r in done),
            "cache_read_input_tokens": _total(r["cache_read_input_tokens"] for r in done),
            "cache_creation_input_tokens": _total(r["cache_creation_input_tokens"] for r in done),
            "handoffs_requested": _total(r["handoffs_requested"] for r in done),
            "handoffs_returned": _total(r["handoffs_returned"] for r in done),
            "cdp_method_refused": sum(refused) if refused else None,
            "cdp_method_refused_measured": len(refused),
            "google_sorry": _total(r["google_sorry"] for r in done),
        }
    return out


def verdict(summary: dict[str, dict[str, Any]], challenger: str = "dev-browser") -> str:
    """The pass bar (docs/browser-bench.md) for `challenger` against
    Playwright MCP, as data says it. The owner decides."""
    mcp, devb = summary.get(BASELINE), summary.get(challenger)
    if not mcp or not devb or not mcp["runs"] or not devb["runs"]:
        return "NOT DECIDED: both arms need finished runs"
    if devb["prompts_seen"] < len(PROMPTS):
        return f"NOT DECIDED: {challenger} finished {devb['prompts_seen']}/{len(PROMPTS)} prompts"
    missing = [k for k in ("median_cost_usd", "median_turns")
               if mcp[k] is None or devb[k] is None]
    if missing:
        return f"NOT DECIDED: {', '.join(missing)} not measured on both arms"
    reasons = []
    if devb["prompts_passed"] < 8:
        reasons.append(f"{challenger} passed {devb['prompts_passed']}/10 prompts in the live "
                       "browser (bar: >= 8/10)")
    if devb["success_rate"] < mcp["success_rate"]:
        reasons.append(f"{challenger} is less reliable than Playwright MCP")
    if devb["median_cost_usd"] >= mcp["median_cost_usd"]:
        reasons.append(f"{challenger}'s median $/task is not lower")
    if devb["median_turns"] >= mcp["median_turns"]:
        reasons.append(f"{challenger}'s median turns are not lower")
    if reasons:
        return "KEEP PLAYWRIGHT MCP: " + "; ".join(reasons)
    return f"BAR MET: {challenger} may become the default (the owner's decision)"


def challengers(summary: dict[str, dict[str, Any]]) -> list[str]:
    """The arms measured against Playwright MCP, in TOOLS order; dev-browser
    always, so a batch without it still says it was not decided."""
    return [t for t in TOOLS if t != BASELINE and (t in summary or t == "dev-browser")]


# ---------------------------------------------------------------------------
# Rendering.

TSV_COLUMNS = ("label", "tool", "prompt", "rep", "task_id", "state", "success", "check",
               "live_browser", "result", "cost_usd", "duration_s", "turns", "input_tokens",
               "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
               "handoffs_requested", "handoffs_returned", "cdp_method_refused",
               "google_sorry", "google_sorry_live")


def _cell(value: Any) -> str:
    if value is None:
        return "not measured"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value).replace("\t", " ").replace("\n", " ")


def render_tsv(rows: list[dict[str, Any]]) -> str:
    lines = ["\t".join(TSV_COLUMNS)]
    for row in rows:
        cells = []
        for column in TSV_COLUMNS:
            value = row.get(column)
            if column == "success" and not row.get("finished"):
                cells.append("unfinished")
            elif column == "state" and value is None:
                cells.append("unread")
            else:
                cells.append(_cell(value))
        lines.append("\t".join(cells))
    return "\n".join(lines) + "\n"


def _usd(value: Any) -> str:
    return "not measured" if value is None else f"${value:.2f}"


def render_markdown(summary: dict[str, dict[str, Any]], rows: list[dict[str, Any]]) -> str:
    out = ["# Browser benchmark: Playwright MCP vs " + " and ".join(challengers(summary)), "",
           "| tool | runs | unfinished | successes | prompts passed | live-browser runs "
           "| median $/task | total $ | median turns | median duration s | input tok "
           "| output tok | cache read tok | cache write tok | hand-offs requested "
           "| hand-offs returned | cdp_method_refused | Google 429/sorry |",
           "|" + "---|" * 18]
    for tool, s in summary.items():
        refused = (f"{s['cdp_method_refused']} ({s['cdp_method_refused_measured']} run(s) "
                   "measured)" if s["cdp_method_refused"] is not None else "not measured")
        out.append("| " + " | ".join([
            tool, str(s["runs"]), str(s["unfinished"]), str(s["successes"]),
            f"{s['prompts_passed']}/{s['prompts_seen']}", str(s["live_browser_runs"]),
            _usd(s["median_cost_usd"]), _usd(s["total_cost_usd"]), _cell(s["median_turns"]),
            _cell(s["median_duration_s"]), _cell(s["input_tokens"]), _cell(s["output_tokens"]),
            _cell(s["cache_read_input_tokens"]), _cell(s["cache_creation_input_tokens"]),
            _cell(s["handoffs_requested"]), _cell(s["handoffs_returned"]), refused,
            _cell(s["google_sorry"])]) + " |")
    out.append("")
    for challenger in challengers(summary):
        label_ = "" if challenger == "dev-browser" else f" ({challenger})"
        out.append(f"**Verdict{label_}:** {verdict(summary, challenger)}")
    out += ["", "## Per prompt", "",
            "| prompt | kind | " + " | ".join(summary) + " |",
            "|---|---|" + "---|" * len(summary)]
    for prompt in PROMPTS:
        cells = []
        for tool in summary:
            reps = [r for r in rows if r["tool"] == tool and r["prompt"] == prompt.id]
            if not reps:
                cells.append("-")
                continue
            ok = sum(1 for r in reps if r["success"])
            pending = sum(1 for r in reps if not r["finished"])
            cells.append(f"{ok}/{len(reps) - pending}" + (f" ({pending} unfinished)"
                                                          if pending else ""))
        out.append(f"| `{prompt.id}` | {prompt.kind} | " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------


def _csv(value: str | None) -> list[str] | None:
    return [v for v in value.split(",") if v] if value else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="browser_bench.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="the runs, one `label tool prompt rep [body]` line each")
    p.add_argument("--batch", required=True)
    p.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    p.add_argument("--prompts")
    p.add_argument("--tools")
    p.add_argument("--usd-per-task", type=float, default=MEDIAN_TASK_USD)
    p.add_argument("--bodies", type=Path, help="write each request body to DIR/<label>.json")
    c = sub.add_parser("collect", help="the fetched tasks of a batch -> TSV and markdown")
    c.add_argument("--dir", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.cmd == "plan":
        try:
            runs = plan(args.batch, prompt_ids=_csv(args.prompts), tools=_csv(args.tools),
                        repeats=args.repeats)
        except ValueError as exc:
            print(f"browser_bench: {exc}", file=sys.stderr)
            return 2
        if args.bodies:
            args.bodies.mkdir(parents=True, exist_ok=True)
        for run in runs:
            cells = [run.label, run.tool, run.prompt_id, str(run.rep)]
            if args.bodies:
                path = args.bodies / f"{run.label}.json"
                path.write_text(json.dumps(run.body), encoding="utf-8")
                cells.append(str(path))
            print("\t".join(cells))
        print(f"{len(runs)} run(s) planned; estimated ${estimate_usd(len(runs), args.usd_per_task):.2f}"
              f" ({len(runs)} x ${args.usd_per_task:.2f} per task)", file=sys.stderr)
        return 0

    rows = collect(args.dir)
    print(f"{len(rows)} run(s) read", file=sys.stderr)
    if not rows:
        print("NOTHING READ: no run in runs.tsv. That is not a result.", file=sys.stderr)
        return 2
    summary = summarize(rows)
    (args.dir / "results.tsv").write_text(render_tsv(rows), encoding="utf-8")
    for tool in summary:
        (args.dir / f"results-{tool}.tsv").write_text(
            render_tsv([r for r in rows if r["tool"] == tool]), encoding="utf-8")
    markdown = render_markdown(summary, rows)
    (args.dir / "summary.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
