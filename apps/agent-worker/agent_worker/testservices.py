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

THIS MODULE STARTS NOTHING. No process, no socket, and no file outside the
clone: a declaration that resolves outside it (a symlinked file or
`.swarm`) or is not a regular file is refused unread, and the read is
bounded at one byte over the limit. Starting the service from the returned
`Declaration` is TS-RUN's, in this same module once it merges.
"""

from __future__ import annotations

import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
