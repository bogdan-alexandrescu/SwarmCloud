"""A declared Postgres runs for the whole attempt, as the worker's child, on loopback only (TS-RUN, #1627).

docs/design/test-services.md §3.1, §4.1, §5.4, §5.5 and §7's TS-RUN row,
owner decisions of 2026-10-23 (§10): option A, the PostgreSQL 16 binaries
in `agent-runtime-base` started by the worker -- never the agent, never a
command from the repository -- after the clone, up for the whole attempt,
stopped and its data removed at the attempt's end on every exit (finish,
park, cancel, fence).

Every test here uses a FAKE runner: no real Postgres, no process, no
socket. The row's list, each assertion carrying the mutation it catches:
`initdb` then `postgres` with the catalogue's argv and the data under the
cache root; the child's `oom_score_adj` is 1000; not ready in the bound is
an error with scrubbed log lines; stop removes the data directory; nothing
listens on a non-loopback address.

TS-CLOUDRUN-REFUSE (BGD 2026-10-24, "Refuse on Cloud Run"): with no
SWARM_CACHE_ROOT the data directory would sit in the workspace tmpfs,
charged to the container's memory, so `start` refuses before `initdb`. Every
other test here runs with it set, as on GKE (`_cache_volume`).
"""

from __future__ import annotations

import ipaddress
import secrets
import signal
import subprocess
from pathlib import Path

import pytest

from agent_worker import testservices
from agent_worker.testservices import Declaration, DeclaredService

PG16 = DeclaredService(name="postgres", version=16, databases=("app_test", "other_test"))
DECLARED = Declaration(present=True, services=(PG16,))
BIN = "/usr/lib/postgresql/16/bin"


class FakeChild:
    def __init__(self, pid: int, *, exits_with: int | None = None, ignores_sigint: bool = False):
        self.pid = pid
        self.returncode: int | None = exits_with
        self.signals: list[int] = []
        self.killed = False
        self.ignores_sigint = ignores_sigint

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        if not self.ignores_sigint:
            self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("postgres", timeout or 0)
        return self.returncode


@pytest.fixture(autouse=True)
def _cache_volume(tmp_path, monkeypatch):
    """The GKE worker: its cache on a volume of its own."""
    root = tmp_path / "cache-volume"
    root.mkdir()
    monkeypatch.setenv("SWARM_CACHE_ROOT", str(root))
    return root


class FakeRunner:
    """Records every call in order; `ready_after` probes answer 0."""

    def __init__(
        self,
        *,
        ready_after: int | None = 1,
        log_text: str = "",
        oom_error: OSError | None = None,
        initdb_status: int = 0,
        create_output: str = "",
        child: FakeChild | None = None,
    ):
        self.calls: list[tuple[str, list[str]]] = []
        self.envs: list[dict[str, str]] = []
        self.stdin: list[str | None] = []
        self.oom: list[tuple[int, int]] = []
        self.ready_after = ready_after
        self.probes = 0
        self.log_text = log_text
        self.oom_error = oom_error
        self.initdb_status = initdb_status
        self.create_output = create_output
        self.child = child or FakeChild(4242)
        self.data_seen: list[Path] = []

    def run(self, argv, *, env, timeout, stdin=None):
        self.calls.append(("run", list(argv)))
        self.envs.append(dict(env))
        self.stdin.append(stdin)
        name = Path(argv[0]).name
        if name == "initdb":
            data = Path(argv[argv.index("-D") + 1])
            if self.initdb_status == 0:
                data.mkdir(mode=0o700, parents=True)
                (data / "PG_VERSION").write_text("16\n")
            return self.initdb_status, "initdb output"
        if name == "pg_isready":
            self.probes += 1
            if self.ready_after is not None and self.probes >= self.ready_after:
                return 0, "accepting connections"
            return 2, "no response"
        return 0, self.create_output

    def spawn(self, argv, *, env, log):
        self.calls.append(("spawn", list(argv)))
        self.envs.append(dict(env))
        Path(log).write_text(self.log_text)
        return self.child

    def set_oom_score_adj(self, pid, value):
        if self.oom_error is not None:
            raise self.oom_error
        self.oom.append((pid, value))


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _start(tmp_path: Path, runner: FakeRunner, *, scrub=lambda s: s, clock=None):
    clock = clock or FakeClock()
    return testservices.start(
        DECLARED, tmp_path / "cache", scrub=scrub, runner=runner, clock=clock, sleep=clock.sleep
    )


def _names(runner: FakeRunner) -> list[tuple[str, str]]:
    return [(kind, Path(argv[0]).name) for kind, argv in runner.calls]


def test_initdb_then_postgres_with_the_catalogue_argv_and_the_data_under_the_cache_root(tmp_path):
    runner = FakeRunner()
    running = _start(tmp_path, runner)
    root = tmp_path / "cache" / "services" / "postgres"
    data = root / "data"

    assert running.errors() == []
    # MUTATION: start `postgres` before `initdb`, or skip the database step.
    assert _names(runner)[:3] == [("run", "initdb"), ("run", "postgres"), ("spawn", "postgres")]
    initdb = runner.calls[0][1]
    # MUTATION: a binary from the PATH, or another major's directory.
    assert initdb[0] == f"{BIN}/initdb"
    # MUTATION: the data anywhere but `<cache>/services/postgres/data`.
    assert initdb[initdb.index("-D") + 1] == str(data)
    # MUTATION: password authentication (a credential would exist) or no user.
    assert "--auth=trust" in initdb and "--username=postgres" in initdb

    server = runner.calls[2][1]
    assert server[0] == f"{BIN}/postgres"
    assert server[server.index("-D") + 1] == str(data)
    settings = [server[i + 1] for i, arg in enumerate(server) if arg == "-c"]
    # MUTATION: drop or change any of §3.1's memory or durability settings.
    for setting in (
        "port=5432",
        "shared_buffers=64MB",
        "work_mem=4MB",
        "maintenance_work_mem=32MB",
        "max_connections=50",
        "max_wal_size=256MB",
        "fsync=off",
        "synchronous_commit=off",
        "full_page_writes=off",
        f"unix_socket_directories={root / 'run'}",
        "unix_socket_permissions=0700",
    ):
        assert setting in settings, setting
    # MUTATION: the data on the workspace (it would be checkpointed) or in /tmp.
    assert data.is_relative_to(tmp_path / "cache")
    assert (root / "run").stat().st_mode & 0o777 == 0o700


def test_the_declared_databases_are_created_before_the_server_listens(tmp_path):
    runner = FakeRunner()
    _start(tmp_path, runner)
    single = runner.calls[1][1]
    # MUTATION: create them over a network connection, or not at all.
    assert single[:2] == [f"{BIN}/postgres", "--single"]
    script = runner.stdin[1] or ""
    # MUTATION: create only the first database.
    assert 'CREATE DATABASE "app_test";' in script
    assert 'CREATE DATABASE "other_test";' in script


def test_a_database_initdb_already_made_is_not_created_again(tmp_path):
    runner = FakeRunner()
    declared = Declaration(
        present=True,
        services=(DeclaredService(name="postgres", version=16, databases=("postgres", "x")),),
    )
    clock = FakeClock()
    testservices.start(
        declared, tmp_path / "cache", scrub=lambda s: s, runner=runner, clock=clock, sleep=clock.sleep
    )
    script = runner.stdin[1] or ""
    # MUTATION: CREATE DATABASE "postgres" would fail the single-user step.
    assert 'CREATE DATABASE "postgres"' not in script
    assert 'CREATE DATABASE "x";' in script


def test_a_failed_database_creation_is_an_error_and_nothing_is_left_running(tmp_path):
    runner = FakeRunner(create_output='ERROR:  database "x" already exists')
    running = _start(tmp_path, runner)
    # MUTATION: single-user mode exits 0 on an ERROR; read its output too.
    assert running.errors() and "database" in running.errors()[0]
    assert not any(kind == "spawn" for kind, _ in runner.calls)
    assert not (tmp_path / "cache" / "services" / "postgres").exists()


def test_the_child_oom_score_adj_is_1000(tmp_path):
    runner = FakeRunner(child=FakeChild(31337))
    _start(tmp_path, runner)
    # MUTATION: write 0, -1000, the worker's own pid, or nothing at all.
    assert runner.oom == [(31337, 1000)]
    assert testservices.OOM_SCORE_ADJ == 1000


def test_the_oom_rank_is_set_before_readiness_is_awaited(tmp_path):
    order: list[str] = []
    runner = FakeRunner()
    real_run, real_oom = runner.run, runner.set_oom_score_adj

    def run(argv, **kw):
        order.append(Path(argv[0]).name)
        return real_run(argv, **kw)

    def oom(pid, value):
        order.append("oom")
        real_oom(pid, value)

    runner.run, runner.set_oom_score_adj = run, oom
    _start(tmp_path, runner)
    # MUTATION: rank it only after it is ready; it allocates before that.
    assert order.index("oom") < order.index("pg_isready")


def test_a_service_that_cannot_be_ranked_for_the_oom_killer_is_not_run(tmp_path):
    child = FakeChild(77)
    runner = FakeRunner(oom_error=PermissionError("denied"), child=child)
    running = _start(tmp_path, runner)
    # MUTATION: carry on with a server the kernel would rank beside the agent.
    assert running.errors() and "oom_score_adj" in running.errors()[0]
    assert child.poll() is not None
    assert not (tmp_path / "cache" / "services" / "postgres").exists()


def test_not_ready_within_the_bound_is_an_error_with_scrubbed_log_lines(tmp_path):
    planted = "tok-" + secrets.token_hex(16)
    runner = FakeRunner(
        ready_after=None,
        log_text=f"LOG:  starting\nFATAL:  could not bind: {planted}\n",
    )
    clock = FakeClock()
    running = _start(
        tmp_path, runner, scrub=lambda s: s.replace(planted, "[REDACTED]"), clock=clock
    )
    [error] = running.errors()
    # MUTATION: wait forever, or past §3.1's 30 s bound.
    assert clock.now <= testservices.READY_TIMEOUT_SECONDS + testservices.READY_INTERVAL_SECONDS
    assert testservices.READY_TIMEOUT_SECONDS == 30
    # MUTATION: report "not ready" without the server's own last lines.
    assert "could not bind" in error and "ready" in error
    # MUTATION: pass the log lines through unscrubbed.
    assert planted not in error and "[REDACTED]" in error
    # A failed start leaves nothing listening and no data behind.
    assert runner.child.poll() is not None
    assert not (tmp_path / "cache" / "services" / "postgres").exists()


def test_readiness_is_probed_on_the_socket_every_interval(tmp_path):
    runner = FakeRunner(ready_after=5)
    clock = FakeClock()
    running = _start(tmp_path, runner, clock=clock)
    assert running.errors() == []
    probe = [argv for kind, argv in runner.calls if Path(argv[0]).name == "pg_isready"][0]
    root = tmp_path / "cache" / "services" / "postgres"
    # MUTATION: probe over TCP, or a binary from the PATH.
    assert probe[0] == f"{BIN}/pg_isready"
    assert probe[probe.index("-h") + 1] == str(root / "run")
    # MUTATION: a sleep between probes longer than §3.1's 100 ms.
    assert runner.probes == 5
    assert clock.now == pytest.approx(4 * testservices.READY_INTERVAL_SECONDS)


def test_a_server_that_exits_before_ready_is_an_error_at_once(tmp_path):
    runner = FakeRunner(
        ready_after=None, log_text="FATAL:  lock file exists\n", child=FakeChild(9, exits_with=1)
    )
    clock = FakeClock()
    running = _start(tmp_path, runner, clock=clock)
    [error] = running.errors()
    # MUTATION: keep probing a dead server for the whole bound.
    assert clock.now < 1
    assert "lock file exists" in error and "exited" in error


def test_a_failed_initdb_is_an_error_with_its_output_and_starts_no_server(tmp_path):
    runner = FakeRunner(initdb_status=1)
    running = _start(tmp_path, runner)
    [error] = running.errors()
    # MUTATION: start the server over a data directory initdb never made.
    assert "initdb" in error and "initdb output" in error
    assert not any(kind == "spawn" for kind, _ in runner.calls)


def test_stop_removes_the_data_directory_and_stops_the_server_fast(tmp_path):
    runner = FakeRunner()
    running = _start(tmp_path, runner)
    root = tmp_path / "cache" / "services" / "postgres"
    assert (root / "data" / "PG_VERSION").exists()
    running.stop()
    # MUTATION: SIGTERM (a "smart" shutdown waits for every test client).
    assert runner.child.signals == [signal.SIGINT]
    # MUTATION: stop the process and leave the data on the cache disk.
    assert not root.exists()
    # Twice is harmless: `_cleanup` runs on every exit, and a park may stop first.
    running.stop()
    assert runner.child.signals == [signal.SIGINT]


def test_stop_kills_a_server_that_ignores_the_fast_shutdown(tmp_path):
    child = FakeChild(5, ignores_sigint=True)
    runner = FakeRunner(child=child)
    running = _start(tmp_path, runner)
    running.stop()
    # MUTATION: wait on it forever, or leave it running past the attempt.
    assert child.killed
    assert not (tmp_path / "cache" / "services" / "postgres").exists()


def test_nothing_listens_on_a_non_loopback_address(tmp_path):
    runner = FakeRunner()
    _start(tmp_path, runner)
    server = runner.calls[2][1]
    settings = [server[i + 1] for i, arg in enumerate(server) if arg == "-c"]
    listen = [s.split("=", 1)[1] for s in settings if s.startswith("listen_addresses=")]
    # MUTATION: `listen_addresses=*`, `0.0.0.0`, or leaving it to postgresql.conf.
    assert listen == ["127.0.0.1"]
    assert all(ipaddress.ip_address(a).is_loopback for a in listen[0].split(","))
    # MUTATION: `-h` or `-i` on the command line would override the setting.
    assert "-h" not in server and "-i" not in server
    assert testservices.LISTEN_HOST == "127.0.0.1" and testservices.LISTEN_PORT == 5432


def test_a_non_loopback_listen_address_is_refused_before_anything_starts(tmp_path, monkeypatch):
    monkeypatch.setattr(testservices, "LISTEN_HOST", "0.0.0.0")
    runner = FakeRunner()
    running = _start(tmp_path, runner)
    # MUTATION: drop the guard; a catalogue edit would then expose the server.
    assert running.errors() and "loopback" in running.errors()[0]
    assert runner.calls == []


def test_the_service_runs_with_none_of_the_worker_environment(tmp_path, monkeypatch):
    planted = "sk-" + secrets.token_hex(20)
    monkeypatch.setenv("ANTHROPIC_API_KEY", planted)
    monkeypatch.setenv("PG_OOM_ADJUST_FILE", "/proc/self/oom_score_adj")
    runner = FakeRunner()
    _start(tmp_path, runner)
    for env in runner.envs:
        # MUTATION: pass `os.environ` through to the server and its backends.
        assert planted not in env.values()
        # MUTATION: inherit PG_OOM_ADJUST_FILE, which resets every backend's rank.
        assert not any(key.startswith("PG") for key in env)


def test_nothing_declared_starts_nothing(tmp_path):
    runner = FakeRunner()
    running = testservices.start(
        testservices.NOTHING_DECLARED, tmp_path / "cache", scrub=lambda s: s, runner=runner
    )
    # MUTATION: start a default Postgres for every repository.
    assert runner.calls == [] and running.services == () and running.errors() == []
    running.stop()


def test_an_invalid_declaration_starts_nothing(tmp_path):
    invalid = Declaration(
        present=True, refusals=(testservices.Refusal("postgres.image", "refused"),)
    )
    runner = FakeRunner()
    running = testservices.start(invalid, tmp_path / "cache", scrub=lambda s: s, runner=runner)
    # MUTATION: start the services of a declaration that carries refusals.
    assert runner.calls == [] and running.services == ()


def test_a_running_server_reports_alive_and_a_dead_one_its_exit(tmp_path):
    runner = FakeRunner()
    running = _start(tmp_path, runner)
    [service] = running.services
    assert service.exit_status() is None
    runner.child.returncode = -9
    # MUTATION: report a server the OOM killer took as still running.
    assert service.exit_status() == -9


NO_CACHE_VOLUME = (
    "test services need SWARM_CACHE_ROOT on its own volume; "
    "this runner keeps its workspace in memory"
)


@pytest.mark.parametrize("value", [None, "", "  "])
def test_without_a_cache_volume_start_refuses_and_runs_no_initdb(tmp_path, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("SWARM_CACHE_ROOT", raising=False)
    else:
        monkeypatch.setenv("SWARM_CACHE_ROOT", value)
    runner = FakeRunner()
    running = _start(tmp_path, runner)
    # MUTATION: initdb into the Cloud Run workspace tmpfs, charged to memory.
    assert runner.calls == []
    [service] = running.services
    # MUTATION: refuse silently, or with a reason that does not name the volume.
    assert service.error == NO_CACHE_VOLUME
    assert running.errors() == [NO_CACHE_VOLUME]
    # MUTATION: leave a data directory behind in the workspace.
    assert not (tmp_path / "cache" / "services").exists()
    running.stop()


def test_the_refusal_reads_the_environment_it_is_given(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_CACHE_ROOT", raising=False)
    runner = FakeRunner()
    clock = FakeClock()
    running = testservices.start(
        DECLARED,
        tmp_path / "cache",
        scrub=lambda s: s,
        runner=runner,
        clock=clock,
        sleep=clock.sleep,
        environ={"SWARM_CACHE_ROOT": str(tmp_path)},
    )
    # MUTATION: decide by a hard-coded profile name rather than the environment.
    assert running.errors() == [] and _names(runner)[0] == ("run", "initdb")
    running.stop()


def test_with_a_cache_volume_start_proceeds_as_before(tmp_path, _cache_volume):
    runner = FakeRunner()
    running = _start(tmp_path, runner)
    # MUTATION: refuse on GKE too, where the data sits on the cache volume.
    assert running.errors() == []
    assert [name for _, name in _names(runner)][:2] == ["initdb", "postgres"]
    running.stop()


def test_the_pre_publish_report_shows_the_refusal_as_not_started(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_CACHE_ROOT", raising=False)
    running = _start(tmp_path, FakeRunner())
    # MUTATION: report the refused service as running, or drop its reason.
    assert running.report() == [f"postgres: service not started: {NO_CACHE_VOLUME}"]


def test_the_pre_publish_report_names_every_service_that_did_not_start(tmp_path):
    started = _start(tmp_path / "up", FakeRunner())
    # MUTATION: report a running service as not started.
    assert started.report() == []
    started.stop()
    failed = _start(tmp_path / "down", FakeRunner(initdb_status=1))
    [line] = failed.report()
    # MUTATION: report TS-RUN's own start failures differently from the refusal.
    assert line.startswith("postgres: service not started: the declared postgres did not start")


def test_the_worker_logs_the_refusal_as_not_started(tmp_path, monkeypatch):
    from agent_worker.lifecycle import Worker

    monkeypatch.delenv("SWARM_CACHE_ROOT", raising=False)
    runner = FakeRunner()
    real_start = testservices.start
    monkeypatch.setattr(
        testservices,
        "start",
        lambda declaration, cache, *, scrub: real_start(
            declaration, cache, scrub=scrub, runner=runner
        ),
    )
    stub = _worker_stub(tmp_path, '[postgres]\nversion = 16\ndatabases = ["app_test"]\n')
    Worker._start_test_services(stub)
    assert runner.calls == []
    warnings = [kw for level, _, kw in stub.log.lines if level == "warning"]
    # MUTATION: log the refusal as something other than a service not started.
    assert [w.get("report") for w in warnings] == [
        f"postgres: service not started: {NO_CACHE_VOLUME}"
    ]


def test_the_worker_starts_the_service_after_the_clone_and_stops_it_in_cleanup():
    """§5.4/§5.5: the one call site. Started once the clone is on disk, and
    stopped in `_cleanup`, the `finally` of `run()` that every exit reaches:
    finish, park, cancel and fence alike."""
    source = (
        Path(__file__).resolve().parents[3] / "apps/agent-worker/agent_worker/lifecycle.py"
    ).read_text()
    clone = source.index("repo_info = self._clone_keeping_lease(task)")
    start = source.index("self._start_test_services()")
    # MUTATION: start it before the clone, when no declaration can be read.
    assert clone < start
    cleanup = source.index("    def _cleanup(self) -> None:")
    stop = source.index('("stop_test_services", self._stop_test_services)')
    destroy = source.index('("destroy_workspace", destroy_workspace)')
    # MUTATION: stop it only on success, or after the cache it writes to is gone.
    assert cleanup < stop < destroy


class _Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str, dict]] = []

    def info(self, msg, **kw):
        self.lines.append(("info", msg, kw))

    def warning(self, msg, **kw):
        self.lines.append(("warning", msg, kw))


class _Phases:
    def __init__(self) -> None:
        self.entered: list[str] = []

    def enter(self, name, **kw):
        self.entered.append(name)


def _worker_stub(tmp_path: Path, toml: str | None):
    import contextlib
    import types

    from agent_worker import workspace as workspace_mod

    work = tmp_path / "attempt" / "work"
    repo = work / workspace_mod.REPO_DIR_NAME
    repo.mkdir(parents=True)
    if toml is not None:
        (repo / ".swarm").mkdir()
        (repo / ".swarm" / "test-services.toml").write_text(toml)
    beats: list[str] = []

    @contextlib.contextmanager
    def meanwhile(what):
        beats.append(what)
        yield

    return types.SimpleNamespace(
        ws=types.SimpleNamespace(work=work, cache=tmp_path / "cache"),
        log=_Log(),
        phases=_Phases(),
        _heartbeat_meanwhile=meanwhile,
        _scrub=lambda value: value,
        _test_services=None,
        beats=beats,
    )


def test_the_worker_starts_what_the_clone_declares_under_its_cache(tmp_path, monkeypatch):
    from agent_worker.lifecycle import Worker

    seen: list[tuple[Declaration, Path]] = []

    def fake_start(declaration, cache, *, scrub):
        seen.append((declaration, cache))
        return testservices.RunningServices(
            services=(testservices.ServiceProcess(service=PG16, root=cache, ready_seconds=1.0),)
        )

    monkeypatch.setattr(testservices, "start", fake_start)
    stub = _worker_stub(tmp_path, '[postgres]\nversion = 16\ndatabases = ["app_test"]\n')
    Worker._start_test_services(stub)
    # MUTATION: read the declaration from anywhere but the checkout, or not start.
    assert [(d.services[0].databases, c) for d, c in seen] == [(("app_test",), tmp_path / "cache")]
    # MUTATION: hold the lease silent through a 30 s ready wait.
    assert stub.beats == ["test_services"]
    assert stub._test_services is not None


def test_the_worker_starts_nothing_without_a_valid_declaration(tmp_path, monkeypatch):
    from agent_worker.lifecycle import Worker

    def refuse(*a, **kw):
        raise AssertionError("started a service the clone does not validly declare")

    monkeypatch.setattr(testservices, "start", refuse)
    for toml in (None, '[postgres]\nversion = 16\ndatabases = ["x"]\nimage = "evil"\n'):
        stub = _worker_stub(tmp_path / str(toml is None), toml)
        # MUTATION: start a default service, or the services of an invalid file.
        Worker._start_test_services(stub)
        assert stub._test_services is None


def test_a_service_that_fails_never_ends_the_attempt(tmp_path, monkeypatch):
    from agent_worker.lifecycle import Worker

    def broken(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(testservices, "start", broken)
    stub = _worker_stub(tmp_path, '[postgres]\nversion = 16\ndatabases = ["x"]\n')
    # MUTATION: let a test service's failure raise out of the attempt.
    Worker._start_test_services(stub)
    assert any(level == "warning" for level, _, _ in stub.log.lines)


def test_the_worker_cleanup_stops_the_services_once(tmp_path):
    from agent_worker.lifecycle import Worker
    import types

    stopped: list[int] = []
    running = types.SimpleNamespace(stop=lambda: stopped.append(1))
    stub = types.SimpleNamespace(_test_services=running)
    Worker._stop_test_services(stub)
    Worker._stop_test_services(stub)
    # MUTATION: leave the server running, or stop it on a second cleanup too.
    assert stopped == [1] and stub._test_services is None
