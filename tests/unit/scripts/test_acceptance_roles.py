"""The acceptance probe `roles` judges by the API's own codes (ROLES-ACCEPT, #1518).

docs/design/workspace-roles.md §6 row 6: a non-admin identity narrowed to
`viewer` in a test workspace is 403 `role_member_required` on `POST
/v1/tasks` and 200 on `GET /v1/tasks`; the narrowing and its revocation are in
`GET /v1/workspace/roles` `audit`; a platform admin's reach into the
workspace is recorded `platform_admin_acted`, and another identity's `u-`
workspace is 403 `tenant_not_member` to it.

scripts/acceptance/groups/roles.sh's `roles_judge` is run here, sourced alone,
over responses built from swarm_api's own constants, so a renamed code or
audit action fails here rather than in a live run that then reads every
refusal as a regression. The run against a deployment is accept.yml's.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
GROUP = ROOT / "scripts" / "acceptance" / "groups" / "roles.sh"
ACCEPT = ROOT / ".github" / "workflows" / "accept.yml"
DESIGN = ROOT / "docs" / "design" / "workspace-roles.md"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("jq") is None,
                                reason="bash and jq are required")

ADMIN = "roles-admin@example-project.iam.gserviceaccount.com"
SINCE = "2026-10-28T10:00:00"


def judge(tmp_path: Path, check: str, status: int, body, since: str = SINCE, admin: str = ADMIN) -> tuple[str, str]:
    path = tmp_path / "body.json"
    path.write_text(body if isinstance(body, str) else json.dumps(body))
    proc = subprocess.run(
        ["bash", "-c", 'set -euo pipefail; source "$1"; shift; roles_judge "$@"', "judge", str(GROUP),
         check, str(status), str(path), since, admin],
        capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    verdict, _, reason = proc.stdout.strip().partition("\t")
    return verdict, reason


def _codes():
    from swarm_api.auth import TenantNotMember
    from swarm_api.errors import RoleMemberRequired, RoleUnchanged

    return RoleMemberRequired.code, TenantNotMember.code, RoleUnchanged.code


def _row(action: str, at: str, by: str = ADMIN, **detail) -> dict:
    return {"action": action, "by": by, "at": at, "detail": detail}


def test_a_viewers_submit_passes_only_as_role_member_required(tmp_path) -> None:
    member_required, not_member, _ = _codes()
    assert judge(tmp_path, "submit", 403, {"code": member_required, "message": "m"})[0] == "PASS"
    assert judge(tmp_path, "submit", 403, {"code": not_member, "message": "m"})[0] == "FAIL"
    verdict, reason = judge(tmp_path, "submit", 201, {"id": "task_1"})
    assert verdict == "FAIL" and "accepted" in reason
    assert judge(tmp_path, "submit", 503, "not json")[0] == "FAIL"


def test_a_viewers_read_passes_only_on_200(tmp_path) -> None:
    member_required, _, _ = _codes()
    assert judge(tmp_path, "read", 200, {"tasks": []})[0] == "PASS"
    assert judge(tmp_path, "read", 403, {"code": member_required})[0] == "FAIL"


def test_another_identitys_personal_workspace_passes_only_as_tenant_not_member(tmp_path) -> None:
    member_required, not_member, _ = _codes()
    assert judge(tmp_path, "personal", 403, {"code": not_member, "message": "m"})[0] == "PASS"
    assert judge(tmp_path, "personal", 200, {"tasks": []})[0] == "FAIL"
    assert judge(tmp_path, "personal", 403, {"code": member_required})[0] == "FAIL"


def test_the_narrowing_and_its_revocation_are_judged_by_the_role_they_set(tmp_path) -> None:
    _, _, unchanged = _codes()
    assert judge(tmp_path, "narrow", 200, {"default_role": "viewer", "previous_default": "member"})[0] == "PASS"
    assert judge(tmp_path, "narrow", 200, {"default_role": "member"})[0] == "FAIL"
    assert judge(tmp_path, "narrow", 409, {"code": unchanged})[0] == "FAIL"
    assert judge(tmp_path, "revoke", 200, {"default_role": "member", "previous_default": "viewer"})[0] == "PASS"
    assert judge(tmp_path, "revoke", 200, {"default_role": "viewer"})[0] == "FAIL"


def test_the_audit_must_hold_the_narrowing_and_its_revocation_since_the_probe_began(tmp_path) -> None:
    from swarm_api import roles

    narrowed = _row(roles.AUDIT_DEFAULT_SET, "2026-10-28T10:01:00.123456+00:00", **{"from": "member", "to": "viewer"})
    revoked = _row(roles.AUDIT_DEFAULT_SET, "2026-10-28T10:02:00.123456+00:00", **{"from": "viewer", "to": "member"})
    old = _row(roles.AUDIT_DEFAULT_SET, "2026-10-27T10:02:00+00:00", **{"from": "viewer", "to": "member"})
    # The API lists newest first.
    assert judge(tmp_path, "audit", 200, {"audit": [revoked, narrowed]})[0] == "PASS"
    assert judge(tmp_path, "audit", 200, {"audit": [narrowed]})[0] == "FAIL"
    assert judge(tmp_path, "audit", 200, {"audit": [old, narrowed]})[0] == "FAIL"
    assert judge(tmp_path, "audit", 200, {"audit": [revoked]})[0] == "FAIL"
    assert judge(tmp_path, "audit", 503, {"code": "role_unavailable"})[0] == "FAIL"


def test_the_reach_must_be_recorded_as_platform_admin_acted_by_the_admin(tmp_path) -> None:
    from swarm_api import roles

    acted = _row(roles.AUDIT_PLATFORM_ADMIN_ACTED, "2026-10-28T10:01:00+00:00",
                 method="PUT", route="/v1/workspace/roles/default", via=roles.VIA_PLATFORM_ADMIN)
    viewed = _row(roles.AUDIT_PLATFORM_ADMIN_VIEWED, "2026-10-28T10:01:00+00:00")
    someone = dict(acted, by="someone@example.com")
    stale = dict(acted, at="2026-10-28T09:01:00+00:00")
    assert judge(tmp_path, "acted", 200, {"audit": [acted]})[0] == "PASS"
    for rows in ([viewed], [someone], [stale], []):
        assert judge(tmp_path, "acted", 200, {"audit": rows})[0] == "FAIL", rows


def test_every_code_the_probe_names_is_the_apis() -> None:
    from swarm_api import roles

    member_required, not_member, _ = _codes()
    text = GROUP.read_text()
    for name, value in (("ROLES_CODE_MEMBER_REQUIRED", member_required),
                        ("ROLES_CODE_NOT_MEMBER", not_member),
                        ("ROLES_AUDIT_DEFAULT_SET", roles.AUDIT_DEFAULT_SET),
                        ("ROLES_AUDIT_ACTED", roles.AUDIT_PLATFORM_ADMIN_ACTED)):
        assert f'{name}="{value}"' in text, name
    assert roles.VIEWER in roles.DEFAULT_ROLES and roles.MEMBER in roles.DEFAULT_ROLES


def test_the_probe_lists_six_checks(tmp_path) -> None:
    proc = subprocess.run(["bash", "-c", 'source "$1"; roles_checks', "x", str(GROUP)],
                          capture_output=True, text=True, timeout=30)
    lines = proc.stdout.splitlines()
    assert len(lines) == 6 and all(l.startswith("roles: ") for l in lines), lines


@pytest.mark.parametrize("tenant,personal,admin,why", [
    ("", "u-someone", ADMIN, "SWARM_ACCEPTANCE_ROLES_TENANT"),
    ("u-me", "u-someone", ADMIN, "group workspace"),
    ("smoke", "u-someone", ADMIN, "the acceptance tenant"),
    ("roles-probe", "", ADMIN, "SWARM_ACCEPTANCE_ROLES_OTHER_PERSONAL"),
    ("roles-probe", "eng", ADMIN, "u-"),
    ("roles-probe", "u-someone", "", "SWARM_ACCEPTANCE_ROLES_ADMIN_SA"),
    ("roles-probe", "u-someone", "not-an-email", "SWARM_ACCEPTANCE_ROLES_ADMIN_SA"),
])
def test_an_unconfigured_probe_says_what_is_missing(tenant, personal, admin, why) -> None:
    script = ('source "$1"; ACC_TENANT=smoke; ACC_ROLES_TENANT="$2"; ACC_ROLES_OTHER_PERSONAL="$3"; '
              'ACC_ROLES_ADMIN_SA="$4"; api_is_front_door() { return 1; }; roles_config_problem')
    proc = subprocess.run(["bash", "-c", script, "x", str(GROUP), tenant, personal, admin],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0 and why in proc.stdout, proc.stdout + proc.stderr
    ok = subprocess.run(["bash", "-c", script, "x", str(GROUP), "roles-probe", "u-someone", ADMIN],
                        capture_output=True, text=True, timeout=30)
    assert ok.returncode == 0 and ok.stdout == "", ok.stdout + ok.stderr


def test_accept_runs_the_probe_report_only_in_the_smoke_job() -> None:
    # Not a matrix group: run.sh's groups are what the waves are held to, and
    # the probe is check-only. The file runs itself (its entry at the end).
    job = yaml.safe_load(ACCEPT.read_text())["jobs"]["smoke"]
    run = "./scripts/verify-remote.sh acceptance/groups/roles"
    (step,) = [s for s in job["steps"] if str(s.get("run", "")).strip() == run]
    assert step["continue-on-error"] == "${{ vars.ROLES_PROBE_REQUIRED != 'true' }}", step
    assert step["if"] == "${{ !cancelled() }}", step
    for name in ("SWARM_ACCEPTANCE_ROLES_TENANT", "SWARM_ACCEPTANCE_ROLES_ADMIN_SA",
                 "SWARM_ACCEPTANCE_ROLES_OTHER_PERSONAL"):
        assert step["env"][name] == "${{ vars.%s }}" % name, name


def test_the_design_says_roles_accept_is_built_and_where() -> None:
    text = DESIGN.read_text()
    assert "**ROLES-ACCEPT (not built" not in text
    assert "**ROLES-ACCEPT (built" in text
    assert "scripts/acceptance/groups/roles.sh" in text
    assert "`role_person_required`" in text


# ---------------------------------------------------------------------------
# run_roles end to end, against a stubbed API
# ---------------------------------------------------------------------------

_HARNESS = r'''
set -euo pipefail
source "$GROUP"
step() { :; }
t_info() { :; }
redact() { cat; }
sleep() { :; }
acc_check() { ACC_CHECK="$1"; }
acc_pass() { printf 'PASS %s: %s\n' "${ACC_CHECK}" "$1"; }
acc_fail() { printf 'FAIL %s: %s\n' "${ACC_CHECK}" "$1"; }
acc_extra() { echo '{"priority": 10}'; }
acc_record_task() { echo "RECORDED ${SWARM_API_TENANT} $1" >>"$WORK/calls"; }
acc_mint_id_token() { [[ "$FAKE_MINT" == ok ]] || return 1; echo minted >"$2"; }
api_is_front_door() { return 1; }
ACC_TENANT=smoke ACC_RUN_ID=run ACC_WORK="$WORK"
ACC_ROLES_TENANT=roles-probe ACC_ROLES_OTHER_PERSONAL=u-someone ACC_ROLES_ADMIN_SA="$ADMIN"
acc_api_as() {
  local who=self key
  [[ -z "$1" ]] || who=admin
  key="$who $2 $3 ${4%%\?*}"
  echo "$key" >>"$WORK/calls"
  case "$key" in
    "admin roles-probe PUT /workspace/roles/default")
      if [[ "$6" == *'"viewer"'* ]]; then ACC_API_STATUS=200; echo '{"default_role":"viewer"}' >"$5"
      elif [[ -f "$WORK/narrowed" ]]; then ACC_API_STATUS=200; echo '{"default_role":"member"}' >"$5"
      else ACC_API_STATUS=409; echo '{"code":"role_unchanged"}' >"$5"; fi
      [[ "$6" != *'"viewer"'* ]] || touch "$WORK/narrowed" ;;
    "self roles-probe GET /workspace/roles") ACC_API_STATUS=200; cat "$WORK/roles.json" >"$5" ;;
    "self roles-probe POST /tasks") ACC_API_STATUS="$SUBMIT_STATUS"; cat "$WORK/submit.json" >"$5" ;;
    "self roles-probe GET /tasks") ACC_API_STATUS=200; echo '{"tasks":[]}' >"$5" ;;
    "admin u-someone GET /tasks") ACC_API_STATUS=403; echo "{\"code\":\"$NOT_MEMBER\"}" >"$5" ;;
    *) ACC_API_STATUS=599; echo '{}' >"$5" ;;
  esac
}
run_roles
'''


def _run_roles(tmp_path: Path, *, submit_status: int, submit_body: dict, mint: str = "ok"):
    from swarm_api import roles

    member_required, not_member, _ = _codes()
    now = "2099-01-01T00:00:00+00:00"
    audit = [
        _row(roles.AUDIT_DEFAULT_SET, now, **{"from": "viewer", "to": "member"}),
        _row(roles.AUDIT_PLATFORM_ADMIN_ACTED, now, method="PUT"),
        _row(roles.AUDIT_DEFAULT_SET, now, **{"from": "member", "to": "viewer"}),
    ]
    (tmp_path / "roles.json").write_text(json.dumps({"me": {"role": "viewer"}, "audit": audit}))
    (tmp_path / "submit.json").write_text(json.dumps(submit_body))
    env = {"GROUP": str(GROUP), "WORK": str(tmp_path), "ADMIN": ADMIN, "FAKE_MINT": mint,
           "SUBMIT_STATUS": str(submit_status), "NOT_MEMBER": not_member, "PATH": "/usr/bin:/bin:/usr/local/bin"}
    proc = subprocess.run(["bash", "-c", _HARNESS], env=env, capture_output=True, text=True, timeout=60)
    calls = (tmp_path / "calls").read_text().splitlines() if (tmp_path / "calls").exists() else []
    return proc, calls


def test_run_roles_passes_all_six_against_an_api_that_gates(tmp_path) -> None:
    member_required, _, _ = _codes()
    proc, calls = _run_roles(tmp_path, submit_status=403, submit_body={"code": member_required})
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert len(lines) == 6 and all(l.startswith("PASS roles: ") for l in lines), proc.stdout
    # Put back first, narrowed, and put back again after the checks.
    puts = [c for c in calls if c.endswith("PUT /workspace/roles/default")]
    assert len(puts) == 3, calls


def test_run_roles_fails_an_accepted_submit_cancels_it_and_still_revokes(tmp_path) -> None:
    proc, calls = _run_roles(tmp_path, submit_status=201, submit_body={"id": "task_9"})
    assert proc.returncode == 0, proc.stderr
    assert "FAIL roles: submit: a viewer's POST /v1/tasks is 403 role_member_required: HTTP 201" in proc.stdout
    assert "RECORDED roles-probe task_9" in calls, calls
    assert sum(c.endswith("PUT /workspace/roles/default") for c in calls) == 3, calls


def test_run_roles_without_an_admin_token_fails_every_check_and_writes_nothing(tmp_path) -> None:
    member_required, _, _ = _codes()
    proc, calls = _run_roles(tmp_path, submit_status=403, submit_body={"code": member_required}, mint="no")
    lines = proc.stdout.splitlines()
    assert len(lines) == 6 and all(l.startswith("FAIL roles: ") and "not measured" in l for l in lines), proc.stdout
    assert calls == [], calls


def test_the_group_file_runs_itself_and_lists_its_six_checks() -> None:
    assert os.access(GROUP, os.X_OK), "verify-remote.sh runs scripts/acceptance/groups/roles.sh directly"
    proc = subprocess.run(["bash", str(GROUP), "--list"], capture_output=True, text=True, timeout=30,
                          env={**os.environ, "SWARM_ENV_FILE": "/nonexistent"})
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert lines[0] == "roles" and len(lines) == 7, proc.stdout
