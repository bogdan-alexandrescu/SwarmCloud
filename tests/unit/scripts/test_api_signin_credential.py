"""The API credential a laptop presents at the IAP front door is the `sc login`
sign-in, not the operator's gcloud access token (#1334).

Measured 2026-10-15: `scripts/startup-measure.sh` and `scripts/browser-bench.sh`
both got `401 ... Error code 900` from IAP on their first request, because
`api_credential()` handed the front door `gcloud auth print-access-token` for
the operator. IAP on this platform uses a Google-managed OAuth client, which
admits a programmatic caller only through an allow-listed client: the Desktop
client `sc login` signs in with (apps/swarm-mcp/swarm_mcp/signin.py). The same
plan bodies submitted through `SwarmClient` succeeded, 60/60.

What holds now, through scripts/lib/common.sh's real `api_request`:

  * at a front door, with no SWARM_IMPERSONATE_SA and outside Cloud Run, the
    header is the sign-in's, minted once per shell and handed to curl as
    `-H @<0600 file>` that is gone when the request returns;
  * the token is never in curl's argv, on stdout or on stderr;
  * no sign-in cached: the request fails, says `run: uv run sc login`, and
    curl is never called;
  * SWARM_IMPERSONATE_SA (CI) still presents the impersonated access token.

Offline: a fake `curl` and `gcloud` on PATH, and a fake `swarm_mcp` package
ahead of the real one on PYTHONPATH, so nothing reaches Google or a keychain.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
COMMON = REPO / "scripts" / "lib" / "common.sh"
HOST = "swarm.example.test"
SA = "swarm-verify@example.iam.gserviceaccount.com"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

FAKE_CURL = r"""#!/usr/bin/env bash
# Records argv, every -H @file's content and mode, and stdin when -K - is used.
set -euo pipefail
record="${FAKE_DIR}/curl.$$.json"
headers=()
modes=()
config=""
prev=""
for a in "$@"; do
  if [[ "${prev}" == "-H" && "${a}" == @* ]]; then
    f="${a#@}"
    headers+=("$(cat "${f}")")
    modes+=("$(stat -c '%a' "${f}" 2>/dev/null || stat -f '%Lp' "${f}")")
  fi
  if [[ "${prev}" == "-K" && "${a}" == "-" ]]; then config="$(cat)"; fi
  prev="${a}"
done
{
  printf '{"argv": %s,' "$(printf '%s\n' "$@" | jq -R . | jq -s .)"
  printf '"headers": %s,' "$(printf '%s\n' ${headers[@]+"${headers[@]}"} | jq -R . | jq -s 'map(select(. != ""))')"
  printf '"modes": %s,' "$(printf '%s\n' ${modes[@]+"${modes[@]}"} | jq -R . | jq -s 'map(select(. != ""))')"
  printf '"config": %s}\n' "$(printf '%s' "${config}" | jq -Rs .)"
} >"${record}"
case " $* " in
  *" -w "*) printf '{"ok": true}\n200' ;;
  *) printf '{"ok": true}' ;;
esac
"""

FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"${FAKE_DIR}/gcloud.calls"
case "$1 $2" in
  "config get-value") printf '%s' "someone@example.com" ;;
  "auth print-access-token") printf '%s' "${GCLOUD_TOKEN}" ;;
  *) exit 3 ;;
esac
"""

# The fake sign-in: the two names the helper uses, nothing else.
FAKE_CONFIG = """\
import os
from types import SimpleNamespace

def resolve(environ=None, *, context=None):
    with open(os.environ["FAKE_DIR"] + "/resolved.url", "w") as fh:
        fh.write(os.environ.get("SWARM_URL", ""))
    return SimpleNamespace(context="dev", url=os.environ.get("SWARM_URL", ""),
                           client_id="123-abc.apps.googleusercontent.com")
"""
FAKE_SIGNIN = """\
import os

class SignInRequired(Exception):
    pass

class SignedIn:
    def __init__(self, deployment, *, store=None):
        self.deployment = deployment

    def id_token(self):
        with open(os.environ["FAKE_DIR"] + "/mints", "a") as fh:
            fh.write("mint\\n")
        token = os.environ.get("SIGNIN_TOKEN", "")
        if not token:
            raise SignInRequired("sign-in required for dev: run `uv run sc login`")
        return token
"""


def _fake() -> str:
    """A stand-in credential, made at run time: a hex run, no vendor prefix."""
    return secrets.token_hex(24)


def _header(token: str) -> str:
    return "Authorization: Bearer " + token


def _world(tmp_path: Path, *, signin_token: str = "", sa: str = "") -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("curl", FAKE_CURL), ("gcloud", FAKE_GCLOUD)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    pkg = tmp_path / "fakepy" / "swarm_mcp"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "config.py").write_text(FAKE_CONFIG)
    (pkg / "signin.py").write_text(FAKE_SIGNIN)
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path / "tmp"),
        "FAKE_DIR": str(tmp_path),
        "PYTHONPATH": str(tmp_path / "fakepy"),
        "NO_COLOR": "1",
        "SWARM_ENV_FILE": str(tmp_path / "no.env"),
        # A laptop's .env names its project, and common.sh has no default one
        # (DAYONE-DESAGA-2); this world has no .env, so it names one here.
        "PROJECT_ID": "swarm-test",
        "API_HOST": HOST,
        "GCLOUD_TOKEN": _fake(),
        "SIGNIN_TOKEN": signin_token,
    }
    if sa:
        env["SWARM_IMPERSONATE_SA"] = sa
    (tmp_path / "tmp").mkdir()
    return env


def _run(tmp_path: Path, env: dict[str, str], script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", f'set -euo pipefail; source "{COMMON}"; {script}'],
                          cwd=REPO, env=env, capture_output=True, text=True, timeout=60)


def _curls(tmp_path: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(tmp_path.glob("curl.*.json"))]


def _mints(tmp_path: Path) -> int:
    path = tmp_path / "mints"
    return len(path.read_text().splitlines()) if path.exists() else 0


def test_a_laptop_presents_the_sc_login_sign_in_as_a_header_file(tmp_path):
    token = _fake()
    env = _world(tmp_path, signin_token=token)
    proc = _run(tmp_path, env,
                'api_request GET /v1/tasks >"${FAKE_DIR}/one.out"; '
                'api_request POST /v1/tasks "{}" >"${FAKE_DIR}/two.out"; echo "status=${API_STATUS}"')
    assert proc.returncode == 0, proc.stderr
    assert "status=200" in proc.stdout
    calls = _curls(tmp_path)
    assert len(calls) == 2, calls
    for call in calls:
        assert call["headers"] == [_header(token)], call
        assert call["modes"] == ["600"], call
        assert not any(token in a for a in call["argv"]), "the token reached curl's argv"
        assert env["GCLOUD_TOKEN"] not in json.dumps(call)
    assert token not in proc.stdout + proc.stderr
    # Minted once per shell, for this front door, and no header file is left.
    assert _mints(tmp_path) == 1
    assert (tmp_path / "resolved.url").read_text() == f"https://{HOST}"
    assert list((tmp_path / "tmp").iterdir()) == []
    gcloud = tmp_path / "gcloud.calls"
    assert not gcloud.exists() or "print-access-token" not in gcloud.read_text()


def test_no_sign_in_fails_and_names_sc_login(tmp_path):
    env = _world(tmp_path)
    proc = _run(tmp_path, env, 'api_request GET /v1/tasks >/dev/null; echo reached')
    assert proc.returncode != 0
    assert "reached" not in proc.stdout
    assert "run: uv run sc login" in proc.stderr
    assert _curls(tmp_path) == []
    assert list((tmp_path / "tmp").iterdir()) == []


def test_api_credential_hands_legacy_callers_the_sign_in(tmp_path):
    # smoke-test.sh, api.sh, live-report.sh read api_credential through
    # auth_config; at a laptop's front door that is the sign-in now.
    token = _fake()
    env = _world(tmp_path, signin_token=token)
    proc = _run(tmp_path, env, 'c="$(api_credential)"; [[ "${c}" == "${SIGNIN_TOKEN}" ]] && echo same')
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "same"
    assert list((tmp_path / "tmp").iterdir()) == []


def test_ci_impersonation_is_unchanged(tmp_path):
    env = _world(tmp_path, signin_token=_fake(), sa=SA)
    proc = _run(tmp_path, env, 'api_request GET /v1/tasks >/dev/null')
    assert proc.returncode == 0, proc.stderr
    calls = _curls(tmp_path)
    assert len(calls) == 1
    assert calls[0]["headers"] == [_header(env["GCLOUD_TOKEN"])], calls[0]
    assert _mints(tmp_path) == 0
    gcloud = (tmp_path / "gcloud.calls").read_text()
    assert f"--impersonate-service-account={SA}" in gcloud


def test_env_example_says_which_tenants_swarm_verify_serves():
    text = (REPO / ".env.example").read_text()
    start = text.index("# CI sets SWARM_ID_TOKEN")
    block = text[start:text.index("SWARM_IMPERSONATE_SA=", start)]
    assert "sc login" in block
    assert "tenant_not_member" in block
    for tenant in ("eng", "smoke", "u-sw-c90291"):
        assert tenant in block, tenant


def test_the_laptop_scripts_reach_the_api_only_through_api_request():
    # The two scripts the finding measured: neither builds its own credential,
    # so the sign-in above is what they present.
    for name in ("browser-bench.sh", "startup-measure.sh"):
        code = "\n".join(line for line in (REPO / "scripts" / name).read_text().splitlines()
                         if not line.lstrip().startswith("#"))
        assert "api_request" in code, name
        for direct in ("access_token", "api_credential", "auth_config", "curl -"):
            assert direct not in code, (name, direct)


def test_browser_bench_help_prints_its_whole_header():
    proc = subprocess.run(["bash", str(REPO / "scripts" / "browser-bench.sh"), "--help"],
                          capture_output=True, text=True, timeout=30, check=False,
                          env={**os.environ, "SWARM_ENV_FILE": "/nonexistent", "NO_COLOR": "1"})
    assert proc.returncode == 0, proc.stderr
    assert "uv run sc login" in proc.stdout
    assert "EXIT." in proc.stdout and "set -euo" not in proc.stdout
