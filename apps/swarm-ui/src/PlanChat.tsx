/**
 * THE PLAN REVIEW CHAT (docs/design/plans-pipeline.md §7, §7.8, §2.5
 * `DC:D8a`-`DC:D8c`, `DC:D7c`; lane U9a PLAN-CHAT-UI, the component alone).
 *
 * WHAT IT DRAWS. A column (`<aside aria-label="Review chat">`) holding the
 * run's transcript (`GET /v1/runs/{id}/plan-chat`, oldest first, every member
 * reads it, Q10): each turn's ask, the answer, its cost and the model it ran
 * (`model.model_id`, or the class with the reason the attempt recorded none),
 * the lines saying what was dropped and why, the turn's diagrams and its
 * proposal. Its head names the agent and the run's review spend against its
 * cap ("$6.40 of $25" from `plan_review_usage`, "at least" when a cost was
 * unreported, amber from 80%, the refusal's sentence at a cap) and keeps the
 * slot the per-plan model control fills (`modelControl`, lane U13
 * PLAN-CHAT-MODEL); this lane draws nothing there of its own.
 *
 * ASKING (§7.1). One turn in flight per run: an ask posts the digest on
 * screen and the text (never a profile, model or resource, invariant 10),
 * answers 202 with the turn, and the turn is streamed as server-sent events
 * from `…/plan-chat/{turn}/stream`: "starting" before the agent, each text
 * delta as it comes, then the final turn, which replaces the deltas. The
 * stream reconnects by the turn's id when the server says so or after 60
 * seconds without a byte. A turn already in flight when the chat opens is
 * streamed the same way. A busy chat (409 `chat_busy`) and a cap (429
 * `chat_cap`) answer in the server's words; the cap disables the ask, as the
 * head already does before the click from the same usage.
 *
 * WHO MAY (§7.6, the run read's `may_decide`, never widened). The comment
 * level asks and pins to the thread; the edit level pins a diagram to the
 * plan and acts on a proposal. A viewer reads. After the decision (the run
 * not PLANNED, or the transcript read's `open: false`) the chat is read-only.
 *
 * PINNING (§7.5, §6.5). Pin to the thread copies an answered turn into the
 * thread at the turn's anchor (the whole plan when it had none). Pin to the
 * plan sends a chat diagram through `plan:add-diagram` against the CURRENT
 * digest and hands the run read it answers (revision N+1) to `onRun`.
 *
 * A PROPOSAL (§7.4) shows its diff against the plan on screen. Request
 * changes pins the answer to the thread (a pinned answer is a comment like any
 * other) and then asks the re-plan on the shown digest; Edit with this hands
 * its plan and the digest it would replace to `onEdit`, which opens the
 * page's editor so a person saves it as their own revision. The agent never
 * applies a revision.
 *
 * TEXT FROM THE AGENT IS DRAWN AS TEXT: every field is a React text child,
 * never HTML, and markup in an answer is shown as written.
 */

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { revisePlan } from './api'
import { Button } from './components'
import DiagramView from './DiagramView'
import { errorHeading, route, write, read, type ApiError, type Result } from './fetch'
import { InfoTip } from './InfoTip'
import { diagramOf, type DiagramAnchor, type PlanDiagram } from './PinnedDiagrams'
import { PlanDiff, RevisionCost, diffPlans, who } from './PlanRevisions'
import './styles/plan-thread.css'
import './styles/plan-chat.css'

type Json = Record<string, unknown>

const text = (v: unknown): string | null => (typeof v === 'string' && v.trim() !== '' ? v : null)
const obj = (v: unknown): Json | null => (v !== null && typeof v === 'object' && !Array.isArray(v) ? (v as Json) : null)
const objs = (v: unknown): Json[] => (Array.isArray(v) ? v.filter((x): x is Json => obj(x) !== null) : [])
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const texts = (v: unknown): string[] => (Array.isArray(v) ? v.flatMap((x) => text(x) ?? []) : [])

/** An ask's bound (`planchat.MAX_ASK_CHARS`). */
export const CHAT_ASK_MAX = 2_000
/** The console closes a stream silent this long and reconnects by the turn's id (§7.1). */
export const CHAT_STREAM_IDLE_MS = 60_000
/** The share of the review dollars from which the head turns amber (§7.8). */
export const CHAT_SPEND_NEAR = 0.8
/** Where a cap is set (`issueruns.REPLAN_CAP_WHERE`), in the refusal's sentence. */
const CAP_WHERE = 'Admin › Settings'
const LIVE = new Set(['starting', 'answering'])

// ---------------------------------------------------------------------------
// The routes (swarm-api routes/runs.py, the plan review chat)
// ---------------------------------------------------------------------------

/** The run's transcript, oldest first, with its usage of the chat's caps. */
export function loadPlanChat(runId: string): Promise<Result<Json>> {
  return read<Json>(route('/v1/runs/{id}/plan-chat', { id: runId }), () => false)
}

/** Ask the review agent one question about the revision on screen: 202 with the turn. */
export function askPlanChat(runId: string, planDigest: string, ask: string, anchor: DiagramAnchor | null): Promise<Result<{ turn?: Json }>> {
  const body: Json = { plan_digest: planDigest, ask }
  if (anchor !== null) body.anchor = anchor
  return write(route('/v1/runs/{id}/plan-chat', { id: runId }), 'POST', body) as Promise<Result<{ turn?: Json }>>
}

/** Copy an answered turn into the thread at `anchor`: 201 with the `chat_pin` entry. */
export function pinChatToThread(runId: string, turnId: string, anchor: DiagramAnchor): Promise<Result<{ entry?: Json }>> {
  return write(route('/v1/runs/{id}/plan-chat/{turn}:pin', { id: runId, turn: turnId }), 'POST', { anchor }) as Promise<Result<{ entry?: Json }>>
}

/** Add a chat turn's diagram to the CURRENT revision: revision N+1, by the person pinning. */
export function pinChatDiagram(runId: string, planDigest: string, turnId: string, diagramId: string): Promise<Result<{ run?: Json }>> {
  return write(route('/v1/runs/{id}/plan:add-diagram', { id: runId }), 'POST', { plan_digest: planDigest, from: { chat_turn_id: turnId, diagram_id: diagramId } }) as Promise<Result<{ run?: Json }>>
}

/** The stream's address: an `EventSource` sends the session's credentials as the page's other reads do. */
export const chatStreamUrl = (runId: string, turnId: string): string => route('/v1/runs/{id}/plan-chat/{turn}/stream', { id: runId, turn: turnId }).url

// ---------------------------------------------------------------------------
// The spend against the cap (§7.8)
// ---------------------------------------------------------------------------

const dollars = (n: number): string => (Number.isInteger(n) ? `$${n}` : `$${n.toFixed(2)}`)

export interface ChatSpend {
  spent: number
  cap: number
  atLeast: boolean
  level: 'ok' | 'near' | 'cap'
  /** The server's sentence for the cap reached, or null below both caps. */
  refusal: string | null
}

/** The run's usage of the chat's caps, read as §7.8 serves it; null when the server serves none. */
export function chatSpend(usage: unknown): ChatSpend | null {
  const u = obj(usage)
  const usd = obj(u?.review_usd)
  const spent = num(usd?.spent)
  const cap = num(usd?.cap)
  if (u === null || spent === null || cap === null) return null
  const turns = obj(u.chat_turns)
  const used = num(turns?.used)
  const limit = num(turns?.cap)
  // The sentences are planchat.ChatCap's, so the head says before the click what the server would answer.
  let refusal: string | null = null
  if (used !== null && limit !== null && used >= limit)
    refusal = `This run has had its ${limit} chat turns. The cap is a platform setting (${CAP_WHERE}); comment, ask for changes, or edit the plan by hand.`
  else if (spent >= cap)
    refusal = `This run has spent $${spent.toFixed(2)} of its $${cap.toFixed(2)} of review agents (chat turns and diagram agents; unreported costs counted). The cap is a platform setting (${CAP_WHERE}); comment, ask for changes, or edit the plan by hand.`
  let level: ChatSpend['level'] = 'ok'
  if (refusal !== null) level = 'cap'
  else if (cap > 0 && spent >= cap * CHAT_SPEND_NEAR) level = 'near'
  return { spent, cap, atLeast: usd?.at_least === true, level, refusal }
}

/** A refusal's sentence: the server's `detail` when it is one (a 429's message is the console's own), else its message. */
function said(e: ApiError): string {
  if (e.code === 'chat_cap' && typeof e.detail === 'string' && e.detail.trim() !== '') return e.detail
  return e.message || errorHeading(e)
}

function Refused({ e }: Readonly<{ e: ApiError | string }>) {
  return (
    <span className="pt-refused" role="alert" data-code={typeof e === 'string' ? undefined : (e.code ?? undefined)}>
      {typeof e === 'string' ? e : said(e)}
    </span>
  )
}

// ---------------------------------------------------------------------------
// One turn
// ---------------------------------------------------------------------------

interface TurnCtx {
  runId: string
  /** The digest on screen: a comment, a pin and a re-plan are about it. */
  digest: string
  /** The run's current digest: Pin to the plan makes the revision after it. */
  current: string
  plan: Json
  mayComment: boolean
  mayEdit: boolean
  onRun: (run: Json) => void
  onEdit: ((plan: Json, replaces: string) => void) | undefined
  replace: (turn: Json) => void
}

function Model({ model }: Readonly<{ model: unknown }>) {
  const m = obj(model)
  if (m === null) return null
  const id = text(m.model_id)
  const cls = text(m.class)
  const from = text(m.from)
  const why = text(m.why_none) ?? text(m.fell_through)
  const title = [cls, from === null ? null : `from the ${from}`, why].filter((x) => x !== null).join(' · ')
  return (
    <code className="pc-model" data-testid="pc-model" title={title || undefined}>
      {id ?? cls ?? '—'}
    </code>
  )
}

function ChatDiagram({ turnId, d, ctx }: Readonly<{ turnId: string; d: PlanDiagram; ctx: TurnCtx }>) {
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const pin = async () => {
    setBusy(true)
    setRefused(null)
    const r = await pinChatDiagram(ctx.runId, ctx.current, turnId, d.id)
    setBusy(false)
    if (r.status === 'ok' || r.status === 'stale') {
      const next = obj(r.data.run)
      if (next !== null) ctx.onRun(next)
    } else if (r.status === 'error') setRefused(r.error)
  }
  return (
    <div className="pc-diagram">
      <DiagramView source={d.source} title={d.title} why={d.why ?? undefined} size="strip" />
      {ctx.mayEdit && (
        <span className="pt-acts">
          <Button kind="secondary" size="sm" busy={busy} aria-label="Pin to the plan" title="Pin to the plan: a new revision holding this diagram" onClick={() => void pin()}>
            Pin
          </Button>
        </span>
      )}
      {refused !== null && <Refused e={refused} />}
    </div>
  )
}

function Proposal({ turnId, proposal, ctx, pinned, onPinned }: Readonly<{ turnId: string; proposal: Json; ctx: TurnCtx; pinned: boolean; onPinned: () => void }>) {
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const plan = obj(proposal.plan)
  if (plan === null) return null
  const requestChanges = async () => {
    setBusy(true)
    setRefused(null)
    // The answer goes to the planner as a pinned comment (§7.5), then the re-plan is asked on the digest on screen.
    if (!pinned) {
      const p = await pinChatToThread(ctx.runId, turnId, { part: 'plan', ref: null })
      if (p.status === 'error') {
        setBusy(false)
        setRefused(p.error)
        return
      }
      onPinned()
    }
    const r = await revisePlan(ctx.runId, ctx.digest)
    setBusy(false)
    if (r.status === 'error') setRefused(r.error)
  }
  return (
    <div className="pc-proposal" data-testid="pc-proposal">
      <span className="pt-head">
        <span className="pt-chip">proposal</span>
        {text(proposal.why) !== null && <span className="pc-why">{text(proposal.why)}</span>}
      </span>
      <PlanDiff rows={diffPlans(ctx.plan, plan)} />
      {ctx.mayEdit && (
        <span className="pt-acts">
          <Button kind="secondary" size="sm" busy={busy} aria-label="Request changes with this" title="Request changes with this proposal" onClick={() => void requestChanges()}>
            Request changes
          </Button>
          {ctx.onEdit !== undefined && (
            <Button kind="ghost" size="sm" aria-label="Edit with this" title="Edit the plan, starting from this proposal" onClick={() => ctx.onEdit?.(plan, ctx.digest)}>
              Edit
            </Button>
          )}
        </span>
      )}
      {refused !== null && <Refused e={refused} />}
    </div>
  )
}

function Turn({ t, streamed, starting, ctx }: Readonly<{ t: Json; streamed: string | null; starting: boolean; ctx: TurnCtx }>) {
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const id = text(t.turn_id)
  const state = text(t.state) ?? 'answered'
  const answer = obj(t.answer)
  const pinned = text(t.pinned_as) !== null
  const anchor = obj(t.anchor)
  const pinAnchor: DiagramAnchor = anchor !== null && text(anchor.part) !== null ? { part: anchor.part as DiagramAnchor['part'], ref: text(anchor.ref) } : { part: 'plan', ref: null }
  const markPinned = (entry: Json | null) => ctx.replace({ ...t, pinned_as: text(entry?.entry_id) ?? 'pinned' })
  const pin = async () => {
    if (id === null) return
    setBusy(true)
    setRefused(null)
    const r = await pinChatToThread(ctx.runId, id, pinAnchor)
    setBusy(false)
    if (r.status === 'ok' || r.status === 'stale') markPinned(obj(r.data.entry))
    else if (r.status === 'error') setRefused(r.error)
  }
  const answerText = text(answer?.text) ?? (LIVE.has(state) ? streamed : null)
  const diagrams = objs(answer?.diagrams).flatMap((d) => diagramOf(d) ?? [])
  const proposal = obj(answer?.proposal)
  return (
    <li className="pc-turn" data-testid="pc-turn" data-state={state} data-turn={id ?? undefined}>
      <span className="pt-head">
        <span className="pt-by">{who(t.asked_by)}</span>
        {num(t.on_revision) !== null && <span className="pt-chip">r{num(t.on_revision)}</span>}
      </span>
      <p className="pc-text pc-ask">{typeof t.ask === 'string' ? t.ask : ''}</p>
      <span className="pt-head">
        <span className="pt-by">agent</span>
        {LIVE.has(state) && <span className="pt-chip is-live">{starting || streamed === null ? 'starting' : 'answering'}</span>}
        {(state === 'failed' || state === 'cancelled') && <span className="pt-chip">{state}</span>}
        <RevisionCost cost={t.cost} />
        <Model model={t.model} />
        {pinned && (
          <span className="pt-chip is-ok" data-testid="pc-pinned">
            pinned
          </span>
        )}
      </span>
      {answerText !== null && <p className="pc-text">{answerText}</p>}
      {text(t.error) !== null && <p className="pc-text pt-refused">{text(t.error)}</p>}
      {texts(answer?.dropped).map((line, i) => (
        <p key={i} className="pc-text pc-dropped">
          {line}
        </p>
      ))}
      {id !== null && diagrams.map((d) => <ChatDiagram key={d.id} turnId={id} d={d} ctx={ctx} />)}
      {id !== null && proposal !== null && <Proposal turnId={id} proposal={proposal} ctx={ctx} pinned={pinned} onPinned={() => markPinned(null)} />}
      {state === 'answered' && answer !== null && !pinned && ctx.mayComment && id !== null && (
        <span className="pt-acts">
          <Button kind="ghost" size="sm" busy={busy} aria-label="Pin to the thread" title="Pin to the thread: a comment quoting this answer" onClick={() => void pin()}>
            Pin
          </Button>
        </span>
      )}
      {refused !== null && <Refused e={refused} />}
    </li>
  )
}

// ---------------------------------------------------------------------------
// The stream of the turn in flight
// ---------------------------------------------------------------------------

/**
 * Stream one turn: its deltas into `onDelta`, "starting" into `onStarting`,
 * the final turn into `onEnded`. Reconnects by the turn's id when told to or
 * after CHAT_STREAM_IDLE_MS without a byte; closed when the turn ends or the
 * component unmounts.
 */
function useTurnStream(runId: string, turnId: string | null, on: { delta: (s: string) => void; starting: () => void; ended: (turn: Json) => void }) {
  const handlers = useRef(on)
  handlers.current = on
  const [round, setRound] = useState(0)
  useEffect(() => {
    if (turnId === null || typeof EventSource === 'undefined') return
    const source = new EventSource(chatStreamUrl(runId, turnId))
    let done = false
    let idle: ReturnType<typeof setTimeout> | null = null
    const again = () => {
      if (done) return
      done = true
      source.close()
      setRound((n) => n + 1)
    }
    const arm = () => {
      if (idle !== null) clearTimeout(idle)
      idle = setTimeout(again, CHAT_STREAM_IDLE_MS)
    }
    arm()
    source.onmessage = (ev: MessageEvent) => {
      arm()
      let data: Json | null
      try {
        data = obj(JSON.parse(String(ev.data)))
      } catch {
        return
      }
      if (data === null) return
      if (typeof data.delta === 'string') handlers.current.delta(data.delta)
      else if (data.reconnect === true) again()
      else if (obj(data.turn) !== null) {
        done = true
        source.close()
        if (idle !== null) clearTimeout(idle)
        handlers.current.ended(obj(data.turn)!)
      } else if (data.state === 'starting') handlers.current.starting()
    }
    return () => {
      done = true
      if (idle !== null) clearTimeout(idle)
      source.close()
    }
  }, [runId, turnId, round])
}

// ---------------------------------------------------------------------------
// The column
// ---------------------------------------------------------------------------

export interface PlanChatProps {
  runId: string
  /** The run read: its state, current digest, `may_decide` and `plan_review_usage`. */
  run: Json | null
  /** The revision on screen and its digest: what an ask, a pin and a re-plan are about. */
  plan: Json
  digest: string | null
  /** The part of the plan an ask is about, when the page chose one; else the whole plan. */
  anchor?: DiagramAnchor | null
  /** The head's model control (lane U13 PLAN-CHAT-MODEL); nothing when absent. */
  modelControl?: ReactNode
  /** The run read a Pin to the plan answered (revision N+1). */
  onRun: (run: Json) => void
  /** Opens the page's editor with a proposal's plan and the digest it would replace. */
  onEdit?: (plan: Json, replaces: string) => void
}

export function PlanChat({ runId, run, plan, digest, anchor = null, modelControl, onRun, onEdit }: Readonly<PlanChatProps>) {
  const [turns, setTurns] = useState<Json[]>([])
  const [open, setOpen] = useState<boolean | null>(null)
  const [usage, setUsage] = useState<unknown>(null)
  const [loadError, setLoadError] = useState<ApiError | null>(null)
  const [body, setBody] = useState('')
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const [capped, setCapped] = useState<string | null>(null)
  const [streamed, setStreamed] = useState('')
  const [starting, setStarting] = useState(false)

  // `withTurns: false` after a turn ends: the stream's final turn is already drawn, only the usage moved.
  const reload = useCallback(async (withTurns = true) => {
    const r = await loadPlanChat(runId)
    if (r.status === 'ok' || r.status === 'stale') {
      if (withTurns) setTurns(objs(r.data.turns))
      if (typeof r.data.open === 'boolean') setOpen(r.data.open)
      if (r.data.usage !== undefined) setUsage(r.data.usage)
      setLoadError(null)
    } else if (r.status === 'error') setLoadError(r.error)
  }, [runId])
  useEffect(() => {
    void reload()
  }, [reload])

  const live = [...turns].reverse().find((t) => LIVE.has(String(t.state))) ?? null
  const liveId = text(live?.turn_id)
  useEffect(() => {
    setStreamed('')
    setStarting(false)
  }, [liveId])
  const replace = useCallback((next: Json) => setTurns((ts) => ts.map((t) => (t.turn_id === next.turn_id ? next : t))), [])
  useTurnStream(runId, liveId, {
    delta: (s) => {
      setStarting(false)
      setStreamed((was) => was + s)
    },
    starting: () => setStarting(true),
    ended: (turn) => {
      replace(turn)
      void reload(false)
    },
  })

  const spend = chatSpend(usage ?? run?.plan_review_usage)
  const may = obj(run?.may_decide)
  const mayComment = may?.comment === true
  const mayEdit = may?.edit === true
  const askOpen = run?.state === 'PLANNED' && open !== false
  const current = text(run?.plan_digest) ?? digest
  const capSentence = capped ?? spend?.refusal ?? null

  const ask = async () => {
    const q = body.trim()
    if (q === '' || digest === null) return
    setBusy(true)
    setRefused(null)
    const r = await askPlanChat(runId, digest, q, anchor)
    setBusy(false)
    if (r.status === 'ok' || r.status === 'stale') {
      const turn = obj(r.data.turn)
      if (turn !== null) setTurns((ts) => [...ts.filter((t) => t.turn_id !== turn.turn_id), turn])
      setBody('')
    } else if (r.status === 'error') {
      if (r.error.code === 'chat_cap') setCapped(said(r.error))
      setRefused(r.error)
    }
  }

  const ctx: TurnCtx = { runId, digest: digest ?? '', current: current ?? '', plan, mayComment, mayEdit, onRun, onEdit, replace }
  const atLeast = spend?.atLeast === true ? ' · at least' : ''
  const spendLabel = spend === null ? null : `${dollars(spend.spent)} of ${dollars(spend.cap)}${atLeast}`

  return (
    <aside className="pc-column" aria-label="Review chat" data-testid="pc-column">
      <header className="pc-head">
        <span className="pc-title">Review agent</span>
        <InfoTip about="the review agent" tip="Answers questions about this plan, draws diagrams and may propose a revision. It never changes the plan: a person requests changes or edits." />
        {modelControl !== undefined && <span className="pc-model-slot">{modelControl}</span>}
        {spend !== null && (
          <span className="pc-spend" data-testid="pc-spend" data-level={spend.level} title="Review agents' spend against this run's cap">
            {spendLabel}
          </span>
        )}
      </header>
      {loadError !== null && <Refused e={loadError} />}
      <ol className="pc-turns" aria-label="Transcript">
        {turns.map((t) => (
          <Turn key={String(t.turn_id)} t={t} streamed={t.turn_id === liveId && streamed !== '' ? streamed : null} starting={t.turn_id === liveId && starting} ctx={ctx} />
        ))}
      </ol>
      {askOpen && mayComment && (
        <div className="pc-compose">
          {capSentence !== null && refused?.code !== 'chat_cap' && <Refused e={capSentence} />}
          <textarea className="pt-box" aria-label="Ask the review agent" rows={3} maxLength={CHAT_ASK_MAX} value={body} onChange={(ev) => setBody(ev.target.value)} />
          <span className="pt-acts">
            <Button kind="primary" size="sm" busy={busy} disabled={body.trim() === '' || digest === null || liveId !== null || capSentence !== null} onClick={() => void ask()}>
              Ask
            </Button>
          </span>
          {refused !== null && <Refused e={refused} />}
        </div>
      )}
    </aside>
  )
}

export default PlanChat
