"""docs/design/live-browser.md §3.2 records the test-runner exception to BROWSER-ENFORCE.

WHY THIS EXISTS (PW-TESTRUNNER-DOCS, part of #2253). §3.2 says the agent's own
Chromium is refused (BROWSER-ENFORCE, owner decision 2026-10-13, #1030). PR
#2254 then opened one sanctioned path on claude-code-browser: a headless TEST
process run through `swarm-test-browsers` launches its own Chromium from a
writable directory, while the agent's `PLAYWRIGHT_BROWSERS_PATH` stays on the
live-browser stub. A reader of §3.2 alone would believe no Chromium but the
platform's can start, and would not learn what the wrapper refuses, what the
download costs, or the risk #2254 left open. What would quietly rot: the
subsection being dropped, a name it states (the variable, the directory, the
stub path, a cited symbol) drifting from the code, or the residual risk being
softened into "closed".

Values are read from the code, never restated here, and the worker is read as
source rather than imported: a top-level import makes every collection of
tests/unit/scripts load the worker.

MUTATION: delete the subsection, or rename `TEST_BROWSERS_ENV` in the runner,
or drop the `--with-deps` refusal from the wrapper; each fails here.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from .test_docs_spec_amendments import _cited_symbol

REPO = Path(__file__).resolve().parents[3]
DOC = REPO / "docs" / "design" / "live-browser.md"
RUNNER = "apps/agent-worker/agent_worker/runners/claude_code.py"
WRAPPER = REPO / "images" / "agent-runtime-browser" / "swarm-test-browsers"
DOCKERFILE = REPO / "images" / "agent-runtime-browser" / "Dockerfile"

SECTION = "### 3.2 Chromium is launched by the platform, and the agent connects to it"
NEXT = "### 3.2.1 dev-browser (opt-in)"
EXCEPTION = "#### The test-runner exception (PW-TESTRUNNER, #2253)"

_SYMBOL = re.compile(r"\b(apps/[\w./-]+\.py)::([\w.]+)")


def _flat(markdown: str) -> str:
    return re.sub(r"\s+", " ", markdown)


def _section() -> str:
    text = DOC.read_text(encoding="utf-8")
    start = text.index(SECTION)
    return text[start: text.index(NEXT, start)]


def _exception() -> str:
    section = _section()
    assert EXCEPTION in section, (
        f"live-browser.md §3.2 has no {EXCEPTION!r} subsection: the sanctioned "
        "test-runner path (#2254) is undocumented where BROWSER-ENFORCE is"
    )
    return _flat(section[section.index(EXCEPTION):])


def _runner_constant(name: str) -> str:
    tree = ast.parse((REPO / RUNNER).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            value = ast.literal_eval(node.value)
            assert isinstance(value, str)
            return value
    raise AssertionError(f"{RUNNER} has no module-level {name}")


@pytest.mark.parametrize("name", ["AGENT_BROWSERS_PATH", "TEST_BROWSERS_ENV",
                                  "TEST_BROWSERS_DIR_NAME", "CACHE_HOME_ENV"])
def test_the_exception_states_the_runners_names_as_the_code_has_them(name):
    value = _runner_constant(name)
    assert f"`{value}`" in _exception() or f"`${value}" in _exception(), (
        f"§3.2's test-runner exception does not state {name} = {value!r} as the runner does"
    )


def test_every_symbol_the_exception_cites_exists():
    cited = _SYMBOL.findall(_exception())
    assert (RUNNER, "prepare_test_browsers") in cited, cited
    for path, qualname in cited:
        _cited_symbol(path, qualname)


def test_the_refusals_it_names_are_the_wrappers():
    wrapper = WRAPPER.read_text(encoding="utf-8")
    text = _exception()
    # The wrapper refuses --with-deps and an empty command with exit 2.
    assert '"--with-deps" in argv' in wrapper and "if not argv:" in wrapper
    assert "PLAYWRIGHT_SKIP_BROWSER_GC" in wrapper
    for phrase in ("`--with-deps`", "empty command", "exit 2",
                   "`PLAYWRIGHT_SKIP_BROWSER_GC`", "`swarm-test-browsers npx playwright test`"):
        assert phrase in text, f"§3.2's exception does not say {phrase!r}"
    assert "swarm-test-browsers" in DOCKERFILE.read_text(encoding="utf-8")


def test_it_says_the_agents_default_stays_the_stub_and_cites_its_decisions():
    text = _exception()
    for phrase in ("#1030", "#2253", "#2254", "2026-10-24",
                   "agent's own `PLAYWRIGHT_BROWSERS_PATH` is unchanged",
                   "not named `ms-playwright`"):
        assert phrase in text, f"§3.2's exception does not say {phrase!r}"


def test_it_states_the_cost_on_each_backend():
    text = _exception()
    for phrase in ("about 170 MB", "memory tmpfs", "Cloud Run", "GKE",
                   "20 GiB", "checkpoint"):
        assert phrase in text, f"§3.2's exception does not state the cost: {phrase!r}"


def test_it_keeps_the_residual_risk_as_2254_stated_it():
    """#2254's "What still could": not softened, not replaced by a new one."""
    text = _exception()
    for phrase in ("`swarm-test-browsers python browse.py`",
                   "cannot tell a test runner from a browsing script",
                   "`npm exec -- node browse.js`",
                   "already possible before",
                   "not a sandbox",
                   "only for test suites",
                   "pod's own egress"):
        assert phrase in text, f"§3.2's exception drops #2254's residual risk: {phrase!r}"
