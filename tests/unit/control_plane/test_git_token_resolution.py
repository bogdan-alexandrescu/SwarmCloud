"""R2, the token resolution order the operator picked (docs/git-tokens.md §3.1,
docs/web-ui/mockups/PICKS.md, 2026-10-05): clone, push and merge use the
repository's token, else the tenant default; the dispatching user's token is
used only for the actions whose author shows on the pull request.

`resolve_r2` is pure -- records in, a decision out -- so the whole table is
checked here with no Firestore, no Secret Manager and no clock. It is not
wired into dispatch yet (lane GT5, contract request E).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from swarm_common.states import ParkReason

from swarm_api.gittokens import (
    GitTokenRecord,
    Scope,
    TokenState,
    record_for_slot,
    resolve_r2,
)

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
REPO = "repo_" + "a" * 16
OTHER_REPO = "repo_" + "b" * 16
ALICE = "alice@saga.xyz"
BOB = "bob@saga.xyz"


def rec(scope: Scope, *, tenant: str = "eng", repo_id: str | None = None,
        user: str | None = None, repo_ids: list[str] | None = None,
        state: TokenState = TokenState.ACTIVE,
        expires_at: datetime | None = None) -> GitTokenRecord:
    record = record_for_slot(
        tenant, scope, repo_id=repo_id, user=user, repo_ids=repo_ids,
        registered_by="root@saga.xyz", now=NOW - timedelta(days=1),
    )
    record.state = state
    record.expires_at = expires_at
    return record


TENANT = rec(Scope.TENANT)
REPO_TOKEN = rec(Scope.REPOSITORY, repo_id=REPO)
ALICE_TOKEN = rec(Scope.USER, user=ALICE)


def test_repository_token_wins_over_the_tenant_default() -> None:
    got = resolve_r2([TENANT, REPO_TOKEN], tenant_id="eng", repo_id=REPO, user=ALICE, now=NOW)
    assert got.credential is REPO_TOKEN
    assert got.park_reason is None


def test_tenant_default_when_the_repository_has_no_token() -> None:
    got = resolve_r2([TENANT, REPO_TOKEN], tenant_id="eng", repo_id=OTHER_REPO,
                     user=ALICE, now=NOW)
    assert got.credential is TENANT
    assert "repo: none registered" in got.tried


def test_user_token_never_clones_or_pushes() -> None:
    """Under R2 a user token alone is not a credential: the task parks."""
    got = resolve_r2([ALICE_TOKEN], tenant_id="eng", repo_id=REPO, user=ALICE, now=NOW)
    assert got.credential is None
    assert got.park_reason is ParkReason.CREDENTIAL_MISSING
    assert got.for_action("clone") is None
    assert got.for_action("push") is None
    assert got.for_action("merge") is None


def test_user_token_is_the_author_of_visible_actions() -> None:
    got = resolve_r2([TENANT, ALICE_TOKEN], tenant_id="eng", repo_id=REPO, user=ALICE, now=NOW)
    assert got.credential is TENANT
    assert got.author is ALICE_TOKEN
    assert got.for_action("open_pull_request") is ALICE_TOKEN
    assert got.for_action("comment") is ALICE_TOKEN
    # The merge resolves like a push under R2, never with a person's token (§3.3).
    assert got.for_action("merge") is TENANT
    assert got.for_action("push") is TENANT


def test_without_a_user_token_the_credential_is_the_author() -> None:
    got = resolve_r2([TENANT, ALICE_TOKEN], tenant_id="eng", repo_id=REPO, user=BOB, now=NOW)
    assert got.author is TENANT
    assert "user: none registered" in got.tried


def test_a_user_token_narrowed_to_other_repositories_does_not_author() -> None:
    narrowed = rec(Scope.USER, user=ALICE, repo_ids=[OTHER_REPO])
    got = resolve_r2([TENANT, narrowed], tenant_id="eng", repo_id=REPO, user=ALICE, now=NOW)
    assert got.author is TENANT
    got = resolve_r2([TENANT, narrowed], tenant_id="eng", repo_id=OTHER_REPO, user=ALICE, now=NOW)
    assert got.author is narrowed


def test_user_match_ignores_case() -> None:
    got = resolve_r2([TENANT, ALICE_TOKEN], tenant_id="eng", repo_id=REPO,
                     user="Alice@Saga.xyz", now=NOW)
    assert got.author is ALICE_TOKEN


@pytest.mark.parametrize("state", [TokenState.REVOKED, TokenState.EXPIRED])
def test_revoked_or_expired_repository_token_falls_through(state: TokenState) -> None:
    dead = rec(Scope.REPOSITORY, repo_id=REPO, state=state)
    got = resolve_r2([TENANT, dead], tenant_id="eng", repo_id=REPO, user=None, now=NOW)
    assert got.credential is TENANT
    assert f"repo: {state.value}" in got.tried


def test_an_expiry_in_the_past_is_expired_whatever_the_state_says() -> None:
    lapsed = rec(Scope.REPOSITORY, repo_id=REPO, expires_at=datetime(2026, 9, 30, tzinfo=timezone.utc))
    got = resolve_r2([lapsed], tenant_id="eng", repo_id=REPO, user=None, now=NOW)
    assert got.credential is None
    assert got.park_reason is ParkReason.CREDENTIAL_MISSING
    assert got.tried == ("repo: expired 2026-09-30", "tenant: none registered")


def test_unverified_is_used() -> None:
    """Not probed yet is not a refusal (§3.2 uses `unknown` the same way)."""
    fresh = rec(Scope.TENANT, state=TokenState.UNVERIFIED)
    got = resolve_r2([fresh], tenant_id="eng", repo_id=REPO, user=None, now=NOW)
    assert got.credential is fresh


def test_nothing_registered_parks_credential_missing() -> None:
    got = resolve_r2([], tenant_id="eng", repo_id=REPO, user=ALICE, now=NOW)
    assert got.credential is None and got.author is None
    assert got.park_reason is ParkReason.CREDENTIAL_MISSING
    assert got.tried == ("repo: none registered", "tenant: none registered",
                         "user: none registered")


def test_another_tenants_records_are_never_used() -> None:
    """Isolation in the resolver itself, not only in the query that feeds it."""
    theirs = [rec(Scope.TENANT, tenant="research"),
              rec(Scope.REPOSITORY, tenant="research", repo_id=REPO),
              rec(Scope.USER, tenant="research", user=ALICE)]
    got = resolve_r2(theirs, tenant_id="eng", repo_id=REPO, user=ALICE, now=NOW)
    assert got.credential is None and got.author is None
    assert got.park_reason is ParkReason.CREDENTIAL_MISSING


def test_the_decision_names_secrets_and_never_values() -> None:
    got = resolve_r2([TENANT, REPO_TOKEN, ALICE_TOKEN], tenant_id="eng", repo_id=REPO,
                     user=ALICE, now=NOW)
    assert got.to_api() == {
        "credential": {"token_id": REPO_TOKEN.token_id, "scope": "repository",
                       "secret_name": REPO_TOKEN.secret_name},
        "author": {"token_id": ALICE_TOKEN.token_id, "scope": "user",
                   "secret_name": ALICE_TOKEN.secret_name},
        "park_reason": None,
        "tried": [],
        "order": "R2",
    }
