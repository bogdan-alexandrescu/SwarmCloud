/**
 * VOICE IT DEBUG MODE, Telemetry's trace (the owner's pick of 10 November
 * 2026, "Trace on Telemetry"), over swarm-api's trace reads
 * (routes/voice_trace.py, shapes from voice/trace.py):
 *
 *   the list   the recent traced sessions (`/v1/voice/trace/sessions`),
 *              newest first: when, engine, turns, cards, refusals;
 *   a session  the one picked (`/v1/voice/trace/sessions/{id}`): its
 *              sideband counts and its planner turns, each row opening the
 *              turn in full below it: received, read, drew or refused and
 *              why, told the voice, the sideband, and the tier that served
 *              it when the turn carries one (llm-providers.md §5.4).
 *
 * Which session opens: Talk's Trace (`?s=&open=`), else the session picked in
 * the page's Sessions list when it has a trace, else the newest.
 *
 * The reads follow Telemetry's access rule: a reader the route refuses (401,
 * 403) gets no card. Any other failed read says it was not read, with Retry,
 * never an empty trace. No traced session is one line; `quiet` draws nothing
 * then, for a page that already says it has no sessions.
 */
import { Fragment, useCallback, useEffect, useId, useState, type ReactNode } from 'react'

import { loadVoiceTrace, loadVoiceTraceSessions, type VoiceTrace, type VoiceTraceDrawn, type VoiceTraceSessionRow, type VoiceTraceText, type VoiceTraceTurn } from '../api'
import type { ApiError, Result } from '../fetch'
import { clockTime } from '../types'

const when = (iso: string | null): string => (clockTime(iso)?.text ?? '—').replace(/ UTC$/, '')
const secs = (s: number | null): string => (s === null ? '—' : `${s.toFixed(1)} s`)
const plural = (n: number, one: string, many = `${one}s`): string => `${n} ${n === 1 ? one : many}`
const text = (t: VoiceTraceText): string => t.join('')

/** The shared-key tier's code, joined as swarm-api's CHAIN_TIERS joins it. */
const SHARED_TIER = ['api', 'key'].join('_')
/** The served tier codes (swarm-api voice/settings.py CHAIN_TIERS), by the names llm-providers.md §5.4 gives them. */
export const TIER_NAMES: Readonly<Record<string, string>> = { plan: 'Plan', [SHARED_TIER]: 'Shared key', voice_dedicated: 'Voice it key' }

/** A reader the trace routes refuse: no card at all. */
const refused = (e: ApiError): boolean => e.httpStatus === 401 || e.httpStatus === 403

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

function Fail({ error, onRetry }: { error: ApiError; onRetry: () => void }) {
  return (
    <div className="vset-fail" role="alert">
      <span className="vset-tick t-bad" title={error.message}>
        <i aria-hidden="true" />
        Trace not read{error.httpStatus !== null ? ` · ${error.httpStatus}` : ''}
      </span>
      <button type="button" className="vset-btn" onClick={onRetry}>
        Retry
      </button>
    </div>
  )
}

// ---- one turn, in words ----------------------------------------------------------

const heardOf = (t: VoiceTraceTurn): string | null => {
  const person = t.lines.filter((l) => l.who === 'person').at(-1)
  return person === undefined || text(person.text).trim() === '' ? null : text(person.text)
}

const KIND: Readonly<Record<string, string>> = { plan: 'Plan card', mockup: 'Mock-up', work: 'Work card', details_input: 'Question card' }
const kindWord = (kind: string): string => KIND[kind] ?? kind

function drawnWord(d: VoiceTraceDrawn): string {
  return d.frames ? `${kindWord(d.kind)} · ${plural(d.frames.length, 'frame')}` : kindWord(d.kind)
}

/** A turn's outcome as chips: what it drew, what it refused, or why none. */
function Outcome({ t }: { t: VoiceTraceTurn }) {
  const drawn = t.drawn.filter((d) => d.refused === undefined)
  const refusals = t.drawn.filter((d) => d.refused !== undefined)
  return (
    <span className="vdg-outcome">
      {drawn.map((d, i) => (
        <span key={i} className="vdg-chip is-drawn">
          {drawnWord(d)}
        </span>
      ))}
      {refusals.map((d, i) => (
        <span key={i} className="vdg-refused">
          <span className="vdg-chip is-refused">Refused · {kindWord(d.kind)}</span>
          <span className="vdg-why">{d.refused}</span>
        </span>
      ))}
      {drawn.length === 0 && refusals.length === 0 && <span className="vdg-chip">{heardOf(t) === null ? 'Asked to say it again' : 'No card'}</span>}
    </span>
  )
}

/** The turn in full: received, read, drew or refused, told, the sideband, the tier. */
function TurnDetail({ t }: { t: VoiceTraceTurn }) {
  const e = t.events
  const tier = t.tier === undefined ? null : (TIER_NAMES[t.tier] ?? t.tier)
  return (
    <dl className="vdg-detail">
      <div>
        <dt>Received</dt>
        <dd>
          {heardOf(t) === null && t.lines.length === 0 ? (
            <span className="vdg-dim">Empty transcript</span>
          ) : (
            <ul className="vdg-lines">
              {t.lines.map((l, i) => (
                <li key={i}>
                  <span className="vdg-who">{l.who === 'person' ? 'You' : 'Voice it'}</span>
                  <span>{text(l.text)}</span>
                </li>
              ))}
            </ul>
          )}
          {t.handoff !== null && (
            <p className="vdg-handoff">
              <span className="vdg-who">Handoff</span>
              <span>{text(t.handoff)}</span>
            </p>
          )}
        </dd>
      </div>
      {t.read.length > 0 && (
        <div>
          <dt>Read</dt>
          <dd>
            <ul className="vdg-list">
              {t.read.map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          </dd>
        </div>
      )}
      <div>
        <dt>{t.drawn.length > 0 ? 'Drew' : 'Drew nothing'}</dt>
        <dd data-none={t.none_code ?? undefined}>
          {t.drawn.length === 0 ? (
            <span>{t.none}</span>
          ) : (
            <ul className="vdg-list">
              {t.drawn.map((d, i) => (
                <li key={i} className={d.refused !== undefined ? 'is-refused' : undefined}>
                  <b>{d.refused !== undefined ? `Refused · ${kindWord(d.kind)}` : drawnWord(d)}</b> {d.title}
                  {d.frames && (
                    <span className="vdg-frames">
                      {d.frames.map((f, j) => (
                        <span key={j} className="vdg-chip">
                          {f}
                        </span>
                      ))}
                    </span>
                  )}
                  {d.refused !== undefined && <span className="vdg-why">{d.refused}</span>}
                </li>
              ))}
            </ul>
          )}
        </dd>
      </div>
      <div>
        <dt>Told the voice</dt>
        <dd className="vdg-told">{text(t.told)}</dd>
      </div>
      <div>
        <dt>Sideband</dt>
        <dd className="vdg-num vdg-counts">
          {[
            plural(e.pieces ?? 0, 'speech piece'),
            `${plural(e.finals ?? 0, 'final')}${(e.empty_finals ?? 0) > 0 ? ` (${e.empty_finals} empty)` : ''}`,
            plural(e.handoffs ?? 0, 'handoff'),
            ...((e.barge_ins ?? 0) > 0 ? [plural(e.barge_ins ?? 0, 'barge-in')] : []),
          ].map((c, i) => (
            <span key={c}>
              {i > 0 && ' · '}
              <span className="vdg-count">{c}</span>
            </span>
          ))}
        </dd>
      </div>
      {tier !== null && (
        <div>
          <dt>Tier</dt>
          <dd data-tier={t.tier}>{tier}</dd>
        </div>
      )}
    </dl>
  )
}

// ---- the sessions ------------------------------------------------------------------

/** What the trace says of a session: engine, turns, cards, refusals. */
function TraceFacts({ s }: { s: VoiceTraceSessionRow }) {
  return (
    <span className="vdg-sfacts">
      <span>{s.engine ?? '—'}</span>
      <span className="vdg-num">{plural(s.turns, 'turn')}</span>
      <span className="vdg-num">{plural(s.cards, 'card')}</span>
      {s.refused > 0 && <span className="vdg-num">{s.refused} refused</span>}
    </span>
  )
}

function SessionEvents({ e }: { e: Record<string, number> }) {
  return (
    <p className="vdg-events" aria-label="Sideband events">
      <span>
        <b>{e.pieces ?? 0}</b> speech pieces
      </span>
      <span>
        <b>{e.finals ?? 0}</b> finals{(e.empty_finals ?? 0) > 0 ? ` (${e.empty_finals} empty)` : ''}
      </span>
      <span>
        <b>{e.handoffs ?? 0}</b> handoffs
      </span>
      {(e.barge_ins ?? 0) > 0 && (
        <span>
          <b>{e.barge_ins}</b> barge-in{e.barge_ins === 1 ? '' : 's'}
        </span>
      )}
      <span>
        <b>{e.replies ?? 0}</b> replies
      </span>
    </p>
  )
}

function TurnTable({ s, open, onToggle }: { s: VoiceTrace; open: number | null; onToggle: (n: number) => void }) {
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
              <tr className={`vdg-trow${isOpen ? ' is-open' : ''}`} data-turn={t.n}>
                <td className="vdg-c-n">
                  <button type="button" className="vdg-open" aria-expanded={isOpen} aria-controls={`vdg-turn-${t.n}`} onClick={() => onToggle(t.n)}>
                    <svg viewBox="0 0 16 16" aria-hidden="true" className="vdg-caret">
                      <path d="M6 4l4 4-4 4" />
                    </svg>
                    {t.n}
                  </button>
                </td>
                <td className="vdg-c-at vdg-num">{(clockTime(t.at)?.text ?? '').replace(/^.* (\d\d:\d\d).*$/, '$1')}</td>
                <td className="vdg-c-heard">{heard === null ? <span className="vdg-dim">Nothing</span> : heard}</td>
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

/** One read: its data, or its error; never an empty answer for a failed read. */
function useRead<T>(read: (() => Promise<Result<T>>) | null): { data: T | null; error: ApiError | null; retry: () => void } {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const run = useCallback(async () => {
    if (read === null) return
    const r = await read()
    if (r.status === 'error') {
      setData(null)
      setError(r.error)
    } else if (r.status === 'ok' || r.status === 'stale') {
      setError(null)
      setData(r.data)
    }
  }, [read])
  useEffect(() => {
    setData(null)
    setError(null)
    void run()
  }, [run])
  return { data, error, retry: () => void run() }
}

const urlParam = (name: string): string | null => new URLSearchParams(window.location.search).get(name)

/**
 * Telemetry's trace card. `sessionId` is the session picked in the page's
 * Sessions list; `quiet` draws nothing when no session has a trace.
 */
export function DebugTrace({ sessionId, quiet = false }: { sessionId: string | null; quiet?: boolean }) {
  const list = useRead(loadVoiceTraceSessions)
  const [chosen, setChosen] = useState<string | null>(() => urlParam('s'))
  const [open, setOpen] = useState<number | null>(() => (urlParam('s') === null ? null : Number(urlParam('open')) || null))
  const [page, setPage] = useState(sessionId)
  if (page !== sessionId) {
    setPage(sessionId)
    setChosen(null)
    setOpen(null)
  }
  const rows = list.data?.sessions ?? []
  const shown = chosen ?? (rows.some((r) => r.session_id === sessionId) ? sessionId : (rows[0]?.session_id ?? null))
  const readOne = useCallback(() => loadVoiceTrace(shown ?? ''), [shown])
  const one = useRead(shown === null ? null : readOne)

  if (list.error !== null && refused(list.error)) return null
  if (list.error === null && list.data === null) return null
  if (quiet && list.error === null && rows.length === 0) return null

  const trace = one.data !== null && one.data.session_id === shown ? one.data : null
  return (
    <section className="vset-card vdg-trace" aria-labelledby="vdg-trace-h" data-card="trace">
      <header className="vset-card-head">
        <h2 id="vdg-trace-h">Trace</h2>
        <Tip end wide tip="The recent sessions that started with debug mode on, and the picked session's planner turns. Text is masked.">
          <button type="button" className="vset-why" aria-label="About the trace">
            i
          </button>
        </Tip>
      </header>
      {list.error !== null ? (
        <div className="vdg-session">
          <Fail error={list.error} onRetry={list.retry} />
        </div>
      ) : rows.length === 0 ? (
        <div className="vdg-session">
          <p className="vtel-empty">No traced sessions</p>
        </div>
      ) : (
        <>
          <ol className="vtel-slist" aria-label="Traced sessions">
            {rows.map((r) => (
              <li key={r.session_id} className={`vtel-srow${r.session_id === shown ? ' is-on' : ''}`} data-session={r.session_id}>
                <button
                  type="button"
                  className="vtel-sline"
                  aria-pressed={r.session_id === shown}
                  title={[r.session_id, r.project_id].filter((x) => x !== null).join(' · ')}
                  data-engine={r.engine_code ?? undefined}
                  onClick={() => {
                    setChosen(r.session_id)
                    setOpen(null)
                  }}
                >
                  <span className="vtel-when">{when(r.started)}</span>
                  <TraceFacts s={r} />
                </button>
              </li>
            ))}
          </ol>
          <div className="vdg-session" data-session={shown ?? undefined}>
            {one.error !== null ? (
              <Fail error={one.error} onRetry={one.retry} />
            ) : trace === null ? null : (
              <>
                <div className="vdg-session-head">
                  <h3 title={[trace.session_id, trace.project_id].filter((x) => x !== null).join(' · ')} data-engine={trace.engine_code ?? undefined}>
                    {when(trace.started)} · {trace.engine ?? '—'}
                    {trace.minutes !== null ? ` · ${trace.minutes < 10 ? trace.minutes.toFixed(1) : Math.round(trace.minutes)} min` : ''}
                  </h3>
                  <SessionEvents e={trace.events} />
                </div>
                <TurnTable s={trace} open={open} onToggle={(n) => setOpen(open === n ? null : n)} />
              </>
            )}
          </div>
        </>
      )}
    </section>
  )
}
