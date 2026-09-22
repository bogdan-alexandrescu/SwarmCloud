"""Everything a broker route reads off `app.state` must be put there.

`finish_account_authorization` (main.py) reads
`request.app.state.token_endpoint` to redeem an authorization code. Nothing
ever set it. So every POST /v1/accounts/exchange raised

    AttributeError: 'State' object has no attribute 'token_endpoint'

answered 500, and swarm-api relayed it as 503 "the account pool answered HTTP
500". Registering a subscription account has never once completed on this
platform.

WHY IT SURVIVED. HttpTokenEndpoint's own docstring reads "Injected so tests
never make a network call" — it was designed to be injected and then never
was. The REFRESH path works, because it builds its own HttpTokenEndpoint
inside CredentialRefresher; only the REDEEM path went through app.state, and
redeem is the one step that cannot be exercised without a real authorization
code. Both ends existed; the assignment between them did not.

The general test below is the point. One missing assignment cost two rounds of
a person signing in to Claude and pasting a code, and a second one would cost
the same. It walks every `app.state.<name>` READ in the module and asserts a
matching WRITE exists.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# create_app refuses to build without PROJECT_ID, deliberately: without it the
# worker service-account pattern cannot be pinned to this project and any
# project's similarly-named account would authenticate as a tenant.
@pytest.fixture(autouse=True)
def _project(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", "swarm-test-project")
    monkeypatch.setenv("REGION", "us-central1")
    monkeypatch.setenv("ENVIRONMENT", "dev")


MAIN = Path(__file__).resolve().parents[3] / "apps" / "quota-broker" / "quota_broker" / "main.py"


def _reads_and_writes() -> tuple[set[str], set[str]]:
    """Every `app.state.<name>` occurrence, split by whether it is assigned.

    Scanned by POSITION, not by a lookahead. `(\w+)(?!\s*=)` looks right and is
    not: the group backtracks one character to satisfy the lookahead, so
    `app.state.broker` yields `broke`. The first version of this test reported
    thirteen missing attributes, every one of them a real name minus its last
    letter -- a test that fails for its own reasons teaches nothing about the
    code.
    """
    src = MAIN.read_text()
    reads: set[str] = set()
    writes: set[str] = set()
    for m in re.finditer(r"(?:request\.)?app\.state\.(\w+)", src):
        name = m.group(1)
        rest = src[m.end():]
        # An assignment, but not a comparison (`==`) or an augmented read.
        if re.match(r"\s*=(?!=)", rest):
            writes.add(name)
        else:
            reads.add(name)
    for pat in (
        r'getattr\(\s*(?:request\.)?app\.state\s*,\s*["\'](\w+)["\']',
    ):
        reads |= set(re.findall(pat, src))
    return reads, writes


def test_token_endpoint_is_assigned():
    """The specific bug."""
    _, writes = _reads_and_writes()
    assert "token_endpoint" in writes, (
        "no code assigns app.state.token_endpoint, and finish_account_authorization "
        "reads it — every /v1/accounts/exchange raises AttributeError and answers 500"
    )


def test_every_app_state_attribute_read_is_also_written():
    """The class of bug. A read with no write is a 500 waiting for the one
    request that reaches it."""
    reads, writes = _reads_and_writes()
    missing = sorted(reads - writes)
    assert not missing, (
        f"these app.state attributes are read but never assigned: {missing}. "
        "Each is an AttributeError on the first request that reaches that line."
    )


def test_the_app_builds_with_a_token_endpoint_present():
    """Construct the real app and look at the attribute, rather than trusting
    the source scan. No network: every collaborator is injected."""
    from quota_broker.main import create_app

    class _Broker:
        db = None

    class _Endpoint:
        def redeem(self, **kw):  # pragma: no cover - never called here
            raise AssertionError("the real endpoint must not be reached in a test")

    app = create_app(
        broker=_Broker(),
        subscription_tenants=lambda: [],
        token_endpoint=_Endpoint(),
    )
    assert getattr(app.state, "token_endpoint", None) is not None
    assert app.state.token_endpoint.__class__.__name__ == "_Endpoint", (
        "the injected endpoint was ignored; a test could then reach the real one"
    )


def test_it_defaults_to_the_real_endpoint_when_not_injected():
    """Production passes nothing, so the default has to be the real one —
    injectability must not mean 'absent unless a test supplies it', which is
    how this broke in the first place."""
    from quota_broker.main import create_app

    class _Broker:
        db = None

    app = create_app(broker=_Broker(), subscription_tenants=lambda: [])
    assert app.state.token_endpoint.__class__.__name__ == "HttpTokenEndpoint"
