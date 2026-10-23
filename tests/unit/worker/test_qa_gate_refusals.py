"""The gate's refusals are known to the run (QA-GATE-REFUSALS; part of #2067).

The visual-QA run of 2026-10-23 filed SwarmCloud's own refusal into the app's
repository as a CORS blocker: the app's OIDC token exchange, a `POST` to its
identity provider (another origin), got the gate's 403, which carries no CORS
header, so Chrome logged a CORS block and every screen stayed on its loading
state. Nothing in the capture said the gate had answered that request.

Held here, through the real `QaTarget`, `QaTargetGuard` and `CdpProxy` against
test_qa_target_signin's scripted Chromium (no Chromium, network or cloud
credential):

* the gate refusing a `POST` to another origin leaves one record, with its
  time, method, origin, path and the gate's code, and none of the request's
  query or body;
* a top-frame navigation off the target leaves a `qa_target_left` record;
* a path carrying the held password is scrubbed in the record.

The refusal RULES are not changed here: what is refused is test_qa_target_gate_policy's.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from agent_worker.livegate import qatarget as gate_qt

import test_qa_target_signin as signin
from test_qa_target_gate_policy import _fulfilled, _pause, _rig, _sent
from test_qa_target_signin import ORIGIN, run, tmp  # noqa: F401  (tmp is a fixture)

IDP = "https://sso.example.net"
TOKEN_PATH = "/realms/apps/protocol/openid-connect/token"


def _post(rig: Any, session: str, url: str, body: str) -> str:
    """A `POST` with a body, paused at the Request stage on the page."""
    request_id = rig.fake._next("R")
    params: dict[str, Any] = {
        "requestId": request_id, "resourceType": "Fetch", "networkId": rig.fake._next("N"),
        "frameId": "PAGE1",
        "request": {"url": url, "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"},
                    "postData": body, "hasPostData": True},
    }
    rig.fake.paused[request_id] = (session, params)
    rig.fake.emit("Fetch.requestPaused", params, session)
    return request_id


def test_a_post_to_another_origin_the_gate_refuses_leaves_one_record_without_query_or_body(tmp):
    async def body() -> None:
        rig, session = await _rig(tmp)
        code = secrets.token_hex(12)
        verifier = secrets.token_hex(16)
        refused = _post(rig, session, f"{IDP}{TOKEN_PATH}?session_state={code}",
                        f"grant_type=authorization_code&code={code}&code_verifier={verifier}")
        fetched = _pause(rig, session, f"{ORIGIN}/api/list")
        await rig.settle()
        # The rule is unchanged: the POST is answered by the gate and never leaves.
        assert _fulfilled(rig, refused) is not None and not _sent(rig, f"{IDP}{TOKEN_PATH}?session_state={code}")
        document = rig.target.refusals()
        records = document["refusals"]
        # MUTATION: drop the note_refusal call on the "refuse" verdict in QaTargetGuard._paused and this is empty.
        assert len(records) == 1, records
        record = records[0]
        # MUTATION: record the full URL (or the origin alone) as the path and this fails.
        assert {k: record[k] for k in ("method", "origin", "path", "code")} == {
            "method": "POST", "origin": IDP, "path": TOKEN_PATH, "code": "qa_write_refused"}
        # MUTATION: record the gate's monotonic clock instead of wall time and `at` is no epoch second.
        assert isinstance(record["at"], float) and record["at"] > 1.7e9
        text = json.dumps(document)
        # MUTATION: keep the query or the postData in the record and the exchange's code is in it.
        assert code not in text and verifier not in text and "?" not in record["path"]
        # A request the gate continued is no refusal.
        assert _fulfilled(rig, fetched) is None
        assert document["armed"] is True and document["format"] == gate_qt.REFUSALS_FORMAT
        await rig.fake.stop()

    run(body)


def test_a_top_frame_navigation_off_the_target_is_recorded_as_left(tmp):
    async def body() -> None:
        rig, session = await _rig(tmp)
        _pause(rig, session, "https://elsewhere.example.org/landing?ref=x", kind="Document")
        await rig.settle()
        records = rig.target.refusals()["refusals"]
        # MUTATION: drop the note_refusal call on the "leave" verdict and this is empty.
        assert [(r["method"], r["origin"], r["path"], r["code"]) for r in records] == [
            ("GET", "https://elsewhere.example.org", "/landing", "qa_target_left")]
        await rig.fake.stop()

    run(body)


def test_a_refused_path_carrying_the_held_password_is_scrubbed_in_the_record():
    records: list[tuple[str, Any]] = []
    target = gate_qt.QaTarget(lambda e, f: records.append((e, f)))
    password = signin._password()
    target.handle(signin._args(signin._target_id(), password))
    target.note_refusal("qa_write_refused", "POST", f"{IDP}/p/{password}/x")
    path = target.refusals()["refusals"][0]["path"]
    # MUTATION: store the raw path and the password is served to the capture.
    assert password not in path and gate_qt.PLACEHOLDER in path


def test_the_record_is_bounded_and_says_how_many_it_dropped():
    target = gate_qt.QaTarget(lambda e, f: None)
    for i in range(gate_qt.MAX_REFUSALS + 3):
        target.note_refusal("qa_write_refused", "POST", f"{IDP}/n/{i}")
    document = target.refusals()
    # MUTATION: keep every refusal and a run that refuses in a loop grows the gate without bound.
    assert len(document["refusals"]) == gate_qt.MAX_REFUSALS
    assert document["dropped"] == 3
    # Numbered, so a reader can tell the newest and drop what it has already read.
    assert document["refusals"][-1]["n"] == gate_qt.MAX_REFUSALS + 3
