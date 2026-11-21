"""swarm_qa_targets names each entry's extra origins and which a platform
admin approved (the ledger's 'QA target extra origins' item): origins only,
copied as strings, so a caller can tell why a run's API host is refused.
Pure: `swarm_mcp.qatargets`, no network."""

from __future__ import annotations

from swarm_mcp import qatargets

BASE = "https://shop.example.com"
IDP = "https://auth.example.com"
API = "https://api.example.com"
FILES = "https://files.example.com:8443"


def _entry(**over):
    entry = {
        "name": "production", "environment": "production", "base_url": BASE, "host": "shop.example.com",
        "host_state": "approved", "sign_in": {"kind": "own_login", "extra_origin": IDP},
        "credential": {"set": True}, "extra_origins": [API, FILES], "approved_origins": [BASE, IDP, API],
    }
    entry.update(over)
    return entry


def test_an_entry_names_its_extra_origins_and_the_approved_ones():
    view = qatargets.entry_view(_entry())
    # MUTATION: leave `extra_origins` out of `entry_view` and the caller never sees the API host.
    assert view["extra_origins"] == [IDP, API, FILES]
    # MUTATION: list every declared origin as approved and FILES reads as reachable.
    assert view["approved_extra_origins"] == [IDP, API]
    text = qatargets.targets_text({"qa_targets": [{"name": "Shop", "id": "qat_0", "repository": "o/shop",
                                                   "environments": [view]}]})
    assert f"{FILES} (not approved)" in text and f"{API} (approved)" in text


def test_anything_but_an_origin_string_is_not_copied():
    view = qatargets.entry_view(_entry(extra_origins=[{"x": 1}, 7, API], sign_in={"kind": "none"},
                                       approved_origins="all"))
    assert view["extra_origins"] == [API]
    assert view["approved_extra_origins"] == []
    assert qatargets.entry_view(_entry(extra_origins="nope"))["extra_origins"] == [IDP]
