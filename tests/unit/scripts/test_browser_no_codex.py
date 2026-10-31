"""BROWSER-NO-CODEX (owner decision 2026-10-13 ~07:15 PDT, answering
BROWSER-ENFORCE's question in PR 1248).

codex (129.7 MB as a layer, docs/browser-startup-speed.md §2.3) sits in
agent-runtime-base's `npm install -g` layer, so every agent-runtime-browser
image carried it although `claude-code-browser` never runs it. A derived image
cannot drop an inherited layer -- deleting the files adds a whiteout and the
bytes are still pulled -- so the browser image is built FROM a second build of
the base, made without codex. Asserted here, against the real files, without
building anything (builds run in CI):

  * images/agent-runtime-base/Dockerfile takes `ARG INSTALL_CODEX=1`, and its
    one `npm install -g` RUN installs codex only when it is 1: the RUN itself
    is executed against a fake `npm`, both ways;
  * build-images.sh builds that second base as its own image,
    `agent-runtime-base-nocodex`, from the base's Dockerfile with
    `INSTALL_CODEX=0` -- and passes INSTALL_CODEX nowhere else -- and
    agent-runtime-browser is built FROM it: on main (the generated config, the
    checked-in browser recipe, build_after) and in a pull request's
    --build-only chain;
  * the new image is promoted and gated like every other one: it is in both
    scripts' ALL_TARGETS, so it is in the build record that push-images.sh
    promotes, `image-sizes.sh --duplicate-layers` gates and `--manifest`
    measures (it is an `agent-runtime-*` image);
  * the browser image's build-time self-test fails when `codex` is on its
    PATH, and when any other tool of the base is missing.

MUTATION: drop the `INSTALL_CODEX` condition from the base's RUN, pass
`INSTALL_CODEX=0` to agent-runtime-base, point the browser recipe back at
`agent-runtime-base:`, or drop `no_codex()` from selftest.py's main, and a
test below fails.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from .test_build_images_concurrency import _build_after
from .test_build_images_concurrency import _run as _run_main
from .test_build_images_pr_check import TAG as BUILD_ONLY_TAG
from .test_build_images_pr_check import _affected, _build_only

REPO = Path(__file__).resolve().parents[3]
BASE_DOCKERFILE = REPO / "images" / "agent-runtime-base" / "Dockerfile"
BROWSER = REPO / "images" / "agent-runtime-browser"
SELFTEST = BROWSER / "selftest.py"
SPEED_DOC = REPO / "docs" / "browser-startup-speed.md"
NOCODEX = "agent-runtime-base-nocodex"


def _instructions(path: Path) -> list[str]:
    """Dockerfile instructions with continuations joined as Docker joins them."""
    out: list[str] = []
    current = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if not current and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if current and line.lstrip().startswith("#"):
            continue
        if line.endswith("\\"):
            current += line[:-1]
            continue
        out.append(current + line)
        current = ""
    return out


def _cli_run() -> str:
    runs = [i for i in _instructions(BASE_DOCKERFILE)
            if i.startswith("RUN ") and "@anthropic-ai/claude-code@" in i]
    assert len(runs) == 1, f"expected one RUN installing the agent CLIs, found {len(runs)}"
    return runs[0][len("RUN "):]


def _npm_args(tmp_path: Path, install_codex: str) -> list[str]:
    bindir = tmp_path / f"bin-{install_codex}"
    bindir.mkdir()
    log = tmp_path / f"npm-{install_codex}.log"
    npm = bindir / "npm"
    npm.write_text(f'#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done >>"{log}"\n'
                   f'printf -- "--\\n" >>"{log}"\n')
    npm.chmod(0o755)
    env = {
        "PATH": f"{bindir}{os.pathsep}/usr/bin:/bin",
        "INSTALL_AGENT_CLIS": "1",
        "INSTALL_CODEX": install_codex,
        "CLAUDE_CODE_VERSION": "9.9.9",
        "CODEX_VERSION": "8.8.8",
        "NPM_CONFIG_PREFIX": str(tmp_path / f"prefix-{install_codex}"),
    }
    done = subprocess.run(["sh", "-c", _cli_run()], env=env, capture_output=True, text=True,
                          timeout=30, check=False)
    assert done.returncode == 0, done.stderr
    return log.read_text().splitlines() if log.exists() else []


# --- the base Dockerfile -------------------------------------------------------


def test_the_base_declares_install_codex_defaulting_to_on():
    text = BASE_DOCKERFILE.read_text(encoding="utf-8")
    runtime = text[text.index("AS runtime"):]
    assert re.search(r"^ARG INSTALL_CODEX=1$", runtime, re.M), "no `ARG INSTALL_CODEX=1` in the runtime stage"
    assert runtime.index("ARG INSTALL_CODEX=1") < runtime.index("@anthropic-ai/claude-code@")


def test_the_default_base_installs_both_clis_in_one_npm_call(tmp_path):
    args = _npm_args(tmp_path, "1")
    installs = [a for a in args if "@" in a]
    assert installs == ["@anthropic-ai/claude-code@9.9.9", "@openai/codex@8.8.8"], args
    assert args.count("install") == 1, f"the CLIs are no longer one npm install: {args}"


def test_the_nocodex_base_installs_claude_code_and_not_codex(tmp_path):
    args = _npm_args(tmp_path, "0")
    assert "@anthropic-ai/claude-code@9.9.9" in args, args
    assert not [a for a in args if "codex" in a], f"INSTALL_CODEX=0 still installs codex: {args}"


def test_codex_is_skipped_in_the_same_run_not_deleted_in_a_later_one():
    """Deleting codex in a later RUN only whites it out (the bytes are still
    pulled), and a check-only RUN can write a duplicate layer (PR 1239)."""
    instructions = _instructions(BASE_DOCKERFILE)
    codex_runs = [i for i in instructions if i.startswith("RUN ") and "codex" in i]
    assert codex_runs == [f"RUN {_cli_run()}"], codex_runs


# --- build-images.sh: main ----------------------------------------------------


def _targets(script: str) -> list[str]:
    found = re.search(r"^ALL_TARGETS=\(([^)]*)\)$", (REPO / "scripts" / script).read_text(), re.M)
    assert found, f"no ALL_TARGETS in scripts/{script}"
    return found.group(1).split()


@pytest.mark.parametrize("script", ["build-images.sh", "push-images.sh"])
def test_the_nocodex_base_is_built_and_promoted(script):
    targets = _targets(script)
    assert NOCODEX in targets, targets
    assert targets.index(NOCODEX) == targets.index("agent-runtime-base") + 1, targets


def test_the_browser_is_ordered_after_the_nocodex_base_and_the_indexer_after_the_base():
    assert _build_after("agent-runtime-browser") == NOCODEX
    assert _build_after("agent-runtime-indexer") == "agent-runtime-base"
    assert _build_after(NOCODEX) == ""


def test_the_browser_recipe_builds_from_the_nocodex_base_by_digest():
    config = yaml.safe_load((BROWSER / "cloudbuild.yaml").read_text(encoding="utf-8"))
    steps = {s["id"]: s for s in config["steps"]}
    resolve = "\n".join(steps["resolve-base"]["args"])
    assert f"{NOCODEX}:$${{tag}}" in resolve, resolve
    assert not re.search(r"agent-runtime-base:", resolve), resolve
    assert "RepoDigests" in resolve
    assert '--build-arg "BASE_IMAGE=$${base}"' in "\n".join(steps["build"]["args"])


def test_main_builds_the_nocodex_base_with_install_codex_0_and_records_it(tmp_path):
    root, proc, _ = _run_main(
        tmp_path, ["agent-runtime-browser", "agent-runtime-base", NOCODEX, "agent-runtime-indexer"],
        parallel=4, seconds=0.2,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    build = root / "build"
    nocodex = (build / f"cloudbuild-{NOCODEX}.yaml").read_text()
    base = (build / "cloudbuild-agent-runtime-base.yaml").read_text()
    assert "images/agent-runtime-base/Dockerfile" in nocodex
    assert "INSTALL_CODEX=0" in nocodex
    assert f"/{NOCODEX}:" in nocodex, "the nocodex build is tagged as its own image"
    # The same tool-mirror step the base's build has: it is the same Dockerfile.
    assert "tool-mirror" in nocodex and "tool-mirror" in base
    assert "INSTALL_CODEX" not in base, "the default base must stay the codex base"

    manifest = json.loads((build / "images-dev.json").read_text())
    entries = {e["name"]: e for e in manifest["images"]}
    assert entries[NOCODEX]["image"].endswith(f"/{NOCODEX}")
    assert entries[NOCODEX]["build_args"] == ["INSTALL_CODEX=0"]
    assert entries["agent-runtime-base"]["build_args"] == []
    assert entries[NOCODEX]["digest"].startswith("sha256:")


def test_a_change_to_the_base_reaches_the_nocodex_base_and_the_browser(tmp_path):
    proc = _affected(tmp_path, ["images/agent-runtime-base/Dockerfile"])
    assert proc.returncode == 0, proc.stderr
    assert set(proc.stdout.split()) == {
        "agent-runtime-base", NOCODEX, "agent-runtime-browser", "agent-runtime-indexer",
        # Built FROM the base by digest too (LIGHTPANDA-BUILD-SCRIPTS).
        "agent-runtime-lightpanda",
    }


# --- build-images.sh: a pull request's --build-only chain -----------------------


def _build_steps(text: str) -> list[list[str]]:
    return [[str(a) for a in s.get("args") or []] for s in (yaml.safe_load(text) or {}).get("steps") or []
            if (s.get("args") or [None])[0] == "build"]


def _flag(args: list[str], name: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args[:-1]) if a == name]


def test_build_only_chain_builds_the_nocodex_base_then_the_browser_from_it(tmp_path):
    _, proc, events = _build_only(
        tmp_path, "--build-only", "--tag", BUILD_ONLY_TAG,
        "agent-runtime-base", NOCODEX, "agent-runtime-browser", "agent-runtime-indexer",
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    submits = {e["target"]: e for e in events if e["args"][:2] == ["builds", "submit"]}
    # Each base is built inside the chain built FROM it, never on its own.
    assert set(submits) == {"agent-runtime-browser", "agent-runtime-indexer"}, sorted(submits)

    browser = _build_steps(submits["agent-runtime-browser"]["text"])
    assert len(browser) == 2, browser
    base, derived = browser
    assert _flag(base, "-f") == ["images/agent-runtime-base/Dockerfile"]
    assert "INSTALL_CODEX=0" in _flag(base, "--build-arg"), base
    assert _flag(base, "-t")[0].split(":")[0].endswith(f"/{NOCODEX}"), base
    assert _flag(derived, "-f") == ["images/agent-runtime-browser/Dockerfile"]
    assert f"BASE_IMAGE={_flag(base, '-t')[0]}" in _flag(derived, "--build-arg"), derived

    indexer = _build_steps(submits["agent-runtime-indexer"]["text"])
    assert _flag(indexer[0], "-f") == ["images/agent-runtime-base/Dockerfile"]
    assert _flag(indexer[0], "-t")[0].split(":")[0].endswith("/agent-runtime-base"), indexer[0]
    every_arg = [a for event in submits.values() for step in _build_steps(event["text"])
                 for a in _flag(step, "--build-arg")]
    assert [a for a in every_arg if a.startswith("INSTALL_CODEX")] == ["INSTALL_CODEX=0"], every_arg


def test_build_only_base_alone_is_built_alone_when_only_the_browser_is_beside_it(tmp_path):
    """The browser's chain builds the codex-less base, so it cannot stand in
    for the default base: with no indexer in the run, the base is its own build."""
    _, proc, events = _build_only(
        tmp_path, "--build-only", "--tag", BUILD_ONLY_TAG, "agent-runtime-base", "agent-runtime-browser",
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    submits = {e["target"]: e for e in events if e["args"][:2] == ["builds", "submit"]}
    assert set(submits) == {"agent-runtime-base", "agent-runtime-browser"}, sorted(submits)
    base = _build_steps(submits["agent-runtime-base"]["text"])
    assert len(base) == 1 and not [a for a in _flag(base[0], "--build-arg") if "INSTALL_CODEX" in a]


# --- the browser image's self-test ------------------------------------------------


def _selftest(monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/opt/playwright")
    spec = importlib.util.spec_from_file_location("browser_selftest", SELFTEST)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _which(present: set[str]):
    return lambda name, *a, **kw: f"/usr/local/bin/{name}" if name in present else None


def test_the_self_test_refuses_an_image_with_codex(monkeypatch):
    selftest = _selftest(monkeypatch)
    present = set(selftest.TOOLS) | {"codex"}
    monkeypatch.setattr(selftest.shutil, "which", _which(present))
    with pytest.raises(SystemExit):
        selftest.no_codex()


def test_the_self_test_passes_without_codex_and_with_every_other_tool(monkeypatch, capsys):
    selftest = _selftest(monkeypatch)
    monkeypatch.setattr(selftest.shutil, "which", _which(set(selftest.TOOLS)))
    monkeypatch.setattr(selftest.os.path, "exists", lambda p: False)
    selftest.no_codex()
    out = capsys.readouterr().out
    assert "codex is absent" in out and f"{len(selftest.TOOLS)} tools" in out, out


@pytest.mark.parametrize("missing", ["claude", "gh", "terraform", "node"])
def test_the_self_test_refuses_an_image_missing_another_tool(monkeypatch, missing):
    selftest = _selftest(monkeypatch)
    assert missing in selftest.TOOLS
    monkeypatch.setattr(selftest.shutil, "which", _which(set(selftest.TOOLS) - {missing}))
    monkeypatch.setattr(selftest.os.path, "exists", lambda p: False)
    with pytest.raises(SystemExit):
        selftest.no_codex()


def test_the_self_test_names_the_base_toolbox():
    """Every tool the base's own toolbox smoke runs is checked here too."""
    selftest_tools = set(re.search(r"^TOOLS = \(([^)]*)\)", SELFTEST.read_text(), re.M | re.S).group(1)
                         .replace('"', " ").replace(",", " ").split())
    smoke = re.search(r'tools="([^"]+)"', BASE_DOCKERFILE.read_text()).group(1).split()
    assert set(smoke) <= selftest_tools, set(smoke) - selftest_tools
    assert "codex" not in selftest_tools


def test_the_self_test_runs_the_codex_check():
    main = SELFTEST.read_text().split('if __name__ == "__main__":', 1)[1]
    assert "no_codex()" in main


# --- the docs -------------------------------------------------------------------


def test_the_speed_doc_records_the_decision():
    text = SPEED_DOC.read_text(encoding="utf-8")
    section = text[text.index("### 2.3"):text.index("### 2.4")]
    assert "BROWSER-NO-CODEX" in section and NOCODEX in section
    assert "129.7" in section
    decisions = text[text.index("## 9."):text.index("## 10.")]
    assert "BROWSER-NO-CODEX" in decisions and "07:15" in decisions
