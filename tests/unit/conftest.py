"""Suite-wide wiring for tests/unit. Keep it small: every test pays for it.

The flaky-test quarantine: every test tests/flaky.txt lists is marked
`flaky` at collection. The unit job deselects them from the gating run
(`-m "not flaky"`) and runs them in a step that reports and never fails the
gate. flaky_quarantine.py says why, and docs/ci.md how to add an entry.
"""

from __future__ import annotations

import pytest

import flaky_quarantine


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    try:
        entries = flaky_quarantine.load()
    except flaky_quarantine.QuarantineListError as exc:
        raise pytest.UsageError(str(exc)) from None
    flaky_quarantine.apply(items, entries, pytest.mark.flaky)
