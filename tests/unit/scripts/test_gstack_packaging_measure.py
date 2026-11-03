"""The gstack packaging spike's measurement (scripts/gstack_packaging_measure.py).

docs/design/agent-plugins-packaging.md proposes what goes into the agent
images from these numbers, so each rule below is the wrong number it would
otherwise produce: a closure that drags in an installer and every binary, a
skipped block counted as a pass, a missing helper read as "nothing to run", a
writable install labelled read-only. Every test builds a small install in a
temporary folder: no network, no Bun, no credentials.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "gstack_packaging_measure.py"


def _load():
    spec = importlib.util.spec_from_file_location("gstack_packaging_measure", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


m = _load()

ELF = b"\x7fELF" + b"\x00" * 60


def _write(path: Path, text: str | bytes, mode: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)
    return path


def _install(tmp_path: Path) -> Path:
    """A miniature gstack: one offered skill, one that is not, helpers, a binary."""
    root = tmp_path / "gstack"
    _write(root / "VERSION", "1.0.0\n")
    _write(root / "README.md", "Run bin/gstack-doctor or ~/.claude/skills/gstack/design/dist/design.\n")
    _write(root / "setup", "#!/usr/bin/env bash\n\"$SCRIPT_DIR/bin/gstack-relink\"\n")
    _write(
        root / "review" / "SKILL.md",
        "---\nname: review\n---\n"
        "```bash\n~/.claude/skills/gstack/bin/gstack-start --skill review\n```\n"
        "Read ~/.claude/skills/gstack/README.md and ~/.claude/skills/gstack/browse/SKILL.md.\n"
        "Then `B=$HOME/.claude/skills/gstack/browse/dist/browse`.\n"
        "Never: ~/.claude/skills/gstack/setup\n"
        "Source: ~/.claude/skills/gstack/lib/page.md.tmpl\n",
    )
    _write(root / "lib" / "page.md.tmpl", "~/.claude/skills/gstack/bin/gstack-template-only\n")
    _write(root / "review" / "SKILL.md.tmpl", "~/.claude/skills/gstack/bin/gstack-template-only\n")
    _write(root / "review" / "sections" / "extra.md", "Run `gstack-extra`.\n")
    _write(root / "browse" / "SKILL.md", "~/.claude/skills/gstack/bin/gstack-browse-only\n")
    _write(root / "browse" / "dist" / "browse", ELF + b"x" * 100, 0o755)
    _write(root / "design" / "dist" / "design", ELF + b"y" * 50, 0o755)
    _write(
        root / "bin" / "gstack-start",
        "#!/usr/bin/env bash\n"
        "# gstack-doctor launches every binary; a comment, not a call\n"
        'SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"\n'
        '"$SCRIPT_DIR/gstack-config" get x\n'
        'bun run "$SCRIPT_DIR/../lib/main.ts"\n',
        0o755,
    )
    _write(root / "bin" / "gstack-config", "#!/usr/bin/env bash\necho config\n", 0o755)
    _write(root / "bin" / "gstack-extra", "#!/usr/bin/env bash\necho extra\n", 0o755)
    _write(root / "bin" / "gstack-doctor", "#!/usr/bin/env bash\n$D/design/dist/design\n", 0o755)
    _write(root / "bin" / "gstack-template-only", "#!/usr/bin/env bash\n", 0o755)
    _write(root / "bin" / "gstack-browse-only", "#!/usr/bin/env bash\n", 0o755)
    _write(root / "bin" / "gstack-relink", "#!/usr/bin/env bash\n", 0o755)
    _write(
        root / "lib" / "main.ts",
        "import { a } from './util';\nimport fs from 'fs';\nimport { join } from 'node:path';\n"
        "import pad from 'left-pad';\n// import never from 'commented-out';\n",
    )
    _write(root / "lib" / "util.ts", "export const a = 1;\n")
    _write(root / "lib" / "unused.ts", "export const b = 2;\n")
    _write(
        root / "node_modules" / "left-pad" / "package.json",
        '{"name": "left-pad", "dependencies": {"pad-core": "1"}, "optionalDependencies": {"absent-opt": "1"}}',
    )
    _write(root / "node_modules" / "left-pad" / "index.js", "module.exports = 1;\n")
    _write(root / "node_modules" / "pad-core" / "package.json", '{"name": "pad-core"}')
    _write(root / "node_modules" / "pad-core" / "addon.node", ELF)
    _write(root / ".git" / "objects" / "big", b"z" * 5000)
    return root


# ── size ──────────────────────────────────────────────────────────────────


def test_size_counts_without_git_and_separates_binaries_from_native_addons(tmp_path):
    root = _install(tmp_path)
    size = m.measure_size(root)
    on_disk = sum(
        p.stat().st_size for p in root.rglob("*") if p.is_file() and ".git" not in p.relative_to(root).parts
    )
    assert size["total_bytes"] == on_disk
    assert ".git" not in size["per_top_level"]
    assert [b["path"] for b in size["binaries"]] == ["browse/dist/browse", "design/dist/design"]
    assert size["node_modules_native_addons"] == {"count": 1, "bytes": len(ELF)}


# ── closure ───────────────────────────────────────────────────────────────


def test_closure_follows_literal_paths_helpers_and_imports(tmp_path):
    root = _install(tmp_path)
    closure = m.skill_closure(root, ["review"])
    files = closure["union"]["file_list"]
    for needed in (
        "review/SKILL.md",
        "review/sections/extra.md",
        "bin/gstack-start",
        "bin/gstack-config",
        "bin/gstack-extra",
        "lib/main.ts",
        "lib/util.ts",
        "browse/dist/browse",
    ):
        assert needed in files, needed
    assert closure["union"]["packages"]["packages"].keys() == {"left-pad", "pad-core"}
    assert closure["union"]["packages"]["not_installed"] == ["absent-opt"]
    assert closure["skills"]["review"]["binaries"] == ["browse/dist/browse"]
    assert closure["skills"]["review"]["binary_via"]["browse/dist/browse"][-1] == "review/SKILL.md"


def test_closure_does_not_follow_installers_templates_comments_docs_or_other_skills_pages(tmp_path):
    root = _install(tmp_path)
    closure = m.skill_closure(root, ["review"])
    files = closure["union"]["file_list"]
    # setup and what it calls only install; the .tmpl is rendered at build time.
    for cut in ("setup", "bin/gstack-relink", "review/SKILL.md.tmpl", "lib/page.md.tmpl", "bin/gstack-template-only"):
        assert cut not in files, cut
    # A comment names gstack-doctor, and README.md (a doc, kept as a leaf)
    # names the design binary: neither is a run-time need.
    assert "README.md" in files
    assert "bin/gstack-doctor" not in files
    assert "design/dist/design" not in files
    # browse is not offered: its page is cut (with the edge reported), its
    # compiled browser is kept because the offered skill runs it.
    assert "browse/SKILL.md" not in files
    assert ["review/SKILL.md", "browse/SKILL.md"] in closure["skills"]["review"]["cut"]
    assert "bin/gstack-browse-only" not in files
    assert "lib/unused.ts" not in files
    assert "commented-out" not in closure["union"]["packages"]["not_installed"]


def test_node_builtins_and_prose_are_not_packages():
    assert m._package_name("fs") is None
    assert m._package_name("node:fs") is None
    assert m._package_name("Failure modes: ___") is None
    assert m._package_name("@anthropic-ai/sdk/resources") == "@anthropic-ai/sdk"
    assert m._package_name("diff/lib/x") == "diff"


def test_a_missing_skill_is_missing_never_an_empty_closure(tmp_path):
    root = _install(tmp_path)
    closure = m.skill_closure(root, ["review", "nope"])
    assert closure["skills"]["nope"] == {"status": "missing"}
    assert m.summary({"closure": closure}).count("missing skills: nope") == 1


# ── what runs without a model ─────────────────────────────────────────────


def test_bash_blocks_are_extracted_in_order():
    md = "x\n```bash\necho one\n```\n```text\nno\n```\n```sh\necho two\n```\n"
    assert m.bash_blocks(md) == ["echo one", "echo two"]


@pytest.mark.parametrize(
    "block",
    [
        "git diff <base>",
        'gstack-skill-end --session-id "SESSION_ID"',
        "gstack-skill-end --outcome OUTCOME --x 1",
        "[exact command]",
        '$B js "x"',
        'cat "${_DESIGN_DIR:?set it}/f"',
        'gstack-version-bump --version "$NEW_VERSION"',
    ],
)
def test_a_block_a_model_must_fill_in_is_not_run(block):
    assert m.placeholders(block), block


@pytest.mark.parametrize(
    "block",
    [
        '~/.claude/skills/gstack/bin/gstack-skill-start --skill "review" --model "claude"',
        'X=$(git rev-parse HEAD); echo "$X"',
        'echo "${GSTACK_SKIP_ASIDE:-}" "$HOME" "$TMPDIR"',
        "for f in a b; do echo $f; done",
        "cat <<'JSON_END'\n{}\nJSON_END",
        'GSTACK_STATE_ROOT=$(gstack-paths --get GSTACK_STATE_ROOT); echo "$GSTACK_STATE_ROOT"',
        'eval "$(gstack-slug)"; gstack-slug --get SLUG',
    ],
)
def test_a_block_that_runs_as_written_has_no_placeholder(block):
    assert m.placeholders(block) == [], block


def test_worker_config_turns_off_the_update_check_telemetry_outside_reviews_and_the_detector():
    lines = set(m.GSTACK_CONFIG.splitlines())
    assert {"update_check: false", "telemetry: off", "codex_reviews: disabled", "design_detector: off"} <= lines


# ── smoke ─────────────────────────────────────────────────────────────────


def _smoke_install(tmp_path: Path) -> Path:
    root = tmp_path / "gstack"
    _write(
        root / "bin" / "gstack-ok",
        "#!/usr/bin/env bash\n"
        'echo "kind=$GSTACK_SESSION_KIND home=$HOME"\n'
        'mkdir -p "$GSTACK_HOME/sessions" && touch "$GSTACK_HOME/sessions/1"\n',
        0o755,
    )
    _write(root / "bin" / "gstack-fail", "#!/usr/bin/env bash\necho 'boom: Permission denied' >&2\nexit 4\n", 0o755)
    _write(
        root / "bin" / "gstack-writes-install",
        '#!/usr/bin/env bash\nD="$(cd "$(dirname "$0")/.." && pwd)"\necho x > "$D/lease" || true\n',
        0o755,
    )
    _write(
        root / "alpha" / "SKILL.md",
        "```bash\n~/.claude/skills/gstack/bin/gstack-ok\n```\n"
        "```bash\n~/.claude/skills/gstack/bin/gstack-ok --base <base>\n```\n"
        "```bash\ngit remote get-url origin\n```\n"
        "```bash\n~/.claude/skills/gstack/bin/gstack-writes-install\n```\n",
    )
    _write(root / "beta" / "SKILL.md", "```bash\n~/.claude/skills/gstack/bin/gstack-fail\n```\n")
    _write(
        root / "gamma" / "SKILL.md",
        "---\nname: gamma\nallowed-tools:\n  - \"Bash(~/.claude/skills/gstack/bin/gstack-launcher *)\"\n---\nNo blocks.\n",
    )
    return root


def test_smoke_runs_unattended_with_home_in_the_workspace(tmp_path):
    root = _smoke_install(tmp_path)
    report = m.smoke(root, ["alpha", "beta", "gamma", "missing"], tmp_path / "work", read_only=False)
    alpha = report["skills"]["alpha"]
    first = alpha["runs"][0]
    assert first["status"] == "ok"
    home = (tmp_path / "work" / "home").resolve()
    assert f"kind=unattended home={home}" in first["stdout_tail"]
    assert (home / ".claude" / "skills" / "gstack").resolve() == root.resolve()
    assert report["env"]["GSTACK_SESSION_KIND"] == "unattended"
    assert "sessions/1" in report["gstack_home_files"]
    # The placeholder block and the plain git block were skipped, never passed.
    assert [r["status"] for r in alpha["runs"]] == ["ok", "skipped", "skipped", "ok"]
    assert alpha["ran"] == 2 and alpha["skipped"] == 2
    # The writable run names what a helper wrote into the install.
    assert alpha["install_writes"] == ["lease"]


def test_a_failing_helper_fails_and_keeps_its_signal(tmp_path):
    root = _smoke_install(tmp_path)
    report = m.smoke(root, ["beta"], tmp_path / "work", read_only=False)
    beta = report["skills"]["beta"]
    assert beta["status"] == "failed" and beta["failed"] == 1
    assert beta["runs"][0]["rc"] == 4
    assert any("Permission denied" in s for s in beta["runs"][0]["signals"])


def test_a_missing_launcher_is_a_failure_not_a_skill_with_nothing_to_run(tmp_path):
    root = _smoke_install(tmp_path)
    report = m.smoke(root, ["gamma", "missing"], tmp_path / "work", read_only=False)
    gamma = report["skills"]["gamma"]
    assert gamma["status"] == "failed"
    assert gamma["runs"][0]["helper"] == "bin/gstack-launcher"
    assert report["skills"]["missing"] == {"status": "missing"}


def test_read_only_is_proved_by_a_probe_write(tmp_path):
    root = _smoke_install(tmp_path)
    with pytest.raises(SystemExit, match="accepted a write"):
        m.smoke(root, ["alpha"], tmp_path / "work", read_only=True)
    assert not (root / ".swarm-read-only-probe").exists()


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file modes")
def test_a_read_only_install_runs_and_writes_nothing_into_it(tmp_path):
    root = _smoke_install(tmp_path)
    for path in [root, *root.rglob("*")]:
        if not path.is_symlink():
            path.chmod(path.stat().st_mode & ~0o222)
    try:
        report = m.smoke(root, ["alpha"], tmp_path / "work", read_only=True)
    finally:
        for path in [root, *root.rglob("*")]:
            if not path.is_symlink():
                path.chmod(path.stat().st_mode | 0o200)
    assert report["read_only"] is True
    alpha = report["skills"]["alpha"]
    assert alpha["status"] == "ok"
    assert alpha["install_writes"] == []
    assert not (root / "lease").exists()


def test_main_writes_the_report_and_a_summary(tmp_path, capsys):
    root = _install(tmp_path)
    out = tmp_path / "report.json"
    assert m.main(["--install", str(root), "--skills", "review", "--json", str(out)]) == 0
    text = capsys.readouterr().out
    assert "install:" in text and "offered-skill closure:" in text
    data = out.read_text()
    assert '"version": "1.0.0"' in data
