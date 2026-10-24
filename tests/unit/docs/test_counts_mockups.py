"""A counts mock-up names only runner profiles and pool accounts that exist and run.

Asked for by BGD on 2026-10-24 06:30Z (#2149, answering COUNTS-FIX3-E on
task_a668679281254d5f9822): "add a guard test in a follow-up lane".

WHY THIS IS A TEST. Counts proposal E drew `codex`, `claude-code-review` and a
codex pool account running agents in October. Both profiles are
`available=False` in the catalogue, and a pool account is a Claude
subscription, so none of that can have happened. The rendered reviews passed
it; CLD's spot-check caught it. A mock-up is the frame a page lane builds to,
so a profile that cannot run, drawn as one that did, becomes a series, a
legend entry and a test fixture in the page that follows.

WHAT IT READS. Every `docs/web-ui/mockups/counts-*.html`, as the text a reader
sees: attributes, `<script>` and `<style>` are not read, so a CSS class such as
`acct-action` is not a pool account. Three rules:

* An unavailable profile (read from `RUNNER_PROFILES`, never restated here) is
  refused wherever it is named, except in a block of text that also says
  "disabled" -- a note that says why the profile is absent. A series label
  (a cell that starts with a colour swatch) is a profile that ran agents, so
  "disabled" does not excuse it there.
* A pool account (`acct-...`) that is not `acct-claude-...` is refused: pool
  accounts are Claude subscriptions only.
* In a group of series labels (one table or list) that names at least one
  catalogue profile -- a by-profile breakdown -- every label must be a
  catalogue profile. That is where a name such as `live-browser` would be
  drawn as a profile that ran.

NEVER VACUOUS. With no counts mock-up on main the test skips and says so, and
a page whose by-profile breakdown the parser cannot find fails rather than
passing over nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import pytest
from swarm_common.profiles import RUNNER_PROFILES

REPO = Path(__file__).resolve().parents[3]
MOCKUPS = REPO / "docs" / "web-ui" / "mockups"
PAGES = sorted(MOCKUPS.glob("counts-*.html"))

UNAVAILABLE = frozenset(name for name, profile in RUNNER_PROFILES.items() if not profile.available)
KNOWN = frozenset(RUNNER_PROFILES)

#: A name is a whole token: a hyphen or a word character on either side makes
#: it part of a longer name (`codex` in `acct-codex-a`, `claude-code` in
#: `claude-code-review`).
_EDGE_BEFORE = r"(?<![\w-])"
_EDGE_AFTER = r"(?![\w-])"
_ACCOUNT = re.compile(_EDGE_BEFORE + r"acct-[a-z0-9][a-z0-9-]*")
_POOL_PREFIX = "acct-claude-"
_DISABLED = re.compile(r"\bdisabled\b", re.IGNORECASE)

#: Elements whose text is one block: a note's "disabled" excuses only the
#: names in its own block or the block just around it. `html` and `body` are
#: blocks so that loose text is read too.
_BLOCK = frozenset(
    "html body p li td th caption figcaption h1 h2 h3 h4 h5 h6 dt dd div blockquote pre "
    "summary label button legend".split()
)
#: Elements that group series labels: one legend, one breakdown table.
_GROUP = frozenset("table ul ol dl figure".split())
_UNREAD = frozenset(("script", "style"))
_VOID = frozenset("area base br col embed hr img input link meta source track wbr".split())


@dataclass
class Block:
    #: The text this block holds itself, its child blocks' text left out: a
    #: name is judged once, in the block that says it.
    own: str
    #: The block's whole text, its child blocks' text included.
    full: str
    #: True when the block's first child is a colour swatch, `<i class="sw">`:
    #: the label of one series in a chart or table.
    series: bool
    group: int
    #: The own text of the block it sits in: a note's "disabled" may sit one
    #: level out (`<div>Disabled: <ul><li>codex</li></ul></div>`).
    parent_own: str


@dataclass
class _Open:
    tag: str
    own: list[str] = field(default_factory=list)
    full: list[str] = field(default_factory=list)
    series: bool = False
    seen_content: bool = False
    group: int = 0
    children: list[Block] = field(default_factory=list)


def _squash(parts: list[str]) -> str:
    return " ".join("".join(parts).split())


class _Reader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self._stack: list[str] = []
        self._open: list[_Open] = []
        self._groups: list[int] = [0]
        self._group_count = 0
        self._unread = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID:
            return
        self._stack.append(tag)
        if tag in _UNREAD:
            self._unread += 1
            return
        if tag in _GROUP:
            self._group_count += 1
            self._groups.append(self._group_count)
        if tag == "i" and self._open and not self._open[-1].seen_content:
            classes = (dict(attrs).get("class") or "").split()
            if "sw" in classes:
                self._open[-1].series = True
        if self._open:
            self._open[-1].seen_content = True
        if tag in _BLOCK:
            self._open.append(_Open(tag, group=self._groups[-1]))

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID or tag not in self._stack:
            return
        while self._stack:
            top = self._stack.pop()
            self._close(top)
            if top == tag:
                break

    def close(self) -> None:
        super().close()
        while self._stack:
            self._close(self._stack.pop())
        for done in self._open:
            # Text outside every block: no element closes over it, so it is
            # judged with nothing around it.
            self._finish(done, parent_own="")
        self._open = []

    def _close(self, tag: str) -> None:
        if tag in _UNREAD:
            self._unread -= 1
            return
        if tag in _BLOCK and self._open and self._open[-1].tag == tag:
            done = self._open.pop()
            full = _squash(done.full)
            if self._open:
                # The parent's text reads on around its child.
                self._open[-1].full.append(" " + full + " ")
                self._open[-1].children.append(self._finish(done, parent_own=None))
            else:
                self._finish(done, parent_own="")
        if tag in _GROUP and len(self._groups) > 1:
            self._groups.pop()

    def _finish(self, done: _Open, parent_own: str | None) -> Block:
        block = Block(_squash(done.own), _squash(done.full), done.series, done.group, "")
        own = block.own
        for child in done.children:
            # A child learns its parent's own text only once the parent closes.
            child.parent_own = own
        if parent_own is not None:
            block.parent_own = parent_own
        if block.full:
            self.blocks.append(block)
        return block

    def handle_data(self, data: str) -> None:
        if self._unread or not self._open:
            return
        if data.strip():
            self._open[-1].seen_content = True
        self._open[-1].own.append(data)
        self._open[-1].full.append(data)


def read_blocks(html: str) -> list[Block]:
    reader = _Reader()
    reader.feed(html)
    reader.close()
    return reader.blocks


def _named(text: str, name: str) -> bool:
    return re.search(_EDGE_BEFORE + re.escape(name) + _EDGE_AFTER, text) is not None


_CLIP = 120


def _clip(text: str) -> str:
    return text if len(text) <= _CLIP else text[: _CLIP - 1] + "…"


def check_page(html: str) -> list[str]:
    """Every rule the page breaks, one line each; empty when it keeps them all."""
    blocks = read_blocks(html)
    problems: list[str] = []
    reported: set[tuple[str, str]] = set()

    def refuse(rule: str, name: str, line: str, where: str) -> None:
        # One line per rule and name, with the first place it was found: a
        # name drawn in eight frames is one defect, not eight.
        if (rule, name) not in reported:
            reported.add((rule, name))
            problems.append(f"{line}: {_clip(where)!r}")

    for block in blocks:
        for name in sorted(UNAVAILABLE):
            if not _named(block.own, name):
                continue
            if block.series:
                refuse("series", name, f"unavailable profile {name!r} drawn as a series that ran agents", block.full)
            elif not (_DISABLED.search(block.own) or _DISABLED.search(block.parent_own)):
                refuse("named", name, f"unavailable profile {name!r} named as one that ran agents", block.full)
        for account in _ACCOUNT.findall(block.own):
            if not account.startswith(_POOL_PREFIX):
                refuse("account", account, f"pool account {account!r} is not {_POOL_PREFIX}*", block.full)

    groups: dict[int, list[Block]] = {}
    for block in blocks:
        if block.series:
            groups.setdefault(block.group, []).append(block)
    for labels in groups.values():
        if not any(label.full in KNOWN for label in labels):
            continue
        for label in labels:
            if label.full not in KNOWN:
                refuse("unknown", label.full, f"series {label.full!r} in a by-profile breakdown is not a catalogue profile", label.full)
    return problems


def profile_series(html: str) -> list[str]:
    """The series labels that are catalogue profiles: what the page draws as ran."""
    return sorted({b.full for b in read_blocks(html) if b.series and b.full in KNOWN})


# --- the checker catches what proposal E drew ---------------------------------

# Built from the catalogue rather than written out, so the fixture names an
# unavailable profile however the catalogue changes. `codex` is the one the
# brief names; the assertion below holds it to being unavailable.
_CODEX = "codex"
_FIXTURE = f"""<!doctype html><html><head><style>.acct-action{{color:red}}</style></head><body>
<figure><table class="legend"><thead><tr><th>Profile</th><th>Oct 2026</th></tr></thead><tbody>
<tr><td><i class="sw" style="background:var(--s1)"></i>claude-code</td><td>177</td></tr>
<tr><td><i class="sw" style="background:var(--s2)"></i>{_CODEX}</td><td>12</td></tr>
<tr><td><i class="sw" style="background:var(--s3)"></i>live-browser</td><td>4</td></tr>
</tbody></table></figure>
<table><thead><tr><th>Month</th><th>Profile</th><th>Agents</th></tr></thead><tbody>
<tr><td>Oct 2026</td><td>{_CODEX}</td><td>12</td></tr></tbody></table>
<table><tbody>
<tr><td><i class="sw" style="background:var(--s1)"></i>acct-claude-01</td><td>9</td></tr>
<tr><td><i class="sw" style="background:var(--s2)"></i>acct-codex-a</td><td>3</td></tr>
</tbody></table>
<p class="note">No {_CODEX} series: the profile is disabled (<code>available=False</code>).</p>
<div class="acct-action">acct-claude-02 drains at the window's end.</div>
<script>var p = "acct-script-only {_CODEX}";</script>
Loose text: acct-loose-1 ran.
</body></html>"""


def test_the_fixture_names_a_profile_the_catalogue_marks_unavailable() -> None:
    assert _CODEX in RUNNER_PROFILES, "the fixture's profile left the catalogue: pick another"
    assert _CODEX in UNAVAILABLE, "the fixture's profile is available now: pick an unavailable one"


def test_codex_in_an_october_bucket_is_refused() -> None:
    problems = check_page(_FIXTURE)
    # Once as a series, and once as a plain cell in the October row: a cell
    # holds no "disabled", and the note that does is not around it.
    assert any(f"{_CODEX!r} drawn as a series" in p for p in problems), problems
    assert f"unavailable profile {_CODEX!r} named as one that ran agents: {_CODEX!r}" in problems, problems


def test_a_codex_pool_account_is_refused() -> None:
    problems = check_page(_FIXTURE)
    assert any("'acct-codex-a' is not acct-claude-*" in p for p in problems), problems
    # Text outside every element but <body> is read too.
    assert any("'acct-loose-1' is not acct-claude-*" in p for p in problems), problems


def test_a_name_outside_the_catalogue_in_a_profile_breakdown_is_refused() -> None:
    problems = check_page(_FIXTURE)
    assert any("'live-browser'" in p and "not a catalogue profile" in p for p in problems), problems


def test_a_disabled_note_attributes_and_scripts_are_not_refused() -> None:
    problems = check_page(_FIXTURE)
    assert not any("No codex series" in p for p in problems), problems
    assert not any("acct-action" in p or "acct-script-only" in p for p in problems), problems
    assert not any(p.startswith(f"pool account '{_POOL_PREFIX}") for p in problems), problems
    assert not any("'claude-code'" in p for p in problems), problems


def test_a_page_that_keeps_every_rule_passes() -> None:
    clean = (
        '<table><tbody><tr><td><i class="sw"></i>claude-code</td><td>1</td></tr>'
        '<tr><td><i class="sw"></i>mock</td><td>2</td></tr></tbody></table>'
        '<table><tbody><tr><td><i class="sw"></i>acct-claude-01</td></tr></tbody></table>'
        f"<ul><li>{_CODEX} and claude-code-review are disabled, so neither is drawn.</li></ul>"
    )
    assert check_page(clean) == []


# --- the counts mock-ups on main ------------------------------------------------


def _page_ids() -> list[str]:
    return [p.name for p in PAGES]


@pytest.mark.skipif(not PAGES, reason="no docs/web-ui/mockups/counts-*.html on main: nothing to check")
@pytest.mark.parametrize("page", PAGES, ids=_page_ids())
def test_counts_mockup_names_only_profiles_and_accounts_that_run(page: Path) -> None:
    html = page.read_text(encoding="utf-8")
    # Never vacuous: a page whose by-profile breakdown the reader cannot find
    # would pass every rule by reading nothing.
    assert profile_series(html), f"{page.name}: no by-profile series found; the reader no longer sees the page"
    problems = check_page(html)
    assert not problems, f"{page.name}:\n" + "\n".join(problems)
