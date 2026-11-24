"""The §5.4 data-flow rules each have a test that fails on their removal (archived issue 1336).

The rules are what the security pass (archived issue 476) signs off before Jev
is turned on for a workspace beyond the owner's. The rules that had no test
failing on their removal get one below. Each says which removal turns it red,
and each carries the control that shows the removal is the only thing it could
be catching. The lane hooks' launcher exports the scrub and the outbound
filter they rely on.

Every fake credential is built at runtime (`secrets.token_hex`, a repeated
pattern, random bytes): a literal would be refused by the worker's publish scan.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import re
from pathlib import Path

import pytest

from agent_worker.judge import budget, client, poststeps, register, sanitize, settings, shapes

REPO = Path(__file__).resolve().parents[3]
LAUNCHER = REPO / "images" / "agent-runtime-base" / "jev-gates" / "bin" / "swarm-jev-gate.mjs"


# ---------------------------------------------------------------------------
# Rules with no test that failed on their removal
# ---------------------------------------------------------------------------


def _token() -> str:
    return "ghp_" + "A1b2C3d4E5" * 4  # the classic PAT shape, 44 characters


def test_the_credential_scan_alone_drops_a_headerless_key() -> None:
    """Rule 2. Red when `_credential_in` leaves `sanitize.is_flagged`: random
    key material has no prefix any redaction rule knows (the control)."""
    from swarm_redaction.rules import redact

    # Random base64 occasionally holds a run some redaction rule matches (CI,
    # run 39388236975), so draw until one does not: the control is that the
    # blob passed below is one the redaction rules leave alone.
    blobs = ("blob = " + base64.b64encode(os.urandom(512)).decode() for _ in range(50))
    blob = next((b for b in blobs if redact(b).count == 0), None)
    assert blob is not None, "every random blob matched a redaction rule"
    out = sanitize.sanitize_state({"candidates": [{"i": 0, "path": "a.py", "excerpt": blob}]})
    assert out.state == {"candidates": [{"i": 0, "path": "a.py"}]} and out.dropped == 1


def test_the_redaction_rules_alone_drop_a_token_glued_to_a_word() -> None:
    """Rule 3. Red when `swarm_redaction.rules.redact` leaves `is_flagged`: the
    publish scan wants a match to start a token, so a token glued to a word
    is the redaction rules' alone (the control)."""
    from agent_worker.lifecycle import _credential_in

    glued = "x" + _token()
    assert _credential_in("", glued) is None
    out = sanitize.sanitize_state({"task": {"type": "fix", "brief": f"see {glued}"}})
    assert out.state == {"task": {"type": "fix"}} and out.dropped == 1


#: Rule 5, spelled out here rather than read from NEVER_SENT_KEYS, so a key
#: removed from that set turns this red.
NEVER_SENT = ("author", "authors", "email", "emails", "user", "login",
              "env", "environ", "environment", "transcript")


@pytest.mark.parametrize("key", NEVER_SENT)
def test_every_never_sent_key_is_removed_at_any_depth_and_any_case(key: str) -> None:
    value = secrets.token_hex(8)
    state = {"issue": {"title": "t", key: value, "rows": [{key.upper(): value, "i": 1}]}}
    out = sanitize.sanitize_state(state)
    assert out.state == {"issue": {"title": "t", "rows": [{"i": 1}]}}
    assert value not in json.dumps(out.state)


def test_j5_sends_an_in_flight_lane_as_title_and_files_only() -> None:
    """Rule 5 (in-flight beyond titles and paths). Red when `j5_duplicate`
    copies a lane's other fields."""
    lane = {"title": "lane t", "files": ["a.py"], "body": "the lane's brief",
            "prompt": "do it", "branch": "swarm/x", "diff": "+x", "ref": "PR 1"}
    state, _ = shapes.j5_duplicate("issue", "body", [lane])
    assert state["in_flight"] == [{"i": 0, "title": "lane t", "files": ["a.py"]}]


def test_j3_sends_a_file_outside_the_checkout_by_path_only(tmp_path: Path) -> None:
    """Rule 5 (another repository's content). Red when `code_window` reads a
    path that resolves outside the attempt's checkout; a file inside it is
    read (the control)."""
    checkout = tmp_path / "repo"
    other = tmp_path / "other-repo"
    checkout.mkdir()
    other.mkdir()
    (other / "x.py").write_text("one\ntwo\nthree\n")
    (checkout / "y.py").write_text("one\ntwo\nthree\n")
    assert poststeps.code_window(checkout, "../other-repo/x.py", 2) == {"path": "../other-repo/x.py"}
    assert "two" in poststeps.code_window(checkout, "y.py", 2)["window"]


def test_the_runner_client_drops_the_runners_own_key(tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule 4 in the runner. Red when `cliagent._jev_client` stops adding the
    runner's credential variables to the client's registered secrets: the
    config `attempt.setup` writes names none (the control)."""
    from agent_worker.judge import attempt
    from agent_worker.runners import claude_code, cliagent

    done = register.prepare(attempt_dir=tmp_path, settings=settings.JudgeSettings(code_opt_in=True),
                            key=None, environ={settings.INTERNAL_URL_ENV: "http://decide.internal/v1"})
    assert done.registered and done.config_path is not None
    assert json.loads(done.config_path.read_text())["secret_env_names"] == []
    own = "k" + secrets.token_hex(24)
    monkeypatch.setenv(attempt.CONFIG_ENV, str(done.config_path))
    monkeypatch.setenv(claude_code.SPEC.key_env, own)
    jev = cliagent._jev_client(claude_code.SPEC)
    assert jev is not None and own in jev.secrets
    out = sanitize.sanitize_state({"task": {"brief": f"use {own}"}}, secrets=jev.secrets)
    assert out.state == {"task": {}}


def _typesafe(tmp_path: Path, key: str, transport: client.Transport) -> client.JudgeClient:
    chosen = settings.resolve_backend(
        settings.JudgeSettings(code_opt_in=True, backend=settings.TYPESAFE), key=key)
    assert isinstance(chosen, settings.Backend)
    return client.JudgeClient(chosen, budget.Ledger.create(tmp_path / "ledger.json"),
                              transport=transport, sleep=lambda s: None,
                              log=lambda *a, **k: None)


QUESTION = {"q": {"type": "noul", "instructions": "Is `x` relevant?"}}
#: The model asked for and whether the tokens were estimated (lane
#: AQ-JEV-ACCOUNTING) are a name and a flag, never a payload.
AUDIT_KEYS = {"call_id", "kind", "backend", "bytes", "input_tokens", "ms", "outcome",
              "requested_model", "tokens_estimated"}


@pytest.mark.parametrize("status", [200, 503])
def test_the_audit_row_holds_counts_never_the_payload_the_answer_or_the_key(
        tmp_path: Path, status: int) -> None:
    """Rule 6, on a success and on a failure. Red when `decide` puts the
    state, the answer or the bearer into the ledger's row."""
    key = "k-" + secrets.token_hex(20)
    marker = "payload-" + secrets.token_hex(6)
    answer = "answer-" + secrets.token_hex(6)
    sent: list[bytes] = []

    def transport(url, body, headers, timeout):
        sent.append(body)
        return status, json.dumps({"answers": {"q": {"noul": 0.5}}, "usage": {"input_tokens": 7},
                                   "note": answer}).encode()

    jev = _typesafe(tmp_path, key, transport)
    jev.decide("J1", {"task": {"brief": marker}}, QUESTION)
    assert sent and marker.encode() in sent[0]  # the control: it was sent
    raw = (tmp_path / "ledger.json").read_text()
    assert marker not in raw and answer not in raw and key not in raw
    (row,) = json.loads(raw)["audit"]
    assert set(row) == AUDIT_KEYS
    assert row["outcome"] == ("ok" if status == 200 else "failed")
    assert row["bytes"] == len(sent[0])


def test_the_lane_hooks_launcher_exports_the_scrub_and_the_outbound_filter() -> None:
    """JEV-LANES and lane jev-outbound-redaction: the launcher scrubs the
    registered secrets and the key to `<redacted>`, and filters what it sends."""
    launcher = LAUNCHER.read_text(encoding="utf-8")
    for name in ("guardedFetch", "scrub", "filterBody", "credentialFlags", "isDeniedPath",
                 "installBrowserFilter"):
        assert re.search(rf"export function {name}\(", launcher), name
    assert 'export const REDACTED = "<redacted>"' in launcher
