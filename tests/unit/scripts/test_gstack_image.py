"""gstack baked into the agent images at a pinned version, through the build mirror.

The owner, 3 November 2026: gstack first, baked into the images at a pinned
version, /cso left out of the first catalogue, no outside AI at first. The
packaging spike (docs/design/agent-plugins-packaging.md, "The image build
proposal") measured what to build; this file holds the images to it:

  * the two build-mirror rows (gstack v1.92.55.0's source archive and Bun
    1.4.2) are in images/agent-runtime-base/tool-mirror.sha256 at the spike's
    sha256, and every Dockerfile that reads them pins the same ARGs;
  * the build stage checks both archives, the tag's commit, and runs setup
    at the final path with the spike's flags, then fails when the closure
    differs from the checked-in lists (gstack-layers check);
  * the base image ships Bun in /usr/local/bin and the base list, the browser
    image adds the browser list, both staged root-owned without `.git`;
  * gstack-layers itself, on small installs in a temporary folder (no Bun, no
    network).
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
BASE_DIR = REPO / "images" / "agent-runtime-base"
BASE_DOCKERFILE = BASE_DIR / "Dockerfile"
BROWSER_DOCKERFILE = REPO / "images" / "agent-runtime-browser" / "Dockerfile"
MANIFEST = BASE_DIR / "tool-mirror.sha256"
LAYERS = BASE_DIR / "gstack-layers"
BASE_LIST = BASE_DIR / "gstack-base-files.txt"
BROWSER_LIST = REPO / "images" / "agent-runtime-browser" / "gstack-browser-files.txt"
MEASURE = REPO / "scripts" / "gstack_packaging_measure.py"

GSTACK_ROW = (
    "63c118f13ccf0d72d06d7bff56231dc08ce3da29ce405ed567224dd303cf5bd2",
    "github.com/garrytan/gstack/archive/refs/tags/v1.92.55.0.tar.gz",
)
BUN_ROW = (
    "36368faef7527875d5ffa52e53cd48021741f2a83eb6208a8dd64068d422a913",
    "github.com/oven-sh/bun/releases/download/bun-v1.4.2/bun-linux-x64.zip",
)
GSTACK_COMMIT = "2512dd7624a474fee036326989ea0c6bb8470e69"
PLUGIN_ROOT = "/opt/swarm/plugins/gstack"


def _load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


layers = _load("gstack_layers", LAYERS)
measure = _load("gstack_packaging_measure", MEASURE)


def _code_lines(text: str) -> list[str]:
    """Logical instructions, continuations joined, comment lines dropped."""
    out: list[str] = []
    cur: list[str] = []
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("#") or (not s and not cur):
            continue
        cur.append(s.rstrip("\\").rstrip())
        if not s.endswith("\\"):
            out.append(" ".join(cur))
            cur = []
    if cur:
        out.append(" ".join(cur))
    return out


def _args(path: Path) -> dict[str, str]:
    args: dict[str, str] = {}
    for line in _code_lines(path.read_text()):
        m = re.match(r"ARG\s+([A-Za-z_][A-Za-z0-9_]*)=(\S+)$", line)
        if m:
            args[m.group(1)] = m.group(2)
    return args


def _stages(path: Path) -> dict[str, list[str]]:
    """Each stage's name -> its instructions (the FROM line first)."""
    stages: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in _code_lines(path.read_text()):
        m = re.match(r"FROM\s+\S+(?:\s+AS\s+(\S+))?", line)
        if m:
            current = [line]
            stages[(m.group(1) or f"stage{len(stages)}").lower()] = current
        elif current is not None:
            current.append(line)
    return stages


def _manifest_rows() -> set[tuple[str, str]]:
    rows = set()
    for line in MANIFEST.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            sha, obj = line.split(None, 1)
            rows.add((sha, obj.strip()))
    return rows


def _render(value: str, args: dict[str, str]) -> str:
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: args.get(m.group(1), m.group(0)), value)


# --------------------------------------------------------------------------- #
# The pins: the mirror rows and every Dockerfile's ARGs agree.
# --------------------------------------------------------------------------- #


def test_the_mirror_lists_gstack_and_bun_at_the_spikes_sha256() -> None:
    rows = _manifest_rows()
    assert GSTACK_ROW in rows, f"{MANIFEST.name} has no row {GSTACK_ROW}"
    assert BUN_ROW in rows, f"{MANIFEST.name} has no row {BUN_ROW}"


def test_the_base_dockerfile_args_equal_the_mirror_rows() -> None:
    args = _args(BASE_DOCKERFILE)
    rows = dict((obj, sha) for sha, obj in _manifest_rows())
    gstack = f"github.com/garrytan/gstack/archive/refs/tags/v{args['GSTACK_VERSION']}.tar.gz"
    bun = f"github.com/oven-sh/bun/releases/download/bun-v{args['BUN_VERSION']}/bun-linux-x64.zip"
    assert rows.get(gstack) == args["GSTACK_SHA256"], (gstack, args["GSTACK_SHA256"])
    assert rows.get(bun) == args["BUN_SHA256"], (bun, args["BUN_SHA256"])
    assert args["GSTACK_COMMIT"] == GSTACK_COMMIT


def test_the_browser_dockerfile_builds_the_same_gstack() -> None:
    base, browser = _args(BASE_DOCKERFILE), _args(BROWSER_DOCKERFILE)
    for name in ("GSTACK_VERSION", "GSTACK_SHA256", "GSTACK_COMMIT"):
        assert browser.get(name) == base[name], f"{name}: browser {browser.get(name)}, base {base[name]}"


def _gstack_runs(path: Path, stage: str) -> list[str]:
    stages = _stages(path)
    assert stage in stages, f"{path.relative_to(REPO)} has no stage {stage}: {sorted(stages)}"
    return [line for line in stages[stage] if line.startswith("RUN ") and "./setup" in line]


@pytest.mark.parametrize(
    "path,stage,listed",
    [(BASE_DOCKERFILE, "gstack-build", "base"), (BROWSER_DOCKERFILE, "gstack-browser-build", "browser")],
    ids=["base", "browser"],
)
def test_the_build_stage_checks_and_sets_up_gstack_at_its_final_path(path: Path, stage: str, listed: str) -> None:
    runs = _gstack_runs(path, stage)
    assert len(runs) == 1, f"{path.relative_to(REPO)}: {len(runs)} RUNs run gstack's setup in {stage}"
    run = _render(runs[0], _args(path))
    assert f"file:///tmp/tool-mirror/{GSTACK_ROW[1]}" in run
    assert re.search(r'echo "\$\{GSTACK_SHA256\}\s+\S+" \| sha256sum -c', runs[0])
    assert "git get-tar-commit-id" in run and "${GSTACK_COMMIT}" in runs[0]
    assert f"-C {PLUGIN_ROOT} --strip-components=1" in run
    for flag in ("GSTACK_SKIP_FONTS=1", "GSTACK_SETUP_SKIP_CSO_BUILD=1", "GSTACK_SECURITY_OFF=1"):
        assert flag in run, flag
    assert "./setup --host claude --no-browser" in run
    # The build's HOME and gstack's state root are throwaway: none of what
    # setup writes there (a Stop hook, ~/.claude.json, ~/.gstack) ships.
    assert re.search(r'HOME="\$\{scratch\}/home"', run) and re.search(r'GSTACK_STATE_ROOT="\$\{scratch\}/', run)
    check = run.index("gstack-layers check")
    staged = run.index("gstack-layers stage")
    assert check < staged, "the lists are staged before they are checked"
    assert "--measure /tmp/gstack-build/gstack_packaging_measure.py" in run
    assert f"--list {listed} --out /out/gstack" in run


def test_the_base_build_stage_installs_the_pinned_bun_after_its_sha256() -> None:
    runs = _gstack_runs(BASE_DOCKERFILE, "gstack-build")
    run = _render(runs[0], _args(BASE_DOCKERFILE))
    assert f"file:///tmp/tool-mirror/{BUN_ROW[1]}" in run
    sha = runs[0].index('echo "${BUN_SHA256}')
    assert sha < runs[0].index("zipfile"), "Bun is unpacked before its sha256 is checked"
    assert "install -o root -g root -m 0755" in run and "/usr/local/bin/bun" in run
    assert 'test "$(/usr/local/bin/bun --version)" = "${BUN_VERSION}"' in runs[0]


def test_the_base_image_ships_bun_and_the_base_list_root_owned() -> None:
    runtime = _stages(BASE_DOCKERFILE)["runtime"]
    assert "COPY --from=gstack-build /usr/local/bin/bun /usr/local/bin/bun" in runtime
    assert f"COPY --from=gstack-build /out/gstack/ {PLUGIN_ROOT}/" in runtime
    copies = [line for line in runtime if line.startswith("COPY --from=gstack-build")]
    # No --chown: COPY writes uid 0, so the agent's uid cannot change it.
    assert all("--chown" not in line for line in copies), copies


def test_the_browser_image_adds_the_browser_list() -> None:
    stages = _stages(BROWSER_DOCKERFILE)
    assert list(stages)[-1] == "runtime", "the runtime is no longer the browser image's last stage"
    runtime = stages["runtime"]
    assert f"COPY --from=gstack-browser-build /out/gstack/ {PLUGIN_ROOT}/" in runtime
    build = stages["gstack-browser-build"]
    assert build[0] == "FROM ${BASE_IMAGE} AS gstack-browser-build"
    # A fresh tree at the final path: the compiled browse embeds the path it
    # was built at (finding 7), and the base's copy is only the base list.
    assert f"rm -rf {PLUGIN_ROOT}" in _gstack_runs(BROWSER_DOCKERFILE, "gstack-browser-build")[0]


# --------------------------------------------------------------------------- #
# The checked-in lists.
# --------------------------------------------------------------------------- #


def test_the_lists_are_of_the_pinned_version_sorted_and_disjoint() -> None:
    version = _args(BASE_DOCKERFILE)["GSTACK_VERSION"]
    base, browser = layers.read_list(BASE_LIST), layers.read_list(BROWSER_LIST)
    for path, files in ((BASE_LIST, base), (BROWSER_LIST, browser)):
        assert path.read_text().startswith(f"# gstack {version}: "), path.name
        assert files == sorted(set(files)), f"{path.name} is not sorted and unique"
        for rel in files:
            assert layers._refuse(rel) is None, f"{path.name}: {rel}"
    assert not set(base) & set(browser)


def test_the_base_list_holds_the_catalogue_and_no_binary_cso_or_node_modules() -> None:
    base = layers.read_list(BASE_LIST)
    for skill in layers.CATALOGUE_SKILLS:
        assert f"{skill}/SKILL.md" in base, skill
    assert not [f for f in base if f.startswith(("cso/", "node_modules/"))]
    assert "design/dist/design" not in base and layers.BROWSER_BINARY not in base
    assert "bin/gstack-cso-launcher" not in base


def test_the_browser_list_holds_browse_and_its_packages_but_no_tunnel_or_musl() -> None:
    browser = layers.read_list(BROWSER_LIST)
    assert layers.BROWSER_BINARY in browser and layers.BROWSER_ENTRY in browser
    packages = {f.split("/")[1] if not f.split("/")[1].startswith("@") else "/".join(f.split("/")[1:3])
                for f in browser if f.startswith("node_modules/")}
    assert {"playwright", "playwright-core", "sharp", "@img/sharp-linux-x64"} <= packages, packages
    assert not [p for p in packages if p.startswith("@ngrok/") or "linuxmusl" in p]
    assert "design/dist/design" not in browser


def test_the_catalogue_is_the_spikes_offered_list_without_cso() -> None:
    assert "cso" not in layers.CATALOGUE_SKILLS
    assert set(layers.CATALOGUE_SKILLS) == set(measure.OFFERED_SKILLS) - {"cso"}


# --------------------------------------------------------------------------- #
# gstack-layers on a small install.
# --------------------------------------------------------------------------- #

ELF = b"\x7fELF" + b"\0" * 60


def _fake_install(root: Path) -> Path:
    def put(rel: str, body: str | bytes, mode: int = 0o644) -> None:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            path.write_bytes(body)
        else:
            path.write_text(body)
        os.chmod(path, mode)

    put("VERSION", "1.0.0\n")
    for skill in layers.CATALOGUE_SKILLS:
        put(f"{skill}/SKILL.md", f"# {skill}\n\n```bash\n~/.claude/skills/gstack/bin/gstack-skill-start\n```\n")
    put("review/SKILL.md", "```bash\n~/.claude/skills/gstack/bin/gstack-skill-start\n$B goto x\n```\n"
        "~/.claude/skills/gstack/browse/dist/browse\n~/.claude/skills/gstack/design/dist/design\n")
    put("cso/SKILL.md", "# not in the catalogue\n")
    put("bin/gstack-skill-start", "#!/usr/bin/env bash\nbun run \"$(dirname \"$0\")/../lib/start.ts\"\n", 0o755)
    put("lib/start.ts", "import { x } from './shared.ts'\n")
    put("lib/shared.ts", "export const x = 1\n")
    put("lib/browser-only.ts", "export const y = 2\n")
    put("browse/dist/browse", ELF, 0o755)
    put("design/dist/design", ELF, 0o755)
    put("browse/src/server.ts",
        "import { chromium } from 'playwright'\nimport { tunnel } from '@ngrok/ngrok'\n"
        "import { y } from '../../lib/browser-only.ts'\nimport { x } from '../../lib/shared.ts'\n")
    put("browse/src/unused.ts", "export {}\n")
    put("node_modules/playwright/package.json", json.dumps({"dependencies": {"playwright-core": "1"}}))
    put("node_modules/playwright/index.js", "module.exports = {}\n")
    put("node_modules/playwright-core/package.json", "{}")
    put("node_modules/@ngrok/ngrok/package.json", "{}")
    put("node_modules/@img/sharp-linuxmusl-x64/package.json", "{}")
    return root


def test_derive_splits_the_closure_into_the_base_and_browser_lists(tmp_path: Path) -> None:
    root = _fake_install(tmp_path / "gstack")
    base = layers.base_files(measure, root)
    assert "bin/gstack-skill-start" in base and "lib/start.ts" in base and "lib/shared.ts" in base
    assert "browse/dist/browse" not in base and "design/dist/design" not in base
    assert not [f for f in base if f.startswith("cso/")]
    browser = layers.browser_files(measure, root, base)
    assert browser == [
        "browse/dist/browse",
        "browse/src/server.ts",
        "lib/browser-only.ts",
        "node_modules/playwright-core/package.json",
        "node_modules/playwright/index.js",
        "node_modules/playwright/package.json",
    ]


def _cli(tmp_path: Path, command: str, *extra: str) -> int:
    return layers.main([
        command, "--measure", str(MEASURE), "--install", str(tmp_path / "gstack"),
        "--base-list", str(tmp_path / "base.txt"), "--browser-list", str(tmp_path / "browser.txt"), *extra,
    ])


def test_check_passes_on_the_derived_lists_and_names_every_difference(tmp_path: Path, capsys) -> None:
    _fake_install(tmp_path / "gstack")
    assert _cli(tmp_path, "derive") == 0
    assert (tmp_path / "base.txt").read_text().startswith("# gstack 1.0.0: ")
    assert _cli(tmp_path, "check") == 0
    listed = (tmp_path / "base.txt").read_text().replace("lib/shared.ts\n", "lib/stale.ts\n")
    (tmp_path / "base.txt").write_text(listed)
    capsys.readouterr()
    assert _cli(tmp_path, "check") == 1
    err = capsys.readouterr().err
    assert "not in the checked-in list: lib/shared.ts" in err
    assert "not in the install's closure: lib/stale.ts" in err


def test_check_fails_when_a_catalogue_skill_is_missing(tmp_path: Path) -> None:
    root = _fake_install(tmp_path / "gstack")
    assert _cli(tmp_path, "derive") == 0
    (root / "spec" / "SKILL.md").unlink()
    with pytest.raises(SystemExit, match="no SKILL.md for: spec"):
        _cli(tmp_path, "check")


def test_stage_copies_with_the_image_modes(tmp_path: Path) -> None:
    root = _fake_install(tmp_path / "gstack")
    assert _cli(tmp_path, "derive") == 0
    out = tmp_path / "out"
    assert _cli(tmp_path, "stage", "--list", "base", "--out", str(out)) == 0
    staged = sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file())
    assert staged == layers.read_list(tmp_path / "base.txt")
    assert (out / "bin" / "gstack-skill-start").stat().st_mode & 0o777 == 0o755
    assert (out / "lib" / "start.ts").stat().st_mode & 0o777 == 0o644
    assert all(p.stat().st_mode & 0o777 == 0o755 for p in out.rglob("*") if p.is_dir())
    assert (out / "review" / "SKILL.md").read_bytes() == (root / "review" / "SKILL.md").read_bytes()


@pytest.mark.parametrize("rel", [".git/config", "../outside", "/etc/passwd", "lib/link.ts", "lib/absent.ts"])
def test_stage_refuses_a_path_it_must_not_copy(tmp_path: Path, rel: str) -> None:
    root = _fake_install(tmp_path / "gstack")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n")
    (root / "lib" / "link.ts").symlink_to(root / "lib" / "shared.ts")
    with pytest.raises(SystemExit, match=re.escape(rel)):
        layers.stage(root, ["lib/shared.ts", rel], tmp_path / "out")
    assert not (tmp_path / "out").exists(), "a refused list still copied files"
