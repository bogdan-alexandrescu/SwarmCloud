"""agent-runtime-base carries a warmed, read-only npm cache built from the UI lockfile.

WHY THIS EXISTS. docs/design/agent-autonomy.md §3.10 (lane AU-8): a fix lane on
this repository's UI ran `npm ci` from an empty cache on every attempt. The
image now builds an npm cache from `apps/swarm-ui/package-lock.json` at build
time (`npm ci --cache /opt/npm-cache`), keeps the cache, discards the
`node_modules` that install wrote, makes the cache read-only and names it in
npm's own config with offline-first installs.

WHAT IS ASSERTED, each because its absence is a quiet failure, not a loud one:

  * the cache is built from the UI's lockfile, read through a bind mount of
    apps/swarm-ui: BuildKit keys the layer on the mounted files, so a lockfile
    change rebuilds it. Not a COPY, which scripts/build-images.sh would read
    (`dockerfile_copy_sources`) as an input of this image and the three built
    FROM it, so every pull request touching the UI lockfile would build four
    worker images; every main build rebuilds this base anyway (APT_REFRESH);
  * no `node_modules` is baked in: one copied into the image is wrong for
    every other repository and for this one the day the lockfile changes;
  * the cache is root-owned and read-only, so no agent can plant a tarball in
    it for the next install;
  * npm's BUILTIN config names the cache with prefer-offline. Not an `ENV`:
    the worker builds the runner's environment rather than inheriting it
    (tests/unit/worker/test_image_env_reaches_the_runner.py), and the builtin
    npmrc is read by every npm whatever HOME and the environment are;
  * the step runs after the agent-CLI install, whose `npm cache clean` must
    not meet a read-only cache, and before the drop to the agent user;
  * EXECUTED, against a stand-in `npm` that writes what the real one writes:
    the step leaves a cache, no node_modules, no install logs, nothing writable,
    and an npmrc that says what the design says.

NOT asserted here: that a real `npm ci` from the cache succeeds offline. That
needs the registry once; the image build does it, and the measurement is in the
Dockerfile's comment.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO / "images" / "agent-runtime-base" / "Dockerfile"
COMMON_SH = REPO / "scripts" / "lib" / "common.sh"

CACHE = "/opt/npm-cache"
#: npm's builtin config file: `<npm's own install dir>/npmrc`. The image's npm
#: is copied from the pinned node image into /usr/local/lib/node_modules/npm.
BUILTIN_NPMRC = "/usr/local/lib/node_modules/npm/npmrc"
UI_FILES = ("apps/swarm-ui/package.json", "apps/swarm-ui/package-lock.json")
UI_MOUNT = "--mount=type=bind,source=apps/swarm-ui,target=/tmp/swarm-ui"


# --------------------------------------------------------------------------- #
# A small Dockerfile reader: logical instructions in order.
# --------------------------------------------------------------------------- #


class Instruction:
    def __init__(self, keyword: str, body: str, index: int):
        self.keyword = keyword
        self.body = body
        self.index = index

    @property
    def shell(self) -> str:
        """The body with line continuations removed, as /bin/sh sees it."""
        return re.sub(r"\\\n", " ", self.body)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.keyword} {self.body[:60]!r}"


def _instructions(text: str) -> list[Instruction]:
    out: list[Instruction] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        parts = [lines[i].rstrip()]
        while parts[-1].endswith("\\") and i + 1 < len(lines):
            i += 1
            parts.append(lines[i].rstrip())
        keyword, _, body = "\n".join(parts).strip().partition(" ")
        out.append(Instruction(keyword.upper(), body, len(out)))
        i += 1
    return out


def _runtime_stage() -> list[Instruction]:
    instrs = _instructions(DOCKERFILE.read_text())
    start = next(
        i.index for i in instrs if i.keyword == "FROM" and re.search(r"\bAS runtime\b", i.body)
    )
    return [i for i in instrs if i.index > start]


def _warm_step() -> Instruction:
    runs = [i for i in _runtime_stage() if i.keyword == "RUN" and f"--cache {CACHE}" in i.shell]
    # MUTATION: delete the RUN that runs `npm ci --cache /opt/npm-cache` and this fails.
    assert len(runs) == 1, f"expected exactly one RUN that builds {CACHE}, found {runs}"
    return runs[0]


def _commands(ins: Instruction) -> list[str]:
    return [c.strip() for c in ins.shell.split("&&")]


# --------------------------------------------------------------------------- #
# Static: what the step does, and where it sits.
# --------------------------------------------------------------------------- #


def test_the_cache_is_built_by_npm_ci_from_the_ui_lockfile_without_install_scripts() -> None:
    step = _warm_step()
    ci = [c for c in _commands(step) if c.startswith("npm clean-install")]
    assert len(ci) == 1, f"the warm step must run exactly one `npm clean-install` (npm ci): {_commands(step)}"
    # Spelled `npm clean-install`, npm's own alias of `npm ci`: the indexer
    # split test reads `npm ci` in this Dockerfile as the index toolchain.
    # MUTATION: replace `npm clean-install` with `npm install` (which may rewrite the
    # lockfile it was handed) and this fails.
    assert f"--cache {CACHE}" in ci[0], ci[0]
    # MUTATION: drop --ignore-scripts and every dependency's install script runs as root.
    assert "--ignore-scripts" in ci[0].split(), ci[0]

    # MUTATION: drop the bind mount of apps/swarm-ui and this fails.
    assert step.shell.startswith(UI_MOUNT + " "), step.shell[:120]
    cmds = _commands(step)
    ci_at = next(n for n, c in enumerate(cmds) if c.startswith("npm clean-install"))
    read = " ".join(cmds[:ci_at])
    for path in UI_FILES:
        mounted = "/tmp/swarm-ui/" + Path(path).name
        # MUTATION: drop /tmp/swarm-ui/package-lock.json from the cp and this fails.
        assert mounted in read.split(), f"{mounted} is not read before npm ci: {cmds[:ci_at]}"


def test_the_lockfile_is_not_a_copy_input_of_the_worker_images() -> None:
    """scripts/build-images.sh builds, on a pull request, every image whose COPY
    lines name a changed path, and the images built FROM it. The UI lockfile as
    a COPY source would build agent-runtime-base, -nocodex, -browser and
    -indexer on every pull request that touches it, for a cache BuildKit
    already rebuilds from the bind mount and every main build rebuilds anyway."""
    out = subprocess.run(
        ["bash", "-c", 'source "$1" >/dev/null && dockerfile_copy_sources "$2"', "_",
         str(COMMON_SH), str(DOCKERFILE)],
        capture_output=True, text=True, check=True, cwd=REPO,
    ).stdout.split()
    # Counted, so a reader that printed nothing cannot read as a pass.
    assert "apps/agent-worker/" in out, out
    for path in UI_FILES:
        # MUTATION: add `COPY apps/swarm-ui/package-lock.json /opt/npm-cache-lockfile/` and this fails.
        assert path not in out, f"{path} became a COPY input of the worker images: {out}"


def test_no_node_modules_is_kept_in_the_image() -> None:
    step = _warm_step()
    cmds = _commands(step)
    cds = [c.split()[1] for c in cmds if c.startswith("cd ") and c.split()[1] != "/"]
    assert len(cds) == 1, f"the step must install in one scratch directory: {cmds}"
    workdir = cds[0]
    assert workdir.startswith("/tmp/"), f"npm ci must run in scratch, not {workdir}"
    ci_at = next(n for n, c in enumerate(cmds) if c.startswith("npm clean-install"))
    removed = [n for n, c in enumerate(cmds) if c.startswith("rm -rf") and workdir in c.split()]
    # MUTATION: delete `rm -rf /tmp/npm-warm` and the 228 MB node_modules ships in the image.
    assert removed and removed[0] > ci_at, f"{workdir} (and its node_modules) is not removed after npm ci"
    for ins in _runtime_stage():
        if ins.keyword in {"COPY", "ADD"}:
            # MUTATION: add `COPY apps/swarm-ui/node_modules ...` and this fails.
            assert "node_modules" not in ins.body or "--from=nodebin" in ins.body, ins


def test_the_cache_is_root_owned_and_read_only() -> None:
    cmds = _commands(_warm_step())
    ci_at = next(n for n, c in enumerate(cmds) if c.startswith("npm clean-install"))
    chown = [n for n, c in enumerate(cmds) if c.startswith("chown -R root:root") and CACHE in c.split()]
    chmod = [n for n, c in enumerate(cmds) if c.startswith("chmod -R a-w") and CACHE in c.split()]
    # MUTATION: delete the chown and the cache keeps whatever owner npm gave it.
    assert chown and chown[0] > ci_at, cmds
    # MUTATION: delete `chmod -R a-w` and an agent could plant a tarball in the cache.
    assert chmod and chmod[0] > ci_at, cmds
    npmrc_chmod = [n for n in chmod if BUILTIN_NPMRC in cmds[n].split()]
    # MUTATION: drop the npmrc from the chmod and it stays rewritable by its owner.
    assert npmrc_chmod, f"{BUILTIN_NPMRC} must be read-only too: {cmds}"


def test_npms_builtin_config_names_the_cache_with_offline_first_installs() -> None:
    text = _warm_step().shell
    writes = re.findall(r"printf '%s\\n'((?:\s+'[^']*')+)\s*>\s*(\S+)", text)
    assert len(writes) == 1, f"expected one printf into npm's builtin npmrc: {text}"
    lines_, target = writes[0]
    settings = re.findall(r"'([^']*)'", lines_)
    # MUTATION: write to /usr/local/etc/npmrc (globalconfig, which follows the
    # prefix the runner does not carry) and this fails.
    assert target == BUILTIN_NPMRC, target
    # MUTATION: write cache=/opt/npm-cachex and this fails.
    assert f"cache={CACHE}" in settings, settings
    # MUTATION: drop prefer-offline=true and every install revalidates against the registry.
    assert "prefer-offline=true" in settings, settings
    # The build itself proves npm reads the file it wrote.
    # MUTATION: delete the `npm config get cache` check and this fails.
    assert re.search(rf'test "\$\(npm config get cache[^)]*\)" = {re.escape(CACHE)}', text), text


def test_the_cache_is_not_named_by_an_image_env() -> None:
    """The worker builds its runner's environment and drops unlisted image ENV
    (test_image_env_reaches_the_runner.py); an `ENV NPM_CONFIG_CACHE` would
    also override every derived image's own `npm_config_cache=` build step."""
    for ins in _runtime_stage():
        if ins.keyword == "ENV":
            # MUTATION: add `NPM_CONFIG_CACHE=/opt/npm-cache` to the runtime ENV and this fails.
            assert not re.search(r"(?i)\bnpm_config_(cache|prefer_offline)=", ins.body), ins


def test_the_step_follows_the_cli_install_and_precedes_the_agent_user() -> None:
    stage = _runtime_stage()
    step = _warm_step()
    cli = [i for i in stage if i.keyword == "RUN" and "npm cache clean" in i.shell]
    assert len(cli) == 1, f"expected the agent-CLI install's one `npm cache clean`: {cli}"
    # MUTATION: move the warm step above the CLI install, whose `npm cache
    # clean` would then empty the builtin cache, and this fails.
    assert step.index > cli[0].index
    users = [i for i in stage if i.keyword == "USER"]
    # MUTATION: move the warm step below `USER swarm:swarm` and the chown fails at build.
    assert users and step.index < users[-1].index


# --------------------------------------------------------------------------- #
# Executed: the step's shell, against a stand-in npm.
# --------------------------------------------------------------------------- #

#: Writes what `npm ci --cache X` writes (X/_cacache, X/_logs, ./node_modules)
#: and answers `npm config get` from the npmrc the step wrote. No registry.
FAKE_NPM = r"""#!/bin/sh
set -eu
case "$1" in
  ci|clean-install)
    cache=""
    while [ $# -gt 0 ]; do
      if [ "$1" = "--cache" ]; then cache="$2"; fi
      shift
    done
    [ -f package-lock.json ] || { echo "npm ci: no lockfile" >&2; exit 1; }
    mkdir -p "$cache/_cacache/content-v2" "$cache/_logs" node_modules/pkg
    echo tarball > "$cache/_cacache/content-v2/blob"
    echo log > "$cache/_logs/debug.log"
    echo js > node_modules/pkg/index.js
    ;;
  config)
    sed -n "s/^$3=//p" "$FAKE_NPMRC"
    ;;
  *) echo "fake npm: unexpected $*" >&2; exit 2 ;;
esac
"""


@pytest.fixture
def ran_step(tmp_path: Path) -> Iterator[tuple[Path, subprocess.CompletedProcess[str]]]:
    """The warm step's shell, run with every image path moved under tmp_path."""
    root = tmp_path / "root"
    (root / "opt").mkdir(parents=True)
    npmrc = root / "npmrc"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "npm").write_text(FAKE_NPM)
    # The test is not root: chown is the one command it cannot run for real.
    (bin_dir / "chown").write_text("#!/bin/sh\nexit 0\n")
    for tool in bin_dir.iterdir():
        tool.chmod(0o755)

    body = _warm_step().shell
    assert body.startswith(UI_MOUNT + " ")
    body = body[len(UI_MOUNT):]
    body = body.replace("/tmp/swarm-ui/", f"{REPO}/apps/swarm-ui/")
    body = body.replace(BUILTIN_NPMRC, str(npmrc))
    body = body.replace("/tmp/npm-warm", str(root / "warm"))
    body = body.replace("/opt/", f"{root}/opt/")
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_NPMRC": str(npmrc),
        "HOME": str(tmp_path),
    }
    proc = subprocess.run(["sh", "-c", body], env=env, capture_output=True, text=True)
    yield root, proc
    for path in (root / "opt").rglob("*"):
        path.chmod(path.stat().st_mode | stat.S_IWUSR)
    (root / "opt").chmod(0o755)


def test_executed_step_keeps_the_cache_and_nothing_else(ran_step) -> None:
    root, proc = ran_step
    assert proc.returncode == 0, proc.stderr + proc.stdout
    cache = root / "opt" / "npm-cache"
    # MUTATION: point `npm ci --cache` somewhere else and this fails.
    assert (cache / "_cacache" / "content-v2" / "blob").is_file()
    # MUTATION: delete `rm -rf /tmp/npm-warm` and this fails.
    assert not (root / "warm").exists(), "the scratch install (and its node_modules) was kept"
    # MUTATION: delete `rm -rf /opt/npm-cache/_logs` and this fails.
    assert not (cache / "_logs").exists(), "npm's install logs were left in the cache"
    assert not list(root.rglob("node_modules")), "a node_modules was left behind"
    kept = sorted(p.name for p in (root / "opt" / "npm-cache-lockfile").iterdir())
    # MUTATION: drop the cp into /opt/npm-cache-lockfile and this fails.
    assert kept == ["package-lock.json", "package.json"], kept
    assert "npm cache warmed" in proc.stdout, proc.stdout


def test_executed_step_leaves_nothing_writable(ran_step) -> None:
    root, proc = ran_step
    assert proc.returncode == 0, proc.stderr + proc.stdout
    paths = [root / "opt" / "npm-cache", root / "npmrc", *(root / "opt").rglob("*")]
    writable = [p for p in paths if p.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)]
    # MUTATION: delete `chmod -R a-w` and this fails.
    assert not writable, f"left writable: {writable}"
    # Counted, so a glob that matched nothing cannot read as a pass.
    assert len(paths) >= 6, paths


def test_executed_step_writes_the_npmrc_the_design_names(ran_step) -> None:
    root, proc = ran_step
    assert proc.returncode == 0, proc.stderr + proc.stdout
    lines = (root / "npmrc").read_text().splitlines()
    # Every /opt/ path moved under tmp_path, the npmrc's cache line with it;
    # the static test above holds it to /opt/npm-cache exactly.
    # MUTATION: write prefer-online=true instead and this fails.
    assert lines == [f"cache={root}{CACHE}", "prefer-offline=true"], lines
