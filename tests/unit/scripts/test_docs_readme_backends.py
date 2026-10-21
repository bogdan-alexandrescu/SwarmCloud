"""The README and the prose around it say where agents run (lane README-BACKENDS, part of #1822).

DOCS-TRUTH (PR 1827) corrected docs/architecture.md §5: Cloud Run Jobs was
CHOSEN as the primary backend, and since 2026-10-10 every agent profile but
`mock` runs on GKE Autopilot. The README still said "Cloud Run Jobs is the
primary backend, not Kubernetes" and that `browser` is what runs on GKE, and
docs/BUILD_PROMPT_V2.md, docs/operations.md and docs/gke-dispatch-403.md
restated the same split in the present tense.

Each test reads the profile split from the frozen catalogue
(`swarm_common.profiles.RUNNER_PROFILES`), so the next profile that moves
turns this red instead of leaving the README quietly wrong again.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
README = REPO / "README.md"
DOCS = REPO / "docs"
BUILD_PROMPT = DOCS / "BUILD_PROMPT_V2.md"
OPERATIONS = DOCS / "operations.md"
GKE_403 = DOCS / "gke-dispatch-403.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The body under a markdown heading, up to the next heading of the same or a higher level."""
    start = text.index(heading)
    level = len(heading) - len(heading.lstrip("#"))
    rest = text[start + len(heading):]
    nxt = re.search(rf"^#{{1,{level}}} ", rest, flags=re.M)
    return rest[: nxt.start()] if nxt else rest


def _flat(markdown: str) -> str:
    return " ".join(re.sub(r"^[ \t]*>[ \t]?", "", markdown, flags=re.M).split())


def _profiles_on(backend) -> list[str]:
    from swarm_common.profiles import RUNNER_PROFILES, resolve_backend

    return sorted(n for n, p in RUNNER_PROFILES.items()
                  if not p.control_plane and resolve_backend(p) is backend)


def _paragraph(text: str, opening: str) -> str:
    start = text.index(opening)
    end = text.find("\n\n", start)
    return _flat(text[start: end if end != -1 else len(text)])


# --------------------------------------------------------------------------
# README.md
# --------------------------------------------------------------------------


def test_readme_decision_says_cloud_run_was_chosen_and_where_agents_run_today():
    from swarm_common.profiles import Backend

    decisions = _section(_text(README), "### Decisions worth knowing")
    # MUTATION: restore "**Cloud Run Jobs is the primary backend, not Kubernetes.**" -> red.
    assert "Cloud Run Jobs is the primary backend" not in decisions
    assert "`browser` runs on GKE because" not in decisions
    decision = _paragraph(decisions, "**Cloud Run Jobs was chosen")
    # WHY it was chosen stays.
    assert "no nodes" in decision and "2026-10-01" in decision
    # WHERE it runs today, dated, from the catalogue, linked rather than restated.
    assert "2026-10-10" in decision
    assert "`apps/common/swarm_common/profiles.py`" in decision
    assert "(docs/architecture.md" in decision
    gke = _profiles_on(Backend.GKE_AUTOPILOT)
    cloud_run = _profiles_on(Backend.CLOUD_RUN_JOB)
    assert "mock" in cloud_run and "claude-code" in gke
    gke_part = decision[decision.index("GKE Autopilot runs"): decision.index("Cloud Run Jobs runs")]
    run_part = decision[decision.index("Cloud Run Jobs runs"):]
    for name in gke:
        # MUTATION: move a GKE profile into the Cloud Run sentence -> red.
        assert f"`{name}`" in gke_part, name
    for name in cloud_run:
        assert f"`{name}`" in run_part, name


def test_readme_execution_plane_names_every_profile_on_its_backend():
    from swarm_common.profiles import Backend

    plane = _paragraph(_text(README), "**Control plane**")
    plane = plane[plane.index("**Execution plane**"):]
    gke_part = plane[plane.index("GKE Autopilot for"):]
    run_part = plane[: plane.index("GKE Autopilot for")]
    # MUTATION: drop `claude-code-browser` from the GKE list -> red.
    for name in _profiles_on(Backend.GKE_AUTOPILOT):
        assert f"`{name}`" in gke_part, name
    for name in _profiles_on(Backend.CLOUD_RUN_JOB):
        assert f"`{name}`" in run_part, name


def test_readme_diagram_no_longer_puts_only_browser_on_gke():
    diagram = _section(_text(README), "## Architecture").split("```")[1]
    # MUTATION: restore "(browser)" under GKE Autopilot -> red.
    assert "(browser)" not in diagram
    assert "every agent" in diagram and "mock" in diagram


def test_readme_says_tmpfs_workspaces_are_cloud_run_only():
    from scheduler.dispatch import CACHE_VOLUME_GIB

    text = _text(README)
    # MUTATION: restore "**Workspaces are memory-backed tmpfs, carved out" -> red.
    assert "**Workspaces are memory-backed tmpfs, carved out" not in text
    storage = _paragraph(text, "**On Cloud Run, workspaces are memory-backed tmpfs")
    # The CLAUDE.md correction about Cloud Run ephemeral disk stays, unsoftened.
    for phrase in ("Preview", '`empty_dir.medium` accepts only `MEMORY`', "fully-GA path", "live migration"):
        assert phrase in storage, phrase
    gke = _paragraph(text, "**On GKE, where every agent profile but `mock` runs")
    for fact in ("disk `emptyDir`", "`/tmp`", "HOME", f"{CACHE_VOLUME_GIB} GiB cache"):
        assert fact in gke, fact


# --------------------------------------------------------------------------
# The rest of the prose
# --------------------------------------------------------------------------


def test_build_prompt_no_longer_says_cloud_run_carries_most_execution():
    text = _text(BUILD_PROMPT)
    flat = _flat(text)
    status = next(line for line in text.splitlines() if line.startswith("**Status:**"))
    # MUTATION: restore any of these present-tense claims -> red.
    for stale in ("Cloud Run Jobs stay the primary execution backend",
                  "They carry four of the five profiles",
                  "and so does most execution",
                  "Still true for the four Cloud Run profiles",
                  "Cloud Run Jobs for `mock`, `generic`, `claude-code` and `codex`, and GKE Autopilot for `browser`"):
        assert stale not in flat, stale
    heading = next(line for line in text.splitlines() if line.startswith("### 2.1 "))
    assert "chosen as primary" in heading and "every agent but `mock`" in heading
    assert "2026-10-10" in _flat(text[text.index(status):text.index("**Supersedes:**")])


def test_operations_status_table_lists_every_gke_worker_pod_not_only_browser():
    # MUTATION: restore "| GKE | browser pods," -> red.
    assert "| GKE | browser pods," not in _text(OPERATIONS)
    assert "| GKE | worker pods (every agent profile but `mock`)," in _text(OPERATIONS)


def test_gke_dispatch_403_dates_its_only_browser_on_gke_statement():
    flat = _flat(_text(GKE_403))
    # MUTATION: restore "`browser` is the only profile whose resolved backend" -> red.
    assert "`browser` is the only profile whose resolved backend" not in flat
    assert "`browser` was then the only profile whose resolved backend" in flat
