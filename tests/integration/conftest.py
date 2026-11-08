"""Wiring for the integration tests: they run scripts against the placeholder deployment.

scripts/lib/common.sh has no default project, region or zone, and reads the
named project's never-touch list when it is sourced
(docs/design/generic-repository.md, sections 2.2 and 2.4). Every test here gets
the placeholder deployment from the one shared helper,
tests/unit/scripts/placeholder_deployment.py, which serves the placeholder
list for whatever project and environment a test's child process names (CI's
integration job sets ENVIRONMENT=ci, so config/ci/ is served). A test that proves
what happens without one opts out with `pytest.mark.no_placeholder_deployment`.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "placeholders", Path(__file__).resolve().parents[1] / "placeholders.py"
)
_placeholders = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_placeholders)
placeholder_deployment = _placeholders.deployment()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", placeholder_deployment.MARKER_HELP)


@pytest.fixture(scope="module", autouse=True)
def _integration_names_its_project(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    yield from placeholder_deployment.activate(tmp_path_factory)


@pytest.fixture(autouse=True)
def _opt_out_of_the_placeholder_deployment(request: pytest.FixtureRequest) -> Iterator[None]:
    if request.node.get_closest_marker(placeholder_deployment.MARKER) is None:
        yield
        return
    with pytest.MonkeyPatch.context() as mp, placeholder_deployment.opted_out(mp):
        yield
