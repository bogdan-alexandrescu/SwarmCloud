"""A CI-fix round is told which test failed (CIFIX-EXCERPT, part of #1914).

Measured 2026-10-22 on PR 1905 (application run 39703499595): the excerpt a
merge step's fix round got (`issueci.excerpt_at`) kept the last 3,000 bytes
of each failing job's log. On "swarm-ui component tests (2/4)" half of those
were "Post job cleanup" git lines, and the `FAIL ... intake.css` line and the
Received value naming the selector sat just above the cut. `ci-gate`, a
roll-up whose log only restates its `needs`, came first among the checks kept.
The round spent about nine minutes rebuilding vitest's shard split to find
which file had failed.

Every log here is synthetic text shaped like that job's; nothing touches
GitHub. The fake writer serves a log's last `limit` bytes exactly as
`forgewrite.GitHubWriter.job_log_tail` does (a ranged read), so what reaches
the excerpt depends on how much of the log `_check_section` asks for.
"""

from __future__ import annotations

import secrets
from typing import Any

from swarm_api import issueci
from swarm_api.forgechecks import RED, CiReading
from swarm_api.validation import IssueRef

REF = IssueRef(owner="acme", repo="widgets", number=1905)
SHA = "c" * 40

FAIL_LINE = (
    " FAIL  src/__tests__/sections.stylesheet.test.ts > the section stylesheets "
    "> styles/intake.css > declares no selector twice"
)
SELECTOR = ".rn-filters > .c-seg > button"
CLEANUP_COMMAND = "[command]/usr/bin/git config --local --unset-all http.https://github.com/.extraheader"
PYTEST_NODE = "tests/unit/control_plane/test_widgets.py::test_sort_is_stable"


def _stamp(i: int) -> str:
    return f"2026-10-22T01:2{i % 10}:{i % 60:02d}.{i:07d}Z "


def _lines_to(target: int, prefix: str) -> list[str]:
    """Filler lines adding up to about `target` bytes."""
    out, size, i = [], 0, 0
    while size < target:
        line = f"{prefix} {i:04d} " + "." * 40
        out.append(line)
        size += len(line) + len(_stamp(i)) + 1
        i += 1
    return out


def _vitest_job_log() -> bytes:
    """Job 119584394927's shape: the failure ~3,500 bytes from the end, then
    the summary, the step's error, and ~2,000 bytes of post-job cleanup."""
    head = ["##[group]Run npx vitest run --shard 2/4", "##[endgroup]"]
    head += _lines_to(6_000, " ✓ src/__tests__/passing.test.ts > renders")
    failure = [
        "⎯⎯⎯⎯⎯⎯⎯ Failed Tests 1 ⎯⎯⎯⎯⎯⎯⎯",
        "",
        FAIL_LINE,
        "AssertionError: expected [ { selector: '.rn-filters > .c-seg > button', …(1) } ] to deeply equal []",
        "",
        "- Expected",
        "+ Received",
        "",
        "- []",
        "+ [",
        "+   {",
        "+     \"lines\": [",
        "+       506,",
        "+       515,",
        "+     ],",
        f"+     \"selector\": \"{SELECTOR}\",",
        "+   },",
        "+ ]",
        "",
        " ❯ src/__tests__/sections.stylesheet.test.ts:41:28",
        "     39|     for (const r of results) {",
        "     40|       // one block, one declaration of each selector",
        "     41|       expect(r.duplicates).toEqual([])",
        "       |                            ^",
        "",
        "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯[1/1]⎯",
    ]
    between = _lines_to(400, "stdout | src/__tests__/other.test.ts > logs")
    summary = [
        "",
        " Test Files  1 failed | 128 passed (129)",
        "      Tests  1 failed | 911 passed (912)",
        "   Start at  01:21:07",
        "   Duration  48.31s",
        "",
        "##[error]Process completed with exit code 1.",
    ]
    cleanup = ["Post job cleanup."]
    cleanup += [CLEANUP_COMMAND if i % 3 == 0 else f"[command]/usr/bin/git submodule foreach --recursive true {i:03d}"
                for i in range(22)]
    cleanup += ["Cleaning up orphan processes"]
    lines = head + failure + between + summary + cleanup
    return ("\n".join(_stamp(i) + line for i, line in enumerate(lines)) + "\n").encode("utf-8")


def _pytest_job_log() -> bytes:
    lines = ["##[group]Run uv run pytest tests/unit -q"]
    lines += _lines_to(5_000, "tests/unit/control_plane/test_passing.py .")
    lines += [
        "=================================== FAILURES ===================================",
        "_____________________________ test_sort_is_stable ______________________________",
        "    def test_sort_is_stable():",
        ">       assert sort_widgets([2, 1]) == [1, 2]",
        "E       assert [2, 1] == [1, 2]",
        "tests/unit/control_plane/test_widgets.py:12: AssertionError",
    ]
    lines += _lines_to(2_500, "-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html")
    lines += [
        "=========================== short test summary info ============================",
        f"FAILED {PYTEST_NODE} - assert [2, 1] == [1, 2]",
        "1 failed, 2300 passed in 211.04s (0:03:31)",
        "##[error]Process completed with exit code 1.",
        "Post job cleanup.",
    ]
    lines += [CLEANUP_COMMAND] * 25
    return ("\n".join(_stamp(i) + line for i, line in enumerate(lines)) + "\n").encode("utf-8")


def _gate_job_log() -> bytes:
    lines = ["##[group]Run needs='{\"ui\":{\"result\":\"failure\"}}'"]
    lines += [f'  "job-{i}": {{ "result": "success", "outputs": {{}} }},' for i in range(60)]
    lines += ["##[error]a needed job did not succeed", "##[error]Process completed with exit code 1."]
    return ("\n".join(_stamp(i) + line for i, line in enumerate(lines)) + "\n").encode("utf-8")


class _Writer:
    """`GitHubWriter`'s two reads `_check_section` makes, from fixtures."""

    def __init__(self, logs: dict[int, bytes]):
        self.logs = logs
        self.limits: list[int] = []

    def check_annotations(self, ref: IssueRef, check_id: int, token: str) -> list[dict[str, Any]]:
        return []  # the vitest shard jobs carry none

    def job_log_tail(self, ref: IssueRef, job_id: int, token: str, *, limit: int) -> str | None:
        self.limits.append(limit)
        raw = self.logs.get(job_id)
        if not raw:
            return None
        text = raw[-limit:].decode("utf-8", errors="replace")
        if len(raw) > limit and "\n" in text:
            text = text.split("\n", 1)[1]
        return text


def _check(check_id: int, name: str) -> dict[str, Any]:
    return {
        "id": check_id,
        "name": name,
        "conclusion": "failure",
        "app": {"slug": "github-actions"},
        "output": {"title": None, "summary": None, "text": None},
    }


def _excerpt(checks: list[dict[str, Any]], logs: dict[int, bytes]) -> str:
    reading = CiReading(state=RED, failing_runs=checks)
    return issueci.excerpt_at(_Writer(logs), REF, SHA, reading, secrets.token_hex(20), label="test")


def _section(excerpt: str, name: str) -> str:
    start = excerpt.index(f"## {name} (")
    rest = excerpt[start + 3:]
    end = rest.find("\n## ")
    return rest if end < 0 else rest[:end]


# --------------------------------------------------------------------------
# The failing test reaches the excerpt
# --------------------------------------------------------------------------

def test_the_fixture_puts_the_failure_where_pr_1905_had_it():
    # The control: the plain 3,000-byte tail this lane replaces would NOT
    # have held the failure, so the next test can come out either way.
    raw = _vitest_job_log()
    assert len(raw) - raw.index(FAIL_LINE.encode()) >= 3_500
    cleanup = len(raw) - raw.index(b"Post job cleanup.")
    assert 1_800 <= cleanup <= 2_600
    assert FAIL_LINE.encode() not in raw[-issueci.MAX_LOG_TAIL_BYTES:]


def test_a_vitest_failure_above_the_post_job_cleanup_names_its_file_test_and_received_value():
    logs = {11: _vitest_job_log()}
    section = _section(_excerpt([_check(11, "swarm-ui component tests (2/4)")], logs), "swarm-ui component tests (2/4)")

    # MUTATION: read only `MAX_LOG_TAIL_BYTES` of the log's end in `_check_section` (the old read) -> red.
    assert FAIL_LINE.strip() in section
    # MUTATION: keep only the FAIL line, not the block under it, in `_log_failures` -> red.
    assert SELECTOR in section and "+ Received" in section
    # MUTATION: drop the cut at `##[error]Process completed with exit code` -> red.
    assert "Post job cleanup" not in section and CLEANUP_COMMAND not in section
    # MUTATION: drop `##[error]` from the failure-line markers -> red.
    assert "##[error]Process completed with exit code 1." in section
    # MUTATION: keep the timestamp column the raw job log carries -> red.
    assert "2026-10-22T01:2" not in section


def test_a_pytest_failure_keeps_the_failed_nodeid_and_its_assertion():
    logs = {12: _pytest_job_log()}
    section = _section(_excerpt([_check(12, "python")], logs), "python")

    # MUTATION: drop the `FAILED <nodeid>` marker in `_log_failures` -> red.
    assert f"FAILED {PYTEST_NODE}" in section
    # MUTATION: drop the pytest `E   ` / `path.py:N: Error` markers -> red.
    assert "E       assert [2, 1] == [1, 2]" in section
    assert "tests/unit/control_plane/test_widgets.py:12: AssertionError" in section
    # MUTATION: drop the summary-line marker -> red.
    assert "1 failed, 2300 passed" in section
    assert CLEANUP_COMMAND not in section


def test_a_log_that_names_no_failure_keeps_its_plain_tail_up_to_the_step_that_failed():
    lines = [f"building layer {i:04d} " + "-" * 30 for i in range(300)]
    lines += ["the last line of the step", "##[error]Process completed with exit code 2."]
    lines += ["Post job cleanup.", CLEANUP_COMMAND]
    raw = ("\n".join(lines) + "\n").encode()
    section = _section(_excerpt([_check(13, "build")], {13: raw}), "build")

    # MUTATION: return "" from `_log_failures` when no marker matches (no plain-tail fallback) -> red.
    assert "the last line of the step" in section and "building layer 0299" in section
    assert CLEANUP_COMMAND not in section


# --------------------------------------------------------------------------
# Which checks, in what order
# --------------------------------------------------------------------------

def test_a_gate_over_needs_sorts_after_the_jobs_it_restates():
    checks = [
        _check(1, "ci-gate"),
        _check(2, "swarm-ui component tests (2/4)"),
        _check(3, "gate"),
        _check(4, "swarm-ui component tests (4/4)"),
    ]
    logs = {1: _gate_job_log(), 2: _vitest_job_log(), 3: _gate_job_log(), 4: _vitest_job_log()}
    excerpt = _excerpt(checks, logs)

    at = {name: excerpt.index(f"## {name} (") for name in
          ("ci-gate", "gate", "swarm-ui component tests (2/4)", "swarm-ui component tests (4/4)")}
    # MUTATION: keep GitHub's order in `_excerpt_order` (no gate-last sort) -> red.
    assert at["swarm-ui component tests (2/4)"] < at["ci-gate"]
    assert at["swarm-ui component tests (4/4)"] < at["ci-gate"]
    assert at["swarm-ui component tests (4/4)"] < at["gate"]
    # The jobs that failed keep GitHub's order among themselves.
    assert at["swarm-ui component tests (2/4)"] < at["swarm-ui component tests (4/4)"]


def test_a_gate_does_not_take_a_slot_from_a_real_failure():
    checks = [_check(1, "ci-gate")] + [_check(10 + i, f"shard ({i}/5)") for i in range(1, 6)]
    excerpt = _excerpt(checks, {})

    # MUTATION: take `MAX_EXCERPT_CHECKS` before sorting the gate last -> red.
    for i in range(1, 6):
        assert f"## shard ({i}/5) (" in excerpt
    assert "## ci-gate (" not in excerpt
    assert "[1 more failing checks not shown]" in excerpt


def test_a_check_name_reported_twice_at_the_head_appears_once():
    name = "swarm-ui component tests (2/4)"
    checks = [_check(2, name), _check(5, name), _check(4, "swarm-ui component tests (4/4)")]
    excerpt = _excerpt(checks, {2: _vitest_job_log(), 5: _vitest_job_log(), 4: _vitest_job_log()})

    # MUTATION: drop the de-duplication by name in `_excerpt_order` -> red.
    assert excerpt.count(f"## {name} (") == 1
    assert "## swarm-ui component tests (4/4) (" in excerpt
    assert "more failing checks not shown" not in excerpt


# --------------------------------------------------------------------------
# The caps hold
# --------------------------------------------------------------------------

def test_each_checks_log_part_and_the_whole_excerpt_stay_within_their_caps():
    checks = [_check(20 + i, f"job {i}") for i in range(issueci.MAX_EXCERPT_CHECKS)]
    logs = {20 + i: (_vitest_job_log() if i % 2 else _pytest_job_log()) for i in range(issueci.MAX_EXCERPT_CHECKS)}
    writer = _Writer(logs)
    reading = CiReading(state=RED, failing_runs=checks)
    excerpt = issueci.excerpt_at(writer, REF, SHA, reading, secrets.token_hex(20), label="test")

    # MUTATION: bound the whole excerpt at more than `MAX_EXCERPT_BYTES` -> red.
    assert len(excerpt.encode("utf-8")) <= issueci.MAX_EXCERPT_BYTES
    # MUTATION: skip `_bound_lines` on the selected failure lines -> red.
    for i in range(2):
        section = _section(excerpt, f"job {i}")
        log_part = section.split(":\n", 1)[1] if ":\n" in section else ""
        assert len(log_part.encode("utf-8")) <= issueci.MAX_LOG_TAIL_BYTES
    # What is read is bounded too: never more than forgewrite will serve.
    assert writer.limits and all(limit <= issueci.LOG_READ_BYTES for limit in writer.limits)


def test_a_credential_in_a_failure_line_is_redacted():
    fake = "ghp_" + secrets.token_hex(18)
    raw = _vitest_job_log().replace(
        b"+ Received", ("+ Received GH_TOKEN=" + fake).encode()
    )
    excerpt = _excerpt([_check(30, "ui")], {30: raw})

    # MUTATION: redact only the plain tail, not the selected failure lines -> red.
    assert "+ Received" in excerpt and fake not in excerpt
