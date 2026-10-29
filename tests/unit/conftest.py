"""Suite-wide wiring for tests/unit. Keep it small: every test pays for it.

The flaky-test quarantine: every test tests/flaky.txt lists is marked
`flaky` at collection. The unit job deselects them from the gating run
(`-m "not flaky"`) and runs them in a step that reports and never fails the
gate. flaky_quarantine.py says why, and docs/ci.md how to add an entry.
"""

from __future__ import annotations

import os

import pytest

import flaky_quarantine

# scripts/lib/common.sh has no default project (DAYONE-DESAGA-2): sourcing it
# with PROJECT_ID unset is an error. The script tests here run against the
# live deployment's configuration, whose deny-list they hold the guards to, so
# the suite names that project unless the caller already named one. A test
# about the default itself scrubs it (tests/unit/scripts/test_common_defaults.py).
os.environ.setdefault("PROJECT_ID", "saga-agents-staging")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    try:
        entries = flaky_quarantine.load()
    except flaky_quarantine.QuarantineListError as exc:
        raise pytest.UsageError(str(exc)) from None
    flaky_quarantine.apply(items, entries, pytest.mark.flaky)
