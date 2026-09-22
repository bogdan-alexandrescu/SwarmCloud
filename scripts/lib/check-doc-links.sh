#!/usr/bin/env bash
# Resolve every internal Markdown link in the repository's documentation.
#
# A broken *file* link is obvious the first time anyone clicks it. A broken
# *anchor* is not: GitHub silently lands the reader at the top of the page, so
# the link looks like it worked and the reader never sees the section they were
# sent to. Seven of them had accumulated here, all the same way -- the headings
# are numbered (`## 4. Cross-tenant escape: ...`) and the links were written
# against the unnumbered titles.
#
# Two of those were the deliberate hand-offs into the Preview-disk risk that
# CLAUDE.md says must not be softened. A hand-off that silently misses is a
# softening, so this is checked rather than proof-read.
#
# The slug rule implemented here is github-slugger's, which is what GitHub
# actually uses: lowercase, strip everything that is not a word character,
# whitespace or a hyphen, then replace EACH space with a hyphen -- so a heading
# containing an em dash yields a double hyphen, which is the detail that makes
# hand-written anchors wrong.
#
# Usage: scripts/lib/check-doc-links.sh

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

require_cmd python3

step "Documentation links"

python3 - "${REPO_ROOT}" <<'PY'
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
targets = [root / "README.md", root / "CLAUDE.md", root / "CONTRACT.md"]
targets += sorted((root / "docs").glob("*.md"))
targets += sorted((root / "kubernetes").glob("*.md"))
targets += sorted((root / "tests").rglob("*.md"))
targets = [p for p in targets if p.is_file()]

# github-slugger: lowercase, drop everything outside [\w\s-] (unicode word
# chars), then replace each space with '-'.
_STRIP = re.compile(r"[^\w\s-]", re.UNICODE)


def slug(heading: str) -> str:
    s = heading.strip().lower()
    s = _STRIP.sub("", s)
    return s.replace(" ", "-")


anchors: dict[Path, set[str]] = {}
for path in targets:
    seen: dict[str, int] = {}
    found: set[str] = set()
    in_fence = False
    for line in path.read_text().splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if not m:
            continue
        base = slug(m.group(2))
        n = seen.get(base, 0)
        seen[base] = n + 1
        found.add(base if n == 0 else f"{base}-{n}")
    anchors[path.resolve()] = found

link = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
bad = []
checked = 0


def _mask_fences(text: str) -> str:
    """Blank out fenced code, KEEPING the line structure so offsets still map.

    Blanking rather than deleting matters twice: a line number computed from
    the masked text still points at the real line, and a link inside a fenced
    example stays unchecked (it is an illustration, not a link).
    """
    out = []
    in_fence = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append("")
            continue
        out.append("" if in_fence else line)
    return "\n".join(out)


for path in targets:
    text = path.read_text()
    # SCANNED OVER THE WHOLE TEXT, NOT LINE BY LINE.
    #
    # This loop used to iterate `text.splitlines()` and run the regex against
    # one line at a time. A Markdown link may be wrapped by the author, and the
    # label side of `[...](...)` legally contains the newline:
    #
    #     you want [how to read a benchmark
    #     result](#how-to-read-a-benchmark-result)
    #
    # Such a link matched nothing, so it was never resolved and never counted
    # -- and the run still printed "0 broken", which reads as "every link is
    # fine" rather than "I did not look at that one". A wrong anchor written
    # that way survived the gate silently, which is this repository's own
    # defect class in the checker meant to catch it: both ends built, the seam
    # never executed. The fence mask keeps code examples excluded and keeps the
    # reported line number honest.
    masked = _mask_fences(text)
    for m in link.finditer(masked):
        lineno = masked.count("\n", 0, m.start()) + 1
        target = m.group(1)
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        filepart, _, frag = target.partition("#")
        dest = (path.parent / filepart).resolve() if filepart else path.resolve()
        checked += 1
        if not dest.exists():
            bad.append(f"{path.relative_to(root)}:{lineno}  {target}  -> no such file")
            continue
        if not frag:
            continue
        known = anchors.get(dest)
        if known is None:
            continue          # a Markdown file outside the scanned set
        if frag not in known:
            near = [a for a in sorted(known) if frag.lstrip("0123456789-") in a]
            hint = f"  (did you mean #{near[0]}?)" if near else ""
            bad.append(
                f"{path.relative_to(root)}:{lineno}  {target}  -> no such anchor{hint}"
            )

for b in bad:
    print(f"    {b}", file=sys.stderr)
print(f"{checked} internal link(s) checked, {len(bad)} broken", file=sys.stderr)
sys.exit(1 if bad else 0)
PY

ok "every internal documentation link resolves, anchors included"
