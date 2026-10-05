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


# ==========================================================================
# Second design pass, lane RI0b (owner, 2026-10-04): AST symbols and LSP call
# graphs, a graph explorer, test selection for merge, and git tokens with a
# permissions view. A pass that dropped one of the owner's four decisions, a
# language server, one of the three merge policies, or the rule that a token
# is never served back would be built without it.
# ==========================================================================

GIT_TOKENS = REPO / "docs" / "git-tokens.md"
REVISED = "## Revised 2026-10-04 (owner): AST and LSP"


def _revised() -> str:
    return _section(_text(DESIGN), _heading(_text(DESIGN), REVISED))


def test_the_design_doc_carries_the_dated_owner_revision():
    _heading(_text(DESIGN), REVISED)
    head = "\n".join(_text(DESIGN).splitlines()[:30])
    assert "RI0b" in head, "the status line says a second pass revised the design"


@pytest.mark.parametrize(
    "needle",
    [
        "symbols",
        "call_edges",
        "symbol_test_map",
        "confidence",
        "tree-sitter",
        "pyright",
        "tsserver",
        "gopls",
        "terraform-ls",
    ],
)
def test_the_revision_names_every_new_layer_and_language_server(needle):
    assert needle in _revised(), needle


def test_every_edge_evidence_kind_is_named_in_the_index_section():
    body = _section(_text(DESIGN), _heading(_text(DESIGN), "## 2. "))
    for evidence in ("`lsp`", "`ast`", "`import`", "`naming`", "`co-change`"):
        assert evidence in body, evidence
    assert "confidence" in body
    assert "symbols" in body and "call_edges" in body, "the layers are folded into §2, not only listed"


def test_the_build_section_says_how_the_ast_and_lsp_passes_run():
    body = _section(_text(DESIGN), _heading(_text(DESIGN), "## 3. ")).lower()
    for needle in ("tree-sitter", "headless", "reverse dependencies", "budget", "timeout", "unsupported", "falls back"):
        assert needle in body, needle


def test_graph_shards_live_under_the_tenant_prefix():
    body = _section(_text(DESIGN), _heading(_text(DESIGN), "## 2. "))
    assert "shard" in body
    assert re.search(r"tenants/<tenant>/repos/<repo_id>/graph/<commit_sha>/", body), "per commit, per tenant"


def test_the_impact_query_goes_from_diff_to_a_test_plan_with_reasons():
    body = _section(_text(DESIGN), _heading(_text(DESIGN), "## 4. "))
    assert "POST /v1/repositories/{repo_id}/impact" in body
    for needle in ("changed symbols", "transitive callers", "depth", "reason", "pull request", "commit"):
        assert needle in body, needle


@pytest.mark.parametrize("policy", ["P1", "P2", "P3"])
def test_each_merge_policy_is_described_as_an_option_not_decided(policy):
    text = _text(DESIGN)
    match = re.search(rf"^\*\*\({policy}\)", text, flags=re.M)
    assert match, f"policy {policy} is not written out as its own option"


def test_test_selection_for_merge_says_how_green_reaches_the_forge_and_the_merge_step():
    body = _section(_text(DESIGN), _heading(_text(DESIGN), "### 4.4 "))
    for needle in (
        "swarmcloud/selected-tests",
        "check run",
        "required check",
        "#295",
        "M1a",
        "app_id",
        "workflow_dispatch",
        "GitHub Actions",
        "for the owner",
    ):
        assert needle in body, needle


def test_the_api_sketch_gains_the_graph_routes():
    body = _section(_text(DESIGN), _heading(_text(DESIGN), "## 6. "))
    for needle in (
        "GET /v1/repositories/{repo_id}/graph",
        "POST /v1/repositories/{repo_id}/impact",
        "GET /v1/repositories/{repo_id}/symbols",
        "selection_policy",
    ):
        assert needle in body, needle


def test_the_build_plan_gains_the_ast_lsp_lanes():
    body = _section(_text(DESIGN), _heading(_text(DESIGN), "## 7. "))
    ri3 = re.search(r"^\| RI3 \|.*$", body, flags=re.M)
    assert ri3 and "tree-sitter" in ri3.group(0), "RI3's extractor is now tree-sitter"
    lanes = re.findall(r"^\| RI\d+[a-z]? \|", body, flags=re.M)
    assert len(lanes) >= 14, f"the plan lost lanes: {len(lanes)}"
    for needle in ("pyright", "tsserver", "gopls", "terraform-ls", "graph storage", "impact",
                   "swarmcloud/selected-tests", "graph explorer", "git token"):
        assert needle in body, needle


def test_the_contract_requests_are_listed_not_made():
    body = _section(_text(DESIGN), _heading(_text(DESIGN), "### 6.3 "))
    for needle in ("(C)", "(D)", "contract-change-requests.md"):
        assert needle in body, needle


def test_the_design_doc_links_the_git_tokens_doc():
    assert "(git-tokens.md)" in _text(DESIGN)


# --------------------------------------------------------------------------
# git tokens
# --------------------------------------------------------------------------


def test_the_git_tokens_doc_exists_and_is_dated():
    assert GIT_TOKENS.is_file(), "docs/git-tokens.md is this lane's deliverable"
    head = "\n".join(_text(GIT_TOKENS).splitlines()[:12])
    assert "**Status:" in head and "PROPOSED" in head and "2026-10-04" in head


@pytest.mark.parametrize(
    "needle",
    [
        "swarm-tenant-<tenant>-git",
        "scripts/create-secrets.sh --stdin",
        "tenant default",
        "per repository",
        "per user",
        "user > repo > tenant",
        "repo > tenant",
        "CREDENTIAL_MISSING",
        "secret_name",
        "invariant 9",
        "merge step",
        "git-merge",
        "rotation",
        "expiry",
    ],
)
def test_the_git_tokens_registry_answers_every_question_the_owner_asked(needle):
    assert needle in _text(GIT_TOKENS), needle


@pytest.mark.parametrize(
    "needle",
    [
        "permissions",
        "X-OAuth-Scopes",
        "github-authentication-token-expiration",
        "clone",
        "push branches",
        "open pull requests",
        "read checks",
        "merge",
        "close issues",
        "read issues",
        "workflow_dispatch",
        "`ok`",
        "`missing`",
        "`unknown`",
        "last verified",
        "expires in",
        "aria-label",
        "copy",
        "redact",
    ],
)
def test_the_permissions_view_is_server_side_booleans_never_the_token(needle):
    assert needle in _text(GIT_TOKENS), needle


def test_the_git_tokens_doc_says_how_often_it_reverifies():
    text = _text(GIT_TOKENS)
    assert re.search(r"re-verif\w+ (every|each|daily|on)", text), "a re-verification cadence is stated"


def test_the_git_tokens_doc_lists_its_contract_requests_and_links_its_mockups():
    text = _text(GIT_TOKENS)
    assert "contract-change-requests.md" in text
    assert "web-ui/mockups/repositories.html" in text


@pytest.mark.parametrize("doc", [GIT_TOKENS], ids=lambda p: p.name)
def test_the_git_tokens_doc_cites_real_files_and_nothing_credential_shaped(doc: Path):
    text = _text(doc)
    assert not _CREDENTIAL_SHAPES.search(text)
    assert "agents-staging" not in text
    for path, line in _CITE.findall(text):
        target = REPO / path
        assert target.is_file(), f"{doc.name} cites {path}, which does not exist"
        if line:
            assert int(line) <= _text(target).count("\n") + 1, f"{path}:{line} is past its end"


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

def _x2_bullet() -> str:
    body = _section(_text(DESIGN), _heading(_text(DESIGN), "### 4.4 "))
    start = body.index("**(X2)")
    end = body.index("The choice of X1 or X2", start)
    return body[start:end]


def test_x2_posts_the_gate_through_the_checks_api_on_the_head_sha():
    # A workflow_dispatch run's own check runs land on the dispatched ref's
    # commit (the default branch), never on the PR head, so X2 can only gate
    # a merge if the workflow posts the check run itself.
    bullet = " ".join(_x2_bullet().split())
    for needle in ("POST /repos/{owner}/{repo}/check-runs", "head_sha", "checks: write",
                   "GITHUB_TOKEN", "default branch's HEAD", "not the gate", "commit status",
                   "M5"):
        assert needle in bullet, needle
    assert "is posted by GitHub Actions on the head sha" not in bullet


def test_the_x2_lane_and_the_gate_mockup_say_who_posts_the_check():
    plan = _section(_text(DESIGN), _heading(_text(DESIGN), "## 7. "))
    ri12 = re.search(r"^\| RI12 \|.*$", plan, flags=re.M)
    assert ri12 and "Checks API" in ri12.group(0) and "checks: write" in ri12.group(0)
    for vid in ("gate-a", "gate-b", "gate-c"):
        assert "Checks API step (X2)" in _variant(vid), vid


def test_index_versions_is_listed_once_in_the_api_sketch():
    body = _section(_text(DESIGN), _heading(_text(DESIGN), "## 6. "))
    assert body.count("repositories/{repo_id}/index_versions/{commit_sha}\n") == 1
    entry = body[body.index("repositories/{repo_id}/index_versions/{commit_sha}\n"):]
    entry = entry[: entry.index("\n\n")]
    for field in ("graph_manifest", "graph_digest", "languages"):
        assert field in entry, field


def test_no_lane_depends_on_a_lane_in_its_own_phase():
    plan = _section(_text(DESIGN), _heading(_text(DESIGN), "## 7. "))
    rows = re.findall(r"^\| (RI\d+[a-z]?) \| (\d) \|.*\| ([^|]*) \|$", plan, flags=re.M)
    phase = {lane: int(p) for lane, p, _ in rows}
    for lane, p, needs in rows:
        for dep in re.findall(r"\bRI\d+[a-z]?\b", needs):
            if dep in phase and phase[dep] == int(p):
                # an in-phase ordering is allowed only when the plan states it
                note = " ".join(plan.split())
                assert f"{lane} follows {dep} within phase {p}" in note, f"{lane} needs {dep}, same phase"
            elif dep in phase:
                assert phase[dep] < int(p), f"{lane} (phase {p}) needs {dep} (phase {phase[dep]})"


def test_the_probe_host_rule_is_not_claimed_to_exist_in_swarm_api():
    body = " ".join(_text(GIT_TOKENS).split())
    assert "apps/agent-worker/agent_worker/forge.py" in body
    assert "swarm-api equivalent" in body
    assert "the rule the issue fetch and the worker already use" not in body
