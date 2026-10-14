"""`scripts/create-secrets.sh --egress-key` and `--egress-credential`: the two
browser-egress platform secrets (docs/browser-egress.md §9.3; lane
EGRESS-BUILD, part of #1030).

Section 9 recorded that no create-secrets.sh slot existed for either, so both
version-1 values were added with bare `gcloud secrets versions add`. The
script is run for real against a fake `gcloud` on PATH that records what it
was asked. What it holds:

  * a value is NEVER accepted on the command line: `--egress-credential VALUE`,
    `--egress-credential=VALUE` and `--egress-key=VALUE` are refused, nothing
    is stored, and the value is not repeated in the refusal;
  * the vendor credential comes from stdin only (never --from-file), is the
    JSON object apps/swarm-egress/swarm_egress/vendor.py parses, and never
    reaches the script's output;
  * the ticket key is generated, never read and never shown;
  * neither secret is created here (terraform/infra/egress.tf owns both and
    their readers): a missing one is refused;
  * a secret that already has an enabled version -- both do since 2026-10-14
    -- is only added to after a TYPED confirmation, the secret's own name, and
    SWARM_ASSUME_YES does not stand in for it.

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
CREDENTIAL = "swarm-egress-credential"
KEY = "swarm-egress-key"

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
enabled = set(filter(None, os.environ.get("FAKE_ENABLED", "").split(",")))
if args[:2] == ["secrets", "describe"]:
    if args[2] in existing:
        print("projects/1/secrets/" + args[2])
        sys.exit(0)
    sys.stderr.write("NOT_FOUND: Secret [" + args[2] + "] not found\n")
    sys.exit(1)
if args[:3] == ["secrets", "versions", "list"]:
    if args[3] in enabled:
        print("projects/1/secrets/" + args[3] + "/versions/1")
    sys.exit(0)
if args[:3] == ["secrets", "versions", "add"]:
    print("projects/1/secrets/" + args[3] + "/versions/2")
    sys.exit(0)
sys.stderr.write("unexpected gcloud call: " + " ".join(args) + "\n")
sys.exit(3)
"""

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="create-secrets.sh needs bash")


def _run(tmp_path: Path, *args: str, stdin: str = "", existing: str = f"{CREDENTIAL},{KEY}",
         enabled: str = "", assume_yes: bool = False) -> tuple[int, str, list[dict]]:
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
        "FAKE_ENABLED": enabled,
    })
    if assume_yes:
        env["SWARM_ASSUME_YES"] = "1"
    # start_new_session: no controlling terminal, so /dev/tty cannot answer a
    # prompt -- as in CI, and as when the value is piped in.
    proc = subprocess.run(["bash", str(SCRIPT), *args], input=stdin, env=env, start_new_session=True,
                          capture_output=True, text=True, timeout=60, check=False)
    calls = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    return proc.returncode, proc.stdout + proc.stderr, calls


def _password() -> str:
    return "pw" + secrets.token_hex(12)


def _credential(password: str) -> str:
    return json.dumps({
        "vendor": "brightdata", "host": "brd.superproxy.io", "port": 33335, "tls": False,
        "zones": {"residential": {"username": "brd-customer-x-zone-residential",
                                  "password": password, "sticky": "attempt"}},
    })


def _adds(calls: list[dict]) -> list[dict]:
    return [c for c in calls if c["args"][:3] == ["secrets", "versions", "add"]]


# --- never on the command line ----------------------------------------------------


@pytest.mark.parametrize("argv", [
    ("--egress-credential", "{VALUE}"),
    ("--egress-credential={VALUE}",),
    ("--egress-credential", "--stdin", "{VALUE}"),
    ("--egress-key={VALUE}",),
    ("--egress-key", "{VALUE}"),
])
def test_a_value_on_the_command_line_is_refused_and_not_repeated(tmp_path, argv):
    value = _password()
    rc, out, calls = _run(tmp_path, *(a.replace("{VALUE}", value) for a in argv), stdin=_credential(_password()))
    assert rc != 0, out
    assert _adds(calls) == []
    assert value not in out, "the refused argv value was echoed back"


def test_the_credential_is_never_read_from_a_file(tmp_path):
    path = tmp_path / "credential.json"
    path.write_text(_credential(_password()))
    rc, out, calls = _run(tmp_path, "--egress-credential", "--from-file", str(path))
    assert rc != 0
    assert _adds(calls) == []


def test_the_credential_needs_stdin(tmp_path):
    rc, out, calls = _run(tmp_path, "--egress-credential", stdin=_credential(_password()))
    assert rc != 0 and "--stdin" in out
    assert _adds(calls) == []


# --- the vendor credential --------------------------------------------------------


def test_a_first_credential_is_stored_from_stdin_and_never_shown(tmp_path):
    password = _password()
    value = _credential(password)
    rc, out, calls = _run(tmp_path, "--egress-credential", "--stdin", stdin=value + "\n")
    assert rc == 0, out
    adds = _adds(calls)
    assert len(adds) == 1 and adds[0]["args"][3] == CREDENTIAL and adds[0]["data"] == value
    assert password not in out and "brd-customer" not in out
    assert not any(c["args"][:2] == ["secrets", "create"] for c in calls), "terraform owns the container"


@pytest.mark.parametrize("value", [
    "not json",
    json.dumps(["a", "list"]),
    json.dumps({"vendor": "brightdata", "zones": {}}),
    json.dumps({"vendor": "brightdata", "zones": {"residential": {}}, "password": "x"}),
])
def test_a_value_that_is_not_the_credential_object_is_refused(tmp_path, value):
    rc, out, calls = _run(tmp_path, "--egress-credential", "--stdin", stdin=value)
    assert rc != 0, out
    assert _adds(calls) == []


def test_a_missing_container_is_refused_not_created(tmp_path):
    rc, out, calls = _run(tmp_path, "--egress-credential", "--stdin", stdin=_credential(_password()), existing="")
    assert rc != 0
    assert "terraform/infra/egress.tf" in out
    assert _adds(calls) == [] and not any(c["args"][:2] == ["secrets", "create"] for c in calls)


# --- the ticket key ---------------------------------------------------------------


def test_a_first_key_is_generated_and_never_shown(tmp_path):
    rc, out, calls = _run(tmp_path, "--egress-key")
    assert rc == 0, out
    adds = _adds(calls)
    assert len(adds) == 1 and adds[0]["args"][3] == KEY
    # swarm_egress.main.MIN_KEY_BYTES is 32.
    assert len(adds[0]["data"]) >= 32
    assert adds[0]["data"] not in out


@pytest.mark.parametrize("extra", [("--stdin",), ("--from-file", "/dev/null"), ("--tenant", "eng"),
                                   ("--disable-previous",)])
def test_the_key_takes_no_value_and_no_tenant(tmp_path, extra):
    rc, out, calls = _run(tmp_path, "--egress-key", *extra)
    assert rc != 0
    assert _adds(calls) == []


# --- overwriting: a typed confirmation --------------------------------------------


@pytest.mark.parametrize("flag, name, stdin", [
    ("--egress-credential", CREDENTIAL, "credential"),
    ("--egress-key", KEY, ""),
])
def test_a_secret_with_an_enabled_version_is_not_added_to_without_a_typed_confirmation(tmp_path, flag, name, stdin):
    value = _credential(_password()) if stdin else ""
    args = (flag, "--stdin") if stdin else (flag,)
    # SWARM_ASSUME_YES is not a typed confirmation.
    rc, out, calls = _run(tmp_path, *args, stdin=value, enabled=name, assume_yes=True)
    assert rc != 0, out
    assert _adds(calls) == []
    assert f"--overwrite {name}" in out, out


@pytest.mark.parametrize("flag, name, stdin", [
    ("--egress-credential", CREDENTIAL, "credential"),
    ("--egress-key", KEY, ""),
])
def test_the_wrong_name_is_not_a_confirmation(tmp_path, flag, name, stdin):
    other = KEY if name == CREDENTIAL else CREDENTIAL
    args = (flag, "--stdin") if stdin else (flag,)
    rc, out, calls = _run(tmp_path, *args, "--overwrite", other,
                          stdin=_credential(_password()) if stdin else "", enabled=name)
    assert rc != 0, out
    assert _adds(calls) == []


@pytest.mark.parametrize("flag, name, stdin", [
    ("--egress-credential", CREDENTIAL, "credential"),
    ("--egress-key", KEY, ""),
])
def test_the_secrets_own_name_confirms_a_new_version(tmp_path, flag, name, stdin):
    args = (flag, "--stdin") if stdin else (flag,)
    rc, out, calls = _run(tmp_path, *args, "--overwrite", name,
                          stdin=_credential(_password()) if stdin else "", enabled=name)
    assert rc == 0, out
    adds = _adds(calls)
    assert len(adds) == 1 and adds[0]["args"][3] == name


def test_overwrite_is_only_for_the_egress_secrets(tmp_path):
    rc, out, calls = _run(tmp_path, "--tenant", "eng", "--provider", "anthropic", "--stdin",
                          "--overwrite", "swarm-tenant-eng-anthropic", stdin="sk-" + "x" * 40)
    assert rc != 0
    assert _adds(calls) == []


def test_help_names_both_slots():
    proc = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                          env={**os.environ, "SWARM_ENV_FILE": "/nonexistent", "NO_COLOR": "1"}, check=False)
    assert proc.returncode == 0, proc.stderr
    for needle in ("--egress-key", "--egress-credential --stdin", "--overwrite", CREDENTIAL, KEY):
        assert needle in proc.stdout, needle
