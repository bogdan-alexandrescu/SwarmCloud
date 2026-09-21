"""The smoke suite's jq must match what GET /v1/tenants/me actually returns.

Both ends of this existed and the middle did not -- the fourth instance of that
shape in two days. routes/tenants.py:get_me returns

    {"tenant": {...}, "principal": {...}}

and the suite read `.tenant_id` at the TOP level, so it found nothing and
reported "no tenant in the response" against a response whose second line names
the tenant. It surfaced on the gate's first successful run, in the step whose
whole purpose is to prove the caller resolves to a tenant.

The fixture below is built from the route's own response builder, not typed by
hand, so it cannot drift from the API the way the jq did.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SMOKE = ROOT / "scripts" / "smoke-test.sh"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")


def _tenant_filter() -> str:
    """The exact jq expression the suite uses for the tenant."""
    m = re.search(r'tenant="\$\(jq -r \'([^\']+)\'', SMOKE.read_text())
    assert m, "could not find the tenant jq in smoke-test.sh"
    return m.group(1)


def _email_filter() -> str:
    m = re.search(r'email="\$\(jq -r \'([^\']+)\'', SMOKE.read_text())
    assert m, "could not find the email jq in smoke-test.sh"
    return m.group(1)


def _jq(filt: str, doc: dict) -> str:
    # `default=str` because tenant_to_api leaves datetimes as datetimes and the
    # real route serialises them through FastAPI. What is under test is the jq
    # PATH, not the timestamp format.
    out = subprocess.run(
        ["jq", "-r", filt],
        input=json.dumps(doc, default=str),
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _real_response() -> dict:
    """What get_me builds, assembled from the codec rather than invented."""
    from datetime import datetime, timezone

    from swarm_api.codec import tenant_to_api
    from swarm_common.models import Tenant

    tenant = Tenant(
        tenant_id="u-sw-c90291",
        kind="user",
        principal="swarm-verify@saga-agents-staging.iam.gserviceaccount.com",
        created_at=datetime(2026, 9, 21, 22, 44, 9, tzinfo=timezone.utc),
        display_name="Verification gate",
    )
    return {
        "tenant": tenant_to_api(tenant),
        "principal": {
            "email": "swarm-verify@saga-agents-staging.iam.gserviceaccount.com",
            "domain": "saga-agents-staging.iam.gserviceaccount.com",
            "groups": [],
            "is_admin": False,
        },
    }


def test_the_tenant_is_found_in_the_real_response_shape():
    assert _jq(_tenant_filter(), _real_response()) == "u-sw-c90291"


def test_the_email_is_found_in_the_real_response_shape():
    got = _jq(_email_filter(), _real_response())
    assert got.startswith("swarm-verify@"), got


def test_a_flat_legacy_shape_still_parses():
    """The fallbacks exist so an older deployment does not fail this step."""
    assert _jq(_tenant_filter(), {"tenant_id": "u-old"}) == "u-old"


def test_an_unrecognisable_response_yields_empty_not_a_wrong_tenant():
    """`// empty` matters: a filter that returned "null" would make the suite
    report a tenant literally named null and pass."""
    assert _jq(_tenant_filter(), {"unexpected": True}) == ""
