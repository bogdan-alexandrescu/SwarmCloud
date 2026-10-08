"""Lane queue, design lane LQD: the doc and the mock-ups say what the owner asked.

WHY THIS EXISTS. Owner decision 2026-10-05 (history V1, epic #640): DESIGN
FIRST, no product code. The history analysis counted 141 implement/review/fix
lanes orchestrated from the operator's laptop -- a spec generator, PR
watchers, a merge watcher, a watchdog and a launch queue kept in memory notes
-- against 3 native issue runs. This lane writes the DESIGN
(docs/lane-queue.md) and the MOCK-UPS (docs/web-ui/mockups/lane-queue.html)
so the owner can pick before anything is built. The lane outcome ledger
(#639) was folded into the same design by the owner the same day.

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

from .test_docs_spec_amendments import _SYMBOL_CITE, _assert_cites_resolve

REPO = Path(__file__).resolve().parents[3]
DESIGN = REPO / "docs" / "lane-queue.md"
MOCKUP = REPO / "docs" / "web-ui" / "mockups" / "lane-queue.html"

_CITE = re.compile(
    r"`((?:apps|terraform|kubernetes|scripts|tests|images|docs|plugin|\.github)/[\w./-]+\.\w+)`"
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


def _body(prefix: str) -> str:
    text = _text(DESIGN)
    return _section(text, _heading(text, prefix))


# --------------------------------------------------------------------------
# the design document
# --------------------------------------------------------------------------


def test_the_design_doc_exists_and_is_dated_and_proposed():
    assert DESIGN.is_file(), "docs/lane-queue.md is this lane's deliverable"
    head = "\n".join(_text(DESIGN).splitlines()[:16])
    assert "**Status:" in head, "the status line leads, as in docs/repo-index.md"
    assert "2026-10-05" in head, "the design is dated"
    assert "PROPOSED" in head, "nothing is built; the status must not claim otherwise"
    for ref in ("#640", "#639", "LQD"):
        assert ref in head, ref


SECTIONS = [
    "## 1. Why",
    "## 2. The lane",
    "## 3. Depending on another lane's merge",
    "## 4. Territory locks",
    "## 5. After the pull request opens",
    "## 6. Verify-only lanes",
    "## 7. The review-minors sink",
    "## 8. The laptop today, and what is retired",
    "## 9. Data model",
    "## 10. Routes",
    "## 11. Scheduler changes",
    "## 12. The invariants",
    "## 13. The lane outcome ledger",
    "## 14. The frozen contract",
    "## 15. Build plan",
    "## 16. Open questions for the owner",
]


@pytest.mark.parametrize("prefix", SECTIONS)
def test_the_design_doc_has_every_part_the_brief_names(prefix):
    _heading(_text(DESIGN), prefix)


def test_the_why_carries_the_measurements_it_rests_on():
    body = _body("## 1. ")
    for needle in ("141", "3 native issue runs", "12 of 40", "#588", "#589", "re-typed"):
        assert needle in body, needle


def test_the_lane_generalises_issue_runs_by_name():
    """Section 2 must say what it generalises, by the code's own names."""
    body = _body("## 2. ")
    for needle in (
        "routes/runs.py",
        "issueruns.py",
        "IssueRun",
        "RunState",
        "advance_run",
        "advance_tenant_runs",
        "compile_plan",
        "#454",
    ):
        assert needle in body, needle


def test_the_lane_states_are_named_and_none_is_a_task_state():
    body = _body("## 2. ")
    for state in ("WAITING", "QUEUED", "RUNNING", "CHECKING", "FIXING", "READY",
                  "MERGED", "VERIFIED", "BLOCKED", "FAILED", "CANCELLED"):
        assert f"`{state}`" in body, state
    assert "TaskState" in body, "the lane machine must say it is not the frozen TaskState"


def test_a_dependency_is_on_a_merge_not_a_finish():
    body = _body("## 3. ")
    for needle in ("depends_on", "merged", "merge_commit_sha", "BLOCKED", "cycle", "closed without merging"):
        assert needle in body, needle


def test_territory_locks_come_from_the_plan_and_name_the_seam_files():
    body = _body("## 4. ")
    for needle in (
        "files",                 # the plan step's touched files
        "seam",
        "swarm_api/main.py",
        "App.tsx",
        "prefix",
        "one Firestore transaction",
        "released",
        "rebase round",
    ):
        assert needle in body, needle


@pytest.mark.parametrize("option", ["T1", "T2", "T3"])
def test_each_seam_policy_is_written_out_as_an_option(option):
    assert re.search(rf"^\*\*\({option}\)", _body("## 4. "), flags=re.M), option


def test_after_the_pull_request_opens_reuses_the_ci_loop_labels_and_closes():
    body = _body("## 5. ")
    for needle in (
        "#545",
        "issueci.py",
        "fix_rounds",
        "ci-fix.yml",
        "`ready`",
        "auto-merge.yml",
        "verdict.json",
        "Closes #N",
        "part of #N",
        "status comment",
        "[swarm] task_",
    ):
        assert needle in body, needle


def test_verify_only_lanes_say_what_an_empty_diff_means():
    body = _body("## 6. ")
    for needle in ("allow_empty_diff", "collect", "published_nothing", "VERIFIED", "empty_diff"):
        assert needle in body, needle


def test_the_minors_sink_follows_the_epic_rule():
    body = _body("## 7. ")
    for needle in ("minors", "epic", "one comment per finding", "call site", "blocker", "major"):
        assert needle in body, needle


@pytest.mark.parametrize(
    "piece",
    ["spec generator", "PR watcher", "merge watcher", "watchdog", "launch queue", "memory notes"],
)
def test_every_laptop_piece_is_mapped_and_its_fate_is_said(piece):
    body = _body("## 8. ")
    assert piece in body, piece
    assert "Retired" in body and "Kept" in body


def test_the_data_model_names_every_document():
    body = _body("## 9. ")
    for needle in (
        "lanes/{lane_id}",
        "lane_territories/",
        "lane_ledger/{lane_id}",
        "lane_minors/",
        "tenant_id",
        "depends_on",
        "territory",
        "seams",
        "allow_empty_diff",
        "issue",
        "pull_request",
    ):
        assert needle in body, needle


def test_the_routes_are_named():
    body = _body("## 10. ")
    for needle in (
        "POST /v1/lanes",
        "GET /v1/lanes",
        "GET /v1/lanes/{lane_id}",
        "POST /v1/lanes/{lane_id}:cancel",
        "POST /v1/lanes/{lane_id}:retry",
        "POST /v1/admin/lanes/advance",
        "GET /v1/lanes/ledger",
        "tenant_scope",
    ):
        assert needle in body, needle


def test_the_scheduler_change_names_the_job_and_its_label():
    body = _body("## 11. ")
    for needle in (
        "terraform/modules/scheduler/jobs.tf",
        "lane_queue_advance",
        "managed-by=swarm-terraform",
        "apps/scheduler",
    ):
        assert needle in body, needle


@pytest.mark.parametrize("number", range(1, 11))
def test_every_invariant_is_addressed_by_its_own_heading(number):
    """Invariants 1-10, each by name: a missing one is one nobody decided."""
    body12 = _body("## 12. ")
    match = re.search(rf"^### Invariant {number}\b.*$", body12, flags=re.M)
    assert match, f"invariant {number} has no heading in section 12"
    body = _section(body12, match.group(0))
    assert len(body.split()) >= 40, f"invariant {number} is named but not argued"


def test_the_ledger_has_every_field_the_owner_named():
    body = _body("## 13. ")
    for needle in (
        "cost_usd",
        "attempts",
        "wall_seconds",
        "waiting",
        "verdict",
        "findings",
        "pull_request",
        "ci_reds",
        "merged_at",
        "issues_closed",
        "issues_left",
        "GET /v1/lanes/ledger",
        "GET /v1/outcomes",      # the task ledger it must not be confused with
        "null",
    ):
        assert needle in body, needle


def test_the_ledger_has_a_console_tab():
    body = _body("## 13. ")
    assert "Work › Lanes" in body and "Ledger" in body


def test_the_frozen_contract_section_writes_the_request_and_does_not_file_it():
    body = _body("## 14. ")
    for needle in ("apps/common/swarm_common/", "contract-change-requests.md", "not filed by this lane"):
        assert needle in body, needle


def test_the_build_plan_is_a_table_of_lanes():
    lanes = re.findall(r"^\| LQ\d+ \|", _body("## 15. "), flags=re.M)
    assert len(lanes) >= 6, "the phased plan lost lanes"


def test_reviewed_after_a_fix_has_a_rereview_and_a_failed_transition():
    """compile_plan has no review after `fix`, so a lane whose review said NOT_YET
    could never be READY unless the design says what "reviewed" means after it."""
    ready = _body("### 5.2 ")
    for needle in ("rereview", "final review", "review.round", "NOT_YET", "issueci._merge"):
        assert needle in ready, needle
    states = _body("### 2.2 ")
    assert "final review NOT_YET" in states, "CHECKING has no way out when the final review says NOT_YET"
    assert "`round`" in _body("## 9. "), "the data model lost review.round"


def test_a_retried_dependency_returns_its_blocked_dependants_to_waiting():
    assert "BLOCKED  --its blocked_by lane retried" in _body("### 2.2 ")


def test_a_failed_lane_keeps_its_lock_while_its_pull_request_is_open():
    assert "keeps its lock while its pull request is open" in _body("### 4.2 ")
    row = re.search(r"^\| `FAILED` \|.*$", _body("### 2.2 "), flags=re.M)
    assert row and "while its pull request is open" in row.group(0)


def test_build_lanes_that_edit_the_same_file_are_serialised():
    """CLAUDE.md: two issues that edit the same file are one lane, or run one after the other."""
    rows = {}
    for line in _body("## 15. ").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 4 and re.fullmatch(r"LQ\d+", cells[0]):
            rows[cells[0]] = (set(re.findall(r"`([^`]+)`", cells[2])), cells[3])

    def ancestors(lane: str) -> set[str]:
        seen: set[str] = set()
        todo = [lane]
        while todo:
            for dep in re.findall(r"LQ\d+", rows[todo.pop()][1]):
                if dep not in seen:
                    seen.add(dep)
                    todo.append(dep)
        return seen

    names = sorted(rows)
    assert len(names) >= 6
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            shared = {f for f in rows[a][0] & rows[b][0] if "." in f}
            if shared:
                assert a in ancestors(b) or b in ancestors(a), (a, b, shared)


def test_the_design_doc_links_its_mockups():
    assert "web-ui/mockups/lane-queue.html" in _text(DESIGN)


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
    assert "agents-staging" not in _text(doc)


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
    assert "saga" not in text.lower()


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
