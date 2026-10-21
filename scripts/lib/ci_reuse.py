#!/usr/bin/env python3
"""Main's push run, and a pull request's run, reuse the test jobs a run of the
SAME TREE already passed (B1, owner decision 2026-10-14; CI-TREE-REUSE, #1798,
owner approval 2026-10-21; docs/ci.md "Main reuses the tested tree").

    ci_reuse.py record --needs JSON --python MODE --event EVENT --sha SHA --out-dir DIR
    ci_reuse.py find   --repo OWNER/NAME --sha SHA [--event push|pull_request]
                       [--github-output F] [--summary F]
    ci_reuse.py gate   --needs JSON --event EVENT [--summary F]

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

`find --event pull_request` (a pull request from this repository, in the
`changes` job): since LANE CI-EARLY a lane's `swarm/**` push runs this
workflow, and its pull request then ran it again on the same tree -- 166
duplicate pairs and 51.5 VM-hours in the 24 h measured for #1798, 18% of all
VM hours. The pull request looks up the tree of the commit it checked out
(refs/pull/N/merge) and reuses a record only from `decide_pull_request`:

  * a `push` run of a `swarm/**` branch of .github/workflows/application.yml
    in this repository, `completed` with conclusion `success` -- a cancelled,
    failed or unfinished run is never reused;
  * whose own commit has this tree, as git computes it here. TREES, NEVER
    SHAS: once main moves, the merge commit's tree is not the lane head's, and
    nothing is reused -- the pull request then tests what it would merge;
  * the same per-job rules as main's: `success` in the record and in the API,
    every shard, and the unit shards only from the whole suite.

`gate` (ci-gate, after its own verdict): a job skipped by reuse counts as
passed only when the `reuse` step listed it, and a run cited it. A reusable
job this change reaches (REACH, the job's own `if:` before its reuse clause)
that was skipped without being listed fails the gate, and the summary names
the run whose green was reused.
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

# What each reusable job's own `if:` says before its reuse clause: the
# `changes` output it reads, how, and the value. gate() fails a job this
# condition reaches that was skipped and not listed as reused.
# test_every_reach_condition_is_the_job_s_own_if holds it to the workflow.
REACH = {
    "shell": ("app", "==", "true"),
    "workflows": ("app", "==", "true"),
    "ruff": ("app", "==", "true"),
    "python-checks": ("python", "in", ("all", "selected")),
    "python-unit": ("python", "!=", "none"),
    "ui-build": ("ui", "==", "true"),
    "ui-tests": ("ui", "==", "true"),
    "integration": ("app", "==", "true"),
    "manifests": ("app", "==", "true"),
}
# The branches whose push runs a pull request may reuse: the lanes' (CI-EARLY).
LANE_PREFIX = "swarm/"
# The events whose run looks a record up (the `changes` job's `reuse` step).
LOOKUP_EVENTS = ("push", "pull_request")


def reach_if(key: str) -> str:
    output, op, value = REACH[key]
    ref = f"needs.changes.outputs.{output}"
    if op == "in":
        return "(" + " || ".join(f"{ref} == '{v}'" for v in value) + ")"
    return f"{ref} {op} '{value}'"


def reached(key: str, outputs: dict) -> bool:
    output, op, value = REACH[key]
    got = str(outputs.get(output) or "")
    if op == "in":
        return got in value
    if op == "==":
        return got == value
    return got != value


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
    reused = _proven(rec, jobs)
    if not reused:
        return [], f"run {run.get('id')} passed no job this run can skip"
    return reused, f"reused from run {run.get('id')} ({event}, {str(run.get('head_sha'))[:12]})"


def _proven(rec: dict, jobs: list[dict]) -> list[str]:
    """The jobs the record AND the API show passed; the unit shards only from
    the whole suite."""
    reused = []
    for key in REUSABLE:
        if key not in rec.get("passed", []):
            continue
        if key == "python-unit" and rec.get("python") != "all":
            continue
        if job_passed(jobs, key):
            reused.append(key)
    return reused


def decide_pull_request(*, tree: str, repo: str, rec: dict, run: dict, jobs: list[dict],
                        head_tree: str | None) -> tuple[list[str], str]:
    """(job ids a pull request's run may skip, why). Pure, like decide():
    only a completed, green push run of a lane branch whose own commit has
    the tree this pull request checked out."""
    if rec.get("tree") != tree:
        return [], f"the record is of tree {str(rec.get('tree'))[:12]}, not {tree[:12]}"
    if run.get("path") != WORKFLOW:
        return [], f"run {run.get('id')} is of {run.get('path')}, not {WORKFLOW}"
    if (run.get("head_repository") or {}).get("full_name") != repo:
        return [], f"run {run.get('id')} is from another repository"
    event = run.get("event")
    if event != "push":
        return [], f"run {run.get('id')} is a {event}: a pull request reuses only a lane push"
    if rec.get("event") != event:
        return [], f"the record says {rec.get('event')}, run {run.get('id')} is a {event}"
    branch = str(run.get("head_branch") or "")
    if not branch.startswith(LANE_PREFIX):
        return [], f"run {run.get('id')} pushed {branch or 'no branch'}, not a {LANE_PREFIX}** lane"
    if run.get("status") != "completed":
        return [], f"run {run.get('id')} is {run.get('status')}, not completed"
    if run.get("conclusion") != "success":
        return [], f"run {run.get('id')} concluded {run.get('conclusion')}"
    if head_tree != tree:
        return [], f"run {run.get('id')}'s commit has tree {str(head_tree)[:12]}, not {tree[:12]}"
    reused = _proven(rec, jobs)
    if not reused:
        return [], f"run {run.get('id')} passed no job this run can skip"
    return reused, f"reused from run {run.get('id')} (push of {branch}, {str(run.get('head_sha'))[:12]})"


def gate(needs: dict, event: str) -> tuple[list[str], str]:
    """(problems, the summary line) for ci-gate. Pure. A skipped reusable job
    is a pass only when its own `if:` did not reach it, or the `reuse` step
    listed it and named the run it came from."""
    outputs = (needs.get("changes") or {}).get("outputs") or {}
    problems: list[str] = []
    try:
        reused = json.loads(outputs.get("reused") or "[]")
    except json.JSONDecodeError:
        reused = None
    if not isinstance(reused, list) or not all(isinstance(k, str) for k in reused):
        return [f"the reuse step's list is not a JSON list of job ids: {outputs.get('reused')!r}"], ""
    source = str(outputs.get("reused_from") or "")
    for key in reused:
        if key not in REUSABLE:
            problems.append(f"the reuse step listed {key}, which is not a reusable job")
    if reused and event not in LOOKUP_EVENTS:
        problems.append(f"a {event} run never reuses, but the reuse step listed {', '.join(reused)}")
    if reused and not source:
        problems.append(f"the reuse step listed {', '.join(reused)} but names no run they passed in")
    for key in REUSABLE:
        result = (needs.get(key) or {}).get("result")
        if result == "skipped" and key not in reused and reached(key, outputs):
            problems.append(f"{key} was skipped, this change reaches it, and the reuse step did not list it:"
                            " its green has no source")
    if reused:
        summary = (f"ci-gate passes on {len(reused)} job(s) reused from {source}, which passed them on this "
                   "exact tree: " + ", ".join(f"`{k}`" for k in reused) + ".")
    else:
        summary = "ci-gate reused nothing: every job this change reaches ran in this run."
    return problems, summary


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


def find(repo: str, sha: str, event: str = "push") -> tuple[list[str], str, str]:
    """(job ids to reuse, why, the URL of the run they passed in). `push` is
    main's lookup, unchanged; `pull_request` reads only lane push runs."""
    tree = tree_of(sha)
    if not tree:
        return [], f"could not read the tree of {sha[:12]}", ""
    listing = _gh_json(f"repos/{repo}/actions/artifacts?name=tested-tree-{tree}&per_page=20")
    artifacts = [a for a in listing.get("artifacts", []) if not a.get("expired")]
    if event == "pull_request":
        # A cheap first cut before any download; decide_pull_request still
        # checks the branch, the event and the tree against the run itself.
        artifacts = [a for a in artifacts
                     if str((a.get("workflow_run") or {}).get("head_branch") or "").startswith(LANE_PREFIX)]
    if not artifacts:
        return [], f"no run recorded tree {tree[:12]}", ""
    merged_head = None
    if event != "pull_request":
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
        if event == "pull_request":
            head_tree = tree_of(run["head_sha"]) if run.get("event") == "push" else None
            reused, why = decide_pull_request(tree=tree, repo=repo, rec=rec, run=run, jobs=jobs,
                                              head_tree=head_tree)
        else:
            head_tree = tree_of(run["head_sha"]) if run.get("event") in ("push", "merge_group") else None
            reused, why = decide(tree=tree, repo=repo, rec=rec, run=run, jobs=jobs,
                                 merged_head=merged_head, head_tree=head_tree)
        if reused:
            return reused, why, str(run.get("html_url") or f"https://github.com/{repo}/actions/runs/{run_id}")
    return [], why or f"no usable record of tree {tree[:12]}", ""


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
    f.add_argument("--event", choices=LOOKUP_EVENTS, default="push")
    f.add_argument("--github-output")
    f.add_argument("--summary")
    g = sub.add_parser("gate")
    g.add_argument("--needs", required=True)
    g.add_argument("--event", required=True)
    g.add_argument("--summary")
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

    if args.cmd == "gate":
        problems, summary = gate(json.loads(args.needs), args.event)
        for problem in problems:
            print(f"::error::{problem}")
        if not problems:
            print(summary)
            if args.summary:
                with open(args.summary, "a") as fh:
                    fh.write(f"### Tested tree reuse\n\n{summary}\n\n")
        return 1 if problems else 0

    # find: ANY failure is "reuse nothing", never a failed job.
    try:
        reused, why, source = find(args.repo, args.sha, args.event)
    except Exception as exc:  # doubt runs everything, never fails the job
        reused, why, source = [], f"the lookup failed ({type(exc).__name__}); running every job", ""
    print(f"reused={json.dumps(reused)}: {why}")
    if args.github_output:
        with open(args.github_output, "a") as fh:
            fh.write(f"reused={json.dumps(reused)}\n")
            fh.write(f"reused_from={source}\n")
    if args.summary:
        with open(args.summary, "a") as fh:
            fh.write("### Tested tree reuse\n\n")
            if reused:
                fh.write(f"**{why}** ({source}). Skipped here, having passed there on the identical tree: "
                         + ", ".join(f"`{k}`" for k in reused) + ".\n\n")
            else:
                fh.write(f"Nothing reused: {why}. Every job runs.\n\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
