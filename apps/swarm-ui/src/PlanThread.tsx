/**
 * THE PLAN'S THREAD IN THE MARGIN (docs/design/plans-pipeline.md §2.3
 * `D:P3a`, `D:P4a`-`D:P4c`, §3.4, §3.5, §5.3, §5.4, §8; lane U4
 * PLAN-THREAD-UI).
 *
 * WHAT IT DRAWS. Against each part of the document (PlanDocument.tsx's
 * `thread(anchor)` mount: `plan`, `requirements`, `step:<step_id>`), that
 * part's entries of the run read's `plan_thread`: comments, the planner's
 * answers, and -- in the plan's own margin -- the events derived from the run
 * (each revision with its cost, the approval, each step's task, the pull
 * request, each CI round, the review, the merge, the keyword), drawn in time
 * order. Resolved comments fold. A revision newer than the one on screen
 * carries its diff against it (PlanRevisions.tsx) and its cost on its row.
 *
 * WHO MAY (§8, read from the run's `may_decide`, never widened). Comment and
 * Resolve need the comment level (`may_decide.comment`); Request changes and
 * Cancel the re-plan need the edit level (`may_decide.edit`). A member below
 * them sees the thread without those controls. A run past PLANNED serves no
 * `may_decide`; comments stay open through DONE (§3.4), so the role floor
 * decides whether Comment is offered then, as it does for Approve on an older
 * API. A run read with no `plan_thread` (an older server, `D:P6d`) draws no
 * thread and no control.
 *
 * REQUEST CHANGES IN THE DECISION BAR (§2.3, §5.3). The bar (`PlanBar`) is
 * drawn by the page head and the document beside it, so the document
 * registers this run's re-plan state under the digest the bar names
 * (`useBarRegistration`) and the bar draws `ReplanControls` for that digest.
 * While a re-plan runs the bar says approving the shown revision cancels it
 * (the owner's answer to Q7) and offers Cancel the re-plan; the thread says
 * "re-planning", and the run is re-read until the re-plan ends. A refusal (a
 * moved plan, the cap, a role floor) is shown in the server's own words; the
 * cap's and the role's disable Request changes.
 *
 * DIAGRAMS IN THE THREAD (§2.5, §6.5; lane U8 DIAGRAMS-ON-PAGE, `DC:D2a`,
 * `DC:D3a`-`DC:D3c`). The planner's opening message (the `planned` event)
 * names each diagram of its revision with its `why` and a link to its tab on
 * the strip (PinnedDiagrams.tsx), or carries the plan's "no diagram" line; a
 * `revised` event names the diagrams its revision added or removed, and says
 * "diagrams only" when that was all it changed. Ask for a diagram sits beside
 * Comment in each part's composer, for the edit level only (Q6) and never
 * when this run's setting turned diagrams off; its waiting entry says
 * "drawing" with its elapsed time and its cost so far, and the run is re-read
 * until it is answered. The answer is a `diagram` entry with the diagram
 * drawn in it -- part of the conversation, not of the plan -- and Pin to the
 * plan makes revision N+1 that holds it; the entry then names it and links to
 * its tab. The cap's refusal disables the ask with the server's sentence.
 *
 * COMMENT TEXT IS OTHER PEOPLE'S TEXT: every field is a React text child,
 * never HTML.
 */

import { useCallback, useEffect, useState, useSyncExternalStore, type ReactNode } from 'react'
import { cancelReplan, commentOnPlan, loadRun, resolvePlanComment, revisePlan, type PlanAnchor } from './api'
import { Button } from './components'
import { errorHeading, type ApiError } from './fetch'
import { InfoTip } from './InfoTip'
import DiagramView from './DiagramView'
import { DiagramLink, askForDiagram, diagramOf, diagramSetting, diagramsOf, pinDiagram, sameDiagram, type DiagramAnchor, type PlanDiagram } from './PinnedDiagrams'
import { PlanDiff, RevisionCost, diffPlans, useRevisionPlan, who } from './PlanRevisions'
import { useRoleGate } from './roleGate'
import { timeAgo } from './Shell'
import { useNow } from './useNow'
import './styles/plan-thread.css'

type Json = Record<string, unknown>

const text = (v: unknown): string | null => (typeof v === 'string' && v.trim() !== '' ? v.trim() : null)
const texts = (v: unknown): string[] => (Array.isArray(v) ? v.flatMap((x) => text(x) ?? []) : [])
const objs = (v: unknown): Json[] => (Array.isArray(v) ? v.filter((x): x is Json => x !== null && typeof x === 'object' && !Array.isArray(x)) : [])
const obj = (v: unknown): Json | null => (v !== null && typeof v === 'object' && !Array.isArray(v) ? (v as Json) : null)
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)

/** How often the run is re-read while a re-plan runs. */
export const REPLAN_POLL_MS = 5_000
/** A comment's bound (§3.4, `MAX_THREAD_TEXT_CHARS`). */
export const COMMENT_MAX = 4_000
/** The re-plan states whose task is still awaited (issueruns `REPLAN_ACTIVE_STATES` and `REPLAN_READ_STATES`). */
const REPLAN_LIVE = new Set(['submitting', 'running', 'cancelling'])
const REPLAN_RUNNING = new Set(['submitting', 'running'])
/** Refusals after which asking again is refused again: Request changes is disabled with the server's sentence. */
const FINAL_REFUSALS = new Set(['replan_cap', 'edit_level_required', 'role_member_required'])

/** A diagram ask's states whose agent is still awaited (issueruns `DIAGRAM_ACTIVE_STATES`). */
const DIAGRAM_LIVE = new Set(['submitting', 'drawing'])
/** Refusals after which asking for a diagram again is refused again: Ask is disabled with the server's sentence. */
const FINAL_ASK_REFUSALS = new Set(['diagram_cap', 'diagrams_off', 'edit_level_required', 'role_member_required'])

export const replanLive = (r: Json | null): boolean => r !== null && REPLAN_LIVE.has(String(r.state))
export const replanRunning = (r: Json | null): boolean => r !== null && REPLAN_RUNNING.has(String(r.state))

// ---------------------------------------------------------------------------
// The conversation: the run read's thread, revisions and re-plan, kept live
// ---------------------------------------------------------------------------

export interface Conversation {
  runId: string | null
  /** Null when the run read serves no thread (an older server). */
  thread: Json[] | null
  revisions: Json[]
  complete: boolean
  replan: Json | null
  may: { comment: boolean; edit: boolean }
  /** Whether Ask for a diagram and Pin to the plan are offered: the edit level, a PLANNED run, diagrams not off (§6.4, §6.5). */
  mayDiagram: boolean
  /** Whether this run planned with diagrams on (`run.plan_settings`, §6.4): only then is an older revision read for its diagrams. */
  diagramsOn: boolean
  /** The run's current digest: the one Pin to the plan replaces. */
  current: string | null
  /** The run Pin to the plan returned on this page (revision N+1), else null; the strip draws its diagrams. */
  pinned: Json | null
  /** The diagram the strip shows, by id; null: its first. */
  tab: string | null
  /** The refusal that ended asking for diagrams on this run (the cap's), in the server's words. */
  capped: string | null
  addEntry: (entry: Json) => void
  setReplan: (replan: Json) => void
  setPinned: (run: Json) => void
  showTab: (diagramId: string) => void
  setCapped: (sentence: string) => void
}

const idOf = (e: Json): string | null => text(e.entry_id)

function mergeEntries(served: Json[], local: Json[]): Json[] {
  const byId = new Map(local.flatMap((e) => (idOf(e) === null ? [] : [[idOf(e)!, e] as const])))
  const out = served.map((e) => byId.get(idOf(e) ?? '') ?? e)
  const have = new Set(served.flatMap((e) => idOf(e) ?? []))
  for (const e of local) if (!have.has(idOf(e) ?? '')) out.push(e)
  return out
}

/** The entries this page wrote that `fresh` (a new run read) does not serve yet. */
function unserved(local: Json[], fresh: Json): Json[] {
  const have = new Set(objs(fresh.plan_thread).flatMap((e) => idOf(e) ?? []))
  return local.filter((e) => !have.has(idOf(e) ?? ''))
}

/** What the reader may do (§8): the run's `may_decide`, or -- past PLANNED, where none is served -- the role floor for a comment. */
function mayOf(md: Json | null | undefined, roleGate: string | null): { comment: boolean; edit: boolean } {
  if (md === undefined) return { comment: false, edit: false }
  if (md === null) return { comment: roleGate === null, edit: false }
  return { comment: md.comment === true || (md.comment === undefined && roleGate === null), edit: md.edit === true }
}

/**
 * The run's thread, revisions and re-plan, starting from the page's run read
 * and re-read while a re-plan runs. An entry this page wrote is kept until a
 * read serves it, so a write is never undone by a read that predates it.
 */
export function usePlanConversation(runId: string | null, run: Json | null): Conversation {
  const [served, setServed] = useState<Json | null>(run)
  const [local, setLocal] = useState<Json[]>([])
  const [ownReplan, setOwnReplan] = useState<Json | null>(null)
  const [pinned, setPinnedRun] = useState<Json | null>(null)
  const [tab, setTab] = useState<string | null>(null)
  const [capped, setCappedSentence] = useState<string | null>(null)
  useEffect(() => {
    setServed(run)
  }, [run])
  const base = served ?? run
  const replan = ownReplan ?? obj(base?.plan_replan)
  // Re-read while a re-plan runs, or while a diagram ask (served or just written) is being drawn.
  const drawing = [...objs(base?.plan_thread), ...local].some((e) => e.kind === 'diagram' && DIAGRAM_LIVE.has(String(e.state)))
  const live = replanLive(replan) || drawing
  useEffect(() => {
    if (runId === null || !live) return
    let alive = true
    const id = setInterval(() => {
      void loadRun(runId).then(
        (r) => {
          if (!alive || (r.status !== 'ok' && r.status !== 'stale')) return
          const fresh = r.data.run as unknown as Json
          setServed(fresh)
          setOwnReplan(null)
          setLocal((l) => unserved(l, fresh))
        },
        () => undefined,
      )
    }, REPLAN_POLL_MS)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [runId, live])
  const roleGate = useRoleGate('member', 'comment')
  const md = base?.may_decide === undefined ? undefined : obj(base?.may_decide)
  const may = mayOf(md, roleGate)
  const thread = base === null || !Array.isArray(base.plan_thread) ? null : mergeEntries(objs(base.plan_thread), local)
  const addEntry = useCallback((entry: Json) => setLocal((l) => [...l.filter((e) => idOf(e) !== idOf(entry)), entry]), [])
  const setReplan = useCallback((r: Json) => setOwnReplan(r), [])
  const setPinned = useCallback((r: Json) => {
    setPinnedRun(r)
    setServed(r)
  }, [])
  const showTab = useCallback((id: string) => setTab(id), [])
  const setCapped = useCallback((sentence: string) => setCappedSentence(sentence), [])
  const shownMay = thread === null ? { comment: false, edit: false } : may
  return {
    runId,
    thread,
    revisions: objs(base?.plan_revisions),
    complete: base?.plan_history_complete !== false,
    replan,
    may: shownMay,
    mayDiagram: shownMay.edit && base?.state === 'PLANNED' && diagramSetting(base).state !== 'off',
    diagramsOn: diagramSetting(base).state === 'on',
    current: text(base?.plan_digest),
    pinned,
    tab,
    capped,
    addEntry,
    setReplan,
    setPinned,
    showTab,
    setCapped,
  }
}

/** The open comments a request for changes sends: unresolved, unanswered, on the revision shown. */
export function openComments(thread: Json[] | null, digest: string | null): Json[] {
  if (thread === null || digest === null) return []
  return thread.filter((e) => e.kind === 'comment' && e.resolved !== true && (e.answered_by_revision ?? null) === null && e.on_digest === digest)
}

// ---------------------------------------------------------------------------
// The bar's re-plan controls, registered by the document under its digest
// ---------------------------------------------------------------------------

interface BarEntry {
  runId: string
  digest: string
  revision: number | null
  open: number
  replan: Json | null
  mayEdit: boolean
  onReplan: (replan: Json) => void
}

const bars = new Map<string, BarEntry>()
const listeners = new Set<() => void>()
const subscribe = (f: () => void) => {
  listeners.add(f)
  return () => {
    listeners.delete(f)
  }
}
const notify = () => listeners.forEach((f) => f())

/** The document says what Request changes acts on, for the bar naming `digest`. */
export function useBarRegistration(entry: BarEntry | null) {
  const key = entry?.digest ?? null
  const runId = entry?.runId ?? null
  const revision = entry?.revision ?? null
  const open = entry?.open ?? 0
  const replan = entry?.replan ?? null
  const mayEdit = entry?.mayEdit ?? false
  const onReplan = entry?.onReplan ?? null
  useEffect(() => {
    if (key === null || runId === null || onReplan === null) return
    bars.set(key, { runId, digest: key, revision, open, replan, mayEdit, onReplan })
    notify()
    return () => {
      bars.delete(key)
      notify()
    }
  }, [key, runId, revision, open, replan, mayEdit, onReplan])
}

const plural = (n: number, one: string) => `${n} ${one}${n === 1 ? '' : 's'}`

/**
 * Request changes, or -- while a re-plan runs -- what Approve does to it and
 * Cancel the re-plan (§5.3). Nothing when the document registered nothing
 * for this digest (no thread served, or the run is not the one shown).
 */
export function ReplanControls({ digest }: Readonly<{ digest: string | null }>) {
  const entry = useSyncExternalStore(subscribe, () => (digest === null ? undefined : bars.get(digest)))
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  if (entry === undefined) return null
  const running = replanRunning(entry.replan)
  if (running) {
    const asked = entry.replan === null ? 0 : texts(entry.replan.entries).length
    const replanId = entry.replan === null ? null : text(entry.replan.replan_id)
    const cancel = async () => {
      if (replanId === null) return
      setBusy(true)
      setRefused(null)
      const r = await cancelReplan(entry.runId, replanId)
      setBusy(false)
      if (r.status === 'ok') entry.onReplan(obj(r.data.replan) ?? { ...entry.replan, state: 'cancelling' })
      else if (r.status === 'error') setRefused(r.error)
    }
    return (
      <span className="pt-bar" data-testid="pt-bar">
        <span className="pt-chip is-live">re-planning</span>
        <span className="pt-bar-note" data-testid="pt-approve-cancels">
          {plural(asked, 'comment')}; approving {entry.revision === null ? 'this revision' : `revision ${entry.revision}`} cancels it
        </span>
        {entry.mayEdit && replanId !== null && (
          <Button kind="ghost" size="sm" busy={busy} onClick={() => void cancel()}>
            Cancel the re-plan
          </Button>
        )}
        {refused !== null && (
          <span className="pt-refused" role="alert" data-code={refused.code ?? undefined}>
            {refused.message || errorHeading(refused)}
          </span>
        )}
      </span>
    )
  }
  if (!entry.mayEdit) return null
  const final = refused !== null && refused.code !== null && FINAL_REFUSALS.has(refused.code)
  const ask = async () => {
    setBusy(true)
    setRefused(null)
    const r = await revisePlan(entry.runId, entry.digest)
    setBusy(false)
    if (r.status === 'ok') entry.onReplan(obj(r.data.replan) ?? { state: 'running', from_digest: entry.digest, entries: [] })
    else if (r.status === 'error') setRefused(r.error)
  }
  return (
    <span className="pt-bar" data-testid="pt-bar">
      {final ? (
        <Button kind="secondary" disabledReason={refused.message || errorHeading(refused)}>
          Request changes
        </Button>
      ) : (
        <Button kind="secondary" busy={busy} disabled={entry.open === 0} onClick={() => void ask()}>
          Request changes
        </Button>
      )}
      {!final && entry.open === 0 && <InfoTip about="Request changes" tip="No open comment on this revision: comment first, then the planner answers every open comment with a new revision." />}
      {refused !== null && !final && (
        <span className="pt-refused" role="alert" data-code={refused.code ?? undefined}>
          {refused.message || errorHeading(refused)}
        </span>
      )}
    </span>
  )
}

// ---------------------------------------------------------------------------
// The margin's thread
// ---------------------------------------------------------------------------

/** The margin key an entry belongs to: `plan`, `requirements` or `step:<id>`. */
export function anchorKey(e: Json, stepIds: ReadonlySet<string>): string {
  if (e.kind === 'event') return 'plan'
  const a = obj(e.anchor)
  const part = text(a?.part)
  const ref = text(a?.ref)
  if (part === 'step' && ref !== null && stepIds.has(ref)) return `step:${ref}`
  if (part === 'requirement') return 'requirements'
  return 'plan'
}

/** The anchor a composer in the margin keyed `key` posts. */
function anchorOf(key: string, requirement: string): PlanAnchor {
  if (key.startsWith('step:')) return { part: 'step', ref: key.slice(5) }
  if (key === 'requirements') return { part: 'requirement', ref: requirement }
  return { part: 'plan', ref: null }
}

const moment = (e: Json): number => {
  const t = typeof e.at === 'string' ? Date.parse(e.at) : NaN
  return Number.isNaN(t) ? Number.POSITIVE_INFINITY : t
}

/** In time order; equal moments keep the order they were served in. */
export function inOrder(entries: Json[]): Json[] {
  return entries
    .map((e, i) => ({ e, i }))
    .sort((a, b) => moment(a.e) - moment(b.e) || a.i - b.i)
    .map((x) => x.e)
}

const lower = (v: unknown): string | null => (text(v) === null ? null : text(v)!.toLowerCase().replace(/_/g, ' '))

/** An event's words: short, lower-case state words, never the API's enums. */
function eventWords(e: Json): ReactNode {
  const rev = num(e.revision)
  switch (e.event) {
    case 'planned':
      return <>planned{rev !== null && <span className="pt-chip">r{rev}</span>}</>
    case 'revised':
      return (
        <>
          {rev === null ? 'revised' : <span className="pt-rev">r{rev}</span>}
          <span className="pd-dim">{who(e.by)}</span>
          {text(e.made_by) !== null && <span className="pt-chip">{lower(e.made_by)}</span>}
        </>
      )
    case 'approved':
      return (
        <>
          approved{rev !== null && <span className="pt-chip">r{rev}</span>}
          <span className="pd-dim">{who(e.by)}</span>
        </>
      )
    case 'rejected':
      return (
        <>
          rejected<span className="pd-dim">{who(e.by)}</span>
          {text(e.reason) !== null && <InfoTip about="the reason" tip={text(e.reason)!} />}
        </>
      )
    case 'step_task':
      return (
        <>
          <code className="pd-mono">{text(e.ref) ?? 'step'}</code>
          {text(e.task_id) !== null && <code className="pd-mono pd-dim">{text(e.task_id)}</code>}
          {lower(e.state) !== null && <span className="pt-chip">{lower(e.state)}</span>}
        </>
      )
    case 'pull_request': {
      const n = num(e.number) ?? text(e.ref)
      const url = text(e.url)
      return url !== null && /^https:\/\//.test(url) ? (
        <a href={url} target="_blank" rel="noreferrer">
          PR #{n}
        </a>
      ) : (
        <>PR #{n}</>
      )
    }
    case 'ci_round':
      return (
        <>
          CI round {num(e.round) ?? '?'}
          {lower(e.state) !== null && <span className="pt-chip">{lower(e.state)}</span>}
        </>
      )
    case 'review': {
      const unmet = texts(e.requirements_unmet)
      return e.requirements_met === true ? (
        <>requirements met</>
      ) : (
        <>
          {plural(unmet.length, 'requirement')} unmet
          {unmet.length > 0 && <InfoTip about="the unmet requirements" tip={unmet.join(' · ')} />}
        </>
      )
    }
    case 'merged':
      return <>merged #{num(e.number) ?? text(e.ref)}</>
    case 'keyword':
      return <>{text(e.keyword) ?? text(e.ref)}</>
    default:
      return <>{lower(e.event) ?? 'event'}</>
  }
}

// ---------------------------------------------------------------------------
// Diagrams in the thread (§2.5, §6.5)
// ---------------------------------------------------------------------------

/** The plan the strip draws: the revision Pin to the plan made on this page, else the one on screen. */
const stripPlan = (c: Conversation, shownPlan: Json): Json => obj(c.pinned?.plan) ?? shownPlan

/** A diagram a message names: a link to its tab when the strip holds it, else its title alone. */
function Named({ d, c, shownPlan }: Readonly<{ d: PlanDiagram; c: Conversation; shownPlan: Json }>) {
  const on = diagramsOf(stripPlan(c, shownPlan)).find((x) => sameDiagram(x, d)) ?? null
  return on === null ? <span className="pt-dname">{d.title}</span> : <DiagramLink d={on} c={c} />
}

/**
 * A revision's plan: the one on screen without a read, any other by `GET
 * .../plan-revisions/{n}` -- and that only when this run planned with
 * diagrams on, since a run planned without them has none to name.
 */
function useRevisionAt(ctx: EntryCtx, revision: number | null): Json | null {
  const known = revision !== null && revision === ctx.shownRevision ? ctx.shownPlan : null
  const read = revision !== null && (known !== null || ctx.c.diagramsOn)
  const got = useRevisionPlan(read ? ctx.c.runId : null, read ? revision : null, known)
  return got !== null && got.status === 'ok' ? got.plan : null
}

/** The planner's opening message (`DC:D1a`, `DC:D2a`): each diagram of its revision with its why, or the plan's no-diagram line. */
function OpeningDiagrams({ revision, ctx }: Readonly<{ revision: number | null; ctx: EntryCtx }>) {
  const plan = useRevisionAt(ctx, revision)
  if (plan === null) return null
  const drawn = diagramsOf(plan)
  const none = text(plan.no_diagram)
  if (drawn.length === 0)
    return none === null ? null : (
      <p className="pt-text pt-nodiagram" data-testid="pt-no-diagram">
        <span className="pd-dim">no diagram:</span> {none}
      </p>
    )
  return (
    <ul className="pt-diagrams" aria-label="Diagrams" data-testid="pt-diagrams">
      {drawn.map((d) => (
        <li key={d.id}>
          <Named d={d} c={ctx.c} shownPlan={ctx.shownPlan} />
          {d.why !== null && <span className="pd-dim">{d.why}</span>}
        </li>
      ))}
    </ul>
  )
}

const isDiagramPath = (path: unknown): boolean => typeof path === 'string' && /^(diagrams|no_diagram)(\.|$)/.test(path)

/** Whether a revision changed nothing but its diagrams (`changed[]`, §3.3), so its chip says "diagrams only". */
export function diagramsOnly(revision: Json | null, madeBy: unknown): boolean {
  if (madeBy === 'add_diagram') return true
  const changed = objs(revision?.changed)
  return changed.length > 0 && changed.every((ch) => isDiagramPath(ch.path))
}

/** The diagrams a revision added or removed (`DC:D3c`), read only when its `changed[]` names a diagram path. */
function RevisedDiagrams({ revision, ctx }: Readonly<{ revision: number; ctx: EntryCtx }>) {
  const after = useRevisionAt(ctx, revision)
  const before = useRevisionAt(ctx, revision - 1)
  if (after === null || before === null) return null
  const was = diagramsOf(before)
  const now = diagramsOf(after)
  const added = now.filter((d) => !was.some((x) => sameDiagram(x, d)))
  const removed = was.filter((d) => !now.some((x) => sameDiagram(x, d)))
  if (added.length === 0 && removed.length === 0) return null
  return (
    <ul className="pt-diagrams" aria-label="Diagrams changed" data-testid="pt-diagrams-changed">
      {added.map((d) => (
        <li key={`+${d.id}`} data-op="add">
          <span className="pt-op" aria-label="added">+</span>
          <Named d={d} c={ctx.c} shownPlan={ctx.shownPlan} />
        </li>
      ))}
      {removed.map((d) => (
        <li key={`-${d.id}`} data-op="remove">
          <span className="pt-op" aria-label="removed">−</span>
          <span className="pt-dname">{d.title}</span>
        </li>
      ))}
    </ul>
  )
}

/**
 * A `diagram` entry (§3.4, §6.5): waiting, it says "drawing" with its elapsed
 * time and cost so far; answered, it draws the diagram and offers Pin to the
 * plan to the edit level; pinned, it names the diagram and links to its tab.
 */
function DiagramEntry({ e, ctx }: Readonly<{ e: Json; ctx: EntryCtx }>) {
  const { c, shownRevision, shownPlan } = ctx
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const id = text(e.entry_id)
  const state = text(e.state) ?? 'drawn'
  const drawn = diagramOf(e.diagram)
  const onRev = num(e.on_revision)
  const onStrip = drawn === null ? null : (diagramsOf(stripPlan(c, shownPlan)).find((d) => sameDiagram(d, drawn)) ?? null)
  const pin = async () => {
    if (c.runId === null || c.current === null || id === null) return
    setBusy(true)
    setRefused(null)
    const r = await pinDiagram(c.runId, c.current, id)
    setBusy(false)
    if (r.status === 'ok' || r.status === 'stale') {
      const next = obj(r.data.run)
      if (next === null) return
      c.setPinned(next)
      const joined = drawn === null ? null : (diagramsOf(obj(next.plan)).find((d) => sameDiagram(d, drawn)) ?? null)
      if (joined !== null) c.showTab(joined.id)
    } else if (r.status === 'error') setRefused(r.error)
  }
  return (
    <li className="pt-entry is-diagram" data-testid="pt-entry" data-kind="diagram" data-state={state} data-entry={id ?? undefined}>
      <span className="pt-head">
        <span className="pt-by">{who(e.by)}</span>
        {DIAGRAM_LIVE.has(state) && <span className="pt-chip is-live">drawing</span>}
        {state === 'failed' && <span className="pt-chip">failed</span>}
        {onRev !== null && onRev !== shownRevision && <span className="pt-chip">r{onRev}</span>}
        {e.cost !== undefined && e.cost !== null && <RevisionCost cost={e.cost} />}
        <When at={e.at} />
      </span>
      {text(e.ask) !== null && <p className="pt-text pt-ask">{text(e.ask)}</p>}
      {text(e.text) !== null && <p className="pt-text">{text(e.text)}</p>}
      {drawn !== null && <DiagramView source={drawn.source} title={drawn.title} why={drawn.why ?? undefined} size="strip" />}
      {onStrip !== null ? (
        <p className="pt-pinned" data-testid="pt-pinned">
          <span className="pt-chip is-ok">pinned</span>
          <DiagramLink d={onStrip} c={c} />
        </p>
      ) : (
        drawn !== null &&
        state === 'drawn' &&
        c.mayDiagram &&
        c.current !== null &&
        id !== null && (
          <span className="pt-acts">
            <Button kind="secondary" size="sm" busy={busy} aria-label="Pin to the plan" title="Pin to the plan: a new revision holding this diagram" onClick={() => void pin()}>
              Pin
            </Button>
          </span>
        )
      )}
      {refused !== null && (
        <span className="pt-refused" role="alert" data-code={refused.code ?? undefined}>
          {refused.message || errorHeading(refused)}
        </span>
      )}
    </li>
  )
}

/** A revision newer than the one on screen: its diff against the shown plan. */
function NewerDiff({ runId, revision, shownPlan }: Readonly<{ runId: string; revision: number; shownPlan: Json }>) {
  const got = useRevisionPlan(runId, revision)
  if (got === null || got.status === 'loading') return <span className="pd-dim">…</span>
  if (got.status === 'error')
    return (
      <span className="pt-refused" role="alert">
        {got.error.message || errorHeading(got.error)}
      </span>
    )
  return <PlanDiff rows={diffPlans(shownPlan, got.plan)} />
}

function When({ at }: Readonly<{ at: unknown }>) {
  const now = useNow()
  const t = text(at)
  if (t === null) return null
  return (
    <time className="pt-when pd-dim" dateTime={t} title={t}>
      {timeAgo(t, now)}
    </time>
  )
}

interface EntryCtx {
  c: Conversation
  shownRevision: number | null
  shownPlan: Json
}

function Entry({ e, ctx }: Readonly<{ e: Json; ctx: EntryCtx }>) {
  const { c, shownRevision, shownPlan } = ctx
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  if (e.kind === 'diagram') return <DiagramEntry e={e} ctx={ctx} />
  if (e.kind === 'event') {
    const rev = num(e.revision)
    const revision = rev === null || (e.event !== 'planned' && e.event !== 'revised') ? null : (c.revisions.find((r) => num(r.revision) === rev) ?? null)
    const newer = e.event === 'revised' && rev !== null && shownRevision !== null && rev > shownRevision && c.runId !== null
    const diagramsChanged = e.event === 'revised' && rev !== null && rev > 1 && (e.made_by === 'add_diagram' || objs(revision?.changed).some((ch) => isDiagramPath(ch.path)))
    return (
      <li className="pt-entry is-event" data-testid="pt-entry" data-kind="event" data-event={String(e.event)} data-revision={rev ?? undefined}>
        <span className="pt-head">
          {eventWords(e)}
          {e.event === 'revised' && diagramsOnly(revision, e.made_by) && <span className="pt-chip">diagrams only</span>}
          {revision !== null && <RevisionCost cost={revision.cost} />}
          <When at={e.at} />
        </span>
        {e.event === 'planned' && <OpeningDiagrams revision={rev} ctx={ctx} />}
        {diagramsChanged && <RevisedDiagrams revision={rev} ctx={ctx} />}
        {newer && <NewerDiff runId={c.runId!} revision={rev} shownPlan={shownPlan} />}
      </li>
    )
  }
  const id = text(e.entry_id)
  const onRev = num(e.on_revision)
  const answered = num(e.answered_by_revision)
  const resolve = async () => {
    if (c.runId === null || id === null) return
    setBusy(true)
    setRefused(null)
    const r = await resolvePlanComment(c.runId, id, true)
    setBusy(false)
    if (r.status === 'ok') c.addEntry(obj(r.data.entry) ?? { ...e, resolved: true })
    else if (r.status === 'error') setRefused(r.error)
  }
  return (
    <li className={`pt-entry is-${String(e.kind)}`} data-testid="pt-entry" data-kind={String(e.kind)} data-entry={id ?? undefined}>
      <span className="pt-head">
        <span className="pt-by">{who(e.by)}</span>
        {onRev !== null && onRev !== shownRevision && <span className="pt-chip">r{onRev}</span>}
        {answered !== null && <span className="pt-chip is-ok">answered r{answered}</span>}
        {e.cost !== undefined && e.cost !== null && <RevisionCost cost={e.cost} />}
        <When at={e.at} />
      </span>
      <p className="pt-text">{typeof e.text === 'string' ? e.text : ''}</p>
      {e.kind === 'comment' && e.resolved !== true && c.may.comment && id !== null && (
        <span className="pt-acts">
          <Button kind="ghost" size="sm" busy={busy} onClick={() => void resolve()}>
            Resolve
          </Button>
        </span>
      )}
      {refused !== null && (
        <span className="pt-refused" role="alert" data-code={refused.code ?? undefined}>
          {refused.message || errorHeading(refused)}
        </span>
      )}
    </li>
  )
}

/** An ask's bound (§6.5, `MAX_DIAGRAM_ASK_CHARS`). */
export const ASK_MAX = 1_000

function Composer({ c, anchorKey: key, label, digest, requirements }: Readonly<{ c: Conversation; anchorKey: string; label: string; digest: string; requirements: number }>) {
  // Closed, or open to write a comment or to ask for a diagram of this part.
  const [mode, setMode] = useState<'comment' | 'diagram' | null>(null)
  const open = mode !== null
  const setOpen = (to: boolean) => setMode(to ? 'comment' : null)
  const [body, setBody] = useState('')
  const [req, setReq] = useState('1')
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  if (!open)
    return (
      <span className="pt-acts">
        <Button kind="ghost" size="sm" onClick={() => setOpen(true)}>
          Comment
        </Button>
        {/* Ask for a diagram (§6.5): the edit level only (Q6), never with diagrams off for this run. */}
        {c.mayDiagram && (
          <Button kind="ghost" size="sm" aria-label="Ask for a diagram" title={c.capped ?? 'Ask for a diagram of this part'} disabled={c.capped !== null} onClick={() => setMode('diagram')}>
            Diagram
          </Button>
        )}
      </span>
    )
  const asking = mode === 'diagram'
  const post = async () => {
    if (c.runId === null || body.trim() === '') return
    setBusy(true)
    setRefused(null)
    const anchor = anchorOf(key, req)
    // An ask answers 202 with its pending `diagram` entry, a comment 201 with the comment: both join the thread.
    const r = asking ? await askForDiagram(c.runId, digest, anchor as DiagramAnchor, body.trim()) : await commentOnPlan(c.runId, digest, anchor, body.trim())
    setBusy(false)
    if (r.status === 'ok' || (asking && r.status === 'stale')) {
      const entry = obj(r.data.entry)
      if (entry !== null) c.addEntry(entry)
      setBody('')
      setMode(null)
    } else if (r.status === 'error') {
      if (asking && r.error.code !== null && FINAL_ASK_REFUSALS.has(r.error.code)) c.setCapped(r.error.message || errorHeading(r.error))
      setRefused(r.error)
    }
  }
  return (
    <div className="pt-compose">
      {key === 'requirements' && requirements > 0 && (
        <select className="pt-select" aria-label="Requirement" value={req} onChange={(ev) => setReq(ev.target.value)}>
          {Array.from({ length: requirements }, (_, i) => (
            <option key={i} value={String(i + 1)}>
              R{i + 1}
            </option>
          ))}
        </select>
      )}
      <textarea
        className="pt-box"
        aria-label={asking ? `Ask for a diagram of ${label}` : `Comment on ${label}`}
        rows={3}
        maxLength={asking ? ASK_MAX : COMMENT_MAX}
        value={body}
        onChange={(ev) => setBody(ev.target.value)}
      />
      <span className="pt-acts">
        <Button kind="primary" size="sm" busy={busy} disabled={body.trim() === '' || (asking && c.capped !== null)} onClick={() => void post()}>
          {asking ? 'Ask' : 'Post'}
        </Button>
        <Button
          kind="ghost"
          size="sm"
          onClick={() => {
            setOpen(false)
            setRefused(null)
          }}
        >
          Cancel
        </Button>
      </span>
      {refused !== null && (
        <span className="pt-refused" role="alert" data-code={refused.code ?? undefined}>
          {refused.message || errorHeading(refused)}
        </span>
      )}
    </div>
  )
}

/**
 * One part's thread in the margin: its open entries in time order, its
 * resolved comments folded, and the composer when the reader may comment.
 * `digest` is the revision on screen, the one a comment is written on.
 */
export function PlanThread({
  c,
  anchorKey: key,
  label,
  plan,
  digest,
  shownRevision,
}: Readonly<{
  c: Conversation
  anchorKey: string
  label: string
  plan: Json
  digest: string | null
  shownRevision: number | null
}>) {
  if (c.thread === null) return null
  const stepIds = new Set(objs(plan.steps).flatMap((s, i) => text(s.step_id) ?? `step-${i + 1}`))
  const mine = inOrder(c.thread.filter((e) => anchorKey(e, stepIds) === key))
  const open = mine.filter((e) => !(e.kind === 'comment' && e.resolved === true))
  const folded = mine.filter((e) => e.kind === 'comment' && e.resolved === true)
  const ctx: EntryCtx = { c, shownRevision, shownPlan: plan }
  const replanning = key === 'plan' && replanRunning(c.replan)
  if (mine.length === 0 && !c.may.comment && !replanning) return null
  return (
    <div className="pt-thread" data-testid="pt-thread" data-anchor={key}>
      {open.length > 0 && (
        <ol className="pt-list" aria-label={`${label}: thread`}>
          {open.map((e, i) => (
            <Entry key={text(e.entry_id) ?? `${String(e.event)}:${i}:${String(e.at)}`} e={e} ctx={ctx} />
          ))}
        </ol>
      )}
      {replanning && (
        <p className="pt-replanning" data-testid="pt-replanning">
          <span className="pt-chip is-live">re-planning</span>
          {c.replan !== null && num(c.replan.from_revision) !== null && <span className="pt-chip">r{num(c.replan.from_revision)}</span>}
          {c.replan !== null && <span className="pd-dim">{plural(texts(c.replan.entries).length, 'comment')}</span>}
        </p>
      )}
      {folded.length > 0 && (
        <details className="pt-resolved" data-testid="pt-resolved">
          <summary>{folded.length} resolved</summary>
          <ol className="pt-list" aria-label={`${label}: resolved`}>
            {folded.map((e, i) => (
              <Entry key={text(e.entry_id) ?? `resolved:${i}`} e={e} ctx={ctx} />
            ))}
          </ol>
        </details>
      )}
      {c.may.comment && digest !== null && <Composer c={c} anchorKey={key} label={label} digest={digest} requirements={texts(plan.requirements).length} />}
    </div>
  )
}
