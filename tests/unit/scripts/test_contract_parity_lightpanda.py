"""Section 18 of `scripts/lib/check-contract-parity.sh`: claude-code-lightpanda.

docs/design/lightpanda-runtime.md §6.5 lists five things the profile depends
on that live in five files no one import joins: the frozen catalogue's entry
(no live view, no `browser_tool`), its Terraform mirror, the runner's CDP port
(never the live-browser 9222), the runner's denied `screenshot` tool, and the
image's Lightpanda pin, which `tool-mirror.sha256` must carry too.

Each test runs the section alone (cut from the script between its header and
the script's closing `hr`) against a fixture copy of exactly those files with
one item removed or contradicted, and expects it to fail naming the item. The
real tree passes. Offline: no cloud credentials, no emulator.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "lib" / "check-contract-parity.sh"
HEADER = "# -- 18. claude-code-lightpanda"

CATALOGUE = "apps/common/swarm_common/profiles.py"
LOCALS = "terraform/infra/locals.tf"
RUNNER = "apps/agent-worker/agent_worker/lightpanda.py"
DOCKERFILE = "images/agent-runtime-lightpanda/Dockerfile"
MIRROR = "images/agent-runtime-base/tool-mirror.sha256"
FILES = (LOCALS, RUNNER, DOCKERFILE, MIRROR)


def _section() -> str:
    text = SCRIPT.read_text()
    start = text.find(HEADER)
    assert start != -1, "check-contract-parity.sh has no claude-code-lightpanda section (§6.5)"
    end = text.find("\nhr\n", start)
    assert end != -1, "the section must sit before the script's closing hr"
    return text[start:end]


def _fixture(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    shutil.copytree(
        REPO / "apps" / "common" / "swarm_common",
        root / "apps" / "common" / "swarm_common",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    for rel in FILES:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / rel, root / rel)
    return root


def _edit(root: Path, rel: str, old: str, new: str, count: int = 1) -> None:
    path = root / rel
    text = path.read_text()
    assert old in text, f"the fixture edit no longer matches {rel}: {old!r}"
    path.write_text(text.replace(old, new, count))


def _run(tmp_path: Path, root: Path) -> subprocess.CompletedProcess[str]:
    harness = tmp_path / "section.sh"
    harness.write_text(
        "set -euo pipefail\n"
        f'source "{REPO}/scripts/lib/common.sh"\n'
        f'REPO_ROOT="{root}"\n'
        "FAILED=0\n"
        f"{_section()}\n"
        'exit "${FAILED}"\n'
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("SWARM_")}
    env["NO_COLOR"] = "1"
    return subprocess.run(
        ["bash", str(harness)], capture_output=True, text=True, env=env, timeout=120, check=False
    )


def test_the_real_tree_passes_every_item(tmp_path):
    result = _run(tmp_path, REPO)
    assert result.returncode == 0, result.stderr
    for item in ("live_browser", "locals.tf", "CDP_PORT", "DISALLOWED_TOOLS", "LIGHTPANDA_SHA256_AMD64"):
        assert re.search(r"ok.*" + re.escape(item), result.stderr), (item, result.stderr)


def test_an_untouched_fixture_copy_passes(tmp_path):
    result = _run(tmp_path, _fixture(tmp_path))
    assert result.returncode == 0, result.stderr


def _profile_missing(root):
    _edit(root, CATALOGUE, '"claude-code-lightpanda": RunnerProfile(\n        name="claude-code-lightpanda",',
          '"claude-code-lightpanda-gone": RunnerProfile(\n        name="claude-code-lightpanda-gone",')


def _live_browser(root):
    _edit(root, CATALOGUE, "inputs=_CLI_AGENT_MCP_INPUTS,   # claude-code's; no browser_tool",
          "inputs=_CLI_AGENT_MCP_INPUTS, live_browser=True,")


def _browser_tool(root):
    _edit(root, CATALOGUE, "inputs=_CLI_AGENT_MCP_INPUTS,   # claude-code's; no browser_tool",
          "inputs=_CLAUDE_CODE_BROWSER_INPUTS,")


def _locals_missing(root):
    _edit(root, LOCALS, '"claude-code-lightpanda" = {', '"claude-code-lightpanda-gone" = {')


def _locals_image(root):
    _edit(root, LOCALS, 'image           = "agent-runtime-lightpanda"', 'image           = "agent-runtime-browser"')


def _cdp_port(root):
    _edit(root, RUNNER, "CDP_PORT = 9223", "CDP_PORT = 9222")


def _no_screenshot(root):
    _edit(root, RUNNER, '    f"mcp__{MCP_SERVER_NAME}__screenshot",\n', "")


def _no_pin(root):
    path = root / DOCKERFILE
    path.write_text(re.sub(r"(?m)^ARG LIGHTPANDA_SHA256_AMD64=.*\n", "", path.read_text()))


def _mirror_lacks_pin(root):
    path = root / MIRROR
    path.write_text(re.sub(r"(?m)^\S+  \S+lightpanda-x86_64-linux\n", "", path.read_text()))


@pytest.mark.parametrize(
    ("break_it", "item"),
    [
        (_profile_missing, "claude-code-lightpanda"),
        (_live_browser, "live_browser"),
        (_browser_tool, "browser_tool"),
        (_locals_missing, "locals.tf"),
        (_locals_image, "locals.tf"),
        (_cdp_port, "CDP_PORT"),
        (_no_screenshot, "DISALLOWED_TOOLS"),
        (_no_pin, "LIGHTPANDA_SHA256_AMD64"),
        (_mirror_lacks_pin, "tool-mirror.sha256"),
    ],
    ids=lambda v: v.__name__.strip("_") if callable(v) else v,
)
def test_a_missing_or_contradicted_item_fails_and_is_named(tmp_path, break_it, item):
    root = _fixture(tmp_path)
    break_it(root)
    result = _run(tmp_path, root)
    assert result.returncode != 0, result.stderr
    failures = [line for line in result.stderr.splitlines() if "fail" in line]
    assert any(item in line for line in failures), (item, result.stderr)
