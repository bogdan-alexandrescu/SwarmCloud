"""`capture --entry-query` (QA-ENTRY-QUERY): a leg's plain `key=value` pairs
on every URL the capture opens.

An app that picks its backend from the address (`?env=production`) lands a
fresh browser on its default leg unless every URL says which: the run on
2026-11-03 captured `/settings/defaults?env=staging` on a production leg.
These hold that the persona entry, every route and every page the crawl walks
carry the leg's pairs, merged with a route's own query (the leg's keys
winning) and kept before a hash route's fragment, and that a malformed value
is refused before anything is opened.
"""

from __future__ import annotations

import json

import pytest

from test_swarm_qa import ROUTES, _capture, qa, world  # noqa: F401  (fixtures)


def _loads(world) -> list[str]:
    return [e[1] for e in world.page.log if e[0] == "goto"]


def test_entry_url_merges_the_legs_pairs_and_keeps_a_routes_own(qa):
    pairs = qa.entry_query("env=production")
    assert pairs == [("env", "production")]
    assert qa.entry_url("https://app.example.test", pairs) == "https://app.example.test/?env=production"
    assert qa.entry_url("https://app.example.test/settings?tab=a&env=staging", pairs) \
        == "https://app.example.test/settings?tab=a&env=production"
    assert qa.entry_url("https://app.example.test/#/runs", pairs) == "https://app.example.test/?env=production#/runs"
    assert qa.entry_url("https://app.example.test/runs", []) == "https://app.example.test/runs"


@pytest.mark.parametrize("raw", ["env", "env=prod&env=staging", "e nv=x", "env=a%20b", "env=a/b",
                                 "&".join(f"k{i}=v" for i in range(9)), "env=" + "x" * 300])
def test_entry_query_refuses_a_malformed_value(qa, raw):
    with pytest.raises(qa.UsageError):
        qa.entry_query(raw)


def test_every_route_the_capture_opens_carries_the_entry_query(qa, world):
    code, _out, _bundle = _capture(qa, world, "--entry-query", "env=production", presets="desktop",
                                   themes="light")
    assert code == 0
    loads = _loads(world)
    assert loads, "the capture opened nothing"
    assert all("env=production" in url for url in loads), loads


def test_a_routes_own_query_is_merged_the_legs_key_winning(qa, world):
    routes = json.loads(json.dumps(ROUTES))
    routes["groups"]["work-a"] = [{"id": "defaults", "address": "settings/defaults",
                                   "path": "/settings/defaults?env=staging&tab=a", "ready": "main h1"}]
    world.routes.write_text(json.dumps(routes), encoding="utf-8")
    code, _out, _bundle = _capture(qa, world, "--entry-query", "env=production", presets="desktop",
                                   themes="light")
    assert code == 0
    loads = [u for u in _loads(world) if "/settings/defaults" in u]
    assert loads and all(u.endswith("/settings/defaults?tab=a&env=production") for u in loads), loads


def test_a_malformed_entry_query_opens_nothing(qa, world, capsys):
    code, out, _bundle = _capture(qa, world, "--entry-query", "token=abc&token=def", presets="desktop",
                                  themes="light")
    assert code == qa.EXIT_USAGE
    assert _loads(world) == [] and not out.exists()
    assert "--entry-query" in capsys.readouterr().err


def test_without_an_entry_query_the_urls_are_as_before(qa, world):
    code, _out, _bundle = _capture(qa, world, presets="desktop", themes="light")
    assert code == 0
    assert all("?" not in url for url in _loads(world)), _loads(world)
