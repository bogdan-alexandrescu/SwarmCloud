"""`scripts/push-images.sh` promotes a release all-or-nothing.

THE DEFECT THIS PINS. Release run 35969538707 promoted seven images to `:dev`
and then refused the eighth: trivy found a fixable HIGH in swarm-ui's libexpat.
The script scanned and tagged ONE IMAGE AT A TIME, so by the time swarm-ui was
refused, agent-runtime-base, agent-runtime-browser, swarm-api,
swarm-scheduler, swarm-quota-broker, swarm-reconciler and (after it) swarm-verify
already pointed `:dev` at the new build, while swarm-ui's `:dev` still pointed
at the old one. The channel then described a release that was never built as a
release -- and deploy.sh's completeness guard reads the channel to decide what
"every image" means.

The properties asserted here, against the REAL script with a fake `gcloud` and
a fake `trivy` on PATH, both of which keep their state in files so the test can
read what the registry ended up saying:

  * every image is resolved and scanned before ANY channel tag moves;
  * one refused scan, or one image that cannot be resolved, moves NO channel
    tag, writes NO promotion manifest, and names every image that failed;
  * a tag that fails to move part-way through (an API error, a lost session)
    is not left as a half-promoted channel: the tags already moved are put back
    to the digest they held before the run, and a tag that did not exist before
    the run is removed again.

WHAT THIS CANNOT PROVE: that Artifact Registry's real `images list` returns the
JSON shape the fake does. The shape was read from the live registry on
2026-09-24 (`gcloud artifacts docker images list ... --include-tags
--format=json`: a list of objects with `version` = `sha256:...` and `tags` = a
list of strings), and the fake serves exactly that.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
TAG = "cafe00c0ffee"
CHANNEL = "dev"
IMAGES = ["swarm-api", "swarm-scheduler", "swarm-reconciler", "swarm-ui"]

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="push-images.sh needs bash and jq",
)

# A registry in a JSON file: {"<image basename>": {"<digest>": ["tag", ...]}}.
FAKE_GCLOUD = r'''#!{python}
import json, os, sys

args = sys.argv[1:]
state_path = os.environ["FAKE_REGISTRY"]
with open(state_path) as fh:
    registry = json.load(fh)


def save():
    with open(state_path, "w") as fh:
        json.dump(registry, fh)


def record(**event):
    with open(os.environ["FAKE_EVENTS"], "a") as fh:
        fh.write(json.dumps(event) + "\n")


def base(ref):
    return ref.rsplit("/", 1)[-1]


def flag(name):
    for i, arg in enumerate(args):
        if arg == name:
            return args[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


if args[:4] == ["artifacts", "docker", "images", "list"]:
    image = base(args[4])
    wanted = (flag("--filter") or "").split(":", 1)[-1]
    fmt = flag("--format") or ""
    print("Listing items under project p, location r, repository swarm-images.", file=sys.stderr)
    if image in set(filter(None, os.environ.get("FAKE_LIST_ERROR", "").split(","))):
        print("ERROR: (gcloud.artifacts.docker.images.list) PERMISSION_DENIED: fake", file=sys.stderr)
        sys.exit(1)
    rows = [
        {"package": args[4], "version": digest, "tags": tags}
        for digest, tags in registry.get(image, {}).items()
        if wanted in tags
    ]
    if fmt == "json":
        print(json.dumps(rows))
    else:
        for row in rows:
            print(row["version"])
    sys.exit(0)

if args[:4] == ["artifacts", "docker", "tags", "add"]:
    src, dst = args[4], args[5]
    image, digest = base(src).split("@", 1)
    image_dst, tag = base(dst).split(":", 1)
    assert image == image_dst, (src, dst)
    if image in set(filter(None, os.environ.get("FAKE_TAG_FAIL", "").split(","))):
        record(event="tag-failed", image=image, tag=tag, digest=digest)
        print(f"ERROR: (gcloud.artifacts.docker.tags.add) fake failure for {image}", file=sys.stderr)
        sys.exit(1)
    for tags in registry[image].values():
        if tag in tags:
            tags.remove(tag)
    registry[image].setdefault(digest, []).append(tag)
    save()
    record(event="tag", image=image, tag=tag, digest=digest)
    sys.exit(0)

if args[:4] == ["artifacts", "docker", "tags", "delete"]:
    image, tag = base(args[4]).split(":", 1)
    for tags in registry[image].values():
        if tag in tags:
            tags.remove(tag)
    save()
    record(event="untag", image=image, tag=tag)
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
    print(f"{image}: libexpat CVE-0000-0000 HIGH fixed in 9.9.9")
    sys.exit(1)
sys.exit(0)
'''


def _new(image: str) -> str:
    return f"sha256:{'1' * 56}{image[:8]:0<8}".replace("-", "0")


def _old(image: str) -> str:
    return f"sha256:{'0' * 56}{image[:8]:0<8}".replace("-", "0")


def _registry(no_previous: tuple[str, ...] = (), unbuilt: tuple[str, ...] = ()) -> dict:
    """Every image built at TAG; every image's :dev on an older digest, except
    those that have never been promoted."""
    registry: dict[str, dict[str, list[str]]] = {}
    for image in IMAGES:
        registry[image] = {}
        if image not in unbuilt:
            registry[image][_new(image)] = [TAG]
        if image not in no_previous:
            registry[image][_old(image)] = [CHANNEL]
    return registry


def _channel(registry: dict) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for image in IMAGES:
        holders = [d for d, tags in registry[image].items() if CHANNEL in tags]
        assert len(holders) <= 1, f"{image}:{CHANNEL} is on several digests: {holders}"
        out[image] = holders[0] if holders else None
    return out


def _run(tmp_path: Path, registry: dict, **fakes: str):
    root = tmp_path / "repo"
    shutil.copytree(REPO / "scripts", root / "scripts")
    (root / "build").mkdir()
    # The build manifest, as build-images.sh leaves it for the release job.
    (root / "build" / "images-dev.json").write_text(
        json.dumps({"tag": TAG, "environment": "dev", "images": [{"name": n} for n in IMAGES]})
    )
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
        "ENVIRONMENT": "dev",
        "NO_COLOR": "1",
        "FAKE_REGISTRY": str(state),
        "FAKE_EVENTS": str(events),
        **fakes,
    }
    proc = subprocess.run(
        ["bash", str(root / "scripts" / "push-images.sh"), "--tag", TAG, "--channel", CHANNEL, "--scan"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    log = [json.loads(l) for l in events.read_text().splitlines() if l] if events.exists() else []
    return root, proc, json.loads(state.read_text()), log


def _summary(proc: subprocess.CompletedProcess) -> str:
    lines = [line for line in proc.stderr.splitlines() if line.strip()]
    assert lines, "the script said nothing at all on failure"
    return lines[-1]


def test_every_image_is_scanned_before_any_channel_tag_moves(tmp_path):
    root, proc, registry, log = _run(tmp_path, _registry())
    assert proc.returncode == 0, proc.stderr[-3000:]

    kinds = [e["event"] for e in log]
    scans = [i for i, k in enumerate(kinds) if k == "scan"]
    tags = [i for i, k in enumerate(kinds) if k == "tag"]
    assert sorted(e["image"] for e in log if e["event"] == "scan") == sorted(IMAGES)
    assert tags, "a clean release moved no channel tag"
    assert max(scans) < min(tags), (
        f"a channel tag moved before every image had been scanned (order: {kinds}); "
        "a refusal later in the loop then leaves the channel half-promoted"
    )

    assert _channel(registry) == {i: _new(i) for i in IMAGES}
    manifest = json.loads((root / "build" / "deployed-images-dev.json").read_text())
    assert {e["name"]: e["digest"] for e in manifest["images"]} == {i: _new(i) for i in IMAGES}


@pytest.mark.parametrize(
    "fakes, refused",
    [
        # The run that motivated this: the LAST image refused, after the rest
        # had already been promoted. Two refusals, so "names every one" is real.
        ({"FAKE_TRIVY_FAIL": "swarm-api,swarm-ui"}, ["swarm-api", "swarm-ui"]),
        # Not a scan at all: one image's digest cannot be read.
        ({"FAKE_LIST_ERROR": "swarm-reconciler"}, ["swarm-reconciler"]),
    ],
    ids=["scan-refused", "digest-unreadable"],
)
def test_one_refused_image_moves_no_channel_tag(tmp_path, fakes, refused):
    before = _registry()
    root, proc, registry, log = _run(tmp_path, before, **fakes)
    assert proc.returncode != 0, "a refused image and the script exited 0"

    moved = [e for e in log if e["event"] == "tag"]
    assert not moved, (
        f"{len(moved)} channel tag(s) moved although {refused} was refused: "
        f"{[e['image'] for e in moved]} -- :{CHANNEL} now describes a mixed release"
    )
    assert _channel(registry) == _channel(before), "the channel changed"
    assert not (root / "build" / "deployed-images-dev.json").exists(), (
        "a promotion manifest was written for a release that was not promoted"
    )

    if "FAKE_TRIVY_FAIL" in fakes:
        scanned = sorted(e["image"] for e in log if e["event"] == "scan")
        assert scanned == sorted(IMAGES), (
            f"scanning stopped early ({scanned}); every refusal should surface in one run"
        )
    summary = _summary(proc)
    for image in refused:
        assert image in summary, f"{image} was refused but the summary omits it: {summary!r}"


def test_a_tag_that_fails_to_move_puts_back_the_ones_that_did(tmp_path):
    # swarm-scheduler has never been on :dev, so undoing it means REMOVING the
    # tag, not moving it back. swarm-reconciler is where the API fails.
    before = _registry(no_previous=("swarm-scheduler",))
    root, proc, registry, log = _run(tmp_path, before, FAKE_TAG_FAIL="swarm-reconciler")
    assert proc.returncode != 0

    assert [e["image"] for e in log if e["event"] == "tag-failed"] == ["swarm-reconciler"], (
        "the fake never saw the failing tag move; the test is not exercising anything"
    )
    assert _channel(registry) == _channel(before), (
        f"after a failed promotion :{CHANNEL} reads {_channel(registry)}, "
        f"not what it read before the run ({_channel(before)}): a mixed release"
    )
    assert not (root / "build" / "deployed-images-dev.json").exists()
    assert "swarm-reconciler" in _summary(proc)
