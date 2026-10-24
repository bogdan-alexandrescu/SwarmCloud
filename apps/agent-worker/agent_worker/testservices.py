"""The services a repository's tests need, read from `.swarm/test-services.toml` (TS-CATALOGUE, #1627).

WHY (docs/design/test-services.md, owner decisions 2026-10-23, §10). The
pre-publish `graph-pytest` check runs the tests that cover a change, and a
repository whose tests need a database errored at setup on every one of
them: since PR 1606 (#1605) that run is reported `skipped`, so a broken
database test reached CI uncaught (four product-oracle lanes on 2026-10-19,
every test `Connect call failed ('127.0.0.1', 5432)`). So a repository
declares what its tests need, checked in at `DECLARATION_PATH`:

    [postgres]
    version = 16
    databases = ["app_test"]

and the worker runs it (option A: the binaries in `agent-runtime-base`, a
child of the worker, up for the whole attempt after the clone).

A NAME, NEVER AN IMAGE OR A COMMAND (invariant 10). The declaration picks an
entry of `CATALOGUE`, a major version that entry offers, and the names of
the databases to create. Everything else -- `image`, `command`, `args`,
`port`, `env`, `memory`, `cpu`, `settings`, `init_sql`, `user`, any key at
all but `version` and `databases` -- is refused by name: each would let a
repository choose what runs, how big it is, or what the platform's process
does at start. The catalogue is v1's one measured need, PostgreSQL 16 (Q3;
Q6: no Redis until TS-SIGNAL's skipped-address count shows one recurring).
It lives here, in the image that carries the binaries it names, so the two
cannot disagree; it is not the frozen contract (§3.3).

READ AS DATA, ALL REFUSALS AT ONCE (§2.2). `tomllib`, the standard library,
so the worker gains no parser dependency (Q2). An unknown table or key, a
version the catalogue does not offer, a malformed or duplicated database
name, a file over `MAX_DECLARATION_BYTES`, bytes that are not UTF-8 and a
TOML syntax error are each a `Refusal` naming the key and the rule, and all
of them come back together, the way `swarm_common.profiles.check_size`
reports a runner size. A declaration with any refusal declares no service:
an invalid file never starts anything. Whether a refusal is the agent's
`failed` check or the base branch's advisory `error` is the gate's call
(TS-GATE), not this module's.

READING STARTS NOTHING. No process, no socket, and no file outside the
clone: a declaration that resolves outside it (a symlinked file or
`.swarm`) or is not a regular file is refused unread, and the read is
bounded at one byte over the limit.

RUNNING IT (TS-RUN, §3.1, §4.1, §5.4, §5.5). `start` runs a valid
declaration's services for the whole attempt, from the moment the clone is
on disk (Q4), as children of the worker -- never the agent's, never a
command the repository chose:

* `initdb` into `<cache>/services/postgres/data`, on the attempt's cache
  directory (the GKE cache volume): disk, not memory, never checkpointed,
  gone with the pod. `trust` authentication, so no credential exists.
* the declared databases, created in single-user mode BEFORE the server
  listens, so a test never sees the server without them;
* `postgres`, with §3.1's settings and `listen_addresses=127.0.0.1`: loopback
  and a 0700 socket directory, nothing on any other address
  (`_loopback_only` refuses to start otherwise);
* `oom_score_adj` 1000 on the server, before it is awaited: inside the
  worker container's one memory limit, the kernel kills the Postgres, never
  the agent (§4.1: a ranking, not a boundary). Its backends inherit it; the
  child's environment is built from nothing, so neither a worker secret nor
  `PG_OOM_ADJUST_FILE` (which would reset every backend's rank) reaches it;
* readiness by `pg_isready` on the socket every `READY_INTERVAL_SECONDS`, up
  to `READY_TIMEOUT_SECONDS`.

A step that fails is an ERROR, never an exception and never the attempt's
end: the service is stopped, its data removed, and the error -- the server's
last log lines through the worker's scrub -- is what the gate reports as
`test-services: error` (§5.3, TS-GATE). `RunningServices.stop` stops the
server (SIGINT, PostgreSQL's fast shutdown; killed after
`STOP_GRACE_SECONDS`) and removes its directory; the worker calls it from
its cleanup, which every exit reaches -- finish, park, cancel, fence (§5.5).
Invariant 7 is untouched: the server shares the worker container's fixed
requests == limits and nothing is added to the pod.

NEVER IN MEMORY (TS-CLOUDRUN-REFUSE, BGD 2026-10-24, "Refuse on Cloud Run").
Without `SWARM_CACHE_ROOT` the attempt's cache is inside the workspace tmpfs
(`cachedirs`), so the data directory would be charged to the container's
memory, against which §4.1 allows a Postgres only 256 MiB. `start` refuses
then, before `initdb`, with `NO_CACHE_VOLUME` as each service's error: the
same not-started report as any other start failure (`RunningServices.report`).
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import signal
import stat
import subprocess
import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .cachedirs import CACHE_ROOT_ENV

#: Where the declaration lives, relative to the clone's root.
DECLARATION_PATH = ".swarm/test-services.toml"

#: §2.2's bound. A real declaration is a few lines; 4 KiB is ample room for
#: comments and refuses anything that is plainly not one before it is parsed.
MAX_DECLARATION_BYTES = 4096

#: §2.1: 1-8 databases, each a lower-case Postgres identifier that needs no
#: quoting (63 bytes is Postgres's NAMEDATALEN - 1).
MAX_DATABASES = 8
DATABASE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")

#: The only keys a service's table may carry.
ALLOWED_KEYS = ("version", "databases")

#: Why the keys a repository is most likely to try are refused, so the
#: refusal says more than "unknown". Any other key is refused just the same.
_REFUSED_BECAUSE = {
    "image": "the platform owns the image (invariant 10)",
    "command": "the platform owns the command (invariant 10)",
    "args": "the platform owns the command and its arguments (invariant 10)",
    "port": "the platform owns the address: 127.0.0.1:5432 (§3.1)",
    "env": "the platform owns the service's environment (invariant 10)",
    "memory": "the platform owns the resources (invariant 7)",
    "cpu": "the platform owns the resources (invariant 7)",
    "settings": "the platform owns the server's settings (§3.1)",
    "init_sql": "a schema, extension or seed data is created by the repository's own fixtures",
    "user": "the platform owns the service's user (§3.1)",
}


@dataclass(frozen=True)
class CatalogueEntry:
    """A service the platform can run: its name and the majors it offers."""

    name: str
    versions: tuple[int, ...]


#: v1: PostgreSQL 16 alone (§3.1, owner decision Q3).
CATALOGUE: dict[str, CatalogueEntry] = {
    "postgres": CatalogueEntry(name="postgres", versions=(16,)),
}


@dataclass(frozen=True)
class Refusal:
    """One rule the declaration breaks. `key` is `<service>.<key>`, a bare
    table name, or `DECLARATION_PATH` for the file as a whole."""

    key: str
    rule: str

    def __str__(self) -> str:
        return f"{self.key}: {self.rule}"


@dataclass(frozen=True)
class DeclaredService:
    """A validated catalogue entry to run. `databases[0]` is the default
    database (`PGDATABASE`, `DATABASE_URL`; §5.1)."""

    name: str
    version: int
    databases: tuple[str, ...]


@dataclass(frozen=True)
class Declaration:
    """What a clone declares. `present` is whether the file exists; with any
    refusal, `services` is empty, so an invalid declaration runs nothing."""

    present: bool
    services: tuple[DeclaredService, ...] = ()
    refusals: tuple[Refusal, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.refusals

    @property
    def declared(self) -> bool:
        """At least one service to run."""
        return bool(self.services)

    def report(self) -> str:
        """Every refusal, one per line: the `test-services` check's output."""
        return "\n".join(str(refusal) for refusal in self.refusals)


NOTHING_DECLARED = Declaration(present=False)


def _refused(*refusals: Refusal) -> Declaration:
    return Declaration(present=True, refusals=tuple(refusals))


def _check_version(entry: CatalogueEntry, table: dict[str, Any], out: list[Refusal]) -> int | None:
    key = f"{entry.name}.version"
    offered = ", ".join(str(v) for v in entry.versions)
    if "version" not in table:
        out.append(Refusal(key, f"required: a major the catalogue offers ({offered})"))
        return None
    version = table["version"]
    # bool is an int in Python: `version = true` must not read as 1.
    if isinstance(version, bool) or not isinstance(version, int) or version not in entry.versions:
        out.append(
            Refusal(key, f"{version!r} is not offered: version is an integer, one of {offered}")
        )
        return None
    return version


def _check_databases(
    entry: CatalogueEntry, table: dict[str, Any], out: list[Refusal]
) -> tuple[str, ...] | None:
    key = f"{entry.name}.databases"
    rule = f"a list of 1-{MAX_DATABASES} names, each matching {DATABASE_NAME.pattern}"
    if "databases" not in table:
        out.append(Refusal(key, f"required: {rule}"))
        return None
    databases = table["databases"]
    if not isinstance(databases, list) or not 1 <= len(databases) <= MAX_DATABASES:
        out.append(Refusal(key, rule))
        return None
    names: list[str] = []
    ok = True
    for index, name in enumerate(databases):
        at = f"{key}[{index}]"
        if not isinstance(name, str) or not DATABASE_NAME.fullmatch(name):
            out.append(Refusal(at, f"{name!r} does not match {DATABASE_NAME.pattern}"))
            ok = False
        elif name in names:
            out.append(Refusal(at, f"{name!r} is a duplicate of an earlier database"))
            ok = False
        else:
            names.append(name)
    return tuple(names) if ok else None


def validate(document: dict[str, Any]) -> Declaration:
    """Hold a parsed TOML document to the catalogue; every refusal at once."""
    refusals: list[Refusal] = []
    services: list[DeclaredService] = []
    known = ", ".join(sorted(CATALOGUE))
    for name, table in document.items():
        entry = CATALOGUE.get(name)
        if entry is None:
            refusals.append(Refusal(name, f"not a catalogue service: one of {known}"))
            continue
        if not isinstance(table, dict):
            refusals.append(Refusal(name, f"[{name}] is a table of version and databases"))
            continue
        for key in table:
            if key not in ALLOWED_KEYS:
                because = _REFUSED_BECAUSE.get(key, "not a declaration key")
                allowed = " and ".join(ALLOWED_KEYS)
                refusals.append(Refusal(f"{name}.{key}", f"refused: {because}; only {allowed}"))
        version = _check_version(entry, table, refusals)
        databases = _check_databases(entry, table, refusals)
        if version is not None and databases is not None:
            services.append(DeclaredService(name=name, version=version, databases=databases))
    if refusals:
        return _refused(*refusals)
    return Declaration(present=True, services=tuple(services))


def parse_declaration(data: bytes) -> Declaration:
    """Parse and validate the file's bytes. Pure: no I/O."""
    if len(data) > MAX_DECLARATION_BYTES:
        return _refused(
            Refusal(DECLARATION_PATH, f"the file is over {MAX_DECLARATION_BYTES} bytes")
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return _refused(Refusal(DECLARATION_PATH, f"not UTF-8 at byte {exc.start}"))
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return _refused(Refusal(DECLARATION_PATH, f"TOML syntax error: {exc}"))
    return validate(document)


def read_declaration(root: Path) -> Declaration:
    """The declaration in the clone at `root`; `NOTHING_DECLARED` without one.

    Reads `DECLARATION_PATH` only, only inside the clone, only a regular
    file, and at most one byte over the bound."""
    path = Path(root) / DECLARATION_PATH
    if not path.exists() and not path.is_symlink():
        return NOTHING_DECLARED
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(Path(root).resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return _refused(
            Refusal(DECLARATION_PATH, "resolves outside the repository; it is not read")
        )
    try:
        mode = resolved.stat().st_mode
        if not stat.S_ISREG(mode):
            return _refused(Refusal(DECLARATION_PATH, "not a regular file"))
        with resolved.open("rb") as handle:
            data = handle.read(MAX_DECLARATION_BYTES + 1)
    except OSError as exc:
        return _refused(Refusal(DECLARATION_PATH, f"could not be read ({exc.strerror})"))
    return parse_declaration(data)


# ---------------------------------------------------------------------------
# Running a declaration (TS-RUN)
# ---------------------------------------------------------------------------

#: §3.1's address: the one tests already default to, on loopback only.
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 5432

#: §3.1: the server's one user, `trust` on loopback and the socket. No
#: password exists, so nothing can leak and nothing needs redaction.
SERVICE_USER = "postgres"

#: §3.1: `pg_isready` every 100 ms, up to 30 s. Not ready by then is an error.
READY_TIMEOUT_SECONDS = 30.0
READY_INTERVAL_SECONDS = 0.1

#: §4.1: the kernel's OOM killer takes the service before the agent.
OOM_SCORE_ADJ = 1000

#: How long a fast shutdown may take before the server is killed. Its data
#: is thrown away either way; this only bounds the attempt's cleanup.
STOP_GRACE_SECONDS = 10.0

#: Each one-shot step (initdb, the databases, one readiness probe). initdb
#: with `--no-sync` on the cache disk takes seconds (§4.1, *est.* 1-3 s).
STEP_TIMEOUT_SECONDS = 120.0
PROBE_TIMEOUT_SECONDS = 2.0

#: How much of the server's own log an error carries.
LOG_TAIL_LINES = 20

#: Under the attempt's cache directory: `services/<name>/{data,run,server.log}`.
SERVICES_DIR_NAME = "services"

#: §3.1's settings, the server's whole command-line configuration. A
#: repository chooses none of them (invariant 10). Memory figures hold the
#: server under §4.1's 512 MiB budget; durability is off because the data is
#: thrown away at the attempt's end.
POSTGRES_SETTINGS: tuple[tuple[str, str], ...] = (
    ("port", str(LISTEN_PORT)),
    ("shared_buffers", "64MB"),
    ("work_mem", "4MB"),
    ("maintenance_work_mem", "32MB"),
    ("max_connections", "50"),
    ("max_wal_size", "256MB"),
    ("fsync", "off"),
    ("synchronous_commit", "off"),
    ("full_page_writes", "off"),
    ("unix_socket_permissions", "0700"),
    ("logging_collector", "off"),
)

#: The databases `initdb` makes itself; declaring one is not an error, and
#: creating it again would be.
INITDB_DATABASES = frozenset({"postgres", "template0", "template1"})

#: The database `initdb` makes that single-user mode and the probe connect to.
MAINTENANCE_DATABASE = "postgres"


def bin_dir(version: int) -> Path:
    """Where `agent-runtime-base` installs the major's binaries (TS-IMAGE):
    by absolute path, so the version that runs is the version declared."""
    return Path(f"/usr/lib/postgresql/{version}/bin")


def service_root(cache: Path, name: str) -> Path:
    return Path(cache) / SERVICES_DIR_NAME / name


def child_env(root: Path) -> dict[str, str]:
    """The service's whole environment, built from nothing: no worker secret,
    no `PG*` variable (`PG_OOM_ADJUST_FILE` would reset each backend's OOM
    rank), and a C locale so `initdb` needs none installed."""
    return {"PATH": "/usr/bin:/bin", "HOME": str(root), "LC_ALL": "C", "LANG": "C", "TZ": "UTC"}


def initdb_argv(version: int, data: Path) -> list[str]:
    return [
        str(bin_dir(version) / "initdb"),
        "-D",
        str(data),
        f"--username={SERVICE_USER}",
        "--auth=trust",
        "--encoding=UTF8",
        "--locale=C",
        "--no-sync",
        "--no-instructions",
    ]


def create_databases_argv(version: int, data: Path) -> list[str]:
    """Single-user mode: the databases exist before the server listens."""
    return [str(bin_dir(version) / "postgres"), "--single", "-D", str(data), MAINTENANCE_DATABASE]


def create_databases_input(databases: tuple[str, ...]) -> str:
    """One statement per line, as single-user mode reads them. The names
    were held to `DATABASE_NAME` already; quoted all the same."""
    return "".join(
        f'CREATE DATABASE "{name}";\n' for name in databases if name not in INITDB_DATABASES
    )


def server_argv(version: int, data: Path, socket_dir: Path) -> list[str]:
    settings = (
        ("listen_addresses", LISTEN_HOST),
        *POSTGRES_SETTINGS,
        ("unix_socket_directories", str(socket_dir)),
    )
    argv = [str(bin_dir(version) / "postgres"), "-D", str(data)]
    for name, value in settings:
        argv += ["-c", f"{name}={value}"]
    return argv


def ready_argv(version: int, socket_dir: Path) -> list[str]:
    return [
        str(bin_dir(version) / "pg_isready"),
        "-h",
        str(socket_dir),
        "-p",
        str(LISTEN_PORT),
        "-U",
        SERVICE_USER,
        "-d",
        MAINTENANCE_DATABASE,
        "-t",
        "1",
    ]


def _loopback_only(host: str) -> bool:
    """Every listen address a loopback one: nothing outside the pod connects."""
    try:
        return all(ipaddress.ip_address(a.strip()).is_loopback for a in host.split(","))
    except ValueError:
        return False


class Child(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def send_signal(self, sig: int) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class ProcessRunner:
    """How the worker runs the service's binaries. The tests replace it."""

    def run(
        self, argv: list[str], *, env: dict[str, str], timeout: float, stdin: str | None = None
    ) -> tuple[int, str]:
        """A one-shot step: its status and its combined output."""
        try:
            done = subprocess.run(
                argv,
                env=env,
                input=stdin if stdin is not None else "",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            out = exc.output or ""
            if isinstance(out, bytes):
                out = out.decode("utf-8", "replace")
            return 124, f"{out}\n(timed out after {timeout:g} s)"
        except OSError as exc:
            return 127, f"could not run {Path(argv[0]).name}: {exc.strerror}"
        return done.returncode, done.stdout

    def spawn(self, argv: list[str], *, env: dict[str, str], log: Path) -> Child:
        """The server, its output to `log`, in its own session so a signal
        to the worker's group is not also one to it."""
        with open(log, "ab") as out:
            return subprocess.Popen(
                argv,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    def set_oom_score_adj(self, pid: int, value: int) -> None:
        """Raising a child's score needs no privilege (§4.1)."""
        Path(f"/proc/{pid}/oom_score_adj").write_text(f"{value}\n")


@dataclass
class ServiceProcess:
    """One declared service: running, or the error that stopped it."""

    service: DeclaredService
    root: Path
    process: Child | None = None
    error: str | None = None
    ready_seconds: float | None = None

    def exit_status(self) -> int | None:
        """None while the server runs; its status once it has exited (a
        negative status is the signal that ended it: -9 is the OOM killer)."""
        if self.process is None:
            return None
        return self.process.poll()

    def stop(self) -> None:
        """Stop the server and remove its data. Idempotent; never raises OSError."""
        process, self.process = self.process, None
        if process is not None and process.poll() is None:
            try:
                # SIGINT is PostgreSQL's fast shutdown: it disconnects the
                # clients rather than waiting for them, as SIGTERM would.
                process.send_signal(signal.SIGINT)
                process.wait(timeout=STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=STOP_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
            except OSError:
                pass
        shutil.rmtree(self.root, ignore_errors=True)


@dataclass
class RunningServices:
    """What `start` returns: every declared service, each up or errored."""

    services: tuple[ServiceProcess, ...] = field(default_factory=tuple)

    def errors(self) -> list[str]:
        return [s.error for s in self.services if s.error is not None]

    def report(self) -> list[str]:
        """One line per declared service that is not running, with its reason:
        what the pre-publish check shows for it."""
        return [
            f"{s.service.name}: service not started: {s.error}"
            for s in self.services
            if s.error is not None
        ]

    def stop(self) -> None:
        for service in self.services:
            service.stop()


def _log_tail(log: Path) -> str:
    try:
        with log.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 16384))
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-LOG_TAIL_LINES:])


def _failed(
    proc: ServiceProcess, what: str, output: str, scrub: Callable[[str], str]
) -> ServiceProcess:
    """Stop it, remove its data, and keep the error: scrubbed, with its lines."""
    lines = "\n".join(output.strip().splitlines()[-LOG_TAIL_LINES:])
    text = f"the declared {proc.service.name} {what}"
    if lines:
        text += f"; its last lines:\n{lines}"
    proc.error = str(scrub(text))
    proc.stop()
    return proc


def _start_postgres(
    service: DeclaredService,
    cache: Path,
    *,
    scrub: Callable[[str], str],
    runner: ProcessRunner,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> ServiceProcess:
    root = service_root(cache, service.name)
    proc = ServiceProcess(service=service, root=root)
    if not _loopback_only(LISTEN_HOST):
        proc.error = f"refused: {LISTEN_HOST!r} is not a loopback address; nothing was started"
        return proc
    data, socket_dir, log = root / "data", root / "run", root / "server.log"
    env = child_env(root)
    started = clock()
    shutil.rmtree(root, ignore_errors=True)
    try:
        root.mkdir(mode=0o700, parents=True)
        socket_dir.mkdir(mode=0o700)
        os.chmod(root, 0o700)
        os.chmod(socket_dir, 0o700)
    except OSError as exc:
        return _failed(proc, f"could not make its directory ({exc.strerror})", "", scrub)

    status, output = runner.run(
        initdb_argv(service.version, data), env=env, timeout=STEP_TIMEOUT_SECONDS
    )
    if status != 0:
        return _failed(proc, f"did not start: initdb exited {status}", output, scrub)

    script = create_databases_input(service.databases)
    if script:
        status, output = runner.run(
            create_databases_argv(service.version, data),
            env=env,
            timeout=STEP_TIMEOUT_SECONDS,
            stdin=script,
        )
        # Single-user mode reports a failed statement and carries on, exit 0.
        if status != 0 or "ERROR:" in output or "FATAL:" in output:
            return _failed(proc, "did not start: a database could not be created", output, scrub)

    try:
        proc.process = runner.spawn(
            server_argv(service.version, data, socket_dir), env=env, log=log
        )
    except OSError as exc:
        return _failed(proc, f"did not start ({exc.strerror})", "", scrub)
    try:
        runner.set_oom_score_adj(proc.process.pid, OOM_SCORE_ADJ)
    except OSError as exc:
        return _failed(
            proc,
            f"was not run: its oom_score_adj could not be set to {OOM_SCORE_ADJ} "
            f"({exc.strerror or type(exc).__name__}), so the kernel would rank it beside the agent",
            "",
            scrub,
        )

    deadline = clock() + READY_TIMEOUT_SECONDS
    probe = ready_argv(service.version, socket_dir)
    while True:
        exited = proc.process.poll()
        if exited is not None:
            return _failed(
                proc, f"exited with status {exited} before it was ready", _log_tail(log), scrub
            )
        status, _ = runner.run(probe, env=env, timeout=PROBE_TIMEOUT_SECONDS)
        if status == 0:
            proc.ready_seconds = round(clock() - started, 3)
            return proc
        if clock() >= deadline:
            return _failed(
                proc,
                f"was not ready within {READY_TIMEOUT_SECONDS:g} s",
                _log_tail(log),
                scrub,
            )
        sleep(READY_INTERVAL_SECONDS)


#: Why a declared service is not started when the worker has no cache volume
#: (`CACHE_ROOT_ENV` unset, as on Cloud Run): its data would sit in the
#: workspace tmpfs, charged to the container's memory, and design §4.1 caps a
#: Postgres in memory at 256 MiB -- less than a test database and its backends
#: need. BGD 2026-10-24: "Refuse on Cloud Run". Read from the environment, not
#: from a profile name, so any runner without its own cache volume refuses.
NO_CACHE_VOLUME = (
    f"test services need {CACHE_ROOT_ENV} on its own volume; "
    "this runner keeps its workspace in memory"
)

#: How each catalogue service is started, by name.
_STARTERS = {"postgres": _start_postgres}


def start(
    declaration: Declaration,
    cache: Path,
    *,
    scrub: Callable[[str], str],
    runner: ProcessRunner | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    environ: Mapping[str, str] | None = None,
) -> RunningServices:
    """Run every service a VALID declaration names, under `cache`; never raises
    for a service that fails to start -- that service carries its error.

    A declaration with any refusal, or none at all, starts nothing. Without a
    cache volume (`CACHE_ROOT_ENV` in `environ`, the process's by default)
    nothing runs and every service carries `NO_CACHE_VOLUME`."""
    if not declaration.valid or not declaration.declared:
        return RunningServices()
    environ = os.environ if environ is None else environ
    if not environ.get(CACHE_ROOT_ENV, "").strip():
        return RunningServices(
            services=tuple(
                ServiceProcess(
                    service=service,
                    root=service_root(cache, service.name),
                    error=NO_CACHE_VOLUME,
                )
                for service in declaration.services
            )
        )
    runner = runner if runner is not None else ProcessRunner()
    started = tuple(
        _STARTERS[service.name](
            service, Path(cache), scrub=scrub, runner=runner, clock=clock, sleep=sleep
        )
        for service in declaration.services
    )
    return RunningServices(services=started)
