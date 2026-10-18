"""Every GitHub client knows its remaining quota, and background reads wait below 20% (#1483, QUOTA-THROTTLE).

Measured 2026-10-17: background reads -- the merge wake every minute per
park, the after-PR gates, the stranded-PR sweep, the index poll -- spent a
person's 5,000 calls an hour, and with no backoff kept asking while GitHub
refused (~25-50 refused calls a minute), so the window never refilled.

What these tests hold:

  1. `forge_limits.QUOTAS` keeps each credential's `X-RateLimit-*` under a
     non-secret identity; only answers received inside `acting_as` count; a
     rate-limited answer holds its credential until the reset GitHub named.
  2. The request paths feed it: `forgewrite.GitHubWriter._call` (whose
     production transport now answers headers), `forge.GitHubIssues._get`,
     the index poll's read, forgeperms' reader. A 403 that is a rate limit is
     never a refusal on any of them (`forge.py` `_get` mapped it to
     `no_access`).
  3. The merge wake, the after-PR gates, the stranded-PR sweep, the index
     poll and forgeperms' own read DEFER under the threshold: nothing is
     sent, nothing is written, one line says whose quota and until when.
  4. The first rate-limited answer of a tick stops the tenant's tick and
     holds it until the reset: the merge wake and the gates asked again for
     every park, every minute.
  5. A person's action -- a redrive, a permissions refresh -- is never
     deferred.
  6. The threshold is a setting, default 20.

Every token is built at runtime. No credentials, no network, no emulator.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from swarm_api import forge, forge_limits, forgewrite, prgate
from swarm_api import settings as settings_module
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.forge_limits import QUOTAS, acting_as, installation_credential, tenant_credential
from swarm_api.forgeapp import HttpAnswer
from swarm_api.forgeperms import ForgePermissions
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.repoindex import read_head_if_changed
from swarm_api.settings import ApiSettings
from swarm_api.validation import IssueRef
from swarm_api.waker import NullWaker
from swarm_common.models import Tenant
from swarm_common.states import ParkReason

from . import forge_fakes
from .conftest import api_settings, auth_header, seed_task, seed_tenant
from .test_forgeperms import FakeGitHub, _app, _repo, _token_repo
from .test_merge_wake import (
    CHECK, HEAD, NOW, NUMBER, SWEEPER, SWEEPER_HEADERS, Clock, QueueGitHub, parked_merge,
)
from .test_stranded_prs import (
    NAME, OWNER, REPO, GitHub as StrandedGitHub, _opener, _register, _sha,
)

LIMIT = 5000
RATE_LIMITED = b'{"message": "API rate limit exceeded for user ID 5000393."}'


def _value() -> str:
    return secrets.token_hex(20)


def _limits(remaining: int, *, reset: datetime, limit: int = LIMIT,
            resource: str = "core") -> dict[str, str]:
    return {"X-RateLimit-Limit": str(limit), "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(int(reset.timestamp())),
            "X-RateLimit-Resource": resource}


def _later(hours: float = 1) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


class Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def lines():
    handler = Lines()
    loggers = [logging.getLogger(name) for name in (
        "swarm_api.forge_limits", "swarm_api.mergewake", "swarm_api.prgate",
        "swarm_api.strandedprs")]
    levels = [lg.level for lg in loggers]
    for lg in loggers:
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
    yield handler
    for lg, level in zip(loggers, levels):
        lg.removeHandler(handler)
        lg.setLevel(level)


class Metered:
    """A 2-tuple fake GitHub behind GitHub's headers: every answer carries
    `X-RateLimit-*`; `limited` answers every request 403 rate-limited."""

    def __init__(self, inner: Any, *, reset: datetime, remaining: int = 4000) -> None:
        self.inner = inner
        self.reset = reset
        self.remaining = remaining
        self.limited = False
        self.paths: list[tuple[str, str]] = []

    def __call__(self, method, url, headers, body, timeout):
        self.paths.append((method, urlparse(url).path))
        if self.limited:
            return 403, RATE_LIMITED, _limits(0, reset=self.reset)
        status, raw = self.inner(method, url, headers, body, timeout)[:2]
        return status, raw, _limits(self.remaining, reset=self.reset)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


# --------------------------------------------------------------------------
# 1. the ledger
# --------------------------------------------------------------------------

def test_a_credential_under_the_threshold_is_deferred_until_its_reset():
    reset = _later()
    now = datetime.now(timezone.utc)
    QUOTAS.observe("tenant:eng", _limits(999, reset=reset))
    deferral = QUOTAS.defer("tenant:eng", 20, now=now)
    assert deferral is not None and deferral.until == reset.replace(microsecond=0)
    assert "credential=tenant:eng" in deferral.line() and "remaining=999/5000" in deferral.line()
    # The control: one call over a fifth is not deferred.
    QUOTAS.observe("tenant:eng", _limits(1001, reset=reset))
    assert QUOTAS.defer("tenant:eng", 20, now=now) is None
    # After the reset GitHub has refilled it: a low reading is history.
    QUOTAS.observe("tenant:eng", _limits(10, reset=reset))
    assert QUOTAS.defer("tenant:eng", 20, now=reset + timedelta(seconds=1)) is None
    # 0 turns the deferral off.
    assert QUOTAS.defer("tenant:eng", 0, now=now) is None


def test_only_an_answer_received_acting_as_a_credential_counts():
    reset = _later()
    QUOTAS.observe_answer(forge_limits.Answer(200, _limits(7, reset=reset)))
    assert QUOTAS.quota("tenant:eng") is None
    with acting_as("tenant:eng"):
        QUOTAS.observe_answer(forge_limits.Answer(200, _limits(7, reset=reset)))
    quota = QUOTAS.quota("tenant:eng")
    assert quota is not None and (quota.limit, quota.remaining, quota.resource) == (LIMIT, 7, "core")
    assert set(quota.to_api()) == {"limit", "remaining", "reset_at", "resource", "observed_at"}


def test_a_rate_limited_answer_holds_its_credential_until_the_reset_it_named():
    now = datetime.now(timezone.utc)
    with acting_as("installation:7"):
        QUOTAS.observe_answer(forge_limits.Answer(429, {"Retry-After": "120"}, b"{}"), now=now)
    deferral = QUOTAS.defer("installation:7", 20, now=now)
    assert deferral is not None and deferral.until == now + timedelta(seconds=120)
    assert QUOTAS.defer("installation:7", 20, now=now + timedelta(seconds=121)) is None
    # Named nothing: held for a tick, not for ever.
    with acting_as("installation:8"):
        QUOTAS.observe_answer(forge_limits.Answer(403, {}, RATE_LIMITED), now=now)
    assert QUOTAS.held("installation:8", now=now) == now + forge_limits.DEFAULT_HOLD


# --------------------------------------------------------------------------
# 2. the request paths
# --------------------------------------------------------------------------

def _pull_answer() -> bytes:
    return json.dumps({"number": 1, "state": "open", "merged": False,
                       "head": {"sha": HEAD, "ref": "x"}, "base": {"ref": "main"}}).encode()


def test_the_writers_answers_update_the_credential_it_acts_as():
    reset = _later()
    writer = forgewrite.GitHubWriter(
        send=lambda *a: (200, _pull_answer(), _limits(4321, reset=reset)))
    with acting_as(installation_credential(99)):
        writer.read_pull(IssueRef(owner="o", repo="r", number=1), 1, _value())
    assert QUOTAS.quota("installation:99").remaining == 4321


def test_the_writers_transport_answers_githubs_headers(monkeypatch):
    class Response:
        status = 200
        headers = {"X-RateLimit-Remaining": "7"}

        def read(self, n: int) -> bytes:
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(forgewrite, "_OPENER", SimpleNamespace(open=lambda r, timeout: Response()))
    sent = forgewrite._urllib_send("GET", "https://api.github.com/rate_limit", {}, None, 1.0)
    assert sent[0] == 200 and sent[2]["X-RateLimit-Remaining"] == "7"


def test_a_403_with_nothing_remaining_is_a_rate_limit_not_a_refusal():
    reset = _later()
    writer = forgewrite.GitHubWriter(send=lambda *a: (
        403, b'{"message": "Forbidden"}', _limits(0, reset=reset)))
    with pytest.raises(forgewrite.ForgeRateLimited) as raised:
        writer.read_pull(IssueRef(owner="o", repo="r", number=1), 1, _value())
    assert forgewrite.is_transient(raised.value)
    assert not forgewrite.is_credential_refusal(raised.value)
    assert raised.value.reset == reset.replace(microsecond=0)


def test_the_issue_reader_maps_a_rate_limited_403_to_read_failed_not_no_access():
    issues = forge.GitHubIssues(send=lambda url, headers, timeout: (403, RATE_LIMITED))
    with pytest.raises(forge.IssueReadFailed) as raised:
        issues.fetch(IssueRef(owner="o", repo="r", number=3), _value())
    assert raised.value.code == "read_failed" and "limiting" in raised.value.message
    # The control: GitHub's real refusal stays one.
    refusing = forge.GitHubIssues(
        send=lambda url, headers, timeout: (403, b'{"message": "Resource not accessible"}'))
    with pytest.raises(forge.IssueNoAccess):
        refusing.fetch(IssueRef(owner="o", repo="r", number=3), _value())


# --------------------------------------------------------------------------
# 3-4. the merge wake and the after-PR gates
# --------------------------------------------------------------------------

@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def metered() -> Metered:
    gh = QueueGitHub()
    gh.open_pull(NUMBER, HEAD, ref="swarm/task_int")
    gh.require(CHECK)
    for number in (51, 52):
        gh.open_pull(number, "c" * 40, ref=f"feature/{number}")
    return Metered(gh, reset=NOW + timedelta(minutes=10))


@pytest.fixture
def wake(db, tokens, group_map, objects, metered, clock) -> TestClient:
    seed_tenant(db, "eng")
    tokens = dict(tokens)
    tokens["token-sweeper"] = {"email": SWEEPER, "email_verified": True, "sub": "sub-sweeper"}
    context = build_context(
        settings=api_settings(rollup_sweeper_users=(SWEEPER,)), db=db,
        verifier=StaticTokenVerifier(tokens), groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=objects, now=clock, forge_tokens=forge_fakes.AnyTenantTokens(),
        forge_writer=forgewrite.GitHubWriter(send=metered, locate=metered.inner.locate,
                                             fetch=metered.inner.fetch),
    )
    return TestClient(create_app(context), raise_server_exceptions=False)


def _tick(client: TestClient) -> dict:
    response = client.post("/v1/admin/merges/wake?tenant_id=eng", headers=SWEEPER_HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def _github_reads(metered: Metered) -> list[str]:
    return [path for method, path in metered.paths if method == "GET"]


def test_a_park_whose_credential_is_low_is_not_read_and_not_written(db, wake, metered, lines):
    parked_merge(db)
    before = json.dumps(db.docs["tasks/task_merge"], default=str, sort_keys=True)
    QUOTAS.observe(tenant_credential("eng"), _limits(500, reset=NOW + timedelta(hours=1)))

    body = _tick(wake)

    assert _github_reads(metered) == []
    assert body["report"]["deferred"] == 1 and body["report"]["visited"] == 0
    # Left exactly as it was: it holds no capacity (invariant 1).
    assert json.dumps(db.docs["tasks/task_merge"], default=str, sort_keys=True) == before
    assert "credential=tenant:eng remaining=500/5000" in lines.text

    # The control: with the quota refilled the same park is read.
    QUOTAS.observe(tenant_credential("eng"), _limits(4000, reset=NOW + timedelta(hours=1)))
    body = _tick(wake)
    assert body["report"]["deferred"] == 0 and _github_reads(metered)


def test_the_wakes_reads_keep_the_quota_of_the_credential_they_act_as(db, wake, metered):
    parked_merge(db)
    metered.remaining = 3210
    _tick(wake)
    assert QUOTAS.quota(tenant_credential("eng")).remaining == 3210


def test_the_first_rate_limited_answer_stops_the_wake_until_the_reset(db, wake, metered,
                                                                      clock, lines):
    parked_merge(db, "task_a")
    parked_merge(db, "task_b")
    metered.limited = True

    body = _tick(wake)
    # One refused call, not one per park.
    assert len(_github_reads(metered)) == 1
    assert body["report"]["failed"] == 1 and body["report"]["deferred"] == 1
    assert "rate-limited; held until" in lines.text

    clock.now = NOW + timedelta(minutes=1)
    metered.limited = False
    body = _tick(wake)
    assert len(_github_reads(metered)) == 1, "asked again before the reset GitHub named"
    assert body["report"]["deferred"] == 2

    clock.now = NOW + timedelta(minutes=11)
    _tick(wake)
    assert len(_github_reads(metered)) > 1


def _gate(db, task_id: str, number: int) -> dict:
    doc = seed_task(db, task_id=task_id, tenant_id="eng", state="PARKED",
                    park_reason=ParkReason.DEPENDENCY_INCOMPLETE.value)
    doc["repository_url"] = "https://github.com/saga-xyz/widgets"
    doc["metadata"] = {prgate.AFTER_PRS_METADATA_KEY: {
        "pull_requests": [number], "merged": [], "state": prgate.STATE_WAITING}}
    return doc


def test_the_first_rate_limited_gate_read_stops_the_gates_until_the_reset(db, wake, metered,
                                                                          clock):
    _gate(db, "task_g1", 51)
    _gate(db, "task_g2", 52)
    metered.limited = True

    body = _tick(wake)
    assert len(_github_reads(metered)) == 1
    assert body["after_prs"]["failed"] == 1 and body["after_prs"]["deferred"] == 1

    clock.now = NOW + timedelta(minutes=1)
    metered.limited = False
    _tick(wake)
    assert len(_github_reads(metered)) == 1

    clock.now = NOW + timedelta(minutes=11)
    _tick(wake)
    assert len(_github_reads(metered)) == 3


def test_gates_are_not_read_while_the_tenants_quota_is_low(db, wake, metered):
    gate = _gate(db, "task_g1", 51)
    before = dict(gate["metadata"][prgate.AFTER_PRS_METADATA_KEY])
    QUOTAS.observe(tenant_credential("eng"), _limits(100, reset=NOW + timedelta(hours=1)))

    body = _tick(wake)
    assert _github_reads(metered) == []
    assert body["after_prs"]["deferred"] == 1
    assert db.docs["tasks/task_g1"]["metadata"][prgate.AFTER_PRS_METADATA_KEY] == before


# --------------------------------------------------------------------------
# 3-5. the stranded-PR sweep
# --------------------------------------------------------------------------

@pytest.fixture
def stranded(db, tokens, group_map, objects):
    github = StrandedGitHub()
    metered = Metered(github, reset=_later())
    seed_tenant(db, "eng")
    _register(db, "eng", OWNER, NAME)
    tokens = dict(tokens)
    tokens["token-sweeper"] = {"email": SWEEPER, "email_verified": True, "sub": "sub-sweeper"}
    context = build_context(
        settings=api_settings(rollup_sweeper_users=(SWEEPER,)), db=db,
        verifier=StaticTokenVerifier(tokens), groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=objects, forge_tokens=forge_fakes.AnyTenantTokens(),
        forge_writer=forgewrite.GitHubWriter(send=metered),
    )
    return TestClient(create_app(context), raise_server_exceptions=False), github, metered


def _sweep(client: TestClient, **kw: Any) -> dict:
    response = client.post("/v1/admin/stranded-prs/sweep?tenant_id=eng", **kw)
    assert response.status_code == 200, response.text
    return response.json()


def test_a_low_quota_defers_the_sweep_and_keeps_what_it_stored(db, stranded):
    client, github, metered = stranded
    github.add(7)
    _opener(db, 7)
    github.green(_sha(7))
    first = _sweep(client, headers=SWEEPER_HEADERS)
    assert [row["number"] for row in first["stranded"]] == [7]
    stored = json.dumps(db.docs["stranded_prs/eng"], default=str, sort_keys=True)
    calls = len(metered.paths)

    QUOTAS.observe(tenant_credential("eng"), _limits(10, reset=_later()))
    body = _sweep(client, headers=SWEEPER_HEADERS)

    assert len(metered.paths) == calls
    assert body["deferred"] == [REPO]
    # Not read is not "nothing stranded": the stored rows stand.
    assert json.dumps(db.docs["stranded_prs/eng"], default=str, sort_keys=True) == stored


def test_an_admins_redrive_is_never_deferred(db, stranded):
    client, github, metered = stranded
    github.add(7)
    _opener(db, 7)
    QUOTAS.observe(tenant_credential("eng"), _limits(10, reset=_later()))

    body = _sweep(client, headers=auth_header("root"), json={"redrive": True})
    assert body["deferred"] == [] and metered.paths


# --------------------------------------------------------------------------
# 2-3. the index poll's read
# --------------------------------------------------------------------------

def _tenant() -> Tenant:
    return Tenant(tenant_id="eng", kind="group", principal="eng@saga.xyz",
                  created_at=datetime.now(timezone.utc))


RECORD = {"owner": OWNER, "repo": NAME, "default_branch": "main"}


def _poll(probe, **kw: Any):
    return read_head_if_changed(RECORD, _tenant(), etag=None, known_sha=None,
                                tokens=forge_fakes.AnyTenantTokens(),
                                forge=forge.GitHubIssues(probe_send=probe), **kw)


def test_the_poll_read_keeps_the_quota_and_a_rate_limit_is_not_no_access():
    reset = _later()
    _poll(lambda url, headers, timeout: HttpAnswer(200, _limits(2500, reset=reset),
                                                   b"a" * 40))
    assert QUOTAS.quota(tenant_credential("eng")).remaining == 2500

    with pytest.raises(forge.IssueReadFailed):
        _poll(lambda url, headers, timeout: HttpAnswer(403, _limits(0, reset=reset),
                                                       RATE_LIMITED))


def test_the_poll_sends_nothing_while_its_credential_is_low():
    sent: list[str] = []

    def probe(url, headers, timeout):
        sent.append(url)
        return HttpAnswer(200, {}, b"a" * 40)

    QUOTAS.observe(tenant_credential("eng"), _limits(3, reset=_later()))
    with pytest.raises(forge_limits.QuotaDeferred):
        _poll(probe, defer_percent=20, now=datetime.now(timezone.utc))
    assert sent == []
    # Without a threshold (a person's read) it is sent.
    _poll(probe)
    assert len(sent) == 1


# --------------------------------------------------------------------------
# 2-5. forgeperms
# --------------------------------------------------------------------------

class LimitedGitHub(FakeGitHub):
    def __init__(self) -> None:
        super().__init__()
        self.limited: set[str] = set()

    def __call__(self, method, url, headers, body, timeout) -> HttpAnswer:
        answer = super().__call__(method, url, headers, body, timeout)
        if url.split("api.github.com", 1)[1] in self.limited:
            return HttpAnswer(403, _limits(0, reset=_later()), RATE_LIMITED)
        return answer


def _service(github: FakeGitHub, clock: list[float]) -> ForgePermissions:
    jwt = "jwt." + secrets.token_hex(12)
    return ForgePermissions(app_jwt=lambda: jwt, app_slug="swarmcloud-saga", send=github,
                            clock=lambda: clock[0])


def test_a_rate_limited_token_probe_is_unmeasured_not_refused():
    github = LimitedGitHub()
    _app(github)
    _token_repo(github, role={"push": True}, status=200)
    github.limited.add("/repos/example-org/api")
    answer = _service(github, [0.0]).read(
        "eng", repositories=lambda: [_repo("example-org", "api")], token=_value,
        now=datetime.now(timezone.utc), is_admin=True)
    row = answer["repositories"][0]
    assert set(row["capabilities"].values()) == {None}
    assert row["blocked_by"] == [] and "limiting" in row["error"]


def test_the_consoles_read_serves_the_last_answer_while_the_token_is_low_and_refresh_reads():
    github = LimitedGitHub()
    _app(github)
    _token_repo(github, role={"push": True}, status=200)
    clock = [0.0]
    service = _service(github, clock)
    repos = [_repo("example-org", "api")]
    now = datetime.now(timezone.utc)

    def read(**kw: Any) -> dict:
        return service.read("eng", repositories=lambda: list(repos), token=_value, now=now,
                            is_admin=True, defer_percent=20, **kw)

    read()
    calls = len(github.calls)
    QUOTAS.observe(tenant_credential("eng"), _limits(50, reset=_later()))
    clock[0] = 600.0
    again = read()
    assert again["deferred"] is True and again["cached"] is True
    assert len(github.calls) == calls
    # Refresh is a person pressing it: never deferred.
    read(refresh=True)
    assert len(github.calls) > calls


# --------------------------------------------------------------------------
# 6. the setting
# --------------------------------------------------------------------------

def test_the_threshold_is_a_setting_defaulting_to_twenty(monkeypatch):
    assert ApiSettings.__dataclass_fields__["forge_quota_defer_percent"].default == 20
    assert forge_limits.DEFAULT_DEFER_PERCENT == 20
    monkeypatch.setenv("FORGE_QUOTA_DEFER_PERCENT", "35")
    assert settings_module._percent("FORGE_QUOTA_DEFER_PERCENT", 20) == 35
    monkeypatch.setenv("FORGE_QUOTA_DEFER_PERCENT", "150")
    with pytest.raises(ValueError):
        settings_module._percent("FORGE_QUOTA_DEFER_PERCENT", 20)
    assert forge_limits.defer_percent(SimpleNamespace(
        settings=SimpleNamespace(forge_quota_defer_percent=35))) == 35
    assert forge_limits.defer_percent(SimpleNamespace()) == 20
