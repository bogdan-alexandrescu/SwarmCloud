"""The OpenAI credential a GPT-Live session runs on (docs/design/voice-it.md §3.6).

THE OWNER'S CHOICE (BGD, 2026-11-02): an account is reserved for Voice it
through a separate "Voice it pool" in the quota broker, and swarm-voice leases
an account from that pool for each session ("pool only, wait for it"). Since 4
November 2026 that account is an OpenAI developer API key (`KEY_KIND`): OpenAI
answered the ChatGPT plan's token 403 on /v1/live/sessions.
The person's own ChatGPT sign-in is no longer read by anything here: there is
no Secret Manager client, no refresh token and no per-person secret in this
package. The leased credential is accountlease.PoolCredential, which the broker
mints and renews; this module keeps only the interface live.py runs against.

THE RULES THIS MODULE HOLDS:

* An access token is never logged, never put in an exception, never returned:
  errors carry an HTTP status only (an error body may quote the request), and
  `Bearer`'s repr is masked.
* `Credential` is what a Live session needs: a bearer valid now, and a way to
  say OpenAI refused the last one.
"""

from __future__ import annotations

from typing import Protocol


class CredentialError(RuntimeError):
    """The credential could not be leased, renewed or used. The message names a status, never a value."""


#: The credential kind the quota broker names for an OpenAI developer API key
#: (quota_broker/openai_api_key.py, CREDENTIAL_KIND). The only kind a Voice it
#: lease hands out: OpenAI refused the ChatGPT plan's token for GPT-Live on
#: 4 November 2026 (403 on /v1/live/sessions; docs/design/voice-it.md §3).
KEY_KIND = "api_key"


class Bearer:
    """A credential whose repr and str never show it.

    An OpenAI developer API key (`kind == KEY_KIND`) is presented as
    `Authorization` ALONE: `account_id` is dropped even when one is given, so
    no `ChatGPT-Account-Id` -- the header naming a ChatGPT plan's account --
    is ever sent beside a key. Any other bearer carries the account id the
    broker gave, as before."""

    __slots__ = ("_value", "account_id", "kind")

    def __init__(self, value: str, account_id: str | None = None, *, kind: str | None = None) -> None:
        self._value = value
        self.kind = kind or None
        self.account_id = None if self.kind == KEY_KIND else (account_id or None)

    def reveal(self) -> str:
        return self._value

    def headers(self) -> dict[str, str]:
        """The request headers that present this bearer to OpenAI."""
        out = {"Authorization": f"Bearer {self._value}"}
        if self.account_id:
            out["ChatGPT-Account-Id"] = self.account_id
        return out

    def __repr__(self) -> str:
        return "Bearer(<redacted>)"

    __str__ = __repr__


class Credential(Protocol):
    """What a Live session needs from a credential."""

    async def bearer(self) -> Bearer:
        """A bearer that is valid now, renewed first if it is about to go stale."""
        ...

    async def refused(self) -> None:
        """OpenAI refused the last bearer: the next `bearer()` must not reuse it."""
        ...
