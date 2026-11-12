"""`swarm-handoff compare` and `serve` work on a step with no live browser (part of #2442).

The owner's 2026-11-12 comment on #2442: a non-browser step (claude-code,
claude-code-large) renders its own branch with the repository's Playwright
and must compare each render with the frame PNGs before it finishes, as a
claude-code-browser step does. `swarm-handoff` now ships in
`agent-runtime-base` (its Dockerfile; tests/unit/scripts/
test_agent_toolbox_image.py pins the COPY), so on those steps it runs:

  * with no live path -- no hand-off spool ($SWARM_HANDOFF), no CDP endpoint,
    no gate -- because nothing on a plain claude-code step provides one;
  * without Pillow, which only the browser image's venv carries: the
    standard-library PNG decoder is the fallback;
  * as a program, the way the agent runs it (`swarm-handoff compare ...`), not
    as an imported module.

And every command that needs the live browser still refuses there, with exit
5 and the `no_live_path` message, rather than waiting on a spool nobody reads.

The PNGs are built here at runtime: two small RGB images, one with a block of
another colour.
"""

from __future__ import annotations

import json
import os
import re
import signal
import struct
import subprocess
import sys
import urllib.request
import zlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
COMMAND = REPO / "images" / "agent-runtime-browser" / "swarm-handoff"

#: Every environment variable that would give the script a live path.
LIVE_ENV = ("SWARM_HANDOFF", "SWARM_CDP_ENDPOINT", "SWARM_LIVE_GATE", "SWARM_RELAY_URL")

#: The commands that act on the live browser or its hand-off spool.
LIVE_ONLY = [
    ["request", "--reason", "sign in"],
    ["wait"],
    ["status"],
    ["device", "phone-390"],
    ["theme", "dark"],
    ["sign-in"],
    ["record", "start", "flow"],
]


def _png(width: int, height: int, pixel) -> bytes:
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            raw += bytes(pixel(x, y))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b"")


def _page(x: int, y: int):
    if y < 12:
        return (30, 30, 30)
    if 40 <= x < 120 and 40 <= y < 64:
        return (40, 90, 220)
    return (255, 255, 255)


def _changed(x: int, y: int):
    return (255, 0, 255) if 40 <= x < 120 and 40 <= y < 64 else _page(x, y)


def _environ(tmp_path: Path) -> dict[str, str]:
    """A plain step's environment: no live path, and Pillow hidden.

    A `PIL` package that raises ImportError, first on PYTHONPATH, stands in
    for an interpreter without Pillow even where the test runner has it."""
    hidden = tmp_path / "no-pillow" / "PIL"
    hidden.mkdir(parents=True)
    (hidden / "__init__.py").write_text("raise ImportError('Pillow is not in this image')\n")
    env = {k: v for k, v in os.environ.items() if k not in LIVE_ENV}
    env["PYTHONPATH"] = str(hidden.parent)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    env["SWARM_ARTIFACTS_DIR"] = str(artifacts)
    return env


def _run(args: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(COMMAND), *args], capture_output=True, text=True,
                          env=env, cwd=cwd, timeout=60, check=False)


def test_the_pillow_stand_in_really_hides_pillow(tmp_path: Path) -> None:
    env = _environ(tmp_path)
    proc = subprocess.run([sys.executable, "-c", "import PIL"], capture_output=True, text=True,
                          env=env, check=False)
    assert proc.returncode != 0 and "Pillow is not in this image" in proc.stderr, proc.stderr


def test_compare_runs_with_no_live_path_and_without_pillow(tmp_path: Path) -> None:
    env = _environ(tmp_path)
    (tmp_path / "frame.png").write_bytes(_png(200, 120, _page))
    (tmp_path / "same.png").write_bytes(_png(200, 120, _page))
    (tmp_path / "other.png").write_bytes(_png(200, 120, _changed))

    same = _run(["compare", "same.png", "frame.png"], env, tmp_path)
    assert same.returncode == 0, same.stdout + same.stderr
    report = json.loads(same.stdout.strip().splitlines()[-1])
    assert report["outcome"] == "compared", report
    # MUTATION: make compare refuse when $SWARM_HANDOFF is unset -- a plain step cannot compare.
    assert report["within_threshold"] is True, report
    assert report["decoder"] == "stdlib", "Pillow was hidden; the stdlib decoder must have run"
    assert Path(report["diff"]).is_file(), report

    other = _run(["compare", "other.png", "frame.png", "--name", "other"], env, tmp_path)
    report = json.loads(other.stdout.strip().splitlines()[-1])
    assert other.returncode == 1, other.stdout + other.stderr
    assert report["outcome"] == "compared" and report["within_threshold"] is False, report
    assert report["boxes"], "the changed block must be located"
    assert Path(env["SWARM_ARTIFACTS_DIR"], "compare", "other.diff.png").is_file()


def test_serve_runs_with_no_live_path(tmp_path: Path) -> None:
    env = _environ(tmp_path)
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<p>render me</p>\n")
    proc = _run(["serve", str(site)], env, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    answer = json.loads(proc.stdout.strip().splitlines()[-1])
    assert answer["outcome"] == "serving", answer
    try:
        assert answer["url"].startswith("http://127.0.0.1:"), answer
        with urllib.request.urlopen(answer["url"] + "index.html", timeout=10) as response:
            assert b"render me" in response.read()
    finally:
        os.kill(answer["pid"], signal.SIGTERM)


@pytest.mark.parametrize("args", LIVE_ONLY, ids=[a[0] for a in LIVE_ONLY])
def test_every_live_only_command_refuses_with_exit_5(tmp_path: Path, args: list[str]) -> None:
    env = _environ(tmp_path)
    proc = _run(args, env, tmp_path)
    # MUTATION: let a live-only command fall through to the spool -- it waits or crashes instead.
    assert proc.returncode == 5, proc.stdout + proc.stderr
    answer = json.loads(proc.stdout.strip().splitlines()[-1])
    assert answer["outcome"] == "refused" and answer["code"] == "no_live_path", answer
    assert "no live browser" in answer["message"], answer


def test_the_live_only_list_covers_every_command_but_compare_and_serve() -> None:
    proc = subprocess.run([sys.executable, str(COMMAND), "--help"], capture_output=True, text=True,
                          timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    listed = proc.stdout.split("{", 1)[1].split("}", 1)[0].split(",")
    # MUTATION: add a live command to the script without adding it here -- this fails.
    assert sorted(listed) == sorted([a[0] for a in LIVE_ONLY] + ["compare", "serve"]), listed


# --- docs/browser-qa.md: the recipe for a step without a live browser --------

DOC = REPO / "docs" / "browser-qa.md"
SUBSECTION = "### On a step without a live browser (claude-code, claude-code-large)"


def _recipe() -> str:
    text = DOC.read_text()
    assert text.count(SUBSECTION) == 1
    body = text.split(SUBSECTION, 1)[1]
    return body.split("\n### ", 1)[0]


def test_the_recipe_follows_section_6_2_and_stays_inside_section_6() -> None:
    text = DOC.read_text()
    assert text.index("### 6.2 ") < text.index(SUBSECTION) < text.index("### 6.3 ")


def test_the_recipe_names_only_flags_swarm_handoff_accepts() -> None:
    recipe = _recipe()
    out = subprocess.run(
        [sys.executable, str(COMMAND), "compare", "--help"], capture_output=True, text=True, check=True
    ).stdout
    accepted = set(re.findall(r"--[a-z][a-z-]*", out))
    assert {"--name", "--top-only", "--threshold"} <= accepted
    used = set()
    for line in recipe.splitlines():
        if "swarm-handoff compare" in line or line.strip().startswith("design/frames"):
            used |= set(re.findall(r"(?<!\S)--[a-z][a-z-]*", line))
    assert {"--name", "--top-only"} <= used
    assert used <= accepted
    commands = set(re.findall(r"swarm-handoff ([a-z-]+)", recipe))
    assert commands == {"compare"}


def test_the_recipe_commands_are_the_ones_the_playwright_cli_and_the_tool_take() -> None:
    recipe = _recipe()
    assert "npx playwright install chromium" in recipe
    assert "--with-deps" in recipe  # named only to forbid it
    assert re.search(r"[Nn]ever `--with-deps`", recipe)
    for flag in ("--color-scheme", "--viewport-size"):
        assert flag in recipe
    assert "--viewport-size 1440,900" in recipe and "--viewport-size 390,844" in recipe
    assert '"$scheme"' in recipe and "for scheme in light dark" in recipe
    assert "--full-page" in recipe and "Do not pass `--full-page`" in recipe
    assert "git worktree add" in recipe and "origin/main" in recipe
    assert "CREATE EXTENSION" in recipe and "127.0.0.1:5432" in recipe
    assert ".swarm/test-services.toml" in recipe


def test_the_recipe_names_the_git_fetch_tool_and_its_one_depth() -> None:
    recipe = _recipe()
    gittools = (REPO / "apps" / "agent-worker" / "agent_worker" / "gittools.py").read_text()
    assert '"git_fetch"' in gittools and 'args["ref"]' in gittools
    assert '`git_fetch` tool with `{"ref": "main", "depth": 1}`' in recipe
    runner_doc = (REPO / "docs" / "runner-git-tools.md").read_text()
    assert "`git_fetch {\"ref\": \"main\", \"depth\": 1}`" in runner_doc
    assert "worker-images.md#coderabbit" in recipe
    assert "## CodeRabbit" in (REPO / "docs" / "worker-images.md").read_text()


def test_the_recipe_keeps_the_no_browser_in_the_base_image_decision() -> None:
    recipe = _recipe()
    assert "no browser binary" in recipe
    assert "claude-code-browser" in recipe
    dockerfile = (REPO / "images" / "agent-runtime-base" / "Dockerfile").read_text()
    code = "\n".join(ln for ln in dockerfile.splitlines() if not ln.lstrip().startswith("#"))
    assert "playwright install" not in code
