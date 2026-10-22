"""`scripts/create-secrets.sh --git-wf`: the platform tenant's workflow-scoped
forge token, `swarm-tenant-<platform tenant>-git-wf` (docs/design/agent-autonomy.md
§3.3, owner decision Q3; lane AU-2 WF-SLOT).

The script is run for real against a fake `gcloud` on PATH that records what
it was asked (the harness of test_create_secrets_tailscale_oauth.py). What it
holds:

  * the value comes from stdin ONLY: a value on the command line, glued to the
    flag or after it, is refused without being repeated, and so is a file
    argument;
  * the secret's name is swarm-tenant-<tenant>-git-wf, the one
    terraform/infra/git_tools.tf declares, and nothing else names it;
  * the secret is NOT created here and no IAM binding is made here: terraform
    owns the container and its one reader, swarm-api, and a tenant that is not
    the platform tenant has no slot to add to;
  * the generic tenant path (`--provider git-wf`) refuses the slot, because it
    would create it with a tenant-credential label and send the operator to
    bind the tenant's worker to it;
  * Terraform declares the slot empty (no secret version in git_tools.tf).

Every fake value is built at runtime.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "create-secrets.sh"
GIT_TOOLS_TF = ROOT / "terraform" / "infra" / "git_tools.tf"
TENANT = "eng"
SECRET = f"swarm-tenant-{TENANT}-git-wf"

FAKE_GCLOUD = r"""#!{python}
import json, os, sys
args = sys.argv[1:]
entry = {"args": args}
data_file = next((a.split("=", 1)[1] for a in args if a.startswith("--data-file=")), None)
if data_file:
    entry["data"] = open(data_file).read()
with open(os.environ["FAKE_GCLOUD_LOG"], "a") as fh:
    fh.write(json.dumps(entry) + "\n")
existing = set(filter(None, os.environ.get("FAKE_EXISTING", "").split(",")))
if args[:2] == ["secrets", "describe"]:
    if args[2] in existing:
        print("projects/1/secrets/" + args[2])
        sys.exit(0)
    sys.stderr.write("NOT_FOUND: Secret [" + args[2] + "] not found\n")
    sys.exit(1)
if args[:3] == ["secrets", "versions", "add"]:
    print("projects/1/secrets/" + args[3] + "/versions/2")
    sys.exit(0)
sys.stderr.write("unexpected gcloud call: " + " ".join(args) + "\n")
sys.exit(3)
"""

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="create-secrets.sh needs bash")


def _run(tmp_path: Path, *args: str, stdin: str = "", existing: str = SECRET) -> tuple[int, str, list[dict]]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gcloud = bindir / "gcloud"
    gcloud.write_text(FAKE_GCLOUD.replace("{python}", sys.executable))
    gcloud.chmod(0o755)
    log = tmp_path / "gcloud.jsonl"
    log.write_text("")
    env = {k: v for k, v in os.environ.items() if k not in ("SWARM_ASSUME_YES", "PROJECT_ID", "ENVIRONMENT")}
    env.update({
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "SWARM_ENV_FILE": str(tmp_path / "no-such.env"),
        "PROJECT_ID": "swarm-test-project",
        "REGION": "us-central1",
        "ENVIRONMENT": "dev",
        "NO_COLOR": "1",
        "TMPDIR": str(tmp_path),
        "FAKE_GCLOUD_LOG": str(log),
        "FAKE_EXISTING": existing,
    })
    proc = subprocess.run(["bash", str(SCRIPT), *args], input=stdin, env=env,
                          capture_output=True, text=True, timeout=60, check=False)
    calls = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    return proc.returncode, proc.stdout + proc.stderr, calls


def _value() -> str:
    # A token-shaped value, assembled here so no literal in this file is one.
    return "gh" + "p_" + secrets.token_hex(18)


def _adds(calls: list[dict]) -> list[dict]:
    return [c for c in calls if c["args"][:3] == ["secrets", "versions", "add"]]


def _writes(calls: list[dict]) -> list[dict]:
    return [c for c in calls if c["args"][:2] in (["secrets", "create"], ["secrets", "add-iam-policy-binding"],
                                                  ["secrets", "set-iam-policy"])]


def test_the_value_is_stored_from_stdin_into_the_slot_and_never_printed(tmp_path):
    value = _value()
    rc, out, calls = _run(tmp_path, "--git-wf", "--tenant", TENANT, "--stdin", stdin=value + "\n")
    # MUTATION: drop the --git-wf branch and the flag is an unknown argument.
    assert rc == 0, out
    (add,) = _adds(calls)
    # MUTATION: build the name as swarm-tenant-<t>-git and the token lands in
    # the slot every worker of the tenant reads.
    assert add["args"][3] == SECRET
    # MUTATION: keep the paste's trailing newline and every push with it fails.
    assert add["data"] == value
    # MUTATION: echo the value (or `set -x`) and it reaches the output.
    assert value not in out, "the token reached the script's output"
    assert "its value was never printed" in out
    # MUTATION: create the secret here, or bind a reader, and a second
    # container or grant exists outside terraform's authoritative binding.
    assert _writes(calls) == []
    # MUTATION: drop the EXIT trap and the plaintext stays in TMPDIR.
    assert not list(tmp_path.glob("swarm-secret.*"))


def test_a_missing_slot_is_not_created_and_points_at_terraform(tmp_path):
    rc, out, calls = _run(tmp_path, "--git-wf", "--tenant", TENANT, "--stdin", stdin=_value(), existing="")
    assert rc != 0
    # MUTATION: fall through to `gcloud secrets create` (as --search-api-key
    # does) and a non-platform tenant gets a slot terraform never declared.
    assert "terraform/infra/git_tools.tf" in out
    assert not _adds(calls)
    assert _writes(calls) == []


def test_a_value_glued_to_the_flag_is_refused_unprinted(tmp_path):
    value = _value()
    rc, out, calls = _run(tmp_path, f"--git-wf={value}", "--tenant", TENANT, stdin="")
    assert rc != 0
    # MUTATION: drop --git-wf=* from the glued-value refusal and this reads
    # "unknown argument: --git-wf=<the token>", printing it.
    assert value not in out
    assert "--stdin" in out and "unknown argument" not in out
    assert calls == []


def test_a_value_after_the_flag_is_refused_unprinted(tmp_path):
    value = _value()
    rc, out, calls = _run(tmp_path, "--git-wf", value, "--tenant", TENANT, "--stdin", stdin=_value())
    assert rc != 0
    # MUTATION: have --git-wf take an argument ("$2") and the token is read
    # from argv, which ps and shell history expose.
    assert value not in out
    assert "--stdin" in out and "unknown argument" not in out
    assert calls == []


@pytest.mark.parametrize("extra", [("--from-file", "/dev/null"), ("--provider", "git"),
                                   ("--subscription",), ("--disable-previous",), ("--search-api-key",),
                                   ("--github-app", "client-secret")])
def test_only_stdin_and_the_tenant_are_accepted(tmp_path, extra):
    rc, out, calls = _run(tmp_path, "--git-wf", "--tenant", TENANT, "--stdin", *extra, stdin=_value())
    assert rc != 0
    # MUTATION: drop the conflicting-flag refusal and --from-file stores a
    # plaintext copy's contents (or another flag's branch runs instead).
    assert "--git-wf takes only --tenant and --stdin" in out
    assert not _adds(calls)


def test_stdin_is_required(tmp_path):
    rc, out, calls = _run(tmp_path, "--git-wf", "--tenant", TENANT, stdin=_value())
    assert rc != 0
    # MUTATION: default to --from-file or an argument when --stdin is absent.
    assert "--git-wf reads the token from --stdin and nothing else" in out
    assert calls == []


def test_the_tenant_is_required(tmp_path):
    rc, out, calls = _run(tmp_path, "--git-wf", "--stdin", stdin=_value())
    assert rc != 0
    # MUTATION: default the tenant and the slot's name is guessed.
    assert "--git-wf needs --tenant" in out
    assert calls == []


def test_the_generic_tenant_path_refuses_the_slot(tmp_path):
    rc, out, calls = _run(tmp_path, "--tenant", TENANT, "--provider", "git-wf", "--stdin", stdin=_value(), existing="")
    assert rc != 0
    # MUTATION: drop the refusal and the generic path creates the slot as a
    # tenant-credential and sends the operator to bind the tenant's worker.
    assert "--git-wf" in out
    assert not _adds(calls)
    assert _writes(calls) == []


@pytest.mark.parametrize("bad", ["", "\n", "-----" + "BEGIN RSA PRIVATE KEY-----\nAAAA\n", "a b", "x" * 2048])
def test_a_value_that_is_not_one_token_is_refused_unprinted(tmp_path, bad):
    rc, out, calls = _run(tmp_path, "--git-wf", "--tenant", TENANT, "--stdin", stdin=bad)
    assert rc != 0
    # MUTATION: drop the shape check and an empty paste, a PEM, two words or a
    # whole file is stored as the token.
    assert not _adds(calls)
    assert "nothing was stored" in out and "unknown argument" not in out
    if bad.strip():
        assert bad.strip() not in out


def test_terraform_declares_the_slot_empty_and_with_the_same_name():
    tf = GIT_TOOLS_TF.read_text()
    # MUTATION: add a google_secret_manager_secret_version and the token's
    # plaintext lives in state.
    assert not re.search(r'resource\s+"google_secret_manager_secret_version"', tf)
    # MUTATION: spell the id differently on one side and the script adds to a
    # secret nothing reads.
    assert 'secret_id = "swarm-tenant-${var.git_wf_tenant}-git-wf"' in tf
    assert 'GIT_WF_NAME="swarm-tenant-${TENANT}-git-wf"' in SCRIPT.read_text()


def test_the_help_names_the_flag():
    proc = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0
    # MUTATION: drop the usage line and the operator is never told the form.
    assert "--git-wf --tenant" in proc.stdout and "--stdin" in proc.stdout
    assert "set -euo pipefail" not in proc.stdout
