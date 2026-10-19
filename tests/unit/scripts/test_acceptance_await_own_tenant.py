"""The await-input acceptance checks run in the caller's OWN service tenant (HW-ACCEPT-ID).

Owner decision 2026-10-19 (part of #1436): release acceptance answers its
await-input step as swarm-verify in swarm-verify's own service tenant,
u-sw-c90291, because a service account answers for no person in the GROUP
tenant `smoke` (403 `person_required`, unrelaxed). So the two await-input
workflows -- `workflow: await-input` and `workflow: await-input expiry` -- are
submitted, read and answered with X-Swarm-Tenant set to that tenant, and every
other check in the group stays in ACC_TENANT.

The id is DERIVED, from the email GET /tenants/me verified, with common.sh's
`personal_tenant_id`, which imports the frozen `tenant_id_for_user`: the
expected value below is computed from the same frozen function, never typed.

Run offline: the real lib.sh, workflow.sh and common.sh are sourced, and only
the calls that would reach a deployment (curl, submit_workflow, cancel_task,
api_fetch) are stubbed, each recording the tenant it would have sent.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

from swarm_common.identity import tenant_id_for_user

ROOT = Path(__file__).resolve().parents[3]
COMMON = ROOT / "scripts" / "lib" / "common.sh"
LIB = ROOT / "scripts" / "acceptance" / "lib.sh"
WORKFLOW = ROOT / "scripts" / "acceptance" / "groups" / "workflow.sh"
DOCKERFILE = ROOT / "images" / "swarm-verify" / "Dockerfile"
CLOUDBUILD = ROOT / "images" / "swarm-verify" / "cloudbuild.yaml"

VERIFY = "swarm-verify@saga-agents-staging.iam.gserviceaccount.com"
OWN = tenant_id_for_user(VERIFY)

#: The ACC_CHECK of each workflow run_workflow submits, and whether it runs in
#: the caller's own tenant.
OWN_TENANT_CHECKS = {"workflow: await-input", "workflow: await-input expiry"}


def _function(text: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n(.*?)^\}}\n", text, re.S | re.M)
    assert match, f"{name}() is not defined"
    return match.group(1)


def _run(body: str, email: str = VERIFY) -> subprocess.CompletedProcess:
    script = f"""#!/usr/bin/env bash
set -euo pipefail
source {shlex.quote(str(COMMON))} >/dev/null
t_pass() {{ printf 'PASS %s\\n' "$*"; }}
t_fail() {{ printf 'FAIL %s\\n' "$*"; }}
t_case() {{ :; }}
t_skip() {{ printf 'SKIP %s\\n' "$*"; }}
t_info() {{ :; }}
test_run_id() {{ printf 'test-run'; }}
source {shlex.quote(str(LIB))}
source {shlex.quote(str(WORKFLOW))}
acc_init
LOG="$(mktemp)"
cancel_all() {{ local id; for id in "$@"; do cancel_task "${{id}}"; done; }}
api_fetch() {{
  printf 'fetch %s %s\\n' "${{SWARM_API_TENANT}}" "$1" >>"${{LOG}}"
  jq -nc --arg e "${{CALLER_EMAIL}}" '{{tenant: {{tenant_id: "smoke"}}, principal: {{email: $e}}}}' >"$2"
}}
submit_workflow() {{
  printf 'submit %s\\n' "${{SWARM_API_TENANT}}" >>"${{LOG}}"
  jq -nc --arg n "$RANDOM" '{{workflow: {{workflow_id: ("wf-" + $n), steps: [{{task_id: ("task-" + $n)}}]}}}}'
}}
cancel_task() {{ printf 'cancel %s %s\\n' "${{SWARM_API_TENANT}}" "$1" >>"${{LOG}}"; }}
api_credential() {{ printf 'cred'; }}
auth_config() {{ cat >/dev/null; }}
api_url() {{ printf 'http://api'; }}
curl() {{
  local prev="" arg
  for arg in "$@"; do
    if [[ "${{prev}}" == "-H" && "${{arg}}" == X-Swarm-Tenant:* ]]; then
      printf 'curl %s\\n' "${{arg#X-Swarm-Tenant: }}" >>"${{LOG}}"
    fi
    prev="${{arg}}"
  done
  printf '200'
}}
{body}
"""
    env = dict(os.environ, NO_COLOR="1", CALLER_EMAIL=email)
    env.pop("SWARM_ACCEPTANCE_TENANT", None)
    env.pop("SWARM_API_TENANT", None)
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
        handle.write(script)
        path = handle.name
    try:
        return subprocess.run(["bash", path], capture_output=True, text=True, timeout=60,
                              check=False, env=env, cwd=ROOT)
    finally:
        os.unlink(path)


def test_the_own_tenant_is_derived_with_the_frozen_rule_from_the_verified_email():
    result = _run('_wf_own_tenant; printf "own=%s\\n" "${WF_OWN_TENANT}"; cat "${LOG}"; rm -f "${LOG}"')
    assert result.returncode == 0, result.stderr
    assert f"own={OWN}" in result.stdout
    assert OWN == "u-sw-c90291"          # dev.tfvars' key: the derivation is the same
    # Asked of the API in the suite's own tenant, before anything is switched.
    assert "fetch smoke /tenants/me" in result.stdout


def test_an_underivable_tenant_is_named_never_a_fallback():
    result = _run('_wf_own_tenant || true; printf "own=[%s] why=%s\\n" "${WF_OWN_TENANT}" "${WF_OWN_PROBLEM}"',
                  email="")
    assert result.returncode == 0, result.stderr
    assert "own=[]" in result.stdout and "principal.email" in result.stdout


def test_the_own_tenant_selects_only_inside_the_wrapper_and_cleanup_follows_it():
    body = r"""
_wf_own_tenant
_wf_in_own_tenant acc_workflow mine '{}'
acc_workflow theirs '{}'
_wf_in_own_tenant acc_raw_artifact task-x review.json "${ACC_WORK}/a"
acc_raw_artifact task-y out.txt "${ACC_WORK}/b"
_wf_in_own_tenant _wf_post_answer wf-1 review "${ACC_WORK}/a" "${ACC_WORK}/a" "${ACC_WORK}/c"
printf 'after=%s\n' "${SWARM_API_TENANT}"
printf 'mine=%s\n' "$(jq -r '.workflow.steps[0].task_id' <<<"${mine}")"
printf 'theirs=%s\n' "$(jq -r '.workflow.steps[0].task_id' <<<"${theirs}")"
printf 'tasks=%s\n' "${ACC_TASKS[*]}"
printf 'other=%s\n' "${ACC_OTHER_TASKS[*]}"
acc_cleanup
cat "${LOG}"
rm -f "${LOG}"
"""
    result = _run(body)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    mine = re.search(r"^mine=(\S+)$", out, re.M).group(1)
    theirs = re.search(r"^theirs=(\S+)$", out, re.M).group(1)
    # The submissions: the wrapped one in the own tenant, the other in smoke.
    assert re.findall(r"^submit (\S+)$", out, re.M) == [OWN, "smoke"]
    # The raw reads and the answer send the header the wrapper selects.
    assert re.findall(r"^curl (\S+)$", out, re.M) == [OWN, "smoke", OWN]
    # And the shell is back in smoke afterwards.
    assert "after=smoke" in out
    # Cleanup cancels each task in the tenant it was submitted in.
    assert f"tasks={theirs}" in out and f"other={OWN} {mine}" in out
    assert f"cancel smoke {theirs}" in out and f"cancel {OWN} {mine}" in out


def test_run_workflow_runs_exactly_the_two_await_checks_in_the_own_tenant():
    run = _function(WORKFLOW.read_text(), "run_workflow")
    submissions = re.findall(r'ACC_CHECK="([^"]+)"\n(?:.*\n){0,2}?.*?(_wf_in_own_tenant )?acc_workflow \w+ ',
                             run)
    seen = {check: bool(wrapped) for check, wrapped in submissions}
    assert set(seen) >= OWN_TENANT_CHECKS | {"workflow: input_from", "workflow: carry"}, seen
    for check, wrapped in seen.items():
        assert wrapped == (check in OWN_TENANT_CHECKS), (check, wrapped)
    # The judges of those two run in it too, and no other judge does.
    judged = re.findall(r"^\s*(_wf_in_own_tenant )?(_wf_check_\w+) ", run, re.M)
    assert {name for wrapped, name in judged if wrapped} == {"_wf_check_await", "_wf_check_expire"}
    assert len(judged) == 7
    # The id is derived before the first own-tenant submission.
    assert run.index("_wf_own_tenant") < run.index("_wf_in_own_tenant")
    # Never typed in.
    assert OWN not in WORKFLOW.read_text().replace(f"swarm-verify's {OWN}", "")


def test_the_answer_asserts_it_was_taken_as_the_service_tenants_principal():
    check = _function(WORKFLOW.read_text(), "_wf_check_await")
    assert '"person service"' in check and "answered_as" in check


def test_the_swarm_verify_image_carries_the_frozen_identity_module():
    """personal_tenant_id imports apps/common/swarm_common/identity.py; the
    suite runs in the swarm-verify image, which must carry it."""
    path = "apps/common/swarm_common/identity.py"
    copies = [line for line in DOCKERFILE.read_text().splitlines() if line.startswith("COPY ")]
    assert any(line.split()[1] == path and line.split()[2] == f"/swarm/{path}" for line in copies), copies
    build = [line.strip() for line in CLOUDBUILD.read_text().splitlines()]
    assert f"cp {path} /workspace/verify-ctx/{path}" in build
    assert f"test -f /workspace/verify-ctx/{path}" in build
    # Standard library only, so the image needs nothing else to import it.
    imports = re.findall(r"^(?:from|import) (\S+)", (ROOT / path).read_text(), re.M)
    assert set(imports) <= {"__future__", "hashlib", "re", "collections.abc", "dataclasses"}, imports
