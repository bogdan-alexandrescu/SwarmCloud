"""The merge train gathers before it boards (the owner's direction, 12 November
2026: "15 pull requests or 30 minutes, up to 40", then 13 November: "8 ready or 15
minutes, up to 40"; it was "6 ready or 20
minutes" from 11 November).

WHY. A tick boarded every eligible `ready` pull request as soon as it was
green, so trains were small and each one paid the whole suite; the full suite
is moving to run only on the train and on main, so fewer, larger trains spend
less CI time -- and since 12 November a lane's pull request runs no checks at
all, so the train's run is the only one. Now a train boards once
MERGE_TRAIN_MIN_PRS (15) pull requests are ready, or once the oldest of them
has been ready for MERGE_TRAIN_MAX_WAIT_MINUTES (30), and holds at most
MERGE_TRAIN_MAX_PRS (40). "Ready since" is the time the `ready` label
was last added, read from GitHub's issue events; one that cannot be read
counts as the full wait, so an unreadable time never holds work. A
`train-first` pull request boards at once and takes the rest with it; a
bisection's held-back half boards at once (start_train's explicit list never
gathers). MIN 1 or WAIT 0 is the old behaviour, with no read at all.

All offline: bash + jq, and a fake `gh` serving each pull request's issue
events from a file and logging every call.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "merge-train.sh"
RUNBOOK = REPO / "docs" / "runbooks" / "merge-train.md"
GATHER_FILE = REPO / "apps" / "swarm-api" / "swarm_api" / "merge_train_gather.json"
GH_REPO = "example-org/example-repo"

FAKE_GH = """#!/usr/bin/env bash
set -euo pipefail
# gh api repos/<R>/issues/<N>/events --paginate --jq <filter>: the filter run
# over $FAKE_EVENTS/<N>.json ([] when absent); a refusal when
# $FAKE_EVENTS/<N>.unreadable exists.
printf '%s\\n' "$*" >>"${FAKE_GH_LOG}"
if [[ "$1" == "api" ]]; then
  n="$(sed -n 's|^repos/[^/]*/[^/]*/issues/\\([0-9]*\\)/events$|\\1|p' <<<"$2")"
  [[ -n "${n}" ]] || { echo "fake gh: unexpected: $*" >&2; exit 64; }
  [[ ! -e "${FAKE_EVENTS}/${n}.unreadable" ]] || { echo "HTTP 403: Resource not accessible by integration" >&2; exit 1; }
  filter=""
  while [[ $# -gt 0 ]]; do
    [[ "$1" != "--jq" ]] || filter="$2"
    shift
  done
  events="${FAKE_EVENTS}/${n}.json"
  [[ -f "${events}" ]] || events=<(echo '[]')
  jq -r "${filter}" "${events}"
  exit 0
fi
echo "fake gh: unexpected: $*" >&2
exit 64
"""


def _need(*tools: str) -> None:
    for tool in tools:
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is required")


def _iso(minutes_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - minutes_ago * 60))


def _labeled(minutes_ago: float, name: str = "ready", event: str = "labeled") -> dict:
    return {"event": event, "label": {"name": name}, "created_at": _iso(minutes_ago)}


class Gather:
    """`merge-train.sh gather` over a chosen list, as start_train calls it."""

    def __init__(self, tmp_path: Path):
        _need("bash", "jq", "awk")
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        fake = self.bin / "gh"
        fake.write_text(FAKE_GH)
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.events = tmp_path / "events"
        self.events.mkdir()
        self.log = tmp_path / "gh.log"
        self.prs: list[dict] = []

    def ready(self, number: int, events: list[dict] | None, *, urgent: bool = False) -> None:
        """Pull request `number`, `ready` (and `train-first` if urgent), with
        these issue events, or events that cannot be read when None."""
        labels = ["ready", *(["train-first"] if urgent else [])]
        self.prs.append({"number": number, "title": f"Change {number}",
                         "headRefOid": secrets.token_hex(20),
                         "labels": [{"name": n} for n in labels]})
        if events is None:
            (self.events / f"{number}.unreadable").write_text("")
        else:
            (self.events / f"{number}.json").write_text(json.dumps(events))

    def run(self, **knobs: str) -> tuple[str, str]:
        """Returns (`board` or `wait`, the step summary)."""
        prs = self.tmp / "prs.json"
        prs.write_text(json.dumps(self.prs))
        chosen = self.tmp / "chosen.tsv"
        chosen.write_text("".join(f"{p['number']}\t{p['headRefOid']}\t{p['title']}\n" for p in self.prs))
        summary = self.tmp / "summary.md"
        summary.write_text("")
        self.log.write_text("")
        env = dict(os.environ)
        for key in [k for k in env if k.startswith("MERGE_TRAIN_")]:
            env.pop(key)
        env.update({"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "GH_REPO": GH_REPO,
                    "FAKE_GH_LOG": str(self.log), "FAKE_EVENTS": str(self.events),
                    "GITHUB_STEP_SUMMARY": str(summary),
                    # The repository's batch size, the script's default too
                    # (a minimum above it is tested on its own).
                    "MERGE_TRAIN_MAX_PRS": "40",
                    "MERGE_TRAIN_MERGE_RETRY_WAITS": "0 0", **knobs})
        proc = subprocess.run(["bash", str(SCRIPT), "gather", "--prs", str(prs), "--chosen", str(chosen)],
                              capture_output=True, text=True, timeout=60, check=False, env=env)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip(), summary.read_text()

    def reads(self) -> int:
        return len([line for line in self.log.read_text().splitlines() if "/events" in line])


@pytest.fixture
def gather(tmp_path: Path) -> Gather:
    return Gather(tmp_path)


# --------------------------------------------------------------------------
# 8 ready or 15 minutes, up to 40
# --------------------------------------------------------------------------


def test_five_ready_for_five_minutes_board_nothing_and_say_so_in_one_line(gather):
    for i, n in enumerate(range(101, 106)):
        gather.ready(n, [_labeled(5 - i * 0.5)])
    verdict, summary = gather.run()
    assert verdict == "wait"
    lines = [line for line in summary.splitlines() if line.strip()]
    assert len(lines) == 1, summary
    line = lines[0]
    assert "5 ready pull requests wait" in line
    assert "#101" in line and "ready 5 minutes" in line
    # It boards at the latest when #101 has waited 15 minutes: 10 from now.
    assert "wait for 8 to board (or 15 minutes)" in line
    assert "at the latest" in line and "in 10 minutes" in line
    assert re.search(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", line), line


def test_seven_ready_wait_and_eight_board_without_reading_when_they_became_ready(gather):
    for n in range(101, 108):
        gather.ready(n, [_labeled(1)])
    assert gather.run()[0] == "wait"
    gather.prs.clear()
    for n in range(101, 109):
        gather.ready(n, [_labeled(1)])
    verdict, _ = gather.run()
    assert verdict == "board"
    assert gather.reads() == 0


def test_five_board_once_the_oldest_has_waited_past_the_window(gather):
    gather.ready(101, [_labeled(14)])
    for n in range(102, 106):
        gather.ready(n, [_labeled(2)])
    assert gather.run()[0] == "wait"
    gather.prs.clear()
    gather.ready(101, [_labeled(16)])
    for n in range(102, 106):
        gather.ready(n, [_labeled(2)])
    verdict, summary = gather.run()
    assert verdict == "board"
    assert "wait" not in summary


def test_the_time_ready_was_last_added_is_the_one_that_counts(gather):
    """Labelled 30 minutes ago, unlabelled, labelled again 3 minutes ago:
    ready for 3 minutes. Another label's event is not `ready`'s."""
    gather.ready(101, [_labeled(30), _labeled(29, event="unlabeled"), _labeled(3),
                       _labeled(40, name="train-first")])
    verdict, summary = gather.run()
    assert verdict == "wait"
    assert "ready 3 minutes" in summary


def test_a_train_first_pull_request_boards_at_once_and_takes_the_rest(gather):
    gather.ready(101, [_labeled(1)])
    gather.ready(102, [_labeled(1)], urgent=True)
    verdict, _ = gather.run()
    assert verdict == "board"
    assert gather.reads() == 0


def test_an_unreadable_ready_time_counts_as_the_full_wait_and_boards(gather):
    gather.ready(101, [_labeled(2)])
    gather.ready(102, None)
    verdict, _ = gather.run()
    assert verdict == "board"


def test_a_pull_request_with_no_ready_event_counts_as_the_full_wait(gather):
    gather.ready(101, [])
    verdict, _ = gather.run()
    assert verdict == "board"


@pytest.mark.parametrize("knobs", [{"MERGE_TRAIN_MIN_PRS": "1"}, {"MERGE_TRAIN_MAX_WAIT_MINUTES": "0"}],
                         ids=["minimum 1", "wait 0"])
def test_minimum_one_or_wait_zero_boards_as_before_with_no_read(gather, knobs):
    gather.ready(101, [_labeled(0.5)])
    verdict, summary = gather.run(**knobs)
    assert verdict == "board"
    assert gather.reads() == 0
    assert summary == ""


def test_the_settings_are_read_from_the_environment(gather):
    for n in range(101, 104):
        gather.ready(n, [_labeled(8)])
    assert gather.run(MERGE_TRAIN_MIN_PRS="3")[0] == "board"
    assert gather.run(MERGE_TRAIN_MAX_WAIT_MINUTES="7")[0] == "board"
    assert gather.run(MERGE_TRAIN_MAX_WAIT_MINUTES="9")[0] == "wait"


def test_a_minimum_above_the_maximum_is_the_maximum(gather):
    """A train never holds more than MERGE_TRAIN_MAX_PRS, so a minimum above
    it would wait the full window every time."""
    for n in range(101, 105):
        gather.ready(n, [_labeled(1)])
    assert gather.run(MERGE_TRAIN_MAX_PRS="4")[0] == "board"


@pytest.mark.parametrize(("knob", "value"), [("MERGE_TRAIN_MIN_PRS", "0"), ("MERGE_TRAIN_MIN_PRS", "six"),
                                             ("MERGE_TRAIN_MAX_WAIT_MINUTES", "-1"),
                                             ("MERGE_TRAIN_MAX_WAIT_MINUTES", "20m")])
def test_a_setting_that_is_not_a_number_is_refused(tmp_path, knob, value):
    _need("bash", "jq")
    (tmp_path / "prs.json").write_text("[]")
    (tmp_path / "chosen.tsv").write_text("")
    env = {**os.environ, knob: value}
    proc = subprocess.run(["bash", str(SCRIPT), "gather", "--prs", str(tmp_path / "prs.json"),
                           "--chosen", str(tmp_path / "chosen.tsv")],
                          capture_output=True, text=True, timeout=60, check=False, env=env)
    assert proc.returncode != 0
    assert knob in proc.stderr


# --------------------------------------------------------------------------
# where the tick gathers, and where it never does
# --------------------------------------------------------------------------


def _start_train() -> str:
    code = SCRIPT.read_text()
    start = code[code.index("start_train() {"):]
    return start[:start.index("\n}\n")]


def test_the_tick_gathers_only_when_it_boards_from_the_pool():
    """A bisection's named half (the explicit list) boards at once; the pool
    gathers, after the guards, over the pull requests that would board."""
    start = _start_train()
    explicit, pool = start.split("else\n", 1)
    assert "gather" not in explicit
    assert "gather_waits" in pool
    assert pool.index("guards_pass") < pool.index("gather_waits")


def test_the_tick_checks_the_settings_before_it_reads_anything():
    code = SCRIPT.read_text()
    tick = code[code.index("cmd_tick() {"):]
    tick = tick[:tick.index("\n}\n")]
    assert tick.index("gather_knobs_valid") < tick.index("list_open_prs")


def test_each_setting_is_explained_next_to_its_default():
    """The defaults are stated once, in the file swarm-api's merge wake tick
    reads too (swarm_api.trainboard, board on time, 12 November 2026)."""
    code = SCRIPT.read_text()
    shared = json.loads(GATHER_FILE.read_text())
    assert (shared["min_prs"], shared["max_wait_minutes"]) == (8, 15)
    assert "apps/swarm-api/swarm_api/merge_train_gather.json" in code
    for knob, key in (("MERGE_TRAIN_MIN_PRS", "min_prs"),
                      ("MERGE_TRAIN_MAX_WAIT_MINUTES", "max_wait_minutes")):
        line = next(i for i, text in enumerate(code.splitlines())
                    if f"${{{knob}:-$(gather_default {key})}}" in text)
        assert code.splitlines()[line - 1].strip().startswith("#"), knob


def test_without_the_shared_file_the_script_stops_and_says_why(tmp_path):
    _need("bash", "jq")
    copy = tmp_path / "scripts"
    shutil.copytree(REPO / "scripts", copy, ignore=shutil.ignore_patterns("__pycache__"))
    env = {k: v for k, v in os.environ.items() if not k.startswith("MERGE_TRAIN_")}
    proc = subprocess.run(["bash", str(copy / "merge-train.sh"), "gather", "--prs", "x",
                           "--chosen", "y"], capture_output=True, text=True, timeout=60,
                          check=False, env=env)
    assert proc.returncode != 0
    assert "merge_train_gather.json" in proc.stderr


def test_the_judge_needs_no_shared_file(tmp_path):
    """auto-merge.yml runs `judge` from a checkout of scripts/ alone: only
    `tick` and `gather` read the gather's defaults."""
    _need("bash", "jq")
    copy = tmp_path / "scripts"
    shutil.copytree(REPO / "scripts", copy, ignore=shutil.ignore_patterns("__pycache__"))
    runs = tmp_path / "check-runs.json"
    runs.write_text(json.dumps({"check_runs": [{"id": 1, "name": "ci-gate", "status": "completed",
                                                "conclusion": "success", "check_suite": {"id": 1}}]}))
    statuses = tmp_path / "status.json"
    statuses.write_text(json.dumps({"state": "success", "statuses": []}))
    required = tmp_path / "required.txt"
    required.write_text("ci-gate\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("MERGE_TRAIN_")}
    proc = subprocess.run(["bash", str(copy / "merge-train.sh"), "judge", "--check-runs", str(runs),
                           "--statuses", str(statuses), "--required", str(required)],
                          capture_output=True, text=True, timeout=60, check=False, env=env)
    assert "merge_train_gather.json" not in proc.stderr, proc.stderr
    assert proc.returncode == 0, proc.stderr


def test_the_runbook_names_both_settings_and_how_to_change_them():
    text = RUNBOOK.read_text()
    for knob in ("MERGE_TRAIN_MIN_PRS", "MERGE_TRAIN_MAX_WAIT_MINUTES"):
        assert knob in text
    assert "gh variable set MERGE_TRAIN_MIN_PRS" in text
    assert "gh variable set MERGE_TRAIN_MAX_WAIT_MINUTES" in text


def test_a_train_holds_at_most_forty_by_default():
    """The owner's direction, 12 November 2026: "15 pull requests or 30
    minutes, up to 40"; the forty stayed when the gather became "8 ready or
    15 minutes" on 13 November. MUTATION: put MAX_PRS's default back to 4."""
    code = SCRIPT.read_text()
    line = next(i for i, text in enumerate(code.splitlines()) if text.startswith("MAX_PRS="))
    assert code.splitlines()[line] == 'MAX_PRS="${MERGE_TRAIN_MAX_PRS:-40}"'
    assert code.splitlines()[line - 1].strip().startswith("#")
