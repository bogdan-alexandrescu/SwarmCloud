"""The unit shard weights are refreshed from main's green runs by a schedule,
not by hand.

#2364. tests/unit/scripts/unit_test_durations.json was refreshed only by a
person following a gh/jq recipe in docs/ci.md, and new test files outran it:
1069 of 1337 weighted at 82638be8, and the old hard 80 % floor turned main and
every pull request red until #2312 refreshed it. The floor is report-only now
(test_unit_shards.py); this file holds the refresh that keeps it from firing:

  * scripts/lib/merge_unit_weights.py, the merge that replaced `jq -S -s add`:
    a measured weight replaces the stored one, a file that no longer exists is
    dropped, an existing file nobody measured keeps its stored weight, and the
    output is the exact format application.yml's shards write -- so a refresh
    that changes nothing produces no diff and no pull request;
  * .github/workflows/unit-weights-refresh.yml: schedule and dispatch only, on
    main's ref, the merge App's token (a pull request opened with the
    GITHUB_TOKEN starts no CI), and the logic in
    scripts/refresh-unit-weights.sh, not restated inline.

All offline: no network, no gh.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
MERGE = REPO / "scripts" / "lib" / "merge_unit_weights.py"
SCRIPT = REPO / "scripts" / "refresh-unit-weights.sh"
WORKFLOW = REPO / ".github" / "workflows" / "unit-weights-refresh.yml"


def _module():
    spec = importlib.util.spec_from_file_location("merge_unit_weights", MERGE)
    assert spec and spec.loader, f"{MERGE} is missing"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tree(root: Path, files: list[str]) -> None:
    for name in files:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_x():\n    pass\n")


def _shard(measured: Path, k: int, weights: dict[str, float]) -> None:
    # `gh run download -p 'unit-durations-*' -D DIR` writes one directory per artifact.
    out = measured / f"unit-durations-{k}" / "unit-durations.json"
    out.parent.mkdir(parents=True)
    out.write_text(json.dumps(weights, indent=1, sort_keys=True) + "\n")


def _merge_cli(tmp_path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(MERGE), "--root", str(tmp_path / "repo"),
         "--stored", str(tmp_path / "stored.json"), "--measured", str(tmp_path / "measured"),
         "--out", str(tmp_path / "out.json"), *extra],
        capture_output=True, text=True, timeout=60,
    )


def test_measured_weights_replace_stored_and_deleted_files_drop():
    merge = _module().merge
    stored = {
        "tests/unit/a/test_kept.py": 3.0,
        "tests/unit/a/test_remeasured.py": 40.0,
        "tests/unit/a/test_deleted.py": 9.9,
    }
    measured = [
        {"tests/unit/a/test_remeasured.py": 12.34, "tests/unit/b/test_new.py": 0.04},
        # A shard that ran a file outside tests/unit (a stray junit `file`), and one
        # naming a test file that is not in the tree: neither is a weight.
        {"apps/worker/test_elsewhere.py": 5.0, "tests/unit/b/test_gone.py": 2.0},
    ]
    present = ["tests/unit/a/test_kept.py", "tests/unit/a/test_remeasured.py", "tests/unit/b/test_new.py",
               "apps/worker/test_elsewhere.py"]
    merged = merge(stored, measured, present)
    # MUTATION: keep the stored 40.0 (stored over measured, jq's `add` the wrong
    # way round) or keep the deleted file's 9.9: red.
    assert merged == {
        "tests/unit/a/test_kept.py": 3.0,
        "tests/unit/a/test_remeasured.py": 12.3,
        "tests/unit/b/test_new.py": 0.1,
    }
    assert list(merged) == sorted(merged)
    assert all(isinstance(v, float) and v > 0 for v in merged.values())


def test_unmeasured_existing_files_keep_their_weight():
    merge = _module().merge
    stored = {"tests/unit/x/test_one.py": 7.5, "tests/unit/x/test_two.py": 0.3}
    present = ["tests/unit/x/test_one.py", "tests/unit/x/test_two.py", "tests/unit/x/test_three.py"]
    # MUTATION: start from the measured values alone (the old recipe overwrote the
    # file with what one run measured): test_two's 0.3 is lost, red.
    merged = merge(stored, [{"tests/unit/x/test_one.py": 8.0}], present)
    assert merged == {"tests/unit/x/test_one.py": 8.0, "tests/unit/x/test_two.py": 0.3}
    # Nothing measured: the stored weights of present files, unchanged.
    assert merge(stored, [], present) == stored


def test_the_merge_writes_the_format_the_shards_write(tmp_path: Path):
    files = ["tests/unit/a/test_kept.py", "tests/unit/a/test_remeasured.py", "tests/unit/b/test_new.py",
             "tests/unit/b/helpers.py", "tests/unit/b/conftest.py"]
    _tree(tmp_path / "repo", files)
    (tmp_path / "stored.json").write_text(json.dumps(
        {"tests/unit/a/test_remeasured.py": 40.0, "tests/unit/a/test_kept.py": 3.0,
         "tests/unit/a/test_deleted.py": 1.0}))
    _shard(tmp_path / "measured", 1, {"tests/unit/a/test_remeasured.py": 12.3})
    _shard(tmp_path / "measured", 2, {"tests/unit/b/test_new.py": 0.5, "scripts/test_stray.py": 4.0})
    proc = _merge_cli(tmp_path)
    assert proc.returncode == 0, proc.stderr
    expected = {"tests/unit/a/test_kept.py": 3.0, "tests/unit/a/test_remeasured.py": 12.3,
                "tests/unit/b/test_new.py": 0.5}
    # application.yml's `record this shard's measured durations`: indent=1,
    # sort_keys, a trailing newline. MUTATION: `jq -S` (indent 2), or no newline: red.
    assert (tmp_path / "out.json").read_text() == json.dumps(expected, indent=1, sort_keys=True) + "\n"
    # The counts the shell script reads, so an empty merge is visible (rule zero).
    report = dict(line.split("=", 1) for line in proc.stdout.split())
    assert report == {
        "artifacts": "2",
        "measured": "2",
        "dropped": "1",
        "test_files": "3",
        "weighted_before": "2",
        "weighted_after": "3",
    }, proc.stdout


def test_the_merge_refuses_a_download_with_no_measurements(tmp_path: Path):
    _tree(tmp_path / "repo", ["tests/unit/test_a.py"])
    (tmp_path / "stored.json").write_text('{"tests/unit/test_a.py": 1.0}\n')
    (tmp_path / "measured").mkdir()
    proc = _merge_cli(tmp_path)
    # MUTATION: treat no artifact as "nothing changed" and exit 0: red. An empty
    # download is a failed refresh, never a clean one.
    assert proc.returncode != 0
    assert "unit-durations" in proc.stderr
    assert not (tmp_path / "out.json").exists()


@pytest.mark.parametrize("bad", ['{"tests/unit/test_a.py": "slow"}', '{"tests/unit/test_a.py": true}', "[1, 2]"])
def test_a_malformed_artifact_is_refused(tmp_path: Path, bad: str):
    _tree(tmp_path / "repo", ["tests/unit/test_a.py"])
    (tmp_path / "stored.json").write_text('{"tests/unit/test_a.py": 1.0}\n')
    out = tmp_path / "measured" / "unit-durations-1" / "unit-durations.json"
    out.parent.mkdir(parents=True)
    out.write_text(bad)
    proc = _merge_cli(tmp_path)
    assert proc.returncode != 0, proc.stdout
    assert not (tmp_path / "out.json").exists()


def _load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text()) or {}
    # PyYAML reads the bare key `on` as the boolean True.
    if True in data:
        data["on"] = data.pop(True)
    return data


def test_the_refresh_workflow_runs_on_schedule_from_main_with_the_merge_app_token():
    workflow = _load(WORKFLOW)
    # Never `pull_request`: the job holds the merge App's key.
    assert set(workflow["on"]) == {"schedule", "workflow_dispatch"}, workflow["on"]
    assert workflow["on"]["schedule"], "a schedule with no cron never runs"
    assert workflow.get("permissions") == {}
    (job_id, job), = workflow["jobs"].items()
    # Refused off main, in the job's own `if:` as well as by the environment.
    assert "github.ref == 'refs/heads/main'" in job["if"], job.get("if")
    assert job["environment"] == {"name": "repo-secrets", "deployment": False}
    assert job["timeout-minutes"] <= 30
    assert job["concurrency"]["group"] and job["concurrency"]["cancel-in-progress"] is False
    # The workflow's own token reads runs and downloads their artifacts; it writes nothing.
    assert job["permissions"] == {"contents": "read", "actions": "read"}, job["permissions"]
    steps = job["steps"]
    checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "${{ github.event.repository.default_branch }}"
    assert checkout["with"]["persist-credentials"] is False
    # A pull request opened, or a branch pushed, with the GITHUB_TOKEN starts no
    # workflow, so the refresh PR would sit with no CI. MUTATION: drop the mint
    # step, or open the PR with github.token: red.
    mint = next(s for s in steps if str(s.get("uses", "")).startswith("actions/create-github-app-token@"))
    assert mint["with"]["private-key"] == "${{ secrets.MERGE_APP_PRIVATE_KEY }}"
    assert mint["with"]["app-id"] == "${{ vars.MERGE_APP_ID }}"
    assert mint["with"]["permission-contents"] == "write"
    assert mint["with"]["permission-pull-requests"] == "write"
    run = next(s for s in steps if "scripts/refresh-unit-weights.sh" in str(s.get("run", "")))
    assert run["env"]["APP_TOKEN"] == f"${{{{ steps.{mint['id']}.outputs.token }}}}"
    assert run["env"]["GH_TOKEN"] == "${{ github.token }}"
    # The logic lives in the script, once: no gh or jq restated in the step.
    body = str(run["run"])
    for restated in ("gh ", "jq ", "git push", "merge_unit_weights"):
        assert restated not in body, f"the workflow step restates {restated!r}"


def test_the_refresh_script_is_strict_executable_and_uses_the_merge():
    text = SCRIPT.read_text()
    lines = [l for l in text.splitlines()[1:] if l.strip() and not l.lstrip().startswith("#")]
    assert text.startswith("#!/usr/bin/env bash\n")
    assert lines[0] == "set -euo pipefail"
    assert SCRIPT.stat().st_mode & 0o111, f"{SCRIPT} is not executable"
    assert "lib/common.sh" in text
    assert "merge_unit_weights.py" in text
    # Walks back through recent green push runs on main, and never labels `ready`:
    # a person merges the refresh.
    for needed in ("--event push", "--status success", "--branch main", "unit-durations-",
                   "chore/unit-weights-refresh"):
        assert needed in text, needed
    assert "--label" not in text and "--add-label" not in text
    # bash 3.2 (CLAUDE.md): no mapfile, no associative arrays.
    assert "mapfile" not in text and "declare -A" not in text
