"""The test-services acceptance fixture is one the platform accepts (TS-ACCEPT, part of #1627).

docs/design/test-services.md §7, row TS-ACCEPT: a fixture repository under
tests/acceptance/ declares a Postgres 16 in `.swarm/test-services.toml` and
holds one test that writes to it. The claude-code acceptance group
(scripts/acceptance/groups/claude-code.sh) has an agent break that test on a
deployed worker and expects the pre-publish record to say `graph-pytest:
failed` with the declaration and `skipped` without it. A fixture the
catalogue refuses would make that check fail on dev for a reason that is the
fixture's, after a release; these hold it here, offline:

* the declaration validates with TS-CATALOGUE's own parser
  (`agent_worker.testservices.read_declaration`) to exactly Postgres 16;
* the fixture's client defaults to the catalogue's address, user and the
  declared database, so a refused connection names the address the gate
  matches against the declared service (§5.3);
* the gate selects the fixture's test as a changed test, finds its Python
  project, and can run it with `-n auto`; `.venv/` stays out of the diff;
* the line the group tells the agent to break is in the test, once.
"""

from __future__ import annotations

import importlib.util
import re
import tomllib
from pathlib import Path

from agent_worker import prepublish, testservices

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests" / "acceptance" / "fixtures" / "test-services"
GROUP = ROOT / "scripts" / "acceptance" / "groups" / "claude-code.sh"


def _group_value(name: str) -> str:
    """A `NAME='...'` or `NAME="..."` assignment in the claude-code group."""
    m = re.search(rf"""^{name}=(?:'([^']*)'|"([^"]*)")$""", GROUP.read_text(), re.M)
    assert m, f"claude-code.sh assigns no {name}"
    return m.group(1) if m.group(1) is not None else m.group(2)


def _client():
    spec = importlib.util.spec_from_file_location("ts_fixture_pgwire", FIXTURE / "pgwire.py")
    assert spec and spec.loader, "the fixture has no pgwire.py"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_fixtures_declaration_validates_with_the_catalogue_parser_to_postgres_16():
    declaration = testservices.read_declaration(FIXTURE)
    # MUTATION: a fixture with no .swarm/test-services.toml is "nothing declared", and
    # the declared half of the acceptance check would run the undeclared case twice.
    assert declaration.present
    # MUTATION: `version = 15`, an `image` or `port` key, or a database name with a
    # hyphen is refused, and the worker starts nothing (§2.2).
    assert declaration.refusals == ()
    # MUTATION: a second service, or a catalogue entry other than Postgres 16.
    assert declaration.services == (
        testservices.DeclaredService(name="postgres", version=16, databases=("swarm_accept",)),
    )


def test_the_fixtures_client_defaults_to_the_catalogue_address_and_the_declared_database():
    client = _client()
    (service,) = testservices.read_declaration(FIXTURE).services
    # MUTATION: a client defaulting to 5433 or to localhost on another host: without
    # the declaration its refusal names an address the gate does not match, and
    # with it the test connects to nothing the worker started.
    assert (client.HOST, client.PORT) == (testservices.LISTEN_HOST, testservices.LISTEN_PORT)
    # MUTATION: another user or database: trust authentication admits only the
    # catalogue's user, and only the declared database exists.
    assert client.USER == testservices.SERVICE_USER
    assert client.DATABASE == service.databases[0]


def test_a_refused_connection_names_the_address_the_gate_reads_as_unreachable():
    client = _client()
    # MUTATION: a client that raises a bare `[Errno 111]` with no address: the gate's
    # `unreachable_services` needs an address, keeps the check failed, and the
    # undeclared half of the acceptance check fails for the client's reason.
    message = f"could not connect to server at {client.HOST}:{client.PORT}: [Errno 111] Connection refused"
    report = (
        '<testsuites><testsuite><testcase classname="test_store" name="test_a_row_written_is_read_back">'
        f'<error message="failed on setup with &quot;ConnectionError: {message}&quot;">{message}</error>'
        "</testcase></testsuite></testsuites>"
    )
    assert prepublish.unreachable_services(report) == [f"{client.HOST}:{client.PORT}"]
    source = (FIXTURE / "pgwire.py").read_text()
    assert 'f"could not connect to server at {host}:{port}: {exc}"' in source


def test_the_gate_runs_the_fixtures_test_as_a_changed_test_of_its_own_project():
    test = "test_store.py"
    # MUTATION: a test file the gate does not recognise is never selected, and the
    # record holds no graph-pytest at all.
    assert prepublish.is_test_path(test)
    assert prepublish.select_tests(FIXTURE, [test], None) == {test: "changed"}
    project = tomllib.loads((FIXTURE / "pyproject.toml").read_text())["project"]
    names = {re.split(r"[<>=!~ \[]", dep, maxsplit=1)[0] for dep in project["dependencies"]}
    # MUTATION: dropping pytest-xdist: the gate runs `pytest -n auto`, which then
    # fails on its arguments, a `failed` that is not the broken test's.
    assert {"pytest", "pytest-xdist"} <= names
    lock = (FIXTURE / "uv.lock").read_text()
    assert 'name = "pytest-xdist"' in lock and 'name = "pytest"' in lock
    # MUTATION: no .gitignore for the project's .venv/: the gate's changed files
    # take in every test_*.py under site-packages.
    ignored = (FIXTURE / ".gitignore").read_text().split()
    assert ".venv/" in ignored


def test_the_line_the_group_has_the_agent_break_is_in_the_test_once():
    line = _group_value("CC_TS_ASSERT_LINE")
    assert _group_value("CC_TS_TEST") == "test_store.py"
    lines = (FIXTURE / "test_store.py").read_text().splitlines()
    # MUTATION: a reworded assertion in the fixture: the agent is asked to change a
    # line that is not there, and the declared half has nothing to fail.
    assert lines.count(line) == 1
    # The test connects in its fixture, so a missing service is a setup error.
    assert "def db():" in "\n".join(lines) and "pgwire.connect()" in "\n".join(lines)
