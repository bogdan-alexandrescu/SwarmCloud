"""The never-touch list's REST read is tried again, a bounded number of times.

Issue 370: dev acceptance went red because the swarm-verify job's ONE read of
`config/dev/deny-list.json` timed out after 30 s (`curl: (28)`) and nothing
asked again, so the smoke target refused to run anything. Refusing to mutate
without the list is intended and stays; what was missing is a second try.

config_fetch_one's REST transport (SWARM_CONFIG_TRANSPORT=rest, the swarm-verify
job) now tries each call -- the metadata server's token, the object's
generation, its media -- up to SWARM_CONFIG_READ_TRIES times (default 3), with
SWARM_CONFIG_READ_PAUSE seconds (default 10) between tries, and ONLY on a
network timeout or no connection (curl exit 28 or 7) or an HTTP 5xx. A 403 is
refused at once, a 404 reads as a missing list at once, and after the last try
the read fails exactly as before, so deny_list_require refuses everything.
Fail closed does not move: no path serves an unread list as an empty one, and
no failed read serves a cached copy.

Offline: a fake `curl` and a fake `sleep` on PATH. The fake sleep logs its
argument and returns at once, so no test waits and nothing touches the network.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from pathlib import Path

import pytest

# It proves what common.sh does with its own list: none is served for it.
pytestmark = pytest.mark.no_placeholder_deployment

REPO = Path(__file__).resolve().parents[3]
COMMON = REPO / "scripts" / "lib" / "common.sh"
EXAMPLE = REPO / "scripts" / "lib" / "deny-list.example.json"
PROJECT = "example-project"
BUCKET = f"swarm-tfstate-{PROJECT}"
OBJECT = "config/dev/deny-list.json"
REFUSAL = "is not readable; nothing that mutates runs without it"
API = f"https://storage.googleapis.com/storage/v1/b/{BUCKET}/o/config%2Fdev%2Fdeny-list.json"
TOKEN_URL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
TIMEOUT_TEXT = "curl: (28) Connection timed out after 30000 milliseconds"

#: The metadata server's token and the JSON API, for the one object, as `curl
#: -fsS` answers. Logs every call, and counts each kind (token, generation,
#: media) in FAKE_COUNTS/<kind>; refuses any call without -fsS --max-time.
#:
#: FAKE_FAIL_WHERE (token|generation|media|storage) names the calls that fail;
#: the first FAKE_FAIL_TIMES of them (default: every one) fail as FAKE_FAIL_HOW
#: says: `timeout` (exit 28, `000` for -w, curl's timeout line on stderr),
#: `refused` (exit 7, no connection), or an HTTP status such as 503 (exit 22,
#: the status for -w, as -f does). FAKE_HTTP_STATUS answers every storage call
#: with that status.
FAKE_CURL = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"${FAKE_CURL_LOG}"
out=""; dump=""; fmt=""; url=""; hdrs=(); f=0; s=0; t=0
while (( $# )); do
  case "$1" in
    -fsS) f=1; s=1 ;;
    --max-time) t=1; shift ;;
    -o) out="$2"; shift ;;
    -D) dump="$2"; shift ;;
    -w) fmt="$2"; shift ;;
    -H) hdrs+=("$2"); shift ;;
    http*) url="$1" ;;
    *) echo "fake curl: unexpected argument $1" >&2; exit 2 ;;
  esac
  shift
done
(( f && s && t )) || { echo "fake curl: a call without -fsS --max-time" >&2; exit 2; }
answer() { # STATUS BODY
  [[ -n "${out}" ]] && printf '%s' "$2" >"${out}" || printf '%s' "$2"
  [[ -z "${fmt}" ]] || printf '%s' "$1"
  [[ "$1" == 200 ]] || { echo "curl: (22) The requested URL returned error: $1" >&2; exit 22; }
  exit 0
}
kind=other
case "${url}" in
  "__TOKEN_URL__") kind=token ;;
  "__API__?fields=generation") kind=generation ;;
  "__API__?alt=media&generation="*) kind=media ;;
esac
mkdir -p "${FAKE_COUNTS}"
n=$(( $(cat "${FAKE_COUNTS}/${kind}" 2>/dev/null || echo 0) + 1 ))
printf '%s' "${n}" >"${FAKE_COUNTS}/${kind}"
where="${FAKE_FAIL_WHERE:-}"
if [[ -n "${where}" && ( "${where}" == "${kind}" || ( "${where}" == storage && "${kind}" != token ) ) ]]; then
  if [[ -z "${FAKE_FAIL_TIMES:-}" ]] || (( n <= FAKE_FAIL_TIMES )); then
    case "${FAKE_FAIL_HOW}" in
      timeout) [[ -z "${fmt}" ]] || printf '000'; echo "__TIMEOUT_TEXT__" >&2; exit 28 ;;
      refused) [[ -z "${fmt}" ]] || printf '000'; echo "curl: (7) Failed to connect" >&2; exit 7 ;;
      *) answer "${FAKE_FAIL_HOW}" "" ;;
    esac
  fi
fi
if [[ "${kind}" == token ]]; then
  [[ "${hdrs[*]}" == "Metadata-Flavor: Google" ]] || answer 403 ""
  answer 200 "{\"access_token\":\"${FAKE_TOKEN}\",\"expires_in\":3599,\"token_type\":\"Bearer\"}"
fi
auth=""
for h in ${hdrs[@]+"${hdrs[@]}"}; do
  [[ "${h}" == @* ]] && auth="$(cat "${h#@}")"
done
[[ "${auth}" == "Authorization: Bearer ${FAKE_TOKEN}" ]] || answer 401 ""
[[ -z "${FAKE_HTTP_STATUS:-}" ]] || answer "${FAKE_HTTP_STATUS}" ""
obj="${FAKE_GCS}/__OBJECT__"
[[ -f "${obj}" ]] || answer 404 ""
gen="$(cat "${obj}.gen")"
if [[ "${kind}" == generation ]]; then
  answer 200 "{\"generation\":\"${gen}\"}"
fi
if [[ "${url}" == "__API__?alt=media&generation=${gen}" ]]; then
  [[ -n "${dump}" ]] && printf 'HTTP/2 200\r\nx-goog-generation: %s\r\ncontent-type: application/json\r\n\r\n' \
    "${gen}" >"${dump}"
  answer 200 "$(cat "${obj}")"
fi
echo "fake curl: refused: ${url}" >&2
exit 2
""".replace("__TOKEN_URL__", TOKEN_URL).replace("__API__", API).replace("__OBJECT__", OBJECT).replace(
    "__TIMEOUT_TEXT__", TIMEOUT_TEXT)

#: Logs its argument and returns at once: no test waits.
FAKE_SLEEP = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"${FAKE_SLEEP_LOG}"
"""


def _example() -> str:
    return json.dumps({**json.loads(EXAMPLE.read_text()), "project": PROJECT})


def _tool(bindir: Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text(body)
    path.chmod(0o755)


def _base_path_without(tmp_path: Path, *missing: str) -> str:
    """A PATH with every tool this host has, bar the named ones."""
    bindir = tmp_path / "host-bin"
    bindir.mkdir()
    for d in os.environ.get("PATH", "/usr/bin:/bin").split(os.pathsep):
        if not os.path.isdir(d):
            continue
        for entry in os.listdir(d):
            src = Path(d) / entry
            dest = bindir / entry
            if entry in missing or dest.exists() or dest.is_symlink():
                continue
            if src.is_file() and os.access(src, os.X_OK):
                dest.symlink_to(src)
    return str(bindir)


def _setup(tmp_path: Path) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _tool(bindir, "curl", FAKE_CURL)
    _tool(bindir, "sleep", FAKE_SLEEP)
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("PROJECT_ID", "SWARM_ENV_FILE", "SWARM_DEPLOY_CONFIG_DIR", "TF_STATE_BUCKET", "KUBECONFIG",
                     "SWARM_CONFIG_TRANSPORT", "SWARM_CONFIG_READ_TRIES", "SWARM_CONFIG_READ_PAUSE")
    }
    env.update(
        PATH=f"{bindir}{os.pathsep}{_base_path_without(tmp_path, 'gcloud', 'curl', 'sleep')}",
        PROJECT_ID=PROJECT, REGION="us-central1", ENVIRONMENT="dev",
        SWARM_ENV_FILE=str(tmp_path / "no-env-file"),
        XDG_CACHE_HOME=str(tmp_path / "cache"),
        HOME=str(tmp_path),
        FAKE_GCS=str(tmp_path / "gcs" / BUCKET),
        FAKE_CURL_LOG=str(tmp_path / "curl.log"),
        FAKE_SLEEP_LOG=str(tmp_path / "sleep.log"),
        FAKE_COUNTS=str(tmp_path / "counts"),
        FAKE_TOKEN=secrets.token_hex(24),
        SWARM_CONFIG_TRANSPORT="rest",
        # The load is the test's own call, not the one sourcing makes.
        SWARM_NO_PROJECT="1",
    )
    obj = tmp_path / "gcs" / BUCKET / OBJECT
    obj.parent.mkdir(parents=True, exist_ok=True)
    obj.write_text(_example())
    Path(f"{obj}.gen").write_text("42")
    return env


def _run(env: dict[str, str], **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", 'source "$1"; deny_list_require; echo "loaded=${#SHARED_DENY_LIST[@]}"', "_", str(COMMON)],
        capture_output=True, text=True, timeout=60, check=False, env={**env, **extra},
    )


def _log(tmp_path: Path, name: str) -> list[str]:
    path = tmp_path / name
    return path.read_text().splitlines() if path.exists() else []


def _count(tmp_path: Path, kind: str) -> int:
    path = tmp_path / "counts" / kind
    return int(path.read_text()) if path.exists() else 0


def _storage_calls(tmp_path: Path) -> int:
    return _count(tmp_path, "generation") + _count(tmp_path, "media")


def _cached(tmp_path: Path) -> Path:
    return tmp_path / "cache" / "swarm" / PROJECT / "dev" / "deny-list.json"


def _accepted(tmp_path: Path, proc: subprocess.CompletedProcess[str]) -> None:
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"loaded={len(json.loads(_example())['entries'])}", proc.stdout
    assert _cached(tmp_path).read_text() == _example()
    cache = _cached(tmp_path).parent
    assert sorted(p.name for p in cache.iterdir()) == [".deny-list.json.generation", "deny-list.json"]


def _refused(tmp_path: Path, proc: subprocess.CompletedProcess[str]) -> None:
    assert proc.returncode != 0, proc.stdout
    assert "loaded=" not in proc.stdout, "the list was served after a failed read"
    assert REFUSAL in proc.stderr, proc.stderr


def _no_secrets(env: dict[str, str], tmp_path: Path, proc: subprocess.CompletedProcess[str]) -> None:
    out = proc.stdout + proc.stderr
    assert env["FAKE_TOKEN"] not in out
    assert env["FAKE_TOKEN"] not in (tmp_path / "curl.log").read_text()
    # The retry notice names no bucket: only the read's own error lines did.
    notices = [line for line in proc.stderr.splitlines() if "try " in line and " of " in line]
    assert all(BUCKET not in line for line in notices), notices


# --- a failure, then a good answer: accepted ----------------------------------


def test_a_timeout_on_the_generation_read_then_a_good_answer_loads_the_list(tmp_path: Path) -> None:
    # MUTATION: one try only (the code before issue 370) and this refuses.
    env = _setup(tmp_path)
    proc = _run(env, FAKE_FAIL_WHERE="generation", FAKE_FAIL_HOW="timeout", FAKE_FAIL_TIMES="1")
    _accepted(tmp_path, proc)
    assert _count(tmp_path, "generation") == 2
    assert _count(tmp_path, "media") == 1
    assert _log(tmp_path, "sleep.log") == ["10"]
    assert "try 2 of 3" in proc.stderr, proc.stderr
    _no_secrets(env, tmp_path, proc)


@pytest.mark.parametrize("status", ["500", "502", "503", "504"])
def test_a_5xx_on_the_generation_read_then_a_good_answer_loads_the_list(tmp_path: Path, status: str) -> None:
    env = _setup(tmp_path)
    proc = _run(env, FAKE_FAIL_WHERE="generation", FAKE_FAIL_HOW=status, FAKE_FAIL_TIMES="1")
    _accepted(tmp_path, proc)
    assert _count(tmp_path, "generation") == 2
    assert _log(tmp_path, "sleep.log") == ["10"]
    assert "try 2 of 3" in proc.stderr and status in proc.stderr, proc.stderr


def test_no_connection_then_a_good_answer_loads_the_list(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    proc = _run(env, FAKE_FAIL_WHERE="generation", FAKE_FAIL_HOW="refused", FAKE_FAIL_TIMES="2")
    _accepted(tmp_path, proc)
    assert _count(tmp_path, "generation") == 3
    assert _log(tmp_path, "sleep.log") == ["10", "10"]


def test_a_timeout_on_the_media_download_then_a_good_answer_loads_the_list(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    proc = _run(env, FAKE_FAIL_WHERE="media", FAKE_FAIL_HOW="timeout", FAKE_FAIL_TIMES="1")
    _accepted(tmp_path, proc)  # also: no `.part.` file left in the cache
    assert _count(tmp_path, "generation") == 1
    assert _count(tmp_path, "media") == 2
    assert _log(tmp_path, "sleep.log") == ["10"]


@pytest.mark.parametrize("how", ["timeout", "503"])
def test_the_metadata_token_call_is_retried_the_same_bounded_way(tmp_path: Path, how: str) -> None:
    env = _setup(tmp_path)
    proc = _run(env, FAKE_FAIL_WHERE="token", FAKE_FAIL_HOW=how, FAKE_FAIL_TIMES="2")
    _accepted(tmp_path, proc)
    assert _count(tmp_path, "token") == 3
    assert _log(tmp_path, "sleep.log") == ["10", "10"]
    _no_secrets(env, tmp_path, proc)


# --- failure on every try: refused, bounded -----------------------------------


@pytest.mark.parametrize("how", ["timeout", "refused", "503"])
def test_failure_on_every_try_refuses_after_three_storage_calls_and_two_pauses(tmp_path: Path, how: str) -> None:
    # MUTATION: retry without a bound and the fake answers forever; treat the
    # exhausted read as an empty list and `loaded=` is printed.
    env = _setup(tmp_path)
    proc = _run(env, FAKE_FAIL_WHERE="storage", FAKE_FAIL_HOW=how)
    _refused(tmp_path, proc)
    assert _storage_calls(tmp_path) == 3
    assert _count(tmp_path, "media") == 0
    assert _log(tmp_path, "sleep.log") == ["10", "10"]
    assert not _cached(tmp_path).exists()
    _no_secrets(env, tmp_path, proc)


def test_timeouts_on_every_try_never_serve_a_cached_copy(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    _accepted(tmp_path, _run(env))
    (tmp_path / "counts").joinpath("generation").unlink()
    proc = _run(env, FAKE_FAIL_WHERE="generation", FAKE_FAIL_HOW="timeout")
    _refused(tmp_path, proc)
    assert _count(tmp_path, "generation") == 3
    assert "could not read" in proc.stderr and "HTTP 000" in proc.stderr


def test_media_timeouts_on_every_try_refuse_and_leave_no_part_file(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    proc = _run(env, FAKE_FAIL_WHERE="media", FAKE_FAIL_HOW="timeout")
    _refused(tmp_path, proc)
    assert _count(tmp_path, "media") == 3
    assert _log(tmp_path, "sleep.log") == ["10", "10"]
    cache = _cached(tmp_path).parent
    assert not _cached(tmp_path).exists()
    assert not list(cache.glob(".*.part.*"))


def test_token_timeouts_on_every_try_refuse_with_no_storage_call(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    proc = _run(env, FAKE_FAIL_WHERE="token", FAKE_FAIL_HOW="timeout")
    _refused(tmp_path, proc)
    assert _count(tmp_path, "token") == 3
    assert _storage_calls(tmp_path) == 0
    assert "could not get a token from the metadata server" in proc.stderr
    _no_secrets(env, tmp_path, proc)


def test_the_tries_and_the_pause_are_the_knobs_and_bounded(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    proc = _run(env, FAKE_FAIL_WHERE="generation", FAKE_FAIL_HOW="timeout",
                SWARM_CONFIG_READ_TRIES="2", SWARM_CONFIG_READ_PAUSE="1")
    _refused(tmp_path, proc)
    assert _count(tmp_path, "generation") == 2
    assert _log(tmp_path, "sleep.log") == ["1"]


@pytest.mark.parametrize("knob", [("SWARM_CONFIG_READ_TRIES", "0"), ("SWARM_CONFIG_READ_TRIES", "x"),
                                  ("SWARM_CONFIG_READ_TRIES", "99"), ("SWARM_CONFIG_READ_PAUSE", "-1"),
                                  ("SWARM_CONFIG_READ_PAUSE", "3600")])
def test_a_knob_out_of_range_refuses_with_no_network_call(tmp_path: Path, knob: tuple[str, str]) -> None:
    env = _setup(tmp_path)
    proc = _run(env, **{knob[0]: knob[1]})
    _refused(tmp_path, proc)
    assert knob[0] in proc.stderr
    assert not _log(tmp_path, "curl.log")


# --- never retried: 403, 404, a body of the wrong shape -----------------------


def test_a_403_is_refused_after_one_storage_call_and_no_pause(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    proc = _run(env, FAKE_HTTP_STATUS="403")
    _refused(tmp_path, proc)
    assert "403: this identity may not read" in proc.stderr
    assert _storage_calls(tmp_path) == 1
    assert not _log(tmp_path, "sleep.log")
    assert "try 2" not in proc.stderr


def test_a_404_reads_as_missing_after_one_storage_call_and_no_pause(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    (tmp_path / "gcs" / BUCKET / OBJECT).unlink()
    proc = _run(env)
    _refused(tmp_path, proc)
    assert "does not exist" in proc.stderr
    assert _storage_calls(tmp_path) == 1
    assert not _log(tmp_path, "sleep.log")
    assert not _cached(tmp_path).exists()


def test_a_4xx_other_than_403_and_404_is_not_retried(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    proc = _run(env, FAKE_FAIL_WHERE="generation", FAKE_FAIL_HOW="429")
    _refused(tmp_path, proc)
    assert _storage_calls(tmp_path) == 1
    assert not _log(tmp_path, "sleep.log")


def test_a_good_answer_whose_body_is_not_a_list_refuses_without_a_retry(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    (tmp_path / "gcs" / BUCKET / OBJECT).write_text("<html>not a list</html>")
    proc = _run(env)
    _refused(tmp_path, proc)
    assert "not valid JSON" in proc.stderr
    assert _storage_calls(tmp_path) == 2
    assert not _log(tmp_path, "sleep.log")
