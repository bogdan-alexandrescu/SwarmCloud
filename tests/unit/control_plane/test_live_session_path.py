"""A live session reports the page's path, checked like every field of its origin (LB-R6, part of #1030).

MEASURED 2026-10-12, re-QA pass 8 (task_4e0cf1c649634b949703): the caption
under the device read "iPhone 15 · 393×852 · path not reported" for a live
session -- the console was never told the page's path, because the gate's
report carried the origin only and swarm-api refused an origin with a `path`
in it. The owner's call on that pass: a live session must report its path,
which is what the mockup's caption has always drawn
("<device> · <W>×<H> · <path>", docs/design/live-browser.md §6 item 11).

The path is page data from an untrusted page, crossing the relay and stored
on `live_sessions/{id}`, so swarm-api checks it as it checks the origin: a
path (a leading `/`), no query and no fragment, printable, at most
`MAX_PATH_CHARS`, and redacted again. A path that fails is null -- shown as
"path not reported" -- and never takes the origin down with it: the strip is
the anti-phishing defence and does not depend on the path. A gate that
predates this reports no path; that is null too.

Every token here is built at runtime.
"""

from __future__ import annotations

import json
import secrets

import pytest

from swarm_api import live as live_module

ORIGIN = {"origin": "https://github.com", "host": "github.com", "display_host": "github.com",
          "registrable_domain": "github.com", "scheme": "https:", "ip_literal": False, "cert_error": None}
DEVICE = {"preset": "iphone-15", "orientation": "portrait", "touch": True}


def _report(origin: object) -> str:
    return json.dumps({"type": "report", "origin": origin, "device": DEVICE})


def _stored(origin: object) -> dict | None:
    report = live_module.parse_report(_report(origin))
    assert report is not None
    return report["origin"]


def test_a_reported_path_is_stored_with_its_origin():
    assert _stored({**ORIGIN, "path": "/settings/profile"}) == {**ORIGIN, "path": "/settings/profile"}


def test_a_gate_that_reports_no_path_stores_the_origin_with_a_null_path():
    assert _stored(ORIGIN) == {**ORIGIN, "path": None}


@pytest.mark.parametrize("path", [
    "settings",                      # not a path
    "/login?return_to=%2F",          # a query
    "/login#top",                    # a fragment
    "/a‮gnp.exe",               # a bidi override
    "/a\nb",                         # a control character
    "/" + "a" * 400,                 # longer than any the gate sends
    7,
])
def test_a_path_that_does_not_hold_together_is_null_and_the_origin_stands(path):
    assert _stored({**ORIGIN, "path": path}) == {**ORIGIN, "path": None}


def test_a_secret_in_a_stored_path_is_redacted_again():
    token = "ghp_" + secrets.token_hex(18)
    stored = _stored({**ORIGIN, "path": f"/reset/{token}"})
    assert stored is not None and token not in json.dumps(stored)
    assert stored["path"].startswith("/reset/")


def test_the_served_origin_model_carries_the_path():
    assert "path" in live_module.LiveOrigin.model_fields
    live_module.LiveOrigin.model_validate({**ORIGIN, "path": "/login"})


def test_the_gate_and_swarm_api_agree_on_the_longest_path():
    """The gate cuts at its limit; swarm-api refuses past its own. Two limits
    that drift apart turn every long path into "path not reported"."""
    from agent_worker.livegate import report

    assert report.MAX_PATH_CHARS == live_module.MAX_PATH_CHARS
