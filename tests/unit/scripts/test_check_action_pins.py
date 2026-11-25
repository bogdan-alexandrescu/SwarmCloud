"""scripts/check-action-pins.sh: every third-party `uses:` in .github/ is
pinned to a full commit SHA with its version in a trailing comment (audit,
2026-11-21; the script's header says why).

No network and no credentials: the script reads files only.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "check-action-pins.sh"
APPLICATION = REPO / ".github" / "workflows" / "application.yml"
SHA = "0123456789abcdef0123456789abcdef01234567"


def _run(*paths: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "PROJECT_ID"}
    return subprocess.run(["bash", str(SCRIPT), *map(str, paths)], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=120)


def _workflow(tmp_path: Path, *uses: str, name: str = "w.yml") -> Path:
    lines = ["on: push", "jobs:", "  a:", "    runs-on: ubuntu-24.04", "    steps:"]
    lines += [f"      - uses: {u}" for u in uses]
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n")
    return path


def test_the_repositorys_own_actions_are_all_pinned():
    """MUTATION: put any `@v5` back in .github/."""
    proc = _run()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout == "", proc.stdout
    # It read something: an empty answer is not a pass.
    assert " uses: in " in proc.stderr and " 0 uses:" not in proc.stderr, proc.stderr


def test_what_it_accepts(tmp_path):
    path = _workflow(
        tmp_path,
        f"actions/checkout@{SHA} # v5.1.0",
        f"github/codeql-action/upload-sarif@{SHA} # v4.39.4",
        f'"actions/cache@{SHA}"  #  v4',
        "./.github/actions/release-apply",
        "docker://alpine@sha256:" + "ab" * 32,
    )
    proc = _run(path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "5 uses: in 1 file(s)" in proc.stderr, proc.stderr


def test_each_unpinned_form_is_refused_by_file_and_line(tmp_path):
    """MUTATION: accept a tag, a branch, a short SHA, an uppercase or 41-hex
    ref, a pin with no version comment, or a container by tag."""
    bad = [
        "actions/checkout@v5",
        "actions/checkout@main",
        "actions/checkout@0123456 # v5",
        f"actions/checkout@{SHA.upper()} # v5",
        f"actions/checkout@{SHA}0 # v5",
        f"actions/checkout@{SHA}",
        f"actions/checkout@{SHA} # pinned",
        "docker://alpine:3.20",
        "actions/checkout",
    ]
    path = _workflow(tmp_path, f"actions/cache@{SHA} # v4.3.0", *bad)
    proc = _run(path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    found = proc.stdout.splitlines()
    assert len(found) == len(bad), found
    for offset, (line, uses) in enumerate(zip(found, bad)):
        # Line 7 is the first bad `uses:` (five header lines, one good pin).
        assert line.startswith(f"{path}:{7 + offset}: "), line
        assert line.endswith(": " + uses.split(" #")[0]), line


def test_a_comment_is_not_a_uses_and_an_empty_tree_is_not_a_pass(tmp_path):
    good = tmp_path / "good"
    good.mkdir()
    (good / "w.yaml").write_text("jobs:\n  a:\n    steps:\n      # - uses: actions/checkout@v5\n"
                                 f"      - uses: actions/checkout@{SHA} # v5.1.0\n")
    proc = _run(good)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "README.md").write_text("uses: actions/checkout@v5\n")
    proc = _run(empty)
    assert proc.returncode == 1 and "nothing was checked" in proc.stderr, proc.stderr
    proc = _run(tmp_path / "absent")
    assert proc.returncode == 1, proc.stderr


def _raw_workflow(tmp_path: Path, *steps: str, name: str = "w.yml") -> Path:
    """A workflow whose step lines are written as given, in any YAML shape."""
    lines = ["on: push", "jobs:", "  a:", "    runs-on: ubuntu-24.04", "    steps:", *steps]
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n")
    return path


def test_a_uses_in_a_shape_it_cannot_read_is_refused_by_file_and_line(tmp_path):
    """Issue 190: a flow mapping and a quoted key are valid YAML that the
    block-style reader skipped, so a planted workflow with both reported "1
    finding of 1 uses:". A quoted block key is read as a block `uses:`; every
    other shape is refused. MUTATION: drop the guard, or skip quoted keys."""
    steps = [
        "      - uses: actions/checkout@v5",              # line 6: block, a tag
        "      - {uses: actions/setup-node@v4}",          # line 7: flow mapping
        '      - "uses": actions/cache@v4',               # line 8: quoted key, a tag
        "      - {name: x, 'uses': actions/cache@v4}",    # line 9: flow, quoted key
        "      - uses :  actions/checkout@v5",            # line 10: space before ':'
        "      - uses:",                                  # line 11: value on a later line
        "          actions/checkout@v5",
        f"      - 'uses': actions/cache@{SHA} # v4.3.0",  # line 13: quoted key, pinned
    ]
    path = _raw_workflow(tmp_path, *steps)
    proc = _run(path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    found = proc.stdout.splitlines()
    assert [line.split(": ", 1)[0] for line in found] == [
        f"{path}:{n}" for n in (6, 7, 8, 9, 10, 11)], found
    assert found[0].endswith("(owner/repo@<40 hex> # <version>): actions/checkout@v5"), found[0]
    assert found[2].endswith("(owner/repo@<40 hex> # <version>): actions/cache@v4"), found[2]
    for n in (1, 3, 4, 5):
        assert ": uses key in a shape this check cannot read (use block style): " in found[n], found[n]
    assert "1 finding" not in proc.stderr and "1 uses:" not in proc.stderr, proc.stderr
    assert "6 action(s)" in proc.stderr, proc.stderr


def test_a_pinned_flow_mapping_is_still_refused_and_a_comment_is_not_read(tmp_path):
    """Refusal is by shape, not by value: a pinned flow-mapping `uses` is
    still a finding, since the check cannot read it. A comment that mentions
    one is not. MUTATION: read comment lines, or accept a flow mapping."""
    path = _raw_workflow(
        tmp_path,
        "      # - {uses: x}",
        "      #   'uses': actions/cache@v4",
        f"      - uses: actions/checkout@{SHA} # v5.1.0",
    )
    proc = _run(path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "1 uses: in 1 file(s)" in proc.stderr, proc.stderr
    path = _raw_workflow(tmp_path, f"      - {{uses: actions/checkout@{SHA}}} # v5.1.0",
                         name="flow.yml")
    proc = _run(path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert proc.stdout.startswith(f"{path}:6: uses key in a shape this check cannot read"), proc.stdout


def test_it_reads_every_yaml_file_under_a_directory(tmp_path):
    """The composite actions under .github/actions are YAML files too, one
    directory down. MUTATION: read only the top directory, or only *.yml."""
    nested = tmp_path / "actions" / "x"
    nested.mkdir(parents=True)
    _workflow(nested, "actions/checkout@v5", name="action.yaml")
    proc = _run(tmp_path)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "action.yaml:6: " in proc.stdout, proc.stdout


def test_quick_checks_runs_it_with_the_workflow_wiring_check():
    """It is a `workflows-*` step of quick-checks, so the verdict step folds
    its result into `release workflow wiring (actionlint)` and ci-gate.
    MUTATION: drop the step, or give it another check's prefix."""
    steps = yaml.safe_load(APPLICATION.read_text())["jobs"]["quick-checks"]["steps"]
    (step,) = [s for s in steps if "check-action-pins.sh" in str(s.get("run", ""))]
    assert step["id"].startswith("workflows-"), step["id"]
    assert step["run"].strip() == "scripts/check-action-pins.sh", step["run"]
    actionlint = next(s for s in steps if s.get("id") == "workflows-actionlint")
    assert step["if"] == actionlint["if"], step["if"]


def test_the_script_follows_the_repositorys_script_rules():
    text = SCRIPT.read_text()
    effective = [l for l in text.splitlines()[1:] if l.strip() and not l.lstrip().startswith("#")]
    assert text.startswith("#!/usr/bin/env bash\n")
    assert effective[0] == "set -euo pipefail", effective[0]
    assert 'lib/common.sh"' in text
    assert os.access(SCRIPT, os.X_OK)
