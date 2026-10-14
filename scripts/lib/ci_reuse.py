#!/usr/bin/env python3
"""Main's push run reuses the test jobs a run of the SAME TREE already passed
(B1, owner decision 2026-10-14; docs/ci.md "Main reuses the tested tree").

    ci_reuse.py record --needs JSON --python MODE --event EVENT --sha SHA --out FILE
    ci_reuse.py find   --repo OWNER/NAME --sha SHA [--github-output F] [--summary F]

WHY. Main's push run re-tested the exact tree its pull request had just
tested: a squash merge of an up-to-date pull request (strict merging) commits
the tree of the pull request's merge commit, which is what that pull request's
run checked out. Measured over the last 150 PRs (2026-10-14): the push run
took 12.8 min, and the release waits for its `build images`, which needs the
unit shards first -- merge to deployed was 26.7 min.

`record` (every run that is not main's): writes what this run proved, keyed by
the tree it checked out -- `tested-tree-<tree>.json`: the tree, the commit,
the event, the `changes` job's python mode, and every test job that concluded
`success`. The workflow uploads it as the artifact `tested-tree-<tree>`.

`find` (main's push run, in the `changes` job): looks the tree up and writes
`reused`, the JSON list of job ids main may skip, and why, to the job summary.
EXACT OR NOTHING. A job is reused only when every one of these holds:

  * a record for this exact tree hash exists, from a run of
    .github/workflows/application.yml in this repository (never a fork's),
    and that run's conclusion is `success`;
  * the run is tied to this commit: a pull_request run at the head of the pull
    request this commit merged, as the GitHub API names it; or a push (a
    merge-train branch) or merge_group run whose own commit has this tree, as
    git computes it here;
  * the job concluded `success` in the record AND in the API's list of that
    run's jobs -- every shard of a matrix job, 1..N with none missing. A job
    the run skipped is run again;
  * for the unit shards, the run ran the WHOLE suite (python=all): a pull
    request's selected subset (scripts/lib/select_unit_tests.py) is never
    reused as main's full run.

Anything else -- no record, an API that cannot be read, a second record that
disagrees -- reuses nothing, and main runs every job, as it did before.
tests/unit/scripts/test_ci_reuse.py holds each rule.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

WORKFLOW = ".github/workflows/application.yml"

# Each reusable job's id in application.yml and the check name its runs
# report. A matrix job's name is the part before its `(k/N)`.
# tests/unit/scripts/test_ci_reuse.py holds this to the workflow's own names.
REUSABLE = {
    "shell": "shellcheck",
    "workflows": "release workflow wiring (actionlint)",
    "ruff": "ruff check",
    "python-checks": "lock / contract / format / bridge install",
    "python-unit": "unit tests (",
    "ui-build": "swarm-ui typecheck / build",
    "ui-tests": "swarm-ui component tests (",
    "integration": "integration tests (emulator)",
    "manifests": "kubernetes manifests",
}
MATRIX = {"python-unit", "ui-tests"}


def record(needs: dict, python: str, event: str, sha: str, tree: str) -> dict:
    passed = sorted(k for k in REUSABLE if (needs.get(k) or {}).get("result") == "success")
    return {"tree": tree, "sha": sha, "event": event, "python": python, "passed": passed}


def _matrix_passed(jobs: list[dict], prefix: str) -> bool:
    shards: dict[int, str] = {}
    total = None
    for job in jobs:
        name = str(job.get("name") or "")
        if not name.startswith(prefix):
            continue
        m = re.fullmatch(re.escape(prefix) + r"(\d+)/(\d+)\)", name)
        if not m:
            return False
        k, n = int(m.group(1)), int(m.group(2))
        if total is not None and n != total:
            return False
        total = n
        shards[k] = str(job.get("conclusion"))
    if not total or sorted(shards) != list(range(1, total + 1)):
        return False
    return all(c == "success" for c in shards.values())


def job_passed(jobs: list[dict], key: str) -> bool:
    name = REUSABLE[key]
    if key in MATRIX:
        return _matrix_passed(jobs, name)
    found = [j for j in jobs if j.get("name") == name]
    return len(found) == 1 and found[0].get("conclusion") == "success"


def decide(*, tree: str, repo: str, rec: dict, run: dict, jobs: list[dict],
           merged_head: str | None, head_tree: str | None) -> tuple[list[str], str]:
    """(job ids to reuse, why). Pure: every fact is passed in."""
    if rec.get("tree") != tree:
        return [], f"the record is of tree {str(rec.get('tree'))[:12]}, not {tree[:12]}"
    if run.get("path") != WORKFLOW:
        return [], f"run {run.get('id')} is of {run.get('path')}, not {WORKFLOW}"
    if (run.get("head_repository") or {}).get("full_name") != repo:
        return [], f"run {run.get('id')} is from another repository"
    if run.get("conclusion") != "success":
        return [], f"run {run.get('id')} concluded {run.get('conclusion')}"
    event = run.get("event")
    if event != rec.get("event"):
        return [], f"the record says {rec.get('event')}, run {run.get('id')} is a {event}"
    if event == "pull_request":
        if not merged_head:
            return [], "the GitHub API names no pull request merged as this commit"
        if run.get("head_sha") != merged_head:
            return [], f"run {run.get('id')} tested {str(run.get('head_sha'))[:12]}, not the merged head {merged_head[:12]}"
    elif event in ("push", "merge_group"):
        if head_tree != tree:
            return [], f"run {run.get('id')}'s commit has tree {str(head_tree)[:12]}, not {tree[:12]}"
    else:
        return [], f"run {run.get('id')} is a {event}, which is never reused"
    reused = []
    for key in REUSABLE:
        if key not in rec.get("passed", []):
            continue
        if key == "python-unit" and rec.get("python") != "all":
            continue
        if job_passed(jobs, key):
            reused.append(key)
    if not reused:
        return [], f"run {run.get('id')} passed no job this run can skip"
    return reused, f"reused from run {run.get('id')} ({event}, {str(run.get('head_sha'))[:12]})"


# --------------------------------------------------------------------------
# The impure half: the GitHub API through `gh`, and git.
# --------------------------------------------------------------------------
def _gh(path: str) -> bytes:
    return subprocess.run(["gh", "api", "-H", "Accept: application/vnd.github+json", path],
                          check=True, capture_output=True, timeout=120).stdout


def _gh_json(path: str):
    return json.loads(_gh(path))


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True, timeout=120).stdout.strip()


def tree_of(sha: str) -> str | None:
    try:
        return _git("rev-parse", f"{sha}^{{tree}}")
    except subprocess.CalledProcessError:
        try:
            _git("fetch", "--no-tags", "--depth=1", "origin", sha)
            return _git("rev-parse", f"{sha}^{{tree}}")
        except subprocess.CalledProcessError:
            return None


def merged_head_of(repo: str, sha: str) -> str | None:
    for pr in _gh_json(f"repos/{repo}/commits/{sha}/pulls"):
        if pr.get("merge_commit_sha") == sha and pr.get("merged_at"):
            return (pr.get("head") or {}).get("sha")
    return None


def find(repo: str, sha: str) -> tuple[list[str], str]:
    tree = tree_of(sha)
    if not tree:
        return [], f"could not read the tree of {sha[:12]}"
    listing = _gh_json(f"repos/{repo}/actions/artifacts?name=tested-tree-{tree}&per_page=20")
    artifacts = [a for a in listing.get("artifacts", []) if not a.get("expired")]
    if not artifacts:
        return [], f"no run recorded tree {tree[:12]}"
    merged_head = None
    try:
        merged_head = merged_head_of(repo, sha)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        merged_head = None
    why = ""
    for art in sorted(artifacts, key=lambda a: a.get("created_at") or "", reverse=True):
        run_id = (art.get("workflow_run") or {}).get("id")
        if not run_id:
            continue
        raw = _gh(f"repos/{repo}/actions/artifacts/{art['id']}/zip")
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            rec = json.loads(z.read(f"tested-tree-{tree}.json"))
        run = _gh_json(f"repos/{repo}/actions/runs/{run_id}")
        jobs = _gh_json(f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100").get("jobs", [])
        head_tree = tree_of(run["head_sha"]) if run.get("event") in ("push", "merge_group") else None
        reused, why = decide(tree=tree, repo=repo, rec=rec, run=run, jobs=jobs,
                             merged_head=merged_head, head_tree=head_tree)
        if reused:
            return reused, why
    return [], why or f"no usable record of tree {tree[:12]}"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--needs", required=True)
    r.add_argument("--python", required=True)
    r.add_argument("--event", required=True)
    r.add_argument("--sha", required=True)
    r.add_argument("--out-dir", required=True)
    r.add_argument("--github-output")
    f = sub.add_parser("find")
    f.add_argument("--repo", required=True)
    f.add_argument("--sha", required=True)
    f.add_argument("--github-output")
    f.add_argument("--summary")
    args = ap.parse_args(argv)

    if args.cmd == "record":
        tree = _git("rev-parse", "HEAD^{tree}")
        rec = record(json.loads(args.needs), args.python, args.event, args.sha, tree)
        out = Path(args.out_dir) / f"tested-tree-{tree}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rec, indent=1, sort_keys=True) + "\n")
        print(f"tree {tree}: passed {', '.join(rec['passed']) or 'nothing'} (python={rec['python']})")
        if args.github_output:
            with open(args.github_output, "a") as fh:
                fh.write(f"tree={tree}\n")
        return 0

    # find: ANY failure is "reuse nothing", never a failed job.
    try:
        reused, why = find(args.repo, args.sha)
    except Exception as exc:  # doubt runs everything, never fails the job
        reused, why = [], f"the lookup failed ({type(exc).__name__}); running every job"
    print(f"reused={json.dumps(reused)}: {why}")
    if args.github_output:
        with open(args.github_output, "a") as fh:
            fh.write(f"reused={json.dumps(reused)}\n")
    if args.summary:
        with open(args.summary, "a") as fh:
            fh.write("### Tested tree reuse\n\n")
            if reused:
                fh.write(f"**{why}.** Skipped here, having passed there on the identical tree: "
                         + ", ".join(f"`{k}`" for k in reused) + ".\n\n")
            else:
                fh.write(f"Nothing reused: {why}. Every job runs.\n\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
