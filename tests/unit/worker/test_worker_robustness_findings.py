"""Eight worker robustness findings from three wave epics (#227, #346, #361).

Each test fails on the worker as it was before the change that names it:

* #361: the whole-diff credential helper no production path called is gone,
  and a JWT header nested past the JSON parser's depth is a finding, not a
  RecursionError out of the publish guard;
* #346: `packed-refs` is streamed and stops at its match, a loose ref that
  exists but is not a regular file is refused rather than read as absent, and
  the worker's re-check names a bounded number of the caller's keys, each cut;
* #227: no lone surrogate from the agent CLI's JSON reaches the result
  summary, and the replacements are counted. The two #227 logging findings are
  in `test_artifacts_manifest_cap.py`, beside the tests of the lines they
  change.

Every fake credential is built at runtime: a literal would trip the publish
scan this file tests.
"""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from agent_worker import gitops, lifecycle
from agent_worker.errors import ConfigError, ExitCode
from agent_worker.redact import replace_lone_surrogates
from agent_worker.runners import cliagent

from test_standalone_outputs import _run, _seed, _summary

WORKER_SOURCE = Path(lifecycle.__file__).resolve().parent


# -- #361: the dead whole-diff helper ----------------------------------------


def test_the_whole_diff_credential_helper_no_path_called_is_gone():
    """`_adds_a_credential` and its parser `_added_by_file` had no caller in
    the worker: both publish scans run `_DiffLeakScanner`. A second parser of
    the same diff, held by tests alone, is one a fix can miss. No worker
    module may define or name either."""
    assert not hasattr(lifecycle, "_adds_a_credential")
    assert not hasattr(lifecycle, "_added_by_file")
    named = [
        f"{path.relative_to(WORKER_SOURCE)}:{number}"
        for path in sorted(WORKER_SOURCE.rglob("*.py"))
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if re.search(r"\b_adds_a_credential\b|\b_added_by_file\b", line)
    ]
    assert named == [], named


# -- #361: a JWT header nested past the parser's depth -----------------------


def _nested_jwt(depth: int) -> str:
    """A three-segment token whose header is JSON nested `depth` deep."""
    header = b'{"alg":' + b"[" * depth + b"]" * depth + b"}"

    def segment(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return ".".join((segment(header), segment(b'{"sub":"1"}'), "s" + "ig" * 12))


def test_a_jwt_header_nested_past_the_parsers_depth_fails_closed():
    token = _nested_jwt(100_000)
    assert lifecycle._decodes_as_a_jwt(token) is True
    hit = lifecycle._credential_in("src/app.py", f"value = {token}\n")
    assert hit is not None and hit.rule == "jwt", hit


def test_a_shallow_header_is_still_judged_by_its_alg():
    """The control: a header the parser reads is judged as before."""
    assert lifecycle._decodes_as_a_jwt(_nested_jwt(3)) is True
    assert lifecycle._decodes_as_a_jwt("eyword_only_args") is False


# -- #346: the ref readers ---------------------------------------------------


def _oid(char: str) -> str:
    return char * 40


def _clone(tmp_path: Path, *, packed: bytes | None = None) -> Path:
    clone = tmp_path / "clone"
    (clone / ".git" / "refs" / "heads").mkdir(parents=True)
    (clone / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    if packed is not None:
        (clone / ".git" / "packed-refs").write_bytes(packed)
    return clone


def test_packed_refs_is_read_only_as_far_as_its_match(tmp_path, monkeypatch):
    """The match is the first line of 4 MiB: reading stops there, instead of
    reading, decoding and splitting the whole file first."""
    filler = b"".join(
        f"{index:040x} refs/heads/other-{index}\n".encode() for index in range(80_000)
    )
    clone = _clone(tmp_path, packed=f"{_oid('a')} refs/heads/main\n".encode() + filler)
    read = []
    real_read = os.read

    def counting_read(fd: int, size: int) -> bytes:
        chunk = real_read(fd, size)
        read.append(len(chunk))
        return chunk

    monkeypatch.setattr(gitops.os, "read", counting_read)
    assert gitops.read_head_as_data(clone) == _oid("a")
    assert sum(read) <= 1024 * 1024, sum(read)


def test_packed_refs_past_an_over_long_line_still_finds_the_branch(tmp_path):
    """A line longer than any ref is skipped as it streams, not held."""
    junk = b"x" * (3 * 1024 * 1024) + b"\n"
    clone = _clone(tmp_path, packed=junk + f"{_oid('b')} refs/heads/main\r\n".encode())
    assert gitops.read_head_as_data(clone) == _oid("b")


def test_an_absent_loose_ref_is_still_taken_from_packed_refs(tmp_path):
    """The control for the refusals below: absence is not refused."""
    clone = _clone(tmp_path, packed=f"# pack-refs\n{_oid('c')} refs/heads/main\n".encode())
    assert gitops.read_head_as_data(clone) == _oid("c")


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory", "linked-folder"])
def test_a_loose_ref_that_is_not_a_regular_file_is_refused(tmp_path, kind):
    """It used to read as absent, and the branch was taken from packed-refs:
    an older commit than the one the agent's own ref, hidden behind a link,
    named. Refused now; nothing is pushed."""
    clone = _clone(tmp_path, packed=f"{_oid('d')} refs/heads/main\n".encode())
    heads = clone / ".git" / "refs" / "heads"
    target = tmp_path / "elsewhere"
    target.write_text(_oid("e") + "\n")
    if kind == "symlink":
        (heads / "main").symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(heads / "main")
    elif kind == "directory":
        (heads / "main").mkdir()
    else:
        (clone / ".git" / "HEAD").write_text("ref: refs/heads/team/main\n")
        (tmp_path / "team").mkdir()
        (tmp_path / "team" / "main").write_text(_oid("e") + "\n")
        (heads / "team").symlink_to(tmp_path / "team")
    with pytest.raises(gitops.GitError, match="not a regular file"):
        gitops.read_head_as_data(clone)


# -- #346: the re-check's message is bounded ---------------------------------


def test_the_recheck_names_ten_refused_keys_each_cut_short():
    keys = {f"k{index:03d}" + "z" * 1000: 1 for index in range(40)}
    with pytest.raises(ConfigError) as caught:
        lifecycle._recheck_runner_input("claude-code", keys)
    message = str(caught.value)
    assert len(message) < 1500, len(message)
    assert "and 30 more" in message, message
    assert "k009" in message and "k010" not in message, message
    assert "z" * 65 not in message


def test_a_few_short_refused_keys_are_named_whole():
    """The control: under the bounds, the message is as it was."""
    with pytest.raises(ConfigError) as caught:
        lifecycle._recheck_runner_input("claude-code", {"alpha": 1, "beta": 2})
    message = str(caught.value)
    assert "alpha" in message and "beta" in message and "more" not in message, message


# -- #227: no lone surrogate reaches the summary -----------------------------


def _has_surrogate(value: Any) -> bool:
    return bool(re.search("[\ud800-\udfff]", json.dumps(value, ensure_ascii=False)))


def test_lone_surrogates_are_replaced_and_counted():
    value = {"k\udc80": ["a\ud800b", ("\udfff",)], "ok": "\U0001f600"}
    cleaned, count = replace_lone_surrogates(value)
    assert count == 3
    assert cleaned == {"k�": ["a�b", ("�",)], "ok": "\U0001f600"}


def test_the_cli_runners_scrub_leaves_no_lone_surrogate():
    scrubbed = cliagent._scrub_json({"r\ud800": ["x\udcff"]}, ())
    assert not _has_surrogate(scrubbed), scrubbed


#: A stand-in for `claude --print` whose answer carries a lone-surrogate escape.
SURROGATE_AGENT = r"""#!/usr/bin/env python3
import sys
sys.stdin.read()
print('{"type": "result", "subtype": "success", "is_error": false, '
      '"result": "done \\ud800 here", "note\\udc80": "x"}')
"""


@pytest.fixture
def surrogate_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    binary = tmp_path / "fake-claude-surrogate"
    binary.write_text(SURROGATE_AGENT)
    binary.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CODE_BIN", str(binary))
    monkeypatch.delenv("CLAUDE_CODE_ARGS", raising=False)
    return binary


def test_a_lone_surrogate_in_the_agents_json_never_reaches_the_summary(
    db, worker_factory, surrogate_cli
):
    _seed(db, {})
    assert _run(worker_factory) == ExitCode.OK
    runner = _summary(db)["runner"]
    assert not _has_surrogate(runner), runner
    assert "�" in json.dumps(runner, ensure_ascii=False), runner
    json.dumps(runner, ensure_ascii=False).encode("utf-8")
    # The CLI runner's `summary` is cut from the raw answer, not the scrubbed
    # JSON: its one surrogate reaches the worker, which replaces and counts it.
    assert runner.get("surrogates_replaced") == 1, runner
