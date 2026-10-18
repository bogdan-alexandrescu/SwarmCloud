"""The mcp acceptance group's judge and wiring, offline (MCP-ACCEPT-B, part of #1433).

tests/acceptance/test_caller_mcp.py judges what scripts/acceptance/groups/
mcp.sh saved from a deployed run. Owner decision 2026-10-18: nothing in
Terraform is public, so the echo server's one invoker is swarm-verify, the
identity the suite runs as, and the task's per-run MCP credential is the
suite's own Google ID token from the metadata server (nothing impersonated). These pin, with no deployment and no network:

* the judge's selftest: the echo server on loopback, the token mint and the
  unauthenticated probe against a fake metadata server and Cloud Run,
  and every check PASSING on a working platform's evidence and FAILING or
  SKIPPING on each defect it names;
* the task body carries the minted ID token as its server's MCP credential,
  with the token's own remaining life, and nowhere the agent reads;
* a call without the token is judged by who refused it: Cloud Run's 403
  passes, the echo server's own 401 means the service is public;
* Cloud Run hands the container a Google-signed token without its signature,
  which the relay cannot scrub, so the echo server withholds that echo, and
  the judge neither calls an echo of it scrubbed nor counts any masked JWT
  as this credential's unless it sits where only this credential goes;
* mcp.sh lists the judge's checks in its order, mints before it submits and
  submits nothing without a token;
* Terraform names no public member for the echo server, and its invoker and
  mcp.sh's invoker are one account, swarm-verify.

Every fake token is built at run time (CLAUDE.md, "Nothing you add may look
like a credential").
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
JUDGE_PATH = ROOT / "tests" / "acceptance" / "test_caller_mcp.py"
GROUP = ROOT / "scripts" / "acceptance" / "groups" / "mcp.sh"
TF = ROOT / "terraform" / "infra" / "mcp_test_server.tf"
SERVICE_ACCOUNT_IDS = ROOT / "terraform" / "modules" / "service_account_ids" / "main.tf"
INFRA_VARIABLES = ROOT / "terraform" / "infra" / "variables.tf"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mcp = _load("acceptance_caller_mcp", JUDGE_PATH)

INVOKER = "swarm-verify@saga-agents-staging.iam.gserviceaccount.com"
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


def test_a_mint_the_metadata_server_cannot_serve_submits_nothing_and_fails_naming_why(tmp_path: Path) -> None:
    base = _dir(tmp_path)
    assert mcp.token(base, INVOKER, mcp.fake_google(AUD, INVOKER, metadata=500)) is False
    state = json.loads((base / "token.json").read_text())
    assert state["minted"] is False and "refused" not in state
    assert "metadata server" in state["reason"]
    assert "credential" not in json.loads((base / "secrets.json").read_text())
    assert mcp.main(["body", "--dir", str(base), "--kind", "task", "--server", "acc-echo",
                     "--run-id", "r1", "--metadata", "{}"]) == 1


def test_the_suite_mints_its_own_token_and_impersonates_nobody(tmp_path: Path) -> None:
    base = _dir(tmp_path)
    calls: list = []
    assert mcp.token(base, INVOKER, mcp.fake_google(AUD, INVOKER, calls=calls)) is True
    assert len(calls) == 1 and calls[0][1].startswith(mcp.METADATA_IDENTITY_URL + "?")
    assert not any("iamcredentials" in c[1] for c in calls)
    # A runtime identity that is not the invoker gets no credential.
    other = "swarm-accept@saga-agents-staging.iam.gserviceaccount.com"
    assert mcp.token(base, other, mcp.fake_google(AUD, INVOKER)) is False
    src = JUDGE_PATH.read_text()
    assert "generateIdToken" not in src and "serviceAccountOpenIdTokenCreator" not in src.split(
        "def mint_id_token", 1)[1]


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


def test_the_echo_server_is_not_public_and_its_invoker_is_swarm_verify() -> None:
    tf = TF.read_text()
    assert not re.search(r"allUsers|allAuthenticatedUsers", tf)
    members = re.findall(r'^\s*member\s*=\s*"([^"]*)"', tf, re.M)
    invokers = re.findall(
        r'resource "google_cloud_run_v2_service_iam_member" "[a-z_]+" \{.*?\n\}', tf, re.S)
    assert len(invokers) == 1
    assert 'role     = "roles/run.invoker"' in invokers[0]
    assert 'member   = "serviceAccount:${local.mcp_test_server_invoker}"' in invokers[0]
    assert "serviceAccount:${local.mcp_test_server_invoker}" in members
    assert re.search(r'mcp_test_server_invoker = "\$\{module\.service_account_ids\.verify_id\}@'
                     r'\$\{var\.project_id\}\.iam\.gserviceaccount\.com"', tf)
    # ... and that id is the one verify.tf creates swarm-verify from.
    assert 'verify_id = "swarm-verify"' in SERVICE_ACCOUNT_IDS.read_text()
    prefix = re.search(r'variable "name_prefix" \{.*?default\s*=\s*"([^"]+)"', INFRA_VARIABLES.read_text(), re.S)
    assert prefix is not None and prefix.group(1) == "swarm"
    # mcp.sh expects that same account by default.
    assert re.search(r'^MCP_INVOKER="\$\{SWARM_ACCEPTANCE_MCP_INVOKER:-swarm-verify@\$\{PROJECT_ID\}'
                     r'\.iam\.gserviceaccount\.com\}"$', GROUP.read_text(), re.M)


#: Cloud Run's marker for the signature of a Google-signed token its IAM
#: check admitted (the echo server's REMOVED_SIGNATURES).
REMOVED = "SIGNATURE_REMOVED_BY_GOOGLE"


def _stripped(token: str) -> str:
    return token.rsplit(".", 1)[0] + "." + REMOVED


def test_the_echo_server_withholds_a_token_cloud_run_stripped_of_its_signature() -> None:
    server = mcp._load_server()
    signed = mcp.fake_id_token(AUD, INVOKER)
    claims = hashlib.sha256(signed.split(".")[1].encode()).hexdigest()
    report = server.whoami(signed, {}, {})
    assert report["credential_echo"] == signed and report["signature_removed"] is False
    for unsigned in (_stripped(signed), signed.rsplit(".", 1)[0] + "."):
        report = server.whoami(unsigned, {}, {})
        assert report["credential_echo"] is None and report["signature_removed"] is True
        assert report["jwt_claims_sha256"] == claims
        assert signed.split(".")[1] not in json.dumps(report)


def _evidence(tmp_path: Path, echo_of) -> tuple[dict, dict]:
    """A working platform's evidence (the selftest's), with the answer's
    credential fields as the deployed server reports them for `echo_of`."""
    server = mcp._load_server()
    values: dict = mcp.mint(tmp_path / "mint")
    values["credential"] = mcp.fake_id_token(AUD, INVOKER)
    values["credential_expires_in"] = 3599
    received = echo_of(values["credential"])
    report = server.whoami(received, {"nonce": "n-1"}, {
        server.TASK_HEADER: "task_" + "a" * 20, server.ATTEMPT_HEADER: "att_" + "b" * 20})
    report["credential_sha256"] = hashlib.sha256(received.encode()).hexdigest()
    base = tmp_path / "run"
    base.mkdir()
    mcp._evidence(base, values, report)
    for name in ("answer.txt", "0.bin"):
        (base / "artifacts" / name).write_text(json.dumps(report))
    return values, {v.check: v for v in mcp.judge(str(base))}


def test_a_withheld_echo_of_a_stripped_token_passes_and_is_named_withheld(tmp_path: Path) -> None:
    _, got = _evidence(tmp_path, _stripped)
    assert got["credential_seen"].verdict == "PASS"
    assert "withheld" in got["credential_seen"].reason
    assert "scrubbed" not in got["credential_seen"].reason
    assert got["no_leak"].verdict == "PASS", got["no_leak"].reason


def test_an_echo_of_the_stripped_token_is_a_leak_and_never_called_scrubbed(tmp_path: Path) -> None:
    values, _ = _evidence(tmp_path, _stripped)
    for echo in (_stripped(values["credential"]), values["credential"][:mcp.JWT_KEPT] + mcp.MASK):
        base = tmp_path / "run"
        for name in ("answer.txt", "0.bin"):
            path = base / "artifacts" / name
            path.write_text(json.dumps({**json.loads(path.read_text()), "credential_echo": echo}))
        got = {v.check: v for v in mcp.judge(str(base))}
        assert got["credential_seen"].verdict == "PASS"
        assert "NOT scrubbed" in got["credential_seen"].reason
        assert got["no_leak"].verdict == "FAIL"


def test_only_a_masked_jwt_where_only_this_credential_goes_is_counted_as_it() -> None:
    credential = mcp.fake_id_token(AUD, INVOKER)
    masked = credential[:mcp.JWT_KEPT] + mcp.MASK
    # Every Google ID token's header starts the same, so its masked form is
    # every such token's: on its own it names no credential.
    assert mcp.fake_id_token(AUD, "other@x.iam.gserviceaccount.com")[:mcp.JWT_KEPT] \
        == credential[:mcp.JWT_KEPT]
    loose = [f"Authorization: Bearer {masked}", f'{{"token": "{masked}"}}']
    tied = [f'{{"mcp_credentials": {{"acc-echo": "{masked}"}}}}',
            f'{{"acc-echo":{{"value":"{masked}"}}}}',
            f'{{"credential_echo": "{masked}"}}',
            json.dumps({"content": f'{{"credential_echo": "{masked}"}}'})]
    blobs = [mcp.Blob("logs", f"item {i}", t.encode()) for i, t in enumerate(loose + tied)]
    held = mcp._holding(blobs, credential, "acc-echo")
    assert held == [f"logs item {i} (its masked JWT form)"
                    for i in range(len(loose), len(loose) + len(tied))]
