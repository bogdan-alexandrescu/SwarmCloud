"""CLAUDE.md is the agent-facing core; the long explanations live in a doc.

BGD, 2026-10-27 (#2524, step claude-md of bundle TOKEN-A): CLAUDE.md is in
every turn of every lane on this repository, and at 25.6 KB it was about 6.4k
tokens of cache reads per turn, laptop-only sections included. It keeps a core
-- the rules, and a one-line form of each laptop rule with a link -- and the
explanations move to docs/working-in-this-repository.md.

What is held here:
  * the core stays small (CORE_BYTES_MAX), so explanations do not drift back;
  * every moved section is still linked from CLAUDE.md, so no rule loses the
    place its reasons live (the anchors themselves are resolved by
    scripts/lib/check-doc-links.sh);
  * the moved text is still there, so "moved" never quietly means "dropped".

BREAK IT: paste the laptop finishing sequence back into CLAUDE.md (too big),
drop a `#laptop` link (unlinked), or delete the CI job table from the doc
(dropped). Each turns this red, naming what.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CLAUDE = REPO / "CLAUDE.md"
DOC = REPO / "docs" / "working-in-this-repository.md"

#: About 10 KB was the decision; the ten invariants and the track-ownership
#: paragraph stay verbatim, which put the core at 13.2 KB on 2026-10-27. The
#: old file was 25.6 KB, so this cap is red on it.
CORE_BYTES_MAX = 13_500

#: Each section of the doc CLAUDE.md must link to by anchor.
MOVED_SECTIONS = (
    "rule-zero",
    "unblocked-work",
    "track-ownership",
    "writing-scripts",
    "writing-terraform",
    "writing-python",
    "writing-docs",
    "workspace-storage",
    "laptop",
    "ci-jobs",
    "make-test-smoke-and-the-checklist",
    "swarmcloud-agent-tests",
    "lanes",
    "reporting",
    "issues",
)

#: Text whose long form moved: one string from each, so the move is not a loss.
MOVED_TEXT = (
    "gh run view <id> --log-failed",
    "| `accept.yml` |",
    "`make test` is fully offline",
    "about a minute for the guard set",
    "Measured 2026-09-26 across 773 agents",
    "An unverified claim in this repository ends",
    "AN EPIC CLOSES ON ITS COMMENTS, NEVER ON ITS INDEX.",
    "`empty_dir.medium` accepts",
    "| `apps/` | Track A (control plane) / Track B (worker + images) |",
)


def test_claude_md_is_the_small_core():
    size = len(CLAUDE.read_bytes())
    # MUTATION: paste a moved section back into CLAUDE.md -> red.
    assert size <= CORE_BYTES_MAX, (
        f"CLAUDE.md is {size} bytes, over {CORE_BYTES_MAX}: it is read on every turn of every lane; "
        "move the explanation to docs/working-in-this-repository.md and link it"
    )


def test_claude_md_links_every_moved_section():
    text = CLAUDE.read_text(encoding="utf-8")
    # MUTATION: drop any one `docs/working-in-this-repository.md#...` link -> red.
    unlinked = [a for a in MOVED_SECTIONS if f"(docs/working-in-this-repository.md#{a})" not in text]
    assert not unlinked, f"CLAUDE.md no longer links these moved sections: {unlinked}"


def test_the_moved_text_is_in_the_doc():
    assert DOC.is_file(), "docs/working-in-this-repository.md is where CLAUDE.md's explanations live"
    doc = DOC.read_text(encoding="utf-8")
    headings = {line.lstrip("#").strip().lower().replace(",", "").replace(" ", "-")
                for line in doc.splitlines() if line.startswith("#")}
    # MUTATION: rename or delete a section heading of the doc -> red.
    assert set(MOVED_SECTIONS) <= headings, sorted(set(MOVED_SECTIONS) - headings)
    # MUTATION: delete a moved block (the CI table, the epic rule) -> red.
    missing = [t for t in MOVED_TEXT if t not in doc]
    assert not missing, f"moved out of CLAUDE.md but not into the doc: {missing}"
