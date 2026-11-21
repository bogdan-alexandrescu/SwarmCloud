"""Code-only consistency checks and doc-citation link checks left after slice 1
of issue #71 dropped the doc-prose assertions (wave 14, lane C4I).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DOCS = REPO / "docs"
ISSUE_RUNS = DOCS / "issue-runs.md"
DISPATCH = REPO / "apps" / "scheduler" / "scheduler" / "dispatch.py"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The body under a markdown heading, up to the next heading of any level."""
    start = text.index(heading)
    rest = text[start + len(heading):]
    nxt = re.search(r"^#{1,4} ", rest, flags=re.M)
    return rest[: nxt.start()] if nxt else rest


def _module_constant(path: Path, name: str) -> str:
    for node in ast.parse(_text(path)).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            assert isinstance(node.value, ast.Constant), name
            return node.value.value
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            assert isinstance(node.value, ast.Constant), name
            return node.value.value
    raise AssertionError(f"{path.relative_to(REPO)} defines no {name}")


def _defines(path: Path, name: str) -> bool:
    return any(
        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name
        for n in ast.walk(ast.parse(_text(path)))
    )


# --------------------------------------------------------------------------
# archived issue 76: the GKE metadata server address, restated in four places
# --------------------------------------------------------------------------

_GKE_METADATA_TEMPLATES = ("worker-job.yaml", "worker-job-v2.yaml", "worker-job-browser.yaml")


def test_gke_metadata_server_address_matches_across_dispatch_templates_and_egress():
    """dispatch.py's GKE_METADATA_SERVER_IP is restated by three worker templates and
    allow-egress.yaml's /32 CIDR; a value changed in one place and not the others would
    route the worker's token proxy to an address the network policy then blocks."""
    address = _module_constant(DISPATCH, "GKE_METADATA_SERVER_IP")
    for template in _GKE_METADATA_TEMPLATES:
        # MUTATION: change the address in one worker template only -> red.
        assert address in _text(REPO / "kubernetes" / "worker-templates" / template), template
    # MUTATION: change the address in dispatch.py without the /32 egress rule -> red.
    assert f"{address}/32" in _text(REPO / "kubernetes" / "network-policies" / "allow-egress.yaml")


# --------------------------------------------------------------------------
# archived issue 2688 (LIGHTPANDA-DOCS): Lightpanda's CDP port, not the live gate's
# --------------------------------------------------------------------------

LIGHTPANDA = REPO / "apps" / "agent-worker" / "agent_worker" / "lightpanda.py"
LIGHTPANDA_RUNNER_TEST = "tests/unit/worker/test_lightpanda_runner.py"


def test_lightpanda_cdp_port_is_not_the_live_browser_gates_port_and_matches_its_test():
    """claude-code-lightpanda's `serve` child listens on the port lightpanda.py's
    CDP_PORT names; it must not collide with the live browser gate's 9222, and the
    runner test that exercises it restates the same port literal."""
    port = _module_constant(LIGHTPANDA, "CDP_PORT")
    # MUTATION: set CDP_PORT to 9222 -> red (Lightpanda must not take the live gate's port).
    assert port != 9222
    # MUTATION: change CDP_PORT without updating its test -> red.
    assert str(port) in _text(REPO / LIGHTPANDA_RUNNER_TEST)


# --------------------------------------------------------------------------
# docs/issue-runs.md: every citation it makes still resolves
# --------------------------------------------------------------------------

_CITED_TEST = re.compile(r"`((?:tests/[\w/]+/)?test_\w+\.py)::(\w+)`")


def test_every_python_test_issue_runs_cites_exists():
    """The two auto-merge refusal tests were renamed away; the doc kept citing them."""
    cited = _CITED_TEST.findall(_text(ISSUE_RUNS))
    # The anchor: the doc's acceptance tables were read, not an empty match.
    assert len(cited) > 40, len(cited)
    missing = []
    for file, name in cited:
        path = REPO / file if file.startswith("tests/") else (
            REPO / "tests" / "unit" / "control_plane" / file
        )
        if not path.is_file() or not _defines(path, name):
            missing.append(f"{file}::{name}")
    assert not missing, missing


def test_the_auto_merge_ui_tests_issue_runs_quotes_exist():
    section = _section(_text(ISSUE_RUNS), "### 4. ")
    tsx = _text(REPO / "apps" / "swarm-ui" / "src" / "__tests__" / "intake.issue.test.tsx")
    rows = [line for line in section.splitlines() if "intake.issue.test.tsx" in line]
    titles = [t for row in rows for t in re.findall(r'"([^"|]+)"', row)]
    assert titles, section
    for title in titles:
        assert title in tsx.replace("\\'", "'"), title
