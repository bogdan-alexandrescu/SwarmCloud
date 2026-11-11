"""The optional security review step: the security-audit skill, checked, turned into a verdict.

The owner's decisions of 4 November 2026 (docs/design/security-review-step.md):
any workflow that implements, reviews or merges code may add a step, after the
fix and beside the re-review, on a claude-code profile whose signed work
contract loads the `security-audit` plugin (github.com/cloudflare/
security-audit-skill, MIT, pinned in the plugin catalogue). The agent runs the
skill in FULL AUDIT MODE at the `quick` profile, scoped to the pull request's
diff, with a capped sub-agent budget, into an output directory under
`$SWARM_ARTIFACTS_DIR` -- outside the checkout -- and is told that no
OS-enforced sandbox is available, so it never runs the target's code.

WHICH STEP. One whose SIGNED plugins load security-audit, on either route
for one release (docs/design/managed-plugins.md section 1.4.6): its signed
work contract's capabilities (`plugins.selected`, digest-checked against the
signed dispatch block), or a signed managed entry `builtin:security-audit`
with `build: "image"` (`pluginload.bundled`). The same signature that
decides the plugin is loaded decides the step is a security review, so a
tenant worker that can write the task document cannot turn a step into one,
or out of one. Nothing else marks it.

THE VALIDATORS' PATH (section 1.4.6). `INSTALL`, this module's own constant:
the bundle recipe fixes that folder in the image's read-only root
filesystem, so the agent cannot edit the validators, and a bundle image puts
the pinned ones exactly there.

AFTER THE AGENT, BEFORE THE UPLOAD (`finish`). The worker, not the agent:

  * on the bundled route, checks the install's `.swarm-bundle.json` against
    the signed entry again (`pluginload.check_bundle`) before any validator
    runs, so the verdict is always from the pinned copy's own validators. A
    manifest that does not match is `SecurityReviewInvalid` with its fixed
    code: the step fails and no validator runs;

  * runs the skill's own `validate-findings.cjs` and
    `validate-coverage-ledger.cjs` on the output. A validator that fails, a
    file the skill did not write, or no `node` is `SecurityReviewInvalid` with
    the validator's own message: the step FAILS (`CAUSE_INVALID`, never
    retried blindly into a pass), and no verdict is written, so nothing
    downstream can read one. Never a silent pass;
  * derives the verdict. A CONFIRMED finding whose `overall_severity` is
    `critical` or `high` makes it NOT_YET, naming each such finding, and
    writes one owner question per finding into `questions.json` (fix it now,
    accept the risk with a reason, or mark it a false positive), blocking,
    listed in the verdict's `needs_owner`: the existing owner-question park
    (#2070) holds the merge in Needs you until each is answered. Anything
    else is MERGE with the counts: confirmed medium, low and informational
    findings are reported and do not block, and `needs_validation` never
    blocks -- the skill keeps an unproven lead there exactly because it could
    not be shown without running code;
  * writes the verdict twice, byte for byte: `verdict.json`, which every step
    whose id ends in `review` owes (`expected_outputs.is_review_step`), and
    `security-verdict.json` (`VERDICT_FILE`), the name a merge step stages it
    under so it never collides with the re-review's `verdict.json` in the
    merge's staged inputs.

THE MERGE (`agent_worker.merge`). A merge step that staged `VERDICT_FILE`
requires it as well as the re-review's: MERGE passes; NOT_YET holds for its
owner questions, then goes on only when every answer accepts the risk or marks
a false positive, and refuses `security_fix_requested` when any asks for the
fix. A merge that staged none reads exactly as it did before.

THE REPORT. The skill's `REPORT.md`, through the worker's redaction filter, is
posted on the pull request as a comment, cut to GitHub's comment limit with a
pointer to the artifact when it is longer (`comment_body`). `findings.json`,
`coverage-ledger.json` and the rest stay as the step's artifacts.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import pluginload
from . import questions as questions_mod

#: The catalogue name of the plugin, and the skill its unattended mode offers.
#: The catalogue entry lands with the plugin's image (a sibling lane); this
#: module only reads the name.
PLUGIN = "security-audit"
SKILL = "security-audit"
#: Where the image installs the plugin, on either route: the folder the bundle
#: recipe fixes (`pluginload.BUNDLE_ROOT`), read-only (section 1.4.6). The
#: review's own constant, so it outlives the code entry. And the skill folder
#: inside the pinned checkout of the repository.
INSTALL = Path("/opt/swarm/plugins") / PLUGIN
SKILL_SUBDIR = Path("skills") / SKILL

#: The skill's output directory, under `$SWARM_ARTIFACTS_DIR`: outside the
#: checkout, as the skill requires, and uploaded as the step's artifacts.
OUTPUT_DIR_NAME = "security-audit"
#: The owner's choice: a bounded pass over one diff, not a whole-repository audit.
PROFILE = "quick"
#: The most agent invocations, across every phase, one review may make (the
#: skill's `budget`). 16 = the four reconnaissance agents and the one coverage
#: critic the skill reserves for `quick`, plus room for six hunters and five
#: verifiers: one pull request's diff seldom seeds more units than that, and a
#: review that cost more than the change it reviews would be dropped by the
#: people paying for it. The skill's own gate defers what does not fit
#: (`deferred` in the ledger), never runs past it, and says so in the report.
SUBAGENT_BUDGET = 16

FINDINGS = "findings.json"
LEDGER = "coverage-ledger.json"
REPORT = "REPORT.md"
VALIDATE_FINDINGS = "validate-findings.cjs"
VALIDATE_LEDGER = "validate-coverage-ledger.cjs"
#: Each validator's wall-clock bound. They read at most 5 MiB and are linear;
#: a minute is far past what one takes, and short of hanging the step.
VALIDATOR_TIMEOUT_SECONDS = 60
#: What of a validator's output is kept in the failure: its last lines say why.
MAX_VALIDATOR_MESSAGE_CHARS = 2000

#: The verdict every review step owes, and the name the merge stages it as.
REVIEW_VERDICT = "verdict.json"
VERDICT_FILE = "security-verdict.json"
#: The owner's merge rule: confirmed findings of these severities block.
BLOCKING_SEVERITIES = ("critical", "high")
SEVERITIES = ("critical", "high", "medium", "low", "informational")
STATUSES = ("confirmed", "needs_validation", "rejected")

#: The owner question's three answers, in the owner's words.
FIX_NOW = "Fix it now"
ACCEPT_RISK = "Accept the risk"
FALSE_POSITIVE = "Mark it a false positive"
#: The answers that let the merge go on; anything else, free text included,
#: does not (`disposition`).
MERGE_ANSWERS = (ACCEPT_RISK, FALSE_POSITIVE)

#: `result_summary.security_review`: the counts, the verdict, the comment.
SUMMARY_KEY = "security_review"
#: The missing-output cause of a verdict the worker refused to derive. Not in
#: `expected_outputs.RETRYABLE_CAUSES`: a retry reruns a whole audit to the
#: same validator, so the step fails, naming the validator's message.
CAUSE_INVALID = "security_review_invalid"

#: GitHub refuses an issue comment over 65,536 characters; the margin keeps
#: the pointer line and the heading inside it.
GITHUB_COMMENT_LIMIT = 65_536
COMMENT_MARGIN = 1_024
COMMENT_HEADING = "## Security review (security-audit, quick profile)"


#: One question's text and context, held well under `questions_mod`'s own
#: bounds so twenty of them stay inside the file's 64 KiB.
MAX_QUESTION_CHARS = 800
MAX_CONTEXT_CHARS = 1200


class SecurityReviewInvalid(Exception):
    """The skill's output did not validate; `str(exc)` is why, in the validator's words."""


Runner = Callable[..., "subprocess.CompletedProcess[str]"]


# --- which step ----------------------------------------------------------------


def applies(started: Any) -> bool:
    """Whether the step's started plugins (`plugins.Started`, or None) load
    security-audit, through its work contract or as a bundled entry."""
    plugins = getattr(started, "plugins", None) or ()
    return any(getattr(p, "name", None) == PLUGIN for p in plugins) or signed_entry(started) is not None


def signed_entry(started: Any) -> pluginload.Bundled | None:
    """security-audit's signed bundled entry among the started plugins
    (`plugins.Started.bundled`), or None on the code route."""
    for entry in getattr(started, "bundled", None) or ():
        if isinstance(entry, pluginload.Bundled) and entry.name == PLUGIN:
            return entry
    return None


def install_dir() -> Path:
    """The plugin's install: `INSTALL`, on either route."""
    return INSTALL


def install_of(skill: Path) -> Path:
    """The install holding `skill` (`skill_dir`'s inverse): where its
    `.swarm-bundle.json` sits."""
    skill = Path(skill)
    return skill.parents[1] if skill.parts[-2:] == SKILL_SUBDIR.parts else skill


def skill_dir(install: Path) -> Path:
    """The skill folder holding SKILL.md and the validators, inside `install`."""
    nested = Path(install) / SKILL_SUBDIR
    return nested if (nested / "SKILL.md").is_file() else Path(install)


def output_dir(artifacts: Path) -> Path:
    return Path(artifacts) / OUTPUT_DIR_NAME


# --- before the agent ----------------------------------------------------------


def guidance(artifacts: Path) -> str:
    """What the agent is told, appended to its prompt: the owner's run settings."""
    out = output_dir(artifacts)
    return (
        "\n\nSECURITY REVIEW STEP. Use the security-audit skill in FULL AUDIT MODE "
        f"at profile `{PROFILE}`, as a scoped run over the diff between the pull "
        "request's base and head: the base is `git merge-base HEAD origin/HEAD` (the "
        "default branch), the head is HEAD of your checkout. Set the budget to "
        f"{SUBAGENT_BUDGET} agent invocations across all phases, and record it in "
        f"run-metadata.json. The output directory is {out}: it is outside the "
        "checkout; create it, and write nothing inside the checkout.\n"
        "NO OS-ENFORCED SANDBOX IS AVAILABLE in this step. Do not build, test, run, "
        "install or execute any of the target's code, scripts, tests or fixtures. "
        "Stay source-only, and keep every lead you cannot prove from source as "
        "needs_validation.\n"
        f"Write {FINDINGS}, {LEDGER}, {REPORT}, FINDINGS-DETAIL.md and "
        "NEEDS-VALIDATION.md there, and run the skill's two validators on them "
        f"before you finish. Do not write {REVIEW_VERDICT} or {VERDICT_FILE}, do not "
        "ask the owner about findings, and post nothing to GitHub: the worker "
        "validates the output, derives the verdict, asks the owner about each "
        "confirmed critical or high finding and posts the report.\n"
    )


# --- after the agent ------------------------------------------------------------


def _tail(text: str) -> str:
    text = text.strip()
    if len(text) <= MAX_VALIDATOR_MESSAGE_CHARS:
        return text
    return "…" + text[-(MAX_VALIDATOR_MESSAGE_CHARS - 1):]


def run_validators(skill: Path, out: Path, *, node: str | None = None,
                   run: Runner = subprocess.run) -> None:
    """Both of the skill's validators on the output, or SecurityReviewInvalid.

    `node` is found on PATH when not given. Each runs with only PATH in its
    environment (no credential reaches it), from the skill's folder, bounded.
    """
    binary = node or shutil.which("node")
    if not binary:
        raise SecurityReviewInvalid(
            "no node binary is on PATH, so the security-audit validators could not run")
    for validator, target in ((VALIDATE_FINDINGS, FINDINGS), (VALIDATE_LEDGER, LEDGER)):
        script = Path(skill) / validator
        if not script.is_file():
            raise SecurityReviewInvalid(f"the security-audit skill has no {validator} at {skill}")
        path = Path(out) / target
        if not path.is_file() or path.is_symlink():
            raise SecurityReviewInvalid(f"the security audit wrote no {target} in {out.name}/")
        try:
            done = run(
                [binary, str(script), str(path)], cwd=str(skill), capture_output=True,
                text=True, timeout=VALIDATOR_TIMEOUT_SECONDS,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, check=False,
            )
        except subprocess.TimeoutExpired:
            raise SecurityReviewInvalid(
                f"{validator} did not finish within {VALIDATOR_TIMEOUT_SECONDS}s") from None
        except OSError as exc:
            raise SecurityReviewInvalid(f"{validator} could not run ({type(exc).__name__})") from exc
        if done.returncode != 0:
            said = _tail(f"{done.stderr or ''}\n{done.stdout or ''}") or "no output"
            raise SecurityReviewInvalid(
                f"{validator} failed on {target} (exit {done.returncode}): {said}")


def read_findings(out: Path) -> list[dict[str, Any]]:
    """The validated `findings.json`: a list of finding objects."""
    try:
        data = json.loads((Path(out) / FINDINGS).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise SecurityReviewInvalid(f"{FINDINGS} could not be read ({type(exc).__name__})") from exc
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise SecurityReviewInvalid(f"{FINDINGS} is not a list of finding objects")
    return data


def severity(finding: Mapping[str, Any]) -> str | None:
    """A confirmed finding's `severity.overall_severity`; None for any other."""
    if finding.get("verdict") != "confirmed":
        return None
    block = finding.get("severity")
    value = block.get("overall_severity") if isinstance(block, Mapping) else None
    return value if value in SEVERITIES else None


def counts(findings: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """`{"confirmed": {severity: n}, "needs_validation": n, "rejected": n}`."""
    confirmed = dict.fromkeys(SEVERITIES, 0)
    other = {"needs_validation": 0, "rejected": 0}
    for finding in findings:
        status = finding.get("verdict")
        level = severity(finding)
        if level is not None:
            confirmed[level] += 1
        elif status in other:
            other[status] += 1
    return {"confirmed": confirmed, **other}


def blocking(findings: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The confirmed critical and high findings, critical first, in file order."""
    found = [f for f in findings if severity(f) in BLOCKING_SEVERITIES]
    return sorted(found, key=lambda f: BLOCKING_SEVERITIES.index(severity(f) or "high"))


def _where(finding: Mapping[str, Any]) -> tuple[str | None, int | None]:
    """The first trace step's file and line: where the finding starts."""
    trace = finding.get("trace")
    first = trace[0] if isinstance(trace, list) and trace and isinstance(trace[0], Mapping) else {}
    file = first.get("file") if isinstance(first.get("file"), str) else None
    line = first.get("line")
    return file, line if type(line) is int and line > 0 else None


def _label(finding: Mapping[str, Any]) -> str:
    title = " ".join(str(finding.get("title") or "untitled finding").split())
    file, line = _where(finding)
    at = f" ({file}:{line})" if file and line else (f" ({file})" if file else "")
    return f"[{severity(finding)}] {title}{at}"


def _clip(text: str, bound: int) -> str:
    return text if len(text) <= bound else text[: bound - 1] + "…"


def owner_question(finding: Mapping[str, Any]) -> dict[str, Any]:
    """One blocking question about one confirmed critical or high finding."""
    return {
        "question": _clip(
            f"The security review confirmed a {severity(finding)} finding: "
            f"{_label(finding)}. What should happen before this pull request merges?",
            MAX_QUESTION_CHARS),
        "options": [
            {"label": FIX_NOW, "description": (
                "Do not merge. The finding is fixed on this branch first, and the "
                "merge waits for a review of the fixed head.")},
            {"label": ACCEPT_RISK, "description": (
                "Merge with the finding open. Say why in your answer: the reason is "
                "recorded with the merge.")},
            {"label": FALSE_POSITIVE, "description": (
                "The finding is not real. Merge, and say why in your answer.")},
        ],
        "recommended": FIX_NOW,
        "context": _clip(
            " ".join(str(finding.get("description") or "").split())
            + f" Fingerprint {finding.get('fingerprint')}; see {OUTPUT_DIR_NAME}/"
            "FINDINGS-DETAIL.md in this step's artifacts.",
            MAX_CONTEXT_CHARS),
        "blocking": True,
        # The branch built none of the three: the answer is a decision about
        # the merge, never a pick a pick-fix round should build.
        "built": None,
    }


def _merge_questions(artifacts: Path, asked: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """`(the file's questions, the index of the first of ours)`, the agent's kept first.

    An agent's own `questions.json` that does not validate is replaced: the
    owner questions that hold the merge must reach the owner.
    """
    existing: list[dict[str, Any]] = []
    try:
        raw = questions_mod.read(artifacts)
        existing = questions_mod.validate(raw) if raw is not None else []
    except questions_mod.Rejected:
        existing = []
    existing = existing[: max(questions_mod.MAX_QUESTIONS - len(asked), 0)]
    # The file's byte bound: the agent's own questions give way to ours.
    while existing and len(json.dumps(existing + asked, indent=2)) + 1 > questions_mod.MAX_BYTES:
        existing.pop()
    return existing + asked, len(existing)


def _questions_for(found: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One question per blocking finding, at most `questions_mod.MAX_QUESTIONS`;
    past that the last question covers the rest together."""
    bound = questions_mod.MAX_QUESTIONS
    if len(found) <= bound:
        return [owner_question(f) for f in found]
    asked = [owner_question(f) for f in found[: bound - 1]]
    rest = found[bound - 1:]
    last = owner_question(rest[0])
    last["question"] = _clip(
        f"The security review confirmed {len(rest)} more critical or high findings: "
        + "; ".join(_label(f) for f in rest)
        + ". What should happen to all of them before this pull request merges?",
        MAX_QUESTION_CHARS)
    return [*asked, last]


def verdict_document(findings: Sequence[Mapping[str, Any]], *, task_id: str, step_id: str,
                     first_question: int = 0,
                     questions: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """The step's verdict: NOT_YET naming the blocking findings, else MERGE with the counts."""
    tally = counts(findings)
    found = blocking(findings)
    document: dict[str, Any] = {
        "verdict": "NOT_YET" if found else "MERGE",
        "findings": [],
        SUMMARY_KEY: {"profile": PROFILE, "budget": SUBAGENT_BUDGET, "counts": tally},
    }
    for finding in found:
        file, line = _where(finding)
        item: dict[str, Any] = {"summary": _label(finding), "severity": severity(finding),
                                "fingerprint": finding.get("fingerprint")}
        if file and line:
            item.update({"file": file, "line": line, "side": "new"})
        document["findings"].append(item)
    if found:
        document["needs_owner"] = [
            {"task_id": task_id, "step_id": step_id, "question_index": first_question + i,
             "question": q["question"]}
            for i, q in enumerate(questions)
        ]
    return document


def check_manifest(started: Any, skill: Path) -> None:
    """On the bundled route, the install's `.swarm-bundle.json` against the
    signed entry (`pluginload.check_bundle`), or SecurityReviewInvalid with
    its fixed code. Nothing on the code route, whose pin is the image's."""
    entry = signed_entry(started)
    if entry is None:
        return
    try:
        pluginload.check_bundle(entry, install_of(skill))
    except pluginload.PluginRefused as exc:
        raise SecurityReviewInvalid(
            f"{exc} ({exc.code}), so the security-audit validators were not run") from None


def finish(artifacts: Path, *, task_id: str, step_id: str, skill: Path,
           node: str | None = None, run: Runner = subprocess.run, started: Any = None) -> dict[str, Any]:
    """Validate the audit, write the verdict and any owner questions; the record.

    `started` is the step's started plugins (`plugins.Started`): on the
    bundled route the manifest is checked against its signed entry first
    (`check_manifest`), before any validator runs. Raises
    SecurityReviewInvalid (after removing any verdict the agent wrote
    itself, so none is uploaded) when the manifest or the output does not
    validate.
    """
    artifacts = Path(artifacts)
    for name in (REVIEW_VERDICT, VERDICT_FILE):
        (artifacts / name).unlink(missing_ok=True)
    check_manifest(started, skill)
    out = output_dir(artifacts)
    run_validators(skill, out, node=node, run=run)
    findings = read_findings(out)
    asked = _questions_for(blocking(findings))
    first = 0
    if asked:
        merged, first = _merge_questions(artifacts, asked)
        (artifacts / questions_mod.NAME).write_text(
            json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    document = verdict_document(findings, task_id=task_id, step_id=step_id,
                                first_question=first, questions=asked)
    body = json.dumps(document, indent=2) + "\n"
    for name in (REVIEW_VERDICT, VERDICT_FILE):
        (artifacts / name).write_text(body, encoding="utf-8")
    return {"verdict": document["verdict"], "counts": document[SUMMARY_KEY]["counts"],
            "blocking": len(document["findings"]), "owner_questions": len(asked)}


# --- the pull request comment -------------------------------------------------------


def comment_body(report: str, *, artifact_pointer: str, scrub: Callable[[str], str],
                 limit: int = GITHUB_COMMENT_LIMIT) -> str:
    """REPORT.md as a comment: redacted, then cut to the limit with a pointer when longer."""
    text = scrub(report).strip()
    head = f"{COMMENT_HEADING}\n\n"
    pointer = (f"\n\n---\n*The report is longer than a GitHub comment allows; the full "
               f"report is {artifact_pointer}.*")
    whole = head + text
    if len(whole) <= limit:
        return whole
    room = max(limit - COMMENT_MARGIN - len(head) - len(pointer), 0)
    cut = text[:room]
    newline = cut.rfind("\n")
    if newline > room // 2:
        cut = cut[:newline]
    return head + cut.rstrip() + "\n\n…" + pointer


# --- the merge's reading -------------------------------------------------------------


def disposition(answer: Mapping[str, Any] | None) -> str | None:
    """`"merge"` for an answer that accepts the risk or calls it a false positive,
    `"fix"` for any other answer, None for no answer yet."""
    if not isinstance(answer, Mapping):
        return None
    choice = answer.get("choice")
    return "merge" if choice in MERGE_ANSWERS else "fix"
