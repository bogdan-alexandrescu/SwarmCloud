"""The hotfix lane ships a labelled commit to dev without queueing behind a
normal release, and cannot do anything the normal release would have stopped.

Owner decision 2026-10-08 (observer proposal H; docs/ci.md, "Hotfix
releases"). Measured that day over 22 successful release.yml runs: new digests
live 43 min p50 / 97 p90 after the run is created, `release-dev` held 100 min
p50, and a hotfix (archived pull request 857) waited about 1 h 45 min. hotfix.yml runs on every push
to main in a concurrency group of its own and proceeds only for a pull request
merged with the `hotfix` label; it runs release.yml's own stages
(.github/actions/release-*), skips verify, warm, acceptance, the GKE proof and
the plugin tag, and FAILS on a plan that changes IAM.

What this file holds: the two scripts the lane runs, against fakes.

* the gate (scripts/lib/hotfix-gate.sh) proceeds only for a MERGED pull
  request into main carrying the label, and fails rather than guess when
  GitHub does not answer;
* scripts/lib/release-order.sh, which both lanes use, refuses a commit that is
  not a descendant of what is already promoted or applied (green,
  "superseded"), and its lock is exclusive;
* both scripts are executable and shellcheck-clean.

WHAT THIS CANNOT PROVE: that GitHub runs the workflows as read, that `gcloud
storage cp --if-generation-match=0` is atomic (GCS documents it as a
precondition on the write), or that the deployer's binding admits hotfix.yml
before the owner applies terraform/bootstrap. Only a labelled merge on main
proves the lane end to end.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
GATE_SH = REPO / "scripts" / "lib" / "hotfix-gate.sh"
ORDER_SH = REPO / "scripts" / "lib" / "release-order.sh"


# ---------------------------------------------------------------------------
# scripts/lib/hotfix-gate.sh, against a fake `gh`

SHA = "a" * 40
FAKE_GH = r"""#!{python}
import os, sys
if os.environ.get("FAKE_GH_FAIL"):
    sys.stderr.write("HTTP 502: bad gateway\n"); sys.exit(1)
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\n")
sys.stdout.write(open(os.environ["FAKE_GH_PULLS"]).read())
"""


def _pr(number, *, merged=True, base="main", labels=()):
    return {
        "number": number,
        "merged_at": "2026-10-08T10:00:00Z" if merged else None,
        "base": {"ref": base},
        "labels": [{"name": n} for n in labels],
    }


def _gate(tmp_path, pulls, **env):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH.replace("{python}", sys.executable))
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "pulls.json").write_text(json.dumps(pulls))
    proc = subprocess.run(
        [str(GATE_SH), "--sha", SHA, "--repo", "owner/name", "--label", "hotfix"],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_GH_PULLS": str(tmp_path / "pulls.json"),
            "FAKE_GH_LOG": str(tmp_path / "gh.log"),
            "SWARM_HOTFIX_GATE_RETRY_DELAY": "0",
            **env,
        },
        timeout=60,
    )
    return proc, dict(l.split("=", 1) for l in proc.stdout.splitlines() if "=" in l)


def test_the_gate_proceeds_for_a_merged_pull_request_with_the_label(tmp_path):
    proc, out = _gate(tmp_path, [_pr(857, labels=("bug", "hotfix"))])
    assert proc.returncode == 0, proc.stderr
    assert out == {"proceed": "true", "pr": "857"}
    assert f"repos/owner/name/commits/{SHA}/pulls" in (tmp_path / "gh.log").read_text()


@pytest.mark.parametrize(
    "pulls",
    [
        [_pr(1)],
        [_pr(2, merged=False, labels=("hotfix",))],
        [_pr(3, base="release", labels=("hotfix",))],
        [_pr(4, labels=("hotfixes",))],
        [],
    ],
    ids=["merged-unlabelled", "open-labelled", "other-base-labelled", "other-label", "no-pull-request"],
)
def test_the_gate_does_not_proceed_without_a_merged_labelled_pull_request(tmp_path, pulls):
    """MUTATION: drop `.merged_at != null`, or the base check, from the jq."""
    proc, out = _gate(tmp_path, pulls)
    assert proc.returncode == 0, proc.stderr
    assert out.get("proceed") == "false", out


def test_the_gate_fails_rather_than_guess_when_github_does_not_answer(tmp_path):
    proc, out = _gate(tmp_path, [_pr(857, labels=("hotfix",))], FAKE_GH_FAIL="1")
    assert proc.returncode == 1
    assert "proceed" not in out
    assert "502" in proc.stderr


# ---------------------------------------------------------------------------
# scripts/lib/release-order.sh, against a fake `gcloud storage` with generations

FAKE_GCLOUD = r"""#!{python}
import os, sys
root = os.environ["FAKE_GCS_ROOT"]
args = sys.argv[1:]
if args[:1] != ["storage"]:
    sys.stderr.write("fake gcloud: only `storage` is modelled\n"); sys.exit(2)
cmd = args[1]
rest = args[2:]
if cmd == "objects":
    cmd, rest = "objects " + rest[0], rest[1:]
match = None
plain = []
for a in rest:
    if a.startswith("--if-generation-match="):
        match = int(a.split("=", 1)[1])
    elif a.startswith("--"):
        continue
    else:
        plain.append(a)
def path(url):
    assert url.startswith("gs://"), url
    return os.path.join(root, url[5:])
def gen(p):
    return int(open(p + ".gen").read()) if os.path.exists(p + ".gen") else 0
def missing(url):
    sys.stderr.write(f"ERROR: (gcloud.storage) gs object not found: 404 {url}\n"); sys.exit(1)
if cmd == "objects describe":
    p = path(plain[0])
    if not os.path.exists(p): missing(plain[0])
    sys.stdout.write('{"generation": "%d", "name": "%s"}\n' % (gen(p), plain[0]))
elif cmd == "cat":
    p = path(plain[0])
    if not os.path.exists(p): missing(plain[0])
    sys.stdout.write(open(p).read())
elif cmd == "cp":
    src, url = plain
    p = path(url)
    exists = os.path.exists(p)
    if match is not None and ((match == 0 and exists) or (match != 0 and gen(p) != match)):
        sys.stderr.write("ERROR: PreconditionFailed: 412 At least one of the pre-conditions you specified did not hold.\n"); sys.exit(1)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    counter = os.path.join(root, ".counter")
    n = (int(open(counter).read()) if os.path.exists(counter) else 1000) + 1
    open(counter, "w").write(str(n))
    open(p, "w").write(open(src).read())
    open(p + ".gen", "w").write(str(n))
elif cmd == "rm":
    p = path(plain[0])
    if not os.path.exists(p): missing(plain[0])
    if match is not None and gen(p) != match:
        sys.stderr.write("ERROR: PreconditionFailed: 412\n"); sys.exit(1)
    os.remove(p); os.remove(p + ".gen")
else:
    sys.stderr.write(f"fake gcloud: {cmd} is not modelled\n"); sys.exit(2)
"""


@pytest.fixture
def world(tmp_path):
    """A repository with main = A -> B -> C, an unrelated commit X, and an
    empty fake state bucket."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"},
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    shas = {}
    for name in ("A", "B", "C"):
        git("commit", "-q", "--allow-empty", "-m", name)
        shas[name] = git("rev-parse", "HEAD")
    git("checkout", "-q", "--orphan", "other")
    git("commit", "-q", "--allow-empty", "-m", "X")
    shas["X"] = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gcloud = bin_dir / "gcloud"
    gcloud.write_text(FAKE_GCLOUD.replace("{python}", sys.executable))
    gcloud.chmod(gcloud.stat().st_mode | stat.S_IXUSR)
    gcs = tmp_path / "gcs"
    gcs.mkdir()
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")},
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GCS_ROOT": str(gcs),
        "TF_STATE_BUCKET": "state",
        "SWARM_ENV_FILE": str(tmp_path / "no.env"),
        "SWARM_RELEASE_ORDER_CHECKOUT": str(repo),
        "SWARM_RELEASE_LOCK_POLL": "0",
        "GITHUB_WORKFLOW": "release",
        "GITHUB_RUN_ID": "42",
        "NO_COLOR": "1",
    }

    class World:
        pass

    w = World()
    w.repo, w.shas, w.gcs, w.env, w.git, w.tmp = repo, shas, gcs, env, git, tmp_path
    return w


def _order(world, *args: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(ORDER_SH), *args], capture_output=True, text=True, env={**world.env, **env}, timeout=60
    )


def _verdict(world, sha: str, **env: str) -> str:
    proc = _order(world, "check", "--environment", "dev", "--sha", sha, **env)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip().splitlines()[-1]


def _record(world, what: str, sha: str) -> None:
    proc = _order(world, "record", "--environment", "dev", "--what", what, "--sha", sha)
    assert proc.returncode == 0, proc.stderr


def test_with_nothing_recorded_every_commit_proceeds(world):
    assert _verdict(world, world.shas["A"]) == "proceed"


@pytest.mark.parametrize("what", ["applied", "promoted"])
def test_a_commit_older_than_what_is_out_is_superseded(world, what):
    """The hotfix applied C; the normal release of B reaches its apply.
    MUTATION: swap the arguments of `merge-base --is-ancestor`."""
    _record(world, what, world.shas["C"])
    assert _verdict(world, world.shas["B"]) == "superseded"
    assert _verdict(world, world.shas["A"]) == "superseded"


def test_the_same_commit_and_its_descendants_proceed(world):
    _record(world, "applied", world.shas["B"])
    _record(world, "promoted", world.shas["B"])
    assert _verdict(world, world.shas["B"]) == "proceed"
    assert _verdict(world, world.shas["C"]) == "proceed"


def test_a_commit_that_is_no_ancestor_at_all_is_superseded(world):
    _record(world, "applied", world.shas["X"])
    assert _verdict(world, world.shas["C"]) == "superseded"


def test_the_verdict_reaches_github_output_and_says_why(world):
    _record(world, "applied", world.shas["C"])
    out = world.tmp / "github_output"
    proc = _order(world, "check", "--environment", "dev", "--sha", world.shas["A"], GITHUB_OUTPUT=str(out))
    assert proc.returncode == 0, proc.stderr
    assert out.read_text() == "superseded=true\n"
    assert "::notice title=Superseded::" in proc.stdout
    out.unlink()
    _order(world, "check", "--environment", "dev", "--sha", world.shas["C"], GITHUB_OUTPUT=str(out))
    assert out.read_text() == "superseded=false\n"


def test_a_record_naming_no_commit_is_refused_not_guessed(world):
    (world.gcs / "state/releases/dev").mkdir(parents=True)
    (world.gcs / "state/releases/dev/applied.json").write_text('{"sha": "main"}')
    (world.gcs / "state/releases/dev/applied.json.gen").write_text("7")
    proc = _order(world, "check", "--environment", "dev", "--sha", world.shas["C"])
    assert proc.returncode == 1
    assert "names no commit SHA" in proc.stderr


def test_a_shallow_checkout_is_refused(world, tmp_path):
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{world.repo}", str(shallow)], check=True)
    _record(world, "applied", world.shas["B"])
    proc = _order(world, "check", "--environment", "dev", "--sha", world.shas["C"],
                  SWARM_RELEASE_ORDER_CHECKOUT=str(shallow))
    assert proc.returncode == 1
    assert "shallow" in proc.stderr


def test_the_record_says_what_shipped_and_who_shipped_it(world):
    _record(world, "applied", world.shas["C"])
    record = json.loads((world.gcs / "state/releases/dev/applied.json").read_text())
    assert record["sha"] == world.shas["C"]
    assert record["what"] == "applied" and record["environment"] == "dev"
    assert record["workflow"] == "release" and record["run_id"] == "42"


def _lock(world, **env: str) -> subprocess.CompletedProcess:
    return _order(world, "lock", "--environment", "dev", "--sha", world.shas["C"], **env)


def test_the_lock_is_exclusive_and_released_by_generation(world):
    """MUTATION: drop --if-generation-match=0 from the create."""
    first = _lock(world)
    assert first.returncode == 0, first.stderr
    generation = first.stdout.strip().removeprefix("generation=")
    assert generation.isdigit(), first.stdout
    second = _lock(world, SWARM_RELEASE_LOCK_WAIT="0", GITHUB_WORKFLOW="hotfix")
    assert second.returncode == 1
    assert "held by release run 42" in second.stderr
    assert "Nothing was changed" in second.stderr
    released = _order(world, "unlock", "--environment", "dev", "--generation", generation)
    assert released.returncode == 0, released.stderr
    assert not (world.gcs / "state/releases/dev/apply.lock").exists()
    assert _lock(world, GITHUB_WORKFLOW="hotfix").returncode == 0


def test_unlock_never_deletes_another_lane_s_lock(world):
    first = _lock(world)
    generation = first.stdout.strip().removeprefix("generation=")
    # Broken as stale and taken by the other lane meanwhile.
    (world.gcs / "state/releases/dev/apply.lock").unlink()
    (world.gcs / "state/releases/dev/apply.lock.gen").unlink()
    assert _lock(world, GITHUB_WORKFLOW="hotfix").returncode == 0
    proc = _order(world, "unlock", "--environment", "dev", "--generation", generation)
    assert proc.returncode == 0, proc.stderr
    assert "left alone" in proc.stderr
    assert (world.gcs / "state/releases/dev/apply.lock").exists()


def test_a_lock_whose_holder_died_is_broken_once_stale(world):
    assert _lock(world).returncode == 0
    path = world.gcs / "state/releases/dev/apply.lock"
    held = json.loads(path.read_text())
    held["at"] = 1
    path.write_text(json.dumps(held))
    proc = _lock(world, SWARM_RELEASE_LOCK_WAIT="0", SWARM_RELEASE_LOCK_STALE="60", GITHUB_WORKFLOW="hotfix")
    assert proc.returncode == 0, proc.stderr
    assert "Breaking it" in proc.stderr
    assert json.loads(path.read_text())["holder"].startswith("hotfix run")


def test_unless_superseded_turns_a_failure_green_only_when_superseded(world, tmp_path):
    out = tmp_path / "github_output"
    fail = ["--", "sh", "-c", "exit 3"]
    proc = _order(world, "unless-superseded", "--environment", "dev", "--sha", world.shas["B"], *fail,
                  GITHUB_OUTPUT=str(out))
    assert proc.returncode == 3, "a failure that is not explained by a newer commit stays a failure"
    assert not out.exists() or "superseded=true" not in out.read_text()

    _record(world, "applied", world.shas["C"])
    proc = _order(world, "unless-superseded", "--environment", "dev", "--sha", world.shas["B"], *fail,
                  GITHUB_OUTPUT=str(out))
    assert proc.returncode == 0, proc.stderr
    assert out.read_text().splitlines()[-1] == "superseded=true"

    ok = _order(world, "unless-superseded", "--environment", "dev", "--sha", world.shas["B"], "--", "true")
    assert ok.returncode == 0


def test_unless_superseded_is_the_command_alone_on_prod(world):
    """A prod release is a person's choice of commit, behind its approval; a
    deliberate rollback must not be called superseded."""
    _record(world, "applied", world.shas["C"])
    proc = _order(world, "unless-superseded", "--environment", "prod", "--sha", world.shas["A"], "--", "sh", "-c", "exit 4")
    assert proc.returncode == 4


@pytest.mark.parametrize("script", [ORDER_SH, GATE_SH], ids=lambda p: p.name)
def test_the_scripts_are_executable_and_shellcheck_clean(script):
    assert os.access(script, os.X_OK)
    lines = [l for l in script.read_text().splitlines() if l.strip() and not l.startswith("#")]
    assert lines[0] == "set -euo pipefail"
    # Skipped, not crashed, where shellcheck is absent -- the same skip as
    # test_release_acceptance_job.py, test_register_tenant_forge_reader.py and
    # test_build_mirror.py. A example-ci VM booted from an image without it
    # raised FileNotFoundError here (PR 1184, run 38552512519); the image now
    # installs it, so CI runs the check.
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck is not installed here")
    proc = subprocess.run(["shellcheck", "-x", str(script)], capture_output=True, text=True, cwd=REPO)
    assert proc.returncode == 0, proc.stdout

