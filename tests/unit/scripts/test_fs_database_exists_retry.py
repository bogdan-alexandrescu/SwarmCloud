"""`fs_database_exists` in scripts/lib/common.sh asks Firestore ONCE MORE when
its first request got no answer at all, and never when it got one.

THE FAILURE THIS PINS. On 2026-09-30 the release's acceptance pre-flight
(`require_platform` in scripts/lib/testlib.sh -> `require_fs_database` ->
`fs_database_exists`) died with

    curl: (28) Connection timed out after 30002 milliseconds

against a database that was there. Nothing had answered: a TCP connect that
never completed is not a statement about the database, and one more attempt
five seconds later is the cheapest way to tell a blip from an outage.

Owner decision, 2026-09-30: retry a network call ONCE when it got no answer --
curl exit 28 (timed out) or 7 (could not connect) -- and NEVER on an HTTP
status. A 403 or a 404 is Firestore answering; asking it again gets the same
answer and hides nothing but time. A second no-answer fails exactly as the
first one used to, with the "failure to LOOK, not proof the database is
absent" message.

Nothing here touches the network: common.sh is sourced and the real function
called, against a fake `curl` on PATH that plays a scripted list of answers
and counts its calls.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
COMMON = REPO / "scripts" / "lib" / "common.sh"

pytestmark = pytest.mark.skipif(
    not COMMON.exists() or shutil.which("bash") is None or shutil.which("jq") is None,
    reason="common.sh, bash and jq are required",
)

# One scripted answer per call, read from line N of curl.answers on the Nth
# call. The no-answer lines are the text curl really prints for those exits.
FAKE_CURL = r"""#!/usr/bin/env bash
n=$(( $(cat "${FAKE_DIR}/curl.count" 2>/dev/null || echo 0) + 1 ))
printf '%s\n' "${n}" >"${FAKE_DIR}/curl.count"
answer="$(sed -n "${n}p" "${FAKE_DIR}/curl.answers")"
case "${answer}" in
  7)   echo "curl: (7) Failed to connect to firestore.googleapis.com port 443 after 3 ms: Couldn't connect to server" >&2; exit 7 ;;
  28)  echo "curl: (28) Connection timed out after 30002 milliseconds" >&2; exit 28 ;;
  6)   echo "curl: (6) Could not resolve host: firestore.googleapis.com" >&2; exit 6 ;;
  200) printf '{"name":"projects/swarm-test-project/databases/swarm","type":"FIRESTORE_NATIVE"}\n' ;;
  403|404|503)
       printf '{"error":{"code":%s,"message":"fake answer %s","status":"FAKE"}}\n' "${answer}" "${answer}" ;;
  *)   echo "fake curl: no answer scripted for call ${n}" >&2; exit 99 ;;
esac
"""


def _call(tmp_path: Path, answers: list[str], fn: str = "fs_database_exists"):
    """Source common.sh, call FN, and return (rc, stderr, curl call count)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(FAKE_CURL)
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (tmp_path / "curl.answers").write_text("\n".join(answers) + "\n")

    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "FAKE_DIR": str(tmp_path),
        "NO_COLOR": "1",
        "SWARM_ENV_FILE": str(tmp_path / "no.env"),
        "PROJECT_ID": "swarm-test-project",
        "FIRESTORE_DATABASE": "swarm",
        # The five-second wait is the production value; a test has no reason
        # to spend it.
        "SWARM_NO_ANSWER_RETRY_DELAY": "0",
    }
    # _ACCESS_TOKEN is set so access_token returns it rather than asking gcloud:
    # this test is about the Firestore request, not about minting a token.
    script = (
        f'source "{COMMON}"; _ACCESS_TOKEN=fake-token; '
        f'rc=0; {fn} || rc=$?; echo "rc=${{rc}}"'
    )
    proc = subprocess.run(
        ["bash", "-c", script, "bash"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    count_file = tmp_path / "curl.count"
    calls = int(count_file.read_text().strip()) if count_file.exists() else 0
    rc_lines = [line for line in proc.stdout.splitlines() if line.startswith("rc=")]
    rc = int(rc_lines[-1][3:]) if rc_lines else proc.returncode
    return rc, proc.stderr, calls


@pytest.mark.parametrize("no_answer", ["28", "7"])
def test_one_no_answer_then_an_answer_succeeds(tmp_path, no_answer):
    rc, stderr, calls = _call(tmp_path, [no_answer, "200"])
    assert calls == 2, (
        f"curl was called {calls} time(s): a curl exit {no_answer} is no answer at "
        "all, and the release on 2026-09-30 died on exactly one of them"
    )
    assert rc == 0, f"the second attempt found the database but the answer was {rc}:\n{stderr}"


def test_two_no_answers_fail_as_a_failure_to_look(tmp_path):
    rc, stderr, calls = _call(tmp_path, ["28", "28"], fn="require_fs_database acceptance")
    assert calls == 2, f"expected exactly one retry after a timeout, curl was called {calls} time(s)"
    assert rc != 0, "two timeouts in a row were reported as a present database"
    assert "failure to LOOK, not proof the database is absent" in stderr, stderr
    assert "Connection timed out" in stderr, (
        "the reason (curl's own line) must still be on stderr:\n" + stderr
    )
    assert "does not exist" not in stderr, (
        "two timeouts were reported as an absent database:\n" + stderr
    )


def test_two_no_answers_are_could_not_tell_not_absent(tmp_path):
    rc, _, calls = _call(tmp_path, ["7", "28"])
    assert calls == 2
    assert rc == 2, f"no answer twice must read as 'could not tell' (2), got {rc}"


@pytest.mark.parametrize("status", ["403", "503"])
def test_an_http_answer_is_never_retried(tmp_path, status):
    # A 200 is scripted second so that a retry would turn this into a pass --
    # the test fails on the property (the call count), not on luck.
    rc, stderr, calls = _call(tmp_path, [status, "200"])
    assert calls == 1, (
        f"Firestore answered {status} and was asked again ({calls} calls): an HTTP "
        "status is an answer, and the owner decision is never to retry one"
    )
    assert rc == 2, f"a {status} must read as 'could not tell' (2), got {rc}:\n{stderr}"


def test_a_404_is_absent_on_the_first_answer(tmp_path):
    rc, _, calls = _call(tmp_path, ["404", "200"])
    assert calls == 1, f"a definite 404 was asked again ({calls} calls)"
    assert rc == 1


def test_a_curl_failure_other_than_no_answer_is_not_retried(tmp_path):
    # Exit 6 (could not resolve the host) is a configuration problem as often
    # as a blip, and it is not one of the two exits the owner named.
    rc, _, calls = _call(tmp_path, ["6", "200"])
    assert calls == 1, f"curl exit 6 was retried ({calls} calls); only 7 and 28 are"
    assert rc == 2
