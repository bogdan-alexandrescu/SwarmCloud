"""The promotion mark is `promoted-<channel>-<tag>`, and nothing reads only the old one.

WHY THE MARK WAS RENAMED (#1886, REGISTRY-MARKS). Artifact Registry
`swarm-images` grows by about 125 GB a day because every build is tagged and
only untagged images are cleaned. The cleanup that follows must keep the live
`:dev` and `:prod` images with NO age limit, and promoted images for 30 days.
Cleanup policies match tags by PREFIX, and the old mark `dev-promoted-<tag>`
starts with `dev`: a keep-forever rule on the prefix `dev` would keep every
mark forever. `promoted-<channel>-<tag>` starts with neither channel.

What is held here, against the SHIPPED scripts with a fake `gcloud` on PATH:

  * `promotion_mark_prefix dev` prints `promoted-dev-`; the old form is
    `legacy_promotion_mark_prefix`, read during the transition only;
  * deploy.sh's "did a release promote this digest?" check (`promoted_earlier`,
    lifted out of deploy.sh so the test cannot pass against a copy) accepts a
    digest carrying either form, for its own channel and digest only;
  * `scripts/registry-remark.sh` plans, for every version carrying an old mark,
    exactly the new mark on the same digest: it removes nothing, moves nothing,
    writes nothing without `--apply`, and touches no repository but
    `swarm-images`.

push-images.sh writing the new form is held in
test_push_images_all_or_nothing.py, next to the promotion it marks.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from .gate_script_functions import function_source

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "scripts"
DEPLOY = SCRIPTS / "lib" / "deploy.sh"
REMARK = SCRIPTS / "registry-remark.sh"

PROJECT = "swarm-test-project"
REGION = "us-central1"
IMAGE_REPO = f"{REGION}-docker.pkg.dev/{PROJECT}/swarm-images"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="the scripts need bash and jq",
)


def digest(char: str) -> str:
    return "sha256:" + char * 64


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "SWARM_ENV_FILE": str(tmp_path / "no-such.env"),
        "PROJECT_ID": PROJECT,
        "REGION": REGION,
        "ENVIRONMENT": "dev",
        "NO_COLOR": "1",
    }
    for name in ("IMAGE_REPO", "IMAGE_HOST", "ARTIFACT_REGISTRY", "SWARM_ASSUME_YES"):
        env.pop(name, None)
    env.update(extra)
    return env


def _bash(body: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f'source "{SCRIPTS}/lib/common.sh"\n{body}', "promotion-mark-test"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


# ---------------------------------------------------------------------------
# The prefix
# ---------------------------------------------------------------------------


def test_the_mark_prefix_names_the_channel_after_the_word_promoted(tmp_path):
    proc = _bash(
        'printf "%s|%s|" "$(promotion_mark_prefix dev)" "$(promotion_mark_prefix prod)"\n'
        'printf "%s|%s" "$(legacy_promotion_mark_prefix dev)" "$(legacy_promotion_mark_prefix prod)"',
        _env(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    new_dev, new_prod, old_dev, old_prod = proc.stdout.split("|")
    # MUTATION: printf '%s-promoted-' (the old form) in promotion_mark_prefix fails here.
    assert (new_dev, new_prod) == ("promoted-dev-", "promoted-prod-"), proc.stdout
    # MUTATION: a legacy_promotion_mark_prefix that prints the new form fails here.
    assert (old_dev, old_prod) == ("dev-promoted-", "prod-promoted-"), proc.stdout
    # The reason for the rename: no mark starts with a channel tag's name.
    # MUTATION: any prefix starting `dev`/`prod` fails here.
    assert not new_dev.startswith(("dev", "prod")) and not new_prod.startswith(("dev", "prod"))


# ---------------------------------------------------------------------------
# deploy.sh's promoted_earlier: both forms, during the transition
# ---------------------------------------------------------------------------

IMAGE = f"{IMAGE_REPO}/workspace-apply"
PINNED = digest("c")

FAKE_TAGS_LIST = r'''#!{python}
import os, sys
args = sys.argv[1:]
assert args[:4] == ["artifacts", "docker", "tags", "list"], args
with open(os.environ["FAKE_CALLS"], "a") as fh:
    fh.write(" ".join(args) + "\n")
# `value(tag.basename(),image,version.basename())`, tab-separated -- spelled
# as full resource names, which promoted_earlier must cut to the last segment.
for row in open(os.environ["FAKE_TAGS"]).read().splitlines():
    tag, image, version = row.split("|")
    print(f"projects/p/locations/l/repositories/r/packages/x/tags/{tag}\t{image}\tprojects/p/versions/{version}")
'''


def _promoted_earlier(tmp_path: Path, rows: list[tuple[str, str, str]], channel: str = "dev") -> int:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gcloud").write_text(FAKE_TAGS_LIST.replace("{python}", sys.executable))
    (bindir / "gcloud").chmod(0o755)
    tags = tmp_path / "tags.txt"
    tags.write_text("\n".join("|".join(r) for r in rows) + "\n")
    calls = tmp_path / "calls.txt"
    work = tmp_path / "work"
    work.mkdir()
    env = _env(
        tmp_path,
        PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
        FAKE_TAGS=str(tags),
        FAKE_CALLS=str(calls),
    )
    body = (
        f'WORK="{work}"; CHANNEL="{channel}"\n'
        + function_source(DEPLOY, "promoted_earlier")
        + f'rc=0; promoted_earlier "{IMAGE}" "{PINNED}" || rc=$?; printf "rc=%s" "$rc"\n'
    )
    proc = _bash(body, env)
    assert proc.stdout.startswith("rc="), proc.stderr[-2000:]
    # Control: the registry was asked. A check that never looked would answer too.
    assert calls.exists() and "tags list" in calls.read_text()
    return int(proc.stdout.removeprefix("rc="))


def test_a_digest_carrying_the_new_mark_was_promoted(tmp_path):
    rows = [("promoted-dev-0123abcd", "workspace-apply", PINNED)]
    # MUTATION: an awk that matches only the old prefix returns 1 here.
    assert _promoted_earlier(tmp_path, rows) == 0


def test_a_digest_carrying_only_the_old_mark_still_reads_as_promoted(tmp_path):
    # Every digest promoted before REGISTRY-MARKS carries only this form until
    # scripts/registry-remark.sh has run.
    rows = [("dev-promoted-0123abcd", "workspace-apply", PINNED)]
    # MUTATION: dropping the legacy prefix from deploy.sh's awk returns 1 here.
    assert _promoted_earlier(tmp_path, rows) == 0


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param([("0123abcd", "workspace-apply", PINNED)], id="commit-tag-only"),
        pytest.param([("promoted-prod-0123abcd", "workspace-apply", PINNED)], id="new-mark-other-channel"),
        pytest.param([("prod-promoted-0123abcd", "workspace-apply", PINNED)], id="old-mark-other-channel"),
        pytest.param([("promoted-dev-0123abcd", "workspace-apply", digest("e"))], id="new-mark-other-digest"),
        pytest.param([("dev-promoted-0123abcd", "workspace-apply", digest("e"))], id="old-mark-other-digest"),
        pytest.param([("promoted-dev-0123abcd", "swarm-verify", PINNED)], id="new-mark-other-image"),
        pytest.param([("dev", "workspace-apply", PINNED)], id="channel-tag-is-not-a-mark"),
    ],
)
def test_no_other_tag_reads_as_a_promotion(tmp_path, rows):
    # MUTATION: an awk matching any tag containing `promoted` returns 0 here.
    assert _promoted_earlier(tmp_path, rows) == 1


# ---------------------------------------------------------------------------
# scripts/registry-remark.sh
# ---------------------------------------------------------------------------

FAKE_REGISTRY_GCLOUD = r'''#!{python}
import json, os, sys

args = sys.argv[1:]
state_path = os.environ["FAKE_REGISTRY"]
registry = json.load(open(state_path))


def record(**event):
    with open(os.environ["FAKE_EVENTS"], "a") as fh:
        fh.write(json.dumps(event) + "\n")


def split(ref, sep):
    path, rest = ref.rsplit(sep, 1)
    return path, rest


record(event="call", args=args)
if args[:4] == ["artifacts", "docker", "images", "list"]:
    repo = args[4]
    rows = []
    for package, versions in registry.items():
        if not package.startswith(repo + "/"):
            continue
        for version, tags in versions.items():
            row = {"package": package, "version": version}
            if "--include-tags" in args:
                # gcloud has spelled tags both ways; one package uses the string.
                row["tags"] = ",".join(tags) if package.endswith("/swarm-ui") else tags
            rows.append(row)
    print("Listing items under project p, location l, repository swarm-images.", file=sys.stderr)
    print(json.dumps(rows))
    sys.exit(0)

if args[:4] == ["artifacts", "docker", "tags", "add"]:
    package, version = split(args[4], "@")
    package_dst, tag = split(args[5], ":")
    assert package == package_dst, args
    for tags in registry[package].values():
        if tag in tags:
            tags.remove(tag)  # `tags add` MOVES an existing tag, as the real one does
    registry[package].setdefault(version, []).append(tag)
    json.dump(registry, open(state_path, "w"))
    record(event="add", package=package, version=version, tag=tag)
    sys.exit(0)

if args[:4] == ["artifacts", "docker", "tags", "delete"]:
    record(event="delete", args=args)
    sys.exit(0)

print("fake gcloud: unhandled " + " ".join(args), file=sys.stderr)
sys.exit(2)
'''


def _pkg(name: str) -> str:
    return f"{IMAGE_REPO}/{name}"


PRE_MARK = "dev-promoted-pre-mark-" + "c" * 64


def _fixture() -> dict:
    return {
        _pkg("swarm-api"): {
            digest("a"): ["0123abcd", "dev-promoted-0123abcd", "dev"],
            digest("b"): ["prod-promoted-456789ab", "dev-promoted-456789ab", "prod"],
            digest("0"): ["pr-12-89abcdef", "89abcdef"],
        },
        _pkg("workspace-apply"): {
            digest("c"): [PRE_MARK],
            # Already re-marked: nothing to add.
            digest("d"): ["dev-promoted-11112222", "promoted-dev-11112222"],
        },
        _pkg("swarm-ui"): {
            digest("e"): ["33334444", "dev-promoted-33334444"],
            # The new mark already names ANOTHER digest: never moved.
            digest("f"): ["dev-promoted-55556666"],
            digest("9"): ["promoted-dev-55556666"],
        },
        # Another repository in the same project: never listed, never written.
        f"{REGION}-docker.pkg.dev/{PROJECT}/other-images/swarm-api": {
            digest("7"): ["dev-promoted-77778888"],
        },
    }


EXPECTED = {
    (_pkg("swarm-api"), digest("a"), "promoted-dev-0123abcd"),
    (_pkg("swarm-api"), digest("b"), "promoted-prod-456789ab"),
    (_pkg("swarm-api"), digest("b"), "promoted-dev-456789ab"),
    (_pkg("workspace-apply"), digest("c"), "promoted-dev-pre-mark-" + "c" * 64),
    (_pkg("swarm-ui"), digest("e"), "promoted-dev-33334444"),
}


def _remark(tmp_path: Path, registry: dict, *args: str, **extra: str):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gcloud").write_text(FAKE_REGISTRY_GCLOUD.replace("{python}", sys.executable))
    (bindir / "gcloud").chmod(0o755)
    state = tmp_path / "registry.json"
    state.write_text(json.dumps(registry))
    events = tmp_path / "events.jsonl"
    env = _env(
        tmp_path,
        PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
        FAKE_REGISTRY=str(state),
        FAKE_EVENTS=str(events),
        **extra,
    )
    proc = subprocess.run(
        ["bash", str(REMARK), *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    log = [json.loads(line) for line in events.read_text().splitlines()] if events.exists() else []
    return proc, json.loads(state.read_text()), log


def _pairs(stdout: str) -> set[tuple[str, str, str]]:
    """`<package>:<new mark> -> <digest>` lines, as the plan prints them."""
    pairs = set()
    for line in stdout.splitlines():
        if " -> " not in line:
            continue
        ref, version = line.strip().split(" -> ")
        package, tag = ref.rsplit(":", 1)
        pairs.add((package, version, tag))
    return pairs


def test_the_script_is_executable():
    # MUTATION: a registry-remark.sh committed without +x fails here.
    assert REMARK.exists() and os.access(REMARK, os.X_OK)


def test_the_dry_run_plans_exactly_the_new_marks_and_writes_nothing(tmp_path):
    before = _fixture()
    proc, after, log = _remark(tmp_path, before)
    assert proc.returncode == 0, proc.stderr[-3000:]
    # MUTATION: a plan that skips the second old mark on one digest, or adds a
    # mark for a digest already carrying it, fails here.
    assert _pairs(proc.stdout) == EXPECTED, proc.stdout
    # MUTATION: a count that counts tags rather than planned marks fails here.
    assert "5 promotion mark(s) to add" in proc.stdout + proc.stderr, proc.stdout + proc.stderr
    # Dry run: no write of any kind.
    # MUTATION: a script that applies without --apply fails here.
    assert [e for e in log if e["event"] in ("add", "delete")] == []
    assert after == before
    # The conflict is named, not moved.
    # MUTATION: a plan that re-points promoted-dev-55556666 fails the pairs check above;
    # one that drops it silently fails here.
    assert "promoted-dev-55556666" in proc.stderr, proc.stderr
    # Control: the repository was listed with its tags, so an empty plan could not pass.
    lists = [e["args"] for e in log if e["args"][:4] == ["artifacts", "docker", "images", "list"]]
    assert lists and lists[0][4] == IMAGE_REPO and "--include-tags" in lists[0], lists


def test_apply_adds_exactly_the_planned_marks_and_removes_none(tmp_path):
    before = _fixture()
    proc, after, log = _remark(tmp_path, before, "--apply")
    # The conflict is left alone and reported, so the run is not clean.
    assert proc.returncode != 0, proc.stdout + proc.stderr
    adds = {(e["package"], e["version"], e["tag"]) for e in log if e["event"] == "add"}
    # MUTATION: a script that writes the old form, or skips a version, fails here.
    assert adds == EXPECTED, adds
    # MUTATION: any `tags delete` call fails here.
    assert [e for e in log if e["event"] == "delete"] == []
    # Never removed and never moved: every tag before is on the same digest after.
    # MUTATION: re-pointing the conflicting promoted-dev-55556666 fails here.
    for package, versions in before.items():
        for version, tags in versions.items():
            missing = set(tags) - set(after[package][version])
            assert not missing, (package, version, missing)
    # Run again: everything is already marked and nothing is written twice.
    events = tmp_path / "events.jsonl"
    events.unlink()
    shutil.rmtree(tmp_path / "bin")
    proc, _, log = _remark(tmp_path, after, "--apply")
    # MUTATION: a second run that re-adds marks fails here.
    assert [e for e in log if e["event"] == "add"] == [], log


def test_apply_without_conflicts_exits_zero(tmp_path):
    before = _fixture()
    del before[_pkg("swarm-ui")][digest("9")]
    proc, after, _ = _remark(tmp_path, before, "--apply")
    # MUTATION: a script that fails a clean run fails here.
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "promoted-dev-55556666" in after[_pkg("swarm-ui")][digest("f")]


def test_the_dry_run_prints_the_count_and_only_the_first_twenty_pairs(tmp_path):
    many = {_pkg("swarm-api"): {digest(f"{i:x}") if i < 16 else "sha256:" + f"{i:02x}" * 32: [f"dev-promoted-{i:08d}"] for i in range(25)}}
    proc, _, _ = _remark(tmp_path, many)
    assert proc.returncode == 0, proc.stderr[-3000:]
    # MUTATION: printing every pair, or none, fails here.
    assert len(_pairs(proc.stdout)) == 20, proc.stdout
    # MUTATION: counting only the printed pairs fails here.
    assert "25 promotion mark(s) to add" in proc.stdout + proc.stderr, proc.stdout + proc.stderr


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({"ARTIFACT_REGISTRY": "other-images"}, id="artifact-registry"),
        pytest.param({"IMAGE_REPO": f"{REGION}-docker.pkg.dev/{PROJECT}/other-images"}, id="image-repo"),
    ],
)
def test_it_refuses_every_repository_but_swarm_images(tmp_path, extra):
    proc, after, log = _remark(tmp_path, _fixture(), "--apply", **extra)
    # MUTATION: removing the repository guard fails here.
    assert proc.returncode != 0
    assert "swarm-images" in proc.stderr, proc.stderr
    # Refused before the registry was even read.
    assert log == [], log
    assert after == _fixture()
