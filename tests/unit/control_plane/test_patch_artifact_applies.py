"""A served patch still applies when it holds no secret (#370).

WHAT BROKE. `swarm artifact <task> swarm-work.patch` served a patch that
`git apply` and `git apply --3way` both refused. Read-time redaction had masked
ordinary source code in it: `_TOKEN = re.compile(...)` and `_MASK_TOKEN =
json.dumps(MASK)` had their values replaced by `********`, and the identifier
`key_withheld` came out as `key_withhel********` because the JWT rule took `ey`
and eight more characters wherever they stood. A `-` or context line that no
longer matches the file is a hunk that does not apply, so PR #369 had to be
rebuilt by hand from the corrupted text.

WHAT THESE HOLD, through the route a patch is actually served by
(`/v1/tasks/{id}/artifacts/content`, which `swarm artifact` reads):

  1. a patch whose lines NAME credentials -- a call, an attribute, a typed
     parameter, a keyword argument, a dict literal, a shell substitution -- is
     served byte for byte, and `git apply --check` accepts it in a real
     repository; the shell filter (`redact` in scripts/lib/common.sh) leaves it
     whole too;
  2. a patch that HOLDS one still has it masked: the three ways the first
     attempt at this fix unmasked real secrets (review of
     task_a2068d6586ec441fa6e3) are each a case below;
  3. an identifier is never cut mid-word, while a token glued onto a word is
     still masked when it is long enough that no identifier is;
  4. every expression the shell filter hands sed fits the 2048 bytes BSD sed
     (macOS, where operators run it) takes for one -- CI's GNU sed has no such
     limit, so nothing else would notice;
  5. the new exemptions stay linear on 256 KiB an agent chose.

Credential-shaped values are assembled with `_shape` (see
test_log_redaction.py) so no tracked file holds one.
"""

from __future__ import annotations

import difflib
import re
import subprocess
import time
from pathlib import Path

import pytest

from swarm_api.redaction import MASK, redact

from .test_artifact_content_read_path import a_finished_task, get
from .test_log_redaction import REPO, _house_filter_text, _shape

# --------------------------------------------------------------------------
# 1. Code that names a credential is served whole, and the patch applies
# --------------------------------------------------------------------------

#: The lines #370 named, and the review's two commonest shapes in this
#: repository (a typed parameter, a keyword argument), before and after.
MODULE_BEFORE = """\
import json
import re

_TOKEN = re.compile(r"[a-z]+")
_MASK_TOKEN = json.dumps("mask")


def probe(*, url: str, token: str | None = None, timeout: int = 5) -> dict:
    key_withheld = token is None
    try:
        return fetch(url, token=token_value, withheld=key_withheld)
    except KeyboardInterrupt:
        return {"next_page_token": None}
"""

MODULE_AFTER = """\
import json
import re

_TOKEN = re.compile(r"[a-z0-9]+")
_MASK_TOKEN = json.dumps(MASK)
eye_tracking = True


def probe(*, url: str, token: str | None = None, secret: SecretStr | None = None) -> dict:
    key_withheld = token is None or secret is None
    try:
        return fetch(url, token=token_value, secret=settings.client_secret, withheld=key_withheld)
    except KeyboardInterrupt:
        return {"next_page_token": page.next_page_token, "credential": issue(ref)}
"""

SCRIPT_BEFORE = """\
#!/usr/bin/env bash
set -euo pipefail
TOKEN="$(gh auth token)"
SECRET="${SWARM_SECRET:-}"
"""

SCRIPT_AFTER = """\
#!/usr/bin/env bash
set -euo pipefail
TOKEN="$(gh auth token --hostname github.com)"
SECRET="${SWARM_SECRET:-}"
export TOKEN SECRET
"""

FILES_BEFORE = {"pkg/probe.py": MODULE_BEFORE, "scripts/login.sh": SCRIPT_BEFORE}
FILES_AFTER = {"pkg/probe.py": MODULE_AFTER, "scripts/login.sh": SCRIPT_AFTER}


def _unified_diff(before: dict[str, str], after: dict[str, str]) -> str:
    """A `git diff`-shaped patch of every file, as a worker's swarm-work.patch is."""
    out: list[str] = []
    for name in before:
        out.append(f"diff --git a/{name} b/{name}\n")
        out.extend(
            difflib.unified_diff(
                before[name].splitlines(keepends=True),
                after[name].splitlines(keepends=True),
                fromfile=f"a/{name}",
                tofile=f"b/{name}",
            )
        )
    return "".join(out)


PATCH = _unified_diff(FILES_BEFORE, FILES_AFTER)


def _git(repo: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *argv], capture_output=True, text=True, timeout=60
    )


def _repository(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return repo


def test_the_patch_under_test_changes_every_line_370_named():
    """The control: the patch really touches the lines that broke, on both
    its `-` and `+` sides, so a filter that masked them could not pass below."""
    removed = [line[1:] for line in PATCH.splitlines() if line.startswith("-") and not line.startswith("---")]
    added = [line[1:] for line in PATCH.splitlines() if line.startswith("+") and not line.startswith("+++")]
    for needle in ("_TOKEN = re.compile", "_MASK_TOKEN = json.dumps", "key_withheld", "token: str | None"):
        assert any(needle in line for line in removed), needle
        assert any(needle in line for line in added), needle
    assert any("token=token_value" in line for line in added)
    assert any("eye_tracking" in line for line in added)
    assert any('TOKEN="$(gh auth token' in line for line in removed)


def test_a_patch_of_code_that_names_credentials_is_served_whole_and_applies(
    client, db, objects, tmp_path
) -> None:
    """THE #370 TEST: the patch served by the route applies to the tree it was
    made against, and applying it gives exactly the changed files."""
    a_finished_task(db, objects, files={"swarm-work.patch": PATCH})

    body = get(client, "task_a", "swarm-work.patch").json()

    assert body["status"] == "ok", body
    assert body["truncated"] is False
    served = body["content"]
    changed = [
        (want, got)
        for want, got in zip(PATCH.splitlines(), served.splitlines())
        if want != got
    ]
    assert not changed, f"redaction changed code lines of the patch: {changed}"
    assert served == PATCH
    assert body["redaction_count"] == 0

    repo = _repository(tmp_path, FILES_BEFORE)
    (tmp_path / "served.patch").write_text(served)
    check = _git(repo, "apply", "--check", str(tmp_path / "served.patch"))
    assert check.returncode == 0, check.stderr
    applied = _git(repo, "apply", str(tmp_path / "served.patch"))
    assert applied.returncode == 0, applied.stderr
    for name, text in FILES_AFTER.items():
        assert (repo / name).read_text() == text, name


def test_the_shell_filter_leaves_the_same_patch_whole():
    """`redact` in scripts/lib/common.sh is the same rule in the terminal: a
    patch piped through it must apply too."""
    assert _house_filter_text(PATCH.rstrip("\n")) == PATCH.rstrip("\n")


# --------------------------------------------------------------------------
# 2. A literal secret in a patch is still masked
# --------------------------------------------------------------------------

#: (line, the part that must never be served). Each is a way the FIRST attempt
#: at #370 served a real secret, or a literal shape the exemptions sit next to.
LITERALS = [
    # A letters-only value after a spaced `=`: nothing bounded the first
    # attempt's bare-name exemption.
    ("TOKEN = " + _shape("qZrLmXvTbQwErTy", "UiOpAsDfGhJkLzXcVbNmP"), "qZrLmXvTbQwErTy"),
    ("token = " + _shape("qwertyuiopasdfgh", "jklzxcvbnmqwerty"), "qwertyuiopasdfgh"),
    # A dotted value: random parts, and plain words.
    ("token = " + _shape("q8Zr7Lm2Xv9T", ".", "aB3cD4eF5gH6"), "q8Zr7Lm2Xv9T"),
    ("password = correct.horse.battery", "horse"),
    # INI / my.cnf: a human's password is a word, a snake_case or camelCase run.
    ("password = hunter", "hunter"),
    ("password = my_dog_rex", "my_dog_rex"),
    ("password=myDogRex", "myDogRex"),
    # An environment dump: every value is a literal, whatever it looks like.
    ("GITHUB_TOKEN=unset_me_now_123", "unset_me_now"),
    ("DB_PASSWORD=MyDogRex", "MyDogRex"),
    # YAML after a colon: a bare word is a value, not a type.
    ("secret: missing_link_42", "missing_link"),
    ("password: Summer2024", "Summer2024"),
    # A CLI flag, and a quoted literal.
    ("--token=abcdefghqrst", "abcdefghqrst"),
    ('REFRESH_TOKEN = "plain-quoted-literal-77"', "plain-quoted"),
]


def test_a_literal_secret_in_a_patch_is_still_masked(client, db, objects) -> None:
    patch = "".join(
        [
            "diff --git a/settings.ini b/settings.ini\n",
            "--- a/settings.ini\n",
            "+++ b/settings.ini\n",
            f"@@ -0,0 +1,{len(LITERALS)} @@\n",
            *(f"+{line}\n" for line, _ in LITERALS),
        ]
    )
    a_finished_task(db, objects, files={"swarm-work.patch": patch})

    body = get(client, "task_a", "swarm-work.patch").json()

    assert body["status"] == "ok", body
    for line, secret in LITERALS:
        assert secret not in body["content"], f"served {secret!r} from {line!r}"
    assert body["redaction_count"] >= len(LITERALS)


@pytest.mark.parametrize("line,secret", LITERALS, ids=[s for _, s in LITERALS])
def test_both_filters_mask_each_literal(line, secret):
    python = redact(line)
    shell = _house_filter_text(line)
    assert secret not in python.text, python.text
    assert secret not in shell, shell
    assert python.text == shell, (python.text, shell)
    assert python.count >= 1


# --------------------------------------------------------------------------
# 3. An identifier is never cut mid-word; a glued token is still masked
# --------------------------------------------------------------------------

IDENTIFIERS = [
    "withheld = withheld or key_withheld",
    "eye_tracking = True; eyebrow_raise = 0",
    "except KeyboardInterrupt: return KeychainStore()",
    "client = kms.KeyManagementServiceClient()",
    "validate_key_material(api_key)",
    "task_id: 'task-bbbbbbbbbbbbbbbb',",
]


@pytest.mark.parametrize("line", IDENTIFIERS)
def test_an_identifier_is_never_cut_mid_word(line):
    python = redact(line)
    assert python.text == line, python.text
    assert python.count == 0
    assert _house_filter_text(line) == line


#: Long enough that no identifier is: `eyJ` and 37 more, `sk-` and 32 more.
GLUED = [
    (_shape("A", "eyJ", "hbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9", ".eyJzdWIiOiIxMjM0NSJ9.c2ln"), "InR5cCI6Ikp"),
    (_shape("X", "sk-", "proj-Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9Jj0KkLlMm"), "Dd4Ee5Ff6Gg7"),
    (_shape("task-", "sk-", "proj-Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9Jj0Kk.md"), "Dd4Ee5Ff6Gg7"),
]


@pytest.mark.parametrize("line,secret", GLUED, ids=["jwt", "openai", "openai-in-a-name"])
def test_a_token_glued_onto_a_word_is_still_masked(line, secret):
    python = redact(line)
    shell = _house_filter_text(line)
    assert secret not in python.text, python.text
    assert secret not in shell, shell
    assert python.text == shell
    assert MASK in python.text


# --------------------------------------------------------------------------
# 4. Every sed expression fits BSD sed
# --------------------------------------------------------------------------

#: `sed: 1: "s/...": unbalanced brackets ([])` -- measured on macOS, where an
#: expression of 2077 bytes was cut at 2048 and failed to compile. A margin
#: below the limit, so a small addition does not land exactly on it.
BSD_SED_EXPRESSION_MAX = 2000


def _expanded_sed_expressions() -> list[str]:
    """The `-e` arguments `redact` hands sed, with every variable expanded."""
    source = (REPO / "scripts/lib/common.sh").read_text()
    match = re.search(r"^redact\(\) \{\n.*?^\}\n", source, re.MULTILINE | re.DOTALL)
    assert match, "redact() is no longer where this test expects it in common.sh"
    script = match.group(0) + "sed() { printf '%s\\0' \"$@\"; cat >/dev/null; }\necho x | redact\n"
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    argv = done.stdout.split("\0")
    return [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-e"]


def test_every_sed_expression_fits_the_bsd_sed_limit():
    expressions = _expanded_sed_expressions()
    assert len(expressions) >= 15, expressions
    long = [(len(e.encode()), e[:60]) for e in expressions if len(e.encode()) > BSD_SED_EXPRESSION_MAX]
    assert not long, long


# --------------------------------------------------------------------------
# 5. Linear on what an agent can write
# --------------------------------------------------------------------------

_SIZE = 256 * 1024


@pytest.mark.parametrize(
    "text",
    [
        "token = " + "ab." * (_SIZE // 3),
        "token = " + "a_b." * (_SIZE // 4),
        "token = " + "aB" * (_SIZE // 2),
        "token: " + "Ab" * (_SIZE // 2),
        "token=a_b " * (_SIZE // 10),
        "token = " + "a." * (_SIZE // 2) + "(",
        "x" + "eyJa" * (_SIZE // 4),
        "a" * _SIZE + "token=",
    ],
    ids=["dotted", "dotted-snake", "camel", "annotation", "many-keys", "call", "glued-jwt", "long-name"],
)
def test_the_exemptions_stay_linear_on_256_kib(text):
    """Measured at 0.1 s each; a quadratic look-ahead takes tens of seconds,
    so the bound is loose enough for a loaded runner and still catches it."""
    started = time.monotonic()
    redact(text)
    assert time.monotonic() - started < 10.0
