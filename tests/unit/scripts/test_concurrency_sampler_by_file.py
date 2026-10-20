"""concurrency-test.sh samples a 300-task backlog, and cancels what it submitted.

THE DEFECT (#1754, item 1). The 2026-10-20 baseline of `make verify-remote`
(execution swarm-verify-gztsl) never took a sample:

    scripts/concurrency-test.sh: line 133: /usr/bin/jq: Argument list too long

The sampler grew its batchGet result as `all="$(jq -nc --argjson a "${all}" ...)"`
and built the sample with `--argjson` for the pools, the leases and the tasks.
Linux caps ONE argument at 128 KiB (MAX_ARG_STRLEN), and a 3x-oversubscribed
backlog of decoded task documents passes that, so the first sample of a real
run died. The script then exited, and nothing it printed said its 300 mock
tasks had been cancelled.

These drive the shipped `fs_query_at`, `fs_tasks_at`, `consistent_sample` and
`cleanup` with Firestore and the API stubbed: 300 tasks of ~1.5 KiB each, which
is ~450 KiB of tasks, well past the per-argument cap.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from .gate_script_functions import SCRIPTS, firestore_document, functions, run_bash

SUITE = SCRIPTS / "concurrency-test.sh"
N_TASKS = 300
PAD = "x" * 1500

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")


def _fixture(tmp: Path) -> Path:
    fix = tmp / "fs"
    (fix / "tasks").mkdir(parents=True)
    ids = [f"task_{i:04d}" for i in range(N_TASKS)]
    tasks = {
        tid: firestore_document(
            "tasks", tid,
            {"state": "LEASED", "current_lease_id": f"lease_{tid}", "tenant_id": "verify",
             "runner_profile": "mock", "prompt_echo": PAD},
        )
        for tid in ids
    }
    for tid, doc in tasks.items():
        (fix / "tasks" / f"{tid}.json").write_text(json.dumps(doc))
    leases = [
        {"document": firestore_document(
            "leases", f"lease_{tid}",
            {"task_id": tid, "units": 1, "pools": ["global", "runner:mock"], "released_at": None})}
        for tid in ids
    ]
    pools = [
        {"document": firestore_document("pools", "global", {"active": N_TASKS, "hard_limit": 1000})},
        {"document": firestore_document("pools", "runner:mock", {"active": N_TASKS, "hard_limit": 1000})},
    ]
    (fix / "leases.json").write_text(json.dumps(leases))
    (fix / "pools.json").write_text(json.dumps(pools))
    (fix / "tasks.json").write_text(json.dumps([{"document": d} for d in tasks.values()]))
    return fix


# Firestore, answered from files. The stub reads its request from stdin-shaped
# here-strings only, so the stub itself never puts a sample on a command line.
FS_STUB = r'''
fs_base() { printf 'https://firestore.fake/v1/projects/p/databases/d/documents'; }
fs_request() {
  local method="$1" url="$2" body="${3:-}" c id first=1
  case "${url}" in
    *:runQuery)
      c="$(jq -r '.structuredQuery.from[0].collectionId' <<<"${body}")"
      cat "${FIX}/${c}.json"
      ;;
    *:batchGet)
      printf '['
      while IFS= read -r id; do
        [[ -n "${id}" ]] || continue
        [[ "${first}" -eq 1 ]] || printf ','
        first=0
        printf '{"found":'
        cat "${FIX}/tasks/${id}.json"
        printf '}'
      done < <(jq -r '.documents[] | split("/") | last' <<<"${body}")
      printf ']'
      ;;
    *) return 1 ;;
  esac
}
SAMPLE_QUERY_LIMIT=1000
CONCURRENCY_JSON="$(states_json "${CONCURRENCY_STATES[@]}")"
'''


def _sampler(tmp: Path, snippet: str):
    fix = _fixture(tmp)
    body = (
        FS_STUB
        + functions(SUITE, "fs_query_at", "fs_tasks_at", "consistent_sample")
        + "\n"
        + snippet
    )
    return run_bash(body, env={"FIX": str(fix), "TMPDIR": str(tmp)})


def test_a_300_task_backlog_is_sampled_whole(tmp_path: Path) -> None:
    """The sample the baseline run never took: every pool, lease and task in it."""
    out = tmp_path / "sample.json"
    r = _sampler(tmp_path, f'consistent_sample >"{out}"')
    # MUTATION: put `all="$(jq -nc --argjson a "${all}" --argjson b "${part}" '$a + $b')"`
    # back in fs_tasks_at, or `--argjson lease_tasks "${lease_tasks}"` back in
    # consistent_sample, and this run dies with "Argument list too long".
    assert r.returncode == 0, r.stderr[-2000:]
    assert "Argument list too long" not in r.stderr, r.stderr[-2000:]
    sample = json.loads(out.read_text())
    # MUTATION: drop a batchGet batch (e.g. step i by 200) and the count falls short.
    assert len(sample["tasks"]) == N_TASKS
    assert len(sample["leases"]) == N_TASKS
    # MUTATION: drop `. + {effective: effective_limit}` and the judge reads no ceiling.
    assert {p["id"]: p["effective"] for p in sample["pools"]} == {"global": 1000, "runner:mock": 1000}


def test_the_lease_holders_are_read_back_by_batch_get_in_full(tmp_path: Path) -> None:
    ids = json.dumps([f"task_{i:04d}" for i in range(N_TASKS)])
    out = tmp_path / "tasks.json"
    r = _sampler(tmp_path, f"fs_tasks_at '{ids}' 2026-10-20T20:00:00Z >\"{out}\"")
    # MUTATION: accumulate the batches with --argjson again and this exits non-zero.
    assert r.returncode == 0, r.stderr[-2000:]
    got = json.loads(out.read_text())
    # MUTATION: keep only the last batch (overwrite instead of append) and 100 come back.
    assert sorted(t["id"] for t in got) == json.loads(ids)


def test_no_sample_sized_value_goes_on_a_jq_command_line() -> None:
    """The behavioural tests above use one fixture size; this holds the rule for every size."""
    text = SUITE.read_text()
    for name in ("all", "pools", "leases", "holders", "lease_tasks", "judged", "a", "b"):
        # MUTATION: reintroduce any of these as `--argjson NAME "${...}"` and this fails.
        assert not re.search(rf'--argjson {name} "\$\{{', text), (
            f"concurrency-test.sh passes ${name} to jq as an argument again; "
            "a 300-task sample is over the 128 KiB per-argument limit"
        )


# --- cleanup: every task it submitted, once, and said ----------------------

CANCEL_STUB = r'''
cancel_task() {
  printf '%s\n' "$1" >>"${CANCEL_LOG}"
  if [[ "$1" == "task_0007" ]]; then API_STATUS=409; return 1; fi
  API_STATUS=200
}
'''


def _trap_lines() -> str:
    """The suite's own trap registrations, as written."""
    text = SUITE.read_text()
    traps = re.findall(r"^trap .*$", text, re.M)
    assert traps, "concurrency-test.sh registers no trap"
    return "\n".join(traps)


def _cleanup_run(tmp: Path, ending: str):
    log = tmp / "cancel.log"
    ids = " ".join(f"task_{i:04d}" for i in range(N_TASKS))
    body = (
        CANCEL_STUB
        + functions(SUITE, "cleanup")
        + f"\nTASK_IDS=({ids})\nCLEANED=0\n"
        + _trap_lines()
        + "\n"
        + ending
    )
    r = run_bash(body, env={"CANCEL_LOG": str(log), "TMPDIR": str(tmp)})
    cancelled = log.read_text().split() if log.exists() else []
    return r, cancelled


def test_a_run_that_dies_cancels_every_task_it_submitted_and_says_how_many(tmp_path: Path) -> None:
    r, cancelled = _cleanup_run(tmp_path, "exit 3")
    # MUTATION: remove `trap cleanup EXIT` and nothing is cancelled.
    assert sorted(cancelled) == [f"task_{i:04d}" for i in range(N_TASKS)]
    # The trap must not turn the failure into anything else.
    assert r.returncode == 3, r.stderr[-2000:]
    # MUTATION: cancel without counting (the old cancel_all) and nothing says how many.
    assert "cancelled 299 of 300" in r.stderr, r.stderr[-2000:]
    assert "refused (HTTP 409)" in r.stderr, r.stderr[-2000:]


def test_an_interrupted_run_cancels_once_and_stops(tmp_path: Path) -> None:
    """TERM used to run cleanup, carry on with the suite, then run it again on EXIT."""
    r, cancelled = _cleanup_run(tmp_path, 'kill -TERM $$\necho "carried on after TERM"')
    # MUTATION: `trap cleanup EXIT INT TERM` again: 600 cancels, and the suite resumes.
    assert len(cancelled) == N_TASKS, f"{len(cancelled)} cancel requests for {N_TASKS} tasks"
    assert "carried on after TERM" not in r.stdout
    assert r.returncode == 143, r.stderr[-2000:]


def test_cleanup_is_idempotent(tmp_path: Path) -> None:
    r, cancelled = _cleanup_run(tmp_path, "cleanup\ncleanup\ntrap - EXIT")
    # MUTATION: drop the CLEANED guard and every task is cancelled twice.
    assert len(cancelled) == N_TASKS
    assert r.stderr.count("cancelled 299 of 300") == 1, r.stderr[-2000:]
