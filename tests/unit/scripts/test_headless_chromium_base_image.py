"""agent-runtime-base carries what a headless Chromium needs, and no Chromium (FIX-2253).

WHY THIS EXISTS (BGD, 2026-10-26, fix (c) on #2253). A step on plain
claude-code could not run a repository's own `npx playwright test`: after
`npx playwright install chromium` the browser failed on a missing
`libglib-2.0.so.0`, and the agent is not root, so `install --with-deps` cannot
add it (task_69128d776a52467488e4). The one step that got through unpacked the
libraries and fonts under /tmp by hand, and without fonts its renderer died
with "Skia FontConfig FATAL" (task_e97a6439de29456ba6ab).

WHAT IS ASSERTED, reading images/agent-runtime-base/Dockerfile:

  * the apt upgrade layer installs every package Playwright's own dependency
    list names for Chromium on Debian 12 (`DEBIAN12_CHROMIUM`, copied from
    playwright-core 1.63.0's `debian12-x64.chromium`: what `install-deps
    chromium` would add), plus fontconfig and the fonts it must find;
  * the same RUN fails the build when fontconfig finds neither font or a
    library Chromium loads does not resolve, so a list that installs and
    still leaves the renderer without fonts cannot pass;
  * no browser binary is baked in: each repository pins its own Playwright
    and so its own Chromium revision, which goes into the agent's writable
    cache at run time;
  * the Dockerfile says why, and docs/browser-qa.md tells a step how to run
    its suite on both Claude profiles.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO / "images" / "agent-runtime-base" / "Dockerfile"
BROWSER_QA_DOC = REPO / "docs" / "browser-qa.md"

#: playwright-core 1.63.0 (the browser image's PLAYWRIGHT_VERSION) and 1.64.1,
#: `nativeDeps` `debian12-x64.chromium`, read 2026-10-26: the same 21 names.
DEBIAN12_CHROMIUM = (
    "libasound2", "libatk-bridge2.0-0", "libatk1.0-0", "libatspi2.0-0",
    "libcairo2", "libcups2", "libdbus-1-3", "libdrm2", "libgbm1",
    "libglib2.0-0", "libnspr4", "libnss3", "libpango-1.0-0", "libx11-6",
    "libxcb1", "libxcomposite1", "libxdamage1", "libxext6", "libxfixes3",
    "libxkbcommon0", "libxrandr2",
)
#: Fontconfig, the libraries it and the renderer read fonts through, and the
#: fonts: Liberation (metric-compatible Arial/Times/Courier) and colour emoji.
FONT_PACKAGES = (
    "fontconfig", "libfontconfig1", "libfreetype6", "fonts-liberation", "fonts-noto-color-emoji",
)


def _runtime_text() -> str:
    text = DOCKERFILE.read_text()
    start = re.search(r"^FROM .* AS runtime$", text, re.MULTILINE)
    assert start, "no runtime stage"
    return text[start.end():]


def _runs(text: str) -> list[str]:
    """Each RUN instruction's body on one line, comments dropped."""
    out: list[str] = []
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("#")]
    i = 0
    while i < len(lines):
        if lines[i].startswith("RUN "):
            parts = [lines[i]]
            while parts[-1].rstrip().endswith("\\") and i + 1 < len(lines):
                i += 1
                parts.append(lines[i])
            out.append(" ".join(" ".join(p.rstrip().rstrip("\\") for p in parts).split()))
        i += 1
    return out


def _apt_layer() -> str:
    layers = [r for r in _runs(_runtime_text()) if "apt-get upgrade" in r]
    assert len(layers) == 1, "expected one apt upgrade layer in the runtime stage"
    return layers[0]


def _chromium_install() -> list[str]:
    """The packages of the apt layer's `apt-get install` that carries libnss3."""
    layer = _apt_layer()
    for command in layer.split("&&"):
        words = command.split()
        if words[:2] == ["apt-get", "install"] and "libnss3" in words:
            assert "--no-install-recommends" in words, command
            return [w for w in words[2:] if not w.startswith("-")]
    raise AssertionError("the apt upgrade layer installs no Chromium libraries")


@pytest.mark.parametrize("package", DEBIAN12_CHROMIUM + FONT_PACKAGES)
def test_the_apt_layer_installs_what_a_headless_chromium_needs(package: str) -> None:
    # MUTATION: drop libglib2.0-0 from the list -- task_69128d776a52467488e4's failure returns.
    assert package in _chromium_install()


def test_the_build_fails_when_fontconfig_finds_no_font() -> None:
    layer = _apt_layer()
    # MUTATION: drop either fc-list check -- a fontconfig with no fonts ships and
    # the renderer dies with "Skia FontConfig FATAL" (task_e97a6439de29456ba6ab).
    assert re.search(r"fc-list [^&]*\| grep -q[^&]*Liberation Sans", layer), layer
    assert re.search(r"fc-list [^&]*\| grep -q[^&]*Noto Color Emoji", layer), layer
    assert layer.find("fonts-noto-color-emoji") < layer.find("fc-list"), "checked before installed"


def test_the_build_fails_when_a_library_chromium_loads_does_not_resolve() -> None:
    layer = _apt_layer()
    match = re.search(r'for lib in ([^;]+); do', layer)
    assert match, "the apt layer must check the libraries resolve, by name"
    libs = match.group(1).split()
    # MUTATION: drop libglib-2.0.so.0 from the loop -- the reported missing library is unchecked.
    for soname in ("libglib-2.0.so.0", "libnss3.so", "libgbm.so.1", "libfontconfig.so.1"):
        assert soname in libs, soname
    assert "/sbin/ldconfig -p" in layer, "resolved through the linker cache, by absolute path"
    assert "exit 1" in layer[match.start():], "a missing library must fail the build"


def test_no_browser_binary_is_baked_into_the_base_image() -> None:
    runtime = _runtime_text()
    code = "\n".join(ln for ln in runtime.splitlines() if not ln.strip().startswith("#"))
    # MUTATION: add `npx playwright install chromium` to a RUN -- every repository
    # would run the image's revision, not the one its @playwright/test pins.
    assert "playwright install" not in code
    assert "PLAYWRIGHT_BROWSERS_PATH" not in code, (
        "the base must leave Playwright's default (the agent's writable XDG cache)")


def test_the_dockerfile_says_why_each_block_is_there() -> None:
    text = DOCKERFILE.read_text()
    for needle in ("#2253", "Skia FontConfig FATAL", "libglib-2.0.so.0",
                   "install-deps", "each repository pins its own"):
        assert needle in text, needle


def test_the_doc_tells_a_step_how_to_run_its_suite_on_both_profiles() -> None:
    doc = BROWSER_QA_DOC.read_text()
    heading = re.search(
        r"^## (?:\d+\. )?Running a repository's own Playwright suite$", doc, re.MULTILINE)
    assert heading, "docs/browser-qa.md has no section on running a repository's own suite"
    section = doc[heading.end():].split("\n## ", 1)[0]
    for needle in ("claude-code-browser", "`claude-code`", "npx playwright install chromium",
                   "swarm-test-browsers npx playwright test", "SWARM_TEST_BROWSERS_PATH",
                   "XDG_CACHE_HOME", "--with-deps", "#2253"):
        assert needle in section, needle
