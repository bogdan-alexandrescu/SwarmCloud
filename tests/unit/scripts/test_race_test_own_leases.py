"""race-test.sh measures its own leases, on a pool nobody else is holding.

THE DEFECT (#1754, item 2). The 2026-10-20 baseline (execution
swarm-verify-7pkc9) narrowed runner:mock to one slot and reported

    OVER LIMIT: runner:mock active=40 limit=1

having submitted 12 tasks, none of which was ever leased ("checked 0 task(s)
with attempts"). The 40 were concurrency-test's leftover backlog, leased at the
pool's normal ceiling before the narrow. Narrowing does not evict a lease, so a
pool's `active` read after it says nothing about whether admission respected
the narrowed ceiling -- only this run's own leases can.

So race-test now (a) reads the pool's unreleased leases before it narrows and
waits, bounded, for any it did not make to drain, failing with "pool not idle"
if they do not, and (b) counts only leases held by its own tasks toward the
peak. These drive the shipped helpers with Firestore stubbed.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from .gate_script_functions import SCRIPTS, firestore_document, functions, lines, run_bash

SUITE = SCRIPTS / "race-test.sh"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")


def lease(lid: str, task: str, pools=("global", "runner:mock"), units: int = 1) -> str:
    return json.dumps(firestore_document(
        "leases", lid, {"task_id": task, "units": units, "pools": list(pools), "released_at": None}))


def foreign(n: int, pool: str = "runner:mock") -> list[str]:
    return [lease(f"lease_f{i}", f"task_foreign_{i}", pools=("global", pool)) for i in range(n)]


# fs_query answers from READS: a list of files, one per call, the last repeating.
# Each file holds what fs_query prints -- one Firestore document per line.
FS_STUB = r'''
fs_query() {
  local n=1 files f
  [[ -f "${READ_N}" ]] && n=$(( $(cat "${READ_N}") + 1 ))
  printf '%s' "${n}" >"${READ_N}"
  read -r -a files <<<"${READS}"
  if [[ "${n}" -le "${#files[@]}" ]]; then f="${files[$(( n - 1 ))]}"; else f="${files[$(( ${#files[@]} - 1 ))]}"; fi
  [[ "${f}" != "FAIL" ]] || return 1
  cat "${f}"
}
'''


def _run(tmp: Path, reads: list[list[str] | str], snippet: str, own: tuple[str, ...] = ()):
    files = []
    for i, rows in enumerate(reads):
        if rows == "FAIL":
            files.append("FAIL")
            continue
        p = tmp / f"read{i}.jsonl"
        p.write_text("".join(r + "\n" for r in rows))
        files.append(str(p))
    body = (
        FS_STUB
        + functions(SUITE, "require_idle_pool", "sample_own_leases")
        + f"\nPOOL_NAME=runner:mock\nTASK_IDS=({' '.join(own)})\n"
        + snippet
    )
    return run_bash(body, env={"READS": " ".join(files), "READ_N": str(tmp / "n"), "TMPDIR": str(tmp)})


# --- the helpers in testlib.sh ---------------------------------------------


def test_leases_are_split_into_this_runs_and_everybody_elses(tmp_path: Path) -> None:
    rows = foreign(40) + [lease("lease_own", "task_own_1"), lease("lease_cc", "task_x", pools=("global", "runner:claude-code"))]
    r = _run(
        tmp_path, [rows],
        'h="$(pool_lease_holders runner:mock)" || exit 9\n'
        'leases_by_owner "${h}" task_own_1 task_own_2\n',
    )
    assert r.returncode == 0, r.stderr
    split = json.loads(r.stdout)
    # MUTATION: count every unreleased lease as this run's and own reads 41.
    assert split["own"] == 1
    assert split["own_units"] == 1
    # MUTATION: drop the pool filter and the runner:claude-code lease counts as foreign (41).
    assert split["foreign"] == 40


def test_a_read_at_the_query_limit_is_refused_not_undercounted(tmp_path: Path) -> None:
    r = _run(tmp_path, [foreign(1000)], "pool_lease_holders runner:mock >/dev/null && echo read-ok")
    # MUTATION: drop the limit check and a truncated list reads as a complete one.
    assert "read-ok" not in r.stdout
    assert "query limit" in r.stderr


# --- [2]: the pool must be idle before it is narrowed ----------------------


def test_a_pool_held_by_other_work_fails_with_pool_not_idle(tmp_path: Path) -> None:
    """The baseline's 40 leftover leases: wait, then refuse rather than measure them."""
    r = _run(tmp_path, [foreign(40)], "require_idle_pool runner:mock 1\necho measured-anyway")
    # MUTATION: skip the idle check and the suite goes on to narrow and measure.
    assert r.returncode != 0
    assert "measured-anyway" not in r.stdout
    fails = lines(r, "FAIL")
    assert any("pool not idle: 40 leases held by other work" in f for f in fails), r.stderr


def test_foreign_leases_that_drain_within_the_wait_let_the_run_start(tmp_path: Path) -> None:
    r = _run(tmp_path, [foreign(40), foreign(3), []], "require_idle_pool runner:mock 60\necho started")
    # MUTATION: fail on the first busy read instead of waiting, and this never starts.
    assert r.returncode == 0, r.stderr
    assert "started" in r.stdout
    assert lines(r, "FAIL") == []


def test_an_unreadable_lease_list_is_not_an_idle_pool(tmp_path: Path) -> None:
    r = _run(tmp_path, ["FAIL"], "require_idle_pool runner:mock 60\necho started")
    # MUTATION: treat a failed read as zero leases and the run starts blind.
    assert r.returncode != 0
    assert "started" not in r.stdout


def test_a_foreign_lease_on_another_pool_does_not_hold_this_one(tmp_path: Path) -> None:
    rows = foreign(5, pool="runner:claude-code")
    r = _run(tmp_path, [rows], "require_idle_pool runner:mock 1\necho started")
    assert r.returncode == 0, r.stderr
    assert "started" in r.stdout


# --- [3]: only this run's leases count toward the peak ---------------------


def test_the_peak_counts_only_this_runs_own_leases(tmp_path: Path) -> None:
    rows = foreign(40) + [lease("lease_own", "task_own_1")]
    r = _run(
        tmp_path, [rows],
        'sample_own_leases || exit 9\nprintf "%s %s %s\\n" "${OWN_UNITS}" "${OWN_LEASES}" "${FOREIGN_LEASES}"',
        own=("task_own_1", "task_own_2"),
    )
    assert r.returncode == 0, r.stderr
    # MUTATION: read the pool's `active` (41) instead of this run's leases.
    assert r.stdout.split() == ["1", "1", "40"]


def test_two_of_this_runs_leases_on_a_one_slot_pool_are_seen(tmp_path: Path) -> None:
    rows = [lease("l1", "task_own_1"), lease("l2", "task_own_2")]
    r = _run(tmp_path, [rows], 'sample_own_leases || exit 9\necho "${OWN_UNITS}"', own=("task_own_1", "task_own_2"))
    # MUTATION: count own leases as a flag (0/1) rather than a sum and oversubscription hides.
    assert r.stdout.split() == ["2"]


def test_the_peak_assertion_reads_own_units() -> None:
    text = SUITE.read_text()
    # MUTATION: go back to `active="$(pool_active "${POOL_NAME}")"` in the sampling loop.
    assert 'active="$(pool_active "${POOL_NAME}")"' not in text
    assert "sample_own_leases" in text and "require_idle_pool" in text
