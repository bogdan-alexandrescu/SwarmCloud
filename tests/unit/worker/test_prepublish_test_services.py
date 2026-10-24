"""The pre-publish gate runs a declared service's tests for real (TS-GATE, #1627).

docs/design/test-services.md §5.1-§5.3 and §7's TS-GATE row, with the owner's
decisions of 2026-10-23 (§10): option A, `.swarm/test-services.toml`,
Postgres 16, the service up for the whole attempt after the clone (Q4: the
WORKER starts it, `lifecycle.Worker._start_test_services`), no extra URL
variables. The gate runs in the runner's process, not the worker's, so it
finds the worker's service by asking `127.0.0.1:5432` (`pg_isready`), and
starts one itself only when nothing answers there -- a declaration the
agent added in its own change, or a service that died (§2.3).

What is held here, each assertion with the mutation it catches:

* every row of §5.3's table;
* §5.1's environment reaches the checks that run pytest, and never the
  gate's own environment, which the agent's session continues with;
* §5.2's order: the declaration read, the service up, the checks run, the
  service probed after each pytest check, a service the gate started
  stopped after the round;
* the issue's acceptance pair: declared plus a failing test is `failed` with
  a fix round, the same tree without the file is `skipped` as PR 1606 made it.

Everything is FAKE: no Postgres, no socket. The gate's runner answers
`pg_isready` from a flag and runs git for real; `uv` is a stand-in that
writes the junitxml report pytest would have written, choosing it by
whether `DATABASE_URL` reached it; the service runner is a
`testservices.ProcessRunner` stand-in.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape, quoteattr

import pytest

from agent_worker import prepublish, testservices

from test_prepublish_gate import Turns, _change, _env, _git, _log, _section, _tool_calls
from test_prepublish_graph_gate import MOD_TEST, _fixture_repo

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

DECLARATION = '[postgres]\nversion = 16\ndatabases = ["app_test", "other_test"]\n'
#: §5.1's variables: what the stand-in uv reports it saw.
SERVICE_VARS = ("PGHOST", "PGPORT", "PGUSER", "PGDATABASE", "DATABASE_URL",
                "SWARM_TEST_SERVICES")

#: `uv run pytest ... --junitxml=<path>`: logs the §5.1 variables it saw and
#: writes $FAKE_JUNIT_UP's report when DATABASE_URL reached it, else
#: $FAKE_JUNIT's; exits 1 unless $FAKE_EXIT says otherwise.
FAKE_UV = r'''
import json, os, sys
seen = {k: os.environ.get(k) for k in %r}
with open(os.environ["TOOL_LOG"], "a") as f:
    f.write(json.dumps({"tool": "uv", "argv": sys.argv[1:], "env": seen}) + "\n")
report = [a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--junitxml=")]
source = os.environ.get("FAKE_JUNIT_UP") if os.environ.get("DATABASE_URL") else None
source = source or os.environ.get("FAKE_JUNIT")
if report and source:
    with open(report[-1], "w") as out, open(source) as src:
        out.write(src.read())
sys.exit(int(os.environ.get("FAKE_EXIT", "1")))
''' % (SERVICE_VARS,)


def _refused(port: int) -> str:
    return f"ConnectionRefusedError: [Errno 111] Connect call failed ('127.0.0.1', {port})"


def _setup_error(name: str, text: str) -> str:
    message = quoteattr(f'failed on setup with "{text}"')
    return (f'<testcase classname="tests.test_mod" name="{name}" time="0.01">'
            f"<error message={message}>{escape('E   ' + text)}</error></testcase>")


def _failure(name: str) -> str:
    return (f'<testcase classname="tests.test_mod" name="{name}" time="0.01">'
            '<failure message="assert 2 == 1">assert 2 == 1</failure></testcase>')


def _passed(name: str) -> str:
    return f'<testcase classname="tests.test_mod" name="{name}" time="0.01" />'


def _junit(*cases: str) -> str:
    return ('<?xml version="1.0" encoding="utf-8"?><testsuites>'
            f'<testsuite name="pytest" tests="{len(cases)}">{"".join(cases)}</testsuite>'
            "</testsuites>")


def _all_refused(port: int) -> str:
    return _junit(_setup_error("test_covered", _refused(port)),
                  _setup_error("test_db", _refused(port)))


# ---------------------------------------------------------------------------
# Stand-ins
# ---------------------------------------------------------------------------


class GateRunner:
    """The gate's runner: `pg_isready` answers from `up`; anything else runs
    for real. Records every command with the environment it was given, and
    calls `after_pytest` once each `uv run pytest` returns."""

    def __init__(self, *, up: bool) -> None:
        self.up = up
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.after_pytest: Callable[[], None] | None = None

    def __call__(self, argv, cwd, env, timeout):
        self.calls.append((list(argv), dict(env)))
        if Path(argv[0]).name == "pg_isready":
            return prepublish.Ran(0 if self.up else 2, "")
        ran = prepublish.run_command(argv, cwd, env, timeout)
        if Path(argv[0]).name == "uv" and "pytest" in argv and self.after_pytest:
            self.after_pytest()
        return ran

    def probes(self) -> list[list[str]]:
        return [argv for argv, _env in self.calls if Path(argv[0]).name == "pg_isready"]


class FakeChild:
    def __init__(self, gate: GateRunner) -> None:
        self.pid = 4242
        self.returncode: int | None = None
        self.signals: list[int] = []
        self._gate = gate

    def poll(self):
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)
        self.returncode = 0
        self._gate.up = False

    def kill(self):
        self.returncode = -9
        self._gate.up = False

    def wait(self, timeout=None):
        return self.returncode if self.returncode is not None else 0


class ServiceRunner:
    """`testservices.ProcessRunner` for a service the GATE starts: its server
    answers the gate's probe once spawned, until it is stopped."""

    def __init__(self, gate: GateRunner, *, initdb_status: int = 0) -> None:
        self.gate = gate
        self.initdb_status = initdb_status
        self.calls: list[str] = []
        self.children: list[FakeChild] = []
        self.roots: list[Path] = []

    def run(self, argv, *, env, timeout, stdin=None):
        name = Path(argv[0]).name
        self.calls.append(name)
        if name == "initdb":
            data = Path(argv[argv.index("-D") + 1])
            self.roots.append(data.parent)
            if self.initdb_status == 0:
                data.mkdir(parents=True)
            return self.initdb_status, "initdb: could not create directory: No space left"
        return 0, ""

    def spawn(self, argv, *, env, log):
        self.calls.append("spawn")
        Path(log).write_text("")
        child = FakeChild(self.gate)
        self.children.append(child)
        self.gate.up = True
        return child

    def set_oom_score_adj(self, pid, value):
        self.calls.append(f"oom={value}")


# ---------------------------------------------------------------------------
# The checkout and the gate
# ---------------------------------------------------------------------------


def _setup(tmp_path: Path, *, declaration: str | None, report: str, report_up: str | None = None,
           in_diff: bool = False, guards: bool = False) -> tuple[Path, dict[str, str]]:
    """A checkout whose changed test file graph-pytest selects. `declaration`
    is on the base branch, or -- `in_diff` -- in the change itself."""
    repo, _ = _fixture_repo(tmp_path, snapshot=False)
    if declaration is not None and not in_diff:
        _change(repo, testservices.DECLARATION_PATH, declaration)
    if guards:
        _change(repo, prepublish.GUARDS_SCRIPT,
                "#!/usr/bin/env bash\n"
                'for a in "$@"; do [ "$a" = --list ] && exit 0; done\n'
                'printf \'{"tool": "guards", "url": "%s"}\\n\' "${DATABASE_URL:-}" '
                '>> "$TOOL_LOG"\nexit 0\n')
    base = _git(repo, "rev-parse", "HEAD")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    uv = bin_dir / "uv"
    uv.write_text(f"#!{sys.executable}" + FAKE_UV)
    uv.chmod(0o755)
    (tmp_path / "junit.xml").write_text(report)
    env = {**_env(tmp_path, bin_dir), "FAKE_JUNIT": str(tmp_path / "junit.xml"),
           prepublish.CLONE_BASE_ENV: base, "TMPDIR": str(tmp_path)}
    if report_up is not None:
        (tmp_path / "junit-up.xml").write_text(report_up)
        env["FAKE_JUNIT_UP"] = str(tmp_path / "junit-up.xml")
    if declaration is not None and in_diff:
        _change(repo, testservices.DECLARATION_PATH, declaration)
    _change(repo, "tests/test_mod.py", MOD_TEST + "\n\ndef test_db():\n    pass\n")
    return repo, env


def _gate(repo: Path, tmp_path: Path, env: dict[str, str], turns: Turns, runner: GateRunner,
          services: ServiceRunner | None = None) -> prepublish.Gate:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(exist_ok=True)
    return prepublish.Gate(repo=repo, env=env, secrets=(), resume=turns,
                           remaining=lambda: 3600.0, log=_log(), artifacts_dir=artifacts,
                           runner=runner, service_runner=services)


def _check(record: dict, name: str, round_: int = 0) -> dict:
    (found,) = [c for c in record["rounds"][round_] if c["name"] == name]
    return found


def _advisory(tmp_path: Path) -> str:
    return (tmp_path / "artifacts" / prepublish.ADVISORY_SECTION_NAME).read_text()


def _uv_envs(tmp_path: Path) -> list[dict]:
    return [c["env"] for c in _tool_calls(tmp_path) if c["tool"] == "uv"]


# ---------------------------------------------------------------------------
# §5.3, row by row
# ---------------------------------------------------------------------------


def test_row1_nothing_declared_and_every_test_refused_is_skipped_as_today(tmp_path):
    repo, env = _setup(tmp_path, declaration=None, report=_all_refused(5432))
    runner, turns = GateRunner(up=False), Turns()
    record = _gate(repo, tmp_path, env, turns, runner).run()
    check = _check(record, "graph-pytest")
    # MUTATION: a declaration's handling leaking into the undeclared case.
    assert check["status"] == "skipped", check
    assert check["reason"].startswith("a service the tests need is unreachable "
                                      "(127.0.0.1:5432); every selected test errored")
    assert "not declared" not in check["reason"]
    # MUTATION: a fix round for a service nobody declared.
    assert turns.prompts == [] and record["fix_turn"] is False
    # MUTATION: probing or starting a service when no file declares one.
    assert runner.probes() == []
    assert "test-services" not in [c["name"] for c in record["rounds"][0]]
    # MUTATION: §5.1's variables set without a declaration.
    assert all(v is None for e in _uv_envs(tmp_path) for v in e.values())


def test_row2_declared_and_ready_a_failing_test_is_failed_with_a_fix_round(tmp_path):
    repo, env = _setup(tmp_path, declaration=DECLARATION, report=_all_refused(5432),
                       report_up=_junit(_failure("test_covered"), _passed("test_db")))
    runner, turns = GateRunner(up=True), Turns()
    record = _gate(repo, tmp_path, env, turns, runner).run()
    # MUTATION: the checks run without §5.1's environment (the stand-in then
    # reports the refusals, and the check is skipped).
    assert _check(record, "graph-pytest")["status"] == "failed"
    # MUTATION: no fix round for a declared service's failing test.
    assert record["fix_turn"] is True and len(turns.prompts) == 1
    assert "graph-pytest" in turns.prompts[0]
    # MUTATION: the worker's running service ignored and a second one started.
    assert runner.probes() and all("127.0.0.1" in argv for argv in runner.probes())


def test_row3_refused_at_an_address_no_declared_service_owns_is_skipped_naming_it(tmp_path):
    repo, env = _setup(tmp_path, declaration=DECLARATION, report=_all_refused(5432),
                       report_up=_all_refused(6379))
    runner, turns = GateRunner(up=True), Turns()
    record = _gate(repo, tmp_path, env, turns, runner).run()
    check = _check(record, "graph-pytest")
    # MUTATION: every refusal read as the declared service's (failed).
    assert check["status"] == "skipped", check
    # MUTATION: the reason without the address or without the hint.
    assert "127.0.0.1:6379" in check["reason"]
    assert "not declared in .swarm/test-services.toml" in check["reason"]
    assert turns.prompts == [] and record["failed"] == []


def test_row4_refused_at_the_declared_address_while_it_runs_is_failed(tmp_path):
    repo, env = _setup(tmp_path, declaration=DECLARATION, report=_all_refused(5432),
                       report_up=_all_refused(5432))
    runner, turns = GateRunner(up=True), Turns()
    record = _gate(repo, tmp_path, env, turns, runner).run()
    # MUTATION: today's rule applied unchanged (skipped): the service was
    # there, so the tests connected wrongly, and that is the agent's to fix.
    assert _check(record, "graph-pytest")["status"] == "failed"
    assert record["fix_turn"] is True and len(turns.prompts) == 1
    # MUTATION: a fix prompt that does not say the declared service was up.
    assert "127.0.0.1:5432" in turns.prompts[0] and "declared postgres" in turns.prompts[0]


def test_row5_the_workers_service_stopping_during_the_run_is_an_error_without_a_fix_round(
        tmp_path):
    repo, env = _setup(tmp_path, declaration=DECLARATION, report=_all_refused(5432),
                       report_up=_junit(_failure("test_covered")))
    runner, turns = GateRunner(up=True), Turns()

    def dies() -> None:
        runner.up = False
    runner.after_pytest = dies
    record = _gate(repo, tmp_path, env, turns, runner).run()
    check = _check(record, "graph-pytest")
    # MUTATION: no probe after the pytest check (failed, with a fix round).
    assert check["status"] == "error", check
    assert "the declared postgres stopped during the run" in check["reason"]
    assert "no longer answers on 127.0.0.1:5432" in check["reason"]
    # MUTATION: an error sent to the agent as something to fix.
    assert turns.prompts == [] and record["fix_turn"] is False


def test_row5_a_service_the_gate_started_killed_by_the_oom_killer_says_so(tmp_path):
    repo, env = _setup(tmp_path, declaration=DECLARATION, report=_all_refused(5432),
                       report_up=_junit(_failure("test_covered")))
    runner, turns = GateRunner(up=False), Turns()
    services = ServiceRunner(runner)

    def killed() -> None:
        services.children[-1].returncode = -9
        runner.up = False
    runner.after_pytest = killed
    record = _gate(repo, tmp_path, env, turns, runner, services).run()
    check = _check(record, "graph-pytest")
    assert check["status"] == "error", check
    # MUTATION: the exit status read but not explained (signal 9 is the OOM killer).
    assert "the declared postgres stopped during the run" in check["reason"]
    assert "signal 9" in check["reason"] and "OOM" in check["reason"]
    assert turns.prompts == []


def test_row6_a_service_that_does_not_start_is_an_error_and_the_tests_skip_as_row1(tmp_path):
    repo, env = _setup(tmp_path, declaration=DECLARATION, report=_all_refused(5432))
    runner, turns = GateRunner(up=False), Turns()
    services = ServiceRunner(runner, initdb_status=1)
    record = _gate(repo, tmp_path, env, turns, runner, services).run()
    service = _check(record, "test-services")
    # MUTATION: a service that did not start reported as passed, or not at all.
    assert service["status"] == "error", service
    # MUTATION: the gate never trying to start what nothing answers for.
    assert services.calls[:1] == ["initdb"]
    check = _check(record, "graph-pytest")
    # MUTATION: §5.1's variables set for a service that is not there (the
    # refusals would then read as row 4, failed).
    assert check["status"] == "skipped" and "not declared" not in check["reason"]
    assert all(e["DATABASE_URL"] is None and e["SWARM_TEST_SERVICES"] is None
               for e in _uv_envs(tmp_path))
    assert turns.prompts == [] and record["fix_turn"] is False
    # MUTATION: the start error in the failure section rather than the advisory one.
    assert _section(tmp_path) is None
    advisory = _advisory(tmp_path)
    assert "test-services" in advisory and "initdb exited 1" in advisory


def test_row7_an_invalid_declaration_the_change_made_is_failed_with_a_fix_round(tmp_path):
    bad = '[postgres]\nversion = 16\ndatabases = ["app_test"]\nimage = "postgres:16"\n'
    repo, env = _setup(tmp_path, declaration=bad, report=_all_refused(5432), in_diff=True)
    runner, turns = GateRunner(up=False), Turns()
    services = ServiceRunner(runner)
    record = _gate(repo, tmp_path, env, turns, runner, services).run()
    # MUTATION: the agent's own invalid file not reported, or not as failed.
    assert _check(record, "test-services")["status"] == "failed"
    assert record["fix_turn"] is True and len(turns.prompts) == 1
    # MUTATION: a fix prompt without the refusal naming the key.
    assert "postgres.image" in turns.prompts[0]
    # MUTATION: an invalid declaration starting anything.
    assert services.calls == []
    assert _check(record, "graph-pytest")["status"] == "skipped"


def test_row8_an_invalid_declaration_from_the_base_is_an_advisory_error(tmp_path):
    bad = '[redis]\nversion = 7\ndatabases = ["x"]\n'
    repo, env = _setup(tmp_path, declaration=bad, report=_all_refused(5432))
    runner, turns = GateRunner(up=False), Turns()
    services = ServiceRunner(runner)
    record = _gate(repo, tmp_path, env, turns, runner, services).run()
    # MUTATION: the base branch's file blamed on the agent (failed).
    assert _check(record, "test-services")["status"] == "error"
    assert turns.prompts == [] and record["fix_turn"] is False and record["failed"] == []
    assert services.calls == []
    # MUTATION: the tests not run as if nothing were declared.
    assert _check(record, "graph-pytest")["status"] == "skipped"
    # MUTATION: the advisory error in the failure section, or nowhere.
    assert _section(tmp_path) is None
    assert "redis: not a catalogue service" in _advisory(tmp_path)


# ---------------------------------------------------------------------------
# §5.1: the environment reaches the checks, never the gate's own
# ---------------------------------------------------------------------------


def test_the_service_environment_reaches_the_pytest_checks_and_never_the_gates_own(tmp_path):
    repo, env = _setup(tmp_path, declaration=DECLARATION, report=_all_refused(5432),
                       report_up=_junit(_passed("test_covered"), _passed("test_db")),
                       guards=True)
    before = dict(env)
    runner, turns = GateRunner(up=True), Turns()
    gate = _gate(repo, tmp_path, env, turns, runner)
    record = gate.run()
    assert _check(record, "graph-pytest")["status"] == "passed"
    (seen,) = _uv_envs(tmp_path)
    url = "postgresql://" + "postgres" + "@127.0.0.1:5432/app_test"
    # MUTATION: any §5.1 variable missing, or the second database as the default.
    assert seen == {"PGHOST": "127.0.0.1", "PGPORT": "5432", "PGUSER": "postgres",
                    "PGDATABASE": "app_test", "DATABASE_URL": url,
                    "SWARM_TEST_SERVICES": "postgres"}
    # MUTATION: the environment given to graph-pytest alone, not the guard script.
    (guards,) = [c for c in _tool_calls(tmp_path) if c["tool"] == "guards"]
    assert guards["url"] == url
    # MUTATION: the variables written into the gate's own environment, which
    # the agent's session continues with.
    assert gate.env == before and "DATABASE_URL" not in env
    # MUTATION: the gate's own commands (git, the probe) given the variables.
    own = [e for argv, e in runner.calls if Path(argv[0]).name in ("git", "pg_isready")]
    assert own and all(not set(SERVICE_VARS) & set(e) for e in own)


# ---------------------------------------------------------------------------
# §5.2: the order inside the gate
# ---------------------------------------------------------------------------


def test_a_service_the_gate_starts_is_stopped_after_each_round_and_started_afresh(tmp_path):
    repo, env = _setup(tmp_path, declaration=DECLARATION, report=_all_refused(5432),
                       report_up=_junit(_failure("test_covered")), in_diff=True)
    runner, turns = GateRunner(up=False), Turns()
    services = ServiceRunner(runner)
    record = _gate(repo, tmp_path, env, turns, runner, services).run()
    # MUTATION: the agent's own new declaration ignored (§2.3): nothing started.
    assert services.calls.count("spawn") == 2 and record["fix_turn"] is True
    # MUTATION: the gate's own server left running, or reused by the fix round.
    first, second = services.children
    assert first.signals and second.signals and first is not second
    # MUTATION: its data kept after the round.
    assert services.roots and not any(root.exists() for root in services.roots)
    # MUTATION: the data put inside the checkout.
    assert not any(root.resolve().is_relative_to(repo.resolve()) for root in services.roots)
    assert "oom=1000" in services.calls


def test_the_workers_service_is_probed_never_started_and_never_stopped(tmp_path):
    repo, env = _setup(tmp_path, declaration=DECLARATION, report=_all_refused(5432),
                       report_up=_junit(_passed("test_covered")))
    runner, turns = GateRunner(up=True), Turns()
    services = ServiceRunner(runner)
    _gate(repo, tmp_path, env, turns, runner, services).run()
    # MUTATION: a second server started beside the worker's on the same port.
    assert services.calls == []
    # MUTATION: the probe skipped after the pytest check (one probe only).
    assert len(runner.probes()) >= 2
    # MUTATION: the gate stopping the worker's whole-attempt service (Q4).
    assert runner.up is True


def test_no_check_that_runs_pytest_means_no_service_is_touched(tmp_path):
    repo, env = _setup(tmp_path, declaration=DECLARATION, report=_all_refused(5432))
    _change(repo, "tests/test_mod.py", MOD_TEST)  # back to the base's test file
    _change(repo, "README.md", "changed\n")
    runner, turns = GateRunner(up=False), Turns()
    services = ServiceRunner(runner)
    record = _gate(repo, tmp_path, env, turns, runner, services).run()
    # MUTATION: a service started for a change no pytest check covers.
    assert runner.probes() == [] and services.calls == []
    assert "test-services" not in [c["name"] for c in record["rounds"][0]]


# ---------------------------------------------------------------------------
# The issue's acceptance pair
# ---------------------------------------------------------------------------


def test_acceptance_declared_and_failing_is_failed_the_same_tree_undeclared_is_skipped(tmp_path):
    reports = {"report": _all_refused(5432),
               "report_up": _junit(_setup_error("test_covered", "psycopg.errors."
                                                "UndefinedTable: relation \"t\" does not exist"),
                                   _failure("test_db"))}
    declared_dir, plain_dir = tmp_path / "declared", tmp_path / "plain"
    declared_dir.mkdir()
    plain_dir.mkdir()

    repo, env = _setup(declared_dir, declaration=DECLARATION, **reports)
    runner, turns = GateRunner(up=True), Turns()
    record = _gate(repo, declared_dir, env, turns, runner).run()
    # MUTATION: the declared service's broken test reaching CI uncaught.
    assert _check(record, "graph-pytest")["status"] == "failed"
    assert record["fix_turn"] is True and len(turns.prompts) == 1

    repo, env = _setup(plain_dir, declaration=None, **reports)
    runner, turns = GateRunner(up=False), Turns()
    record = _gate(repo, plain_dir, env, turns, runner).run()
    # MUTATION: the undeclared repository's behaviour changed by this lane.
    check = _check(record, "graph-pytest")
    assert check["status"] == "skipped" and "127.0.0.1:5432" in check["reason"]
    assert record["fix_turn"] is False and turns.prompts == []


def test_the_service_environment_is_built_from_the_first_declared_database():
    service = testservices.DeclaredService(name="postgres", version=16,
                                           databases=("first_db", "second_db"))
    out = prepublish.service_env([service])
    # MUTATION: a password, a non-loopback host, or the last database.
    assert out["DATABASE_URL"].endswith("@127.0.0.1:5432/first_db")
    assert ":" not in out["DATABASE_URL"].split("//", 1)[1].split("@", 1)[0]
    assert out["PGDATABASE"] == "first_db" and out["SWARM_TEST_SERVICES"] == "postgres"

