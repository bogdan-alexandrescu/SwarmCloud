"""`scripts/create-secrets.sh --tailscale-oauth`: the Tailscale OAuth client
secret the decision-api tailnet egress logs in with (lane JUDGE-NET;
docs/runbooks/tailscale-egress.md).

The script is run for real against a fake `gcloud` on PATH that records what
it was asked (the harness of test_create_secrets_search_key.py). What it holds:

  * the secret's name is the one terraform/infra/tailscale.tf creates and
    kubernetes/apply.sh reads;
  * the value comes from stdin only, never a file or an argument, and never
    reaches the script's output;
  * the secret is NOT created here (terraform owns the container and its one
    reader) and no IAM binding is made here;
  * a value that is not an OAuth client secret is refused, unprinted.

Every fake value is built at runtime.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "create-secrets.sh"
SECRET = "swarm-tailscale-oauth"

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
    return "tskey-" + "client-" + secrets.token_hex(6) + "-" + secrets.token_hex(20)


def _adds(calls: list[dict]) -> list[dict]:
    return [c for c in calls if c["args"][:3] == ["secrets", "versions", "add"]]


def test_the_name_is_terraforms_and_apply_shs():
    assert f'TS_OAUTH_NAME="{SECRET}"' in SCRIPT.read_text()
    assert f'secret_id = "{SECRET}"' in (ROOT / "terraform" / "infra" / "tailscale.tf").read_text()
    assert f'TAILSCALE_OAUTH_GSM_NAME="{SECRET}"' in (ROOT / "kubernetes" / "apply.sh").read_text()


def test_the_value_is_stored_from_stdin_and_never_printed(tmp_path):
    value = _value()
    rc, out, calls = _run(tmp_path, "--tailscale-oauth", "--stdin", stdin=value + "\n")
    assert rc == 0, out
    (add,) = _adds(calls)
    assert add["args"][3] == SECRET and add["data"] == value
    assert value not in out, "the secret reached the script's output"
    assert "its value was never printed" in out
    # Terraform owns the container and its reader: nothing is created or bound here.
    assert not [c for c in calls if c["args"][:2] in (["secrets", "create"], ["secrets", "add-iam-policy-binding"])]
    # The private temp directory is gone.
    assert not list(tmp_path.glob("swarm-secret.*"))


def test_a_missing_container_is_not_created_and_points_at_terraform(tmp_path):
    rc, out, calls = _run(tmp_path, "--tailscale-oauth", "--stdin", stdin=_value(), existing="")
    assert rc != 0
    assert "terraform/infra/tailscale.tf" in out
    assert not _adds(calls)
    assert not [c for c in calls if c["args"][:2] == ["secrets", "create"]]


@pytest.mark.parametrize("extra", [("--from-file", "/dev/null"), ("--tenant", "eng"),
                                   ("--provider", "tailscale"), ("--subscription",),
                                   ("--disable-previous",), ("--search-api-key",)])
def test_only_stdin_is_accepted(tmp_path, extra):
    rc, out, calls = _run(tmp_path, "--tailscale-oauth", "--stdin", *extra, stdin=_value())
    assert rc != 0
    assert not _adds(calls)


def test_stdin_is_required(tmp_path):
    rc, out, calls = _run(tmp_path, "--tailscale-oauth", stdin=_value())
    assert rc != 0 and "--stdin" in out
    assert calls == []


def test_an_empty_value_is_refused(tmp_path):
    rc, out, calls = _run(tmp_path, "--tailscale-oauth", "--stdin", stdin="\n")
    assert rc != 0
    assert not _adds(calls)


@pytest.mark.parametrize("prefix", ["tskey-" + "auth-", "tskey-" + "api-", ""])
def test_a_value_that_is_not_an_oauth_client_secret_is_refused_unprinted(tmp_path, prefix):
    wrong = prefix + secrets.token_hex(20)
    rc, out, calls = _run(tmp_path, "--tailscale-oauth", "--stdin", stdin=wrong)
    assert rc != 0
    assert "not a Tailscale OAuth client secret" in out
    assert wrong not in out
    assert not _adds(calls)


def test_the_help_names_the_flag():
    proc = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0
    assert "--tailscale-oauth --stdin" in proc.stdout
    assert "set -euo pipefail" not in proc.stdout
