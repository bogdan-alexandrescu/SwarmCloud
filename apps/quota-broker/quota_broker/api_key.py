"""A developer API key of either vendor: the pool's second account kind.

docs/design/llm-providers.md §3 and §4 (the owner's ten answers of 9 November
2026, §9). `openai_api_key` was the first key kind and served Voice it alone;
this module generalises it to both vendors and to the workspace pool:

* TWO PROVIDERS, `anthropic-api` and `openai-api`, each its own `provider`
  name rather than a `kind` field: every filter in the broker already keys on
  `provider`, so a key is never handed to a caller that asked for a plan, and
  no stored document is rewritten. `VENDOR_OF` is the one table that groups a
  plan and a key of one vendor.
* VALIDATED BEFORE IT IS STORED (§4.1): the shape here, never quoting the
  value, then one read-only call every key can make (the vendor's list-models
  route). A 401 or 403 refuses the add; a 429 adds the key as limited.
* ONE SECRET, `swarm-account-<tenant>--key-<label>` (`accounts.key_secret_name`):
  the plan's naming family with a `key-` mark, and no `-refresh` half, since a
  key does not rotate.

The key's value is never logged, never put in an exception or an answer, and
is written to Secret Manager alone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from . import openai_api_key
from .oauth import CredentialError

#: The Anthropic developer key kind (§3, new). The worker gets it as
#: `ANTHROPIC_API_KEY` (lane LP-W1).
ANTHROPIC_API = "anthropic-api"
#: The OpenAI developer key kind, which existed for Voice it alone before §4.
OPENAI_API = openai_api_key.PROVIDER
#: Both key kinds, in the console's vendor order.
PROVIDERS: tuple[str, ...] = (ANTHROPIC_API, OPENAI_API)

#: The vendor of every account provider (§3): a plan and a key of one vendor
#: share a row group on Capacity › LLM Providers, and the fallback walks kinds
#: within one vendor, never across vendors (§3.1).
VENDOR_OF: dict[str, str] = {
    "anthropic": "anthropic",
    ANTHROPIC_API: "anthropic",
    "openai": "openai",
    OPENAI_API: "openai",
}

#: What `{secret}` holds, per provider, so a reader can tell a key from a
#: plan's JSON. The OpenAI value is `openai_api_key.KIND`, unchanged, so a
#: dedicated Voice it key stored before this module reads as it always did.
KIND_OF: dict[str, str] = {
    ANTHROPIC_API: "anthropic_api_key",
    OPENAI_API: openai_api_key.KIND,
}

#: Anthropic's secret keys begin with `_ANTHROPIC_PREFIX` and an api version. The bounds are
#: `openai_api_key`'s: generous for every shape seen, tight enough that a
#: pasted JSON blob or a sentence is refused.
_ANTHROPIC_PREFIX = "sk-" + "ant-"
_MIN_LENGTH = 20
_MAX_LENGTH = 512

#: Each vendor's list-models route, the one read-only call a key must be able
#: to make (§4.1). A key with no credit still passes it (§5.2), which is why
#: the exhausted probe (a later stage) is a completion and this is not.
MODELS_URL: dict[str, str] = {
    ANTHROPIC_API: "https://api.anthropic.com/v1/models",
    OPENAI_API: "https://api.openai.com/v1/models",
}
#: Anthropic's API version header, required on every call.
ANTHROPIC_VERSION = "2023-06-01"

#: The answer to a validation call.
CHECK_VALID = "valid"
CHECK_LIMITED = "limited"
CHECK_REFUSED = "refused"

#: A 429 with no `retry-after` holds the key this long (§5.2: "a minute when
#: none is sent").
DEFAULT_RETRY_AFTER_SECONDS = 60


def is_key(provider: str | None) -> bool:
    """Whether `provider` names a developer API key rather than a plan."""
    return provider in PROVIDERS


def vendor_of(provider: str) -> str:
    """The vendor of an account provider; `CredentialError` for one this
    broker does not know."""
    try:
        return VENDOR_OF[provider]
    except KeyError:
        raise CredentialError(f"no vendor is known for the provider {provider!r}") from None


def parse_api_key(provider: str, payload: str) -> str:
    """The key as pasted, trimmed, or `CredentialError` with a sentence that
    never quotes it."""
    if provider == OPENAI_API:
        return openai_api_key.parse_api_key(payload)
    if provider != ANTHROPIC_API:
        raise CredentialError(f"{provider!r} is not an API key kind; a key is one of {', '.join(PROVIDERS)}")
    key = (payload or "").strip()
    if not key.startswith(_ANTHROPIC_PREFIX):
        raise CredentialError(
            "an Anthropic API key begins with 'sk-ant-'; this is not one (a Claude "
            "sign-in is added as a plan, not as a key)"
        )
    if not _MIN_LENGTH <= len(key) <= _MAX_LENGTH:
        raise CredentialError(
            f"an Anthropic API key is {_MIN_LENGTH} to {_MAX_LENGTH} characters; this one is not"
        )
    if any(c.isspace() for c in key) or not key.isascii():
        raise CredentialError("an Anthropic API key is one word of plain characters; this one is not")
    return key


def serialise(provider: str, key: str) -> str:
    """What a key account's secret holds."""
    return json.dumps({"kind": KIND_OF[provider], "api_key": key})


def read_key(provider: str, stored: str) -> str:
    """The key from what `serialise` wrote; `CredentialError` when it is not
    this provider's key secret."""
    if provider == OPENAI_API:
        return openai_api_key.read_key(stored)
    try:
        data = json.loads(stored)
    except ValueError:
        raise CredentialError("the stored secret is not an API key account's") from None
    key = (data.get("api_key")
           if isinstance(data, dict) and data.get("kind") == KIND_OF.get(provider) else None)
    if not isinstance(key, str) or not key:
        raise CredentialError("the stored secret is not an API key account's")
    return key


@dataclass(frozen=True)
class KeyCheck:
    """What the vendor said to the key's one read-only call."""

    outcome: str
    #: Seconds the vendor asked to wait, on a 429; None otherwise.
    retry_after: int | None = None


class KeyCheckUnavailable(RuntimeError):
    """The vendor could not be asked (a network failure or a 5xx): the key is
    neither accepted nor refused, and nothing is stored. Carries the status
    or the error's type, never the key."""


def check_from_status(status: int, retry_after: str | None = None) -> KeyCheck:
    """The outcome a vendor's HTTP status means for a key being added."""
    if 200 <= status < 300:
        return KeyCheck(CHECK_VALID)
    if status in (401, 403):
        return KeyCheck(CHECK_REFUSED)
    if status == 429:
        seconds = DEFAULT_RETRY_AFTER_SECONDS
        try:
            asked = int(float(retry_after)) if retry_after is not None else None
        except ValueError:
            asked = None
        if asked is not None and asked > 0:
            seconds = asked
        return KeyCheck(CHECK_LIMITED, retry_after=seconds)
    raise KeyCheckUnavailable(f"the provider answered HTTP {status}")


def request_headers(provider: str, key: str) -> dict[str, str]:
    """The headers that present `key` to its vendor."""
    if provider == ANTHROPIC_API:
        return {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    return {"Authorization": f"Bearer {key}"}


class HttpKeyValidator:
    """The real validation call. Injected (`app.state.key_validator`) so
    tests never make a network call."""

    def __init__(self, *, timeout: float = 10.0,
                 opener: Callable[..., Any] | None = None) -> None:
        self._timeout = timeout
        self._opener = opener

    def check(self, provider: str, key: str) -> KeyCheck:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            MODELS_URL[provider], headers=request_headers(provider, key), method="GET"
        )
        opener = self._opener or urllib.request.urlopen
        try:
            with opener(request, timeout=self._timeout) as response:
                return check_from_status(int(getattr(response, "status", 200)))
        except urllib.error.HTTPError as exc:
            return check_from_status(exc.code, exc.headers.get("retry-after") if exc.headers else None)
        except (urllib.error.URLError, OSError) as exc:
            # The type alone: a URLError's text never holds the key, but the
            # rule is simpler kept absolute.
            raise KeyCheckUnavailable(f"the provider could not be reached: {type(exc).__name__}") from None


__all__ = [
    "ANTHROPIC_API",
    "ANTHROPIC_VERSION",
    "CHECK_LIMITED",
    "CHECK_REFUSED",
    "CHECK_VALID",
    "DEFAULT_RETRY_AFTER_SECONDS",
    "HttpKeyValidator",
    "KIND_OF",
    "KeyCheck",
    "KeyCheckUnavailable",
    "MODELS_URL",
    "OPENAI_API",
    "PROVIDERS",
    "VENDOR_OF",
    "check_from_status",
    "is_key",
    "parse_api_key",
    "read_key",
    "request_headers",
    "serialise",
    "vendor_of",
]
