"""`answer_excerpt` is JSON-safe as served: copying it needs no escaping (#285).

An `sc:step` row is a Haiku relay that retypes the bridge's outcome into its
`StructuredOutput` JSON. With the raw answer's first 500 characters in
`answer_excerpt`, it failed validation five times on one SUCCEEDED step --
backticks, double quotes, `********` masks, paths and backslashes -- and the
row reported `state: null` for a task that had finished. A field whose text
can be put between two double quotes as it stands gives the relay nothing to
escape, so it can be copied verbatim. `answer`, the whole answer, stays raw.
"""

from __future__ import annotations

import json

import pytest

from swarm_mcp import progress

_NASTY = {
    "backticks": "ran `make test` and ```python\nprint(1)\n``` passed",
    "double-quotes": 'the key "token" was "set" to ""',
    "masks": "GITHUB_TOKEN=******** and password ******** redacted",
    "posix-path": "wrote /workspace/repo/apps/swarm-mcp/swarm_mcp/progress.py",
    "windows-path": "C:\\Users\\dev\\repo\\file.txt and \\\\server\\share",
    "backslashes": "regex \\d+\\s*\\\\ and a trailing \\",
    "newlines-tabs": "line one\nline two\r\n\tindented\x0bvt\x0cff",
    "other-controls": "nul\x00bell\x07esc\x1b[31mred\x1b[0m del\x7f c1\x85 ls\u2028ps\u2029",
    "non-ascii": "café — naïve ✓ 日本語 emoji 🚀 “curly” ‘quotes’",
    "mixed": 'echo "`cat C:\\x\\y`"\n\t******** /a/b \\"q\\"',
}


def _assert_json_safe(text: str) -> None:
    assert json.loads(json.dumps(text)) == text
    assert json.dumps(text, ensure_ascii=False) == '"' + text + '"', text
    if text.isascii():
        assert json.dumps(text) == '"' + text + '"', text
    for bad in ('"', "\\", "`", "\n", "\t", "\r"):
        assert bad not in text, (bad, text)
    assert all(ord(ch) >= 0x20 and not 0x7F <= ord(ch) <= 0x9F for ch in text), text


@pytest.mark.parametrize("raw", list(_NASTY.values()), ids=list(_NASTY))
def test_the_excerpt_needs_no_escaping_as_a_json_string(raw):
    _assert_json_safe(progress.json_safe_excerpt(raw))


@pytest.mark.parametrize("raw", list(_NASTY.values()), ids=list(_NASTY))
def test_a_long_excerpt_is_cut_to_the_limit_and_stays_safe(raw):
    long = raw * (progress.EXCERPT_CHARS // max(1, len(raw)) + 3)
    out = progress.json_safe_excerpt(long)
    assert len(out) <= progress.EXCERPT_CHARS
    assert out.endswith("…")
    _assert_json_safe(out)


def test_the_cut_is_measured_on_the_replaced_text():
    """Fifty characters of whitespace collapse to one: an excerpt measured on
    the raw text would cut 49 characters short of what fits."""
    raw = "a" * 300 + "\n" * 50 + "b" * 300
    out = progress.json_safe_excerpt(raw)
    assert len(out) == progress.EXCERPT_CHARS
    assert out == ("a" * 300 + " " + "b" * 300)[: progress.EXCERPT_CHARS - 1] + "…"


def test_a_short_excerpt_is_not_given_an_ellipsis():
    assert progress.json_safe_excerpt("done") == "done"
    exact = "x" * progress.EXCERPT_CHARS
    assert progress.json_safe_excerpt(exact) == exact


def test_masks_and_path_text_survive():
    assert "********" in progress.json_safe_excerpt(_NASTY["masks"])
    assert "/workspace/repo/apps/swarm-mcp/swarm_mcp/progress.py" in progress.json_safe_excerpt(_NASTY["posix-path"])
    # A backslash becomes a forward slash: the path is still legible as one.
    assert "C:/Users/dev/repo/file.txt" in progress.json_safe_excerpt(_NASTY["windows-path"])


def test_quotes_and_backticks_become_single_quotes():
    assert progress.json_safe_excerpt('say "hi" with `x`') == "say 'hi' with 'x'"


def test_non_ascii_is_kept():
    out = progress.json_safe_excerpt(_NASTY["non-ascii"])
    assert "café" in out and "日本語" in out and "🚀" in out


class _AnswerClient:
    def __init__(self, content: str) -> None:
        self.content = content

    def answer(self, task_id: str) -> dict:
        assert task_id == "task_1"
        return {"status": "ok", "content": self.content, "source": "result"}


@pytest.mark.parametrize("raw", list(_NASTY.values()), ids=list(_NASTY))
def test_final_answer_serves_a_safe_excerpt_and_the_raw_answer(raw):
    out = progress.final_answer(_AnswerClient(raw), {"id": "task_1"})
    assert out["answer"] == raw, "the whole answer stays raw"
    assert out["answer_excerpt"] == progress.json_safe_excerpt(raw)
    _assert_json_safe(out["answer_excerpt"])


def test_final_answer_cuts_a_long_answers_excerpt():
    raw = _NASTY["mixed"] * 200
    out = progress.final_answer(_AnswerClient(raw), {"id": "task_1"})
    assert out["answer"] == raw
    assert len(out["answer_excerpt"]) <= progress.EXCERPT_CHARS
    _assert_json_safe(out["answer_excerpt"])
