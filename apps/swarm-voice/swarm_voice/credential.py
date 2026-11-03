"""The OpenAI credential a GPT-Live session runs on (docs/design/voice-it.md §3.6).

THE OWNER'S CHOICE (BGD, 2026-11-02): the ChatGPT plan account is reserved for
Voice it through a separate "Voice it pool" in the quota broker, and swarm-voice
leases an account from that pool for each session ("pool only, wait for it").
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


class Bearer:
    """An access token whose repr and str never show it, and the ChatGPT plan's
    account id it belongs to (sent as `ChatGPT-Account-Id`, the header the quota
    broker's openai_plan sends with the same kind of token), when the broker gave one."""

    __slots__ = ("_value", "account_id")

    def __init__(self, value: str, account_id: str | None = None) -> None:
        self._value = value
        self.account_id = account_id or None

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
