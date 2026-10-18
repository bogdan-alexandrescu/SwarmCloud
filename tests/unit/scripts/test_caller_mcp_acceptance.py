"""The mcp acceptance group's judge and wiring, offline (MCP-ACCEPT-B, part of #1433).

tests/acceptance/test_caller_mcp.py judges what scripts/acceptance/groups/
mcp.sh saved from a deployed run. Owner decision 2026-10-18: nothing in
Terraform is public, so the echo server's one invoker is the acceptance
identity and the task's per-run MCP credential is that identity's Google ID
token. These pin, with no deployment and no network:

* the judge's selftest: the echo server on loopback, the token mint and the
  unauthenticated probe against a fake metadata server, IAM and Cloud Run,
  and every check PASSING on a working platform's evidence and FAILING or
  SKIPPING on each defect it names;
* the task body carries the minted ID token as its server's MCP credential,
  with the token's own remaining life, and nowhere the agent reads;
* a call without the token is judged by who refused it: Cloud Run's 403
  passes, the echo server's own 401 means the service is public;
* mcp.sh lists the judge's checks in its order, mints before it submits and
  submits nothing without a token;
* Terraform names no public member for the echo server, and its invoker,
  mcp.sh's invoker and terraform/bootstrap's acceptance account are one
  account.

Every fake token is built at run time (CLAUDE.md, "Nothing you add may look
like a credential").
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
JUDGE_PATH = ROOT / "tests" / "acceptance" / "test_caller_mcp.py"
GROUP = ROOT / "scripts" / "acceptance" / "groups" / "mcp.sh"
TF = ROOT / "terraform" / "infra" / "mcp_test_server.tf"
BOOTSTRAP_ACCEPTANCE = ROOT / "terraform" / "bootstrap" / "acceptance.tf"
INFRA_VARIABLES = ROOT / "terraform" / "infra" / "variables.tf"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mcp = _load("acceptance_caller_mcp", JUDGE_PATH)

INVOKER = "swarm-accept@saga-agents-staging.iam.gserviceaccount.com"
URL = "https://swarm-mcp-echo-abc123-uc.a.run.app/mcp"
AUD = "https://swarm-mcp-echo-abc123-uc.a.run.app"


def _dir(tmp_path: Path) -> Path:
    mcp.mint(tmp_path)
    (tmp_path / "registration.json").write_text(json.dumps({"server": {
        "name": "acc-echo", "url": URL, "enabled": True, "auth": {"mode": "dispatch"},
        "allowed_profiles": ["claude-code"]}}))
    return tmp_path


def test_the_judge_selftest_catches_every_defect_it_names() -> None:
    assert mcp.selftest() == 0


def test_a_task_body_carries_the_id_token_as_the_servers_mcp_credential(tmp_path: Path) -> None:
    base = _dir(tmp_path)
    assert mcp.token(base, INVOKER, mcp.fake_google(AUD, INVOKER)) is True
    values = json.loads((base / "secrets.json").read_text())
    claims = mcp.jwt_claims(values["credential"])
    assert claims["aud"] == AUD and claims["email"] == INVOKER
    task = mcp.body(values, kind="task", server="acc-echo", run_id="r1", metadata={})
    assert task["mcp_credentials"] == {"acc-echo": {
        "value": values["credential"], "expires_in": values["credential_expires_in"]}}
    assert 3500 <= task["mcp_credentials"]["acc-echo"]["expires_in"] <= 3600
    assert values["credential"] not in json.dumps(task["input"])
    assert values["credential"] not in (base / "token.json").read_text()


def test_a_mint_iam_refuses_submits_nothing_and_skips_naming_the_grant(tmp_path: Path) -> None:
    base = _dir(tmp_path)
    assert mcp.token(base, INVOKER, mcp.fake_google(AUD, INVOKER, iam=403)) is False
    state = json.loads((base / "token.json").read_text())
    assert state["minted"] is False and state["refused"] is True
    assert "serviceAccountOpenIdTokenCreator" in state["reason"]
    assert "credential" not in json.loads((base / "secrets.json").read_text())
    assert mcp.main(["body", "--dir", str(base), "--kind", "task", "--server", "acc-echo",
                     "--run-id", "r1", "--metadata", "{}"]) == 1
    (base / "server").write_text("acc-echo\n")
    (base / "submit.json").write_text(json.dumps({"status": "not sent", "code": None, "task": ""}))
    (base / "task").write_text("\n")
    verdicts = {v.check: v for v in mcp.judge(str(base))}
    assert verdicts["credential_seen"].verdict == "SKIP"
    assert "serviceAccountOpenIdTokenCreator" in verdicts["credential_seen"].reason


def test_a_call_without_the_token_passes_only_on_cloud_runs_403(tmp_path: Path) -> None:
    base = _dir(tmp_path)
    (base / "server").write_text("acc-echo\n")
    got = {}
    for status in (403, 401, 200, 0):
        assert mcp.probe(base, mcp.fake_google(AUD, INVOKER, cloud_run=status)) == status
        got[status] = {v.check: v for v in mcp.judge(str(base))}["unauthenticated_refused"]
    assert got[403].verdict == "PASS"
    assert got[401].verdict == "FAIL" and "public" in got[401].reason
    assert got[200].verdict == "FAIL"
    assert got[0].verdict == "FAIL"


def _run_mcp() -> str:
    text = GROUP.read_text()
    return text[text.index("run_mcp() {"):]


def test_mcp_sh_lists_the_judges_checks_in_its_order() -> None:
    text = GROUP.read_text()
    keys = re.search(r"^MCP_KEYS=\(([^)]*)\)", text, re.M)
    assert keys is not None
    assert tuple(keys.group(1).split()) == mcp.CHECKS
    listed = re.search(r"mcp_checks\(\) \{\n  cat <<'EOF'\n(.*?)\nEOF", text, re.S)
    assert listed is not None
    assert len(listed.group(1).splitlines()) == len(mcp.CHECKS)


def test_mcp_sh_probes_and_mints_before_it_submits_and_never_without_a_token() -> None:
    body = _run_mcp()
    probe = body.index('/${MCP_JUDGE}" probe --dir "${dir}"')
    token = body.index('/${MCP_JUDGE}" token --dir "${dir}" --invoker "${MCP_INVOKER}"')
    gate = body.index("""jq -r '.minted == true' "${dir}/token.json\"""")
    submit = body.index('_mcp_submit "${dir}"')
    assert probe < token < gate < submit
    assert body.count('_mcp_submit "${dir}"') == 1


def test_the_echo_server_is_not_public_and_its_invoker_is_the_acceptance_account() -> None:
    tf = TF.read_text()
    assert not re.search(r"allUsers|allAuthenticatedUsers", tf)
    members = re.findall(r'^\s*member\s*=\s*"([^"]*)"', tf, re.M)
    invokers = re.findall(
        r'resource "google_cloud_run_v2_service_iam_member" "[a-z_]+" \{.*?\n\}', tf, re.S)
    assert len(invokers) == 1
    assert 'role     = "roles/run.invoker"' in invokers[0]
    assert 'member   = "serviceAccount:${local.mcp_test_server_invoker}"' in invokers[0]
    assert "serviceAccount:${local.mcp_test_server_invoker}" in members
    assert re.search(r'mcp_test_server_invoker = "\$\{var\.name_prefix\}-accept@\$\{var\.project_id\}'
                     r'\.iam\.gserviceaccount\.com"', tf)
    # bootstrap spells the account accept.yml federates as from the same id.
    assert 'account_id   = "${var.name_prefix}-accept"' in BOOTSTRAP_ACCEPTANCE.read_text()
    prefix = re.search(r'variable "name_prefix" \{.*?default\s*=\s*"([^"]+)"', INFRA_VARIABLES.read_text(), re.S)
    assert prefix is not None and prefix.group(1) == "swarm"
    # mcp.sh mints as that same account by default.
    assert re.search(r'^MCP_INVOKER="\$\{SWARM_ACCEPTANCE_MCP_INVOKER:-swarm-accept@\$\{PROJECT_ID\}'
                     r'\.iam\.gserviceaccount\.com\}"$', GROUP.read_text(), re.M)
