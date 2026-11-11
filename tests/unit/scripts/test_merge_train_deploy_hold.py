"""The merge train holds a green train's merge while main's build of the
commit before it is running and nothing has deployed for an hour
(DEPLOY-STARVATION HOLD, issue 3336, part of #2688).

WHY. Main's application runs share one concurrency group with
cancel-in-progress (#2438), so a merge cancels the previous commit's running
`build images` job and that commit's release ends green, superseded. On 5
November 2026 `build images` started 18 to 44 minutes after its push (it needs
main's test jobs, which queue behind the train's and the pull requests' CI)
and then took about 19 minutes, while the train landed a batch every 20 to 40
minutes: no release had deployed since 21:04Z on 4 November. So, before a
green train merges, the tick reads release.yml's runs on main: if no deploy
job succeeded in the last 60 minutes AND main's newest push run has a `build
images` job IN PROGRESS, the train waits -- open and green, said once in its
comment -- until that job ends or 25 minutes pass, and never holds twice.

All offline: bash + jq, and a fake `gh` that serves a JSON world of runs and
jobs, keeps the train's body (the hold's start is recorded in its state
block, so the bound survives ticks) and its comments in files.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "merge-train.sh"
GH_REPO = "example-org/example-repo"
RUN_URL = "https://github.com/example-org/example-repo/actions/runs/42000000001"
TRAIN = 3400

FAKE_GH = r'''#!{python}
import json, os, sys, urllib.parse

args = sys.argv[1:]
with open(os.environ["FAKE_WORLD"]) as fh:
    world = json.load(fh)
with open(os.environ["FAKE_GH_LOG"], "a") as fh:
    fh.write(" ".join(args) + "\n")

if args[:2] == ["pr", "comment"]:
    body = args[args.index("--body-file") + 1]
    with open(os.environ["FAKE_COMMENTS"], "a") as out, open(body) as src:
        out.write(src.read() + "\n----\n")
    sys.exit(0)
if args[:1] == ["api"]:
    if "-X" in args and args[args.index("-X") + 1] == "PATCH":
        if world.get("patch_fails"):
            print("HTTP 502", file=sys.stderr)
            sys.exit(1)
        payload = json.load(sys.stdin)
        with open(os.environ["FAKE_BODY"], "w") as fh:
            fh.write(payload["body"])
        sys.exit(0)
    path = next(a for a in args[1:] if a.startswith("repos/"))
    parsed = urllib.parse.urlparse(path)
    query = dict(urllib.parse.parse_qsl(parsed.query))
    parts = parsed.path.split("/")
    if world.get("unreadable"):
        print("HTTP 502: Bad Gateway", file=sys.stderr)
        sys.exit(1)
    if parts[3:5] == ["actions", "workflows"] and parts[6] == "runs":
        assert query.get("branch") == "main", query
        runs = world["runs"].get(parts[5], [])
        if "event" in query:
            runs = [r for r in runs if r.get("event", "push") == query["event"]]
        runs = runs[: int(query.get("per_page", 30))]
        print(json.dumps({"workflow_runs": [{k: v for k, v in r.items() if k != "jobs"} for r in runs]}))
        sys.exit(0)
    if parts[3:5] == ["actions", "runs"] and parts[6] == "jobs":
        run = next(r for rs in world["runs"].values() for r in rs if r["id"] == int(parts[5]))
        print(json.dumps({"jobs": run.get("jobs", [])}))
        sys.exit(0)
print("fake gh: unexpected: " + " ".join(args), file=sys.stderr)
sys.exit(64)
'''


def _need(*tools: str) -> None:
    # Asserted, not skipped: the train runs on these, and a skip would hide
    # that the hold went untested.
    for tool in tools:
        assert shutil.which(tool) is not None, f"{tool} is required"


def _sha() -> str:
    return secrets.token_hex(20)


def _iso(minutes_ago: float) -> str:
    return datetime.fromtimestamp(time.time() - minutes_ago * 60, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _job(name: str, status: str, conclusion: str | None = None, *, completed_ago: float | None = None) -> dict:
    job = {"name": name, "status": status, "conclusion": conclusion,
           "started_at": _iso(30) if status != "queued" else None,
           "completed_at": _iso(completed_ago) if completed_ago is not None else None}
    return job


def build_run(build_status: str, *, sha: str, run_id: int = 501) -> dict:
    """Main's newest application.yml push run, its `build images` job in
    `build_status` (queued: not started; in_progress: started; completed)."""
    conclusion = "success" if build_status == "completed" else None
    return {"id": run_id, "head_sha": sha, "event": "push", "status": "in_progress",
            "created_at": _iso(40),
            "jobs": [_job("changes", "completed", "success", completed_ago=35),
                     _job("format / unit tests", "completed", "success", completed_ago=20),
                     _job("build images", build_status, conclusion,
                          completed_ago=1 if build_status == "completed" else None)]}


def release_run(run_id: int, *, deployed_ago: float | None, created_ago: float = 90) -> dict:
    """A release on main: its deploy job succeeded `deployed_ago` minutes ago,
    or (None) was skipped because the release ended superseded."""
    deploy = (_job("deploy (dev)", "completed", "success", completed_ago=deployed_ago)
              if deployed_ago is not None else _job("deploy (dev)", "completed", "skipped", completed_ago=created_ago - 5))
    return {"id": run_id, "head_sha": _sha(), "event": "push", "status": "completed",
            "conclusion": "success", "created_at": _iso(created_ago),
            "jobs": [_job("images and scan (dev)", "completed", "success", completed_ago=created_ago - 5), deploy]}


def _state_body(hold: str | None = None) -> str:
    lines = ["<!-- merge-train-state", "id=42000000001-1", f"base={'a' * 40}", f"head={'b' * 40}",
             f"started={int(time.time()) - 1800}", "dry_run=false", f"prs=3390:{'c' * 40}", "suspects="]
    if hold is not None:
        lines.append(f"hold={hold}")
    lines.append("-->")
    return "A merge train.\n\n" + "\n".join(lines) + "\n"


class Hold:
    def __init__(self, tmp_path: Path, world: dict, body: str):
        _need("bash", "jq", "awk")
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir(exist_ok=True)
        fake = self.bin / "gh"
        fake.write_text(FAKE_GH.replace("{python}", sys.executable))
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.world = tmp_path / "world.json"
        self.world.write_text(json.dumps(world))
        self.body = tmp_path / "body.md"
        self.body.write_text(body)
        self.comments = tmp_path / "comments.md"
        self.log = tmp_path / "gh.log"

    def run(self, *, dry: bool = False) -> tuple[str, str]:
        env = dict(os.environ)
        env.update({"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "GH_REPO": GH_REPO,
                    "GH_TOKEN": "fake-" + secrets.token_hex(4), "RUN_URL": RUN_URL,
                    "FAKE_WORLD": str(self.world), "FAKE_GH_LOG": str(self.log),
                    "FAKE_BODY": str(self.body), "FAKE_COMMENTS": str(self.comments), "NO_COLOR": "1",
                    "MERGE_TRAIN_MERGE_RETRY_WAITS": "0 0"})
        env.pop("GITHUB_STEP_SUMMARY", None)
        cmd = ["bash", str(SCRIPT), "hold", "--train", str(TRAIN), "--body", str(self.body)]
        if dry:
            cmd.append("--dry-run")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False, env=env)
        assert proc.returncode == 0, proc.stderr
        verdict, _, why = proc.stdout.strip().partition("\t")
        assert verdict in ("hold", "merge"), proc.stdout
        return verdict, why

    def said(self) -> str:
        return self.comments.read_text() if self.comments.exists() else ""

    def recorded(self) -> str | None:
        m = re.search(r"^hold=(.*)$", self.body.read_text(), re.M)
        return m.group(1) if m else None


def starving(build_status: str, sha: str) -> dict:
    """Nothing deployed in the last 60 minutes: the last deploy was 12 hours
    ago, and every release since ended superseded."""
    return {"runs": {
        "application.yml": [build_run(build_status, sha=sha)],
        "release.yml": [release_run(601, deployed_ago=None, created_ago=30),
                        release_run(600, deployed_ago=None, created_ago=70),
                        release_run(599, deployed_ago=720, created_ago=740)],
    }}


# --------------------------------------------------------------------------
# holds
# --------------------------------------------------------------------------


def test_a_starving_main_with_its_build_in_progress_holds_and_says_so_once(tmp_path):
    sha = _sha()
    hold = Hold(tmp_path, starving("in_progress", sha), _state_body())
    verdict, why = hold.run()
    assert verdict == "hold", why
    said = hold.said()
    assert f"holding the merge so main's build of {sha[:12]} can finish and deploy" in said, said
    assert "nothing has deployed since" in said, said
    # The hold's start is in the train's state, with the build it waits for.
    recorded = hold.recorded()
    assert recorded is not None and recorded.endswith(f":{sha}"), hold.body.read_text()
    assert abs(int(recorded.split(":")[0]) - time.time()) < 120

    # The next tick, the build still running: still held, nothing said again.
    verdict, why = hold.run()
    assert verdict == "hold", why
    assert hold.said().count("holding the merge") == 1, hold.said()


def test_the_held_train_merges_once_the_build_has_finished(tmp_path):
    sha = _sha()
    world = starving("in_progress", sha)
    hold = Hold(tmp_path, world, _state_body())
    assert hold.run()[0] == "hold"
    world["runs"]["application.yml"] = [build_run("completed", sha=sha)]
    hold.world.write_text(json.dumps(world))
    verdict, why = hold.run()
    assert verdict == "merge", why


def test_a_dry_run_says_what_it_would_hold_and_writes_nothing(tmp_path):
    sha = _sha()
    body = _state_body()
    hold = Hold(tmp_path, starving("in_progress", sha), body)
    verdict, why = hold.run(dry=True)
    assert verdict == "hold" and f"main's build of {sha[:12]}" in why, why
    assert hold.said() == "" and hold.body.read_text() == body
    assert "PATCH" not in hold.log.read_text()


# --------------------------------------------------------------------------
# merges
# --------------------------------------------------------------------------


def test_a_recent_deploy_merges(tmp_path):
    world = starving("in_progress", _sha())
    world["runs"]["release.yml"].insert(0, release_run(602, deployed_ago=20, created_ago=45))
    hold = Hold(tmp_path, world, _state_body())
    verdict, why = hold.run()
    assert verdict == "merge", why
    assert hold.said() == "" and hold.recorded() is None


@pytest.mark.parametrize("status", ["queued", "completed"])
def test_a_build_not_started_or_finished_merges(tmp_path, status):
    """A build that has not started is cancelled for free; one that finished
    is not cancelled at all."""
    hold = Hold(tmp_path, starving(status, _sha()), _state_body())
    verdict, why = hold.run()
    assert verdict == "merge", why
    assert hold.said() == "" and hold.recorded() is None


def test_twenty_five_minutes_of_holding_merges(tmp_path):
    sha = _sha()
    began = int(time.time()) - 26 * 60
    hold = Hold(tmp_path, starving("in_progress", sha), _state_body(hold=f"{began}:{sha}"))
    verdict, why = hold.run()
    assert verdict == "merge", why
    assert "25" in why, why


def test_a_second_hold_of_the_same_train_merges(tmp_path):
    """The train held once, for another build; a second build in progress is
    never held for."""
    began = int(time.time()) - 10 * 60
    hold = Hold(tmp_path, starving("in_progress", _sha()), _state_body(hold=f"{began}:{_sha()}"))
    verdict, why = hold.run()
    assert verdict == "merge", why
    assert hold.said() == ""


def test_an_unreadable_api_never_holds(tmp_path):
    world = starving("in_progress", _sha())
    world["unreadable"] = True
    hold = Hold(tmp_path, world, _state_body())
    assert hold.run()[0] == "merge"


def test_a_hold_that_cannot_be_recorded_does_not_hold(tmp_path):
    """Unrecorded, the 25-minute bound would not survive the tick."""
    world = starving("in_progress", _sha())
    world["patch_fails"] = True
    hold = Hold(tmp_path, world, _state_body())
    assert hold.run()[0] == "merge"
    assert hold.said() == ""


# --------------------------------------------------------------------------
# the tick
# --------------------------------------------------------------------------


def _function(name: str) -> str:
    text = SCRIPT.read_text()
    start = text.index(f"\n{name}() {{")
    return text[start:text.index("\n}\n", start)]


def test_a_green_train_asks_the_hold_before_it_lands_and_a_dry_run_says_it():
    handle = _function("handle_train")
    green = handle[handle.index("    green)"):handle.index("    red)")]
    asked = green.index("deploy_hold ")
    assert asked < green.index("Dry run:** nothing landed") < green.index("land_train ")
    assert re.search(r'if \[\[ "\$\{HOLD\}" == "true" \]\]; then[^\n]*\n[^\n]*summary[^\n]*\n\s+return 0', green), green
    assert "would first have held" in green


def test_a_train_from_before_the_hold_still_reads_its_state(tmp_path):
    """`hold` is optional: a train body written before it is judged, not
    abandoned."""
    handle = _function("handle_train")
    required = re.findall(r'cmd_state_get --body "\$\{body\}" --key (\w+) 2>/dev/null\)" \\', handle)
    assert "hold" not in required and set(required) >= {"id", "base", "head", "started"}
