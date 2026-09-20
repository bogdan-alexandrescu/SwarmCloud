"""Acting as a Workspace user from Cloud Run, without a key file.

WHAT THESE ARE WRITTEN AGAINST. The original delegation guarded on
`hasattr(credentials, "with_subject")` and assumed the other case was a
developer's user credentials. The PRODUCTION case is Cloud Run, whose
metadata credentials have no `with_subject` either -- so the branch never fired
where it mattered, every group lookup fell back to the service account's own
identity, and Cloud Identity answered Error(2028), which reads exactly like the
missing-role problem delegation was introduced to solve.

That cost a live outage on 2026-09-20: turning `tenant_groups` back on 503'd
every authenticated request, because a failed lookup is fatal by design.

So the first test here is the one that would have caught it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from swarm_api import delegation
from swarm_api.delegation import DelegatedCredentials, DelegationError, delegate

SCOPE = "https://www.googleapis.com/auth/cloud-identity.groups.readonly"
SA = "swarm-api@example.iam.gserviceaccount.com"
USER = "bogdan@example.com"


class _Response:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _Session:
    """Records what was signed and what was exchanged."""

    def __init__(self, sign=None, exchange=None):
        self.calls: list[tuple[str, dict]] = []
        self._sign = sign or _Response(200, {"signedJwt": "signed.assertion.here"})
        self._exchange = exchange or _Response(
            200, {"access_token": "delegated-token", "expires_in": 3600}
        )

    def post(self, url, json=None, data=None, timeout=None):
        self.calls.append((url, {"json": json, "data": data}))
        if "signJwt" in url:
            return self._sign
        return self._exchange

    @property
    def signed_claims(self) -> dict:
        for url, kwargs in self.calls:
            if "signJwt" in url:
                import json as _json

                return _json.loads(kwargs["json"]["payload"])
        raise AssertionError("nothing was signed")


class _KeyFileCredentials:
    """What a service-account KEY produces. The one kind with with_subject."""

    def __init__(self):
        self.subject = None

    def with_subject(self, subject):
        clone = _KeyFileCredentials()
        clone.subject = subject
        return clone


class _MetadataCredentials:
    """What Cloud Run produces. No with_subject -- the case that broke."""

    service_account_email = SA


class _UserCredentials:
    """What a developer's `gcloud auth login` produces. Neither route works."""


def _creds(session, **kw):
    return DelegatedCredentials(
        source=object(),
        service_account=SA,
        subject=USER,
        scopes=[SCOPE],
        session_factory=lambda: session,
        now=lambda: datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc),
        **kw,
    )


# -- routing ---------------------------------------------------------------


def test_cloud_run_credentials_get_the_keyless_route_not_a_silent_fallback():
    """THE REGRESSION TEST. Metadata credentials have no `with_subject`, and
    the original code treated that as "cannot delegate" and carried on with an
    identity Cloud Identity will never answer for."""
    result = delegate(_MetadataCredentials(), subject=USER, scopes=[SCOPE])
    assert isinstance(result, DelegatedCredentials)
    assert result.subject == USER


def test_a_key_file_keeps_the_direct_path():
    """`with_subject` is one call and needs no extra IAM, so it stays."""
    result = delegate(_KeyFileCredentials(), subject=USER, scopes=[SCOPE])
    assert isinstance(result, _KeyFileCredentials)
    assert result.subject == USER


def test_user_credentials_degrade_instead_of_crashing(monkeypatch, caplog):
    """A developer running the API locally has neither route. That is the
    pre-existing local experience and must not become a crash."""
    monkeypatch.setattr(delegation, "service_account_email", lambda c: None)
    creds = _UserCredentials()
    assert delegate(creds, subject=USER, scopes=[SCOPE]) is creds


@pytest.mark.parametrize("reported", ["default", "", None, "not-an-email"])
def test_an_unusable_service_account_email_is_rejected_not_signed_with(reported):
    """Compute and Cloud Run credentials report `service_account_email` as the
    literal string "default" until they have been refreshed. Signing an
    assertion whose `iss` is "default" fails at the token endpoint with a
    message about the assertion, not about the identity -- so it is rejected
    here, where the cause is still visible.

    Off GCP the metadata fallback cannot answer either, so this returns None
    and `delegate` degrades rather than minting nonsense.
    """
    class _Reporting:
        service_account_email = reported

    assert delegation.service_account_email(_Reporting()) is None


def test_a_real_service_account_email_is_used_as_is():
    class _Real:
        service_account_email = SA

    assert delegation.service_account_email(_Real()) == SA


# -- the assertion ---------------------------------------------------------


def test_the_assertion_asserts_the_user_and_that_is_the_entire_point():
    session = _Session()
    _creds(session).refresh(None)
    claims = session.signed_claims

    assert claims["sub"] == USER, "without `sub` this is just the service account again"
    assert claims["iss"] == SA
    assert claims["scope"] == SCOPE
    assert claims["aud"] == delegation.TOKEN_ENDPOINT


def test_the_assertion_is_minted_slightly_short_of_an_hour():
    """Google caps it at 3600 and rejects more; asking for exactly 3600 leaves
    no room for clock skew against the token endpoint."""
    session = _Session()
    _creds(session).refresh(None)
    claims = session.signed_claims
    assert 0 < claims["exp"] - claims["iat"] < 3600


def test_the_token_expires_early_enough_that_a_request_cannot_outlive_it():
    """A request that starts with twenty seconds left can still be in flight
    when the token dies, and that failure looks like an authorization problem
    rather than a timing one."""
    session = _Session()
    creds = _creds(session)
    creds.refresh(None)
    assert creds.token == "delegated-token"
    lifetime = (creds.expiry - datetime(2026, 9, 20, 12, 0)).total_seconds()
    assert lifetime == 3600 - delegation._REFRESH_SKEW


def test_it_signs_then_exchanges_in_that_order():
    session = _Session()
    _creds(session).refresh(None)
    assert "signJwt" in session.calls[0][0]
    assert session.calls[1][0] == delegation.TOKEN_ENDPOINT
    assert session.calls[1][1]["data"]["assertion"] == "signed.assertion.here"
    assert session.calls[1][1]["data"]["grant_type"] == delegation.JWT_BEARER


# -- the two failures that look like each other ----------------------------


def test_a_signing_refusal_names_the_binding_rather_than_saying_denied():
    """This is the failure that reads like a tautology: the service account
    must be granted permission to sign as ITSELF. An operator who is told only
    "403" will look at the Admin console, which is the wrong console."""
    session = _Session(sign=_Response(403, text="PERMISSION_DENIED"))
    with pytest.raises(DelegationError) as caught:
        _creds(session).refresh(None)

    message = str(caught.value)
    assert "roles/iam.serviceAccountTokenCreator" in message
    assert "being the identity does not" in message


def test_a_domain_refusal_names_the_admin_console_rather_than_iam():
    """The mirror image, and the one people reach for first. `unauthorized_client`
    means the Workspace grant is missing -- no amount of GCP IAM fixes it."""
    session = _Session(
        exchange=_Response(400, text='{"error":"unauthorized_client"}')
    )
    with pytest.raises(DelegationError) as caught:
        _creds(session).refresh(None)

    message = str(caught.value)
    assert "Admin console" in message
    assert SCOPE in message


def test_the_two_refusals_do_not_read_alike():
    """They have opposite remedies in different consoles. Any wording that
    collapses them sends the operator to the wrong one, which is exactly what
    happened on 2026-09-20."""
    iam = _Session(sign=_Response(403, text="PERMISSION_DENIED"))
    domain = _Session(exchange=_Response(400, text='{"error":"unauthorized_client"}'))

    with pytest.raises(DelegationError) as a:
        _creds(iam).refresh(None)
    with pytest.raises(DelegationError) as b:
        _creds(domain).refresh(None)

    assert str(a.value) != str(b.value)
    assert ("Admin console" in str(b.value)) and ("Admin console" not in str(a.value))


def test_a_signed_assertion_that_is_missing_is_not_treated_as_empty():
    session = _Session(sign=_Response(200, {}))
    with pytest.raises(DelegationError, match="no assertion"):
        _creds(session).refresh(None)


def test_an_exchange_that_returns_no_token_is_an_error_not_a_none_token():
    session = _Session(exchange=_Response(200, {"expires_in": 3600}))
    with pytest.raises(DelegationError, match="no access_token"):
        _creds(session).refresh(None)


def test_credentials_reporting_default_are_refreshed_to_learn_their_identity():
    """THE SECOND REGRESSION TEST, for the miss that cost a deploy cycle.

    Cloud Run's credentials report `service_account_email` as "default" until
    `refresh()` is called -- that refresh is what asks the metadata server and
    fills the attribute in. The first attempt skipped it and went straight to a
    hand-rolled urllib call, which failed and logged

        cannot delegate ... no metadata server answered

    on a machine whose metadata server was working perfectly.
    """
    class _CloudRunLike:
        def __init__(self):
            self.service_account_email = "default"
            self.refreshed = 0

        def refresh(self, request):
            self.refreshed += 1
            self.service_account_email = SA

    creds = _CloudRunLike()
    assert delegation.service_account_email(creds) == SA
    assert creds.refreshed == 1, "the attribute was read without ever refreshing"


def test_an_already_known_identity_is_not_refreshed_needlessly():
    """A refresh is a network round trip on a request path."""
    class _Known:
        service_account_email = SA

        def refresh(self, request):  # pragma: no cover - must not be called
            raise AssertionError("refreshed despite already knowing the identity")

    assert delegation.service_account_email(_Known()) == SA


def test_a_refresh_that_raises_is_logged_and_not_swallowed(caplog):
    """A silent None is indistinguishable from "not running on GCP", and
    telling those two apart is the entire diagnosis."""
    class _Broken:
        service_account_email = "default"

        def refresh(self, request):
            raise RuntimeError("metadata server said no")

    import logging

    with caplog.at_level(logging.WARNING, logger=delegation.log.name):
        delegation.service_account_email(_Broken())
    assert any("metadata server said no" in r.getMessage() for r in caplog.records)
