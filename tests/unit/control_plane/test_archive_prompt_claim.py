"""Every description of the checkpoint archive says it carries the prompt.

Issue #244, owner decision 2026-09-27: the CLI session transcripts stay in
every checkpoint archive, and the whole-archive download stays byte for byte
and unredacted. Leaving `input.json` out of the archive therefore does NOT keep
the prompt out of it: `HOME` is `work/`, so Claude Code's transcript
(`work/.claude/projects/...`) and Codex's (`work/.codex/sessions/...`) hold the
prompt as their first user message. The fix is documentation, and this pins
it: each place that describes what the archive holds must name both
transcripts and the issue, so none of them can read again as "the archive is
free of the prompt".

Source text only -- no imports of the modules, no clients, no credentials.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]

TASK_INPUT = ROOT / "apps/swarm-api/swarm_api/task_input.py"
CHECKPOINT_CONTENT = ROOT / "apps/swarm-api/swarm_api/checkpoint_content.py"
WORKER_CHECKPOINT = ROOT / "apps/agent-worker/agent_worker/checkpoint.py"
AGENT_OUTPUT_DOC = ROOT / "docs/agent-output.md"

#: What each description must name: both CLIs' transcript locations and the
#: issue whose decision keeps them in the archive.
REQUIRED = (".claude/projects", ".codex/sessions", "#244")


def _module_docstring(path: Path) -> str:
    doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))
    assert doc, f"{path.relative_to(ROOT)} has no module docstring"
    return doc


def _create_skip_comment() -> str:
    """The comment in `CheckpointManager.create()` that explains the skip set:
    from the `AND SO IS input.json` line to the `skip = frozenset(` line."""
    text = WORKER_CHECKPOINT.read_text(encoding="utf-8")
    start = text.index("# AND SO IS `input.json`")
    end = text.index("skip = frozenset(", start)
    return text[start:end]


DESCRIPTIONS = {
    "task_input.py module docstring": lambda: _module_docstring(TASK_INPUT),
    "docs/agent-output.md": lambda: AGENT_OUTPUT_DOC.read_text(encoding="utf-8"),
    "checkpoint_content.py module docstring": lambda: _module_docstring(
        CHECKPOINT_CONTENT
    ),
    "checkpoint.py create() skip comment": _create_skip_comment,
}


@pytest.mark.parametrize("where", sorted(DESCRIPTIONS))
def test_every_description_of_the_archive_names_the_cli_transcript(where: str) -> None:
    text = DESCRIPTIONS[where]()
    missing = [needle for needle in REQUIRED if needle not in text]
    assert not missing, (
        f"{where} describes the checkpoint archive without {missing}: it must "
        "say the archive carries the prompt in the CLI's session transcript "
        "(issue #244, owner decision 2026-09-27)"
    )


def test_the_archive_download_is_still_documented_unredacted() -> None:
    # The owner's decision of 2026-09-24: the whole archive is served as
    # stored, and the header says so. Editing the prose must not quietly turn
    # the documented download into a masked one.
    assert "X-Swarm-Redaction: not-applied" in _module_docstring(CHECKPOINT_CONTENT)
