"""The receiver's three outside calls, over REST with google-auth, each as the
service's own identity -- the rollup sweeper account (owner decision
2026-10-16; terraform/infra/github_hooks.tf):

  * Secret Manager: the latest version of swarm-github-app-webhook-secret,
    into memory (`read_secret`);
  * Firestore: `createDocument` of github_deliveries/{delivery id}, which
    answers 409 ALREADY_EXISTS for a redelivery -- the dedup
    (`FirestoreDeliveries`);
  * swarm-api: `POST /v1/admin/repositories/push` with
    `{"forge": "github", "full_name": ...}` and an ID token minted for
    swarm-api's audience (`ApiForwarder`).

No client is built at import, and none until the first call that needs it:
every constructor here takes a factory or a callable.

An error never quotes an answer body: Google's errors can echo the request.
"""

from __future__ import annotations

import base64
import binascii
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from .receiver import COLLECTION, StoreUnavailable

SECRET_MANAGER = "https://secretmanager.googleapis.com/v1/"
FIRESTORE = "https://firestore.googleapis.com/v1/"
#: GS-PUSHAPI's route (apps/swarm-api/swarm_api/routes/admin.py `push_repository`).
PUSH_ROUTE = "/v1/admin/repositories/push"
SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
#: A push is one Firestore query and a head read per registration; generous,
#: but bounded so a wedged swarm-api cannot hold a receiver thread for ever.
FORWARD_TIMEOUT_SECONDS = 30
STORE_TIMEOUT_SECONDS = 10


def authorized_session() -> Any:
    """The service's own identity (Cloud Run's metadata server), for Secret
    Manager and Firestore."""
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = google.auth.default(scopes=SCOPES)
    return AuthorizedSession(credentials)


def read_secret(version: str, session: Any) -> bytes:
    response = session.get(SECRET_MANAGER + version + ":access", timeout=30)
    if response.status_code != 200:
        # The status only: an error body may quote the request.
        raise RuntimeError(f"Secret Manager answered HTTP {response.status_code}")
    try:
        value = base64.b64decode(response.json()["payload"]["data"])
    except (KeyError, TypeError, ValueError, binascii.Error):
        raise RuntimeError("Secret Manager returned no payload") from None
    if not value:
        raise RuntimeError("Secret Manager returned no payload")
    return value


class _Lazy:
    """One session, built by `factory` on first use."""

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._session: Any = None
        self._lock = threading.Lock()

    def get(self) -> Any:
        with self._lock:
            if self._session is None:
                self._session = self._factory()
            return self._session


class FirestoreDeliveries:
    """github_deliveries/{id}, created and never updated: `create()` is the dedup."""

    def __init__(self, project: str, database: str, session_factory: Callable[[], Any]) -> None:
        self._url = f"{FIRESTORE}projects/{project}/databases/{database}/documents/{COLLECTION}"
        self._session = _Lazy(session_factory)

    def create(self, delivery_id: str, expires_at: datetime) -> bool:
        stamp = expires_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        response = self._session.get().post(
            self._url,
            params={"documentId": delivery_id},
            json={"fields": {"expires_at": {"timestampValue": stamp}}},
            timeout=STORE_TIMEOUT_SECONDS,
        )
        if response.status_code == 200:
            return True
        if response.status_code == 409:
            return False
        raise StoreUnavailable(f"http_{response.status_code}")


def mint_id_token(audience: str) -> str:
    """An ID token for `audience`, as the service's own account (the metadata
    server on Cloud Run)."""
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token

    return str(id_token.fetch_id_token(Request(), audience))


def _post(url: str, **kwargs: Any) -> Any:
    import requests

    return requests.post(url, **kwargs)


class ApiForwarder:
    """`full_name` -> swarm-api's push route. Answers swarm-api's status."""

    def __init__(self, api_url: str, audience: str, *,
                 mint: Callable[[str], str] = mint_id_token,
                 post: Callable[..., Any] = _post) -> None:
        self._url = api_url.rstrip("/") + PUSH_ROUTE
        self._audience = audience
        self._mint = mint
        self._post = post

    def __call__(self, full_name: str) -> int:
        response = self._post(
            self._url,
            json={"forge": "github", "full_name": full_name},
            headers={"Authorization": f"Bearer {self._mint(self._audience)}"},
            timeout=FORWARD_TIMEOUT_SECONDS,
            # A redirect would carry the bearer to wherever it points.
            allow_redirects=False,
        )
        return int(response.status_code)


__all__ = [
    "ApiForwarder", "FirestoreDeliveries", "PUSH_ROUTE", "authorized_session",
    "mint_id_token", "read_secret",
]
