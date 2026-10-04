"""Repository index, design lane RI0: the doc and the mock-ups say what the owner asked.

WHY THIS EXISTS. Owner request 2026-10-04: register repositories per tenant,
have an agent index each one on an interval or when its default branch moves,
hand that index to the issue-run planner (#454) and to any step working in the
repository, and answer "which tests does this diff need". This lane writes the
DESIGN (docs/repo-index.md) and the MOCK-UPS (docs/web-ui/mockups/
repositories.html) only, so the owner can pick before anything is built. A
design that lost a part of the request, an invariant or the frozen-contract
consequence would be built without it; a mock-up that broke the house rules
(12px floor, theme toggle, honest dashes, placeholder names) would be copied
into the console.

Nothing here reads the code the design describes beyond checking that every
file it cites exists and that a cited line is inside the file. Line numbers
are deliberately not pinned tighter than that: issueruns.py and forge.py are
edited by other lanes every wave.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
DESIGN = REPO / "docs" / "repo-index.md"
MOCKUPS = REPO / "docs" / "web-ui" / "mockups"
MOCKUP = MOCKUPS / "repositories.html"

_CITE = re.compile(
    r"`((?:apps|terraform|kubernetes|scripts|tests|images|docs|plugin)/[\w./-]+\.\w+)(?::(\d+))?`"
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The body under `heading`, up to the next heading of the same or a higher level."""
    start = text.index(heading)
    level = len(heading) - len(heading.lstrip("#"))
    rest = text[start + len(heading):]
    nxt = re.search(rf"^#{{1,{level}}} ", rest, flags=re.M)
    return rest[: nxt.start()] if nxt else rest


def _heading(text: str, prefix: str) -> str:
    match = re.search(rf"^{re.escape(prefix)}.*$", text, flags=re.M)
    assert match, f"no heading starting {prefix!r}"
    return match.group(0)


# --------------------------------------------------------------------------
# the design document
# --------------------------------------------------------------------------


def test_the_design_doc_exists_and_is_dated():
    assert DESIGN.is_file(), "docs/repo-index.md is this lane's deliverable"
    head = "\n".join(_text(DESIGN).splitlines()[:12])
    assert "**Status:" in head, "the status line leads, as in docs/merge-step.md"
    assert "2026-10-04" in head, "the design is dated"
    assert "PROPOSED" in head, "nothing is built; the status must not claim otherwise"


@pytest.mark.parametrize(
    "prefix",
    [
        "## 1. A registered repository",
        "## 2. The index",
        "## 3. How the index is built",
        "## 4. How the index is consumed",
        "## 5. Staleness",
        "## 6. API sketch",
        "## 7. Build plan",
    ],
)
def test_the_design_doc_has_every_part_the_brief_names(prefix):
    _heading(_text(DESIGN), prefix)


def test_the_design_doc_ties_a_registration_to_todays_repository_handling():
    """Section 1 must say how it relates to what swarm-api does today, by name."""
    body = _section(_text(DESIGN), _heading(_text(DESIGN), "## 1. "))
    for needle in (
        "check_repository_url",       # the one repository_url rule
        "IssueRef",                   # how an issue run names its repository
        "read_open_work",             # how an issue run reads the forge
        "swarm-tenant-<tenant>-git",  # the credential, by name, never a value
        "default_branch",
        "allowed_profiles",
        "owner/repo",
    ):
        assert needle in body, needle


def test_the_design_doc_names_what_the_index_contains_and_its_budget():
    body = _section(_text(DESIGN), _heading(_text(DESIGN), "## 2. "))
    for needle in (
        "modules",
        "entry_points",
        "routes",
        "test_map",
        "territory",
        "commands",
        "hot_spots",
        "commit_sha",
        "tenants/<tenant>/",          # invariant 9: under the tenant's own prefix
        "KiB",                        # a size budget, stated in a unit
    ):
        assert needle in body, needle


@pytest.mark.parametrize("number", [1, 2, 3, 9, 10])
def test_the_design_doc_argues_each_invariant_the_feature_touches(number):
    """Indexing is capacity-accounted work (1-3), lives under the tenant (9) and
    is requested by name (10). Each must be argued, not merely listed."""
    text = _text(DESIGN)
    hits = [p for p in re.split(r"\n\s*\n", text) if re.search(rf"\binvariant {number}\b", p, re.I)]
    assert hits, f"invariant {number} is never named"
    assert max(len(p.split()) for p in hits) >= 30, f"invariant {number} is named but not argued"


def test_the_design_doc_has_both_triggers_and_says_incremental_or_full():
    body = _section(_text(DESIGN), _heading(_text(DESIGN), "## 3. "))
    for needle in ("interval", "poll", "webhook", "incremental", "full", "runner profile"):
        assert needle in body.lower(), needle


def test_the_design_doc_names_the_consumers_and_the_tests_query():
    body = _section(_text(DESIGN), _heading(_text(DESIGN), "## 4. "))
    for needle in ("#454", "planner", "by name", "merge step", "changed paths", "implement", "fix"):
        assert needle in body, needle


def test_the_design_doc_staleness_rule_says_the_index_is_behind_head():
    body = _section(_text(DESIGN), _heading(_text(DESIGN), "## 5. "))
    assert "behind" in body and "head" in body
    assert "minor" in body.lower(), "security notes are minors, functionality first"


def test_the_design_doc_api_sketch_names_routes_documents_and_the_frozen_request():
    body = _section(_text(DESIGN), _heading(_text(DESIGN), "## 6. "))
    for needle in (
        "POST /v1/repositories",
        "GET /v1/repositories",
        "GET /v1/repositories/{repo_id}/index",
        "POST /v1/repositories/{repo_id}/tests:select",
        "repositories/{repo_id}",
        "repo_index_runs",
        "profiles.py",
        "contract-change-requests.md",
    ):
        assert needle in body, needle


def test_the_design_doc_build_plan_is_a_table_of_lanes():
    body = _section(_text(DESIGN), _heading(_text(DESIGN), "## 7. "))
    lanes = re.findall(r"^\| RI\d+ \|", body, flags=re.M)
    assert len(lanes) >= 4, "the phased plan lost lanes"


def test_the_design_doc_links_its_mockups():
    assert "web-ui/mockups/repositories.html" in _text(DESIGN)


@pytest.mark.parametrize("doc", [DESIGN, MOCKUP], ids=lambda p: p.name)
def test_every_cited_file_exists_in_doc_and_mockup(doc: Path):
    for path, line in _CITE.findall(_text(doc)):
        target = REPO / path
        assert target.is_file(), f"{doc.name} cites {path}, which does not exist"
        if line:
            length = _text(target).count("\n") + 1
            assert int(line) <= length, f"{doc.name} cites {path}:{line}, past its end"


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
    assert "agents-staging" not in _text(doc)


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


def test_the_mockup_exists_and_is_self_contained():
    assert MOCKUP.is_file(), "docs/web-ui/mockups/repositories.html is this lane's deliverable"
    parsed = _parsed()
    assert not parsed.scripts_src, "no external script: the page opens from disk"
    # The only stylesheet the other mock-ups load is the webfont.
    assert all(h.startswith("https://fonts.googleapis.com/") for h in parsed.styles_href)


@pytest.mark.parametrize("subject", ["list", "register", "detail", "context"])
def test_the_mockup_draws_two_or_three_variants_of_each_subject(subject):
    ids = set(_parsed().ids)
    variants = [v for v in "abc" if f"{subject}-{v}" in ids]
    assert len(variants) >= 2, f"{subject}: variants {variants}"
    assert "d" not in {i.rsplit("-", 1)[-1] for i in ids if i.startswith(f"{subject}-")}


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
    assert "dataset.theme" in text or "setAttribute('data-theme'" in text


def test_the_mockup_uses_the_brand_state_marks():
    text = _text(MOCKUP)
    for mark in ("s-running", "s-succeeded", "s-failed", "s-queued", "s-parked"):
        assert f'<symbol id="{mark}"' in text, mark


def test_the_mockup_dashes_carry_their_reason():
    dashes = _parsed().dashes
    assert len(dashes) >= 3, "honest dashes: an unknown figure is a dash with its reason"
    for dash in dashes:
        assert (dash.get("title") or "").strip(), f"a dash without a reason: {dash}"


def test_the_mockup_uses_placeholder_names_only():
    text = _text(MOCKUP)
    for needle in ("example-org/example-api", "swarm.example.com", "Operator"):
        assert needle in text, needle
    assert "saga" not in text.lower()


@pytest.mark.parametrize(
    "needle",
    ["Default branch", "Index", "Last indexed", "Schedule", "Tests mapped", "Register repository",
     "Context used", "Hot-spots", "Index runs", "Change trigger"],
)
def test_the_mockup_draws_every_element_the_brief_names(needle):
    assert needle in _text(MOCKUP), needle


def test_the_mockup_links_back_to_the_design_doc():
    assert 'href="../../repo-index.md"' in _text(MOCKUP)
