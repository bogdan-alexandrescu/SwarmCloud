"""The pre-publish gate runs a changed package's `npm run test:node` (#2455).

CI's ui-build runs `npm run test:node` in apps/swarm-ui (`node tests/run.mjs`
and the node:test files under `test/*.test.mjs`, no-company-name.test.mjs
among them). The gate ran graph-pytest, vitest, tsc and terraform-fmt only,
and its vitest selection drops `.mjs`/`.cjs` tests, so none of those files
ran before publish: four lanes (#2429, #2430, #2433, #2437) published code
naming the company and went red in ui-build. So a change to a package whose
package.json declares a `test:node` script now gets a `node-tests` check that
runs it from the package directory; skipped, with the reason, where npm or
the package's node_modules is missing, because the gate never runs npm
install.

No git and no npm: the checkout is a plain directory and the runner a fake.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, Sequence

from agent_worker import prepublish

UI = "apps/swarm-ui"
TEST_NODE = "node tests/run.mjs && node --import ./test/register-ts.mjs --test test/*.test.mjs"


def _repo(tmp_path: Path, *, test_node: bool = True, node_modules: bool = True) -> Path:
    repo = tmp_path / "repo"
    scripts = {"test": "vitest run"}
    if test_node:
        scripts["test:node"] = TEST_NODE
    files = {
        f"{UI}/package.json": json.dumps({"name": "swarm-ui", "scripts": scripts}),
        f"{UI}/src/Foo.tsx": "export const Foo = 1\n",
        "apps/agent-worker/pyproject.toml": '[project]\nname = "w"\nversion = "0"\n',
        "apps/agent-worker/agent_worker/mod.py": "X = 1\n",
        "terraform/infra/main.tf": 'resource "x" "y" {}\n',
    }
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    if node_modules:
        (repo / UI / "node_modules" / ".bin").mkdir(parents=True)
    return repo


def _env(tmp_path: Path, *, npm: bool = True) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    if npm:
        tool = bin_dir / "npm"
        tool.write_text("#!/bin/sh\nexit 0\n")
        tool.chmod(0o755)
    return {"PATH": str(bin_dir)}


def _checks(repo: Path, files: Sequence[str], env: Mapping[str, str]) -> dict[str, prepublish.Check]:
    return {c.name: c for c in prepublish.graph_checks(repo, files, base="HEAD", env=env,
                                                       snapshot=None)}


class _Runner:
    def __init__(self, code: int, output: str) -> None:
        self.code, self.output = code, output
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(self, argv: Sequence[str], cwd: Path, env: Mapping[str, str],
                 timeout: float) -> prepublish.Ran:
        self.calls.append((list(argv), Path(cwd)))
        return prepublish.Ran(self.code, self.output)


def test_a_changed_tsx_under_swarm_ui_adds_a_node_tests_check_running_test_node(tmp_path):
    repo = _repo(tmp_path)
    checks = _checks(repo, [f"{UI}/src/Foo.tsx"], _env(tmp_path))
    check = checks["node-tests"]
    assert "test:node" in check.command and UI in check.command
    assert check.files == (f"{UI}/src/Foo.tsx",)
    assert check.run is not None and check.skipped is None


def test_node_tests_runs_npm_in_the_package_and_a_red_exit_fails(tmp_path):
    repo = _repo(tmp_path)
    env = _env(tmp_path)
    check = _checks(repo, [f"{UI}/src/Foo.tsx"], env)["node-tests"]

    red = _Runner(1, "not ok 1 - no source file names the company\n")
    (outcome,) = prepublish.run_checks(repo, [check], env=env, runner=red)
    assert outcome.status == prepublish.FAILED
    assert "names the company" in outcome.output
    ((argv, cwd),) = red.calls
    assert Path(argv[0]).name == "npm" and argv[1:] == ["run", "test:node"]
    assert cwd == repo / UI

    green = _Runner(0, "ok\n")
    (outcome,) = prepublish.run_checks(repo, [check], env=env, runner=green)
    assert outcome.status == prepublish.PASSED


def test_no_node_tests_check_without_a_test_node_script_or_a_ui_change(tmp_path):
    repo = _repo(tmp_path)
    env = _env(tmp_path)
    for files in (["terraform/infra/main.tf"], ["apps/agent-worker/agent_worker/mod.py"]):
        assert "node-tests" not in _checks(repo, files, env), files

    bare = _repo(tmp_path / "bare", test_node=False)
    assert "node-tests" not in _checks(bare, [f"{UI}/src/Foo.tsx"], env)


def test_node_tests_is_skipped_with_a_reason_without_npm_or_node_modules(tmp_path):
    repo = _repo(tmp_path)
    check = _checks(repo, [f"{UI}/src/Foo.tsx"], _env(tmp_path / "no-npm", npm=False))["node-tests"]
    assert check.run is None and check.skipped
    assert "npm" in check.skipped and "CI runs it" in check.skipped

    bare = _repo(tmp_path / "bare", node_modules=False)
    check = _checks(bare, [f"{UI}/src/Foo.tsx"], _env(tmp_path))["node-tests"]
    assert check.run is None and check.skipped
    assert "node_modules" in check.skipped and "CI runs it" in check.skipped
    (outcome,) = prepublish.run_checks(bare, [check], env=_env(tmp_path), runner=_Runner(1, ""))
    assert outcome.status == prepublish.SKIPPED and outcome.reason == check.skipped
