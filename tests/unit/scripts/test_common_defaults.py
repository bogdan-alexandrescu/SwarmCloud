"""Scripts never default to the live project, and the deny-list is keyed by project.

`scripts/lib/common.sh` used to say `PROJECT_ID="${PROJECT_ID:-saga-agents-staging}"`.
That default is what a fresh install inherits when the operator forgets a
`.env`: every script quietly aims at the shared project, the one holding
another team's live cluster, VPC, buckets and service accounts. An unset
project is now an error (Day One plan, lane DAYONE-DESAGA-2, R4).

`SHARED_DENY_LIST` stays in `common.sh` (CLAUDE.md rule 2: once, in one place)
and is keyed by project: the shared project's list applies when `PROJECT_ID` is
the shared project, and it is empty for any other project, whose resources
belong to nobody else. The list for the shared project must never weaken:
`test_deny_list_full_for_shared_project` compares it, entry for entry, with the
literal block the other deny-list tests hold to the live project.

Offline: sources common.sh in bash with a scrubbed environment and no `.env`;
no cloud call, no credential.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
COMMON = REPO / "scripts" / "lib" / "common.sh"
SHARED_PROJECT = "saga-agents-staging"

#: Variables load_env reads that could hand it a project, a .env or a cluster.
#: Scrubbed so the developer's or the runner's own settings cannot make a test pass.
_SCRUB = ("PROJECT_ID", "SWARM_ENV_FILE", "KUBECONFIG", "GKE_CLUSTER", "ARTIFACT_BUCKET")


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    # A path that does not exist: load_env sources nothing, so the repository's
    # own .env (if a developer has one) cannot supply the project either.
    env["SWARM_ENV_FILE"] = str(tmp_path / "no-env-file")
    env.update(extra)
    return env


def _source(tmp_path: Path, script: str, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'source "$1"; {script}', "_", str(COMMON)],
        capture_output=True, text=True, timeout=60, check=False, env=_env(tmp_path, **extra),
    )


def _literal_deny_list() -> list[str]:
    """The `SHARED_DENY_LIST=( ... )` block as written: the shared project's list."""
    block = re.search(r"^SHARED_DENY_LIST=\(\n(.*?)^\)", COMMON.read_text(), re.MULTILINE | re.DOTALL)
    assert block, "scripts/lib/common.sh no longer declares SHARED_DENY_LIST=( ... )"
    return re.findall(r'^\s*"([^"]+)"', block.group(1), re.MULTILINE)


def test_unset_project_is_an_error(tmp_path: Path) -> None:
    # MUTATION: put back `PROJECT_ID="${PROJECT_ID:-saga-agents-staging}"` in
    # load_env and this fails: sourcing succeeds and prints the live project.
    proc = _source(tmp_path, 'printf "%s" "${PROJECT_ID}"')
    assert proc.returncode != 0, f"sourcing common.sh with PROJECT_ID unset succeeded: {proc.stdout!r}"
    assert SHARED_PROJECT not in proc.stdout
    assert "PROJECT_ID" in proc.stderr, proc.stderr


def test_empty_project_is_an_error(tmp_path: Path) -> None:
    proc = _source(tmp_path, 'printf "%s" "${PROJECT_ID}"', PROJECT_ID="")
    assert proc.returncode != 0, f"sourcing common.sh with PROJECT_ID empty succeeded: {proc.stdout!r}"
    assert "PROJECT_ID" in proc.stderr, proc.stderr


def test_project_from_env_file_is_honoured(tmp_path: Path) -> None:
    env_file = tmp_path / "env"
    env_file.write_text("PROJECT_ID=swarm-dayone-test\n")
    env_file.chmod(0o600)
    proc = _source(tmp_path, 'printf "%s" "${PROJECT_ID}"', SWARM_ENV_FILE=str(env_file))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "swarm-dayone-test"


def test_deny_list_empty_for_other_project(tmp_path: Path) -> None:
    # MUTATION: drop the keying in load_env and this fails: the other team's
    # names are refused in a project they are not in.
    script = (
        'printf "count=%s\\n" "${#SHARED_DENY_LIST[@]}"; '
        'if is_shared_resource agents-staging; then echo shared=yes; else echo shared=no; fi; '
        'printf "guard=%s\\n" "$(guard_deny_json)"'
    )
    proc = _source(tmp_path, script, PROJECT_ID="swarm-dayone-test")
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert "count=0" in lines, proc.stdout
    assert "shared=no" in lines, proc.stdout
    # (default) stays: a named database is the platform's rule in every project.
    assert 'guard=["(default)"]' in lines, proc.stdout


def test_deny_list_full_for_shared_project(tmp_path: Path) -> None:
    literal = _literal_deny_list()
    assert len(literal) >= 30, f"the literal deny-list lost entries: {len(literal)}"
    proc = _source(tmp_path, 'printf "%s\\n" "${SHARED_DENY_LIST[@]}"', PROJECT_ID=SHARED_PROJECT)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == literal
    for name in ("agents-staging", "agents-staging-vpc", f"crawler@{SHARED_PROJECT}.iam.gserviceaccount.com"):
        check = _source(tmp_path, f'is_shared_resource "{name}"', PROJECT_ID=SHARED_PROJECT)
        assert check.returncode == 0, f"{name} is not refused in the shared project"


def test_rekeying_never_loses_the_shared_list(tmp_path: Path) -> None:
    # load_env run again after PROJECT_ID moved back to the shared project must
    # restore the whole list, not keep the empty one an earlier call left.
    script = (
        'PROJECT_ID=swarm-dayone-test; load_env; a="${#SHARED_DENY_LIST[@]}"; '
        f'PROJECT_ID={SHARED_PROJECT}; load_env; printf "%s %s" "${{a}}" "${{#SHARED_DENY_LIST[@]}}"'
    )
    proc = _source(tmp_path, script, PROJECT_ID=SHARED_PROJECT)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"0 {len(_literal_deny_list())}"


def test_dev_loop_sets_its_project_before_sourcing() -> None:
    # dev.sh targets the emulator's project (swarm-local). It must set
    # PROJECT_ID before it sources common.sh, or `make dev` dies on a machine
    # with no .env -- and it must never fall back to the live project.
    text = (REPO / "scripts" / "lib" / "dev.sh").read_text()
    source_at = text.index('source "$(cd -- ')
    set_at = text.find('PROJECT_ID="${DEV_PROJECT_ID:-swarm-local}"')
    assert 0 <= set_at < source_at, "dev.sh sets PROJECT_ID only after sourcing common.sh"
