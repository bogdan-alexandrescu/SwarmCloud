"""A production leg's approved IdP token POST goes through after a sign-in the gate counted failed (part of #2688).

On 3 November 2026 (rerun about 17:41 UTC, capture steps on GKE Autopilot
from 17:43) five production legs of one own-login QA target
(prod-user, prod-reviewer, prod-moderator, prod-reader, prod-customer), each
APPROVED with both origins and `approved_idp_token_path`
`/realms/sagents/protocol/openid-connect/token`, failed sign-in
(`qa_sign_in_failed`, one `qa_sign_in_no_form`), and in every screen group
the live gate refused `POST <sso origin><that path>` 69 to 79 times: exactly
the approved origin and path.

The cause, read end to end: the hand-off carries the path, the gate builds
the one URL from the approved extra origin and compares Chromium's URL to it
as a string, and they are equal (test_qa_idp_refresh_cause (a) and (c)).
What refused them was the gate's STATE rule: the exception held only while
the gate's own sign-in was in flight or had ended `signed_in`, and every leg's
sign-in ended `failed`, a verdict the gate reads off `signed_in_selector` on
its own tab, not off the provider. The 15:06 change ("during its own sign-in
as well as after it") widened the in-flight window only, so after a `failed`
verdict every screen's exchange was still refused, whichever image ran.

Held here: the exact approved URL goes, and is recorded as an IdP token
refresh, from the moment the gate's own sign-in begins until the wipe,
whatever that sign-in's verdict; and nothing else does. The record says
whether a refused request carried a query (it never keeps the query). The
gate's refusal record and the capture manifest name the worker's build.

No Chromium, network or cloud credential; every credential is built at run time.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

import pytest

from agent_worker import buildinfo
from agent_worker import qa_target as worker_qt
from agent_worker import startup
from agent_worker.livegate import qatarget as gate_qt
from swarm_api import qaspec as api_qaspec
from swarm_api import qatargets as api_qt

from test_qa_target_signin import Rig, run, tmp  # noqa: F401  (tmp is a fixture)
from test_swarm_qa import _capture, _manifest, qa, world  # noqa: F401  (fixtures)

STUDIO = "https://studio.saga.xyz"
SSO = "https://sso.galatia.saga.xyz"
TOKEN_PATH = "/realms/sagents/protocol/openid-connect/token"
TOKEN_URL = f"{SSO}{TOKEN_PATH}"
LEGS = ("prod-user", "prod-reviewer", "prod-moderator", "prod-reader", "prod-customer")
REPO_PACKAGE = Path(__file__).resolve().parents[3] / "apps" / "agent-worker" / "agent_worker"


def _leg(name: str) -> dict[str, Any]:
    """One production leg as it was approved: both origins, the token path."""
    return {
        "name": name, "base_url": f"{STUDIO}/", "host": "studio.saga.xyz", "origin": STUDIO,
        "environment": "production",
        "sign_in": {"kind": "own_login", "login_path": "/", "signed_in_selector": "#workspace",
                    "extra_origin": SSO,
                    "start_clicks": [{"text": "Production"}, {"text": "Sign in"}]},
        "host_state": api_qt.HOST_APPROVED, "approved_origins": [STUDIO, SSO],
        api_qt.IDP_TOKEN_FIELD: TOKEN_PATH,
    }


def _stored_target() -> dict[str, Any]:
    return {"id": "qat_" + secrets.token_hex(8), "revision": 2, "name": "ai-studio-prod",
            "environments": [_leg(n) for n in LEGS], api_qaspec.MIGRATED: True}


def _handoff(name: str) -> dict[str, Any]:
    """swarm-api's answer for the leg, through the worker's `handoff_args`."""
    answer = api_qt.QaTargetIssuer._answer(api_qaspec.leg(_stored_target(), name))
    answer["environment_name"] = name
    answer["sign_in"].update({"username": "qa-" + secrets.token_hex(4) + "@example.com",
                              "password": "pw-" + secrets.token_hex(12)})
    return worker_qt.handoff_args(answer)


def _target(name: str = "prod-user") -> gate_qt.QaTarget:
    target = gate_qt.QaTarget(lambda _e, _f: None, wall=lambda: 1_700_000_000.0)
    target.handle(_handoff(name))
    return target


def _post(target: gate_qt.QaTarget, url: str, method: str = "POST") -> gate_qt.Verdict:
    return target.judge(method, url, top_document=False)


# --------------------------------------------------------------------------
# The verdicts, exactly the run's approval and request
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", LEGS)
def test_every_legs_hand_off_names_the_one_url_the_run_saw_refused(name):
    target = _target(name)
    assert target.idp_token_url == TOKEN_URL
    assert target.origins == (STUDIO, SSO)


@pytest.mark.parametrize("cause", [gate_qt.SIGN_IN_FAILED, gate_qt.NO_FORM])
def test_the_approved_token_post_goes_after_a_sign_in_the_gate_counted_failed(cause):
    target = _target()
    target.signing_in = True
    target.signing_in = False
    target.finished(gate_qt.STATE_FAILED, cause)
    verdict = _post(target, TOKEN_URL)
    # MUTATION: hold the exception only while `in_flight or state == signed_in`
    # (the rule the 17:43 run met) and every screen's exchange is refused.
    assert verdict.action == "continue", verdict
    assert verdict.allowed == gate_qt.IDP_TOKEN_ALLOWED


def test_the_approved_token_post_goes_after_a_challenge_and_after_a_second_hand_off():
    target = _target()
    target.signing_in = True
    target.finished(gate_qt.STATE_CHALLENGE, gate_qt.CHALLENGE)
    assert _post(target, TOKEN_URL).allowed == gate_qt.IDP_TOKEN_ALLOWED
    # The worker asks again: queued, and the first sign-in has already begun.
    target.handle(_handoff("prod-user") | {"target_id": target.target_id})
    assert _post(target, TOKEN_URL).allowed == gate_qt.IDP_TOKEN_ALLOWED


def test_after_a_failed_sign_in_nothing_else_is_let_through():
    target = _target()
    target.signing_in = True
    target.finished(gate_qt.STATE_FAILED, gate_qt.SIGN_IN_FAILED)
    # MUTATION: let any write to the extra origin through after the sign-in and these continue.
    assert _post(target, f"{SSO}/realms/sagents/account/credentials").action == "refuse"
    assert _post(target, f"{SSO}{TOKEN_PATH}/").action == "refuse"
    assert _post(target, f"{TOKEN_URL}?client_id=ai-studio-web").action == "refuse"
    assert _post(target, f"{STUDIO}{TOKEN_PATH}").action == "refuse"
    assert _post(target, f"{STUDIO}/api/ship").action == "refuse"
    assert _post(target, f"https://api.galatia.saga.xyz{TOKEN_PATH}").action == "refuse"
    for method in ("PUT", "PATCH", "DELETE"):
        assert _post(target, TOKEN_URL, method).action == "refuse", method
    # A top frame posted to the provider still leaves the target.
    assert target.judge("POST", TOKEN_URL, top_document=True).action == "leave"


def test_never_before_the_gates_own_sign_in_begins_and_never_after_the_wipe():
    target = _target()
    # MUTATION: allow from the hand-off (`state == signing_in`) and this continues.
    assert _post(target, TOKEN_URL).action == "refuse"
    target.signing_in = True
    target.finished(gate_qt.STATE_FAILED, gate_qt.SIGN_IN_FAILED)
    target.wipe()
    # MUTATION: leave `wiped` out of the exception's test and this continues.
    assert _post(target, TOKEN_URL).action == "refuse"


# --------------------------------------------------------------------------
# End to end: the sign-in lands without its selector; the crawl's screens load
# --------------------------------------------------------------------------


def _new_tab(rig: Rig) -> str:
    tab = rig.fake._next("TAB")
    rig.fake.pages[tab] = "about:blank"
    rig.fake.emit("Target.targetCreated", {"targetInfo": {"targetId": tab, "type": "page"}})
    return tab


def _token_post(rig: Rig, tab: str, url: str = TOKEN_URL) -> str:
    session = next(s for s, t in rig.fake.sessions.items() if t == tab)
    request_id = rig.fake._next("R")
    params = {"requestId": request_id, "resourceType": "XHR", "networkId": rig.fake._next("N"), "frameId": tab,
              "request": {"url": url, "method": "POST",
                          "headers": {"Content-Type": "application/x-www-form-urlencoded"}}}
    rig.fake.paused[request_id] = (session, params)
    rig.fake.emit("Fetch.requestPaused", params, session)
    return request_id


def _refused(rig: Rig, request_id: str) -> bool:
    return any(m["params"]["requestId"] == request_id for m in rig.fake.sent("Fetch.fulfillRequest"))


def _plain_args() -> dict[str, Any]:
    """The leg's hand-off on the rig's scripted login page: no start clicks."""
    args = _handoff("prod-user")
    args["sign_in"] = {"kind": "own_login", "login_url": f"{STUDIO}/login", "signed_in_selector": "#workspace"}
    return args


def test_every_screen_of_a_leg_whose_sign_in_ended_failed_reaches_its_provider(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        rig.fake.start(rig.sink)
        rig.fake.probes = ["form"]
        # Submitted, and the page never draws `signed_in_selector`.
        rig.fake.after_submit = "loading"
        await gate_qt.handle_ctl(rig.target, rig.proxy, _plain_args())
        await rig.finished()
        assert (rig.target.state, rig.target.cause) == (gate_qt.STATE_FAILED, gate_qt.SIGN_IN_FAILED)
        tab = _new_tab(rig)
        await rig.settle()
        loads = [_token_post(rig, tab) for _ in range(5)]
        other = _token_post(rig, tab, f"{SSO}/realms/sagents/account/credentials")
        await rig.settle()
        # MUTATION: the old state rule and all five are answered 403 by the gate.
        assert not any(_refused(rig, r) for r in loads)
        assert sum(1 for u, _ in rig.fake.network if u == TOKEN_URL) == 5
        record = rig.target.refusals()
        assert [(r["origin"], r["path"]) for r in record["allowed"]] == [(SSO, TOKEN_PATH)] * 5
        # The provider's other path is still the gate's to answer.
        assert _refused(rig, other)
        assert [(r["origin"], r["path"]) for r in record["refusals"]] == [(SSO, "/realms/sagents/account/credentials")]
        await rig.fake.stop()

    run(body)


# --------------------------------------------------------------------------
# The record: a query is said, never kept
# --------------------------------------------------------------------------


def test_a_refused_request_with_a_query_is_told_apart_from_one_without():
    target = _target()
    secret_ish = secrets.token_hex(6)
    target.note_refusal(gate_qt.WRITE_REFUSED, "POST", f"{TOKEN_URL}?code={secret_ish}")
    target.note_refusal(gate_qt.WRITE_REFUSED, "POST", TOKEN_URL)
    with_query, without = target.refusals()["refusals"]
    # MUTATION: keep origin and path alone and the two records read the same.
    assert (with_query["path"], with_query["query"]) == (TOKEN_PATH, True)
    assert (without["path"], without["query"]) == (TOKEN_PATH, False)
    assert secret_ish not in json.dumps(target.refusals())


# --------------------------------------------------------------------------
# Which build ran: the start-up line, the gate's record, the capture manifest
# --------------------------------------------------------------------------


def test_the_code_fingerprint_is_the_checkouts_and_changes_with_one_byte(tmp_path):
    assert buildinfo.describe()["code"] == buildinfo.code_fingerprint(REPO_PACKAGE)
    copy = tmp_path / "agent_worker"
    (copy / "livegate").mkdir(parents=True)
    (copy / "livegate" / "qatarget.py").write_text("A = 1\n", encoding="utf-8")
    (copy / "__pycache__").mkdir()
    (copy / "__pycache__" / "x.py").write_text("ignored\n", encoding="utf-8")
    first = buildinfo.code_fingerprint(copy)
    assert first is not None and first.startswith("sha256:")
    (copy / "__pycache__" / "x.py").write_text("still ignored\n", encoding="utf-8")
    assert buildinfo.code_fingerprint(copy) == first
    (copy / "livegate" / "qatarget.py").write_text("A = 2\n", encoding="utf-8")
    # MUTATION: hash the file names alone and a changed gate reads as the same build.
    assert buildinfo.code_fingerprint(copy) != first
    assert buildinfo.code_fingerprint(tmp_path / "missing") is None


def test_the_build_reads_the_commit_and_image_and_drops_a_malformed_one(tmp_path):
    sha = secrets.token_hex(20)
    image = f"us-central1-docker.pkg.dev/p/swarm-images/agent-runtime-browser@sha256:{secrets.token_hex(32)}"
    got = buildinfo.describe_env({"GIT_SHA": sha, "SWARM_WORKER_IMAGE": image}, REPO_PACKAGE)
    assert (got["commit"], got["image"]) == (sha, image)
    assert buildinfo.describe_env({"SWARM_GIT_SHA": sha, "GIT_SHA": "other"}, REPO_PACKAGE)["commit"] == sha
    bad = buildinfo.describe_env({"GIT_SHA": "a b;c", "SWARM_WORKER_IMAGE": "not an image"}, REPO_PACKAGE)
    assert (bad["commit"], bad["image"]) == (None, None)


def test_the_worker_logs_its_build_in_its_first_line():
    lines: list[tuple[str, dict[str, Any]]] = []

    class Log:
        def info(self, message: str, **fields: Any) -> None:
            lines.append((message, fields))

    startup.Phases(Log()).started(execution="x")
    ((message, fields),) = lines
    assert message == "worker process started"
    # MUTATION: log only pid and python and nothing says which build ran.
    assert fields["build"] == buildinfo.describe()
    assert fields["build"]["code"].startswith("sha256:")


def test_the_gates_refusal_record_names_its_build():
    record = _target().refusals()
    # MUTATION: leave the build out of the record and the capture cannot say which gate refused.
    assert record["build"] == buildinfo.describe()


def test_the_capture_manifest_records_the_workers_build(qa, world, monkeypatch):
    build = {"commit": secrets.token_hex(20), "image": None, "code": "sha256:" + secrets.token_hex(32)}
    monkeypatch.setattr(qa, "_gate_document", lambda: {"format": 1, "armed": True, "refusals": [], "dropped": 0,
                                                       "build": build})
    code, _out, bundle = _capture(qa, world, "--settle", "generic", presets="desktop", themes="light")
    assert code == 0
    # MUTATION: leave `worker` out of the manifest and a run cannot say which gate refused it.
    assert _manifest(bundle)["worker"] == build


def test_a_capture_whose_gate_names_no_build_says_null(qa, world, monkeypatch):
    monkeypatch.setattr(qa, "_gate_document", lambda: {"format": 1, "armed": True, "refusals": [], "dropped": 0,
                                                       "build": {"commit": "a b", "code": 7}})
    code, _out, bundle = _capture(qa, world, "--settle", "generic", presets="desktop", themes="light")
    assert code == 0
    assert _manifest(bundle)["worker"] == {"commit": None, "image": None, "code": None}
