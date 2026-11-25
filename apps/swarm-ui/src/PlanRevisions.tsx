/**
 * THE REVISION PICKER AND THE DIFF (docs/design/plans-pipeline.md §2.3,
 * §3.3, §5.4; lane U4 PLAN-THREAD-UI).
 *
 * The run read serves every revision WITHOUT its plan (`plan_revisions`,
 * newest first); one revision's whole plan is read on demand from `GET
 * /v1/runs/{id}/plan-revisions/{n}`. The picker names each revision with its
 * author and its cost (a dash with its reason when the cost was not
 * reported, never $0, §5.4). Picking one that is not on screen shows it
 * READ-ONLY beside the document with its diff against the newest revision:
 * the document, its margin and the decision bar stay on the revision the bar
 * names, because a revision is never swapped under an open Approve (D3).
 *
 * THE DIFF IS THE PAGE'S OWN. `changed` on a revision is a summary; the page
 * diffs two whole plans itself (§3.3): the summary, the estimate, the
 * rationale, each requirement by its number, and each step by its `step_id`
 * (added, removed, or each field replaced). Plan text is data: every value is
 * a React text child, never HTML.
 */

import { useEffect, useState } from 'react'
import { loadPlanRevision } from './api'
import { Button, Dash } from './components'
import { errorHeading, type ApiError } from './fetch'
import { InfoTip } from './InfoTip'
import './styles/plan-thread.css'

type Json = Record<string, unknown>

const text = (v: unknown): string | null => (typeof v === 'string' && v.trim() !== '' ? v.trim() : null)
const texts = (v: unknown): string[] => (Array.isArray(v) ? v.flatMap((x) => text(x) ?? []) : [])
const objs = (v: unknown): Json[] => (Array.isArray(v) ? v.filter((x): x is Json => x !== null && typeof x === 'object' && !Array.isArray(x)) : [])
const obj = (v: unknown): Json | null => (v !== null && typeof v === 'object' && !Array.isArray(v) ? (v as Json) : null)
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)

/** One changed field between two plans. */
export interface DiffRow {
  /** `summary`, `requirements.<n>` (1 up), `steps.<step_id>` or `steps.<step_id>.<field>`. */
  path: string
  op: 'add' | 'remove' | 'replace'
  before: string | null
  after: string | null
}

const SCALARS = ['summary', 'estimate', 'rationale'] as const
const STEP_TEXT = ['title', 'prompt', 'estimate', 'rationale'] as const
const STEP_LISTS = ['files', 'tests', 'done_when'] as const

function stepKey(step: Json, i: number): string {
  return text(step.step_id) ?? `step-${i + 1}`
}

function row(path: string, before: string | null, after: string | null): DiffRow[] {
  if (before === after) return []
  return [{ path, op: before === null ? 'add' : after === null ? 'remove' : 'replace', before, after }]
}

const listText = (v: unknown): string | null => {
  const items = texts(v)
  return items.length === 0 ? null : items.join(', ')
}

/** What changed from `from` to `to`, in plan order. Two equal plans give []. */
export function diffPlans(from: Json, to: Json): DiffRow[] {
  const rows: DiffRow[] = []
  for (const k of SCALARS) rows.push(...row(k, text(from[k]), text(to[k])))
  const ra = texts(from.requirements)
  const rb = texts(to.requirements)
  for (let i = 0; i < Math.max(ra.length, rb.length); i++) rows.push(...row(`requirements.${i + 1}`, ra[i] ?? null, rb[i] ?? null))
  const sa = objs(from.steps)
  const sb = objs(to.steps)
  const before = new Map(sa.map((s, i) => [stepKey(s, i), s]))
  const after = new Map(sb.map((s, i) => [stepKey(s, i), s]))
  for (const [key, s] of before) if (!after.has(key)) rows.push(...row(`steps.${key}`, text(s.title) ?? text(s.prompt) ?? key, null))
  sb.forEach((s, i) => {
    const key = stepKey(s, i)
    const old = before.get(key)
    if (old === undefined) {
      rows.push(...row(`steps.${key}`, null, text(s.title) ?? text(s.prompt) ?? key))
      return
    }
    for (const f of STEP_TEXT) rows.push(...row(`steps.${key}.${f}`, text(old[f]), text(s[f])))
    for (const f of STEP_LISTS) rows.push(...row(`steps.${key}.${f}`, listText(old[f]), listText(s[f])))
  })
  return rows
}

/** The diff as rows: the path, what was struck and what was added. */
export function PlanDiff({ rows }: { rows: DiffRow[] }) {
  if (rows.length === 0)
    return (
      <p className="pr-diff is-same" data-testid="pr-diff">
        <span className="pt-chip">no change</span>
      </p>
    )
  return (
    <ol className="pr-diff" data-testid="pr-diff" aria-label="Changes">
      {rows.map((r) => (
        <li key={r.path} className="pr-row" data-path={r.path} data-op={r.op}>
          <code className="pr-path">{r.path}</code>
          {r.before !== null && <del className="pr-del">{r.before}</del>}
          {r.after !== null && <ins className="pr-ins">{r.after}</ins>}
        </li>
      ))}
    </ol>
  )
}

/** A revision's whole plan, read once; `known` (the plan on screen) skips the read. */
export type PlanOf = { status: 'loading' } | { status: 'ok'; plan: Json } | { status: 'error'; error: ApiError }

export function useRevisionPlan(runId: string | null, revision: number | null, known: Json | null = null): PlanOf | null {
  const [got, setGot] = useState<{ key: string; of: PlanOf } | null>(null)
  const key = `${runId}:${revision}`
  useEffect(() => {
    if (known !== null || runId === null || revision === null) return
    let live = true
    setGot({ key, of: { status: 'loading' } })
    void loadPlanRevision(runId, revision).then(
      (r) => {
        if (!live) return
        if (r.status === 'ok' || r.status === 'stale') {
          const plan = obj(r.data.revision?.plan)
          setGot({ key, of: plan === null ? { status: 'error', error: { kind: 'server_error', httpStatus: null, code: null, message: `revision ${revision} served no plan` } as ApiError } : { status: 'ok', plan } })
        } else if (r.status === 'error') setGot({ key, of: { status: 'error', error: r.error } })
        else if (r.status === 'empty') setGot({ key, of: { status: 'error', error: { kind: 'server_error', httpStatus: null, code: null, message: `revision ${revision} served no plan` } as ApiError } })
      },
      () => undefined,
    )
    return () => {
      live = false
    }
  }, [key, runId, revision, known])
  if (known !== null) return { status: 'ok', plan: known }
  if (runId === null || revision === null) return null
  return got?.key === key ? got.of : { status: 'loading' }
}

const usd = (n: number) => `$${n.toFixed(2)}`

/** A revision's cost (§5.4): dollars, or a dash with its reason; nothing when none is recorded. */
export function RevisionCost({ cost }: { cost: unknown }) {
  const c = obj(cost)
  if (c === null) return null
  const n = num(c.usd)
  return <span className="pt-cost">{n === null ? <Dash why={text(c.why_none) ?? 'cost not reported'} /> : usd(n)}</span>
}

/** The author's short name: the local part of an address, or `planner`. */
export function who(by: unknown): string {
  const t = text(by)
  if (t === null) return '—'
  const at = t.indexOf('@')
  return at > 0 ? t.slice(0, at) : t
}

/** One revision drawn whole and read-only: no composer, no verb. */
function ReadOnlyPlan({ plan }: { plan: Json }) {
  const steps = objs(plan.steps)
  const reqs = texts(plan.requirements)
  return (
    <div className="pr-plan">
      {text(plan.summary) !== null && <p className="pr-summary">{text(plan.summary)}</p>}
      {reqs.length > 0 && (
        <ol className="pr-reqs" aria-label="Requirements">
          {reqs.map((r, i) => (
            <li key={`${i}:${r}`}>{r}</li>
          ))}
        </ol>
      )}
      <ol className="pr-steps" aria-label="Steps">
        {steps.map((s, i) => (
          <li key={stepKey(s, i)}>
            <b>{text(s.title) ?? stepKey(s, i)}</b>
            {text(s.prompt) !== null && text(s.prompt) !== text(s.title) && <pre className="pd-prompt">{text(s.prompt)}</pre>}
            {texts(s.files).length > 0 && <code className="pd-mono pd-dim">{texts(s.files).join(', ')}</code>}
          </li>
        ))}
      </ol>
    </div>
  )
}

/** The picked revision, read-only, with its diff against the newest. */
function RevisionView({ runId, revision, newest, newestKnown, onClose }: { runId: string; revision: number; newest: number; newestKnown: Json | null; onClose: () => void }) {
  const picked = useRevisionPlan(runId, revision)
  const top = useRevisionPlan(runId, newest, newestKnown)
  const failed = picked?.status === 'error' ? picked.error : top?.status === 'error' ? top.error : null
  return (
    <section className="pr-view" aria-label={`Revision ${revision}, read-only`} data-testid="pr-view">
      <header className="pr-view-h">
        <b>Revision {revision}</b>
        <span className="pt-chip">read-only</span>
        <code className="pd-mono pd-dim">
          r{revision} → r{newest}
        </code>
        <Button kind="ghost" size="sm" onClick={onClose}>
          Close
        </Button>
      </header>
      {failed !== null ? (
        <p className="pt-refused" role="alert">
          {failed.message || errorHeading(failed)}
        </p>
      ) : picked?.status === 'ok' ? (
        <>
          <ReadOnlyPlan plan={picked.plan} />
          {top?.status === 'ok' ? <PlanDiff rows={diffPlans(picked.plan, top.plan)} /> : <span className="pd-dim">…</span>}
        </>
      ) : (
        <span className="pd-dim">…</span>
      )}
    </section>
  )
}

/**
 * The picker: every served revision, newest first, the one the bar names
 * pressed. `shownPlan` is the plan on screen, so the newest is not read again
 * when it is the one shown.
 */
export function PlanRevisionPicker({
  runId,
  revisions,
  shown,
  shownPlan,
  complete,
}: {
  runId: string
  revisions: Json[]
  shown: number | null
  shownPlan: Json
  complete: boolean
}) {
  const [picked, setPicked] = useState<number | null>(null)
  const numbered = revisions.filter((r) => num(r.revision) !== null)
  if (numbered.length === 0) return null
  const newest = num(numbered[0]!.revision)!
  const newestKnown = newest === shown ? shownPlan : null
  return (
    <div className="pr-wrap">
      <div className="pr-picker" role="group" aria-label="Revisions" data-testid="pr-picker">
        {numbered.map((r) => {
          const n = num(r.revision)!
          const author = obj(r.author)
          const on = picked === null ? n === shown : n === picked
          return (
            <button
              key={n}
              type="button"
              className={`pr-rev${on ? ' is-on' : ''}`}
              aria-pressed={on}
              title={text(r.digest) ?? undefined}
              onClick={() => setPicked(n === shown || n === picked ? null : n)}
            >
              <b>Revision {n}</b>
              <span className="pd-dim">{who(author?.by)}</span>
              <RevisionCost cost={r.cost} />
            </button>
          )
        })}
        {!complete && (
          <span className="pt-chip" data-testid="pr-partial">
            history partial
            <InfoTip about="the revision history" tip="This run was planned before revisions were kept: only its current plan is served." />
          </span>
        )}
      </div>
      {picked !== null && <RevisionView runId={runId} revision={picked} newest={newest} newestKnown={newestKnown} onClose={() => setPicked(null)} />}
    </div>
  )
}
