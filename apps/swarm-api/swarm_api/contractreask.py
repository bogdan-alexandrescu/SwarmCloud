"""The control plane's re-ask of a step's acceptance judgment (the owner's answer 5).

docs/design/agent-quality/jev-contract.md sections 5.4, 8.2 and 9; the
owner's answers of 3 November 2026 override section 8.2's recommendation:
when the worker's end-of-step judgment comes back `not_run` (Jev down, over
its cap, no route) or `cannot_judge`, THE CONTROL PLANE re-asks Jev three
times over 30 minutes from the published diff, reaching it through swarm-api's
route to the workspace's backend (answer 1: Direct VPC egress and the
internal load balancer in front of the tailnet egress Service), and only then
sends the step to the owner's approval screen. While it does, the step's
acceptance is `judgment_pending`, waiting on the re-ask.

WHY IN SWARM-API, NOT THE RECONCILER. The re-ask needs exactly what swarm-api
holds and the reconciler does not: the route to the Jev backend (only
swarm-api's egress reaches the internal load balancer), the workspace's
backend choice and signed opt-in readers (`judgment`, `codeintel`), and the
forge read token for the published diff (`impact.resolve_read_token`). The
reconciler would need a second egress, the settings MAC key and a forge
credential, which is three new grants for one bounded pass.

NEVER A SLEEP IN A REQUEST (invariant 4). The pass is one bounded walk over
the due records (`PASS_LIMIT`), started by the scheduled tick; each record is
claimed in a transaction BEFORE Jev is asked (its try counted, its next try
scheduled), asked once with the client's own deadline, and settled in a
second transaction that writes only if the record is still the one claimed:
the same attempt, the same generation, the same digest and commit, and the
same try. So two passes can never ask twice on one try, a newer attempt (a
newer generation) supersedes the record with nothing written, and the re-ask
runs at most `REASK_TRIES` times inside `REASK_WINDOW`. It stops on the first
verdict.

ITS JEV CALLS ARE COUNTED AND CAPPED (the owner's answer, 4 November 2026).
Each re-ask's call is admitted against the workspace's signed
`code_intelligence.jev_monthly_cap_tokens`, after every live reservation the
worker's attempts hold, and recorded in its monthly `jev_usage`, through
`jevaccount.ReaskLedger`: a row of its own per attempt, kind
`contract_judgment`, which the usage page shows as the control plane's
re-ask. A call the cap refuses is a try without a verdict (`budget_monthly`).

WIRED INTO THE SCHEDULE TICK (`routes/schedule_tick.py`): one pass a minute.

THE RE-ASK NEVER SENDS BACK. It runs after the attempt has ended, so there is
no live attempt to put through the retry transition; an `unmet` it finds
writes its failed evidence and goes to the owner with every verdict attached,
as the send-back cap does (section 5.4).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Mapping, Sequence

from google.api_core import exceptions as gexc
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot
from swarm_jev import sanitize

from . import contractevidence as ce
from . import contractjudge as cj
from . import workcontract as wc
from .redaction import redact
from .store import TASKS
from .verdictpublish import spec_refusal

log = logging.getLogger(__name__)

#: The task field the step's acceptance is written under. swarm-api's own,
#: outside the signed spec: a judgment never moves the step's signature.
ACCEPTANCE_FIELD = "contract_acceptance"
#: How many times the step has been sent back (the owner's answer 11).
SEND_BACKS_FIELD = "contract_send_backs"
#: What the next attempt's brief carries, one block per unmet requirement.
SEND_BACK_FIELD = "contract_send_back"

#: `contract_acceptance.status`.
STATUS_MET = cj.JUDGED_MET
STATUS_SENT_BACK = cj.SENT_BACK
STATUS_PENDING = cj.JUDGMENT_PENDING
#: `contract_acceptance.waiting_on` while pending: the re-ask, or the owner.
WAITING_REASK = "reask"
WAITING_OWNER = "owner"

#: `contract_acceptance.hunks`: the text of each hunk a verdict cites, by its
#: item (`i<n>`), as JC2 was shown it -- cut to `contractjudge.HUNK_CHARS` and
#: masked. Stored, not re-read: the published diff can move or vanish, and the
#: person deciding reads what Jev read (the owner's pick on PR #3255).
HUNKS_FIELD = "hunks"

#: Why a pending step went to the owner.
OWNER_SEND_BACK_CAP = "send_back_cap"
OWNER_ATTEMPTS_EXHAUSTED = "attempts_exhausted"
OWNER_REASK_EXHAUSTED = "reask_exhausted"
OWNER_UNMET_AFTER_ATTEMPT = "unmet_after_attempt"
OWNER_CONTRACT_SUPERSEDED = "contract_superseded"

#: The owner's answer 5: three re-asks over 30 minutes. Each try's offset
#: from the moment the worker's judgment came back without a verdict; the
#: last sits inside the window, so every try is asked before it closes.
REASK_TRIES = 3
REASK_WINDOW = timedelta(minutes=30)
REASK_OFFSETS = (timedelta(minutes=5), timedelta(minutes=15), timedelta(minutes=25))
#: One pass's bound: the records it claims, oldest due first.
PASS_LIMIT = 25

#: The Jev ledger kind (jev-contract.md section 2).
JUDGMENT_KIND = "contract_judgment"

OUTCOME_JUDGED = "judged"
OUTCOME_NOT_RUN = "not_run"
OUTCOMES = (OUTCOME_JUDGED, OUTCOME_NOT_RUN)


# --- the signed contract a task carries --------------------------------------


def signed_contract(settings: Any, task: Mapping[str, Any],
                    task_id: str) -> wc.WorkContract | str:
    """The contract revision the task's SIGNED dispatch reference names, or
    why there is none to judge against.

    The step's spec signature must verify (`verdictpublish.spec_refusal`), so
    the reference at `metadata.dispatch.work_contract` is the one submission
    signed; the stored body at `metadata.work_contract` must decode, be the
    task's tenant's, and match the reference by digest
    (`workcontract.verify_reference`). The reason names the check, never a
    value from the document."""
    refusal = spec_refusal(settings, task, task_id)
    if refusal is not None:
        return refusal
    metadata = task.get("metadata") if isinstance(task.get("metadata"), Mapping) else {}
    dispatch = metadata.get("dispatch") if isinstance(metadata.get("dispatch"), Mapping) else {}
    reference = dispatch.get(wc.DISPATCH_FIELD)
    if not isinstance(reference, Mapping):
        return "the step's signed dispatch block names no work contract"
    try:
        contract = wc.decode(metadata.get(wc.METADATA_KEY))
    except wc.WorkContractError:
        return "the step's stored work contract does not decode"
    if contract.tenant_id != task.get("tenant_id"):
        return "the step's work contract is another workspace's"
    if not wc.verify_reference(contract, reference):
        return "the step's stored work contract is not the revision its signed reference names"
    return contract


# --- verdicts ------------------------------------------------------------------


def item_paths(changed: Sequence[str]) -> dict[str, str]:
    """`i<n>` -> the changed path it names, in the order the state sent them."""
    return {f"i{n}": path for n, path in enumerate(changed)}


def checked_verdict(contract: wc.WorkContract, raw: Mapping[str, Any], *,
                    items: Iterable[str]) -> cj.Verdict | str:
    """One posted verdict re-read as `contractjudge.read_judgment` reads an
    answer, or why it is refused (section 9, check 5).

    Refused: a requirement that does not name `judgment` in this revision, a
    verdict or reason outside the closed lists, a probability outside [0, 1],
    an item that was not in the state. Kept but read as `cannot_judge`, as
    the reader does: a `met` citing no item, a `met` or `unmet` under its
    threshold. The thresholds are applied HERE, whatever the worker applied."""
    rid = raw.get("requirement_id")
    if rid not in cj.judged_requirements(contract):
        return "a verdict names a requirement this revision does not judge"
    verdict, reason, item, p = (raw.get("verdict"), raw.get("reason"),
                                raw.get("evidence_item"), raw.get("p"))
    if verdict not in cj.VERDICTS:
        return "a verdict is not met, unmet or cannot_judge"
    if reason is not None and reason not in cj.JUDGMENT_REASONS:
        return "a verdict's reason is not a reason code"
    if isinstance(p, bool) or not isinstance(p, (int, float)) or not 0.0 <= float(p) <= 1.0:
        return "a verdict's probability is not in [0, 1]"
    if item is not None and item not in set(items):
        return "a verdict cites an item that was not in the state"
    p = float(p)
    if verdict == cj.CANNOT_JUDGE or reason is None or (verdict == cj.MET and item is None) \
            or (verdict == cj.MET and p < cj.JUDGMENT_MET_P) \
            or (verdict == cj.UNMET and p < cj.JUDGMENT_UNMET_P):
        return cj.Verdict(str(rid), cj.CANNOT_JUDGE, reason, item, p)
    return cj.Verdict(str(rid), str(verdict), reason, item, p)


def complete(contract: wc.WorkContract, verdicts: Iterable[cj.Verdict]) -> tuple[cj.Verdict, ...]:
    """One verdict per judged requirement, in the contract's order: one not
    posted is `cannot_judge` (missing verification is not a pass)."""
    by_id = {v.requirement_id: v for v in verdicts}
    return tuple(by_id.get(rid, cj.Verdict(rid, cj.CANNOT_JUDGE, None, None, 0.0))
                 for rid in cj.judged_requirements(contract))


def send_back_blocks(contract: wc.WorkContract, verdicts: Iterable[cj.Verdict], *,
                     commit: str, paths: Mapping[str, str]) -> list[dict[str, Any]]:
    """The next attempt's brief: one block per unmet requirement (section 5.4)."""
    text = {r.id: r.text for r in contract.requirements}
    return [{
        "requirement_id": v.requirement_id,
        "requirement": text.get(v.requirement_id),
        "reason": v.reason,
        "evidence_item": v.evidence_item,
        "path": paths.get(v.evidence_item or ""),
        "commit": commit,
        "note": f"Jev judged this requirement unmet at commit {commit}",
    } for v in verdicts if v.verdict == cj.UNMET]


def cited_hunks(verdicts: Iterable[cj.Verdict], hunks: Sequence[str | None],
                changed: Sequence[str]) -> dict[str, str]:
    """`i<n>` -> the hunk text a verdict cites, for `HUNKS_FIELD`.

    Only cited items are kept, so the record holds at most one hunk per judged
    requirement (`contractjudge.JUDGMENT_PER_CALL`) rather than the whole
    diff. Each is sanitized as the evidence path sanitizes what it sends Jev
    (`swarm_jev.sanitize`, context-quality.md section 5.4): the content of a
    denied path (`.env`, `*.tfvars`, under `secrets/`) is never kept, and a
    hunk holding anything credential-shaped is DROPPED whole, never masked,
    because the sanitizer drops it and Jev never read it. What survives is
    cut to `HUNK_CHARS`, as the state cut it, and masked (`redaction.redact`)
    because the record is served to every member who reads the step. An item
    with no text keeps its path alone."""
    out: dict[str, str] = {}
    for v in verdicts:
        item = v.evidence_item or ""
        if not item.startswith("i") or not item[1:].isdigit() or item in out:
            continue
        n = int(item[1:])
        text = hunks[n] if n < len(hunks) else None
        path = changed[n] if n < len(changed) else None
        if not isinstance(text, str) or not text or not isinstance(path, str) \
                or sanitize.is_denied_path(path):
            continue
        text = text[:cj.HUNK_CHARS]
        if sanitize.is_flagged(text):
            continue
        out[item] = redact(text).text
    return out


#: The keys of `contract_acceptance` served to a reader (`served_acceptance`).
#: The lease id and the state's sha256 are the control plane's own and stay
#: behind; everything the approval screen draws is here.
SERVED_KEYS = ("status", "waiting_on", "owner_reason", "attempt_id", "generation",
               "contract_id", "revision", "digest", "commit", "base", "outcome", "reason",
               "call_id", "verdicts", "changed", HUNKS_FIELD, "mechanical", "reask", "at")


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def served_acceptance(raw: Any) -> dict[str, Any] | None:
    """The stored record as the task and the approval detail serve it, times
    as ISO text, or None when the task carries none."""
    if not isinstance(raw, Mapping) or not raw.get("status"):
        return None
    out = _plain({k: raw[k] for k in SERVED_KEYS if k in raw})
    # The stored text goes through the redaction every served text does, so
    # a record written before its hunks were sanitized serves none unmasked.
    if isinstance(out.get(HUNKS_FIELD), Mapping):
        out[HUNKS_FIELD] = {k: redact(v).text for k, v in out[HUNKS_FIELD].items()
                            if isinstance(v, str)}
    return out


def acceptance_of(db: Any, tenant_id: str, task_id: str) -> dict[str, Any] | None:
    """`served_acceptance` of one task of this tenant, one read; None for a
    task that is missing, another tenant's (invariant 9) or never judged."""
    snap = db.collection(TASKS).document(task_id).get()
    data = (snap.to_dict() or {}) if snap.exists else {}
    if data.get("tenant_id") != tenant_id:
        return None
    return served_acceptance(data.get(ACCEPTANCE_FIELD))


def owner_waiting(db: Any, tenant_id: str, *, limit: int) -> list[dict[str, Any]]:
    """The tenant's steps whose acceptance waits on the owner, as stored: the
    approval inbox projects each (`approvals.project_contract`). Read with
    the tenant in the query and checked again here (invariant 9); a record a
    newer attempt superseded is not waiting on anyone."""
    query = (db.collection(TASKS)
             .where(filter=FieldFilter("tenant_id", "==", tenant_id))
             .where(filter=FieldFilter(f"{ACCEPTANCE_FIELD}.status", "==", STATUS_PENDING))
             .where(filter=FieldFilter(f"{ACCEPTANCE_FIELD}.waiting_on", "==", WAITING_OWNER))
             .limit(limit))
    rows = []
    for snap in query.stream():
        task = snap.to_dict() or {}
        acc = task.get(ACCEPTANCE_FIELD) or {}
        if task.get("tenant_id") != tenant_id or acc.get("waiting_on") != WAITING_OWNER \
                or task.get("current_generation", acc.get("generation")) != acc.get("generation"):
            continue
        rows.append({**task, "id": task.get("id") or snap.id})
    return rows


def pending_schedule(now: datetime) -> dict[str, Any]:
    """A fresh re-ask schedule: no try yet, the first due at its offset."""
    return {"tries": 0, "first_at": now, "next_at": now + REASK_OFFSETS[0],
            "deadline": now + REASK_WINDOW, "last": None}


# --- the pass ------------------------------------------------------------------


@dataclass(frozen=True)
class ReaskResult:
    """What one re-ask produced: `judged` with verdicts, or `not_run` with why."""

    outcome: str
    verdicts: tuple[cj.Verdict, ...] = ()
    reason: str | None = None
    call_id: str | None = None
    changed: tuple[str, ...] = ()
    state_sha256: str | None = None
    #: Each sent item's hunk text, in `changed`'s order (None: sent path-only).
    hunks: tuple[str | None, ...] = ()


#: (task, contract, the claimed acceptance record) -> the re-ask's result.
#: Never raises: a judge that cannot ask returns `not_run` with its reason.
Judge = Callable[[Mapping[str, Any], wc.WorkContract, Mapping[str, Any]], ReaskResult]


def _instant(value: Any) -> datetime | None:
    return value if isinstance(value, datetime) else None


def _same(acc: Mapping[str, Any], claimed: Mapping[str, Any]) -> bool:
    keys = ("attempt_id", "generation", "digest", "commit")
    return (all(acc.get(k) == claimed.get(k) for k in keys)
            and acc.get("status") == STATUS_PENDING and acc.get("waiting_on") == WAITING_REASK
            and (acc.get("reask") or {}).get("tries") == (claimed.get("reask") or {}).get("tries"))


@dataclass
class ContractReask:
    """One bounded pass over the due re-asks."""

    db: Any
    settings: Any
    judge: Judge
    now: Callable[[], datetime]
    limit: int = PASS_LIMIT
    report: dict[str, int] = field(default_factory=lambda: {
        "due": 0, "asked": 0, "judged": 0, "owner": 0, "pending": 0, "superseded": 0})

    def _task_ref(self, task_id: str) -> Any:
        return self.db.collection(TASKS).document(task_id)

    def _run(self, body: Callable[[Any], Any]) -> Any:
        return firestore.transactional(body)(self.db.transaction())

    def due(self) -> list[str]:
        """The task ids whose re-ask is due now, oldest due first, at most `limit`.

        A record whose generation a newer attempt has superseded is skipped
        here, before the limit, and nothing is written for it: it keeps its
        old `next_at`, so were it counted it would hold the front of every
        pass and starve every live record of its re-ask and its owner hand-off.
        `_claim` checks the generation again inside its transaction."""
        now = self.now()
        query = (self.db.collection(TASKS)
                 .where(filter=FieldFilter(f"{ACCEPTANCE_FIELD}.status", "==", STATUS_PENDING))
                 .where(filter=FieldFilter(f"{ACCEPTANCE_FIELD}.waiting_on", "==",
                                           WAITING_REASK)))
        rows = []
        for snap in query.stream():
            task = snap.to_dict() or {}
            acc = task.get(ACCEPTANCE_FIELD) or {}
            if task.get("current_generation") != acc.get("generation"):
                self.report["superseded"] += 1
                continue
            due_at = _instant((acc.get("reask") or {}).get("next_at"))
            if due_at is not None and due_at <= now:
                rows.append((due_at, snap.id))
        rows.sort()
        return [task_id for _, task_id in rows[:self.limit]]

    def run(self) -> dict[str, int]:
        """Claim, ask and settle every due record; one record's fault is
        logged and the pass goes on."""
        for task_id in self.due():
            self.report["due"] += 1
            try:
                self._one(task_id)
            except (gexc.GoogleAPIError, ValueError) as exc:
                log.warning("contract re-ask task=%s not settled: %s", task_id,
                            type(exc).__name__)
        return dict(self.report)

    def _one(self, task_id: str) -> None:
        claimed = self._claim(task_id)
        if claimed is None:
            return
        task, contract, acc = claimed
        self.report["asked"] += 1
        try:
            result = self.judge(task, contract, acc)
        except Exception as exc:  # a judge's fault is a try without a verdict, never a pass
            log.warning("contract re-ask task=%s judge raised %s", task_id, type(exc).__name__)
            result = ReaskResult(OUTCOME_NOT_RUN, reason="judge_error")
        self._settle(task_id, acc, contract, result)

    def _claim(self, task_id: str
               ) -> tuple[dict[str, Any], wc.WorkContract, dict[str, Any]] | None:
        """Count the try and schedule the next one BEFORE asking, or hand the
        record to the owner when the tries or the window are spent."""
        now = self.now()
        ref = self._task_ref(task_id)

        def _apply(txn: Any) -> Any:
            snap = _snapshot(txn.get(ref))
            task = (snap.to_dict() or {}) if snap.exists else None
            if task is None:
                return None
            acc = task.get(ACCEPTANCE_FIELD) or {}
            if acc.get("status") != STATUS_PENDING or acc.get("waiting_on") != WAITING_REASK:
                return None
            if task.get("current_generation") != acc.get("generation"):
                # A newer attempt owns the step: this judgment is of a
                # superseded attempt, and nothing is written for it.
                self.report["superseded"] += 1
                return None
            reask = dict(acc.get("reask") or {})
            tries = int(reask.get("tries") or 0)
            first = _instant(reask.get("first_at")) or now
            next_at = _instant(reask.get("next_at"))
            contract = signed_contract(self.settings, task, task_id)
            if isinstance(contract, str) or wc.digest(contract) != acc.get("digest"):
                txn.update(ref, {ACCEPTANCE_FIELD: {
                    **acc, "waiting_on": WAITING_OWNER,
                    "owner_reason": OWNER_CONTRACT_SUPERSEDED, "at": now}})
                self.report["owner"] += 1
                return None
            if tries >= REASK_TRIES or now >= first + REASK_WINDOW:
                txn.update(ref, {ACCEPTANCE_FIELD: {
                    **acc, "waiting_on": WAITING_OWNER,
                    "owner_reason": OWNER_REASK_EXHAUSTED, "at": now}})
                self.report["owner"] += 1
                return None
            if next_at is None or next_at > now:
                return None
            tries += 1
            reask.update(tries=tries, claimed_at=now,
                         next_at=(first + REASK_OFFSETS[tries] if tries < REASK_TRIES
                                  else first + REASK_WINDOW))
            txn.update(ref, {ACCEPTANCE_FIELD: {**acc, "reask": reask}})
            return task, contract, {**acc, "reask": reask}

        return self._run(_apply)

    def _settle(self, task_id: str, claimed: Mapping[str, Any], contract: wc.WorkContract,
                result: ReaskResult) -> None:
        """Write the try's result while the record is still the one claimed;
        otherwise nothing."""
        now = self.now()
        ref = self._task_ref(task_id)
        tenant_id = None

        def _apply(txn: Any) -> None:
            nonlocal tenant_id
            snap = _snapshot(txn.get(ref))
            task = (snap.to_dict() or {}) if snap.exists else {}
            acc = task.get(ACCEPTANCE_FIELD) or {}
            if not _same(acc, claimed) or task.get("current_generation") != acc.get("generation"):
                self.report["superseded"] += 1
                return
            tenant_id = task.get("tenant_id")
            tries = int((acc.get("reask") or {}).get("tries") or 0)
            last = {"try": tries, "at": now, "outcome": result.outcome,
                    "reason": result.reason, "call_id": result.call_id}
            # The whole map, as read in this transaction: never a dotted
            # path, so the record is written exactly as it is checked.
            update: dict[str, Any] = {**acc, "reask": {**(acc.get("reask") or {}), "last": last},
                                      "at": now}
            writes: list[tuple[Any, dict[str, Any]]] = []
            if result.outcome == OUTCOME_JUDGED:
                verdicts = complete(contract, result.verdicts)
                writes = ce.writes(
                    self.db, contract, verdicts, tenant_id=str(tenant_id), task_id=task_id,
                    commit=str(acc["commit"]), attempt_id=str(acc["attempt_id"]),
                    generation=int(acc["generation"]), call_id=result.call_id,
                    source=ce.SOURCE_REASK, paths=item_paths(result.changed),
                    state_sha256=result.state_sha256, at=now)
                update["verdicts"] = [v.to_document() for v in verdicts]
                update["call_id"] = result.call_id
                # The items the re-ask's verdicts cite are its own state's,
                # so the paths and hunks they name are written with them.
                update["changed"] = list(result.changed)
                update[HUNKS_FIELD] = cited_hunks(verdicts, result.hunks, result.changed)
                outcome = cj.judgment_outcome(verdicts)
            else:
                outcome = STATUS_PENDING
            if outcome == STATUS_MET:
                update.update(status=STATUS_MET, waiting_on=None)
                self.report["judged"] += 1
            elif outcome == cj.SENT_BACK:
                update.update(waiting_on=WAITING_OWNER, owner_reason=OWNER_UNMET_AFTER_ATTEMPT)
                self.report["judged"] += 1
                self.report["owner"] += 1
            elif tries >= REASK_TRIES:
                update.update(waiting_on=WAITING_OWNER, owner_reason=OWNER_REASK_EXHAUSTED)
                self.report["owner"] += 1
            else:
                self.report["pending"] += 1
            txn.update(ref, {ACCEPTANCE_FIELD: update})
            for write in writes:
                txn.set(*write)

        self._run(_apply)


# --- the production judge: the published diff, through swarm-api's route -------


def state_digest(state: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode("utf-8")).hexdigest()


#: The arm of the Jev work-contract experiment: the signed dispatch block's
#: `contract_jev` (`contractreview.ARM_SWITCH`), and what an arm-off re-ask
#: records (`contractreview.SKIPPED_ARM`, the worker's post-step's reason).
SKIPPED_ARM = "skipped:arm"


def arm_off(task: Mapping[str, Any]) -> bool:
    """Whether the task's signed dispatch block says `contract_jev: off`."""
    metadata = task.get("metadata") if isinstance(task, Mapping) else None
    dispatch = metadata.get("dispatch") if isinstance(metadata, Mapping) else None
    return isinstance(dispatch, Mapping) and dispatch.get("contract_jev") == "off"


@dataclass
class ForgeJudge:
    """Re-asks Jev from the published diff (`impact.read_diff`, with the
    repository's read token), through the workspace's internal backend at the
    internal load balancer (`judgment.resolve_call_backend`, the owner's
    answer 1). JC2 sends code, so it runs only while the workspace's signed
    `code_intelligence.jev` is on (answer 9 keeps JC2 on it)."""

    ctx: Any

    def __call__(self, task: Mapping[str, Any], contract: wc.WorkContract,
                 acc: Mapping[str, Any]) -> ReaskResult:
        from swarm_jev import JudgeClient

        from .codeintel import CodeIntelligenceStore
        from .jevaccount import ReaskLedger
        from .impact import ImpactRequest, read_diff, resolve_read_token
        from .judgment import (
            INTERNAL_BACKENDS,
            JudgmentSettings,
            NoBackend,
            resolve_call_backend,
            route_address,
            settings_mac,
        )
        from .repositories import repo_id_for
        from .attemptstate import owner_repo

        ctx = self.ctx
        tenant_id = str(task.get("tenant_id"))
        if arm_off(task):
            # The Jev work-contract experiment's arm off (jev-contract.md
            # section 11): the step's own judgment was `skipped:arm`, and so
            # is every re-ask, with nothing read and no call; the record
            # reaches the owner as a workspace that sends no code does.
            return ReaskResult(OUTCOME_NOT_RUN, reason=SKIPPED_ARM)
        if not CodeIntelligenceStore(ctx.db, now=ctx.now,
                                     mac=settings_mac(ctx.settings)).get(tenant_id).jev:
            return ReaskResult(OUTCOME_NOT_RUN, reason="not_opted_in")
        chosen = JudgmentSettings(ctx.db, now=ctx.now, mac=settings_mac(ctx.settings)) \
            .read(tenant_id)["judgment_backend"]
        if chosen not in INTERNAL_BACKENDS:
            return ReaskResult(OUTCOME_NOT_RUN, reason="backend_not_routed")
        backend = resolve_call_backend(
            chosen, internal_url=getattr(ctx.settings, "internal_decision_api_url", ""),
            address=route_address(), typesafe_key=None)
        if isinstance(backend, NoBackend):
            return ReaskResult(OUTCOME_NOT_RUN, reason=backend.code)
        named = owner_repo(task.get("repository_url"))
        base, head = acc.get("base"), acc.get("commit")
        if named is None or not isinstance(base, str) or not wc._SHA.match(base):
            return ReaskResult(OUTCOME_NOT_RUN, reason="no_published_diff")
        tenant = ctx.store.get_tenant(tenant_id)
        if tenant is None or ctx.forge is None or ctx.forge_tokens is None:
            return ReaskResult(OUTCOME_NOT_RUN, reason="no_forge")
        owner, repo = named
        record = {"owner": owner, "repo": repo, "repo_id": repo_id_for(tenant_id, owner, repo)}
        try:
            token, _label = resolve_read_token(ctx.db, tenant, tenant, record["repo_id"],
                                               tokens=ctx.forge_tokens, now=ctx.now())
            diff = read_diff(record, ImpactRequest(base=base, head=head), token,
                             forge=ctx.forge)
        except Exception as exc:  # the forge's fault is a try without a verdict
            return ReaskResult(OUTCOME_NOT_RUN, reason=f"forge_{type(exc).__name__}"[:64])
        finally:
            token = ""
        changed = [{"path": f.path, "status": f.status, "hunk": f.patch} for f in diff.files]
        state = cj.judgment_state(contract, commit=str(head), changed=changed,
                                  mechanical=list(acc.get("mechanical") or []))
        # Counted in the workspace's monthly jev_usage and admitted against
        # its jev_monthly_cap_tokens (the owner's answer, 4 November 2026).
        ledger = ReaskLedger(ctx.db, ctx.settings, ctx.now, tenant_id=tenant_id,
                             task_id=str(task.get("id")),
                             generation=int(acc["generation"]),
                             attempt_id=acc.get("attempt_id"))
        decision = JudgeClient(backend=backend, ledger=ledger,
                               transport=backend.transport()).decide(
            JUDGMENT_KIND, state, cj.judgment_questions(state))
        sent = tuple(row["path"] for row in state["changed"])
        hunks = tuple(row.get("hunk") for row in state["changed"])
        if not decision.ok:
            return ReaskResult(OUTCOME_NOT_RUN, reason=decision.reason or decision.outcome,
                               call_id=decision.call_id or None, changed=sent)
        return ReaskResult(OUTCOME_JUDGED,
                           verdicts=cj.read_judgment(contract, state, decision.answers),
                           call_id=decision.call_id or None, changed=sent,
                           state_sha256=state_digest(state), hunks=hunks)


def run_pass(ctx: Any, *, judge: Judge | None = None) -> dict[str, int]:
    """The scheduled tick's entry point: one bounded pass."""
    return ContractReask(db=ctx.db, settings=ctx.settings, judge=judge or ForgeJudge(ctx),
                         now=ctx.now).run()
