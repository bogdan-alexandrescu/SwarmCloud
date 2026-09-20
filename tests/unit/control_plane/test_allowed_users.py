"""Who is admitted at all, when there is no organisation to belong to.

Authorisation is by hosted domain, which is exactly right for a company:
`allowed_domains = ["saga.xyz"]` means the people in that company. It collapses
for everyone else. A single developer on a personal GCP project has no domain
of their own, so the only value that would admit them is `["gmail.com"]` --
which authorises every Google account on earth to run agents on their billing
account.

That is the difference between SwarmCloud being deployable outside a Google
Workspace or not, so the safe configuration has to be expressible. These pin
that `allowed_users` is additive, is not a second authentication path, and
cannot be used to smuggle in an identity the token did not carry.
"""

from __future__ import annotations

import pytest

from swarm_api.auth import AuthError, Authenticator, Forbidden, Unauthenticated


class _Iap:
    configured = True

    def __init__(self, claims):
        self._claims = claims

    def verify(self, assertion):
        return dict(self._claims)


class _Verifier:
    def verify(self, token):
        raise AuthError("the bearer path should not have been used")


class _Groups:
    def groups_for(self, email, candidates):
        return ()


class _Settings:
    class core:
        allowed_domains = ("saga.xyz",)

    tenant_groups: tuple = ()
    admin_groups: tuple = ()
    admin_users: tuple = ()
    allowed_users: tuple = ()


def _auth(settings, email):
    return Authenticator(
        settings,
        _Verifier(),
        _Groups(),
        iap=_Iap({"email": email, "sub": "accounts.google.com:1"}),
    )


def test_a_domain_caller_still_works_when_no_users_are_listed():
    """The existing behaviour, unchanged. This is the regression guard."""
    ctx = _auth(_Settings(), "person@saga.xyz").authenticate(None, "assertion")
    assert ctx.tenant_id == "u-person"


def test_an_outsider_is_still_forbidden_by_default():
    with pytest.raises(Forbidden):
        _auth(_Settings(), "someone@elsewhere.com").authenticate(None, "assertion")


def test_a_named_address_is_admitted_although_its_domain_is_not():
    """The solo case: one developer, a gmail address, no organisation."""
    class _Solo(_Settings):
        class core:
            allowed_domains = ()

        allowed_users = ("alice@gmail.com",)

    ctx = _auth(_Solo(), "alice@gmail.com").authenticate(None, "assertion")
    assert ctx.tenant_id == "u-alice"


def test_listing_one_gmail_address_does_not_admit_every_gmail_address():
    """The whole point. The alternative configuration -- allowed_domains =
    ["gmail.com"] -- would admit this caller, and everyone else alive."""
    class _Solo(_Settings):
        class core:
            allowed_domains = ()

        allowed_users = ("alice@gmail.com",)

    with pytest.raises(Forbidden):
        _auth(_Solo(), "mallory@gmail.com").authenticate(None, "assertion")


def test_it_is_additive_never_subtractive():
    """A company plus one named outsider: both must work.

    The local part is kept short deliberately. A longer one is hashed rather
    than truncated -- `swarm-agent-worker-u-contractor` is 31 characters and a
    service account name may be 30 -- which is correct behaviour and would make
    this test about tenant-id derivation instead of about authorisation.
    """
    class _Both(_Settings):
        allowed_users = ("ext@agency.example",)

    assert _auth(_Both(), "person@saga.xyz").authenticate(None, "a").tenant_id == "u-person"
    assert _auth(_Both(), "ext@agency.example").authenticate(None, "a").tenant_id == "u-ext"


def test_matching_is_case_insensitive():
    class _Upper(_Settings):
        class core:
            allowed_domains = ()

        allowed_users = ("ALICE@GMAIL.COM",)

    assert _auth(_Upper(), "alice@gmail.com").authenticate(None, "a").tenant_id == "u-alice"


def test_the_address_comes_from_the_token_and_not_from_the_caller():
    """This admits identities; it must never be a way to CLAIM one. The address
    tested is the one the verified assertion carried, which is the same source
    the domain check reads."""
    class _Solo(_Settings):
        class core:
            allowed_domains = ()

        allowed_users = ("alice@gmail.com",)

    # The caller presents a different verified identity. Being on the list is
    # about who you ARE, and the token decides that.
    with pytest.raises(Forbidden):
        _auth(_Solo(), "bob@gmail.com").authenticate(None, "assertion")


def test_an_empty_list_changes_nothing_at_all():
    """Adding the setting must not alter any existing deployment."""
    class _Empty(_Settings):
        allowed_users = ()

    assert _auth(_Empty(), "person@saga.xyz").authenticate(None, "a").tenant_id == "u-person"
    with pytest.raises(Forbidden):
        _auth(_Empty(), "person@elsewhere.com").authenticate(None, "a")


def test_a_deployment_with_neither_domains_nor_users_admits_nobody():
    """"Not configured" must mean refuse, never accept anything -- the same
    property the IAP audience check has, for the same reason."""
    class _Nothing(_Settings):
        class core:
            allowed_domains = ()

        allowed_users = ()

    with pytest.raises((Forbidden, Unauthenticated)):
        _auth(_Nothing(), "anyone@anywhere.com").authenticate(None, "a")
