"""Acting as a Workspace user from Cloud Run, without a service-account key.

WHY THIS FILE EXISTS
--------------------
Cloud Identity's Groups API does not authorize through GCP IAM, so a
`*.gserviceaccount.com` identity cannot read a group no matter which role it
holds. Domain-wide delegation fixes that by letting the service account ACT AS
a real Workspace user -- and the obvious way to use it,
`credentials.with_subject(user)`, exists ONLY on credentials loaded from a
service-account KEY FILE.

On Cloud Run there is no key file. `google.auth.default()` returns
metadata-server credentials, which have no `with_subject`, so the delegation
branch silently did not fire and every lookup fell back to the service
account's own identity:

    impersonation requested for bogdan@saga.xyz but these credentials cannot
    delegate; group lookups will use the service account's own identity
    cloud identity returned 403: Error(2028): Permission denied for resource

The Admin-console grant was necessary and never sufficient. Observed live on
2026-09-20, when turning `tenant_groups` back on 503'd every authenticated
request -- because a failed lookup is fatal by design, since an unknown
higher-priority group could file a caller's work under the wrong tenant.

The keyless equivalent is to build the assertion ourselves and have Google sign
it with the key we are not allowed to hold:

    1. claims  {iss: <sa>, sub: <user>, scope: <scope>, aud: token endpoint}
    2. sign    iamcredentials.googleapis.com …:signJwt   (Google holds the key)
    3. exchange the signed assertion at oauth2.googleapis.com/token
    4. use the returned access token, which now acts as <user>

Step 2 needs `roles/iam.serviceAccountTokenCreator` ON ITSELF -- the service
account must be permitted to sign as itself, which is not implied by being it.
That binding is in terraform beside the service; without it this raises with
the permission named, rather than degrading to the 2028 that started all this.

WHY NOT `impersonated_credentials`. `google.auth.impersonated_credentials`
performs a different operation: it lets principal A act as service account B.
It has no `subject` and cannot assert a Workspace user, so it does not solve
this problem despite the name suggesting it might.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

import google.auth.credentials
from google.auth.transport.requests import AuthorizedSession

log = logging.getLogger(__name__)

IAM_CREDENTIALS_ROOT = "https://iamcredentials.googleapis.com/v1"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
CLOUD_PLATFORM = "https://www.googleapis.com/auth/cloud-platform"

#: Google caps a delegated assertion at one hour. Asking for more is rejected,
#: and asking for exactly 3600 leaves no room for clock skew between us and the
#: token endpoint, so the assertion is minted slightly short.
_ASSERTION_LIFETIME = 3540

#: Refresh this far before expiry. A request that starts with 20 seconds left
#: on the token can still be in flight when it dies, and the failure would look
#: like an authorization problem rather than a timing one.
_REFRESH_SKEW = 120


class DelegationError(Exception):
    """Delegation could not be established. Names the cause, never 'denied'."""


def _usable(email: Any) -> bool:
    """Is this an address we can put in an `iss` claim?

    Compute and Cloud Run credentials report `service_account_email` as the
    literal string "default" until they have been refreshed. Signing an
    assertion whose issuer is "default" fails at the token endpoint with a
    message about the assertion rather than about the identity, so it is
    rejected here, where the cause is still visible.
    """
    return isinstance(email, str) and bool(email) and email != "default" and "@" in email


def service_account_email(credentials: Any) -> str | None:
    """Whose identity is this? Asked three ways, cheapest first.

    THE MIDDLE ONE IS THE IMPORTANT ONE AND WAS MISSING. On Cloud Run
    `google.auth.default()` returns compute-engine credentials whose
    `service_account_email` is "default" until `refresh()` is called -- that
    refresh is what asks the metadata server and fills the attribute in. The
    first attempt at this went straight to a hand-rolled urllib call against
    metadata.google.internal instead, which failed silently and produced

        cannot delegate to bogdan@saga.xyz: these credentials are not a
        service account and no metadata server answered

    on a machine whose metadata server was working perfectly. Letting
    google-auth do its own metadata lookup avoids re-deriving the address, the
    required header, the timeout and the retry policy -- four things it already
    knows and this module has no business restating.
    """
    email = getattr(credentials, "service_account_email", None)
    if _usable(email):
        return email

    try:
        from google.auth.transport.requests import Request

        credentials.refresh(Request())
        email = getattr(credentials, "service_account_email", None)
        if _usable(email):
            return email
    except Exception as exc:
        # Logged, never swallowed. A silent None here is indistinguishable from
        # "not running on GCP", and telling those apart is the whole diagnosis.
        log.warning("could not refresh credentials to learn their identity: %s", exc)

    try:
        import urllib.request

        request = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1"
            "/instance/service_accounts/default/email",
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            value = response.read().decode().strip()
        return value if _usable(value) else None
    except Exception as exc:
        log.warning("the metadata server did not name this identity: %s", exc)
        return None


class DelegatedCredentials(google.auth.credentials.Credentials):
    """Credentials that act as a Workspace user, signed by Google on our behalf.

    `session_factory` and `now` exist so the whole exchange is testable without
    credentials, a network or a clock.
    """

    def __init__(
        self,
        *,
        source: Any,
        service_account: str,
        subject: str,
        scopes: Sequence[str],
        session_factory: Any = None,
        now: Any = None,
    ) -> None:
        super().__init__()
        self._source = source
        self._service_account = service_account
        self._subject = subject
        self._scopes = tuple(scopes)
        self._session_factory = session_factory or (lambda: AuthorizedSession(source))
        self._now = now or (lambda: datetime.now(timezone.utc))

    @property
    def subject(self) -> str:
        return self._subject

    def refresh(self, request: Any) -> None:  # noqa: ARG002 - google-auth's signature
        session = self._session_factory()
        issued = self._now()
        claims = {
            "iss": self._service_account,
            # The whole point. Everything else here is scaffolding around this
            # one field, which is what turns a service account into a person
            # the Groups API is willing to answer.
            "sub": self._subject,
            "scope": " ".join(self._scopes),
            "aud": TOKEN_ENDPOINT,
            "iat": int(issued.timestamp()),
            "exp": int(issued.timestamp()) + _ASSERTION_LIFETIME,
        }

        signed = self._sign(session, claims)
        token, expires_in = self._exchange(session, signed)
        self.token = token
        self.expiry = (issued + timedelta(seconds=expires_in - _REFRESH_SKEW)).replace(
            tzinfo=None
        )

    def _sign(self, session: Any, claims: dict[str, Any]) -> str:
        url = (
            f"{IAM_CREDENTIALS_ROOT}/projects/-/serviceAccounts/"
            f"{self._service_account}:signJwt"
        )
        response = session.post(url, json={"payload": json.dumps(claims)}, timeout=10)
        if response.status_code == 403:
            body = _body(response)
            # TWO DIFFERENT 403s, and the first version of this message
            # confidently named the wrong one. A missing binding and an
            # under-scoped token both come back 403 PERMISSION_DENIED from the
            # same endpoint; only `reason` tells them apart, and they are fixed
            # in completely different places -- one in IAM, one in the code
            # that asked for the credentials.
            if "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in body:
                raise DelegationError(
                    "the credentials used to sign carry the wrong OAuth scopes. "
                    f"signJwt needs {CLOUD_PLATFORM}; a token requested with only "
                    "a narrow scope cannot call it. This is a bug in the caller, "
                    f"not a missing IAM binding. ({body})"
                )
            raise DelegationError(
                f"{self._service_account} may not sign as itself. Grant "
                "roles/iam.serviceAccountTokenCreator on that service account "
                "TO that same service account -- being the identity does not "
                f"imply permission to sign as it. ({body})"
            )
        if response.status_code != 200:
            raise DelegationError(
                f"signJwt returned {response.status_code}: {_body(response)}"
            )
        signed = response.json().get("signedJwt")
        if not signed:
            raise DelegationError("signJwt returned no assertion")
        return signed

    def _exchange(self, session: Any, assertion: str) -> tuple[str, int]:
        response = session.post(
            TOKEN_ENDPOINT,
            data={"grant_type": JWT_BEARER, "assertion": assertion},
            timeout=10,
        )
        if response.status_code != 200:
            body = _body(response)
            if "unauthorized_client" in body:
                raise DelegationError(
                    "the domain refused this delegation: the service account's "
                    "OAuth client id is not authorised in the Admin console for "
                    f"scope(s) {' '.join(self._scopes)}, or the subject "
                    f"{self._subject!r} is not a user in that domain. "
                    f"({body})"
                )
            raise DelegationError(f"token exchange returned {response.status_code}: {body}")
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise DelegationError("the token exchange returned no access_token")
        return token, int(payload.get("expires_in") or 3600)


def _body(response: Any) -> str:
    try:
        return str(response.text)[:300]
    except Exception:  # pragma: no cover - defensive
        return "<unreadable>"


def delegate(
    credentials: Any,
    *,
    subject: str,
    scopes: Sequence[str],
    session_factory: Any = None,
    signer_credentials: Any = None,
) -> Any:
    """Credentials acting as `subject`, by whichever route is available.

    A key file keeps the direct path: `with_subject` is one call and needs no
    extra IAM. Everything else -- Cloud Run, GCE, GKE -- goes through signJwt.
    A developer's USER credentials can do neither, and that is not an error
    worth crashing for: the caller falls back and sees the same 2028 it would
    have seen anyway, which is the pre-existing local-development experience.
    """
    if hasattr(credentials, "with_subject"):
        return credentials.with_subject(subject)

    email = service_account_email(credentials)
    if not email:
        log.warning(
            "cannot delegate to %s: these credentials are not a service account "
            "and no metadata server answered, so group lookups will use the "
            "caller's own identity and will fail with Error(2028)",
            subject,
        )
        return credentials

    # THE SIGNER IS NOT THE CALLER'S CREDENTIALS, and conflating them cost a
    # deploy cycle. `credentials` here was obtained with only the Cloud Identity
    # groups scope, because that is what the group lookup needs. `signJwt` is a
    # different API and requires cloud-platform, so signing with the narrow
    # token returns 403 ACCESS_TOKEN_SCOPE_INSUFFICIENT -- indistinguishable at
    # a glance from the missing-binding 403, and fixed somewhere else entirely.
    signer = signer_credentials
    if signer is None:
        try:
            import google.auth

            signer, _ = google.auth.default(scopes=[CLOUD_PLATFORM])
        except Exception as exc:
            log.warning("could not obtain cloud-platform credentials to sign with: %s", exc)
            signer = credentials

    log.info("delegating group lookups to %s as %s via signJwt", subject, email)
    return DelegatedCredentials(
        source=signer,
        service_account=email,
        subject=subject,
        scopes=scopes,
        session_factory=session_factory,
    )
