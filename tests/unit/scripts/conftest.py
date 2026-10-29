"""Wiring for the script tests in tests/unit/scripts.

scripts/lib/common.sh has no default project (DAYONE-DESAGA-2): sourcing it
with PROJECT_ID unset is an error. The script tests here run against the live
deployment's configuration, whose deny-list they hold the guards to, so this
package names that project unless the caller already named one. A test about
the default itself scrubs it (test_common_defaults.py).

Package-scoped, not set on os.environ at import: an import-time default reached
every test in the worker, the in-process apps included, and quota-broker's
create_app reads PROJECT_ID to decide whether it has a secret store. Package
scope also covers the module-scoped fixtures here that run a script, which a
function-scoped fixture would set up too late for.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest


@pytest.fixture(scope="package", autouse=True)
def _scripts_name_their_project() -> Iterator[None]:
    with pytest.MonkeyPatch.context() as mp:
        if not os.environ.get("PROJECT_ID"):
            mp.setenv("PROJECT_ID", "saga-agents-staging")
        yield
