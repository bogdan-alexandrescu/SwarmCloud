"""`scripts/build-images.sh` submits its Cloud Builds concurrently, and says
which ones failed.

THE DEFECT THIS PINS. Release run 35969538707 spent nineteen minutes in its
build step submitting eight Cloud Builds ONE AFTER ANOTHER: each `gcloud builds
submit` blocked for about three minutes (roughly two of building and one of
upload and polling), and nothing about any build depended on the one before it
-- except one pair. Eight times three is the nineteen minutes.

And a failure was reported as the first one only: the loop ran under `set -e`
with `pipefail`, so the first failed submit ended the script, the images after
it were never attempted, and the operator learned about the second broken image
on the next run.

The properties asserted here, each against the REAL script with a fake `gcloud`
on PATH (the same seam `tests/integration/test_register_tenant_grants.py`
uses), so what is tested is the scheduler that ships rather than a copy of it:

  * independent images are in flight at the same time, and never more of them
    than the configured bound -- the bound matters because saga-agents-staging
    is SHARED, and its concurrent-build quota is shared with it;
  * agent-runtime-browser is not submitted until agent-runtime-base has
    FINISHED, because its checked-in cloudbuild.yaml pulls
    `agent-runtime-base:<tag>` in its first step -- and that wait does not
    serialise anything else;
  * every image is attempted even after one fails, and the failure names every
    image that failed, plus every image that was not submitted because what it
    is built FROM failed;
  * each build's output is attributable to its image when builds interleave;
  * the manifest records the ONE digest carrying exactly this run's tag, not
    the `pr-<run>-<sha>` build of the same commit that a word-matched lookup
    also returns (release run 35972131246 wrote both, glued together);
  * the credential file google-github-actions/auth writes into the checkout
    (`gha-creds-<16 hex>.json`) never goes up with the build source, and a run
    that cannot establish that submits nothing.

WHAT THIS CANNOT PROVE: that real `gcloud` processes run side by side without
contending for their shared credential cache, or that the release job actually
gets faster. Only a release run on main shows that. Nor that the fake's
reading of `.gcloudignore` is gcloud's: it models the subset this repository
uses (comments, `#!include:`, a trailing-slash directory, `*`, a pattern with
and without a slash, `!`). The real thing was measured on 2026-09-24 with
`gcloud meta list-files-for-upload` (SDK 483.0.0) over an export of the
commit with two planted creds files: 699 files listed without the rule, 697
with it, and the two missing were exactly the planted files.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "build-images.sh"
TAG = "cafe00c0ffee"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="build-images.sh needs bash and jq",
)

# Enough gcloud for build-images.sh to run end to end offline. `builds submit`
# records when it started and ended, prints a line naming its image on stdout
# and on stderr (so attribution is tested on both streams), sleeps for as long
# as a build is supposed to take, and fails for any image named in FAKE_FAIL.
FAKE_GCLOUD = r'''#!{python}
import fnmatch, hashlib, json, os, re, sys, time, uuid

args = sys.argv[1:]


def record(**event):
    event["t"] = time.time()
    with open(os.environ["FAKE_EVENTS"], "a") as fh:
        fh.write(json.dumps(event) + "\n")


# WHAT `gcloud builds submit DIR` UPLOADS: every file under DIR that the
# .gcloudignore at DIR's top level does not exclude -- or, when there is none,
# the one gcloud generates (`gcloud topic gcloudignore`): .gcloudignore, .git,
# .gitignore, and whatever .gitignore lists.
def _rules(root, lines):
    out = []
    for line in lines:
        line = line.strip()
        if line.startswith("#!include:"):
            with open(os.path.join(root, line.split(":", 1)[1])) as fh:
                out += _rules(root, fh.read().splitlines())
        elif line and not line.startswith("#"):
            out.append(line)
    return out


def upload_set(root):
    own = os.path.join(root, ".gcloudignore")
    if os.path.exists(own):
        with open(own) as fh:
            rules = _rules(root, fh.read().splitlines())
    else:
        generated = [".gcloudignore", ".git", ".gitignore"]
        if os.path.exists(os.path.join(root, ".gitignore")):
            generated.append("#!include:.gitignore")
        rules = _rules(root, generated)
    files = []
    for dirpath, _, names in os.walk(root):
        for name in names:
            rel = os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/")
            parts = rel.split("/")
            ignored = False
            for rule in rules:
                negated = rule.startswith("!")
                pattern = rule.lstrip("!").strip("/")
                candidates = (
                    ["/".join(parts[:k]) for k in range(1, len(parts) + 1)]
                    if "/" in pattern else parts
                )
                if any(fnmatch.fnmatchcase(c, pattern) for c in candidates):
                    ignored = not negated
            if not ignored:
                files.append(rel)
    return sorted(files)


if args[:2] == ["meta", "list-files-for-upload"]:
    record(event="list-files")
    if os.environ.get("FAKE_LIST_FILES_FAIL"):
        print("ERROR: (gcloud.meta) Invalid choice: 'list-files-for-upload'.", file=sys.stderr)
        sys.exit(2)
    for rel in upload_set(args[2]):
        print(rel)
    sys.exit(0)


if args[:3] == ["artifacts", "repositories", "describe"]:
    print("projects/p/locations/r/repositories/swarm-images")
    sys.exit(0)

if args[:2] == ["builds", "submit"]:
    config = args[args.index("--config") + 1]
    match = re.search(r"cloudbuild-(.+)\.yaml$", config) or re.search(
        r"/images/([^/]+)/cloudbuild\.yaml$", config
    )
    target = match.group(1)
    build_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, target))
    uploaded_creds = [
        rel for rel in upload_set(args[2]) if rel.rsplit("/", 1)[-1].startswith("gha-creds-")
    ]
    record(event="start", target=target, uploaded_creds=uploaded_creds)
    print(f"fake-build-stdout target={target}", flush=True)
    print(
        "Created [https://cloudbuild.googleapis.com/v1/projects/p/locations/r/"
        f"builds/{build_id}].",
        file=sys.stderr,
        flush=True,
    )
    print(f"fake-build-stderr target={target}", file=sys.stderr, flush=True)
    time.sleep(float(os.environ.get("FAKE_BUILD_SECONDS", "1")))
    record(event="end", target=target)
    if target in set(filter(None, os.environ.get("FAKE_FAIL", "").split(","))):
        print(f"ERROR: fake build of {target} failed", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)

if args[:4] == ["artifacts", "docker", "tags", "list"]:
    # Shaped like the live registry on 2026-09-24. application.yml's `build`
    # job ALSO builds every image on a push to main, tagged
    # `pr-<run>-<sha>`, so a release's `tag:<sha>` filter -- a WORD match in
    # gcloud -- finds that build as well as its own. The PR build is listed
    # first, so "take the first line" is caught as surely as "glue them".
    image = args[4]
    name = image.rsplit("/", 1)[-1]
    wanted = next(a for a in args if a.startswith("--filter=")).split(":", 1)[1]
    fmt = next((a for a in args if a.startswith("--format=")), "--format=").split("=", 1)[1]
    rows = [
        (f"pr-99-{wanted}", "sha256:" + hashlib.sha256(f"{image}@pr".encode()).hexdigest()),
        (wanted, "sha256:" + hashlib.sha256(f"{image}@{wanted}".encode()).hexdigest()),
    ]
    if fmt == "json":
        base = f"projects/p/locations/r/repositories/swarm-images/packages/{name}"
        print(json.dumps([
            {"image": image, "tag": f"{base}/tags/{t}", "version": f"{base}/versions/{d}"}
            for t, d in rows
        ]))
    else:
        for _, digest in rows:
            print(digest)
    sys.exit(0)

print("fake gcloud: unhandled " + " ".join(args), file=sys.stderr)
sys.exit(2)
'''


def _sandbox(tmp_path: Path) -> Path:
    """A copy of the scripts and recipes, so the run writes nothing into the
    checkout and REPO_ROOT (derived from the script's own location) is here --
    with the checkout's own ignore files, which decide what a submit uploads."""
    root = tmp_path / "repo"
    shutil.copytree(REPO / "scripts", root / "scripts")
    shutil.copytree(REPO / "images", root / "images")
    for name in (".gitignore", ".gcloudignore"):
        if (REPO / name).exists():
            shutil.copy2(REPO / name, root / name)
    return root


# What google-github-actions/auth@v2 leaves in $GITHUB_WORKSPACE before the
# build step runs: `gha-creds-` + 16 hex + `.json` (src/utils.ts,
# generateCredentialsFilename). The real one is a live credential; this one
# carries nothing.
CREDS = "gha-creds-0123456789abcdef.json"


def _run(
    tmp_path: Path,
    targets: list[str],
    *,
    parallel: int,
    fail: tuple[str, ...] = (),
    seconds: float = 1.5,
    plant_creds: bool = False,
    gcloudignore: str | None = None,
    **fakes: str,
):
    root = _sandbox(tmp_path)
    if plant_creds:
        (root / CREDS).write_text('{"type": "external_account", "note": "fake"}\n')
    if gcloudignore is not None:
        (root / ".gcloudignore").write_text(gcloudignore)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gcloud = bindir / "gcloud"
    gcloud.write_text(FAKE_GCLOUD.replace("{python}", sys.executable))
    gcloud.chmod(0o755)
    events_file = tmp_path / "events.jsonl"
    home = tmp_path / "home"
    home.mkdir()

    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "SWARM_ENV_FILE": str(tmp_path / "no-such.env"),
        "PROJECT_ID": "swarm-test-project",
        "REGION": "us-central1",
        "ENVIRONMENT": "dev",
        "NO_COLOR": "1",
        # The sandbox is not a git checkout, so git_dirty() reports dirty.
        "ALLOW_DIRTY_BUILD": "1",
        "FAKE_EVENTS": str(events_file),
        "FAKE_BUILD_SECONDS": str(seconds),
        "FAKE_FAIL": ",".join(fail),
        # Passed by ENVIRONMENT, not by flag, so that a script which does not
        # know about concurrency runs sequentially and fails these tests on
        # the property rather than on an unknown flag.
        "BUILD_PARALLELISM": str(parallel),
        "BUILD_POLL_INTERVAL": "0.1",
        "BUILD_SUBMIT_STAGGER": "0",
        **fakes,
    }
    env.pop("GITHUB_ACTIONS", None)
    proc = subprocess.run(
        ["bash", str(root / "scripts" / "build-images.sh"), *targets, "--tag", TAG],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    events = (
        [json.loads(line) for line in events_file.read_text().splitlines() if line]
        if events_file.exists()
        else []
    )
    return root, proc, events


def _windows(events: list[dict]) -> dict[str, tuple[float, float]]:
    starts = {e["target"]: e["t"] for e in events if e["event"] == "start"}
    ends = {e["target"]: e["t"] for e in events if e["event"] == "end"}
    return {t: (starts[t], ends.get(t, float("inf"))) for t in starts}


def _max_in_flight(windows: dict[str, tuple[float, float]]) -> int:
    # An end sorts before a start at the same instant: touching is not overlap.
    marks = sorted(
        [(start, 1) for start, _ in windows.values()]
        + [(end, -1) for _, end in windows.values()],
        key=lambda m: (m[0], m[1]),
    )
    peak = level = 0
    for _, delta in marks:
        level += delta
        peak = max(peak, level)
    return peak


def _failure_summary(proc: subprocess.CompletedProcess) -> str:
    """The last thing the script says: the line a reader of a red job sees."""
    lines = [line for line in proc.stderr.splitlines() if line.strip()]
    assert lines, "the script said nothing at all on failure"
    return lines[-1]


INDEPENDENT = [
    "swarm-api",
    "swarm-scheduler",
    "swarm-quota-broker",
    "swarm-reconciler",
    "swarm-ui",
]


@pytest.mark.parametrize("parallel", [2, 4])
def test_independent_images_are_in_flight_together_up_to_the_bound(tmp_path, parallel):
    _, proc, events = _run(tmp_path, INDEPENDENT, parallel=parallel)
    assert proc.returncode == 0, proc.stderr[-3000:]

    windows = _windows(events)
    assert sorted(windows) == sorted(INDEPENDENT), (
        f"not every image was submitted: {sorted(windows)}"
    )
    peak = _max_in_flight(windows)
    assert peak <= parallel, (
        f"{peak} builds were in flight at once with a bound of {parallel}; the "
        "bound exists because saga-agents-staging's build quota is shared"
    )
    assert peak == parallel, (
        f"at most {peak} build(s) were ever in flight with a bound of {parallel}: "
        "the builds are still being submitted one after another, which is the "
        "nineteen minutes release run 35969538707 spent"
    )


def test_the_browser_image_waits_for_the_base_it_is_built_from(tmp_path):
    # Given in the WRONG order on purpose: the old script relied on ALL_TARGETS
    # listing the base first, so a targeted call in any other order broke it.
    _, proc, events = _run(
        tmp_path,
        ["agent-runtime-browser", "swarm-api", "agent-runtime-base"],
        parallel=3,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    windows = _windows(events)

    base_start, base_end = windows["agent-runtime-base"]
    browser_start, _ = windows["agent-runtime-browser"]
    assert browser_start >= base_end, (
        "agent-runtime-browser was submitted before agent-runtime-base finished; "
        "its cloudbuild.yaml pulls agent-runtime-base:<tag> in its first step, "
        "so it fails with manifest-unknown"
    )
    api_start, _ = windows["swarm-api"]
    assert api_start < base_end, (
        "swarm-api waited for agent-runtime-base too: the ordering must hold back "
        "only the image built FROM the base, not serialise the whole release"
    )


def test_every_failed_image_is_named_and_the_rest_are_still_built(tmp_path):
    targets = ["swarm-api", "swarm-scheduler", "swarm-quota-broker", "swarm-reconciler"]
    root, proc, events = _run(
        tmp_path, targets, parallel=2, fail=("swarm-api", "swarm-reconciler")
    )
    assert proc.returncode != 0, "two builds failed and the script exited 0"

    submitted = sorted(_windows(events))
    assert submitted == sorted(targets), (
        f"only {submitted} were submitted: the first failure stopped the run, so "
        "the operator learns about the next broken image one release later"
    )

    summary = _failure_summary(proc)
    for failed in ("swarm-api", "swarm-reconciler"):
        assert failed in summary, f"{failed} failed but the summary does not say so: {summary!r}"
    for built in ("swarm-scheduler", "swarm-quota-broker"):
        assert built not in summary, f"{built} built fine but the summary names it: {summary!r}"

    assert not (root / "build" / "images-dev.json").exists(), (
        "a manifest was written for a partial build; deploy.sh would roll the "
        "failed images back to their previous revision without saying so"
    )


def test_an_image_whose_base_failed_is_not_submitted_and_is_named(tmp_path):
    _, proc, events = _run(
        tmp_path,
        ["agent-runtime-base", "agent-runtime-browser", "swarm-api"],
        parallel=3,
        fail=("agent-runtime-base",),
    )
    assert proc.returncode != 0
    submitted = _windows(events)
    assert "agent-runtime-browser" not in submitted, (
        "agent-runtime-browser was submitted although the base it is built FROM "
        "had just failed; that build can only fail too"
    )
    assert "swarm-api" in submitted, (
        "swarm-api does not depend on agent-runtime-base and should still build"
    )
    summary = _failure_summary(proc)
    assert "agent-runtime-base" in summary, summary
    assert "agent-runtime-browser" in summary, (
        f"the browser image was never built, and the summary does not say so: {summary!r}"
    )


def test_each_build_log_is_attributable_to_its_image(tmp_path):
    targets = ["swarm-api", "swarm-scheduler", "swarm-ui"]
    _, proc, _ = _run(tmp_path, targets, parallel=3)
    assert proc.returncode == 0, proc.stderr[-3000:]
    output = (proc.stdout + "\n" + proc.stderr).splitlines()

    for target in targets:
        for stream in ("stdout", "stderr"):
            marker = f"fake-build-{stream} target={target}"
            carrying = [line for line in output if marker in line]
            assert carrying, f"{target}'s build {stream} never reached the job log"
            unlabelled = [line for line in carrying if not line.startswith(f"[{target}]")]
            assert not unlabelled, (
                f"{target}'s build output is printed without saying whose it is, "
                f"so with builds interleaved it cannot be attributed: {unlabelled}"
            )


def _build_after(target: str) -> str:
    """What build-images.sh itself says `target` must wait for."""
    script = f"""
set -euo pipefail
eval "$(sed -n '/^build_after() {{/,/^}}/p' {SCRIPT})"
declare -F build_after >/dev/null || {{ echo "no build_after() in build-images.sh" >&2; exit 3; }}
build_after {target}
"""
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _uncommented(path: Path) -> str:
    if not path.exists():
        return ""
    return "\n".join(
        line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")
    )


def test_every_image_a_recipe_is_built_from_is_declared_as_its_prerequisite():
    """The ordering is stated once, in build_after(); the fact it encodes lives
    in the recipes. So the recipes are read to find which image each one pulls,
    and build_after() must agree -- a new `FROM <other target>` that nobody
    declared is the race this test exists to catch before a release does."""
    targets = sorted(p.parent.name for p in (REPO / "images").glob("*/Dockerfile"))
    assert len(targets) >= 8, targets
    found = 0
    for target in targets:
        recipe = _uncommented(REPO / "images" / target / "Dockerfile") + _uncommented(
            REPO / "images" / target / "cloudbuild.yaml"
        )
        pulls = {
            other
            for other in targets
            if other != target and re.search(rf"(?<![\w-]){re.escape(other)}:", recipe)
        }
        assert len(pulls) <= 1, f"{target} pulls several targets: {pulls}"
        declared = _build_after(target)
        if pulls:
            found += 1
            assert declared == next(iter(pulls)), (
                f"{target}'s recipe pulls {pulls}, but build_after says {declared!r}"
            )
        else:
            assert declared == "", f"{target} is ordered after {declared!r}, which it never pulls"
    assert found >= 1, (
        "no recipe pulls another target any more; if agent-runtime-browser stopped "
        "being built FROM agent-runtime-base, drop the ordering and this assertion"
    )


def test_the_manifest_records_the_build_this_run_made_and_nothing_else(tmp_path):
    """DIGEST READ-BACK, and the mixed release it caused.

    Release run 35972131246 (b0fff1b, a push to main) wrote a build manifest in
    which EVERY entry was two digests glued together -- `sha256:8448...sha256:
    5f3a...` for swarm-api -- because `tags list --filter=tag:<sha>` is a word
    match that also finds application.yml's `pr-<run>-<sha>` build of the same
    commit, and `tr -d '[:space:]'` joined the two lines. push-images.sh then
    took whichever version was listed first, and on 2026-09-24 :dev held five
    images from one build and three from the other."""
    root, proc, _ = _run(tmp_path, ["swarm-api", "swarm-ui"], parallel=2, seconds=0.2)
    assert proc.returncode == 0, proc.stderr[-3000:]
    manifest = json.loads((root / "build" / "images-dev.json").read_text())
    repo = "us-central1-docker.pkg.dev/swarm-test-project/swarm-images"
    for entry in manifest["images"]:
        image = f"{repo}/{entry['name']}"
        own = "sha256:" + hashlib.sha256(f"{image}@{TAG}".encode()).hexdigest()
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", entry["digest"]), (
            f"{entry['name']}: the manifest digest is not one digest: {entry['digest']!r}"
        )
        assert entry["digest"] == own, (
            f"{entry['name']}: the manifest names {entry['digest']}, not the build "
            f"tagged :{TAG} ({own}) -- it picked up the pr-99-{TAG} build of the same commit"
        )
        assert entry["ref"] == f"{image}@{own}", entry["ref"]


def _submitted(events: list[dict]) -> list[dict]:
    return [e for e in events if e["event"] == "start"]


def test_the_workflow_credential_file_never_goes_up_with_the_source(tmp_path):
    """`gcloud builds submit "${REPO_ROOT}"` uploads the checkout, and in
    release.yml and application.yml the checkout holds the file auth@v2 wrote a
    step earlier. With no .gcloudignore, gcloud's generated one is `.gitignore`
    plus .git -- and .gitignore did not list it -- so the credential went up
    with every build's source."""
    _, proc, events = _run(
        tmp_path, ["swarm-api", "swarm-ui"], parallel=2, seconds=0.2, plant_creds=True
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    submits = _submitted(events)
    assert sorted(e["target"] for e in submits) == ["swarm-api", "swarm-ui"], submits
    leaked = {e["target"]: e["uploaded_creds"] for e in submits if e["uploaded_creds"]}
    assert not leaked, (
        f"the build source uploaded the workflow's credential file: {leaked}"
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        # The ignore rule is gone: the run must refuse rather than leak.
        {"gcloudignore": ".gcloudignore\n.git\n.gitignore\nbuild/\n"},
        # gcloud cannot say what it would upload -- `meta list-files-for-upload`
        # is documented as internal and may disappear. Unknown is not safe.
        {"FAKE_LIST_FILES_FAIL": "1"},
    ],
    ids=["rule-missing", "listing-unavailable"],
)
def test_a_run_that_cannot_keep_the_credential_file_out_submits_nothing(tmp_path, kwargs):
    _, proc, events = _run(
        tmp_path, ["swarm-api", "swarm-ui"], parallel=2, seconds=0.2, plant_creds=True, **kwargs
    )
    assert proc.returncode != 0, "the run exited 0"
    submits = _submitted(events)
    assert not submits, (
        f"{[e['target'] for e in submits]} submitted although nothing established "
        f"that {CREDS} stays out of the upload"
    )
    summary = _failure_summary(proc)
    assert CREDS in summary, f"the last line does not name the credential file: {summary!r}"


BASH4_ONLY = {
    r"\bwait\s+-n\b": "wait -n (bash 4.3)",
    r"\b(declare|local|typeset)\s+-[a-zA-Z]*A": "associative arrays (bash 4)",
    r"\b(declare|local|typeset)\s+-[a-zA-Z]*n\b": "namerefs (bash 4.3)",
    r"\b(mapfile|readarray)\b": "mapfile (bash 4)",
    r"\$\{[A-Za-z_][A-Za-z0-9_]*(,,?|\^\^?)\}": "case modification (bash 4)",
    r"\bcoproc\b": "coproc (bash 4)",
    r"\|&": "|& (bash 4)",
    r"&>>": "&>> (bash 4)",
    r"\bEPOCH(SECONDS|REALTIME)\b": "EPOCHSECONDS (bash 5)",
}


@pytest.mark.parametrize("name", ["build-images.sh", "push-images.sh"])
def test_nothing_bash_3_2_lacks(name):
    """A SHAPE check, and labelled as one. These scripts target the bash 3.2.57
    that macOS ships, and CI runs bash 5, so CI cannot show they run on 3.2.
    What it can do is refuse the constructs a concurrency change reaches for
    first -- `wait -n` above all -- which 3.2 does not have."""
    text = "\n".join(
        line
        for line in (REPO / "scripts" / name).read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    used = [label for pattern, label in BASH4_ONLY.items() if re.search(pattern, text)]
    assert not used, f"{name} uses what bash 3.2 does not have: {used}"
