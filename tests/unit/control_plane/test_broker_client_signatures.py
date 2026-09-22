"""BrokerClient's own methods, driven through the real `_call`.

WHY THIS FILE EXISTS. `BrokerClient.begin_sign_in` and `finish_sign_in` called
`self._call(..., body={...})`. `_call` has never had a `body` parameter -- it
takes `payload` -- so both raised

    TypeError: BrokerClient._call() got an unexpected keyword argument 'body'

on every invocation. Adding an account through the web UI or the API returned
HTTP 500, always, and had done since the methods were written.

Nothing caught it, for a reason worth naming: every existing test fakes the
account pool at a LAYER ABOVE this one. `AccountPool` is a Protocol, tests
substitute their own object for it, and so the one signature that mattered --
BrokerClient's own call into its own helper -- was never executed. Both ends
existed; the join between them did not. That is the fifth instance of that
shape found in this repository in three days.

The mistake was duplicated because `AccountPool(Protocol)` carried full method
BODIES for these two members, calling a `self._call` the Protocol does not
declare, while every sibling member was `...`. BrokerClient's versions were
pasted from there.

So these tests drive the REAL BrokerClient through a fake TRANSPORT -- the
lowest injectable seam -- which means `_call` actually runs. A fake at any
higher layer reproduces the blind spot instead of covering it.
"""

from __future__ import annotations

import inspect
import json

import pytest

from swarm_api.brokerclient import AccountPool, BrokerClient


class _Recorder:
    """A Transport that records the request and returns a canned response."""

    def __init__(self, status=200, body=None):
        self.calls: list[dict] = []
        self._status = status
        self._body = body if body is not None else {}

    def __call__(self, method, url, *, headers, body=None, timeout=30.0):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "body": json.loads(body.decode()) if body else None,
            }
        )
        return self._status, self._body


class _Token:
    def token(self, audience: str) -> str:
        return "t"


def _client(recorder):
    return BrokerClient(
        base_url="https://broker.invalid",
        audience="https://broker.invalid",
        transport=recorder,
        tokens=_Token(),
    )


# -- the two methods that were broken ---------------------------------------


def test_begin_sign_in_reaches_the_transport():
    """Before the fix this raised TypeError inside _call and never got here."""
    rec = _Recorder(body={"authorize_url": "https://claude.ai/x", "state": "s" * 40})
    out = _client(rec).begin_sign_in(
        owner_tenant="eng", label="bogdan-primary", provider="anthropic", lend_to=[]
    )

    assert len(rec.calls) == 1, "the request never reached the transport"
    call = rec.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/v1/accounts/authorize")
    assert call["body"] == {
        "owner_tenant": "eng",
        "label": "bogdan-primary",
        "provider": "anthropic",
        "lend_to": [],
    }
    assert out["state"] == "s" * 40


def test_finish_sign_in_reaches_the_transport():
    rec = _Recorder(status=201, body={"account_id": "eng:bogdan-primary"})
    out = _client(rec).finish_sign_in(state="s" * 40, code="code#hash#state")

    assert len(rec.calls) == 1
    assert rec.calls[0]["url"].endswith("/v1/accounts/exchange")
    assert rec.calls[0]["body"] == {"state": "s" * 40, "code": "code#hash#state"}
    assert out["account_id"] == "eng:bogdan-primary"


def test_the_verifier_is_never_sent_or_returned():
    """PKCE only proves anything while the verifier stays in the broker."""
    rec = _Recorder(body={"authorize_url": "https://claude.ai/x", "state": "s" * 40})
    out = _client(rec).begin_sign_in(
        owner_tenant="eng", label="l", provider="anthropic", lend_to=[]
    )
    assert "verifier" not in json.dumps(rec.calls[0]["body"]).lower()
    assert "verifier" not in json.dumps(out).lower()


# -- the class of bug, not just this instance -------------------------------


def test_every_broker_call_site_uses_a_real_parameter_of_call():
    """The general form. One wrong keyword shipped in two places; this refuses
    the next one regardless of which method grows it."""
    import re

    src = inspect.getsource(BrokerClient)
    accepted = set(inspect.signature(BrokerClient._call).parameters)

    # Top-level keywords of the _call(...) invocation only. A naive scan also
    # catches `safe=''` from a nested quote(...) call in the path argument.
    used: set[str] = set()
    for chunk in re.findall(r"self\._call\((.*?)\n        \)", src, re.S):
        depth = 0
        for line in chunk.split("\n"):
            stripped = line.strip()
            if depth == 0:
                m = re.match(r"(\w+)=", stripped)
                if m:
                    used.add(m.group(1))
            depth += line.count("(") + line.count("{") + line.count("[")
            depth -= line.count(")") + line.count("}") + line.count("]")
    # `method` and `path` are positional at every call site.
    unknown = {k for k in used if k not in accepted}
    assert not unknown, (
        f"_call() does not accept {sorted(unknown)}; it accepts {sorted(accepted - {'self'})}. "
        "A keyword that is not a parameter raises TypeError at call time, which is a 500 "
        "for whoever asked."
    )


def test_the_account_pool_protocol_declares_and_does_not_implement():
    """The Protocol carried real method bodies for these two members while
    every sibling was `...`, and BrokerClient's broken versions were pasted
    from it. A Protocol that implements is a Protocol that can be copied
    wrong."""
    for name in ("begin_sign_in", "finish_sign_in", "refresh", "remove", "set_state"):
        fn = getattr(AccountPool, name, None)
        if fn is None:
            continue
        body = inspect.getsource(fn)
        assert "_call" not in body, (
            f"AccountPool.{name} contains an implementation calling _call. "
            "AccountPool is a Protocol; it declares the interface and an implementer "
            "need not have a _call at all."
        )
