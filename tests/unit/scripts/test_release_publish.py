"""scripts/release/publish.sh: a release reaches the release home only through
a person, signed, and the stable channel moves only after the manual test
(DAYONE-RELEASE-1; plan §7.2 steps 5-6, §7.3; D22; lesson 27).

Since DAYONE-RELEASE-CI (BGD, 2026-10-30) the person may be the reviewer of a
dispatched publish-release run instead of a terminal: `--ci` takes the
version typed into the dispatch form (`SWARM_PUBLISH_VERSION`) as the typed
confirmation, under GitHub Actions only, for a publish only. A promotion stays
a person's typed act.

Every test runs the script against fake `gcloud`, `gcrane` and `cosign` first
on PATH: each logs its argv and answers reads from a state file. Nothing here
reaches Google Cloud. The bundle is one real assemble.sh run of this checkout.
"""

from __future__ import annotations

import hashlib
import json
import os
import importlib.util
import pty
import secrets
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from .test_release_assemble import VERSION, assemble, build_record, fake_digest, image_names

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "release" / "publish.sh"
BUCKET = "gs://swarmcloud"
RELEASE = f"{BUCKET}/releases/{VERSION}"
HOME_REGISTRY = "us-docker.pkg.dev/swarmcloud-prod/swarmcloud"
KMS = "gcpkms://projects/swarmcloud-prod/locations/us/keyRings/swarmcloud-release/cryptoKeys/cosign"
#: The release key the engine pins; the assembled bundle's swarm_install wheel carries it.
RELEASE_KEY = REPO / "apps" / "swarm-install" / "swarm_install" / "release_key.pub"

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
if tool == "gcloud" and args[:2] == ["storage", "cp"]:
    paths = [a for a in args[2:] if not a.startswith("-")]
    if len(paths) == 2 and paths[1].startswith("gs://"):
        source, dest = paths
        # The object gcloud storage cp writes, by its own destination rules
        # (googlecloudsdk/command_lib/storage/tasks/cp/copy_task_iterator.py):
        # a destination ending in "/" is a folder, and so is one that is not an
        # object but a prefix of some, so the source's own name is appended:
        # `cp rc gs://b/channels/rc` with only channels/rc/x in the bucket
        # writes channels/rc/rc (audit 3, A5).
        if dest.endswith("/"):
            dest = dest + os.path.basename(source)
        elif dest not in objects and any(o.startswith(dest + "/") for o in objects):
            dest = dest + "/" + os.path.basename(source)
        state.setdefault("written", []).append([dest, args])
        # Keep what a small upload held, so a test (and a read back) sees it.
        if source in objects or (os.path.isfile(source) and os.path.getsize(source) < 4096):
            held = objects[source] if source in objects else open(source).read()
            held = state.get("lands", {}).get(dest, held)
            state.setdefault("uploaded", {})[dest] = held
            objects[dest] = held
            state["objects"] = objects
        json.dump(state, open(os.environ["FAKE_STATE"], "w"))
    sys.exit(0)
if tool == "gcrane" and args[:1] == ["cp"] and "@" in args[1]:
    # The copy keeps the digest, unless the state says the registry served another.
    state.setdefault("tags", {})[args[2]] = state.get("serves", {}).get(args[2], args[1].split("@", 1)[1])
    json.dump(state, open(os.environ["FAKE_STATE"], "w"))
    sys.exit(0)
if tool == "gcrane" and args[:1] == ["digest"]:
    ref = args[-1]
    print(ref.split("@", 1)[1] if "@" in ref else state.get("tags", {}).get(ref, "sha256:" + "0" * 64))
    sys.exit(0)
# The KMS key's public half is state["kms_key"]; a signature names the key
# that made it, and verify-blob passes only with that same public key.
if tool == "cosign" and args[:1] == ["sign-blob"]:
    out = args[args.index("--output-signature") + 1]
    open(out, "w").write("signed by " + state["kms_key"] + "\n")
    sys.exit(0)
if tool == "cosign" and args[:1] == ["public-key"]:
    print(state["kms_key"])
    sys.exit(0)
if tool == "cosign" and args[:1] == ["verify-blob"]:
    key = open(args[args.index("--key") + 1]).read().strip()
    signature = open(args[args.index("--signature") + 1]).read().strip()
    if signature == "signed by " + key and os.path.isfile(args[-1]):
        print("Verified OK", file=sys.stderr)
        sys.exit(0)
    print("Error: invalid signature when validating ASN.1 encoded signature", file=sys.stderr)
    sys.exit(1)
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
    # The KMS key is the pinned one unless a test says it is another.
    state = {"kms_key": RELEASE_KEY.read_text().strip(), **state}
    (tmp_path / "state.json").write_text(json.dumps(state))
    (tmp_path / "calls.log").write_text("")
    # GITHUB_ too: a run of these tests inside Actions must not stand in for
    # the Actions run `--ci` asks for; a test that wants one says so.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLOUDSDK_", "GOOGLE_", "SWARM_", "GITHUB_"))}
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


def _objects(tmp_path: Path) -> list[str]:
    """Every object a `gcloud storage cp` wrote, as gcloud names it, in order."""
    return [dest for dest, _ in json.loads((tmp_path / "state.json").read_text()).get("written", [])]


def _object_writes(tmp_path: Path, name: str) -> list[list[str]]:
    """The argv of every `gcloud storage cp` that wrote the object `name`."""
    return [args for dest, args in json.loads((tmp_path / "state.json").read_text()).get("written", [])
            if dest == name]


def _tested(version: str = VERSION, result: str = "pass") -> str:
    return json.dumps({
        "version": version, "previous": None, "tested_at": "2026-10-29T20:00:00Z", "project": "a-test-project",
        "result": result,
        "steps": [{"name": n, "result": result} for n in ("install", "doctor", "uninstall", "reinstall")],
    })


#: The digest the published release's SHA256SUMS holds for its plugin.zip.
PLUGIN_ZIP_SHA256 = hashlib.sha256(b"a published plugin.zip").hexdigest()


def _published(**extra: str) -> dict:
    sums = f"{PLUGIN_ZIP_SHA256}  plugin.zip\n{'0' * 64}  install.sh\n"
    return {"objects": {f"{RELEASE}/SHA256SUMS": sums, f"{RELEASE}/install.sh": "x", **extra}}


def _marketplace_module():
    spec = importlib.util.spec_from_file_location("publish_test_marketplace",
                                                  REPO / "scripts" / "release" / "marketplace.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


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
    dest = _objects(tmp_path)
    assert dest[-1] == f"{BUCKET}/channels/stable", dest
    assert f"{BUCKET}/marketplace.json" in dest
    assert f"{BUCKET}/install.sh" in dest
    # What moves is uncached: a teammate must not read yesterday's channel.
    for call in _writes(_calls(tmp_path)):
        assert "--cache-control=no-cache" in call, call


def test_promote_writes_the_stable_object_whatever_prefix_exists(tmp_path: Path) -> None:
    """`channels/stable` is written as that object even when the bucket holds
    objects under a `channels/stable/` prefix, and read back (A5)."""
    state = _published(**{f"{RELEASE}/TESTED": _tested(), f"{BUCKET}/channels/stable/stray": "x"})
    proc = _run(tmp_path, state, "--promote", VERSION, typed=VERSION)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    dest = _objects(tmp_path)
    assert dest[-1] == f"{BUCKET}/channels/stable", dest
    assert not [d for d in dest if d.startswith(f"{BUCKET}/channels/stable/")], dest
    assert ["gcloud", "storage", "cat", f"{BUCKET}/channels/stable"] in _calls(tmp_path)


def test_promote_refuses_a_stable_object_that_does_not_read_back(tmp_path: Path) -> None:
    state = _published(**{f"{RELEASE}/TESTED": _tested()})
    state["lands"] = {f"{BUCKET}/channels/stable": "2026.09.9\n"}
    proc = _run(tmp_path, state, "--promote", VERSION, typed=VERSION)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert f"{BUCKET}/channels/stable" in proc.stderr and "2026.09.9" in proc.stderr, proc.stderr


def test_promote_marketplace_names_the_plugin_zip_by_url(tmp_path: Path) -> None:
    """The root marketplace is added BY URL, so Claude Code fetches only that
    file: its plugin is releases/V/plugin.zip by absolute URL, with the digest
    V's published SHA256SUMS holds (audit finding N9)."""
    state = _published(**{f"{RELEASE}/TESTED": _tested()})
    proc = _run(tmp_path, state, "--promote", VERSION, typed=VERSION)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    uploaded = json.loads((tmp_path / "state.json").read_text()).get("uploaded", {})
    (sc,) = json.loads(uploaded[f"{BUCKET}/marketplace.json"])["plugins"]
    # MUTATION: promote with --source ./releases/V/plugin again and this fails.
    assert sc["source"] == {"source": "archive", "url": f"https://storage.googleapis.com/swarmcloud/releases/{VERSION}/plugin.zip",
                            "sha256": PLUGIN_ZIP_SHA256}


def test_promote_refuses_a_release_without_plugin_zip(tmp_path: Path) -> None:
    """A release whose SHA256SUMS lists no plugin.zip has no plugin a marketplace
    added by URL can install: it is not promoted, and nothing is written."""
    state = _published(**{f"{RELEASE}/TESTED": _tested(), f"{RELEASE}/SHA256SUMS": f"{'0' * 64}  install.sh\n"})
    proc = _run(tmp_path, state, "--promote", VERSION, typed=VERSION)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "plugin.zip" in proc.stderr, proc.stderr
    assert _writes(_calls(tmp_path)) == []


def test_promote_needs_the_version_typed(tmp_path: Path) -> None:
    state = _published(**{f"{RELEASE}/TESTED": _tested()})
    proc = _run(tmp_path, state, "--promote", VERSION, typed="yes",
                extra_env={"SWARM_ASSUME_YES": "1"})
    assert proc.returncode == 1
    assert _writes(_calls(tmp_path)) == []


def test_channel_moved_only_by_promote(tmp_path: Path, bundle: Path, assembled: tuple[Path, Path]) -> None:
    """A publish uploads under releases/V/ and, outside it, only the
    channel-agnostic root install.sh: the channel and the root marketplace move
    only with `--promote`, after the manual test."""
    proc = _publish(tmp_path, bundle, assembled[1])
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-3000:]
    dest = _destinations(_calls(tmp_path))
    assert dest, "publish uploaded nothing"
    assert all(d.startswith(RELEASE) or d == f"{BUCKET}/install.sh" for d in dest), dest
    assert f"{BUCKET}/marketplace.json" not in dest, dest
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
    # SHA256SUMS goes up last of the release, so a half-uploaded release has
    # none to read; only the root install.sh follows it.
    assert [d for d in dest if d.startswith(RELEASE)][-1] == f"{RELEASE}/SHA256SUMS", dest
    assert dest[-1] == f"{BUCKET}/install.sh", dest
    assert f"{RELEASE}/SHA256SUMS.sig" in dest and f"{RELEASE}/cosign.pub" in dest


def _rekey_wheel(bundle: Path, tmp_path: Path, key: str | None) -> None:
    """Put `key` as the engine wheel's release_key.pub (None: drop it), resummed."""
    (wheel,) = bundle.glob("swarm_install-*.whl")
    rebuilt = tmp_path / wheel.name
    member = "swarm_install/release_key.pub"
    with zipfile.ZipFile(wheel) as source, zipfile.ZipFile(rebuilt, "w") as target:
        for item in source.infolist():
            if item.filename == member:
                if key is not None:
                    target.writestr(item, key + "\n")
            else:
                target.writestr(item, source.read(item))
    shutil.move(rebuilt, wheel)
    _resum(bundle)


def test_publish_verifies_the_new_signature_with_the_wheels_key_before_writing(
        tmp_path: Path, bundle: Path, assembled: tuple[Path, Path]) -> None:
    """The SHA256SUMS signature is checked with the release_key.pub inside the
    bundle's own engine wheel (the key every install pins) before any image is
    copied or any object written (audit 3, A11)."""
    proc = _publish(tmp_path, bundle, assembled[1])
    assert proc.returncode == 0, proc.stderr[-3000:]
    calls = _calls(tmp_path)
    verifies = [c for c in calls if c[:2] == ["cosign", "verify-blob"]]
    # MUTATION: drop the verify-blob and this fails.
    assert len(verifies) == 1, [c[:2] for c in calls]
    (verify,) = verifies
    assert Path(verify[-1]).name == "SHA256SUMS", verify
    key = Path(verify[verify.index("--key") + 1])
    assert key.name == "release_key.pub", verify
    assert "--insecure-ignore-tlog=true" in verify, verify
    first_write = min(calls.index(c) for c in _writes(calls) if c[:2] != ["cosign", "sign-blob"])
    assert calls.index(verify) < first_write, calls[:first_write + 1]


def test_publish_refuses_a_signature_the_pinned_key_does_not_verify(tmp_path: Path, bundle: Path,
                                                                     assembled: tuple[Path, Path]) -> None:
    """A KMS key that is not the one the wheel pins would fail every install,
    and a published release is never overwritten: nothing is written."""
    other = "-----BEGIN PUBLIC KEY-----\n" + secrets.token_hex(32) + "\n-----END PUBLIC KEY-----"
    proc = _publish(tmp_path, bundle, assembled[1], state={"kms_key": other})
    assert proc.returncode == 1, proc.stdout[-2000:] + proc.stderr[-3000:]
    assert "release_key.pub" in proc.stderr, proc.stderr[-2000:]
    assert [c for c in _writes(_calls(tmp_path)) if c[:2] != ["cosign", "sign-blob"]] == []


def test_publish_refuses_a_wheel_without_the_release_key(tmp_path: Path, bundle: Path,
                                                         assembled: tuple[Path, Path]) -> None:
    _rekey_wheel(bundle, tmp_path, None)
    proc = _publish(tmp_path, bundle, assembled[1])
    assert proc.returncode == 1, proc.stdout[-2000:] + proc.stderr[-3000:]
    assert "release_key.pub" in proc.stderr, proc.stderr[-2000:]
    assert _writes(_calls(tmp_path)) == []


def test_publish_verifies_with_the_wheels_key_not_the_checkouts(tmp_path: Path, bundle: Path,
                                                                assembled: tuple[Path, Path]) -> None:
    """What installs pin is the key inside the published wheel, so that is the
    key publish checks against, whatever this checkout holds."""
    other = "-----BEGIN PUBLIC KEY-----\n" + secrets.token_hex(32) + "\n-----END PUBLIC KEY-----"
    _rekey_wheel(bundle, tmp_path, other)
    proc = _publish(tmp_path, bundle, assembled[1], state={"kms_key": other})
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-3000:]
    proc = _publish(tmp_path, bundle, assembled[1])
    assert proc.returncode == 1, proc.stdout[-2000:] + proc.stderr[-3000:]


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


# -- a release candidate moves channels/rc (DAYONE-RELEASE-2) -----------------------------------

CANDIDATE = f"{VERSION}-rc.1"
CANDIDATE_RELEASE = f"{BUCKET}/releases/{CANDIDATE}"


def _as_candidate(bundle: Path) -> Path:
    """The assembled bundle, renamed to CANDIDATE where publish.sh reads the
    version (compat.json, the plugin), SHA256SUMS rewritten to match."""
    compat = json.loads((bundle / "compat.json").read_text())
    compat["plugin"] = CANDIDATE
    (bundle / "compat.json").write_text(json.dumps(compat))
    plugin_json = bundle / "plugin" / ".claude-plugin" / "plugin.json"
    plugin = json.loads(plugin_json.read_text())
    plugin["version"] = CANDIDATE
    plugin_json.write_text(json.dumps(plugin))
    archive_sha256 = _marketplace_module().zip_plugin(bundle / "plugin", bundle / "plugin.zip")
    (bundle / "marketplace.json").write_text(
        json.dumps(_marketplace_module().marketplace(CANDIDATE, archive_sha256), indent=2) + "\n")
    _resum(bundle)
    return bundle


@pytest.mark.parametrize("candidate", [False, True], ids=["final", "candidate"])
def test_every_publish_puts_install_sh_at_the_root(tmp_path: Path, bundle: Path, assembled: tuple[Path, Path],
                                                   candidate: bool) -> None:
    """Every publish, a candidate's included, copies the bundle's install.sh to
    the bucket root, uncached, after SHA256SUMS: `curl .../install.sh |
    SWARM_CHANNEL=rc sh` must work before any promotion, since a promotion
    needs an install of the candidate first (audit finding N10)."""
    version = CANDIDATE if candidate else VERSION
    proc = _publish(tmp_path, _as_candidate(bundle) if candidate else bundle, assembled[1], typed=version)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-3000:]
    calls = _calls(tmp_path)
    dest = _destinations(calls)
    # MUTATION: copy install.sh to the root only in --promote again and this fails.
    assert f"{BUCKET}/install.sh" in dest, dest
    assert dest.index(f"{BUCKET}/install.sh") > dest.index(f"{BUCKET}/releases/{version}/SHA256SUMS"), dest
    (call,) = [c for c in _writes(calls) if c[0] == "gcloud" and _destinations([c]) == [f"{BUCKET}/install.sh"]]
    assert "--cache-control=no-cache" in call, call
    assert [a for a in call[3:] if not a.startswith("-")][0] == str(bundle / "install.sh"), call
    # The script it puts there reads whichever channel it is asked for.
    assert "channels/${CHANNEL}" in (bundle / "install.sh").read_text()


def test_candidate_publish_writes_the_rc_marketplace(tmp_path: Path, bundle: Path,
                                                     assembled: tuple[Path, Path]) -> None:
    """A candidate's publish writes channels/rc.marketplace.json, the
    candidate's own marketplace (plugin.zip by URL), uncached, before the
    channel line: a deployment on the rc channel adds it (audit finding N10).
    It is a sibling of the channel object, never under a `channels/rc/`
    prefix, which would make `channels/rc` a folder to gcloud (A5)."""
    proc = _publish(tmp_path, _as_candidate(bundle), assembled[1], typed=CANDIDATE)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-3000:]
    dest = _objects(tmp_path)
    # MUTATION: drop the rc marketplace write and this fails.
    assert f"{BUCKET}/channels/rc.marketplace.json" in dest, dest
    assert dest.index(f"{BUCKET}/channels/rc.marketplace.json") < dest.index(f"{BUCKET}/channels/rc"), dest
    (call,) = _object_writes(tmp_path, f"{BUCKET}/channels/rc.marketplace.json")
    assert "--cache-control=no-cache" in call, call
    uploaded = json.loads((tmp_path / "state.json").read_text()).get("uploaded", {})
    (sc,) = json.loads(uploaded[f"{BUCKET}/channels/rc.marketplace.json"])["plugins"]
    assert sc["version"] == CANDIDATE
    assert sc["source"]["url"] == f"https://storage.googleapis.com/swarmcloud/releases/{CANDIDATE}/plugin.zip"
    assert sc["source"]["sha256"] == hashlib.sha256((bundle / "plugin.zip").read_bytes()).hexdigest()


def test_publish_refuses_a_marketplace_with_a_relative_source(tmp_path: Path, bundle: Path,
                                                              assembled: tuple[Path, Path]) -> None:
    """A bundle whose marketplace names the plugin by a relative path (which a
    marketplace added by URL cannot resolve) is refused before anything is
    written (N9)."""
    document = json.loads((bundle / "marketplace.json").read_text())
    document["plugins"][0]["source"] = "./plugin"
    (bundle / "marketplace.json").write_text(json.dumps(document, indent=2) + "\n")
    _resum(bundle)
    proc = _publish(tmp_path, bundle, assembled[1])
    assert proc.returncode == 1, proc.stdout[-2000:] + proc.stderr[-3000:]
    assert "marketplace.json" in proc.stderr, proc.stderr[-2000:]
    assert _writes(_calls(tmp_path)) == []


def test_candidate_publish_moves_the_rc_channel_last(tmp_path: Path, bundle: Path,
                                                     assembled: tuple[Path, Path]) -> None:
    """A candidate's publish writes `channels/rc` naming it, uncached, after
    SHA256SUMS: `SWARM_CHANNEL=rc` install.sh reads that channel, and an
    installer that reads it finds a complete, signed release."""
    proc = _publish(tmp_path, _as_candidate(bundle), assembled[1], typed=CANDIDATE)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-3000:]
    dest = _objects(tmp_path)
    # MUTATION: drop the rc write from publish.sh and this fails.
    assert dest[-1] == f"{BUCKET}/channels/rc", dest
    assert dest.index(f"{CANDIDATE_RELEASE}/SHA256SUMS") < dest.index(f"{BUCKET}/channels/rc.marketplace.json"), dest
    assert f"{BUCKET}/channels/stable" not in dest, dest
    (rc,) = _object_writes(tmp_path, f"{BUCKET}/channels/rc")
    assert "--cache-control=no-cache" in rc, rc
    # What the channel holds is one line, the candidate's version, as
    # install.sh reads it (`curl .../channels/${SWARM_CHANNEL}`).
    uploaded = json.loads((tmp_path / "state.json").read_text()).get("uploaded", {})
    assert uploaded.get(f"{BUCKET}/channels/rc") == f"{CANDIDATE}\n", uploaded.get(f"{BUCKET}/channels/rc")


#: What the bucket held after 2026.10.1-rc.1 was published from 67e6e636f: the
#: marketplace under a `channels/rc/` prefix, then the channel line copied INTO
#: that prefix as channels/rc/rc, and no object channels/rc (audit 3, A5).
RC1_LAYOUT = {f"{BUCKET}/channels/rc/marketplace.json": "{}", f"{BUCKET}/channels/rc/rc": "2026.10.1-rc.1\n"}


@pytest.mark.parametrize("before", [{}, RC1_LAYOUT, {**RC1_LAYOUT, f"{BUCKET}/channels/rc": "2026.10.1-rc.1\n"}],
                         ids=["empty", "rc1-prefix", "rc1-prefix-and-object"])
def test_candidate_publish_writes_exactly_the_channel_objects(tmp_path: Path, bundle: Path,
                                                              assembled: tuple[Path, Path], before: dict) -> None:
    """The channel line lands as the object `channels/rc`, the one install.sh
    and preflight read, and the marketplace as `channels/rc.marketplace.json`,
    whatever `channels/rc/` prefix the bucket already holds (A5)."""
    proc = _publish(tmp_path, _as_candidate(bundle), assembled[1], state={"objects": dict(before)},
                    typed=CANDIDATE)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-3000:]
    channel_objects = [d for d in _objects(tmp_path) if d.startswith(f"{BUCKET}/channels/")]
    # MUTATION: copy $WORK/rc to gs://swarmcloud/channels/rc again and the rc1-prefix case fails.
    assert channel_objects == [f"{BUCKET}/channels/rc.marketplace.json", f"{BUCKET}/channels/rc"], channel_objects
    objects = json.loads((tmp_path / "state.json").read_text())["objects"]
    assert objects[f"{BUCKET}/channels/rc"] == f"{CANDIDATE}\n"


def test_candidate_publish_reads_the_channel_back(tmp_path: Path, bundle: Path,
                                                  assembled: tuple[Path, Path]) -> None:
    """After the write, publish reads `channels/rc` back, and it holds exactly
    the candidate's version."""
    proc = _publish(tmp_path, _as_candidate(bundle), assembled[1], typed=CANDIDATE)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-3000:]
    calls = _calls(tmp_path)
    read = ["gcloud", "storage", "cat", f"{BUCKET}/channels/rc"]
    # MUTATION: drop the read back and this fails.
    assert read in calls, calls[-5:]
    last_cp = max(i for i, c in enumerate(calls) if c[:3] == ["gcloud", "storage", "cp"])
    assert calls.index(read) > last_cp


@pytest.mark.parametrize("held", ["2026.10.1-rc.1\n", "", f"{VERSION}-rc.10\n"])
def test_candidate_publish_fails_when_the_channel_does_not_read_back(tmp_path: Path, bundle: Path,
                                                                     assembled: tuple[Path, Path], held: str) -> None:
    state = {"lands": {f"{BUCKET}/channels/rc": held}}
    proc = _publish(tmp_path, _as_candidate(bundle), assembled[1], state=state, typed=CANDIDATE)
    assert proc.returncode == 1, proc.stdout[-2000:] + proc.stderr[-3000:]
    assert f"{BUCKET}/channels/rc" in proc.stderr and CANDIDATE in proc.stderr, proc.stderr[-2000:]


def test_candidate_publish_by_ci_moves_the_rc_channel(tmp_path: Path, bundle: Path,
                                                      assembled: tuple[Path, Path]) -> None:
    """The dispatched publish of a candidate moves channels/rc as a typed one
    does; channels/stable stays a person's `--promote`."""
    proc = _run(tmp_path, {}, "--bundle", str(_as_candidate(bundle)), "--source-images", str(assembled[1]),
                "--ci", typed=None, extra_env=_actions(version=CANDIDATE))
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-3000:]
    dest = _objects(tmp_path)
    assert dest[-1] == f"{BUCKET}/channels/rc", dest
    assert f"{BUCKET}/channels/stable" not in dest, dest


def test_candidate_dry_run_writes_no_channel(tmp_path: Path, bundle: Path, assembled: tuple[Path, Path]) -> None:
    proc = _run(tmp_path, {}, "--bundle", str(_as_candidate(bundle)), "--source-images", str(assembled[1]),
                "--dry-run")
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert _writes(_calls(tmp_path)) == []
    assert f"{BUCKET}/channels/rc" in proc.stderr, proc.stderr[-2000:]


def test_final_release_publish_moves_no_channel(tmp_path: Path, bundle: Path, assembled: tuple[Path, Path]) -> None:
    """A final release (no -rc.N) moves neither channel at publish: stable only
    by `--promote`, and rc never to a final release."""
    proc = _publish(tmp_path, bundle, assembled[1])
    assert proc.returncode == 0, proc.stderr[-3000:]
    dest = _destinations(_calls(tmp_path))
    assert not [d for d in dest if "/channels/" in d], dest


def test_a_failed_candidate_publish_moves_no_channel(tmp_path: Path, bundle: Path,
                                                     assembled: tuple[Path, Path]) -> None:
    """The channel moves only after the publish succeeded."""
    state = {"serves": {f"{HOME_REGISTRY}/swarm-api:{CANDIDATE}": fake_digest("other")}}
    proc = _publish(tmp_path, _as_candidate(bundle), assembled[1], state=state, typed=CANDIDATE)
    assert proc.returncode == 1, proc.stderr[-2000:]
    assert not [d for d in _destinations(_calls(tmp_path)) if "/channels/" in d]


# -- --ci: the dispatched, approved run stands for the typed version (DAYONE-RELEASE-CI) -----


def _actions(version: str | None = VERSION, actions: str | None = "true", **extra: str) -> dict:
    """The environment publish-release.yml gives publish.sh."""
    env = dict(extra)
    if actions is not None:
        env["GITHUB_ACTIONS"] = actions
    if version is not None:
        env["SWARM_PUBLISH_VERSION"] = version
    return env


def _publish_ci(tmp_path: Path, bundle: Path, record: Path, env: dict, *more: str) -> subprocess.CompletedProcess:
    # No terminal: nobody is there to type, which is the point.
    return _run(tmp_path, {}, "--bundle", str(bundle), "--source-images", str(record), "--ci", *more,
                typed=None, extra_env=env)


def test_ci_publish_under_actions_with_the_dispatched_version(tmp_path: Path, bundle: Path,
                                                              assembled: tuple[Path, Path]) -> None:
    """In Actions, with the dispatch form's version equal to the bundle's,
    `--ci` publishes with no terminal, exactly as a typed publish does."""
    proc = _publish_ci(tmp_path, bundle, assembled[1], _actions())
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-3000:]
    calls = _calls(tmp_path)
    assert [c for c in calls if c[:2] == ["gcrane", "cp"]], "nothing was copied"
    assert [c for c in calls if c[:2] == ["cosign", "sign"]], "nothing was signed"
    dest = _destinations(calls)
    assert dest[-2:] == [f"{RELEASE}/SHA256SUMS", f"{BUCKET}/install.sh"], dest
    assert all(d.startswith(RELEASE) for d in dest[:-1]), dest
    assert "to continue" not in proc.stderr
    assert "SWARM_PUBLISH_VERSION" in proc.stderr, proc.stderr


@pytest.mark.parametrize("actions", [None, "", "false", "1", "TRUE"])
def test_ci_refused_outside_actions(tmp_path: Path, bundle: Path, assembled: tuple[Path, Path],
                                    actions: str | None) -> None:
    """Outside GitHub Actions `--ci` is refused before anything is called,
    even with the matching version set."""
    proc = _publish_ci(tmp_path, bundle, assembled[1], _actions(actions=actions))
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "GITHUB_ACTIONS" in proc.stderr, proc.stderr
    assert _calls(tmp_path) == []


@pytest.mark.parametrize("version", [None, "", "2026.10.2", f"{VERSION}-rc.1", f" {VERSION}"])
def test_ci_refused_unless_the_dispatched_version_matches(tmp_path: Path, bundle: Path,
                                                          assembled: tuple[Path, Path],
                                                          version: str | None) -> None:
    """The version typed into the dispatch form is the confirmation: absent,
    or not the bundle's, it confirms nothing and nothing is written."""
    proc = _publish_ci(tmp_path, bundle, assembled[1], _actions(version=version))
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "SWARM_PUBLISH_VERSION" in proc.stderr, proc.stderr
    assert _writes(_calls(tmp_path)) == []


def test_ci_is_not_answered_by_assume_yes(tmp_path: Path, bundle: Path, assembled: tuple[Path, Path]) -> None:
    proc = _publish_ci(tmp_path, bundle, assembled[1], _actions(version="2026.10.2", SWARM_ASSUME_YES="1"))
    assert proc.returncode != 0
    assert _writes(_calls(tmp_path)) == []


def test_actions_without_ci_still_needs_the_version_typed(tmp_path: Path, bundle: Path,
                                                          assembled: tuple[Path, Path]) -> None:
    """Nothing but `--ci` turns the dispatched version into a confirmation."""
    proc = _run(tmp_path, {}, "--bundle", str(bundle), "--source-images", str(assembled[1]),
                typed=None, extra_env=_actions())
    assert proc.returncode != 0
    assert _writes(_calls(tmp_path)) == []


@pytest.mark.parametrize("mode", ["promote", "record-test"])
def test_ci_never_promotes_or_records(tmp_path: Path, mode: str) -> None:
    """The stable channel and the test record stay a person's typed act."""
    record = tmp_path / "tested.json"
    record.write_text(_tested())
    args = ["--promote", VERSION] if mode == "promote" else ["--record-test", VERSION, "--record", str(record)]
    state = _published(**({f"{RELEASE}/TESTED": _tested()} if mode == "promote" else {}))
    proc = _run(tmp_path, state, *args, "--ci", typed=None, extra_env=_actions())
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "--ci" in proc.stderr, proc.stderr
    assert _calls(tmp_path) == []


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
