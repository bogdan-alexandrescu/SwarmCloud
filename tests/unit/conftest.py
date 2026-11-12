"""Suite-wide wiring for tests/unit. Keep it small: every test pays for it.

The flaky-test quarantine: every test tests/flaky.txt lists is marked
`flaky` at collection. The unit job deselects them from the gating run
(`-m "not flaky"`) and runs them in a step that reports and never fails the
gate. flaky_quarantine.py says why, and docs/ci.md how to add an entry.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest

import flaky_quarantine

# PROJECT_ID for the script tests is named in tests/unit/scripts/conftest.py,
# for that package only: set here, suite-wide, it reached every app built in
# process too, and quota-broker's create_app turns on its Secret Manager paths
# when it sees one (the lending and refresh tests then failed on what a
# deployment with a project needs).

_spec = importlib.util.spec_from_file_location("placeholders", Path(__file__).resolve().parents[1] / "placeholders.py")
_placeholders = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_placeholders)


@pytest.fixture(scope="session", autouse=True)
def _the_placeholder_organisation_names_the_label_prefix() -> Iterator[None]:
    """SWARM_ORG_DOMAIN, the placeholder organisation's domain, suite-wide.

    Every label key is `<prefix>/<name>` and kubernetes/label_prefix.py refuses
    to render one with no prefix configured (the owner's "configurable", 8
    November 2026), so every test that renders a manifest, in process or in a
    child, needs a deployment that names its domain. No app reads the variable;
    a test of the refusal removes it from its own child's environment.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("SWARM_ORG_DOMAIN", _placeholders.ORG_DOMAIN)
        yield


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    try:
        entries = flaky_quarantine.load()
    except flaky_quarantine.QuarantineListError as exc:
        raise pytest.UsageError(str(exc)) from None
    flaky_quarantine.apply(items, entries, pytest.mark.flaky)
