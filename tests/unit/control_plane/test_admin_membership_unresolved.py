"""A Cloud Identity failure on an ADMIN group is not "you are not an admin".

THE DEFECT:

    `_from_claims` asked one question -- `groups_for(email, tenant_groups +
    admin_groups)` -- and `groups_for` applies the PRIORITY rule: a lookup that
    failed below a confirmed match cannot change which tenant is picked, so it is
    logged and swallowed. That rule is right, and tests/unit/control_plane/
    test_group_resolution.py pins it.

    Admin is not a priority question. `is_admin` is `any(g in admin_set for g in
    member_groups)` over the WHOLE list, and admin groups were concatenated LAST,
    so they were exactly the ones whose failures the priority rule discarded. A
    caller confirmed in `eng` whose `swarm-admins` check failed came back as
    `("eng@saga.xyz",)` -- silence that `any()` reads as absence -- and every
    admin route answered

        403  "admin group membership is required for this operation"

    to someone holding it. That sentence sends an operator to check the group,
    the bindings and ADMIN_GROUPS, none of which are broken, while the real cause
    is a directory blip that fixes itself.

The fix asks the two questions separately and carries the third state --
`admin_unresolved` -- to `require_admin`, which answers 503 instead of 403. The
direction matters: this never grants admin, it only stops the platform asserting
something it does not know.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import GroupLookupError
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import ADMIN_GROUP, ENG_GROUP, api_settings, auth_header

ADMIN_ROUTE = "/v1/admin/tenants"


class FlakyGroups:
    """Membership from a fixed map, with a chosen set of groups that never answer.

    Mirrors `CloudIdentityGroups.groups_for`: a failure is swallowed when it
    cannot change the FIRST match, and raised when it can. Keeping that rule here
    is the point -- the bug was not in the rule, it was in asking the admin
    question through it.
    """

    def __init__(self, mapping: dict[str, tuple[str, ...]], failing: set[str]) -> None:
        self._mapping = {k.lower(): tuple(v) for k, v in mapping.items()}
        self._failing = {g.lower() for g in failing}
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def groups_for(self, member_email: str, candidate_groups: tuple[str, ...]) -> tuple[str, ...]:
        self.calls.append((member_email, tuple(candidate_groups)))
        mine = {g.lower() for g in self._mapping.get(member_email.lower(), ())}
        found: list[str] = []
        first_match: int | None = None
        first_failure: int | None = None
        for index, group in enumerate(candidate_groups):
            if group.lower() in self._failing:
                if first_failure is None:
                    first_failure = index
                continue
            if group.lower() in mine:
                found.append(group)
                if first_match is None:
                    first_match = index
        if first_failure is not None and (first_match is None or first_failure < first_match):
            raise GroupLookupError(f"cloud identity did not answer for index {first_failure}")
        return tuple(found)


def _client(db, tokens, groups) -> TestClient:
    ctx = build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=groups,
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
    )
    return TestClient(create_app(ctx), raise_server_exceptions=False)


@pytest.fixture
def admin_map() -> dict[str, tuple[str, ...]]:
    """`root` is an admin AND in eng; `alice` is in eng only."""
    return {
        "root@saga.xyz": (ADMIN_GROUP, ENG_GROUP),
        "alice@saga.xyz": (ENG_GROUP,),
    }


def test_an_unanswered_admin_lookup_is_503_not_403(db, tokens, admin_map) -> None:
    """The regression. root IS an admin; only the lookup failed.

    Before the fix this was a 403 saying admin membership is required, because
    `swarm-admins` sat below a confirmed `eng` and its failure was discarded by
    the tenant priority rule.
    """
    client = _client(db, tokens, FlakyGroups(admin_map, failing={ADMIN_GROUP}))

    response = client.get(ADMIN_ROUTE, headers=auth_header("root"))
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == "upstream_unavailable"
    assert "could not be resolved" in body["message"], body
    assert "is required for this operation" not in body["message"], (
        "a failed lookup must not be phrased as a statement about the caller"
    )


def test_a_genuine_non_admin_is_still_a_403(db, tokens, admin_map) -> None:
    """Separating the cases must not turn every refusal into a retry.

    alice is simply not in the admin group and nothing failed; that is an answer
    and it keeps its 403.
    """
    client = _client(db, tokens, FlakyGroups(admin_map, failing=set()))

    response = client.get(ADMIN_ROUTE, headers=auth_header("alice"))
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"


def test_a_confirmed_admin_is_unaffected_by_an_unrelated_failure(db, tokens, admin_map) -> None:
    """A tenant group that did not answer below a confirmed match is still swallowed.

    The original rule survives: one flaky group must not take down the API when
    it changes no answer. Here `research` fails, root is confirmed in both
    `swarm-admins` and `eng`, and the admin surface stays open.
    """
    client = _client(db, tokens, FlakyGroups(admin_map, failing={"research@saga.xyz"}))

    response = client.get(ADMIN_ROUTE, headers=auth_header("root"))
    assert response.status_code == 200, response.text


def test_a_non_admin_can_still_use_the_ordinary_api_during_an_admin_blip(
    db, tokens, admin_map
) -> None:
    """The cost of failing closed has to stay on the admin surface.

    `is_admin` is also read to WIDEN ordinary reads (platform-wide task counts,
    other tenants' pools). An unresolved admin there means the narrow view, which
    is correct and useful -- 503-ing every request in the deployment because an
    admin group is unreachable would be a far larger outage than the one being
    fixed.
    """
    client = _client(db, tokens, FlakyGroups(admin_map, failing={ADMIN_GROUP}))

    response = client.get("/v1/tenants/me", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    principal = response.json()["principal"]
    assert principal["is_admin"] is False
    # ... and the screen says WHY it is false.
    assert principal["is_admin_unresolved"] is True


def test_the_admin_users_escape_hatch_does_not_depend_on_a_group_lookup(
    db, tokens, admin_map
) -> None:
    """ADMIN_USERS exists for deployments where the Groups API cannot be read.

    ApiSettings.admin_users is there because this platform's service account may
    not be able to read groups AT ALL. Making it wait on a group lookup would
    break it in precisely the situation it was added for, so a named admin is
    never asked about.
    """
    groups = FlakyGroups(admin_map, failing={ADMIN_GROUP})
    ctx = build_context(
        settings=api_settings(admin_users=("alice@saga.xyz",)),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=groups,
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
    )
    client = TestClient(create_app(ctx), raise_server_exceptions=False)

    response = client.get(ADMIN_ROUTE, headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    assert not any(
        ADMIN_GROUP in candidates for email, candidates in groups.calls
        if email.lower() == "alice@saga.xyz"
    ), "a named admin must not be gated on the lookup the hatch exists to bypass"


def test_an_admin_group_that_is_also_a_tenant_group_is_still_asked_about(
    db, tokens
) -> None:
    """The case that hid best.

    When the same group is registered as both, the tenant pass answers it at a
    priority where a failure is discarded. Concatenating the lists deduplicated
    it away, so there was no second chance to notice.
    """
    settings = api_settings(
        tenant_groups=(ENG_GROUP, ADMIN_GROUP),
        admin_groups=(ADMIN_GROUP,),
    )
    groups = FlakyGroups({"root@saga.xyz": (ADMIN_GROUP, ENG_GROUP)}, failing={ADMIN_GROUP})
    ctx = build_context(
        settings=settings,
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=groups,
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
    )
    client = TestClient(create_app(ctx), raise_server_exceptions=False)

    response = client.get(ADMIN_ROUTE, headers=auth_header("root"))
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "upstream_unavailable"
