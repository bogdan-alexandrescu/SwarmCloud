"""The bundle recipes, the bundler image and the .swarm-bundle.json writer.

docs/design/managed-plugins.md, section 1.4, lane 14 of table 17.1. The
owner, 6 November 2026 (Q13): gstack and security-audit migrate to the managed
path through a build step that publishes a new image version with them
bundled; 9 November 2026: that bundle build runs in the deployment, at any
upstream pin an admin applied after the diff. So the agent-runtime-base
stages' steps became recipes the bundler image also runs, and every image that
carries a bundled plugin carries its .swarm-bundle.json. This file holds:

  * images/agent-runtime-base/bundled-plugins.json, the one place a release's
    default pins are written, passes the writer's checks, and the checks
    refuse every pin that could leave the mirror or reach a shell;
  * the tree and recipe digests are the format the module docstring of
    images/plugin-bundler/swarm-bundle defines (the worker compares them);
  * the security-audit recipe, at the default pins, stages a tree whose digest
    equals the tree the stage staged before the recipe existed, and writes a
    manifest naming that digest;
  * the bundler image carries every input the recipes name.

No cloud, no emulator, no Docker: the recipes run on a temporary folder.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
BUNDLER = REPO / "images" / "plugin-bundler"
WRITER = BUNDLER / "swarm-bundle"
RECIPES = BUNDLER / "recipes"
PINS = REPO / "images" / "agent-runtime-base" / "bundled-plugins.json"
BASE_DOCKERFILE = REPO / "images" / "agent-runtime-base" / "Dockerfile"
BUNDLER_DOCKERFILE = BUNDLER / "Dockerfile"
MIRROR_ROWS = REPO / "images" / "agent-runtime-base" / "tool-mirror.sha256"
STAGED_MIRROR = REPO / "build" / "tool-mirror"


def _load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


bundle = _load("swarm_bundle", WRITER)


def _default_pins() -> dict:
    return json.loads(PINS.read_text())


def _write_pins(tmp_path: Path, doc: dict) -> Path:
    path = tmp_path / "pins.json"
    path.write_text(json.dumps(doc))
    return path


# --------------------------------------------------------------------------- #
# The release's default pins.
# --------------------------------------------------------------------------- #


def test_the_default_pins_pass_the_checks_and_name_both_bundled_plugins() -> None:
    pins = bundle.load_pins(PINS)
    assert sorted(pins) == ["gstack", "security-audit"]
    # Every plugin it names has a recipe, and every recipe a pin.
    assert sorted(p.name for p in RECIPES.iterdir() if p.is_dir()) == sorted(pins)


def test_every_default_pin_is_a_row_of_the_tool_mirror() -> None:
    rows = {}
    for line in MIRROR_ROWS.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            sha, obj = line.split(None, 1)
            rows[obj.strip()] = sha
    for name, pins in bundle.load_pins(PINS).items():
        objects = [pins["source"], *pins.get("toolchain", {}).values()]
        for pinned in objects:
            # MUTATION: bump a pin in bundled-plugins.json and not the mirror.
            assert rows.get(pinned["object"]) == pinned["sha256"], (name, pinned["object"])


def test_the_default_pins_are_the_catalogues_versions() -> None:
    from agent_worker import plugins
    from swarm_api import plugincatalogue

    for name, pins in bundle.load_pins(PINS).items():
        for catalogue in (plugincatalogue.CATALOGUE, plugins.CATALOGUE):
            entry = catalogue[name]
            assert (entry.version, entry.install_path) == (pins["version"], pins["install_path"]), name


# --------------------------------------------------------------------------- #
# The pins checks: nothing that leaves the mirror or reaches a shell.
# --------------------------------------------------------------------------- #


def _mutated(path: str, value) -> dict:
    doc = _default_pins()
    target = doc
    keys = path.split(".")
    for key in keys[:-1]:
        target = target[key]
    if value is KeyError:
        del target[keys[-1]]
    else:
        target[keys[-1]] = value
    return doc


@pytest.mark.parametrize(
    "path,value,why",
    [
        ("schema", 2, "schema"),
        ("plugins.gstack.source.sha256", "63C118F13CCF0D72D06D7BFF56231DC08CE3DA29CE405ED567224DD303CF5BD2",
         "sha256"),
        ("plugins.gstack.source.sha256", "63c118f1", "sha256"),
        ("plugins.gstack.source.commit", "2512dd7", "commit"),
        ("plugins.gstack.source.object", "github.com/garrytan/../../etc/passwd", "leaves the mirror"),
        ("plugins.gstack.source.object", "/etc/passwd", "mirror object"),
        ("plugins.gstack.source.object", "github.com/a/$(id).tar.gz", "mirror object"),
        ("plugins.gstack.source.object", "github.com/a/b c.tar.gz", "mirror object"),
        ("plugins.gstack.version", "1.0;rm -rf /", "version"),
        ("plugins.gstack.version", "", "version"),
        ("plugins.gstack.install_id", "plugin-1234", "builtin:gstack"),
        ("plugins.gstack.install_path", "/workspace/gstack", "/opt/swarm/plugins/gstack"),
        ("plugins.gstack.toolchain.bun.sha256", "x" * 64, "sha256"),
        ("plugins.gstack.toolchain.bun.object", "../bun.zip", "mirror object"),
        ("plugins.gstack.image", "gcr.io/x/y", "unknown keys"),
        ("plugins.security-audit.source", KeyError, "lacks"),
        ("plugins", {}, "names no plugin"),
    ],
)
def test_a_pin_the_writer_must_not_build_from_is_refused(tmp_path: Path, path: str, value, why: str) -> None:
    pins = _write_pins(tmp_path, _mutated(path, value))
    with pytest.raises(bundle.PinsError, match=re.escape(why)):
        bundle.load_pins(pins)


def test_pin_prints_one_value_and_refuses_a_file_wrong_anywhere(tmp_path: Path, capsys) -> None:
    assert bundle.main(["pin", "--pins", str(PINS), "--plugin", "gstack", "toolchain.bun.version"]) == 0
    assert capsys.readouterr().out == "1.4.2\n"
    # A bad security-audit pin refuses a gstack read too: a recipe never reads
    # a value from a file that is wrong anywhere.
    bad = _write_pins(tmp_path, _mutated("plugins.security-audit.source.commit", "nope"))
    assert bundle.main(["pin", "--pins", str(bad), "--plugin", "gstack", "version"]) == 2
    assert "plugins.security-audit.source.commit" in capsys.readouterr().err
    for key in ("source", "nothing.here", "toolchain.bun.missing"):
        assert bundle.main(["pin", "--pins", str(PINS), "--plugin", "gstack", key]) == 2
    assert bundle.main(["pin", "--pins", str(PINS), "--plugin", "jev-belay", "version"]) == 2


# --------------------------------------------------------------------------- #
# The digests.
# --------------------------------------------------------------------------- #


def _tree(root: Path, files: dict[str, tuple[bytes, int]]) -> Path:
    root.mkdir(parents=True)
    os.chmod(root, 0o755)
    for rel, (content, mode) in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        os.chmod(path, mode)
    for folder in sorted({p for p in root.rglob("*") if p.is_dir()}):
        os.chmod(folder, 0o755)
    return root


def test_the_tree_digest_is_the_documented_format(tmp_path: Path) -> None:
    root = _tree(tmp_path / "t", {"a/run": (b"#!/bin/sh\n", 0o755), "b.md": (b"skill\n", 0o644)})
    # Built by hand from the docstring's lines: the worker (lane 19) will
    # compute the same string, so a change here is a format change.
    run_sha = hashlib.sha256(b"#!/bin/sh\n").hexdigest()
    md_sha = hashlib.sha256(b"skill\n").hexdigest()
    want = hashlib.sha256(
        f"d 0755 a/\nf 0755 {run_sha} a/run\nf 0644 {md_sha} b.md\n".encode()).hexdigest()
    assert bundle.tree_digest([root]) == (want, 2, 1)


def test_the_tree_digest_leaves_out_only_the_roots_manifest(tmp_path: Path) -> None:
    root = _tree(tmp_path / "t", {"skills/x/SKILL.md": (b"---\n", 0o644)})
    before = bundle.tree_digest([root])
    (root / ".swarm-bundle.json").write_text("{}")
    assert bundle.tree_digest([root]) == before
    # A manifest-named file below the root is content like any other.
    (root / "skills" / ".swarm-bundle.json").write_text("{}")
    assert bundle.tree_digest([root]) != before


def test_the_tree_digest_changes_with_content_mode_and_names(tmp_path: Path) -> None:
    files = {"lib/a.ts": (b"one", 0o644), "bin/b": (b"two", 0o755)}
    base = bundle.tree_digest([_tree(tmp_path / "base", files)])[0]
    for i, change in enumerate([
        {"lib/a.ts": (b"ONE", 0o644)},
        {"lib/a.ts": (b"one", 0o755)},
        {"lib/c.ts": (b"one", 0o644), "lib/a.ts": None},
    ]):
        mutated = {k: v for k, v in {**files, **change}.items() if v is not None}
        assert bundle.tree_digest([_tree(tmp_path / f"m{i}", mutated)])[0] != base, change
    # The same files written in another order digest the same.
    again = dict(reversed(list(files.items())))
    assert bundle.tree_digest([_tree(tmp_path / "again", again)])[0] == base


def test_a_tree_laid_over_another_digests_as_their_union(tmp_path: Path) -> None:
    base = {"SKILL.md": (b"base", 0o644), "lib/x.ts": (b"x", 0o644)}
    browser = {"browse/dist/browse": (b"bin", 0o755), "lib/y.ts": (b"y", 0o644)}
    laid = bundle.tree_digest([_tree(tmp_path / "base", base), _tree(tmp_path / "browser", browser)])
    assert laid == bundle.tree_digest([_tree(tmp_path / "union", {**base, **browser})])
    # The lists are disjoint; a file in both trees is refused, not overwritten.
    _tree(tmp_path / "clash", {"lib/x.ts": (b"other", 0o644)})
    with pytest.raises(SystemExit, match="more than one tree"):
        bundle.tree_digest([tmp_path / "base", tmp_path / "clash"])


def test_a_symlink_or_fifo_in_the_tree_is_refused(tmp_path: Path) -> None:
    root = _tree(tmp_path / "t", {"a": (b"a", 0o644)})
    (root / "link").symlink_to("/etc/passwd")
    with pytest.raises(SystemExit, match="not a regular file"):
        bundle.tree_digest([root])
    root2 = _tree(tmp_path / "t2", {"a": (b"a", 0o644)})
    os.mkfifo(root2 / "pipe")
    with pytest.raises(SystemExit, match="not a regular file"):
        bundle.tree_digest([root2])


def test_the_recipe_digest_is_by_input_name_and_content_not_path(tmp_path: Path) -> None:
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    for folder in ("one", "two"):
        (tmp_path / folder / "build").write_text("#!/usr/bin/env bash\n")
        (tmp_path / folder / "list.txt").write_text("a\n")
    one = bundle.recipe_digest({"build": tmp_path / "one" / "build", "base-list": tmp_path / "one" / "list.txt"})
    two = bundle.recipe_digest({"base-list": tmp_path / "two" / "list.txt", "build": tmp_path / "two" / "build"})
    assert one == two
    lines = "".join(f"{one[1][n]}  {n}\n" for n in sorted(one[1]))
    assert one[0] == hashlib.sha256(lines.encode()).hexdigest()
    (tmp_path / "two" / "list.txt").write_text("a\nb\n")
    assert bundle.recipe_digest({"build": tmp_path / "two" / "build",
                                 "base-list": tmp_path / "two" / "list.txt"})[0] != one[0]
    assert bundle.recipe_digest({"build": tmp_path / "one" / "build",
                                 "browser-list": tmp_path / "one" / "list.txt"})[0] != one[0]


def test_write_puts_the_designs_fields_in_a_read_only_manifest(tmp_path: Path) -> None:
    root = _tree(tmp_path / "out", {"skills/security-audit/SKILL.md": (b"---\n", 0o644)})
    recipe = tmp_path / "build"
    recipe.write_text("#!/usr/bin/env bash\n")
    out = root / ".swarm-bundle.json"
    assert bundle.main(["write", "--pins", str(PINS), "--plugin", "security-audit", "--tree", str(root),
                        "--input", f"build={recipe}", "--out", str(out)]) == 0
    doc = json.loads(out.read_text())
    pins = _default_pins()["plugins"]["security-audit"]
    # Section 1.4.2: the install id, the source commit and sha256, the recipe
    # digest and the sha256 of the built tree.
    assert doc["install_id"] == "builtin:security-audit"
    assert doc["source"] == pins["source"]
    assert doc["version"] == pins["version"]
    assert doc["recipe"]["digest"] == bundle.recipe_digest({"build": recipe})[0]
    assert doc["tree"] == {"sha256": bundle.tree_digest([root])[0], "files": 1, "folders": 2}
    assert "toolchain" not in doc
    assert oct(out.stat().st_mode & 0o777) == "0o644"
    # The manifest does not change the digest of the tree it sits in.
    assert bundle.tree_digest([root])[0] == doc["tree"]["sha256"]


@pytest.mark.parametrize("pair", ["build", "=x", "Build=x", "build=/nonexistent/file"])
def test_write_refuses_an_input_that_is_not_a_named_file(tmp_path: Path, pair: str) -> None:
    root = _tree(tmp_path / "out", {"a": (b"a", 0o644)})
    with pytest.raises(SystemExit):
        bundle.main(["write", "--pins", str(PINS), "--plugin", "security-audit", "--tree", str(root),
                     "--input", pair, "--out", str(root / ".swarm-bundle.json")])


# --------------------------------------------------------------------------- #
# The recipes, run.
# --------------------------------------------------------------------------- #


def _require(tool: str) -> str:
    found = shutil.which(tool)
    if found is None:
        if os.environ.get("CI"):
            pytest.fail(f"{tool} is not on PATH in CI, so the recipe cannot run; it must not pass by skipping")
        pytest.skip(f"{tool} is not installed here; CI runs this")
    return found


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(kw.pop("home")), "TMPDIR": str(kw.pop("tmp"))}
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False, env=env, **kw)


def test_the_security_audit_recipe_refuses_bad_pins_before_it_fetches(tmp_path: Path) -> None:
    _require("curl")
    bad = _write_pins(tmp_path, _mutated("plugins.security-audit.source.object", "github.com/a/../../x"))
    proc = _run([str(RECIPES / "security-audit" / "build"), "--pins", str(bad), "--mirror", str(tmp_path),
                 "--out", str(tmp_path / "out")], home=tmp_path, tmp=tmp_path)
    assert proc.returncode != 0
    assert "leaves the mirror" in proc.stderr
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("recipe", ["gstack", "security-audit"])
def test_a_recipe_refuses_an_existing_out_or_an_unknown_argument(tmp_path: Path, recipe: str) -> None:
    build = str(RECIPES / recipe / "build")
    proc = _run([build, "--nope", "x"], home=tmp_path, tmp=tmp_path)
    assert proc.returncode == 2 and "unknown argument" in proc.stderr
    (tmp_path / "out").mkdir()
    args = ["--pins", str(PINS), "--mirror", str(tmp_path), "--out", str(tmp_path / "out")]
    if recipe == "gstack":
        args += ["--layers", "x", "--measure", "x", "--base-list", "x", "--browser-list", "x",
                 "--list", "base", "--bun", "present"]
    proc = _run([build, *args], home=tmp_path, tmp=tmp_path)
    # MUTATION: stage into a folder that already holds files; they would ship.
    assert proc.returncode == 2 and "exists" in proc.stderr


def test_the_gstack_recipe_refuses_a_list_or_bun_mode_it_does_not_know(tmp_path: Path) -> None:
    base = [str(RECIPES / "gstack" / "build"), "--pins", str(PINS), "--mirror", str(tmp_path),
            "--layers", "x", "--measure", "x", "--base-list", "x", "--browser-list", "x",
            "--out", str(tmp_path / "out")]
    for extra, why in ((["--list", "all", "--bun", "install"], "--list"),
                       (["--list", "base", "--bun", "fetch"], "--bun"),
                       (["--list", "base"], "--bun is required")):
        proc = _run(base + extra, home=tmp_path, tmp=tmp_path)
        assert proc.returncode == 2 and why in proc.stderr, (extra, proc.stderr)


def _security_audit_archive() -> tuple[str, bytes]:
    """The pinned archive (the default pins' object): build/tool-mirror's copy
    when a build staged one, else the upstream URL the mirror is filled from;
    checked against the pinned sha256 either way."""
    source = _default_pins()["plugins"]["security-audit"]["source"]
    staged = STAGED_MIRROR / source["object"]
    if staged.is_file():
        data = staged.read_bytes()
    else:
        last: Exception | None = None
        data = b""
        for attempt in range(4):
            try:
                with urllib.request.urlopen(f"https://{source['object']}", timeout=60) as response:
                    data = response.read()
                break
            except OSError as exc:
                last = exc
                time.sleep(2 * (attempt + 1))
        else:
            pytest.fail(f"https://{source['object']} could not be read ({last}); main's build fills the "
                        "mirror from the same URL, so it would fail too")
    assert hashlib.sha256(data).hexdigest() == source["sha256"]
    return source["object"], data


def _staged_before_the_recipe(data: bytes, out: Path) -> Path:
    """What the security-audit-build stage staged before the recipe existed:
    skills/security-audit/ and LICENSE at the archive's relative paths,
    directories 0755, files 0644 (the stage's own commands, in Python)."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        prefix = members[0].name.split("/")[0] + "/"
        wanted = [m for m in members
                  if m.name == prefix + "LICENSE" or m.name.startswith(prefix + "skills/security-audit/")
                  or m.name == prefix + "skills/security-audit"]
        for member in wanted:
            member.name = member.name[len(prefix):]
        archive.extractall(out, members=wanted, filter="data")
    (out / "skills").mkdir(exist_ok=True)
    for path in [out, *out.rglob("*")]:
        os.chmod(path, 0o755 if path.is_dir() else 0o644)
    return out


def test_the_security_audit_recipe_stages_todays_tree_at_the_default_pins(tmp_path: Path) -> None:
    _require("curl")
    _require("git")
    obj, data = _security_audit_archive()
    mirror = tmp_path / "mirror"
    (mirror / obj).parent.mkdir(parents=True)
    (mirror / obj).write_bytes(data)
    out = tmp_path / "out"
    cmd = [str(RECIPES / "security-audit" / "build"), "--pins", str(PINS), "--mirror", str(mirror),
           "--out", str(out)]
    node = shutil.which("node")
    if node is not None:
        cmd += ["--validate-with", node]
    proc = _run(cmd, home=tmp_path, tmp=tmp_path)
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-4000:]

    before = _staged_before_the_recipe(data, tmp_path / "before")
    # MUTATION: stage README.md too, or drop a chmod: the trees differ.
    assert bundle.tree_digest([out]) == bundle.tree_digest([before])
    doc = json.loads((out / ".swarm-bundle.json").read_text())
    assert doc["tree"]["sha256"] == bundle.tree_digest([before])[0]
    assert doc["recipe"]["digest"] == bundle.recipe_digest({"build": RECIPES / "security-audit" / "build"})[0]
    assert doc["source"] == _default_pins()["plugins"]["security-audit"]["source"]
    # The validators' scratch copy stayed out of the tree.
    assert sorted(p.name for p in out.iterdir()) == [".swarm-bundle.json", "LICENSE", "skills"]


def test_the_security_audit_recipe_refuses_an_archive_of_another_sha(tmp_path: Path) -> None:
    _require("curl")
    obj = _default_pins()["plugins"]["security-audit"]["source"]["object"]
    mirror = tmp_path / "mirror"
    (mirror / obj).parent.mkdir(parents=True)
    (mirror / obj).write_bytes(b"not the archive")
    proc = _run([str(RECIPES / "security-audit" / "build"), "--pins", str(PINS), "--mirror", str(mirror),
                 "--out", str(tmp_path / "out")], home=tmp_path, tmp=tmp_path)
    assert proc.returncode != 0
    assert not (tmp_path / "out").exists()


# --------------------------------------------------------------------------- #
# The scripts and the bundler image.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", [WRITER, RECIPES / "gstack" / "build", RECIPES / "security-audit" / "build"])
def test_the_bundlers_scripts_are_executable(path: Path) -> None:
    assert os.access(path, os.X_OK), path


@pytest.mark.parametrize("recipe", ["gstack", "security-audit"])
def test_each_recipe_starts_with_strict_mode_and_is_shellcheck_clean(recipe: str) -> None:
    path = RECIPES / recipe / "build"
    lines = [line for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")]
    assert lines[0] == "set -euo pipefail"
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        if os.environ.get("CI"):
            pytest.fail("shellcheck is not on PATH in CI")
        pytest.skip("shellcheck is not installed here; CI runs this")
    proc = subprocess.run([shellcheck, "-x", str(path)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout


def _arg(path: Path, name: str) -> str:
    m = re.search(rf"^ARG {name}=(\S+)$", path.read_text(), re.M)
    assert m, (path, name)
    return m.group(1)


def test_the_bundler_builds_on_the_release_images_python_and_node() -> None:
    for name in ("PYTHON_IMAGE", "NODE_IMAGE"):
        # MUTATION: move the base's digest and not the bundler's.
        assert _arg(BUNDLER_DOCKERFILE, name) == _arg(BASE_DOCKERFILE, name), name
    froms = re.findall(r"^FROM (\S+)", BUNDLER_DOCKERFILE.read_text(), re.M)
    assert froms and all(f.startswith("${MIRROR_DOCKER_HUB}/library/") for f in froms), froms


def test_the_bundler_image_carries_every_input_the_recipes_name() -> None:
    text = BUNDLER_DOCKERFILE.read_text()
    copies = dict(re.findall(r"^COPY (?!--from)(\S+) (\S+)$", text, re.M))
    for source in ("images/plugin-bundler/swarm-bundle", "images/plugin-bundler/recipes/",
                   "images/agent-runtime-base/gstack-layers", "images/agent-runtime-base/gstack-base-files.txt",
                   "images/agent-runtime-browser/gstack-browser-files.txt",
                   "scripts/gstack_packaging_measure.py", "images/agent-runtime-base/bundled-plugins.json"):
        assert source in copies, source
        assert (REPO / source).exists(), source
    # The recipes find the writer two folders up, as in the release stages'
    # bind mount of images/plugin-bundler.
    assert copies["images/plugin-bundler/swarm-bundle"] == "/opt/swarm-bundler/swarm-bundle"
    assert copies["images/plugin-bundler/recipes/"] == "/opt/swarm-bundler/recipes/"
    assert "COPY --from=nodebin /usr/local/bin/node /usr/local/bin/node" in text
    # The scripts the bundler copies must reach the build context.
    ignored = (REPO / ".dockerignore").read_text().splitlines()
    assert "!scripts/gstack_packaging_measure.py" in ignored
