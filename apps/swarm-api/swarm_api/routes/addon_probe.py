"""The brave-search add-on's probe: one real search, as swarm-api, for an admin
(lane DAYONE-SEARCH-PROBE, part of #2688).

    POST /v1/admin/setup/addons/brave-search/probe
      -> {"ok": bool, "reason": str, "status": int | null, "results": int}

`swarm addon enable brave-search` (and `doctor`) asks this route whether the
add-on works, instead of reading the key's IAM policy: a policy can say
swarm-api may read the key while the key is wrong, revoked or over its plan,
and only a search says the tool answers. The per-attempt route
(`POST /v1/attempts/{attempt_id}/search`) cannot be used for it: it needs an
attempt's proof, and the installer has no attempt.

WHO MAY CALL. A platform admin, like every Admin › Setup route (`admin_auth`
on the route, so the admin surface's sweeps see it gated); no service identity.

WHAT IT DOES. One fixed, harmless query (`PROBE_QUERY`, one result) through
the provider and key source the per-attempt route builds
(`swarm_api.search.PROVIDERS`, `SecretManagerKey`), with swarm-api's own key.
Nothing is written: no tenant's daily cap is spent and no audit row is made,
because no tenant searched.

WHAT IT ANSWERS. Only the four fields above, always 200, so a 404 means one
thing to the engine: a swarm-api older than this route. Never the key, a
header, the provider's body or a result's text: `results` is a count, and
`reason` is built from a kind and a status, then scrubbed of the key anyway.
When swarm-api may not read the key (its accessor grant on
`swarm-search-api-key` is an IAM change held for the owner), the answer is
`ok: false` with a reason naming that access, not a 500.

INVARIANTS. No infrastructure demand (1), no capacity or lease (2, 3, 5), no
tenant's data (9). The caller names nothing: no query, provider, URL or key
(10).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Request

from ..deps import AppContext, admin_auth, get_context
from ..search import (
    KEY_DENIED,
    KEY_MISSING,
    PROVIDERS,
    KeySource,
    KeyUnavailable,
    ProviderError,
    SearchProvider,
    SecretManagerKey,
    scrub,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin/setup/addons", tags=["setup"])

#: The one query the probe sends: a word anyone could search for, naming no
#: tenant, person or repository.
PROBE_QUERY = "weather"
PROBE_COUNT = 1


@dataclass
class SearchProbe:
    """The probe's logic; `app.state.search_probe` holds a fake in a test."""

    enabled: bool
    secret: str
    provider: SearchProvider
    key: KeySource

    @classmethod
    def from_settings(cls, settings: Any) -> "SearchProbe":
        return cls(
            enabled=bool(getattr(settings, "search_tool_enabled", False)),
            secret=settings.search_api_key_secret,
            provider=PROVIDERS[settings.search_provider](),
            key=SecretManagerKey(settings.project_id, settings.search_api_key_secret),
        )

    def run(self) -> dict[str, Any]:
        if not self.enabled:
            return _answer(False, "swarm-api's search tool is off (SEARCH_TOOL_ENABLED is not true on "
                                  "its running revision)")
        try:
            key = self.key.read()
        except KeyUnavailable as exc:
            log.warning("search_probe key_unavailable kind=%s", exc.kind)
            if exc.kind == KEY_DENIED:
                return _answer(False, f"swarm-api's service account has no read access to {self.secret}: "
                                      "it needs roles/secretmanager.secretAccessor on that secret")
            if exc.kind == KEY_MISSING:
                return _answer(False, f"{self.secret} has no enabled version: store the Brave key through "
                                      "the add-on's link")
            return _answer(False, str(exc))
        try:
            results = self.provider.search(PROBE_QUERY, PROBE_COUNT, key=key)
        except ProviderError as exc:
            log.warning("search_probe provider_error provider=%s kind=%s status=%s",
                        self.provider.name, exc.kind, exc.status)
            where = f" (HTTP {exc.status})" if exc.status is not None else ""
            reason = scrub(f"the search provider ({self.provider.name}) answered {exc.kind}{where}", key)
            return _answer(False, reason, status=exc.status)
        log.info("search_probe ok provider=%s results=%d", self.provider.name, len(results))
        return _answer(True, f"one search through {self.provider.name} answered {len(results)} result(s)",
                       status=200, results=len(results))


def _answer(ok: bool, reason: str, *, status: int | None = None, results: int = 0) -> dict[str, Any]:
    return {"ok": ok, "reason": reason, "status": status, "results": results}


def search_probe(request: Request, ctx: AppContext = Depends(get_context)) -> SearchProbe:
    found = getattr(request.app.state, "search_probe", None)
    return found if found is not None else SearchProbe.from_settings(ctx.settings)


@router.post("/brave-search/probe", dependencies=[Depends(admin_auth)])
def brave_search_probe(probe: SearchProbe = Depends(search_probe)) -> dict[str, Any]:
    return probe.run()
