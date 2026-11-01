"""A laptop signed in before issue 2942's fix still holds application-default
credentials that name an unrelated project as their quota project, and
google-auth sends it as `x-goog-user-project` on every call. Every place the
installer builds its own credentials drops it (`api_client.application_default`)
and sends the install's own project only where that step means to.

The typed approvals that end a run on one wrong answer ask again instead
(`preflight.confirm`), and nothing is recorded or changed until one matches.

No cloud credentials: the credentials are built here, and the transport under
the real AuthorizedSession answers every request itself.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

import pytest

from swarm_install import api_client, doctor
from swarm_install.exitcodes import ExitCode
from swarm_install.steps import bootstrap, foundations, identity, preflight

from .test_foundations import COMPUTE_SA, FakeProjectApi, _project_and_budget, _recorded
from .test_preflight import NUMBER, PROJECT, Answers, FakeGcp, ctx_for

QUOTA = "x-goog-user-project"
DEPLOYER = f"swarm-tf-deployer@{PROJECT}.iam.gserviceaccount.com"


class _Transport:
    """Every request the real AuthorizedSession (or google-auth's Request)
    sends, kept with its headers and answered 200 with `body`."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]) -> None:
        import requests
        import requests.adapters

        self.sent: list[dict[str, str]] = []
        sent = self.sent

        class Adapter(requests.adapters.BaseAdapter):
            def send(self, request, **kwargs):  # noqa: ANN001, ANN003
                sent.append(dict(request.headers))
                response = requests.Response()
                response.status_code, response.request, response.url = 200, request, request.url
                response._content = json.dumps(body).encode()
                return response

            def close(self) -> None:
                pass

        adapter = Adapter()
        monkeypatch.setattr(requests.Session, "get_adapter", lambda self, url: adapter)

    def quota_projects(self) -> list[str | None]:
        return [headers.get(QUOTA) for headers in self.sent]


@pytest.fixture
def laptop(monkeypatch: pytest.MonkeyPatch) -> str:
    """The old sign-in: user credentials whose quota project is the laptop's
    gcloud project, which has nothing to do with the install."""
    import google.auth
    from google.oauth2.credentials import Credentials

    project = "laptop-default-" + secrets.token_hex(3)

    def default(scopes: Any = None, **_: Any) -> tuple[Any, str]:
        return Credentials(token=secrets.token_hex(16), quota_project_id=project, scopes=scopes), project

    monkeypatch.setattr(google.auth, "default", default)
    return project


_ANSWER = {"token": "id-" + secrets.token_hex(4), "accessToken": "at-" + secrets.token_hex(4),
           "expireTime": "2099-01-01T00:00:00Z"}


def test_identity_session_drops_it_and_bills_cloud_identity_to_the_install(laptop, monkeypatch) -> None:
    sent = _Transport(monkeypatch, _ANSWER)
    gcp = identity.IdentityGcp()
    gcp.session.request("GET", "https://admin.googleapis.com/admin/directory/v1/customers/my_customer")
    gcp._billed(PROJECT, "GET", "https://cloudidentity.googleapis.com/v1/groups")
    assert sent.quota_projects() == [None, PROJECT]


def test_bootstrap_impersonation_drops_it(laptop, monkeypatch) -> None:
    sent = _Transport(monkeypatch, _ANSWER)
    deployer = bootstrap._impersonated(DEPLOYER)
    deployer.session.request("GET", f"https://cloudresourcemanager.googleapis.com/v3/projects/{PROJECT}")
    # The generateAccessToken call (the person's credentials), then the call as the deployer.
    assert sent.quota_projects() == [None, None]

    sent.sent.clear()
    assert bootstrap.can_mint_token(DEPLOYER) is True
    assert sent.quota_projects() == [None]


def test_api_client_id_token_minting_drops_it(laptop, monkeypatch) -> None:
    sent = _Transport(monkeypatch, _ANSWER)
    token = api_client.IamCredentials().generate_id_token(DEPLOYER, "aud", include_email=True)
    assert token == _ANSWER["token"]
    assert sent.quota_projects() == [None]


def test_gcp_session_goes_through_the_one_helper(laptop, monkeypatch) -> None:
    sent = _Transport(monkeypatch, {"billingAccounts": []})
    preflight.Gcp().billing_accounts()
    assert sent.quota_projects() == [None]
    assert getattr(api_client.application_default(), "quota_project_id", None) is None


class _Refreshing:
    """Credentials doctor refreshes and reads; they record the quota project
    they carried when refreshed."""

    def __init__(self, quota_project: str | None) -> None:
        self.quota_project_id = quota_project
        self.token: str | None = None
        self.account = "admin@example.com"
        self.refreshed_with: list[str | None] = []

    def with_quota_project(self, quota_project: str | None) -> _Refreshing:
        return _Refreshing(quota_project)

    def refresh(self, request: Any) -> None:
        self.refreshed_with.append(self.quota_project_id)
        self.token = secrets.token_hex(8)


def test_doctor_access_token_and_principal_drop_it(monkeypatch) -> None:
    import google.auth

    laptop = "laptop-default-" + secrets.token_hex(3)
    made: list[_Refreshing] = []

    def default(scopes: Any = None, **_: Any) -> tuple[Any, str]:
        made.append(_Refreshing(laptop))
        return made[-1], laptop

    monkeypatch.setattr(google.auth, "default", default)
    monkeypatch.setattr(doctor, "on_google_runtime", lambda: False)
    original = _Refreshing.with_quota_project
    used: list[_Refreshing] = []

    def kept(self: _Refreshing, quota_project: str | None) -> _Refreshing:
        used.append(original(self, quota_project))
        return used[-1]

    monkeypatch.setattr(_Refreshing, "with_quota_project", kept)
    assert doctor.access_token()
    refreshed = [c for c in [*made, *used] if c.refreshed_with]
    assert [c.refreshed_with for c in refreshed] == [[None]]
    assert doctor.principal() == "user:admin@example.com"


# -- the typed approvals, asked again -----------------------------------------


def test_project_approval_asks_again_after_an_empty_line(tmp_path: Path) -> None:
    gcp = FakeGcp(exists=False, billed=False)
    api = FakeProjectApi(gcp)
    rig = _recorded(tmp_path)
    ask = Answers(foundations_project=["", "", PROJECT], foundations_budget=[""])
    result = rig.engine(_project_and_budget(gcp, ask, api)).run()
    assert result.exit_code == ExitCode.OK, result.message
    approvals = [q for s, q in ask.asked if s == "foundations.project"][1:]
    assert len(approvals) == 2 and "nothing was typed" in approvals[1]
    assert rig.store.load().outputs.get("budget_amount")


def test_project_approval_gives_up_after_its_tries_creating_nothing(tmp_path: Path) -> None:
    gcp = FakeGcp(exists=False, billed=False)
    api = FakeProjectApi(gcp)
    rig = _recorded(tmp_path)
    ask = Answers(foundations_project=["", *["yes"] * preflight.CONFIRM_TRIES])
    result = rig.engine(_project_and_budget(gcp, ask, api)).run()
    assert result.exit_code == ExitCode.FAIL
    assert "nothing was created or linked" in result.message
    assert api.calls == [] and gcp.mutations == []
    assert "budget_amount" not in rig.store.load().outputs


def test_remove_editor_approval_asks_again_after_an_empty_line() -> None:
    gcp = FakeGcp()
    gcp.iam["bindings"] = [{"role": "roles/editor", "members": [COMPUTE_SA, "user:owner@example.com"]}]
    ask = Answers(foundations_default_compute_editor=["", foundations.DefaultComputeEditorStep.CONFIRM])
    step = foundations.DefaultComputeEditorStep(lambda: gcp, ask)
    ctx = ctx_for(outputs={"project_number": NUMBER}, dry_run=False)
    step.apply(ctx, step.plan(ctx))
    assert gcp.iam["bindings"] == [{"role": "roles/editor", "members": ["user:owner@example.com"]}]
    assert len(ask.asked) == 2 and "nothing was typed" in ask.asked[1][1]


def test_remove_editor_approval_gives_up_changing_nothing() -> None:
    gcp = FakeGcp()
    gcp.iam["bindings"] = [{"role": "roles/editor", "members": [COMPUTE_SA]}]
    ask = Answers(foundations_default_compute_editor=["yes"] * preflight.CONFIRM_TRIES)
    step = foundations.DefaultComputeEditorStep(lambda: gcp, ask)
    ctx = ctx_for(outputs={"project_number": NUMBER}, dry_run=False)
    with pytest.raises(ValueError, match="nothing was changed"):
        step.apply(ctx, step.plan(ctx))
    assert gcp.mutations == []
    assert len(ask.asked) == preflight.CONFIRM_TRIES


@pytest.mark.parametrize("answer", ["delegation", "Delegation"])
def test_groups_reader_approval_asks_again_then_takes_delegation(answer: str) -> None:
    ask = Answers(identity_groups_reader_role=["", answer])
    step = identity.GroupsReaderRoleStep(lambda: FakeGcp(), ask)
    ctx = ctx_for(dry_run=False)
    step.apply(ctx, step.plan(ctx))
    assert ctx.outputs["groups_auth"] == api_client.DELEGATION
    assert len(ask.asked) == 2 and "nothing was typed" in ask.asked[1][1] and "delegation" in ask.asked[1][1]


def test_groups_reader_approval_gives_up_assigning_and_recording_nothing() -> None:
    ask = Answers(identity_groups_reader_role=["", "assign", "no"])
    gcp = FakeGcp()
    step = identity.GroupsReaderRoleStep(lambda: gcp, ask)
    ctx = ctx_for(dry_run=False)
    with pytest.raises(ValueError, match="nothing was assigned"):
        step.apply(ctx, step.plan(ctx))
    assert "groups_auth" not in ctx.outputs and gcp.mutations == []
    assert "'assign' is not ASSIGN" in ask.asked[2][1]


def test_confirm_returns_the_answer_that_matched() -> None:
    ctx = ctx_for()
    ask = Answers(preflight_region=["", "DELEGATION"])
    assert preflight.confirm(ask, ctx, "preflight.region", "Go?", "ASSIGN", also=("delegation",)) == "delegation"
    ask = Answers(preflight_region=["ASSIGN"])
    assert preflight.confirm(ask, ctx, "preflight.region", "Go?", "ASSIGN", also=("delegation",)) == "ASSIGN"

