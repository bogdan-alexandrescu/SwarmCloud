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
 * ONE STREAM PER SESSION. The card list's stream is the session's only one:
 * it also carries the Talk stage's events (`STAGE_EVENTS`), which the list
 * hands up through `onStageEvent` for the Talk pane to pass to the stage
 * (Stage.tsx `feed`), so a live session holds one connection.
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
import { route } from '../fetch'
import { CARD_KINDS, type CardCreator, type CardKindSpec, type CardReference, type CardState } from './cards'
import { DetailsInputCard } from './cards/InputCards'
import { BudgetCard, PlanCard, ReviewCard, StopCard, VoiceCardFrame, WorkCard } from './cards/WorkCards'

export const VOICE_EVENTS = '/v1/voice/sessions/{id}/events'

/** The events the card list reads (swarm_api/voice/stream.py `EVENT_TYPES`, less the stage's). */
export const STREAM_EVENTS = ['card.upsert', 'card.remove', 'session.state', 'meter.update'] as const

/** The events the Talk stage reads (swarm_api/voice/stream.py `STAGE_TYPES`). */
export const STAGE_EVENTS = ['visual.upsert', 'note.upsert', 'focus'] as const

/** Every event the stream carries (swarm_api/voice/stream.py `EVENT_TYPES`). */
const SESSION_EVENTS: readonly string[] = [...STREAM_EVENTS, ...STAGE_EVENTS]

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

export interface CardListState {
  /** The highest sequence seen; a reconnect asks for what comes after it. */
  lastSeq: number
  cards: VoiceCard[]
}

export const EMPTY_LIST: CardListState = { lastSeq: 0, cards: [] }

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

/**
 * The list after one event. An event at or below `lastSeq` was already
 * applied (a resumed stream repeating itself) and changes nothing. A card of
 * no known kind is never added.
 */
export function applyStreamEvent(s: CardListState, ev: StreamEvent): CardListState {
  if (!Number.isInteger(ev.seq) || ev.seq <= s.lastSeq) return s
  const lastSeq = ev.seq
  if (ev.type === 'card.upsert') {
    const card = asCard(ev.data.card)
    if (card === null || specOf(card.kind) === null) return { ...s, lastSeq }
    const at = s.cards.findIndex((c) => c.card_id === card.card_id)
    const held = s.cards[at]
    if (held === undefined) return { lastSeq, cards: [...s.cards, card] }
    if (card.version <= held.version) return { ...s, lastSeq }
    const cards = s.cards.slice()
    cards[at] = card
    return { lastSeq, cards }
  }
  if (ev.type === 'card.remove') {
    const nested = asCard(ev.data.card)
    const id = typeof ev.data.card_id === 'string' ? ev.data.card_id : nested?.card_id ?? null
    return { lastSeq, cards: id === null ? s.cards : s.cards.filter((c) => c.card_id !== id) }
  }
  return { ...s, lastSeq }
}

type Action = { type: 'event'; ev: StreamEvent } | { type: 'reset' }

function reducer(s: CardListState, a: Action): CardListState {
  return a.type === 'reset' ? EMPTY_LIST : applyStreamEvent(s, a.ev)
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

// ---- the session's one stream ---------------------------------------------------

/** What a stream's reader does with each event, and with the link. */
export interface StreamReader {
  onEvent: (ev: StreamEvent) => void
  /** The stream opened, or gave up (a refusal, or the session's end). */
  onLink?: (link: 'open' | 'closed') => void
}

/**
 * Opens `sessionId`'s stream from `after` and reads every event it carries,
 * the card list's and the stage's. Returns its close.
 */
export function openSessionStream(sessionId: string, after: number, reader: StreamReader): () => void {
  const source = new EventSource(streamUrl(sessionId, after))
  for (const type of SESSION_EVENTS) {
    source.addEventListener(type, (e) => {
      const ev = streamEvent(type, e as MessageEvent)
      if (ev !== null) reader.onEvent(ev)
    })
  }
  // EventSource reconnects by itself while it can; CLOSED means it gave up.
  source.onerror = () => {
    if (source.readyState === EventSource.CLOSED) reader.onLink?.('closed')
  }
  source.onopen = () => reader.onLink?.('open')
  return () => source.close()
}

// ---- drawing one card -----------------------------------------------------------

/** Kinds whose renderers are later lanes: named in one line, nothing more. */
function NamedOnly({ card, spec }: CardProps) {
  return <VoiceCardFrame card={card} spec={spec} />
}

const RENDERERS: Readonly<Record<string, (p: CardProps) => JSX.Element>> = {
  work: WorkCard,
  review: ReviewCard,
  plan: PlanCard,
  budget: BudgetCard,
  stop: StopCard,
  details_input: DetailsInputCard,
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
 * a review can see every kind without a call. `onStageEvent` receives the
 * stream's stage events (`STAGE_EVENTS`) as they arrive.
 */
export function CardList({
  sessionId,
  fixtures,
  onStageEvent,
}: {
  sessionId: string | null
  fixtures?: readonly VoiceCard[]
  onStageEvent?: (ev: StreamEvent) => void
}) {
  const [s, dispatch] = useReducer(reducer, EMPTY_LIST)
  const [link, setLink] = useState<Link>('off')
  const [round, setRound] = useState(0)
  const lastSeq = useRef(0)
  const logged = useRef(new Set<string>())
  const shownFor = useRef<string | null>(null)
  const toStage = useRef(onStageEvent)
  toStage.current = onStageEvent
  lastSeq.current = s.lastSeq

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
    return openSessionStream(sessionId, lastSeq.current, {
      onEvent: (ev) => {
        if ((STAGE_EVENTS as readonly string[]).includes(ev.type)) toStage.current?.(ev)
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

  // Fixture mode (a development build with no live API) draws one card of each kind while no session is live.
  const cards = fixtures ?? (sessionId === null && s.cards.length === 0 && USE_FIXTURES ? FIXTURE_CARDS : s.cards)
  return (
    <div className="vt-card-list">
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
