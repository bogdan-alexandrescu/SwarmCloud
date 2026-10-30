"""No job can run a fork pull request on a self-hosted runner, and every job
that is not on the self-hosted pool is there for a stated, checked reason.

OWNER DECISION, 2026-10-12. This repository is public, its fork-PR approval
policy is `first_time_contributors` (left as it is), and its jobs may target
the ephemeral GCP VMs that `runner-dispatcher` starts for the `bogdan-ci`
label (repo webhook `/hook/swarmcloud`, sagaxyz/ai-studio#336). A RETURNING
outside contributor's fork PR needs no approval, so a self-hosted job it
reaches would run their code on our VMs. The decision: fork PRs never reach
the VMs. Pushes and same-repository PRs may.

OWNER DECISION, 2026-10-14: "make sure we use CI runners for all jobs moving
forward if capacity allows". So EVERY job uses the one approved expression
unless it is in HOSTED_JOBS below, and each HOSTED_JOBS entry names a reason
this file checks against the job: a cloud identity through workload identity,
the pool's own judge, or -- since CI-FOLD (#1799, owner approval 2026-10-21)
-- a job that runs nothing on a VM's worth of setup: a name-keeper that only
reads `needs`, or a fixed summary (test_ci_fold.py). Repository secrets on a guarded job are refused
except the ones GUARDED_SECRETS names for that job (owner decision
2026-10-14: secrets on ephemeral self-hosted VMs accepted). A new job that silently uses a hosted image fails here,
and so does a job that keeps a hosted entry after its credential is gone.

OWNER DECISION, 2026-10-20: main's own runs go first. A push or a dispatch on
`main` (main's CI run, every release and hotfix) resolves to GitHub-hosted
ubuntu-24.04, so it never queues behind lane pull requests on the pool (two
releases failed that morning waiting 45 minutes for a queued image build).
Pull requests, lane-branch pushes and merge-queue entries keep the pool. The
clause is scoped by event, not by ref alone: `pull_request_target` and
`schedule` also run with `github.ref` = main, and they stay where they were.

The approved expression, with its capacity escape hatch and main's clause: the repo variable
SELF_HOSTED_CI set to `off` sends every guarded job back to GitHub-hosted
ubuntu-24.04 (unset, or anything else, means the pool):

    runs-on: ${{ (vars.SELF_HOSTED_CI == 'off' || ((github.event_name == 'push' || github.event_name == 'workflow_dispatch') && github.ref == 'refs/heads/main') || ((github.event_name == 'pull_request' || github.event_name == 'pull_request_target') && github.event.pull_request.head.repo.full_name != github.repository)) && 'ubuntu-24.04' || fromJSON(format('["self-hosted","linux","x64","{0}"]', vars.SWARM_CI_RUNNER_LABEL || 'swarm-ci')) }}

THE SHORT FORM. A workflow no fork's pull request can start does not need
the fork clause, and two of the repository's own guards forbid the text it is
made of in those files (`pull_request.head.repo` in auto-merge.yml, which
must never touch a pull request's code; the repository's name in accept.yml,
which must not name the public repository):

    runs-on: ${{ vars.SELF_HOSTED_CI == 'off' && 'ubuntu-24.04' || fromJSON(format('["self-hosted","linux","x64","{0}"]', vars.SWARM_CI_RUNNER_LABEL || 'swarm-ci')) }}

It is accepted ONLY for a file in SHORT_FORM_WORKFLOWS whose `on:` has no
`pull_request` trigger (a fork's `pull_request` runs the fork's own copy of the
workflow file and its code). SHORT_FORM_WORKFLOWS says why each file qualifies;
anywhere a fork `pull_request` can start the job, the long form is required.

The guard FAILS CLOSED: any label that is not a GitHub-hosted image name
(`ubuntu-*`, `windows-*`, `macos-*`) is treated as self-hosted, and any
`runs-on` expression it cannot resolve is refused, so a new spelling has to
be added here deliberately rather than slipping past. The expression is also
EVALUATED (resolve() below) for a push, a same-repository PR and a fork PR,
with the hatch on and off, so the property is asserted, not only the text.

Matrix-driven `runs-on` (`${{ matrix.os }}`) is resolved against the job's
`strategy.matrix` values and `include` entries, each value judged on its own.

NOT COVERED by the expression, and so not by this guard: `workflow_run`
(the triggering PR's fork is `github.event.workflow_run.head_repository`, and
`github.event.pull_request` is absent there, so the expression resolves to
self-hosted). A `workflow_run` job runs the default branch's workflow file,
which is trusted; it runs fork code only if it checks out the triggering
head. Do not move such a job to self-hosted without that check.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path

import pytest
import yaml

from .ci_fold_layout import PATCH_NOTE

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO / ".github" / "workflows"

HOSTED = re.compile(r"^(ubuntu|windows|macos)-[\w.-]+$")

#: The one approved spelling. Group 1 is the hosted fallback, group 2 the
#: JSON label list the job gets on a push or a same-repository PR.
FORK_GATED = re.compile(
    r"^\$\{\{\s*"
    r"\(vars\.SELF_HOSTED_CI == 'off'"
    r" \|\| \(\(github\.event_name == 'push' \|\| github\.event_name == 'workflow_dispatch'\)"
    r" && github\.ref == 'refs/heads/main'\)"
    r" \|\| \(\(github\.event_name == 'pull_request' \|\| github\.event_name == 'pull_request_target'\)"
    r" && github\.event\.pull_request\.head\.repo\.full_name != github\.repository\)\)"
    r" && '([^']+)' \|\| fromJSON\('(\[[^']*\])'\)"
    r"\s*\}\}$"
)
#: The short spelling: the escape hatch only, no fork clause. Same groups.
SHORT_FORM = re.compile(
    r"^\$\{\{\s*"
    r"vars\.SELF_HOSTED_CI == 'off'"
    r" && '([^']+)' \|\| fromJSON\('(\[[^']*\])'\)"
    r"\s*\}\}$"
)
#: Workflow files allowed the short form, and why each qualifies. Each is also
#: checked against its own `on:` (no `pull_request`) below.
SHORT_FORM_WORKFLOWS: dict[str, str] = {
    "accept.yml": (
        "starts only from a completed release on main (workflow_run) or by hand "
        "(workflow_dispatch): never from any pull request"
    ),
    "merge-train.yml": (
        "starts only on schedule, workflow_run of a train/ branch, or dispatch: "
        "never from a pull request"
    ),
    "auto-merge.yml": (
        "runs on pull_request_target (the base branch's workflow file, with no "
        "checkout of the pull request: test_auto_merge_workflow.py), workflow_run, "
        "check_suite and dispatch: never on `pull_request`, so a fork's code is "
        "never what runs"
    ),
}
MATRIX_REF = re.compile(r"^\$\{\{\s*matrix\.([\w-]+)\s*\}\}$")


def _labels(value: object) -> list[str]:
    """The labels a literal (non-expression) runs-on value names."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, dict):  # {group: ..., labels: ...}
        out = []
        if "group" in value:
            out.append(f"group:{value['group']}")  # a runner group is never hosted
        return out + _labels(value.get("labels", []))
    raise TypeError(f"unrecognised runs-on: {value!r}")


def _self_hosted(labels: list[str]) -> bool:
    return not labels or any(not HOSTED.match(label) for label in labels)


def _matrix_values(job: dict, key: str) -> list[object]:
    matrix = ((job.get("strategy") or {}).get("matrix")) or {}
    if isinstance(matrix, str):
        return []  # fromJSON(...) matrix: unresolvable
    values = list(matrix.get(key, []) or [])
    values += [inc[key] for inc in matrix.get("include", []) or [] if key in inc]
    return values


def _trigger_names(doc: dict) -> set[str]:
    """The event names a workflow's `on:` lists (YAML reads the key `on` as True)."""
    on = doc.get("on", doc.get(True))
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {str(e) for e in on}
    return {str(e) for e in (on or {})}


#: The C loader where PyYAML was built with libyaml; the same YAML 1.1 rules.
_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


@functools.lru_cache(maxsize=None)
def _doc(path: Path) -> dict:
    """Each workflow file, parsed once per run and shared read-only.

    Every test here and each parametrized case re-parsed every workflow with the
    pure-Python loader: 13.0 CI-seconds on main (run 40171974036), when this file
    is one of the guard walkers held under 5 (test_select_unit_tests.py
    `test_the_walkers_of_github_and_images_stay_cheap`). Nothing here mutates a
    parsed document, so one parse serves them all.
    """
    return yaml.load(path.read_text(), Loader=_LOADER) or {}


def _file_triggers(filename: str) -> set[str]:
    return _trigger_names(_doc(WORKFLOWS / filename))


def _short_form_allowed(filename: str | None, triggers: set[str] | None) -> str | None:
    """None when the short form is allowed for this workflow, else why not."""
    if filename is None or triggers is None:
        return "the short form is judged per workflow file, and none was given"
    if filename not in SHORT_FORM_WORKFLOWS:
        return f"{filename} is not in SHORT_FORM_WORKFLOWS"
    if "pull_request" in triggers:
        return f"{filename} has a `pull_request` trigger, so a fork can start the job"
    return None


def _is_guarded(runs_on: object) -> bool:
    """On the pool through either approved spelling."""
    return isinstance(runs_on, str) and bool(
        FORK_GATED.match(runs_on.strip()) or SHORT_FORM.match(runs_on.strip())
    )


def verdict(job: dict, filename: str | None = None, triggers: set[str] | None = None) -> tuple[str, str]:
    """('hosted' | 'gated' | 'EXPOSED', why) for one job's runs-on. `filename`
    and `triggers` (the workflow's event names) are needed only to judge the
    short form."""
    if "uses" in job and "runs-on" not in job:
        return "hosted", "calls a reusable workflow (its own jobs are scanned)"
    runs_on = job.get("runs-on")
    if runs_on is None:
        return "EXPOSED", "no runs-on"
    if isinstance(runs_on, str) and "${{" in runs_on:
        gated = FORK_GATED.match(runs_on.strip())
        if gated:
            fallback, labels = gated.group(1), json.loads(gated.group(2))
            if _self_hosted([fallback]):
                return "EXPOSED", f"fork fallback {fallback!r} is not a hosted image"
            return "gated", f"self-hosted {labels} behind the fork check"
        short = SHORT_FORM.match(runs_on.strip())
        if short:
            fallback, labels = short.group(1), json.loads(short.group(2))
            if _self_hosted([fallback]):
                return "EXPOSED", f"fallback {fallback!r} is not a hosted image"
            why = _short_form_allowed(filename, triggers)
            if why:
                return "EXPOSED", f"short form without a fork check: {why}"
            return "gated", f"self-hosted {labels}; no fork can start this workflow"
        ref = MATRIX_REF.match(runs_on.strip())
        if ref:
            values = _matrix_values(job, ref.group(1))
            if not values:
                return "EXPOSED", f"matrix.{ref.group(1)} has no static values to resolve"
            for v in values:
                if isinstance(v, str) and "${{" in v:
                    sub = verdict({"runs-on": v})
                    if sub[0] == "EXPOSED":
                        return sub
                elif _self_hosted(_labels(v)):
                    return "EXPOSED", f"matrix value {v!r} is self-hosted with no fork check"
            return "hosted", f"matrix.{ref.group(1)} resolves to hosted images only"
        return "EXPOSED", f"unrecognised runs-on expression: {runs_on!r}"
    labels = _labels(runs_on)
    if _self_hosted(labels):
        return "EXPOSED", f"bare self-hosted labels {labels} reach fork PRs"
    return "hosted", f"{labels}"


def _jobs():
    files = sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])
    assert files, f"no workflows found under {WORKFLOWS}"  # the scan saw something
    for path in files:
        doc = _doc(path)
        for name, job in (doc.get("jobs") or {}).items():
            yield f"{path.name}:{name}", job


#: Jobs that ask for the pool by its bare labels, on purpose. The only one is
#: runner-fallback.yml's probe: while SELF_HOSTED_CI is off nothing else asks
#: for a bogdan-ci runner, so the probe is how the judge sees the pool serve
#: again (docs/ci.md, "CI falls back to GitHub-hosted runners on its own").
#: Accepted only while its workflow starts on schedule or dispatch alone (no
#: fork can start it), and the job holds no permission, no secret and no
#: checkout.
PROBE_JOBS: dict[str, str] = {
    "runner-fallback.yml:probe": "the pool probe CI-FALLBACK reads while the hatch is off",
}
PROBE_TRIGGERS = frozenset({"schedule", "workflow_dispatch"})


def _probe_refused(name: str, job: dict) -> str | None:
    """None when a PROBE_JOBS entry still qualifies, else why not."""
    filename = name.split(":", 1)[0]
    if not _file_triggers(filename) <= PROBE_TRIGGERS:
        return f"{filename} can start on {sorted(_file_triggers(filename) - PROBE_TRIGGERS)}"
    if job.get("permissions") != {}:
        return f"permissions {job.get('permissions')!r} is not {{}}"
    if _SECRET.findall(yaml.safe_dump(job)):
        return "holds a secret"
    if any("checkout" in str(s.get("uses", "")) for s in job.get("steps") or []):
        return "checks out code"
    return None


def _verdict_of(name: str, job: dict) -> tuple[str, str]:
    filename = name.split(":", 1)[0]
    if name in PROBE_JOBS:
        why = _probe_refused(name, job)
        if why:
            return "EXPOSED", f"pool probe no longer qualifies: {why}"
        return "probe", PROBE_JOBS[name]
    return verdict(job, filename, _file_triggers(filename))


def test_no_job_runs_a_fork_pr_on_a_self_hosted_runner() -> None:
    jobs = list(_jobs())
    assert jobs, "no jobs parsed: the guard would assert over nothing"
    verdicts = {name: _verdict_of(name, job) for name, job in jobs}
    gated = sorted(n for n, (v, _) in verdicts.items() if v == "gated")
    print(f"scanned {len(jobs)} job(s); {len(gated)} self-hosted behind the fork check: {gated}")
    exposed = {n: why for n, (v, why) in verdicts.items() if v == "EXPOSED"}
    assert not exposed, "jobs that could run a fork PR on a self-hosted runner:\n" + "\n".join(
        f"  {n}: {why}" for n, why in sorted(exposed.items())
    )


APPROVED = (
    "${{ (vars.SELF_HOSTED_CI == 'off'"
    " || ((github.event_name == 'push' || github.event_name == 'workflow_dispatch')"
    " && github.ref == 'refs/heads/main')"
    " || ((github.event_name == 'pull_request'"
    " || github.event_name == 'pull_request_target')"
    " && github.event.pull_request.head.repo.full_name != github.repository))"
    " && 'ubuntu-24.04' || fromJSON(format('[\"self-hosted\",\"linux\",\"x64\",\"{0}\"]', vars.SWARM_CI_RUNNER_LABEL || 'swarm-ci')) }}"
)
SHORT = (
    "${{ vars.SELF_HOSTED_CI == 'off' && 'ubuntu-24.04'"
    " || fromJSON(format('[\"self-hosted\",\"linux\",\"x64\",\"{0}\"]', vars.SWARM_CI_RUNNER_LABEL || 'swarm-ci')) }}"
)
#: The spelling PR 1174 approved, before the escape hatch. Fork-safe, but no
#: longer the one spelling: a job still using it ignores SELF_HOSTED_CI=off.
PRE_HATCH = (
    "${{ (github.event_name == 'pull_request' || github.event_name == 'pull_request_target')"
    " && github.event.pull_request.head.repo.full_name != github.repository"
    " && 'ubuntu-latest' || fromJSON(format('[\"self-hosted\",\"linux\",\"x64\",\"{0}\"]', vars.SWARM_CI_RUNNER_LABEL || 'swarm-ci')) }}"
)


@pytest.mark.parametrize(
    ("runs_on", "expected"),
    [
        # Positive controls: what must pass.
        ("ubuntu-24.04", "hosted"),
        (["ubuntu-latest"], "hosted"),
        (APPROVED, "gated"),
        # What must fail.
        (["self-hosted", "bogdan-ci"], "EXPOSED"),
        ("bogdan-ci", "EXPOSED"),
        ("self-hosted", "EXPOSED"),
        ({"group": "vms", "labels": ["bogdan-ci"]}, "EXPOSED"),
        ("${{ fromJSON('[\"self-hosted\",\"bogdan-ci\"]') }}", "EXPOSED"),
        # The fork check with its fallback swapped to a self-hosted label.
        (APPROVED.replace("'ubuntu-24.04'", "'bogdan-ci'"), "EXPOSED"),
        # The check inverted (== instead of !=) is not the approved spelling.
        (APPROVED.replace("!=", "=="), "EXPOSED"),
        # Only one of the two PR events checked.
        (APPROVED.replace(" || github.event_name == 'pull_request_target'", ""), "EXPOSED"),
        # The escape hatch dropped, or the pre-hatch spelling: not the one spelling.
        (APPROVED.replace("vars.SELF_HOSTED_CI == 'off' || ", ""), "EXPOSED"),
        (PRE_HATCH, "EXPOSED"),
    ],
)
def test_verdict(runs_on: object, expected: str) -> None:
    assert verdict({"runs-on": runs_on})[0] == expected


NO_PR = {"workflow_run", "workflow_dispatch"}


@pytest.mark.parametrize(
    ("runs_on", "filename", "triggers", "expected"),
    [
        # Allowed: a listed file whose triggers hold no `pull_request`.
        (SHORT, "accept.yml", NO_PR, "gated"),
        (SHORT, "auto-merge.yml", {"pull_request_target", "workflow_run", "check_suite"}, "gated"),
        # Mutation: the same job in a workflow a fork's pull_request can start.
        (SHORT, "accept.yml", NO_PR | {"pull_request"}, "EXPOSED"),
        # Mutation: a file nobody listed, even with safe triggers.
        (SHORT, "application.yml", NO_PR, "EXPOSED"),
        # Mutation: no workflow context given at all.
        (SHORT, None, None, "EXPOSED"),
        # Mutation: the hatch dropped, or a self-hosted fallback.
        (SHORT.replace("vars.SELF_HOSTED_CI == 'off' && ", ""), "accept.yml", NO_PR, "EXPOSED"),
        (SHORT.replace("'ubuntu-24.04'", "'bogdan-ci'"), "accept.yml", NO_PR, "EXPOSED"),
        # The long form never needs the file's triggers.
        (APPROVED, "application.yml", NO_PR | {"pull_request"}, "gated"),
    ],
)
def test_the_short_form_is_judged_by_the_workflows_triggers(
    runs_on: str, filename: str | None, triggers: set[str] | None, expected: str
) -> None:
    """MUTATION: dropping the `pull_request` check, or the file allowlist,
    turns the EXPOSED rows above into 'gated' and this test red."""
    assert verdict({"runs-on": runs_on}, filename, triggers)[0] == expected


def test_the_short_form_files_really_have_no_pull_request_trigger() -> None:
    """MUTATION: adding `pull_request:` to accept.yml or auto-merge.yml fails
    this even if every runs-on in it were unchanged. The control: the same
    parser sees `pull_request` in application.yml, so an empty set here is not
    a parser that reads nothing."""
    assert "pull_request" in _file_triggers("application.yml")
    for filename, why in SHORT_FORM_WORKFLOWS.items():
        triggers = _file_triggers(filename)
        assert triggers, f"{filename}: parsed no triggers"
        assert "pull_request" not in triggers, f"{filename} qualifies because it {why}"


def test_every_short_form_file_uses_it_somewhere() -> None:
    """A SHORT_FORM_WORKFLOWS entry no job uses is a stale licence."""
    used = {
        name.split(":", 1)[0]
        for name, job in _jobs()
        if isinstance(job.get("runs-on"), str) and SHORT_FORM.match(job["runs-on"].strip())
    }
    assert used == set(SHORT_FORM_WORKFLOWS), (used, set(SHORT_FORM_WORKFLOWS))


def test_matrix_runs_on_is_resolved() -> None:
    job = {"runs-on": "${{ matrix.os }}", "strategy": {"matrix": {"os": ["ubuntu-24.04"]}}}
    assert verdict(job)[0] == "hosted"
    job["strategy"]["matrix"]["include"] = [{"os": ["self-hosted", "bogdan-ci"]}]
    assert verdict(job)[0] == "EXPOSED"
    job["strategy"]["matrix"]["include"] = [{"os": APPROVED}]
    assert verdict(job)[0] == "hosted"
    assert verdict({"runs-on": "${{ matrix.os }}"})[0] == "EXPOSED"  # unresolvable


# --- The expression, evaluated ----------------------------------------------

POOL = ["self-hosted", "linux", "x64", "bogdan-ci"]
REPOSITORY = "bogdan-alexandrescu/SwarmCloud"
#: The only context paths the approved expression reads. Anything else in an
#: expression handed to resolve() is refused rather than guessed at.
_CONTEXT_PATH = re.compile(r"\b(?:vars|github)(?:\.[A-Za-z_][\w-]*)+")


#: The ref GitHub gives each event when a test names none: a lane branch for a
#: push or dispatch, the merge ref for a pull request, the base branch for
#: pull_request_target and schedule, the queue ref for a merge group.
_DEFAULT_REF = {
    "pull_request": "refs/pull/1/merge",
    "pull_request_target": "refs/heads/main",
    "schedule": "refs/heads/main",
    "merge_group": "refs/heads/gh-readonly-queue/main/pr-1-0000000",
}
MAIN = "refs/heads/main"


def resolve(runs_on: str, *, event: str, fork: bool, hatch: str = "", ref: str | None = None) -> object:
    """What GitHub would put in `runs-on` for this event: evaluates the
    expression's &&/|| (which, as in Python's and/or, return an operand), with
    `github.event.pull_request` null on a push as it is on GitHub. `hatch` is
    vars.SELF_HOSTED_CI; GitHub gives an unset variable as ''."""
    body = runs_on.strip()
    assert body.startswith("${{") and body.endswith("}}"), body
    body = body[3:-2]
    is_pr = event in ("pull_request", "pull_request_target")
    values = {
        "vars.SELF_HOSTED_CI": hatch,
        "github.event_name": event,
        "github.ref": ref if ref is not None else _DEFAULT_REF.get(event, "refs/heads/swarm/task_lane"),
        "github.repository": REPOSITORY,
        "github.event.pull_request.head.repo.full_name": (
            ("someone/SwarmCloud" if fork else REPOSITORY) if is_pr else None
        ),
    }
    names: dict[str, object] = {}

    def bind(match: re.Match) -> str:
        path = match.group(0)
        assert path in values, f"resolve() does not model {path!r}"
        name = f"_v{len(names)}"
        names[name] = values[path]
        return name

    # Contexts first, so a quoted '&&' or 'fromJSON' can never be rewritten
    # inside a path; the approved expression has neither inside a string.
    py = _CONTEXT_PATH.sub(bind, body)
    py = py.replace("&&", " and ").replace("||", " or ").replace("fromJSON(", "_fromjson(")
    return eval(py, {"__builtins__": {}}, {**names, "_fromjson": json.loads})  # noqa: S307


@pytest.mark.parametrize(
    ("event", "fork", "hatch", "expected"),
    [
        ("push", False, "", POOL),
        ("push", False, "on", POOL),
        ("merge_group", False, "", POOL),
        ("workflow_dispatch", False, "", POOL),
        ("pull_request", False, "", POOL),
        ("pull_request_target", False, "", POOL),
        # Fork PRs never reach the pool, whatever the hatch says.
        ("pull_request", True, "", "ubuntu-24.04"),
        ("pull_request", True, "on", "ubuntu-24.04"),
        ("pull_request_target", True, "", "ubuntu-24.04"),
        # The capacity escape hatch sends everything to GitHub-hosted.
        ("push", False, "off", "ubuntu-24.04"),
        ("pull_request", False, "off", "ubuntu-24.04"),
        ("pull_request", True, "off", "ubuntu-24.04"),
    ],
)
def test_the_approved_expression_resolves(event: str, fork: bool, hatch: str, expected: object) -> None:
    assert resolve(APPROVED, event=event, fork=fork, hatch=hatch) == expected


@pytest.mark.parametrize(
    ("event", "fork", "hatch", "ref", "expected"),
    [
        # Main's own runs go first, on GitHub-hosted (owner decision 2026-10-20).
        ("push", False, "", MAIN, "ubuntu-24.04"),
        ("workflow_dispatch", False, "", MAIN, "ubuntu-24.04"),
        # A lane branch and a dispatch elsewhere keep the pool.
        ("push", False, "", "refs/heads/swarm/task_abc", POOL),
        ("workflow_dispatch", False, "", "refs/heads/fix/x", POOL),
        # Events that also carry ref = main are not main's runs: they keep the pool.
        ("pull_request_target", False, "", MAIN, POOL),
        ("schedule", False, "", MAIN, POOL),
        # A fork PR is still hosted whatever its ref says.
        ("pull_request_target", True, "", MAIN, "ubuntu-24.04"),
    ],
)
def test_mains_own_runs_go_hosted(event: str, fork: bool, hatch: str, ref: str, expected: object) -> None:
    # MUTATION: drop the main clause from APPROVED and the workflows -- a push to main resolves to the pool.
    # MUTATION: scope it by `github.ref == 'refs/heads/main'` alone -- pull_request_target and schedule go hosted.
    assert resolve(APPROVED, event=event, fork=fork, hatch=hatch, ref=ref) == expected


def test_resolve_can_come_out_the_other_way() -> None:
    """The control: the inverted fork check sends a fork PR to the pool, and
    the pre-hatch spelling ignores the hatch. If resolve() could not tell
    these apart, its passes above would mean nothing."""
    inverted = APPROVED.replace("!=", "==")
    assert resolve(inverted, event="pull_request", fork=True) == POOL
    assert resolve(PRE_HATCH, event="push", fork=False, hatch="off") == POOL
    # The main clause is what moves main: point it at another ref and a push to main keeps the pool.
    elsewhere = APPROVED.replace("github.ref == 'refs/heads/main'", "github.ref == 'refs/heads/nowhere'")
    assert resolve(elsewhere, event="push", fork=False, ref=MAIN) == POOL


def test_every_guarded_job_in_the_workflows_resolves_safely() -> None:
    """The spellings actually in the files, not only the constant above."""
    seen = 0
    for name, job in _jobs():
        runs_on = job.get("runs-on")
        if not _is_guarded(runs_on):
            continue
        seen += 1
        # The short form has no fork clause by design (its workflows are never
        # started by a fork's pull_request: judged by verdict()); every
        # guarded job still honours the hatch and otherwise gets the pool.
        if FORK_GATED.match(runs_on.strip()):
            for event in ("pull_request", "pull_request_target"):
                assert resolve(runs_on, event=event, fork=True) == "ubuntu-24.04", name
            # Main's own runs go hosted; a same-repository PR keeps the pool.
            assert resolve(runs_on, event="push", fork=False, ref=MAIN) == "ubuntu-24.04", name
            assert resolve(runs_on, event="pull_request", fork=False) == POOL, name
        assert resolve(runs_on, event="push", fork=False, hatch="off") == "ubuntu-24.04", name
        assert resolve(runs_on, event="push", fork=False) == POOL, name
    assert seen, "no guarded job found: the loop asserted over nothing"


# --- Every job is on the pool, or hosted for a checked reason ---------------

CLOUD = "cloud"
JUDGE = "judge"
KEEPER = "keeper"
SUMMARY = "summary"

REASONS = {
    CLOUD: (
        "holds a Google identity through workload identity (id-token: write and "
        "google-github-actions/auth): deploy and cloud credentials stay on "
        "GitHub-hosted, the rule PR 1184 recorded for release.yml"
    ),
    JUDGE: (
        "judges the self-hosted pool and sets SELF_HOSTED_CI (runner-fallback.yml): "
        "on the pool it would queue behind the outage it exists to notice"
    ),
    KEEPER: (
        "only reads its `needs` -- a name-keeper with no action, no checkout and "
        "no secret (CI-FOLD, #1799): a VM create and boot (~52 s) bought it nothing"
    ),
    SUMMARY: (
        "only writes a fixed step summary -- no action, no checkout, no `needs`, "
        "no secret (CI-FOLD, #1799): a VM create and boot bought it nothing"
    ),
}

SECRETS_ON_VMS = "owner decision 2026-10-14: secrets on ephemeral self-hosted VMs accepted"

#: The only repository secrets a guarded job may hold, per job. The owner
#: accepted the residual risk: for the job's life the secret is readable on
#: the VM through IAP SSH or a disk snapshot in saga-ci-runners. Any other
#: secret on a guarded job, or one of these on another job, fails.
GUARDED_SECRETS: dict[str, tuple[frozenset[str], str]] = {
    "auto-merge.yml:enable": (frozenset({"MERGE_APP_PRIVATE_KEY"}), SECRETS_ON_VMS),
    "auto-merge.yml:disable": (frozenset({"MERGE_APP_PRIVATE_KEY"}), SECRETS_ON_VMS),
    "auto-merge.yml:ready-on-green": (frozenset({"MERGE_APP_PRIVATE_KEY"}), SECRETS_ON_VMS),
    "merge-train.yml:tick": (frozenset({"MERGE_APP_PRIVATE_KEY"}), SECRETS_ON_VMS),
    "accept.yml:sandbox": (frozenset({"SWARM_SANDBOX_GITHUB_TOKEN"}), SECRETS_ON_VMS),
    "accept.yml:sandbox-results": (frozenset({"SWARM_SANDBOX_GITHUB_TOKEN"}), SECRETS_ON_VMS),
}

#: Every job NOT on the guarded expression. Keyed `file:job`.
HOSTED_JOBS: dict[str, str] = {
    "accept.yml:target": CLOUD,
    "accept.yml:quiesce": CLOUD,
    "accept.yml:smoke": CLOUD,
    "accept.yml:acceptance": CLOUD,
    "accept.yml:acceptance-2": CLOUD,
    "accept.yml:tenant-isolation": CLOUD,
    "accept.yml:report": CLOUD,
    "accept.yml:live-browser": CLOUD,
    "application.yml:build": CLOUD,
    "ci-fix.yml:fix": CLOUD,
    "hotfix.yml:images": CLOUD,
    "hotfix.yml:promote": CLOUD,
    "hotfix.yml:infrastructure": CLOUD,
    "hotfix.yml:deploy": CLOUD,
    "iam-refusal-probe.yml:probe": CLOUD,
    "release.yml:build": CLOUD,
    "release.yml:promote": CLOUD,
    "release.yml:infrastructure": CLOUD,
    "release.yml:infrastructure-iam": CLOUD,
    "release.yml:deploy": CLOUD,
    "security.yml:images": CLOUD,
    "terraform.yml:plan": CLOUD,
    "runner-fallback.yml:judge": JUDGE,
    # CI-FOLD (#1799): the name-keepers of the folded sub-minute checks, the
    # two that stand for the shards, and terraform's fixed summary.
    "application.yml:workflows": KEEPER,
    "application.yml:ruff": KEEPER,
    "application.yml:manifests": KEEPER,
    "application.yml:python-checks": KEEPER,
    "application.yml:python": KEEPER,
    "application.yml:ui": KEEPER,
    "security.yml:secrets": KEEPER,
    "security.yml:policy": KEEPER,
    "terraform.yml:validate": KEEPER,
    "terraform.yml:test": KEEPER,
    "terraform.yml:plan-not-run": SUMMARY,
}

_SECRET = re.compile(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)")


def _workflow_jobs():
    """(file:job, job, the workflow's own top-level permissions)."""
    files = sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])
    for path in files:
        doc = _doc(path)
        for name, job in (doc.get("jobs") or {}).items():
            yield f"{path.name}:{name}", job, doc.get("permissions")


def _id_token(job: dict, workflow_permissions: object) -> bool:
    """A job's own `permissions` replaces the workflow's entirely."""
    perms = job["permissions"] if "permissions" in job else workflow_permissions
    if perms == "write-all":
        return True
    return isinstance(perms, dict) and perms.get("id-token") == "write"


def _secrets(job: dict) -> set[str]:
    return set(_SECRET.findall(yaml.safe_dump(job))) - {"GITHUB_TOKEN"}


def _uses(job: dict) -> list[str]:
    return [str(s.get("uses", "")) for s in job.get("steps") or []]


def _reason_holds(reason: str, job: dict, workflow_permissions: object) -> bool:
    if reason == CLOUD:
        return _id_token(job, workflow_permissions) and any(
            u.startswith("google-github-actions/auth@") for u in _uses(job)
        )
    if reason in (KEEPER, SUMMARY):
        steps = job.get("steps") or []
        bare = bool(steps) and all("uses" not in s and "run" in s for s in steps) and not _secrets(job)
        if reason == KEEPER:
            return bare and bool(job.get("needs"))
        return bare and not job.get("needs") and all("GITHUB_STEP_SUMMARY" in s["run"] for s in steps)
    if reason == JUDGE:
        return any(
            u.startswith("actions/create-github-app-token@")
            and "permission-actions-variables" in (s.get("with") or {})
            for s in job.get("steps") or []
            for u in [str(s.get("uses", ""))]
        )
    raise AssertionError(f"unknown reason {reason!r}")


def test_every_job_is_on_the_pool_or_hosted_for_a_checked_reason() -> None:
    jobs = list(_workflow_jobs())
    assert jobs, "no jobs parsed"
    problems = []
    names = set()
    for name, job, wf_perms in jobs:
        names.add(name)
        if "uses" in job and "runs-on" not in job:
            continue  # a reusable workflow call: its own jobs are scanned
        runs_on = job.get("runs-on")
        gated = _is_guarded(runs_on)
        if name in PROBE_JOBS:
            if _verdict_of(name, job)[0] != "probe":
                problems.append(f"{name}: {_verdict_of(name, job)[1]}")
            continue
        if name in HOSTED_JOBS:
            reason = HOSTED_JOBS[name]
            if gated:
                waiting = f" {PATCH_NOTE}" if reason in (KEEPER, SUMMARY) else ""
                problems.append(f"{name}: listed as hosted ({reason}) but on the guarded expression{waiting}")
            elif _verdict_of(name, job)[0] != "hosted":
                problems.append(f"{name}: listed as hosted but runs-on {runs_on!r} is not a hosted image")
            elif not _reason_holds(reason, job, wf_perms):
                problems.append(f"{name}: kept hosted because it {REASONS[reason]}, which is no longer true")
        elif not gated:
            problems.append(
                f"{name}: runs-on {runs_on!r} is neither the guarded expression nor in HOSTED_JOBS"
            )
        elif _verdict_of(name, job)[0] != "gated":
            problems.append(f"{name}: {_verdict_of(name, job)[1]}")
    stale = sorted(set(HOSTED_JOBS) - names)
    problems += [f"{n}: in HOSTED_JOBS but no such job" for n in stale]
    print(f"checked {len(jobs)} job(s), {len(HOSTED_JOBS)} kept hosted")
    assert not problems, "\n".join(problems)


def _secrets_refused(name: str, job: dict) -> set[str]:
    """The repository secrets a guarded job holds beyond what GUARDED_SECRETS
    allows that job."""
    allowed = GUARDED_SECRETS.get(name, (frozenset(), ""))[0]
    return _secrets(job) - allowed


def test_no_guarded_job_holds_a_cloud_identity_or_an_unlisted_secret() -> None:
    """The other direction: a credential never rides onto the pool by a job
    being moved, or by a credential being added to a job already there.
    The only exceptions are GUARDED_SECRETS, job by job."""
    held = []
    guarded = set()
    for name, job, wf_perms in _workflow_jobs():
        if not _is_guarded(job.get("runs-on")):
            continue
        guarded.add(name)
        if _id_token(job, wf_perms):
            held.append(f"{name}: id-token: write")
        if _secrets_refused(name, job):
            held.append(f"{name}: secrets {sorted(_secrets_refused(name, job))} not in GUARDED_SECRETS")
    for name, (names, reason) in GUARDED_SECRETS.items():
        assert reason == SECRETS_ON_VMS, name
        if name not in guarded:
            held.append(f"{name}: in GUARDED_SECRETS but not a guarded job (stale entry)")
    assert not held, "credentials on self-hosted jobs:\n" + "\n".join(held)


def test_the_secret_allowlist_can_fail() -> None:
    """Controls: an allowed secret on another job, and another secret on an
    allowed job, are both refused."""
    app = {"steps": [{"with": {"private-key": "${{ secrets.MERGE_APP_PRIVATE_KEY }}"}}]}
    assert _secrets_refused("auto-merge.yml:enable", app) == set()
    assert _secrets_refused("auto-merge.yml:requeue", app) == {"MERGE_APP_PRIVATE_KEY"}
    extra = {"steps": [{"env": {"A": "${{ secrets.MERGE_APP_PRIVATE_KEY }}", "B": "${{ secrets.OTHER }}"}}]}
    assert _secrets_refused("auto-merge.yml:enable", extra) == {"OTHER"}
    pat = {"steps": [{"env": {"T": "${{ secrets.SWARM_SANDBOX_GITHUB_TOKEN }}"}}]}
    assert _secrets_refused("auto-merge.yml:disable", pat) == {"SWARM_SANDBOX_GITHUB_TOKEN"}


def test_the_reason_checks_can_fail() -> None:
    """Controls: each reason is false for a job without its credential."""
    bare = {"runs-on": "ubuntu-24.04", "steps": [{"run": "true"}]}
    for reason in REASONS:
        assert not _reason_holds(reason, bare, {"contents": "read"}), reason
    cloud = {"permissions": {"id-token": "write"}, "steps": [{"uses": "google-github-actions/auth@v3"}]}
    assert _reason_holds(CLOUD, cloud, None)
    # A name-keeper or a summary job stops qualifying the moment it checks
    # out, runs an action or holds a secret. MUTATION: drop the `uses` test.
    keeper = {"needs": ["checks"], "steps": [{"run": 'test "${VERDICT}" = success'}]}
    assert _reason_holds(KEEPER, keeper, None)
    assert not _reason_holds(KEEPER, {**keeper, "steps": [{"uses": "actions/checkout@v5"}, *keeper["steps"]]}, None)
    assert not _reason_holds(KEEPER, {**keeper, "steps": [{"run": "echo ${{ secrets.X }}"}]}, None)
    summary = {"steps": [{"run": 'echo done >> "$GITHUB_STEP_SUMMARY"'}]}
    assert _reason_holds(SUMMARY, summary, None)
    assert not _reason_holds(SUMMARY, {**summary, "needs": "x"}, None)
    assert not _reason_holds(SUMMARY, {"steps": [{"uses": "actions/checkout@v5"}, *summary["steps"]]}, None)
    # A job's own permissions replace the workflow's.
    assert not _id_token({"permissions": {"contents": "read"}}, {"id-token": "write"})
    assert _id_token({}, {"id-token": "write"})
