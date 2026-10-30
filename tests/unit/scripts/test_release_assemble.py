"""scripts/release/assemble.sh: the release bundle's shape (DAYONE-RELEASE-1).

The bundle is what every company's install reads from the release home, with
no checkout of this repository (design §6.1, plan §7.2 step 4): the Terraform
module, the wheels, the merged manifest, the image digests, the Kubernetes
trees, the plugin and its marketplace, the accepted CVEs and SHA256SUMS. These
tests assemble one bundle from this checkout, offline (the wheels are built by
`uv build` from local sources; the image digests come from a fake build
record), and read it the way the engine does.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
ASSEMBLE = REPO / "scripts" / "release" / "assemble.sh"
VERSION = "2026.10.1"
DIGEST = re.compile(r"\Asha256:[0-9a-f]{64}\Z")


def image_names() -> list[str]:
    """What terraform/infra pins by digest (verify.tf refuses a missing one),
    plus two worker images, which only the scheduler names."""
    pinned = json.loads((REPO / "terraform" / "infra" / "images.json").read_text())
    return [*pinned["platform"], *pinned["gated"], "agent-runtime-base", "workspace-apply"]


def fake_digest(name: str) -> str:
    return "sha256:" + hashlib.sha256(f"fake image {name}".encode()).hexdigest()


def build_record(path: Path, names: list[str] | None = None, **override: dict) -> Path:
    """A build record in scripts/build-images.sh's shape (build/images-<env>.json)."""
    images = []
    for name in names if names is not None else image_names():
        image = f"us-central1-docker.pkg.dev/ci-project/swarm/{name}"
        entry = {"name": name, "image": image, "tag": "abc123", "digest": fake_digest(name),
                 "ref": f"{image}@{fake_digest(name)}"}
        entry.update(override.get(name, {}))
        images.append(entry)
    path.write_text(json.dumps({"tag": "abc123", "commit": "0" * 40, "environment": "dev", "images": images}))
    return path


def assemble(tmp: Path, record: Path, version: str = VERSION) -> subprocess.CompletedProcess:
    # A refusal test must not pass because there is no script to refuse.
    assert ASSEMBLE.is_file(), f"{ASSEMBLE.relative_to(REPO)} does not exist"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLOUDSDK_", "GOOGLE_"))}
    env.update(NO_COLOR="1", SWARM_ENV_FILE=str(tmp / "absent.env"))
    return subprocess.run(
        ["bash", str(ASSEMBLE), "--version", version, "--images", str(record), "--out", str(tmp / "out")],
        env=env, capture_output=True, text=True, timeout=600, check=False,
    )


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    tmp = tmp_path_factory.mktemp("assemble")
    proc = assemble(tmp, build_record(tmp / "record.json"))
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    return tmp / "out"


def tar_names(path: Path) -> list[str]:
    with tarfile.open(path, "r:gz") as tar:
        return [m.name for m in tar.getmembers()]


# -- the Terraform module (design §5.1) -----------------------------------------


def test_module_has_no_tfvars_or_environments(bundle: Path) -> None:
    """`module.tgz` is terraform/{bootstrap,infra,modules} and nothing that
    configures one deployment: the engine writes its own tfvars (§5.1)."""
    names = tar_names(bundle / "module.tgz")
    files = [n for n in names if not n.endswith("/")]
    for root in ("terraform/bootstrap/", "terraform/infra/", "terraform/modules/"):
        assert any(n.startswith(root) for n in files), f"module.tgz has nothing under {root}"
    # MUTATION: tar the whole terraform/ tree and these fail.
    assert not [n for n in names if "environments" in n.split("/")], "module.tgz carries environments/"
    assert not [n for n in names if n.endswith((".tfvars", ".tfvars.json"))], "module.tgz carries a tfvars file"
    assert not [n for n in names if ".terraform" in n.split("/") or "tfstate" in n]
    outside = [n for n in names if not n.startswith(("terraform/bootstrap", "terraform/infra", "terraform/modules"))]
    assert not outside, outside[:10]
    # The live deployment's bootstrap tfvars exists in the checkout; it is the
    # file this test is about.
    assert (REPO / "terraform" / "bootstrap" / "terraform.tfvars").is_file()


# -- the wheels (plan R6) -----------------------------------------------------------


def _metadata(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".dist-info/METADATA"))
        return zf.read(name).decode()


def _requires(wheel: Path) -> list[str]:
    return [line.split(":", 1)[1].strip() for line in _metadata(wheel).splitlines()
            if line.startswith("Requires-Dist:")]


def test_two_wheels(bundle: Path) -> None:
    """Two tool wheels at the release's version: the bridge (`swarm_mcp`: `sc`,
    `swarm`, the plugin's MCP server) and the engine (`swarm_install`). The
    bridge never pulls in the Google clients the engine needs (R6). The only
    other wheels are the two in-repo libraries they require, which are not on
    PyPI and so must come from the release itself, never from an index."""
    wheels = sorted(p.name for p in bundle.glob("*.whl"))
    tools = [w for w in wheels if w.startswith(("swarm_mcp-", "swarm_install-"))]
    assert tools == [f"swarm_install-{VERSION}-py3-none-any.whl", f"swarm_mcp-{VERSION}-py3-none-any.whl"], wheels
    libraries = sorted(set(wheels) - set(tools))
    assert libraries == [f"swarm_common-{VERSION}-py3-none-any.whl",
                         f"swarm_redaction-{VERSION}-py3-none-any.whl"], wheels

    bridge = _requires(bundle / f"swarm_mcp-{VERSION}-py3-none-any.whl")
    assert not [r for r in bridge if r.lower().startswith(("google", "swarm-install", "swarm_install"))], bridge
    engine = _requires(bundle / f"swarm_install-{VERSION}-py3-none-any.whl")
    assert any(r.startswith("google-cloud-") for r in engine), engine
    # Every in-repo requirement of a tool wheel is a wheel in this release.
    shipped = {w.split("-")[0].replace("_", "-") for w in wheels}
    for req in bridge + engine:
        name = re.split(r"[ <>=;\[]", req, maxsplit=1)[0].lower().replace("_", "-")
        if name.startswith("swarm-"):
            assert name in shipped, f"{req} is required but not in the release"
    with zipfile.ZipFile(bundle / f"swarm_install-{VERSION}-py3-none-any.whl") as zf:
        assert "swarm_install/engine.py" in zf.namelist()
        # The installed engine loads the release's manifest from the wheel.
        assert "swarm_install/_manifest/manifest.yaml" in zf.namelist()
        assert [n for n in zf.namelist() if n.startswith("swarm_install/_manifest/manifest.d/")]
    with zipfile.ZipFile(bundle / f"swarm_mcp-{VERSION}-py3-none-any.whl") as zf:
        assert "swarm_mcp/cli.py" in zf.namelist()
        assert not [n for n in zf.namelist() if n.startswith("swarm_install/")]
    assert f"Version: {VERSION}" in _metadata(bundle / f"swarm_mcp-{VERSION}-py3-none-any.whl")


# -- SHA256SUMS ------------------------------------------------------------------------


def test_sha256sums_cover_every_file(bundle: Path) -> None:
    """Every file the release ships is in SHA256SUMS with its true digest, and
    SHA256SUMS names nothing that is not there: the engine checks each file it
    reads against it (preflight.release, cluster.bundle)."""
    from swarm_install.steps.preflight import parse_sums

    sums = parse_sums((bundle / "SHA256SUMS").read_bytes())
    files = sorted(p.relative_to(bundle).as_posix() for p in bundle.rglob("*")
                   if p.is_file() and p.name != "SHA256SUMS")
    assert sorted(sums) == files
    for name in files:
        assert sums[name] == hashlib.sha256((bundle / name).read_bytes()).hexdigest(), name
    for must in ("module.tgz", "manifest.yaml", "images.json", "kubernetes.tgz", "compat.json",
                 "marketplace.json", "cve-acceptances.json", "RELEASE.md", "install.sh",
                 "plugin/.claude-plugin/plugin.json", "plugin/compat.json"):
        assert must in sums, must


# -- images.json -------------------------------------------------------------------------


def test_images_json_digests_only(bundle: Path, tmp_path: Path) -> None:
    """`images.json` maps each image to a digest and nothing else: no tag, no
    registry, no CI project (H28: never trust a tag)."""
    images = json.loads((bundle / "images.json").read_text())
    assert sorted(images) == sorted(image_names())
    assert all(DIGEST.match(v) for v in images.values()), images
    assert images == {n: fake_digest(n) for n in image_names()}
    assert "ci-project" not in (bundle / "images.json").read_text()

    # A record whose entry has only a tag is refused, and so is one that
    # leaves out an image terraform pins.
    tagged = build_record(tmp_path / "tagged.json", **{"swarm-api": {"digest": "latest", "ref": "x/swarm-api:latest"}})
    proc = assemble(tmp_path / "a", tagged)
    assert proc.returncode != 0 and "swarm-api" in proc.stderr, proc.stderr[-2000:]
    short = build_record(tmp_path / "short.json", [n for n in image_names() if n != "swarm-ui"])
    proc = assemble(tmp_path / "b", short)
    assert proc.returncode != 0 and "swarm-ui" in proc.stderr, proc.stderr[-2000:]


# -- kubernetes.tgz (review finding 3) -------------------------------------------------------


def test_bundle_carries_kubernetes(bundle: Path, tmp_path: Path) -> None:
    """`kubernetes.tgz` holds render.py, policies/, swarm-system/, rbac/ and the
    tenant templates, and render.py runs from it with no checkout: stage 10,
    team add, upgrade and uninstall render from the release, never a clone."""
    from swarm_install.steps.cluster import unpack

    root = unpack((bundle / "kubernetes.tgz").read_bytes(), tmp_path / "kubernetes")
    for tree in ("policies", "swarm-system", "rbac", "namespaces", "service-accounts",
                 "network-policies", "worker-templates", "storage"):
        src = sorted(p.name for p in (REPO / "kubernetes" / tree).iterdir() if p.suffix == ".yaml")
        got = sorted(p.name for p in (root / tree).iterdir()) if (root / tree).is_dir() else []
        assert src and set(src) <= set(got), (tree, src, got)

    # A bare interpreter, no site-packages (-S) and no PYTHON* variables (-E):
    # every module render.py imports must come from the bundle itself.
    for argv in (["policies"], ["tenant", "--tenant", "lint"]):
        ran = subprocess.run(
            [sys.executable, "-S", "-E", str(root / "render.py"), *argv],
            cwd=tmp_path, capture_output=True, text=True, timeout=120, check=False,
        )
        assert ran.returncode == 0, (argv, ran.stderr[-2000:])
        assert "kind:" in ran.stdout, argv
    # MUTATION: leave scheduler/ out of the bundle and the render above fails.
    for package in ("scheduler", "quota_broker", "swarm_common"):
        assert (root / package).is_dir(), package


# -- the manifest and the accepted CVEs ----------------------------------------------------------


def test_manifest_is_the_merged_manifest(bundle: Path, tmp_path: Path) -> None:
    """`manifest.yaml` is the base merged with every fragment, loadable on its
    own: an installer has no install/manifest.d/ beside it."""
    from swarm_install.manifest import load

    merged = load(bundle / "manifest.yaml", tmp_path / "no-fragments")
    here = load()
    assert sorted(e.id for e in merged.entries) == sorted(e.id for e in here.entries)
    assert set(merged.env) == set(here.env)


def test_cve_acceptances_carry_each_expiry(bundle: Path) -> None:
    """Every accepted CVE of `.trivyignore.yaml` is in the release with its
    expiry, in the shape preflight.release reads (lesson 27), and in RELEASE.md."""
    import yaml

    from swarm_install.steps.preflight import parse_acceptances

    accepted = parse_acceptances((bundle / "cve-acceptances.json").read_bytes())
    source = yaml.safe_load((REPO / ".trivyignore.yaml").read_text())["vulnerabilities"]
    assert {a.cve for a in accepted} == {str(v["id"]) for v in source}
    assert {(a.cve, a.expires.isoformat()) for a in accepted} == {(str(v["id"]), str(v["expired_at"])) for v in source}
    notes = (bundle / "RELEASE.md").read_text()
    for a in accepted:
        assert a.cve in notes and a.expires.isoformat() in notes, a.cve


def test_refuses_a_version_that_is_not_a_release_name(tmp_path: Path) -> None:
    proc = assemble(tmp_path, build_record(tmp_path / "r.json"), version="latest")
    assert proc.returncode == 2, proc.stderr[-2000:]
    assert not (tmp_path / "out").exists()


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck_clean() -> None:
    proc = subprocess.run(["shellcheck", "-x", str(ASSEMBLE)], cwd=REPO / "scripts",
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_tar_members_are_reproducible(bundle: Path) -> None:
    """Owners, times and modes are normalised, so the same commit assembles
    the same module.tgz digest wherever it runs."""
    with tarfile.open(bundle / "module.tgz", "r:gz") as tar:
        members = tar.getmembers()
    assert {(m.uid, m.gid, m.uname, m.gname, m.mtime) for m in members} == {(0, 0, "", "", 0)}
    data = (bundle / "module.tgz").read_bytes()
    assert io.BytesIO(data).read(4)[:2] == b"\x1f\x8b"


# -- install.sh (design 6.3) --------------------------------------------------------------------------

FAKE_CURL = r'''#!{python}
"""A fake curl: serves the release home from a bundle directory."""
import os, shutil, sys

args = sys.argv[1:]
out = args[args.index("-o") + 1] if "-o" in args else None
url = args[-1]
home = "https://storage.googleapis.com/swarmcloud/"
bundle = os.environ["FAKE_BUNDLE"]
version = os.environ["FAKE_VERSION"]
if url == home + "channels/stable":
    data = (version + "\n").encode()
elif url.startswith(home + f"releases/{version}/") and os.path.isfile(os.path.join(bundle, url.rsplit("/", 1)[1])):
    data = open(os.path.join(bundle, url.rsplit("/", 1)[1]), "rb").read()
else:
    sys.exit(22)
if out:
    open(out, "wb").write(data)
else:
    sys.stdout.buffer.write(data)
'''

FAKE_UV = r'''#!{python}
import json, os, sys
with open(os.environ["FAKE_UV_LOG"], "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")
'''


def _install(tmp_path: Path, bundle: Path) -> tuple[subprocess.CompletedProcess, list[list[str]]]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, text in (("curl", FAKE_CURL), ("uv", FAKE_UV)):
        (bindir / name).write_text(text.replace("{python}", sys.executable))
        (bindir / name).chmod(0o755)
    log = tmp_path / "uv.log"
    log.write_text("")
    env = {"PATH": f"{bindir}{os.pathsep}/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
           "FAKE_BUNDLE": str(bundle), "FAKE_VERSION": VERSION, "FAKE_UV_LOG": str(log)}
    proc = subprocess.run(["sh", str(REPO / "install" / "install.sh")], env=env, capture_output=True,
                          text=True, timeout=120, check=False)
    return proc, [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def test_install_sh_installs_both_wheels_from_the_stable_release(bundle: Path, tmp_path: Path) -> None:
    """`curl .../install.sh | sh` installs the stable release's bridge and
    engine as ONE uv tool (so `swarm install` finds the engine), every wheel
    checked against SHA256SUMS first, the in-repo libraries from the release."""
    proc, calls = _install(tmp_path, bundle)
    assert proc.returncode == 0, proc.stderr
    (call,) = calls
    assert call[:2] == ["tool", "install"]
    wheels = [a for a in call if " @ file://" in a]
    assert [w.split(" @ ")[0] for w in wheels] == ["swarm-mcp", "swarm-install", "swarm-common", "swarm-redaction"]
    for w in wheels:
        assert w.endswith(f"-{VERSION}-py3-none-any.whl"), w
    assert call.count("--with") == 3


def test_install_sh_refuses_a_wheel_that_does_not_match(bundle: Path, tmp_path: Path) -> None:
    tampered = tmp_path / "tampered"
    shutil.copytree(bundle, tampered)
    wheel = tampered / f"swarm_install-{VERSION}-py3-none-any.whl"
    wheel.write_bytes(wheel.read_bytes() + b"\0")
    proc, calls = _install(tmp_path, tampered)
    assert proc.returncode == 1 and "SHA256SUMS" in proc.stderr, proc.stderr
    assert calls == []


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_install_sh_is_posix_and_shellcheck_clean() -> None:
    """It is piped to `sh`, which is dash on Debian and Ubuntu: POSIX only."""
    proc = subprocess.run(["shellcheck", "-s", "sh", str(REPO / "install" / "install.sh")],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
