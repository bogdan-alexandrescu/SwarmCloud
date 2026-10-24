"""A repository's `.swarm/test-services.toml` is read as data and held to the catalogue (TS-CATALOGUE, #1627).

docs/design/test-services.md §2 and §3, owner decisions of 2026-10-23 (§10):
a repository names the services its tests need in a checked-in TOML file --
a catalogue service NAME, a major version the catalogue offers, and the
databases to create -- and nothing else. The catalogue in v1 is PostgreSQL
16. Everything that would let a repository choose what runs or how big it is
(an image, a command, a port, a memory figure, an environment, a user) is
refused by name (invariant 10), and every refusal is reported together, the
way `check_size` reports a runner size.

This lane builds the reading and the validation only
(`agent_worker.testservices`): nothing is started, nothing listens, and no
file outside the clone is read. TS-RUN starts the service from the
validated declaration this module returns.
"""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

import pytest

from agent_worker import testservices

VALID = '[postgres]\nversion = 16\ndatabases = ["app_test"]\n'


def _write(root: Path, text: str | bytes) -> Path:
    path = root / ".swarm" / "test-services.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def _keys(declaration: testservices.Declaration) -> list[str]:
    return [refusal.key for refusal in declaration.refusals]


def test_a_valid_file_parses_into_the_declared_service(tmp_path):
    _write(tmp_path, VALID)
    declaration = testservices.read_declaration(tmp_path)
    # MUTATION: a validator that refuses the documented example, or loses its databases.
    assert declaration.refusals == ()
    assert declaration.services == (
        testservices.DeclaredService(name="postgres", version=16, databases=("app_test",)),
    )
    assert declaration.present and declaration.valid and declaration.declared


def test_several_databases_keep_their_order(tmp_path):
    _write(tmp_path, '[postgres]\nversion = 16\ndatabases = ["app_test", "_audit", "b2"]\n')
    declaration = testservices.read_declaration(tmp_path)
    # MUTATION: sorting or de-duplicating silently; the first database is the default one (§5.1).
    assert declaration.services[0].databases == ("app_test", "_audit", "b2")


def test_a_repository_without_the_file_declares_nothing(tmp_path):
    declaration = testservices.read_declaration(tmp_path)
    # MUTATION: a missing file reported as a refusal, which would put a check on every lane.
    assert declaration.refusals == ()
    assert declaration.services == ()
    assert not declaration.present and not declaration.declared
    assert declaration.valid


def test_the_catalogue_is_postgres_16_alone():
    # MUTATION: a second service or major slipping into v1 without its binaries (§3, Q3, Q6).
    assert set(testservices.CATALOGUE) == {"postgres"}
    assert testservices.CATALOGUE["postgres"].versions == (16,)
    assert testservices.DECLARATION_PATH == ".swarm/test-services.toml"
    assert testservices.MAX_DECLARATION_BYTES == 4096


@pytest.mark.parametrize(
    "key, value",
    [
        ("image", '"postgres:16"'),
        ("command", '["postgres", "-c", "fsync=on"]'),
        ("port", "5433"),
        ("memory", '"4Gi"'),
        ("env", '{ POSTGRES_PASSWORD = "x" }'),
        ("user", '"root"'),
        ("args", '["-c", "shared_buffers=2GB"]'),
        ("cpu", "4"),
        ("settings", '{ shared_buffers = "2GB" }'),
        ("init_sql", '"CREATE EXTENSION vector"'),
        ("anything_else", "1"),
    ],
)
def test_every_key_but_version_and_databases_is_refused_by_name(tmp_path, key, value):
    _write(tmp_path, VALID + f"{key} = {value}\n")
    declaration = testservices.read_declaration(tmp_path)
    # MUTATION: an allow-everything validator, or one that lists only some refused keys.
    assert f"postgres.{key}" in _keys(declaration)
    # An invalid declaration starts nothing (§2.2).
    assert declaration.services == ()
    assert not declaration.declared


def test_an_unknown_table_is_refused_by_name(tmp_path):
    _write(tmp_path, VALID + '[redis]\nversion = 7\n')
    declaration = testservices.read_declaration(tmp_path)
    # MUTATION: unknown tables ignored instead of refused (Q6: no Redis in v1).
    assert "redis" in _keys(declaration)
    assert declaration.services == ()


def test_a_top_level_key_that_is_not_a_table_is_refused(tmp_path):
    _write(tmp_path, "version = 16\n" + VALID)
    declaration = testservices.read_declaration(tmp_path)
    # MUTATION: a bare root key accepted as if it were a service setting.
    assert "version" in _keys(declaration)
    _write(tmp_path, "postgres = 16\n")
    # MUTATION: `postgres` as a scalar crashing the validator instead of refusing.
    assert "postgres" in _keys(testservices.read_declaration(tmp_path))


@pytest.mark.parametrize("version", ["15", "17", '"16"', "16.0", "true"])
def test_a_version_the_catalogue_does_not_offer_is_refused(tmp_path, version):
    _write(tmp_path, f'[postgres]\nversion = {version}\ndatabases = ["app_test"]\n')
    declaration = testservices.read_declaration(tmp_path)
    # MUTATION: accepting any integer, a string, a float or a bool (True == 1 in Python).
    assert _keys(declaration) == ["postgres.version"]
    assert declaration.services == ()


def test_version_15_is_refused_naming_what_the_catalogue_offers(tmp_path):
    _write(tmp_path, '[postgres]\nversion = 15\ndatabases = ["app_test"]\n')
    [refusal] = testservices.read_declaration(tmp_path).refusals
    # MUTATION: a refusal that says no more than "invalid".
    assert "16" in refusal.rule
    assert "postgres.version" in str(refusal)


def test_a_missing_version_or_databases_is_refused(tmp_path):
    _write(tmp_path, "[postgres]\n")
    # MUTATION: defaulting a missing version or database list instead of refusing.
    assert sorted(_keys(testservices.read_declaration(tmp_path))) == [
        "postgres.databases",
        "postgres.version",
    ]


@pytest.mark.parametrize(
    "name",
    ["App_Test", "1db", "app-test", "app test", "", "x" * 64, "ü"],
)
def test_a_malformed_database_name_is_refused(tmp_path, name):
    _write(tmp_path, f'[postgres]\nversion = 16\ndatabases = ["ok", "{name}"]\n')
    declaration = testservices.read_declaration(tmp_path)
    # MUTATION: a looser name pattern than [a-z_][a-z0-9_]{0,62}.
    assert _keys(declaration) == ["postgres.databases[1]"]
    assert declaration.services == ()


def test_the_longest_legal_database_name_is_accepted(tmp_path):
    name = "a" + "_" * 62
    _write(tmp_path, f'[postgres]\nversion = 16\ndatabases = ["{name}"]\n')
    # MUTATION: an off-by-one in the 63-character bound.
    assert testservices.read_declaration(tmp_path).services[0].databases == (name,)


@pytest.mark.parametrize(
    "databases",
    ['"app_test"', "[]", "[1]", str([f"db{n}" for n in range(9)]).replace("'", '"')],
)
def test_databases_must_be_a_list_of_one_to_eight_names(tmp_path, databases):
    _write(tmp_path, f"[postgres]\nversion = 16\ndatabases = {databases}\n")
    declaration = testservices.read_declaration(tmp_path)
    # MUTATION: a bare string, an empty list, a non-string or a ninth database accepted.
    assert any(key.startswith("postgres.databases") for key in _keys(declaration))
    assert declaration.services == ()


def test_eight_databases_are_accepted(tmp_path):
    names = [f"db{n}" for n in range(8)]
    _write(tmp_path, f"[postgres]\nversion = 16\ndatabases = {names}\n".replace("'", '"'))
    # MUTATION: an off-by-one in the 1-8 bound.
    assert testservices.read_declaration(tmp_path).services[0].databases == tuple(names)


def test_a_duplicated_database_name_is_refused(tmp_path):
    _write(tmp_path, '[postgres]\nversion = 16\ndatabases = ["app_test", "other", "app_test"]\n')
    declaration = testservices.read_declaration(tmp_path)
    # MUTATION: duplicates silently collapsed instead of refused.
    assert _keys(declaration) == ["postgres.databases[2]"]
    assert "duplicate" in declaration.refusals[0].rule


def test_a_file_over_4_kib_is_refused(tmp_path):
    padding = "# " + "x" * 78 + "\n"
    text = VALID + padding * (5 * 1024 // len(padding))
    assert len(text.encode()) > 5000
    _write(tmp_path, text)
    declaration = testservices.read_declaration(tmp_path)
    # MUTATION: no size bound, or one checked after parsing an unbounded read.
    assert _keys(declaration) == [testservices.DECLARATION_PATH]
    assert "4096" in declaration.refusals[0].rule
    assert declaration.services == ()


def test_a_file_of_exactly_4_kib_is_accepted(tmp_path):
    text = VALID + "#" * (4096 - len(VALID) - 1) + "\n"
    assert len(text.encode()) == 4096
    _write(tmp_path, text)
    # MUTATION: an off-by-one that refuses the bound itself.
    assert testservices.read_declaration(tmp_path).declared


def test_a_toml_syntax_error_is_refused(tmp_path):
    _write(tmp_path, '[postgres\nversion = 16\n')
    declaration = testservices.read_declaration(tmp_path)
    # MUTATION: tomllib's exception escaping to the gate instead of becoming a refusal.
    assert _keys(declaration) == [testservices.DECLARATION_PATH]
    assert "TOML" in declaration.refusals[0].rule
    assert declaration.present and not declaration.valid


def test_bytes_that_are_not_utf_8_are_refused(tmp_path):
    _write(tmp_path, b"[postgres]\nversion = 16\ndatabases = [\"\xff\"]\n")
    declaration = testservices.read_declaration(tmp_path)
    # MUTATION: a UnicodeDecodeError escaping instead of becoming a refusal.
    assert _keys(declaration) == [testservices.DECLARATION_PATH]


def test_every_refusal_is_reported_together(tmp_path):
    _write(
        tmp_path,
        '[postgres]\nversion = 15\nimage = "postgres:16"\nport = 5433\n'
        'databases = ["app_test", "Bad-Name", "app_test"]\n'
        "[redis]\nversion = 7\n",
    )
    declaration = testservices.read_declaration(tmp_path)
    # MUTATION: stopping at the first refusal.
    assert sorted(_keys(declaration)) == sorted(
        [
            "postgres.version",
            "postgres.image",
            "postgres.port",
            "postgres.databases[1]",
            "postgres.databases[2]",
            "redis",
        ]
    )
    report = declaration.report()
    # MUTATION: a report that drops refusals the result holds.
    assert all(str(refusal) in report for refusal in declaration.refusals)


def test_a_declaration_that_points_outside_the_clone_is_not_read(tmp_path):
    clone = tmp_path / "clone"
    outside = tmp_path / "outside.toml"
    outside.write_text(VALID, encoding="utf-8")
    (clone / ".swarm").mkdir(parents=True)
    (clone / ".swarm" / "test-services.toml").symlink_to(outside)
    declaration = testservices.read_declaration(clone)
    # MUTATION: following a symlink out of the clone and honouring what it finds.
    assert _keys(declaration) == [testservices.DECLARATION_PATH]
    assert declaration.services == ()

    other = tmp_path / "other"
    (other / "elsewhere").mkdir(parents=True)
    (other / "elsewhere" / "test-services.toml").write_text(VALID, encoding="utf-8")
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".swarm").symlink_to(other / "elsewhere")
    # MUTATION: checking only the file and not the `.swarm` directory it sits in.
    assert _keys(testservices.read_declaration(linked)) == [testservices.DECLARATION_PATH]


def test_a_path_that_is_not_a_regular_file_is_refused(tmp_path):
    (tmp_path / ".swarm" / "test-services.toml").mkdir(parents=True)
    declaration = testservices.read_declaration(tmp_path)
    # MUTATION: an IsADirectoryError escaping, or a directory read as "nothing declared".
    assert _keys(declaration) == [testservices.DECLARATION_PATH]


def test_reading_and_validating_starts_nothing(tmp_path, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("the declaration reader started a process or opened a socket")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(os, "system", refuse)
    monkeypatch.setattr(socket, "socket", refuse)
    _write(tmp_path, VALID)
    # MUTATION: TS-RUN's start creeping into the reader (§7: this lane starts nothing).
    assert testservices.read_declaration(tmp_path).declared
    assert testservices.parse_declaration(VALID.encode()).declared
