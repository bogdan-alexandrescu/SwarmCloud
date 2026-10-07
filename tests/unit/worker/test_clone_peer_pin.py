"""Which address the probe reached, which one git reached, and the pin (#721, P27).

MEASURED 2026-10-06 (chunk-3 observer, P27): #734's egress probe connected
11.7-39.6 s after process start, and git's own connect still stalled in 5 of
7 clones (7.1-35.6 s). Nothing said whether git was connecting to the address
the probe had just proved reachable. Owner decision 2026-10-06:

* every clone try records `probe_peer` (the probe's `getpeername()`) and
  `git_peer` (curl's `Connected to <host> (<ip>)` in the clone's own trace);
* when git's connect STALLED and `git_peer` differs from `probe_peer`, the
  clone's next try is pinned to the probe's address with
  `git -c http.curloptResolve=<host>:443:<ip>` -- the URL keeps its host name,
  so TLS is still verified against it; a probe that failed never pins;
* whether a try was pinned is recorded.

Offline: every connect and every git step is a stand-in. The addresses are
from the documentation ranges (RFC 5737, RFC 3849).
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from agent_worker import egress, gitops, lifecycle
from agent_worker.errors import ExitCode
from agent_worker.logs import build_logger

from worker_seeds import seed_attempt

GITHUB = ("github.com", 443)
URL = "https://github.com/octo/widgets.git"
PROBE_IP = "192.0.2.10"
OTHER_IP = "192.0.2.20"
PIN = f"http.curloptResolve=github.com:443:{PROBE_IP}"


def _logger():
    return build_logger(
        task_id="task_1", attempt_id="att_1", tenant_id="eng", generation=1,
        runner_profile="mock", stream=io.StringIO(),
    )


class _Conn:
    def __init__(self, ip: str | None) -> None:
        self.ip = ip

    def getpeername(self):
        if self.ip is None:
            raise OSError("not connected")
        return (self.ip, 443)

    def close(self) -> None:
        pass


def _answered_probe(ip: str | None = PROBE_IP) -> egress.EgressProbe:
    probe = egress.EgressProbe(
        [GITHUB], connect=lambda target, timeout=None: _Conn(ip), interval_seconds=0.0,
    )
    probe.start()
    assert probe.wait(GITHUB, timeout=5) is True
    probe.stop()
    return probe


def _failed_probe() -> egress.EgressProbe:
    def refuse(target, timeout=None):
        raise ConnectionRefusedError()

    probe = egress.EgressProbe([GITHUB], connect=refuse, interval_seconds=0.0, cap_seconds=0.05)
    probe.start()
    assert probe.wait(GITHUB, timeout=5) is False
    probe.stop()
    return probe


# ---------------------------------------------------------------------------
# the probe's peer
# ---------------------------------------------------------------------------


def test_the_probe_records_the_address_it_reached():
    probe = _answered_probe()

    assert probe.peer(GITHUB) == PROBE_IP
    (target,) = probe.result()["targets"]
    assert target["peer"] == PROBE_IP


def test_a_probe_that_never_answered_has_no_peer():
    probe = _failed_probe()

    assert probe.peer(GITHUB) is None
    assert probe.result()["targets"][0]["peer"] is None


def test_a_connection_that_cannot_say_its_peer_still_counts_as_answered():
    """A connect that succeeded is the measurement; a missing peer is not a failure."""
    probe = _answered_probe(ip=None)

    assert probe.result()["targets"][0]["ready"] is True
    assert probe.peer(GITHUB) is None


# ---------------------------------------------------------------------------
# git's peer, from the trace the clone already parses
# ---------------------------------------------------------------------------

TOP = "20261006T100000.000000Z-H00000001-P00000001"


def _events() -> str:
    def event(kind: str, time: str, **fields: Any) -> str:
        return json.dumps({"event": kind, "sid": TOP, "time": f"2026-10-06T{time}Z", **fields})

    return "\n".join([
        event("start", "10:00:00.000000", argv=["git", "clone"]),
        event("exit", "10:00:40.000000", code=0),
    ]) + "\n"


def _wire(ip: str, *, connect_at: str = "10:00:01.000000", connected: bool = True) -> str:
    def line(clock: str, message: str) -> str:
        return f"{clock} http.c:756{' ' * 20}{message}"

    lines = [line("10:00:00.200000", f"== Info:   Trying {ip}:443...")]
    if connected:
        lines.append(line(connect_at, f"== Info: Connected to github.com ({ip}) port 443 (#0)"))
    return "\n".join(lines) + "\n"


def test_git_peer_is_the_address_curl_says_it_connected_to():
    phases = gitops.clone_phase_timings(_events(), _wire(OTHER_IP))

    assert phases["git_peer"] == OTHER_IP


def test_an_ipv6_git_peer_is_read_too():
    phases = gitops.clone_phase_timings(_events(), _wire("2001:db8::1"))

    assert phases["git_peer"] == "2001:db8::1"


def test_a_connected_line_whose_parenthesis_is_no_address_gives_no_peer():
    wire = _wire(OTHER_IP).replace(f"({OTHER_IP})", "(not-an-address)")

    assert "git_peer" not in gitops.clone_phase_timings(_events(), wire)


# ---------------------------------------------------------------------------
# the pin decision
# ---------------------------------------------------------------------------


def _stalled(git_peer: str | None, seconds: float = 7.1) -> dict[str, Any]:
    phases: dict[str, Any] = {"connect_seconds": seconds, "connect_tries": 1}
    if git_peer is not None:
        phases["git_peer"] = git_peer
    return phases


def test_a_stalled_connect_to_another_address_pins_the_next_try():
    peers = gitops.PeerPin(_answered_probe(), URL)

    first = peers.observe(_stalled(OTHER_IP))

    assert first == {"probe_peer": PROBE_IP, "git_peer": OTHER_IP, "peer_pinned": False}
    assert peers.config_args() == ["-c", PIN]
    assert peers.observe({"connect_seconds": 0.05, "git_peer": PROBE_IP})["peer_pinned"] is True


def test_a_stalled_connect_to_the_probes_address_does_not_pin():
    peers = gitops.PeerPin(_answered_probe(), URL)

    peers.observe(_stalled(PROBE_IP))

    assert peers.config_args() == []


def test_a_quick_connect_to_another_address_does_not_pin():
    peers = gitops.PeerPin(_answered_probe(), URL)

    peers.observe(_stalled(OTHER_IP, seconds=0.08))

    assert peers.config_args() == []


def test_a_connect_that_never_landed_pins_to_the_probes_address():
    """curl tried and never said `Connected to`: git reached nothing, the probe
    reached its address -- they differ."""
    peers = gitops.PeerPin(_answered_probe(), URL)

    record = peers.observe({"connect_tries": 2})

    assert record["git_peer"] is None
    assert peers.config_args() == ["-c", PIN]


def test_a_failed_probe_never_pins():
    peers = gitops.PeerPin(_failed_probe(), URL)

    record = peers.observe(_stalled(OTHER_IP, seconds=35.6))

    assert record == {"probe_peer": None, "git_peer": OTHER_IP, "peer_pinned": False}
    assert peers.config_args() == []


def test_no_probe_never_pins():
    peers = gitops.PeerPin(None, URL)

    peers.observe(_stalled(OTHER_IP))

    assert peers.config_args() == []


def test_an_ssh_clone_records_the_probe_peer_and_is_never_pinned():
    """`http.curloptResolve` is curl's; an ssh clone never goes through curl."""
    probe = egress.EgressProbe(
        [("github.com", 22)], connect=lambda target, timeout=None: _Conn(PROBE_IP),
        interval_seconds=0.0,
    )
    probe.start()
    assert probe.wait(("github.com", 22), timeout=5)
    probe.stop()
    peers = gitops.PeerPin(probe, "ssh://git@github.com/octo/widgets.git")

    record = peers.observe({"connect_tries": 1})

    assert record["probe_peer"] == PROBE_IP
    assert peers.config_args() == []


def test_an_ipv6_probe_address_is_bracketed_in_the_pin():
    peers = gitops.PeerPin(_answered_probe(ip="2001:db8::10"), URL)

    peers.observe(_stalled(OTHER_IP))

    assert peers.config_args() == ["-c", "http.curloptResolve=github.com:443:[2001:db8::10]"]


# ---------------------------------------------------------------------------
# through shallow_clone and retry_clone: the next try carries the pin
# ---------------------------------------------------------------------------


def _git_steps(tries: list[dict[str, Any]], outcomes: list[tuple[str, bool]]):
    """`_run_git_steps`, writing the trace each try's git would have written."""

    def run(steps, *, env, **kwargs):
        git_peer, ok = outcomes[len(tries)]
        tries.append({"argv": [list(step) for step in steps]})
        Path(env["GIT_TRACE2_EVENT"]).write_text(_events())
        Path(env["GIT_TRACE_CURL"]).write_text(
            _wire(git_peer, connect_at="10:00:07.300000") if not ok
            else _wire(git_peer, connect_at="10:00:00.250000")
        )
        if not ok:
            raise gitops.GitTransient("fatal: unable to access: Connection timed out")
        return 1.0

    return run


def _clone_twice(monkeypatch, tmp_path: Path, probe, outcomes) -> tuple[list, list, Any]:
    tries: list[dict[str, Any]] = []
    record: list[dict[str, Any]] = []
    monkeypatch.setattr(gitops, "_run_git_steps", _git_steps(tries, outcomes))
    monkeypatch.setattr(gitops, "_read_head", lambda *a, **k: "a" * 40)
    peers = gitops.PeerPin(probe, URL)
    result = gitops.retry_clone(
        lambda: gitops.shallow_clone(
            url=URL, ref=None, destination=tmp_path / "repo", private_dir=tmp_path / "private",
            logs_dir=tmp_path, timeout_seconds=60, logger=_logger(), egress=probe, peers=peers,
        ),
        destination=tmp_path / "repo", max_wait_seconds=45, remaining_seconds=lambda: 3600,
        logger=_logger(), sleep=lambda seconds: None, record=record,
    )
    return tries, record, result


def _pinned(argv: list[list[str]]) -> bool:
    return any(PIN in step for step in argv)


def test_a_stalled_try_to_another_address_pins_the_retry(monkeypatch, tmp_path):
    tries, record, result = _clone_twice(
        monkeypatch, tmp_path, _answered_probe(), [(OTHER_IP, False), (PROBE_IP, True)]
    )

    assert [_pinned(t["argv"]) for t in tries] == [False, True]
    # The URL is unchanged: curl still verifies the certificate against github.com.
    assert URL in tries[1]["argv"][0]
    assert [(t["probe_peer"], t["git_peer"], t["peer_pinned"]) for t in record] == [
        (PROBE_IP, OTHER_IP, False), (PROBE_IP, PROBE_IP, True),
    ]
    assert result.phases["peer_pinned"] is True


def test_a_stalled_try_to_the_probes_address_retries_unpinned(monkeypatch, tmp_path):
    tries, record, _ = _clone_twice(
        monkeypatch, tmp_path, _answered_probe(), [(PROBE_IP, False), (PROBE_IP, True)]
    )

    assert [_pinned(t["argv"]) for t in tries] == [False, False]
    assert [t["peer_pinned"] for t in record] == [False, False]


def test_a_failed_probe_never_pins_the_retry(monkeypatch, tmp_path):
    tries, record, _ = _clone_twice(
        monkeypatch, tmp_path, _failed_probe(), [(OTHER_IP, False), (OTHER_IP, True)]
    )

    assert [_pinned(t["argv"]) for t in tries] == [False, False]
    assert [t["probe_peer"] for t in record] == [None, None]


def test_try_record_names_the_peers_of_a_try_with_no_trace():
    """A try that raised something other than a GitError has no phases at all."""
    entry = gitops.try_record(1.0, error=RuntimeError("boom"))

    assert entry["probe_peer"] is None and entry["git_peer"] is None
    assert entry["peer_pinned"] is False


# ---------------------------------------------------------------------------
# the worker's clone_timed carries them
# ---------------------------------------------------------------------------


def test_clone_timed_carries_both_peers_and_whether_it_pinned(db, worker_factory, monkeypatch):
    seed_attempt(db)
    seen: list[Any] = []

    def clone(**kwargs: Any) -> gitops.CloneResult:
        peers = kwargs["peers"]
        seen.append(peers)
        phases = {"connect_seconds": 0.04, "git_peer": PROBE_IP, **peers.observe(
            {"connect_seconds": 0.04, "git_peer": PROBE_IP}
        )}
        return gitops.CloneResult(
            path=kwargs["destination"], url=kwargs["url"], ref=kwargs["ref"],
            commit="a" * 40, duration_seconds=0.1, phases=phases,
        )

    monkeypatch.setattr(lifecycle, "shallow_clone", clone)
    worker, _, _ = worker_factory(repository_url=URL)
    worker.egress_connect = lambda target, timeout=None: _Conn(PROBE_IP)
    worker.egress_interval_seconds = 0.0

    assert worker.run() == ExitCode.OK

    assert len(seen) == 1 and isinstance(seen[0], gitops.PeerPin)
    (clone_mark,) = [
        e["detail"]["clone"] for e in db.events("task_1")
        if (e.get("detail") or {}).get("cause") == "clone_timed"
    ]
    assert clone_mark["probe_peer"] == PROBE_IP
    assert clone_mark["git_peer"] == PROBE_IP
    assert clone_mark["peer_pinned"] is False
    (egress_mark,) = [
        e["detail"]["egress"] for e in db.events("task_1")
        if (e.get("detail") or {}).get("cause") == "egress_ready"
    ]
    assert egress_mark["targets"][0]["peer"] == PROBE_IP


def test_the_pin_option_is_curls_resolve_list_and_nothing_else():
    """`http.curloptResolve` feeds CURLOPT_RESOLVE: the name still resolves to
    the pinned address for TLS and the Host header; `-c` is per command, so no
    config file is written and nothing outlives the clone."""
    peers = gitops.PeerPin(_answered_probe(), URL)
    peers.observe(_stalled(OTHER_IP))

    flag, value = peers.config_args()
    assert flag == "-c"
    assert value.split("=", 1) == ["http.curloptResolve", f"github.com:443:{PROBE_IP}"]
