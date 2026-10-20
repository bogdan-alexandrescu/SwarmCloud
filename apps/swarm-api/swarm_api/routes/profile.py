"""Settings › Profile: the read and the save (docs/design/user-profile.md §4.2).

    GET /v1/profile               who you are, your workspaces and role in each,
                                  your connections, your bell settings and your
                                  Claude accounts, in one read
    PUT /v1/profile/preferences   replace your display name, default workspace
                                  and bell settings, at the revision you read

FOR PEOPLE. A caller `Authenticator._special` names -- the schedule tick, the
rollup sweeper, a listed service account, any service identity -- has no
profile and gets 403.

NEITHER ROUTE NAMES A PERSON, and none ever will (Q8). The caller is always
the verified identity. An unknown query parameter such as `?user=` is a 422,
not ignored, so a client that believes it is reading someone else's profile is
told it is not; the save's body forbids every field it does not name. There is
no admin variant.

The sections are `swarm_api.profile`'s; the document and its save are
`swarm_api.preferences`'.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from ..auth import AuthContext, Authenticator
from ..brokerclient import AccountPool
from ..deps import AppContext, current_auth, get_context
from ..errors import Forbidden, ValidationFailed
from ..preferences import Preferences, build_preferences
from ..profile import read_profile, saved_profile
from ..schemas import StrictModel
from .accounts import account_pool

router = APIRouter(prefix="/v1/profile", tags=["profile"])

#: The `detail.reason` of each refusal. The codes are the established
#: `forbidden` and `validation_failed`; `swarm_api.refusals`' guard holds new
#: codes to a switch, and neither refusal may be switched off.
PEOPLE_ONLY = "profile_people_only"
NOT_YOUR_WORKSPACE = "not_your_workspace"


def preferences_store(
    request: Request, ctx: AppContext = Depends(get_context)
) -> Preferences:
    """`user_preferences/`, built once per app and injectable in tests.

    On `app.state`, as `routes.accounts.account_pool` is, so its caches are
    per app, `create_app()` reads no key and opens nothing, and a test needs no
    credential."""
    prefs = getattr(request.app.state, "preferences", None)
    if prefs is None:
        # The Authenticator's own store when it has one: one cache per app,
        # so a save's forget() reaches the copy authentication reads and the
        # next request acts in the new default (#1653). Else, as before, a
        # store of its own (no child key: every read is PreferencesUnavailable).
        prefs = getattr(ctx.authenticator, "preferences", None)
        if prefs is None:
            prefs = build_preferences(ctx.db, ctx.settings)
        request.app.state.preferences = prefs
    return prefs


def person(request: Request, auth: AuthContext = Depends(current_auth)) -> AuthContext:
    """The verified caller, when it is a person, on a request that names nobody."""
    if Authenticator._special(auth):
        raise Forbidden(
            "Settings › Profile is for people; this identity has no profile.",
            detail={"reason": PEOPLE_ONLY},
        )
    if request.query_params:
        # Never ignored: `?user=` must not look as if it selected someone.
        raise ValidationFailed(
            "The profile routes take no query parameters: the profile is always "
            "the signed-in person's own.",
            detail={"parameters": sorted(set(request.query_params.keys()))},
        )
    return auth


class NotificationPreferences(StrictModel):
    #: Topics from `preferences.TOPICS`; any of them, needs-you ones included (Q3).
    muted: list[str] = Field(max_length=32)
    #: One of `preferences.REMINDERS`.
    reminder: str = Field(max_length=32)


class PreferencesWrite(StrictModel):
    """The whole preference set; every field is required, so a save replaces
    rather than merges and a missing field is a 422, not a silent reset."""

    display_name: str | None = Field(max_length=256)
    default_workspace: str | None = Field(max_length=128)
    notifications: NotificationPreferences
    #: The `revision` the last read returned: a stale one is a 409.
    revision: int


@router.get("")
def get_profile(
    auth: AuthContext = Depends(person),
    ctx: AppContext = Depends(get_context),
    prefs: Preferences = Depends(preferences_store),
    pool: AccountPool = Depends(account_pool),
) -> dict[str, Any]:
    return read_profile(ctx, auth, prefs, pool)


@router.put("/preferences")
def put_preferences(
    body: PreferencesWrite,
    auth: AuthContext = Depends(person),
    ctx: AppContext = Depends(get_context),
    prefs: Preferences = Depends(preferences_store),
) -> dict[str, Any]:
    """Replace the caller's preferences. A default workspace is one of THIS
    request's `tenant_choices` or the save is refused before anything is
    read or written, and the message never says whether the workspace exists."""
    if body.default_workspace is not None and body.default_workspace not in {
        tenant_id for tenant_id, _ in auth.tenant_choices
    }:
        raise ValidationFailed(
            "default_workspace is not one of your workspaces. Choose one you can "
            "act in, or null for the computed default.",
            detail={"reason": NOT_YOUR_WORKSPACE},
        )
    stored = prefs.save(
        auth.email,
        display_name=body.display_name,
        default_workspace=body.default_workspace,
        muted=body.notifications.muted,
        reminder=body.notifications.reminder,
        revision=body.revision,
    )
    return saved_profile(ctx, auth, stored)
