"""The installed engine finds the release's dependency manifest (lane
DAYONE-WHEEL-MANIFEST, part of #2688).

`uv tool install` of the release wheel puts `swarm_install` in a virtualenv
with no repository above it, so `install/manifest.yaml` three levels up does not
exist and every step that loads the manifest at import fails before stage 0.
The wheel carries the manifest as package data (`swarm_install/_manifest/`) and
`swarm_install.manifest` resolves, in order:

1. `SWARM_INSTALL_MANIFEST`, a manifest.yaml whose sibling `manifest.d/` holds
   its fragments;
2. the packaged copy, through importlib.resources;
3. the repository checkout (development).

No credentials, no network: a temp dir laid out as an installed package.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from swarm_install import manifest as m

REPO = Path(__file__).resolve().parents[3]
APP = REPO / "apps" / "swarm-install"

_BASE = "version: 1\nentries: []\n"


def _fragment(entry_id: str) -> str:
    return (
        "version: 1\nentries:\n"
        f"- id: {entry_id}\n  kind: declared-value\n  scope: core\n  stage: 0\n  provider: engine\n"
    )


def _layout(root: Path, entry_id: str) -> Path:
    """manifest.yaml beside manifest.d/ holding one fragment naming `entry_id`."""
    (root / "manifest.d").mkdir(parents=True)
    (root / "manifest.yaml").write_text(_BASE)
    (root / "manifest.d" / "lane.yaml").write_text(_fragment(entry_id))
    return root / "manifest.yaml"


def _installed_package(site: Path, *, packaged: bool) -> Path:
    """`site/swarm_install/` as the wheel installs it: manifest.py with no
    repository above it, and (if `packaged`) the force-included `_manifest/`."""
    pkg = site / "swarm_install"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    shutil.copy2(APP / "swarm_install" / "manifest.py", pkg / "manifest.py")
    if packaged:
        _layout(pkg / "_manifest", "declared-value.from_the_wheel")
    return pkg


def _import_in(site: Path, env_value: str | None) -> dict:
    """Import `swarm_install.manifest` from `site` in a fresh interpreter and
    report what it resolved and what `load()` merged."""
    env = {k: v for k, v in os.environ.items() if k != m.MANIFEST_ENV}
    env["PYTHONPATH"] = str(site)
    if env_value is not None:
        env[m.MANIFEST_ENV] = env_value
    code = (
        "import json, swarm_install.manifest as m\n"
        "loaded = m.load() if m.MANIFEST_PATH.is_file() else None\n"
        "print(json.dumps({'module': m.__file__, 'path': str(m.MANIFEST_PATH),"
        " 'fragments': str(m.FRAGMENTS_DIR),"
        " 'ids': sorted(e.id for e in loaded.entries) if loaded else None}))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


# -- the resolution order ------------------------------------------------------


def test_env_wins_over_the_packaged_copy_and_the_checkout(tmp_path: Path) -> None:
    override = _layout(tmp_path / "release", "declared-value.from_the_env")
    pkg = tmp_path / "site" / "swarm_install"
    _layout(pkg / "_manifest", "declared-value.from_the_wheel")
    checkout = tmp_path / "checkout"
    _layout(checkout / "install", "declared-value.from_the_checkout")

    path, fragments = m.resolve_paths({m.MANIFEST_ENV: str(override)}, pkg, checkout)

    assert (path, fragments) == (override, override.parent / "manifest.d")
    assert [e.id for e in m.load(path, fragments).entries] == ["declared-value.from_the_env"]


def test_packaged_copy_wins_over_the_checkout(tmp_path: Path) -> None:
    pkg = tmp_path / "site" / "swarm_install"
    _layout(pkg / "_manifest", "declared-value.from_the_wheel")
    checkout = tmp_path / "checkout"
    _layout(checkout / "install", "declared-value.from_the_checkout")

    for environ in ({}, {m.MANIFEST_ENV: "  "}):
        path, fragments = m.resolve_paths(environ, pkg, checkout)
        assert (path, fragments) == (pkg / "_manifest" / "manifest.yaml", pkg / "_manifest" / "manifest.d")
        assert [e.id for e in m.load(path, fragments).entries] == ["declared-value.from_the_wheel"]


def test_checkout_is_the_fallback(tmp_path: Path) -> None:
    pkg = tmp_path / "site" / "swarm_install"
    pkg.mkdir(parents=True)  # an editable install: no _manifest/ beside the code
    checkout = tmp_path / "checkout"
    _layout(checkout / "install", "declared-value.from_the_checkout")

    for package_root in (pkg, None):
        path, fragments = m.resolve_paths({}, package_root, checkout)
        assert (path, fragments) == (checkout / "install" / "manifest.yaml", checkout / "install" / "manifest.d")


def test_this_checkout_still_reads_its_own_install_tree() -> None:
    """The editable install the tests run on has no packaged copy, so the
    module's names are the checkout's, as every caller had them."""
    assert m.MANIFEST_PATH == REPO / "install" / "manifest.yaml"
    assert m.FRAGMENTS_DIR == REPO / "install" / "manifest.d"


def test_installed_package_loads_its_packaged_manifest(tmp_path: Path) -> None:
    site = tmp_path / "venv" / "lib" / "site-packages"
    pkg = _installed_package(site, packaged=True)

    got = _import_in(site, None)

    assert Path(got["module"]).resolve() == (pkg / "manifest.py").resolve()
    assert Path(got["path"]) == pkg / "_manifest" / "manifest.yaml"
    assert Path(got["fragments"]) == pkg / "_manifest" / "manifest.d"
    assert got["ids"] == ["declared-value.from_the_wheel"]


def test_installed_package_honours_the_env_override(tmp_path: Path) -> None:
    site = tmp_path / "venv" / "lib" / "site-packages"
    _installed_package(site, packaged=True)
    override = _layout(tmp_path / "release", "declared-value.from_the_env")

    got = _import_in(site, str(override))

    assert Path(got["path"]) == override
    assert Path(got["fragments"]) == override.parent / "manifest.d"
    assert got["ids"] == ["declared-value.from_the_env"]


def test_installed_package_without_a_copy_names_the_checkout_path(tmp_path: Path) -> None:
    """Nothing packaged and no checkout: the module still imports (no step dies
    at import), and the path it names is the missing checkout one."""
    site = tmp_path / "venv" / "lib" / "site-packages"
    _installed_package(site, packaged=False)

    got = _import_in(site, None)

    assert Path(got["path"]).name == "manifest.yaml"
    assert Path(got["path"]).parent.name == "install"
    assert got["ids"] is None


# -- the wheel's package data --------------------------------------------------


def _force_include() -> dict[str, str]:
    pyproject = tomllib.loads((APP / "pyproject.toml").read_text(encoding="utf-8"))
    return pyproject["tool"]["hatch"]["build"]["targets"]["wheel"].get("force-include", {})


def test_pyproject_packages_the_manifest_and_the_whole_fragment_directory() -> None:
    mapping = _force_include()
    by_target = {target: source for source, target in mapping.items()}

    base = by_target.get(f"swarm_install/{m.PACKAGED_DIR}/manifest.yaml")
    fragments = by_target.get(f"swarm_install/{m.PACKAGED_DIR}/manifest.d")

    assert base is not None, f"the wheel does not carry install/manifest.yaml: {mapping}"
    assert fragments is not None, f"the wheel does not carry install/manifest.d/: {mapping}"
    assert (APP / base).resolve() == REPO / "install" / "manifest.yaml"
    # The directory itself, not a list of files, so a later lane's fragment
    # ships with no edit to pyproject.toml.
    assert (APP / fragments).resolve() == REPO / "install" / "manifest.d"
    assert (APP / fragments).resolve().is_dir()
