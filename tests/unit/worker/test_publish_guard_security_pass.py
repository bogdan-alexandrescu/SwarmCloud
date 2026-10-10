"""The publish guard's security pass (owner decision 2026-10-09, lane SEC-GUARD).

Seven boxes of epics #346 and #361, each a way a credential -- or a value
shaped like one -- reached the forge or a refusal reason past the worker's
publish guard (`lifecycle._credential_in`, `final_tree_leak`,
`_first_leaking_commit`):

* #346 box 40: the clean publish repository's `.git/config` was not
  re-checked after the second reap, right before the credential-bearing push.
* #361 box 71: the refusal reason echoed the agent-chosen file path, scrubbed
  only of registered secrets.
* #361 box 72: `_PLACEHOLDER` was a substring match, so a real value merely
  containing `test` read as a fixture in a test path.
* #361 box 73: `node_modules/**/test/*.js` and `vendor/**/tests/*` got the
  loose tier.
* #361 box 74: a vendor token with `test` or `EXAMPLE` glued inside it passed
  in a test path.
* #361 box 79: a key body put between an existing, unchanged BEGIN/END pair
  added no marker under `-U0`, so it was published.
* #361 box 80: traditional encrypted key ciphertext in a variable, with its
  headers and markers elsewhere, was published.

The test-path tier itself stays (owner decision): these tighten what slipped
through it. Every credential-shaped value below is ASSEMBLED at run time, so
this file adds no line the worker's own scan or trivy takes for a real one.
"""

from __future__ import annotations

import base64
import random
import shutil
import string
import subprocess
from pathlib import Path

import pytest

from agent_worker import lifecycle, workspace as workspace_mod

from test_strategy_end_to_end import (  # noqa: F401 - fixtures are used by name
    forge,
    local_urls,
    origin,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

_RNG = random.Random(1009)
_ALNUM = string.ascii_letters + string.digits
_UPPER_DIGITS = string.ascii_uppercase + string.digits
_PW = "pass" + "word"


def _shape(*parts: str) -> str:
    """Join parts into one value, so the source never holds it whole."""
    return "".join(parts)


def _random_token(length: int, alphabet: str = _ALNUM, digits: int = 4) -> str:
    """`length` distinct characters, `digits` of them digits: credential-shaped
    by construction (high entropy, letters and digits mixed)."""
    letters = [c for c in alphabet if not c.isdigit()]
    numbers = [c for c in alphabet if c.isdigit()]
    chars = _RNG.sample(numbers, digits) + _RNG.sample(letters, length - digits)
    _RNG.shuffle(chars)
    return "".join(chars)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _marker(edge: str, kind: str = " RSA") -> str:
    return _shape("-----", edge, kind, " PRIVATE", " KEY", "-----")


def _rule(path: str, text: str) -> str | None:
    hit = lifecycle._credential_in(path, text)
    return None if hit is None else hit.rule


class _QuietLog:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


def _git(repo: Path, *args: str) -> str:
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    return subprocess.run(
        ["git", *ident, *args], cwd=str(repo), check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main", ".")
    (tmp_path / "logs").mkdir(exist_ok=True)
    return repo


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _final(repo: Path, tmp_path: Path, base: str) -> str | None:
    return lifecycle.final_tree_leak(
        repo=repo, base=base, leaks=lifecycle._credential_in, private_dir=tmp_path,
        logs_dir=tmp_path / "logs", timeout_seconds=60, logger=_QuietLog(),
    )


def _first(repo: Path, tmp_path: Path, shas: list[str], floor: str) -> int | None:
    return lifecycle._first_leaking_commit(
        shas=shas, keep=None, leaks=lifecycle._credential_in, git=["git"], repo=repo,
        private_dir=tmp_path, logs_dir=tmp_path / "logs", timeout_seconds=60,
        logger=_QuietLog(), floor=floor,
    )


# -- #346 box 40: the publish repository's config is re-checked after the second reap


def test_a_publish_repo_config_changed_before_the_second_reap_publishes_nothing(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The harvest builds the clean publish repository after the first reap;
    an agent process alive between then and the second reap could rewrite its
    `.git/config` (a `core.sshCommand`, a credential helper, a remote) and the
    push would read it with the tenant token in hand. The second reap is now
    followed by a check that the config is byte-for-byte what the worker built."""
    worker, config, _ = worker_factory(
        task_id="t-cfg", attempt_id="att-t-cfg", lease_id="lease-t-cfg",
        repository_url=f"file://{origin}",
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": "t-cfg", "metadata": {"dispatch": {"strategy": "direct-pr"}}}
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    assert worker._maybe_clone(task) is not None

    built: list[Path] = []
    real_prepare = lifecycle.prepare_publish_repo

    def recording_prepare(**kwargs):
        path = real_prepare(**kwargs)
        built.append(Path(path))
        return path

    monkeypatch.setattr(lifecycle, "prepare_publish_repo", recording_prepare)
    calls = {"n": 0}

    def reap() -> tuple[int, ...]:
        calls["n"] += 1
        if calls["n"] == 2:
            # What a process that escaped the first reap would do just before
            # the second one killed it.
            with (built[-1] / ".git" / "config").open("a") as handle:
                handle.write("[core]\n\tsshCommand = sh -c 'touch /tmp/pwned'\n")
        return ()

    worker.reap_before_publish = reap
    clone = worker.ws.work / lifecycle.REPO_DIR_NAME
    (clone / "a.txt").write_text("a\n")
    out = worker._harvest_git(publish=True)

    assert calls["n"] >= 2, "the publish never reached the second reap"
    assert out["published"] is False, out
    assert ".git/config" in out["publish_reason"], out["publish_reason"]
    assert not forge.pulls, "a pull request was opened"


def test_an_unchanged_publish_repo_config_still_publishes(
    worker_factory, monkeypatch, origin, local_urls, forge
):
    """The control: with nothing touching the config, the same flow publishes."""
    worker, config, _ = worker_factory(
        task_id="t-cfg-ok", attempt_id="att-t-cfg-ok", lease_id="lease-t-cfg-ok",
        repository_url=f"file://{origin}",
    )
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    task = {"task_id": "t-cfg-ok", "metadata": {"dispatch": {"strategy": "direct-pr"}}}
    worker._task = task
    monkeypatch.setattr(worker, "_git_token", lambda: "not-a-real-token")
    assert worker._maybe_clone(task) is not None
    worker.reap_before_publish = lambda: ()
    (worker.ws.work / lifecycle.REPO_DIR_NAME / "a.txt").write_text("a\n")
    out = worker._harvest_git(publish=True)
    assert out["published"] is True, out.get("publish_reason")


# -- #361 box 71: the refusal reason does not echo a credential-shaped path ---


def _aws_key() -> str:
    return _shape("AK", "IA", _random_token(16, _UPPER_DIGITS))


def test_a_token_shaped_file_name_is_not_echoed_in_the_refusal(tmp_path):
    repo = _repo(tmp_path)
    (repo / "README").write_text("x\n")
    base = _commit_all(repo, "base")
    token = _shape("gh", "p_", _random_token(36))
    (repo / f"{token}.txt").write_text(f"k = '{_aws_key()}'\n")
    _commit_all(repo, "add")

    reason = _final(repo, tmp_path, base)
    assert reason is not None
    assert "aws_access_key_id" in reason
    assert token not in reason, reason
    assert token[8:] not in reason, reason


def test_a_high_entropy_path_segment_is_not_echoed_in_the_refusal(tmp_path):
    repo = _repo(tmp_path)
    (repo / "README").write_text("x\n")
    base = _commit_all(repo, "base")
    secret_dir = _random_token(32)
    (repo / secret_dir).mkdir()
    (repo / secret_dir / "cfg.txt").write_text(f"k = '{_aws_key()}'\n")
    _commit_all(repo, "add")

    reason = _final(repo, tmp_path, base)
    assert reason is not None
    assert secret_dir not in reason, reason
    assert "cfg.txt" in reason, reason


def test_a_very_long_path_is_cut_in_the_refusal(tmp_path):
    repo = _repo(tmp_path)
    (repo / "README").write_text("x\n")
    base = _commit_all(repo, "base")
    deep = repo
    for index in range(12):
        deep = deep / (f"d{index:02d}" + "a" * 180)
    deep.mkdir(parents=True)
    (deep / "cfg.txt").write_text(f"k = '{_aws_key()}'\n")
    _commit_all(repo, "add")

    reason = _final(repo, tmp_path, base)
    assert reason is not None
    assert len(reason) < 600, len(reason)
    assert "cfg.txt" in reason, reason


def test_an_ordinary_path_is_still_named_in_full(tmp_path):
    repo = _repo(tmp_path)
    (repo / "README").write_text("x\n")
    base = _commit_all(repo, "base")
    (repo / "src").mkdir()
    (repo / "src" / "settings.py").write_text(f"k = '{_aws_key()}'\n")
    _commit_all(repo, "add")

    assert _final(repo, tmp_path, base) == (
        "the final tree adds a credential in src/settings.py (rule aws_access_key_id, line 1); remove it"
    )


# -- #361 box 72: a placeholder word must be a whole word, and the rest no secret


@pytest.mark.parametrize(
    "value",
    [
        lambda: _shape("db-test-", _random_token(24)),
        lambda: _shape(_random_token(20), "test"),
        lambda: _shape("Latest", _random_token(20)),
        lambda: _shape("fake", _random_token(20)),
        lambda: _shape(_random_token(20), "EXAMPLE"),
        lambda: _shape("xxxx", _random_token(20)),
    ],
    ids=["db-test-host", "glued-test", "latest", "glued-fake", "glued-example", "x-run-prefix"],
)
def test_a_real_value_holding_a_placeholder_word_is_refused_in_tests(value):
    text = f'conn = {{"{_PW}": "{value()}"}}\n'
    assert _rule("tests/unit/test_db.py", text) == "key_value_assignment"


@pytest.mark.parametrize(
    "value",
    ["test-" + _PW + "-123", "fake-token", "dummy", "<" + _PW + ">", "********", "${DB_PW}",
     "example-value-0001"],
)
def test_a_value_that_is_only_a_placeholder_still_passes_in_tests(value):
    text = f'conn = {{"{_PW}": "{value}"}}\n'
    assert _rule("tests/unit/test_db.py", text) is None


# -- #361 box 73: third-party code is never the agent's test code -------------


@pytest.mark.parametrize(
    "path",
    [
        "node_modules/left-pad/test/index.js",
        "web/node_modules/pkg/__tests__/a.test.js",
        "vendor/github.com/x/y/tests/z_test.go",
        "third_party/lib/test/test_a.py",
        "bower_components/pkg/test/a.js",
        "venv/lib/python3.11/site-packages/pkg/tests/test_a.py",
    ],
)
def test_vendored_third_party_test_code_gets_the_strict_tier(path):
    assert lifecycle.is_test_path(path) is False
    assert _rule(path, f"login({_PW}='hunter2')\n") == "key_value_assignment"


def test_the_agents_own_test_code_keeps_the_loose_tier():
    assert lifecycle.is_test_path("tests/unit/test_login.py") is True
    assert _rule("tests/unit/test_login.py", f"login({_PW}='hunter2')\n") is None


# -- #361 box 74: a vendor token's placeholder must be explicit, not glued ----


@pytest.mark.parametrize(
    "make",
    [
        lambda: _shape("AK", "IA", _random_token(12, _UPPER_DIGITS), "TEST"),
        lambda: _shape("AK", "IA", _random_token(9, _UPPER_DIGITS), "EXAMPLE"),
        lambda: _shape("gh", "p_", _random_token(29), "EXAMPLE"),
        lambda: _shape("gh", "p_", "test", _random_token(32)),
        lambda: _shape("sk", "-", _random_token(32), "-test"),
        lambda: _shape("gh", "p_", _random_token(32), "xxxx"),
    ],
    ids=["aws-glued-test", "aws-glued-example", "github-glued-example",
         "github-test-prefix", "sk-test-suffix", "github-x-run-suffix"],
)
def test_a_real_vendor_token_with_a_placeholder_word_is_refused_in_tests(make):
    token = make()
    assert _rule("tests/unit/test_client.py", f"key = '{token}'\n") is not None


@pytest.mark.parametrize(
    "make",
    [
        lambda: _shape("AK", "IA", "IOSFODNN7", "EXAMPLE"),
        lambda: _shape("sk", "-", "test-aaaa"),
        lambda: _shape("gh", "p_", "x" * 36),
        lambda: _shape("AK", "IA", "A" * 16),
        lambda: _shape("sk", "-ant-", "test-0000"),
    ],
    ids=["aws-documentation-example", "sk-test", "github-xxxx", "repeated", "sk-ant-test"],
)
def test_an_explicit_vendor_placeholder_still_passes_in_tests(make):
    token = make()
    assert _rule("tests/unit/test_client.py", f"key = '{token}'\n") is None
    assert _rule("src/client.py", f"key = '{token}'\n") is not None


# -- #361 box 79: a key body between an existing, unchanged marker pair --------


def _key_body_lines(raw_len: int = 900) -> list[str]:
    body = _b64(_RNG.randbytes(raw_len))
    return [body[i : i + 64] for i in range(0, len(body), 64)]


def test_a_key_body_between_an_unchanged_marker_pair_is_refused(tmp_path):
    repo = _repo(tmp_path)
    fixture = repo / "src" / "keys.txt"
    fixture.parent.mkdir()
    fixture.write_text(f"intro\n{_marker('BEGIN')}\nstub\n{_marker('END')}\noutro\n")
    base = _commit_all(repo, "a stub key")
    fixture.write_text(
        "\n".join(["intro", _marker("BEGIN"), *_key_body_lines(), _marker("END"), "outro"]) + "\n"
    )
    sha = _commit_all(repo, "a real key")

    reason = _final(repo, tmp_path, base)
    assert reason == (
        "the final tree adds a credential in src/keys.txt (rule private_key_block, line 3); remove it"
    ), reason
    assert _first(repo, tmp_path, [sha], base) == 1


def test_a_key_body_above_an_unchanged_orphan_end_is_refused(tmp_path):
    repo = _repo(tmp_path)
    fixture = repo / "tests" / "test_keys.py"
    fixture.parent.mkdir()
    fixture.write_text(f'TAIL = """\n{_marker("END")}\n"""\n')
    base = _commit_all(repo, "an orphan end")
    lines = ['TAIL = """', *_key_body_lines(), _marker("END"), '"""']
    fixture.write_text("\n".join(lines) + "\n")
    _commit_all(repo, "a body above it")

    reason = _final(repo, tmp_path, base)
    assert reason is not None and "rule private_key_block" in reason, reason


def test_an_edit_outside_an_unchanged_marker_pair_publishes(tmp_path):
    """The control: the file holds a stub block (as main's own redaction tests
    do), and an edit well away from it is not refused for it."""
    repo = _repo(tmp_path)
    fixture = repo / "src" / "keys.txt"
    fixture.parent.mkdir()
    fixture.write_text(f"intro\n{_marker('BEGIN')}\nstub\n{_marker('END')}\n\noutro\n")
    base = _commit_all(repo, "a stub key")
    fixture.write_text(f"intro\n{_marker('BEGIN')}\nstub\n{_marker('END')}\n\nchanged outro\n")
    sha = _commit_all(repo, "an unrelated edit")

    assert _final(repo, tmp_path, base) is None
    assert _first(repo, tmp_path, [sha], base) is None


def test_the_span_reader_masks_exactly_what_main_masks():
    """PARITY: the spans the context check reads are `mask_private_keys`'s own."""
    from swarm_redaction import MASK, mask_private_keys

    samples = [
        "\n".join(["a", _marker("BEGIN"), *_key_body_lines(), _marker("END"), "b"]) + "\n",
        "\n".join([*_key_body_lines(), _marker("END"), "tail"]) + "\n",
        "\n".join([_marker("BEGIN"), *_key_body_lines(300), "", "prose after"]) + "\n",
        "nothing here\n",
        f"{_marker('END')}\n{_marker('BEGIN')}\nstub\n",
    ]
    for text in samples:
        spans = lifecycle._private_key_spans(text)
        rebuilt, pos = [], 0
        for start, end in spans:
            rebuilt.append(text[pos:start])
            rebuilt.append(MASK)
            pos = end
        rebuilt.append(text[pos:])
        # mask_private_keys keeps a BEGIN marker and masks what follows it.
        masked, count = mask_private_keys(text, False, False)
        assert "".join(rebuilt) == masked
        assert len(spans) == count


# -- #361 box 80: headerless encrypted key ciphertext in a variable -----------


@pytest.mark.parametrize("path", ["src/app.py", "tests/test_app.py"])
@pytest.mark.parametrize("raw_len", [128, 1200, 1776])
def test_headerless_encrypted_key_ciphertext_in_a_variable_is_refused(path, raw_len):
    text = f'CIPHER = "{_b64(_RNG.randbytes(raw_len))}"\n'
    assert _rule(path, text) == "headerless_private_key"


@pytest.mark.parametrize("path", ["src/app.py", "tests/test_app.py"])
def test_wrapped_ciphertext_lines_in_a_triple_quoted_string_are_refused(path):
    text = 'CIPHER = """\n' + "\n".join(_key_body_lines(1192)) + '\n"""\n'
    assert _rule(path, text) == "headerless_private_key"


def test_ciphertext_split_into_concatenated_string_pieces_is_refused():
    body = _b64(_RNG.randbytes(1200))
    pieces = [body[i : i + 64] for i in range(0, len(body), 64)]
    text = "CIPHER = (\n" + "".join(f'    "{p}"\n' for p in pieces) + ")\n"
    assert _rule("src/app.py", text) == "headerless_private_key"


def _png_blob() -> str:
    return _b64(b"\x89PNG\r\n\x1a\n" + _RNG.randbytes(1192))


@pytest.mark.parametrize(
    "text",
    [
        lambda: f'ICON = "data:image/png;base64,{_png_blob()}"\n',
        lambda: f'DIGEST = "{_RNG.randbytes(600).hex()}"\n',
        lambda: f'"integrity": "sha512-{_b64(_RNG.randbytes(64))}",\n',
        lambda: f'SMALL = "{_b64(_RNG.randbytes(96))}"\n',
        lambda: "x = '" + "abcdefghijklmnopqrstuvwxyz" * 10 + "'\n",
        lambda: "path = 'src/components/" + "/".join(["SomeComponentName"] * 12) + "'\n",
        lambda: f'GZ = "{_b64(bytes.fromhex("1f8b0800") + _RNG.randbytes(1196))}"\n',
    ],
    ids=["png-data-uri", "hex-digest", "lockfile-integrity", "short-blob", "letters",
         "path", "gzip"],
)
def test_ordinary_base64_and_text_is_not_ciphertext(text):
    assert _rule("src/app.py", text()) is None
