"""The spec says what the code does: owner decisions of 2026-10-01.

WHY THIS EXISTS. Two documents described a platform this repository does not
run:

  * docs/BUILD_PROMPT_V2.md called itself "specification, not yet built" and
    said the substrate was "GKE Autopilot, and only GKE Autopilot ...
    Everything is a pod". The catalogue (`swarm_common.profiles`) puts four of
    the five profiles on Cloud Run Jobs and `BackendRouter` in
    apps/scheduler/scheduler/dispatch.py routes them there. The owner decided
    on 2026-10-01 that Cloud Run Jobs STAY primary and GKE Autopilot runs only
    the browser runner, and that the spec moves to match the code.
  * docs/cost-control.md told an admin to set `monthly_budget_usd` and promised
    `PARKED(BUDGET_EXHAUSTED)`. The API refuses that field (422) and nothing
    writes that park. The owner decided the same day there are no per-tenant
    budgets, and none are planned.

Each test reads the live catalogue where it can, so a profile moving backend
fails here rather than leaving the spec quietly wrong again.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from swarm_common.profiles import RUNNER_PROFILES, Backend, resolve_backend

REPO = Path(__file__).resolve().parents[3]
BUILD_PROMPT = REPO / "docs" / "BUILD_PROMPT_V2.md"
CONTRACT = REPO / "CONTRACT.md"
COST = REPO / "docs" / "cost-control.md"
TENANCY = REPO / "docs" / "multi-tenancy.md"
QUOTA = REPO / "docs" / "quota-management.md"
REQUESTS = REPO / "docs" / "contract-change-requests.md"
ARCHITECTURE = REPO / "docs" / "architecture.md"

AMENDED = (BUILD_PROMPT, CONTRACT, COST, TENANCY, QUOTA, REQUESTS, ARCHITECTURE)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The body under a markdown heading, up to the next heading of any level."""
    start = text.index(heading)
    rest = text[start + len(heading):]
    nxt = re.search(r"^#{1,4} ", rest, flags=re.M)
    return rest[: nxt.start()] if nxt else rest


def _backends() -> dict[str, Backend]:
    return {name: resolve_backend(p) for name, p in RUNNER_PROFILES.items()}


# --------------------------------------------------------------------------
# the catalogue these documents describe
# --------------------------------------------------------------------------


def test_the_catalogue_is_what_the_amendment_describes():
    """The premise: Cloud Run Jobs for four profiles, GKE for browser alone.

    If this fails the profiles moved, and every document below is describing
    the old split: amend them before changing the expectation here.
    """
    backends = _backends()
    assert {n for n, b in backends.items() if b is Backend.GKE_AUTOPILOT} == {"browser"}
    assert {n for n, b in backends.items() if b is Backend.CLOUD_RUN_JOB} == {
        "mock",
        "generic",
        "claude-code",
        "codex",
        # #295, contract requests 33, 35 and 36 (accepted 2026-10-01).
        "merge",
        "post-verdict",
        "claude-code-review",
    }


# --------------------------------------------------------------------------
# BUILD_PROMPT_V2.md
# --------------------------------------------------------------------------


def test_build_prompt_no_longer_claims_to_be_unbuilt():
    status = next(line for line in _text(BUILD_PROMPT).splitlines() if line.startswith("**Status:**"))
    assert "not yet built" not in status
    assert "built" in status


def test_build_prompt_substrate_is_cloud_run_primary_with_gke_for_browser():
    text = _text(BUILD_PROMPT)
    assert "only GKE Autopilot" not in text
    assert "Everything is a pod" not in text
    heading = next(line for line in text.splitlines() if line.startswith("### 2.1 "))
    assert "Cloud Run Jobs" in heading
    body = _section(text, heading)
    for name, backend in _backends().items():
        label = "Cloud Run Jobs" if backend is Backend.CLOUD_RUN_JOB else "GKE Autopilot"
        row = next((line for line in body.splitlines() if line.startswith(f"| `{name}` ")), None)
        assert row is not None, f"§2.1 has no row for the {name!r} profile"
        assert label in row, f"§2.1 puts {name!r} somewhere the catalogue does not: {row}"
    assert "swarm_common/profiles.py" in body
    assert "apps/scheduler/scheduler/dispatch.py" in body
    assert "2026-10-01" in body


def test_build_prompt_records_why_and_the_workspace_storage_correction():
    body = _section(_text(BUILD_PROMPT), next(
        line for line in _text(BUILD_PROMPT).splitlines() if line.startswith("### 2.1 ")
    ))
    # WHY: the v1 rationale in architecture.md §5, cited.
    assert "docs/architecture.md" in body
    assert "no nodes" in body
    # The CLAUDE.md "Correction (workspace storage)", not softened.
    for phrase in ("tmpfs", "GA", "live migration", "Preview"):
        assert phrase in body, phrase
    # Checkpointing stays mandatory, for the reasons CLAUDE.md gives.
    for reason in ("park-and-exit", "cancellation", "stale generation", "crash"):
        assert reason in body, reason
    assert "mandatory" in body.lower()


def test_build_prompt_no_longer_lists_cloud_run_jobs_for_deletion():
    text = _text(BUILD_PROMPT)
    deleted = _section(text, "## 6. What gets deleted")
    assert "terraform/modules/cloud_run_jobs/" not in deleted or "NOT deleted" in deleted
    assert "CLOUD_RUN_JOB` member and every branch" not in deleted


#: How far a cited line may drift from where its text now sits. A doc cites
#: `file:line` so a reader can jump there; pinning the EXACT line turned every
#: PR that inserted code above it red (#461 added lines to lifecycle.py and
#: broke this file). The property kept is "the doc cites code that says what
#: the doc claims, near where it says": the text must exist, and the cited line
#: must be within this many lines of it.
CITE_WINDOW = 150


def _near(lines: list[str], line: int, needle: str) -> str | None:
    """The line closest to `line` (1-based) within CITE_WINDOW containing `needle`."""
    lo = max(0, line - 1 - CITE_WINDOW)
    hi = min(len(lines), line + CITE_WINDOW)
    hits = [i for i in range(lo, hi) if needle in lines[i]]
    if not hits:
        return None
    return lines[min(hits, key=lambda i: abs(i - (line - 1)))]


def _cited_line(path: str, line: int, needle: str) -> str:
    """The line near `path:line` that says `needle`; fails naming both if none."""
    lines = (REPO / path).read_text(encoding="utf-8").splitlines()
    found = _near(lines, line, needle)
    if found is None:
        where = [i + 1 for i, text in enumerate(lines) if needle in text]
        pytest.fail(
            f"the doc cites {path}:{line} for {needle!r}, which is "
            + (f"at line(s) {where}: more than {CITE_WINDOW} lines away, update the citation"
               if where else "nowhere in that file")
        )
    return found


def test_a_cited_line_may_drift_within_the_window_but_not_past_it():
    """The window tolerates code inserted above a citation and nothing more."""
    body = [f"line {n}" for n in range(1, 1001)]
    body[499] = "def assign(self):"  # line 500
    assert _near(body, 500, "assign(") == "def assign(self):"
    assert _near(body, 500 - CITE_WINDOW, "assign(") == "def assign(self):"
    assert _near(body, 500 + CITE_WINDOW, "assign(") == "def assign(self):"
    assert _near(body, 500 - CITE_WINDOW - 1, "assign(") is None
    assert _near(body, 500 + CITE_WINDOW + 1, "assign(") is None
    assert _near(body, 500, "credential_env_from_account(") is None
    # Out-of-range citations do not index past the file.
    assert _near(body, 5000, "assign(") is None
    assert _near(body, 1, "line 1") == "line 1"


def test_build_prompt_marks_the_unbuilt_root_gvisor_shape():
    """§2.2, §2.6.3 and §3 describe root + gVisor, an init container and a
    sidecar: none of it runs. Each carries a dated note saying what does, and
    the lines those notes cite still say what the notes claim."""
    text = _text(BUILD_PROMPT)
    isolation = _section(text, "### 2.2 Isolation: root inside the pod, gVisor underneath")
    assert "Amended 2026-10-01" in isolation
    assert "`images/agent-runtime-base/Dockerfile:658`" in isolation
    assert "`kubernetes/render.py:390`" in isolation
    _cited_line("images/agent-runtime-base/Dockerfile", 658, "USER swarm:swarm")
    gvisor = _cited_line("kubernetes/render.py", 390, "--runtime gvisor")
    assert "NOT the" in gvisor, gvisor

    dispatch = _section(text, "#### 2.6.3 Dispatch — the pod starts already logged in")
    assert "Amended 2026-10-01" in dispatch
    assert "no init container" in dispatch
    assert "`apps/agent-worker/agent_worker/lifecycle.py:3357`" in dispatch
    assert "`apps/agent-worker/agent_worker/lifecycle.py:3464`" in dispatch
    assert "`apps/agent-worker/agent_worker/accountlease.py:445`" in dispatch
    _cited_line("apps/agent-worker/agent_worker/lifecycle.py", 3357, "assign(")
    _cited_line("apps/agent-worker/agent_worker/lifecycle.py", 3464, "credential_env_from_account(")
    _cited_line("apps/agent-worker/agent_worker/accountlease.py", 445, "def credential_env_from_account(")

    architecture = _section(text, "## 3. Architecture")
    assert "Amended 2026-10-01" in architecture
    assert "`browser`" in architecture and "`claude-code`" in architecture


# --------------------------------------------------------------------------
# CONTRACT.md
# --------------------------------------------------------------------------


def test_contract_names_the_backend_split_per_profile():
    decisions = _section(_text(CONTRACT), "## Platform decisions")
    line = next(line for line in decisions.split("\n- ") if "Primary backend" in line)
    for name in ("mock", "generic", "claude-code", "codex", "browser"):
        assert f"`{name}`" in line, name
    assert "swarm_common/profiles.py" in line
    assert "dispatch.py" in line
    assert "2026-10-01" in line


def test_contract_keeps_the_live_migration_constraint_and_its_correction():
    text = _text(CONTRACT)
    # CLAUDE.md: must stay in the docs and must not be softened.
    assert "Cloud Run ephemeral disk is Preview and" in text
    assert "disables live migration" in text
    assert "Correction (workspace storage)" in text


def test_contract_says_what_pod_means_on_each_backend():
    text = _text(CONTRACT)
    assert "Cloud Run Job execution" in text


# --------------------------------------------------------------------------
# budgets: cost-control.md, multi-tenancy.md, quota-management.md
# --------------------------------------------------------------------------


def test_cost_control_does_not_advertise_a_budget():
    text = _text(COST)
    assert '"monthly_budget_usd": 2000' not in text
    assert "A tenant over budget gets" not in text
    assert "per-tenant budgets |" not in text
    for phrase in ("not built and not planned", "2026-10-01", "record_spend", "BUDGET_EXHAUSTED", "422"):
        assert phrase in text, phrase


def test_multi_tenancy_does_not_say_nothing_attributes_cost():
    text = _text(TENANCY)
    assert "nothing attributes cost" not in text
    for phrase in ("record_spend", "2026-10-01", "BUDGET_EXHAUSTED"):
        assert phrase in text, phrase


def test_quota_management_marks_budget_exhausted_never_written():
    row = next(line for line in _text(QUOTA).splitlines() if line.startswith("| `BUDGET_EXHAUSTED` |"))
    assert "never written" in row
    assert "states.py" in row


# --------------------------------------------------------------------------
# contract-change-requests.md
# --------------------------------------------------------------------------


def test_budget_exhausted_is_recorded_as_a_request_not_an_edit():
    text = _text(REQUESTS)
    heading = next(
        (line for line in text.splitlines() if line.startswith("## ") and "BUDGET_EXHAUSTED" in line),
        None,
    )
    assert heading is not None, "no entry records BUDGET_EXHAUSTED as unused"
    number = re.match(r"## (\d+)\.", heading).group(1)
    assert re.search(rf"^\| {number} \| .*BUDGET_EXHAUSTED.* \| (?:open|PROPOSED)", text, flags=re.M)
    body = _section(text, heading)
    assert "**Status:" in body or "**Status:**" in text[text.index(heading):]
    entry = text[text.index(heading):]
    for part in ("### What is true today", "### The requested change", "### If it is declined"):
        assert part in entry, part


# --------------------------------------------------------------------------
# every file:line an amended document cites resolves
# --------------------------------------------------------------------------

_CITE = re.compile(r"`((?:apps|terraform|kubernetes|scripts|tests|images)/[\w./-]+\.\w+):(\d+)`")


@pytest.mark.parametrize("doc", AMENDED, ids=lambda p: p.name)
def test_every_cited_line_exists(doc: Path):
    for path, line in _CITE.findall(_text(doc)):
        target = REPO / path
        assert target.is_file(), f"{doc.name} cites {path}, which does not exist"
        lines = target.read_text(encoding="utf-8").count("\n") + 1
        assert int(line) <= lines, f"{doc.name} cites {path}:{line}, past its end ({lines})"
