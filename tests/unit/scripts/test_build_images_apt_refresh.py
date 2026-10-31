"""Every main build and every release rebuilds each image that upgrades OS
packages (owner decision 2026-10-14: "force a fresh apt layer", on every
release).

THE FAILURE THIS PREVENTS. Releases 38535875750 (2c7bbea5) and 38540997006
(cc6353d0) refused to promote workspace-apply over curl and libcurl4t64
8.14.1-2+deb13u5 (CVE-2026-8286, HIGH, fixed in +deb13u7). Its Dockerfile runs
`apt-get upgrade`, but the RUN had last run on 2026-10-13: release
38540997006's log reads `workspace-apply reuse no input changed since
346f2725df43`. Main's incremental build re-tags a digest whose inputs did not
change, and nothing in the tree changes when Debian ships a fix.

The properties asserted here, against the REAL script in the incremental
tests' git sandbox:

  * a digest of an image whose Dockerfile upgrades OS packages, made by
    another build (its recorded `apt_refresh` is not this build's
    APT_REFRESH), is rebuilt though no input changed; an image built FROM one
    is rebuilt with it; an image that upgrades nothing (swarm-verify) is still
    reused -- the rule is not "rebuild all";
  * without BUILD_APT_REFRESH, APT_REFRESH names the GitHub run, so two runs
    never share one, and the manifest records it as each image's apt_refresh;
  * a release rebuilds them even from a record CI built whole for this very
    commit, and still re-tags swarm-verify;
  * every build of such an image is passed APT_REFRESH: the generated configs
    as `--build-arg APT_REFRESH=<it>`, workspace-apply's checked-in recipe as
    the `_APT_REFRESH` substitution its build step passes on. CI run
    38545464833 caught the first version answering "no upgrade" for the base
    and workspace-apply: `grep -v | grep -q` under pipefail, SIGPIPE;
  * every Dockerfile RUN that upgrades OS packages names ${APT_REFRESH}, with
    `ARG APT_REFRESH` declared in its stage;
  * workspace-apply's apt RUN fails its own build when a simulated
    dist-upgrade still has something to install.

MUTATION: drop the apt_refresh clause from plan_incremental (the rebuild
tests go red), restore the early exit for a whole CI record (the release test
goes red), stop calling apt_build_args or adding _APT_REFRESH to the subs (the
config test goes red), or remove `${APT_REFRESH}` from any apt RUN (the
Dockerfile test goes red).

WHAT THIS CANNOT PROVE: that the image built in Cloud Build installs
+deb13u7. That is the build's own assertion and the release's scan, in CI.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from .test_build_images_incremental import APT_PIN, Sandbox, _assert_whole, _retagged, _submitted
from .test_ci_built_images import IMAGES
from .test_release_rebuilds_stale_images import Release

REPO = Path(__file__).resolve().parents[3]

# The images whose recipe Dockerfile runs `apt-get upgrade` or `apk upgrade`,
# and the three built FROM a base, which are rebuilt with it.
UPGRADES = {
    "agent-runtime-base", "agent-runtime-base-nocodex", "swarm-api", "swarm-scheduler",
    "swarm-quota-broker", "swarm-reconciler", "swarm-ui", "swarm-live", "swarm-egress",
    "swarm-typesafe-fwd", "swarm-graph", "swarm-github-hooks", "workspace-apply",
}
# agent-runtime-lightpanda (LIGHTPANDA-BUILD-SCRIPTS) is built FROM the base
# by digest, as the indexer is.
BUILT_FROM_A_BASE = {"agent-runtime-browser", "agent-runtime-indexer", "agent-runtime-lightpanda"}
NO_UPGRADE = set(IMAGES) - UPGRADES - BUILT_FROM_A_BASE
REBUILT = UPGRADES | BUILT_FROM_A_BASE

# Wraps the sandbox's fake gcloud, recording each call's argv and, for a
# submit, the text of the config it names, before running the fake.
RECORDING_GCLOUD = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
entry = {"args": args}
if "--config" in args:
    with open(args[args.index("--config") + 1]) as fh:
        entry["config"] = fh.read()
with open(os.environ["GCLOUD_CALLS"], "a") as fh:
    fh.write(json.dumps(entry) + "\n")
real = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gcloud-real")
os.execv(sys.executable, [sys.executable, real, *args])
'''

UPGRADE_RE = re.compile(r"(apt-get|apk)\s+(-\S+\s+)*upgrade(\s|$)")


class Recording(Sandbox):
    def __init__(self, tmp_path: Path):
        super().__init__(tmp_path)
        bindir = tmp_path / "bin"
        shutil.move(bindir / "gcloud", bindir / "gcloud-real")
        wrapper = bindir / "gcloud"
        wrapper.write_text(RECORDING_GCLOUD.replace("{python}", sys.executable))
        wrapper.chmod(0o755)
        self.env["GCLOUD_CALLS"] = str(tmp_path / "gcloud-calls.jsonl")

    def submits(self) -> dict[str, dict]:
        """target -> the recorded `builds submit` call that built it."""
        path = self.tmp / "gcloud-calls.jsonl"
        out = {}
        for line in path.read_text().splitlines() if path.exists() else []:
            call = json.loads(line)
            args = call["args"]
            if args[:2] != ["builds", "submit"]:
                continue
            config = args[args.index("--config") + 1]
            m = re.search(r"cloudbuild-(.+)\.yaml$", config) or re.search(r"/images/([^/]+)/cloudbuild\.yaml$", config)
            out[m.group(1)] = call
        return out


@pytest.fixture
def box(tmp_path: Path) -> Recording:
    return Recording(tmp_path)


def test_a_digest_another_build_upgraded_is_rebuilt_though_no_input_changed(box):
    """The release failure: no input of workspace-apply changed, and its
    digest is another build's. It is rebuilt, with every other image that
    upgrades OS packages; swarm-verify is re-tagged."""
    head = box.commit("docs", {"docs/ci.md": "words\n"})
    proc, manifest, events = box.build(box.record(box.first), BUILD_APT_REFRESH="this-build")
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert _submitted(events) == REBUILT, f"submitted {_submitted(events)}"
    assert NO_UPGRADE, "the control needs an image that upgrades nothing"
    assert _retagged(events) == NO_UPGRADE, (
        "an image whose Dockerfile upgrades no OS package has no apt layer to refresh; "
        "rebuilding it too would make this rule 'rebuild everything'"
    )
    assert "every build and release refreshes them" in proc.stderr, proc.stderr[-4000:]
    _assert_whole(manifest, head)
    by_name = {i["name"]: i for i in manifest["images"]}
    assert by_name["workspace-apply"]["reused"] is False and by_name["workspace-apply"]["built_from"] == head
    for name in UPGRADES:
        assert by_name[name]["apt_refresh"] == "this-build", by_name[name]
    for name in NO_UPGRADE:
        assert by_name[name]["apt_refresh"] is None, by_name[name]


def test_a_digest_this_build_made_is_reused(box):
    """The control: a record carrying this build's APT_REFRESH (the sandbox
    pins it) rebuilds nothing on a docs-only commit."""
    box.commit("docs", {"docs/ci.md": "words\n"})
    proc, _manifest, events = box.build(box.record(box.first), BUILD_APT_REFRESH=APT_PIN)
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert _submitted(events) == set()
    assert _retagged(events) == set(IMAGES)


def test_without_an_override_apt_refresh_names_the_github_run(box):
    head = box.commit("docs", {"docs/ci.md": "words\n"})
    env = {k: v for k, v in box.env.items() if k != "BUILD_APT_REFRESH"}
    box.env = {**env, "GITHUB_RUN_ID": "4242", "GITHUB_RUN_ATTEMPT": "2"}
    proc, manifest, events = box.build(box.record(box.first))
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert _submitted(events) == REBUILT, f"submitted {_submitted(events)}"
    expected = f"{head[:12]}-run4242-2"
    ws = {i["name"]: i for i in manifest["images"]}["workspace-apply"]
    assert ws["apt_refresh"] == expected, ws
    subs = box.submits()["workspace-apply"]["args"]
    assert f"_APT_REFRESH={expected}" in subs[subs.index("--substitutions") + 1].split(","), subs


def test_a_release_rebuilds_them_even_from_a_record_ci_built_whole_for_this_commit(tmp_path):
    """Owner decision: every release. CI built every image from this commit,
    in another run; the release rebuilds the OS-upgrading ones and re-tags
    the rest."""
    box = Release(tmp_path)
    head = box.commit("anything", box.append("apps/swarm-ui/src/App.tsx", "export const z = 3;\n"))
    record = json.loads(box.record(head).read_text())
    for entry in record["images"]:
        # The digest the sandbox's fake gcloud reports for <image>:<tag>, as
        # Artifact Registry would for what CI pushed at this commit's tag.
        digest = "sha256:" + hashlib.sha256(f"built {entry['image']}:{head[:12]}".encode()).hexdigest()
        entry.update(apt_refresh="ci-run", digest=digest, ref=f"{entry['image']}@{digest}")

    proc, manifest, events = box.release(record)
    assert proc.returncode == 0, proc.stderr[-4000:]
    assert _submitted(events) == REBUILT, f"the release submitted {_submitted(events)}"
    by_name = {i["name"]: i for i in manifest["images"]}
    offered = {e["name"]: e for e in record["images"]}
    for name in NO_UPGRADE:
        assert by_name[name]["digest"] == offered[name]["digest"], f"{name} was not kept as CI built it"
        assert by_name[name]["reused"] is True, by_name[name]
    for name in REBUILT:
        assert by_name[name]["reused"] is False and by_name[name]["built_from"] == head, by_name[name]


def test_every_build_of_an_upgrading_image_is_passed_apt_refresh(box):
    box.commit("docs", {"docs/ci.md": "words\n"})
    proc, _manifest, _events = box.build("none", BUILD_APT_REFRESH="r-2031")
    assert proc.returncode == 0, proc.stderr[-4000:]
    submits = box.submits()
    assert set(submits) == set(IMAGES), f"recorded submits for {sorted(submits)}"

    for name in UPGRADES:
        args = submits[name]["args"]
        if name == "workspace-apply":
            subs = args[args.index("--substitutions") + 1].split(",")
            assert "_APT_REFRESH=r-2031" in subs, subs
            continue
        config = yaml.safe_load(submits[name]["config"])
        build_args = [a for s in config["steps"] for i, a in enumerate(s.get("args") or [])
                      if i and s["args"][i - 1] == "--build-arg"]
        assert "APT_REFRESH=r-2031" in build_args, f"{name}: {build_args}"
    for name in NO_UPGRADE:
        call = submits[name]
        assert "APT_REFRESH" not in call.get("config", "") and "_APT_REFRESH" not in " ".join(call["args"]), name


def test_workspace_apply_s_recipe_passes_the_substitution_to_its_dockerfile():
    recipe = yaml.safe_load((REPO / "images" / "workspace-apply" / "cloudbuild.yaml").read_text())
    build = next(s for s in recipe["steps"] if s.get("id") == "build")
    args = build["args"]
    assert args[args.index("APT_REFRESH=${_APT_REFRESH}") - 1] == "--build-arg", args


def _instructions(text: str) -> list[tuple[str, str]]:
    """(KEYWORD, body) for each instruction, continuations joined, comment and
    blank lines dropped (Docker drops them inside a continuation too)."""
    out: list[tuple[str, str]] = []
    parts: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            parts.append(stripped[:-1].strip())
            continue
        parts.append(stripped)
        keyword, _, body = " ".join(parts).partition(" ")
        out.append((keyword.upper(), body))
        parts = []
    return out


def _recipe_dockerfiles() -> list[Path]:
    return sorted(REPO.glob("images/*/Dockerfile")) + [REPO / "apps" / "swarm-live" / "Dockerfile"]


def test_every_os_package_upgrade_names_apt_refresh_in_its_run_line():
    upgrades = 0
    for dockerfile in _recipe_dockerfiles():
        declared = False
        for keyword, body in _instructions(dockerfile.read_text()):
            if keyword == "FROM":
                declared = False
            elif keyword == "ARG" and re.match(r"APT_REFRESH(=|\s|$)", body):
                declared = True
            elif keyword == "RUN" and UPGRADE_RE.search(body):
                upgrades += 1
                where = f"{dockerfile.relative_to(REPO)}: RUN {body[:80]}"
                assert declared, f"{where} has no `ARG APT_REFRESH` in its stage"
                assert "${APT_REFRESH}" in body, f"{where} does not name ${{APT_REFRESH}}"
    # 2 RUNs in each of the base, four services and swarm-live, 1 in swarm-ui
    # and workspace-apply: a parser that found none would pass vacuously.
    assert upgrades >= 14, upgrades


def test_workspace_apply_fails_its_build_when_an_upgrade_is_still_pending():
    runs = [body for keyword, body in _instructions((REPO / "images" / "workspace-apply" / "Dockerfile").read_text())
            if keyword == "RUN" and UPGRADE_RE.search(body)]
    assert len(runs) == 1, runs
    run = runs[0]
    assert "apt-get -s dist-upgrade" in run, run
    assert run.index("apt-get -s dist-upgrade") > run.index("apt-get install"), (
        "the check must follow the installs, or a package installed after it goes unchecked"
    )
    assert re.search(r"grep '\^Inst ' \S+ >&2; then .*exit 1", run), run
