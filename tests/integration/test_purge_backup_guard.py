"""What `scripts/purge-data.sh` says when it cannot see the backup bucket.

Driven end to end with a fake `gcloud` and a fake `curl` on PATH. Nothing is
created, no credentials are used, and every case below dies before the typed
confirmation, so no deletion path is ever entered.

THE PROPERTY, and why it is worth a file of its own:

    The backup step stands immediately before an IRREVERSIBLE Firestore purge.
    Its probe was

        gcloud storage buckets describe ... >/dev/null 2>&1

    so every non-zero exit meant "the bucket does not exist" -- and non-zero is
    also what an expired session, a wrong PROJECT_ID, a missing
    storage.buckets.get and a 503 produce. The message it printed was

        artifact bucket gs://... does not exist, so no backup can be taken;
        pass --no-backup to accept that

    i.e. it diagnosed the wrong cause AND offered, as the remedy, to delete the
    data with no backup at all. An operator whose token had quietly expired
    could destroy a tenant's data with nothing to restore from, on the script's
    own advice. Ranked the most misleading finding in
    docs/audits/2026-09-18/04-script-error-messages.md for exactly that reason.

The three answers that have to stay distinguishable are the same three the
shared-resource verification keeps apart: present, provably absent, and could
not look. Only the middle one may mention `--no-backup`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "purge-data.sh"

PROJECT = "swarm-test-project"
#: Deliberately NOT on the shared deny-list in scripts/lib/common.sh -- guard 2
#: refuses those outright and the run would never reach the backup step.
BUCKET = "swarm-test-project-swarm-artifacts"

#: The exact sentence that OFFERS the unbacked purge. Grepping for the flag
#: alone is not enough: the "could not look" branch names it in order to warn
#: against it, which is the opposite of offering it.
OFFER = "pass --no-backup to accept that"

pytestmark = pytest.mark.skipif(
    not SCRIPT.exists() or shutil.which("jq") is None,
    reason="purge-data.sh and jq are both required",
)


def _gcloud(bucket_describe: str) -> str:
    """A fake gcloud whose `buckets describe` branch is supplied per case."""
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'args="$*"\n'
        'case "${args}" in\n'
        '  *"storage buckets describe"*)\n'
        f"{bucket_describe}\n"
        "    ;;\n"
        '  *"auth print-access-token"*)   echo "ya29.fake" ;;\n'
        '  *"auth print-identity-token"*) echo "id.fake" ;;\n'
        "  *) : ;;\n"
        "esac\n"
        "exit 0\n"
    )


BUCKET_PRESENT = '    echo "' + BUCKET + '"'

#: gcloud's real shape for a dead session. The exit status is the same 1 a
#: genuinely absent bucket produces; only the stderr tells them apart.
BUCKET_EXPIRED_SESSION = """    cat >&2 <<'MSG'
ERROR: (gcloud.storage.buckets.describe) There was a problem refreshing your current auth tokens: Reauthentication required.
Please run:
  $ gcloud auth login
MSG
    exit 1"""

#: gcloud ANSWERED, and the answer was NOT_FOUND. This is the one real absence.
BUCKET_NOT_FOUND = """    echo "ERROR: (gcloud.storage.buckets.describe) gs://{bucket} not found: 404." >&2
    exit 1""".format(bucket=BUCKET)

#: The split that is very common in practice: an operator who may delete
#: Firestore documents but holds no storage role on the artifact bucket.
BUCKET_PERMISSION_DENIED = """    cat >&2 <<'MSG'
ERROR: (gcloud.storage.buckets.describe) HTTPError 403: caller does not have storage.buckets.get access to the Google Cloud Storage bucket.
MSG
    exit 1"""


#: `fs_database_exists` calls curl WITHOUT -o and reads the body from stdout;
#: `fs_request` calls it WITH `-o <file> -w '%{http_code}'` and reads the status
#: from stdout. Both shapes have to work, or the fixture -- not the script --
#: is what fails. An empty document is the whole contract here: every count
#: comes back 0, and `--artifacts` is what keeps the run going past that.
FAKE_CURL = r"""#!/usr/bin/env bash
set -euo pipefail
out=""
want_status=0
prev=""
for arg in "$@"; do
  case "${prev}" in
    -o) out="${arg}" ;;
    -w) [[ "${arg}" == *http_code* ]] && want_status=1 ;;
  esac
  prev="${arg}"
done
cat >/dev/null 2>&1 || true
body='{}'
if [[ -n "${out}" ]]; then
  printf '%s' "${body}" > "${out}"
  [[ "${want_status}" -eq 1 ]] && printf '200'
else
  printf '%s' "${body}"
fi
"""


def _purge(tmp: Path, bucket_describe: str) -> subprocess.CompletedProcess:
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    for name, body in (("gcloud", _gcloud(bucket_describe)), ("curl", FAKE_CURL)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)

    # common.sh refuses to source a group- or world-writable env file, and it is
    # right to: it sources it as shell.
    env_file = tmp / "env"
    env_file.write_text(
        f"PROJECT_ID={PROJECT}\n"
        "REGION=us-central1\n"
        "ENVIRONMENT=dev\n"
        "FIRESTORE_DATABASE=swarm\n"
        f"ARTIFACT_BUCKET={BUCKET}\n"
    )
    env_file.chmod(0o600)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["SWARM_ENV_FILE"] = str(env_file)
    env["NO_COLOR"] = "1"
    # Never set SWARM_ASSUME_YES here. The script unsets it anyway, and the
    # confirmation refuses a non-TTY -- which is what stops these runs short of
    # the deletion path even when the backup step succeeds.
    env.pop("SWARM_ASSUME_YES", None)

    return subprocess.run(
        # --artifacts, so a zero document count does not exit early at
        # "nothing to purge" before the backup step is reached.
        [str(SCRIPT), "--artifacts"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=180,
        stdin=subprocess.DEVNULL,
    )


def test_a_dead_session_is_not_reported_as_a_missing_bucket(tmp_path) -> None:
    """The regression this file exists for.

    Nothing may claim the bucket is absent, and nothing may invite the operator
    to purge without a backup, because the run proved neither.
    """
    proc = _purge(tmp_path, BUCKET_EXPIRED_SESSION)
    transcript = proc.stdout + proc.stderr

    assert proc.returncode != 0, transcript
    assert "does not exist" not in transcript, (
        "an unanswered lookup was rendered as an absent bucket:\n" + transcript
    )
    assert OFFER not in transcript, (
        "the script offered to skip the backup of an irreversible purge on the "
        "strength of a lookup that never happened:\n" + transcript
    )
    assert "authentication, not a missing resource" in transcript, transcript
    assert "gcloud auth login" in transcript, transcript


def test_a_permission_gap_is_not_reported_as_a_missing_bucket(tmp_path) -> None:
    """403 is the common one: Firestore access without a storage role."""
    proc = _purge(tmp_path, BUCKET_PERMISSION_DENIED)
    transcript = proc.stdout + proc.stderr

    assert proc.returncode != 0, transcript
    assert "does not exist" not in transcript, transcript
    assert OFFER not in transcript, transcript
    assert "Do NOT reach for --no-backup" in transcript, (
        "naming the flag in order to WARN against it is the point; offering it "
        "is the defect:\n" + transcript
    )
    assert "failure to LOOK UP" in transcript, transcript
    # The real text has to survive, or the operator is sent at the wrong cause.
    assert "storage.buckets.get" in transcript, transcript


def test_a_bucket_that_really_is_absent_still_says_so(tmp_path) -> None:
    """Separating the cases must not disarm the message that was correct.

    gcloud answered 404. Here absence is a fact, and `--no-backup` is a real
    choice an operator may make.
    """
    proc = _purge(tmp_path, BUCKET_NOT_FOUND)
    transcript = proc.stdout + proc.stderr

    assert proc.returncode != 0, transcript
    assert f"gs://{BUCKET} does not exist" in transcript, transcript
    assert OFFER in transcript, transcript
    assert "authentication, not a missing resource" not in transcript, transcript


def test_a_readable_bucket_backs_up_and_only_then_asks(tmp_path) -> None:
    """The happy path still runs, and still cannot delete anything unattended.

    The export is taken, and the run then stops at the typed confirmation --
    which refuses a non-TTY rather than assuming consent.
    """
    proc = _purge(tmp_path, BUCKET_PRESENT)
    transcript = proc.stdout + proc.stderr

    assert "backup written to" in transcript, transcript
    assert "interactive confirmation" in transcript, (
        "the run got past the confirmation without a TTY:\n" + transcript
    )
    assert proc.returncode != 0, transcript
