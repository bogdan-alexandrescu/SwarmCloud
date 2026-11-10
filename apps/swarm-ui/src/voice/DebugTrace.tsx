// MOCK-HOOK FILE
/**
 * VOICE IT DEBUG MODE, direction "Trace on Telemetry" (owner, 10 November
 * 2026), drawn from voice/debugFixture.ts at `?vd=telemetry`:
 *
 *   DebugSwitch  the row in Settings' Engine card; its meaning behind its (i);
 *   TurnLine     Talk's one line under the Stage: the last planner turn's
 *                outcome, the turn in full in its tooltip;
 *   DebugTrace   Telemetry's trace: recent sessions, the chosen session's
 *                turns, each row opening the turn in full below it.
 */
import { Fragment, useEffect, useId, useState, type ReactNode } from 'react'

import { buttonClass } from '../components'
import { clockTime } from '../types'
import {
  BAKERY,
  debugOn,
  drawnCount,
  liveTurn,
  onDebugChange,
  openParam,
  plain,
  sessionCards,
  setDebugOn,
  TRACE_SESSIONS,
  type Masked,
  type TraceDrawn,
  type TraceSession,
  type TraceTurn,
} from './debugFixture'
import '../styles/voice-debug.css'

export function useDebugOn(): boolean {
  const [on, setOn] = useState(debugOn())
  useEffect(() => onDebugChange(() => setOn(debugOn())), [])
  return on
}

const when = (iso: string): string => (clockTime(iso)?.text ?? '—').replace(/ UTC$/, '')
const secs = (s: number): string => `${s.toFixed(1)} s`
const plural = (n: number, one: string, many = `${one}s`): string => `${n} ${n === 1 ? one : many}`

function openTrace(session: string, turn: number): void {
  window.history.pushState(null, '', `/voice/telemetry?vd=telemetry&s=${session}&open=${turn}`)
  window.dispatchEvent(new PopStateEvent('popstate'))
}

/** A tip that holds more than a sentence; open on hover or focus, at once. */
function Tip({ tip, children, wide = false, end = false }: { tip: ReactNode; children: ReactNode; wide?: boolean; end?: boolean }) {
  const id = useId()
  const [open, setOpen] = useState(false)
  return (
    <span
      className={`c-tip-host vdg-tiphost${end ? ' is-end' : ''}`}
      aria-describedby={id}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)}
      onBlur={() => setOpen(false)}
      onKeyDown={(e) => {
        if (e.key === 'Escape') setOpen(false)
      }}
    >
      {children}
      <span role="tooltip" id={id} className={`c-tip vdg-tip${wide ? ' is-wide' : ''}`} data-help-description="" hidden={!open}>
        {tip}
      </span>
    </span>
  )
}

// ---- Settings ---------------------------------------------------------------------

/** The Debug row's value: the switch, and Traces while it is on. */
export function DebugSwitch({ ro }: { ro: boolean }) {
  const on = useDebugOn()
  return (
    <span className="vdg-switchrow">
      <button type="button" role="switch" aria-checked={on} aria-label="Debug mode" className="vdg-switch" disabled={ro} onClick={() => setDebugOn(!on)}>
        <i aria-hidden="true" />
      </button>
      {on && (
        <button type="button" className={buttonClass({ kind: 'secondary', size: 'sm' })} onClick={() => openTrace(BAKERY.session_id, 0)}>
          Traces
        </button>
      )}
    </span>
  )
}

/** The Debug row's (i): a visible tip, so it can be read in full. */
export function DebugWhy() {
  return (
    <Tip
      wide
      tip={
        <>
          Every session keeps a turn-by-turn trace: what the planner was given, what it read, the cards and mock-ups it drew or refused and why, and what the voice was
          told. Talk shows the last turn under the Stage; Telemetry keeps the traces. Text is masked. Off, nothing is drawn and nothing is kept.
        </>
      }
    >
      <button type="button" className="vset-why" aria-label="About debug mode">
        i
      </button>
    </Tip>
  )
}

// ---- one turn, in words ----------------------------------------------------------

function MaskedText({ text }: { text: readonly Masked[] }) {
  return (
    <>
      {text.map((p, i) =>
        typeof p === 'string' ? (
          <Fragment key={i}>{p}</Fragment>
        ) : (
          <span key={i} className="vdg-mask" title={`Masked: ${p.masked}`}>
            {p.masked}
          </span>
        ),
      )}
    </>
  )
}

const heardOf = (t: TraceTurn): Masked[] | null => {
  const person = t.lines.filter((l) => l.who === 'person').at(-1)
  return person === undefined || plain(person.text).trim() === '' ? null : person.text
}

const KIND: Record<TraceDrawn['kind'], string> = { plan: 'Plan card', mockup: 'Mock-up', work: 'Work card', details_input: 'Question card' }

function drawnWord(d: TraceDrawn): string {
  return d.frames ? `${KIND[d.kind]} · ${plural(d.frames.length, 'frame')}` : KIND[d.kind]
}

/** A turn's outcome as chips: what it drew, what it refused, or why none. */
function Outcome({ t }: { t: TraceTurn }) {
  const drawn = t.drawn.filter((d) => d.refused === undefined)
  const refused = t.drawn.filter((d) => d.refused !== undefined)
  const heard = heardOf(t)
  return (
    <span className="vdg-outcome">
      {drawn.map((d) => (
        <span key={d.title} className="vdg-chip is-drawn">
          {drawnWord(d)}
        </span>
      ))}
      {refused.map((d) => (
        <span key={d.title} className="vdg-refused">
          <span className="vdg-chip is-refused">Refused · {KIND[d.kind]}</span>
          <span className="vdg-why">{d.refused}</span>
        </span>
      ))}
      {drawn.length === 0 && refused.length === 0 && <span className="vdg-chip">{heard === null ? 'Asked to say it again' : 'No card'}</span>}
    </span>
  )
}

/** The turn in full: received, read, drew or refused, told, the sideband. */
function TurnDetail({ t, compact = false }: { t: TraceTurn; compact?: boolean }) {
  const heard = heardOf(t)
  return (
    <dl className={`vdg-detail${compact ? ' is-compact' : ''}`}>
      <div>
        <dt>Received</dt>
        <dd>
          {heard === null && !compact ? (
            <span className="vdg-dim">Empty transcript</span>
          ) : compact ? (
            <span>
              {heard === null ? 'Empty transcript' : plural(t.lines.length, 'transcript line')}
              {t.handoff !== null ? ' · handoff' : ' · no handoff'}
            </span>
          ) : (
            <ul className="vdg-lines">
              {t.lines.map((l, i) => (
                <li key={i}>
                  <span className="vdg-who">{l.who === 'person' ? 'You' : 'Voice it'}</span>
                  <span>
                    <MaskedText text={l.text} />
                  </span>
                </li>
              ))}
            </ul>
          )}
          {!compact && t.handoff !== null && (
            <p className="vdg-handoff">
              <span className="vdg-who">Handoff</span>
              <span>
                <MaskedText text={t.handoff} />
              </span>
            </p>
          )}
        </dd>
      </div>
      {t.read.length > 0 && (
        <div>
          <dt>Read</dt>
          <dd>
            {compact ? (
              t.read.join(' · ')
            ) : (
              <ul className="vdg-list">
                {t.read.map((r) => (
                  <li key={r}>{r}</li>
                ))}
              </ul>
            )}
          </dd>
        </div>
      )}
      <div>
        <dt>{t.drawn.length > 0 ? 'Drew' : 'Drew nothing'}</dt>
        <dd>
          {t.drawn.length === 0 ? (
            <span>{t.none}</span>
          ) : (
            <ul className="vdg-list">
              {t.drawn.map((d) => (
                <li key={d.title} className={d.refused ? 'is-refused' : undefined}>
                  <b>{d.refused ? `Refused · ${KIND[d.kind]}` : drawnWord(d)}</b> {d.title}
                  {d.frames && !compact && (
                    <span className="vdg-frames">
                      {d.frames.map((f) => (
                        <span key={f} className="vdg-chip">
                          {f}
                        </span>
                      ))}
                    </span>
                  )}
                  {d.refused && <span className="vdg-why">{d.refused}</span>}
                </li>
              ))}
            </ul>
          )}
        </dd>
      </div>
      <div>
        <dt>Told the voice</dt>
        <dd className="vdg-told">
          <MaskedText text={t.told} />
        </dd>
      </div>
      <div>
        <dt>Sideband</dt>
        <dd className="vdg-num vdg-counts">
          {[
            plural(t.events.pieces, 'speech piece'),
            plural(t.events.finals, 'final'),
            plural(t.events.handoffs, 'handoff'),
            ...(t.events.barge_ins > 0 ? [plural(t.events.barge_ins, 'barge-in')] : []),
          ].map((c, i) => (
            <span key={c}>
              {i > 0 && ' · '}
              <span className="vdg-count">{c}</span>
            </span>
          ))}
        </dd>
      </div>
    </dl>
  )
}

// ---- Talk ------------------------------------------------------------------------

/** One line under the Stage: the last planner turn's outcome, the turn in full in its tooltip. */
export function TurnLine() {
  const t = liveTurn()
  const heard = heardOf(t)
  const cards = drawnCount(t)
  const frames = t.drawn.find((d) => d.frames)?.frames?.length ?? 0
  return (
    <div className="vdg-turn" data-outcome={cards > 0 ? 'drawn' : 'none'} aria-label="Last planner turn">
      <Tip wide tip={<TurnDetail t={t} compact />}>
        <span className="vdg-turn-main" tabIndex={0}>
          <span className={`vdg-dot${cards > 0 ? ' is-ok' : ' is-warn'}`} aria-hidden="true" />
          <span className="vdg-turn-n">Turn {t.n}</span>
          {heard === null ? (
            <span className="vdg-turn-heard is-none">Heard nothing</span>
          ) : (
            <span className="vdg-turn-heard">
              “<MaskedText text={heard} />”
            </span>
          )}
          <span className="vdg-turn-out">
            {cards === 0 ? `0 cards · ${heard === null ? 'empty transcript' : (t.none ?? 'no card')}` : frames > 0 ? `1 mock-up · ${plural(frames, 'frame')}` : plural(cards, 'card')}
          </span>
          <span className="vdg-turn-secs">{secs(t.secs)}</span>
        </span>
      </Tip>
      <button type="button" className={buttonClass({ kind: 'ghost', size: 'sm' })} onClick={() => openTrace(BAKERY.session_id, t.n)}>
        Trace
      </button>
    </div>
  )
}

// ---- Telemetry -------------------------------------------------------------------

/** What the trace adds to the page's own session row: engine, turns, cards. */
export function TraceFacts({ s }: { s: TraceSession }) {
  return (
    <span className="vdg-sfacts">
      <span>{s.engine}</span>
      <span className="vdg-num">{plural(s.turns.length, 'turn')}</span>
      <span className="vdg-num">{plural(sessionCards(s), 'card')}</span>
    </span>
  )
}

function SessionEvents({ s }: { s: TraceSession }) {
  const e = s.events
  return (
    <p className="vdg-events" aria-label="Sideband events">
      <span>
        <b>{e.pieces}</b> speech pieces
      </span>
      <span>
        <b>{e.finals}</b> finals{e.empty_finals > 0 ? ` (${e.empty_finals} empty)` : ''}
      </span>
      <span>
        <b>{e.handoffs}</b> handoffs
      </span>
      {e.barge_ins > 0 && (
        <span>
          <b>{e.barge_ins}</b> barge-in{e.barge_ins === 1 ? '' : 's'}
        </span>
      )}
      <span>
        <b>{e.replies}</b> replies
      </span>
    </p>
  )
}

function TurnTable({ s, open, onToggle }: { s: TraceSession; open: number | null; onToggle: (n: number) => void }) {
  return (
    <table className="vdg-turns">
      <thead>
        <tr>
          <th className="vdg-c-n">Turn</th>
          <th className="vdg-c-at">At</th>
          <th>Heard</th>
          <th>Outcome</th>
          <th className="vdg-r">Took</th>
        </tr>
      </thead>
      <tbody>
        {s.turns.map((t) => {
          const heard = heardOf(t)
          const isOpen = open === t.n
          return (
            <Fragment key={t.n}>
              <tr className={`vdg-trow${isOpen ? ' is-open' : ''}`}>
                <td className="vdg-c-n">
                  <button type="button" className="vdg-open" aria-expanded={isOpen} aria-controls={`vdg-turn-${t.n}`} onClick={() => onToggle(t.n)}>
                    <svg viewBox="0 0 16 16" aria-hidden="true" className="vdg-caret">
                      <path d="M6 4l4 4-4 4" />
                    </svg>
                    {t.n}
                  </button>
                </td>
                <td className="vdg-c-at vdg-num">{(clockTime(t.at)?.text ?? '').replace(/^.* (\d\d:\d\d).*$/, '$1')}</td>
                <td className="vdg-c-heard">{heard === null ? <span className="vdg-dim">Nothing</span> : <MaskedText text={heard} />}</td>
                <td>
                  <Outcome t={t} />
                </td>
                <td className="vdg-r vdg-num">{secs(t.secs)}</td>
              </tr>
              {isOpen && (
                <tr className="vdg-trow-detail" id={`vdg-turn-${t.n}`}>
                  <td colSpan={5}>
                    <TurnDetail t={t} />
                  </td>
                </tr>
              )}
            </Fragment>
          )
        })}
      </tbody>
    </table>
  )
}

/** Telemetry's trace of the session picked in the page's Sessions list: its turns, each opening in full. */
export function DebugTrace({ sessionId }: { sessionId: string }) {
  const [open, setOpen] = useState<number | null>(() => Number(new URLSearchParams(location.search).get('open')) || openParam())
  const [shown, setShown] = useState(sessionId)
  if (shown !== sessionId) {
    setShown(sessionId)
    setOpen(null)
  }
  const session = TRACE_SESSIONS.find((s) => s.session_id === sessionId)
  if (session === undefined) return null
  return (
    <section className="vset-card vdg-trace" aria-labelledby="vdg-trace-h" data-card="trace">
      <header className="vset-card-head">
        <h2 id="vdg-trace-h">Trace</h2>
        <Tip end wide tip="The picked session's planner turns, kept while debug mode is on. Text is masked.">
          <button type="button" className="vset-why" aria-label="About the trace">
            i
          </button>
        </Tip>
      </header>
      <div className="vdg-session">
        <div className="vdg-session-head">
          <h3>
            {when(session.started)} · {session.engine}
          </h3>
          <SessionEvents s={session} />
        </div>
        <TurnTable s={session} open={open} onToggle={(n) => setOpen(open === n ? null : n)} />
      </div>
    </section>
  )
}
