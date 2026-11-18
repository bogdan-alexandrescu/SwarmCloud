"""Main's image build reuses what a commit did not change, starts the slowest
image first, and builds swarm-speech on its previous image's layers (the owner,
2026-11-06: unblock CI so more merges and deploys happen overnight).

MEASURED that morning from Cloud Build: on main's newest commit `build
images` rebuilt every image, each in 1-4 min except swarm-speech (1,717 s
and 1,521 s), which started in the LAST batch of BUILD_PARALLELISM, so the
release waited ~45 min per commit. A commit that changed only apps/swarm-api
(3b82588b4) still rebuilt swarm-live, swarm-egress, swarm-graph,
swarm-typesafe-fwd, swarm-github-hooks, swarm-speech, swarm-voice and
workspace-apply: plan_incremental rebuilt every image whose Dockerfile
upgrades OS packages on every main build (APT_REFRESH), though the release
rebuilds each of them again and promotes only its own.

The properties asserted here, against the REAL script in the incremental
tests' git sandbox:

  * on main, a commit touching only swarm-api rebuilds exactly the images
    whose inputs include it (`--affected-by`), though the record's OS
    upgrades are another build's; each reused digest keeps the apt_refresh of
    the build that made it;
  * the queue is submitted slowest first, by the previous record's
    `build_seconds` (swarm-speech's measured 1,717 s when no record carries
    it), a base counting its slowest image built FROM it; every built image's
    seconds are recorded, and a reused one keeps the previous record's;
  * swarm-speech's generated config builds with BuildKit, writes its inline
    cache and reads it from the previous record's digest; no other image's
    does, and a --build-only config (a pull request's) reads no registry;
  * swarm-speech's Dockerfile installs its third-party closure in a stage
    that reads only the lock and names no APT_REFRESH, and copies it in
    before the apt layer and before any source.

MUTATION: restore the APT_REFRESH clause for main's build (the swarm-api test
goes red), drop queue_order (the order tests go red), drop cache_build_args
or layer_cached (the cache test goes red), or move the venv copy below the
apt layer (the Dockerfile test goes red).

WHAT THIS CANNOT PROVE: that Cloud Build's BuildKit serves the cached layer.
The first two builds of swarm-speech after this change show it in their logs
(`CACHED` on the venv copy) and their durations.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from .test_build_images_incremental import REGISTRY, Sandbox, _assert_whole, _old_digest, _retagged, _submitted
from .test_ci_built_images import IMAGES

REPO = Path(__file__).resolve().parents[3]
SPEECH_DOCKERFILE = REPO / "apps" / "swarm-speech" / "Dockerfile"


@pytest.fixture
def box(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


def _affected_by(box: Sandbox, path: str) -> set[str]:
    proc = subprocess.run(
        ["bash", str(box.root / "scripts" / "build-images.sh"), "--affected-by", "-"],
        input=path + "\n", env=box.env, capture_output=True, text=True, timeout=120, check=False,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    return {line for line in proc.stdout.split() if line}


def _order(stderr: str) -> list[str]:
    m = re.search(r"build order, slowest first: (.+)", stderr)
    assert m, f"no build order in the log:\n{stderr[-4000:]}"
    return m.group(1).split()


def test_a_swarm_api_change_on_main_rebuilds_only_the_images_that_take_it(box):
    """3b82588b4: only apps/swarm-api changed, and the record's OS upgrades
    were another run's. Main rebuilds what --affected-by names, nothing else."""
    path = "apps/swarm-api/swarm_api/previews.py"
    head = box.commit("api", {path: "PREVIEWS = 1\n"})
    record = box.record(box.first, per_image={n: {"apt_refresh": "an-earlier-run"} for n in IMAGES})
    proc, manifest, events = box.build(record, BUILD_APT_REFRESH="this-run")
    assert proc.returncode == 0, proc.stderr[-4000:]

    expected = _affected_by(box, path)
    assert "swarm-api" in expected and "swarm-speech" not in expected, expected
    assert _submitted(events) == expected, f"submitted {sorted(_submitted(events))}, inputs reach {sorted(expected)}"
    assert _retagged(events) == set(IMAGES) - expected
    _assert_whole(manifest, head)
    by_name = {i["name"]: i for i in manifest["images"]}
    for name in set(IMAGES) - expected:
        entry = by_name[name]
        assert entry["reused"] is True and entry["digest"] == _old_digest(name, box.first), entry
        # The reused digest's OS packages are the earlier run's, and the
        # record says so: a release's check then rebuilds it.
        if name in {"swarm-speech", "workspace-apply", "swarm-live"}:
            assert entry["apt_refresh"] == "an-earlier-run", entry
    assert by_name["swarm-api"]["apt_refresh"] == "this-run", by_name["swarm-api"]


def test_the_queue_starts_with_the_slowest_recorded_image(box):
    """Every image is rebuilt (.dockerignore is an input of each); the record
    says swarm-speech took 1,717 s and swarm-graph 300 s, every other one 60 s
    or less, so the queue starts swarm-speech, then swarm-graph."""
    box.commit("all", box.append(".dockerignore"))
    seconds = {n: 30 for n in IMAGES}
    seconds.update({"swarm-speech": 1717, "swarm-graph": 300, "agent-runtime-browser": 200,
                    "agent-runtime-base-nocodex": 50})
    record = box.record(box.first, per_image={n: {"build_seconds": s} for n, s in seconds.items()})
    proc, manifest, _events = box.build(record)
    assert proc.returncode == 0, proc.stderr[-4000:]
    order = _order(proc.stderr)
    assert sorted(order) == sorted(IMAGES), order
    assert order[:2] == ["swarm-speech", "swarm-graph"], order
    # The codex-less base holds up the browser (200 s), so it starts third,
    # ahead of every 30-second image, and before the image built FROM it.
    assert order[2] == "agent-runtime-base-nocodex", order
    assert order.index("agent-runtime-base-nocodex") < order.index("agent-runtime-browser"), order
    for entry in manifest["images"]:
        assert entry["reused"] is False, entry
        assert isinstance(entry["build_seconds"], int) and entry["build_seconds"] >= 0, entry


def test_without_recorded_seconds_swarm_speech_still_starts_first(box):
    box.commit("all", box.append(".dockerignore"))
    proc, _manifest, _events = box.build(box.record(box.first))
    assert proc.returncode == 0, proc.stderr[-4000:]
    order = _order(proc.stderr)
    assert order[0] == "swarm-speech", order
    # Every other image keeps ALL_TARGETS order.
    assert order[1:] == [n for n in IMAGES if n != "swarm-speech"], order


def test_a_reused_image_keeps_the_seconds_of_the_build_that_made_it(box):
    box.commit("ui", box.append("apps/swarm-ui/src/App.tsx", "export const y = 2;\n"))
    record = box.record(box.first, per_image={"swarm-speech": {"build_seconds": 1521}})
    proc, manifest, events = box.build(record)
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert _submitted(events) == {"swarm-ui"}
    by_name = {i["name"]: i for i in manifest["images"]}
    assert by_name["swarm-speech"]["build_seconds"] == 1521, by_name["swarm-speech"]
    assert by_name["swarm-verify"]["build_seconds"] is None, by_name["swarm-verify"]
    assert isinstance(by_name["swarm-ui"]["build_seconds"], int), by_name["swarm-ui"]


def test_swarm_speech_is_built_from_its_previous_image_s_layers(box):
    box.commit("speech", box.append("apps/swarm-speech/Dockerfile"))
    proc, _manifest, events = box.build(box.record(box.first))
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert "swarm-speech" in _submitted(events)
    config = (box.root / "build" / "cloudbuild-swarm-speech.yaml").read_text()
    assert "DOCKER_BUILDKIT=1" in config, config
    assert "- BUILDKIT_INLINE_CACHE=1" in config, config
    ref = f"{REGISTRY}/swarm-speech@{_old_digest('swarm-speech', box.first)}"
    assert re.search(r"- --cache-from\n\s+- " + re.escape(ref) + r"\n", config), config
    for other in box.root.glob("build/cloudbuild-*.yaml"):
        if other.name != "cloudbuild-swarm-speech.yaml":
            assert "--cache-from" not in other.read_text(), other.name


def test_a_build_only_config_reads_no_registry_cache(box):
    """A pull request's build pushes nothing and replays with --no-cache."""
    proc = subprocess.run(
        ["bash", str(box.root / "scripts" / "build-images.sh"), "--build-only", "swarm-speech", "--tag", "abc123"],
        env=box.env, capture_output=True, text=True, timeout=300, check=False,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    config = (box.root / "build" / "build-only" / "cloudbuild-swarm-speech.yaml").read_text()
    assert "--cache-from" not in config and "BUILDKIT_INLINE_CACHE" not in config, config
    # The Dockerfile mounts from a stage (RUN --mount), which only BuildKit reads.
    assert "DOCKER_BUILDKIT=1" in config, config


def _stages(text: str) -> dict[str, list[str]]:
    """Stage name -> its instruction lines, continuations joined."""
    stages: dict[str, list[str]] = {}
    current = None
    logical = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not logical and (not line or line.startswith("#")):
            continue
        logical += line.rstrip("\\") + " "
        if line.endswith("\\"):
            continue
        m = re.match(r"FROM\s+\S+(?:\s+AS\s+(\S+))?", logical, re.I)
        if m:
            current = m.group(1) or "?"
            stages[current] = []
        elif current is not None:
            stages[current].append(logical.strip())
        logical = ""
    return stages


def test_swarm_speech_installs_its_dependencies_before_the_apt_layer_and_the_source():
    stages = _stages(SPEECH_DOCKERFILE.read_text())
    deps = stages["deps"]
    copies = [line for line in deps if line.startswith("COPY ") and "--from=" not in line]
    assert copies == ["COPY apps/swarm-speech/pyproject.toml apps/swarm-speech/uv.lock /src/apps/swarm-speech/"], copies
    assert not any("APT_REFRESH" in line for line in deps), "an APT_REFRESH in deps busts its cache every build"
    assert any("uv pip install" in line and "--require-hashes" in line for line in deps), deps

    runtime = stages["runtime"]
    venv = runtime.index("COPY --from=deps /opt/venv /opt/venv")
    apt = next(i for i, line in enumerate(runtime) if line.startswith("RUN") and "APT_REFRESH" in line)
    own = next(i for i, line in enumerate(runtime) if "--find-links /wheels" in line)
    assert venv < apt < own, (venv, apt, own)
    assert "--no-index" in runtime[own] and "uv pip check" in runtime[own], runtime[own]
