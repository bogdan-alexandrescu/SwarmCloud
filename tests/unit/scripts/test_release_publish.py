"""scripts/release/publish.sh: a release reaches the release home only through
a person, signed, and the stable channel moves only after the manual test
(DAYONE-RELEASE-1; plan §7.2 steps 5-6, §7.3; D22; lesson 27).

Every test runs the script against fake `gcloud`, `gcrane` and `cosign` first
on PATH: each logs its argv and answers reads from a state file. Nothing here
reaches Google Cloud. The bundle is one real assemble.sh run of this checkout.
"""

from __future__ import annotations

import hashlib
import json
import os
import pty
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from .test_release_assemble import VERSION, assemble, build_record, fake_digest, image_names

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "release" / "publish.sh"
BUCKET = "gs://swarmcloud"
RELEASE = f"{BUCKET}/releases/{VERSION}"
HOME_REGISTRY = "us-docker.pkg.dev/swarmcloud-prod/swarmcloud"
KMS = "gcpkms://projects/swarmcloud-prod/locations/us/keyRings/swarmcloud-release/cryptoKeys/cosign"

FAKE = r'''#!{python}
"""A fake {tool}: logs argv, answers reads from $FAKE_STATE."""
import json, os, sys

tool = "{tool}"
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps([tool, *args]) + "\n")
state = json.load(open(os.environ["FAKE_STATE"]))
objects = state.get("objects", {})

if tool == "gcloud" and args[:2] == ["storage", "cat"]:
    url = args[-1]
    if url in objects:
        sys.stdout.write(objects[url])
        sys.exit(0)
    print(f"ERROR: (gcloud.storage.cat) The following URLs matched no objects or files: {url}", file=sys.stderr)
    sys.exit(1)
if tool == "gcloud" and args[:2] == ["storage", "ls"]:
    url = args[-1].rstrip("/")
    hits = [o for o in objects if o == url or o.startswith(url + "/")]
    if hits:
        print("\n".join(hits))
        sys.exit(0)
    print(f"ERROR: (gcloud.storage.ls) One or more URLs matched no objects: {url}", file=sys.stderr)
    sys.exit(1)
if tool == "gcrane" and args[:1] == ["cp"] and "@" in args[1]:
    # The copy keeps the digest, unless the state says the registry served another.
    state.setdefault("tags", {})[args[2]] = state.get("serves", {}).get(args[2], args[1].split("@", 1)[1])
    json.dump(state, open(os.environ["FAKE_STATE"], "w"))
    sys.exit(0)
if tool == "gcrane" and args[:1] == ["digest"]:
    ref = args[-1]
    print(ref.split("@", 1)[1] if "@" in ref else state.get("tags", {}).get(ref, "sha256:" + "0" * 64))
    sys.exit(0)
if tool == "cosign" and args[:1] == ["sign-blob"]:
    out = args[args.index("--output-signature") + 1]
    open(out, "w").write("fake signature\n")
    sys.exit(0)
if tool == "cosign" and args[:1] == ["public-key"]:
    print("fake public key")
    sys.exit(0)
sys.exit(0)
'''


@pytest.fixture(scope="module")
def assembled(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    tmp = tmp_path_factory.mktemp("publish-bundle")
    record = build_record(tmp / "record.json")
    proc = assemble(tmp, record)
    assert proc.returncode == 0, proc.stderr[-3000:]
    return tmp / "out", record


@pytest.fixture
def bundle(assembled: tuple[Path, Path], tmp_path: Path) -> Path:
    target = tmp_path / "bundle"
    shutil.copytree(assembled[0], target)
    return target


def _env(tmp_path: Path, state: dict) -> dict[str, str]:
    assert SCRIPT.is_file(), f"{SCRIPT.relative_to(REPO)} does not exist"
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for tool in ("gcloud", "gcrane", "cosign"):
        path = bindir / tool
        path.write_text(FAKE.replace("{python}", sys.executable).replace("{tool}", tool))
        path.chmod(0o755)
    (tmp_path / "state.json").write_text(json.dumps(state))
    (tmp_path / "calls.log").write_text("")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLOUDSDK_", "GOOGLE_", "SWARM_"))}
    env.update(
        PATH=f"{bindir}{os.pathsep}{env.get('PATH', '')}",
        FAKE_STATE=str(tmp_path / "state.json"),
        FAKE_LOG=str(tmp_path / "calls.log"),
        HOME=str(tmp_path),
        NO_COLOR="1",
        SWARM_ENV_FILE=str(tmp_path / "absent.env"),
    )
    return env


def _run(tmp_path: Path, state: dict, *args: str, typed: str | None = None,
         extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Run publish.sh; with `typed`, a terminal on stdin with it already typed."""
    env = _env(tmp_path, state)
    env.update(extra_env or {})
    if typed is None:
        return subprocess.run(["bash", str(SCRIPT), *args], env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=300, check=False)
    master, slave = pty.openpty()
    try:
        os.write(master, (typed + "\n").encode())
        return subprocess.run(["bash", str(SCRIPT), *args], env=env, stdin=slave,
                              capture_output=True, text=True, timeout=300, check=False)
    finally:
        os.close(slave)
        os.close(master)


def _calls(tmp_path: Path) -> list[list[str]]:
    return [json.loads(line) for line in (tmp_path / "calls.log").read_text().splitlines() if line.strip()]


def _writes(calls: list[list[str]]) -> list[list[str]]:
    """Every call that changes the release home."""
    found = []
    for call in calls:
        tool, args = call[0], call[1:]
        if tool == "gcloud" and args[:2] in (["storage", "cp"], ["storage", "rsync"]):
            if [a for a in args[2:] if not a.startswith("-")][-1].startswith("gs://"):
                found.append(call)
        elif tool == "gcrane" and args[:1] == ["cp"]:
            found.append(call)
        elif tool == "cosign" and args[:1] in (["sign"], ["sign-blob"]):
            found.append(call)
    return found


def _destinations(calls: list[list[str]]) -> list[str]:
    return [[a for a in c[3:] if not a.startswith("-")][-1] for c in _writes(calls) if c[0] == "gcloud"]


def _tested(version: str = VERSION, result: str = "pass") -> str:
    return json.dumps({
        "version": version, "previous": None, "tested_at": "2026-10-29T20:00:00Z", "project": "a-test-project",
        "result": result,
        "steps": [{"name": n, "result": result} for n in ("install", "doctor", "uninstall", "reinstall")],
    })


def _published(**extra: str) -> dict:
    return {"objects": {f"{RELEASE}/SHA256SUMS": "x", f"{RELEASE}/install.sh": "x", **extra}}


def _publish(tmp_path: Path, bundle: Path, record: Path, state: dict | None = None,
             typed: str | None = VERSION) -> subprocess.CompletedProcess:
    return _run(tmp_path, state or {}, "--bundle", str(bundle), "--source-images", str(record), typed=typed)


def _resum(bundle: Path) -> None:
    lines = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(bundle).as_posix()}"
             for p in sorted(bundle.rglob("*")) if p.is_file() and p.name != "SHA256SUMS"]
    (bundle / "SHA256SUMS").write_text("\n".join(lines) + "\n")


# -- the manual test record gates the channel (D22, §7.3) ---------------------------------


def test_publish_refuses_without_manual_test_record(tmp_path: Path) -> None:
    """`--promote V` with no `releases/V/TESTED` refuses and writes nothing."""
    proc = _run(tmp_path, _published(), "--promote", VERSION, typed=VERSION)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "TESTED" in proc.stderr and "--record-test" in proc.stderr, proc.stderr
    assert _writes(_calls(tmp_path)) == []
    # A record that says the test failed is no record of a pass.
    failed = _published(**{f"{RELEASE}/TESTED": _tested(result="fail")})
    proc = _run(tmp_path, failed, "--promote", VERSION, typed=VERSION)
    assert proc.returncode == 1 and _writes(_calls(tmp_path)) == [], proc.stderr
    # A record of another version is not this version's.
    other = _published(**{f"{RELEASE}/TESTED": _tested(version="2026.10.2")})
    proc = _run(tmp_path, other, "--promote", VERSION, typed=VERSION)
    assert proc.returncode == 1 and _writes(_calls(tmp_path)) == [], proc.stderr


def test_promote_moves_channel_after_the_record(tmp_path: Path) -> None:
    state = _published(**{f"{RELEASE}/TESTED": _tested()})
    proc = _run(tmp_path, state, "--promote", VERSION, typed=VERSION)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    dest = _destinations(_calls(tmp_path))
    assert f"{BUCKET}/channels/stable" in dest
    assert f"{BUCKET}/marketplace.json" in dest
    assert f"{BUCKET}/install.sh" in dest
    # What moves is uncached: a teammate must not read yesterday's channel.
    for call in _writes(_calls(tmp_path)):
        assert "--cache-control=no-cache" in call, call


def test_promote_needs_the_version_typed(tmp_path: Path) -> None:
    state = _published(**{f"{RELEASE}/TESTED": _tested()})
    proc = _run(tmp_path, state, "--promote", VERSION, typed="yes",
                extra_env={"SWARM_ASSUME_YES": "1"})
    assert proc.returncode == 1
    assert _writes(_calls(tmp_path)) == []


def test_channel_moved_only_by_promote(tmp_path: Path, bundle: Path, assembled: tuple[Path, Path]) -> None:
    """A publish uploads under releases/V/ and nowhere else: the channel and
    the root marketplace move only with `--promote`, after the manual test."""
    proc = _publish(tmp_path, bundle, assembled[1])
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-3000:]
    dest = _destinations(_calls(tmp_path))
    assert dest, "publish uploaded nothing"
    assert all(d.startswith(RELEASE) for d in dest), dest
    text = (SCRIPT.read_text())
    # The one place the script names the channel is the promotion.
    channel_lines = [line for line in text.splitlines() if "channels/stable" in line and not line.lstrip().startswith("#")]
    assert channel_lines and all("PROMOTE" in line or "CHANNEL" in line for line in channel_lines), channel_lines


def test_record_test_writes_tested_once(tmp_path: Path) -> None:
    record = tmp_path / "tested.json"
    record.write_text(_tested())
    proc = _run(tmp_path, _published(), "--record-test", VERSION, "--record", str(record), typed=VERSION)
    assert proc.returncode == 0, proc.stderr
    assert _destinations(_calls(tmp_path)) == [f"{RELEASE}/TESTED"]
    # A failing record is refused; a second record is refused (the first stands).
    record.write_text(_tested(result="fail"))
    proc = _run(tmp_path, _published(), "--record-test", VERSION, "--record", str(record), typed=VERSION)
    assert proc.returncode == 1 and _writes(_calls(tmp_path)) == []
    record.write_text(_tested())
    proc = _run(tmp_path, _published(**{f"{RELEASE}/TESTED": _tested()}),
                "--record-test", VERSION, "--record", str(record), typed=VERSION)
    assert proc.returncode == 1 and _writes(_calls(tmp_path)) == []


# -- the CVE acceptances (lesson 27) --------------------------------------------------------------


@pytest.mark.parametrize("entry", [
    {"cve": "CVE-2026-1", "package": "x", "reason": "r"},
    {"cve": "CVE-2026-1", "package": "x", "reason": "r", "expires": ""},
    {"cve": "CVE-2026-1", "package": "x", "reason": "r", "expires": "2020-01-01"},
])
def test_cve_acceptances_have_expiry(tmp_path: Path, bundle: Path, assembled: tuple[Path, Path], entry: dict) -> None:
    """A release that accepts a CVE with no expiry, or past it, is never
    published: the installer would refuse it anyway (preflight.release)."""
    (bundle / "cve-acceptances.json").write_text(json.dumps([entry]))
    _resum(bundle)
    proc = _publish(tmp_path, bundle, assembled[1])
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "CVE-2026-1" in proc.stderr, proc.stderr
    assert _writes(_calls(tmp_path)) == []


# -- what a publish does ---------------------------------------------------------------------------


def test_publish_copies_and_signs_every_image_by_digest(tmp_path: Path, bundle: Path,
                                                         assembled: tuple[Path, Path]) -> None:
    proc = _publish(tmp_path, bundle, assembled[1])
    assert proc.returncode == 0, proc.stderr[-3000:]
    calls = _calls(tmp_path)
    copies = [c for c in calls if c[:2] == ["gcrane", "cp"]]
    signs = [c for c in calls if c[:2] == ["cosign", "sign"]]
    for name in image_names():
        d = fake_digest(name)
        assert [f"us-central1-docker.pkg.dev/ci-project/swarm/{name}@{d}", f"{HOME_REGISTRY}/{name}:{VERSION}"] in \
            [c[2:4] for c in copies], name
        assert any(f"{HOME_REGISTRY}/{name}@{d}" in c and f"--key={KMS}" in c for c in signs), name
    blob = [c for c in calls if c[:2] == ["cosign", "sign-blob"]]
    assert len(blob) == 1 and f"--key={KMS}" in blob[0]
    dest = _destinations(calls)
    # SHA256SUMS goes up last, so a half-uploaded release has none to read.
    assert dest[-1] == f"{RELEASE}/SHA256SUMS", dest
    assert f"{RELEASE}/SHA256SUMS.sig" in dest and f"{RELEASE}/cosign.pub" in dest


def test_publish_refuses_a_copy_with_another_digest(tmp_path: Path, bundle: Path,
                                                    assembled: tuple[Path, Path]) -> None:
    """The copy is checked by digest before it is signed (H28)."""
    state = {"serves": {f"{HOME_REGISTRY}/swarm-api:{VERSION}": fake_digest("other")}}
    proc = _publish(tmp_path, bundle, assembled[1], state=state)
    assert proc.returncode == 1 and "swarm-api" in proc.stderr, proc.stderr[-2000:]
    calls = _calls(tmp_path)
    assert not [c for c in calls if c[:2] == ["cosign", "sign"] and any("/swarm-api@" in a for a in c)]
    assert not [c for c in _writes(calls) if c[0] == "gcloud"]


def test_publish_refuses_a_tampered_bundle(tmp_path: Path, bundle: Path, assembled: tuple[Path, Path]) -> None:
    (bundle / "images.json").write_text(json.dumps({n: fake_digest("other") for n in image_names()}))
    proc = _publish(tmp_path, bundle, assembled[1])
    assert proc.returncode == 1 and "images.json" in proc.stderr, proc.stderr
    assert _writes(_calls(tmp_path)) == []


def test_publish_refuses_a_digest_the_record_does_not_hold(tmp_path: Path, bundle: Path) -> None:
    record = build_record(tmp_path / "other.json", **{"swarm-api": {"digest": fake_digest("x"),
                                                                     "ref": f"x/swarm-api@{fake_digest('x')}"}})
    proc = _publish(tmp_path, bundle, record)
    assert proc.returncode == 1 and "swarm-api" in proc.stderr, proc.stderr
    assert _writes(_calls(tmp_path)) == []


def test_publish_never_overwrites_a_release(tmp_path: Path, bundle: Path, assembled: tuple[Path, Path]) -> None:
    proc = _publish(tmp_path, bundle, assembled[1], state=_published())
    assert proc.returncode == 1 and "already" in proc.stderr, proc.stderr
    assert _writes(_calls(tmp_path)) == []


def test_publish_needs_the_version_typed(tmp_path: Path, bundle: Path, assembled: tuple[Path, Path]) -> None:
    proc = _run(tmp_path, {}, "--bundle", str(bundle), "--source-images", str(assembled[1]),
                typed=None, extra_env={"SWARM_ASSUME_YES": "1"})
    assert proc.returncode == 1
    assert _writes(_calls(tmp_path)) == []


def test_dry_run_writes_nothing(tmp_path: Path, bundle: Path, assembled: tuple[Path, Path]) -> None:
    proc = _run(tmp_path, {}, "--bundle", str(bundle), "--source-images", str(assembled[1]), "--dry-run")
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert _writes(_calls(tmp_path)) == []
    assert "gcrane cp" in proc.stderr and "cosign sign" in proc.stderr


@pytest.mark.parametrize("project", ["agents-staging", "some-other-project"])
def test_refuses_a_project_that_is_not_the_release_home(tmp_path: Path, project: str) -> None:
    proc = _run(tmp_path, _published(**{f"{RELEASE}/TESTED": _tested()}),
                "--promote", VERSION, "--project", project, typed=VERSION)
    assert proc.returncode != 0
    assert _calls(tmp_path) == []


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck_clean() -> None:
    proc = subprocess.run(["shellcheck", "-x", str(SCRIPT)], cwd=REPO / "scripts",
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
