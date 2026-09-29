"""`scripts/register-tenant.sh` binds the custom role ids the module defines.

terraform/modules/custom_role_ids/main.tf spells every custom role id once, for
both terraform roots, and fixes the suffix as the CONSTANT
`local.custom_role_suffix` (#79): a suffix set in only one place makes the
binder bind roles the definer never made. The script used to build its two ids
from a `CUSTOM_ROLE_SUFFIX` environment variable instead, so an exported suffix
-- left over from when it was a terraform/infra variable -- made it bind
`swarmTenantWorkerFirestore_<x>`, a role that does not exist (#227).

These tests read both files. The script's id assignments are EXECUTED, in bash
with `CUSTOM_ROLE_SUFFIX` set in the environment, and compared with the ids the
module's locals produce. The comparison is then run again against copies with
one side's suffix changed, and must fail: a parity test that also passes over
two files that disagree proves nothing.

WHAT THIS CANNOT PROVE: that terraform evaluates the module the way the few
lines modelled here do. The model is pinned to the module's own `role_suffix`
expression, so a change to that derivation fails here rather than drifting.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "register-tenant.sh"
MODULE = REPO / "terraform" / "modules" / "custom_role_ids" / "main.tf"

PROJECT = "swarm-test-project"

# The module key of each role the script grants, and the variable it lands in.
SCRIPT_ROLES = {
    "worker_firestore": "FIRESTORE_ROLE_ID",
    "bucket_metadata_reader": "BUCKET_METADATA_ROLE_ID",
}

# The assignments that produce the script's ids, in the order they run.
ID_VARS = (
    "CUSTOM_ROLE_SUFFIX_MODULE",
    "ROLE_ID_SUFFIX",
    "FIRESTORE_ROLE_ID",
    "FIRESTORE_ROLE",
    "BUCKET_METADATA_ROLE_ID",
)

SUFFIX_RE = re.compile(r'^\s*custom_role_suffix\s*=\s*"([^"]*)"\s*$', re.M)
# The derivation modelled by `_module_ids`. If the module ever spells it
# differently, fail here instead of comparing against a guess.
ROLE_SUFFIX_EXPR = (
    'role_suffix = local.custom_role_suffix == "" ? "" : '
    '"_${local.custom_role_suffix}"'
)
ID_RE = re.compile(r'^\s*(\w+)\s*=\s*"(\w+)\$\{local\.role_suffix\}"\s*$', re.M)


def _module_ids(text: str) -> dict[str, str]:
    assert ROLE_SUFFIX_EXPR in " ".join(text.split()), (
        "custom_role_ids no longer derives role_suffix the way this test models it"
    )
    found = SUFFIX_RE.findall(text)
    assert len(found) == 1, f"expected one custom_role_suffix constant, found {found}"
    suffix = found[0]
    role_suffix = "" if suffix == "" else f"_{suffix}"
    ids = {key: base + role_suffix for key, base in ID_RE.findall(text)}
    assert set(SCRIPT_ROLES) <= set(ids), f"module ids missing a role: {sorted(ids)}"
    return ids


def _script_ids(text: str) -> dict[str, str]:
    """Run the script's own id assignments and read back what they produce."""
    lines = []
    for var in ID_VARS:
        hits = [
            line
            for line in text.splitlines()
            if re.match(rf"^{var}=", line)
        ]
        assert len(hits) == 1, f"expected one top-level {var}= in the script, found {hits}"
        lines.append(hits[0])
    probe = "\n".join(
        [
            "set -euo pipefail",
            f"PROJECT_ID={PROJECT}",
            *lines,
            *(f'printf "%s=%s\\n" {v} "${{{v}}}"' for v in ID_VARS),
        ]
    )
    out = subprocess.run(
        ["bash", "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        # A suffix left in the environment must not reach the ids.
        env={"PATH": "/usr/bin:/bin", "CUSTOM_ROLE_SUFFIX": "stale"},
    ).stdout
    values = dict(line.split("=", 1) for line in out.splitlines())
    assert values["FIRESTORE_ROLE"] == (
        f"projects/{PROJECT}/roles/{values['FIRESTORE_ROLE_ID']}"
    )
    return {key: values[var] for key, var in SCRIPT_ROLES.items()}


def _diverged(script_text: str, module_text: str) -> dict[str, tuple[str, str]]:
    module = _module_ids(module_text)
    script = _script_ids(script_text)
    return {
        key: (script[key], module[key])
        for key in SCRIPT_ROLES
        if script[key] != module[key]
    }


def test_script_role_ids_match_the_module() -> None:
    diverged = _diverged(SCRIPT.read_text(), MODULE.read_text())
    assert not diverged, f"script vs module role ids (script, module): {diverged}"


def test_the_environment_cannot_set_the_suffix() -> None:
    text = SCRIPT.read_text()
    # Whole word: CUSTOM_ROLE_SUFFIX_MODULE is the script's own constant.
    assert not re.search(r"\$\{?CUSTOM_ROLE_SUFFIX\b", text), (
        "register-tenant.sh reads CUSTOM_ROLE_SUFFIX from the environment; the "
        "suffix is terraform/modules/custom_role_ids' constant"
    )


@pytest.mark.parametrize("base", ["swarmTenantWorkerFirestore", "swarmBucketMetadataReader"])
def test_each_role_id_is_spelled_once(base: str) -> None:
    """The describe and the binding read the same variable, not a re-spelling."""
    code = [
        line
        for line in SCRIPT.read_text().splitlines()
        if base in line and not line.lstrip().startswith("#")
    ]
    assert len(code) == 1, f"{base} is spelled on {len(code)} code lines: {code}"


def test_a_changed_module_suffix_is_caught() -> None:
    module = MODULE.read_text()
    changed = SUFFIX_RE.sub('  custom_role_suffix = "v2"', module, count=1)
    assert changed != module
    diverged = _diverged(SCRIPT.read_text(), changed)
    assert set(diverged) == set(SCRIPT_ROLES), diverged


def test_a_changed_script_suffix_is_caught() -> None:
    script = SCRIPT.read_text()
    changed, n = re.subn(
        r'^CUSTOM_ROLE_SUFFIX_MODULE=.*$',
        'CUSTOM_ROLE_SUFFIX_MODULE="v2"',
        script,
        flags=re.M,
    )
    assert n == 1
    diverged = _diverged(changed, MODULE.read_text())
    assert set(diverged) == set(SCRIPT_ROLES), diverged
