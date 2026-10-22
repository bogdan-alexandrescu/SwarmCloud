"""Root causes `failure-review` has found, kept across firings (docs/design/failure-review.md
§4's fingerprint and validation, §5; lane FR-2 DIGEST, part of #1933).

WHY A RECORD PER CAUSE. Every firing reads seven hours, one of them the
previous firing's last; a cause that keeps failing is found again every six
hours. Without a record each finding would be a new issue and a new fix run.
`failure_causes/{fingerprint}` is that record: its issue, its state, its fix
runs, the signatures it matched and the failures it has counted, so

  * a NEW cause is filed once (`decide` -> `file_and_run`, or `file_held`
    for a hold category, or `report` for a cause in a tenant's own code);
  * a cause ALREADY FILED gets one comment per firing, never a second issue;
  * a cause with an OPEN FIX (a live issue run, an open PR) gets no second run;
  * a cause FIXED THAT RECURS gets a comment and one new run, and a cause
    whose second fix run failed or recurred is `held` for the owner;
  * a cause a person closed as NOT PLANNED gets the comment and no run.

THE FINGERPRINT IS SWARM-API'S, not the agent's (§4): `fr-` and the first 12
hex of SHA-256 over `component|site|class|signature`, each normalised first
-- case and spacing, and the signature's figures, ids and paths as
placeholders (`failuredigest.normalise`) -- so the same cause found by two
reruns, or worded with other figures, is one fingerprint. An agent that names
a `known_fingerprint` swarm-api holds gets that one.

THE SAME FAILURE IN TWO OVERLAPPING WINDOWS IS ONE FAILURE. `observe` keeps
the ids it has counted (`failure_ids`, the most recent `MAX_FAILURE_IDS`) and
counts only the new ones, so the hour two windows share is never counted
twice, and a retried firing that observes again changes nothing.

`validate_rca` is §4's gate: nothing is filed from an `rca.json` that does not
account for every reviewed failure exactly once, give each cause evidence, and
keep `hold`, `where` and `action` inside their vocabularies. A file that fails files
nothing (§9).

WHAT THIS MODULE DOES NOT DO: it files no issue and starts no run. It decides,
and records what the type (lane FR-3) did, each write one transaction.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence

from google.cloud import firestore
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from swarm_common.admission import _snapshot

from .errors import ValidationFailed
from .failuredigest import normalise

CAUSES = "failure_causes"

#: §5's states. `open`: filed, no fix run yet or none live; `fixing`: a fix run
#: started; `held`: `needs-owner`, no run; `fixed`: its fix merged; `reported`:
#: a cause in a tenant's own repository, reported to that tenant, never filed here;
#: `no_change`: the investigator said no code change is needed (`action: none`,
#: #2003), recorded with its why and never filed -- until it recurs.
OPEN, FIXING, HELD, FIXED, REPORTED, NO_CHANGE = "open", "fixing", "held", "fixed", "reported", "no_change"
STATES = (OPEN, FIXING, HELD, FIXED, REPORTED, NO_CHANGE)
#: §4's vocabularies.
HOLDS = ("none", "iam", "contract", "secrets", "tenancy", "shared", "cost")
WHERE = ("platform", "tenant_repo")
#: §4's `action` (#2003): `fix` files and runs (the default, and every rca
#: written before the field existed); `none` needs no code change; `existing`
#: is an open issue already in the platform repository (`existing_issue`).
ACTIONS = ("fix", "none", "existing")
#: §5: two fix runs that did not hold are the owner's to look at.
MAX_FIX_RUNS = 2
#: Bounds on a record, so a cause that recurs for months stays one small document.
MAX_FAILURE_IDS = 500
MAX_SIGNATURES = 20

FINGERPRINT = re.compile(r"^fr-[0-9a-f]{12}$")
_MARKER = re.compile(r"<!-- failure-review fingerprint=(fr-[0-9a-f]{12})(?: firing=[A-Za-z0-9_-]+)? -->")


def _norm(value: Any) -> str:
    return " ".join(str(value or "").split()).lower()


def fingerprint(component: str, site: str, cls: str, signature: str) -> str:
    """`fr-` + 12 hex of SHA-256 over the normalised `component|site|class|signature` (§4)."""
    parts = (_norm(component), " ".join(str(site or "").split()), _norm(cls),
             normalise(signature, limit=500))
    return "fr-" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]


def marker(fp: str) -> str:
    """The line an issue body opens with: found again before anything is filed twice (§5)."""
    return f"<!-- failure-review fingerprint={fp} -->"


def comment_marker(fp: str, firing_id: str) -> str:
    """The line a per-firing comment opens with: one comment per firing, a retry finds it."""
    return f"<!-- failure-review fingerprint={fp} firing={firing_id} -->"


def marker_in(text: Any) -> str | None:
    """The fingerprint a body's marker names, or None."""
    matched = _MARKER.search(text if isinstance(text, str) else "")
    return matched.group(1) if matched else None


# --------------------------------------------------------------------------
# rca.json (§4)
# --------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Evidence(_Strict):
    command: str = Field(min_length=1, max_length=2000)
    result: str = Field(max_length=8000)


class FingerprintOf(_Strict):
    component: str = Field(min_length=1, max_length=100)
    site: str = Field(min_length=1, max_length=300)
    cls: str = Field(alias="class", min_length=1, max_length=100)
    signature: str = Field(min_length=1, max_length=1000)


class CauseIn(_Strict):
    fingerprint_of: FingerprintOf
    known_fingerprint: str | None = None
    failures: list[str] = Field(min_length=1)
    cause: str = Field(min_length=1, max_length=2000)
    evidence: list[Evidence] = Field(min_length=1)
    fix: str = Field(min_length=1, max_length=8000)
    files: list[str] = Field(default_factory=list, max_length=200)
    hold: Literal["none", "iam", "contract", "secrets", "tenancy", "shared", "cost"]
    where: Literal["platform", "tenant_repo"]
    action: Literal["fix", "none", "existing"] = "fix"
    existing_issue: int | None = Field(default=None, ge=1)


class Unexplained(_Strict):
    failure: str
    tried: str = Field(min_length=1, max_length=4000)


class Rca(_Strict):
    firing_id: str
    causes: list[CauseIn] = Field(default_factory=list)
    unexplained: list[Unexplained] = Field(default_factory=list)


@dataclass(frozen=True)
class Cause:
    """One validated cause, with swarm-api's fingerprint."""

    fingerprint: str
    signature: str
    failures: tuple[str, ...]
    cause: str
    evidence: tuple[Evidence, ...]
    fix: str
    files: tuple[str, ...]
    hold: str
    where: str
    known: bool
    action: str = "fix"
    existing_issue: int | None = None


def validate_rca(raw: Any, *, firing_id: str, refs: Iterable[str],
                 known: Iterable[str] = ()) -> tuple[list[Cause], list[Unexplained]]:
    """§4's gate. Raises ValidationFailed naming every problem; returns the causes otherwise.

    * the file is this firing's;
    * every reviewed reference appears in exactly one cause or in
      `unexplained`, and nothing else does;
    * every cause has evidence, and `hold` and `where` are in their vocabularies;
    * the fingerprint is computed here; a `known_fingerprint` is honoured only
      when swarm-api holds it.
    """
    try:
        doc = Rca.model_validate(json.loads(raw) if isinstance(raw, (str, bytes)) else raw)
    except (ValidationError, ValueError) as exc:
        raise ValidationFailed(f"rca.json does not validate: {_brief(exc)}") from None
    problems: list[str] = []
    if doc.firing_id != firing_id:
        problems.append("rca.json names another firing")
    wanted = set(refs)
    held = set(known)
    seen: dict[str, int] = {}
    for cause in doc.causes:
        for ref in cause.failures:
            seen[ref] = seen.get(ref, 0) + 1
    for item in doc.unexplained:
        seen[item.failure] = seen.get(item.failure, 0) + 1
    missing = sorted(wanted - set(seen))
    twice = sorted(ref for ref, n in seen.items() if n > 1)
    stray = sorted(set(seen) - wanted)
    if missing:
        problems.append(f"not accounted for: {', '.join(missing[:20])}")
    if twice:
        problems.append(f"accounted for more than once: {', '.join(twice[:20])}")
    if stray:
        problems.append(f"not in the digest: {', '.join(stray[:20])}")
    if problems:
        raise ValidationFailed("rca.json does not validate: " + "; ".join(problems))
    causes: list[Cause] = []
    for item in doc.causes:
        of = item.fingerprint_of
        computed = fingerprint(of.component, of.site, of.cls, of.signature)
        known_fp = item.known_fingerprint if item.known_fingerprint in held else None
        causes.append(Cause(
            fingerprint=known_fp or computed, signature=normalise(of.signature, limit=500),
            failures=tuple(item.failures), cause=item.cause, evidence=tuple(item.evidence),
            fix=item.fix, files=tuple(item.files), hold=item.hold, where=item.where,
            known=known_fp is not None, action=item.action, existing_issue=item.existing_issue,
        ))
    return causes, list(doc.unexplained)


def _brief(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:10])
    return "not JSON"


# --------------------------------------------------------------------------
# What to do with a cause (§5, §6)
# --------------------------------------------------------------------------


class Action(str, Enum):
    FILE_AND_RUN = "file_and_run"        # a new platform cause: file it, start one fix run
    FILE_HELD = "file_held"              # a new cause in a hold category: file it `needs-owner`, no run
    REPORT = "report"                    # a cause in a tenant's own code: a finding to that tenant
    COMMENT = "comment"                  # already filed: this firing's comment, nothing else
    COMMENT_AND_RUN = "comment_and_run"  # filed, no live fix, a run left: comment and one new run
    HOLD = "hold"                        # comment, set `held`, label `needs-owner`, no run
    NO_CHANGE = "no_change"              # `action: none`: recorded and summarised, no issue, no run


#: How a person closed the cause's issue, when they did (`state_reason` on GitHub).
CLOSED_NOT_PLANNED = "not_planned"
CLOSED_COMPLETED = "completed"


@dataclass(frozen=True)
class Facts:
    """What the forge and the run store say about a filed cause, read by the type."""

    live_run: bool = False
    open_pr: bool = False
    issue_closed: str | None = None


def decide(record: Mapping[str, Any] | None, *, hold: str, where: str,
           facts: Facts = Facts()) -> Action:
    """§5 and §6's rule for one cause this firing found."""
    if where == "tenant_repo":
        return Action.REPORT
    if record is None or not record.get("issue"):
        return Action.FILE_AND_RUN if hold == "none" else Action.FILE_HELD
    if facts.issue_closed == CLOSED_NOT_PLANNED or record.get("state") == HELD:
        return Action.COMMENT
    if facts.live_run or facts.open_pr:
        return Action.COMMENT
    if hold != "none" or len(record.get("runs") or []) >= MAX_FIX_RUNS:
        return Action.HOLD
    return Action.COMMENT_AND_RUN


# --------------------------------------------------------------------------
# The record
# --------------------------------------------------------------------------


@dataclass
class Observation:
    record: dict[str, Any]
    created: bool
    new_failures: list[str] = field(default_factory=list)
    #: False when this firing already observed the cause (a retried firing).
    first_this_firing: bool = True


class FailureCauses:
    """`failure_causes/{fingerprint}`. Each write is one transaction."""

    def __init__(self, db: Any, *, now: Callable[[], datetime]) -> None:
        self._db = db
        self._now = now

    def _ref(self, fp: str) -> Any:
        if not FINGERPRINT.fullmatch(fp):
            raise ValidationFailed("not a failure-review fingerprint")
        return self._db.collection(CAUSES).document(fp)

    def get(self, fp: str) -> dict[str, Any] | None:
        snap = self._ref(fp).get()
        return (snap.to_dict() or {}) if snap.exists else None

    def known(self, limit: int = 200) -> list[dict[str, Any]]:
        """Every record the digest names as a known cause (§3): all but `reported`.

        A `fixed` cause is listed so that its recurrence is named as one.
        """
        rows = [snap.to_dict() or {} for snap in self._db.collection(CAUSES).limit(limit).stream()]
        return sorted((r for r in rows if r.get("state") != REPORTED),
                      key=lambda r: str(r.get("fingerprint")))

    def observe(self, cause: Cause, *, failure_ids: Sequence[str], firing_id: str,
                title: str = "") -> Observation:
        """Record that `firing_id` found `cause`; count only failures not counted before.

        `failure_ids` are the failures' own ids (`failuredigest.Digest.refs`
        resolves `cause.failures`, which are this firing's references): a
        reference means nothing to the next firing, an agent id does.
        """
        ref = self._ref(cause.fingerprint)
        now = self._now()

        @firestore.transactional
        def _apply(txn: Any) -> Observation:
            snap = _snapshot(txn.get(ref))
            data = (snap.to_dict() or {}) if snap.exists else None
            if data is None:
                fresh = list(dict.fromkeys(failure_ids))
                record = {
                    "fingerprint": cause.fingerprint,
                    "title": title[:200],
                    "issue": None,
                    "state": REPORTED if cause.where == "tenant_repo" else (
                        OPEN if cause.hold == "none" else HELD),
                    "hold": cause.hold,
                    "where": cause.where,
                    "runs": [],
                    "first_seen": now,
                    "last_seen": now,
                    "failures_seen": len(fresh),
                    "failure_ids": fresh[-MAX_FAILURE_IDS:],
                    "signatures": [cause.signature],
                    "last_firing": firing_id,
                }
                txn.set(ref, record)
                return Observation(record=record, created=True, new_failures=fresh)
            counted = list(data.get("failure_ids") or [])
            already = set(counted)
            fresh = [f for f in dict.fromkeys(failure_ids) if f not in already]
            first = data.get("last_firing") != firing_id
            signatures = list(data.get("signatures") or [])
            if cause.signature not in signatures:
                signatures = (signatures + [cause.signature])[-MAX_SIGNATURES:]
            patch = {
                "last_seen": now,
                "failures_seen": int(data.get("failures_seen") or 0) + len(fresh),
                "failure_ids": (counted + fresh)[-MAX_FAILURE_IDS:],
                "signatures": signatures,
                "last_firing": firing_id,
            }
            if not fresh and not first:
                return Observation(record=data, created=False, new_failures=[],
                                   first_this_firing=False)
            txn.update(ref, patch)
            return Observation(record={**data, **patch}, created=False, new_failures=fresh,
                               first_this_firing=first)

        return _apply(self._db.transaction())

    def record_issue(self, fp: str, number: int) -> None:
        """The issue the type filed (or found by its marker) for this cause."""
        self._ref(fp).update({"issue": int(number), "updated_at": self._now()})

    def record_run(self, fp: str, run_id: str, *, firing_id: str) -> bool:
        """A fix run started for this cause; False (and nothing written) past MAX_FIX_RUNS.

        Recorded by run id, so a retried firing that records the same run
        again adds nothing.
        """
        ref = self._ref(fp)
        now = self._now()

        @firestore.transactional
        def _apply(txn: Any) -> bool:
            snap = _snapshot(txn.get(ref))
            data = (snap.to_dict() or {}) if snap.exists else None
            if data is None:
                return False
            runs = [dict(r) for r in data.get("runs") or [] if isinstance(r, Mapping)]
            if any(r.get("run_id") == run_id for r in runs):
                return True
            if len(runs) >= MAX_FIX_RUNS:
                return False
            runs.append({"run_id": run_id, "firing_id": firing_id, "started_at": now})
            txn.update(ref, {"runs": runs, "state": FIXING, "updated_at": now})
            return True

        return _apply(self._db.transaction())

    def record_no_change(self, fp: str, *, firing_id: str, fix: str, evidence: Sequence[Mapping[str, Any]]) -> None:
        """`action: none`: the investigator's why and its evidence, so the record
        shows what was dropped and on what grounds (#2003)."""
        self._ref(fp).update({"state": NO_CHANGE, "updated_at": self._now(), "no_change": {
            "firing_id": firing_id, "fix": fix, "evidence": [dict(e) for e in evidence]}})

    def set_state(self, fp: str, state: str, *, merge: Mapping[str, Any] | None = None) -> None:
        """`held` (needs-owner), `fixed` (with the merge that fixed it), or back to `open`."""
        if state not in STATES:
            raise ValidationFailed(f"state must be one of {', '.join(STATES)}")
        patch: dict[str, Any] = {"state": state, "updated_at": self._now()}
        if merge is not None:
            patch["fixed_by"] = dict(merge)
        self._ref(fp).update(patch)


def failures_of(causes: Sequence[Cause]) -> set[str]:
    """Every reference the validated causes account for."""
    return {ref for cause in causes for ref in cause.failures}
