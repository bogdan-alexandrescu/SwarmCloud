"""`scripts/create-secrets.sh --join-person --stdin`: the join acceptance
probe's person credential, `swarm-accept-join-person`
(docs/design/join-requests.md section 8; terraform/infra/acceptance_join.tf).

A copy of the --egress-credential path, run for real against a fake `gcloud`
on PATH that records what it was asked. What it holds:

  * a value is NEVER accepted on the command line, and a refused one is not
    repeated;
  * the credential comes from stdin only (never --from-file), and nothing of
    it -- refresh token, client id, client secret -- reaches the output, on
    success or on refusal;
  * the shape check: a JSON object with `type` "authorized_user" and a
    non-empty `client_id` and `refresh_token`, else nothing is stored;
  * the secret is never created here: a missing one is refused;
  * a secret with an enabled version is only added to after a TYPED
    confirmation, its own name, and SWARM_ASSUME_YES does not stand in for it.

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
SECRET = "swarm-accept-join-person"

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


def _run(tmp_path: Path, *args: str, stdin: str = "", existing: str = SECRET,
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


def _fields() -> dict[str, str]:
    return {
        "client_id": "cid" + secrets.token_hex(10) + ".apps.example.com",
        "client_secret": "cs" + secrets.token_hex(12),
        "refresh_token": "rt" + secrets.token_hex(24),
    }


def _credential(fields: dict[str, str], **extra) -> str:
    return json.dumps({"type": "authorized_user", **fields, **extra})


def _adds(calls: list[dict]) -> list[dict]:
    return [c for c in calls if c["args"][:3] == ["secrets", "versions", "add"]]


def _assert_nothing_echoed(out: str, fields: dict[str, str]) -> None:
    for name, value in fields.items():
        assert value not in out, f"{name} reached the output"


# --- never on the command line ----------------------------------------------------


@pytest.mark.parametrize("argv", [
    ("--join-person", "{VALUE}"),
    ("--join-person={VALUE}",),
    ("--join-person", "--stdin", "{VALUE}"),
])
def test_a_value_on_the_command_line_is_refused_and_not_repeated(tmp_path, argv):
    value = "rt" + secrets.token_hex(24)
    rc, out, calls = _run(tmp_path, *(a.replace("{VALUE}", value) for a in argv), stdin=_credential(_fields()))
    assert rc != 0, out
    assert _adds(calls) == []
    assert value not in out, "the refused argv value was echoed back"


def test_the_credential_is_never_read_from_a_file(tmp_path):
    path = tmp_path / "authorized_user.json"
    path.write_text(_credential(_fields()))
    rc, out, calls = _run(tmp_path, "--join-person", "--from-file", str(path))
    assert rc != 0
    assert _adds(calls) == []


def test_the_credential_needs_stdin(tmp_path):
    rc, out, calls = _run(tmp_path, "--join-person", stdin=_credential(_fields()))
    assert rc != 0 and "--stdin" in out
    assert _adds(calls) == []


@pytest.mark.parametrize("extra", [("--tenant", "eng"), ("--provider", "anthropic"), ("--disable-previous",),
                                   ("--egress-credential",), ("--settings-mac-key",)])
def test_it_takes_no_tenant_and_no_other_slot(tmp_path, extra):
    rc, out, calls = _run(tmp_path, "--join-person", "--stdin", *extra, stdin=_credential(_fields()))
    assert rc != 0, out
    assert _adds(calls) == []


# --- storing it, and printing none of it ------------------------------------------


def test_a_first_credential_is_stored_from_stdin_and_nothing_is_echoed(tmp_path):
    fields = _fields()
    value = _credential(fields)
    rc, out, calls = _run(tmp_path, "--join-person", "--stdin", stdin=value + "\n")
    assert rc == 0, out
    adds = _adds(calls)
    assert len(adds) == 1 and adds[0]["args"][3] == SECRET and adds[0]["data"] == value
    _assert_nothing_echoed(out, fields)
    assert not any(c["args"][:2] == ["secrets", "create"] for c in calls), "terraform owns the container"


def test_the_value_reaches_gcloud_only_as_a_file_never_as_an_argument(tmp_path):
    fields = _fields()
    rc, out, calls = _run(tmp_path, "--join-person", "--stdin", stdin=_credential(fields))
    assert rc == 0, out
    for call in calls:
        joined = " ".join(call["args"])
        for value in fields.values():
            assert value not in joined
    # The private temp directory is gone on exit.
    assert not list(tmp_path.glob("swarm-secret.*"))


# --- the shape check --------------------------------------------------------------


def _shape_cases() -> list:
    f = _fields()
    no_client = {k: v for k, v in f.items() if k != "client_id"}
    no_refresh = {k: v for k, v in f.items() if k != "refresh_token"}
    return [
        pytest.param("not json " + f["refresh_token"], f, id="not-json"),
        pytest.param(json.dumps([f["refresh_token"]]), f, id="a-list"),
        pytest.param(json.dumps({"type": "service_account", **f}), f, id="service-account"),
        pytest.param(json.dumps(f), f, id="no-type"),
        pytest.param(_credential(no_client), f, id="no-client-id"),
        pytest.param(_credential(no_refresh), f, id="no-refresh-token"),
        pytest.param(_credential({**f, "refresh_token": " "}), f, id="blank-refresh-token"),
        pytest.param(_credential({**f, "client_id": 7}), f, id="client-id-not-a-string"),
    ]


@pytest.mark.parametrize("value, fields", _shape_cases())
def test_a_value_that_is_not_an_authorized_user_credential_is_refused_unechoed(tmp_path, value, fields):
    rc, out, calls = _run(tmp_path, "--join-person", "--stdin", stdin=value)
    assert rc != 0, out
    assert _adds(calls) == []
    assert "authorized_user" in out
    _assert_nothing_echoed(out, fields)


def test_an_empty_value_is_refused(tmp_path):
    rc, out, calls = _run(tmp_path, "--join-person", "--stdin", stdin="\n")
    assert rc != 0
    assert _adds(calls) == []


def test_a_missing_container_is_refused_not_created(tmp_path):
    rc, out, calls = _run(tmp_path, "--join-person", "--stdin", stdin=_credential(_fields()), existing="")
    assert rc != 0
    assert "terraform/infra/acceptance_join.tf" in out
    assert _adds(calls) == [] and not any(c["args"][:2] == ["secrets", "create"] for c in calls)


# --- overwriting: a typed confirmation --------------------------------------------


def test_an_enabled_version_is_not_replaced_without_a_typed_confirmation(tmp_path):
    fields = _fields()
    # SWARM_ASSUME_YES is not a typed confirmation.
    rc, out, calls = _run(tmp_path, "--join-person", "--stdin", stdin=_credential(fields), enabled=SECRET,
                          assume_yes=True)
    assert rc != 0, out
    assert _adds(calls) == []
    assert f"--overwrite {SECRET}" in out, out
    _assert_nothing_echoed(out, fields)


def test_the_wrong_name_is_not_a_confirmation(tmp_path):
    rc, out, calls = _run(tmp_path, "--join-person", "--stdin", "--overwrite", "swarm-egress-credential",
                          stdin=_credential(_fields()), enabled=SECRET)
    assert rc != 0, out
    assert _adds(calls) == []


def test_the_secrets_own_name_confirms_a_new_version(tmp_path):
    fields = _fields()
    rc, out, calls = _run(tmp_path, "--join-person", "--stdin", "--overwrite", SECRET,
                          stdin=_credential(fields), enabled=SECRET)
    assert rc == 0, out
    adds = _adds(calls)
    assert len(adds) == 1 and adds[0]["args"][3] == SECRET
    _assert_nothing_echoed(out, fields)


def test_help_names_the_slot():
    proc = subprocess.run(["bash", str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                          env={**os.environ, "SWARM_ENV_FILE": "/nonexistent", "NO_COLOR": "1"}, check=False)
    assert proc.returncode == 0, proc.stderr
    for needle in ("--join-person --stdin", f"--overwrite {SECRET}", "authorized_user"):
        assert needle in proc.stdout, needle
