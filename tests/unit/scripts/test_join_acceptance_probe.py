"""JOIN-DOCS-ACCEPT: the join-request acceptance probe
(docs/design/join-requests.md §6 row 8 and §8).

scripts/acceptance/groups/join.sh measures, against dev, the flow the unit
tests in tests/unit/control_plane prove offline: an admin turns a workspace's
join link on and reads its code once; a second identity, a person outside the
workspace's group, asks with the code in a body; the admin approves; the
person's next request selects the workspace (`via: platform_member`); the
admin, a scoped probe admin, is refused the membership's removal
(`probe_admin_not_permitted`) and the person is still a member; the person
then leaves through the front door (`POST /v1/workspace/leave`) and is no
longer a member; the link is turned off again. A run that finds the person
still a member has them leave first and runs the full flow, approval
included. With `platform_memberships` off, approval answers
`admitted: false` with the group command and admits nobody. No Google group
is ever written: the probe reads the group only through the API's own fresh
per-group check, and calls no Cloud Identity API at all.

Held here, with no network and no credentials, by running the probe against
a fake `curl` (the metadata server, the IAM Credentials API, Secret Manager,
Google's token endpoint and swarm-api) and a fake `gcloud`, both on PATH:

* the on path PASSes every check, and the off path PASSes the
  `admitted: false` check and admits nobody;
* an approval that does not admit, a removal the probe admin is allowed, a
  refusal that removes the person anyway, or a leave that leaves the person
  a member, FAILs;
* a person an earlier run left a member leaves first, and every check,
  the approval included, is measured and passes; a leave refused there fails
  every check;
* neither the join code, the person's refresh token nor any minted token
  reaches argv or the probe's output;
* the person's requests go through the front door (SWARM_ACCEPTANCE_CONSOLE_URL,
  where IAP takes the Desktop client's ID token, minted with `openid email`
  only), and the admin's to the service's own address; pointed at the
  service's address the person is refused;
* the probe SKIPs, naming what is missing, until its settings and the front
  door's URL are set, and never runs in the shared smoke tenant;
* verify-remote.sh carries its settings into the execution.
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

import pytest

from .test_release_reuses_ci_images import _code, _workflow

REPO = Path(__file__).resolve().parents[3]

_PLACEHOLDERS_SPEC = importlib.util.spec_from_file_location("placeholders", REPO / "tests" / "placeholders.py")
_PLACEHOLDERS = importlib.util.module_from_spec(_PLACEHOLDERS_SPEC)
_PLACEHOLDERS_SPEC.loader.exec_module(_PLACEHOLDERS)
#: REGION, ZONE and a never-touch list served for the project a child names
#: (common.sh has no default for either); see tests/unit/scripts/placeholder_deployment.py.
placeholder_deployment = _PLACEHOLDERS.deployment()
GROUP = REPO / "scripts" / "acceptance" / "groups" / "join.sh"
VERIFY_REMOTE = REPO / "scripts" / "verify-remote.sh"
TARGET = "acceptance/groups/join"
#: The probe's settings: the test workspace, the platform-admin identity, the
#: person who asks, and the Secret Manager secret holding that person's
#: sign-in. None is itself a secret.
SETTINGS = (
    "SWARM_ACCEPTANCE_JOIN_TENANT",
    "SWARM_ACCEPTANCE_JOIN_ADMIN_SA",
    "SWARM_ACCEPTANCE_JOIN_PERSON",
    "SWARM_ACCEPTANCE_JOIN_PERSON_SECRET",
)
CHECKS = ("link-on", "ask", "approve", "selects", "no-group-write", "removal", "leave",
          "link-off")
TENANT = "join-accept"
ADMIN = "qa-admin@example-project.iam.gserviceaccount.com"
PERSON = "join-tester@example.com"
PERSON_SECRET = "swarm-acceptance-join-person"
API_URL = "https://swarm-api.example.a.run.app"
#: The front door, IAP in front of swarm-api: the only address that takes the
#: person's ID token (its audience is the Desktop OAuth client). The swarm-verify
#: job's own setting, not the probe's, so not in SETTINGS (accept.yml does not
#: pass it).
CONSOLE = "SWARM_ACCEPTANCE_CONSOLE_URL"
FRONT_URL = "https://swarm.example.com"
#: The deployment's private sandbox: configuration with no default in
#: scripts/acceptance/config.sh, so the probe gets the placeholder repository's.
SANDBOX_URL = f"https://github.com/{_PLACEHOLDERS.GITHUB_REPOSITORY}.git"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None or shutil.which("base64") is None,
    reason="the probe needs bash, jq and base64",
)


#: A stand-in for every endpoint the probe reaches from the swarm-verify job.
#: It logs its argv, tells the caller apart by the bearer token in the header
#: file, and keeps the fake API's state as files in FAKE_DIR. Like the real
#: deployment, the service's own address (FAKE_API) takes only the admin's
#: token, and the front door (FAKE_FRONT) only the person's; hosts.log says
#: which address each caller reached. FAKE_SWITCH is
#: platform_memberships; FAKE_BROKEN breaks one behaviour so a test can show
#: the probe notices. The admin is a scoped probe admin, so its removal is
#: refused probe_admin_not_permitted, as swarm-api's roles.PROBE_ADMIN_ROUTES has it.
#: The person leaves by their own POST /v1/workspace/leave, which, like
#: swarm-api's, answers only for a person who selects the workspace (else 403
#: tenant_not_member, as the sign-in refuses it before the route).
FAKE_CURL = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >>"${FAKE_DIR}/curl.argv"
headers=() data="" url="" method="GET" tenant=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -H)
      if [[ "$2" == @* ]]; then headers+=("${2#@}")
      elif [[ "$2" == X-Swarm-Tenant:* ]]; then tenant="${2#X-Swarm-Tenant: }"
      fi
      shift 2 ;;
    --data-binary) data="$2"; [[ "${data}" == @* ]] && data="$(cat "${data#@}")"; shift 2 ;;
    -X) method="$2"; shift 2 ;;
    -m|-w|-o) shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
bearer=""
for h in ${headers[@]+"${headers[@]}"}; do
  line="$(grep -m1 '^Authorization: Bearer ' "${h}" || true)"
  [[ -z "${line}" ]] || bearer="${line#Authorization: Bearer }"
done
answer() { printf '%s\n%s' "$2" "$1"; exit 0; }
refuse() { answer "$1" "{\"code\":\"$2\",\"message\":\"refused\",\"detail\":{}}"; }
case "${url}" in
  http://metadata.google.internal/*/token) printf '{"access_token":"%s"}' "${FAKE_ACCESS}"; exit 0 ;;
  https://iamcredentials.googleapis.com/*)
    [[ "${bearer}" == "${FAKE_ACCESS}" ]] || exit 22
    printf '{"token":"%s"}' "${FAKE_ADMIN_ID}"; exit 0 ;;
  https://secretmanager.googleapis.com/v1/projects/*/secrets/*/versions/latest:access)
    [[ "${bearer}" == "${FAKE_ACCESS}" ]] || exit 22
    printf '%s\n' "${url}" >>"${FAKE_DIR}/secrets.read"
    printf '{"payload":{"data":"%s"}}' "${FAKE_CREDENTIAL_B64}"; exit 0 ;;
  https://oauth2.googleapis.com/token)
    [[ "${data}" == *"refresh_token=${FAKE_REFRESH}"* && "${data}" == *"grant_type=refresh_token"* ]] || exit 22
    tr '&' '
' <<<"${data}" | sed -n 's/^scope=//p' >>"${FAKE_DIR}/scopes"
    printf '{"access_token":"x","id_token":"%s","token_type":"Bearer"}' "${FAKE_PERSON_ID}"; exit 0 ;;
  "${FAKE_API}"/*) host=api; path="${url#"${FAKE_API}"}" ;;
  "${FAKE_FRONT}"/*) host=front; path="${url#"${FAKE_FRONT}"}" ;;
  *) printf '%s\n' "${url}" >>"${FAKE_DIR}/elsewhere"; exit 7 ;;
esac
who=""
[[ "${bearer}" == "${FAKE_ADMIN_ID}" ]] && who=admin
[[ "${bearer}" == "${FAKE_PERSON_ID}" ]] && who=person
printf '%s %s\n' "${who:-nobody}" "${host}" >>"${FAKE_DIR}/hosts.log"
[[ "${who}:${host}" == "admin:api" || "${who}:${host}" == "person:front" ]] || answer 401 '{}'
printf '%s %s %s %s\n' "${who}" "${method}" "${path}" "${tenant}" >>"${FAKE_DIR}/api.log"
on=false; [[ "${FAKE_SWITCH}" == "on" ]] && on=true
member=0; [[ -f "${FAKE_DIR}/member" ]] && member=1
case "${who} ${method} ${path}" in
  "admin PUT /v1/workspace/join-link")
    if [[ "$(jq -r .on <<<"${data}")" == "true" ]]; then
      touch "${FAKE_DIR}/link_on"
      answer 200 "{\"on\":true,\"set_by\":\"${FAKE_ADMIN}\",\"set_at\":\"2026-11-05T10:00:00+00:00\",\"link\":\"https://console.example.com/join#${FAKE_CODE}\"}"
    fi
    rm -f "${FAKE_DIR}/link_on"
    [[ "${FAKE_BROKEN:-}" == "link-stays-on" ]] && touch "${FAKE_DIR}/link_on"
    answer 200 '{"on":false,"set_by":"a","set_at":"2026-11-05T10:00:00+00:00"}' ;;
  "admin GET /v1/workspace/join-link")
    if [[ -f "${FAKE_DIR}/link_on" ]]; then answer 200 '{"on":true}'; fi
    answer 200 '{"on":false}' ;;
  "admin GET /v1/workspace/members")
    rows='[]'
    [[ "${member}" == 1 ]] && rows="[{\"email\":\"${FAKE_PERSON}\",\"by\":\"a\",\"at\":\"t\",\"via\":\"join_request\",\"group_changed\":false,\"still_in_group\":${FAKE_IN_GROUP:-false}}]"
    answer 200 "{\"platform_memberships\":${on},\"members\":${rows}}" ;;
  "admin DELETE /v1/workspace/members/person")
    printf '%s\n' "$(jq -r .email <<<"${data}")" >>"${FAKE_DIR}/removals"
    case "${FAKE_BROKEN:-}" in
      removal-allowed)
        [[ "$(jq -r .email <<<"${data}")" == "${FAKE_PERSON}" && "${member}" == 1 ]] || refuse 404 not_found
        rm -f "${FAKE_DIR}/member"
        answer 200 "{\"email\":\"${FAKE_PERSON}\",\"state\":\"removed\",\"still_in_group\":false}" ;;
      refusal-removes) rm -f "${FAKE_DIR}/member" ;;
    esac
    refuse 403 probe_admin_not_permitted ;;
  "admin POST /v1/admin/join-requests/jr-1/approve")
    [[ -f "${FAKE_DIR}/asked" ]] || refuse 404 join_request_unknown
    if [[ "${on}" == true ]]; then
      [[ "${FAKE_BROKEN:-}" == "no-admit" ]] || touch "${FAKE_DIR}/member"
      answer 200 "{\"ref\":\"jr-1\",\"state\":\"approved\",\"email\":\"${FAKE_PERSON}\",\"tenant_id\":\"${FAKE_TENANT}\",\"role\":\"member\",\"admitted\":true,\"group_change\":null}"
    fi
    answer 200 "{\"ref\":\"jr-1\",\"state\":\"approved\",\"email\":\"${FAKE_PERSON}\",\"tenant_id\":\"${FAKE_TENANT}\",\"role\":\"member\",\"admitted\":false,\"group_change\":{\"group\":\"eng@example.com\",\"command\":\"gcloud identity groups memberships add --group-email=eng@example.com --member-email=${FAKE_PERSON}\"}}" ;;
  "person GET /v1/join-requests/mine") answer 200 '{"requests":[]}' ;;
  "person POST /v1/workspace/leave")
    printf '%s\n' "${member}" >>"${FAKE_DIR}/leaves"
    [[ "${tenant}" == "${FAKE_TENANT}" && "${member}" == 1 ]] || refuse 403 tenant_not_member
    case "${FAKE_BROKEN:-}" in
      leave-refused) refuse 404 workspace_leave_not_member ;;
      leave-keeps) ;;
      *) rm -f "${FAKE_DIR}/member" ;;
    esac
    answer 200 "{\"email\":\"${FAKE_PERSON}\",\"state\":\"removed\",\"still_in_group\":false}" ;;
  "person POST /v1/join-requests")
    [[ -f "${FAKE_DIR}/link_on" && "$(jq -r .code <<<"${data}")" == "${FAKE_CODE}" ]] || refuse 404 join_link_unknown
    [[ "${member}" == 0 ]] || refuse 409 join_already_member
    touch "${FAKE_DIR}/asked"
    answer 202 '{"ref":"jr-1","state":"requested","display_name":"Join accept","created_at":"t"}' ;;
  "person GET /v1/tenants/me")
    [[ "${tenant}" == "${FAKE_TENANT}" && "${member}" == 1 ]] || refuse 403 tenant_not_member
    answer 200 "{\"tenant_id\":\"${FAKE_TENANT}\",\"kind\":\"group\"}" ;;
  "person GET /v1/tenants/mine")
    rows='[{"tenant_id":"u-join-tester","kind":"user","via":"personal","is_default":true}'
    [[ "${member}" == 1 ]] && rows+=",{\"tenant_id\":\"${FAKE_TENANT}\",\"kind\":\"group\",\"via\":\"platform_member\",\"is_default\":false}"
    answer 200 "${rows}]" ;;
esac
refuse 404 not_found
"""

FAKE_GCLOUD = '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >>"${FAKE_DIR}/gcloud.calls"\nexit 1\n'


def _tools(tmp_path: Path) -> Path:
    """Every tool on PATH, with curl and gcloud replaced by the stand-ins."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for directory in os.environ["PATH"].split(os.pathsep):
        if not os.path.isdir(directory):
            continue
        for entry in os.scandir(directory):
            if entry.name not in ("gcloud", "curl") and not (bin_dir / entry.name).exists():
                (bin_dir / entry.name).symlink_to(entry.path)
    for name, text in (("curl", FAKE_CURL), ("gcloud", FAKE_GCLOUD)):
        tool = bin_dir / name
        tool.write_text(text)
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def _probe(tmp_path: Path, switch: str, broken: str = "", member: bool = False, **extra: str):
    """Runs run_join as the swarm-verify job would, against the fakes. With
    `member`, the person starts as a platform member, as an earlier run with
    the switch on leaves them."""
    bin_dir = _tools(tmp_path)
    fake = tmp_path / "fake"
    fake.mkdir()
    if member:
        (fake / "member").touch()
    refresh, client_secret = secrets.token_urlsafe(24), secrets.token_urlsafe(12)
    credential = json.dumps({"type": "authorized_user", "client_id": "cli.apps.example.com",
                             "client_secret": client_secret, "refresh_token": refresh})
    tokens = {"FAKE_ACCESS": secrets.token_hex(16), "FAKE_ADMIN_ID": secrets.token_hex(16),
              "FAKE_PERSON_ID": secrets.token_hex(16), "FAKE_CODE": secrets.token_urlsafe(16),
              "FAKE_REFRESH": refresh}
    import base64

    env = {"PATH": str(bin_dir), "HOME": str(tmp_path), "NO_COLOR": "1", "TMPDIR": str(tmp_path),
           "SWARM_ENV_FILE": str(tmp_path / "no.env"), "CLOUD_RUN_JOB": "swarm-verify",
           "PROJECT_ID": "swarm-dev-test", "API_URL": API_URL, "API_HOST": "front.example.com",
           "FAKE_DIR": str(fake), "FAKE_API": API_URL, "FAKE_FRONT": FRONT_URL, "FAKE_SWITCH": switch,
           "FAKE_BROKEN": broken, "FAKE_TENANT": TENANT, "FAKE_PERSON": PERSON,
           "FAKE_ADMIN": ADMIN, "SWARM_ACCEPTANCE_JOIN_SETTLE": "0",
           "SWARM_ACCEPTANCE_SANDBOX_REPOSITORY_URL": SANDBOX_URL,
           "FAKE_CREDENTIAL_B64": base64.b64encode(credential.encode()).decode(),
           SETTINGS[0]: TENANT, SETTINGS[1]: ADMIN, SETTINGS[2]: PERSON, SETTINGS[3]: PERSON_SECRET,
           CONSOLE: FRONT_URL, **placeholder_deployment.child_env("swarm-dev-test"), **tokens, **extra}
    script = (f'source "{REPO}/scripts/lib/common.sh"\nSUITE_NAME=acceptance\n'
              f'source "{REPO}/scripts/lib/testlib.sh"\n'
              f'source "{REPO}/scripts/acceptance/lib.sh"\nsource "{GROUP}"\n'
              'acc_init\nrun_join\n_join_undo_on_exit\nacc_cleanup\nt_summary\n')
    proc = subprocess.run([str(bin_dir / "bash"), "-c", script], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=120)
    secret_values = (*tokens.values(), client_secret)
    return proc, fake, secret_values


def _verdicts(proc: subprocess.CompletedProcess) -> dict[str, str]:
    """check -> PASS / FAIL / SKIP, from the probe's own lines."""
    out: dict[str, str] = {}
    for line in proc.stderr.splitlines():
        text = line.strip()
        for verdict in ("PASS", "FAIL", "SKIP"):
            if text.startswith(verdict + " join: "):
                check = text[len(verdict) + 7:].split(":", 1)[0]
                out.setdefault(check, verdict)
                if verdict == "FAIL":
                    out[check] = "FAIL"
    return out


def _assert_nothing_leaked(proc, fake: Path, secret_values) -> None:
    argv = (fake / "curl.argv").read_text()
    for value in secret_values:
        assert value not in argv, "a credential or the join code reached curl's argv"
        assert value not in proc.stdout + proc.stderr, "a credential or the join code was printed"


def _assert_no_group_write(fake: Path) -> None:
    """Nothing but the fakes' own endpoints was called: no Cloud Identity API,
    and gcloud not at all."""
    assert not (fake / "elsewhere").exists(), (fake / "elsewhere").read_text()
    assert not (fake / "gcloud.calls").exists(), (fake / "gcloud.calls").read_text()
    assert "cloudidentity" not in (fake / "curl.argv").read_text()


# ---------------------------------------------------------------------------
# The flow, end to end against the fakes
# ---------------------------------------------------------------------------


def test_with_platform_memberships_on_every_check_passes(tmp_path):
    proc, fake, secret_values = _probe(tmp_path, "on")
    assert _verdicts(proc) == {c: "PASS" for c in CHECKS}, proc.stderr
    assert proc.returncode == 0, proc.stderr
    log = (fake / "api.log").read_text().splitlines()
    # The person asked with the code in a body; the admin approved; the
    # person's next request selected the workspace; the admin's removal was a
    # real request, refused, made once (the EXIT path does not retry it); the
    # person then left by their own request, once, as a member (the EXIT path
    # does not leave again); the link ended off.
    assert "person POST /v1/join-requests" in [" ".join(r.split()[:3]) for r in log]
    assert f"person GET /v1/tenants/me {TENANT}" in log
    assert (fake / "removals").read_text().splitlines() == [PERSON]
    assert "probe_admin_not_permitted" in proc.stderr
    assert f"person POST /v1/workspace/leave {TENANT}" in log
    assert (fake / "leaves").read_text().splitlines() == ["1"]
    assert not (fake / "link_on").exists()
    assert not (fake / "member").exists(), "the person is still a member after the run"
    # The person's sign-in was read from Secret Manager, once, by name.
    assert f"/secrets/{PERSON_SECRET}/versions/latest:access" in (fake / "secrets.read").read_text()
    _assert_nothing_leaked(proc, fake, secret_values)
    _assert_no_group_write(fake)


def test_with_platform_memberships_off_approval_answers_the_group_command(tmp_path):
    proc, fake, secret_values = _probe(tmp_path, "off")
    verdicts = _verdicts(proc)
    assert proc.returncode == 0, proc.stderr
    assert verdicts == {c: "PASS" for c in CHECKS}, proc.stderr
    assert "admitted: false" in proc.stderr and "gcloud identity groups memberships add" in proc.stderr
    # No membership was written, yet the removal's refusal is still a real
    # request, and the person is still refused after it; their leave, too, is
    # a real request, refused because there is nothing to leave.
    assert (fake / "removals").read_text().splitlines() == [PERSON]
    assert (fake / "leaves").read_text().splitlines() == ["0"]
    assert not (fake / "member").exists() and not (fake / "link_on").exists()
    _assert_nothing_leaked(proc, fake, secret_values)
    _assert_no_group_write(fake)


def test_an_approval_that_admits_nobody_fails_with_the_switch_on(tmp_path):
    """MUTATION: the approval answers 200 but writes no membership."""
    proc, _fake, _ = _probe(tmp_path, "on", broken="no-admit")
    verdicts = _verdicts(proc)
    assert verdicts["selects"] == "FAIL", proc.stderr
    assert proc.returncode != 0


def test_a_removal_the_probe_admin_is_allowed_fails(tmp_path):
    """MUTATION: the probe admin's removal answers 200 removed: the probe
    admin holds a power the owner decided it does not."""
    proc, _fake, _ = _probe(tmp_path, "on", broken="removal-allowed")
    assert _verdicts(proc)["removal"] == "FAIL", proc.stderr
    assert "probe_admin_not_permitted" in proc.stderr
    assert proc.returncode != 0


def test_a_refusal_that_removes_the_person_anyway_fails(tmp_path):
    """MUTATION: the removal answers 403 probe_admin_not_permitted but the
    membership is gone: the refusal is checked, not assumed."""
    proc, _fake, _ = _probe(tmp_path, "on", broken="refusal-removes")
    assert _verdicts(proc)["removal"] == "FAIL", proc.stderr
    assert "must still be a member" in proc.stderr
    assert proc.returncode != 0


def test_a_person_an_earlier_run_left_a_member_leaves_first_and_every_check_passes(tmp_path):
    """A run that finds the person still a member (an earlier run crashed
    before its leave): the person leaves first, then the full flow runs --
    the ask is a new request and the approval is measured, never skipped."""
    proc, fake, secret_values = _probe(tmp_path, "on", member=True)
    verdicts = _verdicts(proc)
    assert proc.returncode == 0, proc.stderr
    # MUTATION: keep the member's path (ask 409, approval SKIP) -> not all PASS.
    assert verdicts == {c: "PASS" for c in CHECKS}, proc.stderr
    assert "join_already_member" not in proc.stderr
    log = (fake / "api.log").read_text()
    assert "/approve" in log
    # Left twice, each time a member: first, and at the end of the run.
    assert (fake / "leaves").read_text().splitlines() == ["1", "1"]
    assert (fake / "removals").read_text().splitlines() == [PERSON]
    assert not (fake / "member").exists() and not (fake / "link_on").exists()
    _assert_nothing_leaked(proc, fake, secret_values)
    _assert_no_group_write(fake)


def test_a_member_whose_leave_first_is_refused_fails_every_check(tmp_path):
    """MUTATION: the leave route refuses the member: nothing can be measured
    from a person who is already in, so every check FAILs, never passes."""
    proc, fake, _ = _probe(tmp_path, "on", broken="leave-refused", member=True)
    assert _verdicts(proc) == {c: "FAIL" for c in CHECKS}, proc.stderr
    assert "could not leave first" in proc.stderr
    assert "/approve" not in (fake / "api.log").read_text()
    assert proc.returncode != 0


def test_a_leave_that_keeps_the_person_a_member_fails(tmp_path):
    """MUTATION: the leave answers 200 removed but the membership stays: the
    leave is checked by the person's next request and the member list."""
    proc, _fake, _ = _probe(tmp_path, "on", broken="leave-keeps")
    verdicts = _verdicts(proc)
    assert verdicts["leave"] == "FAIL", proc.stderr
    assert "the person is still a member" in proc.stderr
    assert proc.returncode != 0


def test_a_leave_the_route_refuses_fails(tmp_path):
    proc, _fake, _ = _probe(tmp_path, "on", broken="leave-refused")
    assert _verdicts(proc)["leave"] == "FAIL", proc.stderr
    assert "workspace_leave_not_member" in proc.stderr
    assert proc.returncode != 0



def test_a_person_the_group_already_holds_fails_no_group_write(tmp_path):
    """MUTATION: the API's fresh per-group check finds the person in the
    Google group after the approval -- something wrote the group."""
    proc, _fake, _ = _probe(tmp_path, "on", FAKE_IN_GROUP="true")
    assert _verdicts(proc)["no-group-write"] == "FAIL", proc.stderr


def test_a_link_left_on_fails_link_off(tmp_path):
    proc, _fake, _ = _probe(tmp_path, "on", broken="link-stays-on")
    assert _verdicts(proc)["link-off"] == "FAIL", proc.stderr


# ---------------------------------------------------------------------------
# The person through the front door, the admin at the service's address
# ---------------------------------------------------------------------------


def test_the_persons_requests_go_through_the_front_door_and_the_admins_do_not(tmp_path):
    proc, fake, _ = _probe(tmp_path, "on")
    assert proc.returncode == 0, proc.stderr
    hosts = (fake / "hosts.log").read_text().splitlines()
    person = [h.split()[1] for h in hosts if h.startswith("person ")]
    admin = [h.split()[1] for h in hosts if h.startswith("admin ")]
    # whoami, before, the ask, after, mine, kept, the leave and gone: every one
    # of the person's.
    assert len(person) >= 8 and set(person) == {"front"}, hosts
    assert len(admin) >= 6 and set(admin) == {"api"}, hosts
    assert "nobody" not in " ".join(hosts)
    urls = [line.split()[-1] for line in (fake / "curl.argv").read_text().splitlines()]
    assert any(u == f"{FRONT_URL}/v1/join-requests/mine" for u in urls), urls
    assert any(u == f"{FRONT_URL}/v1/tenants/me" for u in urls), urls
    assert any(u == f"{FRONT_URL}/v1/workspace/leave" for u in urls), urls
    assert not any(u.startswith(f"{API_URL}/v1/join-requests") or u.startswith(f"{API_URL}/v1/tenants/")
                   or u == f"{API_URL}/v1/workspace/leave" for u in urls), urls
    # The person's only workspace route is their own leave; the admin's
    # routes are never sent through the front door.
    assert not any((u.startswith(f"{FRONT_URL}/v1/workspace") and u != f"{FRONT_URL}/v1/workspace/leave")
                   or u.startswith(f"{FRONT_URL}/v1/admin") for u in urls), urls


def test_a_trailing_slash_on_the_front_door_is_not_doubled(tmp_path):
    proc, fake, _ = _probe(tmp_path, "on", **{CONSOLE: FRONT_URL + "/"})
    assert _verdicts(proc) == {c: "PASS" for c in CHECKS}, proc.stderr
    assert f"{FRONT_URL}//v1" not in (fake / "curl.argv").read_text()


def test_the_persons_id_token_is_minted_with_openid_email_only(tmp_path):
    proc, fake, _ = _probe(tmp_path, "on")
    assert proc.returncode == 0, proc.stderr
    assert (fake / "scopes").read_text().splitlines() == ["openid%20email"]


def test_the_person_at_the_services_own_address_is_refused(tmp_path):
    """MUTATION: the front door's URL is the service's address, where the
    Desktop client's ID token is refused: whoami fails and nothing runs."""
    proc, fake, _ = _probe(tmp_path, "on", **{CONSOLE: API_URL})
    assert _verdicts(proc) == {c: "FAIL" for c in CHECKS}, proc.stderr
    assert "HTTP 401" in proc.stderr and "front door" in proc.stderr
    assert not (fake / "link_on").exists()


# ---------------------------------------------------------------------------
# What it needs before it runs
# ---------------------------------------------------------------------------


def _bash(script: str, **env: str) -> subprocess.CompletedProcess:
    # No script guesses the project any more, so the caller names one.
    full = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"), "NO_COLOR": "1",
            "PROJECT_ID": "swarm-dev-test", **placeholder_deployment.child_env("swarm-dev-test"), **env}
    return subprocess.run(["bash", "-c", f'source "{REPO}/scripts/lib/common.sh"\nsource "{GROUP}"\n{script}'],
                          cwd=REPO, env=full,
                          capture_output=True, text=True, timeout=60)


def test_the_checks_are_the_eight_the_design_names():
    lines = _bash("join_checks").stdout.splitlines()
    assert [line.split(":")[1].strip() for line in lines] == list(CHECKS)


def test_it_names_every_missing_setting():
    proc = _bash("join_settings_problem || true")
    for name in (*SETTINGS, CONSOLE):
        assert name in proc.stdout, proc.stdout


def test_the_front_door_must_be_an_https_origin():
    full = {**dict(zip(SETTINGS, (TENANT, ADMIN, PERSON, PERSON_SECRET))), CONSOLE: FRONT_URL}
    assert _bash("join_settings_problem && echo OK", **full).stdout.strip() == "OK"
    for value in ("http://swarm.example.com", "https://user@swarm.example.com",
                  f"{FRONT_URL}/v1", f"{FRONT_URL}?x=1", "swarm.example.com"):
        proc = _bash("join_settings_problem || echo REFUSED", **{**full, CONSOLE: value})
        assert "REFUSED" in proc.stdout and CONSOLE in proc.stdout, value


def test_it_never_runs_in_the_smoke_tenant_a_personal_one_or_as_a_service_account():
    full = {**dict(zip(SETTINGS, (TENANT, ADMIN, PERSON, PERSON_SECRET))), CONSOLE: FRONT_URL}
    assert _bash("join_settings_problem && echo OK", **full).stdout.strip() == "OK"
    # The never-touch list is the placeholder deployment's, served for the project _bash
    # names: a name on it (a PEER entry, never a real resource) is refused as the secret,
    # and its service account as the admin.
    peer_secret = placeholder_deployment.PEER["bucket"]
    peer_sa = placeholder_deployment.PEER["service_account"]
    for name, value in ((SETTINGS[0], "smoke"), (SETTINGS[0], "u-someone"), (SETTINGS[0], "Eng"),
                        (SETTINGS[1], peer_sa),
                        (SETTINGS[2], "swarm-qa@example-project.iam.gserviceaccount.com"),
                        (SETTINGS[3], "a/b"), (SETTINGS[3], peer_secret)):
        proc = _bash("join_settings_problem || echo REFUSED", **{**full, name: value})
        assert "REFUSED" in proc.stdout, (name, value)


def test_until_it_is_configured_every_check_skips_and_says_why(tmp_path):
    proc, _fake, _ = _probe(tmp_path, "on", **{SETTINGS[3]: ""})
    verdicts = _verdicts(proc)
    assert verdicts == {c: "SKIP" for c in CHECKS}, proc.stderr
    assert SETTINGS[3] in proc.stderr


def test_without_the_front_door_every_check_skips_naming_it(tmp_path):
    proc, fake, _ = _probe(tmp_path, "on", **{CONSOLE: ""})
    assert _verdicts(proc) == {c: "SKIP" for c in CHECKS}, proc.stderr
    assert f"{CONSOLE} is unset" in proc.stderr
    assert not (fake / "api.log").exists(), "the probe made a request with no front door"


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
    for name in (*SETTINGS, CONSOLE):
        assert name in proc.stderr, proc.stderr
    assert proc.stderr.count("SKIP") == len(CHECKS), proc.stderr
    assert "NOT MEASURED" in proc.stderr
    assert "SANDBOX_REPOSITORY_URL" not in proc.stderr
    assert "FAIL" not in proc.stderr


def test_partly_provisioned_it_names_only_what_is_missing(tmp_path):
    proc = _standalone(tmp_path, **{SETTINGS[0]: TENANT, SETTINGS[2]: PERSON})
    assert proc.returncode == 0, proc.stderr
    assert f"{SETTINGS[1]} is unset" in proc.stderr and f"{SETTINGS[3]} is unset" in proc.stderr
    assert f"{SETTINGS[0]} is unset" not in proc.stderr and f"{SETTINGS[2]} is unset" not in proc.stderr
    assert f"{CONSOLE} is unset" in proc.stderr


def test_provisioned_but_for_the_front_door_it_skips_naming_it(tmp_path):
    proc = _standalone(tmp_path, **dict(zip(SETTINGS, (TENANT, ADMIN, PERSON, PERSON_SECRET))))
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr.count("SKIP") == len(CHECKS), proc.stderr
    assert f"{CONSOLE} is unset" in proc.stderr
    for name in SETTINGS:
        assert f"{name} is unset" not in proc.stderr
    assert "SANDBOX_REPOSITORY_URL" not in proc.stderr


def test_provisioned_it_still_reaches_the_configuration_check(tmp_path):
    proc = _standalone(tmp_path, **dict(zip(SETTINGS, (TENANT, ADMIN, PERSON, PERSON_SECRET))),
                       **{CONSOLE: FRONT_URL})
    assert proc.returncode == 1, proc.stderr
    assert "SWARM_ACCEPTANCE_SANDBOX_REPOSITORY_URL is not set" in proc.stderr, proc.stderr
    assert "SKIP" not in proc.stderr


# ---------------------------------------------------------------------------
# Where it runs
# ---------------------------------------------------------------------------


def test_accept_yml_runs_it_in_the_smoke_job_beside_the_roles_probe():
    jobs = _workflow("accept.yml")["jobs"]
    steps = jobs["smoke"]["steps"]
    mine = [i for i, s in enumerate(steps) if TARGET in _code(s.get("run", ""))]
    roles = [i for i, s in enumerate(steps) if "acceptance/groups/roles" in _code(s.get("run", ""))]
    assert len(mine) == 1 and len(roles) == 1
    assert mine[0] == roles[0] + 1, "the join probe runs right after the roles probe"
    step = steps[mine[0]]
    assert _code(step["run"]).strip() == f"./scripts/verify-remote.sh {TARGET}"
    assert "!cancelled()" in str(step.get("if"))
    for name in SETTINGS:
        # An account or a person's address is a masked secret
        # (docs/design/generic-repository.md, section 2.1); the rest are variables.
        context = "secrets" if name.endswith(("_SA", "_PERSON")) else "vars"
        assert step["env"][name] == f"${{{{ {context}.{name.removeprefix('SWARM_ACCEPTANCE_')} }}}}"
    for job_id, job in jobs.items():
        if job_id != "smoke":
            assert TARGET not in json.dumps(job), job_id


def test_verify_remote_carries_the_probes_settings_into_the_execution(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "gcloud"
    fake.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >>"${FAKE_DIR}/gcloud.calls"\necho ok\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HOME": str(tmp_path),
           "NO_COLOR": "1", "FAKE_DIR": str(tmp_path), "TMPDIR": str(tmp_path),
           "SWARM_ENV_FILE": str(tmp_path / "no.env"), "PROJECT_ID": "swarm-dev-test",
           SETTINGS[0]: TENANT, SETTINGS[2]: PERSON, SETTINGS[3]: PERSON_SECRET,
           **placeholder_deployment.child_env("swarm-dev-test")}
    proc = subprocess.run(["bash", str(VERIFY_REMOTE), TARGET], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    call = (tmp_path / "gcloud.calls").read_text()
    assert f"--args scripts/{TARGET}.sh" in call
    assert f"{SETTINGS[0]}={TENANT}" in call
    assert f"{SETTINGS[2]}={PERSON}" in call
    assert f"{SETTINGS[3]}={PERSON_SECRET}" in call
    assert SETTINGS[1] not in call


def test_the_group_runs_on_its_own():
    assert GROUP.stat().st_mode & stat.S_IXUSR, "scripts/acceptance/groups/join.sh is not executable"
    assert GROUP.read_text().splitlines()[0] == "#!/usr/bin/env bash"

