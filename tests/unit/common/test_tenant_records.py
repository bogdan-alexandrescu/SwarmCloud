"""Contract request 87: `Tenant.service_account`, `.namespace` and `.gcs_prefix` are records, not authority.

Accepted by the owner 2026-10-17 on PR 1464 (docs/design/tenant-settings-integrity.md
§5 request C, part of #1457). A tenant identity can rewrite its own
`tenants/<id>` document, so a reader that dispatches as the stored
`service_account` or into the stored `namespace` runs tenant A's attempt as
tenant B (§1.3 rows 1-3). The frozen type is where a future reader learns
that; this test holds the warning beside each field.
"""

from __future__ import annotations

import inspect

import pytest

from swarm_common.models import Tenant


@pytest.mark.parametrize("name", ["service_account", "gcs_prefix", "namespace"])
def test_each_identity_field_is_marked_record_only(name):
    lines = inspect.getsource(Tenant).splitlines()
    at = next(i for i, line in enumerate(lines) if line.strip().startswith(f"{name}:"))
    assert "RECORD ONLY" in lines[at], lines[at]


def test_the_fields_are_kept_and_still_default_to_none():
    """Comments only: no field is removed or retyped."""
    fields = Tenant.__dataclass_fields__
    for name in ("service_account", "gcs_prefix", "namespace"):
        assert fields[name].default is None
