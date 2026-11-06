"""A CI-fix round hears about EVERY failing vitest file (#3170).

Measured on PR #3146, "swarm-ui component tests (1/4)": vitest said
`Tests  2 failed`, but the excerpt (`issueci.excerpt_at`) named one file.
The later failure, a `waitFor` timeout, printed a DOM dump of tens of KiB
under its FAIL header, so the earlier `phone.chrome.test.tsx` FAIL sat above
the 16 KiB the log read reaches (`forgewrite.MAX_LOG_TAIL_BYTES`), and even
inside it the dump would have filled the 3,000 bytes kept front-first. Both
CI-fix rounds fixed the one file they were told about.

Every log here is synthetic text shaped like that job's; nothing touches
GitHub. The fake writer serves a log's last `limit` bytes exactly as
`forgewrite.GitHubWriter.job_log_tail` does (a ranged read), and records
every `limit` asked for, so a test can tell a re-read from the first read.
"""

from __future__ import annotations

import secrets
from typing import Any

from swarm_api import forgewrite, issueci
from swarm_api.forgechecks import RED, CiReading
from swarm_api.validation import IssueRef

REF = IssueRef(owner="acme", repo="widgets", number=3146)
SHA = "d" * 40
NAME = "swarm-ui component tests (1/4)"
RUN_ID = 39_812_004_117
JOB_ID = 120_331_456_771

PHONE_FILE = "src/__tests__/phone.chrome.test.tsx"
PHONE_FAIL = f" FAIL  {PHONE_FILE} > #139 at 390: the first Agents row starts above the midpoint"
PHONE_ASSERTION = "AssertionError: expected 512 to be less than 422"
QA_FILE = "src/__tests__/qa1fix30.workflows.test.tsx"
QA_FAIL = f" FAIL  {QA_FILE} > the workflows page > shows the retry button on a failed step"
QA_ASSERTION = (
    "TestingLibraryElementError: Unable to find role=\"button\" and name \"Retry\" "
    "(waitFor timed out after 1000ms)"
)


def _stamp(i: int) -> str:
    return f"2026-11-03T22:1{i % 10}:{i % 60:02d}.{i:07d}Z "


def _lines_to(target: int, prefix: str) -> list[str]:
    """Filler lines adding up to about `target` bytes."""
    out, size, i = [], 0, 0
    while size < target:
        line = f"{prefix} {i:04d} " + "." * 40
        out.append(line)
        size += len(line) + len(_stamp(i)) + 1
        i += 1
    return out


def _dom_dump(target: int) -> list[str]:
    """A testing-library DOM dump of about `target` bytes, some single lines
    longer than the whole 3,000 bytes a section keeps."""
    out, size, i = ["Ignored nodes: comments, script, style", "<body>"], 0, 0
    while size < target:
        if i % 7 == 3:
            line = "        <svg d=\"" + "M0 0L1 1" * 500 + f"\" data-row=\"{i}\" />"
        else:
            line = f"      <div class=\"wf-step wf-step--{i:04d}\" data-state=\"failed\">" + "x" * 120
        out.append(line)
        size += len(line) + len(_stamp(i)) + 1
        i += 1
    return out + ["</body>"]


def _shard_log(*, tests_failed: int = 2, dump_bytes: int = 40_000) -> bytes:
    """PR #3146 shard 1/4's shape: the phone.chrome FAIL first, then the
    waitFor FAIL and its DOM dump, then the summary and the post-job cleanup."""
    head = ["##[group]Run npx vitest run --shard 1/4", "##[endgroup]"]
    head += _lines_to(5_000, " ✓ src/__tests__/passing.test.tsx > renders")
    # The run output names each failure once already; the Failed Tests section repeats it.
    head += [PHONE_FAIL, "   → expected 512 to be less than 422"]
    head += _lines_to(800, " ✓ src/__tests__/passing.test.tsx > renders more")
    head += [QA_FAIL, "   → Unable to find role=\"button\" and name \"Retry\""]
    head += _lines_to(800, " ✓ src/__tests__/passing.test.tsx > renders the rest")
    failures = [
        "⎯⎯⎯⎯⎯⎯⎯ Failed Tests 2 ⎯⎯⎯⎯⎯⎯⎯",
        "",
        PHONE_FAIL,
        PHONE_ASSERTION,
        "",
        "- Expected",
        "+ Received",
        "",
        "- 422",
        "+ 512",
        "",
        f" ❯ {PHONE_FILE}:88:31",
        "     87|     const row = await screen.findByTestId('agents-row-0')",
        "     88|     expect(row.getBoundingClientRect().top).toBeLessThan(422)",
        "       |                               ^",
        "",
        "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯[1/2]⎯",
        "",
        QA_FAIL,
        QA_ASSERTION,
        *_dom_dump(dump_bytes),
        f" ❯ {QA_FILE}:140:9",
        "",
        "⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯[2/2]⎯",
    ]
    summary = [
        "",
        " Test Files  2 failed | 141 passed (143)",
        f"      Tests  {tests_failed} failed | 2181 passed (2183)",
        "   Start at  22:14:02",
        "   Duration  61.20s",
        "",
        "##[error]Process completed with exit code 1.",
    ]
    cleanup = ["Post job cleanup."]
    cleanup += [f"[command]/usr/bin/git submodule foreach --recursive true {i:03d}" for i in range(20)]
    lines = head + failures + summary + cleanup
    return ("\n".join(_stamp(i) + line for i, line in enumerate(lines)) + "\n").encode("utf-8")


def _pytest_log() -> bytes:
    lines = ["##[group]Run uv run pytest tests/unit -q"]
    lines += _lines_to(30_000, "tests/unit/control_plane/test_passing.py .")
    lines += [
        "=========================== short test summary info ============================",
        "FAILED tests/unit/control_plane/test_widgets.py::test_sort_is_stable - assert [2, 1] == [1, 2]",
        "1 failed, 2300 passed in 211.04s (0:03:31)",
        "##[error]Process completed with exit code 1.",
        "Post job cleanup.",
    ]
    return ("\n".join(_stamp(i) + line for i, line in enumerate(lines)) + "\n").encode("utf-8")


class _Writer:
    """`GitHubWriter`'s two reads `_check_section` makes, from fixtures."""

    def __init__(self, logs: dict[int, bytes]):
        self.logs = logs
        self.limits: list[int] = []

    def check_annotations(self, ref: IssueRef, check_id: int, token: str) -> list[dict[str, Any]]:
        return []  # the vitest shard jobs carry none

    def job_log_tail(self, ref: IssueRef, job_id: int, token: str, *, limit: int,
                     cap: int = forgewrite.MAX_LOG_TAIL_BYTES) -> str | None:
        # forgewrite's own clamp: `limit` never above `cap` (forgewrite's test holds `cap`'s own).
        limit = max(1, min(int(limit), int(cap)))
        self.limits.append(limit)
        raw = self.logs.get(job_id)
        if not raw:
            return None
        text = raw[-limit:].decode("utf-8", errors="replace")
        if len(raw) > limit and "\n" in text:
            text = text.split("\n", 1)[1]
        return text


def _check(check_id: int, name: str, *, run_id: int | None = None) -> dict[str, Any]:
    check: dict[str, Any] = {
        "id": check_id,
        "name": name,
        "conclusion": "failure",
        "app": {"slug": "github-actions"},
        "output": {"title": None, "summary": None, "text": None},
    }
    if run_id is not None:
        check["details_url"] = f"https://github.com/acme/widgets/actions/runs/{run_id}/job/{check_id}"
    return check


def _excerpt(writer: _Writer, checks: list[dict[str, Any]]) -> str:
    reading = CiReading(state=RED, failing_runs=checks)
    return issueci.excerpt_at(writer, REF, SHA, reading, secrets.token_hex(20), label="test")


def _section(excerpt: str, name: str) -> str:
    start = excerpt.index(f"## {name} (")
    rest = excerpt[start + 3:]
    end = rest.find("\n## ")
    return rest if end < 0 else rest[:end]


def _log_part(section: str) -> str:
    return section.split("failures in the log:\n", 1)[1] if "failures in the log:\n" in section else ""


# --------------------------------------------------------------------------
# Every failing file reaches the excerpt
# --------------------------------------------------------------------------

def test_a_dom_dump_past_16_kib_does_not_hide_the_other_failing_file():
    raw = _shard_log()
    # The control: the 16 KiB today's read reaches holds neither FAIL header.
    assert PHONE_FAIL.encode() not in raw[-issueci.LOG_READ_BYTES:]
    assert QA_FAIL.encode() not in raw[-issueci.LOG_READ_BYTES:]
    writer = _Writer({JOB_ID: raw})
    excerpt = _excerpt(writer, [_check(JOB_ID, NAME, run_id=RUN_ID)])
    section = _section(excerpt, NAME)

    # MUTATION: read only `LOG_READ_BYTES` of the log's end (no re-read when fewer FAIL headers than `Tests N failed`) -> red.
    assert PHONE_FILE in section and QA_FILE in section
    # MUTATION: keep the blocks front-first with no per-block share (the DOM dump crowds out the rest) -> red.
    assert PHONE_ASSERTION in section and QA_ASSERTION in section
    # MUTATION: drop the `Tests N failed` line from what is kept after the blocks -> red.
    assert "Tests  2 failed" in section
    # MUTATION: append the not-shown line even when every header was kept -> red.
    assert "failures not shown" not in section
    # MUTATION: keep a block's remaining lines uncut (no `[... N lines of this block cut]`) -> red.
    assert "lines of this block cut]" in section
    # MUTATION: skip `_bound_lines`/the per-line cap on the kept failure text -> red.
    assert len(_log_part(section).encode("utf-8")) <= issueci.MAX_LOG_TAIL_BYTES
    # MUTATION: bound the whole excerpt at more than `MAX_EXCERPT_BYTES` -> red.
    assert len(excerpt.encode("utf-8")) <= issueci.MAX_EXCERPT_BYTES
    # MUTATION: re-read with no cap, or more than once -> red.
    assert writer.limits == [issueci.LOG_READ_BYTES, forgewrite.MAX_LOG_FULL_READ_BYTES]


def test_failures_still_unseen_are_counted_with_run_and_job():
    # vitest says 3 failed; even the full read finds two headers.
    raw = _shard_log(tests_failed=3)
    writer = _Writer({JOB_ID: raw})
    section = _section(_excerpt(writer, [_check(JOB_ID, NAME, run_id=RUN_ID)]), NAME)

    # MUTATION: drop the not-shown line when headers are still fewer than N -> red.
    assert f"[1 of 3 failures not shown: run {RUN_ID} job {JOB_ID}]" in section
    # MUTATION: give up on the found headers when the count is not met -> red.
    assert PHONE_FILE in section and QA_FILE in section
    # MUTATION: re-read more than once -> red.
    assert writer.limits == [issueci.LOG_READ_BYTES, forgewrite.MAX_LOG_FULL_READ_BYTES]

    # No run id to be had (no details_url, no stamp): the line names the job alone.
    writer = _Writer({JOB_ID: raw})
    section = _section(_excerpt(writer, [_check(JOB_ID, NAME)]), NAME)
    # MUTATION: print `run None` when `workflow_run_of` answers None -> red.
    assert f"[1 of 3 failures not shown: job {JOB_ID}]" in section
    assert "run None" not in section


def test_a_log_whose_headers_match_the_count_is_read_once():
    # A short dump: both headers sit inside the first 16 KiB read.
    raw = _shard_log(dump_bytes=1_500)
    assert len(raw) < issueci.LOG_READ_BYTES
    writer = _Writer({JOB_ID: raw})
    section = _section(_excerpt(writer, [_check(JOB_ID, NAME, run_id=RUN_ID)]), NAME)

    # MUTATION: re-read whenever a count is found, not only when headers are fewer -> red.
    assert writer.limits == [issueci.LOG_READ_BYTES]
    # MUTATION: count the run-output and Failed-Tests copies of a header twice (no dedupe) -> red.
    assert "failures not shown" not in section
    assert PHONE_FILE in section and QA_FILE in section


def test_a_pytest_log_is_read_once_as_before():
    writer = _Writer({JOB_ID: _pytest_log()})
    section = _section(_excerpt(writer, [_check(JOB_ID, "python", run_id=RUN_ID)]), "python")

    # MUTATION: re-read a log that carries no vitest `Tests N failed` line -> red.
    assert writer.limits == [issueci.LOG_READ_BYTES]
    # MUTATION: keep only vitest FAIL blocks, dropping pytest's failure lines -> red.
    assert "FAILED tests/unit/control_plane/test_widgets.py::test_sort_is_stable" in section
    assert "1 failed, 2300 passed" in section
    assert "failures not shown" not in section
