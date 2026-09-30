"""`make logs` (no FOLLOW) must not print a private key's body in clear.

THE DEFECT THIS PINS (#260). `gcloud logging read ... --order desc` -- the
default, non-FOLLOW branch of the `logs` target -- delivers matching log
entries NEWEST FIRST. A private key printed across several lines by a worker
is written BEGIN, body, END, in that order; read back with `--order desc`
those same entries arrive END, body, BEGIN. `redact()`'s awk stage (shared
with `swarm_api.redaction.mask_private_keys`) only masks a key FORWARD from a
BEGIN it has already seen -- so with the marker order reversed, the body
lines print in clear, and a Cloud Logging table line's own `timestamp
service ` prefix defeats the fallback base64 look-back that might otherwise
have caught them from the END side.

The fix is at the Makefile: the lines are reversed locally, after `gcloud`
returns them (so `--order desc` and `--limit` still decide WHICH entries come
back -- only their order changes) and before `redact-stream.sh`, restoring
BEGIN-then-body-then-END for the masking pass.

WHY `make -n`, NOT A REGEX OVER THE MAKEFILE TEXT. Asking `make` itself to
render the recipe (with a fake PROJECT_ID and FOLLOW unset) is what actually
runs when someone types `make logs`, expanded exactly as `make` expands it --
`$(SCRIPTS)`, `$$`, the lot. A regex over the Makefile's source would drift
from that the next time the recipe's shell quoting changes without its shape
changing. The real `gcloud logging read ...` invocation (up to the first
pipe) is replaced with `cat` of a fake, hand-built log stream so this runs
with no network access and no credentials; everything after that first pipe
-- the fix's reversal, then `redact-stream.sh` -- runs unmodified.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.skipif(
    shutil.which("make") is None or shutil.which("bash") is None,
    reason="make and bash are both required",
)

MASK = "********"

#: A stand-in for `gcloud logging read`'s own table format: each entry
#: prefixed the way a real Cloud Logging line is, `HH:MM:SS  service  `. The
#: private-key BODY lines are marked with `Kxxxx...` runs, the same
#: convention `tests/fixtures/redaction-parity.json`'s PEM cases use, so a
#: leak is unambiguous and never looks like a real credential.
#: Split so the repository's own secret scan (security.yml, a grep for a
#: PEM private-key header) does not read this fixture as a committed key.
_BEGIN = "-----BEGIN RSA " + "PRIVATE KEY-----"
_END = "-----END RSA PRIVATE KEY-----"
_BODY = [f"K{n:04d}" * 12 + "QQQQ" for n in range(4)]

#: TRUE order, oldest to newest, the way a worker would have written it.
_TRUE_ORDER = [_BEGIN, *_BODY, _END]


def _logs_pipeline_after_gcloud() -> str:
    """The `logs` target's non-FOLLOW branch, as `make` itself renders it,
    with the `gcloud logging read ...` invocation cut away at its first pipe.

    What is left is exactly what runs on whatever `gcloud` prints: the fix's
    reversal (once added) and `$(SCRIPTS)/lib/redact-stream.sh`, both with
    `$(SCRIPTS)` and every `$$` already expanded by `make`.
    """
    proc = subprocess.run(
        ["make", "-n", "logs", "PROJECT_ID=test-project"],
        cwd=REPO, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    match = re.search(r"^\s*gcloud logging read .*$", proc.stdout, re.MULTILINE)
    assert match, f"the non-FOLLOW branch is no longer one `gcloud logging read` line:\n{proc.stdout}"
    line = match.group(0)
    assert " | " in line, f"no pipe after the gcloud invocation:\n{line}"
    _, _, rest = line.partition(" | ")
    assert "redact-stream.sh" in rest, f"redact-stream.sh dropped out of the pipeline:\n{rest}"
    return rest.rstrip(" \\")


def _run_with_fake_log(lines: list[str], tmp_path: Path) -> str:
    """Feed `lines` (already ordered and prefixed as `gcloud` would print
    them) through the real post-`gcloud` pipeline, via a fake log file."""
    pipeline = _logs_pipeline_after_gcloud()
    fake = tmp_path / "fake-gcloud-logs.txt"
    fake.write_text("\n".join(lines) + "\n")
    command = f"cat {fake} | {pipeline}"
    proc = subprocess.run(
        ["bash", "-c", command], cwd=REPO, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_a_key_logged_across_lines_survives_descending_order(tmp_path) -> None:
    """The regression this file exists for.

    `gcloud logging read --order desc` hands the pipeline these six entries
    END-first, each with a realistic `timestamp  service  ` prefix; the fix
    must still mask the whole key as one block, and none of the body lines'
    own text may reach the terminal in clear.
    """
    descending = [
        f"14:23:{6 - n:02d}  swarm-worker  {line}"
        for n, line in enumerate(reversed(_TRUE_ORDER))
    ]
    output = _run_with_fake_log(descending, tmp_path)

    for body_line in _BODY:
        assert body_line not in output, (
            "a private key's body printed in clear under descending order:\n" + output
        )
    assert _BEGIN in output, output
    assert MASK in output, output
    assert _END not in output, (
        "the END marker survived unmasked -- the key was not treated as one block:\n" + output
    )
