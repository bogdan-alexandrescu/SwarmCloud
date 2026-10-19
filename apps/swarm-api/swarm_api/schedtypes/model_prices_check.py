"""`model-prices-check`: the nightly observed-rate check of the price setting (docs/schedules.md §3.15, #1481).

Owner decision, 2026-10-17 (#1481's comment): "a nightly observed-rate
check. It derives per-token rates per model from finished attempts (the CLI's
reported cost and usage) and opens an issue when they disagree with the price
setting by more than 10%."

AN API ACTION, NO AGENT (SD8, §2.7). A firing reads attempt and task
documents swarm-api already holds -- every tenant's, as aggregates only
(§3.13) -- and records what it found on the firing. No task, no lease, no
capacity (invariants 1-3); no profile, because nothing runs (10).

WHAT IS COUNTED. A FINISHED attempt (`completed_at` in the last
`WINDOW`), created within `READ_BACK` -- so an attempt up to a day long that
finished tonight is read -- that names its MODEL (`Attempt.model`, contract
request 103), REPORTED its cost and all four of its token counts. Not counted:
  * an attempt whose task says `cost_estimated` (the worker's flag on
    `result_summary`): its cost was priced FROM the setting by swarm-api
    (`spendestimate`), so it agrees with the setting by construction, and
    counting it would pull a real disagreement towards zero. The flag is the
    task's latest attempt's, so an earlier attempt of that task is passed
    over too: fewer measurements, never a wrong one;
  * a running attempt, one with no cost, no model, or a token count missing.

THE RATE. An attempt records ONE set of token totals and ONE cost, so its
four rates cannot be told apart from one attempt; what is measurable is what
its tokens cost. Per model, over the window: `observed_usd` is the sum of the
reported costs, `setting_usd` is what the same token counts cost at the
setting (`modelprices.estimate_cost`), and each is also given per million
tokens. The deviation is `observed / setting - 1`. An attempt's cost
includes the CLI's own small housekeeping model, which the attempt does not
count separately; it is a few percent at most on the models it runs beside,
inside the tolerance this check exists to apply.

THE JUDGEMENT, per model (one finding per model, however many attempts):
  * `AGREES`: |deviation| <= 10%;
  * `DISAGREES`: |deviation| > 10% -- a FINDING;
  * `TOO_FEW`: fewer than `MIN_ATTEMPTS` attempts, or nothing the setting
    would charge for: not judged;
  * `UNPRICED`: the setting has no entry for the model. Listed, never
    compared: "Nothing is invented" (#1481), so there is no figure to be off
    from; the console says "cost not reported" for it.

THE ISSUE. One per finding, in `issue_repository`, which must be registered in
the schedule's owning tenant and is written with that tenant's credential
(`swarm-tenant-<t>-git`), through `GitHubWriter.create_issue`, as
`cost-report` posts its comment. Its body opens with `marker(model, entry)`,
which names the model AND the setting's four rates, and a firing first looks
for it (`find_issue`): the same disagreement with the same setting is opened
ONCE, not every night, and a changed setting that still disagrees is a new
issue. The body carries aggregates only -- the model, counts, sums and rates
-- never a tenant, task, attempt or person (invariant 9).

Without `issue_repository`, or with one the tenant has not registered, a
finding is recorded on the firing and the `issues` item FAILS: a check that
found a disagreement and told nobody must not read as a success.

DRY RUN (§2.8): the same reads and judgement, and which models would be
opened. Nothing is written and no credential is read.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

from swarm_common.models import Attempt

from .. import modelprices
from ..forge import ForgeReadError
from ..forgewrite import ForgeWriteError
from ..repositories import Repositories, repo_id_for
from ..scheduletypes import BudgetCaps
from ..validation import IssueRef
from . import cost_report, issue_sweep

log = logging.getLogger(__name__)

TYPE = "model-prices-check"

#: SD4 leaves this type's caps to its lane. A firing runs no model, so its
#: own cost is $0.00 (the dollar caps are the smallest a budget accepts).
#: Two items: the report and the issues.
BUDGET_CAPS = BudgetCaps(per_run_usd=0.01, per_day_usd=0.05, max_concurrent=2)

#: The owner's figure: more than 10% off is a disagreement.
TOLERANCE = 0.10
#: Finished in the last day (nightly); created up to two days back, so a
#: day-long attempt that finished tonight is read.
WINDOW = timedelta(hours=24)
READ_BACK = timedelta(hours=48)
#: Fewer attempts than this are not judged: one odd run is not a rate.
MIN_ATTEMPTS = 3
#: Tenants read per firing (`Store.list_tenants`' own default).
MAX_TENANTS = 200

AGREES = "agrees"
DISAGREES = "disagrees"
TOO_FEW = "too_few"
UNPRICED = "unpriced"

REPORT = "report"
ISSUES = "issues"

NO_ISSUE_REPOSITORY = "no_issue_repository"
REPOSITORY_NOT_REGISTERED = "repository_not_registered"
NO_ROOM = "no_room"

_COUNTS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


# --------------------------------------------------------------------------
# Observing and judging (pure)
# --------------------------------------------------------------------------


def _counts(attempt: Attempt) -> dict[str, int] | None:
    out: dict[str, int] = {}
    for key in _COUNTS:
        value = getattr(attempt, key, None)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        out[key] = value
    return out


def _estimated(task: Any) -> bool:
    summary = getattr(task, "result_summary", None)
    return isinstance(summary, Mapping) and summary.get("cost_estimated") is True


def _priced_name(model: str, prices: Mapping[str, Mapping[str, Any]]) -> str:
    """The setting's name for `model` (`claude-haiku-4-5` for its dated ID), else `model`."""
    if model in prices:
        return model
    for name in prices:
        if modelprices.price_for(model, {name: prices[name]}) is not None:
            return name
    return model


def observe(attempts: Iterable[Attempt], tasks: Mapping[str, Any],
            prices: Mapping[str, Mapping[str, Any]], *, since: datetime,
            until: datetime) -> dict[str, dict[str, Any]]:
    """Per model: the counted attempts, their tokens, reported cost and cost at the setting."""
    rows: dict[str, dict[str, Any]] = {}
    for attempt in attempts:
        model, cost, finished = attempt.model, attempt.cost_usd, attempt.completed_at
        if not isinstance(model, str) or not model or finished is None or not since <= finished < until:
            continue
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0:
            continue
        counts = _counts(attempt)
        if counts is None or not sum(counts.values()):
            continue
        if _estimated(tasks.get(attempt.task_id)):
            continue
        name = _priced_name(model, prices)
        row = rows.setdefault(name, {"attempts": 0, "tokens": dict.fromkeys(_COUNTS, 0),
                                     "observed_usd": 0.0, "setting_usd": 0.0})
        row["attempts"] += 1
        for key, value in counts.items():
            row["tokens"][key] += value
        row["observed_usd"] = round(row["observed_usd"] + float(cost), 10)
        at_setting = modelprices.estimate_cost({model: counts}, prices)
        if at_setting is None or row["setting_usd"] is None:
            row["setting_usd"] = None
        else:
            row["setting_usd"] = round(row["setting_usd"] + at_setting, 10)
    return rows


def compare(rows: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Judge each row in place (`status`, `deviation`, rates); return ONE finding per disagreeing model."""
    findings: list[dict[str, Any]] = []
    for name in sorted(rows):
        row = rows[name]
        tokens = sum(row["tokens"].values())
        row["observed_usd_per_mtok"] = round(row["observed_usd"] / tokens * 1_000_000, 6)
        setting = row["setting_usd"]
        row["setting_usd_per_mtok"] = (round(setting / tokens * 1_000_000, 6)
                                       if setting is not None else None)
        row["deviation"] = None
        if setting is None:
            row["status"] = UNPRICED
            continue
        if row["attempts"] < MIN_ATTEMPTS or setting <= 0:
            row["status"] = TOO_FEW
            continue
        row["deviation"] = round(row["observed_usd"] / setting - 1, 6)
        if abs(row["deviation"]) > TOLERANCE:
            row["status"] = DISAGREES
            findings.append({"model": name, **{k: row[k] for k in (
                "attempts", "tokens", "observed_usd", "setting_usd", "observed_usd_per_mtok",
                "setting_usd_per_mtok", "deviation")}})
        else:
            row["status"] = AGREES
    return findings


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def compute(ctx: Any, schedule: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """The night's report: every tenant's attempts, as aggregates per model (§3.13)."""
    since, until = now - WINDOW, now
    report: dict[str, Any] = {
        "type": TYPE,
        "window": {"finished_from": since.isoformat(), "finished_to": until.isoformat()},
        "tolerance": TOLERANCE,
        "min_attempts": MIN_ATTEMPTS,
        "truncated": [],
    }
    if (schedule.get("scope") or {}).get("mode") == "platform":
        tenants = [t.tenant_id for t in ctx.store.list_tenants(limit=MAX_TENANTS)]
        if len(tenants) >= MAX_TENANTS:
            report["truncated"].append("tenants")
    else:
        tenants = [str(schedule["tenant_id"])]
    attempts: list[Attempt] = []
    tasks: dict[str, Any] = {}
    for tenant_id in tenants:
        rows, found, cut = cost_report.read_tenant(ctx.store, tenant_id, now - READ_BACK, until)
        attempts.extend(rows)
        tasks.update(found)
        if cut:
            report["truncated"].append("attempts")
    prices = ctx.store.get_platform_settings()["model_prices"]
    rows = observe(attempts, tasks, prices, since=since, until=until)
    report["findings"] = compare(rows)
    report["models"] = rows
    report["settings"] = {f["model"]: dict(prices[f["model"]]) for f in report["findings"]}
    return report


# --------------------------------------------------------------------------
# The issue
# --------------------------------------------------------------------------


def marker(model: str, entry: Mapping[str, Any]) -> str:
    rates = "/".join(f"{float(entry[k]):g}" for k in modelprices.RATES)
    return f"<!-- swarm:model-price-check {model} {rates} -->"


def _usd(value: float | None) -> str:
    return "none" if value is None else f"${value:,.4f}"


def title(finding: Mapping[str, Any]) -> str:
    pct = round(abs(finding["deviation"]) * 100)
    side = "above" if finding["deviation"] > 0 else "below"
    return (f"The reported per-token cost of {finding['model']} is {pct}% {side} "
            f"its price setting")


def body(finding: Mapping[str, Any], entry: Mapping[str, Any], report: Mapping[str, Any],
         schedule_id: str) -> str:
    tokens = finding["tokens"]
    window = report["window"]
    lines = [
        marker(finding["model"], entry),
        "",
        f"SwarmCloud's nightly price check (schedule `{schedule_id}`) compared what finished "
        f"attempts of `{finding['model']}` reported they cost with what their reported tokens "
        f"cost at the price setting (`model_prices`, #1481). They are more than "
        f"{round(TOLERANCE * 100)}% apart.",
        "",
        f"Attempts finished {window['finished_from']} to {window['finished_to']}: "
        f"{finding['attempts']}.",
        "",
        "| | input | output | cache read | cache write |",
        "|---|---:|---:|---:|---:|",
        f"| tokens | {tokens['input_tokens']:,} | {tokens['output_tokens']:,} | "
        f"{tokens['cache_read_input_tokens']:,} | {tokens['cache_creation_input_tokens']:,} |",
        f"| setting, USD / MTok | {entry['input']:g} | {entry['output']:g} | "
        f"{entry['cache_read']:g} | {entry['cache_write']:g} |",
        "",
        f"* Reported cost: {_usd(finding['observed_usd'])} "
        f"({finding['observed_usd_per_mtok']:g} USD per million tokens).",
        f"* At the setting: {_usd(finding['setting_usd'])} "
        f"({finding['setting_usd_per_mtok']:g} USD per million tokens).",
        f"* Deviation: {finding['deviation'] * 100:+.1f}%.",
        f"* The setting's entry: source `{entry.get('source')}`, as of {entry.get('as_of')}.",
        "",
        "Check the provider's pricing page, then correct the entry in the console's admin "
        "settings, or explain the difference here. This issue is opened once for this model "
        "at these figures; a changed setting that still disagrees opens a new one.",
    ]
    return "\n".join(lines) + "\n"


def _repository(ctx: Any, schedule: Mapping[str, Any], name: str | None) -> tuple[IssueRef | None, str | None, str]:
    """The issue repository, or a code and its sentence. Firestore only."""
    if not name:
        return None, NO_ISSUE_REPOSITORY, (
            "this schedule names no issue_repository, so its findings are recorded on the firing only")
    owner, repo = name.split("/", 1)
    ref = IssueRef(owner=owner, repo=repo, number=0)
    tenant_id = str(schedule["tenant_id"])
    if Repositories(ctx.db, now=ctx.now).find(tenant_id, repo_id_for(tenant_id, owner, repo)) is None:
        return ref, REPOSITORY_NOT_REGISTERED, (
            f"{name} is not a repository the schedule's tenant has registered, so nothing is opened there")
    return ref, None, ""


def open_issues(ctx: Any, schedule: Mapping[str, Any], ref: IssueRef, report: Mapping[str, Any],
                label: str) -> dict[str, dict[str, Any]]:
    """One issue per finding, found again by its marker. Per model: its outcome."""
    out: dict[str, dict[str, Any]] = {}
    token = ""
    schedule_id = str(schedule["schedule_id"])
    try:
        tenant = ctx.store.get_tenant(str(schedule["tenant_id"]))
        if tenant is None:
            return {f["model"]: {"outcome": "failed", "code": "tenant_not_found",
                                 "message": "the schedule's tenant is not on record"}
                    for f in report["findings"]}
        try:
            token = ctx.forge_tokens.token_for(tenant)
        except (ForgeReadError, ForgeWriteError) as exc:
            return {f["model"]: {"outcome": "failed", "code": exc.code, "message": exc.message}
                    for f in report["findings"]}
        writer = ctx.forge_writer
        for finding in report["findings"]:
            model = finding["model"]
            entry = report["settings"][model]
            try:
                found = writer.find_issue(ref, marker(model, entry), token)
                issue = found or writer.create_issue(
                    ref, title=title(finding), body=body(finding, entry, report, schedule_id),
                    labels=(label,), token=token)
                out[model] = {"outcome": "succeeded", "number": issue.number, "url": issue.url,
                              "already_open": found is not None}
            except (ForgeReadError, ForgeWriteError) as exc:
                # Both raise constant, token-free sentences (forge.py, forgewrite.py).
                out[model] = {"outcome": "failed", "code": exc.code, "message": exc.message}
    finally:
        token = ""
    return out


# --------------------------------------------------------------------------
# The executor seam (`schedulefire.Executor`)
# --------------------------------------------------------------------------


def _ids(firing: Any) -> tuple[str, str]:
    return str(firing.firing["firing_id"]), str(firing.schedule["schedule_id"])


def create(firing: Any) -> list[dict[str, Any]]:
    params = issue_sweep.params_of(firing)
    firing_id, schedule_id = _ids(firing)
    if firing.room < 1:
        log.info("schedule %s firing %s: no room for the price check", schedule_id, firing_id)
        return []
    report = compute(firing.ctx, firing.schedule, firing.now)
    findings = report["findings"]
    log.info("schedule %s firing %s: price check over %d models, %d disagree",
             schedule_id, firing_id, len(report["models"]), len(findings))
    if not findings:
        return [firing.api_action(REPORT, outcome="succeeded", detail=report)]
    issues: dict[str, Any]
    ref, code, message = _repository(firing.ctx, firing.schedule, params.issue_repository)
    where = {"repository": params.issue_repository}
    if firing.room < 2:
        issues = {"outcome": "failed", **where, "code": NO_ROOM,
                  "message": "max_concurrent leaves room for the report only, so no issue was opened"}
    elif code or ref is None:
        issues = {"outcome": "failed", **where, "code": code, "message": message}
    else:
        models = open_issues(firing.ctx, firing.schedule, ref, report, params.issue_label)
        failed = any(row["outcome"] == "failed" for row in models.values())
        issues = {"outcome": "failed" if failed else "succeeded", **where, "models": models}
    detail = {k: v for k, v in issues.items() if k != "outcome"}
    if firing.room < 2:
        # One item: the report carries why nothing was opened, and fails.
        return [firing.api_action(REPORT, outcome="failed", detail={**report, "issues": detail})]
    return [
        firing.api_action(REPORT, outcome="succeeded", detail=report),
        firing.api_action(ISSUES, outcome=issues["outcome"], detail=detail),
    ]


def dry_run(firing: Any) -> dict[str, Any]:
    params = issue_sweep.params_of(firing)
    report = compute(firing.ctx, firing.schedule, firing.now)
    out: dict[str, Any] = {"report": report, "issues": None}
    if report["findings"]:
        _ref, code, message = _repository(firing.ctx, firing.schedule, params.issue_repository)
        models = [f["model"] for f in report["findings"]]
        out["issues"] = ({"repository": params.issue_repository, "would": "not_open", "code": code,
                          "message": message, "models": models} if code else
                         {"repository": params.issue_repository, "would": "open_or_find", "models": models})
    return out
