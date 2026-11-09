"""Voice it's speech sweep runs every two minutes.

docs/design/voice-it-own-speech.md §4.5 (stop 1, the sweep) and §6 row I4,
lane L5. Owner's recorded answer: the sweep route admits the tick account and
the doctor's (SWARM_SPEECH_SWEEP_CALLERS); the job signs as the tick account,
the platform's existing Cloud Scheduler identity.

These read the Terraform as text: no provider, no plan, no credentials.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
INFRA = ROOT / "terraform" / "infra"
SWEEP_TF = INFRA / "voice_speech_sweep.tf"
LOCALS_TF = INFRA / "locals.tf"
DEPLOYER_TF = INFRA / "deployer.tf"
ROUTES_PY = ROOT / "apps" / "swarm-api" / "swarm_api" / "routes" / "voice_speech.py"
FRAGMENT = ROOT / "install" / "manifest.d" / "voice-speech-sweep.yaml"
COMMON_SH = ROOT / "scripts" / "lib" / "common.sh"

JOB = ("google_cloud_scheduler_job", "voice_speech_sweep")
GRANT = ("google_cloud_run_v2_service_iam_member", "speech_sweep_tick_invokes_api")


def _block(text: str, header: str) -> str:
    """The body of the block whose opening line matches `header` (a regex),
    braces balanced; fails the test if there is none."""
    m = re.search(header + r"\s*\{", text, re.M)
    assert m, f"no block matching {header!r}"
    depth, i = 0, m.end() - 1
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1 : j]
    raise AssertionError(f"unbalanced block {header!r}")


def _resource(text: str, rtype: str, name: str) -> str:
    return _block(text, rf'^resource\s+"{rtype}"\s+"{name}"')


def _code(text: str) -> str:
    """The text with every `#` comment removed (no `#` sits inside a string
    in this file)."""
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


@pytest.fixture(scope="module")
def sweep_tf() -> str:
    assert SWEEP_TF.is_file(), "terraform/infra/voice_speech_sweep.tf does not exist"
    return _code(SWEEP_TF.read_text())


@pytest.fixture(scope="module")
def job(sweep_tf: str) -> str:
    return _resource(sweep_tf, *JOB)


@pytest.fixture(scope="module")
def grant(sweep_tf: str) -> str:
    return _resource(sweep_tf, *GRANT)


def _resource_types(text: str) -> list[tuple[str, str]]:
    return re.findall(r'^resource\s+"([a-z0-9_]+)"\s+"([a-z0-9_]+)"', text, re.M)


# --- the switch --------------------------------------------------------------


def test_the_file_makes_the_job_and_its_one_grant_only(sweep_tf: str) -> None:
    assert sorted(_resource_types(sweep_tf)) == sorted([JOB, GRANT])
    assert not re.search(r"^data\s", sweep_tf, re.M)


def test_every_resource_exists_only_while_voice_speech_is_on(sweep_tf: str) -> None:
    for rtype, name in _resource_types(sweep_tf):
        body = _resource(sweep_tf, rtype, name)
        first = body.strip().splitlines()[0].strip()
        assert first == "count = local.voice_speech_on ? 1 : 0", (rtype, name, first)


# --- the job -----------------------------------------------------------------


def test_job_runs_every_two_minutes_in_utc(job: str) -> None:
    assert re.search(r'^\s*schedule\s*=\s*"\*/2 \* \* \* \*"\s*$', job, re.M), job
    assert re.search(r'^\s*time_zone\s*=\s*"Etc/UTC"\s*$', job, re.M), job


def test_job_posts_to_the_relays_sweep_route_on_swarm_api(job: str) -> None:
    route = re.search(r'^SWEEP_ROUTE\s*=\s*"([^"]+)"', ROUTES_PY.read_text(), re.M)
    assert route, "swarm_api/routes/voice_speech.py names no SWEEP_ROUTE"
    target = _block(job, r"^\s*http_target")
    assert re.search(r'^\s*http_method\s*=\s*"POST"\s*$', target, re.M), target
    uri = re.search(r"^\s*uri\s*=\s*(.+)$", target, re.M)
    assert uri, target
    assert 'module.cloud_run_api.service_urls["swarm-api"]' in uri.group(1), uri.group(1)
    assert uri.group(1).rstrip().endswith(f'{route.group(1)}"'), uri.group(1)


def test_job_signs_as_the_tick_account_for_the_audience_the_route_checks(job: str) -> None:
    oidc = _block(job, r"^\s*oidc_token")
    assert re.search(
        r"^\s*service_account_email\s*=\s*module\.iam\.tick_service_account\s*$", oidc, re.M
    ), oidc
    assert re.search(r'^\s*audience\s*=\s*local\.push_audiences\["swarm-api"\]\s*$', oidc, re.M), oidc
    assert "oauth_token" not in job
    # The route admits exactly what locals.tf hands it: the same audience, and
    # the tick account among the callers.
    locals_tf = LOCALS_TF.read_text()
    assert re.search(
        r'SWARM_SPEECH_SWEEP_AUDIENCE\s*=\s*local\.voice_speech_on \? local\.push_audiences\["swarm-api"\]',
        locals_tf,
    )
    callers = re.search(r"SWARM_SPEECH_SWEEP_CALLERS\s*=\s*(.+)$", locals_tf, re.M)
    assert callers and "module.iam.tick_service_account" in callers.group(1)


def test_job_has_a_retry_and_an_attempt_deadline(job: str) -> None:
    deadline = re.search(r'^\s*attempt_deadline\s*=\s*"(\d+)s"\s*$', job, re.M)
    assert deadline, job
    # Long enough for a stop's operation wait (ComputeClient.OPERATION_SECONDS,
    # 300) and inside swarm-api's request_timeout (660s), Cloud Scheduler's
    # HTTP limit being 1800s.
    assert 300 < int(deadline.group(1)) < 660
    retry = _block(job, r"^\s*retry_config")
    count = re.search(r"^\s*retry_count\s*=\s*(\d+)\s*$", retry, re.M)
    assert count and int(count.group(1)) >= 1, retry


def test_job_carries_the_managed_by_label(job: str) -> None:
    # google_cloud_scheduler_job takes no labels argument; the platform's
    # scheduler jobs carry the label as their description's first field.
    desc = re.search(r'^\s*description\s*=\s*"([^"]*)"', job, re.M)
    assert desc and desc.group(1).startswith("managed-by=swarm-terraform;"), job
    name = re.search(r'^\s*name\s*=\s*"([^"]*)"', job, re.M)
    assert name and name.group(1) == "${var.name_prefix}-voice-speech-sweep", job


def test_job_waits_for_the_grant(job: str) -> None:
    depends = re.search(r"^\s*depends_on\s*=\s*\[(.*?)\]", job, re.M | re.S)
    assert depends and f"{GRANT[0]}.{GRANT[1]}" in depends.group(1), job


# --- the grant ---------------------------------------------------------------


def test_grant_is_run_invoker_on_swarm_api_for_the_tick_only(grant: str) -> None:
    assert re.search(r'^\s*role\s*=\s*"roles/run\.invoker"\s*$', grant, re.M), grant
    assert re.search(r"^\s*member\s*=\s*module\.iam\.tick_member\s*$", grant, re.M), grant
    assert re.search(
        r'^\s*name\s*=\s*\[for k, _ in module\.cloud_run_api\.service_ids : k if k == "swarm-api"\]\[0\]\s*$',
        grant,
        re.M,
    ), grant


def test_release_deployer_already_acts_as_the_tick() -> None:
    # Creating a job that mints OIDC tokens as the tick needs actAs on it; the
    # deployer's map already has it, so this lane adds none.
    body = DEPLOYER_TF.read_text()
    assert re.search(r'"swarm-tick"\s*=\s*module\.iam\.tick_service_account', body)


# --- the Day One installer ---------------------------------------------------


def test_manifest_fragment_lists_both_for_infra_pass() -> None:
    doc = yaml.safe_load(FRAGMENT.read_text())
    refs = {r for e in doc["entries"] for r in e["refs"]["terraform"]}
    assert refs == {f"infra/{JOB[0]}.{JOB[1]}", f"infra/{GRANT[0]}.{GRANT[1]}"}
    for entry in doc["entries"]:
        assert entry["step"].startswith("infra.pass-"), entry
        assert "voice_speech" in entry["apply"], entry


# --- the shared project ------------------------------------------------------


def _deny_list() -> list[str]:
    """The never-touch list, read the way every script reads it: through
    deny_list_load in scripts/lib/common.sh, from a config object. The
    repository holds no deployment's list (docs/design/generic-repository.md,
    section 2.4), so the object is the placeholder one,
    scripts/lib/deny-list.example.json, laid out by deny_list_use_example in a
    directory of this test's own; the plan guard judges every real plan against
    the deployment's own object."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {k: v for k, v in os.environ.items()
               if k not in ("PROJECT_ID", "ENVIRONMENT", "SWARM_DEPLOY_CONFIG_DIR", "XDG_CACHE_HOME")}
        env.update(SWARM_NO_PROJECT="1", SWARM_ENV_FILE=str(Path(tmp) / "absent.env"), NO_COLOR="1")
        proc = subprocess.run(
            ["bash", "-c", 'set -euo pipefail; source "$1"; deny_list_use_example "$2"; '
             'printf "%s\\n" "${SHARED_DENY_LIST[@]}"', "_", str(COMMON_SH), tmp],
            env=env, capture_output=True, text=True, timeout=60, check=False,
        )
    assert proc.returncode == 0, proc.stderr
    names = proc.stdout.splitlines()
    assert "peer-vpc" in names, names
    return names


def test_nothing_names_a_deny_listed_resource() -> None:
    text = SWEEP_TF.read_text() + FRAGMENT.read_text()
    hits = [n for n in _deny_list() if n in text]
    assert not hits, hits
