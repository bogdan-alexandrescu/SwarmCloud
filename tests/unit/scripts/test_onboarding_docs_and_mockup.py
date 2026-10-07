"""Onboarding, design lane C4T (#780): the doc and the mock-ups say what the owner asked.

WHY THIS EXISTS. Owner request 2026-10-07 (#780): an onboarding experience in
the console AND the sc plugin that hand-holds a user through git access,
enabling one or more GitHub orgs and choosing the repositories SwarmCloud may
read and write, acting AS THE USER, with token custody designed rather than
deferred. This lane writes the DESIGN (docs/onboarding.md) and the MOCK-UPS
(docs/web-ui/mockups/onboarding.html) only. A design that lost one of the
three mechanisms, a comparison criterion, a failure state's recovery copy,
the plugin transcript, an invariant or the frozen-contract draft would be
built without it; a mock-up that broke the house rules (12px floor, theme
toggle, honest dashes, placeholder names) would be copied into the console.

Like test_repo_index_design_doc.py, nothing here reads the code the design
describes beyond holding that every file it cites exists and every
`path::qualname` or `path` (`anchor`) resolves, and that it cites no line
numbers.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from .test_docs_spec_amendments import _assert_cites_resolve

REPO = Path(__file__).resolve().parents[3]
DESIGN = REPO / "docs" / "onboarding.md"
MOCKUP = REPO / "docs" / "web-ui" / "mockups" / "onboarding.html"

_CITE = re.compile(
    r"`((?:apps|terraform|kubernetes|scripts|tests|images|docs|plugin)/[\w./-]+\.\w+)`"
)

#: The onboarding steps, in order: one state machine for both surfaces (§2).
STEPS = ("signed_in", "github_connected", "orgs_enabled", "repos_chosen", "access_verified", "ready")

#: The failure states the brief names, each with its recovery copy (§2.3).
NAMED_FAILURES = ("SSO_NOT_AUTHORISED", "CLASSIC_PAT_BLOCKED", "ORG_APPROVAL_PENDING", "REPO_NOT_INSTALLED")

#: The mock-up's five subjects, each drawn two or three ways.
SUBJECTS = ("entry", "connect", "chooser", "verify", "access")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


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


def _part(prefix: str) -> str:
    text = _text(DESIGN)
    return _section(text, _heading(text, prefix))


# --------------------------------------------------------------------------
# the design document
# --------------------------------------------------------------------------


def test_the_design_doc_exists_and_is_dated():
    assert DESIGN.is_file(), "docs/onboarding.md is this lane's deliverable"
    head = "\n".join(_text(DESIGN).splitlines()[:12])
    assert "**Status:" in head, "the status line leads, as in docs/git-tokens.md"
    assert "2026-10-07" in head and "#780" in head
    assert "PROPOSED" in head, "nothing is built; the status must not claim otherwise"


@pytest.mark.parametrize(
    "prefix",
    [
        "## 1. The access mechanism",
        "## 2. The onboarding flow",
        "## 3. Data model, API, the worker's credential, Terraform",
        "## 4. The sc plugin flow",
        "## 5. Build plan",
        "## 6. Owner decisions",
    ],
)
def test_the_design_doc_has_every_part_the_brief_names(prefix):
    _heading(_text(DESIGN), prefix)


@pytest.mark.parametrize(
    "criterion",
    [
        "Acts as the user",
        "Per-org selection",
        "Per-repo selection",
        "SAML SSO orgs",
        "Org-admin approval",
        "Lifetime and rotation",
        "Revocation",
        "Where the secret lives",
        "Invariant 9",
        "Several users per tenant",
        "Effort",
    ],
)
def test_the_mechanism_comparison_scores_every_criterion(criterion):
    table = _part("### 1.2 ")
    row = re.search(rf"^\| {re.escape(criterion)} \|(.*)\|$", table, flags=re.M)
    assert row, f"the comparison has no row for {criterion!r}"
    cells = [c.strip() for c in row.group(1).split("|")]
    assert len(cells) >= 3 and all(cells), f"{criterion}: every option is scored, none left blank"


@pytest.mark.parametrize("option", ["(A)", "(B)", "(C)"])
def test_each_mechanism_is_written_out_with_what_an_org_admin_does(option):
    body = _part("### 1.1 ")
    assert re.search(rf"^\*\*{re.escape(option)}", body, flags=re.M), f"option {option} is not its own paragraph"
    admins = _part("### 1.4 ")
    assert re.search(rf"^\* \*\*{re.escape(option)}", admins, flags=re.M), f"{option}: no org-admin step"


def test_the_mechanism_section_recommends_one_with_reasons():
    body = _flat(_part("### 1.3 "))
    assert "Recommended: (B)" in body
    for needle in ("user access token", "refresh token", "installation", "Secret Manager", "fallback"):
        assert needle in body, needle


def test_the_custody_rules_are_stated_not_softened():
    body = _flat(_part("## 1. The access mechanism"))
    for needle in (
        "never in the repository",
        "public",
        "Job environment",
        "redact",
        "scripts/create-secrets.sh --stdin",
        "swarm-tenant-<tenant>-git",
    ):
        assert needle in body, needle


def test_the_flow_is_one_resumable_state_machine_for_both_surfaces():
    body = _part("## 2. The onboarding flow")
    flat = _flat(body)
    assert "resumable" in flat and "console" in flat and "plugin" in flat
    for step in STEPS:
        assert f"`{step}`" in body, step
    order = [body.index(f"`{step}`") for step in STEPS]
    assert order == sorted(order), "the steps are introduced in order"


@pytest.mark.parametrize("step", STEPS)
def test_each_step_names_its_verification_probe(step):
    table = _part("### 2.2 ")
    row = re.search(rf"^\| `{step}` \|(.*)\|$", table, flags=re.M)
    assert row, f"{step} has no row in the step table"
    assert len([c for c in row.group(1).split("|") if c.strip()]) >= 3, f"{step}: done-when, probe, failures"


@pytest.mark.parametrize(
    "probe",
    ["GET /user", "GET /user/installations", "page", "upload-pack", "receive-pack", "pull request"],
)
def test_the_probes_cover_token_reach_paging_clone_push_and_pr(probe):
    assert probe in _part("### 2.2 "), probe


@pytest.mark.parametrize("code", NAMED_FAILURES)
def test_each_named_failure_has_exact_recovery_copy(code):
    table = _part("### 2.3 ")
    row = re.search(rf"^\| `{code}` \|(.*)\|$", table, flags=re.M)
    assert row, f"{code} has no row"
    copy = row.group(1)
    assert re.search(r"“[^”]{30,}”", copy), f"{code}: the recovery copy is quoted, word for word"


def test_the_flow_says_how_to_add_or_remove_an_org_or_repo_later():
    body = _flat(_part("### 2.4 "))
    for needle in ("Add an org", "Remove an org", "Add a repository", "Remove a repository", "refused"):
        assert needle in body, needle


def test_the_flow_covers_several_users_in_one_tenant():
    body = _flat(_part("## 2. The onboarding flow"))
    assert "second user" in body and "submitted_by" in body


@pytest.mark.parametrize(
    "collection",
    ["onboarding/", "forge_connections/", "forge_orgs/", "forge_grants/", "forge_authorizations/", "git_tokens/"],
)
def test_the_data_model_names_each_document(collection):
    assert collection in _part("### 3.1 "), collection


@pytest.mark.parametrize(
    "route",
    [
        "GET /v1/onboarding",
        "POST /v1/onboarding/github/authorize",
        "POST /v1/onboarding/github/exchange",
        "GET /v1/access/orgs",
        "GET /v1/access/orgs/{owner}/repositories",
        "PUT /v1/access/grants/{repo_id}",
        "DELETE /v1/access/orgs/{owner}",
        "POST /v1/access/grants/{repo_id}/verify",
        "GET /v1/repositories/readable",
    ],
)
def test_the_api_sketch_names_each_route(route):
    assert route in _part("### 3.2 "), route


def test_no_route_serves_or_accepts_a_token_value_except_the_named_one():
    body = _flat(_part("### 3.2 "))
    assert "No route returns a token" in body
    assert "additive" in body


def test_the_worker_credential_section_carries_a_draft_contract_request():
    body = _part("### 3.3 ")
    flat = _flat(body)
    for needle in (
        "forge_credential",
        "Worker._git_token",
        "resolve_git_token",
        "submitted_by",
        "canonical_step_spec",
        "CREDENTIAL_MISSING",
    ):
        assert needle in flat, needle
    draft = _part("#### Draft contract request")
    for part in ("What is true today", "The requested change", "What it would break if accepted",
                 "If it is declined", "Invariants"):
        assert part in draft, part
    assert "not filed" in _flat(draft), "a draft, not an entry in contract-change-requests.md"


def test_the_terraform_list_goes_through_the_dev_iam_review_and_labels():
    body = _flat(_part("### 3.4 "))
    for needle in ("dev-iam", "managed-by=swarm-terraform", "Cloud Scheduler", "description",
                   "secretVersionAdder", "secretAccessor"):
        assert needle in body, needle


def test_the_migration_keeps_todays_tenant_token():
    body = _flat(_part("### 3.5 "))
    assert "swarm-tenant-<tenant>-git" in body
    assert "unchanged" in body


@pytest.mark.parametrize("number", range(1, 11))
def test_the_design_doc_argues_every_invariant(number):
    body = _part("### 3.6 ")
    hits = [p for p in re.split(r"\n\s*\n|\n(?=\d+\. )", body)
            if re.search(rf"^\s*\**{number}\.\s|\binvariant {number}\b", p, re.I | re.M)]
    assert hits, f"invariant {number} is never named"
    assert max(len(p.split()) for p in hits) >= 25, f"invariant {number} is named but not argued"


def test_the_plugin_flow_names_its_commands_and_bridge_tools():
    body = _part("## 4. The sc plugin flow")
    for needle in ("/sc:setup", "uv run sc setup", "uv run sc access", "swarm_setup_status",
                   "swarm_setup_connect", "swarm_setup_repos", "swarm_setup_verify", "swarm_access"):
        assert needle in body, needle


def test_the_plugin_transcript_hands_off_to_the_browser_fails_and_recovers():
    body = _part("### 4.3 ")
    fences = re.findall(r"```text\n(.*?)```", body, flags=re.S)
    assert fences, "the transcript is a fenced text block"
    transcript = "\n".join(fences)
    assert "browser" in transcript
    assert any(code in transcript for code in NAMED_FAILURES), "a named failure is shown"
    assert "resum" in transcript.lower(), "the recovery resumes the same state machine"
    for step in STEPS[1:]:
        assert step in transcript, f"the transcript never reaches {step}"
    assert len(transcript.splitlines()) >= 40, "a full first-time session, not a sketch"


def test_the_build_plan_is_lanes_with_disjoint_territories_per_phase():
    plan = _part("## 5. Build plan")
    rows = re.findall(r"^\| (OB\d+[a-z]?) \| (\d) \| ([^|]*) \| ([^|]*) \| ([^|]*) \|$", plan, flags=re.M)
    assert len(rows) >= 8, f"the phased plan lost lanes: {len(rows)}"
    phase0 = [r for r in rows if r[1] == "0"]
    assert phase0, "a phase 0 exists"
    assert any("next_page" in r[2] and "owner/repo" in r[2] for r in phase0), \
        "phase 0 pages the Register picker and accepts a typed owner/repo"
    by_phase: dict[str, dict[str, str]] = {}
    for lane, phase, _builds, territory, _needs in rows:
        files = re.findall(r"`([^`]+)`", territory)
        assert files, f"{lane} names no territory"
        seen = by_phase.setdefault(phase, {})
        for path in files:
            assert path not in seen, f"{path} is in both {seen.get(path)} and {lane} in phase {phase}"
            seen[path] = lane


def test_a_failed_connection_parks_at_admission_not_at_dispatch():
    # Dispatch runs after the lease is reserved: a credential check there
    # reserves a pool and releases it again (invariant 2), and no sweep would
    # return the park to READY (invariant 4). CREDENTIAL_MISSING is decided in
    # credentials.py, asked by loop.Scheduler._admit_one and its sweep.
    plan = _part("## 5. Build plan")
    ob6 = re.search(r"^\| OB6 \|.*$", plan, flags=re.M)
    assert ob6, "lane OB6 exists"
    assert "apps/scheduler/scheduler/credentials.py" in ob6.group(0)
    assert "apps/scheduler/scheduler/loop.py" in ob6.group(0)
    assert "dispatch.py" not in ob6.group(0)
    assert "before dispatch" not in _flat(_text(DESIGN)), "the connection check is at admission"
    assert "credential_for" in _part("### 3.3")


def test_no_lane_depends_on_a_lane_in_its_own_or_a_later_phase():
    plan = _part("## 5. Build plan")
    rows = re.findall(r"^\| (OB\d+[a-z]?) \| (\d) \|.*\| ([^|]*) \|$", plan, flags=re.M)
    phase = {lane: int(p) for lane, p, _ in rows}
    for lane, p, needs in rows:
        for dep in re.findall(r"\bOB\d+[a-z]?\b", needs):
            assert dep in phase, f"{lane} needs {dep}, which is not a lane"
            assert phase[dep] < int(p), f"{lane} (phase {p}) needs {dep} (phase {phase[dep]})"


def test_every_owner_decision_has_options_and_a_recommendation():
    body = _part("## 6. Owner decisions")
    decisions = re.split(r"^### ", body, flags=re.M)[1:]
    assert len(decisions) >= 6, f"only {len(decisions)} decisions"
    for d in decisions:
        title = d.splitlines()[0]
        assert title.rstrip().endswith("?"), f"{title!r} is asked as a question"
        options = re.findall(r"^\* \*\*\([a-z0-9]+\)", d, flags=re.M)
        assert 2 <= len(options) <= 3, f"{title!r}: {len(options)} options"
        assert "**Recommendation:**" in d, f"{title!r}: no recommendation"


def test_the_design_doc_links_its_mockup_and_its_siblings():
    text = _text(DESIGN)
    for link in ("web-ui/mockups/onboarding.html", "(git-tokens.md)", "(repo-index.md)",
                 "(merge-step.md)", "(contract-change-requests.md)"):
        assert link in text, link


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
    assert "saga" not in text.lower()


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
