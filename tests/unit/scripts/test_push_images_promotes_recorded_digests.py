"""`push-images.sh --manifest` promotes the digests a build RECORDED, and still
promotes them all or not at all.

WHY. The release no longer builds (owner decision, 2026-09-24; docs/ci.md). It
promotes the build application.yml made for its commit, from the manifest that
build recorded. Resolving `:<tag>` instead would promote whatever the tag
points at when the release runs -- and a tag moves whenever the same commit is
built again: a re-run of the build job, or a dispatched prod release, which
builds the commit for prod under the same tag. What is scanned and promoted
must be what the recorded build produced.

The properties asserted, against the real script with a fake `gcloud` and
`trivy` that keep their state in files:

  * the digests promoted -- and scanned -- are the recorded ones, even when
    `:<tag>` points at a later build of the same commit;
  * every recorded digest is confirmed in Artifact Registry BEFORE any channel
    tag moves, and one that is missing or unreadable moves no tag at all;
  * a manifest built for another environment is refused before anything is
    read or scanned: swarm-ui bakes its environment in at build time;
  * a manifest entry that names another registry is refused;
  * the scan gate is unchanged: one refusal promotes nothing.

WHAT THIS CANNOT PROVE: that `gcloud artifacts versions describe` behaves as
the fake does beyond the two outcomes measured on the live registry on
2026-09-24 (gcloud 483.0.0): a present version exits 0 printing its name, an
absent one exits 1 with "NOT_FOUND: Requested entity was not found".
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
REGISTRY = "us-central1-docker.pkg.dev/swarm-test-project/swarm-images"
TAG = "cafe00c0ffee"
IMAGES = ["swarm-api", "swarm-scheduler", "swarm-ui"]

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="push-images.sh needs bash and jq",
)

FAKE_GCLOUD = r'''#!{python}
import json, os, re, sys

args = sys.argv[1:]
state_path = os.environ["FAKE_REGISTRY"]
with open(state_path) as fh:
    registry = json.load(fh)


def record(**event):
    with open(os.environ["FAKE_EVENTS"], "a") as fh:
        fh.write(json.dumps(event) + "\n")


def flag(name):
    for i, arg in enumerate(args):
        if arg == name:
            return args[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


def base(ref):
    return ref.rsplit("/", 1)[-1]


if args[:3] == ["artifacts", "versions", "describe"]:
    digest, package = args[3], flag("--package")
    where = (flag("--location"), flag("--project"), flag("--repository"))
    record(event="describe", image=package, digest=digest, where=list(where))
    if package in set(filter(None, os.environ.get("FAKE_DESCRIBE_ERROR", "").split(","))):
        print("ERROR: (gcloud.artifacts.versions.describe) PERMISSION_DENIED: fake", file=sys.stderr)
        sys.exit(1)
    if where != ("us-central1", "swarm-test-project", "swarm-images") or digest not in registry.get(package, {}):
        print("ERROR: (gcloud.artifacts.versions.describe) NOT_FOUND: Requested entity was not found.", file=sys.stderr)
        sys.exit(1)
    print(f"projects/{where[1]}/locations/{where[0]}/repositories/{where[2]}/packages/{package}/versions/{digest}")
    sys.exit(0)

if args[:4] == ["artifacts", "docker", "images", "list"]:
    image = base(args[4])
    wanted = (flag("--filter") or "").split(":", 1)[-1]
    rows = [
        {"package": args[4], "version": digest, "tags": tags}
        for digest, tags in registry.get(image, {}).items()
        if any(wanted in re.split(r"[^A-Za-z0-9]+", tag) for tag in tags)
    ]
    print(json.dumps(rows))
    sys.exit(0)

if args[:4] == ["artifacts", "docker", "tags", "add"]:
    image, digest = base(args[4]).split("@", 1)
    _, tag = base(args[5]).split(":", 1)
    for tags in registry[image].values():
        if tag in tags:
            tags.remove(tag)
    registry[image].setdefault(digest, []).append(tag)
    with open(state_path, "w") as fh:
        json.dump(registry, fh)
    record(event="tag", image=image, tag=tag, digest=digest)
    sys.exit(0)

print("fake gcloud: unhandled " + " ".join(args), file=sys.stderr)
sys.exit(2)
'''

FAKE_TRIVY = r'''#!{python}
import json, os, sys

ref = sys.argv[-1]
image = ref.rsplit("/", 1)[-1].split("@", 1)[0]
with open(os.environ["FAKE_EVENTS"], "a") as fh:
    fh.write(json.dumps({"event": "scan", "image": image, "ref": ref}) + "\n")
if image in set(filter(None, os.environ.get("FAKE_TRIVY_FAIL", "").split(","))):
    print(f"{image}: CVE-0000-0000 HIGH fixed in 9.9.9")
    sys.exit(1)
sys.exit(0)
'''


def _d(build: str, image: str) -> str:
    return "sha256:" + hashlib.sha256(f"{build}/{image}".encode()).hexdigest()


def recorded(image: str) -> str:
    """The digest the build recorded."""
    return _d("recorded", image)


def rebuilt(image: str) -> str:
    """A later build of the same commit, which now carries :TAG."""
    return _d("rebuilt", image)


def previous(image: str) -> str:
    return _d("previous", image)


def _registry(missing: tuple[str, ...] = ()) -> dict:
    registry: dict[str, dict[str, list[str]]] = {}
    for image in IMAGES:
        registry[image] = {previous(image): ["dev"], rebuilt(image): [TAG]}
        if image not in missing:
            # Still in the registry, but no longer tagged: the tag moved on.
            registry[image][recorded(image)] = []
    return registry


def _manifest(environment: str = "dev", **override: dict) -> dict:
    images = []
    for image in IMAGES:
        entry = {
            "name": image,
            "image": f"{REGISTRY}/{image}",
            "tag": TAG,
            "digest": recorded(image),
            "ref": f"{REGISTRY}/{image}@{recorded(image)}",
        }
        entry.update(override.get(image, {}))
        images.append(entry)
    return {"tag": TAG, "commit": "c0ffee" * 6 + "abcd", "environment": environment, "images": images}


def _run(tmp_path: Path, registry: dict, manifest: dict, *, channel: str = "dev", **fakes: str):
    root = tmp_path / "repo"
    shutil.copytree(REPO / "scripts", root / "scripts")
    (root / "build").mkdir()
    manifest_path = root / "build" / "images-dev.json"
    manifest_path.write_text(json.dumps(manifest))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("gcloud", FAKE_GCLOUD), ("trivy", FAKE_TRIVY)):
        path = bindir / name
        path.write_text(body.replace("{python}", sys.executable))
        path.chmod(0o755)
    state = tmp_path / "registry.json"
    state.write_text(json.dumps(registry))
    events = tmp_path / "events.jsonl"
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "SWARM_ENV_FILE": str(tmp_path / "no-such.env"),
        "SWARM_TRIVY": str(bindir / "trivy"),
        "PROJECT_ID": "swarm-test-project",
        "REGION": "us-central1",
        "ENVIRONMENT": channel,
        "NO_COLOR": "1",
        "FAKE_REGISTRY": str(state),
        "FAKE_EVENTS": str(events),
        **fakes,
    }
    env.pop("GITHUB_ACTIONS", None)
    proc = subprocess.run(
        ["bash", str(root / "scripts" / "push-images.sh"),
         "--manifest", str(manifest_path), "--channel", channel, "--scan"],
        env=env, capture_output=True, text=True, timeout=120, check=False,
    )
    log = [json.loads(l) for l in events.read_text().splitlines() if l] if events.exists() else []
    return root, proc, json.loads(state.read_text()), log


def _channel(registry: dict, channel: str = "dev") -> dict[str, str | None]:
    out = {}
    for image in IMAGES:
        holders = [d for d, tags in registry[image].items() if channel in tags]
        assert len(holders) <= 1, f"{image}:{channel} is on several digests: {holders}"
        out[image] = holders[0] if holders else None
    return out


def _last_line(proc) -> str:
    lines = [l for l in proc.stderr.splitlines() if l.strip()]
    assert lines, "the script said nothing"
    return lines[-1]


def test_the_recorded_digests_are_scanned_and_promoted_not_what_the_tag_says_now(tmp_path):
    root, proc, registry, log = _run(tmp_path, _registry(), _manifest())
    assert proc.returncode == 0, proc.stderr[-3000:]

    scanned = {e["image"]: e["ref"] for e in log if e["event"] == "scan"}
    assert scanned == {i: f"{REGISTRY}/{i}@{recorded(i)}" for i in IMAGES}, (
        f"scanned {scanned}: not the digests the build recorded"
    )
    wrong = {i: d for i, d in _channel(registry).items() if d != recorded(i)}
    assert not wrong, (
        f":dev points at something other than the recorded build for {sorted(wrong)}"
        f"{' -- the later rebuild :' + TAG + ' carries' if any(d == rebuilt(i) for i, d in wrong.items()) else ''}"
    )
    deployed = json.loads((root / "build" / "deployed-images-dev.json").read_text())
    assert {e["name"]: e["digest"] for e in deployed["images"]} == {i: recorded(i) for i in IMAGES}
    assert deployed["tag"] == TAG

    kinds = [e["event"] for e in log]
    confirmed = [i for i, k in enumerate(kinds) if k == "describe"]
    moved = [i for i, k in enumerate(kinds) if k == "tag"]
    assert sorted(e["image"] for e in log if e["event"] == "describe") == sorted(IMAGES), (
        "not every recorded digest was confirmed in the registry"
    )
    assert max(confirmed) < min(moved), f"a tag moved before every digest was confirmed: {kinds}"


@pytest.mark.parametrize(
    "registry_kw, fakes, refused",
    [
        # Recorded, and since deleted (or recorded from somewhere else).
        ({"missing": ("swarm-scheduler",)}, {}, "swarm-scheduler"),
        # The registry would not say: unreadable is not absent, and refuses too.
        ({}, {"FAKE_DESCRIBE_ERROR": "swarm-ui"}, "swarm-ui"),
        # The scan gate, unchanged by where the digests came from.
        ({}, {"FAKE_TRIVY_FAIL": "swarm-api"}, "swarm-api"),
    ],
    ids=["digest-missing", "registry-unreadable", "scan-refused"],
)
def test_one_image_that_cannot_be_promoted_moves_no_channel_tag(tmp_path, registry_kw, fakes, refused):
    before = _registry(**registry_kw)
    root, proc, registry, log = _run(tmp_path, before, _manifest(), **fakes)
    assert proc.returncode != 0, f"{refused} could not be promoted and the script exited 0"
    moved = [e["image"] for e in log if e["event"] == "tag"]
    assert not moved, f"{moved} moved to :dev although {refused} was refused"
    assert _channel(registry) == _channel(before)
    assert not (root / "build" / "deployed-images-dev.json").exists()
    assert refused in _last_line(proc), f"the last line does not name {refused}: {_last_line(proc)!r}"


def test_a_manifest_built_for_another_environment_is_refused_before_anything_is_read(tmp_path):
    root, proc, registry, log = _run(tmp_path, _registry(), _manifest(environment="dev"), channel="prod")
    assert proc.returncode != 0, "a dev build was promoted to :prod"
    assert not log, f"the registry or the scanner was used before the refusal: {log}"
    summary = _last_line(proc)
    assert "dev" in summary and "prod" in summary, summary


def test_an_entry_recorded_in_another_registry_is_refused(tmp_path):
    elsewhere = "europe-west1-docker.pkg.dev/someone-else/images/swarm-ui"
    manifest = _manifest(**{"swarm-ui": {"image": elsewhere, "ref": f"{elsewhere}@{recorded('swarm-ui')}"}})
    before = _registry()
    _, proc, registry, log = _run(tmp_path, before, manifest)
    assert proc.returncode != 0
    assert not [e for e in log if e["event"] == "tag"]
    assert _channel(registry) == _channel(before)
    assert "swarm-ui" in _last_line(proc), _last_line(proc)
