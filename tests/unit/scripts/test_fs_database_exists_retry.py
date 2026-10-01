"""`fs_database_exists` in scripts/lib/common.sh keeps asking Firestore while
its requests get no answer at all, for a bounded budget, and never asks again
once it got one.

THE FAILURES THIS PINS. On 2026-09-30 the release's acceptance pre-flight
(`require_platform` in scripts/lib/testlib.sh -> `require_fs_database` ->
`fs_database_exists`) died with

    curl: (28) Connection timed out after 30002 milliseconds

against a database that was there. #398 answered with one retry five seconds
later, and the next release died the same way: execution swarm-verify-fg44d
timed out at 22:24:06 and its retry timed out too, ending at 22:25:11 (#401).
The subnet's flow logs show why one retry is not enough. For 30 to 90 s after
some Cloud Run instances start, their packets to Google APIs leave and nothing
comes back, while the same address answers other instances at the same
moment. A retry inside that window gets the same silence.

Owner decision on #401, 2026-09-30: keep retrying a no-answer -- curl exit 28
(timed out) or 7 (could not connect) -- every SWARM_NO_ANSWER_RETRY_DELAY
seconds (10) until SWARM_NO_ANSWER_BUDGET_SECONDS (120) are spent, counting
curl's own --max-time so that no attempt is started that could end past the
budget. NEVER retry an HTTP status: a 403 or a 404 is Firestore answering, and
asking again gets the same answer. Once the budget is spent the call fails
exactly as before, with the "failure to LOOK, not proof the database is
absent" message.

Nothing here touches the network or waits. common.sh is sourced and the real
function called, against a fake `curl`, `sleep` and `date` on PATH that share
one virtual clock: a curl exit 28 costs its 30 s --max-time, a `sleep N` costs
N, and `date +%s` reads the clock. So the production values themselves are
exercised, in milliseconds.
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

#: What fs_database_exists passes as curl's --max-time (HTTP_TIMEOUT's default).
CURL_MAX_TIME = 30

#: The production defaults, restated: the numbers the owner chose on #401.
DEFAULT_BUDGET = 120
DEFAULT_DELAY = 10

# One scripted answer per call, read from line N of curl.answers on the Nth
# call. The no-answer lines are the text curl really prints for those exits.
# A 28 is a timeout, so it advances the shared clock by the --max-time it
# waited out; every other answer is instant.
FAKE_CURL = r"""#!/usr/bin/env bash
n=$(( $(cat "${FAKE_DIR}/curl.count" 2>/dev/null || echo 0) + 1 ))
printf '%s\n' "${n}" >"${FAKE_DIR}/curl.count"
now="$(cat "${FAKE_DIR}/clock")"
printf '%s\n' "${now}" >>"${FAKE_DIR}/curl.starts"
answer="$(sed -n "${n}p" "${FAKE_DIR}/curl.answers")"
case "${answer}" in
  7)   echo "curl: (7) Failed to connect to firestore.googleapis.com port 443 after 3 ms: Couldn't connect to server" >&2; exit 7 ;;
  28)  printf '%s\n' "$(( now + 30 ))" >"${FAKE_DIR}/clock"
       echo "curl: (28) Connection timed out after 30002 milliseconds" >&2; exit 28 ;;
  6)   echo "curl: (6) Could not resolve host: firestore.googleapis.com" >&2; exit 6 ;;
  200) printf '{"name":"projects/swarm-test-project/databases/swarm","type":"FIRESTORE_NATIVE"}\n' ;;
  403|404|503)
       printf '{"error":{"code":%s,"message":"fake answer %s","status":"FAKE"}}\n' "${answer}" "${answer}" ;;
  *)   echo "fake curl: no answer scripted for call ${n}" >&2; exit 99 ;;
esac
"""

# `sleep` is an external command in bash, so this one on PATH is the one the
# helper runs. It advances the clock instead of waiting.
FAKE_SLEEP = r"""#!/usr/bin/env bash
printf '%s\n' "$1" >>"${FAKE_DIR}/sleeps"
printf '%s\n' "$(( $(cat "${FAKE_DIR}/clock") + ${1%.*} ))" >"${FAKE_DIR}/clock"
"""

# `date +%s` reads the shared clock; any other format is the real date.
FAKE_DATE = r"""#!/usr/bin/env bash
if [[ "$#" -eq 1 && "$1" == "+%s" ]]; then
  cat "${FAKE_DIR}/clock"
  exit 0
fi
PATH="${REAL_PATH}" exec date "$@"
"""

START = 1_000_000


def _install(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _call(tmp_path: Path, answers: list[str], fn: str = "fs_database_exists",
          extra_env: dict[str, str] | None = None):
    """Source common.sh, call FN, and return what happened.

    Returns (rc, stderr, curl call count, the clock at each curl start
    relative to the first, the sleeps, the clock at the end relative to the
    first start).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install(bin_dir, "curl", FAKE_CURL)
    _install(bin_dir, "sleep", FAKE_SLEEP)
    _install(bin_dir, "date", FAKE_DATE)
    (tmp_path / "curl.answers").write_text("\n".join(answers) + "\n")
    (tmp_path / "clock").write_text(f"{START}\n")

    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "REAL_PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "FAKE_DIR": str(tmp_path),
        "NO_COLOR": "1",
        "SWARM_ENV_FILE": str(tmp_path / "no.env"),
        "PROJECT_ID": "swarm-test-project",
        "FIRESTORE_DATABASE": "swarm",
    }
    env.update(extra_env or {})
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

    def lines(name: str) -> list[str]:
        path = tmp_path / name
        return path.read_text().split() if path.exists() else []

    count = lines("curl.count")
    calls = int(count[-1]) if count else 0
    starts = [int(s) - START for s in lines("curl.starts")]
    sleeps = [float(s) for s in lines("sleeps")]
    end = int(lines("clock")[-1]) - START
    rc_lines = [line for line in proc.stdout.splitlines() if line.startswith("rc=")]
    rc = int(rc_lines[-1][3:]) if rc_lines else proc.returncode
    return rc, proc.stderr, calls, starts, sleeps, end


# ---------------------------------------------------------------------------
# a no-answer that ends inside the budget costs nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("no_answer", ["28", "7"])
def test_one_no_answer_then_an_answer_succeeds(tmp_path, no_answer):
    rc, stderr, calls, _, _, _ = _call(tmp_path, [no_answer, "200"])
    assert calls == 2, (
        f"curl was called {calls} time(s): a curl exit {no_answer} is no answer at "
        "all, and the release on 2026-09-30 died on exactly one of them"
    )
    assert rc == 0, f"the second attempt found the database but the answer was {rc}:\n{stderr}"


def test_three_timeouts_then_an_answer_succeeds_within_the_budget(tmp_path):
    """#401: one retry was not enough. Three 30 s timeouts are 90 s of silence,
    the long end of what the flow logs measured; the fourth request answers."""
    rc, stderr, calls, starts, _, end = _call(
        tmp_path, ["28", "28", "28", "200"],
        extra_env={"SWARM_NO_ANSWER_RETRY_DELAY": "0"},
    )
    assert calls == 4, f"expected four requests, curl was called {calls} time(s):\n{stderr}"
    assert rc == 0, f"the fourth request found the database but the answer was {rc}:\n{stderr}"
    assert end <= DEFAULT_BUDGET, f"the retries ran {end}s, past the {DEFAULT_BUDGET}s budget"


def test_each_retry_is_one_warning_naming_the_elapsed_seconds(tmp_path):
    _, stderr, calls, starts, _, _ = _call(
        tmp_path, ["28", "28", "200"],
        extra_env={"SWARM_NO_ANSWER_RETRY_DELAY": "0"},
    )
    assert calls == 3, stderr
    warnings = [line for line in stderr.splitlines() if line.startswith("warn") and "no answer" in line]
    assert len(warnings) == 2, "one warning line per retry, got:\n" + stderr
    # The elapsed seconds since the first request, at the moment of each retry.
    assert "30s" in warnings[0], warnings[0]
    assert "60s" in warnings[1], warnings[1]


# ---------------------------------------------------------------------------
# the budget, with the production values
# ---------------------------------------------------------------------------


def test_the_default_budget_never_starts_a_request_that_could_end_past_it(tmp_path):
    """Defaults: retry every 10 s for 120 s, each request allowed its 30 s.

    Timeouts at 0-30, 40-70 and 80-110 s; a fourth would start at 120 and could
    run to 150, so it is not started. The total stays inside the budget.
    """
    rc, stderr, calls, starts, sleeps, end = _call(tmp_path, ["28"] * 10, fn="require_fs_database acceptance")
    assert starts == [0, 40, 80], starts
    assert sleeps == [DEFAULT_DELAY, DEFAULT_DELAY], sleeps
    assert all(s + CURL_MAX_TIME <= DEFAULT_BUDGET for s in starts), starts
    assert end <= DEFAULT_BUDGET, end
    assert rc != 0
    assert "failure to LOOK, not proof the database is absent" in stderr, stderr


def test_fast_no_answers_are_asked_every_delay_until_the_budget(tmp_path):
    """A refused connect (7) costs no time, so the delay alone paces the
    retries, and the last one still starts no later than budget - max-time."""
    _, _, calls, starts, _, _ = _call(tmp_path, ["7"] * 20)
    assert starts == list(range(0, DEFAULT_BUDGET - CURL_MAX_TIME + 1, DEFAULT_DELAY)), starts
    assert calls == len(starts)


# ---------------------------------------------------------------------------
# a no-answer that outlasts the budget fails as before
# ---------------------------------------------------------------------------


def test_no_answer_past_the_budget_fails_as_a_failure_to_look(tmp_path):
    rc, stderr, calls, starts, _, end = _call(
        tmp_path, ["28"] * 10, fn="require_fs_database acceptance",
        extra_env={"SWARM_NO_ANSWER_RETRY_DELAY": "0", "SWARM_NO_ANSWER_BUDGET_SECONDS": "60"},
    )
    # 0-30 and 30-60 fit in 60 s; a third request could end at 90.
    assert calls == 2, f"expected two requests inside a 60 s budget, curl was called {calls} time(s)"
    assert end <= 60, end
    assert rc != 0, "a spent budget of timeouts was reported as a present database"
    assert "failure to LOOK, not proof the database is absent" in stderr, stderr
    assert "Connection timed out" in stderr, (
        "the reason (curl's own line) must still be on stderr:\n" + stderr
    )
    assert "does not exist" not in stderr, (
        "a spent budget of timeouts was reported as an absent database:\n" + stderr
    )


def test_no_answers_past_the_budget_are_could_not_tell_not_absent(tmp_path):
    rc, _, calls, _, _, _ = _call(
        tmp_path, ["7", "28", "28", "28"],
        extra_env={"SWARM_NO_ANSWER_RETRY_DELAY": "0", "SWARM_NO_ANSWER_BUDGET_SECONDS": "60"},
    )
    # 7 at 0, 28 from 0 to 30, 28 from 30 to 60: a fourth could end at 90.
    assert calls == 3, calls
    assert rc == 2, f"no answer through the whole budget must read as 'could not tell' (2), got {rc}"


# ---------------------------------------------------------------------------
# what is never retried
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["403", "503"])
def test_an_http_answer_is_never_retried(tmp_path, status):
    # A 200 is scripted second so that a retry would turn this into a pass --
    # the test fails on the property (the call count), not on luck.
    rc, stderr, calls, _, _, _ = _call(tmp_path, [status, "200"])
    assert calls == 1, (
        f"Firestore answered {status} and was asked again ({calls} calls): an HTTP "
        "status is an answer, and the owner decision is never to retry one"
    )
    assert rc == 2, f"a {status} must read as 'could not tell' (2), got {rc}:\n{stderr}"


def test_an_http_answer_after_a_timeout_is_not_retried(tmp_path):
    rc, _, calls, _, _, _ = _call(tmp_path, ["28", "403", "200"])
    assert calls == 2, f"the 403 after a timeout was asked again ({calls} calls)"
    assert rc == 2


def test_a_404_is_absent_on_the_first_answer(tmp_path):
    rc, _, calls, _, _, _ = _call(tmp_path, ["404", "200"])
    assert calls == 1, f"a definite 404 was asked again ({calls} calls)"
    assert rc == 1


def test_a_curl_failure_other_than_no_answer_is_not_retried(tmp_path):
    # Exit 6 (could not resolve the host) is a configuration problem as often
    # as a blip, and it is not one of the two exits the owner named.
    rc, _, calls, _, _, _ = _call(tmp_path, ["6", "200"])
    assert calls == 1, f"curl exit 6 was retried ({calls} calls); only 7 and 28 are"
    assert rc == 2
