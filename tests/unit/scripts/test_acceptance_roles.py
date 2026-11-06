"""ROLES-ACCEPT: the workspace roles acceptance probe
(docs/design/workspace-roles.md §6 row 6 and §8; #1518).

scripts/acceptance/groups/roles.sh measures, against dev, what the unit tests
in tests/unit/control_plane prove offline: a viewer is refused a submission
with `role_member_required` and may still read; the viewer grant and its
revocation are in the workspace's audit; a platform admin reaches a group
workspace it is not in, and its write there is recorded
`platform_admin_acted`; another identity's `u-` workspace stays
`tenant_not_member`.

Held here, with no network and no credentials:

* the probe's judge, run by bash, PASSes exactly the API's own codes and
  actions, read from swarm_api rather than restated, and FAILs anything else;
* the probe SKIPs, naming what is missing, until the owner has provisioned
  its identities, and never runs against the shared smoke tenant;
* accept.yml runs it in the existing smoke job (BGD, 2026-10-28), through
  verify-remote.sh, which carries its four settings into the execution;
* the design records it as built.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import subprocess
from datetime import datetime, timezone
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
#: The probe's settings: the test workspace, the viewer and platform-admin
#: identities, and another identity's personal workspace. None is a secret.
SETTINGS = (
    "SWARM_ACCEPTANCE_ROLES_TENANT",
    "SWARM_ACCEPTANCE_ROLES_VIEWER_SA",
    "SWARM_ACCEPTANCE_ROLES_ADMIN_SA",
    "SWARM_ACCEPTANCE_ROLES_OTHER_WORKSPACE",
)
CHECKS = ("viewer-submit", "viewer-read", "audit", "reach", "personal")
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
    from swarm_api.errors import RoleMemberRequired

    return SimpleNamespace(member_required=RoleMemberRequired.code, not_member=TenantNotMember.code,
                           default_set=roles.AUDIT_DEFAULT_SET, acted=roles.AUDIT_PLATFORM_ADMIN_ACTED)


def _refusal(code: str) -> dict:
    return {"code": code, "message": "refused", "detail": {}}


def _audit(*rows: dict) -> dict:
    return {"tenant_id": "roles-accept", "kind": "group", "audit": list(rows)}


def _default_set(to: str, frm: str, reason: str = REASON) -> dict:
    return {"action": _api().default_set, "by": "qa-admin@example.iam.gserviceaccount.com",
            "at": "2026-10-28T19:00:01.123456+00:00",
            "detail": {"from": frm, "to": to, "via": "platform_admin", "reason": reason}}


def _acted(at: str = "2026-10-28T19:00:01.123456+00:00", route: str = "/v1/workspace/roles/default") -> dict:
    return {"action": _api().acted, "by": "qa-admin@example.iam.gserviceaccount.com",
            "at": at, "detail": {"method": "PUT", "route": route, "path_params": {},
                                 "via": "platform_admin", "reach": "non_member"}}


# ---------------------------------------------------------------------------
# The judge, held to the API's codes
# ---------------------------------------------------------------------------


def test_the_checks_are_the_five_the_design_names():
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


def test_reach_passes_on_the_admins_write_and_its_platform_admin_acted_row():
    started = str(int(datetime(2026, 10, 28, 18, 43, 20, tzinfo=timezone.utc).timestamp()))
    assert _judge("reach", 200, _audit(_acted()), started)[0] == "PASS"
    # MUTATION: accept a write with no row, or an older row.
    assert _judge("reach", 200, _audit(_default_set("viewer", "member")), started)[0] == "FAIL"
    assert _judge("reach", 200, _audit(_acted(at="2026-10-27T10:00:00+00:00")), started)[0] == "FAIL"
    assert _judge("reach", 200, _audit(_acted(at="2026-10-28T19:00:01Z")), started)[0] == "PASS"
    # The reach refused: the admin's write never happened.
    assert _judge("reach", 403, _audit(_acted()), started)[0] == "FAIL"


def test_another_identitys_personal_workspace_passes_only_on_tenant_not_member():
    assert _judge("personal", 403, _refusal(_api().not_member))[0] == "PASS"
    assert _judge("personal", 200, {"tenant_id": "u-someone"})[0] == "FAIL"
    assert _judge("personal", 403, _refusal(_api().member_required))[0] == "FAIL"


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
        "roles-accept", "swarm-qa@p.iam.gserviceaccount.com",
        "qa-admin@p.iam.gserviceaccount.com", "u-someone"))}
    assert _bash('roles_settings_problem && echo OK', **full).stdout.strip() == "OK"
    for tenant in ("smoke", "u-someone", "Eng"):
        proc = _bash("roles_settings_problem || echo REFUSED", **{**full, SETTINGS[0]: tenant})
        assert "REFUSED" in proc.stdout, tenant
    proc = _bash("roles_settings_problem || echo REFUSED", **{**full, SETTINGS[3]: "eng"})
    assert "REFUSED" in proc.stdout


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
        assert step["env"][name] == f"${{{{ vars.{name.removeprefix('SWARM_ACCEPTANCE_')} }}}}"
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
           "SWARM_ENV_FILE": str(tmp_path / "no.env"),
           SETTINGS[0]: "roles-accept", SETTINGS[1]: "swarm-qa@p.iam.gserviceaccount.com"}
    proc = subprocess.run(["bash", str(VERIFY_REMOTE), TARGET], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    call = (tmp_path / "gcloud.calls").read_text()
    assert f"--args scripts/{TARGET}.sh" in call
    assert f"{SETTINGS[0]}=roles-accept" in call
    assert f"{SETTINGS[1]}=swarm-qa@p.iam.gserviceaccount.com" in call
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
    sa = "swarm-qa@p.iam.gserviceaccount.com"
    script = (f'source "{REPO}/scripts/lib/common.sh"\nsource "{REPO}/scripts/acceptance/lib.sh"\n'
              f'acc_api_as {sa} roles-accept GET /workspace/roles "" "{tmp_path}/out"\n')
    env = {"PATH": str(bin_dir), "HOME": str(tmp_path), "NO_COLOR": "1", "TMPDIR": str(tmp_path),
           "SWARM_ENV_FILE": str(tmp_path / "no.env"), "CLOUD_RUN_JOB": "swarm-verify",
           "API_URL": "https://swarm-api.example.a.run.app", "API_HOST": "front.example.com",
           "FAKE_DIR": str(tmp_path), "FAKE_ACCESS": access, "FAKE_ID": id_token}
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
