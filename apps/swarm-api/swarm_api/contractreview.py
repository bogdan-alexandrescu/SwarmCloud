"""Jev's review of a work contract at submission (JC1; part of #2688).

The owner chose on 3 November 2026 (BGD) to have Jev review a work contract
when it is submitted and flag the requirements no worker could enforce,
before anything runs. The design is docs/design/agent-quality/jev-contract.md
section 4, with the owner's answers of that day (its section 14), which this
module builds to:

* **Its own mode (answer 7).** `SWARM_CONTRACT_REVIEW=off|report|enforce`,
  read at each submission, OUTSIDE `refusals.py` (whose switch is keyed on the
  error's code, and this refusal's code is the established
  `validation_failed`). `off` -- the default, and what anything else reads as
  -- runs no review and records nothing: the submission is exactly what it was
  before this module. `report` reviews, records the review in the step's
  signed dispatch block, and LOGS the 422 it would have made and the
  amendment it would have made, changing nothing: a review whose refusal is
  only reported must not change the contract either. `enforce` refuses and
  amends.
* **Its own opt-in (answer 9).** A new signed workspace setting,
  `workspace_settings/{tenant}.contract_review.enabled`, gates the call, not
  `code_intelligence.jev`: a workspace can have its contracts reviewed without
  sending code. Off (the default), nothing is sent to Jev; the review is
  recorded `not_run` with reason `not_opted_in`. The block carries
  swarm-api's MAC (`judgment.block_mac`, block `contract_review`, listed in
  `judgment.API_SETTINGS_MAC_FIELDS` until the frozen
  `SETTINGS_MAC_FIELDS` lists it, contract request 138); for a tenant cut over in
  SETTINGS_MAC_TENANTS a block that does not verify reads as off.
* **Open on failure (answer 2).** No route, no key, a timeout, a 429 or 5xx,
  a malformed answer, the month over its cap: the contract is accepted on the
  mechanical compile alone and the review is recorded `not_run` with the
  client's reason. A strict contract is exactly as enforceable as it was; a
  failed call never makes anything advisory.
* **Amended automatically (answer 3).** A requirement Jev classes `judgeable`
  at p >= 0.6 that does not name `judgment` gets it, as revision 2 by the
  `reviewer` (`contractjudge.review_amendment`, through
  `workcontract.amend(author="reviewer")`, which refuses any other change).
* **Refused as section 4.5 says.** A strict contract with a requirement
  classed `unenforceable` at p >= 0.6 is a 422 `validation_failed` whose
  `detail` is `contractjudge.refusal_detail`: `detail.reason` is the
  contract's own `work_contract_not_enforceable`, and `detail.jev` holds
  Jev's closed values only, never a requirement's text.

ONE CALL INSIDE THE REQUEST. The whole review has `DEADLINE_SECONDS` (15 s)
and one retry, not the client's two: a transport that refuses to dial past
the deadline, and a sleep that never waits past it, wrap the shared
`swarm_jev.JudgeClient`. The backend is the workspace's `judgment_backend`,
resolved for ONE call by `judgment.resolve_call_backend` (Direct VPC egress to
the internal load balancer in front of the tailnet egress, answer 1), never a
fallback to TypeSafe.

THE MONTH PAYS (section 7). The review's calls are admitted only while the
month's spend is under `code_intelligence.jev_monthly_cap_tokens` and are
charged to it, kind `contract_review`, in a row of their own
(`jevusage.record_contract_review`). A failed write is logged by type and
never fails the submission: accounting never refuses work.

WHAT IS SENT. `contractjudge.review_state`: the contract's own text by index,
its strictness, acceptance, artifacts and write scope, through the shared
sanitizer. Never the tenant, the owner, the contract id, the repository URL
or a commit. A requirement whose text the sanitizer would drop is not asked
about at all and is recorded `not_judged` (`unreadable`). Nothing Jev returns is acted on but its closed choices.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from google.cloud import firestore
from pydantic import StrictBool
from swarm_jev import sanitize
from swarm_jev.client import Decision, JudgeClient, Transport

from swarm_common.admission import _snapshot
from swarm_common.models import Tenant

from . import contractjudge as cj
from . import jevusage
from . import judgment
from . import workcontract as wc
from .admins import AUDIT_COLLECTION
from .auth import AuthContext
from .errors import RoleAdminRequired, ValidationFailed
from .qasettings import authorize_read, can_edit
from .roles import ADMIN, RoleStore
from .schemas import StrictModel

logger = logging.getLogger(__name__)

# -- the mode (the owner's answer 7) -------------------------------------------

MODE_ENV = "SWARM_CONTRACT_REVIEW"
MODE_OFF = "off"
MODE_REPORT = "report"
MODE_ENFORCE = "enforce"
MODES = (MODE_OFF, MODE_REPORT, MODE_ENFORCE)


def mode(environ: Mapping[str, str] | None = None) -> str:
    """The review's mode: exactly `report` or `enforce`, else `off`. A typo
    reads as off, the shipped state, never as enforce. The read is written
    `(os.environ if environ is None else environ).get(...)` so that
    scripts/lib/check-env-parity.sh sees swarm-api read the key terraform sets."""
    value = ((os.environ if environ is None else environ).get(MODE_ENV) or "").strip().lower()
    return value if value in MODES else MODE_OFF


# -- the workspace setting (the owner's answer 9) ------------------------------

COLLECTION = judgment.SETTINGS_COLLECTION
FIELD = "contract_review"
ENABLED = "enabled"
SET_AT = "set_at"
SET_BY = "set_by"
AUDIT_ACTION = "workspace_contract_review_set"
#: The block's name in `judgment.API_SETTINGS_MAC_FIELDS`.
MAC_BLOCK = FIELD
OWNED_FIELDS = (ENABLED, SET_AT, SET_BY, judgment.MAC_FIELD)
#: Off until the workspace's owner or admin turns it on: a contract's text
#: leaves the platform for the workspace's Jev backend only when they chose so.
DEFAULT_ENABLED = False


class ContractReviewPut(StrictModel):
    #: StrictBool: "true" and 1 are refused, not read as on.
    enabled: StrictBool


def stored_enabled(doc: Mapping[str, Any] | None, *, tenant_id: str | None = None,
                   mac: judgment.SettingsMac | None = None) -> bool:
    """True only when the stored block says `enabled: true` (and, with `mac`
    and `tenant_id`, for a tenant cut over, its MAC verifies)."""
    block = doc.get(FIELD) if isinstance(doc, Mapping) else None
    if not isinstance(block, Mapping):
        return False
    if mac is not None and tenant_id:
        block = judgment.trusted_block(tenant_id, MAC_BLOCK, block, mac)
    return block.get(ENABLED) is True


def authorize_write(auth: AuthContext, tenant: Tenant | None, tenant_id: str,
                    roles: RoleStore | None) -> Tenant:
    """The workspace, when the caller may change its contract review: its
    owner, the group's admin (read fresh) or a platform admin. 404 for one
    they are not in, 403 for a member who is not its admin."""
    tenant = authorize_read(auth, tenant, tenant_id)
    if not can_edit(auth, tenant, roles, fresh=True):
        raise RoleAdminRequired(
            "Only the workspace's owner or admin, or a platform admin, may change whether its "
            "work contracts are reviewed. Nothing was changed.",
            detail={"tenant_id": tenant_id, "required": ADMIN})
    return tenant


class ContractReviewStore:
    """`workspace_settings/{tenant}.contract_review`, read and audited, signed write."""

    def __init__(self, db: Any, *, now: Callable[[], datetime],
                 mac: judgment.SettingsMac | None = None) -> None:
        self._db = db
        self._now = now
        self._mac = mac if mac is not None else judgment.SettingsMac()

    @property
    def mac(self) -> judgment.SettingsMac:
        return self._mac

    def _ref(self, tenant_id: str) -> Any:
        return self._db.collection(COLLECTION).document(tenant_id)

    def document(self, tenant_id: str) -> dict[str, Any]:
        snap = self._ref(tenant_id).get()
        return (snap.to_dict() or {}) if snap.exists else {}

    def put(self, tenant_id: str, enabled: bool, *, by: str) -> None:
        """Store `enabled`, signed, and its audit in one transaction. The same
        value again writes nothing, unless the stored block does not verify
        under the current key: then it is re-signed, with no audit row."""
        ref = self._ref(tenant_id)
        audit = self._db.collection(AUDIT_COLLECTION).document()
        now = self._now()
        keys = self._mac.keys

        @firestore.transactional
        def _write(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            whole = (snap.to_dict() or {}) if snap.exists else None
            previous = stored_enabled(whole, tenant_id=tenant_id, mac=self._mac)
            stored = (whole or {}).get(FIELD)
            stored = dict(stored) if isinstance(stored, Mapping) else {}
            signed = not keys or judgment.block_verifies(tenant_id, MAC_BLOCK, stored, keys[:1])
            if previous == enabled and signed:
                return
            block = {k: v for k, v in stored.items() if k not in OWNED_FIELDS}
            block.update({ENABLED: enabled, SET_AT: now, SET_BY: by})
            mac = judgment.block_mac(tenant_id, MAC_BLOCK, block, keys)
            if mac is not None:
                block[judgment.MAC_FIELD] = mac
            if whole is None:
                txn.set(ref, {"tenant_id": tenant_id, FIELD: block})
            else:
                # The whole map, so a MAC made over other values never stays
                # beside the new ones.
                txn.update(ref, {FIELD: block})
            if previous == enabled:
                return
            txn.set(audit, {
                "action": AUDIT_ACTION,
                "target_tenant_id": tenant_id,
                "by": by,
                "at": now,
                "detail": {"tenant_id": tenant_id, "setting": f"{FIELD}.{ENABLED}",
                           "from": previous, "to": enabled},
            })

        _write(self._db.transaction())


def view(tenant_id: str, doc: Mapping[str, Any], *, editable: bool,
         mac: judgment.SettingsMac | None = None,
         environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """What GET and PUT answer: the setting in force, its default, the
    platform's mode (so a person sees that `off` reviews nothing whatever the
    setting says), who set it and whether the caller may change it."""
    block = doc.get(FIELD) if isinstance(doc.get(FIELD), Mapping) else {}
    set_at = block.get(SET_AT)
    set_by = block.get(SET_BY)
    return {
        "tenant_id": tenant_id,
        FIELD: {ENABLED: stored_enabled(doc, tenant_id=tenant_id, mac=mac)},
        "defaults": {FIELD: {ENABLED: DEFAULT_ENABLED}},
        "mode": mode(os.environ if environ is None else environ),
        "set_at": set_at.isoformat() if isinstance(set_at, datetime) else None,
        "set_by": set_by if isinstance(set_by, str) else None,
        "can_edit": editable,
    }


# -- the review ----------------------------------------------------------------

#: The whole review's deadline inside the submission request (section 7).
DEADLINE_SECONDS = 15.0
#: One try and one retry per call, not the client's three tries.
ATTEMPTS = 2
#: The `kind` of the review's calls in the ledger rows and in `jev_usage`.
KIND = jevusage.CONTRACT_REVIEW_KIND
#: The key of the signed dispatch block the review record is stored under,
#: beside the contract's reference (never inside its canonical bytes).
DISPATCH_FIELD = "contract_review"

#: `not_run` reasons this module adds to the client's own.
NOT_OPTED_IN = "not_opted_in"
DEADLINE = "deadline"
ERROR = "error"


@dataclass(frozen=True)
class Reviewed:
    """What the review makes of one step's prepared contract: the contract
    the step carries (revision 2 when the reviewer amended it) and the record
    to store in its signed dispatch block (None when the mode is off or the
    contract has no requirement to review)."""

    prepared: wc.Prepared
    record: dict[str, Any] | None


class _Ledger:
    """The review's admission and audit (`swarm_jev.JevLedger`): a call is
    admitted while the month's spend plus its estimate is under the cap, and
    every call's audit row is kept for the month's charge."""

    def __init__(self, *, used: int, cap: int | None) -> None:
        self._used = used
        self._cap = cap
        self.rows: list[dict[str, Any]] = []

    def admit(self, *, estimate: int) -> str | None:
        if self._cap is not None and self._used + estimate > self._cap:
            return "budget_monthly"
        return None

    def record(self, row: dict[str, Any], *, succeeded: bool | None = None) -> bool:
        self.rows.append(dict(row))
        self._used += int(row.get("input_tokens") or 0)
        return False


def bounded(transport: Transport, *, deadline_at: float, clock: Callable[[], float],
            attempts: int = ATTEMPTS) -> Transport:
    """`transport` that never dials more than `attempts` times per call --
    past them it answers the last answer again, so the client's reason is the
    backend's own (`http_503`) -- and never past `deadline_at`, nor with a
    timeout longer than what is left of it."""
    dialled = [0]
    last: list[tuple[int, bytes]] = []

    def call(url: str, body: bytes, headers: Mapping[str, str],
             timeout: float) -> tuple[int, bytes]:
        dialled[0] += 1
        if dialled[0] > attempts and last:
            return last[-1]
        left = deadline_at - clock()
        if dialled[0] > attempts or left <= 0:
            raise TimeoutError("the contract review's deadline or retry is spent")
        last.append(transport(url, body, headers, min(timeout, left)))
        return last[-1]

    return call


def _log(event: str, **fields: Any) -> None:
    logger.warning(json.dumps({"event": event, **fields}, sort_keys=True, default=str))


def _read_secret_manager(project_id: str) -> Callable[[str], str | None]:
    def read(secret_id: str) -> str | None:
        from google.cloud import secretmanager  # lazy: only a TypeSafe workspace reads it

        name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
        version = secretmanager.SecretManagerServiceClient().access_secret_version(
            request={"name": name})
        return bytes(version.payload.data).decode("utf-8")

    return read


class ContractReviewer:
    """JC1 for one deployment. Built per submission service; holds no client
    until a review runs, and reads its mode and route from `environ` at each
    review, never at import."""

    def __init__(self, db: Any, *, mac: judgment.SettingsMac, now: Callable[[], datetime],
                 project_id: str = "", environ: Mapping[str, str] | None = None,
                 transport: Transport | None = None,
                 read_secret: Callable[[str], str | None] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 log: Callable[..., None] = _log) -> None:
        self._db = db
        self._mac = mac
        self._now = now
        self._environ = environ
        self._transport = transport
        self._read_secret = read_secret or _read_secret_manager(project_id)
        self._clock = clock
        self._sleep = sleep
        self._log = log

    @property
    def environ(self) -> Mapping[str, str]:
        return os.environ if self._environ is None else self._environ

    def _settings_doc(self, tenant_id: str) -> dict[str, Any]:
        if self._db is None:
            return {}
        snap = self._db.collection(COLLECTION).document(tenant_id).get()
        return (snap.to_dict() or {}) if snap.exists else {}

    def _backend(self, tenant_id: str, submitter_tenant_id: str | None,
                 doc: Mapping[str, Any]) -> judgment.CallBackend | judgment.NoBackend:
        top = judgment.trusted_block(tenant_id, judgment.TOP_BLOCK, doc, self._mac)
        name = judgment.stored_backend(top)
        key = None
        if name == judgment.TYPESAFE_BACKEND:
            # The submitter's own key, then the workspace's: each tried in
            # turn, so a submitter with none still reaches the workspace's.
            from .provider_keys import TYPESAFE, secret_name_for

            for owner in (submitter_tenant_id, tenant_id):
                if not owner:
                    continue
                key = judgment.typesafe_call_key(
                    tenant_id, submitter_tenant_id,
                    {secret_name_for(owner, TYPESAFE)}, self._read_secret)
                if key:
                    break
        environ = self.environ
        return judgment.resolve_call_backend(
            name, internal_url=environ.get("INTERNAL_DECISION_API_URL", ""),
            address=judgment.route_address(
                environ.get(judgment.INTERNAL_DECISION_API_ADDRESS_ENV, "")),
            typesafe_key=key)

    def _month_used(self, tenant_id: str) -> int:
        month = self._now().strftime("%Y-%m")
        return jevusage.JevUsageReader(self._db).month_used(tenant_id, month)

    def _charge(self, tenant_id: str, contract: wc.WorkContract,
                rows: list[dict[str, Any]]) -> None:
        sent = [r for r in rows if not str(r.get("outcome", "")).startswith("skipped:")]
        tokens = sum(int(r.get("input_tokens") or 0) for r in rows)
        outcomes: dict[str, int] = {}
        for r in rows:
            outcome = str(r.get("outcome") or "")
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        output = [r["output_tokens"] for r in rows if isinstance(r.get("output_tokens"), int)]
        try:
            jevusage.record_contract_review(
                self._db, tenant_id, contract_id=contract.contract_id,
                revision=contract.revision, calls=len(sent), input_tokens=tokens,
                outcomes=outcomes, output_tokens=sum(output) if output else None,
                at=self._now())
        except Exception as exc:  # noqa: BLE001 -- accounting never fails a submission
            self._log("contract_review_charge_failed", error=type(exc).__name__,
                      calls=len(sent), input_tokens=tokens)

    def _ask(self, tenant_id: str, contract: wc.WorkContract,
             backend: judgment.CallBackend,
             cap: int | None) -> tuple[tuple[cj.ReviewClass, ...] | None, str | None, str]:
        """Jev's classes for every requirement, or None and why not; with the
        first call's id."""
        # Section 4.2: a requirement whose text the shared sanitizer would
        # drop is never asked about -- Jev would answer for a text it never
        # read -- and is recorded `not_judged`, never enforceable.
        unread = unreadable(contract, backend)
        classes: list[cj.ReviewClass] = [
            cj.ReviewClass(r.id, k, cj.NOT_JUDGED, None, 0.0)
            for k, r in enumerate(contract.requirements) if r.id in unread]
        asked = [rid for rid in contract.requirement_ids() if rid not in unread]
        if not asked:
            return tuple(classes), None, ""
        ledger = _Ledger(used=self._month_used(tenant_id), cap=cap)
        deadline_at = self._clock() + DEADLINE_SECONDS
        raw = self._transport or backend.transport()
        call_id = ""
        why: str | None = None
        try:
            for batch in cj.batches(asked, cj.REVIEW_PER_CALL):
                client = JudgeClient(
                    backend=backend, ledger=ledger,
                    transport=bounded(raw, deadline_at=deadline_at, clock=self._clock),
                    sleep=lambda s: self._sleep(max(0.0, min(s, deadline_at - self._clock()))),
                    log=self._log, clock=self._clock)
                state = cj.review_state(contract, batch)
                decision: Decision = client.decide(KIND, state, cj.review_questions(state))
                call_id = call_id or decision.call_id
                if not decision.ok:
                    why = DEADLINE if self._clock() >= deadline_at else (decision.reason
                                                                         or decision.outcome)
                    break
                classes.extend(cj.read_review(contract, state, decision.answers))
        except Exception as exc:  # noqa: BLE001 -- open on any failure (answer 2)
            self._log("contract_review_failed", error=type(exc).__name__)
            why = ERROR
        if ledger.rows:
            self._charge(tenant_id, contract, ledger.rows)
        return (None if why else tuple(sorted(classes, key=lambda c: c.index))), why, call_id

    def review(self, prepared: wc.Prepared, *, tenant_id: str,
               submitter_tenant_id: str | None, step_id: str | None,
               enforced: frozenset[str] = wc.WORKER_ENFORCED_TODAY) -> Reviewed:
        """Review one step's prepared contract (after `workcontract.prepare`,
        so a contract code already refused never reaches Jev). Raises
        ValidationFailed only in `enforce` mode, for section 4.5's refusal."""
        current = mode(self.environ)
        contract = prepared.contract
        if current == MODE_OFF or not contract.requirements:
            return Reviewed(prepared, None)
        at = self._now().isoformat()
        doc = self._settings_doc(tenant_id)
        if not stored_enabled(doc, tenant_id=tenant_id, mac=self._mac):
            record = cj.review_record(contract, None, call_id="", backend="", at=at,
                                      not_run_reason=NOT_OPTED_IN)
            return Reviewed(prepared, {**record, "mode": current})
        backend = self._backend(tenant_id, submitter_tenant_id, doc)
        if isinstance(backend, judgment.NoBackend):
            record = cj.review_record(contract, None, call_id="", backend=backend.backend,
                                      at=at, not_run_reason=backend.code)
            return Reviewed(prepared, {**record, "mode": current})
        code_intelligence = judgment.trusted_block(
            tenant_id, "code_intelligence", doc.get("code_intelligence"), self._mac)
        cap = jevusage.cap_of(code_intelligence)
        # JC2 sends code, so it is gated on `code_intelligence.jev` (answer 9),
        # and runs only where the worker's post-step runs it at all.
        jc2_runs = wc.JC2_AT_END_OF_STEP and code_intelligence.get("jev") is True
        classes, why, call_id = self._ask(tenant_id, contract, backend, cap)
        if classes is None:
            record = cj.review_record(contract, None, call_id=call_id, backend=backend.name,
                                      at=at, not_run_reason=why or ERROR)
            self._log("contract_review_not_run", reason=why, contract_revision=contract.revision,
                      mode=current)
            return Reviewed(prepared, {**record, "mode": current})
        outcome = cj.review_outcome(contract, classes)
        record = {**cj.review_record(contract, outcome, call_id=call_id, backend=backend.name,
                                     at=at), "mode": current}
        if outcome.outcome == cj.REVIEW_REFUSED:
            refusal = refuse(contract, outcome, call_id=call_id, backend=backend.name)
            if current == MODE_ENFORCE:
                raise refusal
            self._log("contract_review_would_refuse", status=refusal.status_code,
                      body=refusal.to_payload())
            return Reviewed(prepared, record)
        if outcome.outcome == cj.REVIEW_AMENDED:
            if current != MODE_ENFORCE:
                self._log("contract_review_would_amend", contract_revision=contract.revision,
                          add_judgment=list(outcome.add_judgment))
                return Reviewed(prepared, record)
            return self._amend(prepared, outcome, record, call_id=call_id,
                               backend=backend.name, step_id=step_id, enforced=enforced,
                               jc2_runs=jc2_runs)
        return Reviewed(prepared, record)

    def _amend(self, prepared: wc.Prepared, outcome: cj.Review, record: dict[str, Any], *,
               call_id: str, backend: str, step_id: str | None,
               enforced: frozenset[str], jc2_runs: bool) -> Reviewed:
        """Revision 2 by the reviewer. `judgment` counts as enforced only
        where JC2 runs at the end of the step (`jc2_runs`, the owner's pick
        "Gate on JC2"), never merely because this review answered. A strict
        revision 2 that names a component no worker enforces, `judgment`
        included while JC2 does not run, is refused, as the mechanical compile
        refuses revision 1: a strict contract never silently degrades."""
        amendment = cj.review_amendment(prepared.contract, outcome)
        if amendment is None:
            return Reviewed(prepared, record)
        revised = amendment.contract
        try:
            compiled = wc.compile_contract(revised, step_id=step_id or "",
                                           enforced=wc.enforced_with_judgment(
                                               enforced, jc2_runs=jc2_runs))
        except wc.WorkContractError as exc:
            detail = {
                "reason": exc.code,
                "contract_id": revised.contract_id,
                "revision": revised.revision,
                "digest": wc.digest(revised),
                "unenforced": list(exc.detail.get("unenforced") or []),
                "jev": {"call_id": call_id, "backend": backend,
                        "requirements": [c.to_document() for c in outcome.classes
                                         if c.requirement_id in set(outcome.add_judgment)]},
            }
            raise ValidationFailed(
                f"contract {revised.contract_id} is strict, and Jev found requirements only a "
                f"judgment could prove ({', '.join(outcome.add_judgment)}), which no worker "
                f"enforces with {', '.join(detail['unenforced'])}", detail=detail) from exc
        metadata = dict(prepared.metadata)
        metadata[wc.METADATA_KEY] = revised.to_document()
        amended = replace(prepared, contract=revised, compiled=compiled, metadata=metadata)
        record = {**record, "amended_to": {"revision": revised.revision,
                                           "digest": wc.digest(revised),
                                           "previous_digest": revised.previous_digest,
                                           "changed": list(amendment.changed)}}
        return Reviewed(amended, record)


def unreadable(contract: wc.WorkContract, backend: judgment.CallBackend) -> frozenset[str]:
    """The requirements whose text the call's sanitizer drops: credential-
    shaped, or holding the backend's own key (`swarm_jev.sanitize.is_flagged`,
    the check `JudgeClient.decide` applies to every string it sends)."""
    held = (backend.key,) if backend.key else ()
    return frozenset(r.id for r in contract.requirements if sanitize.is_flagged(r.text, held))


def refuse(contract: wc.WorkContract, outcome: cj.Review, *, call_id: str,
           backend: str) -> ValidationFailed:
    """Section 4.5's 422: `validation_failed`, the message naming the
    requirements, `detail` exactly `contractjudge.refusal_detail`."""
    return ValidationFailed(
        f"contract {contract.contract_id} is strict, and Jev found requirements no worker "
        f"could enforce: {', '.join(outcome.unenforceable)}",
        detail=cj.refusal_detail(contract, outcome, call_id=call_id, backend=backend))


__all__ = [
    "AUDIT_ACTION",
    "ContractReviewPut",
    "ContractReviewStore",
    "ContractReviewer",
    "DEADLINE_SECONDS",
    "DEFAULT_ENABLED",
    "DISPATCH_FIELD",
    "ENABLED",
    "FIELD",
    "KIND",
    "MAC_BLOCK",
    "MODE_ENFORCE",
    "MODE_ENV",
    "MODE_OFF",
    "MODE_REPORT",
    "Reviewed",
    "authorize_write",
    "bounded",
    "mode",
    "refuse",
    "stored_enabled",
    "unreadable",
    "view",
]
