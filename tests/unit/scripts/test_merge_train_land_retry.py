"""The merge train retries a merge GitHub fails transiently (part of #2688).

WHY. On 5 November 2026 at 17:50Z a train of five pull requests passed every
check, then its first squash-merge failed with `Post
"https://api.github.com/graphql": dial tcp ...:443: i/o timeout`. The tick
closed the train "landed only ... before stopping", and the same five boarded
a new train that ran every check again: about 40 minutes for one network
blip. GitHub answered 503, 504 and timeouts several times that day.

So `merge_one` retries one pull request's merge when the failure is transient
(a timeout, a reset connection, a TLS handshake, an HTTP 502, 503 or 504, a
rate limit), three attempts in all, and reads the pull request again before
each retry: one that merged although the reply was lost is counted as landed,
never merged twice. A real refusal (a conflict, a missing required check, the
head moved, any other 4xx) stops as before.

All offline: bash, jq, git, and a fake `gh` whose merges answer from a script
per pull request and whose pull requests live in a JSON file.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "merge-train.sh"
GH_REPO = "bogdan-alexandrescu/SwarmCloud"
RUN_URL = "https://github.com/bogdan-alexandrescu/SwarmCloud/actions/runs/42000000001"

TIMEOUT = 'Post "https://api.github.com/graphql": dial tcp 140.82.112.5:443: i/o timeout'
MOVED = "GraphQL: Head branch was modified. Review and try the merge again. (mergePullRequest)"

# A merge's outcome, by name: what the fake `gh pr merge` says, whether it
# exits 0, and whether the pull request is merged afterwards.
OUTCOMES = {
    "ok": ("", 0, True),
    "timeout": (TIMEOUT, 1, False),
    # The merge went through; only GitHub's reply was lost.
    "lost": (TIMEOUT, 1, True),
    "reset": ("read tcp 10.1.0.4:51234->140.82.112.5:443: read: connection reset by peer", 1, False),
    "tls": ("net/http: TLS handshake timeout", 1, False),
    "502": ("HTTP 502: Bad Gateway (https://api.github.com/graphql)", 1, False),
    "503": ("HTTP 503: Service Unavailable (https://api.github.com/graphql)", 1, False),
    "504": ("HTTP 504: Gateway Timeout (https://api.github.com/graphql)", 1, False),
    "ratelimit": ("HTTP 403: API rate limit exceeded for installation ID 1.", 1, False),
    "moved": (MOVED, 1, False),
    "conflict": ("GraphQL: Pull Request is not mergeable (mergePullRequest)", 1, False),
    "required": ("GraphQL: Required status check \"ci-gate\" is expected. (mergePullRequest)", 1, False),
    "422": ("HTTP 422: Validation Failed", 1, False),
}

FAKE_GH = r'''#!{python}
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as fh:
    fh.write(" ".join(args) + "\n")
path = os.environ["FAKE_WORLD"]
with open(path) as fh:
    world = json.load(fh)
outcomes = json.loads(os.environ["FAKE_OUTCOMES"])

def save():
    with open(path, "w") as fh:
        json.dump(world, fh)

if args[:2] == ["pr", "view"]:
    pr = world["prs"][args[2]]
    # The re-read between attempts asks for exactly these fields.
    if args[args.index("--json") + 1] == "state,headRefOid" and pr.get("reread_fails", 0) > 0:
        pr["reread_fails"] -= 1
        save()
        print(outcomes["timeout"][0], file=sys.stderr)
        sys.exit(1)
    fields = args[args.index("--json") + 1].split(",")
    full = {"state": pr["state"], "headRefOid": pr["head"], "title": pr["title"],
            "labels": [{"name": "ready"}]}
    print(json.dumps({f: full[f] for f in fields}))
    sys.exit(0)
if args[:2] == ["pr", "merge"]:
    pr = world["prs"][args[2]]
    assert os.environ.get("GH_TOKEN") == os.environ["APP_TOKEN"], "merges go through the App's token"
    assert args[args.index("--match-head-commit") + 1] == pr["head"]
    if pr["state"] == "MERGED":
        pr["merged_twice"] = True
        save()
        print("GraphQL: Pull request is already merged (mergePullRequest)", file=sys.stderr)
        sys.exit(1)
    said, code, merged = outcomes[pr["merges"].pop(0)]
    if merged:
        pr["state"] = "MERGED"
        pr["merged"] = pr.get("merged", 0) + 1
    elif pr.get("pushed_to_on_failure"):
        pr["head"] = "0" * 40
    save()
    if said:
        print(said, file=sys.stderr)
    sys.exit(code)
if args[:1] == ["api"] and "statuses" in " ".join(args):
    sys.exit(0)
print("fake gh: unexpected: " + " ".join(args), file=sys.stderr)
sys.exit(64)
'''


def _need(*tools: str) -> None:
    # Asserted, not skipped: a skip would hide that the landing went untested.
    for tool in tools:
        assert shutil.which(tool) is not None, f"{tool} is required"


def _git(cwd: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True, env=env).stdout.strip()


class Train:
    """A green train of pull requests on a main that does not move: the fake
    merges change no tree, so main's tree after them is the base's, which is
    the train's head here."""

    def __init__(self, tmp_path: Path, merges: dict[int, list[str]], extra: dict[int, dict] | None = None):
        _need("bash", "jq", "git")
        self.tmp = tmp_path
        origin = tmp_path / "origin.git"
        work = tmp_path / "seed"
        work.mkdir()
        _git(work, "init", "-q", "-b", "main")
        (work / "README").write_text("main\n")
        _git(work, "add", "README")
        _git(work, "commit", "-q", "-m", "main")
        _git(tmp_path, "clone", "-q", "--bare", str(work), str(origin))
        self.clone = tmp_path / "clone"
        _git(tmp_path, "clone", "-q", str(origin), str(self.clone))
        self.base = _git(self.clone, "rev-parse", "HEAD")
        self.numbers = list(merges)
        self.heads = {n: secrets.token_hex(20) for n in self.numbers}
        prs = {str(n): {"state": "OPEN", "head": self.heads[n], "title": f"Change {n}", "merges": m,
                        **(extra or {}).get(n, {})}
               for n, m in merges.items()}
        self.world = tmp_path / "world.json"
        self.world.write_text(json.dumps({"prs": prs}))
        self.log = tmp_path / "gh.log"
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        fake = self.bin / "gh"
        fake.write_text(FAKE_GH.replace("{python}", sys.executable))
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)

    def land(self) -> tuple[dict[str, str], str]:
        env = dict(os.environ)
        env.update({"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "GH_REPO": GH_REPO,
                    "GH_TOKEN": "fake-" + secrets.token_hex(4), "APP_TOKEN": "app-" + secrets.token_hex(4),
                    "REPO_DIR": str(self.clone), "RUN_URL": RUN_URL, "FAKE_WORLD": str(self.world),
                    "FAKE_GH_LOG": str(self.log), "FAKE_OUTCOMES": json.dumps(OUTCOMES),
                    "MERGE_TRAIN_MERGE_RETRY_WAITS": "0 0", "NO_COLOR": "1"})
        env.pop("GITHUB_STEP_SUMMARY", None)
        pairs = " ".join(f"{n}:{self.heads[n]}" for n in self.numbers)
        proc = subprocess.run(
            ["bash", str(SCRIPT), "land", "--base", self.base, "--head", self.base, "--prs", pairs,
             "--id", "42000000001-1"],
            capture_output=True, text=True, timeout=60, check=False, env=env)
        assert proc.returncode == 0, proc.stderr
        out = dict(line.split("\t", 1) for line in proc.stdout.splitlines() if "\t" in line)
        return out, proc.stdout + proc.stderr

    def merges_of(self, n: int) -> int:
        return sum(1 for line in self.log.read_text().splitlines() if line.startswith(f"pr merge {n} "))

    def pr(self, n: int) -> dict:
        return json.loads(self.world.read_text())["prs"][str(n)]


def test_a_timeout_then_success_lands_the_pull_request_and_the_rest_of_the_train(tmp_path):
    train = Train(tmp_path, {3317: ["timeout", "ok"], 3318: ["ok"], 3319: ["ok"], 3320: ["ok"], 3321: ["ok"]})
    out, said = train.land()
    assert out["rc"] == "0", said
    assert out["landed"] == "3317 3318 3319 3320 3321"
    assert out["reason"] == ""
    assert train.merges_of(3317) == 2
    assert all(train.pr(n)["merged"] == 1 for n in train.numbers)
    # The retry is logged in the run, with its reason, and the pull request
    # was read again before it.
    assert "::warning::merging #3317 failed transiently (attempt 1 of 3)" in said
    assert "i/o timeout" in said
    calls = train.log.read_text().splitlines()
    first, second = [i for i, c in enumerate(calls) if c.startswith("pr merge 3317 ")]
    assert any(c.startswith("pr view 3317 ") for c in calls[first + 1:second])


def test_three_timeouts_stop_the_train_as_before(tmp_path):
    train = Train(tmp_path, {3317: ["timeout", "timeout", "timeout"], 3318: ["ok"]})
    out, said = train.land()
    assert out["rc"] == "2", said
    assert out["landed"] == ""
    assert out["reason"].startswith("GitHub could not merge #3317 in 3 attempts, each failing transiently")
    assert "i/o timeout" in out["reason"]
    assert train.merges_of(3317) == 3
    assert train.merges_of(3318) == 0
    assert said.count("::warning::merging #3317 failed transiently") == 2


def test_a_head_moved_refusal_is_not_retried(tmp_path):
    train = Train(tmp_path, {3317: ["moved", "ok"], 3318: ["ok"]})
    out, said = train.land()
    assert out["rc"] == "2", said
    assert out["landed"] == ""
    assert out["reason"] == f"GitHub refused to merge #3317: {MOVED} "
    assert train.merges_of(3317) == 1
    assert "failed transiently" not in said


def test_a_merge_whose_reply_timed_out_is_found_on_the_re_read_and_counted_once(tmp_path):
    train = Train(tmp_path, {3317: ["lost"], 3318: ["ok"]})
    out, said = train.land()
    assert out["rc"] == "0", said
    assert out["landed"] == "3317 3318"
    assert train.merges_of(3317) == 1
    assert train.pr(3317)["merged"] == 1
    assert not train.pr(3317).get("merged_twice")
    assert "counted as landed, not merged again" in said


def test_a_merge_whose_last_reply_timed_out_is_found_on_the_re_read_too(tmp_path):
    train = Train(tmp_path, {3317: ["timeout", "timeout", "lost"], 3318: ["ok"]})
    out, said = train.land()
    assert out["rc"] == "0", said
    assert out["landed"] == "3317 3318"
    assert train.merges_of(3317) == 3


def test_a_lost_reply_and_a_failed_re_read_still_merges_once(tmp_path):
    # The re-read fails too, so the merge is tried again: GitHub refuses a
    # pull request already merged, and the read after that refusal lands it.
    train = Train(tmp_path, {3317: ["lost"], 3318: ["ok"]}, {3317: {"reread_fails": 1}})
    out, said = train.land()
    assert out["rc"] == "0", said
    assert out["landed"] == "3317 3318"
    assert train.pr(3317)["merged"] == 1
    assert train.merges_of(3317) == 2


@pytest.mark.parametrize("outcome", ["reset", "tls", "502", "503", "504", "ratelimit"])
def test_a_transient_failure_is_retried(tmp_path, outcome):
    train = Train(tmp_path, {3317: [outcome, "ok"], 3318: ["ok"]})
    out, said = train.land()
    assert out["rc"] == "0", said
    assert out["landed"] == "3317 3318"
    assert train.merges_of(3317) == 2


@pytest.mark.parametrize("outcome", ["conflict", "required", "422"])
def test_a_real_refusal_stops_at_once(tmp_path, outcome):
    train = Train(tmp_path, {3317: [outcome, "ok"], 3318: ["ok"]})
    out, said = train.land()
    assert out["rc"] == "2", said
    assert out["landed"] == ""
    assert train.merges_of(3317) == 1
    assert out["reason"].startswith("GitHub refused to merge #3317: ")


def test_a_failure_part_way_keeps_what_landed_before_it(tmp_path):
    train = Train(tmp_path, {3317: ["ok"], 3318: ["503", "503", "503"], 3319: ["ok"]})
    out, said = train.land()
    assert out["rc"] == "2", said
    assert out["landed"] == "3317"
    assert "HTTP 503" in out["reason"]
    assert train.merges_of(3319) == 0


def test_a_pull_request_pushed_to_between_attempts_is_not_retried(tmp_path):
    train = Train(tmp_path, {3317: ["timeout", "ok"], 3318: ["ok"]}, {3317: {"pushed_to_on_failure": True}})
    out, said = train.land()
    assert out["rc"] == "2", said
    assert out["landed"] == ""
    assert "#3317 changed while its merge was being retried" in out["reason"]
    assert train.merges_of(3317) == 1


def test_the_default_waits_are_three_attempts_of_a_short_growing_wait():
    text = SCRIPT.read_text()
    assert 'MERGE_RETRY_WAITS="${MERGE_TRAIN_MERGE_RETRY_WAITS:-10 30}"' in text


def test_land_refuses_waits_that_are_not_seconds(tmp_path):
    env = dict(os.environ, GH_REPO=GH_REPO, APP_TOKEN="x", REPO_DIR=str(tmp_path), RUN_URL=RUN_URL,
               MERGE_TRAIN_MERGE_RETRY_WAITS="10s")
    sha = "a" * 40
    proc = subprocess.run(["bash", str(SCRIPT), "land", "--base", sha, "--head", sha, "--prs", f"1:{sha}",
                           "--id", "1-1"], capture_output=True, text=True, timeout=30, check=False, env=env)
    assert proc.returncode != 0
    assert "MERGE_TRAIN_MERGE_RETRY_WAITS" in proc.stderr
