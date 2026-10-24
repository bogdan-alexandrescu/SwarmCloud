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

And the group's verdict (`_cc_judge_test_services`), with no deployment: it
passes on the record a working gate leaves (measured on 2026-10-24 by
running `prepublish.Gate` over this fixture against a Postgres 16 that
`testservices.start` brought up) and fails, saying why, on each record a
defect leaves. lib.sh's own code runs; testlib's reporters print.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import subprocess
import tomllib
from pathlib import Path

import pytest
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


# ---------------------------------------------------------------------------
# The acceptance check's verdict, offline (scripts/acceptance/groups/claude-code.sh)
# ---------------------------------------------------------------------------

LIB = ROOT / "scripts" / "acceptance" / "lib.sh"
TASK = "task_" + "7a" * 10


def _bash(body: str, tmp_path: Path) -> subprocess.CompletedProcess:
    """`body` with lib.sh and the group sourced; testlib's reporters print."""
    script = f"""#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT={shlex.quote(str(ROOT))}
ACC_RUN_ID=run1
ACC_CHECK=""
ACC_REPOSITORY_URL=https://github.com/example/sandbox
ACC_GITHUB_REPO=example/sandbox
ACC_REF=main
ACC_ISSUE=1
t_pass() {{ printf 'PASS %s\\n' "$*"; }}
t_fail() {{ printf 'FAIL %s\\n' "$*"; }}
t_skip() {{ printf 'SKIP %s\\n' "$*"; }}
t_case() {{ printf 'CASE %s\\n' "$*"; }}
t_info() {{ printf 'INFO %s\\n' "$*"; }}
step() {{ :; }}
redact() {{ cat; }}
source {shlex.quote(str(LIB))}
source {shlex.quote(str(GROUP))}
ACC_WORK={shlex.quote(str(tmp_path))}
{body}
"""
    path = tmp_path / "run.sh"
    path.write_text(script)
    return subprocess.run(["bash", str(path)], capture_output=True, text=True, timeout=60,
                          check=False, env=dict(os.environ, NO_COLOR="1"))


def _round(graph: dict | None, *, services: dict | None = None) -> list[dict]:
    """A first round as the gate records it: ruff skipped (the fixture has no
    ruff config), graph-pytest as given, a test-services outcome first."""
    checks = [{"name": "ruff-check", "status": "skipped", "duration_seconds": 0.0, "files": 1,
               "reason": "the repository configures no ruff (no ruff.toml, no [tool.ruff])"}]
    if graph is not None:
        checks.append({"name": "graph-pytest", "duration_seconds": 1.6, "files": 1, **graph})
    if services is not None:
        checks.insert(0, {"name": "test-services", "duration_seconds": 0.0, "files": 1, **services})
    return checks


UNREACHABLE = ("a service the tests need is unreachable (127.0.0.1:5432); every selected test "
               "errored at setup connecting to it; CI runs it")


def _summary(rounds: list | None = None, *, fix_turn: bool = False, pr: int | None = None,
             stored: bool = True, **record) -> dict:
    """A result_summary. `stored` writes each round as Firestore holds it."""
    record = {"rebase": None, "fix_turn": fix_turn, "failed": [], "base": "a" * 40,
              "rounds": [{"items": r} if stored else r for r in (rounds or [])], **record}
    git = {"pull_request": {"number": pr}} if pr is not None else {"publish_reason": "collect"}
    return {"runner": {"status": "succeeded", "output": {"prepublish": record}}, "git": git}


def _judge(which: str, summary: dict | str, tmp_path: Path) -> list[str]:
    text = summary if isinstance(summary, str) else json.dumps(summary)
    result = _bash(f"_cc_judge_test_services {which} {shlex.quote(text)} {TASK}", tmp_path)
    assert result.returncode == 0, result.stderr
    return [line for line in result.stdout.splitlines() if line.startswith(("PASS", "FAIL"))]


def _verdicts(lines: list[str]) -> list[str]:
    return [line.split(" ", 1)[0] for line in lines]


DECLARED_OK = _summary([_round({"status": "failed"}), _round({"status": "passed"})], fix_turn=True)
UNDECLARED_OK = _summary([_round({"status": "skipped", "reason": UNREACHABLE})])


@pytest.mark.parametrize("stored", [True, False])
def test_the_record_a_working_gate_leaves_passes_both_halves(tmp_path, stored):
    declared = _summary([_round({"status": "failed"})], fix_turn=True, stored=stored)
    undeclared = _summary([_round({"status": "skipped", "reason": UNREACHABLE})], stored=stored)
    # MUTATION: reading `.rounds[0]` only as a list misses Firestore's {"items": ...}.
    assert _verdicts(_judge("declared", declared, tmp_path)) == ["PASS"] * 4
    assert _verdicts(_judge("undeclared", undeclared, tmp_path)) == ["PASS"] * 4


def test_a_declared_test_that_was_skipped_fails(tmp_path):
    # MUTATION: the worker never started Postgres: the defect #1627 is about.
    lines = _judge("declared", _summary([_round({"status": "skipped", "reason": UNREACHABLE})]),
                   tmp_path)
    assert lines[0].startswith("FAIL") and "expected 'failed', got 'skipped'" in lines[0]


def test_only_the_first_round_is_judged(tmp_path):
    # The fix turn may repair the test; round 2 passing is not the verdict.
    assert _verdicts(_judge("declared", DECLARED_OK, tmp_path)) == ["PASS"] * 4
    # MUTATION: judging the last round would read round 2's `passed` here.
    flipped = _summary([_round({"status": "passed"}), _round({"status": "failed"})], fix_turn=True)
    assert _judge("declared", flipped, tmp_path)[0].startswith("FAIL")


def test_a_declared_failure_with_no_fix_turn_fails(tmp_path):
    lines = _judge("declared", _summary([_round({"status": "failed"})], fix_turn=False,
                                        fix_skipped="no_session_or_budget"), tmp_path)
    # MUTATION: `.fix_turn // true` would read a missing or false fix turn as taken.
    assert [line for line in lines if "fix_turn" in line][0].startswith("FAIL")


def test_a_declared_service_that_did_not_start_fails(tmp_path):
    rounds = [_round({"status": "failed"}, services={
        "status": "error", "reason": "a declared service did not start; the tests ran without it"})]
    lines = _judge("declared", _summary(rounds, fix_turn=True), tmp_path)
    # MUTATION: ignoring test-services would pass a failure that is not the test's.
    assert any(line.startswith("FAIL") and "did not start" in line for line in lines)


@pytest.mark.parametrize("graph, words", [
    ({"status": "failed"}, "expected 'skipped', got 'failed'"),
    ({"status": "skipped", "reason": "uv is not on PATH in this image"}, "skipped for another reason"),
    ({"status": "passed"}, "expected 'skipped', got 'passed'"),
])
def test_an_undeclared_run_that_is_not_skipped_as_unreachable_fails(tmp_path, graph, words):
    lines = _judge("undeclared", _summary([_round(graph)]), tmp_path)
    # MUTATION: accepting any `skipped`, or any status, says nothing about the service.
    assert any(line.startswith("FAIL") and words in line for line in lines), lines


def test_a_pull_request_opened_by_the_step_fails(tmp_path):
    lines = _judge("declared", _summary([_round({"status": "failed"})], fix_turn=True, pr=12),
                   tmp_path)
    # MUTATION: not reading result_summary.git would let a published step pass.
    assert lines[-1].startswith("FAIL") and "#12" in lines[-1]


@pytest.mark.parametrize("summary, words", [
    ({"runner": {"output": {"truncated": True, "preview": "{"}}}, "8000-character preview"),
    ({"runner": {"output": {}}}, "recorded no pre-publish gate"),
    (_summary([], skipped="unknown_base"), "ran no round"),
    (_summary([_round(None)]), "ran no graph-pytest: ruff-check:skipped"),
    ("not json", "could not be read"),
])
def test_a_record_that_cannot_answer_fails_saying_why(tmp_path, summary, words):
    lines = _judge("declared", summary, tmp_path)
    # MUTATION: an absent record read as an empty pass, or as a crash of the suite.
    assert len(lines) == 1 and lines[0].startswith("FAIL") and words in lines[0], lines


_RUN_STUBS = r"""
N=0
acc_submit() {
  local var="$1" profile="$2" input="$3" extra="$4"
  N=$(( N + 1 ))
  printf 'SUBMIT %s %s %s\n' "${profile}" "$(jq -c '{repository_ref, max_attempts, strategy: (.strategy // null)}' <<<"${extra}")" "$(jq -r .prompt <<<"${input}")"
  printf -v "${var}" 'task_%02d' "${N}"
}
acc_door() { printf '422 invalid_dispatch'; }
api_fetch() { printf '{}' >"$2"; }
_cc_check_collect() { :; }
_cc_check_direct_pr() { :; }
_cc_check_issue() { :; }
_cc_check_model_class() { :; }
_cc_check_test_services() { printf 'JUDGE %s %s\n' "$1" "$2"; }
run_claude_code
"""


def test_the_group_submits_the_pair_as_collect_steps_on_their_branches_and_judges_each(tmp_path):
    result = _bash(_RUN_STUBS, tmp_path)
    assert result.returncode == 0, result.stderr
    submits = {}
    for line in result.stdout.splitlines():
        if line.startswith("SUBMIT"):
            _, profile, extra, prompt = line.split(" ", 3)
            submits[json.loads(extra)["repository_ref"]] = (profile, json.loads(extra), prompt)
    refs = (_group_value("CC_TS_DECLARED_REF"), _group_value("CC_TS_UNDECLARED_REF"))
    # MUTATION: both halves cloning one branch makes the pair compare a tree with itself.
    assert refs[0] != refs[1] and set(refs) <= set(submits)
    for ref in refs:
        profile, extra, prompt = submits[ref]
        # MUTATION: a direct-pr step opens a pull request on the sandbox per run.
        assert profile == "claude-code" and extra["strategy"] is None
        assert extra["max_attempts"] == 1
        assert _group_value("CC_TS_ASSERT_LINE").strip() in prompt and "do not commit" in prompt
    judged = [line.split(" ") for line in result.stdout.splitlines() if line.startswith("JUDGE")]
    ids = {ref: f"task_{i + 1:02d}" for i, ref in enumerate(
        json.loads(line.split(" ", 3)[2])["repository_ref"]
        for line in result.stdout.splitlines() if line.startswith("SUBMIT"))}
    # MUTATION: judging each half on the other's task swaps the verdicts.
    assert judged == [["JUDGE", "declared", ids[refs[0]]], ["JUDGE", "undeclared", ids[refs[1]]]]


def test_the_group_lists_both_checks(tmp_path):
    result = _bash("claude_code_checks", tmp_path)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    # MUTATION: a check the run reports but the list omits leaves --list and the
    # crash count short of what the run is answerable for.
    assert sum("graph-pytest failed, with a fix turn" in line for line in lines) == 1
    assert sum("graph-pytest skipped, 127.0.0.1:5432 unreachable" in line for line in lines) == 1
