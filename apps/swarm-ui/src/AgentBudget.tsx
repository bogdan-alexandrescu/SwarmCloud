/**
 * THE PER-AGENT BUDGET, WHERE PEOPLE WATCH AGENTS (#2459, BUDGET-UI-AGENT;
 * docs/design/agent-budget.md §2 and §5).
 *
 * An attempt's budget is resolved at submission and pinned in its dispatch
 * block, `dispatch.budget` -- `{usd?, tokens?, from: {step, workspace,
 * ceiling}}` -- which the list's summary row carries as the full row does
 * (`codec.dispatch_of`). At 80% the worker tells the agent to finish and
 * publish and writes a `budget_warning` event; at 100% it stops the agent at
 * its next turn boundary, checkpoints, publishes, and ends it FAILED.
 *
 * READ BOTH SHAPES, THE ONE SHIPPED AND THE ONE ACCEPTED. Contract requests
 * 118 and 121 (accepted 2026-10-26, applied by BUDGET-CONTRACT) give the end
 * `EndCause.BUDGET_EXCEEDED` and the warning `EventType.BUDGET_WARNING`.
 * Until then the end is `end_cause` null with `last_error` beginning
 * `budget_exceeded:`, and the warning is a `running` event whose
 * `detail.cause` is `budget_warning`. Every reader here takes either, so the
 * console does not go blank on the day the contract lands.
 *
 * NOTHING HERE FOR AN AGENT WITH NO BUDGET. "Off until a workspace admin
 * sets one" is the owner's default: an unbudgeted agent's row and Details
 * read exactly as they did, and no event read is made for it.
 *
 * The task's `dispatch` type (types.ts) has no `budget` yet; another lane is
 * changing that file, so the block is read here as the untyped JSON it is.
 */
import { useEffect, useState } from 'react'

import { loadTaskEventsPage } from './api'
import { absTime } from './AttemptTimeline'
import { ToneMark } from './components'
import { branchOf, pullRequestOf } from './decision'
import { tokenCount } from './measure'
import { CONCURRENCY_STATES, TERMINAL_STATES, timeAgo, type Task, type TaskEvent } from './types'

/** The end cause and the warning's cause, as the worker writes them (`agent_worker/budget.py`). */
export const BUDGET_EXCEEDED = 'budget_exceeded'
export const BUDGET_WARNING = 'budget_warning'

/** Where one unit's limit came from: the min of the three (`swarm_api.budget.resolve`). */
export type BudgetSource = 'step' | 'workspace' | 'ceiling'
const SOURCES: readonly BudgetSource[] = ['step', 'workspace', 'ceiling']

/** The words each source is said in, on the card and in the row's title. */
export const SOURCE_SAY: Readonly<Record<BudgetSource, string>> = {
  step: 'set by this step',
  workspace: "the workspace's default",
  ceiling: "the platform's ceiling",
}

/** Spent, in either unit. A unit the run did not report is absent, never 0. */
export interface BudgetAmounts {
  usd: number | null
  tokens: number | null
}

/** `dispatch.budget`: the limits the worker enforces, and where each came from. */
export interface AgentBudget extends BudgetAmounts {
  from: { usd: BudgetSource | null; tokens: BudgetSource | null }
}

function rec(v: unknown): Record<string, unknown> | null {
  return typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

function amount(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : null
}

function amounts(v: unknown): BudgetAmounts | null {
  const r = rec(v)
  if (r === null) return null
  const out = { usd: amount(r['usd']), tokens: amount(r['tokens']) }
  return out.usd === null && out.tokens === null ? null : out
}

/**
 * The source a unit's limit came from: the first of step, workspace and
 * ceiling that holds the resolved figure. Null when the block does not say,
 * which the card draws as "not recorded", never as a guess.
 */
function sourceOf(from: Record<string, unknown> | null, unit: 'usd' | 'tokens', limit: number | null): BudgetSource | null {
  if (from === null || limit === null) return null
  for (const s of SOURCES) {
    if (amount(rec(from[s])?.[unit]) === limit) return s
  }
  return null
}

/** The agent's resolved budget, or null when it has none: "off" draws nothing. */
export function budgetOf(task: Pick<Task, 'dispatch'>): AgentBudget | null {
  const block = rec(rec(task.dispatch)?.['budget'])
  const limits = amounts(block)
  if (block === null || limits === null) return null
  const from = rec(block['from'])
  return { ...limits, from: { usd: sourceOf(from, 'usd', limits.usd), tokens: sourceOf(from, 'tokens', limits.tokens) } }
}

/** `result_summary.budget`, the worker's numbers at the end (`BudgetGuard.summary`). */
export interface BudgetEnd {
  used: BudgetAmounts | null
  warned: boolean
  exceeded: boolean
  stopped: 'turn_boundary' | 'mid_turn' | null
}

export function budgetEndOf(task: Pick<Task, 'result_summary'>): BudgetEnd | null {
  const b = rec(rec(task.result_summary)?.['budget'])
  if (b === null) return null
  const stopped = b['stopped']
  return {
    used: amounts(b['used']),
    warned: b['warned'] === true,
    exceeded: b['end_cause'] === BUDGET_EXCEEDED,
    stopped: stopped === 'turn_boundary' || stopped === 'mid_turn' ? stopped : null,
  }
}

/**
 * WHETHER THE BUDGET ENDED THIS AGENT. A finished agent only, and any of the
 * three places the end is written: the end cause (once request 118 lands),
 * the `last_error` prefix the worker writes today, or the result summary --
 * which the list's summary row does not carry, so the row reads the first two.
 */
export function stoppedAtBudget(task: Pick<Task, 'state' | 'end_cause' | 'last_error' | 'result_summary'>): boolean {
  if (!TERMINAL_STATES.has(task.state)) return false
  if (task.end_cause === BUDGET_EXCEEDED) return true
  if (typeof task.last_error === 'string' && task.last_error.startsWith(`${BUDGET_EXCEEDED}:`)) return true
  return budgetEndOf(task)?.exceeded === true
}

/** The 80% warning, read off its event. */
export interface BudgetWarning {
  at: string
  percent: number | null
  /** Whether the agent itself was told (claude-code's hook); codex is not. Null when the event does not say. */
  told: boolean | null
  used: BudgetAmounts | null
}

/**
 * THE CURRENT ATTEMPT'S WARNING, from an event page, or null. Either shape
 * (the file's head says why), and only this attempt's: the limit is per
 * attempt (§3), so a continuation that starts from zero is not marked by its
 * predecessor's warning. An event that carries no generation is taken.
 */
export function budgetWarningIn(events: readonly TaskEvent[] | null, generation: number | null): BudgetWarning | null {
  if (events === null) return null
  let found: TaskEvent | null = null
  for (const e of events) {
    const d = rec(e.detail)
    const isWarning = e.type === BUDGET_WARNING || (e.type === 'running' && d?.['cause'] === BUDGET_WARNING)
    if (!isWarning) continue
    if (typeof e.generation === 'number' && typeof generation === 'number' && e.generation !== generation) continue
    if (found === null || Date.parse(e.at) > Date.parse(found.at)) found = e
  }
  if (found === null) return null
  const d = rec(found.detail)
  const told = d?.['told']
  return {
    at: found.at,
    percent: amount(d?.['percent']),
    told: typeof told === 'boolean' ? told : null,
    used: amounts(d?.['used']),
  }
}

/** One amount in its unit: `$1.00`, `15M tokens`. */
export function amountSay(v: number, unit: 'usd' | 'tokens'): string {
  return unit === 'usd' ? `$${v.toFixed(2)}` : `${tokenCount(v)} tokens`
}

/** Every limit the budget holds, joined: `$1.00`, `$1.00 and 15M tokens`. */
export function limitSay(b: BudgetAmounts): string {
  const parts: string[] = []
  if (b.usd !== null) parts.push(amountSay(b.usd, 'usd'))
  if (b.tokens !== null) parts.push(amountSay(b.tokens, 'tokens'))
  return parts.join(' and ')
}

/** What was used, in the units the budget limits only: a figure in a unit nobody limited is not this card's. */
function usedSay(used: BudgetAmounts, limit: BudgetAmounts): string | null {
  const parts: string[] = []
  if (limit.usd !== null && used.usd !== null) parts.push(`${amountSay(used.usd, 'usd')} of ${amountSay(limit.usd, 'usd')}`)
  if (limit.tokens !== null && used.tokens !== null) parts.push(`${amountSay(used.tokens, 'tokens')} of ${amountSay(limit.tokens, 'tokens')}`)
  return parts.length === 0 ? null : parts.join(' · ')
}

/**
 * WHAT THE LIST'S ROW SAYS OF AN AGENT THE BUDGET STOPPED: the amount from
 * the worker's own sentence (`BudgetGuard.error`: "the agent used X of a
 * budget of Y"), else the limit pinned at submission.
 */
export function budgetStopLine(task: Pick<Task, 'dispatch' | 'last_error'>): string {
  const said = /used (.+?) of a budget of (.+?) and was stopped/.exec(task.last_error ?? '')
  if (said !== null) return `Stopped at its budget of ${said[2]} after using ${said[1]}; its work was kept and published.`
  const b = budgetOf(task)
  return b !== null
    ? `Stopped at its budget of ${limitSay(b)}; its work was kept and published.`
    : 'Stopped at its budget; its work was kept and published.'
}

/**
 * THE CURRENT ATTEMPT'S WARNING, READ. `events` is a page the caller already
 * holds (the Details read); when it has the warning, nothing more is read.
 * Otherwise a budgeted agent that is live, or that ended warned, reads its
 * NEWEST page (`order=desc`): Details' own page is the oldest 200, and a long
 * run's warning is near its end.
 *
 * Re-read on the list's poll only when the row says its worker beat again
 * (`heartbeat_at`) or its attempt changed, and never again once found for
 * this attempt. An unbudgeted agent makes no read at all.
 */
export function useBudgetWarning(
  task: Task,
  events: readonly TaskEvent[] | null = null,
): { warning: BudgetWarning | null; read: 'held' | 'reading' | 'read' | 'failed' | 'none' } {
  const budget = budgetOf(task)
  const generation = typeof task.current_generation === 'number' ? task.current_generation : null
  const held = budgetWarningIn(events, generation)
  const wants = budget !== null && held === null && (CONCURRENCY_STATES.has(task.state) || budgetEndOf(task)?.warned === true)
  const attemptKey = `${task.id}:${generation}`
  const key = `${attemptKey}:${task.heartbeat_at ?? ''}:${task.state}`
  const [got, setGot] = useState<{ attempt: string; warning: BudgetWarning | null; failed: boolean } | null>(null)
  const found = got !== null && got.attempt === attemptKey && got.warning !== null
  useEffect(() => {
    if (!wants || found) return
    let live = true
    Promise.resolve()
      .then(() => loadTaskEventsPage(task.id, { order: 'desc' }))
      .then((r) => r ?? null, () => null)
      .then((r) => {
        if (!live) return
        const page = r === null ? null : r.status === 'ok' || r.status === 'stale' ? r.data.events : r.status === 'empty' ? [] : null
        setGot({ attempt: attemptKey, warning: budgetWarningIn(page, generation), failed: page === null })
      })
    return () => {
      live = false
    }
    // `key` names everything the read depends on: the task, its attempt, its last beat.
  }, [key, wants, found])
  if (held !== null) return { warning: held, read: 'held' }
  if (!wants) return { warning: null, read: 'none' }
  if (got === null || got.attempt !== attemptKey) return { warning: null, read: 'reading' }
  return { warning: got.warning, read: got.failed ? 'failed' : 'read' }
}

/**
 * THE ROW'S MARK, OR NOTHING. A live agent past its warning: an amber
 * `budget 80%`. An ended one the budget stopped: `stopped at its budget`,
 * so the row does not read as a plain failure. Its title is the sentence;
 * the row's opener carries the same words (`budgetRowSay`). `warning` is the
 * row's own `useBudgetWarning` read, made once per row.
 */
export function BudgetRowMark({ task, live, warning }: { task: Task; live: boolean; warning: BudgetWarning | null }) {
  if (stoppedAtBudget(task)) {
    return (
      <span className="cr-budget is-stopped" title={budgetStopLine(task)}>
        <ToneMark tone="warn">stopped at its budget</ToneMark>
      </span>
    )
  }
  if (!live || warning === null) return null
  return (
    <span className="cr-budget is-warned" title={warnedSay(task, warning)}>
      <ToneMark tone="warn">budget {warning.percent ?? 80}%</ToneMark>
    </span>
  )
}

/** The warned row's whole sentence: how far, of what, and what the agent was told. */
function warnedSay(task: Task, warning: BudgetWarning): string {
  const b = budgetOf(task)
  const used = b !== null && warning.used !== null ? usedSay(warning.used, b) : null
  return `Past ${warning.percent ?? 80}% of its budget${b !== null ? ` of ${limitSay(b)}` : ''}${used !== null ? ` (${used} at the warning)` : ''}; it was told to finish and publish.`
}

/** The words a row's accessible name carries for its budget, or null: the mark's sentence. */
export function budgetRowSay(task: Task, warning: BudgetWarning | null, live: boolean): string | null {
  if (stoppedAtBudget(task)) return budgetStopLine(task)
  if (live && warning !== null) return warnedSay(task, warning)
  return null
}

/**
 * THE BUDGET CARD ON THE AGENT'S DETAILS: the limit and its unit, where it
 * came from, what was used, when it was warned, and for a stop that the work
 * was kept and published, with its pull request when it opened one. Nothing
 * for an agent with no budget.
 */
export function BudgetCard({ task, events, now }: { task: Task; events: readonly TaskEvent[] | null; now: number }) {
  const budget = budgetOf(task)
  const { warning, read } = useBudgetWarning(task, events)
  if (budget === null) return null
  const end = budgetEndOf(task)
  const stopped = stoppedAtBudget(task)
  const finished = TERMINAL_STATES.has(task.state)
  const pr = stopped ? pullRequestOf(task) : null
  const branch = stopped && pr === null ? branchOf(task) : null
  const used = end?.used ?? warning?.used ?? null
  const usedLine = used !== null ? usedSay(used, budget) : null
  const tone = stopped ? 'is-stopped' : warning !== null && !finished ? 'is-warn' : ''
  const units: ('usd' | 'tokens')[] = (['usd', 'tokens'] as const).filter((u) => budget[u] !== null)
  return (
    <section className={`dt-card dt-budget ${tone}`.trim()} data-budget={stopped ? 'stopped' : warning !== null ? 'warned' : 'set'} aria-label="Budget">
      <div className="dt-card-head">
        <h2>{stopped ? 'Budget: stopped at its budget' : 'Budget'}</h2>
        {warning !== null && !stopped && !finished && (
          <span className="dt-card-r">
            <ToneMark tone="warn">budget {warning.percent ?? 80}%</ToneMark>
          </span>
        )}
      </div>
      <ul className="ctl-facts dt-ifacts">
        {units.map((u) => (
          <li className="ctl-fact" key={u} data-unit={u}>
            <b>limit</b>
            <span>
              {amountSay(budget[u] as number, u)}
              {' · '}
              <span className={budget.from[u] === null ? 'is-absent' : undefined}>
                {budget.from[u] !== null ? SOURCE_SAY[budget.from[u] as BudgetSource] : 'where it came from is not recorded'}
              </span>
            </span>
          </li>
        ))}
        <li className={`ctl-fact${usedLine === null ? ' is-absent' : ''}`} data-fact="used">
          <b>used</b>
          <span>
            {usedLine !== null
              ? `${usedLine}${end?.used != null ? '' : ' at the warning'}`
              : finished
                ? 'not reported'
                : read === 'read'
                  ? 'under 80% so far · the worker reports it at 80% and at the end'
                  : read === 'failed'
                    ? 'not read · the event read failed'
                    : 'reading…'}
          </span>
        </li>
        {(warning !== null || end?.warned === true) && (
          <li className="ctl-fact" data-fact="warned">
            <b>warned</b>
            <span>
              {warning !== null ? (
                <>
                  <span title={absTime(warning.at)}>at {warning.percent ?? 80}%, {timeAgo(warning.at, now)}</span>
                  {warning.told === true
                    ? ' · the agent was told to finish and publish'
                    : warning.told === false
                      ? ' · this runner cannot be told mid-run'
                      : ''}
                </>
              ) : (
                'at 80% · when is not on the events read'
              )}
            </span>
          </li>
        )}
      </ul>
      {stopped && (
        <p className="dt-note" data-fact="kept">
          {end?.stopped === 'mid_turn'
            ? 'Stopped mid-turn, past 120% of its budget. '
            : end?.stopped === 'turn_boundary'
              ? 'Stopped at a turn boundary. '
              : ''}
          Its work was checkpointed and published as it stood, and the step can be resumed.
          {pr !== null ? (
            <>
              {' '}
              <a href={pr.url} target="_blank" rel="noreferrer">
                Pull request #{pr.number}
              </a>
            </>
          ) : branch?.name ? (
            <>
              {' '}Branch <span className="mono">{branch.name}</span>.
            </>
          ) : null}
        </p>
      )}
    </section>
  )
}
