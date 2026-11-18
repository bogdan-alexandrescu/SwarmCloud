"""`config_fetch` copies the deployment's config prefix into a private cache.

The real `<env>.tfvars` and `deny-list.json` live in
`gs://<state-bucket>/config/<env>/` (docs/design/generic-repository.md,
section 2.3). `config_fetch <env>` copies them into
`${XDG_CACHE_HOME}/swarm/<project>/<env>/`, outside the clone, keyed by each
object's generation; `tf_var_file` returns the cached tfvars and never falls
back to a tracked file; the Makefile asks `scripts/lib/resolve.sh tfvars`.

Offline: a fake `gcloud` on PATH serves a directory as the bucket and logs each
call. No cloud call, no credential.
"""

from __future__ import annotations

import os
import secrets
import subprocess
from pathlib import Path

import pytest

# It proves what common.sh does with no list, or with its own: none is served for it.
pytestmark = pytest.mark.no_placeholder_deployment

REPO = Path(__file__).resolve().parents[3]
COMMON = REPO / "scripts" / "lib" / "common.sh"
RESOLVE = REPO / "scripts" / "lib" / "resolve.sh"
PROJECT = "example-project"
BUCKET = f"swarm-tfstate-{PROJECT}"

#: `gcloud storage objects list gs://B/P/* --format=value(name,generation)` and
#: `gcloud storage cp gs://B/O#GEN DEST`, served from $FAKE_GCS/<bucket>/<object>
#: with each generation in $FAKE_GCS/.gen/<bucket>/<object>. Any other call fails.
FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"${FAKE_GCS_LOG}"
[[ -z "${FAKE_GCS_FAIL:-}" ]] || { echo "${FAKE_GCS_FAIL_TEXT:-ERROR: (gcloud.storage) 503 backend error}" >&2; exit 1; }
if [[ "$1 $2 $3" == "storage objects list" ]]; then
  url="${4#gs://}"; prefix="${url%\*}"; bucket="${prefix%%/*}"; path="${prefix#*/}"
  for f in "${FAKE_GCS}/${bucket}/${path}"*; do
    [[ -f "${f}" ]] || continue
    name="${path}${f##*/}"
    printf '%s\t%s\n' "${name}" "$(cat "${FAKE_GCS}/.gen/${bucket}/${name}")"
  done
  exit 0
fi
if [[ "$1 $2" == "storage cp" ]]; then
  src=""; dest=""
  for a in "${@:3}"; do
    case "${a}" in --*) ;; gs://*) src="${a#gs://}" ;; *) dest="${a}" ;; esac
  done
  obj="${src%%#*}"; gen="${src##*#}"
  [[ "$(cat "${FAKE_GCS}/.gen/${obj}")" == "${gen}" ]] || { echo "no such generation" >&2; exit 1; }
  cp "${FAKE_GCS}/${obj}" "${dest}"
  exit 0
fi
echo "fake gcloud: unexpected call: $*" >&2
exit 2
"""


class Bucket:
    def __init__(self, root: Path) -> None:
        self.root = root

    def put(self, name: str, body: str, generation: str) -> None:
        obj = self.root / BUCKET / f"config/dev/{name}"
        obj.parent.mkdir(parents=True, exist_ok=True)
        obj.write_text(body)
        gen = self.root / ".gen" / BUCKET / f"config/dev/{name}"
        gen.parent.mkdir(parents=True, exist_ok=True)
        gen.write_text(generation)

    def delete(self, name: str) -> None:
        (self.root / BUCKET / f"config/dev/{name}").unlink()


def _setup(tmp_path: Path) -> tuple[Bucket, dict[str, str]]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "gcloud"
    fake.write_text(FAKE_GCLOUD)
    fake.chmod(0o755)
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("PROJECT_ID", "SWARM_ENV_FILE", "SWARM_DEPLOY_CONFIG_DIR", "TF_STATE_BUCKET", "KUBECONFIG")
    }
    env.update(
        PATH=f"{bindir}:{env.get('PATH', '/usr/bin:/bin')}",
        PROJECT_ID=PROJECT, REGION="us-central1", ZONE="us-central1-a", ENVIRONMENT="dev",
        SWARM_ENV_FILE=str(tmp_path / "no-env-file"),
        XDG_CACHE_HOME=str(tmp_path / "cache"),
        HOME=str(tmp_path),
        FAKE_GCS=str(tmp_path / "gcs"),
        FAKE_GCS_LOG=str(tmp_path / "gcloud.log"),
        # These tests are about the fetch alone. Sourcing common.sh with a
        # project loads the never-touch list at once (test_shared_deny_list.py
        # holds that), which would fetch before the test's own call does.
        SWARM_NO_PROJECT="1",
    )
    return Bucket(tmp_path / "gcs"), env


def _run(env: dict[str, str], script: str, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'source "$1"; {script}', "_", str(COMMON)],
        capture_output=True, text=True, timeout=60, check=False, env={**env, **extra},
    )


def _cache(tmp_path: Path) -> Path:
    return tmp_path / "cache" / "swarm" / PROJECT / "dev"


def _copies(tmp_path: Path) -> int:
    log = tmp_path / "gcloud.log"
    return sum(1 for line in log.read_text().splitlines() if line.startswith("storage cp")) if log.exists() else 0


def test_fetch_copies_the_prefix_into_a_private_cache_outside_the_clone(tmp_path: Path) -> None:
    bucket, env = _setup(tmp_path)
    bucket.put("dev.tfvars", 'project_id = "example-project"\n', "11")
    proc = _run(env, 'config_fetch dev && printf "%s" "$(config_cache_dir dev)"')
    assert proc.returncode == 0, proc.stderr
    cache = Path(proc.stdout)
    assert cache == _cache(tmp_path)
    assert REPO not in cache.parents
    assert (cache / "dev.tfvars").read_text() == 'project_id = "example-project"\n'
    assert (cache / "dev.tfvars").stat().st_mode & 0o077 == 0
    assert cache.stat().st_mode & 0o077 == 0
    listed = (tmp_path / "gcloud.log").read_text().splitlines()[0]
    assert listed.startswith(f"storage objects list gs://{BUCKET}/config/dev/*")


def test_an_unchanged_generation_is_not_copied_again(tmp_path: Path) -> None:
    bucket, env = _setup(tmp_path)
    bucket.put("dev.tfvars", "a = 1\n", "11")
    assert _run(env, "config_fetch dev").returncode == 0
    assert _run(env, "config_fetch dev").returncode == 0
    assert _copies(tmp_path) == 1
    bucket.put("dev.tfvars", "a = 2\n", "12")
    assert _run(env, "config_fetch dev").returncode == 0
    assert _copies(tmp_path) == 2
    assert (_cache(tmp_path) / "dev.tfvars").read_text() == "a = 2\n"


def test_a_deleted_object_leaves_the_cache(tmp_path: Path) -> None:
    # A deleted deny-list must read as missing, never as its last copy.
    bucket, env = _setup(tmp_path)
    bucket.put("deny-list.json", "{}", "5")
    assert _run(env, "config_fetch dev").returncode == 0
    assert (_cache(tmp_path) / "deny-list.json").exists()
    bucket.delete("deny-list.json")
    assert _run(env, "config_fetch dev").returncode == 0
    assert not (_cache(tmp_path) / "deny-list.json").exists()


def test_a_failed_listing_never_serves_the_cache(tmp_path: Path) -> None:
    bucket, env = _setup(tmp_path)
    bucket.put("dev.tfvars", "a = 1\n", "11")
    assert _run(env, "config_fetch dev").returncode == 0
    proc = _run(env, "config_fetch dev && echo fetched; tf_var_file; echo reached", FAKE_GCS_FAIL="1")
    assert proc.returncode != 0
    assert "fetched" not in proc.stdout and "reached" not in proc.stdout
    assert "could not list" in proc.stderr


def test_a_failed_listing_is_printed_through_redact(tmp_path: Path) -> None:
    # MUTATION: drop `| redact` from _config_list's error line and this fails.
    _, env = _setup(tmp_path)
    value = secrets.token_hex(16)
    proc = _run(env, "config_fetch dev", FAKE_GCS_FAIL="1", FAKE_GCS_FAIL_TEXT=f"ERROR: 403 access_token={value}")
    assert proc.returncode != 0
    assert "could not list" in proc.stderr, proc.stderr
    assert value not in proc.stderr, proc.stderr


def test_a_cache_file_another_user_could_write_is_refused(tmp_path: Path) -> None:
    bucket, env = _setup(tmp_path)
    bucket.put("dev.tfvars", "a = 1\n", "11")
    assert _run(env, "config_fetch dev").returncode == 0
    (_cache(tmp_path) / "dev.tfvars").chmod(0o666)
    proc = _run(env, "config_fetch dev && echo fetched")
    assert "fetched" not in proc.stdout
    assert "group- or world-writable" in proc.stderr


def test_tf_var_file_is_the_cached_tfvars(tmp_path: Path) -> None:
    bucket, env = _setup(tmp_path)
    bucket.put("dev.tfvars", "a = 1\n", "11")
    proc = _run(env, "tf_var_file")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == str(_cache(tmp_path) / "dev.tfvars")


def test_tf_var_file_names_the_missing_object_and_never_falls_back(tmp_path: Path) -> None:
    # MUTATION: return terraform/environments/<env>/<env>.tfvars when the
    # object is missing and this fails: the tracked file is printed.
    _bucket, env = _setup(tmp_path)
    proc = _run(env, "tf_var_file; echo reached")
    assert proc.returncode != 0
    assert "reached" not in proc.stdout
    assert "terraform/environments" not in proc.stdout
    assert f"gs://{BUCKET}/config/dev/dev.tfvars does not exist" in proc.stderr


def test_resolve_tfvars_prints_the_cached_path(tmp_path: Path) -> None:
    bucket, env = _setup(tmp_path)
    bucket.put("prod.tfvars", "a = 1\n", "3")
    obj = tmp_path / "gcs" / BUCKET / "config" / "prod"
    obj.mkdir(parents=True)
    (tmp_path / "gcs" / BUCKET / "config" / "dev" / "prod.tfvars").rename(obj / "prod.tfvars")
    gen = tmp_path / "gcs" / ".gen" / BUCKET / "config" / "prod"
    gen.mkdir(parents=True)
    (tmp_path / "gcs" / ".gen" / BUCKET / "config" / "dev" / "prod.tfvars").rename(gen / "prod.tfvars")
    proc = subprocess.run([str(RESOLVE), "tfvars", "prod"], capture_output=True, text=True,
                          timeout=60, check=False, env=env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == str(tmp_path / "cache" / "swarm" / PROJECT / "prod" / "prod.tfvars")


def test_resolve_deny_list_checks_the_object(tmp_path: Path) -> None:
    _bucket, env = _setup(tmp_path)
    proc = subprocess.run([str(RESOLVE), "deny-list", "dev"], capture_output=True, text=True,
                          timeout=60, check=False, env=env)
    assert proc.returncode != 0
    assert "is not readable; nothing that mutates runs without it" in proc.stderr


def test_the_makefile_asks_resolve_for_its_tfvars() -> None:
    text = (REPO / "Makefile").read_text()
    assert "VAR_FILE = $(shell $(RESOLVE) tfvars $(ENVIRONMENT))" in text
    assert "terraform/environments/$(ENVIRONMENT)" not in text
    # Read-only targets load the list too, so a broken one is found early.
    for target in ("tf-plan", "status", "prerequisites"):
        block = text.split(f"\n{target}:", 1)[1].split("\n\n", 1)[0]
        assert "$(RESOLVE) deny-list $(ENVIRONMENT)" in block, target


def test_fetches_running_at_once_never_collide(tmp_path: Path) -> None:
    # A plan guard and a kubectl call can source common.sh at the same moment.
    # MUTATION: one shared `.<name>.part` file for every process and this goes
    # red: a copy fails with "File exists" or a reader sees half a file.
    bucket, env = _setup(tmp_path)
    body = 'project_id = "example-project"\n' * 200
    bucket.put("dev.tfvars", body, "11")
    script = ('pids=(); for i in 1 2 3 4 5 6 7 8 9 10 11 12; do config_fetch dev & pids+=("$!"); done; '
              'bad=0; for p in "${pids[@]}"; do wait "$p" || bad=$((bad + 1)); done; echo "failed=${bad}"')
    proc = _run(env, script)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "failed=0", proc.stderr
    assert (_cache(tmp_path) / "dev.tfvars").read_text() == body
