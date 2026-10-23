"""`scripts/create-secrets.sh` no longer stores a `-git-wf` slot (#2029, WF-APP-TOKEN).

BGD, 2026-10-22: the `-git-wf` slot and IAM row L-2 are removed. A
workflow-file push uses a token the SwarmCloud GitHub App mints per push, for
a repository whose `workflow_files` switch is on, so there is nothing to
store. What this holds, and nothing else:

  * `--git-wf` (bare, or with a value glued to it) is refused as an unknown
    argument, a glued value is never repeated, and gcloud is never asked to
    store anything;
  * `--provider git-wf` is refused with a sentence that says the slot no
    longer exists and points to the App.

The script is run for real against a fake `gcloud` on PATH that records what
it was asked. Every fake value is built at runtime.
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


def _stores(calls: list[dict]) -> list[dict]:
    return [c for c in calls if c["args"][:2] in (["secrets", "create"], ["secrets", "versions"],
                                                  ["secrets", "add-iam-policy-binding"],
                                                  ["secrets", "set-iam-policy"])]


def test_git_wf_is_an_unknown_argument_and_nothing_is_stored(tmp_path):
    value = _value()
    rc, out, calls = _run(tmp_path, "--git-wf", "--tenant", TENANT, "--stdin", stdin=value + "\n")
    # MUTATION: keep the --git-wf branch and this stores a version (rc 0).
    assert rc != 0, out
    assert "unknown argument: --git-wf" in out
    assert "no longer exists" in out and "GitHub App" in out
    assert _stores(calls) == []
    assert value not in out


def test_a_value_glued_to_the_removed_flag_is_refused_unprinted(tmp_path):
    value = _value()
    rc, out, calls = _run(tmp_path, f"--git-wf={value}", "--tenant", TENANT)
    # MUTATION: let it fall to the generic `-*` arm and the value is printed.
    assert rc != 0
    assert value not in out
    assert "unknown argument: --git-wf" in out
    assert _stores(calls) == []


def test_provider_git_wf_says_the_slot_no_longer_exists_and_points_to_the_app(tmp_path):
    rc, out, calls = _run(tmp_path, "--tenant", TENANT, "--provider", "git-wf", "--stdin",
                          stdin=_value(), existing="")
    # MUTATION: drop the git-wf refusal and the generic path creates the secret.
    assert rc != 0, out
    assert "no longer exists" in out and "GitHub App" in out
    assert _stores(calls) == []
