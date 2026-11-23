"""The sc skill's whole text: its core SKILL.md and every on-demand reference
file under plugin/skills/sc/reference/.

The skill was split (improvement plan 2026-11-23): the core keeps the verb
table, `check`, `watch` and arming the watch, and each other verb's rules
moved word for word into a reference file that only that verb loads. A test
that pins a rule pins it here, wherever it now lives, so a rule dropped in
the move goes red; a test that must see a rule in the CORE reads `CORE`.
"""

from __future__ import annotations

from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[3] / "plugin"
SKILL_DIR = PLUGIN / "skills" / "sc"
CORE = SKILL_DIR / "SKILL.md"
REFERENCE_DIR = SKILL_DIR / "reference"


def reference_files() -> list[Path]:
    return sorted(REFERENCE_DIR.glob("*.md"))


def text() -> str:
    """The core, then every reference file, in name order."""
    return "\n".join(path.read_text() for path in [CORE, *reference_files()])
