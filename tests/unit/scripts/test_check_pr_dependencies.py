"""Auto-merge refuses a pull request whose body says it depends on something
that has not landed.

OWNER DECISION, 2026-10-08 (observer proposal I, part 2). Parallel lanes open
pull requests written on top of each other's, and `ready` merged whichever
went green first. A body line `depends on PR <N>` or `depends on #<N>` (any
case) now holds it: `scripts/check-pr-dependencies.sh` asks REST
`repos/<R>/issues/<n>` of each number, and a pull request not merged or an
issue not closed refuses, through the gate's own `refuse`, with a comment.

These tests RUN the script against a fake `gh`:

* nothing named, or every dependency landed: exit 0;
* an open or closed-unmerged pull request, an open issue, or a number that
  names nothing: exit 2 and the refusal sentence on stdout;
* an unreadable answer or a failed read: non-zero and not 2 -- fail closed.

The workflow wiring, the gate's refusal and docs/ci.md's examples are held at
the bottom. Nothing here touches the network.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "check-pr-dependencies.sh"
AUTO_MERGE = REPO / ".github" / "workflows" / "auto-merge.yml"
CI_DOC = REPO / "docs" / "ci.md"
THIS_REPO = "bogdan-alexandrescu/SwarmCloud"
SELF = 4242
REFUSED = 2

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None, reason="bash and jq are required"
)


def _first_effective_line(text: str) -> str:
    for line in text.splitlines()[1:]:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def test_the_script_has_the_house_shape():
    assert SCRIPT.is_file(), f"{SCRIPT.relative_to(REPO)} does not exist"
    text = SCRIPT.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    assert _first_effective_line(text) == "set -euo pipefail"
    assert os.access(SCRIPT, os.X_OK)
    assert re.search(r'^source .*/lib/common\.sh"$', text, re.MULTILINE)


# ---------------------------------------------------------------------------
# Running it against a fake gh
# ---------------------------------------------------------------------------

# `api repos/<R>/pulls/<n>` answers from pull.json; `api repos/<R>/issues/<n>`
# from issues/<n>.json, a missing one being GitHub's 404, and FAKE_GH_REST_FAIL
# naming a number whose read fails like a 502. Every call is logged.
FAKE_GH = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_GH_LOG}"
path=""
for arg in "$@"; do
  case "${arg}" in repos/*) path="${arg}" ;; esac
done
if [[ "${1:-}" == "api" ]]; then
  case "${path}" in
    repos/*/pulls/*)
      cat "${FAKE_GH_PULL}"
      exit "${FAKE_GH_PULL_EXIT:-0}"
      ;;
    repos/*/issues/*)
      number="${path##*/}"
      if [[ " ${FAKE_GH_REST_FAIL:-} " == *" ${number} "* ]]; then
        echo "gh: Server Error (HTTP 502)" >&2
        exit 1
      fi
      if [[ -f "${FAKE_GH_ISSUES}/${number}.json" ]]; then
        cat "${FAKE_GH_ISSUES}/${number}.json"
        exit 0
      fi
      echo '{"message":"Not Found","status":"404"}'
      echo "gh: Not Found (HTTP 404)" >&2
      exit 1
      ;;
  esac
fi
echo "fake gh: unexpected call: $*" >&2
exit 3
"""


def _pull(body: str | None) -> dict:
    return {"number": SELF, "title": "A fact-style headline", "state": "open", "body": body}


def _issue(number: int, *, state: str = "open", pull: bool = False, merged: bool = False) -> dict:
    item: dict = {"number": number, "state": state, "title": f"item {number}"}
    if pull:
        item["pull_request"] = {
            "url": f"https://api.github.com/repos/{THIS_REPO}/pulls/{number}",
            "merged_at": "2026-10-08T04:16:19Z" if merged else None,
        }
    return item


def _merged(number: int) -> dict:
    return _issue(number, state="closed", pull=True, merged=True)


@pytest.fixture
def run_check(tmp_path: Path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "gh"
    fake.write_text(FAKE_GH)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    issues_dir = tmp_path / "issues"
    issues_dir.mkdir()

    def run(
        pull: dict | str,
        items: list[dict] | None = None,
        *,
        pr: str = str(SELF),
        pull_exit: int = 0,
        rest_fail: str = "",
    ):
        for old in issues_dir.iterdir():
            old.unlink()
        for item in items or []:
            (issues_dir / f"{item['number']}.json").write_text(json.dumps(item))
        pull_file = tmp_path / "pull.json"
        pull_file.write_text(pull if isinstance(pull, str) else json.dumps(pull))
        log = tmp_path / "gh.log"
        summary = tmp_path / "summary.md"
        for path in (log, summary):
            path.write_text("")
        env = {
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "GH_REPO": THIS_REPO,
            "GITHUB_STEP_SUMMARY": str(summary),
            "NO_COLOR": "1",
            "FAKE_GH_LOG": str(log),
            "FAKE_GH_PULL": str(pull_file),
            "FAKE_GH_PULL_EXIT": str(pull_exit),
            "FAKE_GH_ISSUES": str(issues_dir),
            "FAKE_GH_REST_FAIL": rest_fail,
        }
        proc = subprocess.run(
            ["bash", str(SCRIPT), "--pr", pr],
            env=env, capture_output=True, text=True, timeout=30, check=False,
        )
        calls = log.read_text()
        looked_up = [int(n) for n in re.findall(r"^api repos/[^ ]*/issues/(\d+)", calls, re.MULTILINE)]
        return proc, looked_up, calls

    return run


#: Every form the phrase takes (docs/ci.md, "Saying what a pull request
#: depends on"), each naming 840.
PHRASES = [
    "depends on PR 840",
    "depends on PR #840",
    "depends on #840",
    "Depends on PR 840",
    "DEPENDS ON #840",
    "This depends  on\tpr   840 landing first.",
    "Depends on PR #840.",
]


@pytest.mark.parametrize("body", PHRASES)
def test_an_open_pull_request_named_as_a_dependency_is_refused(run_check, body: str):
    """MUTATION: match case-sensitively, or drop the `#` or the `PR` form."""
    proc, looked_up, _calls = run_check(_pull(body), [_issue(840, pull=True)])
    assert proc.returncode == REFUSED, body + "\n" + proc.stdout + proc.stderr
    assert looked_up == [840]
    assert "#840 is an open pull request that has not merged" in proc.stdout, proc.stdout


@pytest.mark.parametrize("body", PHRASES)
def test_a_merged_dependency_passes(run_check, body: str):
    """The control for the refusal above: the same phrase, the dependency
    merged. MUTATION: refuse whenever the phrase appears."""
    proc, looked_up, _calls = run_check(_pull(body), [_merged(840)])
    assert proc.returncode == 0, body + "\n" + proc.stdout + proc.stderr
    assert looked_up == [840]
    assert proc.stdout.strip() == ""


@pytest.mark.parametrize(
    "body",
    [
        None,
        "",
        "part of #840",
        "see #840; after #840 merges",
        "fixes #840",
        "independs on #840",
        "depends on 840",
        "depends on PR840x",
        "it depends on what #840 does",
    ],
    ids=["null-body", "empty", "part-of", "see-after", "closing-keyword", "inside-a-word",
         "bare-number", "trailing-letters", "not-adjacent"],
)
def test_text_that_is_not_the_phrase_names_no_dependency(run_check, body):
    """MUTATION: read any `#N` in the body as a dependency."""
    proc, looked_up, _calls = run_check(_pull(body), [_issue(840, pull=True)])
    assert proc.returncode == 0, f"{body!r}\n{proc.stdout}{proc.stderr}"
    assert looked_up == []


def test_a_closed_issue_passes_and_an_open_one_refuses(run_check):
    proc, looked_up, _calls = run_check(_pull("depends on #124"), [_issue(124, state="closed")])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert looked_up == [124]
    proc, looked_up, _calls = run_check(_pull("depends on #124"), [_issue(124)])
    assert proc.returncode == REFUSED, proc.stdout + proc.stderr
    assert "#124 is an open issue" in proc.stdout


def test_a_pull_request_closed_without_merging_refuses(run_check):
    """Closed is not landed: what this depends on never reached main.
    MUTATION: pass every pull request that is not open."""
    proc, looked_up, _calls = run_check(_pull("depends on PR 777"), [_issue(777, state="closed", pull=True)])
    assert proc.returncode == REFUSED, proc.stdout + proc.stderr
    assert looked_up == [777]
    assert "closed without merging" in proc.stdout


def test_a_number_that_names_nothing_refuses(run_check):
    """A dependency nobody can read has not landed. MUTATION: treat a 404 as
    nothing to wait for."""
    proc, looked_up, _calls = run_check(_pull("depends on #99999"))
    assert proc.returncode == REFUSED, proc.stdout + proc.stderr
    assert looked_up == [99999]
    assert "#99999 does not exist" in proc.stdout


def test_its_own_number_is_ignored(run_check):
    proc, looked_up, _calls = run_check(_pull(f"depends on #{SELF}"), [_issue(SELF, pull=True)])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert looked_up == []


def test_every_unlanded_dependency_is_named_and_landed_ones_are_not(run_check):
    proc, looked_up, _calls = run_check(
        _pull("depends on PR 840\ndepends on #841\ndepends on #124\nDepends on PR #840"),
        [_issue(840, pull=True), _merged(841), _issue(124)],
    )
    assert proc.returncode == REFUSED, proc.stdout + proc.stderr
    assert sorted(looked_up) == [124, 840, 841], "each number is read once"
    assert "#840" in proc.stdout and "#124" in proc.stdout
    assert "#841" not in proc.stdout


# Fail closed: an error must never read as "nothing to wait for".


@pytest.mark.parametrize(
    "pull",
    [
        {"number": 1, "body": "depends on #840"},
        {"number": SELF, "body": ["depends on #840"]},
        {"message": "Not Found"},
        "this is not json",
        "",
    ],
    ids=["another-pull-request", "body-not-a-string", "no-pull-request", "not-json", "empty"],
)
def test_an_unreadable_answer_fails_closed(run_check, pull):
    """MUTATION: treat an unreadable body as an empty one."""
    proc, _looked_up, _calls = run_check(pull, [_issue(840, pull=True)])
    assert proc.returncode not in (0, REFUSED), proc.stdout + proc.stderr


def test_a_failed_pull_request_read_fails_closed(run_check):
    proc, _looked_up, _calls = run_check(_pull("depends on #840"), [_merged(840)], pull_exit=1)
    assert proc.returncode not in (0, REFUSED), proc.stdout + proc.stderr


def test_a_failed_dependency_read_fails_closed(run_check):
    """A 502 is not a 404. MUTATION: treat every failed read as "not found"
    -- which would refuse, but with a reason that is not true."""
    proc, _looked_up, _calls = run_check(_pull("depends on #840"), [_merged(840)], rest_fail="840")
    assert proc.returncode not in (0, REFUSED), proc.stdout + proc.stderr


@pytest.mark.parametrize("pr", ["", "0", "12a", "$(id)", "-1"])
def test_a_pull_request_number_that_is_not_a_number_is_refused(run_check, pr: str):
    proc, looked_up, calls = run_check(_pull("depends on #840"), pr=pr)
    assert proc.returncode not in (0, REFUSED)
    assert calls == "" and looked_up == []


# ---------------------------------------------------------------------------
# The workflow calls it before auto-merge is enabled
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def enable() -> dict:
    return yaml.safe_load(AUTO_MERGE.read_text())["jobs"]["enable"]


def _ids(job: dict) -> list:
    return [step.get("id") for step in job.get("steps") or []]


def test_the_enable_job_runs_the_check_from_the_default_branch_before_the_gate(enable: dict):
    """Main's script, never the pull request's: pull_request_target holds a
    write token. MUTATION: move the check after the merge, or drop its `if:`."""
    ids = _ids(enable)
    assert "dependencies" in ids, ids
    assert ids.index("main-scripts") < ids.index("dependencies") < ids.index("gate") < ids.index("merge"), ids
    step = enable["steps"][ids.index("dependencies")]
    assert "scripts/check-pr-dependencies.sh --pr \"${PR_NUMBER}\"" in step["run"]
    assert step.get("if") == "steps.pr.outputs.evaluate == 'true'", step.get("if")
    assert "github.event" not in step["run"]


def test_a_failure_that_is_not_a_refusal_fails_the_step(enable: dict):
    """Fail closed: only exit 0 passes and only exit 2 refuses.
    MUTATION: `|| true` after the script."""
    run = enable["steps"][_ids(enable).index("dependencies")]["run"]
    assert '[[ "${rc}" -ne 2 || ! -s "${out}" ]]' in run
    assert "exit 1" in run
    assert "|| true" not in run


def test_the_gate_reads_the_checks_finding(enable: dict):
    gate = enable["steps"][_ids(enable).index("gate")]
    assert (gate.get("env") or {}).get("DEPENDENCY_REFUSAL") == "${{ steps.dependencies.outputs.refusal }}"
    assert 'refuse "${DEPENDENCY_REFUSAL}"' in gate["run"]


@pytest.fixture
def run_gate_with_dependency(enable: dict, tmp_path: Path):
    """The gate's own shell with a dependency refusal handed in, against a
    fake gh that records the label removal and the comment."""
    script = enable["steps"][_ids(enable).index("gate")]["run"]
    bindir = tmp_path / "gate-bin"
    bindir.mkdir()
    fake = bindir / "gh"
    fake.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nprintf '%s\\n' \"$*\" >> \"${FAKE_GH_LOG}\"\n"
        "if [[ \"${1:-} ${2:-}\" == \"pr comment\" ]]; then cat \"$5\" >> \"${FAKE_GH_COMMENTS}\"; exit 0; fi\n"
        f"if [[ \"$*\" == \"pr edit {SELF} --remove-label ready\" ]]; then exit 0; fi\n"
        "echo \"fake gh: unexpected call: $*\" >&2\nexit 3\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    def run(event_name: str, refusal: str):
        log = tmp_path / "gate-gh.log"
        comments = tmp_path / "comments.md"
        output = tmp_path / "output.txt"
        summary = tmp_path / "gate-summary.md"
        for path in (log, comments, output, summary):
            path.write_text("")
        env = {
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "GH_REPO": THIS_REPO,
            "EVENT_NAME": event_name,
            "PR_NUMBER": str(SELF),
            "PR_TITLE": "A fact-style headline",
            "BASE_REF": "main",
            "HEAD_SHA": "0" * 40,
            "MERGE_APP_ID": "123456",
            "HAS_MERGE_APP_KEY": "true",
            "CLOSING_REFUSAL": "",
            "DEPENDENCY_REFUSAL": refusal,
            "GITHUB_STEP_SUMMARY": str(summary),
            "GITHUB_OUTPUT": str(output),
            "FAKE_GH_LOG": str(log),
            "FAKE_GH_COMMENTS": str(comments),
        }
        proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30, check=False)
        return proc, log.read_text(), comments.read_text(), output.read_text()

    return run


def _script_refusal(run_check) -> str:
    proc, _looked_up, _calls = run_check(_pull("depends on PR 840"), [_issue(840, pull=True)])
    assert proc.returncode == REFUSED, proc.stdout + proc.stderr
    return proc.stdout.strip()


def test_on_the_label_a_refusal_comments_fails_and_arms_nothing(run_check, run_gate_with_dependency):
    """Like the gate's other label-time refusals: `ready` kept, run failed.
    MUTATION: drop the 0b block, and the gate reads the rules and says merge."""
    refusal = _script_refusal(run_check)
    proc, calls, comments, output = run_gate_with_dependency("pull_request_target", refusal)
    assert proc.returncode != 0
    assert comments.startswith("**Not queued for auto-merge.** " + refusal), comments
    assert "decision=refused" in output and "decision=merge" not in output, output
    assert "api " not in calls and "--remove-label" not in calls, calls


def test_on_a_re_evaluation_a_refusal_drops_ready_and_disarms(run_check, run_gate_with_dependency):
    refusal = _script_refusal(run_check)
    proc, calls, comments, output = run_gate_with_dependency("workflow_dispatch", refusal)
    assert proc.returncode == 0, proc.stderr
    assert f"pr edit {SELF} --remove-label ready" in calls, calls
    assert refusal in comments, comments
    assert "disarm=true" in output and "decision=merge" not in output, output


# ---------------------------------------------------------------------------
# The docs give the phrase the script reads
# ---------------------------------------------------------------------------


def _doc_section() -> str:
    text = CI_DOC.read_text()
    start = text.index("### Saying what a pull request depends on")
    end = text.index("\n### ", start + 1)
    return text[start:end]


def test_docs_give_each_form_of_the_phrase_and_the_script_reads_each(run_check):
    """The examples an author copies from docs/ci.md are the ones the script
    matches. MUTATION: document `requires #N`, or drop a form from the script."""
    section = _doc_section()
    block = section.split("```text\n", 1)[1].split("```", 1)[0]
    examples = [line.strip() for line in block.splitlines() if line.strip()]
    assert len(examples) == 3, examples
    for example in examples:
        proc, looked_up, _calls = run_check(_pull(example), [_issue(840, pull=True)])
        assert proc.returncode == REFUSED and looked_up == [840], example
    assert "scripts/check-pr-dependencies.sh" in section
    assert "Any case" in section


def test_the_auto_merge_refusal_list_links_the_section():
    text = CI_DOC.read_text()
    assert "(#saying-what-a-pull-request-depends-on)" in text
