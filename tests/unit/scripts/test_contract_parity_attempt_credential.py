"""Section 5 of `scripts/lib/check-contract-parity.sh` holds contract request 152.

`Attempt` gains `account_id`, `credential_source` and `credential_tier`
(docs/contract-change-requests.md request 152, accepted by the owner on
2026-11-09; docs/design/llm-providers.md §8.2). `apps/swarm-ui/src/types.ts`
restates them on `AttemptRow`, and the tier's two values as the
`CredentialTier` union, because the browser cannot import Python. Section 5 is
where the TypeScript restatements are held to the frozen module, so it holds
these too:

  * the real tree passes and says so;
  * `AttemptRow` without one of the three fails, naming the field;
  * a `CredentialTier` union with a value the frozen `CREDENTIAL_TIERS` does
    not have, or without one it has, fails, naming the union;
  * `AttemptRow.credential_tier` typed as anything but the union fails.

Each test runs section 5 alone (cut from the script between its header and
section 6's) against a fixture copy of exactly the files it reads. Offline: no
cloud credentials, no emulator.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "lib" / "check-contract-parity.sh"
HEADER = "# 5. The frozen catalogue and the task lifecycle, restated in TypeScript."
NEXT = "# 6. The tenant NAMESPACE"

TYPES = "apps/swarm-ui/src/types.ts"
FILES = (
    TYPES,
    "apps/swarm-ui/src/outcomes.ts",
    "terraform/modules/scheduler/variables.tf",
)


def _section() -> str:
    text = SCRIPT.read_text()
    start = text.find(HEADER)
    assert start != -1, "check-contract-parity.sh has no TypeScript section 5"
    end = text.find(NEXT, start)
    assert end != -1, "section 5 must sit before section 6"
    return text[start:end]


def _fixture(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    shutil.copytree(
        REPO / "apps" / "common" / "swarm_common",
        root / "apps" / "common" / "swarm_common",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    for rel in FILES:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / rel, root / rel)
    return root


def _edit(root: Path, old: str, new: str) -> None:
    path = root / TYPES
    text = path.read_text()
    assert text.count(old) == 1, f"the fixture edit no longer matches {TYPES} once: {old!r}"
    path.write_text(text.replace(old, new))


def _run(tmp_path: Path, root: Path) -> subprocess.CompletedProcess[str]:
    harness = tmp_path / "section.sh"
    harness.write_text(
        "set -euo pipefail\n"
        f'source "{REPO}/scripts/lib/common.sh"\n'
        f'REPO_ROOT="{root}"\n'
        "FAILED=0\n"
        f"{_section()}\n"
        'exit "${FAILED}"\n'
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("SWARM_")}
    # Except the placeholder deployment's never-touch list (conftest.py):
    # common.sh reads it for the project as it is sourced, and stops without it.
    if "SWARM_DEPLOY_CONFIG_DIR" in os.environ:
        env["SWARM_DEPLOY_CONFIG_DIR"] = os.environ["SWARM_DEPLOY_CONFIG_DIR"]
    env["NO_COLOR"] = "1"
    return subprocess.run(
        ["bash", str(harness)], capture_output=True, text=True, env=env, timeout=120, check=False
    )


def _failures(result: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in result.stderr.splitlines() if "fail" in line]


def test_the_real_tree_passes_and_names_both_checks(tmp_path):
    result = _run(tmp_path, REPO)
    assert result.returncode == 0, result.stderr
    for item in ("AttemptRow.credential", "CredentialTier"):
        assert re.search(r"ok.*" + re.escape(item), result.stderr), (item, result.stderr)


def test_an_untouched_fixture_copy_passes(tmp_path):
    result = _run(tmp_path, _fixture(tmp_path))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "field",
    ["account_id", "credential_source", "credential_tier"],
)
def test_an_attempt_row_without_a_field_fails_naming_it(tmp_path, field):
    root = _fixture(tmp_path)
    path = root / TYPES
    text = path.read_text()
    row = re.search(r"export interface AttemptRow \{(.*?)\n\}", text, re.S)
    assert row is not None
    body, count = re.subn(r"\n\s*%s\?:[^\n]*" % field, "", row.group(1))
    assert count == 1, (field, count)
    path.write_text(text[: row.start(1)] + body + text[row.end(1):])
    result = _run(tmp_path, root)
    assert result.returncode != 0, result.stderr
    assert any(field in line for line in _failures(result)), (field, result.stderr)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("export type CredentialTier = 'plan' | 'api_key'", "export type CredentialTier = 'plan' | 'api_key' | 'key'"),
        ("export type CredentialTier = 'plan' | 'api_key'", "export type CredentialTier = 'plan'"),
    ],
    ids=["extra-tier", "missing-tier"],
)
def test_a_tier_union_that_drifts_fails_naming_it(tmp_path, old, new):
    root = _fixture(tmp_path)
    _edit(root, old, new)
    result = _run(tmp_path, root)
    assert result.returncode != 0, result.stderr
    assert any("CredentialTier" in line for line in _failures(result)), result.stderr


def test_a_tier_field_typed_loosely_fails(tmp_path):
    root = _fixture(tmp_path)
    _edit(root, "credential_tier?: CredentialTier | null", "credential_tier?: string | null")
    result = _run(tmp_path, root)
    assert result.returncode != 0, result.stderr
    assert any("credential_tier" in line for line in _failures(result)), result.stderr
