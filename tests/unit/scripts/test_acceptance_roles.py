"""ROLES-ACCEPT: the workspace roles acceptance probe
(docs/design/workspace-roles.md §6 row 6 and §8; archived issue 1518).

scripts/acceptance/groups/roles.sh measures, against dev, what the unit tests
in tests/unit/control_plane prove offline: a viewer is refused a submission
with `role_member_required` and may still read; the viewer grant and its
revocation are in the workspace's audit; another identity's `u-` workspace
stays `tenant_not_member`; and the scoped probe admin stays inside its limits: it
selects no workspace it is not a member of, an admin route not on its list
refuses it `probe_admin_not_permitted`, and it cannot name a person `admin`.

Held here, with no network and no credentials:

* the probe's judge, run by bash, PASSes exactly the API's own codes and
  actions, read from swarm_api rather than restated, and FAILs anything else;
* the probe SKIPs, naming what is missing, until the owner has provisioned
  its identities, and never runs against the shared smoke tenant;
* accept.yml runs it in the existing smoke job (the owner, 2026-10-28), through
  verify-remote.sh, which carries its four settings into the execution;
* the design records it as built.
"""

from __future__ import annotations

import importlib.util
import json
import os
import secrets
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from .test_release_reuses_ci_images import _code, _workflow

REPO = Path(__file__).resolve().parents[3]
GROUP = REPO / "scripts" / "acceptance" / "groups" / "roles.sh"
CONFIG = REPO / "scripts" / "acceptance" / "config.sh"
VERIFY_REMOTE = REPO / "scripts" / "verify-remote.sh"
DESIGN = REPO / "docs" / "design" / "workspace-roles.md"
TARGET = "acceptance/groups/roles"
_PLACEHOLDERS_SPEC = importlib.util.spec_from_file_location("placeholders", REPO / "tests" / "placeholders.py")
_PLACEHOLDERS = importlib.util.module_from_spec(_PLACEHOLDERS_SPEC)
_PLACEHOLDERS_SPEC.loader.exec_module(_PLACEHOLDERS)
placeholder_deployment = _PLACEHOLDERS.deployment()
#: The probe's settings: the test workspace, the viewer and platform-admin
#: identities, and another identity's personal workspace. None is a secret.
SETTINGS = (
    "SWARM_ACCEPTANCE_ROLES_TENANT",
    "SWARM_ACCEPTANCE_ROLES_VIEWER_SA",
    "SWARM_ACCEPTANCE_ROLES_ADMIN_SA",
    "SWARM_ACCEPTANCE_ROLES_OTHER_WORKSPACE",
)
CHECKS = ("viewer-submit", "viewer-read", "audit", "personal", "unlisted", "not-permitted", "no-admin")
REASON = "ROLES-ACCEPT test-acceptance-20261028190000"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="the probe needs bash and jq",
)


def _bash(script: str, **env: str) -> subprocess.CompletedProcess:
    full = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"), "NO_COLOR": "1", **env}
    return subprocess.run(["bash", "-c", f'source "{CONFIG}"\nsource "{GROUP}"\n{script}'], cwd=REPO, env=full,
                          capture_output=True, text=True, timeout=60)


def _judge(check: str, status: int | str, body: object, extra: str = "") -> tuple[str, str]:
    text = body if isinstance(body, str) else json.dumps(body)
    proc = _bash('roles_judge "${C}" "${S}" "${B}" "${X}"', C=check, S=str(status), B=text, X=extra)
    assert proc.returncode == 0, proc.stderr
    verdict, _, reason = proc.stdout.strip().partition("\t")
    assert verdict in ("PASS", "FAIL"), proc.stdout
    return verdict, reason


def _api() -> SimpleNamespace:
    """The codes and audit actions as swarm_api spells them. Imported here,
    not at module top: tests/unit/scripts collects without the service stack
    (test_collection_stays_cheap.py)."""
    from swarm_api import roles
    from swarm_api.auth import TenantNotMember
    from swarm_api.errors import ProbeAdminNotPermitted, RoleAdminRequired, RoleMemberRequired

    return SimpleNamespace(member_required=RoleMemberRequired.code, not_member=TenantNotMember.code,
                           default_set=roles.AUDIT_DEFAULT_SET,
                           not_permitted=ProbeAdminNotPermitted.code, admin_required=RoleAdminRequired.code,
                           probe_source=roles.SOURCE_PROBE_ADMIN)


def _refusal(code: str, **detail: str) -> dict:
    return {"code": code, "message": "refused", "detail": detail}


def _audit(*rows: dict) -> dict:
    return {"tenant_id": "roles-accept", "kind": "group", "audit": list(rows)}


def _default_set(to: str, frm: str, reason: str = REASON) -> dict:
    return {"action": _api().default_set, "by": "qa-admin@example-project.iam.gserviceaccount.com",
            "at": "2026-10-28T19:00:01.123456+00:00",
            "detail": {"from": frm, "to": to, "via": "probe_admin", "reason": reason}}


# ---------------------------------------------------------------------------
# The judge, held to the API's codes
# ---------------------------------------------------------------------------


def test_the_checks_are_the_seven_the_design_names():
    proc = _bash("roles_checks")
    lines = proc.stdout.splitlines()
    assert [line.split(":")[1].strip() for line in lines] == list(CHECKS)
    assert all(line.startswith("roles: ") for line in lines)


def test_a_viewers_submission_passes_only_on_the_apis_role_code():
    """MUTATION: accept any 403. A viewer refused for another reason --
    tenant_not_member because the identity left the group -- proves nothing
    about roles."""
    assert _judge("viewer-submit", 403, _refusal(_api().member_required))[0] == "PASS"
    assert _judge("viewer-submit", 403, _refusal(_api().not_member))[0] == "FAIL"
    assert _judge("viewer-submit", 403, _refusal("role_admin_required"))[0] == "FAIL"
    # The gate is off, or the viewer grant did not take: a task was created.
    verdict, reason = _judge("viewer-submit", 201, {"id": "t-1", "state": "QUEUED"})
    assert verdict == "FAIL" and "201" in reason


def test_a_viewers_read_passes_only_on_200():
    assert _judge("viewer-read", 200, {"tasks": []})[0] == "PASS"
    assert _judge("viewer-read", 403, _refusal(_api().member_required))[0] == "FAIL"


def test_the_audit_needs_both_the_grant_and_its_revocation_of_this_run():
    both = _audit(_default_set("member", "viewer"), _default_set("viewer", "member"))
    assert _judge("audit", 200, both, REASON)[0] == "PASS"
    # MUTATION: look for the grant only.
    verdict, reason = _judge("audit", 200, _audit(_default_set("viewer", "member")), REASON)
    assert verdict == "FAIL" and "revocation" in reason
    # Another run's rows are not this run's evidence.
    other = _audit(_default_set("member", "viewer", "ROLES-ACCEPT older"),
                   _default_set("viewer", "member", "ROLES-ACCEPT older"))
    assert _judge("audit", 200, other, REASON)[0] == "FAIL"
    assert _judge("audit", 503, _refusal("role_unavailable"), REASON)[0] == "FAIL"


def test_the_platform_admin_reach_is_no_longer_measured_here():
    """The probe's admin is a scoped probe admin, a member of the test
    workspace's group: it never goes through the reach, so no check may claim
    the reach or a `platform_admin_acted` row (owner decision, 2026-11-20)."""
    text = GROUP.read_text()
    assert "platform_admin_acted" not in text
    assert "reach" not in [line.split(":")[1].strip() for line in _bash("roles_checks").stdout.splitlines()]
    assert _judge("reach", 200, _audit())[0] == "FAIL"


def test_another_identitys_personal_workspace_passes_only_on_tenant_not_member():
    assert _judge("personal", 403, _refusal(_api().not_member))[0] == "PASS"
    assert _judge("personal", 200, {"tenant_id": "u-someone"})[0] == "FAIL"
    assert _judge("personal", 403, _refusal(_api().member_required))[0] == "FAIL"


def test_an_unlisted_workspace_passes_only_on_tenant_not_member():
    """MUTATION: accept any refusal, or let the probe admin in. Selected, it
    is in that workspace's group or a platform admin: neither is the scoped
    probe admin the owner provisioned."""
    assert _judge("unlisted", 403, _refusal(_api().not_member))[0] == "PASS"
    verdict, reason = _judge("unlisted", 200, {"tenant": {"tenant_id": "smoke"}})
    assert verdict == "FAIL" and "200" in reason
    assert _judge("unlisted", 503, _refusal("membership_unavailable"))[0] == "FAIL"
    assert _judge("unlisted", 403, _refusal(_api().not_permitted))[0] == "FAIL"


def test_an_unlisted_admin_route_passes_only_on_probe_admin_not_permitted():
    """MUTATION: accept role_admin_required, which a plain member gets too,
    or the 422 the empty name meets once the gate let it past."""
    assert _judge("not-permitted", 403, _refusal(_api().not_permitted, source="probe_admin"))[0] == "PASS"
    assert _judge("not-permitted", 403, _refusal(_api().admin_required))[0] == "FAIL"
    verdict, reason = _judge("not-permitted", 422, _refusal("validation_failed", field="display_name"))
    assert verdict == "FAIL" and "admin gate" in reason and "nothing was written" in reason
    assert _judge("not-permitted", 200, {"changed": False})[0] == "FAIL"


def test_naming_an_admin_passes_only_on_role_admin_required_as_the_probe_admin():
    """MUTATION: accept role_admin_required from any source. A member the
    probe-admin entry does not cover is refused the same code by the role
    write's own gate, which proves nothing about the entry's limit."""
    api = _api()
    assert _judge("no-admin", 403, _refusal(api.admin_required, source=api.probe_source))[0] == "PASS"
    verdict, reason = _judge("no-admin", 403, _refusal(api.admin_required, source="override"))
    assert verdict == "FAIL" and "probe_admin_users" in reason
    assert _judge("no-admin", 403, _refusal(api.admin_required))[0] == "FAIL"
    verdict, reason = _judge("no-admin", 422, _refusal("validation_failed", field="reason"))
    assert verdict == "FAIL" and "nothing was written" in reason
    assert _judge("no-admin", 200, {"role": "admin"})[0] == "FAIL"
    assert _judge("no-admin", 403, _refusal(api.not_permitted, source=api.probe_source))[0] == "FAIL"


def test_the_limit_checks_send_nothing_a_gate_could_turn_into_a_write():
    """The rename's name and the role write's reason are refused 422 after
    the admin gate and before anything is read or written, so a broken gate
    cannot change the test workspace. Held to the API's own limits."""
    from swarm_api import roles

    text = GROUP.read_text()
    assert "'{\"display_name\": \"\"}'" in text
    width = int(text.split("printf -v pad '%*s' ", 1)[1].split()[0])
    assert width > roles.REASON_MAX
    nobody = text.split('ROLES_NOBODY="', 1)[1].split('"', 1)[0]
    assert nobody.endswith("@example.invalid") and not roles.is_service_account(nobody)
    assert roles.clean_email(nobody) == nobody


def test_the_judge_never_echoes_a_response_body_it_does_not_understand():
    secret = "ya29." + os.urandom(12).hex()
    verdict, reason = _judge("viewer-submit", 500, f"<html>{secret}</html>")
    assert verdict == "FAIL" and secret not in reason


# ---------------------------------------------------------------------------
# What it needs before it runs
# ---------------------------------------------------------------------------


def test_it_names_every_missing_setting():
    proc = _bash("roles_settings_problem || true")
    for name in SETTINGS:
        assert name in proc.stdout, proc.stdout


def test_it_never_narrows_the_smoke_tenant_or_a_personal_one():
    """Every other acceptance group submits in smoke: a viewer default there
    would turn the whole suite red for the length of the probe."""
    full = {name: value for name, value in zip(SETTINGS, (
        "roles-accept", "swarm-qa@example-project.iam.gserviceaccount.com",
        "qa-admin@example-project.iam.gserviceaccount.com", "u-someone"))}
    assert _bash('roles_settings_problem && echo OK', **full).stdout.strip() == "OK"
    for tenant in ("smoke", "u-someone", "Eng"):
        proc = _bash("roles_settings_problem || echo REFUSED", **{**full, SETTINGS[0]: tenant})
        assert "REFUSED" in proc.stdout, tenant
    proc = _bash("roles_settings_problem || echo REFUSED", **{**full, SETTINGS[3]: "eng"})
    assert "REFUSED" in proc.stdout


# ---------------------------------------------------------------------------
# Run on its own, as verify-remote.sh runs it, without the sandbox URL
# ---------------------------------------------------------------------------


def _standalone(tmp_path: Path, **settings: str) -> subprocess.CompletedProcess:
    """Executes the group file as the swarm-verify job does for this step: the
    probe's own settings only, no SWARM_ACCEPTANCE_SANDBOX_REPOSITORY_URL."""
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "NO_COLOR": "1", "TMPDIR": str(tmp_path),
           "SWARM_ENV_FILE": str(tmp_path / "no.env"), "PROJECT_ID": "swarm-dev-test",
           **placeholder_deployment.child_env("swarm-dev-test"), **settings}
    env.pop("SWARM_ACCEPTANCE_SANDBOX_REPOSITORY_URL", None)
    return subprocess.run(["bash", str(GROUP)], cwd=REPO, env=env, capture_output=True, text=True, timeout=60)


def test_unprovisioned_it_skips_naming_every_missing_setting_instead_of_failing(tmp_path):
    proc = _standalone(tmp_path)
    assert proc.returncode == 0, proc.stderr
    for name in SETTINGS:
        assert name in proc.stderr, proc.stderr
    assert proc.stderr.count("SKIP") == len(CHECKS), proc.stderr
    assert "NOT MEASURED" in proc.stderr
    assert "SANDBOX_REPOSITORY_URL" not in proc.stderr
    assert "FAIL" not in proc.stderr


def test_partly_provisioned_it_names_only_what_is_missing(tmp_path):
    proc = _standalone(tmp_path, **{SETTINGS[0]: "roles-accept", SETTINGS[2]: "u-someone"})
    assert proc.returncode == 0, proc.stderr
    assert f"{SETTINGS[1]} is unset" in proc.stderr and f"{SETTINGS[3]} is unset" in proc.stderr
    assert f"{SETTINGS[0]} is unset" not in proc.stderr and f"{SETTINGS[2]} is unset" not in proc.stderr
    assert "SANDBOX_REPOSITORY_URL" not in proc.stderr


def test_provisioned_it_still_reaches_the_configuration_check(tmp_path):
    proc = _standalone(tmp_path, **dict(zip(SETTINGS, ("roles-accept", "swarm-qa@example-project.iam.gserviceaccount.com", "qa-admin@example-project.iam.gserviceaccount.com", "u-someone"))))
    assert proc.returncode == 1, proc.stderr
    assert "SWARM_ACCEPTANCE_SANDBOX_REPOSITORY_URL is not set" in proc.stderr, proc.stderr
    assert "SKIP" not in proc.stderr


# ---------------------------------------------------------------------------
# Where it runs
# ---------------------------------------------------------------------------


def test_accept_yml_runs_it_in_the_existing_smoke_job():
    jobs = _workflow("accept.yml")["jobs"]
    steps = [s for s in jobs["smoke"]["steps"] if TARGET in _code(s.get("run", ""))]
    assert len(steps) == 1
    step = steps[0]
    # Unquoted: the probe clones no fixture, so it is not one of the suites
    # test_acceptance_target.py makes wait for the sandbox sync.
    assert _code(step["run"]).strip() == f"./scripts/verify-remote.sh {TARGET}"
    assert "!cancelled()" in str(step.get("if"))
    for name in SETTINGS:
        # An account or a person's address is a masked secret
        # (docs/design/generic-repository.md, section 2.1); the rest are variables.
        context = "secrets" if name.endswith(("_SA", "_PERSON")) else "vars"
        assert step["env"][name] == f"${{{{ {context}.{name.removeprefix('SWARM_ACCEPTANCE_')} }}}}"
    # Not a new matrix group, and not the tenant-isolation job.
    for job_id, job in jobs.items():
        if job_id != "smoke":
            assert TARGET not in json.dumps(job), job_id


def _fake_gcloud(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "gcloud"
    fake.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >>"${FAKE_DIR}/gcloud.calls"\necho ok\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def test_verify_remote_carries_the_probes_settings_into_the_execution(tmp_path):
    """The swarm-verify job's environment is its Terraform template's; these
    four come from the repository's variables, through the execution's
    override, and only when set."""
    env = {"PATH": f"{_fake_gcloud(tmp_path)}{os.pathsep}{os.environ['PATH']}", "HOME": str(tmp_path),
           "NO_COLOR": "1", "FAKE_DIR": str(tmp_path), "TMPDIR": str(tmp_path),
           "SWARM_ENV_FILE": str(tmp_path / "no.env"), "PROJECT_ID": "swarm-test-project",
           SETTINGS[0]: "roles-accept", SETTINGS[1]: "swarm-qa@example-project.iam.gserviceaccount.com",
           **placeholder_deployment.child_env("swarm-test-project")}
    proc = subprocess.run(["bash", str(VERIFY_REMOTE), TARGET], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    call = (tmp_path / "gcloud.calls").read_text()
    assert f"--args scripts/{TARGET}.sh" in call
    assert f"{SETTINGS[0]}=roles-accept" in call
    assert f"{SETTINGS[1]}=swarm-qa@example-project.iam.gserviceaccount.com" in call
    assert SETTINGS[2] not in call and SETTINGS[3] not in call


#: A curl stand-in for the swarm-verify job: the metadata server, the IAM
#: Credentials API and swarm-api. It logs its argv, and answers the API only
#: for the ID token generateIdToken handed out.
FAKE_CURL = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >>"${FAKE_DIR}/curl.argv"
header="" data="" url=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -H) [[ "$2" == @* ]] && header="${2#@}"; shift 2 ;;
    --data-binary) data="$2"; shift 2 ;;
    -m|-X|-w) shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
case "${url}" in
  http://metadata.google.internal/*/token) printf '{"access_token":"%s"}' "${FAKE_ACCESS}" ;;
  https://iamcredentials.googleapis.com/*)
    printf '%s %s\n' "${url}" "${data}" >>"${FAKE_DIR}/mint"
    grep -qx "Authorization: Bearer ${FAKE_ACCESS}" "${header}" || exit 22
    printf '{"token":"%s"}' "${FAKE_ID}" ;;
  https://swarm-api.example.a.run.app/*)
    grep -qx "Authorization: Bearer ${FAKE_ID}" "${header}" || { printf '{}\n401'; exit 0; }
    printf '{"me":{"email":"viewer"}}\n200' ;;
  *) exit 7 ;;
esac
"""


def test_acc_api_as_mints_the_id_token_inside_cloud_run_without_gcloud(tmp_path):
    """accept.yml runs the probe in the swarm-verify job, whose image has no
    gcloud (images/swarm-verify/Dockerfile): there acc_api_as mints SA's ID
    token through generateIdToken with the metadata server's access token,
    and neither token reaches argv."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Every tool on PATH but gcloud, and curl replaced by the stand-in.
    for directory in os.environ["PATH"].split(os.pathsep):
        if not os.path.isdir(directory):
            continue
        for entry in os.scandir(directory):
            if entry.name not in ("gcloud", "curl") and not (bin_dir / entry.name).exists():
                (bin_dir / entry.name).symlink_to(entry.path)
    curl = bin_dir / "curl"
    curl.write_text(FAKE_CURL)
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
    access, id_token = secrets.token_hex(16), secrets.token_hex(16)
    sa = "swarm-qa@example-project.iam.gserviceaccount.com"
    script = (f'source "{REPO}/scripts/lib/common.sh"\nsource "{REPO}/scripts/acceptance/lib.sh"\n'
              f'acc_api_as {sa} roles-accept GET /workspace/roles "" "{tmp_path}/out"\n')
    env = {"PATH": str(bin_dir), "HOME": str(tmp_path), "NO_COLOR": "1", "TMPDIR": str(tmp_path),
           "SWARM_ENV_FILE": str(tmp_path / "no.env"), "CLOUD_RUN_JOB": "swarm-verify",
           "PROJECT_ID": "swarm-test-project",
           "API_URL": "https://swarm-api.example.a.run.app", "API_HOST": "front.example.com",
           "FAKE_DIR": str(tmp_path), "FAKE_ACCESS": access, "FAKE_ID": id_token,
           **placeholder_deployment.child_env("swarm-test-project")}
    proc = subprocess.run([str(bin_dir / "bash"), "-c", script], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    err = (tmp_path / "out.err").read_text()
    assert (tmp_path / "out.status").read_text() == "200", err
    mint = (tmp_path / "mint").read_text()
    assert f"/serviceAccounts/{sa}:generateIdToken" in mint
    assert '"audience":"https://swarm-api.example.a.run.app"' in mint
    assert '"includeEmail":true' in mint
    argv = (tmp_path / "curl.argv").read_text()
    assert access not in argv and id_token not in argv
    assert access not in err + proc.stdout + proc.stderr and id_token not in err + proc.stdout + proc.stderr
    assert not list(tmp_path.glob("swarm-auth.*")), "a header file outlived its request"


def test_the_group_runs_on_its_own():
    """verify-remote.sh executes scripts/<target>.sh, so the group file is
    also its own entry point."""
    mode = GROUP.stat().st_mode
    assert mode & stat.S_IXUSR, "scripts/acceptance/groups/roles.sh is not executable"
    head = GROUP.read_text().splitlines()
    assert head[0] == "#!/usr/bin/env bash"


# ---------------------------------------------------------------------------
# The design says it is built
# ---------------------------------------------------------------------------


def test_the_design_records_roles_accept_as_built():
    text = DESIGN.read_text()
    start = text.index("**ROLES-ACCEPT (")
    entry = text[start:text.index("\n\n", start)]
    assert "built on 2026-10-28" in entry
    assert "`scripts/acceptance/groups/roles.sh`" in entry
    assert "`.github/workflows/accept.yml`" in entry
    assert "No acceptance probe asserts" not in entry
    left = text[text.index("**What is left, in order.**"):]
    assert not left.startswith("**What is left, in order.** ROLES-ACCEPT;")
