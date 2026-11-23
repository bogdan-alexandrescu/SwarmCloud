"""Lane queue, design lane LQD: the doc and the mock-ups say what the owner asked.

WHY THIS EXISTS. Owner decision 2026-10-05 (history V1, the lane-queue epic): DESIGN
FIRST, no product code. The history analysis counted 141 implement/review/fix
lanes orchestrated from the operator's laptop -- a spec generator, PR
watchers, a merge watcher, a watchdog and a launch queue kept in memory notes
-- against 3 native issue runs. This lane writes the DESIGN
(docs/lane-queue.md) and the MOCK-UPS (docs/web-ui/mockups/lane-queue.html)
so the owner can pick before anything is built. The lane outcome ledger
(its own design) was folded into the same design by the owner the same day.

A design that lost one of the brief's parts (merge dependencies, territory
locks with seam files, the CI-fix loop, ready-labelling, issue closing,
verify-only lanes, the minors sink, the laptop mapping, the data model, the
routes, the scheduler change, an invariant, the build plan, the ledger) would
be built without it; a mock-up that broke the house rules (12px floor, theme
toggle, honest dashes, placeholder names) would be copied into the console.

Nothing here reads the code the design describes beyond checking that every
file it cites exists and that every `path::qualname` or `path` (`anchor`) it
cites resolves. It cites no line numbers (lane CITD): issueruns.py, issueci.py
and runs.py are edited by other lanes every wave, and a line number drifts
with each edit where a symbol or an anchor fails only when it is gone.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from .test_no_live_literals import _listed
from .test_docs_spec_amendments import _SYMBOL_CITE, _assert_cites_resolve
from .test_no_live_literals import guard

REPO = Path(__file__).resolve().parents[3]
DESIGN = REPO / "docs" / "lane-queue.md"
MOCKUP = REPO / "docs" / "web-ui" / "mockups" / "lane-queue.html"

_CITE = re.compile(
    r"`((?:apps|terraform|kubernetes|scripts|tests|images|docs|plugin|\.github)/[\w./-]+\.\w+)`"
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# citations and security scans, over the design doc and the mock-up
# --------------------------------------------------------------------------


@pytest.mark.parametrize("doc", [DESIGN, MOCKUP], ids=lambda p: p.name)
def test_every_cited_file_exists_in_doc_and_mockup(doc: Path):
    cited = _CITE.findall(_text(doc))
    if doc is DESIGN:
        symbols = _SYMBOL_CITE.findall(_text(doc))
        assert len(cited) + len(symbols) >= 10, "the design cites the code it generalises"
    for path in cited:
        assert (REPO / path).is_file(), f"{doc.name} cites {path}, which does not exist"
    _assert_cites_resolve(doc)


# --------------------------------------------------------------------------
# both: placeholders only, nothing credential-shaped, nothing on the deny-list
# --------------------------------------------------------------------------

_CREDENTIAL_SHAPES = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)


@pytest.mark.parametrize("doc", [DESIGN, MOCKUP], ids=lambda p: p.name)
def test_doc_and_mockup_carry_nothing_credential_shaped(doc: Path):
    assert not _CREDENTIAL_SHAPES.search(_text(doc))


@pytest.mark.parametrize("doc", [DESIGN, MOCKUP], ids=lambda p: p.name)
def test_doc_and_mockup_name_nothing_on_the_shared_project_deny_list(doc: Path):
    assert "peer-cluster" not in _text(doc)
    # The live values (the shared project's resources among them) come from the
    # deployment pattern list, read the way test_no_live_literals.py reads it;
    # with no list this skips and says why. Reported by list line, never value.
    text = _text(doc)
    hits = [f"list:{e.line}" for e in _listed() if e.pattern.search(text)]
    assert not hits, f"{doc.name} names listed values: {hits}"


# --------------------------------------------------------------------------
# the mock-up page
# --------------------------------------------------------------------------


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.dashes: list[dict[str, str | None]] = []
        self.scripts_src: list[str] = []
        self.styles_href: list[str] = []
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids.append(a["id"])
        if "dash" in (a.get("class") or "").split():
            self.dashes.append(a)
        if tag == "script" and a.get("src"):
            self.scripts_src.append(a["src"])
        if tag == "link" and a.get("rel") == "stylesheet":
            self.styles_href.append(a.get("href") or "")
        if tag == "a" and a.get("href"):
            self.hrefs.append(a["href"])


def _parsed() -> _Page:
    parser = _Page()
    parser.feed(_text(MOCKUP))
    return parser


def test_the_mockup_exists_and_is_self_contained():
    assert MOCKUP.is_file(), "docs/web-ui/mockups/lane-queue.html is this lane's deliverable"
    parsed = _parsed()
    assert not parsed.scripts_src, "no external script: the page opens from disk"
    assert all(h.startswith("https://fonts.googleapis.com/") for h in parsed.styles_href)


#: subject -> the fewest variants the brief asks for. The ledger is "2 variants".
SUBJECTS = {"board": 2, "detail": 2, "add": 2, "ledger": 2}


@pytest.mark.parametrize("subject", sorted(SUBJECTS))
def test_the_mockup_draws_the_variants_the_brief_asks_for(subject):
    ids = set(_parsed().ids)
    variants = [v for v in "abc" if f"{subject}-{v}" in ids]
    assert len(variants) >= SUBJECTS[subject], f"{subject}: variants {variants}"
    assert f"{subject}-d" not in ids, "two or three variants, not more"


def test_the_mockup_ends_with_all_screens_to_pick_linking_every_variant():
    parsed = _parsed()
    assert "pick" in parsed.ids
    text = _text(MOCKUP)
    tail = text[text.index('<h2 id="pick"'):]
    assert "All screens to pick" in tail
    variant_ids = [i for i in parsed.ids if re.fullmatch(r"(board|detail|add|ledger)-[abc]", i)]
    for vid in variant_ids:
        assert f'href="#{vid}"' in tail, f"the pick table does not link {vid}"
    later = re.findall(r'<h2 id="([^"]+)"', tail)
    assert later == ["pick"], f"'All screens to pick' is the last section, not {later}"


def test_the_mockup_keeps_the_12px_floor_and_no_caps_or_tracking():
    text = _text(MOCKUP)
    sizes = [float(s) for s in re.findall(r"font-size:\s*([\d.]+)px", text)]
    sizes += [float(s) for s in re.findall(r"font:[^;}\"]*?([\d.]+)px", text)]
    assert sizes, "the size scan found nothing; the regex is wrong, not the page right"
    assert min(sizes) >= 12, f"below the 12px floor: {sorted(set(s for s in sizes if s < 12))}"
    assert "letter-spacing" not in text
    assert "uppercase" not in text


def test_the_mockup_has_a_light_dark_system_toggle():
    text = _text(MOCKUP)
    for mode in ("light", "dark", "system"):
        assert f'data-set-theme="{mode}"' in text, mode
    assert "dataset.theme" in text


def test_the_mockup_uses_the_brand_state_marks():
    text = _text(MOCKUP)
    for mark in ("s-running", "s-succeeded", "s-failed", "s-queued", "s-parked"):
        assert f'<symbol id="{mark}"' in text, mark


def test_the_mockup_dashes_carry_their_reason():
    dashes = _parsed().dashes
    assert len(dashes) >= 4, "honest dashes: an unknown figure is a dash with its reason"
    for dash in dashes:
        assert (dash.get("title") or "").strip(), f"a dash without a reason: {dash}"


def test_the_mockup_uses_placeholder_names_only():
    text = _text(MOCKUP)
    for needle in ("example-org/example-api", "swarm.example.com", "Operator"):
        assert needle in text, needle
    # Nothing in it that the deployment-values guard's shape rules read as a deployment's value.
    source = guard.Source(MOCKUP.relative_to(REPO).as_posix(), MOCKUP.read_text(encoding="utf-8"))
    assert not guard.scan([source], guard.Allow.load(), [], archive_refs=False, address=None)


def test_the_mockup_names_nothing_on_the_pattern_list():
    # The company half of the placeholder check, from the deployment pattern
    # list (case-insensitive); skips, saying why, with no list.
    text = _text(MOCKUP)
    hits = [f"list:{e.line}" for e in _listed() if e.pattern.search(text)]
    assert not hits, f"{MOCKUP.name} names listed values: {hits}"


@pytest.mark.parametrize(
    "needle",
    [
        "Depends on",
        "Territory",
        "Seam",
        "CI fix round",
        "ready",
        "Review minors",
        "Verify only",
        "Add lanes",
        "Lane ledger",
        "CI reds by job",
        "Waiting",
        "Cost",
    ],
)
def test_the_mockup_draws_every_element_the_brief_names(needle):
    assert needle in _text(MOCKUP), needle


def test_the_mockup_links_back_to_the_design_doc():
    assert "../../lane-queue.md" in _parsed().hrefs
