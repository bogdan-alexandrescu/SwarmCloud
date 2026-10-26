"""The per-agent budget: the workspace default, the platform ceiling, the step's own, and how they resolve (BUDGET-CAP, part of #2459).

BGD's decisions, 2026-10-26 (#2459):

* A WORKSPACE ADMIN sets a per-agent budget on the workspace, a token count
  or a dollar amount, the unit chosen per workspace: `budget: {unit, amount}`
  in `workspace_settings/{tenant_id}`. Absent by default, and absent means no
  limit: a workspace that sets nothing runs exactly as before.
* A PLATFORM ADMIN sets a ceiling no workspace default can exceed:
  `agent_budget_ceiling: {usd, tokens}` in the platform settings document
  (`control/settings`, `store.PLATFORM_SETTINGS_DEFAULTS`), either or both
  set. A workspace default above the ceiling in its own unit is refused on
  save, `budget_above_ceiling`, the ceiling in the message.
* A STEP (a task, a batch entry or a workflow step) may carry `budget:
  {usd: N}` or `{tokens: N}`, the way it carries `timeout_seconds`. It may
  only LOWER the limit: one above the workspace default or the ceiling in
  the same unit is refused at submission, 422 `budget_above_limit`, naming
  both. It chooses no resource (invariant 10).
* An attempt's budget is min(step, workspace, ceiling), each unit on its
  own: with a step in dollars and a workspace default in tokens, both are
  limits and each is checked.

BOTH REFUSALS SHIP REPORT-ONLY (`refusals.SWITCHES`, the owner's rule of
2026-10-08 for every new refusal): `REFUSAL_BUDGET_ABOVE_LIMIT=on` and
`REFUSAL_BUDGET_ABOVE_CEILING=on` turn them on. Off loses nothing the owner
asked for, because the min holds either way: a step that asks for more runs
under the lower limit, and a default above the ceiling is capped by it at
every resolution. Off, the caller is only told later -- by the limit the
task's `dispatch.budget` shows.

RESOLVED AT SUBMISSION, PINNED IN THE SIGNED SPEC. `resolve` runs in
`SubmissionService._build_task`, for every task, batch entry, workflow step
and child, and its answer is written into `metadata.dispatch.budget`, which
the spec signature covers (`swarm_common.specsign.SIGNED_METADATA_KEYS`).
Every attempt of the task enforces that pinned figure: a worker reads no key
the signature does not cover, and the workspace document is one a tenant
worker can write (below). A change to the workspace default or the ceiling
applies to the next submission, as `max_additional_repositories` does.

A STOPPED STEP'S RESUME. The limit is per ATTEMPT: the worker counts the
attempt's own usage, so a continuation of a step that ended
`budget_exceeded` gets the budget its own submission resolved, from zero.

THE CEILING ALONE TURNS NOTHING ON. With no workspace default and no step
budget there is no budget at all, whatever the ceiling says ("default: off
until a workspace admin sets one"); once either is set, the ceiling is one of
the limits in the min. A ceiling lowered under a default saved earlier still
binds that way: the min takes it.

NOT YET MAC'D, AND WHAT THAT BOUNDS. The settings MAC covers only the blocks
the FROZEN `swarm_common.specsign.SETTINGS_MAC_FIELDS` lists; a `budget`
block is contract request 119, filed by this lane, not made. This module
signs and verifies the block the moment the contract lists it (`SIGNED`), and
until then reads it as `qasettings` reads its value: acted on only when it is
a shape this module could have written. A tenant worker that writes another
workspace's document directly can therefore remove or change that
workspace's default within the ceiling. Bounded twice: a step's own budget is
inside the spec signature and cannot be forged, and the ceiling lives in the
platform's document, not the workspace's.

Nothing here takes or returns a credential, an image, a command or a
resource spec (invariant 10). Nothing here holds capacity (invariant 1).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

from google.cloud import firestore
from pydantic import StrictStr, model_validator

from swarm_common.admission import _snapshot
from swarm_common.models import Tenant
from swarm_common.specsign import SETTINGS_MAC_FIELDS

from .admins import AUDIT_COLLECTION
from .auth import AuthContext
from .errors import RoleAdminRequired, ValidationFailed
from .judgment import MAC_FIELD, SETTINGS_COLLECTION, SettingsMac, block_mac, trusted_block
from .qasettings import authorize_read, can_edit
from .refusals import refuse
from .roles import ADMIN, RoleStore
from .schemas import BUDGET_MAX_TOKENS, BUDGET_MAX_USD, StepBudget, StrictModel

COLLECTION = SETTINGS_COLLECTION
#: The map in `workspace_settings/{tenant_id}`, and the block name contract
#: request 119 asks `SETTINGS_MAC_FIELDS` to sign it under.
FIELD = "budget"
MAC_BLOCK = "budget"
#: The keys of the block a reader acts on (request 119's covered fields).
COVERED = ("unit", "amount")
#: True once the frozen contract lists the block: from then on every write is
#: signed and every read for a tenant on the MAC cutover verified, with no
#: change here. Until then `canonical_settings_block` refuses the block name.
SIGNED = MAC_BLOCK in SETTINGS_MAC_FIELDS
AUDIT_ACTION = "agent_budget_set"

#: The platform setting (`store.PLATFORM_SETTINGS_DEFAULTS`).
CEILING_SETTING = "agent_budget_ceiling"
#: Where the resolved budget is pinned: `metadata.dispatch.budget`, read by
#: the worker as `agent_worker.budget.DISPATCH_KEY` (a test holds them equal).
DISPATCH_KEY = "budget"

USD = "usd"
TOKENS = "tokens"
UNITS = (USD, TOKENS)

#: The largest amount any of the three may name (`schemas`, which says why).
MAX_USD = BUDGET_MAX_USD
MAX_TOKENS = BUDGET_MAX_TOKENS

#: The refusals, each with its own code a caller can branch on.
ABOVE_LIMIT = "budget_above_limit"
ABOVE_CEILING = "budget_above_ceiling"


class BudgetAboveLimit(ValidationFailed):
    """A step's budget is above the workspace default or the platform ceiling."""

    code = ABOVE_LIMIT


class BudgetAboveCeiling(ValidationFailed):
    """A workspace default is above the platform ceiling in its unit."""

    code = ABOVE_CEILING


@dataclass(frozen=True)
class Limits:
    """A budget in either unit or both; None is "no limit in that unit"."""

    usd: float | None = None
    tokens: int | None = None

    @property
    def empty(self) -> bool:
        return self.usd is None and self.tokens is None

    def as_dict(self) -> dict[str, Any]:
        """Only the units set, so `{}` is "no limit" and a one-unit budget
        reads as the caller wrote it."""
        out: dict[str, Any] = {}
        if self.usd is not None:
            out[USD] = self.usd
        if self.tokens is not None:
            out[TOKENS] = self.tokens
        return out


NONE = Limits()


def amount_problem(unit: Any, amount: Any) -> str | None:
    """Why `amount` is not a budget in `unit`, or None when it is one."""
    if unit not in UNITS:
        return f"a budget's unit is one of {', '.join(UNITS)}"
    if isinstance(amount, bool):
        return "a budget's amount is a number, not a boolean"
    if unit == TOKENS:
        if not isinstance(amount, int) or not 1 <= amount <= MAX_TOKENS:
            return f"a token budget is a whole number from 1 to {MAX_TOKENS:,}"
        return None
    if not isinstance(amount, (int, float)) or not math.isfinite(amount) \
            or not 0 < amount <= MAX_USD:
        return f"a dollar budget is a number above 0 and at most {MAX_USD:,.0f}"
    return None


def _limits(unit: str, amount: Any) -> Limits:
    return Limits(usd=float(amount)) if unit == USD else Limits(tokens=int(amount))


def stored_default(block: Mapping[str, Any] | None) -> Limits:
    """The workspace default as stored, when it is one this module could have
    written; NONE otherwise. A malformed block is no limit, never a guessed
    one: see the module's MAC note for why that is the safe reading."""
    if not isinstance(block, Mapping):
        return NONE
    unit, amount = block.get("unit"), block.get("amount")
    if amount_problem(unit, amount) is not None:
        return NONE
    return _limits(unit, amount)


def ceiling_of(value: Any) -> Limits:
    """The platform ceiling from its stored value; a malformed unit is unset."""
    if not isinstance(value, Mapping):
        return NONE
    usd, tokens = value.get(USD), value.get(TOKENS)
    return Limits(
        usd=float(usd) if usd is not None and amount_problem(USD, usd) is None else None,
        tokens=int(tokens) if tokens is not None and amount_problem(TOKENS, tokens) is None
        else None,
    )


def ceiling_problem(value: Any) -> str | None:
    """Why a ceiling an admin sends is not one, or None. `{}` clears it."""
    if not isinstance(value, Mapping):
        return f"{CEILING_SETTING} is a map of {USD} and {TOKENS}"
    unknown = sorted(set(value) - set(UNITS))
    if unknown:
        return f"{CEILING_SETTING} names only {USD} and {TOKENS}, not {', '.join(unknown)}"
    for unit in UNITS:
        if value.get(unit) is not None:
            why = amount_problem(unit, value[unit])
            if why is not None:
                return f"{CEILING_SETTING}.{unit}: {why}"
    return None


def _money(value: float) -> str:
    return f"${value:,.2f}"


def describe(limits: Limits) -> str:
    """`$1.00`, `15,000,000 tokens`, both joined by "and", or "none"."""
    parts = []
    if limits.usd is not None:
        parts.append(_money(limits.usd))
    if limits.tokens is not None:
        parts.append(f"{limits.tokens:,} tokens")
    return " and ".join(parts) or "none"


def _over(asked: Limits, bound: Limits) -> bool:
    """`asked` is above `bound` in a unit both name."""
    return bool(
        (asked.usd is not None and bound.usd is not None and asked.usd > bound.usd)
        or (asked.tokens is not None and bound.tokens is not None and asked.tokens > bound.tokens)
    )


def refuse_step_above(step: Limits, workspace: Limits, ceiling: Limits, *,
                      step_id: str | None = None) -> None:
    """422 `budget_above_limit` when the step's budget would RAISE a limit:
    above the workspace default or the ceiling in the same unit. The message
    names both, so the caller sees what it may ask for. Behind its switch:
    off, it is logged and returns, and `resolve` still takes the min."""
    if step.empty or not (_over(step, workspace) or _over(step, ceiling)):
        return
    where = f"step {step_id!r}" if step_id else "this task"
    refuse(BudgetAboveLimit(
        f"{where} asks for a budget of {describe(step)}, above what it may have: the "
        f"workspace default is {describe(workspace)} and the platform ceiling is "
        f"{describe(ceiling)}. A step's budget may only lower the limit; nothing was submitted.",
        detail={"step_id": step_id, "budget": step.as_dict(),
                "workspace_default": workspace.as_dict(), "ceiling": ceiling.as_dict()},
    ))


def refuse_default_above_ceiling(default: Limits, ceiling: Limits) -> None:
    """422 `budget_above_ceiling` when a workspace default is above the
    platform ceiling in its unit; the ceiling is in the message. Behind its
    switch: off, the default is saved and the ceiling caps it at resolution."""
    if not _over(default, ceiling):
        return
    refuse(BudgetAboveCeiling(
        f"a workspace budget of {describe(default)} is above the platform ceiling of "
        f"{describe(ceiling)}. Nothing was changed.",
        detail={"budget": default.as_dict(), "ceiling": ceiling.as_dict()},
    ))


def _least(*values: Any) -> Any:
    present = [v for v in values if v is not None]
    return min(present) if present else None


def resolve(step: Limits, workspace: Limits, ceiling: Limits) -> Limits | None:
    """The attempt's budget, min(step, workspace, ceiling) per unit, or None
    -- no budget, today's behaviour -- when neither the step nor the
    workspace set one."""
    if step.empty and workspace.empty:
        return None
    return Limits(
        usd=_least(step.usd, workspace.usd, ceiling.usd),
        tokens=_least(step.tokens, workspace.tokens, ceiling.tokens),
    )


def dispatch_block(resolved: Limits, *, step: Limits, workspace: Limits,
                   ceiling: Limits) -> dict[str, Any]:
    """What `metadata.dispatch.budget` holds: the limits the worker enforces,
    and where each came from, for the console and the end's numbers."""
    return {
        **resolved.as_dict(),
        "from": {"step": step.as_dict(), "workspace": workspace.as_dict(),
                 "ceiling": ceiling.as_dict()},
    }


def step_limits(budget: StepBudget | None) -> Limits:
    """A step's `budget` (`schemas.StepBudget`) as limits; NONE when it has none."""
    if budget is None:
        return NONE
    return Limits(usd=float(budget.usd) if budget.usd is not None else None,
                  tokens=budget.tokens)


class BudgetSettingsPut(StrictModel):
    """`PUT /v1/workspaces/{tenant_id}/budget`: the default, or null to clear it."""

    #: `{unit: "usd"|"tokens", amount: N}`; null removes the default.
    budget: dict[StrictStr, Any] | None

    @model_validator(mode="after")
    def _shape(self) -> "BudgetSettingsPut":
        if self.budget is None:
            return self
        unknown = sorted(set(self.budget) - set(COVERED))
        if unknown:
            raise ValueError(f"budget names only unit and amount, not {', '.join(unknown)}")
        why = amount_problem(self.budget.get("unit"), self.budget.get("amount"))
        if why is not None:
            raise ValueError(why)
        return self

    def limits(self) -> Limits:
        if self.budget is None:
            return NONE
        return _limits(self.budget["unit"], self.budget["amount"])


def authorize_write(auth: AuthContext, tenant: Tenant | None, tenant_id: str,
                    roles: RoleStore | None) -> Tenant:
    """The workspace, when the caller may change its budget: its owner, its
    admin or a platform admin. 404 outside it, 403 for a member."""
    tenant = authorize_read(auth, tenant, tenant_id)
    if not can_edit(auth, tenant, roles, fresh=True):
        raise RoleAdminRequired(
            "Only the workspace's owner or admin, or a platform admin, may change its budget. "
            "Nothing was changed.", detail={"tenant_id": tenant_id, "required": ADMIN})
    return tenant


class BudgetStore:
    """`workspace_settings/{tenant}.budget`, read and audited write."""

    def __init__(self, db: Any, *, now: Callable[[], datetime], mac: SettingsMac) -> None:
        self._db = db
        self._now = now
        self._mac = mac

    def _ref(self, tenant_id: str) -> Any:
        return self._db.collection(COLLECTION).document(tenant_id)

    def _trusted(self, tenant_id: str, block: Any) -> dict[str, Any] | None:
        if not isinstance(block, Mapping):
            return None
        if SIGNED:
            return trusted_block(tenant_id, MAC_BLOCK, block, self._mac) or None
        return dict(block)

    def block(self, tenant_id: str) -> dict[str, Any] | None:
        snap = self._ref(tenant_id).get()
        return self._trusted(tenant_id, (snap.to_dict() or {}).get(FIELD)) if snap.exists else None

    def default(self, tenant_id: str) -> Limits:
        return stored_default(self.block(tenant_id))

    def put(self, tenant_id: str, default: Limits, *, by: str) -> None:
        """Store `default` (NONE clears it) and its audit in one transaction;
        the default already stored writes nothing."""
        ref = self._ref(tenant_id)
        audit = self._db.collection(AUDIT_COLLECTION).document()
        now = self._now()
        transaction = self._db.transaction()

        @firestore.transactional
        def _write(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            whole = (snap.to_dict() or {}) if snap.exists else {}
            previous = stored_default(self._trusted(tenant_id, whole.get(FIELD)))
            if previous == default:
                return
            if default.empty:
                block: dict[str, Any] | None = None
            else:
                unit = USD if default.usd is not None else TOKENS
                fields = {"unit": unit, "amount": default.usd if unit == USD else default.tokens}
                block = {**fields, "updated_at": now, "updated_by": by}
                mac = block_mac(tenant_id, MAC_BLOCK, fields, self._mac.keys) if SIGNED else None
                if mac is not None:
                    block[MAC_FIELD] = mac
            txn.set(ref, {"tenant_id": tenant_id, FIELD: block}, merge=True)
            txn.set(audit, {
                "action": AUDIT_ACTION,
                "target_tenant_id": tenant_id,
                "by": by,
                "at": now,
                "detail": {"tenant_id": tenant_id, "from": previous.as_dict(),
                           "to": default.as_dict()},
            })

        _write(transaction)


def view(tenant_id: str, block: Mapping[str, Any] | None, ceiling: Limits, *,
         editable: bool) -> dict[str, Any]:
    """What GET and PUT answer: the default in force (null: no limit), the
    ceiling it must stay under, the bounds, who set it and whether the caller
    may change it."""
    default = stored_default(block)
    set_at = (block or {}).get("updated_at") if not default.empty else None
    set_by = (block or {}).get("updated_by") if not default.empty else None
    unit = None if default.empty else (USD if default.usd is not None else TOKENS)
    return {
        "tenant_id": tenant_id,
        FIELD: None if unit is None else {
            "unit": unit, "amount": default.usd if unit == USD else default.tokens},
        "ceiling": ceiling.as_dict(),
        "bounds": {USD: {"min_exclusive": 0, "max": MAX_USD}, TOKENS: {"min": 1, "max": MAX_TOKENS}},
        "set_at": set_at.isoformat() if isinstance(set_at, datetime) else None,
        "set_by": set_by if isinstance(set_by, str) else None,
        "can_edit": editable,
    }


__all__ = [
    "ABOVE_CEILING",
    "ABOVE_LIMIT",
    "BudgetAboveCeiling",
    "BudgetAboveLimit",
    "BudgetSettingsPut",
    "BudgetStore",
    "CEILING_SETTING",
    "DISPATCH_KEY",
    "Limits",
    "NONE",
    "StepBudget",
    "authorize_write",
    "ceiling_of",
    "ceiling_problem",
    "describe",
    "dispatch_block",
    "refuse_default_above_ceiling",
    "refuse_step_above",
    "resolve",
    "step_limits",
    "stored_default",
    "view",
]
