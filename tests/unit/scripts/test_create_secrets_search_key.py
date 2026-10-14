"""`scripts/create-secrets.sh --search-api-key`: the search tool's platform key
(docs/search-tool.md; lane SEARCH-TOOL, part of #1030).

The script is run for real against a fake `gcloud` on PATH that records what
it was asked (the harness of test_github_app_runbook.py, extended with the
create and IAM calls this mode makes). What it holds:

  * the secret's name is swarm-api's SEARCH_API_KEY_SECRET default;
  * the value comes from stdin only, never a file or an argument, and never
    reaches the script's output;
  * the secret is created when missing, region-only, and swarm-api -- and
    nobody else -- is granted secretAccessor on it;
  * a binding that fails is a failed run, not "stored".

Every fake key is built at runtime.
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
SECRET = "swarm-search-api-key"

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
if args[:2] == ["secrets", "create"]:
    sys.exit(0)
if args[:3] == ["secrets", "versions", "add"]:
    print("projects/1/secrets/" + args[3] + "/versions/3")
    sys.exit(0)
if args[:2] == ["secrets", "add-iam-policy-binding"]:
    if os.environ.get("FAKE_IAM_FAILS"):
        sys.stderr.write("PERMISSION_DENIED: setIamPolicy\n")
        sys.exit(1)
    sys.exit(0)
sys.stderr.write("unexpected gcloud call: " + " ".join(args) + "\n")
sys.exit(3)
"""

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="create-secrets.sh needs bash")


def _run(tmp_path: Path, *args: str, stdin: str = "", existing: str = "",
         iam_fails: bool = False) -> tuple[int, str, list[dict]]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gcloud = bindir / "gcloud"
    gcloud.write_text(FAKE_GCLOUD.replace("{python}", sys.executable))
    gcloud.chmod(0o755)
    log = tmp_path / "gcloud.jsonl"
    log.write_text("")
    env = {k: v for k, v in os.environ.items()
           if k not in ("SWARM_ASSUME_YES", "PROJECT_ID", "ENVIRONMENT", "FAKE_IAM_FAILS")}
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
    if iam_fails:
        env["FAKE_IAM_FAILS"] = "1"
    proc = subprocess.run(["bash", str(SCRIPT), *args], input=stdin, env=env,
                          capture_output=True, text=True, timeout=60, check=False)
    calls = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    return proc.returncode, proc.stdout + proc.stderr, calls


def _key() -> str:
    return "BSA" + secrets.token_hex(14)


def test_the_name_is_swarm_apis_default():
    from swarm_api.settings import ApiSettings  # imported here: tests/unit/scripts must collect without the service stack

    assert ApiSettings.__dataclass_fields__["search_api_key_secret"].default == SECRET
    assert f'SEARCH_SECRET="{SECRET}"' in SCRIPT.read_text()


def test_a_missing_secret_is_created_stored_and_granted_to_swarm_api_alone(tmp_path):
    value = _key()
    rc, out, calls = _run(tmp_path, "--search-api-key", "--stdin", stdin=value + "\n")
    assert rc == 0, out
    creates = [c for c in calls if c["args"][:2] == ["secrets", "create"]]
    assert len(creates) == 1 and creates[0]["args"][2] == SECRET
    assert "--replication-policy=user-managed" in creates[0]["args"]
    assert "--locations=us-central1" in creates[0]["args"]
    adds = [c for c in calls if c["args"][:3] == ["secrets", "versions", "add"]]
    assert len(adds) == 1 and adds[0]["args"][3] == SECRET and adds[0]["data"] == value
    grants = [c for c in calls if c["args"][:2] == ["secrets", "add-iam-policy-binding"]]
    assert len(grants) == 1
    assert ("--member=serviceAccount:swarm-api@swarm-test-project.iam.gserviceaccount.com"
            in grants[0]["args"])
    assert "--role=roles/secretmanager.secretAccessor" in grants[0]["args"]
    assert value not in out, "the key reached the script's output"


def test_an_existing_secret_gets_a_version_and_is_not_recreated(tmp_path):
    rc, out, calls = _run(tmp_path, "--search-api-key", "--stdin", stdin=_key(), existing=SECRET)
    assert rc == 0, out
    assert not any(c["args"][:2] == ["secrets", "create"] for c in calls)
    assert sum(c["args"][:3] == ["secrets", "versions", "add"] for c in calls) == 1


@pytest.mark.parametrize("extra", [("--from-file", "/dev/null"), ("--tenant", "eng"),
                                   ("--provider", "brave"), ("--subscription",),
                                   ("--disable-previous",)])
def test_only_stdin_is_accepted(tmp_path, extra):
    rc, out, calls = _run(tmp_path, "--search-api-key", "--stdin", *extra, stdin=_key())
    assert rc != 0
    assert not any(c["args"][:3] == ["secrets", "versions", "add"] for c in calls)


def test_stdin_is_required(tmp_path):
    rc, out, calls = _run(tmp_path, "--search-api-key", stdin=_key())
    assert rc != 0 and "--stdin" in out
    assert calls == []


def test_an_empty_value_is_refused(tmp_path):
    rc, out, calls = _run(tmp_path, "--search-api-key", "--stdin", stdin="\n")
    assert rc != 0
    assert not any(c["args"][:3] == ["secrets", "versions", "add"] for c in calls)


def test_a_failed_grant_is_a_failed_run(tmp_path):
    value = _key()
    rc, out, _ = _run(tmp_path, "--search-api-key", "--stdin", stdin=value, iam_fails=True)
    assert rc != 0
    assert "search_unconfigured" in out
    assert value not in out
