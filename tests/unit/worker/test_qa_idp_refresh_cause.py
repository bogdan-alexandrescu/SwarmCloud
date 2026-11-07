"""Why the gate refused an approved IdP token refresh on every screen of a crawl (part of #2688).

On 2 November 2026 a visual-QA crawl of a two-origin, Keycloak-signed staging
leg showed, on every one of its 15 screens, `gate_refused {POST, <sso origin>,
/realms/<realm>/protocol/openid-connect/token, qa_write_refused}`, although
every leg of the target carried both approved origins and exactly that
`approved_idp_token_path`. Each candidate cause is held here, ruled in or
out, against the real swarm-api answer, the worker's hand-off, and the gate's
`QaTarget`, `QaTargetGuard`, `SignIn` and `CdpProxy` on
test_qa_target_signin's scripted Chromium:

(a) THE HAND-OFF: ruled out. swarm-api serves the leg's token path, the worker
    carries it, the gate builds the one URL the record names, and the
    crawler's own tab (a second page, opened after the sign-in) is attached
    and judged by the same `QaTarget`: once signed in, its token POST goes.
(b) THE GATE'S STATE: ruled in. The gate lets the token path through only
    while its OWN sign-in ended `signed_in`, which it reads off
    `signed_in_selector` on its own tab. A test account that signs in and
    lands on a page without that element (an access-pending page) ends the
    sign-in `failed`, although the IdP session is real and every page the
    agent or the crawler then loads is signed in: every page load's token
    exchange was then refused, on every tab, for the rest of the attempt.
    FIXED on 2026-11-03 (test_qa_idp_token_production_legs): the exception
    holds from the gate's sign-in's start until the wipe, whatever its
    verdict, so the (b) tests below now hold that every load goes.
(c) THE COMPARISON: ruled out for what Chromium sends (host in lower case, no
    default port, no fragment), but the record cannot tell a query string
    apart: it keeps the path and never the query, so a refused POST with a
    query reads exactly like the record above.
(d) THE LANDING RACE: ruled in for the first load, and fixed. The gate used
    to send the agent's tab to the base URL before it recorded its sign-in's
    outcome, so the first landing's token POST was refused even on a sign-in
    that succeeded. That explained one load, not 15. `CdpProxy.qa_sign_in`
    now records the outcome first, so the landing meets `signed_in`.

THE FIX THAT FOLLOWS (b): the run must say which state the gate's sign-in is
in, or the refusal reads as the gate's rule and not as the sign-in it
undoes. The worker writes the gate's public sign-in state to the hand-off
spool (`qa_target.STATUS_NAME`), and the capture copies it into the
manifest's `gate_refusals.sign_in`.

No Chromium, network or cloud credential; every credential is built at run time.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_worker import qa_target as worker_qt
from agent_worker.livegate import qatarget as gate_qt
from swarm_api import qaspec as api_qaspec
from swarm_api import qatargets as api_qt

from test_qa_target_signin import Rig, run, tmp  # noqa: F401  (tmp is a fixture)
from test_swarm_qa import _capture, _manifest, qa, world  # noqa: F401  (fixtures)

STUDIO = "https://studio.example.com"
SSO = "https://sso.staging.example.com"
TOKEN_PATH = "/realms/agents/protocol/openid-connect/token"
TOKEN_URL = f"{SSO}{TOKEN_PATH}"
LEGS = ("stg-user", "stg-reviewer", "stg-moderator", "stg-reader", "stg-padmin", "stg-sadmin", "stg-customer")


def _leg(name: str) -> dict[str, Any]:
    return {
        "name": name, "base_url": f"{STUDIO}/?env=staging", "host": "studio.example.com",
        "origin": STUDIO, "environment": "non_production",
        "sign_in": {"kind": "own_login", "login_path": "/login", "signed_in_selector": "#workspace",
                    "extra_origin": SSO, "start_clicks": [{"text": "Sign in"}]},
        "host_state": api_qt.HOST_APPROVED, "approved_origins": [STUDIO, SSO],
        api_qt.IDP_TOKEN_FIELD: TOKEN_PATH,
    }


def _stored_target() -> dict[str, Any]:
    """A target of seven legs, as swarm-api read it on 2 November 2026."""
    return {"id": "qat_" + secrets.token_hex(8), "revision": 4, "name": "studio",
            "environments": [_leg(n) for n in LEGS], api_qaspec.MIGRATED: True}


def _gate_args(rig_args: dict[str, Any] | None = None, **over: Any) -> dict[str, Any]:
    """The worker's hand-off for one leg, with a credential built at run time."""
    args: dict[str, Any] = {
        "target_id": "qat_" + secrets.token_hex(8), "environment_name": "stg-user",
        "base_url": f"{STUDIO}/?env=staging", "origins": [STUDIO, SSO], "environment": "non_production",
        "rate": {"navigation_ms": 1000, "requests_per_s": 10}, "read_posts": [], "exclusions": {"paths": []},
        "mutating": False,
        "sign_in": {"kind": "own_login", "login_url": f"{STUDIO}/login", "signed_in_selector": "#workspace"},
        "credential": {"username": "qa-" + secrets.token_hex(4) + "@example.com",
                       "password": "pw-" + secrets.token_hex(12)},
        "idp_token_path": TOKEN_PATH,
    }
    args.update(rig_args or {})
    args.update(over)
    return args


# --------------------------------------------------------------------------
# (a) The approved path reaches the gate, and the crawler's tab with it
# --------------------------------------------------------------------------


def test_a_swarm_api_serves_each_legs_approved_token_path_and_both_origins():
    target = _stored_target()
    for name in LEGS:
        answer = api_qt.QaTargetIssuer._answer(api_qaspec.leg(target, name))
        # RULED OUT (a): an environments[] leg's view keeps the approval's fields.
        assert answer["idp_token_path"] == TOKEN_PATH, name
        assert answer["origins"] == [STUDIO, SSO], name


def test_a_the_worker_carries_the_path_and_the_gate_builds_the_url_the_record_names():
    answer = api_qt.QaTargetIssuer._answer(api_qaspec.leg(_stored_target(), "stg-user"))
    answer["environment_name"] = "stg-user"
    answer["sign_in"].update({"username": "qa-" + secrets.token_hex(4) + "@example.com",
                              "password": "pw-" + secrets.token_hex(12)})
    args = worker_qt.handoff_args(answer)
    assert args["idp_token_path"] == TOKEN_PATH
    target = gate_qt.QaTarget(lambda e, f: None)
    target.handle(args)
    # The refused record's origin and path, joined: exactly the gate's one URL.
    assert target.idp_token_url == TOKEN_URL
    target.signing_in = True
    target.finished(gate_qt.STATE_SIGNED_IN)
    assert target.judge("POST", TOKEN_URL, top_document=False).allowed == gate_qt.IDP_TOKEN_ALLOWED


def _new_tab(rig: Rig) -> str:
    """The crawler's own tab, opened after the sign-in (swarm_qa `capture`)."""
    tab = rig.fake._next("TAB")
    rig.fake.pages[tab] = "about:blank"
    rig.fake.emit("Target.targetCreated", {"targetInfo": {"targetId": tab, "type": "page"}})
    return tab


def _token_post(rig: Rig, tab: str) -> str:
    session = next(s for s, t in rig.fake.sessions.items() if t == tab)
    request_id = rig.fake._next("R")
    params = {"requestId": request_id, "resourceType": "XHR", "networkId": rig.fake._next("N"), "frameId": tab,
              "request": {"url": TOKEN_URL, "method": "POST",
                          "headers": {"Content-Type": "application/x-www-form-urlencoded"}}}
    rig.fake.paused[request_id] = (session, params)
    rig.fake.emit("Fetch.requestPaused", params, session)
    return request_id


def _refused(rig: Rig, request_id: str) -> bool:
    return any(m["params"]["requestId"] == request_id for m in rig.fake.sent("Fetch.fulfillRequest"))


async def _signed_in_rig(tmp: Path, after_submit: str) -> Rig:
    rig = Rig(tmp)
    rig.fake.start(rig.sink)
    rig.fake.probes = ["form"]
    rig.fake.after_submit = after_submit
    await gate_qt.handle_ctl(rig.target, rig.proxy, _gate_args())
    await rig.finished()
    return rig


def test_a_the_crawlers_own_tab_is_judged_by_the_same_signed_in_gate(tmp):
    async def body() -> None:
        rig = await _signed_in_rig(tmp, "signed_in")
        assert rig.target.state == gate_qt.STATE_SIGNED_IN
        tab = _new_tab(rig)
        await rig.settle()
        loads = [_token_post(rig, tab) for _ in range(3)]
        await rig.settle()
        # RULED OUT (a): a second page is attached by the guard and its token POST goes.
        assert not any(_refused(rig, r) for r in loads)
        assert sum(1 for u, _ in rig.fake.network if u == TOKEN_URL) == 3
        assert rig.target.refusals()["refusals"] == []
        assert len(rig.target.refusals()["allowed"]) == 3
        await rig.fake.stop()

    run(body)


# --------------------------------------------------------------------------
# (b) The gate's own sign-in did not end signed_in
# --------------------------------------------------------------------------


def test_b_a_sign_in_that_lands_without_the_selector_fails_and_every_load_still_goes(tmp):
    async def body() -> None:
        # The account signs in, and the app lands it on a page that does not
        # draw `signed_in_selector` (an access-pending page): the probe reads
        # "loading" until SIGNED_IN_SECONDS pass.
        rig = await _signed_in_rig(tmp, "loading")
        # RULED IN (b): the IdP session is real, and the gate counts the sign-in failed.
        assert rig.target.state == gate_qt.STATE_FAILED
        assert rig.target.cause == gate_qt.SIGN_IN_FAILED
        tab = _new_tab(rig)
        await rig.settle()
        loads = [_token_post(rig, tab) for _ in range(15)]
        await rig.settle()
        # Every screen of the crawl reaches the provider, recorded as allowed.
        # MUTATION: read the sign-in's verdict and all 15 are refused (the old rule).
        assert not any(_refused(rig, r) for r in loads)
        assert sum(1 for u, _ in rig.fake.network if u == TOKEN_URL) == 15
        assert rig.target.refusals()["refusals"] == []
        assert {(r["method"], r["origin"], r["path"], r["code"]) for r in rig.target.refusals()["allowed"]} == {
            ("POST", SSO, TOKEN_PATH, gate_qt.IDP_TOKEN_ALLOWED)}
        await rig.fake.stop()

    run(body)


def test_b_the_agent_signing_in_through_its_own_tab_does_not_change_the_gates_state(tmp):
    async def body() -> None:
        rig = await _signed_in_rig(tmp, "loading")
        # The agent clicks the chooser in its own tab (the capture prompt tells
        # it to), and the IdP session completes there: nothing the gate counts.
        tab = _new_tab(rig)
        await rig.settle()
        sent = _token_post(rig, tab)
        await rig.settle()
        assert rig.target.state == gate_qt.STATE_FAILED
        # The state stays failed, and the token POST goes all the same.
        assert not _refused(rig, sent)
        await rig.fake.stop()

    run(body)


# --------------------------------------------------------------------------
# (c) The comparison
# --------------------------------------------------------------------------


def test_c_the_url_chromium_sends_matches_and_a_query_reads_the_same_in_the_record():
    target = gate_qt.QaTarget(lambda e, f: None)
    target.handle(_gate_args())
    target.signing_in = True
    target.finished(gate_qt.STATE_SIGNED_IN)
    # RULED OUT (c) for what Chromium sends: lower-case host, no :443, no fragment.
    assert target.judge("POST", TOKEN_URL, top_document=False).action == "continue"
    with_query = f"{TOKEN_URL}?client_id=studio"
    assert target.judge("POST", with_query, top_document=False).action == "refuse"
    target.note_refusal(gate_qt.WRITE_REFUSED, "POST", with_query)
    (record,) = target.refusals()["refusals"]
    # The record keeps origin and path only, so a query is never visible in errors.json.
    assert (record["origin"], record["path"]) == (SSO, TOKEN_PATH)


# --------------------------------------------------------------------------
# (d) The landing race: the first load only, fixed
# --------------------------------------------------------------------------


def test_d_the_first_landing_runs_after_the_sign_in_is_recorded(tmp):
    async def body() -> None:
        rig = Rig(tmp)
        rig.fake.start(rig.sink)
        seen: list[str] = []
        send = rig.fake.send

        async def spy(message: dict[str, Any]) -> None:
            if message["method"] == "Page.navigate" and message["params"].get("url", "").startswith(STUDIO + "/?"):
                seen.append(rig.target.state)
            await send(message)

        rig.fake.send = spy  # type: ignore[method-assign]
        rig.proxy._send = spy  # the proxy holds the bound send it was built with
        await gate_qt.handle_ctl(rig.target, rig.proxy, _gate_args())
        await rig.finished()
        assert rig.target.state == gate_qt.STATE_SIGNED_IN
        # The landing runs once the outcome is recorded: its token POST meets
        # signed_in, not signing_in with the sign-in already over.
        assert seen == [gate_qt.STATE_SIGNED_IN]
        await rig.fake.stop()

    run(body)


# --------------------------------------------------------------------------
# The fix: the run says which state the gate's sign-in is in
# --------------------------------------------------------------------------


def _worker(**state: Any) -> worker_qt.QaTargetSignIn:
    worker = worker_qt.QaTargetSignIn(SimpleNamespace(runner_profile=worker_qt.PROFILE),
                                      logger=SimpleNamespace(info=lambda *a, **k: None,
                                                             warning=lambda *a, **k: None))
    worker._started = True
    for key, value in state.items():
        setattr(worker, key, value)
    return worker


def test_the_worker_writes_the_gates_sign_in_state_to_the_spool(tmp_path):
    target_id = "qat_" + secrets.token_hex(8)
    worker = _worker(has_target=True, target_id=target_id, environment_name="stg-user",
                     state="failed", cause="qa_sign_in_failed", sign_ins=1)
    worker.keep(None, None, None, tmp_path)
    # MUTATION: write only the markdown notice and the capture cannot say why the token POST was refused.
    document = json.loads((tmp_path / worker_qt.STATUS_NAME).read_text(encoding="utf-8"))
    assert document == {"target_id": target_id, "environment_name": "stg-user", "state": "failed",
                        "cause": "qa_sign_in_failed", "sign_ins": 1}
    worker.state, worker.cause = "signed_in", None
    worker.keep(None, None, None, tmp_path)
    document = json.loads((tmp_path / worker_qt.STATUS_NAME).read_text(encoding="utf-8"))
    assert document["state"] == "signed_in" and document["cause"] is None


def test_an_attempt_with_no_target_writes_no_sign_in_state(tmp_path):
    (tmp_path / worker_qt.STATUS_NAME).write_text("{}", encoding="utf-8")
    worker = _worker(has_target=False, wanted=False)
    worker.keep(None, None, None, tmp_path)
    # MUTATION: keep an earlier attempt's file and a run with no target reads as one.
    assert not (tmp_path / worker_qt.STATUS_NAME).exists()


def test_the_sign_in_state_carries_no_credential(tmp_path):
    password = "pw-" + secrets.token_hex(12)
    worker = _worker(has_target=True, target_id="qat_" + secrets.token_hex(8), state="signed_in", sign_ins=1)
    worker.keep(None, None, None, tmp_path)
    text = (tmp_path / worker_qt.STATUS_NAME).read_text(encoding="utf-8")
    assert password not in text
    assert set(json.loads(text)) == {"target_id", "environment_name", "state", "cause", "sign_ins"}


def test_the_capture_records_the_gates_sign_in_state_beside_its_refusals(qa, world, monkeypatch, tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    (spool / qa.QA_TARGET_STATUS_NAME).write_text(json.dumps(
        {"target_id": "qat_" + secrets.token_hex(8), "environment_name": "stg-user", "state": "failed",
         "cause": "qa_sign_in_failed", "sign_ins": 1}), encoding="utf-8")
    monkeypatch.setenv("SWARM_HANDOFF", str(spool))
    monkeypatch.setattr(qa, "_gate_document", lambda: {"format": 1, "armed": True, "refusals": [], "dropped": 0})
    code, _out, bundle = _capture(qa, world, "--settle", "generic", presets="desktop", themes="light")
    assert code == 0
    # MUTATION: leave the state out of the manifest and a refused token refresh reads as the gate's rule.
    assert _manifest(bundle)["gate_refusals"]["sign_in"] == {"state": "failed", "cause": "qa_sign_in_failed"}


def test_a_capture_with_no_sign_in_state_says_null(qa, world, monkeypatch, tmp_path):
    monkeypatch.setenv("SWARM_HANDOFF", str(tmp_path))
    monkeypatch.setattr(qa, "_gate_document", lambda: {"format": 1, "armed": True, "refusals": [], "dropped": 0})
    code, _out, bundle = _capture(qa, world, "--settle", "generic", presets="desktop", themes="light")
    assert code == 0
    assert _manifest(bundle)["gate_refusals"]["sign_in"] is None


def test_the_worker_and_the_capture_name_the_same_file(qa):
    # MUTATION: rename either constant and the capture reads a file the worker never writes.
    assert qa.QA_TARGET_STATUS_NAME == worker_qt.STATUS_NAME


def test_the_handoff_spools_file_list_names_the_sign_in_state():
    from agent_worker import handoff

    listing = handoff.__doc__.split("$SWARM_HANDOFF = work/.swarm-handoff/", 1)[1].split("WHY A SPOOL.", 1)[0]
    # MUTATION: leave qa-target.json out of the list and a reader of the spool never finds who writes it.
    line = next((row for row in listing.splitlines() if row.strip().startswith(worker_qt.STATUS_NAME)), "")
    assert "this worker's" in line
    assert worker_qt.NOTICE_NAME in listing
