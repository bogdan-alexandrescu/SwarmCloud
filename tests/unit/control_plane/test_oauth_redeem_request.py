"""The shape of the authorization_code token request, pinned.

`HttpTokenEndpoint.redeem` sent RFC 6749 form-encoding -- which is what the
spec prescribes, and what `exchange` (the refresh_token grant) demonstrably
needs. On a freshly issued code the endpoint answered:

    400 {"type":"error","error":{"type":"invalid_request_error",
         "message":"Invalid request format"}}      req_011CfHkoFVrh7ZqoY6jFzz4R

An invalid request FORMAT, not an invalid code -- the code was never examined.
It also omitted `state`, which Claude Code includes on the token request.

The asymmetry between the two grants is deliberate and measured, not a guess,
and this file exists so nobody "tidies" redeem back to match exchange. Both
directions of that mistake have now cost a real sign-in: `exchange`'s own
comment records that JSON there is answered 403.

Nothing inside this repository could have caught the original. Redeeming needs
a real authorization code from a real person signing in to Claude, so the first
execution of this method was against the live endpoint.
"""

from __future__ import annotations

import json

import pytest

from quota_broker.oauth import CLAUDE_CODE_CLIENT_ID, HttpTokenEndpoint


class _Captured(Exception):
    def __init__(self, request):
        self.request = request


@pytest.fixture
def capture(monkeypatch):
    """Intercept at urlopen so the real Request object is inspected."""
    import urllib.request

    def _urlopen(request, timeout=None):
        raise _Captured(request)

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)


def _send(**kw):
    ep = HttpTokenEndpoint()
    try:
        ep.redeem(code="the-code", verifier="the-verifier",
                  redirect_uri="https://platform.claude.com/oauth/code/callback", **kw)
    except _Captured as c:
        return c.request
    raise AssertionError("urlopen was not reached")


def test_the_authorization_code_grant_sends_json(capture):
    req = _send(state="st")
    assert req.get_header("Content-type") == "application/json", (
        "form-encoding is answered 400 invalid_request_error by this endpoint"
    )
    body = json.loads(req.data.decode())
    assert body["grant_type"] == "authorization_code"


def test_the_body_carries_every_field_the_grant_needs(capture):
    body = json.loads(_send(state="st").data.decode())
    assert body == {
        "grant_type": "authorization_code",
        "code": "the-code",
        "client_id": CLAUDE_CODE_CLIENT_ID,
        "redirect_uri": "https://platform.claude.com/oauth/code/callback",
        "code_verifier": "the-verifier",
        "state": "st",
    }


def test_state_is_sent_when_supplied(capture):
    assert json.loads(_send(state="abc123").data.decode())["state"] == "abc123"


def test_state_is_omitted_rather_than_sent_empty(capture):
    """An empty string is not a state. Sending one invites the endpoint to
    reject a request that would otherwise have been fine."""
    assert "state" not in json.loads(_send().data.decode())


def test_the_verifier_is_sent_and_never_the_challenge(capture):
    """PKCE: the token request carries the VERIFIER. Sending the challenge
    again would pass a server that never checks and fail one that does."""
    body = json.loads(_send(state="st").data.decode())
    assert body["code_verifier"] == "the-verifier"
    assert "code_challenge" not in body


def test_the_refresh_grant_still_uses_form_encoding(capture):
    """The other half of the asymmetry. exchange's comment records that JSON
    here is answered 403, so a tidy-up that unified them would break the
    path that currently works -- which is every credential refresh on the
    platform."""
    ep = HttpTokenEndpoint()
    try:
        ep.exchange("a-refresh-token")
    except _Captured as c:
        req = c.request
    else:  # pragma: no cover
        raise AssertionError("urlopen was not reached")

    assert req.get_header("Content-type") == "application/x-www-form-urlencoded"
    assert b"grant_type=refresh_token" in req.data
