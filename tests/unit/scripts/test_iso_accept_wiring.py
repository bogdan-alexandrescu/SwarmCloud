"""The cross-tenant Firestore probe can be run, and read, by acceptance (ISO-ACCEPT-WIRE).

Part of #1457 (tenant isolation design B). ISO-ACCEPT (PR 1478) built
tests/acceptance/fixtures/tenant-isolation/test_cross_tenant_firestore.py: from
a tenant-A worker pod it asks Firestore for another tenant's documents with
the worker's own ambient credential, and passes only on 403. Owner decision,
2026-10-20: acceptance runs it on every run, report-only until the repository
variable ISO_PROBE_REQUIRED is "true", so the cutover has its proof.

The acceptance group submits the probe as a `generic` pytest agent, so what it
can read back is the runner's record: `stdout_tail` on exit 0, and on any
other exit only `last_error`, which carries the END OF STDERR (runners/
generic.py). So each check writes one `ISO-ACCEPT-RESULT {json}` line to
stdout and to stderr, whatever it concluded, and a probe that could not measure at all
still writes one per check rather than erroring in its fixture. These tests
hold that, offline: the probe runs in a subprocess pytest against a copy of
the file with its metadata and Firestore calls replaced, never the network.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PROBE = ROOT / "tests" / "acceptance" / "fixtures" / "tenant-isolation" / "test_cross_tenant_firestore.py"
CI_MD = ROOT / "docs" / "ci.md"


def _probe():
    spec = importlib.util.spec_from_file_location("iso_accept_probe_under_test", PROBE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves a class's module through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# The result line
# --------------------------------------------------------------------------


def test_a_result_line_is_one_marked_json_line_naming_the_check_and_verdict():
    probe = _probe()
    line = probe.result_line(probe.Verdict("settings", False, "allowed by IAM\nsecond line"))
    # MUTATION: write the JSON without the marker, or with a different one --
    # the acceptance group finds the lines by RESULT_MARKER in a stderr tail.
    assert line.startswith(probe.RESULT_MARKER), line
    assert probe.RESULT_MARKER == "ISO-ACCEPT-RESULT "
    # MUTATION: json.dumps(..., indent=2) -- a line the group reads one at a time.
    assert "\n" not in line
    body = json.loads(line[len(probe.RESULT_MARKER):])
    assert body == {"check": "settings", "verdict": "FAIL", "reason": "allowed by IAM\nsecond line"}


def test_parse_results_reads_every_marked_line_out_of_a_tail_and_nothing_else():
    probe = _probe()
    lines = [probe.result_line(probe.Verdict(c, c != "lease", f"why {c}")) for c in probe.CHECKS]
    tail = "pytest exited 1\n..F" + lines[0] + "\nnoise ISO-ACCEPT-RESULT not json\n" + "\n".join(lines[1:])
    # MUTATION: anchor the marker at a line start -- pytest's progress dots
    # can share the line it is written on.
    assert probe.parse_results(tail) == {c: ("PASS" if c != "lease" else "FAIL") for c in probe.CHECKS}


def test_judge_reports_a_probe_that_could_not_measure_as_a_failed_check_not_an_error():
    probe = _probe()
    verdict = probe.judge(probe.NotMeasurable("no metadata server answered"), "tenant")
    # MUTATION: have the `prober` fixture call pytest.fail on NotMeasurable
    # again -- every test errors in setup and no result line is written.
    assert verdict.check == "tenant"
    assert verdict.denied is False
    assert verdict.reason.startswith("not measured: no metadata server answered"), verdict.reason


# --------------------------------------------------------------------------
# The pytest module, run as the generic runner runs it
# --------------------------------------------------------------------------

#: Loaded beside the copied probe as its conftest: replaces who-am-I and the
#: Firestore call on the collected module, so nothing reaches a network.
_CONFTEST = '''
import os
import pytest

@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    for item in items:
        module = item.module
        mode = os.environ["ISO_TEST_MODE"]
        if mode == "unmeasurable":
            def identity():
                raise module.NotMeasurable("no metadata server answered (test)")
        else:
            def identity():
                return module.Identity(project="p", email="swarm-agent-worker-smoke@p.iam.gserviceaccount.com",
                                       tenant="smoke", bearer=os.environ["ISO_TEST_BEARER"])
        module.identity = identity
        if mode == "denied":
            module._call = lambda *a, **k: (403, {"error": {"status": "PERMISSION_DENIED",
                                                            "message": "Missing or insufficient permissions."}})
        elif mode == "allowed":
            module._call = lambda *a, **k: (404, {"error": {"status": "NOT_FOUND", "message": "no document"}})
'''


def _run(tmp_path: Path, mode: str) -> tuple[subprocess.CompletedProcess[str], str]:
    here = tmp_path / "tenant-isolation"
    here.mkdir()
    (here / PROBE.name).write_text(PROBE.read_text())
    (here / "conftest.py").write_text(_CONFTEST)
    bearer = secrets.token_hex(24)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SWARM_ACCEPT_", "PYTEST_"))}
    env.update(ISO_TEST_MODE=mode, ISO_TEST_BEARER=bearer)
    # As the generic runner does: `python -m pytest -q --color=no <absolute path>`,
    # started in the file's own directory (which is how the probe opts in).
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--color=no", "-p", "no:cacheprovider", "-p", "no:xdist",
         str(here / PROBE.name)],
        cwd=here, env=env, capture_output=True, text=True, timeout=120, check=False,
    )
    return done, bearer


def _results(stream: str) -> list[dict]:
    return [json.loads(m.group(1)) for m in re.finditer(r"ISO-ACCEPT-RESULT (\{.*\})", stream)]


@pytest.mark.parametrize(
    ("mode", "exit_code", "verdict", "reason"),
    [
        ("denied", 0, "PASS", "denied: HTTP 403 PERMISSION_DENIED"),
        ("allowed", 1, "FAIL", "allowed by IAM"),
        ("unmeasurable", 1, "FAIL", "not measured: no metadata server answered (test)"),
    ],
)
def test_each_check_writes_its_result_line_to_both_streams_whatever_it_concluded(tmp_path, mode, exit_code, verdict, reason):
    done, bearer = _run(tmp_path, mode)
    assert done.returncode == exit_code, done.stdout[-2000:] + done.stderr[-2000:]
    # MUTATION: write the result line to only one of the two streams -- on a
    # red probe the generic runner keeps only stderr's tail (in last_error),
    # on a green one only stdout's, so the group could not read the verdicts.
    for stream in (done.stderr, done.stdout):
        results = _results(stream)
        assert [r["check"] for r in results] == ["settings", "tenant", "lease"], stream[-2000:]
        for result in results:
            assert result["verdict"] == verdict, result
            assert result["reason"].startswith(reason), result
    # MUTATION: put the bearer into a Verdict reason or Identity's repr.
    assert bearer not in done.stdout and bearer not in done.stderr


def test_pytest_naming_the_file_from_elsewhere_still_skips_and_writes_no_result(tmp_path):
    here = tmp_path / "tenant-isolation"
    here.mkdir()
    (here / PROBE.name).write_text(PROBE.read_text())
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SWARM_ACCEPT_", "PYTEST_"))}
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--color=no", "-p", "no:cacheprovider", "-p", "no:xdist",
         str(here / PROBE.name)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120, check=False,
    )
    # MUTATION: write the result line before the opt-in check -- a repository
    # check that names the changed file must not measure the live database.
    assert done.returncode == 0, done.stdout[-2000:]
    assert "3 skipped" in done.stdout
    assert not _results(done.stderr + done.stdout)


# --------------------------------------------------------------------------
# What the probe and docs/ci.md say about it
# --------------------------------------------------------------------------


def test_the_probe_no_longer_says_acceptance_does_not_run_it():
    text = PROBE.read_text()
    # MUTATION: restore "the acceptance workflow does not ask today".
    assert "does not ask today" not in text
    assert "ISO_PROBE_REQUIRED" in text
    assert "acceptance (tenant-isolation)" in text


def test_docs_ci_names_the_probe_job_its_report_only_mode_and_the_variable_that_makes_it_required():
    text = CI_MD.read_text()
    row = next(line for line in text.splitlines() if line.startswith("| `accept.yml` |"))
    # MUTATION: drop the job from the workflow table's accept.yml row.
    assert "`acceptance (tenant-isolation)`" in row, row
    assert "`ISO_PROBE_REQUIRED`" in row, row
    section = text.split("### The cross-tenant Firestore probe is report-only until the cutover", 1)
    # MUTATION: delete the section.
    assert len(section) == 2, "docs/ci.md has no section for the cross-tenant probe"
    body = " ".join(section[1].split("\n## ", 1)[0].split("\n### ", 1)[0].split())
    for needle in (
        "ISO_PROBE_REQUIRED",
        "test_cross_tenant_firestore.py",
        "docs/runbooks/isolation-cutover.md",
        "report-only",
        "after the last tenant is cut over",
    ):
        assert needle in body, f"the probe's section does not say {needle!r}"
