"""graph-pytest is `skipped`, not failed, when every test errors because a service is unreachable.

Issue #1605, lane PREPUB-SERVICES. Measured 2026-10-19 on four product-oracle
lanes: the agent container has no Postgres, so every selected test errored at
setup with `Connect call failed ('127.0.0.1', 5432)`, the check reported
FAILED, and a paid fix round started that could not succeed and was not about
the lane's code. The same files pass against a real Postgres (95/95, 113/113,
118/118).

A check that cannot reach a service its tests need could not run, the same
class as "uv is not on PATH": `skipped`, with the address in the reason, and
never in the fix prompt. Conservative on purpose: a single assertion failure,
a single pass, or a single setup error that is anything else (an ImportError,
a fixture bug) keeps the check FAILED -- only an all-unreachable run is
skipped.

The gate reads pytest's junitxml report, not the console text. The stand-in
`uv` here writes the report pytest would have written to the `--junitxml=`
path it was given.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import pytest

from agent_worker import prepublish

from test_prepublish_gate import Turns, _change, _env, _gate, _section, _tool_calls
from test_prepublish_graph_gate import MOD_TEST, _fixture_repo

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

#: `uv run pytest ... --junitxml=<path>`: writes $FAKE_JUNIT's text to <path>
#: and exits 1, as pytest does when a test errored.
FAKE_UV = r'''
import json, os, sys
with open(os.environ["TOOL_LOG"], "a") as f:
    f.write(json.dumps({"tool": "uv", "argv": sys.argv[1:]}) + "\n")
report = [a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--junitxml=")]
if report and os.environ.get("FAKE_JUNIT"):
    with open(report[-1], "w") as out, open(os.environ["FAKE_JUNIT"]) as src:
        out.write(src.read())
print("E   ConnectionRefusedError: console text the gate must not need")
sys.exit(int(os.environ.get("FAKE_EXIT", "1")))
'''

REFUSED = "ConnectionRefusedError: [Errno 111] Connect call failed ('127.0.0.1', 5432)"
IMPORT_ERROR = "ModuleNotFoundError: No module named 'oracle_api'"


def _setup_error(name: str, text: str) -> str:
    message = quoteattr(f'failed on setup with "{text}"')
    return (f'<testcase classname="tests.test_mod" name="{name}" time="0.01">'
            f"<error message={message}>{escape('Traceback ...' + chr(10) + 'E   ' + text)}"
            "</error></testcase>")


def _failure(name: str) -> str:
    return (f'<testcase classname="tests.test_mod" name="{name}" time="0.01">'
            '<failure message="assert 2 == 1">assert 2 == 1</failure></testcase>')


def _passed(name: str) -> str:
    return f'<testcase classname="tests.test_mod" name="{name}" time="0.01" />'


def _junit(*cases: str) -> str:
    return ('<?xml version="1.0" encoding="utf-8"?><testsuites>'
            f'<testsuite name="pytest" tests="{len(cases)}">{"".join(cases)}</testsuite>'
            "</testsuites>")


def _setup(tmp_path: Path, report: str) -> tuple[Path, str, dict[str, str]]:
    """A checkout whose changed test file graph-pytest selects, a stand-in uv
    that reports `report`, and the gate's environment."""
    repo, base = _fixture_repo(tmp_path, snapshot=False)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    uv = bin_dir / "uv"
    uv.write_text(f"#!{sys.executable}" + FAKE_UV)
    uv.chmod(0o755)
    (tmp_path / "junit.xml").write_text(report)
    env = {**_env(tmp_path, bin_dir), "FAKE_JUNIT": str(tmp_path / "junit.xml"),
           prepublish.CLONE_BASE_ENV: base}
    _change(repo, "tests/test_mod.py", MOD_TEST + "\n\ndef test_db():\n    pass\n")
    return repo, base, env


def _graph_pytest(repo: Path, base: str, env: dict[str, str]) -> prepublish.Outcome:
    checks = {c.name: c for c in prepublish.select_checks(repo, ["tests/test_mod.py"],
                                                          base=base, env=env)}
    (outcome,) = prepublish.run_checks(repo, [checks["graph-pytest"]], env=env)
    return outcome


def test_every_test_refused_at_setup_is_skipped_naming_the_address_and_takes_no_fix_turn(
        tmp_path):
    repo, _base, env = _setup(tmp_path, _junit(_setup_error("test_covered", REFUSED),
                                               _setup_error("test_db", REFUSED)))
    turns = Turns()
    record = _gate(repo, tmp_path, env, turns).run()
    (round_,) = record["rounds"]
    (check,) = [c for c in round_ if c["name"] == "graph-pytest"]
    assert check["status"] == "skipped", check
    assert "127.0.0.1:5432" in check["reason"] and "unreachable" in check["reason"]
    assert turns.prompts == [] and record["fix_turn"] is False and record["failed"] == []
    assert _section(tmp_path) is None
    advisory = (tmp_path / "artifacts" / prepublish.ADVISORY_SECTION_NAME).read_text()
    assert "graph-pytest skipped (" in advisory and "127.0.0.1:5432" in advisory
    # The gate read pytest's own report, written outside the checkout.
    (call,) = [c for c in _tool_calls(tmp_path) if c["tool"] == "uv"]
    (arg,) = [a for a in call["argv"] if a.startswith("--junitxml=")]
    assert not Path(arg.split("=", 1)[1]).resolve().is_relative_to(repo.resolve())


def test_a_psycopg_refusal_to_localhost_is_skipped_too(tmp_path):
    text = ('psycopg.OperationalError: connection failed: connection to server at '
            '"localhost" (::1), port 5432 failed: Connection refused')
    repo, base, env = _setup(tmp_path, _junit(_setup_error("test_covered", text)))
    outcome = _graph_pytest(repo, base, env)
    assert outcome.status == "skipped" and "localhost:5432" in (outcome.reason or "")


def test_one_assertion_failure_among_the_refusals_is_failed(tmp_path):
    repo, base, env = _setup(tmp_path, _junit(_setup_error("test_covered", REFUSED),
                                              _failure("test_db")))
    assert _graph_pytest(repo, base, env).status == "failed"


def test_a_setup_import_error_is_failed(tmp_path):
    repo, base, env = _setup(tmp_path, _junit(_setup_error("test_covered", IMPORT_ERROR),
                                              _setup_error("test_db", IMPORT_ERROR)))
    assert _graph_pytest(repo, base, env).status == "failed"


def test_an_import_error_beside_the_refusals_is_failed(tmp_path):
    repo, base, env = _setup(tmp_path, _junit(_setup_error("test_covered", REFUSED),
                                              _setup_error("test_db", IMPORT_ERROR)))
    assert _graph_pytest(repo, base, env).status == "failed"


def test_a_pass_beside_the_refusals_is_failed(tmp_path):
    repo, base, env = _setup(tmp_path, _junit(_setup_error("test_covered", REFUSED),
                                              _passed("test_db")))
    outcome = _graph_pytest(repo, base, env)
    assert outcome.status == "failed"
    turns = Turns()
    record = _gate(repo, tmp_path, env, turns).run()
    assert record["fix_turn"] is True and len(turns.prompts) == 1


def test_a_refusal_to_a_remote_host_is_failed(tmp_path):
    remote = "ConnectionRefusedError: [Errno 111] Connect call failed ('10.4.0.7', 5432)"
    repo, base, env = _setup(tmp_path, _junit(_setup_error("test_covered", remote)))
    assert _graph_pytest(repo, base, env).status == "failed"


def test_no_report_is_failed(tmp_path):
    repo, base, env = _setup(tmp_path, "")
    env["FAKE_JUNIT"] = ""
    assert _graph_pytest(repo, base, env).status == "failed"
