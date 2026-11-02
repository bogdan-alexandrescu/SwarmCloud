"""Voice it's "Sign in with ChatGPT": the first version of the person's OpenAI secret.

THE OWNER'S PICK (BGD, 2026-11-02): "Sign-in button, then go live". swarm-voice
runs a GPT-Live session on the person's own ChatGPT plan
(`swarm_voice.credential.SubscriptionCredential`), which reads the person's
personal secret `swarm-tenant-<personal tenant>-openai` as

    {"kind": "chatgpt_subscription", "client_id": ..., "refresh_token": ...}

and writes each rotated refresh token back as a new version. Nothing wrote the
first version; this module does, from a sign-in the person makes in the console
(Voice it › Preferences).

THE SIGN-IN IS THE ACCOUNT POOL'S, NOT A SECOND ONE. The ChatGPT-plan pool
already signs a plan in (`quota_broker.openai_plan` and `quota_broker.oauth`):
PKCE at auth.openai.com with the Codex CLI's public client, the redirect
`http://localhost:1455/auth/callback` (measured: `127.0.0.1` failed live),
scope `openid profile email offline_access`, and a form POST to `/oauth/token`.
swarm-api's image installs swarm-api and the shared packages only
(images/swarm-api/Dockerfile), so the broker's module cannot be imported here;
its constants and its authorize parameters are restated below and
tests/unit/control_plane/test_voice_signin.py holds every one EQUAL to the
broker's, so the two cannot drift. Moving them into a shared package is asked
in the pull request.

WHERE THE CODE COMES BACK. The client's one registered redirect is the
person's own machine. The broker's command-line half listens on port 1455 and
catches it (`sc account add --provider openai`); a browser cannot listen, so
after signing in the person's browser opens `http://localhost:1455/...` and
shows a page that does not load. Its address carries the code and the state:
the person copies the whole address from the address bar and pastes it into
Preferences. `parse_callback` takes that address, `<code>#<state>` (the shape
the broker's paste takes) or the bare code.

THE RULES THIS MODULE HOLDS (invariant 9; voice-it.md §3.6):

* ONLY THE CALLER'S OWN SECRET. The secret id is computed from the verified
  email (`tenant_id_for_user`, as `swarm_voice.credential.personal_secret_id`
  computes it); no field of a request names a person, a tenant or a secret.
  A pending sign-in is bound to the person who started it, and another person
  finishing it is refused before the code is redeemed.
* WRITE-ONLY. The secret is written through Config › Secrets' writer
  (`workspace_secrets.SecretManagerWorkspaceSecrets`): create the secret if it
  is missing, add a version, and NO IAM write. The person's worker account
  gets no read on it; swarm-voice's read and add-version grants are terraform's
  (terraform/infra/voice.tf). Nothing here reads a payload back.
* THE REFRESH TOKEN APPEARS NOWHERE ELSE. Not in a response, a log line, an
  audit row, a Firestore field or an exception: errors carry a status only,
  because an error body can quote the request. The status record and its
  audit row are `provider_keys`' (`ProviderKeys.record`): set, when, by whom,
  the version name.
* A SIGN-IN IS SINGLE-USE AND SHORT. The pending record (state, PKCE verifier,
  person) is deleted in the transaction that claims it, before the code is
  redeemed, and is dead after `PENDING_TTL`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit

from google.cloud import firestore
from swarm_common.identity import tenant_id_for_user

from ..errors import ApiError, Forbidden, UpstreamUnavailable, ValidationFailed
from ..provider_keys import USER, ProviderKeys
from ..workspace_secrets import SecretWriter

log = logging.getLogger(__name__)

# -- the pool's sign-in, restated (module docstring; a test holds them equal) --

ISSUER = "https://auth.openai.com"
AUTHORIZE_ENDPOINT = f"{ISSUER}/oauth/authorize"
TOKEN_ENDPOINT = f"{ISSUER}/oauth/token"
#: The Codex CLI's public PKCE client: not a secret. It is also the
#: `client_id` the stored credential names, so swarm-voice refreshes with it.
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPES = "openid profile email offline_access"

#: What the stored credential says it is: `swarm_voice.credential.KIND_SUBSCRIPTION`.
KIND_SUBSCRIPTION = "chatgpt_subscription"
#: The secret's suffix, and the `provider_keys` record's provider.
PROVIDER = "openai"

#: One document per started sign-in, keyed by sha256(state). Holds the PKCE
#: verifier and the person; never a code or a token.
PENDING = "voice_chatgpt_signins"
#: The broker's own sign-in window: long enough to sign in and paste.
PENDING_TTL = timedelta(minutes=15)
#: Bounds on what the person pastes: an address with a code and a state is
#: well under 2 KiB; anything longer is not the callback address.
MAX_PASTE = 4096

#: What the console shows the person, once, beside the authorize link.
INSTRUCTIONS = (
    "Sign in to ChatGPT in the tab that opens. When it is done, that tab goes to "
    "an address starting http://localhost:1455/auth/callback and says the page "
    "cannot be reached: that is expected. Copy the whole address from the "
    "address bar and paste it here."
)


class SignInRefused(Forbidden):
    """Another person's pending sign-in: the established `forbidden` code."""


class SignInFailed(ApiError):
    """OpenAI's token endpoint refused or could not be reached. Status only."""

    status_code = 502
    code = "chatgpt_sign_in_failed"


# -- PKCE, as quota_broker.oauth makes it ------------------------------------------


def new_verifier() -> str:
    return secrets.token_urlsafe(32)


def challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def new_state() -> str:
    return secrets.token_urlsafe(32)


def authorize_url(*, state: str, code_challenge: str) -> str:
    """`quota_broker.openai_plan.build_authorize_url`, parameter for parameter."""
    return f"{AUTHORIZE_ENDPOINT}?" + urlencode(
        {
            "response_type": "code",
            "client_id": CODEX_CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "state": state,
        }
    )


def parse_callback(pasted: str) -> tuple[str, str | None]:
    """The code, and the state when the paste carries one.

    Takes the callback address from the address bar, `<code>#<state>`, or the
    bare code. Every refusal is a constant: the paste holds a live code."""
    value = (pasted or "").strip()
    if not value or len(value) > MAX_PASTE:
        raise ValidationFailed("paste the whole address the sign-in ended on")
    if value.lower().startswith(("http://", "https://")):
        query = parse_qs(urlsplit(value).query)
        if query.get("error"):
            raise ValidationFailed(
                "ChatGPT did not complete the sign-in; press Sign in with ChatGPT to try again")
        codes = query.get("code") or []
        states = query.get("state") or []
        if len(codes) != 1 or not codes[0].strip():
            raise ValidationFailed(
                "that address carries no sign-in code; copy the whole address the "
                "sign-in tab ended on")
        return codes[0].strip(), (states[0].strip() or None) if states else None
    if "#" in value:
        code, _, state = value.partition("#")
        return code.strip(), (state.strip() or None)
    return value, None


def stored_payload(refresh_token: str) -> bytes:
    """Exactly what `swarm_voice.credential.SubscriptionCredential` parses."""
    return json.dumps({
        "kind": KIND_SUBSCRIPTION,
        "client_id": CODEX_CLIENT_ID,
        "refresh_token": refresh_token,
    }).encode("utf-8")


def personal_secret_id(person: str) -> str:
    """`swarm_voice.credential.personal_secret_id`: the person's own OpenAI secret."""
    return f"swarm-tenant-{tenant_id_for_user(person)}-{PROVIDER}"


def _digest(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# -- the token endpoint ---------------------------------------------------------------


class TokenEndpoint(Protocol):
    def redeem(self, *, code: str, verifier: str) -> dict[str, Any]: ...


class HttpTokenEndpoint:
    """auth.openai.com's token endpoint, as `quota_broker.openai_plan.HttpPlanTokenEndpoint`
    redeems a code. Errors carry the HTTP status only: the body can echo the form."""

    def __init__(self, url: str = TOKEN_ENDPOINT, *, timeout: float = 30.0) -> None:
        self._url = url
        self._timeout = timeout

    def redeem(self, *, code: str, verifier: str) -> dict[str, Any]:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            self._url,
            data=urlencode({
                "grant_type": "authorization_code",
                "client_id": CODEX_CLIENT_ID,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": REDIRECT_URI,
            }).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise SignInFailed(
                f"ChatGPT refused the sign-in code (HTTP {exc.code}); codes are "
                "single-use and short-lived, so press Sign in with ChatGPT again") from None
        except urllib.error.URLError as exc:
            raise SignInFailed(
                f"could not reach ChatGPT's sign-in ({type(exc.reason).__name__})") from None
        except ValueError:
            raise SignInFailed("ChatGPT's sign-in answered with no JSON") from None


# -- the sign-in ------------------------------------------------------------------------


class ChatGPTSignIn:
    """Start, finish and report the caller's ChatGPT sign-in. `person` is always
    the verified email; nothing else chooses whose secret is written."""

    def __init__(self, db: Any, writer: SecretWriter, tokens: TokenEndpoint, *,
                 now: Callable[[], datetime]) -> None:
        self._db = db
        self._writer = writer
        self._tokens = tokens
        self._now = now

    def _keys(self) -> ProviderKeys:
        return ProviderKeys(self._db, now=self._now)

    def status(self, person: str) -> dict[str, Any]:
        """Whether a sign-in is stored, and when. Never a value."""
        record = self._keys().status(tenant_id_for_user(person), PROVIDER)
        return {"stored": bool(record["set"]), "stored_at": record["set_at"]}

    def begin(self, person: str) -> dict[str, Any]:
        verifier = new_verifier()
        state = new_state()
        self._db.collection(PENDING).document(_digest(state)).set({
            "person": person,
            "verifier": verifier,
            "created_at": self._now(),
        })
        return {
            "authorize_url": authorize_url(state=state, code_challenge=challenge_for(verifier)),
            "state": state,
            "redirect_uri": REDIRECT_URI,
            "expires_in_seconds": int(PENDING_TTL.total_seconds()),
            "instructions": INSTRUCTIONS,
        }

    def _claim(self, person: str, state: str) -> str:
        """The pending sign-in's verifier, its record deleted in the same
        transaction, so one code is redeemed at most once."""
        ref = self._db.collection(PENDING).document(_digest(state))
        now = self._now()
        transaction = self._db.transaction()

        @firestore.transactional
        def _take(txn: Any) -> tuple[str, str]:
            snap = ref.get(transaction=txn)
            if not getattr(snap, "exists", False):
                return "missing", ""
            pending = snap.to_dict() or {}
            # Another person's sign-in is left exactly as it was: deleting it
            # would let anyone who saw a state burn somebody else's sign-in.
            if str(pending.get("person") or "") != person:
                return "foreign", ""
            txn.delete(ref)
            started = _aware(pending.get("created_at"))
            if started is None or now - started > PENDING_TTL:
                return "expired", ""
            return "ok", str(pending.get("verifier") or "")

        outcome, verifier = _take(transaction)
        if outcome == "foreign":
            raise SignInRefused("this sign-in was started by someone else; start your own")
        if outcome != "ok" or not verifier:
            raise ValidationFailed(
                "this sign-in has expired or was already used; press Sign in with ChatGPT "
                "to start again")
        return verifier

    def finish(self, person: str, *, state: str, pasted: str) -> dict[str, Any]:
        code, pasted_state = parse_callback(pasted)
        if pasted_state is not None and pasted_state != state:
            raise ValidationFailed(
                "that address belongs to a different sign-in than the one this page "
                "started; press Sign in with ChatGPT and paste the address it ends on")
        verifier = self._claim(person, state)
        answer = self._tokens.redeem(code=code, verifier=verifier)
        refresh_token = answer.get("refresh_token") if isinstance(answer, dict) else None
        if not isinstance(refresh_token, str) or not refresh_token:
            raise SignInFailed(
                "ChatGPT's sign-in returned no refresh token, so nothing was stored")
        tenant_id = tenant_id_for_user(person)
        secret_id = personal_secret_id(person)
        try:
            self._writer.create_secret(tenant_id, secret_id)
            version = self._writer.add_version(secret_id, stored_payload(refresh_token))
        except UpstreamUnavailable:
            raise UpstreamUnavailable(
                "the ChatGPT sign-in could not be stored in Secret Manager; sign in again") from None
        log.info("stored a ChatGPT sign-in tenant=%s version=%s", tenant_id, version)
        self._keys().record(tenant_id=tenant_id, provider=PROVIDER, scope=USER, by=person,
                            version=version, via_link=False)
        return self.status(person)
