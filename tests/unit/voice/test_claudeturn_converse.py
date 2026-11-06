"""The fast front's one session-long Claude Code child (claudeturn.ClaudeCode.converse;
docs/design/voice-it-own-speech.md §14.1).

Against a fake `claude` executable that reads stream-json user messages on
stdin and answers each with stream-json events, this holds that:

* the child is started once and answers every sentence on the same process,
  with `--input-format stream-json`, the front's pin, `--tools ""`, only the
  planner's MCP server and `--strict-mcp-config`;
* its environment is BUILT: the leased token in CLAUDE_CODE_OAUTH_TOKEN, a
  private HOME and nothing of this process's own settings;
* the captured events are scrubbed of the token, which no repr carries;
* closing it stops the child and removes the private HOME;
* a child that exits mid-session is `ChildGone`, not a hang.

The token is built at runtime; no cloud credential or emulator.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import stat
import sys
from pathlib import Path

import pytest

_APP = str(Path(__file__).resolve().parents[3] / "apps" / "swarm-voice")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from swarm_voice import claudeturn  # noqa: E402

FAKE = r'''#!{python}
import json, os, sys
dump = {dump!r}
with open(dump, "w") as fh:
    json.dump({{"argv": sys.argv[1:], "env": dict(os.environ), "home": os.environ.get("HOME"),
               "cwd": os.getcwd(), "pid": os.getpid()}}, fh)
token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
for n, line in enumerate(sys.stdin, 1):
    msg = json.loads(line)
    text = msg["message"]["content"][0]["text"]
    if text == "die":
        sys.exit(3)
    print(json.dumps({{"type": "assistant", "message": {{"id": "m%d" % n, "usage": {{"input_tokens": 10, "output_tokens": 2}},
                     "content": [{{"type": "text", "text": "leak " + token}}]}}}}), flush=True)
    print(json.dumps({{"type": "result", "is_error": False, "result": "Heard %d: %s" % (n, text),
                     "usage": {{"input_tokens": 10, "output_tokens": 2}}}}), flush=True)
'''


@pytest.fixture()
def child(tmp_path):
    dump = tmp_path / "child.json"
    script = tmp_path / "claude"
    script.write_text(FAKE.format(python=sys.executable, dump=str(dump)))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script), dump


async def _no_tools(name, args):  # noqa: ANN001
    return "none", True


def test_one_child_answers_every_sentence_and_its_home_is_removed(child, monkeypatch):
    binary, dump = child
    token = "t" + secrets.token_hex(20)
    monkeypatch.setenv("SWARM_VOICE_SETTING", "x" + secrets.token_hex(8))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k" + secrets.token_hex(12))

    async def main():
        claude = claudeturn.ClaudeCode(binary=binary, model=claudeturn.front_model(), max_turns=3)
        conversation = await claude.converse(system="be brief", token=token, tools=[], handler=_no_tools,
                                             known=(token,))
        try:
            first = await conversation.say("hello")
            second = await conversation.say("again")
            home = conversation.home
            assert os.path.isdir(home)
            assert token not in repr(conversation)
        finally:
            await conversation.close()
        return first, second, home

    first, second, home = asyncio.run(main())
    assert first.result_text == "Heard 1: hello" and second.result_text == "Heard 2: again"
    assert first.usage == {"input_tokens": 10, "output_tokens": 2}
    assert token not in json.dumps(first.events) and token not in json.dumps(second.events)
    assert not os.path.exists(home)

    seen = json.loads(dump.read_text())
    argv, env = seen["argv"], seen["env"]
    assert argv[:3] == ["-p", "--model", "claude-haiku-5-5"]
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv and argv[argv.index("--allowedTools") + 1] == "mcp__planner"
    assert argv[argv.index("--system-prompt") + 1] == "be brief"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == token
    assert env["HOME"] == home == seen["cwd"] and env["TMPDIR"] == home
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert "ANTHROPIC_API_KEY" not in env and "SWARM_VOICE_SETTING" not in env
    assert Path(argv[argv.index("--mcp-config") + 1]).parent == Path(home)


def test_a_child_that_exits_is_gone_not_a_hang(child):
    binary, _dump = child
    token = "t" + secrets.token_hex(20)

    async def main():
        claude = claudeturn.ClaudeCode(binary=binary)
        conversation = await claude.converse(system="s", token=token, tools=[], handler=_no_tools, known=(token,))
        try:
            with pytest.raises(claudeturn.ChildGone):
                await asyncio.wait_for(conversation.say("die"), 10)
            await asyncio.sleep(0)
            with pytest.raises(claudeturn.ChildGone):
                await asyncio.wait_for(conversation.say("anyone?"), 10)
        finally:
            await conversation.close()
        return conversation.home

    home = asyncio.run(main())
    assert not os.path.exists(home)


def test_the_two_pins_are_read_from_the_environment(monkeypatch):
    monkeypatch.delenv("VOICE_FRONT_MODEL", raising=False)
    monkeypatch.delenv("VOICE_PLANNER_MODEL", raising=False)
    assert claudeturn.front_model() == "claude-haiku-5-5"
    assert claudeturn.planner_model() == "claude-fable-5-1"
    assert claudeturn.ClaudeCode().model == "claude-fable-5-1"
    monkeypatch.setenv("VOICE_FRONT_MODEL", "claude-haiku-6")
    monkeypatch.setenv("VOICE_PLANNER_MODEL", "claude-fable-6")
    assert claudeturn.front_model() == "claude-haiku-6"
    assert claudeturn.ClaudeCode().model == "claude-fable-6"


def test_a_child_that_cannot_start_leaves_no_home(tmp_path):
    async def main():
        claude = claudeturn.ClaudeCode(binary=str(tmp_path / "missing"), tmp_root=str(tmp_path))
        with pytest.raises(OSError):
            await claude.converse(system="s", token="t" + secrets.token_hex(8), tools=[], handler=_no_tools,
                                  known=())

    asyncio.run(main())
    assert [p.name for p in tmp_path.iterdir()] == []
