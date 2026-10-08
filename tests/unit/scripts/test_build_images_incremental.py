"""Main rebuilds only the images a commit changed, and re-tags the previous
digests of the rest (owner decision 2026-10-08, observer proposal H).

THE COST THIS REMOVES. Measured 2026-10-08: application.yml's `build images`
rebuilt all 9 images on every push to main, 9.0-10.4 min (agent-runtime-base
5m04, then agent-runtime-browser 3m21; a service image 2m09-2m42), and a
uv.lock change alone reached 7 of 9. `scripts/build-images.sh --incremental
PREV` now rebuilds an image only when `--affected-by`'s closure reaches it
from `git diff <the commit its previous digest was built from> HEAD`, and adds
this commit's tag to every other image's previous digest -- so the manifest
still names all 9 digests, each carrying this commit's tag, and nothing that
reads the manifest changes.

The properties asserted here, against the REAL script in a git sandbox, with a
fake `gcloud` on PATH that remembers which tag points at which digest:

  * an image whose inputs changed is rebuilt; every other one is re-tagged,
    never submitted, and recorded with `reused` and the `built_from` commit of
    the build that made its digest;
  * a change to agent-runtime-base rebuilds the two images built FROM it, and
    a base whose re-tag fails is rebuilt together with them;
  * a reused image is diffed from the commit its digest was BUILT from, not
    from the previous record's commit, so a chain of reuses cannot hide a
    change made two builds ago;
  * uv.lock: a Python image is rebuilt only when its own pruned `uv export`
    exports something different at the two commits;
  * every image is rebuilt when build logic changed, there is no ancestor
    record, the record is of a commit that is not an ancestor, a reused digest
    is older than seven days, --full-build is given, or the environment is
    prod;
  * application.yml asks for the previous record, builds incrementally, can
    be told to build everything, and has the history and the permission it
    needs.

WHAT THIS CANNOT PROVE: that Artifact Registry's `docker tags add` behaves as
the fake does (a tag moved to the named digest). It is the command
push-images.sh already uses to move a channel tag.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from .test_ci_built_images import IMAGES, REGISTRY

REPO = Path(__file__).resolve().parents[3]
WORKFLOW = REPO / ".github" / "workflows" / "application.yml"
WORKER = {"agent-runtime-base", "agent-runtime-browser", "agent-runtime-indexer"}
PYTHON_SERVICES = {"swarm-api", "swarm-scheduler", "swarm-quota-broker", "swarm-reconciler"}

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None or shutil.which("git") is None,
    reason="build-images.sh needs bash, jq and git",
)

# A gcloud that remembers tags. A build makes a digest from the image and the
# tag; `tags add` points a tag at the digest named; `tags list` answers from
# what it remembers, else from the build.
FAKE_GCLOUD = r'''#!{python}
import hashlib, json, os, re, sys

args = sys.argv[1:]
state_path = os.environ["FAKE_TAGS"]
state = json.load(open(state_path)) if os.path.exists(state_path) else {}


def record(**event):
    with open(os.environ["FAKE_EVENTS"], "a") as fh:
        fh.write(json.dumps(event) + "\n")


if args[:3] == ["artifacts", "repositories", "describe"]:
    print("projects/p/locations/r/repositories/swarm-images")
    sys.exit(0)
if args[:2] == ["builds", "submit"]:
    config = args[args.index("--config") + 1]
    match = re.search(r"cloudbuild-(.+)\.yaml$", config) or re.search(r"/images/([^/]+)/cloudbuild\.yaml$", config)
    record(event="builds-submit", target=match.group(1))
    sys.exit(0)
if args[:4] == ["artifacts", "docker", "tags", "add"]:
    src, dst = args[4], args[5]
    image, digest = src.split("@", 1)
    name = image.rsplit("/", 1)[-1]
    if name in os.environ.get("FAKE_TAG_FAIL", "").split(","):
        record(event="tags-add-failed", target=name)
        print("ERROR: (gcloud.artifacts.docker.tags.add) NOT_FOUND: Requested entity was not found.", file=sys.stderr)
        sys.exit(1)
    tag = dst.rsplit(":", 1)[1]
    state.setdefault(image, {})[tag] = digest
    json.dump(state, open(state_path, "w"))
    record(event="tags-add", target=name, digest=digest, tag=tag)
    sys.exit(0)
if args[:4] == ["artifacts", "docker", "tags", "list"]:
    image = args[4]
    wanted = next(a for a in args if a.startswith("--filter=")).split(":", 1)[1]
    base = f"projects/p/locations/r/repositories/swarm-images/packages/{image.rsplit('/', 1)[-1]}"
    digest = state.get(image, {}).get(wanted) or "sha256:" + hashlib.sha256(f"built {image}:{wanted}".encode()).hexdigest()
    print(json.dumps([{"image": image, "tag": f"{base}/tags/{wanted}", "version": f"{base}/versions/{digest}"}]))
    sys.exit(0)
record(event="gcloud-unhandled", args=args)
print("fake gcloud: unhandled " + " ".join(args), file=sys.stderr)
sys.exit(2)
'''


def _old_digest(name: str, commit: str) -> str:
    return "sha256:" + hashlib.sha256(f"previous {name} {commit}".encode()).hexdigest()


def _iso(seconds_ago: float = 3600) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


class Sandbox:
    """A git repository holding what build-images.sh reads, one commit deep to
    start with, and a fake gcloud."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.root = tmp_path / "repo"
        shutil.copytree(REPO / "scripts", self.root / "scripts")
        shutil.copytree(REPO / "images", self.root / "images")
        for name in (".gitignore", ".gcloudignore", ".dockerignore", "pyproject.toml", "uv.lock"):
            if (REPO / name).exists():
                shutil.copy2(REPO / name, self.root / name)
        (self.root / ".github" / "workflows").mkdir(parents=True)
        shutil.copy2(WORKFLOW, self.root / ".github" / "workflows" / "application.yml")
        (self.root / "apps" / "swarm-ui" / "src").mkdir(parents=True)
        (self.root / "apps" / "swarm-ui" / "src" / "App.tsx").write_text("export {};\n")
        self.git("init", "-q")
        self.first = self.commit("sandbox")

        bindir = tmp_path / "bin"
        bindir.mkdir()
        gcloud = bindir / "gcloud"
        gcloud.write_text(FAKE_GCLOUD.replace("{python}", sys.executable))
        gcloud.chmod(0o755)
        home = tmp_path / "home"
        home.mkdir()
        self.env = {
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(home),
            "SWARM_ENV_FILE": str(tmp_path / "no-such.env"),
            "PROJECT_ID": "swarm-test-project",
            "REGION": "us-central1",
            "ENVIRONMENT": "dev",
            "NO_COLOR": "1",
            "FAKE_EVENTS": str(tmp_path / "events.jsonl"),
            "FAKE_TAGS": str(tmp_path / "tags.json"),
            "BUILD_PARALLELISM": "8",
            "BUILD_POLL_INTERVAL": "0.05",
            "BUILD_SUBMIT_STAGGER": "0",
        }
        for leak in ("GITHUB_ACTIONS", "GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT", "GH_TOKEN", "GITHUB_TOKEN"):
            self.env.pop(leak, None)

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
             "-c", "commit.gpgsign=false", *args],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    def commit(self, message: str, changes: dict[str, str] | None = None) -> str:
        for path, content in (changes or {}).items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def append(self, path: str, text: str = "\n# touched\n") -> dict[str, str]:
        return {path: (self.root / path).read_text() + text}

    def record(self, commit: str, *, environment: str = "dev", built_at: str | None = None,
               per_image: dict[str, dict] | None = None) -> Path:
        """What build-images.sh wrote for `commit`, as --previous fetches it."""
        images = []
        for name in IMAGES:
            entry = {
                "name": name,
                "image": f"{REGISTRY}/{name}",
                "tag": commit[:12],
                "digest": _old_digest(name, commit),
                "ref": f"{REGISTRY}/{name}@{_old_digest(name, commit)}",
            }
            entry.update((per_image or {}).get(name, {}))
            images.append(entry)
        path = self.tmp / f"previous-{commit[:12]}.json"
        path.write_text(json.dumps({
            "tag": commit[:12], "commit": commit, "built_at": built_at or _iso(),
            "environment": environment, "images": images,
        }))
        return path

    def build(self, previous: Path | str, *extra: str, **env: str):
        (self.tmp / "events.jsonl").unlink(missing_ok=True)
        proc = subprocess.run(
            ["bash", str(self.root / "scripts" / "build-images.sh"), "--incremental", str(previous), *extra],
            env={**self.env, **env}, capture_output=True, text=True, timeout=300, check=False,
        )
        path = self.tmp / "events.jsonl"
        events = [json.loads(l) for l in path.read_text().splitlines() if l] if path.exists() else []
        manifest_path = self.root / "build" / f"images-{env.get('ENVIRONMENT', 'dev')}.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
        return proc, manifest, events


def _submitted(events: list[dict]) -> set[str]:
    return {e["target"] for e in events if e["event"] == "builds-submit"}


def _retagged(events: list[dict]) -> set[str]:
    return {e["target"] for e in events if e["event"] == "tags-add"}


def _assert_whole(manifest: dict, head: str) -> None:
    """The manifest names every image once, each at this commit's tag."""
    assert manifest is not None, "no manifest was written"
    assert manifest["commit"] == head
    assert [i["name"] for i in manifest["images"]] == IMAGES, "the manifest does not name all 9 images in order"
    for image in manifest["images"]:
        assert image["tag"] == head[:12], image
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", image["digest"]), image
        assert image["ref"] == f"{image['image']}@{image['digest']}", image


@pytest.fixture
def box(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


# ---------------------------------------------------------------------------
# What is rebuilt, and what is reused.
# ---------------------------------------------------------------------------
def test_an_affected_image_is_rebuilt_and_every_other_is_retagged(box):
    head = box.commit("ui", box.append("apps/swarm-ui/src/App.tsx", "export const x = 1;\n"))
    proc, manifest, events = box.build(box.record(box.first))
    assert proc.returncode == 0, proc.stderr[-4000:]

    assert _submitted(events) == {"swarm-ui"}, f"submitted {_submitted(events)}"
    assert _retagged(events) == set(IMAGES) - {"swarm-ui"}
    for event in events:
        if event["event"] == "tags-add":
            assert event["tag"] == head[:12]
            assert event["digest"] == _old_digest(event["target"], box.first), event
    _assert_whole(manifest, head)
    by_name = {i["name"]: i for i in manifest["images"]}
    assert by_name["swarm-ui"]["reused"] is False and by_name["swarm-ui"]["built_from"] == head
    for name in set(IMAGES) - {"swarm-ui"}:
        assert by_name[name]["reused"] is True, by_name[name]
        assert by_name[name]["built_from"] == box.first, by_name[name]
        assert by_name[name]["digest"] == _old_digest(name, box.first)
    assert manifest["incremental"] == {"previous": box.first, "full_build": None}


def test_a_commit_that_changes_no_image_input_builds_nothing_and_still_names_all_nine(box):
    head = box.commit("docs", {"docs/ci.md": "words\n"})
    proc, manifest, events = box.build(box.record(box.first))
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert _submitted(events) == set()
    assert _retagged(events) == set(IMAGES)
    _assert_whole(manifest, head)
    assert all(i["reused"] for i in manifest["images"])


def test_a_base_change_rebuilds_the_images_built_from_it(box):
    head = box.commit("base", box.append("images/agent-runtime-base/Dockerfile"))
    proc, manifest, events = box.build(box.record(box.first))
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert _submitted(events) == WORKER
    assert _retagged(events) == set(IMAGES) - WORKER
    _assert_whole(manifest, head)


def test_a_base_whose_retag_fails_is_rebuilt_with_everything_built_from_it(box):
    head = box.commit("docs", {"docs/ci.md": "words\n"})
    proc, manifest, events = box.build(box.record(box.first), FAKE_TAG_FAIL="agent-runtime-base")
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert _submitted(events) == WORKER, (
        "the base could not be re-tagged, so the images built FROM it must be built on a fresh base"
    )
    assert _retagged(events) == set(IMAGES) - WORKER
    _assert_whole(manifest, head)


def test_a_reused_image_is_diffed_from_the_commit_its_digest_was_built_from(box):
    """swarm-ui changed in `middle`, but the previous record (of `middle`)
    reused swarm-ui from `first`. Diffing from the record's commit would see no
    swarm-ui change and keep a digest built before it."""
    middle = box.commit("ui", box.append("apps/swarm-ui/src/App.tsx", "export const y = 2;\n"))
    head = box.commit("docs", {"docs/ci.md": "words\n"})
    previous = box.record(middle, per_image={
        "swarm-ui": {"built_from": box.first, "built_at": _iso(), "reused": True,
                     "digest": _old_digest("swarm-ui", box.first),
                     "ref": f"{REGISTRY}/swarm-ui@{_old_digest('swarm-ui', box.first)}"},
    })
    proc, manifest, events = box.build(previous)
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert _submitted(events) == {"swarm-ui"}
    _assert_whole(manifest, head)


# ---------------------------------------------------------------------------
# uv.lock, narrowed to each image's own export.
# ---------------------------------------------------------------------------
needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="uv.lock precision needs uv")


def _lock_with(box: Sandbox, package: str, rewrite) -> dict[str, str]:
    lock = (box.root / "uv.lock").read_text()
    blocks = lock.split("\n[[package]]\n")
    hit = [i for i, b in enumerate(blocks) if b.startswith(f'name = "{package}"\n')]
    assert len(hit) == 1, f"uv.lock has {len(hit)} blocks for {package}"
    blocks[hit[0]] = rewrite(blocks[hit[0]])
    return {"uv.lock": "\n[[package]]\n".join(blocks)}


@needs_uv
def test_a_lock_change_no_image_exports_rebuilds_no_python_image(box):
    # swarm-mcp is pruned from every image's export, so its version is in none.
    head = box.commit("lock", _lock_with(
        box, "swarm-mcp", lambda b: re.sub(r'^version = "[^"]+"', 'version = "9.9.9"', b, count=1, flags=re.M)))
    proc, manifest, events = box.build(box.record(box.first))
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert _submitted(events) == set(), (
        f"submitted {_submitted(events)} for a uv.lock change no image's own export reads"
    )
    _assert_whole(manifest, head)


@needs_uv
def test_a_lock_change_rebuilds_only_the_images_whose_export_it_changes(box):
    # google-cloud-monitoring is in agent-runtime-base's export and in no
    # service's (measured 2026-10-08 by diffing the exports).
    replacement = hashlib.sha256(b"another wheel").hexdigest()
    head = box.commit("lock", _lock_with(
        box, "google-cloud-monitoring",
        lambda b: re.sub(r"sha256:[0-9a-f]{64}", "sha256:" + replacement, b, count=1)))
    proc, manifest, events = box.build(box.record(box.first))
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert _submitted(events) == WORKER, f"submitted {_submitted(events)}"
    _assert_whole(manifest, head)


def test_without_uv_a_lock_change_rebuilds_every_image_that_takes_the_lock(box, tmp_path):
    # PATH with the fake gcloud and the tools the script needs, and no uv.
    tools = tmp_path / "tools"
    tools.mkdir()
    for tool in ("bash", "jq", "git", "awk", "sed", "grep", "head", "tail", "wc", "tr", "cut", "uniq",
                 "paste", "sort", "mktemp", "rm", "mv", "mkdir", "cat", "date", "dirname", "basename",
                 "find", "tee", "env", "sleep", "kill", "pkill", "python3", "readlink", "cp", "ls", "id"):
        found = shutil.which(tool)
        if found:
            (tools / tool).symlink_to(found)
    head = box.commit("lock", box.append("uv.lock", "\n"))
    proc, manifest, events = box.build(
        box.record(box.first), PATH=f"{tmp_path / 'bin'}{os.pathsep}{tools}")
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert _submitted(events) == PYTHON_SERVICES | WORKER
    _assert_whole(manifest, head)


# ---------------------------------------------------------------------------
# A full rebuild, and why.
# ---------------------------------------------------------------------------
FULL = {
    "build-images.sh changed": lambda box: (box.commit("x", box.append("scripts/build-images.sh")), {}),
    "common.sh changed": lambda box: (box.commit("x", box.append("scripts/lib/common.sh")), {}),
    "ci-built-images.sh changed": lambda box: (box.commit("x", box.append("scripts/lib/ci-built-images.sh")), {}),
    "application.yml changed": lambda box: (box.commit("x", box.append(".github/workflows/application.yml")), {}),
    "no previous record": lambda box: (box.commit("x", {"docs/ci.md": "words\n"}), {"previous": "none"}),
    "previous record missing": lambda box: (
        box.commit("x", {"docs/ci.md": "words\n"}), {"previous": str(box.tmp / "nothing.json")}),
    "full_build requested": lambda box: (
        box.commit("x", {"docs/ci.md": "words\n"}), {"extra": ["--full-build", "the full_build input was set"]}),
    "prod": lambda box: (box.commit("x", {"docs/ci.md": "words\n"}), {"env": {"ENVIRONMENT": "prod"},
                                                                    "record": {"environment": "prod"}}),
    "a digest older than seven days": lambda box: (
        box.commit("x", {"docs/ci.md": "words\n"}), {"record": {"built_at": _iso(8 * 86400)}}),
    "a reused digest older than seven days": lambda box: (
        box.commit("x", {"docs/ci.md": "words\n"}),
        {"record": {"per_image": {"swarm-api": {"built_at": _iso(8 * 86400)}}}}),
}

FULL_SAYS = {
    "build-images.sh changed": "scripts/build-images.sh changed",
    "common.sh changed": "scripts/lib/common.sh changed",
    "ci-built-images.sh changed": "scripts/lib/ci-built-images.sh changed",
    "application.yml changed": ".github/workflows/application.yml changed",
    "no previous record": "no previous build",
    "previous record missing": "no previous build",
    "full_build requested": "full_build input",
    "prod": "prod",
    "a digest older than seven days": "more than 7 days",
    "a reused digest older than seven days": "more than 7 days",
}


@pytest.mark.parametrize("case", sorted(FULL))
def test_every_image_is_rebuilt_when(box, case):
    head, how = FULL[case](box)
    previous = how.get("previous") or box.record(box.first, **how.get("record", {}))
    proc, manifest, events = box.build(previous, *how.get("extra", []), **how.get("env", {}))
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert _submitted(events) == set(IMAGES), f"{case}: submitted only {_submitted(events)}"
    assert _retagged(events) == set(), f"{case}: re-tagged {_retagged(events)}"
    _assert_whole(manifest, head)
    assert not any(i["reused"] for i in manifest["images"])
    assert FULL_SAYS[case] in (manifest["incremental"]["full_build"] or ""), manifest["incremental"]


def test_a_record_of_a_commit_that_is_not_an_ancestor_rebuilds_everything(box):
    box.git("checkout", "-q", "-b", "elsewhere")
    stranger = box.commit("elsewhere", {"docs/other.md": "x\n"})
    box.git("checkout", "-q", "-")
    head = box.commit("x", {"docs/ci.md": "words\n"})
    proc, manifest, events = box.build(box.record(stranger))
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert _submitted(events) == set(IMAGES)
    assert "not an ancestor" in manifest["incremental"]["full_build"]
    _assert_whole(manifest, head)


@pytest.mark.parametrize("extra", [["swarm-api"], ["--reuse-ci", "only"], ["--build-only"], ["--async"]])
def test_incremental_is_the_whole_build_and_takes_nothing_that_narrows_it(box, extra):
    proc, manifest, events = box.build(box.record(box.first), *extra)
    assert proc.returncode != 0
    assert "--incremental" in proc.stderr
    assert _submitted(events) == set() and manifest is None


def test_full_build_without_incremental_is_refused(box):
    proc = subprocess.run(
        ["bash", str(box.root / "scripts" / "build-images.sh"), "--full-build", "why"],
        env=box.env, capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode != 0 and "--full-build" in proc.stderr


# ---------------------------------------------------------------------------
# application.yml's wiring.
# ---------------------------------------------------------------------------
def _workflow() -> dict:
    data = yaml.safe_load(WORKFLOW.read_text())
    if True in data:
        data["on"] = data.pop(True)
    return data


def test_application_builds_incrementally_and_can_be_told_to_build_everything():
    workflow = _workflow()
    build = workflow["jobs"]["build"]
    assert build["permissions"].get("actions") == "read", "the build job cannot read its earlier runs"
    checkout = next(s for s in build["steps"] if str(s.get("uses", "")).startswith("actions/checkout@"))
    assert (checkout.get("with") or {}).get("fetch-depth") == 0, (
        "a one-commit clone can neither diff from a previous build nor tell an ancestor"
    )
    assert any(str(s.get("uses", "")).startswith("astral-sh/setup-uv@") for s in build["steps"]), (
        "without uv every uv.lock change rebuilds every Python image"
    )
    step = next(s for s in build["steps"] if "scripts/build-images.sh" in str(s.get("run", "")))
    run = step["run"]
    assert "ci-built-images.sh --previous" in run, run
    assert "build-images.sh --incremental" in run, run
    assert "--full-build" in run and "FULL_BUILD" in run, run
    assert (step.get("env") or {}).get("GH_TOKEN"), "the previous record is read with gh, which needs a token"
    dispatch = workflow["on"]["workflow_dispatch"]["inputs"]["full_build"]
    assert dispatch["type"] == "boolean" and dispatch["default"] is False
    summary = [s for s in build["steps"] if "GITHUB_STEP_SUMMARY" in str(s.get("run", ""))]
    assert summary and "reused" in summary[0]["run"], "the job no longer says which images were rebuilt"
