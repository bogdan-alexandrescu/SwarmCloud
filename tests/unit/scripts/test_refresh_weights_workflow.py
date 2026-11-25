"""The scheduled test-weights refresh (CI scanner gates, the epic's weights item).

`.github/workflows/refresh-weights.yml` runs `scripts/refresh-unit-weights.sh`
and `scripts/refresh-ui-weights.sh` on a schedule, off the merge path, and
`scripts/refresh-weights-pr.sh` opens what they changed as one pull request
from `maint/test-weights` with the merge App's token -- never a push to the
default branch.

The script runs end to end here against a local bare remote (git's
`url.<file>.insteadOf` stands in for github.com) and a fake `gh`: no
network, no credential.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[3]
WORKFLOW = REPO / ".github" / "workflows" / "refresh-weights.yml"
SCRIPT = REPO / "scripts" / "refresh-weights-pr.sh"
UNIT = "tests/unit/scripts/unit_test_durations.json"
UI = "tests/unit/scripts/ui_test_durations.json"
BRANCH = "maint/test-weights"


def _workflow() -> dict[Any, Any]:
    return yaml.safe_load(WORKFLOW.read_text())


def _steps() -> list[dict[str, Any]]:
    return _workflow()["jobs"]["refresh"]["steps"]


# --- the workflow ---------------------------------------------------------------


def test_it_runs_only_on_the_schedule_and_by_hand_off_the_merge_path():
    """MUTATION: add pull_request, push or merge_group, and it joins the
    checks a pull request, the train or a release waits for."""
    workflow = _workflow()
    # PyYAML reads the bare key `on` as True.
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"schedule", "workflow_dispatch"}, triggers
    assert triggers["schedule"] and all("cron" in s for s in triggers["schedule"]), triggers


def test_it_holds_read_permissions_and_the_app_key_only_where_it_must():
    workflow = _workflow()
    assert workflow["permissions"] == {}, workflow["permissions"]
    (job_id,) = workflow["jobs"]
    job = workflow["jobs"][job_id]
    assert job["permissions"] == {"contents": "read", "actions": "read"}, job["permissions"]
    assert job["environment"] == {"name": "repo-secrets", "deployment": False}, job
    assert "refs/heads/main" in job["if"] and "fork == false" in job["if"], job["if"]
    assert job.get("timeout-minutes"), job


def test_the_checkout_keeps_no_credential_and_the_token_is_minted_only_for_a_change():
    steps = _steps()
    (checkout,) = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert checkout["with"]["persist-credentials"] is False, checkout
    (mint,) = [s for s in steps if str(s.get("uses", "")).startswith("actions/create-github-app-token@")]
    assert "steps.refresh.outputs.changed == 'true'" in mint["if"], mint
    perms = {k: v for k, v in mint["with"].items() if k.startswith("permission-")}
    assert perms == {"permission-contents": "write", "permission-pull-requests": "write"}, perms
    runs = [str(s.get("run", "")) for s in steps if s.get("run")]
    assert runs == ["scripts/refresh-weights-pr.sh refresh", "scripts/refresh-weights-pr.sh open"], runs
    (refresh,) = [s for s in steps if s.get("id") == "refresh"]
    assert refresh["env"]["GH_TOKEN"] == "${{ github.token }}", refresh["env"]
    (open_step,) = [s for s in steps if s.get("run") == "scripts/refresh-weights-pr.sh open"]
    assert open_step["env"]["APP_TOKEN"] == "${{ steps.app-token.outputs.token }}", open_step["env"]
    assert "steps.refresh.outputs.changed == 'true'" in open_step["if"], open_step


def test_the_script_has_the_house_shape():
    text = SCRIPT.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    effective = [ln.strip() for ln in text.splitlines()[1:] if ln.strip() and not ln.strip().startswith("#")]
    assert effective[0] == "set -euo pipefail"
    assert os.access(SCRIPT, os.X_OK)


# --- the script, end to end -----------------------------------------------------

FAKE_GH = r"""#!/usr/bin/env bash
set -euo pipefail
if [ "${GH_TOKEN:-}" = "${EXPECT_TOKEN:?}" ]; then t=app; else t=other; fi
printf '%s token=%s\n' "$*" "${t}" >>"${GH_LOG:?}"
case "$1 $2" in
  "pr list") cat "${GH_OPEN:?}" ;;
  "pr create") echo "https://github.invalid/pr/1" ;;
  *) exit 64 ;;
esac
"""


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def _setup(tmp_path: Path, *, unit_edit: bool = True, ui_edit: bool = False, ui_fails: bool = False,
           open_prs: str = "[]") -> dict[str, Any]:
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    shutil.copytree(REPO / "scripts" / "lib", tree / "scripts" / "lib")
    shutil.copy2(SCRIPT, tree / "scripts" / SCRIPT.name)
    (tree / "tests" / "unit" / "scripts").mkdir(parents=True)
    (tree / UNIT).write_text('{\n "tests/unit/a.py": 1.0\n}\n')
    (tree / UI).write_text('{}\n')
    (tree / "README").write_text("x\n")

    def fake_refresh(name: str, path: str, edit: bool, fails: bool) -> None:
        body = "#!/usr/bin/env bash\nset -euo pipefail\n"
        if edit:
            body += f"printf '{{\\n \"tests/unit/a.py\": 2.5\\n}}\\n' > \"$(dirname \"$0\")/../{path}\"\n"
        # Something else the run left in the tree: never committed.
        body += 'echo stray > "$(dirname "$0")/../README"\n'
        if fails:
            body += "echo 'no run could be read' >&2; exit 1\n"
        script = tree / "scripts" / name
        script.write_text(body)
        script.chmod(0o755)

    fake_refresh("refresh-unit-weights.sh", UNIT, unit_edit, False)
    fake_refresh("refresh-ui-weights.sh", UI, ui_edit, ui_fails)

    _git(tree, "init", "-q", "-b", "main")
    _git(tree, "config", "user.email", "t@example.invalid")
    _git(tree, "config", "user.name", "t")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-q", "-m", "main")

    remotes = tmp_path / "remotes"
    bare = remotes / "acme" / "widgets.git"
    bare.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(tree), str(bare)], check=True)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    (tmp_path / "open.json").write_text(open_prs)
    token = secrets.token_hex(16)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SWARM_", "GIT_", "GH_", "GITHUB_"))}
    env.update(
        PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
        SWARM_ENV_FILE=str(tmp_path / "no.env"), NO_COLOR="1", TMPDIR=str(tmp_path),
        GH_LOG=str(tmp_path / "gh.log"), GH_OPEN=str(tmp_path / "open.json"), EXPECT_TOKEN=token,
        GH_REPO="acme/widgets", GITHUB_OUTPUT=str(tmp_path / "output"),
        GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0=f"url.file://{remotes}/.insteadOf",
        GIT_CONFIG_VALUE_0="https://github.com/",
    )
    return {"tree": tree, "bare": bare, "env": env, "token": token, "tmp": tmp_path}


def _run(ctx: dict[str, Any], command: str, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(ctx["tree"] / "scripts" / SCRIPT.name), command],
                          cwd=ctx["tree"], env={**ctx["env"], **extra}, capture_output=True, text=True,
                          timeout=120, check=False)


def test_a_change_is_pushed_to_the_maintenance_branch_and_opened_never_to_main(tmp_path):
    """MUTATION: push HEAD:main, commit the stray file, or call gh with the
    workflow's token instead of the App's."""
    ctx = _setup(tmp_path)
    main_before = _git(ctx["bare"], "rev-parse", "refs/heads/main")
    refreshed = _run(ctx, "refresh")
    assert refreshed.returncode == 0, refreshed.stdout + refreshed.stderr
    assert (tmp_path / "output").read_text() == "changed=true\n"

    opened = _run(ctx, "open", APP_TOKEN=ctx["token"])
    assert opened.returncode == 0, opened.stdout + opened.stderr
    assert _git(ctx["bare"], "rev-parse", "refs/heads/main") == main_before, "main was pushed"
    pushed = _git(ctx["bare"], "rev-parse", f"refs/heads/{BRANCH}")
    assert _git(ctx["bare"], "rev-parse", f"{pushed}~1") == main_before, "one commit on main's tip"
    files = _git(ctx["bare"], "diff", "--name-only", main_before, pushed).splitlines()
    assert files == [UNIT], files
    calls = (tmp_path / "gh.log").read_text().splitlines()
    assert calls[0].startswith(f"pr list --repo acme/widgets --head {BRANCH} --base main"), calls
    assert calls[1].startswith(f"pr create --repo acme/widgets --base main --head {BRANCH} --title "), calls
    assert "[swarm] task_" not in calls[1]
    assert all(c.endswith("token=app") for c in calls), calls
    assert ctx["token"] not in opened.stdout + opened.stderr + refreshed.stdout + refreshed.stderr


def test_an_open_refresh_pull_request_is_updated_in_place_not_duplicated(tmp_path):
    ctx = _setup(tmp_path, open_prs='[{"number": 7, "url": "https://github.invalid/pr/7"}]')
    assert _run(ctx, "refresh").returncode == 0
    opened = _run(ctx, "open", APP_TOKEN=ctx["token"])
    assert opened.returncode == 0, opened.stdout + opened.stderr
    assert "pr/7" in opened.stderr, opened.stderr
    calls = (tmp_path / "gh.log").read_text().splitlines()
    assert not any(c.startswith("pr create") for c in calls), calls


def test_no_change_opens_nothing(tmp_path):
    ctx = _setup(tmp_path, unit_edit=False)
    refreshed = _run(ctx, "refresh")
    assert refreshed.returncode == 0, refreshed.stdout + refreshed.stderr
    assert (tmp_path / "output").read_text() == "changed=false\n"
    opened = _run(ctx, "open", APP_TOKEN=ctx["token"])
    assert opened.returncode == 0, opened.stdout + opened.stderr
    assert not (tmp_path / "gh.log").exists()
    assert "refs/heads/" + BRANCH not in _git(ctx["bare"], "for-each-ref")


def test_one_refresh_that_cannot_read_its_run_is_red_and_the_others_figures_still_count(tmp_path):
    """MUTATION: let a refresh's failure pass, or drop the other's change."""
    ctx = _setup(tmp_path, ui_fails=True)
    refreshed = _run(ctx, "refresh")
    assert refreshed.returncode == 1, refreshed.stdout + refreshed.stderr
    assert "refresh-ui-weights.sh could not refresh" in refreshed.stderr, refreshed.stderr
    assert (tmp_path / "output").read_text() == "changed=true\n"


def test_open_without_the_app_token_fails_before_pushing(tmp_path):
    ctx = _setup(tmp_path)
    assert _run(ctx, "refresh").returncode == 0
    opened = _run(ctx, "open", APP_TOKEN="")
    assert opened.returncode == 1, opened.stdout + opened.stderr
    assert "refs/heads/" + BRANCH not in _git(ctx["bare"], "for-each-ref")
