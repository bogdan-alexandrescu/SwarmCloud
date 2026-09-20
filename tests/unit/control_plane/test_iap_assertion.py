"""The IAP assertion path: what it accepts, and what it must refuse.

Written after the fact, which is the point. The verifier shipped untested and
the failure it was meant to fix -- a signed-in user getting 401 from the web UI
-- was found by the person using it, not by me. These pin the properties that
would be expensive to get wrong.
"""

from __future__ import annotations

import pytest

from swarm_api.auth import AuthError, Authenticator, IapAssertionVerifier, Unauthenticated

AUD_A = "/projects/209012342332/global/backendServices/817602226733443034"
AUD_B = "/projects/209012342332/global/backendServices/6904312892305383900"


def test_no_audience_means_the_path_is_off_not_unpinned():
    """The single most important property here.

    google-auth SKIPS the `aud` check when no audience is passed. An IAP
    assertion is issued to anyone who can reach ANY IAP-protected resource
    anywhere, so an unpinned verifier would accept a token minted for a
    completely unrelated backend in a stranger's project.

    "Not configured" therefore has to mean "refuse", never "accept anything".
    """
    v = IapAssertionVerifier(())
    assert v.configured is False
    with pytest.raises(AuthError):
        v.verify("any.assertion.at.all")


def test_empty_strings_do_not_count_as_a_configured_audience():
    """An env var read as "" must not switch the check on with a blank value."""
    assert IapAssertionVerifier(("", "")).configured is False
    assert IapAssertionVerifier(("", AUD_A)).configured is True


def test_both_backend_audiences_are_accepted():
    """Two backends front this platform -- the API and the UI -- and a request
    may arrive through either, so an assertion minted for either is valid."""
    v = IapAssertionVerifier((AUD_A, AUD_B))
    assert v.configured is True
    assert v._audiences == (AUD_A, AUD_B)


class _Verifier:
    def verify(self, token):
        raise AuthError("bearer should not have been consulted")


class _Groups:
    def groups_for(self, email, candidates):
        return ()


class _Iap:
    configured = True

    def __init__(self, claims=None, error=None):
        self._claims = claims
        self._error = error
        self.calls = 0

    def verify(self, assertion):
        self.calls += 1
        if self._error:
            raise self._error
        return dict(self._claims)


class _Settings:
    class core:
        allowed_domains = ("saga.xyz",)

    tenant_groups: tuple = ()
    admin_groups: tuple = ()
    admin_users: tuple = ()


def test_a_bad_assertion_does_not_fall_through_to_the_bearer_path():
    """A caller claiming an identity it cannot prove must be rejected outright.

    Falling back would let a forged assertion be masked by a valid bearer, so an
    attacker who had either could present both and be judged on the better one.
    """
    iap = _Iap(error=AuthError("assertion verification failed"))
    auth = Authenticator(_Settings(), _Verifier(), _Groups(), iap=iap)

    with pytest.raises(Unauthenticated):
        auth.authenticate("Bearer some-otherwise-valid-token", "bad.assertion")
    assert iap.calls == 1


def test_the_bearer_path_still_works_when_no_assertion_is_present():
    """A direct caller from inside the VPC has no IAP header and must still work."""
    iap = _Iap(claims={"email": "x@saga.xyz"})
    auth = Authenticator(_Settings(), _Verifier(), _Groups(), iap=iap)

    # The bearer verifier raises, proving the bearer path was the one taken.
    with pytest.raises(Unauthenticated):
        auth.authenticate("Bearer whatever", None)
    assert iap.calls == 0, "the IAP verifier was consulted without an assertion"


def test_an_assertion_is_ignored_when_the_verifier_is_not_configured():
    """Belt and braces: with IAP off, an attacker-supplied header changes nothing."""
    iap = _Iap(claims={"email": "attacker@saga.xyz"})
    iap.configured = False
    auth = Authenticator(_Settings(), _Verifier(), _Groups(), iap=iap)

    with pytest.raises(Unauthenticated):
        auth.authenticate(None, "attacker.supplied.assertion")
    assert iap.calls == 0


def test_a_verified_assertion_resolves_a_tenant_through_the_shared_path():
    """Domain enforcement and tenant resolution must not differ by how the
    caller arrived -- two copies would be two tenant boundaries."""
    iap = _Iap(claims={"email": "person@saga.xyz", "sub": "accounts.google.com:1"})
    auth = Authenticator(_Settings(), _Verifier(), _Groups(), iap=iap)

    ctx = auth.authenticate(None, "good.assertion")
    assert ctx.principal.email == "person@saga.xyz"
    assert ctx.tenant_id == "u-person"


def test_an_assertion_from_outside_the_allowed_domain_is_forbidden_not_unauthenticated():
    """Correct identity, wrong organisation: 403, and IAP does not change that."""
    from swarm_api.auth import Forbidden

    iap = _Iap(claims={"email": "person@elsewhere.com", "sub": "s"})
    auth = Authenticator(_Settings(), _Verifier(), _Groups(), iap=iap)

    with pytest.raises(Forbidden):
        auth.authenticate(None, "good.assertion")


# -- admin by email, the escape hatch -------------------------------------

class _AdminUserSettings(_Settings):
    admin_users: tuple = ("bogdan@saga.xyz",)


class _MemberGroups:
    """A group resolver that DOES answer, unlike production today."""

    def __init__(self, groups):
        self._groups = groups

    def groups_for(self, email, candidates):
        return self._groups


def _ctx(settings, groups=()):
    iap = _Iap(claims={"email": "bogdan@saga.xyz", "sub": "accounts.google.com:1"})
    auth = Authenticator(settings, _Verifier(), _MemberGroups(groups), iap=iap)
    return auth.authenticate(None, "good.assertion")


def test_nobody_is_admin_when_no_groups_resolve_and_no_users_are_named():
    """The state the platform was actually in, and why this exists.

    is_admin is computed from Cloud Identity group membership. swarm-api
    cannot read groups -- the Groups API does not authorize through GCP IAM
    and a service account is not a Workspace principal -- so member_groups is
    empty, admin_set is empty, and every operator screen 403s for everyone.
    """
    assert _ctx(_Settings()).is_admin is False


def test_a_named_email_is_admin_without_any_group():
    assert _ctx(_AdminUserSettings()).is_admin is True


def test_the_email_comes_from_the_assertion_not_from_the_caller():
    """The escape hatch must not be a way to claim admin.

    The address tested is the one the verified IAP assertion carried, which is
    the same source a group lookup would have started from.
    """
    class _Other(_Settings):
        admin_users: tuple = ("someone-else@saga.xyz",)

    assert _ctx(_Other()).is_admin is False


def test_matching_is_case_insensitive():
    class _Upper(_Settings):
        admin_users: tuple = ("BOGDAN@SAGA.XYZ",)

    assert _ctx(_Upper()).is_admin is True


def test_group_membership_still_grants_admin_on_its_own():
    """The escape hatch is additive. It must not replace the group path."""
    class _ByGroup(_Settings):
        admin_groups: tuple = ("swarm-admins@saga.xyz",)

    assert _ctx(_ByGroup(), groups=("swarm-admins@saga.xyz",)).is_admin is True
