"""The never-touch list is read as ONE object, never through a folder listing.

accept.yml's swarm-accept identity and the swarm-verify job's identity may read
`config/<env>/deny-list.json` and nothing else under `config/` (the bootstrap
rows in terraform/bootstrap/deny_list_readers.tf). A loader that lists
`config/<env>/` first needs `storage.objects.list` on the bucket, which neither
holds, so `config_fetch_one ENV NAME` reads the one object:

* over gcloud: `storage objects describe <exact URL>` for its generation, then
  `storage cp <exact URL>#<generation>`, and no other call;
* over REST, only when SWARM_CONFIG_TRANSPORT=rest is set (the swarm-verify
  image has no gcloud): the metadata server's token, the object's metadata and
  its media, each with `curl -fsS --max-time`, the media's x-goog-generation
  checked against the generation asked for;
* from SWARM_DEPLOY_CONFIG_DIR, the local source every --self-test uses.

Every failure is a refusal, never an empty or a stale list. Offline: a fake
`gcloud` and a fake `curl` on PATH serve a directory and log each call.
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
URL = f"gs://{BUCKET}/{OBJECT}"
REFUSAL = "is not readable; nothing that mutates runs without it"
API = f"https://storage.googleapis.com/storage/v1/b/{BUCKET}/o/config%2Fdev%2Fdeny-list.json"
TOKEN_URL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"

#: Answers ONLY `storage objects describe <URL> --format=value(generation)` and
#: `storage cp --quiet <URL>#<generation> DEST`, for the one object. Anything
#: else -- a listing, a bucket-level call, a cp with no generation -- exits 2,
#: so a loader that reaches past the one object fails here as it fails in IAM.
#: FAKE_DESCRIBE_TEXT replaces describe's answer; FAKE_DESCRIBE_ERR makes it fail
#: with that text; FAKE_CP_FAIL makes the copy fail.
FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"${FAKE_LOG}"
obj="${FAKE_GCS}/__OBJECT__"
if [[ "$*" == "storage objects describe __URL__ --format=value(generation)" ]]; then
  [[ -z "${FAKE_DESCRIBE_ERR:-}" ]] || { printf '%s\n' "${FAKE_DESCRIBE_ERR}" >&2; exit 1; }
  if [[ -n "${FAKE_DESCRIBE_TEXT+x}" ]]; then printf '%s\n' "${FAKE_DESCRIBE_TEXT}"; exit 0; fi
  [[ -f "${obj}" ]] || { echo "ERROR: (gcloud.storage.objects.describe) __URL__ not found: 404." >&2; exit 1; }
  cat "${obj}.gen"; echo
  exit 0
fi
if [[ "$1 $2 $3" == "storage cp --quiet" && "$4" == "__URL__#"* && $# -eq 5 ]]; then
  [[ -z "${FAKE_CP_FAIL:-}" ]] || { echo "ERROR: (gcloud.storage.cp) 503 backend error" >&2; exit 1; }
  [[ -f "${obj}" && "${4#*#}" == "$(cat "${obj}.gen")" ]] || { echo "no such generation" >&2; exit 1; }
  cp "${obj}" "$5"
  exit 0
fi
echo "fake gcloud: refused: $*" >&2
exit 2
""".replace("__URL__", URL).replace("__OBJECT__", OBJECT)

#: The metadata server's token and the JSON API, for the one object. Logs every
#: argument (so a token in argv would show) and refuses any call without -f,
#: -sS and --max-time. FAKE_TOKEN is the token it hands out; FAKE_HTTP_STATUS
#: answers the storage calls with that status instead; FAKE_MEDIA_GEN is the
#: x-goog-generation the media download reports.
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
if [[ "${url}" == "__TOKEN_URL__" ]]; then
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
if [[ "${url}" == "__API__?fields=generation" ]]; then
  answer 200 "{\"generation\":\"${FAKE_META_GEN-${gen}}\"}"
fi
if [[ "${url}" == "__API__?alt=media&generation=${gen}" ]]; then
  [[ -n "${dump}" ]] && printf 'HTTP/2 200\r\nx-goog-generation: %s\r\ncontent-type: application/json\r\n\r\n' \
    "${FAKE_MEDIA_GEN:-${gen}}" >"${dump}"
  answer 200 "$(cat "${obj}")"
fi
echo "fake curl: refused: ${url}" >&2
exit 2
""".replace("__TOKEN_URL__", TOKEN_URL).replace("__API__", API).replace("__OBJECT__", OBJECT)


def _example(project: str = PROJECT) -> str:
    return json.dumps({**json.loads(EXAMPLE.read_text()), "project": project})


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


def _setup(tmp_path: Path, *, gcloud: bool = True, curl: bool = True) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    if gcloud:
        _tool(bindir, "gcloud", FAKE_GCLOUD)
    if curl:
        _tool(bindir, "curl", FAKE_CURL)
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("PROJECT_ID", "SWARM_ENV_FILE", "SWARM_DEPLOY_CONFIG_DIR", "TF_STATE_BUCKET", "KUBECONFIG",
                     "SWARM_CONFIG_TRANSPORT")
    }
    env.update(
        PATH=f"{bindir}{os.pathsep}{_base_path_without(tmp_path, 'gcloud', 'curl')}",
        PROJECT_ID=PROJECT, REGION="us-central1", ENVIRONMENT="dev",
        SWARM_ENV_FILE=str(tmp_path / "no-env-file"),
        XDG_CACHE_HOME=str(tmp_path / "cache"),
        HOME=str(tmp_path),
        FAKE_GCS=str(tmp_path / "gcs" / BUCKET),
        FAKE_LOG=str(tmp_path / "gcloud.log"),
        FAKE_CURL_LOG=str(tmp_path / "curl.log"),
        FAKE_TOKEN=secrets.token_hex(24),
        # The load is the test's own call, not the one sourcing makes.
        SWARM_NO_PROJECT="1",
    )
    return env


def _put(tmp_path: Path, body: str, generation: str) -> None:
    obj = tmp_path / "gcs" / BUCKET / OBJECT
    obj.parent.mkdir(parents=True, exist_ok=True)
    obj.write_text(body)
    Path(f"{obj}.gen").write_text(generation)


def _delete(tmp_path: Path) -> None:
    (tmp_path / "gcs" / BUCKET / OBJECT).unlink()


def _run(env: dict[str, str], script: str, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'source "$1"; {script}', "_", str(COMMON)],
        capture_output=True, text=True, timeout=60, check=False, env={**env, **extra},
    )


def _log(tmp_path: Path, name: str) -> list[str]:
    path = tmp_path / name
    return path.read_text().splitlines() if path.exists() else []


def _cached(tmp_path: Path) -> Path:
    return tmp_path / "cache" / "swarm" / PROJECT / "dev" / "deny-list.json"


LOADED = 'deny_list_require; echo "loaded=${#SHARED_DENY_LIST[@]}"'


def _refused(proc: subprocess.CompletedProcess[str]) -> None:
    assert proc.returncode != 0, proc.stdout
    assert "loaded=" not in proc.stdout, "the list was served after a failed read"
    assert REFUSAL in proc.stderr, proc.stderr


# --- over gcloud --------------------------------------------------------------


def test_the_list_is_read_by_describe_then_cp_of_its_generation_and_nothing_else(tmp_path: Path) -> None:
    # MUTATION: load the list through config_fetch's folder listing again and
    # the fake refuses the `storage objects list` call: this goes red.
    env = _setup(tmp_path)
    _put(tmp_path, _example(), "1700000000000001")
    proc = _run(env, LOADED)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"loaded={len(json.loads(_example())['entries'])}"
    assert _log(tmp_path, "gcloud.log") == [
        f"storage objects describe {URL} --format=value(generation)",
        f"storage cp --quiet {URL}#1700000000000001 {_log_dest(tmp_path)}",
    ]
    assert _cached(tmp_path).stat().st_mode & 0o077 == 0
    assert not _log(tmp_path, "curl.log")


def _log_dest(tmp_path: Path) -> str:
    """The part file the one cp wrote to, which is mktemp's name."""
    line = next(x for x in _log(tmp_path, "gcloud.log") if x.startswith("storage cp"))
    dest = line.rsplit(" ", 1)[1]
    assert dest.startswith(str(_cached(tmp_path).parent / ".deny-list.json.part.")), dest
    return dest


def test_an_unchanged_generation_is_described_again_but_not_copied_again(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    _put(tmp_path, _example(), "7")
    assert _run(env, LOADED).returncode == 0
    assert _run(env, LOADED).returncode == 0
    calls = _log(tmp_path, "gcloud.log")
    assert sum(c.startswith("storage objects describe") for c in calls) == 2
    assert sum(c.startswith("storage cp") for c in calls) == 1


def test_a_new_generation_replaces_the_cached_copy(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    _put(tmp_path, _example(), "7")
    assert _run(env, LOADED).returncode == 0
    _put(tmp_path, _example("example-other-project"), "8")
    _refused(_run(env, LOADED))
    assert "example-other-project" in _cached(tmp_path).read_text()


def test_a_deleted_object_reads_as_missing_never_as_its_last_copy(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    _put(tmp_path, _example(), "7")
    assert _run(env, LOADED).returncode == 0
    _delete(tmp_path)
    proc = _run(env, LOADED)
    _refused(proc)
    assert f"{URL} does not exist" in proc.stderr
    assert not _cached(tmp_path).exists()
    # A missing object is not an expired session.
    assert "gcloud auth login" not in proc.stderr


@pytest.mark.parametrize("answer", ["", "\n", "not-a-generation"])
def test_an_empty_or_odd_generation_refuses_before_any_copy(tmp_path: Path, answer: str) -> None:
    env = _setup(tmp_path)
    _put(tmp_path, _example(), "7")
    proc = _run(env, LOADED, FAKE_DESCRIBE_TEXT=answer)
    _refused(proc)
    assert "no generation" in proc.stderr
    assert not any(c.startswith("storage cp") for c in _log(tmp_path, "gcloud.log"))
    assert not _cached(tmp_path).exists()


def test_a_copy_that_fails_after_describe_succeeded_refuses_and_leaves_no_part_file(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    _put(tmp_path, _example(), "7")
    proc = _run(env, LOADED, FAKE_CP_FAIL="1")
    _refused(proc)
    assert f"could not copy {URL}#7" in proc.stderr
    cache = _cached(tmp_path).parent
    assert not _cached(tmp_path).exists()
    assert not list(cache.glob(".*.part.*"))


def test_a_403_refuses_says_so_and_never_says_re_authenticate(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    _put(tmp_path, _example(), "7")
    value = secrets.token_hex(16)
    proc = _run(env, LOADED, FAKE_DESCRIBE_ERR=(
        "ERROR: (gcloud.storage.objects.describe) HTTPError 403: swarm-accept does not have "
        f"storage.objects.get access to the Google Cloud Storage object. access_token={value}"))
    _refused(proc)
    assert "403" in proc.stderr and "may not read" in proc.stderr, proc.stderr
    assert "gcloud auth login" not in proc.stderr
    assert value not in proc.stderr
    assert not any(c.startswith("storage cp") for c in _log(tmp_path, "gcloud.log"))


def test_an_expired_session_is_still_reported_as_one(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    _put(tmp_path, _example(), "7")
    proc = _run(env, LOADED, FAKE_DESCRIBE_ERR="ERROR: Reauthentication required.")
    assert proc.returncode != 0
    assert "loaded=" not in proc.stdout
    assert "gcloud auth login" in proc.stderr


def test_fetches_of_the_one_object_running_at_once_never_collide(tmp_path: Path) -> None:
    # MUTATION: one shared `.deny-list.json.part` file and this goes red.
    env = _setup(tmp_path)
    body = _example()
    _put(tmp_path, body, "11")
    script = ('pids=(); for i in 1 2 3 4 5 6 7 8 9 10 11 12; do config_fetch_one dev deny-list.json & pids+=("$!"); done; '
              'bad=0; for p in "${pids[@]}"; do wait "$p" || bad=$((bad + 1)); done; echo "failed=${bad}"')
    proc = _run(env, script)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "failed=0", proc.stderr
    assert _cached(tmp_path).read_text() == body


@pytest.mark.parametrize("name", ["../dev.tfvars", "", ".hidden", "a/b"])
def test_a_name_that_is_not_one_file_is_refused(tmp_path: Path, name: str) -> None:
    env = _setup(tmp_path)
    proc = _run(env, f'config_fetch_one dev "{name}" && echo fetched')
    assert "fetched" not in proc.stdout
    assert not _log(tmp_path, "gcloud.log")


def test_a_cached_copy_another_user_could_write_is_refused(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    _put(tmp_path, _example(), "7")
    assert _run(env, LOADED).returncode == 0
    _cached(tmp_path).chmod(0o666)
    proc = _run(env, LOADED)
    _refused(proc)
    assert "group- or world-writable" in proc.stderr


def test_an_environment_name_that_is_not_one_is_refused(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    proc = _run(env, 'config_fetch_one "../x" deny-list.json && echo fetched')
    assert "fetched" not in proc.stdout
    assert "is not an environment name" in proc.stderr


# --- no gcloud ----------------------------------------------------------------


def test_no_gcloud_and_no_transport_refuses_with_zero_network_calls(tmp_path: Path) -> None:
    # MUTATION: fall back to REST when gcloud is missing and curl.log fills.
    env = _setup(tmp_path, gcloud=False)
    _put(tmp_path, _example(), "7")
    proc = _run(env, LOADED)
    _refused(proc)
    assert "gcloud is not installed" in proc.stderr
    assert not _log(tmp_path, "curl.log")


def test_an_unknown_transport_refuses_with_zero_network_calls(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    _put(tmp_path, _example(), "7")
    proc = _run(env, LOADED, SWARM_CONFIG_TRANSPORT="http")
    _refused(proc)
    assert "SWARM_CONFIG_TRANSPORT" in proc.stderr
    assert not _log(tmp_path, "curl.log") and not _log(tmp_path, "gcloud.log")


# --- over REST, only when asked for ---------------------------------------------


def test_rest_reads_the_one_object_and_keeps_the_token_out_of_argv_and_stderr(tmp_path: Path) -> None:
    env = _setup(tmp_path, gcloud=False)
    _put(tmp_path, _example(), "42")
    proc = _run(env, LOADED, SWARM_CONFIG_TRANSPORT="rest")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("loaded=")
    calls = _log(tmp_path, "curl.log")
    assert len(calls) == 3, calls
    assert calls[0].endswith(TOKEN_URL)
    assert calls[1].endswith(f"{API}?fields=generation")
    assert calls[2].endswith(f"{API}?alt=media&generation=42")
    assert all("-fsS" in c and "--max-time" in c for c in calls)
    assert env["FAKE_TOKEN"] not in (tmp_path / "curl.log").read_text()
    assert env["FAKE_TOKEN"] not in proc.stderr + proc.stdout
    # Nothing holding the token is left in the cache.
    cache = _cached(tmp_path).parent
    assert sorted(p.name for p in cache.iterdir()) == [".deny-list.json.generation", "deny-list.json"]


def test_rest_is_never_chosen_because_gcloud_is_missing(tmp_path: Path) -> None:
    env = _setup(tmp_path, gcloud=False)
    _put(tmp_path, _example(), "42")
    _refused(_run(env, LOADED))
    assert not _log(tmp_path, "curl.log")


def test_rest_refuses_a_download_of_another_generation(tmp_path: Path) -> None:
    env = _setup(tmp_path, gcloud=False)
    _put(tmp_path, _example(), "42")
    proc = _run(env, LOADED, SWARM_CONFIG_TRANSPORT="rest", FAKE_MEDIA_GEN="43")
    _refused(proc)
    assert "generation 43, not 42" in proc.stderr
    assert not _cached(tmp_path).exists()


def test_rest_refuses_an_empty_generation(tmp_path: Path) -> None:
    env = _setup(tmp_path, gcloud=False)
    _put(tmp_path, _example(), "42")
    proc = _run(env, LOADED, SWARM_CONFIG_TRANSPORT="rest", FAKE_META_GEN="")
    _refused(proc)
    assert "no generation" in proc.stderr
    assert not any("alt=media" in c for c in _log(tmp_path, "curl.log"))


def test_rest_refuses_a_403(tmp_path: Path) -> None:
    env = _setup(tmp_path, gcloud=False)
    _put(tmp_path, _example(), "42")
    proc = _run(env, LOADED, SWARM_CONFIG_TRANSPORT="rest", FAKE_HTTP_STATUS="403")
    _refused(proc)
    assert "403" in proc.stderr and "may not read" in proc.stderr


def test_rest_reads_a_missing_object_as_missing(tmp_path: Path) -> None:
    env = _setup(tmp_path, gcloud=False)
    _put(tmp_path, _example(), "42")
    assert _run(env, LOADED, SWARM_CONFIG_TRANSPORT="rest").returncode == 0
    _delete(tmp_path)
    proc = _run(env, LOADED, SWARM_CONFIG_TRANSPORT="rest")
    _refused(proc)
    assert "does not exist" in proc.stderr
    assert not _cached(tmp_path).exists()


def test_rest_refuses_when_curl_is_missing(tmp_path: Path) -> None:
    env = _setup(tmp_path, gcloud=False, curl=False)
    _put(tmp_path, _example(), "42")
    proc = _run(env, LOADED, SWARM_CONFIG_TRANSPORT="rest")
    _refused(proc)
    assert "curl is not installed" in proc.stderr


def test_rest_skips_the_guard_bin_curl(tmp_path: Path) -> None:
    # scripts/lib/guard-bin/curl loads this list to judge the call: reaching
    # the bucket through it would load the list again, without end.
    env = _setup(tmp_path, gcloud=False, curl=False)
    _put(tmp_path, _example(), "42")
    env["PATH"] = f"{REPO / 'scripts' / 'lib' / 'guard-bin'}{os.pathsep}{env['PATH']}"
    proc = _run(env, LOADED, SWARM_CONFIG_TRANSPORT="rest")
    _refused(proc)
    assert "curl is not installed" in proc.stderr


# --- the local source, for every --self-test -----------------------------------


def test_the_local_directory_still_serves_a_self_test_with_no_network_call(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    work = tmp_path / "selftest"
    work.mkdir()
    proc = _run(env, f'deny_list_use_example "{work}"; echo "loaded=${{#SHARED_DENY_LIST[@]}}"')
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"loaded={len(json.loads(_example())['entries'])}"
    assert not _log(tmp_path, "gcloud.log") and not _log(tmp_path, "curl.log")


def test_a_list_removed_from_the_local_directory_leaves_the_cache(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    work = tmp_path / "selftest"
    work.mkdir()
    assert _run(env, f'deny_list_use_example "{work}"').returncode == 0
    (work / "config" / "dev" / "deny-list.json").unlink()
    proc = _run(env, LOADED, SWARM_DEPLOY_CONFIG_DIR=str(work / "config"), XDG_CACHE_HOME=str(work / "cache"))
    _refused(proc)
    assert "deny-list.json does not exist" in proc.stderr
    assert not (work / "cache" / "swarm" / PROJECT / "dev" / "deny-list.json").exists()
