"""The release promotes the build application.yml made, and builds nothing of
its own on a push.

Owner decision, 2026-09-24 (docs/ci.md): application.yml builds every image
once per commit on main and records the digests; release.yml waits for that
job, then scans and promotes exactly those digests, all or nothing. Before,
every push to main built all eight images twice, and the release waited
behind the duplicate (94ee043: application.yml's build 16:33:09-16:41:51, the
release's own 16:39:24-16:48:17).

release.yml runs only on main, so NOTHING on a pull request executes it. What
a pull request CAN check is how the two workflows are wired, and that is what
this file reads -- the workflows themselves, parsed, with the one expression
that decides "reuse only" versus "reuse or build" evaluated for each event:

  * no step of the release runs build-images.sh except through --reuse-ci, and
    on a push that is `only`: no second Cloud Build;
  * the job that reuses can read CI's runs (actions: read, GH_TOKEN) and still
    authenticate to GCP (id-token: write);
  * it scans the manifest that step wrote (push-images.sh --scan-only, before
    the approval) and hands it on; the one promotion fetches that manifest
    and promotes it through push-images.sh --manifest, with the scan on and
    after trivy is installed (test_release_prod_gate.py holds the promotion
    behind the approval);
  * application.yml builds every commit the release would ship, with no tag of
    its own, and a run on main is neither cancelled nor -- while still queued
    -- replaced by the next push.

WHAT THIS CANNOT PROVE: that GitHub runs these workflows as read -- that the
job gets the token scopes it asks for, that the API returns what the scripts
expect, that a real release reuses a real build. Only a release on main does.
`test_ci_built_images.py` and `test_push_images_promotes_recorded_digests.py`
cover the scripts' behaviour; application.yml's `workflows` job lints both
files with actionlint.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO / ".github" / "workflows"


def _workflow(name: str) -> dict:
    data = yaml.safe_load((WORKFLOWS / name).read_text())
    # PyYAML reads the bare key `on` as the boolean True.
    if True in data:
        data["on"] = data.pop(True)
    return data


def _code(run: str) -> str:
    return "\n".join(l for l in (run or "").splitlines() if not l.lstrip().startswith("#"))


def _steps(workflow: dict, script: str):
    """(job id, job, index, step) for every step whose run: invokes `script`."""
    for job_id, job in workflow["jobs"].items():
        for index, step in enumerate(job.get("steps") or []):
            if re.search(rf"(^|[\s/]){re.escape(script)}(\s|$)", _code(step.get("run", ""))):
                yield job_id, job, index, step


def _needs(job: dict) -> list[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def _upstream(jobs: dict, job_id: str) -> set[str]:
    """Every job `job_id` needs, directly or through another job."""
    seen: set[str] = set()
    todo = list(_needs(jobs[job_id]))
    while todo:
        need = todo.pop()
        if need not in seen:
            seen.add(need)
            todo.extend(_needs(jobs[need]))
    return seen


def _gh_format(template: str, *args) -> str:
    """GitHub's format(): `{N}` is the Nth argument, `{{` and `}}` are literal
    braces -- the same rules as str.format for the plain `{N}` it uses."""
    return str(template).format(*(str(a) for a in args))


def _evaluate(expression, **context: str):
    """A GitHub expression of the small family these files use -- context
    lookups, string literals, ==, !=, &&, ||, ! and format() -- for one set of
    values. Anything outside that family raises, and the test says so rather
    than guessing."""
    if isinstance(expression, bool):
        return expression
    body = re.fullmatch(r"\s*\$\{\{(.*)\}\}\s*", str(expression), re.S)
    text = body.group(1) if body else str(expression)
    for name, value in context.items():
        text = re.sub(rf"(?<![\w.]){re.escape(name)}(?![\w.])", repr(value), text)
    text = text.replace("&&", " and ").replace("||", " or ")
    text = re.sub(r"!(?!=)", " not ", text)
    return eval(text, {"__builtins__": {}}, {"format": _gh_format})  # noqa: S307 -- repository file, test only


def _render(template, **context: str) -> str:
    """A workflow string holding any number of `${{ }}` interpolations, as
    GitHub would render it for one set of values."""
    return re.sub(
        r"\$\{\{(.*?)\}\}",
        lambda m: str(_evaluate("${{" + m.group(1) + "}}", **context)),
        str(template),
        flags=re.S,
    )


def _reuse_mode(step: dict, event_name: str) -> str:
    """What `--reuse-ci` receives in this step when the run's event is `event_name`."""
    code = _code(step["run"])
    arg = re.search(r"build-images\.sh\s+--reuse-ci\s+(\"?\$\{?(\w+)\}?\"?|[\w-]+)", code)
    assert arg, f"no `--reuse-ci <mode>` in: {code!r}"
    if arg.group(2):
        expression = (step.get("env") or {}).get(arg.group(2))
        assert expression is not None, f"--reuse-ci reads ${arg.group(2)}, which the step never sets"
        return _evaluate(expression, **{"github.event_name": event_name})
    return arg.group(1)


def test_the_release_builds_only_through_reuse_and_on_a_push_only_reuses():
    release = _workflow("release.yml")
    builds = list(_steps(release, "build-images.sh"))
    assert builds, "no step of release.yml obtains the images at all"
    for job_id, _, _, step in builds:
        assert "--reuse-ci" in _code(step["run"]), (
            f"release.yml's {job_id} job runs build-images.sh without --reuse-ci: a second Cloud "
            "Build of a commit application.yml already built"
        )
        assert _reuse_mode(step, "push") == "only", (
            f"on a push, release.yml's {job_id} job may build: {_reuse_mode(step, 'push')!r}"
        )
        assert _reuse_mode(step, "workflow_dispatch") == "or-build", (
            "a release dispatched by hand for a commit CI never built cannot build it: "
            f"{_reuse_mode(step, 'workflow_dispatch')!r}"
        )
    for job_id, job in release["jobs"].items():
        for step in job.get("steps") or []:
            assert "builds submit" not in _code(step.get("run", "")), (
                f"release.yml's {job_id} job submits a Cloud Build directly"
            )


def test_the_job_that_reuses_can_read_ci_and_still_reach_gcp():
    release = _workflow("release.yml")
    for job_id, job, _, step in _steps(release, "build-images.sh"):
        granted = job.get("permissions") or release.get("permissions") or {}
        assert granted.get("actions") in ("read", "write"), (
            f"release.yml's {job_id} job cannot read application.yml's runs: permissions {granted}"
        )
        assert granted.get("id-token") == "write", (
            f"release.yml's {job_id} job can no longer authenticate to GCP: permissions {granted}"
        )
        env = {**(job.get("env") or {}), **(step.get("env") or {})}
        assert "GH_TOKEN" in env, f"release.yml's {job_id} job gives gh no token"


# build-images.sh writes build/images-${ENVIRONMENT}.json; the scan and the
# promotion must both read exactly that file.
_OBTAINED_MANIFEST = re.compile(r"--manifest\s+\"?build/images-\$\{?ENVIRONMENT\}?\.json\"?")


def _trivy_installed_before(steps: list, index: int) -> bool:
    installs = [
        i for i, s in enumerate(steps)
        if "trivy" in _code(s.get("run", "")) and "install" in _code(s.get("run", ""))
    ]
    return bool(installs) and min(installs) < index


def _uses(step: dict, action: str) -> bool:
    return str(step.get("uses", "")).startswith(f"actions/{action}@")


def test_the_release_scans_before_the_approval_and_promotes_that_manifest_after_it():
    """Owner decision, 2026-09-24 (docs/ci.md): moving :prod sits behind the
    prod approval; scanning, which is read-only, may come before it. So the
    job that obtains the images scans them WITHOUT promoting and hands that
    exact manifest on, and the one promotion -- in a later job, which
    test_release_prod_gate.py holds behind the approval -- fetches it, and
    scans it again as part of promoting it."""
    release = _workflow("release.yml")
    jobs = release["jobs"]
    rendered = {"github.event.inputs.environment": None}  # a push: dev

    obtained = list(_steps(release, "build-images.sh"))
    assert len(obtained) == 1, f"release.yml obtains its images in {len(obtained)} places"
    build_id, build, build_index, _ = obtained[0]

    # 1. Before the approval: scan exactly what was obtained, move nothing.
    scans = [(i, s) for i, s in enumerate(build["steps"]) if "push-images.sh" in _code(s.get("run", ""))]
    assert len(scans) == 1, (
        f"release.yml's {build_id} job runs push-images.sh {len(scans)} times; it should scan, once"
    )
    index, step = scans[0]
    code = _code(step["run"])
    assert index > build_index, "the scan runs before the images are obtained"
    assert _OBTAINED_MANIFEST.search(code), f"the scan does not read the manifest the step before it wrote: {code!r}"
    assert re.search(r"(^|\s)--scan-only(\s|$)", code) and "--no-scan" not in code, (
        f"release.yml's {build_id} job runs before the approval and does more than scan: {code!r}"
    )
    assert _trivy_installed_before(build["steps"], index), "trivy is not installed before the scan that needs it"
    handed = [
        s for s in build["steps"][index + 1:]
        if _uses(s, "upload-artifact")
        and _render((s.get("with") or {}).get("path", ""), **rendered) == "build/images-dev.json"
    ]
    assert len(handed) == 1, (
        f"release.yml's {build_id} job does not hand the manifest it scanned to the promotion "
        "(one upload-artifact of build/images-<env>.json, after the scan)"
    )
    artifact = _render(handed[0]["with"]["name"], **rendered)

    # 2. After it: one promotion, of that manifest, scanned again.
    promotions = [
        (job_id, i, s)
        for job_id, job in jobs.items()
        for i, s in enumerate(job.get("steps") or [])
        if "push-images.sh" in _code(s.get("run", "")) and "--scan-only" not in _code(s.get("run", ""))
    ]
    assert len(promotions) == 1, f"release.yml promotes in {len(promotions)} places, not one"
    job_id, index, step = promotions[0]
    job = jobs[job_id]
    code = _code(step["run"])
    assert _OBTAINED_MANIFEST.search(code), f"the promotion does not promote the manifest that was scanned: {code!r}"
    assert re.search(r"(^|\s)--scan(\s|$)", code) and "--no-scan" not in code, (
        f"the promotion does not scan: {code!r}"
    )
    assert _trivy_installed_before(job["steps"], index), "trivy is not installed before the promotion's scan"
    fetched = [
        i for i, s in enumerate(job["steps"][:index])
        if _uses(s, "download-artifact")
        and _render((s.get("with") or {}).get("name", ""), **rendered) == artifact
        and _render((s.get("with") or {}).get("path", ""), **rendered).rstrip("/") == "build"
    ]
    assert fetched, f"release.yml's {job_id} job promotes without fetching {artifact!r} into build/ first"
    assert job_id == build_id or build_id in _upstream(jobs, job_id), (
        f"release.yml's {job_id} job does not need {build_id}, so the manifest it fetches may not exist yet"
    )


def test_application_builds_every_commit_the_release_would_ship():
    application = _workflow("application.yml")
    release = _workflow("release.yml")
    push = application["on"].get("push") or {}
    assert "main" in (push.get("branches") or []), "application.yml does not run on a push to main"
    assert "paths-ignore" not in push, "application.yml's push trigger ignores paths the release may ship"
    if "paths" in push:
        missing = [p for p in release["on"]["push"].get("paths", []) if p not in push["paths"]]
        assert not missing, (
            f"a push touching only {missing} starts a release but no build in application.yml"
        )

    builders = list(_steps(application, "build-images.sh"))
    assert len(builders) == 1, f"application.yml builds in {len(builders)} places"
    job_id, job, _, step = builders[0]
    assert _evaluate(job.get("if", "true"), **{"github.event_name": "push"}), (
        f"application.yml's {job_id} job does not run on a push to main"
    )
    code = _code(step["run"])
    assert "--reuse-ci" not in code, "application.yml's build reuses instead of building"
    assert "--tag" not in code, (
        "application.yml's build names its own tag; the tag is the commit's, derived once "
        "(git_sha), so a release that builds a commit itself tags it the same way"
    )


def test_a_build_on_main_is_not_cancelled_by_the_next_push():
    """The release waits for this run's build. Cancelling it part-way strands
    that release, and never stopped the Cloud Build anyway."""
    cancel = _workflow("application.yml")["concurrency"]["cancel-in-progress"]
    assert _evaluate(cancel, **{"github.ref": "refs/heads/main"}) is False, (
        f"cancel-in-progress is {cancel!r}: a push to main cancels the build the previous "
        "commit's release is waiting for"
    )
    assert _evaluate(cancel, **{"github.ref": "refs/pull/1/merge"}) is True, (
        f"cancel-in-progress is {cancel!r}: pull requests no longer cancel superseded runs"
    )


def test_a_queued_build_on_main_is_never_replaced_by_the_next_push():
    """cancel-in-progress is not enough. GitHub keeps ONE pending run per
    concurrency group and cancels the older pending run when a newer one
    queues, whatever cancel-in-progress says. Measured on 2026-09-24: release
    run 36035365877, pending in release-dev, was cancelled at 17:41:28 -- two
    seconds after 36036058352 queued -- with zero jobs.

    With one group for all of main, a push made while commit B's application
    run was queued behind A's cancelled B's run. release.yml has a path filter
    and application.yml has none, so when that push touched only docs, tests
    or the README it started no release to replace B's: B's release, still
    pending, then found its commit never built and went red, and B's change
    did not reach dev until the next push that starts a release. So on main
    each commit's run has a group of its own; on a pull request a newer push
    still supersedes the older run."""
    group = _workflow("application.yml")["concurrency"]["group"]

    def rendered(ref: str, sha: str) -> str:
        return _render(group, **{"github.ref": ref, "github.sha": sha})

    main_a = rendered("refs/heads/main", "a" * 40)
    main_b = rendered("refs/heads/main", "b" * 40)
    assert main_a != main_b, (
        f"every commit on main shares the concurrency group {main_a!r}: a queued run is cancelled "
        "by the next push to main, and the release waiting for its build is stranded"
    )
    pr_a = rendered("refs/pull/7/merge", "a" * 40)
    pr_b = rendered("refs/pull/7/merge", "b" * 40)
    assert pr_a == pr_b, (
        f"a pull request's pushes land in different groups ({pr_a!r}, {pr_b!r}): a superseded "
        "run is no longer cancelled"
    )
    assert rendered("refs/pull/8/merge", "a" * 40) != pr_a, "two pull requests share one group"
    assert main_a != pr_a, "a pull request can queue in main's group"
