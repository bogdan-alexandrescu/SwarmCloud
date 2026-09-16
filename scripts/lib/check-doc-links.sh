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
for path in targets:
    text = path.read_text()
    in_fence = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for m in link.finditer(line):
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
