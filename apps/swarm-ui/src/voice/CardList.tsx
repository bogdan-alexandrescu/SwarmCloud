/**
 * VOICE IT'S CARD LIST: the session's cards beside the Talk pane
 * (docs/design/voice-it.md §3.5, §4; section 10, phase two, "The console
 * renders work, review, plan, budget, stop and details cards").
 *
 * WHERE THE CARDS COME FROM. swarm-api's card stream,
 * `GET /v1/voice/sessions/{id}/events`, as server-sent events. Every event is
 * stored before it is sent and carries its sequence as the SSE id, so:
 *
 *   * `EventSource` resumes by itself after the stream ends itself (about
 *     every nine minutes), sending `Last-Event-ID`;
 *   * when the stream is opened again by hand (Reconnect), it asks for
 *     `?after=<the last sequence seen>`;
 *   * an event at or below the last sequence seen is dropped, so a stream
 *     that repeats itself never draws a card twice.
 *
 * ONE CARD, ONE PLACE. Cards stand in the order their first event arrived;
 * a later `card.upsert` of the same `card_id` replaces it in place, and only
 * when its `version` is newer. `card.remove` takes it away.
 *
 * ONLY THE CATALOGUE IS DRAWN (cards.ts, held to swarm-api's schema by
 * tests/unit/control_plane/test_voice_cards.py). A card of a kind the
 * catalogue does not list is not drawn, and the console logs it once per
 * kind. A renderer is handed only the body fields its kind names and only
 * the actions its kind allows. The diagram, mock-up, proposal, credential and
 * rescue renderers are later lanes; until they merge, those kinds are named
 * in the list as one line each (their title and summary) and nothing else.
 *
 * NOTHING HERE SPENDS. The stream is read-only; the only action a card draws
 * in phase two is a link to where the thing lives (Open in Work, Open diff,
 * Open where you act) and the await-input card's own answer, which goes
 * through the workflow's ordinary input route. Acting from a card is phase
 * three.
 */

import { useEffect, useReducer, useRef, useState } from 'react'

import { USE_FIXTURES } from '../api'
import { Button } from '../components'
import { read, route } from '../fetch'
import { CARD_KINDS, type CardCreator, type CardKindSpec, type CardReference, type CardState } from './cards'
import { DetailsInputCard } from './cards/InputCards'
import { NavigateCard, NavigateTo, pushPath } from './cards/NavigateCard'
import { BudgetCard, PlanCard, ReviewCard, StopCard, VoiceCardFrame, WorkCard } from './cards/WorkCards'
import { Roster } from './Roster'

export const VOICE_EVENTS = '/v1/voice/sessions/{id}/events'

/** The project's roster, read once when the list mounts or its stream reopens (swarm_api/routes/voice_roster.py). */
export const VOICE_ROSTER = '/v1/voice/projects/{project_id}/roster'

/** The work feed's events the main view draws (swarm_api/voice/stream.py `WORK_TYPES`, less `paused`). */
export const WORK_EVENTS = ['work.delegation', 'work.step', 'roster.update'] as const

/** The events the card list reads (swarm_api/voice/stream.py `EVENT_TYPES`, less the stage's). */
export const STREAM_EVENTS = ['card.upsert', 'card.remove', 'session.state', 'meter.update', ...WORK_EVENTS] as const

/** The events the Talk stage reads (swarm_api/voice/stream.py `STAGE_TYPES`). */
export const STAGE_EVENTS = ['visual.upsert', 'note.upsert', 'focus'] as const

/** The event debug mode adds: one finished planner turn, for Talk's turn line (swarm_api/voice/trace.py `TRACE_TURN`). */
export const TRACE_EVENTS = ['trace.turn'] as const

/** A spent Live key's next tier is held: Talk offers again (swarm_api/voice/stream.py `TIER_REOPEN`). */
export const TIER_EVENTS = ['voice.tier.reopen'] as const

/** Every event the stream carries (swarm_api/voice/stream.py `EVENT_TYPES`). */
const SESSION_EVENTS: readonly string[] = [...STREAM_EVENTS, ...STAGE_EVENTS, ...TRACE_EVENTS, ...TIER_EVENTS]

const IS_TRACE_EVENT: readonly string[] = TRACE_EVENTS

const IS_TIER_EVENT: readonly string[] = TIER_EVENTS

const IS_STAGE_EVENT: readonly string[] = STAGE_EVENTS

/** A card as the stream carries it (swarm_api/voice/cards.py `card_view`). */
export interface VoiceCard {
  card_id: string
  kind: string
  project_id: string | null
  session_id: string | null
  version: number
  state: CardState
  title: string
  created_by: CardCreator
  references: Partial<Record<CardReference, string | null>> | null
  summary: string | null
  at: string | null
  body: Record<string, unknown> | null
}

/** What a renderer is handed: the card, its kind, and only its kind's body fields and actions. */
export interface CardProps {
  card: VoiceCard
  spec: CardKindSpec
  body: Readonly<Record<string, unknown>>
}

const BY_KIND: ReadonlyMap<string, CardKindSpec> = new Map(CARD_KINDS.map((k) => [k.kind, k]))

export function specOf(kind: unknown): CardKindSpec | null {
  return typeof kind === 'string' ? BY_KIND.get(kind) ?? null : null
}

/** The body fields the catalogue names for this kind, and nothing else. */
export function catalogueBody(card: VoiceCard, spec: CardKindSpec): Record<string, unknown> {
  const raw = card.body !== null && typeof card.body === 'object' ? card.body : {}
  const out: Record<string, unknown> = {}
  for (const f of spec.fields) if (Object.prototype.hasOwnProperty.call(raw, f)) out[f] = raw[f]
  return out
}

// ---- the list's state ---------------------------------------------------------

/** One step of a delegation: the grant's phrase and the ids its path named, nothing else (workfeed.py `step_payload`). */
export interface WorkStep {
  seq: number
  phrase: string
  ids: Readonly<Record<string, string>>
  at: string | null
}

/** A delegation in flight: from its start until its end, with its newest steps (at most `STEPS_KEPT`). */
export interface Delegation {
  delegation_id: string
  at: string | null
  steps: WorkStep[]
  /** Steps older than the ones kept. */
  folded: number
}

export const ROSTER_KINDS = ['delegation', 'agent', 'workflow', 'run'] as const
export type RosterKind = (typeof ROSTER_KINDS)[number]
export const ROSTER_STATES = ['queued', 'running', 'waiting', 'done', 'failed', 'cancelled'] as const
export type RosterState = (typeof ROSTER_STATES)[number]

/** One roster row (swarm_api/voice/roster.py): its kind, id, title, state and progress. */
export interface RosterRow {
  kind: RosterKind
  id: string
  title: string | null
  state: RosterState
  done: number | null
  total: number | null
  /** The sequence of the event that last moved it; 0 when it came from the route. */
  seq: number
}

/** The most steps the working card holds; older ones are counted, not kept. */
export const STEPS_KEPT = 50

export interface CardListState {
  /** The highest sequence seen; a reconnect asks for what comes after it. */
  lastSeq: number
  cards: VoiceCard[]
  /**
   * The delegations in flight, oldest first: the working card follows the first.
   * Optional so a state built as `{ lastSeq, cards }` still is one; absent reads as none.
   */
  work?: Delegation[]
  /** The crew strip's rows, in the order they first appeared; absent reads as none. */
  roster?: RosterRow[]
}

export const EMPTY_LIST: CardListState = { lastSeq: 0, cards: [], work: [], roster: [] }

/** One stream event as it arrives: its type, its sequence and its data. */
export interface StreamEvent {
  type: string
  seq: number
  data: Record<string, unknown>
}

function asCard(v: unknown): VoiceCard | null {
  if (typeof v !== 'object' || v === null) return null
  const c = v as Record<string, unknown>
  if (typeof c.card_id !== 'string' || c.card_id === '' || typeof c.kind !== 'string') return null
  return { ...(c as unknown as VoiceCard), version: typeof c.version === 'number' ? c.version : 0 }
}

const text = (v: unknown): string | null => (typeof v === 'string' && v !== '' ? v : null)
const count = (v: unknown): number | null => (typeof v === 'number' && Number.isInteger(v) && v >= 0 ? v : null)

/** A roster row as served or as a `roster.update` carries it, or null when it is not one. */
export function asRosterRow(v: unknown, seq: number): RosterRow | null {
  if (typeof v !== 'object' || v === null) return null
  const r = v as Record<string, unknown>
  const kind = ROSTER_KINDS.find((k) => k === r.kind)
  const state = ROSTER_STATES.find((k) => k === r.state)
  const id = text(r.id)
  if (kind === undefined || state === undefined || id === null) return null
  return { kind, id, title: text(r.title), state, done: count(r.done), total: count(r.total), seq }
}

/** `rows` laid over the roster: a row already listed changes in place and keeps its title when the update has none. */
function mergeRows(roster: RosterRow[], rows: readonly RosterRow[]): RosterRow[] {
  const out = roster.slice()
  for (const row of rows) {
    const at = out.findIndex((r) => r.kind === row.kind && r.id === row.id)
    const held = out[at]
    if (held === undefined) out.push(row)
    else out[at] = { ...row, title: row.title ?? held.title }
  }
  return out
}

/**
 * The roster as the route served it, read after `since`: a row an event moved
 * later than that keeps the event's state (and takes the route's title).
 */
export function seedRoster(s: CardListState, served: readonly RosterRow[], since: number): CardListState {
  const newer = (s.roster ?? []).filter((r) => r.seq > since)
  const rows = served.map((row) => {
    const moved = newer.find((r) => r.kind === row.kind && r.id === row.id)
    return moved === undefined ? row : { ...moved, title: moved.title ?? row.title }
  })
  return { ...s, roster: [...rows, ...newer.filter((r) => !rows.some((x) => x.kind === r.kind && x.id === r.id))] }
}

const DELEGATION_STATE: Readonly<Record<string, RosterState>> = { started: 'running', progress: 'running', done: 'done', failed: 'failed', refused: 'failed' }
const DELEGATION_ENDS: readonly string[] = ['done', 'failed', 'refused']

/** The working card and the roster after one work feed event. */
function applyWork(s: CardListState, ev: StreamEvent): CardListState {
  const at = text(ev.data.at)
  const held = { work: s.work ?? [], roster: s.roster ?? [] }
  if (ev.type === 'work.delegation') {
    const id = text(ev.data.delegation_id)
    const state = typeof ev.data.state === 'string' ? DELEGATION_STATE[ev.data.state] : undefined
    if (id === null || state === undefined) return s
    const roster = mergeRows(held.roster, [{ kind: 'delegation', id, title: null, state, done: null, total: null, seq: ev.seq }])
    // Its end is the next output: the card goes, and its steps with it.
    if (DELEGATION_ENDS.includes(String(ev.data.state))) return { ...s, roster, work: held.work.filter((d) => d.delegation_id !== id) }
    if (held.work.some((d) => d.delegation_id === id)) return { ...s, roster }
    return { ...s, roster, work: [...held.work, { delegation_id: id, at, steps: [], folded: 0 }] }
  }
  if (ev.type === 'work.step') {
    const phrase = text(ev.data.phrase)
    if (phrase === null || held.work.length === 0) return s
    const id = text(ev.data.delegation_id)
    // A step names its delegation; one that names none belongs to the one the card follows.
    const which = id === null ? 0 : held.work.findIndex((d) => d.delegation_id === id)
    const d = held.work[which]
    if (d === undefined) return s
    const raw = typeof ev.data.ids === 'object' && ev.data.ids !== null ? (ev.data.ids as Record<string, unknown>) : {}
    const ids: Record<string, string> = {}
    for (const [k, v] of Object.entries(raw)) if (typeof v === 'string') ids[k] = v
    const steps = [...d.steps, { seq: ev.seq, phrase, ids, at }]
    const over = Math.max(0, steps.length - STEPS_KEPT)
    const work = held.work.slice()
    work[which] = { ...d, steps: steps.slice(over), folded: d.folded + over }
    return { ...s, work }
  }
  if (ev.type === 'roster.update') {
    const rows = Array.isArray(ev.data.rows) ? ev.data.rows.flatMap((r) => asRosterRow(r, ev.seq) ?? []) : []
    return rows.length === 0 ? s : { ...s, roster: mergeRows(held.roster, rows) }
  }
  return s
}

/**
 * The list after one event. An event at or below `lastSeq` was already
 * applied (a resumed stream repeating itself) and changes nothing. A card of
 * no known kind is never added. The work feed's events move the working card
 * and the roster (`applyWork`).
 */
export function applyStreamEvent(s: CardListState, ev: StreamEvent): CardListState {
  if (!Number.isInteger(ev.seq) || ev.seq <= s.lastSeq) return s
  const lastSeq = ev.seq
  if (ev.type === 'card.upsert') {
    const card = asCard(ev.data.card)
    if (card === null || specOf(card.kind) === null) return { ...s, lastSeq }
    const at = s.cards.findIndex((c) => c.card_id === card.card_id)
    const held = s.cards[at]
    if (held === undefined) return { ...s, lastSeq, cards: [...s.cards, card] }
    if (card.version <= held.version) return { ...s, lastSeq }
    const cards = s.cards.slice()
    cards[at] = card
    return { ...s, lastSeq, cards }
  }
  if (ev.type === 'card.remove') {
    const nested = asCard(ev.data.card)
    const id = typeof ev.data.card_id === 'string' ? ev.data.card_id : nested?.card_id ?? null
    return { ...s, lastSeq, cards: id === null ? s.cards : s.cards.filter((c) => c.card_id !== id) }
  }
  return { ...applyWork(s, ev), lastSeq }
}

type Action = { type: 'event'; ev: StreamEvent } | { type: 'reset' } | { type: 'roster'; rows: readonly RosterRow[]; since: number }

function reducer(s: CardListState, a: Action): CardListState {
  if (a.type === 'roster') return seedRoster(s, a.rows, a.since)
  return a.type === 'reset' ? EMPTY_LIST : applyStreamEvent(s, a.ev)
}

/** `GET /v1/voice/projects/{project_id}/roster`: the rows, or null when it was not read. */
export async function loadRoster(projectId: string): Promise<RosterRow[] | null> {
  const r = await read<unknown>(route(VOICE_ROSTER, { project_id: projectId }), () => false)
  if (r.status !== 'ok' && r.status !== 'stale') return null
  const rows = (r.data as { rows?: unknown } | null)?.rows
  return Array.isArray(rows) ? rows.flatMap((row) => asRosterRow(row, 0) ?? []) : []
}

/** An SSE frame's event: the sequence is the frame's id, else the data's `seq`. */
export function streamEvent(type: string, e: MessageEvent): StreamEvent | null {
  let data: unknown
  try {
    data = JSON.parse(String(e.data))
  } catch {
    return null
  }
  if (typeof data !== 'object' || data === null) return null
  const d = data as Record<string, unknown>
  const fromId = /^\d{1,12}$/.test(e.lastEventId ?? '') ? Number(e.lastEventId) : null
  const seq = fromId ?? (typeof d.seq === 'number' ? d.seq : NaN)
  return { type, seq, data: d }
}

/** The stream's address: `?after=` only when something was already seen. */
export function streamUrl(sessionId: string, after: number): string {
  return route(VOICE_EVENTS, { id: sessionId }, after > 0 ? new URLSearchParams({ after: String(after) }) : undefined).url
}

// ---- one stream per session, shared --------------------------------------------

/** One reader of a session's stream: the events it reads, and what it does with each. */
export interface StreamReader {
  types: readonly string[]
  onEvent: (ev: StreamEvent) => void
  /** The stream opened, or gave up (a refusal, or the session's end). */
  onLink?: (link: 'open' | 'closed') => void
}

interface SharedStream {
  source: EventSource
  readers: Set<StreamReader>
}

const sessionStreams = new Map<string, SharedStream>()

/**
 * Reads `sessionId`'s stream through one `EventSource`, so a live session
 * holds one connection, not one per reader. The card list is its reader; the
 * Talk stage reads none of its own and is handed its events by the card list
 * (`onStageEvent`). The first reader opens it from `after`; a reader that joins an open
 * stream reads from where it stands. A reader that joins a stream that gave
 * up (Reconnect) opens it again from its own `after`, and every reader still
 * on it moves to the new one. The stream closes when its last reader leaves.
 * Returns the reader's leave.
 */
export function readSessionStream(sessionId: string, after: number, reader: StreamReader): () => void {
  let shared = sessionStreams.get(sessionId)
  if (shared === undefined || shared.source.readyState === EventSource.CLOSED) {
    shared?.source.close()
    const source = new EventSource(streamUrl(sessionId, after))
    const next: SharedStream = { source, readers: shared?.readers ?? new Set() }
    for (const type of SESSION_EVENTS) {
      source.addEventListener(type, (e) => {
        const ev = streamEvent(type, e as MessageEvent)
        if (ev === null) return
        for (const r of [...next.readers]) if (r.types.includes(type)) r.onEvent(ev)
      })
    }
    // EventSource reconnects by itself while it can; CLOSED means it gave up.
    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED) for (const r of [...next.readers]) r.onLink?.('closed')
    }
    source.onopen = () => {
      for (const r of [...next.readers]) r.onLink?.('open')
    }
    sessionStreams.set(sessionId, next)
    shared = next
  }
  const held = shared
  held.readers.add(reader)
  return () => {
    held.readers.delete(reader)
    if (held.readers.size === 0 && sessionStreams.get(sessionId) === held) {
      sessionStreams.delete(sessionId)
      held.source.close()
    }
  }
}

// ---- drawing one card -----------------------------------------------------------

/** Kinds whose renderers are later lanes: named in one line, nothing more. */
function NamedOnly({ card, spec }: CardProps) {
  return <VoiceCardFrame card={card} spec={spec} />
}

const RENDERERS: Readonly<Record<string, (p: CardProps) => JSX.Element | null>> = {
  work: WorkCard,
  review: ReviewCard,
  plan: PlanCard,
  budget: BudgetCard,
  stop: StopCard,
  details_input: DetailsInputCard,
  navigate: NavigateCard,
}

/** One card, or nothing when its kind is not in the catalogue. */
export function VoiceCardView({ card }: { card: VoiceCard }) {
  const spec = specOf(card.kind)
  if (spec === null) return null
  const Draw = RENDERERS[spec.kind] ?? NamedOnly
  return <Draw card={card} spec={spec} body={catalogueBody(card, spec)} />
}

// ---- the list -------------------------------------------------------------------

type Link = 'off' | 'open' | 'closed' | 'unsupported'

/**
 * The cards of `sessionId`'s stream. When the session ends (`sessionId`
 * becomes null) the stream closes and the cards stay; a new session starts
 * an empty list. `fixtures` draws the given cards with no stream; the
 * console's fixture mode draws `FIXTURE_CARDS` while no session is live, so
 * a review can see every kind without a call.
 *
 * `onStageEvent` is handed the stream's visual, note and focus events
 * (`STAGE_EVENTS`) as they arrive: the Talk stage reads them from here
 * (Talk.tsx), so a live session holds one stream, not two. `onTraceEvent`
 * is handed debug mode's `trace.turn` events the same way, for Talk's turn line,
 * and `onTierEvent` the `voice.tier.reopen` events, for Talk's reopen.
 *
 * THE CREW STRIP (Roster.tsx) heads the list: `projectId`'s roster, read once
 * when the list mounts or its stream reopens, then kept current by the
 * stream's `roster.update` events, which carry the changed rows only.
 * `navigate` is the console's own, for a Navigate card's Open and a row's.
 */
export function CardList({
  sessionId,
  fixtures,
  onStageEvent,
  onTraceEvent,
  onTierEvent,
  projectId = null,
  navigate = pushPath,
}: {
  sessionId: string | null
  fixtures?: readonly VoiceCard[]
  projectId?: string | null
  navigate?: (to: string) => void
  onStageEvent?: (ev: StreamEvent) => void
  onTraceEvent?: (ev: StreamEvent) => void
  onTierEvent?: (ev: StreamEvent) => void
}) {
  const [s, dispatch] = useReducer(reducer, EMPTY_LIST)
  const [link, setLink] = useState<Link>('off')
  const [round, setRound] = useState(0)
  const lastSeq = useRef(0)
  const logged = useRef(new Set<string>())
  const shownFor = useRef<string | null>(null)
  const toStage = useRef(onStageEvent)
  const toTrace = useRef(onTraceEvent)
  const toTier = useRef(onTierEvent)
  lastSeq.current = s.lastSeq
  toStage.current = onStageEvent
  toTrace.current = onTraceEvent
  toTier.current = onTierEvent

  useEffect(() => {
    if (sessionId === null) {
      setLink('off')
      return
    }
    if (shownFor.current !== sessionId) {
      shownFor.current = sessionId
      lastSeq.current = 0
      dispatch({ type: 'reset' })
    }
    if (typeof EventSource === 'undefined') {
      setLink('unsupported')
      return
    }
    setLink('open')
    return readSessionStream(sessionId, lastSeq.current, {
      types: SESSION_EVENTS,
      onEvent: (ev) => {
        // The stage's events go up to the stage; the list still counts their sequence, so a Reconnect repeats none.
        if (IS_STAGE_EVENT.includes(ev.type)) toStage.current?.(ev)
        if (IS_TRACE_EVENT.includes(ev.type)) toTrace.current?.(ev)
        if (IS_TIER_EVENT.includes(ev.type)) toTier.current?.(ev)
        if (ev.type === 'card.upsert') {
          const kind = (ev.data.card as { kind?: unknown } | undefined)?.kind
          const name = typeof kind === 'string' ? kind : '(none)'
          if (specOf(kind) === null && !logged.current.has(name)) {
            logged.current.add(name)
            console.warn(`Voice it: a card of kind ${JSON.stringify(name)} is not in the catalogue and is not drawn`)
          }
        }
        dispatch({ type: 'event', ev })
      },
      onLink: setLink,
    })
  }, [sessionId, round])

  // The roster, read once per session and per reopened stream; the events after it keep it current.
  useEffect(() => {
    if (sessionId === null || projectId === null) return
    let gone = false
    const since = lastSeq.current
    void loadRoster(projectId).then((rows) => {
      if (!gone && rows !== null) dispatch({ type: 'roster', rows, since })
    })
    return () => {
      gone = true
    }
  }, [sessionId, projectId, round])

  // Fixture mode (a development build with no live API) draws one card of each kind while no session is live.
  const cards = fixtures ?? (sessionId === null && s.cards.length === 0 && USE_FIXTURES ? FIXTURE_CARDS : s.cards)
  return (
    <NavigateTo.Provider value={navigate}>
    <div className="vt-card-list">
      <Roster rows={s.roster ?? []} navigate={navigate} />
      {link === 'closed' && sessionId !== null && (
        <p className="c-hint" role="status">
          The card stream closed.{' '}
          <Button kind="secondary" size="sm" onClick={() => setRound((n) => n + 1)}>
            Reconnect
          </Button>
        </p>
      )}
      {link === 'unsupported' && (
        <p className="c-hint" role="status">
          This browser cannot read the card stream (it has no EventSource).
        </p>
      )}
      {cards.length === 0 ? (
        <p className="vt-cards-none">
          {sessionId === null
            ? 'No cards yet. What the conversation plans, starts and waits on appears here as you talk.'
            : 'No cards in this session yet.'}
        </p>
      ) : (
        cards.map((c) => <VoiceCardView key={c.card_id} card={c} />)
      )}
    </div>
    </NavigateTo.Provider>
  )
}

// ---- the fixture cards (the console's fixture mode only) ---------------------

const FIXTURE_AT = '2026-11-02T10:00:00Z'

function fixture(kind: string, state: CardState, title: string, summary: string, body: Record<string, unknown>, references: VoiceCard['references'] = null): VoiceCard {
  return {
    card_id: `vc_fixture_${kind}`,
    kind,
    project_id: 'vp_fixture',
    session_id: 'vs_fixture',
    version: 1,
    state,
    title,
    created_by: kind === 'budget' || kind === 'stop' || kind === 'review' ? 'platform' : 'planner',
    references,
    summary,
    at: FIXTURE_AT,
    body,
  }
}

const FIXTURE_LANE = { name: 'cards', territory: ['apps/swarm-ui/src/voice/CardList.tsx'], reservation_usd: 6, state: 'RUNNING' }

/** One card of each kind this lane draws, for fixture mode and the tests. */
export const FIXTURE_CARDS: readonly VoiceCard[] = [
  fixture('work', 'waiting', 'Card renderers lane', 'One lane is running.', {
    target_kind: 'workflow', target_id: 'wf-fixture', repository: 'example/repo', lanes: [FIXTURE_LANE], live_state: 'RUNNING',
  }, { workflow: 'wf-fixture' }),
  fixture('review', 'waiting', 'Cards render beside the Talk pane', 'The review says merge; checks pass.', {
    change_summary: 'Adds the card list and six renderers.', checks: 'passing', verdict: 'merge', merge_state: 'open', release_state: null,
  }, { pull_request: 'https://github.com/example/repo/pull/1' }),
  fixture('plan', 'proposed', 'Voice it cards', 'Two waves, three lanes, $18 reserved.', {
    goal: 'Draw every card kind beside the Talk pane.', size: 'feature',
    waves: [{ name: 'Wave 1', lanes: [FIXTURE_LANE, { ...FIXTURE_LANE, name: 'diagrams', reservation_usd: 4, state: null }] }, { name: 'Wave 2', lanes: [{ ...FIXTURE_LANE, name: 'mock-ups', reservation_usd: 8, state: null }] }],
    reserved_usd: 18, session_cap_usd: 100, day_cap_usd: 300,
  }),
  fixture('budget', 'waiting', 'Budget', '$12 spent and $18 reserved of $100.', {
    lines: [{ label: 'Talk', spent_usd: 4, reserved_usd: 0 }, { label: 'Lanes', spent_usd: 8, reserved_usd: 18 }],
    spent_usd: 12, reserved_usd: 18, session_cap_usd: 100, day_cap_usd: 300, not_counted: ['Claude subscription seats'],
  }),
  fixture('stop', 'waiting', 'Held: a workflow file', 'A lane wanted to edit a workflow file.', {
    what: 'The lane changes .github/workflows/ci.yml.', rule: 'workflow_file', where: 'https://github.com/example/repo/pull/1',
  }),
  fixture('details_input', 'waiting', 'Which theme first?', 'One question for you.', {
    question: 'Which theme should the mock-ups use first?', options: ['Light', 'Dark'], free_text: true, answer: null, task_id: null,
  }),
]
