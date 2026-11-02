"""A release builds every image whose inputs changed since the digest it would
promote (lane REL-SKIP, part of #1194; owner-approved 2026-10-12).

MEASURED. Release run 38273482069, on main commit f816e09b (2026-10-12,
apps/agent-worker/agent_worker/livegate/**, which reaches agent-runtime-base,
-browser and -indexer), built nothing and then failed. Nothing release-side
asked whether the digests it would promote matched the commit: `build-images.sh
--reuse-ci` took application.yml's record whole, and that record is an
INCREMENTAL build -- most of its digests re-tagged from earlier commits, each
naming the `built_from` commit that made it. Whatever decides "changed" there
decides what the release ships.

THE RULE HELD HERE. A digest is promotable for commit C only if C has not
changed that image's inputs since the digest's own `built_from` commit. That
compares what is promoted with what made it -- not with HEAD~1, not with the
previous release -- so a release that failed, was cancelled or was superseded
in between can never make a later one skip an image:

  * N changed swarm-ui and N's release never shipped; N+1 changed only docs.
    If N+1's record offers swarm-ui's digest from BEFORE N, the release builds
    swarm-ui, and only swarm-ui. If it offers N's digest, the release promotes
    N's digest and builds nothing.
  * the whole chain: N's CI build failed, so N+1's CI build diffs from the
    record before N, rebuilds swarm-ui, and the release promotes that record
    untouched;
  * a stale agent-runtime-base rebuilds the two images built FROM it;
  * a docs-only push builds nothing, and the release promotes CI's record as
    fetched.

The real build-images.sh runs in a git sandbox, with a fake `gh` serving CI's
record and a fake `gcloud` that remembers which tag names which digest.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from .test_build_images_incremental import (
    Sandbox,
    _iso,
    _old_digest,
    _retagged,
    _submitted,
)
from .test_ci_built_images import FAKE_GH, IMAGES, OWNER_REPO, REGISTRY, ci_run

# agent-runtime-lightpanda (LIGHTPANDA-BUILD-SCRIPTS) is built FROM the base by digest.
WORKER = {"agent-runtime-base", "agent-runtime-base-nocodex", "agent-runtime-browser", "agent-runtime-indexer",
          "agent-runtime-lightpanda"}


class Release(Sandbox):
    """The sandbox, plus CI's runs as a fake `gh` reads them."""

    def __init__(self, tmp_path: Path):
        super().__init__(tmp_path)
        gh = tmp_path / "bin" / "gh"
        gh.write_text(FAKE_GH.replace("{python}", sys.executable))
        gh.chmod(0o755)
        self.env.update({
            "GITHUB_REPOSITORY": OWNER_REPO,
            "FAKE_WORLD": str(tmp_path / "world.json"),
            "FAKE_COUNTS": str(tmp_path / "counts.json"),
            "CI_BUILD_POLL": "0.05",
            "CI_BUILD_WAIT": "30",
            "CI_BUILD_APPEAR": "1",
            "CI_BUILD_API_TRIES": "3",
        })

    def reused_from(self, name: str, commit: str) -> dict:
        """A record entry for `name` that CI re-tagged from `commit`'s build."""
        digest = _old_digest(name, commit)
        return {"built_from": commit, "built_at": _iso(), "reused": True,
                "digest": digest, "ref": f"{REGISTRY}/{name}@{digest}"}

    def release(self, record: dict, mode: str = "only"):
        """`build-images.sh --reuse-ci MODE` at HEAD, CI's run of HEAD holding `record`."""
        head = self.git("rev-parse", "HEAD")
        (self.tmp / "world.json").write_text(json.dumps({"runs": [ci_run(901, sha=head, record=record)]}))
        for name in ("events.jsonl", "counts.json"):
            (self.tmp / name).unlink(missing_ok=True)
        manifest_path = self.root / "build" / "images-dev.json"
        manifest_path.unlink(missing_ok=True)
        proc = subprocess.run(
            ["bash", str(self.root / "scripts" / "build-images.sh"), "--reuse-ci", mode],
            env=self.env, capture_output=True, text=True, timeout=300, check=False,
        )
        path = self.tmp / "events.jsonl"
        events = [json.loads(l) for l in path.read_text().splitlines() if l] if path.exists() else []
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
        return proc, manifest, events


@pytest.fixture
def box(tmp_path: Path) -> Release:
    return Release(tmp_path)


def _by_name(manifest: dict) -> dict[str, dict]:
    return {i["name"]: i for i in manifest["images"]}


@pytest.mark.parametrize("mode", ["only", "or-build"])
def test_a_digest_from_before_a_failed_release_s_change_is_rebuilt_not_promoted(box, mode):
    """N changed swarm-ui and its release failed or was cancelled: :dev still
    runs swarm-ui from `first`. N+1 touches nothing new. A record for N+1 that
    offers swarm-ui from `first` offers the image N changed, unchanged."""
    deployed = box.first
    box.commit("N: the ui", box.append("apps/swarm-ui/src/App.tsx", "export const n = 1;\n"))
    head = box.commit("N+1: docs only", {"docs/ci.md": "words\n"})
    record = json.loads(box.record(head).read_text())
    for entry in record["images"]:
        entry.update(box.reused_from(entry["name"], deployed))

    proc, manifest, events = box.release(record, mode)
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert _submitted(events) == {"swarm-ui"}, (
        f"submitted {_submitted(events)}: the release must build exactly the image whose inputs "
        "changed since the digest it was offered, whatever release last succeeded"
    )
    images = _by_name(manifest)
    assert images["swarm-ui"]["built_from"] == head and images["swarm-ui"]["reused"] is False
    assert images["swarm-ui"]["digest"] != _old_digest("swarm-ui", deployed), "promoted the stale digest"
    for name in set(IMAGES) - {"swarm-ui"}:
        assert images[name]["digest"] == _old_digest(name, deployed), f"{name} was rebuilt for nothing"
    assert "swarm-ui" in proc.stderr and "inputs this commit has changed" in proc.stderr, proc.stderr[-2000:]


def test_n_s_own_digest_is_promoted_by_n_plus_1_without_a_build(box):
    """The same history, CI right: N's build made swarm-ui, and N+1's record
    reuses it. N's release never shipped it, and N+1's release does."""
    deployed = box.first
    n = box.commit("N: the ui", box.append("apps/swarm-ui/src/App.tsx", "export const n = 1;\n"))
    head = box.commit("N+1: docs only", {"docs/ci.md": "words\n"})
    record = json.loads(box.record(head).read_text())
    for entry in record["images"]:
        entry.update(box.reused_from(entry["name"], n if entry["name"] == "swarm-ui" else deployed))

    proc, manifest, events = box.release(record)
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert _submitted(events) == set(), f"submitted {_submitted(events)}"
    assert manifest == record, "the release did not promote CI's record as fetched"
    assert _by_name(manifest)["swarm-ui"]["digest"] == _old_digest("swarm-ui", n)


def test_the_whole_chain_ships_n_s_change_after_n_s_build_and_release_failed(box):
    """N's CI build failed, so N had no record and no release. N+1's CI
    build builds on the record before N -- and still rebuilds what N changed,
    because it diffs from that record's commit, not from HEAD~1."""
    deployed = box.first
    box.commit("N: the ui", box.append("apps/swarm-ui/src/App.tsx", "export const n = 1;\n"))
    head = box.commit("N+1: docs only", {"docs/ci.md": "words\n"})

    proc, ci_record, events = box.build(box.record(deployed))
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert _submitted(events) == {"swarm-ui"}, f"CI submitted {_submitted(events)}"

    proc, manifest, events = box.release(ci_record)
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert _submitted(events) == set(), f"the release rebuilt {_submitted(events)} CI had built right"
    assert manifest == ci_record
    assert _by_name(manifest)["swarm-ui"]["built_from"] == head


def test_a_stale_base_rebuilds_the_images_built_from_it(box):
    deployed = box.first
    box.commit("N: the base", box.append("images/agent-runtime-base/Dockerfile"))
    head = box.commit("N+1: docs only", {"docs/ci.md": "words\n"})
    record = json.loads(box.record(head).read_text())
    for entry in record["images"]:
        entry.update(box.reused_from(entry["name"], deployed))

    proc, manifest, events = box.release(record)
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert _submitted(events) == WORKER, f"submitted {_submitted(events)}"
    for name in WORKER:
        assert _by_name(manifest)[name]["built_from"] == head


def test_a_docs_only_push_builds_nothing_and_promotes_ci_s_record(box):
    deployed = box.first
    head = box.commit("docs only", {"docs/ci.md": "words\n"})
    record = json.loads(box.record(head).read_text())
    for entry in record["images"]:
        entry.update(box.reused_from(entry["name"], deployed))

    proc, manifest, events = box.release(record)
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert _submitted(events) == set(), f"submitted {_submitted(events)}"
    assert _retagged(events) == set(), "a release whose record is current moved a tag"
    assert manifest == record
    assert not (box.root / "build" / "ci-images-dev.json").exists(), "the check left its copy behind"


def test_a_record_ci_built_whole_needs_no_history(box):
    """Every image built from this commit: nothing to check, so nothing in git
    is read -- the check is only for digests CI re-tagged."""
    head = box.commit("anything", box.append("apps/swarm-ui/src/App.tsx", "export const z = 3;\n"))
    record = json.loads(box.record(head).read_text())

    proc, manifest, events = box.release(record)
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert _submitted(events) == set()
    assert manifest == record
    assert "Incremental build" not in proc.stderr


def test_the_release_images_stage_has_uv_for_the_check():
    """A digest CI kept across a uv.lock change is checked by its own `uv
    export`; without uv the release would rebuild it."""
    action = (Path(__file__).resolve().parents[3] / ".github" / "actions" / "release-images" / "action.yml").read_text()
    uv = action.find("astral-sh/setup-uv@")
    build = action.find("./scripts/build-images.sh --reuse-ci")
    assert 0 <= uv < build, "release-images installs no uv before build-images.sh --reuse-ci"



def test_a_one_commit_checkout_fetches_the_history_it_checks_against(box):
    """release.yml checks out one commit. The check fetches the rest, and so
    builds the stale image alone rather than every image."""
    deployed = box.first
    box.commit("N: the ui", box.append("apps/swarm-ui/src/App.tsx", "export const n = 1;\n"))
    head = box.commit("N+1: docs only", {"docs/ci.md": "words\n"})
    record = json.loads(box.record(head).read_text())
    for entry in record["images"]:
        entry.update(box.reused_from(entry["name"], deployed))
    shallow = box.tmp / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{box.root}", str(shallow)], check=True)
    assert box.git("-C", str(shallow), "rev-parse", "--is-shallow-repository") == "true"
    box.root = shallow

    proc, manifest, events = box.release(record)
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert _submitted(events) == {"swarm-ui"}, f"submitted {_submitted(events)}"
    assert box.git("-C", str(shallow), "rev-parse", "--is-shallow-repository") == "false"
