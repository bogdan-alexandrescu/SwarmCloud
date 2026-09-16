"""Cloud Run adds its own Authorization header; the caller's token must still win.

Diagnosed live on 2026-09-16: the fingerprint the service logged never matched
the one the client sent, and verification failed with MalformedError. Starlette
joins repeated headers with ", ", so a naive partition(" ") yielded
`<caller-token>, Bearer <platform-token>` -- not a JWT.
"""

from __future__ import annotations

import pytest

from swarm_api.auth import Unauthenticated, bearer_token, bearer_tokens


def test_single_credential_is_unchanged():
    assert bearer_tokens("Bearer abc.def.ghi") == ["abc.def.ghi"]
    assert bearer_token("Bearer abc.def.ghi") == "abc.def.ghi"


def test_two_joined_credentials_are_split():
    """The exact shape Starlette produces for duplicate Authorization headers."""
    header = "Bearer caller.token.here, Bearer platform.token.here"
    assert bearer_tokens(header) == ["caller.token.here", "platform.token.here"]


def test_naive_parse_would_have_returned_a_non_jwt():
    """Regression guard: the old behaviour produced something with 2 commas."""
    header = "Bearer caller.token.here, Bearer platform.token.here"
    naive = header.partition(" ")[2].strip()
    assert "," in naive, "this is what used to reach verify() and raise MalformedError"
    assert naive not in bearer_tokens(header)


def test_missing_header_is_refused():
    with pytest.raises(Unauthenticated):
        bearer_tokens(None)


def test_non_bearer_scheme_is_refused():
    with pytest.raises(Unauthenticated):
        bearer_tokens("Basic dXNlcjpwYXNz")


def test_header_never_appears_in_the_error():
    """A token is full impersonation; it must not leak through an exception."""
    secret = "sup3rs3cret.token.value"
    with pytest.raises(Unauthenticated) as err:
        bearer_tokens(f"Basic {secret}")
    assert secret not in str(err.value)
