"""Jev's two questions about a work contract, as states, questions and readers.

The owner's agent quality program (the owner, 3 November 2026); the design is
docs/design/agent-quality/jev-contract.md, and the owner's answers to its
twelve choices are its section 14. Two calls:

* **JC1, the submission review** (section 4): per requirement, can the proof
  kinds it names decide it (`mechanical`), could a reader of the diff and the
  test results at the step's end decide it (`judgeable`), or does nothing
  observable decide it (`unenforceable`)? `review_state`, `review_questions`,
  `read_review`; `review_outcome` applies the acting threshold and
  `review_amendment` makes the only change Jev may cause, revision 2 by the
  `reviewer` adding `judgment` (the owner's answer 3).
* **JC2, the acceptance judgment** (section 5): per requirement that names
  `judgment`, is it `met`, `unmet`, or `cannot_judge` at the step's commit,
  and which one item of the state does the verdict rest on?
  `judgment_state`, `judgment_questions`, `read_judgment`;
  `judgment_outcome` and `judgment_evidence` say what the platform does.

Pure: no network, no Firestore, no clock. The states are the wire shape the
Jev client speaks (`agent_worker/judge/client.py`: `choice` questions, an
answer's `choice` and `probabilities`); the caller sends them through the
shared sanitizer and the client. The platform acts on closed values only:
nothing here parses, matches or acts on Jev's free text, and a missing or
malformed answer reads as `not_judged` (JC1) or `cannot_judge` (JC2), never
as a pass. A state never carries the tenant, the owner, the contract id, the
repository URL or the agent's own summary, PR body or transcript: the
functions take no such argument, and every entry is rebuilt from a closed set
of keys.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from . import workcontract as wc


#: JC1 acts on a class only at p >= 0.6, the floor of J0's classification
#: (`agent_worker/judge/shapes.CLASSIFY_MIN_CONFIDENCE`). A starting value the
#: experiment calibrates (section 11), not an owner's decision.
REVIEW_ACT_P = 0.6
#: JC2 counts a `met` and sends back on an `unmet` only at p >= 0.7; between,
#: the verdict is `cannot_judge`. Starting values, calibrated likewise.
JUDGMENT_MET_P = 0.7
JUDGMENT_UNMET_P = 0.7

#: The client's limits (`agent_worker/judge/client.py`), restated because this
#: module builds what that client sends: 64 questions per call, 255 options
#: per choice, a state of at most 70,000 characters.
MAX_QUESTIONS = 64
MAX_CHOICE_OPTIONS = 255
MAX_STATE_CHARS = 70_000
#: JC1 asks two questions per requirement, JC2 three.
REVIEW_PER_CALL = MAX_QUESTIONS // 2
JUDGMENT_PER_CALL = MAX_QUESTIONS // 3
#: One diff hunk, as J3 cuts it (`shapes.HUNK_CHARS`).
HUNK_CHARS = 1_200
#: JC2's `e<k>` choice lists every changed item and mechanical entry plus
#: `none`, under the API's option cap.
MAX_MECHANICAL = len(wc.VERIFICATION_KINDS)
MAX_CHANGED = MAX_CHOICE_OPTIONS - 1 - MAX_MECHANICAL
_PATH_CHARS = 512
_NAMED = 32

#: The two-send-backs-per-task cap (the owner's answer 11): past it, or when a
#: send-back would spend the agent's last attempt, the step goes to the owner.
MAX_SEND_BACKS = 2

# --- JC1 ---------------------------------------------------------------------

MECHANICAL = "mechanical"
JUDGEABLE = "judgeable"
UNENFORCEABLE = "unenforceable"
#: Not a Jev answer: what a missing or malformed answer reads as.
NOT_JUDGED = "not_judged"
REVIEW_CLASSES = {
    MECHANICAL: "the verification kinds it names can prove it as written",
    JUDGEABLE: "a reader of the diff and the test results at the step's end could decide "
               "it, and the named kinds cannot",
    UNENFORCEABLE: "nothing observable when the step ends decides it",
}
REVIEW_REASONS = (
    "vague_outcome", "unobservable_at_step_end", "outside_the_repository",
    "depends_on_a_later_event", "subjective", "proof_kind_mismatch",
    "several_requirements_in_one", "none",
)
VERIFICATION_MEANINGS = {
    "unit_tests": "the step's named unit tests ran at the commit and passed",
    "ci": "the repository's CI conclusion at the commit is success",
    "review": "a review step's verdict at the commit is MERGE",
    "browser_check": "a browser check ran against the deployed change and passed",
    "artifact_present": "the named artifact exists at the step's end",
    wc.VERIFICATION_JUDGMENT: "a judge reading the diff and the mechanical results decides it",
}

#: `jev_review.outcome` values (section 3).
REVIEW_OK = "ok"
REVIEW_AMENDED = "amended"
REVIEW_REFUSED = "refused"
REVIEW_NOT_RUN = "not_run"

# --- JC2 ---------------------------------------------------------------------

MET = "met"
UNMET = "unmet"
CANNOT_JUDGE = "cannot_judge"
VERDICTS = {
    MET: "the diff and the mechanical results show the requirement done",
    UNMET: "they show it not done, or done wrongly",
    CANNOT_JUDGE: "the state does not hold what deciding it needs",
}
JUDGMENT_REASONS = (
    "not_in_diff", "partially_done", "contradicted_by_tests", "wrong_place",
    "scope_exceeded", "evidence_missing", "satisfied",
)
NO_ITEM = "none"
_STATUSES = ("added", "modified", "deleted", "renamed")

#: What the platform does with one step's verdicts (section 5.4).
JUDGED_MET = "met"
SENT_BACK = "sent_back"
JUDGMENT_PENDING = "judgment_pending"


def _p(answer: Mapping[str, Any]) -> float | None:
    """The probability of the answer's own choice, or None when it has none."""
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, Mapping):
        return None
    p = probabilities.get(answer.get("choice"))
    if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) \
            or not 0.0 <= p <= 1.0:
        return None
    return float(p)


def _choice(answers: Any, name: str, options: Iterable[str]) -> tuple[str, float] | None:
    """`(choice, p)` when the answer is one of `options` with a probability."""
    if not isinstance(answers, Mapping):
        return None
    answer = answers.get(name)
    if not isinstance(answer, Mapping):
        return None
    choice = answer.get("choice")
    if not isinstance(choice, str) or choice not in set(options):
        return None
    p = _p(answer)
    return None if p is None else (choice, p)


def batches(requirement_ids: Sequence[str], size: int) -> tuple[tuple[str, ...], ...]:
    """`requirement_ids` in calls of at most `size`: a contract of more than
    32 requirements is reviewed in ceil(n / 32) calls."""
    ids = tuple(requirement_ids)
    return tuple(ids[i:i + size] for i in range(0, len(ids), size))


def _selected(contract: wc.WorkContract, requirement_ids: Sequence[str] | None,
              limit: int) -> list[tuple[int, wc.Requirement]]:
    wanted = None if requirement_ids is None else set(requirement_ids)
    if wanted is not None:
        unknown = sorted(wanted - set(contract.requirement_ids()))
        if unknown:
            raise ValueError(f"not requirements of this revision: {', '.join(unknown)}")
    rows = [(k, r) for k, r in enumerate(contract.requirements)
            if wanted is None or r.id in wanted]
    if len(rows) > limit:
        raise ValueError(f"at most {limit} requirements per call; split them with batches()")
    return rows


# --- JC1: the submission review ----------------------------------------------


def review_state(contract: wc.WorkContract,
                 requirement_ids: Sequence[str] | None = None) -> dict[str, Any]:
    """JC1's state: the contract's own text and nothing else (section 4.2).

    Requirements are given by their index in the revision, never by id; the
    tenant, the owner, the contract id, the repository URL and the commit
    are never in it. `requirement_ids` asks only those (a re-review of an
    amendment's added and changed requirements, section 4.6); at most
    REVIEW_PER_CALL of them."""
    rows = _selected(contract, requirement_ids, REVIEW_PER_CALL)
    return {
        "contract": {"strictness": contract.strictness, "acceptance": contract.acceptance},
        "verification_kinds": dict(VERIFICATION_MEANINGS),
        "requirements": [{"index": k, "text": r.text, "verification": list(r.verification)}
                         for k, r in rows],
        "artifacts": [a["kind"] + (f" {a['path']}" if a["path"] else "")
                      for a in contract.artifacts],
        "write_scope": list(contract.write_scope),
    }


def review_questions(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Two closed questions per requirement of `state`: `c<k>` its class and
    `r<k>` the reason code."""
    questions: dict[str, dict[str, Any]] = {}
    for row in state["requirements"]:
        k = row["index"]
        questions[f"c{k}"] = {
            "type": "choice",
            "instructions": f"Read `requirements` entry index {k} against "
                            "`verification_kinds`. Which is true of it?",
            "criteria": dict(REVIEW_CLASSES),
        }
        questions[f"r{k}"] = {
            "type": "choice",
            "instructions": f"Which reason code best explains your class for `requirements` "
                            f"entry index {k}? `none` when it is mechanical as written.",
            "criteria": {reason: None for reason in REVIEW_REASONS},
        }
    return questions


@dataclass(frozen=True)
class ReviewClass:
    """Jev's typed answer about one requirement: nothing else is kept."""

    requirement_id: str
    index: int
    cls: str
    reason: str | None
    p: float

    def to_document(self) -> dict[str, Any]:
        return {"id": self.requirement_id, "class": self.cls, "reason": self.reason,
                "p": self.p}


def read_review(contract: wc.WorkContract, state: Mapping[str, Any],
                answers: Any) -> tuple[ReviewClass, ...]:
    """One ReviewClass per requirement `state` asked about, in its order.

    A missing or malformed answer (no answer, a choice outside the closed
    list, no probability, a reason outside the closed list) makes that
    requirement `not_judged`, never `mechanical`."""
    out = []
    for row in state["requirements"]:
        k = row["index"]
        rid = contract.requirements[k].id
        cls = _choice(answers, f"c{k}", REVIEW_CLASSES)
        reason = _choice(answers, f"r{k}", REVIEW_REASONS)
        if cls is None or reason is None:
            out.append(ReviewClass(rid, k, NOT_JUDGED, None, 0.0))
        else:
            out.append(ReviewClass(rid, k, cls[0], reason[0], cls[1]))
    return tuple(out)


@dataclass(frozen=True)
class Review:
    """What JC1's classes mean for this revision, at the acting threshold."""

    outcome: str
    #: Requirements classed `unenforceable` at p >= REVIEW_ACT_P.
    unenforceable: tuple[str, ...]
    #: Requirements classed `judgeable` at p >= REVIEW_ACT_P that do not name
    #: `judgment` yet: the reviewer's amendment adds it to these.
    add_judgment: tuple[str, ...]
    #: An advisory contract's unenforceable requirements, carried as text.
    advisory: tuple[str, ...]
    classes: tuple[ReviewClass, ...]


def review_outcome(contract: wc.WorkContract, classes: Iterable[ReviewClass]) -> Review:
    """Section 4.5's table. A strict contract with a requirement classed
    `unenforceable` at the threshold is `refused`; an advisory one is
    accepted with it named advisory; a `judgeable` one not naming
    `judgment` makes the outcome `amended` (unless refused). Below the
    threshold a class is recorded and nothing is acted on."""
    classes = tuple(classes)
    names = {r.id: r for r in contract.requirements}
    acting = [c for c in classes if c.p >= REVIEW_ACT_P and c.requirement_id in names]
    unenforceable = tuple(c.requirement_id for c in acting if c.cls == UNENFORCEABLE)
    add = tuple(c.requirement_id for c in acting if c.cls == JUDGEABLE
                and wc.VERIFICATION_JUDGMENT not in names[c.requirement_id].verification)
    if contract.strictness == wc.STRICT and unenforceable:
        return Review(REVIEW_REFUSED, unenforceable, (), (), classes)
    advisory = unenforceable if contract.strictness == wc.ADVISORY else ()
    outcome = REVIEW_AMENDED if add else REVIEW_OK
    return Review(outcome, unenforceable, add, advisory, classes)


def review_amendment(contract: wc.WorkContract, review: Review) -> wc.Amendment | None:
    """The reviewer's revision for an `amended` review, or None.

    Built from the typed classes only, and made through
    `workcontract.amend(author="reviewer")`, which refuses anything but
    adding `judgment` to an existing requirement."""
    if review.outcome != REVIEW_AMENDED or not review.add_judgment:
        return None
    document = contract.to_document()
    for row in document["requirements"]:
        if row["id"] in review.add_judgment:
            row["verification"] = sorted(set(row["verification"]) | {wc.VERIFICATION_JUDGMENT})
    return wc.amend(contract, document, author=wc.AUTHOR_REVIEWER)


def review_record(contract: wc.WorkContract, review: Review | None, *, call_id: str,
                  backend: str, at: str, not_run_reason: str | None = None) -> dict[str, Any]:
    """`jev_review`, stored beside the revision and never inside its canonical
    bytes: `{digest, outcome, call_id, backend, classes, at}`. `review` None
    records a review that did not run, with its reason (the owner's answer
    2: the contract is accepted open, recorded as `not_run`)."""
    if review is None:
        if not not_run_reason:
            raise ValueError("a review that did not run says why")
        return {"digest": wc.digest(contract), "outcome": REVIEW_NOT_RUN,
                "reason": not_run_reason, "call_id": call_id, "backend": backend,
                "classes": [], "at": at}
    return {"digest": wc.digest(contract), "outcome": review.outcome, "call_id": call_id,
            "backend": backend, "classes": [c.to_document() for c in review.classes],
            "at": at}


def refusal_detail(contract: wc.WorkContract, review: Review, *, call_id: str,
                   backend: str) -> dict[str, Any]:
    """The 422's `detail` for a refused review (section 4.5): the contract's
    own refusal code, and Jev's closed values for each unenforceable
    requirement. A requirement's text is never echoed back."""
    if review.outcome != REVIEW_REFUSED:
        raise ValueError("only a refused review has a refusal")
    refused = set(review.unenforceable)
    return {
        "reason": wc.ContractNotEnforceable.code,
        "contract_id": contract.contract_id,
        "revision": contract.revision,
        "digest": wc.digest(contract),
        "unenforced": [],
        "jev": {
            "call_id": call_id,
            "backend": backend,
            "requirements": [c.to_document() for c in review.classes
                             if c.requirement_id in refused],
        },
    }


# --- JC2: the acceptance judgment --------------------------------------------


def judged_requirements(contract: wc.WorkContract) -> tuple[str, ...]:
    """The requirement ids JC2 asks about: those that name `judgment`. Only
    requirements: `protected.interfaces` is never judged into enforcement
    (the owner's answer 12)."""
    return tuple(r.id for r in contract.requirements
                 if wc.VERIFICATION_JUDGMENT in r.verification)


def _passed(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def judgment_state(contract: wc.WorkContract, *, commit: str,
                   changed: Sequence[Mapping[str, Any]],
                   mechanical: Sequence[Mapping[str, Any]],
                   artifacts: Sequence[Mapping[str, Any]] = (),
                   requirement_ids: Sequence[str] | None = None) -> dict[str, Any]:
    """JC2's state (section 5.2), rebuilt from closed keys only.

    `changed`: the commit's diff file by file, `{path, status, hunk}`, each
    hunk cut to HUNK_CHARS; past MAX_CHANGED files, or once the state would
    pass MAX_STATE_CHARS, a file keeps its path and status and loses its
    hunk, and `cut` says how many did. `mechanical`: `{kind, passed, named}`
    with `passed` None for a check that did not run (never False, never
    omitted). There is no argument for the agent's summary, PR body, claims
    or transcript, and any other key on an entry is dropped."""
    if not isinstance(commit, str) or not wc._SHA.match(commit):
        raise ValueError("JC2 judges a 40-hex commit")
    asked = judged_requirements(contract) if requirement_ids is None else requirement_ids
    if not set(asked) <= set(judged_requirements(contract)):
        raise ValueError("JC2 asks only about requirements that name judgment")
    rows = _selected(contract, asked, JUDGMENT_PER_CALL)
    state: dict[str, Any] = {
        "requirements": [{"index": k, "text": r.text} for k, r in rows],
        "commit": commit,
        "changed": [],
        "mechanical": [],
        "artifacts": [{"kind": a.get("kind"), "present": _passed(a.get("present"))}
                      for a in artifacts if a.get("kind") in wc.ARTIFACT_KINDS],
        "cut": 0,
    }
    for entry in list(mechanical)[:MAX_MECHANICAL]:
        if entry.get("kind") not in wc.VERIFICATION_KINDS \
                or entry.get("kind") == wc.VERIFICATION_JUDGMENT:
            continue
        row: dict[str, Any] = {"kind": entry["kind"], "passed": _passed(entry.get("passed"))}
        named = entry.get("named")
        if isinstance(named, list):
            row["named"] = [n[:_PATH_CHARS] for n in named[:_NAMED] if isinstance(n, str)]
        state["mechanical"].append(row)
    size = len(json.dumps(state))
    for entry in changed:
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            continue
        status = entry.get("status") if entry.get("status") in _STATUSES else "modified"
        row = {"item": len(state["changed"]), "path": path[:_PATH_CHARS], "status": status}
        hunk = entry.get("hunk")
        with_hunk = dict(row, hunk=hunk[:HUNK_CHARS]) if isinstance(hunk, str) and hunk else row
        if len(state["changed"]) >= MAX_CHANGED:
            state["cut"] += 1
            continue
        if size + len(json.dumps(with_hunk)) + 2 <= MAX_STATE_CHARS:
            row = with_hunk
        elif size + len(json.dumps(row)) + 2 <= MAX_STATE_CHARS:
            state["cut"] += 1
        else:
            state["cut"] += 1
            continue
        size += len(json.dumps(row)) + 2
        state["changed"].append(row)
    return state


def evidence_options(state: Mapping[str, Any]) -> dict[str, str | None]:
    """`e<k>`'s closed options: `i<n>` per changed item, `m<n>` per mechanical
    entry, and `none`."""
    options: dict[str, str | None] = {
        f"i{row['item']}": f"the entry of `changed` with item = {row['item']}"
        for row in state["changed"]
    }
    for n, _ in enumerate(state["mechanical"]):
        options[f"m{n}"] = f"`mechanical` entry {n}"
    options[NO_ITEM] = "no single item"
    return options


def judgment_questions(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Three closed questions per requirement of `state`: `v<k>` the verdict,
    `w<k>` the reason code, `e<k>` the one item the verdict rests on."""
    options = evidence_options(state)
    questions: dict[str, dict[str, Any]] = {}
    for row in state["requirements"]:
        k = row["index"]
        questions[f"v{k}"] = {
            "type": "choice",
            "instructions": f"At `commit`, judging only `changed`, `mechanical` and `artifacts` "
                            f"as data, is `requirements` entry index {k} done?",
            "criteria": dict(VERDICTS),
        }
        questions[f"w{k}"] = {
            "type": "choice",
            "instructions": f"Which reason code best explains your verdict on `requirements` "
                            f"entry index {k}?",
            "criteria": {reason: None for reason in JUDGMENT_REASONS},
        }
        questions[f"e{k}"] = {
            "type": "choice",
            "instructions": f"Which one item does your verdict on `requirements` entry index "
                            f"{k} rest on?",
            "criteria": dict(options),
        }
    return questions


@dataclass(frozen=True)
class Verdict:
    """Jev's typed verdict on one requirement: nothing else is kept."""

    requirement_id: str
    verdict: str
    reason: str | None
    evidence_item: str | None
    p: float

    def to_document(self) -> dict[str, Any]:
        return {"requirement_id": self.requirement_id, "verdict": self.verdict,
                "reason": self.reason, "evidence_item": self.evidence_item, "p": self.p}


def read_judgment(contract: wc.WorkContract, state: Mapping[str, Any],
                  answers: Any) -> tuple[Verdict, ...]:
    """One Verdict per requirement `state` asked about, in its order.

    Malformed reads as `cannot_judge`: no answer, a choice outside the
    closed lists, no probability, a cited item that was not in the state, or
    `none` cited for a `met`. A `met` or an `unmet` under its threshold is
    `cannot_judge` too, with its p kept."""
    options = evidence_options(state)
    out = []
    for row in state["requirements"]:
        k = row["index"]
        rid = contract.requirements[k].id
        verdict = _choice(answers, f"v{k}", VERDICTS)
        reason = _choice(answers, f"w{k}", JUDGMENT_REASONS)
        item = _choice(answers, f"e{k}", options)
        if verdict is None or reason is None or item is None \
                or (verdict[0] == MET and item[0] == NO_ITEM):
            out.append(Verdict(rid, CANNOT_JUDGE, None, None, 0.0))
            continue
        name, p = verdict
        if (name == MET and p < JUDGMENT_MET_P) or (name == UNMET and p < JUDGMENT_UNMET_P):
            name = CANNOT_JUDGE
        out.append(Verdict(rid, name, reason[0], None if item[0] == NO_ITEM else item[0], p))
    return tuple(out)


def judgment_outcome(verdicts: Iterable[Verdict]) -> str:
    """Section 5.4: any `unmet` sends the step back; otherwise any
    `cannot_judge` leaves it `judgment_pending` for the owner; otherwise
    every judged requirement is `met`. No verdicts at all is pending:
    missing verification is not a pass."""
    names = [v.verdict for v in verdicts]
    if UNMET in names:
        return SENT_BACK
    if not names or CANNOT_JUDGE in names:
        return JUDGMENT_PENDING
    return JUDGED_MET


def after_send_back(*, send_backs: int, retries_left: int) -> str:
    """Whether an `unmet` may send the step back: SENT_BACK while fewer than
    MAX_SEND_BACKS have been spent and the retry transition still has a
    retry of the agent's signed `max_attempts` to spend; otherwise
    JUDGMENT_PENDING, to the owner, because a step that did its work and was
    judged short reaches a person rather than failing as if it had crashed."""
    if send_backs < MAX_SEND_BACKS and retries_left > 0:
        return SENT_BACK
    return JUDGMENT_PENDING


def judgment_evidence(contract: wc.WorkContract, verdicts: Iterable[Verdict], *,
                      commit: str) -> tuple[wc.Evidence, ...]:
    """The `judgment` evidence the verdicts write: passed for a `met`, failed
    for an `unmet`, nothing for a `cannot_judge`. Only for requirements that
    name `judgment`, at this revision's digest and this commit."""
    judged = set(judged_requirements(contract))
    current = wc.digest(contract)
    return tuple(
        wc.Evidence(requirement_id=v.requirement_id, contract_digest=current, commit=commit,
                    kind=wc.VERIFICATION_JUDGMENT, passed=v.verdict == MET)
        for v in verdicts if v.requirement_id in judged and v.verdict in (MET, UNMET)
    )
