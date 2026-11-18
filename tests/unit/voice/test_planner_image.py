"""swarm-voice's image carries the Claude Code CLI the planner runs each turn on
(docs/design/voice-it.md §3.6), at the version images/agent-runtime-base pins,
read from that Dockerfile so the pin has one source.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
VOICE = REPO / "apps" / "swarm-voice" / "Dockerfile"
BASE = REPO / "images" / "agent-runtime-base" / "Dockerfile"


def _arg(text: str, name: str) -> str | None:
    match = re.search(rf"^ARG {name}=(\S+)$", text, re.M)
    return match.group(1) if match else None


def test_the_cli_version_is_read_from_agent_runtime_base_never_copied():
    voice, base = VOICE.read_text(), BASE.read_text()
    version = _arg(base, "CLAUDE_CODE_VERSION")
    assert version and re.fullmatch(r"[0-9][0-9.]*", version)
    assert "COPY images/agent-runtime-base/Dockerfile" in voice
    assert "s/^ARG CLAUDE_CODE_VERSION=" in voice
    # No second copy of the pin, in any form.
    assert version not in voice and _arg(voice, "CLAUDE_CODE_VERSION") is None
    assert '"@anthropic-ai/claude-code@${version}"' in voice


def test_node_is_the_image_agent_runtime_base_copies_and_the_cli_is_on_path():
    voice, base = VOICE.read_text(), BASE.read_text()
    assert _arg(voice, "NODE_IMAGE") == _arg(base, "NODE_IMAGE")
    assert re.search(r"@sha256:[0-9a-f]{64}$", _arg(voice, "NODE_IMAGE") or "")
    assert "COPY --from=claudecli /opt/claude /opt/claude" in voice
    assert re.search(r'PATH="[^"]*/opt/claude/bin', voice)
    assert "&& claude --version" in voice
