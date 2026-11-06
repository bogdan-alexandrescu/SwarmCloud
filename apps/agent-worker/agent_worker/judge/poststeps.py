"""The planner's and the reviewer's Jev post-steps: J2, J5 and J3 (lane CTXQ-JW).

They run in the runner (`runners/cliagent.run_cli_agent`), after the agent's
last turn and before its result is written, over what the agent wrote into
`$SWARM_ARTIFACTS_DIR` -- and only when the worker set Jev up for the attempt
(`judge.attempt`: the workspace opted in to Jev for code).

* PLANNER (`plan_post_step`), over `plan.json`:
  * J2, plan coverage: one `choice` per requirement over the plan's steps and
    `none`. A requirement whose best step is under p 0.5, or `none`, is flagged,
    with `coverage` `none` (no step) or `weak` (a step, under p 0.5).
  * J5, duplicate: one `choice` over the in-flight lanes the stager wrote
    (`inflight.json`, titles and files only, §5.4) and `none`. p >= 0.85 on a
    lane asks for the auto-approved plan to be HELD FOR A HUMAN (owner
    decision §8 Q5 A) -- swarm-api's planner post-step
    (`routes/runs.py::_from_planner`) reads `PLAN_ADVISORY_FILE` and does it.
    Jev never decides; it can only make a person look.
  Written to `PLAN_ADVISORY_FILE` beside `plan.json`; `plan.json` itself is
  never touched.
* REVIEWER (`review_post_step`), over every verdict file the step owes in its
  artifacts: each declared output named `*verdict.json` and each
  `*verdict.json` it wrote (`verdict.json`, and the experiment reviews'
  `code-verdict.json` and `ui-verdict.json`; owner, 2 November 2026) -- a
  review's, or a re-review's own (never the earlier verdict a re-review
  stages: `contextpack.tasktype.classify` types a step that owes its own
  verdict a review, and a fix that only stages one a fix):
  * J3, finding triage: one `noul` per finding, "is this a real defect in
    this diff, as stated?", with the code around the finding's line and the
    patch's hunk for its file. A finding is an item of `findings`,
    `blockers`, `majors` or `minors`, a string or an object. The answers go
    onto the findings as `p_real` (`verdict.annotate_findings`); under 0.15 a
    finding is marked "verify before acting" and still reaches the fixer.
    `verdict` and every other key are never touched. The step's summary
    says how many findings each file had and how many were scored; a file
    that cannot be read is left byte for byte and named there.

* EVERY STEP WITH A SIGNED WORK CONTRACT (`contract_post_step`), JC2: each
  requirement naming `judgment`, judged at the commit against its diff; the
  verdict is posted by `deliver_contract_verdict`, and unlike the others it
  runs, as `not_run`, without Jev too. See its section below.

ALL OR NOTHING, PER QUESTION SET. A J whose call is skipped (breaker, budget),
fails, times out or answers malformed contributes nothing: with every call
failing no file is written or rewritten, so the outputs are byte for byte the
local-only run's. Nothing here raises.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from agent_worker.judge import shapes
from agent_worker.judge.sanitize import is_denied_path, is_flagged

PLAN_FILE = "plan.json"
VERDICT_FILE = "verdict.json"
#: What the planner's post-step writes, and swarm-api's `_from_planner` reads
#: (`swarm_api.routes.runs.PLAN_ADVISORY_FILE` restates it).
PLAN_ADVISORY_FILE = "plan-advisory.json"
PLAN_ADVISORY_SCHEMA = "swarm.plan-advisory/v1"
#: The most of a plan or verdict read: `issueruns.MAX_PLAN_BYTES` and
#: `verdict.MAX_VERDICT_BYTES` are both 256 KiB or less.
MAX_READ_BYTES = 256 * 1024
#: J3 asks about at most this many findings: each is one call of the task's
#: eight (§5.3 estimates "mostly 0"), and the gate's J4 needs one too.
J3_MAX_FINDINGS = 5
#: §5.2: the code around a finding, ±40 lines.
WINDOW_LINES = 40
MAX_REQUIREMENTS = 64
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _read_json(path: Path) -> Any:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_READ_BYTES:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _write_atomically(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.jev.tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)  # a link planted at the name is replaced, never followed


def _ask(judge: Any, kind: str, state: Any, questions: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Every question's answer, or None."""
    try:
        decision = judge.decide(kind, state, questions)
    except Exception:  # noqa: BLE001 -- a refused question is a local-only output
        return None
    if not decision.ok or not isinstance(decision.answers, dict):
        return None
    if any(name not in decision.answers for name in questions):
        return None
    return decision.answers


# -- the planner: J2 and J5 -----------------------------------------------------


def _in_flight(context_dir: Path | None) -> list[dict[str, Any]]:
    """The stager's layer I rows: titles, refs and files, nothing else (§5.4)."""
    if context_dir is None:
        return []
    staged = _read_json(Path(context_dir) / "inflight.json")
    rows = staged.get("rows") if isinstance(staged, dict) else None
    out = []
    for row in rows if isinstance(rows, list) else ():
        if isinstance(row, dict) and isinstance(row.get("title"), str):
            out.append({"title": row["title"], "ref": str(row.get("ref") or ""),
                        "files": [f for f in row.get("files") or [] if isinstance(f, str)]})
    return out[:shapes.MAX_IN_FLIGHT]


def plan_post_step(judge: Any, artifacts_dir: Path, context_dir: Path | None) -> dict | None:
    """J2 and J5 over the planner's plan.json; the advisory written, or None."""
    if judge is None:
        return None
    plan = _read_json(Path(artifacts_dir) / PLAN_FILE)
    if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list):
        return None  # a NOT_READY verdict, or no plan: nothing to judge
    steps = [s for s in plan["steps"] if isinstance(s, dict) and s.get("step_id")]
    requirements = [r for r in plan.get("requirements") or [] if isinstance(r, str)]
    advisory: dict[str, Any] = {"schema": PLAN_ADVISORY_SCHEMA, "advisory": True}
    asked = requirements[:MAX_REQUIREMENTS]
    if asked and steps:
        answers = _ask(judge, "J2", *shapes.j2_plan_coverage(asked, steps))
        if answers is not None:
            flagged = shapes.read_plan_coverage(answers)
            advisory["coverage"] = {
                "requirements": len(asked),
                "flagged": [{"index": k, "requirement": asked[k], "coverage": how}
                            for k, how in flagged if k < len(asked)],
            }
    lanes = _in_flight(context_dir)
    if lanes and steps:
        title = str(plan.get("summary") or "").split("\n", 1)[0]
        body = "\n".join([str(plan.get("summary") or ""), *requirements])
        answers = _ask(judge, "J5", *shapes.j5_duplicate(title, body, lanes))
        if answers is not None:
            lane = shapes.read_duplicate(answers)
            if lane is not None and lane < len(lanes):
                _choice, p = shapes._choice(answers, "dup")
                advisory["duplicate"] = {"title": lanes[lane]["title"], "ref": lanes[lane]["ref"],
                                         "p": round(p, 4)}
            else:
                advisory["duplicate"] = None
    if "coverage" not in advisory and "duplicate" not in advisory:
        return None
    advisory["hold_for_human"] = bool(advisory.get("duplicate"))
    _write_atomically(Path(artifacts_dir) / PLAN_ADVISORY_FILE,
                      json.dumps(advisory, indent=2, sort_keys=True) + "\n")
    return advisory


# -- the reviewer: J3 -------------------------------------------------------------


def _inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def code_window(checkout: Path, file: str, line: int | None) -> dict[str, Any]:
    """`{path, window}` around `line`, or `{path}` alone: for a denied path
    (§5.4 names only), a file outside the checkout, a link, or no line."""
    out: dict[str, Any] = {"path": file}
    target = Path(checkout) / file
    if is_denied_path(file) or line is None or target.is_symlink() or not target.is_file() \
            or not _inside(target, Path(checkout)):
        return out
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    first = max(line - 1 - WINDOW_LINES, 0)
    out["window"] = "\n".join(lines[first:line + WINDOW_LINES])
    return out


def patch_hunks(patch: str) -> dict[str, list[tuple[int, int, str]]]:
    """Per new-side path, each hunk's new-side range and its text."""
    out: dict[str, list[tuple[int, int, str]]] = {}
    current: str | None = None
    lines: list[str] = []
    start = count = 0

    def close() -> None:
        if current is not None and lines:
            out.setdefault(current, []).append((start, count, "\n".join(lines)))

    for line in patch.splitlines():
        if line.startswith("diff --git "):
            close()
            current, lines = None, []
        elif line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else None
        elif current is not None and (match := _HUNK.match(line)):
            close()
            start, count = int(match.group(1)), int(match.group(2) or "1")
            lines = [line]
        elif lines and line[:1] in ("+", "-", " "):
            lines.append(line)
    close()
    return out


def _hunk_for(hunks: Mapping[str, Sequence[tuple[int, int, str]]], file: str,
              line: int | None) -> str:
    rows = list(hunks.get(file) or ())
    if not rows:
        return ""
    if line is not None:
        for start, count, text in rows:
            if start <= line <= start + max(count, 1) - 1:
                return text[:shapes.HUNK_CHARS]
    return rows[0][2][:shapes.HUNK_CHARS]


def _staged_patch(work_dir: Path, payload: Mapping[str, Any]) -> str:
    for entry in payload.get("staged_inputs") or ():
        raw = entry.get("path") if isinstance(entry, dict) else entry
        if isinstance(raw, str) and raw.endswith(".patch"):
            target = Path(work_dir) / raw
            if _inside(target, Path(work_dir)) and not target.is_symlink() and target.is_file():
                try:
                    return target.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return ""
    return ""


def _line(value: Any) -> int | None:
    return value if type(value) is int and value >= 1 else None


#: The lists a verdict file carries its findings in, in the order J3 asks
#: about them: `verdict.json`'s `findings` (#2837), and the experiment reviews'
#: `blockers`, `majors` and `minors` (`code-verdict.json`, `ui-verdict.json`).
#: Under the J3_MAX_FINDINGS cap a blocker is asked about before a minor.
FINDING_LISTS = ("blockers", "findings", "majors", "minors")
_LIST_RANK = {"blockers": 0, "findings": 1, "majors": 1, "minors": 2}
#: What a finding object says its defect is, in the order read: the `summary`
#: shape, then the review briefs' `problem` shape (`verdict._MINOR_TEXT_FIELDS`).
_FINDING_TEXT = ("summary", "title", "message", "problem", "what")
#: The suffix that marks an artifact as a verdict file.
VERDICT_SUFFIX = "verdict.json"


def _verdict_files(artifacts_dir: Path, payload: Mapping[str, Any]) -> list[str]:
    """The verdict files this review step owes, relative to `artifacts_dir`:
    every declared output (input.json's `expected_outputs`) whose name ends in
    `verdict.json`, and every `*verdict.json` the step wrote at the top of
    the directory, `verdict.json` among them. Sorted, each once."""
    from agent_worker import expected_outputs as expected_mod

    names = {n for n in expected_mod.parse_names(payload.get(expected_mod.METADATA_KEY)).names
             if n.rsplit("/", 1)[-1].endswith(VERDICT_SUFFIX)}
    try:
        names.update(p.name for p in Path(artifacts_dir).iterdir()
                     if p.name.endswith(VERDICT_SUFFIX) and not p.name.startswith("."))
    except OSError:
        pass
    return sorted(names)


def _read_verdict_file(artifacts_dir: Path, name: str) -> tuple[dict | None, str]:
    """The verdict file's object, or None and why it cannot be read."""
    root = Path(artifacts_dir)
    path = root / name
    try:
        # A link anywhere under the directory is refused, never followed: the
        # agent wrote these files and may have planted one.
        parts = Path(name).parts
        if any(root.joinpath(*parts[:n]).is_symlink() for n in range(1, len(parts) + 1)):
            return None, "a link"
        if not _inside(path, Path(artifacts_dir)):
            return None, "outside the artifacts directory"
        if not path.exists():
            return None, "not written"
        if not path.is_file():
            return None, "not a file"
        if path.stat().st_size > MAX_READ_BYTES:
            return None, f"over {MAX_READ_BYTES} bytes"
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None, "not JSON"
    except OSError as exc:
        return None, f"unreadable ({type(exc).__name__})"
    try:
        document = json.loads(text)
    except ValueError:
        return None, "not JSON"
    if not isinstance(document, dict):
        return None, "not a JSON object"
    return document, ""


def _finding_of(item: Any) -> dict[str, Any] | None:
    """What J3 is asked about for one item -- `{summary, file?, line?}` -- or
    None for an item that says no defect."""
    if isinstance(item, str):
        item = {"summary": item}
    if not isinstance(item, dict):
        return None
    text = next((item[k] for k in _FINDING_TEXT
                 if isinstance(item.get(k), str) and item[k].strip()), None)
    if text is None:
        return None
    file = item.get("file") if isinstance(item.get("file"), str) else ""
    line = _line(item.get("line"))
    return {k: v for k, v in (("summary", text), ("file", file), ("line", line)) if v}


def _minor(item: Any) -> bool:
    from agent_worker import verdict as verdict_mod

    severity = item.get("severity") if isinstance(item, dict) else None
    return isinstance(severity, str) and severity.strip().lower() == verdict_mod.SEVERITY_MINOR


def _annotate(document: dict[str, Any], scores: Mapping[tuple[str, int], float]) -> dict[str, Any]:
    """`document` with `p_real` (and the verify mark) on each scored item, as
    `verdict.annotate_findings` writes it on `findings`: a string item becomes
    `{"summary": <it>, ...}`, an object keeps every key. Nothing is dropped
    or reordered, and no other key -- `verdict` above all -- is touched."""
    from agent_worker import verdict as verdict_mod

    out = dict(document)
    for key in FINDING_LISTS:
        p_real = {index: p for (k, index), p in scores.items() if k == key}
        if p_real:
            out[key] = verdict_mod.annotate_findings({"findings": document[key]},
                                                     p_real)["findings"]
    return out


def review_post_step(judge: Any, artifacts_dir: Path, checkout: Path | None, work_dir: Path,
                     payload: Mapping[str, Any]) -> dict | None:
    """J3 over every verdict file the review step owes (`_verdict_files`), in
    either shape: a `findings` list, or `blockers`/`majors`/`minors` lists,
    each item a string or an object.

    Returns the step's summary, `{asked, files: {name: {findings, scored,
    p_real}}, unreadable: {name: why}}`; None when there is nothing to report,
    and None -- every file untouched -- when any J3 call fails. A file that
    cannot be read is never written, and is named under `unreadable`."""
    if judge is None:
        return None
    artifacts_dir = Path(artifacts_dir)
    documents: dict[str, dict[str, Any]] = {}
    unreadable: dict[str, str] = {}
    for name in _verdict_files(artifacts_dir, payload):
        document, why = _read_verdict_file(artifacts_dir, name)
        if document is None:
            unreadable[name] = why
        else:
            documents[name] = document
    if not documents and not unreadable:
        return None
    candidates: list[tuple[int, int, str, str, int, dict[str, Any]]] = []
    counts: dict[str, int] = {}
    for order, (name, document) in enumerate(sorted(documents.items())):
        counts[name] = 0
        for key in FINDING_LISTS:
            items = document.get(key)
            for index, item in enumerate(items if isinstance(items, list) else ()):
                finding = _finding_of(item)
                if finding is None:
                    continue
                counts[name] += 1
                rank = 2 if _minor(item) else _LIST_RANK[key]
                candidates.append((rank, order, name, key, index, finding))
    candidates.sort(key=lambda row: (row[0], row[1], FINDING_LISTS.index(row[3]), row[4]))
    hunks = patch_hunks(_staged_patch(work_dir, payload)) if candidates else {}
    stops = tuple(getattr(judge, "hard_stop_paths", ()) or ())
    scores: dict[str, dict[tuple[str, int], float]] = {name: {} for name in documents}
    asked = 0
    for _rank, _order, name, key, index, finding in candidates[:J3_MAX_FINDINGS]:
        file, line = finding.get("file", ""), finding.get("line")
        code = code_window(checkout, file, line) if file and checkout is not None else {}
        # §5.4: names only for a denied path. `diff_hunk` is a top-level
        # string the client's sanitizer cannot tie to a path, so withhold it here.
        hunk = "" if file and is_denied_path(file, stops) else _hunk_for(hunks, file, line)
        state, questions = shapes.j3_finding(finding, code, hunk)
        asked += 1
        answers = _ask(judge, "J3", state, questions)
        p = shapes.read_finding(answers) if answers is not None else None
        if p is None:
            return None  # all or nothing: no partial triage reaches the fixer
        scores[name][(key, index)] = min(max(p, 0.0), 1.0)
    if not asked and not unreadable:
        return None
    files: dict[str, Any] = {}
    for name, document in sorted(documents.items()):
        scored = scores[name]
        files[name] = {"findings": counts[name], "scored": len(scored),
                       "p_real": {f"{k}[{i}]": round(v, 4) for (k, i), v in sorted(
                           scored.items(), key=lambda kv: (FINDING_LISTS.index(kv[0][0]),
                                                           kv[0][1]))}}
    # Every answer is in: only now is any file rewritten.
    for name, document in documents.items():
        if scores[name]:
            _write_atomically(artifacts_dir / name,
                              json.dumps(_annotate(document, scores[name]), indent=2) + "\n")
    return {"asked": asked, "files": files, "unreadable": unreadable}


# -- JC2: the end-of-step contract judgment -------------------------------------
#
# docs/design/agent-quality/jev-contract.md sections 5.1-5.3, 7 and 9, lane 6;
# the owner's answers 4 and 8 (3 November 2026). After the agent's process
# tree has exited, ONE bounded round of calls judges each requirement of the
# step's signed contract that names `judgment`, against the commit's diff and
# the mechanical results, and the verdict is posted to swarm-api's
# `state/contract-verdict`, which runs every check of section 9 and writes the
# evidence and the transition in one fenced transaction. The worker writes
# nothing trusted. The calls spend the reserve on top of the task's budget
# (`budget.RESERVED_CALLS`, the owner's answer 8), which J0-J5 and
# `jev_decide` cannot. Any failure -- no client, a spent budget, an open
# breaker, a timeout, a malformed answer, too little time -- is posted as
# `not_run` with its reason, never as a pass, and the control plane re-asks
# from the published diff (the owner's answer 5).

#: Where the worker names the step's signed contract for the runner: the
#: stored body's path (`metadata.work_contract`, as `plugins.selected` reads
#: it) and the digest of the SIGNED dispatch reference. The digest is the
#: authority: a body that does not digest to it is not judged.
CONTRACT_FILE_ENV = "SWARM_WORK_CONTRACT_FILE"
CONTRACT_DIGEST_ENV = "SWARM_WORK_CONTRACT_DIGEST"
#: The most of a contract body read.
MAX_CONTRACT_BYTES = 256 * 1024
#: The route's state op.
VERDICT_OP = "contract-verdict"
OUTCOME_JUDGED = "judged"
OUTCOME_NOT_RUN = "not_run"
#: JC2 is not started with less than this left before the worker's kill: one
#: call's worst case (three tries of `client.CALL_TIMEOUT_SECONDS`, 10 s, and
#: their backoff, about 32 s) plus the two git reads' bound. Invariant 4: the
#: worker never waits for Jev past its deadline; it posts `not_run` instead.
JC2_MIN_SECONDS = 60
#: The arm of the Jev work-contract experiment (jev-contract.md section 11):
#: the signed dispatch block's `contract_jev` switch, handed to the runner by
#: the worker beside the contract (`lifecycle.Worker._stage_work_contract`).
#: `off` asks Jev nothing and posts `not_run` with reason `SKIPPED_ARM`, the
#: word `swarm_api.contractreview.SKIPPED_ARM` and `scripts/benchstat.py`
#: read; `on`, or no switch, judges exactly as before (it never turns JC2 on
#: where the workspace left Jev off).
CONTRACT_JEV_ENV = "SWARM_CONTRACT_JEV"
CONTRACT_JEV_SWITCH = "contract_jev"
ARM_OFF = "off"
SKIPPED_ARM = "skipped:arm"
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_REASON_CODE = re.compile(r"[^A-Za-z0-9_.:-]")


def _reason(value: Any) -> str:
    """A short reason code the route accepts: `[A-Za-z0-9_.:-]`, 64 at most."""
    text = _REASON_CODE.sub("_", str(value or "failed"))[:64]
    return text or "failed"


def _not_run(digest: str, commit: str | None, base: str | None, reason: str,
             *, changed: Sequence[str] = (), mechanical: Sequence[Mapping[str, Any]] = (),
             call_id: str | None = None) -> dict[str, Any]:
    return {"digest": digest, "commit": commit, "base": base, "outcome": OUTCOME_NOT_RUN,
            "reason": _reason(reason), "call_id": call_id, "state_sha256": None,
            "changed": list(changed), "mechanical": [dict(m) for m in mechanical],
            "verdicts": []}


def read_contract(env: Mapping[str, str]) -> tuple[dict[str, Any] | None, str | None]:
    """`(body, digest)` the worker named for this step, or `(None, None)` for
    a step with no contract. The body is None when its file cannot be read;
    the digest is None when it is not 64 hex."""
    path, digest = env.get(CONTRACT_FILE_ENV, "").strip(), env.get(CONTRACT_DIGEST_ENV, "").strip()
    if not path and not digest:
        return None, None
    document = _read_json(Path(path)) if path else None
    if isinstance(document, dict) and len(json.dumps(document)) > MAX_CONTRACT_BYTES:
        document = None
    return (document if isinstance(document, dict) else None,
            digest if _HEX64.fullmatch(digest) else None)


def contract_arm(env: Mapping[str, str]) -> str | None:
    """The step's `contract_jev` word the worker handed over, `on` or `off`;
    None when it set none (or any other word)."""
    value = (env.get(CONTRACT_JEV_ENV) or "").strip()
    return value if value in ("on", ARM_OFF) else None


def _verified(document: Mapping[str, Any] | None, digest: str) -> bool:
    """Whether the body is the revision the signed reference names."""
    if not isinstance(document, Mapping):
        return False
    from swarm_common.specsign import SpecNotCanonical
    from swarm_common.workcontract import contract_digest

    try:
        return contract_digest(document) == digest
    except (SpecNotCanonical, TypeError, ValueError):
        return False


def _mechanical(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One entry per proof kind the judged requirements name beside
    `judgment`, each `passed: None`: at the end of the step no mechanical
    check has run at this commit yet, and missing is not a pass, nor a fail."""
    kinds: list[str] = []
    for row in document.get("requirements") or ():
        verification = row.get("verification") if isinstance(row, Mapping) else None
        if isinstance(verification, list) and shapes.JUDGMENT in verification:
            for kind in verification:
                if kind in shapes.VERIFICATION_KINDS and kind != shapes.JUDGMENT \
                        and kind not in kinds:
                    kinds.append(kind)
    return [{"kind": kind, "passed": None}
            for kind in sorted(kinds, key=shapes.VERIFICATION_KINDS.index)]


def _artifacts(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The contract's artifacts, `present: None`: the publish has not run."""
    rows = document.get("artifacts")
    return [{"kind": a.get("kind"), "present": None}
            for a in (rows if isinstance(rows, list) else ())
            if isinstance(a, Mapping) and a.get("kind") in shapes.ARTIFACT_KINDS]


def _reserved(judge: Any) -> Any:
    """`judge` spending the reserve (`budget.Ledger.for_reserve`): JC2's
    calls are the only ones that may."""
    ledger = getattr(judge, "ledger", None)
    if ledger is None or not hasattr(ledger, "for_reserve"):
        return judge
    try:
        import dataclasses

        return dataclasses.replace(judge, ledger=ledger.for_reserve())
    except TypeError:
        return judge


def contract_post_step(judge: Any, document: Mapping[str, Any] | None, digest: str | None,
                       checkout: Path | None, *, base: str | None,
                       deadline_left: float | None,
                       environ: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """JC2 over the step's signed contract: the body `state/contract-verdict`
    takes (without its tuple and `request_id`), or None when there is nothing
    to judge or nothing the route could take (no contract, a digest that is
    not 64 hex, a contract with no requirement naming `judgment`).

    `document` and `digest` are the worker's (`read_contract`), never the
    agent's. The state holds the requirements' text, the commit's diff and
    the mechanical results ONLY: the agent's summary, its PR body, its claims
    and its transcript are not arguments here, so they never reach Jev.

    `environ` (the process's own when None) carries the experiment's arm
    (`CONTRACT_JEV_ENV`): `off` is `not_run`, `skipped:arm`, and no call.
    Never raises."""
    if digest is None or not _HEX64.fullmatch(digest):
        return None
    if document is not None and not shapes.judged_requirements(document):
        return None
    if contract_arm(os.environ if environ is None else environ) == ARM_OFF:
        commit = shapes.head_commit(checkout) if checkout is not None else None
        return _not_run(digest, commit, base if isinstance(base, str)
                        and _HEX40.fullmatch(base) else None, SKIPPED_ARM)
    if document is None:
        # A body the worker named and this process cannot read: it may name
        # `judgment`, so say so rather than post nothing.
        return _not_run(digest, None, base, "contract_unreadable")
    base = base if isinstance(base, str) and _HEX40.fullmatch(base) else None
    try:
        return _judge_contract(judge, document, digest, checkout, base=base,
                               deadline_left=deadline_left)
    except Exception as exc:  # noqa: BLE001 -- a judgment that failed did not run
        return _not_run(digest, None, base, f"error_{type(exc).__name__}")


def _judge_contract(judge: Any, document: Mapping[str, Any], digest: str,
                    checkout: Path | None, *, base: str | None,
                    deadline_left: float | None) -> dict[str, Any]:
    rows = shapes.judged_requirements(document)
    mechanical = _mechanical(document)
    commit = shapes.head_commit(checkout) if checkout is not None else None
    if not _verified(document, digest):
        return _not_run(digest, commit, base, "contract_unverified")
    if judge is None:
        return _not_run(digest, commit, base, "not_opted_in")
    if checkout is None or commit is None:
        return _not_run(digest, commit, base, "no_commit")
    if base is None:
        return _not_run(digest, commit, base, "no_base")
    if deadline_left is not None and deadline_left < JC2_MIN_SECONDS:
        return _not_run(digest, commit, base, "deadline")
    changed = shapes.changed_from_checkout(checkout, base, commit)
    if changed is None:
        return _not_run(digest, commit, base, "no_diff", mechanical=mechanical)
    reserved = _reserved(judge)
    sent: list[str] | None = None
    sha: str | None = None
    call_id: str | None = None
    verdicts: list[dict[str, Any]] = []
    cited: dict[int, str] = {}
    failure: str | None = None
    for start in range(0, len(rows), shapes.JC2_PER_CALL):
        batch = rows[start:start + shapes.JC2_PER_CALL]
        state = shapes.jc2_state(batch, commit=commit, changed=changed, mechanical=mechanical,
                                 artifacts=_artifacts(document))
        paths = [row["path"] for row in state["changed"]]
        if sent is not None and paths != sent:
            # Another batch's state cut other files: its items would not be
            # the posted ones, so its requirements stay unjudged (cannot_judge).
            continue
        questions = shapes.jc2_questions(state)
        try:
            decision = reserved.decide(shapes.JC2_KIND, state, questions)
        except Exception as exc:  # noqa: BLE001 -- a refused question is not a verdict
            failure = failure or f"error_{type(exc).__name__}"
            continue
        if not getattr(decision, "ok", False) or not isinstance(decision.answers, dict):
            failure = failure or (decision.reason or decision.outcome)
            continue
        if sent is None:
            sent, sha = paths, shapes.jc2_state_sha256(state)
            call_id = decision.call_id or None
        read = shapes.read_jc2(batch, state, decision.answers)
        _cite(cited, read, state)
        verdicts.extend(read)
    if not verdicts:
        return _not_run(digest, commit, base, failure or "failed",
                        mechanical=mechanical, call_id=call_id)
    return {"digest": digest, "commit": commit, "base": base, "outcome": OUTCOME_JUDGED,
            "reason": None, "call_id": call_id, "state_sha256": sha,
            "changed": list(sent or ()),
            "hunks": [cited.get(n) for n in range(len(sent or ()))],
            "mechanical": mechanical, "verdicts": verdicts}


def _cite(cited: dict[int, str], verdicts: Sequence[Mapping[str, Any]],
          state: Mapping[str, Any]) -> None:
    """Record in `cited` (item -> text) the hunk each verdict's `i<n>` rests
    on, as the batch's own state sent it to Jev, for the route's `hunks`
    (`ContractVerdictBody.hunks`; swarm-api keeps the cited ones,
    `contractreask.cited_hunks`, and the console prints them under each
    verdict). Nothing here is sent to Jev.

    The rules are `cited_hunks`'s, so the worker posts no more than the record
    keeps: only a cited item, the first verdict's text for it, cut to
    `shapes.HUNK_CHARS` (the state's own cut); none for an item the state sent
    by path alone (no change text) or the content of a denied path, and a hunk
    holding anything credential-shaped is withheld whole (`sanitize`, the
    shared package swarm-api checks with), because Jev never read it. The body
    therefore carries at most one hunk per judged requirement."""
    rows = state.get("changed") or ()
    for verdict in verdicts:
        item = verdict.get("evidence_item") or ""
        if not item.startswith("i") or not item[1:].isdigit():
            continue
        n = int(item[1:])
        if n in cited or n >= len(rows):
            continue
        path, text = rows[n].get("path"), rows[n].get("hunk")
        if not isinstance(text, str) or not text or not isinstance(path, str) \
                or is_denied_path(path):
            continue
        text = text[:shapes.HUNK_CHARS]
        if not is_flagged(text):
            cited[n] = text


def deliver_contract_verdict(post: Any, verdict: Mapping[str, Any] | None, *,
                             published_head: str | None, request_id: str) -> str:
    """Post one JC2 verdict to `state/contract-verdict`, once: what happened.

    `post(op, fields)` is the attempt's proven state call
    (`control.RemoteControlPlane._post`). A verdict about a commit other than
    the one this attempt published (the publish committed or rebased after
    the judgment) is posted as `not_run`, `commit_moved`, about the published
    head, so the control plane re-asks from the published diff: a verdict on
    another commit counts for nothing (section 9, check 4).

    A STALE GENERATION (`FencedError`, 409 `attempt_fenced`) is re-raised and
    nothing further is posted: the caller exits without running anything
    else and without touching the lease (invariant 5). Every other refusal
    or outage is returned as `refused:<why>`; it never fails the step."""
    from agent_worker.errors import FencedError

    if verdict is None:
        return "nothing_to_post"
    if not isinstance(published_head, str) or not _HEX40.fullmatch(published_head):
        return "not_published"
    body = dict(verdict)
    if body.get("commit") != published_head:
        reason = body.get("reason") if body.get("outcome") == OUTCOME_NOT_RUN else "commit_moved"
        body = _not_run(str(body.get("digest")), published_head, body.get("base"),
                        reason or "commit_moved", mechanical=body.get("mechanical") or ())
    body["request_id"] = request_id
    try:
        post(VERDICT_OP, body)
    except FencedError:
        raise
    except Exception as exc:  # noqa: BLE001 -- a verdict that did not land is re-asked
        code = getattr(exc, "code", None)
        return f"refused:{_reason(code or type(exc).__name__)}"
    return "posted"
