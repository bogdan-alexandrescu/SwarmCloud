"""Suite-wide wiring for tests/unit. Keep it small: every test pays for it.

The flaky-test quarantine: every test tests/flaky.txt lists is marked
`flaky` at collection. The unit job deselects them from the gating run
(`-m "not flaky"`) and runs them in a step that reports and never fails the
gate. flaky_quarantine.py says why, and docs/ci.md how to add an entry.
"""

from __future__ import annotations

import pytest

import flaky_quarantine

# PROJECT_ID for the script tests is named in tests/unit/scripts/conftest.py,
# for that package only: set here, suite-wide, it reached every app built in
# process too, and quota-broker's create_app turns on its Secret Manager paths
# when it sees one (the lending and refresh tests then failed on what a
# deployment with a project needs).


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    try:
        entries = flaky_quarantine.load()
    except flaky_quarantine.QuarantineListError as exc:
        raise pytest.UsageError(str(exc)) from None
    flaky_quarantine.apply(items, entries, pytest.mark.flaky)


@pytest.fixture
def fix_loop_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The `fix-loop` profile unavailable, as it shipped until contract request
    151 (2026-11-09). A module written against the re-review that a workflow
    merging on its review's verdict got before then (one fix round, a
    `re-review` step) holds that path with
    `pytestmark = pytest.mark.usefixtures("fix_loop_off")`; the loop itself is
    held by the test_fix_loop_*.py and test_issue_run_fix_loop.py modules."""
    from dataclasses import replace

    from swarm_common.profiles import RUNNER_PROFILES

    monkeypatch.setitem(RUNNER_PROFILES, "fix-loop", replace(
        RUNNER_PROFILES["fix-loop"], available=False,
        disabled_reason="fix-loop is not available (patched off for this test)"))
