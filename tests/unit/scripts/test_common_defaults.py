"""Scripts never default to a deployment: no project, region or zone defaults.

`scripts/lib/common.sh` once defaulted PROJECT_ID to the live project, and later
REGION and ZONE to the live region. A default is what a fresh install inherits
when the operator forgets a `.env`: every script quietly aims at somebody's
deployment, possibly the shared project that holds another team's live cluster.
An unset project or region is now an error that names the variable
(docs/design/generic-repository.md, section 2.2). The zone has no default
either, but nothing in scripts/ or the workflows reads it, so sourcing
common.sh does not require it: ZONE stays empty, and a script that comes to
need it calls `require_setting ZONE` itself.

The never-touch list is no longer keyed by a project id written here: it is
read from the deployment's own config object (test_shared_deny_list.py holds
its rules). What this file holds is how the opt-out and the defaults meet it:
a script that names no project never gets a list, so a guard it reaches
refuses, and switching PROJECT_ID never keeps the previous project's list.

Offline: sources common.sh in bash with a scrubbed environment and no `.env`;
the config object is a local directory (SWARM_DEPLOY_CONFIG_DIR). No cloud
call, no credential.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

# It proves what common.sh does with no list, or with its own: none is served for it.
pytestmark = pytest.mark.no_placeholder_deployment

REPO = Path(__file__).resolve().parents[3]
COMMON = REPO / "scripts" / "lib" / "common.sh"
EXAMPLE = REPO / "scripts" / "lib" / "deny-list.example.json"
PROJECT = "example-project"
REFUSAL = "is not readable; nothing that mutates runs without it"

#: Variables load_env reads that could hand it a project, a .env, a cluster or a
#: list. Scrubbed so the developer's or the runner's own settings cannot make a
#: test pass.
_SCRUB = ("PROJECT_ID", "REGION", "ZONE", "SWARM_ENV_FILE", "KUBECONFIG", "GKE_CLUSTER",
          "ARTIFACT_BUCKET", "SWARM_NO_PROJECT", "SWARM_DEPLOY_CONFIG_DIR", "TF_STATE_BUCKET",
          "STATE_BUCKET_NAME")


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    # A path that does not exist: load_env sources nothing, so the repository's
    # own .env (if a developer has one) cannot supply the project either.
    env["SWARM_ENV_FILE"] = str(tmp_path / "no-env-file")
    env["SWARM_DEPLOY_CONFIG_DIR"] = str(tmp_path / "config")
    env["XDG_CACHE_HOME"] = str(tmp_path / "cache")
    env["ENVIRONMENT"] = "dev"
    env.update(extra)
    return env


def _write_list(tmp_path: Path, project: str = PROJECT) -> None:
    folder = tmp_path / "config" / "dev"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = folder / "deny-list.json"
    target.write_text(json.dumps({**json.loads(EXAMPLE.read_text()), "project": project}))
    target.chmod(0o600)


def _source(tmp_path: Path, script: str, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'source "$1"; {script}', "_", str(COMMON)],
        capture_output=True, text=True, timeout=60, check=False, env=_env(tmp_path, **extra),
    )


def test_unset_project_is_an_error(tmp_path: Path) -> None:
    proc = _source(tmp_path, 'printf "%s" "${PROJECT_ID}"; echo reached')
    assert proc.returncode != 0, f"sourcing common.sh with PROJECT_ID unset succeeded: {proc.stdout!r}"
    assert "reached" not in proc.stdout
    assert "PROJECT_ID" in proc.stderr, proc.stderr


def test_empty_project_is_an_error(tmp_path: Path) -> None:
    proc = _source(tmp_path, 'printf "%s" "${PROJECT_ID}"', PROJECT_ID="")
    assert proc.returncode != 0, f"sourcing common.sh with PROJECT_ID empty succeeded: {proc.stdout!r}"
    assert "PROJECT_ID" in proc.stderr, proc.stderr


def test_unset_region_is_an_error_that_names_it(tmp_path: Path) -> None:
    # MUTATION: put back `REGION="${REGION:-us-central1}"` in load_env and this
    # fails: sourcing succeeds with a region nobody chose.
    _write_list(tmp_path)
    proc = _source(tmp_path, 'echo "reached ${REGION}"', PROJECT_ID=PROJECT, ZONE="example-zone")
    assert proc.returncode != 0, proc.stdout
    assert "reached" not in proc.stdout
    assert "REGION is not set" in proc.stderr, proc.stderr


def test_unset_zone_does_not_stop_a_script_and_stays_empty(tmp_path: Path) -> None:
    # Nothing reads ZONE, and requiring it when common.sh was sourced stopped
    # every image build on main (no workflow sets it). It is optional, with no
    # default: an unset zone is empty, never a guessed one.
    # MUTATION: put back `require_setting ZONE` in load_env and this fails with
    # "ZONE is not set"; give ZONE a default and the empty check fails.
    _write_list(tmp_path)
    proc = _source(tmp_path, 'printf "reached [%s]" "${ZONE}"', PROJECT_ID=PROJECT, REGION="example-region")
    assert "ZONE is not set" not in proc.stderr, proc.stderr
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "reached []", proc.stdout


def test_unset_project_stops_a_script_even_with_region_and_zone(tmp_path: Path) -> None:
    proc = _source(tmp_path, "echo reached", REGION="example-region", ZONE="example-zone")
    assert proc.returncode != 0, proc.stdout
    assert "reached" not in proc.stdout
    assert "PROJECT_ID is not set" in proc.stderr, proc.stderr


def test_unset_region_stops_a_script_with_zone_unset_too(tmp_path: Path) -> None:
    _write_list(tmp_path)
    proc = _source(tmp_path, "echo reached", PROJECT_ID=PROJECT)
    assert proc.returncode != 0, proc.stdout
    assert "reached" not in proc.stdout
    assert "REGION is not set" in proc.stderr, proc.stderr
    assert "ZONE is not set" not in proc.stderr, proc.stderr


def test_no_default_region_or_zone_is_written() -> None:
    text = COMMON.read_text()
    # Only a placeholder may follow `:-` (deny_list_use_example's self-test
    # fallback); a real region there is a guess at somebody's deployment.
    assert not re.search(r'^\s*(REGION|ZONE)="\$\{(REGION|ZONE):-(?!example-)[a-z]', text, re.MULTILINE)


def test_no_project_opt_out_has_no_list_and_its_guards_refuse(tmp_path: Path) -> None:
    # A script that touches no project sets SWARM_NO_PROJECT=1: it runs with
    # PROJECT_ID empty -- never a deployment's -- needs no region, and a project
    # nobody named is never treated as one whose list may be empty.
    proc = _source(tmp_path, 'printf "[%s] %s\\n" "${PROJECT_ID}" "${#SHARED_DENY_LIST[@]}"; '
                   'is_shared_resource anything; echo reached', SWARM_NO_PROJECT="1")
    assert proc.stdout.splitlines()[0] == "[] 0", proc.stderr
    assert proc.returncode != 0
    assert "reached" not in proc.stdout
    assert REFUSAL in proc.stderr, proc.stderr


def test_prerequisites_no_cloud_needs_no_project() -> None:
    # --no-cloud checks this machine only; it opts out before sourcing common.sh.
    text = (REPO / "scripts" / "prerequisites.sh").read_text()
    assert text.index("SWARM_NO_PROJECT=1") < text.index('source "$(cd -- ')


def test_project_from_env_file_is_honoured(tmp_path: Path) -> None:
    env_file = tmp_path / "env"
    env_file.write_text(f"PROJECT_ID={PROJECT}\nREGION=example-region\nZONE=example-zone\n")
    env_file.chmod(0o600)
    _write_list(tmp_path)
    proc = _source(tmp_path, 'printf "%s %s %s" "${PROJECT_ID}" "${REGION}" "${ZONE}"',
                   SWARM_ENV_FILE=str(env_file))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"{PROJECT} example-region example-zone"


def test_switching_project_never_keeps_the_previous_list(tmp_path: Path) -> None:
    # The list was keyed by a literal project once; now it is keyed by the
    # project it was loaded for. A shell that moves PROJECT_ID to a project
    # with no readable list must refuse, not judge by the old one.
    # MUTATION: return early from deny_list_load whenever a list is loaded and
    # this fails: "reached" is printed.
    _write_list(tmp_path)
    proc = _source(tmp_path, 'PROJECT_ID=other-project; is_shared_resource peer-cluster; echo reached',
                   PROJECT_ID=PROJECT, REGION="example-region", ZONE="example-zone")
    assert proc.returncode != 0, proc.stdout
    assert "reached" not in proc.stdout
    assert REFUSAL in proc.stderr, proc.stderr


def test_dev_loop_sets_its_project_before_sourcing() -> None:
    # dev.sh targets the emulator's project (swarm-local). It must set
    # PROJECT_ID before it sources common.sh, or `make dev` dies on a machine
    # with no .env, and it says it touches no cloud project, so sourcing does
    # not fetch a never-touch list from a state bucket that does not exist.
    text = (REPO / "scripts" / "lib" / "dev.sh").read_text()
    source_at = text.index('source "$(cd -- ')
    set_at = text.find('PROJECT_ID="${DEV_PROJECT_ID:-swarm-local}"')
    assert 0 <= set_at < source_at, "dev.sh sets PROJECT_ID only after sourcing common.sh"
    assert 0 <= text.find("SWARM_NO_PROJECT=1") < source_at


#: What CI runs with no PROJECT_ID (application.yml's shell and quick-checks
#: jobs, security.yml's checks and trivy jobs): each touches no project, says
#: so with SWARM_NO_PROJECT=1, and must get past common.sh (the Day One project-defaults lane).
#: The guards' self-tests prove themselves against the placeholder list
#: (deny_list_use_example). destroy.sh's is not here: its fixture still names
#: the old literal's resources, which lane 3 replaces (see the PR body).
_CI_NO_PROJECT = [
    ("scripts/lib/plan-guard.sh", "--self-test"),
    ("scripts/lib/kubectl-guard.sh", "--self-test"),
    ("scripts/lib/auth-guard.sh", "--self-test"),
    ("scripts/lib/check-doc-links.sh",),
    ("scripts/lib/check-env-parity.sh",),
]


def _script(tmp_path: Path, *argv: str, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(REPO / argv[0]), *argv[1:]], capture_output=True, text=True,
        timeout=300, check=False, cwd=str(REPO), env=_env(tmp_path, **extra),
    )


def test_ci_checks_run_with_no_project(tmp_path: Path) -> None:
    for argv in _CI_NO_PROJECT:
        proc = _script(tmp_path, *argv)
        assert "PROJECT_ID is not set" not in proc.stderr, argv
        assert proc.returncode == 0, (argv, proc.stderr[-2000:])


def test_only_the_offline_mode_opts_out(tmp_path: Path) -> None:
    # The opt-out is per mode: destroy.sh's real run, bench-ui.sh's capture
    # and ci-fix.sh with no API address still refuse an unset project.
    for argv in (("scripts/destroy.sh",), ("scripts/bench-ui.sh",), ("scripts/ci-fix.sh",)):
        proc = _script(tmp_path, *argv, API_HOST="", API_URL="")
        assert proc.returncode != 0, argv
        assert "PROJECT_ID is not set" in proc.stderr, (argv, proc.stderr[-2000:])


def test_require_project_stops_an_opted_out_script(tmp_path: Path) -> None:
    # check-cluster-network-parity.sh opts out to skip on a runner with no
    # credentials, and calls require_project once it has some.
    proc = _source(tmp_path, "require_project; echo reached", SWARM_NO_PROJECT="1")
    assert proc.returncode != 0
    assert "reached" not in proc.stdout
    assert "PROJECT_ID is not set" in proc.stderr


def test_empty_deny_list_expansions_are_guarded() -> None:
    # bash 3.2 dies on "${arr[@]}" of an empty array under `set -u`, and a list
    # written empty is a valid list.
    bare = re.compile(r'(?<![+])"\$\{SHARED_DENY_LIST\[[@*]\]\}"')
    offenders = []
    for path in sorted((REPO / "scripts").rglob("*.sh")):
        lines = path.read_text().splitlines()
        for n, line in enumerate(lines, 1):
            # Allowed: a line under an explicit non-empty test, and the copies
            # release-home.sh and release/publish.sh still take of the removed
            # SHARED_PROJECT_DENY_LIST (unbound now, so `set -u` stops them).
            guarded = "SHARED_PROJECT_DENY_LIST=" in line or (n > 1 and "-gt 0" in lines[n - 2])
            if bare.search(line) and not guarded:
                offenders.append(f"{path.relative_to(REPO)}:{n}")
    assert not offenders


def test_a_list_written_over_several_lines_reads_as_empty_and_says_why(tmp_path: Path) -> None:
    # deployment_list reads a one-line `NAME = [...]` only. A list split over
    # lines reads as empty, so a script that needs it refuses (fail-closed);
    # the operator is told why rather than left with "names no domain".
    # MUTATION: drop the note and this fails.
    _write_list(tmp_path)
    tfvars = tmp_path / "config" / "dev" / "dev.tfvars"
    tfvars.write_text('allowed_domains = [\n  "example.com",\n]\n')
    tfvars.chmod(0o600)
    proc = _source(tmp_path, 'printf "[%s]" "$(deployment_list allowed_domains)"', PROJECT_ID=PROJECT,
                   REGION="example-region", ZONE="example-zone")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "[]", proc.stdout
    assert "allowed_domains spans several lines" in proc.stderr, proc.stderr


def test_a_list_on_one_line_is_read_without_a_note(tmp_path: Path) -> None:
    _write_list(tmp_path)
    tfvars = tmp_path / "config" / "dev" / "dev.tfvars"
    tfvars.write_text('allowed_domains = ["example.com", "example.org"] # two\n')
    tfvars.chmod(0o600)
    proc = _source(tmp_path, 'deployment_list allowed_domains', PROJECT_ID=PROJECT,
                   REGION="example-region", ZONE="example-zone")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["example.com", "example.org"]
    assert "spans several lines" not in proc.stderr, proc.stderr


# A placeholder bucket (scripts/guards/placeholders.json) that is neither the
# default nor the configured name, so the explicit one is the one that wins.
_EXPLICIT_BUCKET = "swarm-artifacts-example-project"
_BUCKET = 'printf "%s|" "${TF_STATE_BUCKET}"; unset SWARM_DEPLOY_CONFIG_DIR; config_source dev'


def test_a_configured_state_bucket_name_is_read_before_the_default(tmp_path: Path) -> None:
    # A deployment whose bootstrap set state_bucket_name (a suffixed bucket)
    # names it in .env as STATE_BUCKET_NAME; the scripts and config_source
    # find it there, not under the default nobody created.
    # MUTATION: drop STATE_BUCKET_NAME from TF_STATE_BUCKET's fallback or from
    # config_source's and this fails.
    _write_list(tmp_path)
    proc = _source(tmp_path, _BUCKET, PROJECT_ID=PROJECT, REGION="example-region",
                   STATE_BUCKET_NAME=f"swarm-tfstate-{PROJECT}-x1")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"swarm-tfstate-{PROJECT}-x1|gs://swarm-tfstate-{PROJECT}-x1/config/dev", proc.stdout


def test_the_state_bucket_defaults_when_no_name_is_configured(tmp_path: Path) -> None:
    _write_list(tmp_path)
    proc = _source(tmp_path, _BUCKET, PROJECT_ID=PROJECT, REGION="example-region")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"swarm-tfstate-{PROJECT}|gs://swarm-tfstate-{PROJECT}/config/dev", proc.stdout


def test_an_explicit_tf_state_bucket_still_wins(tmp_path: Path) -> None:
    _write_list(tmp_path)
    proc = _source(tmp_path, _BUCKET, PROJECT_ID=PROJECT, REGION="example-region",
                   STATE_BUCKET_NAME="swarm-tfstate-configured", TF_STATE_BUCKET=_EXPLICIT_BUCKET)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"{_EXPLICIT_BUCKET}|gs://{_EXPLICIT_BUCKET}/config/dev", proc.stdout
