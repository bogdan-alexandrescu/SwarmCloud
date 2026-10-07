"""docs/runbooks/github-app.md registers the SwarmCloud GitHub App exactly as
the Terraform and the scripts expect (docs/onboarding.md §3.4, lane OB2 of
#780).

The runbook is what the owner follows by hand, once, at GitHub; the Terraform
declares the slots its secrets land in, and `scripts/create-secrets.sh
--github-app` is the only way a value reaches them. Three places, one set of
names, so these tests hold them together:

  * every App secret slot Terraform declares is named in the runbook, and the
    runbook names no slot Terraform does not declare;
  * every command in the runbook that stores a value reads it from stdin, never
    a file argument, and the script accepts exactly the slots Terraform
    declares, refuses anything else, and refuses to create a slot Terraform
    should have made;
  * the registration settings the design fixes are in the runbook: the callback
    URL on the console host, user authorisation during installation, expiring
    user tokens, the D8 permission set with no workflows write, no webhook;
  * the non-secret settings land in tfvars variables that exist.

The script is run for real with a fake `gcloud` on PATH that records what it
was asked to do. A value is built at runtime, never written here as a literal
(the worker's publish scan refuses a credential-shaped added line).
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

REPO = Path(__file__).resolve().parents[3]
RUNBOOK = REPO / "docs" / "runbooks" / "github-app.md"
SECRET_MODULE = REPO / "terraform" / "modules" / "secret_manager"
INFRA = REPO / "terraform" / "infra"
DEV_TFVARS = REPO / "terraform" / "environments" / "dev" / "dev.tfvars"
SCRIPT = REPO / "scripts" / "create-secrets.sh"

SLOT_RE = re.compile(r"swarm-github-app-[a-z][a-z-]*[a-z]")


def _declared_slot_words() -> list[str]:
    """The App slot words modules/secret_manager declares, read from its HCL."""
    text = "\n".join(p.read_text() for p in sorted(SECRET_MODULE.glob("*.tf")))
    lists = re.findall(r"github_app_slot_words\s*=\s*\[([^\]]*)\]", text)
    assert len(lists) == 1, "modules/secret_manager no longer declares one github_app_slot_words list"
    assert 'slot => "swarm-github-app-${slot}"' in text, "the App secret ids are no longer swarm-github-app-<slot>"
    return re.findall(r'"([a-z-]+)"', lists[0])


def _declared_slots() -> set[str]:
    """The App secret ids modules/secret_manager declares."""
    return {f"swarm-github-app-{word}" for word in _declared_slot_words()}


def _runbook() -> str:
    assert RUNBOOK.exists(), "docs/runbooks/github-app.md does not exist"
    return RUNBOOK.read_text()


def test_terraform_declares_the_two_app_slots() -> None:
    # The control for the runbook comparison below: an empty scan would make
    # "the runbook names every slot" pass over nothing.
    assert _declared_slots() == {
        "swarm-github-app-client-secret",
        "swarm-github-app-private-key",
    }


def test_the_runbook_names_every_slot_terraform_declares_and_no_other() -> None:
    named = set(SLOT_RE.findall(_runbook()))
    declared = _declared_slots()
    assert declared <= named, f"runbook does not name {sorted(declared - named)}"
    assert named <= declared, f"runbook names slots Terraform does not declare: {sorted(named - declared)}"


def _store_commands(text: str) -> list[str]:
    """The runbook's command lines that store an App secret (prose about the flag is not a command)."""
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("scripts/create-secrets.sh") and "--github-app" in line
    ]


def test_every_store_command_in_the_runbook_reads_stdin() -> None:
    commands = _store_commands(_runbook())
    slots = {m.group(1) for c in commands for m in [re.search(r"--github-app\s+([a-z-]+)", c)] if m}
    assert slots == {"client-secret", "private-key"}, f"runbook store commands cover {sorted(slots)}"
    for command in commands:
        assert "--stdin" in command, f"store command without --stdin: {command}"
        assert "--from-file" not in command, f"store command naming a file argument: {command}"


@pytest.mark.parametrize(
    "phrase",
    [
        "https://swarm.saga.xyz/onboarding/github/callback",
        "Request user authorization (OAuth) during installation",
        "Expire user authorization tokens",
        "Contents: Read and write",
        "Pull requests: Read and write",
        "Issues: Read and write",
        "Checks: Read-only",
        "Metadata: Read-only",
        "Workflows: No access",
        "Webhook",
        "github_app_client_id",
        "enable_forge_user_slots",
        "enable_forge_refresh",
        "swarm-forge-refresh",
        "swarmForgeSlotCreator",
    ],
)
def test_the_runbook_states_each_fixed_setting(phrase: str) -> None:
    assert phrase in _runbook(), f"runbook does not say {phrase!r}"


def test_the_runbook_turns_the_webhook_off() -> None:
    text = _runbook()
    assert re.search(r"(?i)webhook[^\n]*\bactive\b[^\n]*\b(off|unticked|untick|clear)", text), (
        "the runbook must say to untick the webhook's Active box: the design needs no webhook"
    )


def test_the_tfvars_the_runbook_names_exist() -> None:
    variables = (INFRA / "variables.tf").read_text() + "\n".join(
        p.read_text() for p in sorted(INFRA.glob("*.tf"))
    )
    for name in ("enable_github_app", "github_app_id", "github_app_client_id", "github_app_slug", "enable_forge_refresh"):
        assert f'variable "{name}"' in variables, f"terraform/infra declares no variable {name}"
        assert name in _runbook(), f"runbook does not name the tfvars value {name}"
    dev = DEV_TFVARS.read_text()
    assert re.search(r"(?m)^github_app_client_id\s*=", dev), "dev.tfvars carries no github_app_client_id line"


def test_the_runbook_carries_nothing_credential_shaped() -> None:
    text = _runbook()
    for prefix in ("ghp_", "gho_", "ghu_", "ghr_", "ghs_", "github_pat_"):
        assert re.search(re.escape(prefix) + r"[A-Za-z0-9_]{8,}", text) is None, f"runbook carries a {prefix} token shape"
    assert "BEGIN RSA" not in text and re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", text) is None


# ---------------------------------------------------------------------------
# scripts/create-secrets.sh --github-app, against a fake gcloud.
# ---------------------------------------------------------------------------

FAKE_GCLOUD = r"""#!{python}
import json, os, sys
args = sys.argv[1:]
log = os.environ["FAKE_GCLOUD_LOG"]
entry = {"args": args}
data_file = next((a.split("=", 1)[1] for a in args if a.startswith("--data-file=")), None)
if data_file:
    entry["data"] = open(data_file).read()
with open(log, "a") as fh:
    fh.write(json.dumps(entry) + "\n")
existing = set(filter(None, os.environ.get("FAKE_EXISTING", "").split(",")))
if args[:2] == ["secrets", "describe"]:
    if args[2] in existing:
        print("projects/1/secrets/" + args[2])
        sys.exit(0)
    sys.stderr.write("NOT_FOUND: Secret [" + args[2] + "] not found\n")
    sys.exit(1)
if args[:3] == ["secrets", "versions", "add"]:
    print("projects/1/secrets/" + args[3] + "/versions/7")
    sys.exit(0)
sys.stderr.write("unexpected gcloud call: " + " ".join(args) + "\n")
sys.exit(3)
"""

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="create-secrets.sh needs bash")


def _run(tmp_path: Path, *args: str, stdin: str = "", existing: str = "") -> tuple[int, str, list[dict]]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gcloud = bindir / "gcloud"
    gcloud.write_text(FAKE_GCLOUD.replace("{python}", sys.executable))
    gcloud.chmod(0o755)
    log = tmp_path / "gcloud.jsonl"
    log.write_text("")
    env = {k: v for k, v in os.environ.items() if k not in ("SWARM_ASSUME_YES", "PROJECT_ID", "ENVIRONMENT")}
    env.update(
        {
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
        }
    )
    proc = subprocess.run(
        ["bash", str(SCRIPT), *args],
        input=stdin,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    return proc.returncode, proc.stdout + proc.stderr, calls


SLOTS = {"client-secret": "swarm-github-app-client-secret", "private-key": "swarm-github-app-private-key"}


def _fake_value(slot: str) -> str:
    if slot == "private-key":
        marker = "PRIVATE " + "KEY"
        return f"-----BEGIN {marker}-----\n{secrets.token_hex(48)}\n-----END {marker}-----\n"
    return secrets.token_hex(20)


@pytest.mark.parametrize("slot", sorted(SLOTS))
def test_the_script_adds_a_version_to_the_terraform_slot_from_stdin(tmp_path: Path, slot: str) -> None:
    value = _fake_value(slot)
    rc, out, calls = _run(tmp_path, "--github-app", slot, "--stdin", stdin=value, existing=SLOTS[slot])
    assert rc == 0, out
    adds = [c for c in calls if c["args"][:3] == ["secrets", "versions", "add"]]
    assert len(adds) == 1 and adds[0]["args"][3] == SLOTS[slot]
    # One trailing newline is stripped, as for every other value.
    assert adds[0]["data"] == value.rstrip("\n")
    assert value.strip() not in out, "the value reached the script's output"
    assert not any(c["args"][:2] == ["secrets", "create"] for c in calls)


def test_the_script_never_creates_an_app_slot(tmp_path: Path) -> None:
    rc, out, calls = _run(tmp_path, "--github-app", "client-secret", "--stdin", stdin=_fake_value("client-secret"))
    assert rc != 0
    assert "terraform" in out.lower()
    assert not any(c["args"][:2] == ["secrets", "create"] for c in calls)
    assert not any(c["args"][:3] == ["secrets", "versions", "add"] for c in calls)


@pytest.mark.parametrize(
    "extra",
    [
        ("--from-file", "/dev/null"),
        ("--tenant", "eng"),
        ("--provider", "git"),
        ("--subscription",),
        ("--disable-previous",),
    ],
)
def test_the_script_refuses_anything_but_stdin_for_an_app_slot(tmp_path: Path, extra: tuple[str, ...]) -> None:
    rc, out, calls = _run(
        tmp_path, "--github-app", "client-secret", "--stdin", *extra,
        stdin=_fake_value("client-secret"), existing=SLOTS["client-secret"],
    )
    assert rc != 0, out
    assert not any(c["args"][:3] == ["secrets", "versions", "add"] for c in calls)


def test_the_script_requires_stdin_for_an_app_slot(tmp_path: Path) -> None:
    rc, out, calls = _run(tmp_path, "--github-app", "client-secret", existing=SLOTS["client-secret"])
    assert rc != 0 and "--stdin" in out
    assert not any(c["args"][:3] == ["secrets", "versions", "add"] for c in calls)


def test_the_script_refuses_an_unknown_app_slot(tmp_path: Path) -> None:
    rc, out, calls = _run(tmp_path, "--github-app", "webhook-secret", "--stdin", stdin="x" * 20)
    assert rc != 0 and "client-secret" in out and "private-key" in out
    assert calls == []


def test_the_script_refuses_a_private_key_that_is_not_pem(tmp_path: Path) -> None:
    rc, out, calls = _run(
        tmp_path, "--github-app", "private-key", "--stdin",
        stdin=secrets.token_hex(40), existing=SLOTS["private-key"],
    )
    assert rc != 0 and "PEM" in out
    assert not any(c["args"][:3] == ["secrets", "versions", "add"] for c in calls)


def test_the_script_accepts_exactly_the_slots_terraform_declares() -> None:
    text = SCRIPT.read_text()
    arms = re.findall(r"^\s*([a-z|-]+)\)\s*;;\s*$", text, re.M)
    accepted = {word for arm in arms if "client-secret" in arm for word in arm.split("|")}
    assert accepted == set(_declared_slot_words()) == set(SLOTS)
    assert 'APP_SLOT_ID="swarm-github-app-${GITHUB_APP_SLOT}"' in text
