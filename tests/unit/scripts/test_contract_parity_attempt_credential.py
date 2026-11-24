"""Contract request 152 and section 5 of `scripts/lib/check-contract-parity.sh`.

`Attempt` gained `account_id`, `credential_source` and `credential_tier`
(docs/contract-change-requests.md request 152, accepted by the owner on
2026-11-09; docs/design/llm-providers.md §8.2). The console restates them on
`AttemptRow`, and the tier's values as the `CredentialTier` union.

Section 5 of the parity script used to hold those restatements by reading
`apps/swarm-ui/src/types.ts` as text. Since part of #72 the console half is a
console test on the generated constants
(apps/swarm-ui/src/__tests__/contract.parity.test.ts), and what is held here
is the Python half, opening no console file:

  * the frozen `Attempt` carries the three fields and `attempt_to_api` serves
    them, a tier the frozen `CREDENTIAL_TIERS` refuses being served as null;
  * the frozen `CREDENTIAL_TIERS`, `ParkReason` and `EndCause` members reach
    the `states` constants group, which `scripts/gen_api_types.py` writes to
    `states.json` (its `--check` fails CI while the file differs);
  * the parity script reads no console TypeScript any more;
  * section 5 now holds the quota broker sweep interval, stated in
    scripts/api_constants/states.py, to the Terraform quota-refresh cron: the
    real tree passes and says so, and a drift on either side fails, naming it.

Section 5 runs alone (cut from the script between its header and section 6's)
against a fixture copy of exactly the files it reads. Offline: no cloud
credentials, no emulator.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from swarm_api.codec import attempt_to_api
from swarm_common.models import CREDENTIAL_TIERS, Attempt, EndCause
from swarm_common.states import ParkReason

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "lib" / "check-contract-parity.sh"
STATES = "scripts/api_constants/states.py"
SCHEDULER_VARS = "terraform/modules/scheduler/variables.tf"
HEADER = "# 5. The quota broker sweep interval, stated in Python, against Terraform."
NEXT = "# 6. The tenant NAMESPACE"
RUN_ON = ("account_id", "credential_source", "credential_tier")


def _states_group():
    spec = importlib.util.spec_from_file_location("parity_test_states_group", REPO / STATES)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# The Python half of what the console restates
# --------------------------------------------------------------------------


def _attempt(**fields) -> Attempt:
    return Attempt(
        attempt_id="a-1", task_id="t-1", tenant_id="parity", generation=1,
        lease_id="l-1", backend="CLOUD_RUN_JOB",
        created_at=datetime(2026, 11, 24, tzinfo=UTC), **fields,
    )


def test_attempt_to_api_serves_the_credential_an_attempt_ran_on():
    served = attempt_to_api(_attempt(account_id="acct-1", credential_source="account_pool",
                                     credential_tier=CREDENTIAL_TIERS[0]))
    assert {name: served[name] for name in RUN_ON} == {
        "account_id": "acct-1", "credential_source": "account_pool",
        "credential_tier": CREDENTIAL_TIERS[0],
    }
    assert {name: attempt_to_api(_attempt())[name] for name in RUN_ON} == dict.fromkeys(RUN_ON)


def test_the_frozen_tiers_and_reasons_reach_the_generated_constants():
    built = _states_group().build()
    assert built["credential_tiers"] == list(CREDENTIAL_TIERS)
    assert set(CREDENTIAL_TIERS) == {"plan", "api_key"}
    assert built["park_reason"] == [m.value for m in ParkReason]
    assert built["end_cause"] == [m.value for m in EndCause]
    assert EndCause.CHILD_CASCADE.value in built["end_cause"]


def test_the_parity_script_reads_no_console_typescript():
    text = SCRIPT.read_text()
    assert "types.ts" not in text
    assert "TS_TYPES" not in text and "GENERATED_TS" not in text
    # A comment may point a reader at the console tests; no code line names a
    # console file (section 6 scans the whole tree, apps/swarm-ui among it,
    # without naming it).
    code = [line for line in text.splitlines()
            if re.search(r"swarm-ui|\.tsx?\b", line) and not line.lstrip().startswith("#")]
    assert code == []


# --------------------------------------------------------------------------
# Section 5: the quota sweep interval against the Terraform cron
# --------------------------------------------------------------------------


def _section() -> str:
    text = SCRIPT.read_text()
    start = text.find(HEADER)
    assert start != -1, "check-contract-parity.sh has no quota sweep section 5"
    end = text.find(NEXT, start)
    assert end != -1, "section 5 must sit before section 6"
    return text[start:end]


def _fixture(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    for rel in (STATES, SCHEDULER_VARS):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / rel, root / rel)
    return root


def _edit(root: Path, rel: str, old: str, new: str) -> None:
    path = root / rel
    text = path.read_text()
    assert text.count(old) == 1, f"the fixture edit no longer matches {rel} once: {old!r}"
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


def test_the_real_tree_passes_and_names_the_interval(tmp_path):
    result = _run(tmp_path, REPO)
    assert result.returncode == 0, result.stderr
    assert re.search(r"ok QUOTA_SWEEP_INTERVAL_SECONDS \(states\.py\): 300s matches", result.stderr), result.stderr


def test_an_untouched_fixture_copy_passes(tmp_path):
    result = _run(tmp_path, _fixture(tmp_path))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("rel", "old", "new"),
    [
        (STATES, "QUOTA_SWEEP_INTERVAL_SECONDS = 300", "QUOTA_SWEEP_INTERVAL_SECONDS = 600"),
        (SCHEDULER_VARS, 'default = "*/5 * * * *"', 'default = "*/10 * * * *"'),
    ],
    ids=["python-moved", "cron-moved"],
)
def test_an_interval_that_drifts_fails_naming_it(tmp_path, rel, old, new):
    root = _fixture(tmp_path)
    _edit(root, rel, old, new)
    result = _run(tmp_path, root)
    assert result.returncode != 0, result.stderr
    assert any("QUOTA_SWEEP_INTERVAL_SECONDS" in line for line in _failures(result)), result.stderr
    assert "DRIFTED" in result.stderr, result.stderr


def test_a_cron_set_outside_the_module_fails(tmp_path):
    root = _fixture(tmp_path)
    override = root / "terraform" / "infra" / "main.tf"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text('quota_refresh_schedule = "*/5 * * * *"\n')
    result = _run(tmp_path, root)
    assert result.returncode != 0, result.stderr
    assert "set outside the module" in result.stderr, result.stderr
