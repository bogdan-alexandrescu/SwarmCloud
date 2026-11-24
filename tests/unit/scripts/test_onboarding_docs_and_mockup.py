"""Onboarding, design lane C4T (archived issue 780): the doc and the mock-ups say what the owner asked.

WHY THIS EXISTS. Owner request 2026-10-07 (archived issue 780): an onboarding experience in
the console AND the sc plugin that hand-holds a user through git access,
enabling one or more GitHub orgs and choosing the repositories SwarmCloud may
read and write, acting AS THE USER, with token custody designed rather than
deferred. This lane writes the DESIGN (docs/onboarding.md) and the MOCK-UPS
(docs/web-ui/mockups/onboarding.html) only. A design that lost one of the
three mechanisms, a comparison criterion, a failure state's recovery copy,
the plugin transcript, an invariant or the frozen-contract draft would be
built without it; a mock-up that broke the house rules (12px floor, theme
toggle, honest dashes, placeholder names) would be copied into the console.

Nothing here reads the code the design describes.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from .test_no_live_literals import _listed
from .test_no_live_literals import guard

REPO = Path(__file__).resolve().parents[3]
DESIGN = REPO / "docs" / "onboarding.md"
MOCKUP = REPO / "docs" / "web-ui" / "mockups" / "onboarding.html"

#: The failure states the brief names, each with its recovery copy (§2.3).
NAMED_FAILURES = ("SSO_NOT_AUTHORISED", "CLASSIC_PAT_BLOCKED", "ORG_APPROVAL_PENDING", "REPO_NOT_INSTALLED")

#: The mock-up's five subjects, each drawn two or three ways.
SUBJECTS = ("entry", "connect", "chooser", "verify", "access")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


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


class _Ids(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.dashes: list[dict[str, str | None]] = []
        self.scripts_src: list[str] = []
        self.styles_href: list[str] = []

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


def _parsed() -> _Ids:
    parser = _Ids()
    parser.feed(_text(MOCKUP))
    return parser


def _variant(vid: str) -> str:
    """The HTML of one variant block, from its opening div to the next variant or section."""
    text = _text(MOCKUP)
    start = text.index(f'<div class="variant" id="{vid}">')
    nxt = re.search(r'<div class="variant" id=|<h2 ', text[start + 10:])
    return text[start: start + 10 + nxt.start()] if nxt else text[start:]


def _variants(subject: str) -> list[str]:
    ids = set(_parsed().ids)
    return [f"{subject}-{v}" for v in "abc" if f"{subject}-{v}" in ids]


def test_the_mockup_exists_and_is_self_contained():
    assert MOCKUP.is_file(), "docs/web-ui/mockups/onboarding.html is this lane's deliverable"
    parsed = _parsed()
    assert not parsed.scripts_src, "no external script: the page opens from disk"
    assert all(h.startswith("https://fonts.googleapis.com/") for h in parsed.styles_href)


@pytest.mark.parametrize("subject", SUBJECTS)
def test_the_mockup_draws_two_or_three_variants_of_each_subject(subject):
    ids = set(_parsed().ids)
    assert len(_variants(subject)) >= 2, f"{subject}: variants {_variants(subject)}"
    assert f"{subject}-d" not in ids


def test_the_mockup_keeps_the_12px_floor_and_no_caps_or_tracking():
    text = _text(MOCKUP)
    sizes = [float(s) for s in re.findall(r"font-size:\s*([\d.]+)px", text)]
    sizes += [float(s) for s in re.findall(r"font:[^;}\"]*?([\d.]+)px", text)]
    sizes += [float(s) for s in re.findall(r'font-size="([\d.]+)"', text)]
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
    for mark in ("s-running", "s-succeeded", "s-failed", "s-queued", "s-parked", "s-warn"):
        assert f'<symbol id="{mark}"' in text, mark


def test_the_mockup_dashes_carry_their_reason():
    dashes = _parsed().dashes
    assert len(dashes) >= 5, "honest dashes: an unknown figure is a dash with its reason"
    for dash in dashes:
        assert (dash.get("title") or "").strip(), f"a dash without a reason: {dash}"


def test_the_mockup_uses_placeholder_names_only():
    text = _text(MOCKUP)
    for needle in ("example-org", "example-user", "swarm.example.com", "Operator"):
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


@pytest.mark.parametrize("vid", ["entry-a", "entry-b", "entry-c"])
def test_each_entry_variant_draws_the_checklist(vid):
    if vid not in _parsed().ids:
        pytest.skip(f"{vid} not drawn")
    body = _variant(vid)
    for label in ("Connect GitHub", "Enable orgs", "Choose repositories", "Verify"):
        assert label in body, f"{vid}: {label}"


@pytest.mark.parametrize("subject", ["chooser"])
def test_each_chooser_variant_has_search_paging_and_read_write(subject):
    for vid in _variants(subject):
        body = _variant(vid)
        assert "i-search" in body or "Search" in body, f"{vid}: no search"
        assert re.search(r"Page \d|Next page|Load more", body), f"{vid}: no paging"
        assert "Read" in body and "Write" in body, f"{vid}: no per-repo read/write"
        assert "owner/repo" in body, f"{vid}: no typed owner/repo fallback"


def test_each_verify_variant_shows_clone_push_and_pull_request():
    for vid in _variants("verify"):
        body = _variant(vid)
        for check in ("Clone", "Push", "Pull request"):
            assert check in body, f"{vid}: {check}"


@pytest.mark.parametrize("code", NAMED_FAILURES)
def test_every_named_failure_is_drawn_somewhere(code):
    assert code in _text(MOCKUP), code


def test_each_access_variant_adds_and_removes_orgs_and_repos():
    for vid in _variants("access"):
        body = _variant(vid)
        assert "Add org" in body and "Remove" in body, vid
        assert "example-user" in body, f"{vid}: the personal account is one of the owners"


def test_each_connect_variant_says_where_the_token_goes_and_never_shows_one():
    for vid in _variants("connect"):
        body = _variant(vid)
        assert "Secret Manager" in body, vid
        for label in re.findall(r'aria-label="([^"]*)"', body):
            assert "token value" not in label.lower()
        assert "Copy token" not in body


def test_the_mockup_ends_with_an_all_screens_to_pick_summary():
    text = _text(MOCKUP)
    assert 'id="pick"' in text and "All screens to pick" in text
    summary = text[text.index('id="pick"'):]
    for subject in SUBJECTS:
        for vid in _variants(subject):
            assert f'href="#{vid}"' in summary, f"the summary misses {vid}"
    assert not re.search(r'<h2 id="(?!pick)', summary), "the summary is the page's last section"


def test_the_mockup_links_back_to_the_design_doc():
    assert 'href="../../onboarding.md"' in _text(MOCKUP)
