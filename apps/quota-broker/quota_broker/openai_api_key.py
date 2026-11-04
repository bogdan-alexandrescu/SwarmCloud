"""An OpenAI developer API key: the Voice it pool's Live-capable account kind.

WHY THIS KIND EXISTS (measured 4 November 2026, 02:00 UTC). swarm-voice leased
the ChatGPT plan account eng:chatgpt-1 from the Voice it pool and
`POST https://api.openai.com/v1/live/sessions` answered 403 Forbidden. The
plan's token is the Codex sign-in (`openai_plan.SCOPES`, `openid profile email
offline_access`), and OpenAI's own documentation says why: Sign in with
ChatGPT's identity scopes do not grant OpenAI API resources, and plan use is
limited to text Responses for selected partners
(https://developers.openai.com/siwc/quickstart). GPT-Live is a developer API
model billed per minute (https://developers.openai.com/api/docs/models/gpt-live-1),
so a Live session runs on a developer API key and on nothing else.

WHAT HOLDS FOR THIS KIND (the owner, "fix it", 4 November 2026):

* VOICE IT ONLY. Registered into the Voice it pool alone, never lent, never
  moved into the workspace pool, so `may_serve` refuses it to every tenant
  and no agent or workspace is ever handed it (`main.register_account`,
  `main.set_pools`).
* STORED AS EVERY CREDENTIAL IS: one Secret Manager secret, `{base}`, whose
  only accessor is the broker, which is also its only writer. No worker
  service account is bound to it, and there is no `{base}-refresh`: a key
  does not rotate, so the refresh sweep has nothing to exchange
  (`accounts.due_for_refresh` skips it).
* NO USAGE WINDOW. OpenAI publishes no plan window for a key (it is billed
  per minute), so the usage poller never asks and the account's windows read
  "not measured" (`Account.measured` is False), never 0.
* THE BEARER IS THE KEY ALONE: `Authorization: Bearer <key>`, and no
  `ChatGPT-Account-Id` (that header names a ChatGPT plan's account).

The key's value is never logged, never put in an exception and never
returned by any route but the Voice it lease, which hands it to swarm-voice's
process alone.
"""

from __future__ import annotations

import json

from .oauth import CredentialError

#: The account's `provider`. Its own name, not `openai`: a task leasing an
#: `openai` account (codex on a ChatGPT plan) filters on the provider, so a
#: key can never be chosen for one even before the pool rule refuses it.
PROVIDER = "openai-api"
#: What `{base}` holds, so a reader can tell a key from a plan's JSON.
KIND = "openai_api_key"
#: The credential kind the Voice it lease names, so swarm-voice sends the key
#: as a bare bearer.
CREDENTIAL_KIND = "api_key"

#: What a Voice it session needs from its account (`VoiceLease.capability`),
#: and the account kinds that have it. Only a developer API key may open a
#: GPT-Live session; a ChatGPT plan never can (OpenAI's 403, above).
CAPABILITY_LIVE = "live"
LIVE_PROVIDERS: tuple[str, ...] = (PROVIDER,)

#: OpenAI's secret keys begin `sk-` (project keys `sk-proj-`, service account
#: keys `sk-svcacct-`). Bounds generous enough for every shape seen, tight
#: enough that a pasted JSON blob or a sentence is refused.
_PREFIX = "sk-"
_MIN_LENGTH = 20
_MAX_LENGTH = 512


def parse_api_key(payload: str) -> str:
    """The key as pasted, trimmed, or `CredentialError` with a sentence that
    never quotes it."""
    key = (payload or "").strip()
    if not key.startswith(_PREFIX):
        raise CredentialError(
            "an OpenAI API key begins with 'sk-'; this is not one (a ChatGPT sign-in "
            "cannot open a Live session, so only a developer API key is accepted here)"
        )
    if not _MIN_LENGTH <= len(key) <= _MAX_LENGTH:
        raise CredentialError(
            f"an OpenAI API key is {_MIN_LENGTH} to {_MAX_LENGTH} characters; this one is not"
        )
    if any(c.isspace() for c in key) or not key.isascii():
        raise CredentialError("an OpenAI API key is one word of plain characters; this one is not")
    return key


def serialise(key: str) -> str:
    """What `{base}` holds for a key account."""
    return json.dumps({"kind": KIND, "api_key": key})


def read_key(stored: str) -> str:
    """The key from what `serialise` wrote; `CredentialError` when it is not
    a key account's secret."""
    try:
        data = json.loads(stored)
    except ValueError:
        raise CredentialError("the stored secret is not an OpenAI API key account's") from None
    key = data.get("api_key") if isinstance(data, dict) and data.get("kind") == KIND else None
    if not isinstance(key, str) or not key:
        raise CredentialError("the stored secret is not an OpenAI API key account's")
    return key


def is_live_capable(provider: str) -> bool:
    return provider in LIVE_PROVIDERS


__all__ = [
    "CAPABILITY_LIVE",
    "CREDENTIAL_KIND",
    "KIND",
    "LIVE_PROVIDERS",
    "PROVIDER",
    "is_live_capable",
    "parse_api_key",
    "read_key",
    "serialise",
]
