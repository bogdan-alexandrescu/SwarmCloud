"""`scripts/push-images.sh` promotes a release all-or-nothing.

THE DEFECT THIS PINS. Release run 35969538707 promoted seven images to `:dev`
and then refused the eighth: trivy found a fixable HIGH in swarm-ui's libexpat.
The script scanned and tagged ONE IMAGE AT A TIME, so six images were already
on the new build when swarm-ui was refused, and swarm-verify was moved after
it; swarm-ui's `:dev` stayed on the old one. The channel then described a
release that was never built as a release -- and deploy.sh's completeness
guard reads the channel to decide what "every image" means.

The next release, run 35972131246, mixed the channel a second way: the
digest lookup's `tags:<sha>` filter is a word match, so it also found
application.yml's `pr-<run>-<sha>` build of the same commit, and the script
took whichever came first -- five images from one build, three from the other.

The properties asserted here, against the REAL script with a fake `gcloud` and
a fake `trivy` on PATH, both of which keep their state in files so the test can
read what the registry ended up saying:

  * every image is resolved and scanned before ANY channel tag moves, and
    resolved to the build carrying EXACTLY the release's tag;
  * one refused scan, or one image that cannot be resolved, moves NO channel
    tag, writes NO promotion manifest, and names every image that failed;
  * a tag that fails to move part-way through (an API error, a lost session)
    is not left as a half-promoted channel: the tags already moved are put back
    to the digest they held before the run, and a tag that did not exist before
    the run is removed again;
  * when that put-back ITSELF fails -- an expired session fails the undo too,
    because the undo uses the same credential -- the red job's last line says
    the channel is MIXED and names the images left on the new build. It used
    to say "nothing promoted" while :dev held two new images and six old ones;
  * a move that failed because the session is dead is named as authentication.

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
import json, os, re, sys

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


# FAKE_SESSION_EXPIRES_AFTER=N: the first N tag writes succeed and every one
# after them fails as an expired session does -- the promotion's next move AND
# every put-back, because the undo uses the same credential. Counted from the
# event log, so it holds across the separate processes the script starts.
def session_expired():
    limit = os.environ.get("FAKE_SESSION_EXPIRES_AFTER")
    if limit is None:
        return False
    done = 0
    if os.path.exists(os.environ["FAKE_EVENTS"]):
        with open(os.environ["FAKE_EVENTS"]) as fh:
            done = sum(1 for line in fh if json.loads(line)["event"] in ("tag", "untag"))
    return done >= int(limit)


AUTH_ERROR = (
    "ERROR: (gcloud.artifacts.docker.tags.{verb}) There was a problem refreshing "
    "your current auth tokens: ('invalid_grant: Bad Request'). Request had invalid "
    "authentication credentials. UNAUTHENTICATED"
)


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
    # gcloud's `tags:X` is a WORD match, not equality: `tags:<sha>` also finds
    # `pr-<run>-<sha>`, and `tags:dev` finds `dev-old`. The fake matches the
    # same way, so a script that trusts the filter is caught here.
    rows = [
        {"package": args[4], "version": digest, "tags": tags}
        for digest, tags in registry.get(image, {}).items()
        if any(wanted in re.split(r"[^A-Za-z0-9]+", tag) for tag in tags)
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
    if session_expired():
        record(event="tag-failed", image=image, tag=tag, digest=digest)
        print(AUTH_ERROR.format(verb="add"), file=sys.stderr)
        sys.exit(1)
    if image in set(filter(None, os.environ.get("FAKE_TAG_FAIL", "").split(","))):
        record(event="tag-failed", image=image, tag=tag, digest=digest)
        if os.environ.get("FAKE_TAG_FAIL_AUTH"):
            print(AUTH_ERROR.format(verb="add"), file=sys.stderr)
        else:
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
    if session_expired():
        record(event="untag-failed", image=image, tag=tag)
        print(AUTH_ERROR.format(verb="delete"), file=sys.stderr)
        sys.exit(1)
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


def _pr_build(image: str) -> str:
    return f"sha256:{'2' * 56}{image[:8]:0<8}".replace("-", "0")


def _registry(no_previous: tuple[str, ...] = (), unbuilt: tuple[str, ...] = ()) -> dict:
    """Every image built at TAG; every image's :dev on an older digest, except
    those that have never been promoted. And, as on every push to main, a
    SECOND build of the same commit from application.yml, tagged
    `pr-<run>-<sha>` -- listed first, so a word-matched lookup finds it first."""
    registry: dict[str, dict[str, list[str]]] = {}
    for image in IMAGES:
        registry[image] = {_pr_build(image): [f"pr-99-{TAG}"]}
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

    # The build tagged exactly :TAG, not the pr-99-TAG build of the same commit
    # that a word-matched lookup finds first. On 2026-09-24 :dev held five
    # images from application.yml's build and three from the release's.
    wrong = {i: d for i, d in _channel(registry).items() if d != _new(i)}
    assert not wrong, (
        f":{CHANNEL} points at something other than the :{TAG} build for {sorted(wrong)}"
        f"{' -- the pr-99 build of the same commit' if any(d == _pr_build(i) for i, d in wrong.items()) else ''}"
    )
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


def test_an_undo_that_fails_says_the_channel_is_mixed_not_that_nothing_was_promoted(tmp_path):
    """The case the undo path exists for, carried one step further: the session
    expires between the swarm-scheduler and swarm-reconciler moves. The
    reconciler's move fails -- and so does every put-back, because the undo
    uses the same dead credential. :dev now holds two images from the new
    build and the rest from the old one.

    The job's last line used to read "...failed at swarm-reconciler; nothing
    promoted" regardless. A human reading the red job, or a `skip_build`
    redeploy that rebuilds its manifest from :dev, then trusts a mixed
    channel."""
    # swarm-scheduler has never been on :dev, so its put-back is a DELETE: both
    # undo paths meet the dead session.
    before = _registry(no_previous=("swarm-scheduler",))
    root, proc, registry, log = _run(tmp_path, before, FAKE_SESSION_EXPIRES_AFTER="2")
    assert proc.returncode != 0

    moved = [e["image"] for e in log if e["event"] == "tag"]
    assert moved == ["swarm-api", "swarm-scheduler"], (
        f"the fake did not produce the scenario (moved {moved}); the test is not exercising anything"
    )
    after = _channel(registry)
    stuck = sorted(i for i in IMAGES if after[i] != _channel(before)[i])
    assert stuck == ["swarm-api", "swarm-scheduler"], (
        f"expected the undo to fail for both moved images, the channel differs for {stuck}"
    )
    assert not (root / "build" / "deployed-images-dev.json").exists()

    summary = _summary(proc)
    assert "nothing promoted" not in summary, (
        f"the last line says nothing was promoted while :{CHANNEL} holds {stuck} "
        f"on the new build: {summary!r}"
    )
    assert "MIXED" in summary, f"the last line does not say :{CHANNEL} is mixed: {summary!r}"
    for image in stuck:
        assert image in summary, f"{image} is left on the new build but the last line omits it: {summary!r}"
    # On the LAST line, not anywhere in stderr: gcloud's own error text already
    # contains the word, and that is not the script naming the cause.
    assert "authentication" in summary, (
        f"every failure here was UNAUTHENTICATED and the last line does not say so: {summary!r}"
    )

    # The way back, for exactly the images that are stuck.
    repo = "us-central1-docker.pkg.dev/swarm-test-project/swarm-images"
    assert (
        f"gcloud artifacts docker tags add {repo}/swarm-api@{_old('swarm-api')} "
        f"{repo}/swarm-api:{CHANNEL}"
    ) in proc.stderr
    assert f"gcloud artifacts docker tags delete {repo}/swarm-scheduler:{CHANNEL}" in proc.stderr


def test_a_move_refused_by_a_dead_session_is_named_as_authentication(tmp_path):
    """The move fails UNAUTHENTICATED, the put-back succeeds (a session can come
    back -- or the undo is what finally refreshes it). "Nothing promoted" is
    then TRUE and must stay; what was missing is the reason. Phase 3 never
    looked at whether the failure was authentication, so a dead session read
    as a registry fault."""
    before = _registry()
    _, proc, registry, log = _run(
        tmp_path, before, FAKE_TAG_FAIL="swarm-reconciler", FAKE_TAG_FAIL_AUTH="1"
    )
    assert proc.returncode != 0
    assert [e["image"] for e in log if e["event"] == "tag-failed"] == ["swarm-reconciler"]
    assert _channel(registry) == _channel(before)

    summary = _summary(proc)
    assert "nothing promoted" in summary, summary
    assert "authentication" in summary, (
        f"the move failed UNAUTHENTICATED and the last line does not say so: {summary!r}"
    )
