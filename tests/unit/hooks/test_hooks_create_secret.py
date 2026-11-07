"""`scripts/create-secrets.sh --github-app-webhook-secret --stdin`: the GitHub
App's webhook secret, which swarm-github-hooks verifies X-Hub-Signature-256
with (docs/design/graph-service.md §6.2; lane GS-HOOKS).

The value comes from the App's settings page and is typed by the owner: it is
never generated here, never an argument or a file, and never printed. The
script is run for real against a fake `gcloud` on PATH that records what it was
asked (the harness of tests/unit/scripts/test_create_secrets_tailscale_oauth.py).

  * the secret's name is the one terraform/infra/github_hooks.tf creates and
    the receiver reads;
  * the value is stored from stdin, exactly, and never reaches the output;
  * the container is NOT created here and no IAM binding is made (terraform
    owns the container and its one reader, the rollup sweeper account);
  * anything but stdin, a value on the command line, and an empty value are
    refused with nothing stored.

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
SECRET = "swarm-github-app-webhook-secret"
FLAG = "--github-app-webhook-secret"

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
    return secrets.token_hex(20)


def _adds(calls: list[dict]) -> list[dict]:
    return [c for c in calls if c["args"][:3] == ["secrets", "versions", "add"]]


def test_the_name_is_terraforms_and_the_receivers():
    assert f'WEBHOOK_SECRET_NAME="{SECRET}"' in SCRIPT.read_text()
    tf = (ROOT / "terraform" / "infra" / "github_hooks.tf").read_text()
    assert re.search(rf'github_hooks_secret_id\s*=\s*"{SECRET}"', tf)
    assert "secret_id = local.github_hooks_secret_id" in tf
    sys.path.insert(0, str(ROOT / "apps" / "swarm-github-hooks"))
    from swarm_github_hooks.main import SECRET_ID

    assert SECRET_ID == SECRET


def test_the_value_is_stored_from_stdin_exactly_and_never_printed(tmp_path):
    value = _value()
    rc, out, calls = _run(tmp_path, FLAG, "--stdin", stdin=value + "\n")
    assert rc == 0, out
    (add,) = _adds(calls)
    assert add["args"][3] == SECRET and add["data"] == value
    assert value not in out, "the secret reached the script's output"
    assert "its value was never printed" in out
    assert not [c for c in calls if c["args"][:2] in (["secrets", "create"], ["secrets", "add-iam-policy-binding"])]
    assert not list(tmp_path.glob("swarm-secret.*"))


def test_inner_spaces_are_kept(tmp_path):
    """GitHub signs with the secret as typed. Only the trailing newline of the
    paste is dropped; anything else changed would make every signature fail."""
    value = _value() + " " + _value()
    rc, out, calls = _run(tmp_path, FLAG, "--stdin", stdin=value + "\n")
    assert rc == 0, out
    (add,) = _adds(calls)
    assert add["data"] == value


def test_a_missing_container_is_not_created_and_points_at_terraform(tmp_path):
    rc, out, calls = _run(tmp_path, FLAG, "--stdin", stdin=_value(), existing="")
    assert rc != 0
    assert "terraform/infra/github_hooks.tf" in out and "enable_github_hooks" in out
    assert not _adds(calls)
    assert not [c for c in calls if c["args"][:2] == ["secrets", "create"]]


@pytest.mark.parametrize("extra", [("--from-file", "/dev/null"), ("--tenant", "eng"),
                                   ("--provider", "github"), ("--subscription",),
                                   ("--disable-previous",), ("--search-api-key",),
                                   ("--github-app", "client-secret"), ("--graph-ticket-key",)])
def test_only_stdin_is_accepted(tmp_path, extra):
    rc, out, calls = _run(tmp_path, FLAG, "--stdin", *extra, stdin=_value())
    assert rc != 0
    assert not _adds(calls)


def test_stdin_is_required(tmp_path):
    rc, out, calls = _run(tmp_path, FLAG, stdin=_value())
    assert rc != 0 and "--stdin" in out
    assert calls == []


def test_a_value_on_the_command_line_is_refused_unrepeated(tmp_path):
    value = _value()
    rc, out, calls = _run(tmp_path, f"{FLAG}={value}", "--stdin", stdin=_value())
    assert rc != 0
    assert value not in out
    assert calls == []


@pytest.mark.parametrize("stdin", ["\n", "", "x" * 2048])
def test_an_empty_or_implausible_value_is_refused(tmp_path, stdin):
    rc, out, calls = _run(tmp_path, FLAG, "--stdin", stdin=stdin)
    assert rc != 0
    assert not _adds(calls)


def test_the_help_names_the_flag():
    # common.sh has no default project (DAYONE-DESAGA-2), so --help too needs
    # one named; the same one _run names.
    env = {**os.environ, "PROJECT_ID": "swarm-test-project"}
    proc = subprocess.run(["bash", str(SCRIPT), "--help"], env=env, capture_output=True, text=True, timeout=30,
                          check=False)
    assert proc.returncode == 0, proc.stderr
    assert f"{FLAG} --stdin" in proc.stdout
