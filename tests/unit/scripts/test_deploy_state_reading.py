"""docs/DEPLOY_STATE.md carries the newest dated reading, not the 2026-09-24 one.

WHY THIS EXISTS. #1822 comment 14: the record was dated 2026-09-24 and said the
services were a laptop deploy of `7c52762`, a month after the release workflow
started deploying main. CLAUDE.md cites this file as the dated record of what
is deployed, so a reading nobody refreshes reads as today's state. The
operator re-read it live on 2026-10-27 between 05:52 and 06:12 UTC (bundle
H18); these tests hold the doc to that reading:

  * the header is dated with the read date and window;
  * the four-sentence summary names the commit the services run (`17d6c5abd`),
    not the laptop deploy;
  * the 2026-09-24 reading survives as a dated "previous reading" note, which
    CLAUDE.md's "Issues" section still quotes;
  * the two states the triage asked to be read are recorded: `swarm-authprobe`
    and the `swarm-issue-sweep-u-bogdan` scheduler job, both absent.

Offline: reads only the markdown file.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DOC = REPO / "docs" / "DEPLOY_STATE.md"

READ_DATE = "2026-10-27"
READ_WINDOW = "between 05:52 and 06:12 UTC"
DEPLOYED_COMMIT = "17d6c5abd"
LAPTOP_COMMIT = "7c52762"


def _text() -> str:
    return DOC.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The body under a markdown heading, up to the next heading of the same or a higher level."""
    start = text.index(heading)
    level = len(heading) - len(heading.lstrip("#"))
    body = text[start + len(heading):]
    for line_start, line in _lines_with_offsets(body):
        stripped = line.lstrip("#")
        hashes = len(line) - len(stripped)
        if 0 < hashes <= level and stripped.startswith(" "):
            return body[:line_start]
    return body


def _lines_with_offsets(body: str):
    offset = 0
    for line in body.splitlines(keepends=True):
        yield offset, line.rstrip("\n")
        offset += len(line)


def test_the_header_is_dated_with_the_read_date_and_window():
    header = _text().split("\n## ", 1)[0]
    assert f"**Measured {READ_DATE} {READ_WINDOW}" in header, header


def test_the_summary_names_the_commit_main_deployed_not_the_laptop_deploy():
    summary = _section(_text(), "## In four sentences")
    assert DEPLOYED_COMMIT in summary, summary
    assert LAPTOP_COMMIT not in summary, summary
    assert "release workflow" in summary, summary


def test_the_2026_09_24_reading_is_kept_as_a_dated_previous_reading():
    previous = _section(_text(), "## Previous reading: 2026-09-24")
    assert LAPTOP_COMMIT in previous, previous
    assert "laptop deploy" in previous, previous


def test_swarm_authprobe_and_the_issue_sweep_job_are_recorded_as_read():
    text = _text()
    authprobe = _section(text, "## In the project, not in terraform")
    assert "`swarm-authprobe` is absent" in authprobe, authprobe
    assert "Cannot find service [swarm-authprobe]" in authprobe, authprobe
    ticks = _section(text, "## Scheduler ticks")
    assert "`swarm-issue-sweep-u-bogdan` is absent" in ticks, ticks
    assert "NOT_FOUND" in ticks, ticks
