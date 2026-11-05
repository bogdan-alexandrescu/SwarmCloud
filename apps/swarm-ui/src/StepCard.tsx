/**
 * THE WORKFLOW PAGE'S STEP CARD (owner's pick, 2026-11-05: "action bar and
 * stat tiles"). The inspector's head already names the step and its state, so
 * the card starts with its actions: Agent, Logs, Changes with its count, the
 * pull request when the step opened one, and on the right Stop while the step
 * can stop or Retry once it failed. Then four tiles for the selected attempt
 * (elapsed, attempt n/N, cost, tokens), a timeline of the attempts by how long
 * each ran, and everything else behind one Details fold.
 *
 * Every figure is read from what swarm-api serves: the step's row
 * (`stepRows`), its task, and its attempt documents (`attemptFacts`). A figure
 * nobody reported is a dash whose tooltip says why, never a zero.
 */
import { useEffect, useState, type MouseEvent, type ReactNode } from 'react'
import { Button, ButtonLink } from './components'
import { pullRequestOf } from './decision'
import { durationText, type Cell } from './measure'
import { agentPath } from './OverviewRegions'
import { addressToPath } from './paths'
import { attemptFacts, attemptPhase, attemptsInOrder, type Fact } from './stepviews'
import { TERMINAL_STATES, type AttemptRow, type Task, type TaskState } from './types'
import type { AttemptLoader, SiblingRef, StepRowModel } from './WorkflowViews'
import './styles/step-card.css'

function Ic({ children }: { children: ReactNode }) {
  return (
    <svg className="c-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      {children}
    </svg>
  )
}

/** The bar's icons, shared with Run steps' rows so a step's actions look the same everywhere. */
export const STEP_ICON = {
  agent: (
    <Ic>
      <rect x="5" y="7" width="14" height="11" rx="3" />
      <path d="M12 7V4M9 12h.01M15 12h.01M9.5 15h5" />
    </Ic>
  ),
  logs: (
    <Ic>
      <path d="M5 6h14M5 10h14M5 14h9M5 18h6" />
    </Ic>
  ),
  changes: (
    <Ic>
      <path d="M12 5v6M9 8h6M9 17h6" />
      <rect x="4.5" y="3.5" width="15" height="17" rx="2" />
    </Ic>
  ),
  pr: (
    <Ic>
      <circle cx="7" cy="6" r="2" />
      <circle cx="7" cy="18" r="2" />
      <circle cx="17" cy="18" r="2" />
      <path d="M7 8v8M17 16V9a2 2 0 0 0-2-2h-4M13 5l-2 2 2 2" />
    </Ic>
  ),
  prev: (
    <Ic>
      <path d="m15 6-6 6 6 6" />
    </Ic>
  ),
  next: (
    <Ic>
      <path d="m9 6 6 6-6 6" />
    </Ic>
  ),
}

/** A plain left click, which an in-app link may route itself; any modifier keeps the browser's own behaviour. */
export function plainClick(e: MouseEvent): boolean {
  return e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey
}

/** The agent's own path, under the list that holds its state; a task the read did not carry keeps the Live address. */
export function stepAgentHref(taskId: string, state: TaskState | null): string {
  return state === null ? addressToPath(`work/task/${encodeURIComponent(taskId)}`) : agentPath({ id: taskId, state })
}

/** The step's Changes as a bar button: the count as a badge when someone counted it. */
export function ChangesButton({
  href,
  count,
  title,
  onClick,
  className,
}: {
  href: string
  count: number | string | null
  title: string
  onClick?: (e: MouseEvent<HTMLAnchorElement>) => void
  className?: string
}) {
  return (
    <ButtonLink href={href} size="sm" icon={STEP_ICON.changes} title={title} onClick={onClick} className={className}>
      Changes
      {count !== null && <span className="sc-count">{count}</span>}
    </ButtonLink>
  )
}

type Outcome = 'running' | 'succeeded' | 'failed' | 'ended'

const OUTCOME_WORD: Readonly<Record<Outcome, string>> = {
  running: 'running',
  succeeded: 'succeeded',
  failed: 'failed',
  ended: 'ended',
}

const at = (iso: string | null): number => (iso === null ? NaN : Date.parse(iso))

/** How one attempt ended, from its own document; the newest of a succeeded task is that success. */
export function attemptOutcome(a: AttemptRow, latest: boolean, taskState: TaskState | null): Outcome {
  const phase = attemptPhase(taskState, latest).kind
  if (phase === 'running' || phase === 'admitting') return 'running'
  if (a.exit_code === 0 || (latest && taskState === 'SUCCEEDED')) return 'succeeded'
  if ((typeof a.exit_code === 'number' && a.exit_code !== 0) || (latest && (taskState === 'FAILED' || taskState === 'DEAD_LETTERED'))) return 'failed'
  return 'ended'
}

/** How long an attempt ran, for its segment's width: start to end, or to now while it runs. Null when unrecorded. */
function spanMs(a: AttemptRow, outcome: Outcome, now: number): number | null {
  const s = at(a.started_at)
  if (!Number.isFinite(s)) return null
  const e = at(a.completed_at)
  if (Number.isFinite(e)) return Math.max(0, e - s)
  return outcome === 'running' ? Math.max(0, now - s) : null
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** "11:41", or "Oct 19 11:41" on another day than today (UTC). */
export function clockText(ms: number, now: number): string {
  const d = new Date(ms)
  const hm = `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`
  return new Date(now).toISOString().slice(0, 10) === d.toISOString().slice(0, 10) ? hm : `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()} ${hm}`
}

/** One stat: label over figure. An unreported figure is a dash whose tooltip is the reason. */
function Tile({ label, cell, hint }: { label: string; cell: Cell; hint?: string }) {
  const absent = cell.kind === 'absent'
  return (
    <div className={`sc-tile${absent ? ' is-absent' : ''}`} data-tile={label.toLowerCase()}>
      <span className="sc-tile-k">{label}</span>
      {absent ? (
        <span className="sc-tile-v" title={`${cell.text}: ${cell.note}`} aria-label={`${label}: ${cell.text}`} tabIndex={0}>
          —
        </span>
      ) : (
        <span className="sc-tile-v" title={hint ?? cell.note}>
          {cell.text}
        </span>
      )}
    </div>
  )
}

const factOf = (facts: readonly Fact[], key: string): Fact | undefined => facts.find((f) => f.key === key)

type AttemptsRead = { kind: 'reading' } | { kind: 'ready'; attempts: AttemptRow[] } | { kind: 'failed'; detail: string }

export interface StepCardProps {
  workflowId: string
  row: StepRowModel
  /** The step's task as the board read it, or null (no task yet, or not in the read). */
  task: Task | null
  taskState: TaskState | null
  /** The workflow's Changes tab filtered to this step, with its file count (`changesCount`); null: no task. */
  changes: { href: string; count: number | string | null; say: string | null } | null
  now: number
  load: AttemptLoader
  siblings: readonly SiblingRef[]
  siblingIndex: number
  onSibling: (delta: -1 | 1) => void
  /** The step's Stop (`StopRun`), drawn on the bar's right while the step can stop; null otherwise. */
  stop: ReactNode
  /** The step's Retry, drawn in Stop's place once the step failed; null otherwise. */
  retry: ReactNode
  /** What the card used to show under its facts: a review verdict, the merge checklist, an await-input form. */
  extras?: ReactNode
  /** Whether `extras` holds anything, so an empty fold is not drawn. */
  hasExtras?: boolean
  /** The page's failure banner for this step is on screen and says why already, so the card does not say it twice. */
  leadInView?: boolean
}

export function StepCard({
  workflowId,
  row,
  task,
  taskState,
  changes,
  now,
  load,
  siblings,
  siblingIndex,
  onSibling,
  stop,
  retry,
  extras = null,
  hasExtras = false,
  leadInView = false,
}: StepCardProps) {
  const taskId = row.taskId
  const [read, setRead] = useState<AttemptsRead>({ kind: 'reading' })
  const [index, setIndex] = useState<number | null>(null)
  const [whole, setWhole] = useState(false)

  useEffect(() => {
    if (taskId === null) return
    let alive = true
    setRead({ kind: 'reading' })
    setIndex(null)
    load(taskId).then((r) => {
      if (!alive) return
      if (r.status === 'ok' || r.status === 'stale') setRead({ kind: 'ready', attempts: attemptsInOrder(r.data.attempts) })
      else if (r.status === 'empty') setRead({ kind: 'ready', attempts: [] })
      else setRead({ kind: 'failed', detail: r.status === 'error' ? r.error.message : 'The attempt read did not complete.' })
    })
    return () => {
      alive = false
    }
  }, [taskId, load])

  const tries = read.kind === 'ready' ? read.attempts : []
  const n = tries.length
  const sel = n === 0 ? null : (index ?? n - 1)
  const cur = sel === null ? null : (tries[sel] ?? null)
  const latest = sel === n - 1
  const terminal = taskState !== null && TERMINAL_STATES.has(taskState)
  const facts =
    cur === null
      ? []
      : attemptFacts(
          cur,
          attemptPhase(taskState, latest),
          now,
          latest && terminal ? row.result : null,
          latest && terminal ? row.times.ranMs : null,
        )
  const outcomes = tries.map((a, i) => attemptOutcome(a, i === n - 1, taskState))
  const spans = tries.map((a, i) => spanMs(a, outcomes[i]!, now))
  const total = spans.reduce<number>((s, v) => s + (v ?? 0), 0)
  // ATTEMPT n OF THE N ALLOWED, the wording the table uses ("1 of 3"). A step
  // whose attempt documents outnumber its counted attempts (a merge's CI park
  // is refunded) says `run` and counts the documents instead.
  const allowed = task?.max_attempts ?? null
  const counted = row.sort.attempts === null || row.sort.attempts === n
  const pr = task === null ? null : pullRequestOf(task)
  const attempted = taskId !== null && (n > 0 || (task?.attempt_count ?? 0) > 0)
  const cause = row.why !== null && row.why.kind === 'cause' && !leadInView ? row.why.full : null
  const waits = !attempted && row.why !== null && row.why.kind === 'why' ? row.why.text : null
  const others = siblings.length > 1
  const olderError = cur !== null && !latest && cur.error !== null && cur.error !== '' ? cur.error : null
  const details = cur !== null || others || hasExtras || read.kind === 'failed'
  const hasNewer = siblingIndex > 0
  const hasOlder = siblingIndex < siblings.length - 1
  const agentHref = taskId === null ? null : stepAgentHref(taskId, taskState)

  return (
    <section className="sc-card" aria-label={`Step ${row.step.step_id} of ${workflowId}`}>
      {agentHref !== null && (
        <div className="sc-bar" role="toolbar" aria-label="Step actions">
          <ButtonLink href={agentHref} size="sm" icon={STEP_ICON.agent} title={taskId ?? undefined}>
            Agent
          </ButtonLink>
          {attempted && (
            <ButtonLink href={`${agentHref}/logs`} size="sm" icon={STEP_ICON.logs}>
              Logs
            </ButtonLink>
          )}
          {attempted && changes !== null && (
            <ChangesButton href={changes.href} count={changes.count} title={changes.say ?? `${row.step.step_id}’s changes in this workflow`} />
          )}
          {pr !== null && (
            <ButtonLink href={pr.url} size="sm" icon={STEP_ICON.pr} title={`Pull request #${pr.number}, ${pr.state}, on GitHub`} target="_blank" rel="noreferrer">
              PR #{pr.number}
            </ButtonLink>
          )}
          {(retry !== null || stop !== null) && <span className="sc-end">{retry ?? stop}</span>}
        </div>
      )}

      {waits !== null && <div className="sc-wait">{waits}</div>}

      {cause !== null && (
        <div className="sc-err" role="group" aria-label="Why it failed">
          <pre className={`sc-err-text${whole ? ' is-whole' : ''}`}>{cause}</pre>
          {cause.split('\n').length > 3 && (
            <Button size="sm" onClick={() => setWhole(!whole)} aria-expanded={whole}>
              {whole ? 'Less' : 'Show all'}
            </Button>
          )}
        </div>
      )}

      {cur !== null && sel !== null && (
        <div className="sc-tiles">
          <Tile label="Elapsed" cell={factOf(facts, 'took')!.cell} />
          <Tile
            label={counted ? 'Attempt' : 'Run'}
            cell={{ kind: 'measured', text: counted && allowed !== null ? `${sel + 1} / ${allowed}` : `${sel + 1} / ${n}`, note: '' }}
            hint={
              counted && allowed !== null
                ? `Attempt ${sel + 1} of the ${allowed} allowed`
                : `Run ${sel + 1} of the ${n} attempt records this step has; ${row.attempts.text} count against its attempts`
            }
          />
          <Tile label="Cost" cell={factOf(facts, 'cost')!.cell} />
          <Tile label="Tokens" cell={factOf(facts, 'tokens')!.cell} />
        </div>
      )}

      {cur !== null && sel !== null && (
        <div className="sc-tl">
          <div className="sc-tl-row">
            {n > 1 && (
              <Button size="sm" kind="ghost" iconOnly icon={STEP_ICON.prev} disabled={sel === 0} onClick={() => setIndex(sel - 1)}>
                Previous attempt
              </Button>
            )}
            <ol className={`sc-tl-bar${n === 1 ? ' is-one' : ''}`} aria-label="Attempts">
              {tries.map((a, i) => {
                const o = outcomes[i]!
                const ms = spans[i] ?? null
                const took = ms === null ? 'not timed' : durationText(ms)
                const grow = n === 1 ? 1 : Math.max(total > 0 && ms !== null ? ms / total : 1 / n, 0.12) * 100
                return (
                  <li key={a.attempt_id} style={{ flexGrow: grow }}>
                    {n === 1 ? (
                      <span className={`sc-seg is-${o}`} title={`${OUTCOME_WORD[o]} · ${took}`} />
                    ) : (
                      <button
                        type="button"
                        className={`sc-seg is-${o}${i === sel ? ' is-here' : ''}`}
                        aria-pressed={i === sel}
                        title={`Attempt ${i + 1} · ${OUTCOME_WORD[o]} · ${took}${a.exit_code !== null ? ` · exit ${a.exit_code}` : ''}`}
                        onClick={() => setIndex(i)}
                      >
                        {i + 1}
                      </button>
                    )}
                  </li>
                )
              })}
            </ol>
            {n > 1 && (
              <Button size="sm" kind="ghost" iconOnly icon={STEP_ICON.next} disabled={sel === n - 1} onClick={() => setIndex(sel + 1)}>
                Next attempt
              </Button>
            )}
          </div>
          <Ends a={cur} running={outcomes[sel] === 'running'} one={n === 1} which={sel + 1} now={now} />
        </div>
      )}

      {details && (
        <details className="sc-more">
          <summary>Details</summary>
          <dl className="sc-facts">
            {read.kind === 'failed' && (
              <>
                <dt>Attempts</dt>
                <dd title={read.detail}>not read</dd>
              </>
            )}
            {cur !== null && (
              <>
                <FactRow label="Exit code" fact={factOf(facts, 'exit')} />
                <FactRow label="Checkpoints" fact={factOf(facts, 'ckpts')} />
                <FactRow label="Generation" fact={factOf(facts, 'gen')} />
                {factOf(facts, 'parked') !== undefined && <FactRow label="Parked" fact={factOf(facts, 'parked')} />}
                {olderError !== null && (
                  <>
                    <dt>Error</dt>
                    <dd>
                      <pre className="sc-err-text is-whole">{olderError}</pre>
                    </dd>
                  </>
                )}
              </>
            )}
            {others && (
              <>
                <dt>Other workflows</dt>
                <dd className="sc-others" role="group" aria-label="The same step in other workflows of the same shape on this board, newest first">
                  <Button size="sm" kind="ghost" iconOnly icon={STEP_ICON.prev} disabled={!hasNewer} onClick={() => onSibling(-1)}>
                    Same step, newer workflow
                  </Button>
                  <span>
                    {siblingIndex + 1} / {siblings.length}
                  </span>
                  <Button size="sm" kind="ghost" iconOnly icon={STEP_ICON.next} disabled={!hasOlder} onClick={() => onSibling(1)}>
                    Same step, older workflow
                  </Button>
                </dd>
              </>
            )}
          </dl>
          {hasExtras && <div className="sc-extras">{extras}</div>}
        </details>
      )}
    </section>
  )
}

function FactRow({ label, fact }: { label: string; fact: Fact | undefined }) {
  if (fact === undefined) return null
  return (
    <>
      <dt>{label}</dt>
      <dd className={fact.cell.kind === 'absent' ? 'is-absent' : undefined} title={fact.cell.note}>
        {fact.cell.kind === 'absent' ? '—' : fact.cell.text}
      </dd>
    </>
  )
}

/** The selected attempt's own start and end under the bar (UTC); `now` while it runs. */
function Ends({ a, running, one, which, now }: { a: AttemptRow; running: boolean; one: boolean; which: number; now: number }) {
  const s = at(a.started_at)
  const e = at(a.completed_at)
  const start = Number.isFinite(s) ? clockText(s, now) : '—'
  const end = Number.isFinite(e) ? clockText(e, now) : running ? 'now' : '—'
  if (one) {
    return (
      <div className="sc-tl-ends">
        <span title="Started (UTC)">{start}</span>
        <span title={running ? 'Still running' : 'Ended (UTC)'}>{end}</span>
      </div>
    )
  }
  return (
    <div className="sc-tl-ends is-one-span" title={`Attempt ${which}, UTC`}>
      <span>
        {start} – {end}
      </span>
    </div>
  )
}
