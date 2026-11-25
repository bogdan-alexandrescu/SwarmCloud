# source-text: fixture - reads only the repo-index design doc, its mock-up and the deployment pattern list; the checker selects it through a variable name that elsewhere in the file holds a .py path (deployment_values.py, imported, not read)
"""Repository index, design lane RI0: the doc and the mock-ups say what the owner asked.

WHY THIS EXISTS. Owner request 2026-10-04: register repositories per tenant,
have an agent index each one on an interval or when its default branch moves,
hand that index to the issue-run planner (archived issue 454) and to any step working in the
repository, and answer "which tests does this diff need". This lane writes the
DESIGN (docs/repo-index.md) and the MOCK-UPS (docs/web-ui/mockups/
repositories.html) only, so the owner can pick before anything is built. A
design that lost a part of the request, an invariant or the frozen-contract
consequence would be built without it; a mock-up that broke the house rules
(12px floor, theme toggle, honest dashes, placeholder names) would be copied
into the console.

Nothing here reads the code the design describes beyond checking that every
file it cites exists and that every `path::qualname` or `path` (`anchor`) it
cites resolves. It cites no line numbers (lane CITD): issueruns.py and
forge.py are edited by other lanes every wave, and a line number drifts with
each edit where a symbol or an anchor fails only when it is gone.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

from .test_docs_spec_amendments import _assert_cites_resolve
from .test_no_live_literals import guard

REPO = Path(__file__).resolve().parents[3]
DESIGN = REPO / "docs" / "repo-index.md"
MOCKUPS = REPO / "docs" / "web-ui" / "mockups"
MOCKUP = MOCKUPS / "repositories.html"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered first: a dataclass resolves its annotations through sys.modules.
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


guard = _load("swarm_guards_deployment_values", REPO / "scripts" / "guards" / "deployment_values.py")
placeholders = _load("swarm_tests_placeholders", REPO / "tests" / "placeholders.py")


def _listed() -> list:
    """The deployment pattern list's entries, as tests/unit/scripts/test_no_live_literals.py
    reads them; skips when no list is given (this repository holds none)."""
    path = os.environ.get(guard.LIST_FILE_VARIABLE)
    text = Path(path).read_text(encoding="utf-8") if path else os.environ.get(guard.LIST_VARIABLE)
    if not text or not text.strip():
        pytest.skip("no deployment pattern list (DEPLOYMENT_PATTERNS or DEPLOYMENT_PATTERNS_FILE): "
                    "nothing says what is live")
    return guard.parse_list(text).entries


def _assert_names_no_shared_resource(text: str) -> None:
    """Offline: not even the placeholder never-touch names (the shared project's
    resources, placeholders.PEER) appear; the live names are the pattern list's."""
    for name in placeholders.PEER.values():
        assert name not in text, name

_CITE = re.compile(
    r"`((?:apps|terraform|kubernetes|scripts|tests|images|docs|plugin)/[\w./-]+\.\w+)`"
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# citations and security scans, over the design doc and the mock-up
# --------------------------------------------------------------------------


@pytest.mark.parametrize("doc", [DESIGN, MOCKUP], ids=lambda p: p.name)
def test_every_cited_file_exists_in_doc_and_mockup(doc: Path):
    for path in _CITE.findall(_text(doc)):
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
    _assert_names_no_shared_resource(_text(doc))


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
    # Nothing in it that the deployment-values guard's shape rules read as a deployment's value.
    source = guard.Source(MOCKUP.relative_to(REPO).as_posix(), MOCKUP.read_text(encoding="utf-8"))
    assert not guard.scan([source], guard.Allow.load(), [], archive_refs=False, address=None)
    # The company and the live deployment's names: test_docs_name_nothing_the_pattern_list_lists.
    _assert_names_no_shared_resource(text)


@pytest.mark.parametrize(
    "needle",
    ["Default branch", "Index", "Last indexed", "Schedule", "Tests mapped", "Register repository",
     "Context used", "Hot-spots", "Index runs", "Change trigger"],
)
def test_the_mockup_draws_every_element_the_brief_names(needle):
    assert needle in _text(MOCKUP), needle


def test_the_mockup_links_back_to_the_design_doc():
    assert 'href="../../repo-index.md"' in _text(MOCKUP)


# ==========================================================================
# Second design pass, lane RI0b (owner, 2026-10-04): AST symbols and LSP call
# graphs, a graph explorer, test selection for merge, and git tokens with a
# permissions view. A pass that dropped one of the owner's four decisions, a
# language server, one of the three merge policies, or the rule that a token
# is never served back would be built without it.
# ==========================================================================

GIT_TOKENS = REPO / "docs" / "git-tokens.md"


# --------------------------------------------------------------------------
# git tokens
# --------------------------------------------------------------------------


@pytest.mark.parametrize("doc", [GIT_TOKENS], ids=lambda p: p.name)
def test_the_git_tokens_doc_cites_real_files_and_nothing_credential_shaped(doc: Path):
    text = _text(doc)
    assert not _CREDENTIAL_SHAPES.search(text)
    for path in _CITE.findall(text):
        assert (REPO / path).is_file(), f"{doc.name} cites {path}, which does not exist"
    _assert_cites_resolve(doc)


# --------------------------------------------------------------------------
# the mock-up page, second pass
# --------------------------------------------------------------------------

_NEW_SUBJECTS = ["graph", "impact", "gate", "settings", "tokens", "perms"]
_ALL_SUBJECTS = ["list", "register", "detail", "context", *_NEW_SUBJECTS]


def _variant(vid: str) -> str:
    """The HTML of one variant block, from its opening div to the next variant or section."""
    text = _text(MOCKUP)
    start = text.index(f'<div class="variant" id="{vid}">')
    nxt = re.search(r'<div class="variant" id=|<h2 ', text[start + 10:])
    return text[start: start + 10 + nxt.start()] if nxt else text[start:]


@pytest.mark.parametrize("subject", _NEW_SUBJECTS)
def test_the_mockup_draws_two_or_three_variants_of_each_new_subject(subject):
    ids = set(_parsed().ids)
    variants = [v for v in "abc" if f"{subject}-{v}" in ids]
    assert len(variants) >= 2, f"{subject}: variants {variants}"
    assert f"{subject}-d" not in ids


@pytest.mark.parametrize("vid", ["graph-a", "graph-b", "graph-c"])
def test_each_graph_variant_is_drawn_as_static_svg(vid):
    body = _variant(vid)
    assert "<svg" in body and ("<line" in body or "<path" in body), "a graph is drawn, not described"
    for view in ("module", "call graph", "test map"):
        assert view in body.lower(), f"{vid} lacks the {view} view"
    assert "lsp" in body and "ast" in body, "edge evidence is shown"


def test_the_mockup_graph_text_keeps_the_12px_floor_inside_svg_too():
    sizes = [float(s) for s in re.findall(r'font-size="([\d.]+)"', _text(MOCKUP))]
    assert not [s for s in sizes if s < 12], "an SVG font-size attribute is below the floor"


@pytest.mark.parametrize("vid", ["impact-a", "impact-b"])
def test_each_impact_variant_goes_from_diff_to_tests_with_reasons(vid):
    body = _variant(vid)
    for needle in ("changed symbols", "callers", "tests to run", "because"):
        assert needle in body.lower(), f"{vid}: {needle}"


def test_the_impact_views_cover_a_commit_and_a_pull_request():
    both = _variant("impact-a") + _variant("impact-b")
    assert "Commit" in both and "Pull request" in both


@pytest.mark.parametrize("vid,policy", [("gate-a", "P1"), ("gate-b", "P2"), ("gate-c", "P3")])
def test_each_gate_variant_is_one_merge_policy(vid, policy):
    body = _variant(vid)
    assert policy in body
    assert "swarmcloud/selected-tests" in body
    assert "selected tests" in body.lower() and "full suite" in body.lower()
    assert "/sc:" in body, "the Claude Code-adjacent surface is drawn too"


def test_the_p3_gate_draws_its_fallback_reason():
    assert "fallback" in _variant("gate-c").lower()


@pytest.mark.parametrize(
    "needle",
    ["pyright", "tsserver", "gopls", "terraform-ls", "unsupported", "failing", "Graph depth",
     "Selection policy", "Resolved token"],
)
def test_the_settings_variants_draw_every_element_the_brief_names(needle):
    assert needle in _variant("settings-a") + _variant("settings-b"), needle


def test_the_token_pages_show_scopes_and_never_a_value():
    both = _variant("tokens-a") + _variant("tokens-b")
    for needle in ("Tenant", "Repository", "User", "Expires", "Last verified", "Repos covered",
                   "Register token", "Rotate", "last 4"):
        assert needle in both, needle
    assert "never shown again" in both.lower()


@pytest.mark.parametrize(
    "capability",
    ["Clone", "Push branches", "Open PRs", "Read checks", "Merge", "Close issues", "Read issues",
     "workflow_dispatch"],
)
def test_the_permission_matrix_has_every_capability(capability):
    assert capability in _variant("perms-a"), capability


@pytest.mark.parametrize("vid", ["perms-a", "perms-b", "perms-c"])
def test_each_permission_variant_marks_ok_missing_and_unknown(vid):
    body = _variant(vid)
    for mark in ("ok", "missing", "unknown"):
        assert re.search(rf"\b{mark}\b", body), f"{vid}: {mark}"
    assert 'class="dash"' in body or "unknown" in body


def test_no_aria_label_or_copy_button_on_a_token_page_carries_a_token_shape():
    for vid in ("tokens-a", "tokens-b", "perms-a", "perms-b", "perms-c"):
        body = _variant(vid)
        for label in re.findall(r'aria-label="([^"]*)"', body):
            assert not _CREDENTIAL_SHAPES.search(label)
            assert "token value" not in label.lower()
        assert "Copy token" not in body


def test_the_mockup_ends_with_an_all_screens_to_pick_summary():
    text = _text(MOCKUP)
    assert 'id="pick"' in text and "All screens to pick" in text
    summary = text[text.index('id="pick"'):]
    for subject in _ALL_SUBJECTS:
        ids = [i for i in _parsed().ids if re.fullmatch(rf"{subject}-[abc]", i)]
        assert ids, subject
        for vid in ids:
            assert f'href="#{vid}"' in summary, f"the summary misses {vid}"
    assert not re.search(r'<h2 id="(?!pick)', summary), "the summary is the page's last section"


def test_the_mockup_links_the_git_tokens_doc():
    assert 'href="../../git-tokens.md"' in _text(MOCKUP)


# --------------------------------------------------------------------------
# review findings on the second pass
# --------------------------------------------------------------------------

def test_the_x2_lane_and_the_gate_mockup_say_who_posts_the_check():
    for vid in ("gate-a", "gate-b", "gate-c"):
        assert "Checks API step (X2)" in _variant(vid), vid


@pytest.mark.parametrize("doc", [DESIGN, MOCKUP, GIT_TOKENS], ids=lambda p: p.name)
def test_docs_name_nothing_the_pattern_list_lists(doc: Path):
    """The live deployment: its company, project and the shared project's
    never-touch resources, read from the pattern list rather than spelled here."""
    entries = _listed()
    text = _text(doc)
    named = sorted({entry.line for entry in entries if entry.pattern.search(text)})
    assert not named, f"{doc.name} names the live deployment (pattern list lines {named})"
