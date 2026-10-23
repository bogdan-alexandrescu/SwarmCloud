"""The merge train (owner decision 2026-10-14, "A1 Merge train": keep main a
tested tree) -- scripts/merge-train.sh and .github/workflows/merge-train.yml.

WHY. Ruleset 24160219 (`main-protection`) has strict up-to-date on, so a green
`ready` pull request waits for its branch to be brought up to date and CI to
run again: a median 17 minutes after green, p90 173, and 2.93 CI runs per
pull request. GitHub's merge queue is refused on a user-owned repository. The
train tests several `ready` pull requests together, on main plus each one
squashed in order, and lands them one squash per pull request.

What is held here, all offline (no network, no credentials, bash + jq + git):

* `select`: which open pull requests may board -- green at head, `ready`,
  mergeable, not a draft, not a fork, aimed at main, not a train itself, not
  the worker's `[swarm] task_` placeholder title;
* `judge`: a train head's checks read as green, red (with the failing job's
  link) or pending;
* `plan`: bisection by halves, and ejection of a lone culprit;
* `compose`: the train is main plus one squash commit per pull request, in
  order, titled `<title> (#N)`, and a pull request that conflicts is dropped
  rather than failing the train;
* the state block a train pull request carries, and its refusal of a value
  that is not what the train wrote;
* the workflow's triggers, concurrency, permissions and credential;
* auto-merge.yml's gate leaves a `ready` pull request to the train when
  vars.MERGE_TRAIN is `on`, and is unchanged otherwise;
* the runbook carries the ruleset change and says it is not applied.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from .test_auto_merge_workflow import FAKE_GH, PROTECTED, _outputs, _step

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "merge-train.sh"
WORKFLOW = REPO / ".github" / "workflows" / "merge-train.yml"
AUTO_MERGE = REPO / ".github" / "workflows" / "auto-merge.yml"
RUNBOOK = REPO / "docs" / "runbooks" / "merge-train.md"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "merge_train"

REQUIRED = ["ci-gate", "secret scan"]


def _need(*tools: str) -> None:
    for tool in tools:
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is required")


def _run(*args: str, cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    _need("bash", "jq")
    full_env = dict(os.environ)
    full_env.update(env or {})
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True, text=True, timeout=60, check=False, cwd=cwd, env=full_env,
    )


def _required_file(tmp_path: Path, names: list[str] = REQUIRED) -> Path:
    path = tmp_path / "required.txt"
    path.write_text("".join(f"{n}\n" for n in names))
    return path


# --------------------------------------------------------------------------
# the script's shape
# --------------------------------------------------------------------------


def test_the_script_is_executable_strict_and_sources_common():
    text = SCRIPT.read_text()
    assert os.access(SCRIPT, os.X_OK), "scripts/merge-train.sh is not executable"
    assert text.startswith("#!/usr/bin/env bash\n")
    effective = [
        line for line in text.splitlines()[1:]
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert effective[0] == "set -euo pipefail", effective[0]
    assert re.search(r'^source "\$\(cd -- "\$\(dirname -- "\$\{BASH_SOURCE\[0\]\}"\)" && pwd\)/lib/common\.sh"$',
                     text, flags=re.M)


def test_the_script_uses_nothing_bash_3_2_lacks():
    code = "\n".join(
        line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#")
    )
    for pattern in (r"\bmapfile\b", r"\breadarray\b", r"\bdeclare -A\b", r"\blocal -A\b",
                    r"\$\{[A-Za-z_]+,,\}", r"\$\{[A-Za-z_]+\^\^\}", r"\bcoproc\b"):
        assert not re.search(pattern, code), f"bash 3.2 has no {pattern}"


# --------------------------------------------------------------------------
# select: who may board
# --------------------------------------------------------------------------


def _select(tmp_path: Path, prs: Path = FIXTURES / "prs.json", required: list[str] = REQUIRED):
    proc = _run("select", "--prs", str(prs), "--required", str(_required_file(tmp_path, required)))
    assert proc.returncode == 0, proc.stderr
    return [line.split("\t") for line in proc.stdout.splitlines() if line]


def test_select_boards_only_green_ready_mergeable_same_repo_pull_requests_oldest_first(tmp_path):
    rows = _select(tmp_path)
    assert [r[0] for r in rows] == ["101", "114", "120"], rows


def test_select_carries_the_head_it_judged_and_a_one_line_title(tmp_path):
    rows = {r[0]: r for r in _select(tmp_path)}
    assert rows["101"][1] == "1010000000000000000000000000000000000001"
    assert rows["101"][2] == "The docs say why the reconciler parks"
    # A tab in a title would split the record: it is flattened to a space.
    assert rows["114"][2] == "Tabs in a title are flattened"
    assert all(len(r) == 3 for r in rows.values())


@pytest.mark.parametrize("number, why", [
    ("102", "a draft"),
    ("103", "a fork"),
    ("104", "no ready label (a label that merely contains the word does not count)"),
    ("105", "conflicting"),
    ("106", "mergeability unknown"),
    ("107", "a check that is not required failed"),
    ("108", "a required check never reported"),
    ("109", "a required check still running"),
    ("110", "the worker's placeholder title, any case, leading whitespace"),
    ("111", "a train branch"),
    ("112", "another base"),
    ("113", "a required status context errored"),
])
def test_select_leaves_out(tmp_path, number: str, why: str):
    assert number not in [r[0] for r in _select(tmp_path)], why


def test_select_judges_against_the_required_list_it_is_given(tmp_path):
    """The control for 108: with only ci-gate required, a pull request that never
    reported `secret scan` boards -- so 108's absence above is the missing check."""
    assert "108" in [r[0] for r in _select(tmp_path, required=["ci-gate"])]


def test_select_can_board_a_bisection_s_pull_request_github_has_not_rechecked(tmp_path):
    """Right after a train lands, GitHub recomputes every open pull request's
    mergeability and reports UNKNOWN meanwhile. A bisection's held-back half is
    boarded then, by name, and compose finds a real conflict anyway; a
    CONFLICTING one still stays out."""
    proc = _run("select", "--prs", str(FIXTURES / "prs.json"),
                "--required", str(_required_file(tmp_path)), "--allow-unknown-mergeable")
    assert proc.returncode == 0, proc.stderr
    numbers = [line.split("\t")[0] for line in proc.stdout.splitlines() if line]
    assert numbers == ["101", "106", "114", "120"]


def test_select_with_no_open_pull_requests_prints_nothing(tmp_path):
    empty = tmp_path / "none.json"
    empty.write_text("[]")
    assert _select(tmp_path, prs=empty) == []


def test_select_refuses_an_empty_required_list(tmp_path):
    """No required check means `green` would mean nothing: fail closed."""
    proc = _run("select", "--prs", str(FIXTURES / "prs.json"),
                "--required", str(_required_file(tmp_path, [])))
    assert proc.returncode != 0


# --------------------------------------------------------------------------
# judge: a train head is green, red or pending
# --------------------------------------------------------------------------


def _judge(tmp_path: Path, check_runs: list[dict], statuses: list[dict] | None = None,
           required: list[str] = REQUIRED, workflow_runs: list[dict] | None = None):
    cr = tmp_path / "check-runs.json"
    cr.write_text(json.dumps({"total_count": len(check_runs), "check_runs": check_runs}))
    st = tmp_path / "status.json"
    st.write_text(json.dumps({"state": "pending", "statuses": statuses or []}))
    extra: list[str] = []
    if workflow_runs is not None:
        wf = tmp_path / "workflow-runs.json"
        wf.write_text(json.dumps({"total_count": len(workflow_runs), "workflow_runs": workflow_runs}))
        extra = ["--workflow-runs", str(wf)]
    proc = _run("judge", "--check-runs", str(cr), "--statuses", str(st),
                "--required", str(_required_file(tmp_path, required)), *extra)
    assert proc.returncode == 0, proc.stderr
    lines = [line.split("\t") for line in proc.stdout.splitlines() if line]
    verdict = [line[1] for line in lines if line[0] == "verdict"]
    assert len(verdict) == 1, proc.stdout
    failed = [(line[1], line[2]) for line in lines if line[0] == "failed"]
    return verdict[0], failed


def _cr(name: str, status: str = "completed", conclusion: str | None = "success",
        url: str = "https://github.com/o/r/actions/runs/1/job/2") -> dict:
    return {"name": name, "status": status, "conclusion": conclusion, "html_url": url}


def test_judge_green_when_every_required_check_passed(tmp_path):
    verdict, failed = _judge(tmp_path, [_cr("ci-gate"), _cr("secret scan", conclusion="skipped"),
                                        _cr("Trivy", status="in_progress", conclusion=None)])
    assert (verdict, failed) == ("green", [])


def test_judge_counts_a_commit_status_as_a_check(tmp_path):
    verdict, _ = _judge(tmp_path, [_cr("ci-gate")],
                        statuses=[{"context": "secret scan", "state": "success", "target_url": "u"}])
    assert verdict == "green"


def test_judge_pending_while_a_required_check_runs_or_has_not_reported(tmp_path):
    assert _judge(tmp_path, [_cr("ci-gate", status="queued", conclusion=None),
                             _cr("secret scan")])[0] == "pending"
    assert _judge(tmp_path, [_cr("ci-gate")])[0] == "pending"


def test_judge_red_names_the_failing_job_with_its_link(tmp_path):
    url = "https://github.com/bogdan-alexandrescu/SwarmCloud/actions/runs/77/job/88"
    verdict, failed = _judge(tmp_path, [
        _cr("ci-gate", status="in_progress", conclusion=None),
        _cr("secret scan"),
        _cr("python-unit (3)", conclusion="failure", url=url),
    ])
    assert verdict == "red"
    assert failed == [("python-unit (3)", url)]


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "cancelled", "action_required", "startup_failure"])
def test_judge_red_on_any_failed_conclusion_required_or_not(tmp_path, conclusion: str):
    assert _judge(tmp_path, [_cr("ci-gate"), _cr("secret scan"), _cr("Trivy", conclusion=conclusion)])[0] == "red"


def test_judge_red_on_a_failed_commit_status(tmp_path):
    verdict, failed = _judge(tmp_path, [_cr("ci-gate")],
                             statuses=[{"context": "secret scan", "state": "error", "target_url": "https://x/y"}])
    assert verdict == "red"
    assert failed == [("secret scan", "https://x/y")]


# CHECK-RULE-2 (BGD, 2026-10-22, both answers): the judge decides a check by
# the latest run (highest id) of EACH event that ran it, as the worker's
# merge step, swarm-api and auto-merge.yml's gate 5 do. The event is the one
# that made the run's check suite, from `--workflow-runs` (the Actions runs at
# the head); without it each check suite is its own group, as gate 5 groups.


def _run_of(run_id: int, name: str, suite: int, conclusion: str | None = "success",
            status: str = "completed") -> dict:
    return {"id": run_id, "name": name, "status": status, "conclusion": conclusion,
            "check_suite": {"id": suite},
            "html_url": f"https://github.com/o/r/actions/runs/{run_id}/job/{run_id}"}


def _events(**suites: str) -> list[dict]:
    return [{"id": 900 + int(suite), "check_suite_id": int(suite), "event": event}
            for suite, event in suites.items()]


def test_judge_a_required_check_re_run_green_in_its_event_is_green(tmp_path):
    runs = [_run_of(1, "ci-gate", 101, "failure"), _run_of(2, "ci-gate", 103), _run_of(3, "secret scan", 101)]
    verdict, failed = _judge(tmp_path, runs, workflow_runs=_events(**{"101": "push", "103": "push"}))
    # MUTATION: judge every run instead of each event's latest -> "red".
    assert (verdict, failed) == ("green", [])


def test_judge_without_workflow_runs_judges_each_check_suite_on_its_own(tmp_path):
    runs = [_run_of(1, "ci-gate", 101, "failure"), _run_of(2, "ci-gate", 102), _run_of(3, "secret scan", 101)]
    verdict, failed = _judge(tmp_path, runs)
    # MUTATION: group by name alone when no event is known -> "green".
    assert verdict == "red"
    assert failed == [("ci-gate", runs[0]["html_url"])]
    # The control: the same suites, told they are one event, go green.
    assert _judge(tmp_path, runs, workflow_runs=_events(**{"101": "push", "102": "push"}))[0] == "green"


#: A check nothing requires, by (run id, suite, status, conclusion); suites
#: 101 and 103 are push, 102 pull_request. (case, runs, verdict)
JUDGE_OTHER = [
    ("push failure then success", [(1, 101, "completed", "failure"), (2, 103, "completed", "success")], "green"),
    ("push in_progress then success", [(1, 101, "in_progress", None), (2, 103, "completed", "success")], "green"),
    ("push success, pull_request failure", [(1, 101, "completed", "success"), (2, 102, "completed", "failure")], "red"),
    ("push success then failure", [(1, 101, "completed", "success"), (2, 103, "completed", "failure")], "red"),
]


@pytest.mark.parametrize(("case", "runs", "verdict"), JUDGE_OTHER, ids=[c[0] for c in JUDGE_OTHER])
def test_judge_a_check_nothing_requires_by_the_latest_run_of_each_event(tmp_path, case, runs, verdict):
    checks = [_run_of(9, "ci-gate", 101), _run_of(8, "secret scan", 101)]
    checks += [_run_of(i, "terraform", suite, conclusion, status) for i, suite, status, conclusion in runs]
    got, _failed = _judge(tmp_path, checks, workflow_runs=_events(
        **{"101": "push", "102": "pull_request", "103": "push"}))
    # MUTATION: judge every run instead of each event's latest -> the first
    # case is "red". MUTATION: take the newest run whatever its event -> the
    # pull_request case's push run could outvote it.
    assert got == verdict, case


def test_the_tick_passes_the_actions_runs_when_a_check_ran_twice():
    """MUTATION: keep calling the judge with --check-runs alone -> a train's
    head is judged each check suite on its own, and a push re-run green in a
    new suite still reads red."""
    text = SCRIPT.read_text()
    start = text.index("\nhandle_train() {")
    tick = text[start:text.index("\n}\n", start)]
    assert "actions/runs?head_sha=${head}" in tick
    assert re.search(r"cmd_judge --check-runs \"\$\{cr\}\".*--workflow-runs", tick, re.S), tick[:200]


# --------------------------------------------------------------------------
# plan: bisect by halves, eject a lone culprit
# --------------------------------------------------------------------------


def _plan(verdict: str, train: str, suspects: str = "") -> dict[str, str]:
    proc = _run("plan", "--verdict", verdict, "--train", train, "--suspects", suspects)
    assert proc.returncode == 0, proc.stderr
    out = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    assert set(out) == {"eject", "next", "suspects"}, proc.stdout
    return out


def test_a_red_train_of_four_is_halved():
    assert _plan("red", "101 102 103 104") == {"eject": "", "next": "101 102", "suspects": "103 104"}


def test_a_red_train_of_three_keeps_the_larger_half_first():
    assert _plan("red", "101 102 103") == {"eject": "", "next": "101 102", "suspects": "103"}


def test_halving_keeps_the_suspects_held_back_by_an_earlier_halving():
    assert _plan("red", "101 102", "103 104") == {"eject": "", "next": "101", "suspects": "102 103 104"}


def test_a_red_train_of_one_ejects_it_and_tries_the_suspects_next():
    assert _plan("red", "101", "102 103 104") == {"eject": "101", "next": "102 103 104", "suspects": ""}


def test_a_red_train_of_one_with_no_suspects_ejects_it_and_ends_the_bisection():
    assert _plan("red", "101") == {"eject": "101", "next": "", "suspects": ""}


def test_a_green_bisection_train_hands_on_to_the_held_back_half():
    assert _plan("green", "101 102", "103 104") == {"eject": "", "next": "103 104", "suspects": ""}


def test_a_green_train_with_no_suspects_plans_nothing():
    assert _plan("green", "101 102 103 104") == {"eject": "", "next": "", "suspects": ""}


@pytest.mark.parametrize("bad", ["101 x", "101;rm", "-1", ""])
def test_plan_refuses_a_train_that_is_not_pull_request_numbers(bad: str):
    assert _run("plan", "--verdict", "red", "--train", bad, "--suspects", "").returncode != 0


# --------------------------------------------------------------------------
# compose: main plus one squash per pull request, in order
# --------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        "PATH": os.environ["PATH"], "HOME": str(repo),
    }
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


@pytest.fixture
def history(tmp_path: Path):
    """main with one file, and four pull request heads branched from it:
    101 adds a.txt; 102 rewrites shared.txt; 103 rewrites shared.txt the other
    way (conflicts with 102, not with main); 104 adds d.txt in two commits."""
    _need("git", "bash", "jq")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "shared.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    heads = {}

    def branch(n: int, edits: list[dict[str, str]]):
        _git(repo, "checkout", "-q", "-b", f"pr{n}", base)
        for i, files in enumerate(edits):
            for name, content in files.items():
                (repo / name).write_text(content)
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", f"pr{n} commit {i}")
        heads[n] = _git(repo, "rev-parse", "HEAD")

    branch(101, [{"a.txt": "a\n"}])
    branch(102, [{"shared.txt": "from 102\n"}])
    branch(103, [{"shared.txt": "from 103\n"}])
    branch(104, [{"d.txt": "d1\n"}, {"d.txt": "d2\n"}])
    _git(repo, "checkout", "-q", "main")
    return repo, base, heads


def _compose(tmp_path: Path, repo: Path, base: str, rows: list[tuple[int, str, str]]):
    prs = tmp_path / "train.tsv"
    prs.write_text("".join(f"{n}\t{sha}\t{title}\n" for n, sha, title in rows))
    proc = _run("compose", "--repo", str(repo), "--base", base, "--prs", str(prs))
    assert proc.returncode == 0, proc.stderr
    lines = [line.split("\t") for line in proc.stdout.splitlines() if line]
    included = [int(line[1]) for line in lines if line[0] == "included"]
    dropped = [int(line[1]) for line in lines if line[0] == "dropped"]
    head = [line[1] for line in lines if line[0] == "head"]
    assert len(head) == 1, proc.stdout
    return included, dropped, head[0]


def test_compose_squashes_each_pull_request_in_order_under_its_title(tmp_path, history):
    repo, base, heads = history
    included, dropped, head = _compose(tmp_path, repo, base, [
        (101, heads[101], "The first change"),
        (104, heads[104], "The fourth change, in two commits"),
    ])
    assert (included, dropped) == ([101, 104], [])
    subjects = _git(repo, "log", "--format=%s", f"{base}..{head}").splitlines()
    assert subjects == ["The fourth change, in two commits (#104)", "The first change (#101)"]
    # One commit per pull request, each with ONE parent: squashed, not merged.
    for sha in _git(repo, "rev-list", f"{base}..{head}").splitlines():
        assert len(_git(repo, "rev-list", "--parents", "-n", "1", sha).split()) == 2
    assert _git(repo, "show", f"{head}:a.txt") == "a"
    assert _git(repo, "show", f"{head}:d.txt") == "d2"
    assert _git(repo, "merge-base", "--is-ancestor", base, head) == ""


def test_compose_drops_a_pull_request_that_conflicts_with_one_ahead_of_it(tmp_path, history):
    repo, base, heads = history
    included, dropped, head = _compose(tmp_path, repo, base, [
        (102, heads[102], "Two"),
        (103, heads[103], "Three"),
        (101, heads[101], "One"),
    ])
    assert (included, dropped) == ([102, 101], [103])
    assert _git(repo, "show", f"{head}:shared.txt") == "from 102"
    assert _git(repo, "log", "--format=%s", f"{base}..{head}").splitlines() == ["One (#101)", "Two (#102)"]


def test_compose_leaves_the_repository_s_own_checkout_untouched(tmp_path, history):
    repo, base, heads = history
    before = _git(repo, "rev-parse", "HEAD")
    _compose(tmp_path, repo, base, [(102, heads[102], "Two"), (103, heads[103], "Three")])
    assert _git(repo, "rev-parse", "HEAD") == before
    assert _git(repo, "status", "--porcelain") == ""
    assert (repo / "shared.txt").read_text() == "base\n"


def test_compose_with_every_pull_request_dropped_reports_the_base_as_head(tmp_path, history):
    repo, base, heads = history
    included, dropped, head = _compose(tmp_path, repo, base, [(101, "f" * 40, "Not fetched")])
    assert (included, dropped, head) == ([], [101], base)


def test_compose_takes_a_title_with_shell_metacharacters_as_text(tmp_path, history):
    repo, base, heads = history
    title = 'It handles $(touch pwned) and `x` and "quotes"'
    _, _, head = _compose(tmp_path, repo, base, [(101, heads[101], title)])
    assert _git(repo, "log", "-1", "--format=%s", head) == f"{title} (#101)"
    assert not (tmp_path / "pwned").exists()


# --------------------------------------------------------------------------
# the state a train pull request carries
# --------------------------------------------------------------------------

STATE = {
    "id": "18000000001-1",
    "base": "a" * 40,
    "head": "b" * 40,
    "started": "1760400000",
    "dry_run": "false",
    "prs": f"101:{'1' * 40} 104:{'4' * 40}",
    "suspects": "102 103",
}


def _render(state: dict[str, str]) -> subprocess.CompletedProcess:
    return _run("state-render", *[f"{k}={v}" for k, v in state.items()])


def test_state_round_trips_through_a_pull_request_body(tmp_path):
    proc = _render(STATE)
    assert proc.returncode == 0, proc.stderr
    body = tmp_path / "body.md"
    body.write_text("A train.\n\nSome prose a person wrote.\n\n" + proc.stdout)
    for key, value in STATE.items():
        got = _run("state-get", "--body", str(body), "--key", key)
        assert got.returncode == 0, got.stderr
        assert got.stdout.rstrip("\n") == value, key


def test_state_allows_no_suspects(tmp_path):
    proc = _render({**STATE, "suspects": ""})
    body = tmp_path / "body.md"
    body.write_text(proc.stdout)
    got = _run("state-get", "--body", str(body), "--key", "suspects")
    assert (got.returncode, got.stdout.strip()) == (0, "")


@pytest.mark.parametrize("key, value", [
    ("base", "main"),
    ("head", "b" * 39),
    ("started", "yesterday"),
    ("dry_run", "maybe"),
    ("prs", "101"),
    ("prs", f"101:{'1' * 40};curl x"),
    ("suspects", "102,$(id)"),
    ("id", "../main"),
])
def test_state_get_refuses_a_value_the_train_never_writes(tmp_path, key: str, value: str):
    rendered = _render(STATE).stdout
    body = tmp_path / "body.md"
    body.write_text(re.sub(rf"^{key}=.*$", f"{key}={value}", rendered, flags=re.M))
    assert _run("state-get", "--body", str(body), "--key", key).returncode != 0


def test_state_get_refuses_a_body_with_no_state(tmp_path):
    body = tmp_path / "body.md"
    body.write_text("Just prose.\n")
    assert _run("state-get", "--body", str(body), "--key", "head").returncode != 0


# --------------------------------------------------------------------------
# the workflow
# --------------------------------------------------------------------------


def _workflow(path: Path) -> dict:
    data = yaml.safe_load(path.read_text())
    # YAML 1.1 reads a bare `on:` key as the boolean True.
    if True in data:
        data["on"] = data.pop(True)
    return data


@pytest.fixture(scope="module")
def train_workflow() -> dict:
    return _workflow(WORKFLOW)


def test_the_train_runs_every_five_minutes_and_on_demand(train_workflow):
    on = train_workflow["on"]
    assert on["schedule"] == [{"cron": "*/5 * * * *"}]
    assert "workflow_dispatch" in on
    assert on["workflow_dispatch"]["inputs"]["dry_run"]["type"] == "boolean"


def test_a_finished_train_ci_run_wakes_the_train_without_waiting_for_the_schedule(train_workflow):
    assert set(train_workflow["on"]["workflow_run"]["workflows"]) == {"application", "security"}
    job = train_workflow["jobs"]["tick"]
    assert "startsWith(github.event.workflow_run.head_branch, 'train/')" in job["if"]
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in job["if"]


def test_the_schedule_does_nothing_unless_the_train_is_on(train_workflow):
    cond = train_workflow["jobs"]["tick"]["if"]
    assert "github.event_name == 'schedule' && vars.MERGE_TRAIN == 'on'" in cond


def test_one_train_at_a_time_and_a_running_one_is_never_cancelled(train_workflow):
    # On the job: a workflow-level group would let every skipped
    # `workflow_run` run (any branch's CI finishing) cancel a pending tick.
    assert "concurrency" not in train_workflow
    assert train_workflow["jobs"]["tick"]["concurrency"] == {"group": "merge-train", "cancel-in-progress": False}


def test_the_train_lands_with_the_merge_app_token_and_never_a_pat(train_workflow):
    text = WORKFLOW.read_text()
    assert set(re.findall(r"secrets\.([A-Z_]+)", text)) == {"MERGE_APP_PRIVATE_KEY"}
    steps = train_workflow["jobs"]["tick"]["steps"]
    mint = [s for s in steps if str(s.get("uses", "")).startswith("actions/create-github-app-token@")]
    assert len(mint) == 1
    assert mint[0]["with"]["app-id"] == "${{ vars.MERGE_APP_ID }}"
    assert mint[0]["with"]["private-key"] == "${{ secrets.MERGE_APP_PRIVATE_KEY }}"
    run = [s for s in steps if "merge-train.sh tick" in str(s.get("run", ""))]
    assert len(run) == 1
    assert run[0]["env"]["APP_TOKEN"] == f"${{{{ steps.{mint[0]['id']}.outputs.token }}}}"
    assert run[0]["env"]["GH_TOKEN"] == "${{ github.token }}"


def test_the_train_runs_main_s_scripts_and_no_pull_request_s_code(train_workflow):
    steps = train_workflow["jobs"]["tick"]["steps"]
    checkouts = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert len(checkouts) == 1
    assert checkouts[0]["with"]["ref"] == "${{ github.event.repository.default_branch }}"
    assert checkouts[0]["with"]["persist-credentials"] is False


def test_the_github_token_asks_only_for_what_the_train_does_with_it(train_workflow):
    assert train_workflow["permissions"] == {}
    # actions: read -- the Actions runs at a train's head, whose events the
    # judge groups a check's runs by (CHECK-RULE-2). MUTATION: drop it -> the
    # read 403s and the tick judges each check suite on its own.
    assert train_workflow["jobs"]["tick"]["permissions"] == {
        "contents": "read",
        "checks": "read",
        "actions": "read",
        "statuses": "write",
        "pull-requests": "write",
    }


def test_the_workflow_never_expands_an_expression_inside_run(train_workflow):
    for step in train_workflow["jobs"]["tick"]["steps"]:
        assert "${{" not in str(step.get("run", "")), step.get("name")


def test_application_yml_is_not_touched_by_the_train():
    """The train is a draft pull request from `train/<id>`, so application.yml,
    security.yml and terraform.yml run their `pull_request` jobs on it: every
    check the ruleset requires, and never `build`, which mints a Google identity
    the pool refuses to any ref but main. A `push: train/**` trigger would run
    `build` on the train ref and fail every train (terraform/bootstrap/wif.tf)."""
    on = _workflow(REPO / ".github" / "workflows" / "application.yml")["on"]
    # `swarm/**` runs the tests on a lane push (LANE CI-EARLY), and `build`
    # is held to main there by its own `if:` (test_ci_early.py).
    assert on["push"]["branches"] == ["main", "swarm/**"]
    assert not any(b.startswith("train/") for b in on["push"]["branches"])
    assert "pull_request" in on


# --------------------------------------------------------------------------
# auto-merge.yml leaves `ready` to the train when it is on
# --------------------------------------------------------------------------


@pytest.fixture
def gate(tmp_path: Path):
    _need("bash", "jq")
    script = _step(_workflow(AUTO_MERGE)["jobs"]["enable"], "gate")["run"]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "gh"
    fake.write_text(FAKE_GH)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    def run(merge_train: str | None, event_name: str = "pull_request_target"):
        files = {
            "branch": PROTECTED,
            "rules": [],
            "check-runs": {"check_runs": [{"name": "shellcheck", "status": "completed", "conclusion": "success"}]},
        }
        paths = {}
        for name, content in files.items():
            paths[name] = tmp_path / f"{name}.json"
            paths[name].write_text(json.dumps(content))
        for name in ("gh.log", "comments.md", "summary.md", "output.txt"):
            (tmp_path / name).write_text("")
        env = {
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "GH_REPO": "bogdan-alexandrescu/SwarmCloud",
            "EVENT_NAME": event_name,
            "PR_NUMBER": "4242",
            "PR_TITLE": "A fact-style headline",
            "BASE_REF": "main",
            "HEAD_SHA": "0000000000000000000000000000000000abcd",
            "MERGE_APP_ID": "123456",
            "HAS_MERGE_APP_KEY": "true",
            "DEPENDENCY_REFUSAL": "",
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
            "GITHUB_OUTPUT": str(tmp_path / "output.txt"),
            "FAKE_GH_LOG": str(tmp_path / "gh.log"),
            "FAKE_GH_BRANCH": str(paths["branch"]),
            "FAKE_GH_CHECK_RUNS": str(paths["check-runs"]),
            "FAKE_GH_RULES": str(paths["rules"]),
            "FAKE_GH_COMMENTS": str(tmp_path / "comments.md"),
        }
        if merge_train is not None:
            env["MERGE_TRAIN"] = merge_train
        proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True,
                              timeout=30, check=False)
        return proc, _outputs(tmp_path / "output.txt"), (tmp_path / "comments.md").read_text()

    return run


@pytest.mark.parametrize("value", [None, "", "off", "ON ", "true"])
def test_with_the_train_off_a_green_gate_still_says_merge(gate, value):
    proc, outputs, _ = gate(value)
    assert proc.returncode == 0, proc.stderr
    assert outputs.get("decision") == "merge"


def test_with_the_train_on_a_green_gate_leaves_the_pull_request_to_the_train(gate):
    proc, outputs, comments = gate("on")
    assert proc.returncode == 0, proc.stderr
    assert outputs.get("decision") == "train"
    assert "merge train" in comments.lower()


def test_with_the_train_on_a_re_evaluation_says_nothing_more_on_the_pull_request(gate):
    proc, outputs, comments = gate("on", event_name="workflow_dispatch")
    assert proc.returncode == 0, proc.stderr
    assert outputs.get("decision") == "train"
    assert comments == ""


def test_the_gate_reads_the_train_switch_from_a_repository_variable():
    step = _step(_workflow(AUTO_MERGE)["jobs"]["enable"], "gate")
    assert step["env"]["MERGE_TRAIN"] == "${{ vars.MERGE_TRAIN }}"


def test_a_train_decision_arms_nothing():
    job = _workflow(AUTO_MERGE)["jobs"]["enable"]
    assert _step(job, "merge")["if"] == "steps.gate.outputs.decision == 'merge'"


# --------------------------------------------------------------------------
# the ruleset change: prepared, not applied
# --------------------------------------------------------------------------


def test_the_runbook_carries_the_ruleset_change_and_says_it_is_not_applied():
    text = RUNBOOK.read_text()
    assert "gh api -X PUT repos/bogdan-alexandrescu/SwarmCloud/rulesets/24160219" in text
    assert '"strict_required_status_checks_policy": false' in text
    assert '{"context": "merge-train"}' in text
    assert re.search(r"not applied", text, flags=re.I)
    assert "MERGE_TRAIN" in text


def test_the_runbook_put_keeps_every_rule_the_ruleset_has_today():
    text = RUNBOOK.read_text()
    for rule in ("deletion", "non_fast_forward", "pull_request", "required_status_checks"):
        assert f'"type": "{rule}"' in text, rule
    for context in ("secret scan", "trivy (repo)", "checkov (terraform + kubernetes)",
                    "platform policy assertions", "ci-gate"):
        assert f'{{"context": "{context}"}}' in text, context


def test_the_runbook_says_how_to_roll_back():
    text = RUNBOOK.read_text()
    assert '"strict_required_status_checks_policy": true' in text
    assert re.search(r"^#+ .*roll(ing)? back", text, flags=re.M | re.I)
