"""`swarm_install.api_client`: the engine reaches swarm-api as the deployer,
with a Google-signed ID token for the API backend's IAP audience, on two routes
only (DAYONE-ENGINE-4; plan §5.3, review-fix finding 2; lesson 18).

No credentials: `generateIdToken` and swarm-api are fakes, and every token is
built at runtime.
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest

from swarm_install import api_client
from swarm_install.api_client import (
    GROUPS_PROBE_ROUTE,
    HOOK_ROUTE,
    ApiCallError,
    ApiClient,
    IamCredentials,
    Response,
    RouteRefused,
)

PROJECT = "p-test"
AUDIENCE = "/projects/123456789012/global/backendServices/987654321"


class FakeResponse:
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status_code = status
        self._body = body
        self.content = b"x"

    def json(self) -> dict[str, Any]:
        return self._body


class FakeAdcSession:
    """The person's application-default session: it carries THEIR token, and
    may only ever be used to mint the deployer's ID token."""

    def __init__(self) -> None:
        self.user_token = secrets.token_hex(16)
        self.minted = secrets.token_hex(24)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, json: dict[str, Any], timeout: int) -> FakeResponse:
        self.calls.append((method, url, json))
        return FakeResponse(200, {"token": self.minted})


class FakeHttp:
    def __init__(self, body: dict[str, Any] | None = None, status: int = 200) -> None:
        self.calls: list[tuple[str, str, dict[str, str], dict[str, Any]]] = []
        self.body = body or {}
        self.status = status

    def __call__(self, method: str, url: str, headers: Any, body: Any) -> Response:
        self.calls.append((method, url, dict(headers), dict(body)))
        return Response(self.status, self.body)


def _client(adc: FakeAdcSession, http: FakeHttp) -> ApiClient:
    return ApiClient(project=PROJECT, base_url="https://swarm.example.com", audience=AUDIENCE,
                     minter=IamCredentials(adc), http=http)


def test_id_token_minted_for_deployer_with_iap_audience() -> None:
    """The faked generateIdToken call names swarm-tf-deployer, the API
    backend's IAP audience and includeEmail; its token is the bearer."""
    adc, http = FakeAdcSession(), FakeHttp({"mode": "admin_role", "results": []})
    _client(adc, http).groups_probe([{"group": "eng@example.com", "member": "a@example.com"}])

    [(method, url, body)] = adc.calls
    assert method == "POST"
    assert url == ("https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
                   f"swarm-tf-deployer@{PROJECT}.iam.gserviceaccount.com:generateIdToken")
    assert body == {"audience": AUDIENCE, "includeEmail": True}
    [(_, called, headers, _)] = http.calls
    assert called == "https://swarm.example.com" + GROUPS_PROBE_ROUTE
    assert headers["Authorization"] == f"Bearer {adc.minted}"


def test_never_sends_a_gcloud_user_token() -> None:
    """Lesson 18: IAP refuses a gcloud user token (401, code 900). The
    person's own credentials mint the ID token and are never sent to swarm-api;
    the transport to swarm-api is a bare one, not the authorised session."""
    adc, http = FakeAdcSession(), FakeHttp({})
    _client(adc, http).activate_hook()

    [(_, _, headers, body)] = http.calls
    sent = repr(headers) + repr(body)
    assert adc.user_token not in sent
    assert set(headers) == {"Authorization", "Content-Type"}
    assert all(not url.startswith("https://swarm.example.com") for _, url, _ in adc.calls)
    # The default transport carries no ambient credential either.
    assert api_client.ApiClient.__dataclass_fields__["http"].default is api_client.requests_http


def test_only_hook_and_groups_probe_routes() -> None:
    """Review-fix finding 2: swarm-api admits the deployer on the hook and
    groups-probe routes only, so the client refuses any other before it mints."""
    assert api_client.ROUTES == {("POST", HOOK_ROUTE), ("POST", GROUPS_PROBE_ROUTE)}
    adc, http = FakeAdcSession(), FakeHttp({})
    client = _client(adc, http)
    for method, path in (("GET", "/v1/admin/setup"), ("POST", "/v1/admin/setup/links"),
                         ("GET", GROUPS_PROBE_ROUTE), ("POST", "/v1/agents")):
        with pytest.raises(RouteRefused):
            client._call(method, path, {})
    assert adc.calls == [] and http.calls == []

    client.activate_hook()
    client.groups_probe([])
    assert [c[1].removeprefix("https://swarm.example.com") for c in http.calls] == [HOOK_ROUTE, GROUPS_PROBE_ROUTE]


def test_error_names_status_and_code_never_the_token() -> None:
    adc, http = FakeAdcSession(), FakeHttp({"error": {"code": "forbidden"}}, status=403)
    with pytest.raises(ApiCallError) as raised:
        _client(adc, http).activate_hook()
    assert raised.value.status == 403 and raised.value.code == "forbidden"
    assert adc.minted not in str(raised.value)


def test_for_install_needs_the_recorded_audience() -> None:
    from types import SimpleNamespace

    ctx = SimpleNamespace(project=PROJECT, state=SimpleNamespace(hostname="swarm.example.com"), outputs={})
    with pytest.raises(LookupError, match="IAP audience"):
        api_client.for_install(ctx)
    ctx.outputs["api_iap_audience"] = AUDIENCE
    client = api_client.for_install(ctx, minter=IamCredentials(FakeAdcSession()), http=FakeHttp())
    assert client.base_url == "https://swarm.example.com" and client.audience == AUDIENCE


def test_the_doctor_reads_the_probe_through_results(monkeypatch) -> None:
    """Finding N6: the doctor asks with no pairs and reads swarm-api's
    `{mode, results}` answer through `ApiClient.groups_probe`, the shape
    stage 8 reads, not a `groups` key of its own."""
    from types import SimpleNamespace

    from swarm_install.verbs import doctor as doctor_verb

    http = FakeHttp({"mode": "admin_role", "results": [
        {"group": "eng@example.com", "state": "met"},
        {"group": "ops@example.com", "state": "unmet", "reason": "permission_denied"}]})
    monkeypatch.setattr(api_client, "for_install",
                        lambda ctx: _client(FakeAdcSession(), http))
    state = SimpleNamespace(project=PROJECT, hostname="swarm.example.com", outputs={})
    found = doctor_verb._InstallApi(state).groups_probe()
    assert found == [{"group": "eng@example.com", "state": "met", "reason": ""},
                     {"group": "ops@example.com", "state": "unmet", "reason": "permission_denied"}]
    [(_, _, _, body)] = http.calls
    assert body == {"groups": []}
