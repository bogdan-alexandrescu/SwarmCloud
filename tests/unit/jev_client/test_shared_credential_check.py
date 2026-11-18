"""One credential check for every Jev call (the owner, 4 November 2026).

The worker's publish-gate credential check -- `_credential_in` and its
helpers, the headerless-key and ciphertext detection among them -- lives in
the shared package `swarm_redaction.credentials`. The shared Jev client
(`swarm_jev.sanitize`) scans with it by default, so a Jev call swarm-api makes,
in a process that never imports the worker, withholds exactly what the
worker's own call withholds.

What this holds:

* a Jev call from a process without the worker (swarm-api's) does not send a
  headerless private key or a ciphertext blob, which no redaction rule names
  (the control);
* the worker's `lifecycle._credential_in` and the shared scan's check are the
  shared package's own objects, not copies;
* the order of checks before a send stays: the registered secrets, the
  credential check, the redaction rules.

Every fake credential is built at runtime from random bytes.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import sys

import pytest

#: Builds, at runtime, two strings no redaction rule names and the publish
#: gate refuses: a plaintext DER private key (a SEQUENCE opening with a key's
#: version INTEGER) and encrypted-key ciphertext (random bytes, a whole number
#: of cipher blocks), each base64 in an assignment. Random bytes now and then
#: hold a run some redaction rule matches, so each is drawn until none does.
_BLOBS = r'''
import base64, os
from swarm_redaction.rules import redact

def _draw(make):
    for _ in range(50):
        text = "blob = '" + base64.b64encode(make()).decode() + "'"
        if redact(text).count == 0:
            return text
    raise AssertionError("every random blob matched a redaction rule")

def _der_key():
    body = b"\x02\x01\x00" + os.urandom(300)
    return b"\x30\x82" + len(body).to_bytes(2, "big") + body

CIPHERTEXT = _draw(lambda: os.urandom(512))
DER_KEY = _draw(_der_key)
'''

#: swarm-api's process: the shared client, a backend and a ledger of its own,
#: and nothing of the worker. Prints what the transport was sent and what the
#: decision counted, then which worker modules were loaded (none).
_SWARM_API_CALL = _BLOBS + r'''
import json, sys
from swarm_jev.client import JudgeClient

class Backend:
    name, url, model, stringify_state, key = "typesafe", "http://127.0.0.1:1/v1/decide", "m", False, None

class Ledger:
    def admit(self, *, estimate):
        return None
    def record(self, row, *, succeeded=None):
        return False

sent = []
def transport(url, body, headers, timeout):
    sent.append(body.decode())
    return 200, json.dumps({"answers": {"q": {"noul": 0.9}},
                            "usage": {"input_tokens": 10}}).encode()

decision = JudgeClient(Backend(), Ledger(), transport=transport, sleep=lambda s: None,
                       log=lambda event, **f: None).decide(
    "J1", {"candidates": [{"i": 0, "path": "a.py", "excerpt": CIPHERTEXT},
                          {"i": 1, "path": "b.py", "excerpt": DER_KEY},
                          {"i": 2, "path": "c.py", "excerpt": "keep"}]},
    {"q": {"type": "noul", "instructions": "relevant?"}})
body = sent[0]
print(json.dumps({
    "outcome": decision.outcome,
    "dropped": decision.dropped,
    "ciphertext_sent": CIPHERTEXT.split("'")[1] in body,
    "der_key_sent": DER_KEY.split("'")[1] in body,
    "state": json.loads(body)["state"],
    "worker": sorted(m for m in sys.modules if m.split(".")[0] == "agent_worker"),
}))
'''


def _run(code: str) -> dict:
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True, timeout=120)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_swarm_api_jev_call_withholds_a_headerless_key_and_a_ciphertext_blob() -> None:
    """Red before the move: without the worker, the shared scan asked only the
    redaction rules, and both blobs were sent."""
    result = _run(_SWARM_API_CALL)
    assert result["worker"] == [], result["worker"]
    assert result["ciphertext_sent"] is False
    assert result["der_key_sent"] is False
    assert result["dropped"] == 2, result
    assert result["state"] == {"candidates": [
        {"i": 0, "path": "a.py"}, {"i": 1, "path": "b.py"},
        {"i": 2, "path": "c.py", "excerpt": "keep"}]}, result["state"]
    assert result["outcome"] == "ok", result


def test_the_shared_scan_flags_what_the_publish_gate_refuses() -> None:
    """The same strings, asked of the shared predicate and of the worker's
    `_credential_in` at its strictest tier, in the test's own process."""
    from swarm_jev import sanitize as shared_sanitize
    from swarm_redaction import credentials

    from agent_worker.lifecycle import _credential_in

    namespace: dict = {}
    exec(_BLOBS, namespace)  # noqa: S102 -- the fixture builder above, nothing else
    for text in (namespace["CIPHERTEXT"], namespace["DER_KEY"]):
        hit = _credential_in("", text)
        assert hit is not None and hit.rule == credentials.HEADERLESS_KEY_RULE, hit
        assert credentials.is_credential(text) is True
        assert shared_sanitize.is_flagged(text) is True
    assert credentials.is_credential("plain words") is False
    assert shared_sanitize.is_flagged("plain words") is False


def test_the_worker_check_is_the_shared_object() -> None:
    from swarm_redaction import credentials

    from agent_worker import lifecycle

    assert lifecycle._credential_in is credentials.credential_in
    assert lifecycle.CredentialHit is credentials.CredentialHit
    assert lifecycle.is_test_path is credentials.is_test_path
    assert lifecycle.HEADERLESS_KEY_RULE == credentials.HEADERLESS_KEY_RULE


def test_the_checks_run_in_order_secrets_then_credential_check_then_rules(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The order the worker applied before sending: a registered secret is
    asked first, then the credential check, then the redaction rules; each
    stops the scan when it hits."""
    from swarm_jev import sanitize as shared_sanitize

    asked: list[str] = []
    real_check, real_rules = shared_sanitize.is_credential, shared_sanitize._rules_redact

    def check(text: str) -> bool:
        asked.append("check")
        return real_check(text)

    def rules(text: str):
        asked.append("rules")
        return real_rules(text)

    monkeypatch.setattr(shared_sanitize, "is_credential", check)
    monkeypatch.setattr(shared_sanitize, "_rules_redact", rules)
    secret = secrets.token_hex(12)

    assert shared_sanitize.is_flagged("has " + secret, [secret]) is True
    assert asked == []  # the registered secret answered first
    assert shared_sanitize.is_flagged("plain words", [secret]) is False
    assert asked == ["check", "rules"]
    asked.clear()
    namespace: dict = {}
    exec(_BLOBS, namespace)  # noqa: S102 -- the fixture builder above, nothing else
    assert shared_sanitize.is_flagged(namespace["CIPHERTEXT"]) is True
    assert asked == ["check"]  # the credential check answered before the rules
