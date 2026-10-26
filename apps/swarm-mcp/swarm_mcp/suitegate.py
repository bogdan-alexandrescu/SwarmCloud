"""`swarm_suite_gate`: a repository's whole Playwright suite as a sharded gate (PLAYWRIGHT-GATE, part of #2253).

BGD's decision of 2026-10-26: SwarmCloud runs a repository's Playwright suite
as a gate, so a project (first: Product Oracle, ~1,230 mock specs) can gate a
merge train in SwarmCloud instead of on a laptop. docs/suite-gate.md says what
it runs, what it costs and how a project wires it in.

IT IS A WORKFLOW, NOT A RUNTIME. Invariant 10 says a caller names a runner
profile and supplies data, never an image, a command line or resources. So
this module takes a registered repository, a ref and a few parameters, renders
N shard steps and one report step on `claude-code` (its `standard` class, by
leaving the class unnamed), and submits them through `workflows.submit` like
any other workflow. The shell lines the agents run are written HERE, from
values checked against plain-name and relative-path patterns, so no caller
string can add a command to them.

* a shard step runs `npm ci`, `npx playwright install chromium` and
  `npx playwright test --shard k/N --retries=1 --reporter=blob,line` from
  `working_dir`, and puts its blob report in its artifacts under a name only
  it writes. It runs tests and nothing else: never a fix, never a commit
  (`allow_empty_diff`, strategy `collect`, `merge: off`).
* the `report` step stages every shard's blob (`input_from`), merges them with
  `npx playwright merge-reports` into one HTML report artifact, and writes
  `summary.md` and `summary.json`: passed, failed, flaky, and each failure's
  spec and first error line. That summary is what the caller reads, through
  `swarm_result` and `swarm_artifact`.

A failing TEST is not a failing STEP: a shard whose tests fail still writes
its blob and succeeds, so the report runs and names the failures. A shard
that produces no blob (the install failed, Chromium cannot start) is a
failing step: the platform fails it for the missing output, retries it, and
under `fail_workflow` the report is cancelled, so the gate reads FAILED rather
than green over a slice nobody ran.
"""

from __future__ import annotations

import re
from typing import Any

from . import profiles as catalogue
from . import workflows
from .client import SwarmError

#: The suites this tool renders. One today; the parameter exists so a second
#: is an entry here, not a second tool.
SUITES = ("playwright",)

MIN_SHARDS = 1
#: 20 shards and the report are 21 standard units of plain claude-code at
#: once (none of them runs on the browser pool), well under the API's
#: 50-step workflow limit. Past ~20 a shard's fixed cost (npm ci, the Chromium
#: download, the agent's start) outweighs the specs it runs.
MAX_SHARDS = 20
#: Eight shards of Product Oracle's ~1,230 specs is ~154 specs each: a shard
#: finishes in minutes, and the gate holds 9 units while it runs.
DEFAULT_SHARDS = 8

#: The one profile every step runs on: plain claude-code, which FIX-2253 gives
#: Chromium's system libraries and fonts. Its class is `standard` (1 unit).
PROFILE = "claude-code"
#: A shard and the report run commands and read their output; neither writes
#: code. The cheaper class does that as well as the default `opus`.
MODEL_CLASS = "sonnet"

REPORT_STEP = "report"
SUMMARY_FILES = ("summary.md", "summary.json")
HTML_REPORT = "playwright-report.zip"

#: `owner/repo`, or its GitHub URL: the registration is matched on the pair.
_SLUG = re.compile(r"^(?:https://github\.com/)?([A-Za-z0-9_.-]{1,100})/([A-Za-z0-9_.-]{1,100}?)"
                   r"(?:\.git)?/?$", re.IGNORECASE)
#: A branch, tag or SHA. No leading '-' (an option to git), no whitespace, no
#: shell metacharacter; `..` and `.lock` are refused below as git refuses them.
_REF = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]{0,199}$")
#: One path segment of `working_dir` or `env_file`: a plain name.
_SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,99}$|^\.[A-Za-z0-9_][A-Za-z0-9._-]{0,98}$")
#: A Playwright project name, as `--project=<name>` takes it unquoted.
_PROJECT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,99}$")


def _refuse(message: str) -> SwarmError:
    return SwarmError(f"swarm_suite_gate: {message}. Nothing was sent")


def check_shards(value: Any) -> int:
    if value is None:
        return DEFAULT_SHARDS
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_SHARDS <= value <= MAX_SHARDS:
        raise _refuse(f"shards is a whole number from {MIN_SHARDS} to {MAX_SHARDS} "
                      f"(default {DEFAULT_SHARDS}); it was {value!r}")
    return value


def check_ref(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not _REF.fullmatch(text) or ".." in text or text.endswith((".lock", "/")) or "//" in text:
        raise _refuse(f"ref is a branch, tag or commit SHA of plain characters; {value!r} is not")
    return text


def check_relative_path(value: Any, name: str, *, default: str | None = None) -> str | None:
    """A repo-relative path of plain segments, or `default` when not given."""
    if value is None or (isinstance(value, str) and value.strip() in ("", ".") and default is not None):
        return default
    text = value.strip().strip("/") if isinstance(value, str) else ""
    if not isinstance(value, str) or value.strip().startswith("/") or not text:
        raise _refuse(f"{name} is a path relative to the repository's root; {value!r} is not")
    segments = text.split("/")
    if not all(_SEGMENT.fullmatch(s) and s not in (".", "..") for s in segments):
        raise _refuse(f"{name} is a relative path of plain names (letters, digits, '.', '_', '-'), "
                      f"never '..' or a shell character; {value!r} is not")
    return "/".join(segments)


def check_project(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _PROJECT.fullmatch(value.strip()):
        raise _refuse(f"project is a Playwright project's name (letters, digits, '.', '_', '-'); "
                      f"{value!r} is not")
    return value.strip()


def check_suite(value: Any) -> str:
    if value not in SUITES:
        raise _refuse(f"suite is one of {list(SUITES)}; it was {value!r}")
    return str(value)


def registered(client: Any, repository: Any) -> tuple[str, str]:
    """(owner, repo) of a repository the caller's workspace registered; refused otherwise."""
    match = _SLUG.fullmatch(repository.strip()) if isinstance(repository, str) else None
    if match is None:
        raise _refuse(f"repository is owner/repo; {repository!r} is not")
    row = client.registration(match.group(1), match.group(2))
    if not isinstance(row, dict) or not row.get("repo_id"):
        raise _refuse(f"{match.group(1)}/{match.group(2)} is not registered in the selected "
                      "workspace, so no step could clone it; a workspace admin registers it "
                      "in the console's Repositories")
    return str(row.get("owner") or match.group(1)), str(row.get("repo") or match.group(2))


def shard_step_id(k: int, n: int) -> str:
    return f"shard-{k}-of-{n}"


def blob_name(k: int, n: int) -> str:
    """The artifact a shard writes and the report stages: one name per shard,
    since the name is also where the file lands in the report's checkout."""
    return f"playwright-blob-{k}-of-{n}.zip"


def _env_prefix(env_file: str | None) -> str:
    """The env file loaded in the SAME command line as the tests: an agent's
    shell calls do not share exported variables, so a separate `source`
    would load it into a shell the tests never run in."""
    if env_file is None:
        return ""
    return f'set -a && . "$(git rev-parse --show-toplevel)/{env_file}" && set +a && '


def shard_prompt(k: int, n: int, *, working_dir: str, project: str | None, env_file: str | None) -> str:
    test = f"npx playwright test --shard {k}/{n} --retries=1 --reporter=blob,line"
    if project is not None:
        test += f" --project={project}"
    env_note = (
        f"   (The test line first loads `{env_file}`, the suite's non-secret env file; keep it "
        "one command line.)\n" if env_file is not None else ""
    )
    blob = blob_name(k, n)
    return (
        f"You are shard {k} of {n} of a Playwright suite gate. Run the tests, report, and stop. "
        "Never fix code, never edit a test, never commit: what you find goes in the report, "
        "not in the repository.\n\n"
        f"1. From the repository's root, `cd {working_dir}`, then run, in order:\n"
        "   `npm ci`\n"
        "   `npx playwright install chromium` (never `--with-deps`: the image carries Chromium's "
        "libraries)\n"
        f"   `{_env_prefix(env_file)}{test}`\n"
        f"{env_note}"
        "   Run that test command once. `--retries=1` already retries a flaky failure once; do "
        "not run it again, narrow it, or change its arguments.\n"
        "2. Failing tests are this gate's answer, not your failure: the command exits non-zero "
        "when a test fails, and you still finish normally.\n"
        f"3. The blob reporter writes one .zip under `blob-report/` (beside the Playwright config, "
        f"in `{working_dir}`). Copy it to "
        f"`$SWARM_ARTIFACTS_DIR/{blob}` -- exactly that name: the report step stages it by name. "
        "If no blob was written (`npm ci` or the install failed, Chromium did not start), do NOT "
        "create one: say what failed, with its first error line, and stop. The step then fails "
        "for the missing blob, which is the truth.\n"
        "4. Leave the checkout as you found it: delete what the run created (`test-results/`, "
        "`blob-report/`, `playwright-report/`) and `git checkout --` any tracked file it changed, "
        "so `git status --porcelain` prints nothing.\n"
        "5. End with one line: the counts the line reporter printed (passed, failed, flaky, "
        "skipped)."
    )


def report_prompt(n: int, *, working_dir: str) -> str:
    blobs = [blob_name(k, n) for k in range(1, n + 1)]
    listed = "\n".join(f"   - `{b}`" for b in blobs)
    return (
        f"You are the report step of a Playwright suite gate of {n} shard(s). Merge their blob "
        "reports, summarise, and stop. Never fix code, never commit.\n\n"
        f"1. These {n} blob reports were staged OUTSIDE the checkout, in its parent directory "
        "(`..` from the repository's root), one per shard; the platform lists their absolute "
        "paths under 'Earlier steps of this workflow gave you these files':\n"
        f"{listed}\n"
        "   Every one must be there; if one is missing, say which, write no summary, and stop: a "
        "report step that leaves no summary.json is a red gate.\n"
        f"2. `cd {working_dir}`, run `npm ci` (merge-reports needs the repository's own "
        "`@playwright/test`), then `mkdir -p /tmp/all-blobs` and move every one of those files "
        "from the checkout's parent (their absolute paths) into it.\n"
        "3. Merge them twice, from the same directory:\n"
        "   `npx playwright merge-reports --reporter=html /tmp/all-blobs` (writes "
        "`playwright-report/`), then\n"
        "   `PLAYWRIGHT_JSON_OUTPUT_NAME=/tmp/results.json npx playwright merge-reports "
        "--reporter=json /tmp/all-blobs`.\n"
        f"4. Zip `playwright-report/` to `$SWARM_ARTIFACTS_DIR/{HTML_REPORT}` and copy "
        "`/tmp/results.json` to `$SWARM_ARTIFACTS_DIR/results.json`.\n"
        "5. From results.json write `$SWARM_ARTIFACTS_DIR/summary.json`: "
        '`{"passed": stats.expected, "failed": stats.unexpected, "flaky": stats.flaky, '
        '"skipped": stats.skipped, "shards": ' f"{n}" ', "failures": [{"spec": <file>:<line>, '
        '"title": <test title>, "error": <first line of the last result\'s error message>}]}`, '
        "one failure per test whose status is `unexpected`, walking `suites` recursively. Then "
        "`$SWARM_ARTIFACTS_DIR/summary.md`: a first line `PASSED` or `FAILED` (FAILED when "
        "failed > 0), the four counts, and one line per failure: its spec, title and first error "
        "line. Flaky tests are listed after the failures, marked flaky.\n"
        "6. Delete what you created in the checkout (`playwright-report/`, `test-results/`) so "
        "`git status --porcelain` prints nothing.\n"
        "7. End with summary.md's text as your final message."
    )


def render(owner: str, repo: str, *, ref: str, working_dir: str, shards: int,
           project: str | None = None, env_file: str | None = None) -> dict[str, Any]:
    """The workflow as `swarm_workflow` would take it: steps in the tool's form,
    and the top-level fields `workflows.submit` sends."""
    steps: list[dict[str, Any]] = []
    for k in range(1, shards + 1):
        steps.append({
            "step_id": shard_step_id(k, shards),
            "runner_profile": PROFILE,
            "prompt": shard_prompt(k, shards, working_dir=working_dir, project=project,
                                   env_file=env_file),
            "allow_empty_diff": True,
        })
    shard_ids = [s["step_id"] for s in steps]
    steps.append({
        "step_id": REPORT_STEP,
        "runner_profile": PROFILE,
        "prompt": report_prompt(shards, working_dir=working_dir),
        "depends_on": shard_ids,
        "input_from": {shard_step_id(k, shards): blob_name(k, shards) for k in range(1, shards + 1)},
        "allow_empty_diff": True,
    })
    return {
        "steps": steps,
        "strategy": "collect",
        "merge": "off",
        "repository_url": f"https://github.com/{owner}/{repo}",
        "repository_ref": ref,
        "title": workflows.check_title(f"Playwright gate · {owner}/{repo}@{ref} · {shards} shards",
                                       where="swarm_suite_gate"),
        "label": "suite-gate",
    }


def gate(client: Any, args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Check every argument, render, submit. Returns (envelope, rendered).

    Every check runs before the registration is read, and the registration is
    read before anything is sent: a refused call sends nothing.
    """
    check_suite(args.get("suite"))
    shards = check_shards(args.get("shards"))
    ref = check_ref(args.get("ref"))
    working_dir = check_relative_path(args.get("working_dir"), "working_dir", default=".")
    project = check_project(args.get("project"))
    env_file = check_relative_path(args.get("env_file"), "env_file")
    owner, repo = registered(client, args.get("repository"))
    rendered = render(owner, repo, ref=ref, working_dir=working_dir or ".", shards=shards,
                      project=project, env_file=env_file)
    steps = catalogue.with_model_classes(
        workflows.build_steps(rendered["steps"]),
        [{"step_id": s["step_id"], "model_class": MODEL_CLASS} for s in rendered["steps"]],
    )
    envelope = workflows.submit(
        client,
        steps=steps,
        strategy=rendered["strategy"],
        merge=rendered["merge"],
        repository_url=rendered["repository_url"],
        repository_ref=rendered["repository_ref"],
        title=rendered["title"],
        label=rendered["label"],
    )
    return envelope, {**rendered, "steps": steps, "shards": shards}


def reply_fields(rendered: dict[str, Any]) -> dict[str, Any]:
    """What the caller reads next, beside the workflow reply."""
    n = rendered["shards"]
    return {
        "suite": "playwright",
        "shards": n,
        "shard_steps": [shard_step_id(k, n) for k in range(1, n + 1)],
        "report_step": REPORT_STEP,
        "read": (
            f"When the workflow ends, swarm_result on the `{REPORT_STEP}` step's task gives "
            f"summary.md's text; swarm_artifact reads summary.md, summary.json (counts and each "
            f"failure's spec and first error line) and {HTML_REPORT} (the merged HTML report). "
            "A FAILED shard means a slice did not run: the gate is not green."
        ),
    }
