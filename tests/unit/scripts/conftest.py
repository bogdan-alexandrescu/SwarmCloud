"""Wiring for the script tests in tests/unit/scripts.

scripts/lib/common.sh has no default project, region or zone (the Day One project-defaults lane;
docs/design/generic-repository.md, section 2.2): sourcing it with any of them
unset is an error. And once a project is named, it reads that deployment's
never-touch list from config/<env>/deny-list.json and stops when it cannot
(section 2.4). So this package runs against the placeholder deployment of
section 3.1, from the one shared helper, placeholder_deployment.py: the
placeholder project, region and zone, and the placeholder list,
scripts/lib/deny-list.example.json, served for whatever project a test's child
process names. No test here names, or reads the list of, a real deployment. A
test about the defaults or the loader itself opts out with
`pytest.mark.no_placeholder_deployment` (test_common_defaults.py,
test_shared_deny_list.py, test_common_config_fetch.py).

Package-scoped, not set on os.environ at import: an import-time default reached
every test in the worker, the in-process apps included, and quota-broker's
create_app reads PROJECT_ID to decide whether it has a secret store. Package
scope also covers the module-scoped fixtures here that run a script, which a
function-scoped fixture would set up too late for.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "placeholders", Path(__file__).resolve().parents[2] / "placeholders.py"
)
_placeholders = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_placeholders)
placeholder_deployment = _placeholders.deployment()

#: The placeholder deployment (docs/design/generic-repository.md, section 3.1).
PLACEHOLDER_DEPLOYMENT = {
    "PROJECT_ID": placeholder_deployment.PROJECT,
    "REGION": placeholder_deployment.REGION,
    "ZONE": placeholder_deployment.ZONE,
}

#: The example list, written for a project under <config_dir>/<env>/ for each env.
write_placeholder_deny_list = placeholder_deployment.write_deny_list


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", placeholder_deployment.MARKER_HELP)


@pytest.fixture(scope="package", autouse=True)
def _scripts_name_their_project(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    yield from placeholder_deployment.activate(tmp_path_factory)


@pytest.fixture(autouse=True)
def _opt_out_of_the_placeholder_deployment(request: pytest.FixtureRequest) -> Iterator[None]:
    if request.node.get_closest_marker(placeholder_deployment.MARKER) is None:
        yield
        return
    with pytest.MonkeyPatch.context() as mp, placeholder_deployment.opted_out(mp):
        yield
