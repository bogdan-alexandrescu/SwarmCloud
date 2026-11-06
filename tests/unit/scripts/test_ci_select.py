"""scripts/ci-select.sh and the application.yml steps that run it.

BGD's pick, 2026-11-06 (part of #2688; docs/ci.md "The full suite runs once
per change, in the merge train"): a lane's pull request runs only the tests
its change reaches, the merge train runs the full suite, and main's push skips
its test jobs when a green, landed train tested its exact tree.

Every case runs offline: the script against a small repository built in
tmp_path with copies of the selectors it reads, and `gh` replaced by a
stand-in that serves fixture JSON through the real jq.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
WORKFLOW = REPO / ".github/workflows/application.yml"
SCRIPT = "scripts/ci-select.sh"
LIBS = ("common.sh", "related_tests.py", "select_unit_tests.py", "ui_shards.py", "ci_reuse.py")
GH_REPO = "acme/widgets"

sys.path.insert(0, str(REPO / "scripts/lib"))
import ci_reuse  # noqa: E402


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def _step(job: str, step_id: str) -> dict:
    (step,) = [s for s in _jobs()[job]["steps"] if s.get("id") == step_id]
    return step


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """A repository with the script, its selectors, one app of two modules,
    their unit and integration tests and a swarm-ui module with its test."""
    root = tmp_path / "repo"
    (root / "scripts/lib").mkdir(parents=True)
    shutil.copy2(REPO / SCRIPT, root / SCRIPT)
    for lib in LIBS:
        shutil.copy2(REPO / "scripts/lib" / lib, root / "scripts/lib" / lib)
    _write(root, "apps/foo/foo/__init__.py", "")
    _write(root, "apps/foo/foo/alpha.py", "def run():\n    return 1\n")
    _write(root, "apps/foo/foo/beta.py", "def other():\n    return 2\n")
    _write(root, "apps/foo/pyproject.toml", "[project]\nname = 'foo'\n")
    _write(root, "tests/unit/conftest.py", "")
    _write(root, "tests/unit/test_alpha.py", "from foo import alpha\n\ndef test_a():\n    assert alpha.run() == 1\n")
    _write(root, "tests/unit/test_beta.py", "from foo import beta\n\ndef test_b():\n    assert beta.other() == 2\n")
    _write(root, "tests/integration/test_alpha_live.py", "from foo.alpha import run\n\ndef test_live():\n    assert run()\n")
    _write(root, "tests/integration/test_unrelated.py", "def test_nothing():\n    assert True\n")
    _write(root, "tests/integration/fake_platform.py", "PLATFORM = 1\n")
    _write(root, "tests/unit/scripts/ui_test_durations.json", "{}\n")
    _write(root, "apps/swarm-ui/vitest.config.ts", "export default { test: { include: ['src/**/*.test.ts'] } }\n")
    _write(root, "apps/swarm-ui/package.json", "{}\n")
    _write(root, "apps/swarm-ui/src/widget.ts", "export const widget = 1\n")
    _write(root, "apps/swarm-ui/src/other.ts", "export const other = 2\n")
    _write(root, "apps/swarm-ui/src/__tests__/widget.test.ts", "import { widget } from '../widget'\n")
    _write(root, "apps/swarm-ui/src/__tests__/other.test.ts", "import { other } from '../other'\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "fixture")
    return root


def _lane(root: Path, *changed: str) -> dict[str, str]:
    listing = root.parent / "changed"
    listing.write_text("".join(f"{c}\n" for c in changed))
    out = root.parent / "lane-output"
    out.write_text("")
    proc = subprocess.run([str(root / SCRIPT), "lane", "--changed", str(listing), "--github-output", str(out)],
                          capture_output=True, text=True, cwd=root)
    assert proc.returncode == 0, proc.stderr
    return dict(line.split("=", 1) for line in out.read_text().splitlines())


# --------------------------------------------------------------------------
# lane
# --------------------------------------------------------------------------
def test_a_one_module_change_selects_only_what_reaches_it(tree: Path):
    """MUTATION: run every integration test, or none, for a module one of them imports."""
    got = _lane(tree, "apps/foo/foo/alpha.py")
    assert got["lane"] == "true", got
    assert got["integration_tests"] == "tests/integration/test_alpha_live.py", got
    assert got["ui_tests"] == "" and got["ui_shards"] == "[1]", got


def test_a_ui_module_change_selects_its_component_tests(tree: Path):
    got = _lane(tree, "apps/swarm-ui/src/widget.ts")
    assert got["lane"] == "true", got
    assert got["ui_tests"] == "src/__tests__/widget.test.ts", got
    assert json.loads(got["ui_shards"]) == [1], got
    assert got["integration_tests"] == "", got


@pytest.mark.parametrize("path", [
    "tests/unit/conftest.py",
    "uv.lock",
    "pyproject.toml",
    "apps/swarm-ui/package-lock.json",
    "apps/swarm-ui/package.json",
    "apps/swarm-ui/vitest.config.ts",
    "apps/common/swarm_common/types.py",
    ".github/workflows/application.yml",
    "tests/integration/fake_platform.py",
])
def test_shared_ground_runs_the_full_suite(tree: Path, path: str):
    """MUTATION: narrow a change to a conftest, a manifest or a lockfile."""
    got = _lane(tree, "apps/foo/foo/alpha.py", path)
    assert got["lane"] == "false", got
    assert path in got["why"], got
    assert got["ui_tests"] == "" and got["integration_tests"] == "", got


def test_a_selection_that_cannot_be_computed_runs_the_full_suite(tree: Path):
    """MUTATION: read a selector that crashed as an empty selection."""
    (tree / "scripts/lib/related_tests.py").write_text("raise SystemExit('broken')\n")
    got = _lane(tree, "apps/foo/foo/alpha.py")
    assert got["lane"] == "false", got
    assert "could not be computed" in got["why"], got


def _ui_pick(root: Path, tests: str, shard: int = 1, shards: int = 1) -> tuple[dict[str, str], list[str]]:
    out = root.parent / "pick-output"
    out.write_text("")
    files = root.parent / "shard-files"
    env = {**os.environ, "UI_TESTS": tests, "SHARD": str(shard), "SHARDS": str(shards),
           "DURATIONS": str(root / "tests/unit/scripts/ui_test_durations.json"),
           "SHARD_FILES": str(files), "GITHUB_OUTPUT": str(out)}
    proc = subprocess.run([str(root / SCRIPT), "ui-pick"], capture_output=True, text=True, cwd=root, env=env)
    assert proc.returncode == 0, proc.stderr
    return dict(line.split("=", 1) for line in out.read_text().splitlines()), files.read_text().split()


def test_ui_pick_splits_the_selection_and_never_writes_an_empty_run(tree: Path):
    """MUTATION: write no count for an empty selection (vitest would run everything)."""
    out, files = _ui_pick(tree, "")
    assert out == {"count": "0"} and files == []
    both = "src/__tests__/widget.test.ts src/__tests__/other.test.ts"
    out, files = _ui_pick(tree, both)
    assert out == {"count": "2"} and sorted(files) == sorted(both.split())
    first, one = _ui_pick(tree, both, 1, 2)
    second, two = _ui_pick(tree, both, 2, 2)
    assert first["count"] == second["count"] == "1" and sorted(one + two) == sorted(both.split())


# --------------------------------------------------------------------------
# train-match
# --------------------------------------------------------------------------
FAKE_GH = r'''#!/usr/bin/env python3
"""Stand-in gh: `gh api <path> [--paginate] [--jq <expr>]` from FAKE_GH_ROUTES."""
import json, os, subprocess, sys
args = sys.argv[1:]
assert args[0] == "api", args
path = args[1]
routes = json.load(open(os.environ["FAKE_GH_ROUTES"]))
if path not in routes:
    sys.stderr.write(f"no route {path}\n")
    sys.exit(1)
body = json.dumps(routes[path])
if "--jq" in args:
    expr = args[args.index("--jq") + 1]
    sys.exit(subprocess.run(["jq", "-r", expr], input=body, text=True).returncode)
print(body)
'''


def _train_routes(head_sha: str, head_tree: str, *, gate: str = "success", state: str = "closed",
                  comment: str = "Green, and landed: #11, #12, one squash each.", author: str = "Bot") -> dict:
    url = f"https://github.com/{GH_REPO}/pull/900"
    return {
        f"repos/{GH_REPO}/pulls?state=all&sort=updated&direction=desc&per_page=50": [
            {"number": 901, "title": "A lane's change (#901)", "html_url": "x",
             "head": {"ref": "swarm/lane", "sha": head_sha, "repo": {"full_name": GH_REPO}}},
            {"number": 900, "title": "Merge train 123-1: #11, #12", "html_url": url,
             "head": {"ref": "train/123-1", "sha": head_sha, "repo": {"full_name": GH_REPO}}},
        ],
        f"repos/{GH_REPO}/git/commits/{head_sha}": {"sha": head_sha, "tree": {"sha": head_tree}},
        f"repos/{GH_REPO}/commits/{head_sha}/check-runs?check_name=ci-gate&filter=latest": {
            "check_runs": [{"name": "ci-gate", "conclusion": gate, "completed_at": "2026-11-06T07:00:00Z"}]},
        f"repos/{GH_REPO}/pulls/900": {"number": 900, "state": state},
        f"repos/{GH_REPO}/issues/900/comments?per_page=100": [
            {"user": {"type": "User"}, "body": "looks good"},
            {"user": {"type": author}, "body": comment + "\n\n(scripts/merge-train.sh, run x)"},
        ],
    }


def _train_match(root: Path, routes: dict, sha: str = "HEAD") -> dict[str, str]:
    bin_dir = root.parent / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    routes_file = root.parent / "routes.json"
    routes_file.write_text(json.dumps(routes))
    out = root.parent / "train-output"
    out.write_text("")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_GH_ROUTES": str(routes_file),
           "TRAIN_WAIT_SECONDS": "0", "TRAIN_POLL_SECONDS": "0"}
    proc = subprocess.run([str(root / SCRIPT), "train-match", "--repo", GH_REPO, "--sha", _git(root, "rev-parse", sha),
                           "--github-output", str(out)], capture_output=True, text=True, cwd=root, env=env)
    assert proc.returncode == 0, proc.stderr
    return dict(line.split("=", 1) for line in out.read_text().splitlines())


def test_a_main_push_whose_tree_a_green_landed_train_tested_skips_the_suite(tree: Path):
    """MUTATION: list fewer jobs than ci_reuse.py may skip, or name no train."""
    head_tree = _git(tree, "rev-parse", "HEAD^{tree}")
    got = _train_match(tree, _train_routes("a" * 40, head_tree))
    assert json.loads(got["reused"]) == list(ci_reuse.REUSABLE), got
    assert got["reused_from"] == f"https://github.com/{GH_REPO}/pull/900", got
    assert got["train"] == "900", got


def test_a_main_push_no_train_tested_runs_the_suite(tree: Path):
    """MUTATION: match a train by number or sha instead of by tree."""
    got = _train_match(tree, _train_routes("a" * 40, "b" * 40))
    assert got == {"reused": "[]", "reused_from": "", "train": ""}, got


@pytest.mark.parametrize("change", [
    {"gate": "failure"},
    {"comment": "Green. **Dry run:** nothing landed."},
    {"comment": "Green, but nothing landed: main moved."},
    {"author": "User"},
    {"state": "open"},
])
def test_a_train_that_did_not_pass_and_land_is_never_trusted(tree: Path, change: dict):
    """MUTATION: drop the ci-gate check, the landed comment, its author or the closed state."""
    head_tree = _git(tree, "rev-parse", "HEAD^{tree}")
    got = _train_match(tree, _train_routes("a" * 40, head_tree, **change))
    assert got["reused"] == "[]" and got["train"] == "", (change, got)


def test_an_unreadable_api_runs_the_suite(tree: Path):
    got = _train_match(tree, {})
    assert got == {"reused": "[]", "reused_from": "", "train": ""}, got


# --------------------------------------------------------------------------
# application.yml
# --------------------------------------------------------------------------
def test_main_s_push_looks_for_the_train_before_the_tree_record():
    """MUTATION: run the train step on a pull request, after `reuse`, or with no read of checks."""
    changes = _jobs()["changes"]
    ids = [s.get("id") for s in changes["steps"]]
    assert ids.index("train") < ids.index("reuse"), ids
    step = _step("changes", "train")
    assert step["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'", step["if"]
    assert "./scripts/ci-select.sh train-match" in step["run"], step["run"]
    assert changes["permissions"].get("checks") == "read" and changes["permissions"].get("issues") == "read"
    assert "id-token" not in changes["permissions"]


def test_a_lane_pull_request_and_only_one_selects():
    step = _step("changes", "lane")
    assert step["if"] == ("github.event_name == 'pull_request' && startsWith(github.head_ref, 'swarm/') && "
                          "github.event.pull_request.head.repo.full_name == github.repository"), step["if"]
    assert "./scripts/ci-select.sh lane" in step["run"], step["run"]
    outputs = _jobs()["changes"]["outputs"]
    for key in ("lane", "ui_tests", "integration_tests"):
        assert outputs[key].replace(" ", "") == f"${{{{steps.lane.outputs.{key}}}}}", outputs[key]


def _run_step(step: dict, tmp_path: Path, **env: str) -> dict[str, str]:
    out = tmp_path / "step-output"
    out.write_text("")
    full = {**os.environ, "GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
            "RUNNER_TEMP": str(tmp_path), **env}
    proc = subprocess.run(["bash", "-c", step["run"]], capture_output=True, text=True, cwd=tmp_path, env=full)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    return dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)


def test_a_merge_train_pull_request_runs_every_job(tmp_path: Path):
    """MUTATION: let the diff narrow a train, or let the select step narrow its unit shards."""
    diff = _step("changes", "diff")
    got = _run_step(diff, tmp_path, EVENT="pull_request", HEAD_REF="train/123-1", HEAD_REPO=GH_REPO,
                    REPO=GH_REPO, APP_PATHS=diff["env"]["APP_PATHS"], BASE_SHA="x", HEAD_SHA="y")
    assert got == {"app": "true", "python": "all", "areas": "", "ui": "true"}, got
    select = _step("changes", "select")
    got = _run_step(select, tmp_path, EVENT="pull_request", PYTHON="all", AREAS="", HEAD_REF="train/123-1",
                    LANE="", LANE_WHY="", BASE_SHA="x", HEAD_SHA="y")
    assert got == {"python": "all", "areas": "", "tests": ""}, got


def test_a_lane_on_shared_ground_runs_the_whole_unit_suite(tmp_path: Path):
    select = _step("changes", "select")
    got = _run_step(select, tmp_path, EVENT="pull_request", PYTHON="all", AREAS="", HEAD_REF="swarm/x",
                    LANE="false", LANE_WHY="uv.lock is a lockfile", BASE_SHA="x", HEAD_SHA="y")
    assert got == {"python": "all", "areas": "", "tests": ""}, got


def test_a_lane_takes_its_own_component_shard_count(tmp_path: Path):
    step = _step("changes", "ui_shards")
    got = _run_step(step, tmp_path, LANE="true", LANE_UI_SHARDS="[1, 2]")
    assert got == {"shards": "[1, 2]"}, got


def test_integration_runs_whole_except_on_a_lane_that_reaches_none(tmp_path: Path):
    """MUTATION: skip integration on main or a train, or run all of it on a lane that reaches none."""
    pick = _step("integration", "pick")
    assert _run_step(pick, tmp_path, LANE="", INTEGRATION_TESTS="") == {"none": "false"}
    assert _run_step(pick, tmp_path, LANE="true", INTEGRATION_TESTS="") == {"none": "true"}
    assert _run_step(pick, tmp_path, LANE="true", INTEGRATION_TESTS="tests/integration/test_a.py") == {"none": "false"}
    steps = _jobs()["integration"]["steps"]
    after = steps[steps.index(pick) + 1:]
    assert after and all(s.get("if") == "steps.pick.outputs.none != 'true'" for s in after), after
    (run,) = [s for s in after if "uv run pytest" in str(s.get("run", ""))]
    assert run["env"]["INTEGRATION_TESTS"] == "${{ needs.changes.outputs.integration_tests }}"
    assert "uv run pytest tests/integration -q -n auto -m \"not serial\"" in run["run"]


def test_the_required_check_names_are_unchanged():
    """The ruleset and the merge step read these names; a rename un-requires them."""
    jobs = _jobs()
    assert jobs["ci-gate"]["name"] == "ci-gate"
    assert jobs["python"]["name"] == "format / unit tests"
    assert jobs["ui"]["name"] == "swarm-ui typecheck / component tests"
    assert jobs["integration"]["name"] == "integration tests (emulator)"
    assert jobs["manifests"]["name"] == "kubernetes manifests"
    assert jobs["shell"]["name"] == "shellcheck"
