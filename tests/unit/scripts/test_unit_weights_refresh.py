"""The unit shard weights are refreshed by a script, not by a hand-typed recipe (#2364).

tests/unit/scripts/unit_test_durations.json was refreshed only by hand, so new
test files pushed its coverage under test_unit_shards.py's hard 80 % floor and
turned main and every pull request red. scripts/lib/merge_unit_weights.py
merges the `unit-durations-<k>` artifacts main's shards upload into the stored
file; scripts/refresh-unit-weights.sh finds main's newest green push run that
has them and runs the merge. Everything here is offline: `gh` is a fake on
PATH that answers from a fixture directory.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
MERGE = REPO / "scripts" / "lib" / "merge_unit_weights.py"
REFRESH = REPO / "scripts" / "refresh-unit-weights.sh"


def _merge_module():
    spec = importlib.util.spec_from_file_location("merge_unit_weights", MERGE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _tree(root: Path, files: list[str]) -> Path:
    for rel in files:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("def test_x():\n    pass\n")
    return root


def _artifacts(base: Path, shards: dict[int, dict[str, float]]) -> Path:
    for k, weights in shards.items():
        (base / f"unit-durations-{k}").mkdir(parents=True)
        (base / f"unit-durations-{k}" / "unit-durations.json").write_text(json.dumps(weights))
    return base


def test_a_measured_weight_replaces_a_stored_one_and_a_deleted_file_is_dropped(tmp_path):
    """MUTATION: keep a deleted file, let a stored weight win over a measured
    one, or drop a file the run did not measure."""
    merge = _merge_module()
    root = _tree(tmp_path / "repo", [
        "tests/unit/a/test_measured.py", "tests/unit/a/test_unmeasured.py", "tests/unit/a/test_new.py",
    ])
    stored = {
        "tests/unit/a/test_measured.py": 9.0,
        "tests/unit/a/test_unmeasured.py": 4.0,
        "tests/unit/a/test_deleted.py": 7.0,
    }
    measured = {
        "tests/unit/a/test_measured.py": 2.5,
        "tests/unit/a/test_new.py": 1.5,
        # A path outside tests/unit, and a file gone from the tree, are not weights.
        "apps/x/test_elsewhere.py": 3.0,
        "tests/unit/a/test_gone_since.py": 3.0,
    }
    out = merge.merge(stored, measured, root)
    assert out == {
        "tests/unit/a/test_measured.py": 2.5,
        "tests/unit/a/test_unmeasured.py": 4.0,
        "tests/unit/a/test_new.py": 1.5,
    }


def test_the_command_writes_sorted_indent_1_json_and_is_byte_stable(tmp_path):
    root = _tree(tmp_path / "repo", ["tests/unit/b/test_z.py", "tests/unit/a/test_a.py"])
    stored = root / "weights.json"
    stored.write_text(json.dumps({"tests/unit/b/test_z.py": 3.0}))
    downloads = _artifacts(tmp_path / "dl", {1: {"tests/unit/a/test_a.py": 1.0}, 2: {"tests/unit/b/test_z.py": 2.0}})
    cmd = ["python3", str(MERGE), "--root", str(root), "--stored", str(stored), "--measured", str(downloads)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    text = stored.read_text()
    assert text == '{\n "tests/unit/a/test_a.py": 1.0,\n "tests/unit/b/test_z.py": 2.0\n}\n', text
    assert "2 of 2 test files" in proc.stdout, proc.stdout
    # Run again on the same artifacts: not one byte changes.
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert stored.read_text() == text


def test_no_artifact_or_a_malformed_one_is_refused_and_writes_nothing(tmp_path):
    """An unread download is never "nothing to refresh". MUTATION: exit 0
    on an empty directory, or write the stored file anyway."""
    root = _tree(tmp_path / "repo", ["tests/unit/a/test_a.py"])
    stored = root / "weights.json"
    stored.write_text('{"tests/unit/a/test_a.py": 5.0}')
    empty = tmp_path / "empty"
    empty.mkdir()
    bad = tmp_path / "bad" / "unit-durations-1"
    bad.mkdir(parents=True)
    (bad / "unit-durations.json").write_text("[1, 2]")
    for downloads in (empty, bad.parent):
        proc = subprocess.run(
            ["python3", str(MERGE), "--root", str(root), "--stored", str(stored), "--measured", str(downloads)],
            capture_output=True, text=True, timeout=60)
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert stored.read_text() == '{"tests/unit/a/test_a.py": 5.0}'


def test_the_refresh_script_has_the_house_shape():
    text = REFRESH.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    first = next(l.strip() for l in text.splitlines()[1:] if l.strip() and not l.strip().startswith("#"))
    assert first == "set -euo pipefail"
    assert os.access(REFRESH, os.X_OK)
    assert re.search(r'^source .*/lib/common\.sh"$', text, re.M)
    assert "merge_unit_weights.py" in text


def _fake_repo(tmp_path: Path, runs: dict[str, dict[int, dict[str, float]]]) -> tuple[Path, dict[str, str]]:
    """A checkout with the two scripts, common.sh and three test files, and a
    fake `gh` that lists `runs` (newest first) and downloads each one's
    artifacts; a run with no artifact fails to download, as gh does."""
    root = tmp_path / "repo"
    (root / "scripts" / "lib").mkdir(parents=True)
    for rel in ("scripts/refresh-unit-weights.sh", "scripts/lib/merge_unit_weights.py", "scripts/lib/common.sh"):
        shutil.copy(REPO / rel, root / rel)
    _tree(root, ["tests/unit/a/test_a.py", "tests/unit/a/test_b.py", "tests/unit/a/test_c.py"])
    (root / "tests" / "unit" / "scripts").mkdir(parents=True)
    (root / "tests" / "unit" / "scripts" / "unit_test_durations.json").write_text(
        json.dumps({"tests/unit/a/test_a.py": 9.0, "tests/unit/a/test_gone.py": 1.0}, indent=1) + "\n")
    store = tmp_path / "runs"
    for run_id, shards in runs.items():
        _artifacts(store / run_id, shards) if shards else (store / run_id).mkdir(parents=True)
    (tmp_path / "run-ids.json").write_text(json.dumps([{"databaseId": int(r)} for r in runs]))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "{tmp_path}/gh-calls"
case "$1 $2" in
  "run list") cat "{tmp_path}/run-ids.json" ;;
  "run download")
    run="$3"; shift 3; dest=""
    while [[ $# -gt 0 ]]; do case "$1" in -D) dest="$2"; shift 2 ;; *) shift ;; esac; done
    if [[ -z "$(ls -A "{store}/$run")" ]]; then echo "no valid artifacts found to download" >&2; exit 1; fi
    mkdir -p "$dest"; cp -R "{store}/$run/." "$dest/" ;;
  *) echo "unexpected gh $*" >&2; exit 3 ;;
esac
""")
    (bin_dir / "gh").chmod(0o755)
    env = {k: v for k, v in os.environ.items()
           if k not in {"PROJECT_ID", "IMAGE_REPO", "ENVIRONMENT", "REPO_ROOT", "GH_REPO", "GITHUB_REPOSITORY"}}
    env.update(NO_COLOR="1", SWARM_ENV_FILE=str(tmp_path / "no.env"), PATH=f"{bin_dir}:{env.get('PATH', '')}")
    return root, env


def test_the_refresh_walks_back_to_the_newest_run_with_weights_and_merges_them(tmp_path):
    """A push run that skipped python-unit uploads no weights; the refresh
    walks past it. MUTATION: read only the newest run, or skip the merge."""
    root, env = _fake_repo(tmp_path, {
        "300": {},
        "200": {1: {"tests/unit/a/test_a.py": 2.0}, 2: {"tests/unit/a/test_b.py": 3.0}},
        "100": {1: {"tests/unit/a/test_c.py": 8.0}},
    })
    proc = subprocess.run(["bash", str(root / "scripts" / "refresh-unit-weights.sh")],
                          cwd=root, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    weights = json.loads((root / "tests" / "unit" / "scripts" / "unit_test_durations.json").read_text())
    assert weights == {"tests/unit/a/test_a.py": 2.0, "tests/unit/a/test_b.py": 3.0}, weights
    assert "200" in proc.stdout + proc.stderr
    calls = (tmp_path / "gh-calls").read_text()
    assert "--event push" in calls and "--branch main" in calls and "--status success" in calls, calls
    assert "run download 100" not in calls, calls


def test_the_refresh_fails_when_no_run_has_weights(tmp_path):
    root, env = _fake_repo(tmp_path, {"300": {}, "200": {}})
    before = (root / "tests" / "unit" / "scripts" / "unit_test_durations.json").read_text()
    proc = subprocess.run(["bash", str(root / "scripts" / "refresh-unit-weights.sh")],
                          cwd=root, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert (root / "tests" / "unit" / "scripts" / "unit_test_durations.json").read_text() == before
