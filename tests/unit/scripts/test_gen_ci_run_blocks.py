# source-text: fixture - the planted workflows the generator publishes are scratch files the test rewrites, not the repository's code
"""scripts/gen_ci_run_blocks.py publishes run text only, deterministically (issue 71).

Every workflow here is written under tmp_path: the test never reads the
repository's .github/ (that would be an out-of-tree read), and it never
regenerates the committed JSON -- `--check` runs as a plain command in CI
(application.yml, quick-checks) and in scripts/changed-guards.sh.
"""

from __future__ import annotations

import importlib.util
import json
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "gen_ci_run_blocks.py"

_spec = importlib.util.spec_from_file_location("gen_ci_run_blocks", SCRIPT)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

WORKFLOW = """\
name: gate
on:
  pull_request:
    types: [labeled]
permissions:
  contents: read
jobs:
  enable:
    if: github.event.label.name == 'ready'
    needs: [other]
    runs-on: ubuntu-24.04
    env:
      JOB_LEVEL: "1"
    steps:
      - uses: actions/checkout@0123456789abcdef0123456789abcdef01234567 # v5
      - name: the gate
        id: gate
        shell: bash
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          set -euo pipefail
          echo gate
      - name: say it
        run: echo said
      - name: say it
        run: echo said again
      - run: echo nameless
  no-shell:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@0123456789abcdef0123456789abcdef01234567 # v5
"""

ACTION = """\
name: release apply
runs:
  using: composite
  steps:
    - id: apply
      shell: bash
      run: echo apply
"""


def _tree(root: Path) -> Path:
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "gate.yml").write_text(WORKFLOW)
    (root / ".github" / "actions" / "apply").mkdir(parents=True)
    (root / ".github" / "actions" / "apply" / "action.yml").write_text(ACTION)
    return root


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return _tree(tmp_path / "repo")


def _written(root: Path) -> dict:
    assert gen.main([], root=root) == 0
    return json.loads((root / gen.OUTPUT_RELATIVE).read_text())


def test_each_run_step_is_published_by_job_and_step_key(root: Path):
    files = _written(root)["files"]
    steps = files[".github/workflows/gate.yml"]["jobs"]["enable"]["steps"]
    assert steps["gate"] == {"run": "set -euo pipefail\necho gate\n"}
    assert steps["say it"] == {"run": "echo said"}
    assert steps["say it (2)"] == {"run": "echo said again"}
    assert steps["step-5"] == {"run": "echo nameless"}
    assert set(steps) == {"gate", "say it", "say it (2)", "step-5"}


def test_a_composite_actions_steps_are_published_without_a_job(root: Path):
    files = _written(root)["files"]
    assert files[".github/actions/apply/action.yml"] == {"steps": {"apply": {"run": "echo apply"}}}


def test_a_job_with_no_run_step_is_left_out(root: Path):
    jobs = _written(root)["files"][".github/workflows/gate.yml"]["jobs"]
    assert set(jobs) == {"enable"}


@pytest.mark.parametrize("key", ["on", "permissions", "if", "needs", "uses", "env", "shell", "runs-on", "name"])
def test_nothing_but_run_text_is_published(root: Path, key: str):
    """MUTATION: publish a step's or job's `env`, `if`, `uses`, ... beside its run."""
    document = _written(root)
    keys: set[str] = set()

    def walk(node, under_steps=False):
        if isinstance(node, dict):
            for name, value in node.items():
                if not under_steps:
                    keys.add(name)
                walk(value, under_steps=(name in {"steps", "jobs", "files"}))
    walk(document)
    assert key not in keys, keys


def test_the_output_is_deterministic_sorted_and_ends_in_a_newline(root: Path):
    first = (gen.render(root))
    assert first == gen.render(root)
    assert first.endswith("}\n") and not first.endswith("\n\n")
    canonical = json.dumps(json.loads(first), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    assert first == canonical.replace('=\\"', "=\\u0022")


def test_a_quote_after_an_equals_sign_is_written_as_its_unicode_escape(root: Path):
    """The publish scan reads `GH_TOKEN=\\"${APP_TOKEN}\\"` as a literal
    credential; `=\\u0022` is the same JSON string and passes it.
    MUTATION: drop the replacement (the raw escape is back in the file)."""
    workflow = root / ".github" / "workflows" / "gate.yml"
    workflow.write_text(workflow.read_text().replace(
        "echo nameless", 'GH_TOKEN="${APP_TOKEN}" gh pr view "a=\\\\"'))
    text = gen.render(root)
    assert '=\\"' not in text, text
    assert 'GH_TOKEN=\\u0022${APP_TOKEN}\\" gh pr view' in text, text
    steps = json.loads(text)["files"][".github/workflows/gate.yml"]["jobs"]["enable"]["steps"]
    assert steps["step-5"]["run"] == 'GH_TOKEN="${APP_TOKEN}" gh pr view "a=\\\\"'


def test_check_passes_on_a_current_file_and_fails_after_a_run_block_changes(root: Path, capsys):
    assert gen.main([], root=root) == 0
    assert gen.main(["--check"], root=root) == 0
    workflow = root / ".github" / "workflows" / "gate.yml"
    workflow.write_text(workflow.read_text().replace("echo gate", "echo gate changed"))
    capsys.readouterr()
    assert gen.main(["--check"], root=root) == 1
    assert gen.REGENERATE in capsys.readouterr().err
    assert gen.main([], root=root) == 0
    assert gen.main(["--check"], root=root) == 0


def test_check_fails_when_the_file_is_missing_and_writes_nothing(root: Path):
    assert gen.main(["--check"], root=root) == 1
    assert not (root / gen.OUTPUT_RELATIVE).exists()


def test_a_change_outside_run_text_leaves_the_file_current(root: Path):
    """Only run text is a function of the output: a trigger edit is not drift."""
    assert gen.main([], root=root) == 0
    workflow = root / ".github" / "workflows" / "gate.yml"
    workflow.write_text(workflow.read_text().replace("types: [labeled]", "types: [labeled, unlabeled]"))
    assert gen.main(["--check"], root=root) == 0


def test_inputs_prints_every_source_then_the_generator_and_the_output(root: Path, capsys):
    assert gen.main(["--inputs"], root=root) == 0
    assert capsys.readouterr().out.splitlines() == [
        ".github/actions/apply/action.yml",
        ".github/workflows/gate.yml",
        "scripts/gen_ci_run_blocks.py",
        gen.OUTPUT_RELATIVE,
    ]
    assert not (root / gen.OUTPUT_RELATIVE).exists()


def test_the_loader_reads_a_published_block(tmp_path: Path, monkeypatch):
    from . import ci_run_blocks

    root = _tree(tmp_path / "repo")
    assert gen.main([], root=root) == 0
    monkeypatch.setattr(ci_run_blocks, "GENERATED", root / gen.OUTPUT_RELATIVE)
    ci_run_blocks._files.cache_clear()
    try:
        assert ci_run_blocks.run_block("gate.yml", "enable", "gate") == "set -euo pipefail\necho gate\n"
        assert ci_run_blocks.run_block(".github/actions/apply/action.yml", None, "apply") == "echo apply"
        with pytest.raises(KeyError):
            ci_run_blocks.run_block("gate.yml", "enable", "absent")
    finally:
        ci_run_blocks._files.cache_clear()


def test_a_workflow_that_is_not_a_mapping_publishes_nothing(tmp_path: Path):
    path = tmp_path / "empty.yml"
    path.write_text(textwrap.dedent("""\
        # only a comment
        """))
    assert gen.blocks(path) is None
