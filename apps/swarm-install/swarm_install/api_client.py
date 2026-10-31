"""The engine's calls to swarm-api (plan §5.3, review-fix finding 2).

swarm-api's ingress is `internal-and-cloud-load-balancing`, so a laptop reaches
it only through the load balancer, behind IAP. IAP refuses a gcloud user token
(401, code 900, lesson 18), and the person running the install is not
necessarily a platform admin. So the engine calls swarm-api as the deployer
service account it already impersonates, `swarm-tf-deployer`:

1. `iamcredentials generateIdToken` for `swarm-tf-deployer@<P>` with
   `includeEmail = true` and the **API backend's IAP audience** (computed in
   infra pass 2 and recorded in the state as `api_iap_audience`);
2. that ID token, and nothing else, is the bearer on the call to
   `https://<H>/v1/...`. The person's own credentials only mint it; they are
   never sent to swarm-api.

IAP admits the account because TF-2 grants it `roles/iap.httpsResourceAccessor`
on the API backend only (`iam-binding.deployer-iap-api-accessor`); swarm-api
admits its verified email on exactly two routes and refuses it everywhere
else, so this client refuses every other route before it mints anything:

| Route | Who calls it |
|---|---|
| `POST /v1/admin/setup/github-app/hook` | the hand-off, after the App's self-check (swarm-api signs the App JWT; no key material crosses) |
| `POST /v1/admin/setup/health/groups-probe` | stage 8's probe, through swarm-api's own Groups identity in the mode it runs |

The minted token is a credential: it is never stored, logged, put in an event
or returned. It lives for one request.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

DEPLOYER = "swarm-tf-deployer"
IAM_API = "https://iamcredentials.googleapis.com/v1"

HOOK_ROUTE = "/v1/admin/setup/github-app/hook"
GROUPS_PROBE_ROUTE = "/v1/admin/setup/health/groups-probe"

#: The only (method, path) pairs swarm-api admits the deployer on.
ROUTES: frozenset[tuple[str, str]] = frozenset({("POST", HOOK_ROUTE), ("POST", GROUPS_PROBE_ROUTE)})

#: The group-read modes swarm-api runs in (`GROUPS_AUTH`, DAYONE-IDENTITY-1).
ADMIN_ROLE = "admin_role"
DELEGATION = "delegation"
GROUPS_MODES = (ADMIN_ROLE, DELEGATION)


def deployer_email(project: str) -> str:
    return f"{DEPLOYER}@{project}.iam.gserviceaccount.com"


class RouteRefused(ValueError):
    """The engine asked for a route swarm-api never admits its identity on."""


class ApiCallError(RuntimeError):
    """swarm-api (or IAP in front of it) answered with an error. The message
    carries the status and swarm-api's error code, never a header."""

    def __init__(self, status: int, code: str, path: str, *, message: str = "",
                 detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(f"{path} answered HTTP {status}" + (f" ({code})" if code else "")
                         + (f": {message}" if message else ""))
        self.status = status
        self.code = code
        self.path = path
        self.message = message
        self.detail: Mapping[str, Any] = detail or {}


class IdTokenMinter(Protocol):
    def generate_id_token(self, service_account: str, audience: str, *, include_email: bool) -> str: ...


class IamCredentials:
    """`generateIdToken` over REST with the person's application-default
    credentials (which hold `iam.serviceAccounts.getOpenIdToken` on the
    deployer through `iam-binding.installer-impersonates-deployer`)."""

    def __init__(self, session: Any = None) -> None:
        self._session = session

    @property
    def session(self) -> Any:
        if self._session is None:
            import google.auth  # lazy: no credentials needed to import the engine
            from google.auth.transport.requests import AuthorizedSession

            credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            self._session = AuthorizedSession(credentials)
        return self._session

    def generate_id_token(self, service_account: str, audience: str, *, include_email: bool) -> str:
        response = self.session.request(
            "POST",
            f"{IAM_API}/projects/-/serviceAccounts/{service_account}:generateIdToken",
            json={"audience": audience, "includeEmail": include_email},
            timeout=60,
        )
        if response.status_code >= 400:
            raise ApiCallError(response.status_code, "generateIdToken", f"generateIdToken for {service_account}")
        return str(response.json()["token"])


@dataclass(frozen=True)
class Response:
    status: int
    body: Mapping[str, Any]


#: `http(method, url, headers, body) -> Response`: a plain, unauthenticated
#: transport. The bearer is the minted ID token in `headers`, nothing else.
Http = Callable[[str, str, Mapping[str, str], Mapping[str, Any]], Response]


def requests_http(method: str, url: str, headers: Mapping[str, str], body: Mapping[str, Any]) -> Response:
    import requests  # a bare session: no ambient credentials are attached

    with requests.Session() as session:
        session.trust_env = False  # never a netrc or proxy credential from the environment
        found = session.request(method, url, headers=dict(headers), json=dict(body), timeout=60)
    try:
        payload = found.json() if found.content else {}
    except ValueError:
        payload = {}
    return Response(found.status_code, payload if isinstance(payload, dict) else {})


@dataclass(frozen=True)
class GroupResult:
    group: str
    state: str  # met | unmet | unknown
    reason: str = ""
    detail: str = ""


@dataclass(frozen=True)
class GroupsProbe:
    """swarm-api's answer: the mode it read groups in, and met/unmet per group
    (never a member list, review finding 12)."""

    mode: str
    results: tuple[GroupResult, ...] = field(default_factory=tuple)

    def of(self, group: str) -> GroupResult:
        for found in self.results:
            if found.group == group:
                return found
        return GroupResult(group, "unknown", "absent", f"swarm-api's answer did not name {group}")


def refusal(status: int, body: Any, path: str) -> ApiCallError:
    """swarm-api's `ApiError` handler serves the flat `{code, message, detail?}`
    (swarm_api/errors.py `to_payload`); a body nested under `error` (IAP, or a
    front end in between) is read too, so neither shape loses the code."""
    error: Any = body if isinstance(body, Mapping) else {}
    if "code" not in error and "error" in error:
        error = error["error"]
    if not isinstance(error, Mapping):
        return ApiCallError(status, str(error or ""), path)
    detail = error.get("detail")
    return ApiCallError(status, str(error.get("code") or ""), path, message=str(error.get("message") or ""),
                        detail=detail if isinstance(detail, Mapping) else None)


@dataclass
class ApiClient:
    project: str
    base_url: str
    audience: str
    minter: IdTokenMinter = field(default_factory=IamCredentials)
    http: Http = requests_http

    def _call(self, method: str, path: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        if (method, path) not in ROUTES:
            raise RouteRefused(
                f"{method} {path}: swarm-api admits {DEPLOYER} only on {sorted(p for _, p in ROUTES)}; "
                "the engine calls nothing else"
            )
        token = self.minter.generate_id_token(deployer_email(self.project), self.audience, include_email=True)
        found = self.http(method, self.base_url.rstrip("/") + path,
                          {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, body)
        if not 200 <= found.status < 300:
            raise refusal(found.status, found.body, path)
        return found.body

    def activate_hook(self) -> Mapping[str, Any]:
        """Turn the GitHub App's webhook on; swarm-api signs the App JWT itself."""
        return self._call("POST", HOOK_ROUTE, {"active": True})

    def groups_probe(self, probes: Sequence[Mapping[str, str]]) -> GroupsProbe:
        """`checkTransitiveMembership` for each `{group, member}`, run by swarm-api
        with its own Groups identity in the mode it runs (review finding 8)."""
        body = self._call("POST", GROUPS_PROBE_ROUTE, {"groups": [dict(p) for p in probes]})
        mode = str(body.get("mode") or "")
        results = tuple(
            GroupResult(
                group=str(r.get("group", "")),
                state=str(r.get("state", "unknown")),
                reason=str(r.get("reason", "") or ""),
                detail=str(r.get("detail", "") or ""),
            )
            for r in body.get("results", ()) or ()
            if isinstance(r, Mapping)
        )
        return GroupsProbe(mode=mode, results=results)


def for_install(ctx: Any, *, minter: IdTokenMinter | None = None, http: Http | None = None) -> ApiClient:
    """The client for this install: `https://<H>` and the API backend's IAP
    audience recorded by infra pass 2."""
    hostname = ctx.state.hostname or ctx.outputs.get("hostname")
    if not hostname:
        raise LookupError("no hostname is recorded (preflight.inputs asks it)")
    audience = ctx.outputs.get("api_iap_audience")
    if not audience:
        raise LookupError("the API backend's IAP audience is not recorded yet (infra pass 2, stage 7, records it)")
    return ApiClient(
        project=ctx.project,
        base_url=f"https://{hostname}",
        audience=str(audience),
        minter=minter if minter is not None else IamCredentials(),
        http=http if http is not None else requests_http,
    )
