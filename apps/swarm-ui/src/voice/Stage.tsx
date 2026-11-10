/**
 * VOICE IT, TALK: THE STAGE (direction A, the owner's pick of 5 November
 * 2026; docs/design/voice-it-own-speech.md §7, §8).
 *
 * The page is a stage: what the session is talking about fills it. The
 * project's visuals are a row of tabs, each with its note count; the stage
 * draws the one the planner drew last (the visuals route answers the most
 * recently drawn first), until the person picks another tab. A file slice, a
 * pull request's diff and a log are drawn as numbered lines from the body
 * swarm-api read itself, headed by where they came from and their line range;
 * a note's `lines` anchor bands its lines and carries its pin. A mock-up is
 * one proposal's frames, each drawn in its own sandboxed frame with nothing
 * allowed and a content policy that fetches nothing; a note on a frame pins
 * there, the approved frame carries a tick whose tip says who approved it and
 * when, and Approve on a frame posts it to the approval route (the person may
 * also approve by voice, through the planner). A read-back the gate
 * holds slides in at the right edge: what was heard, what will run, Go and
 * Not that (posted to the read-back route), and its clock.
 *
 * ON A PHONE (the picks review): every line wraps inside the page's width,
 * so the visual never pans in a window of its own; the tabs wrap instead of
 * scrolling with no bar.
 *
 * LIVE (part of #2688). While the session is live the stage also applies
 * its stream's visual, note and focus events (swarm_api/voice/stream.py
 * `STAGE_TYPES`). It opens no stream of its own: the card list reads the
 * session's one stream (`GET /v1/voice/sessions/{id}/events`) and passes these
 * events up (CardList.tsx `onStageEvent`), and Talk.tsx hands them over through
 * a `stageFeed()` (the owner's pick, 7 November 2026: a second stream cost
 * about 3,600 database reads an hour). A `visual.upsert` takes the stage at once, a `focus` moves
 * it to the visual the planner points at and bands the lines it names, and a
 * `note.upsert` re-reads the visual it names, so a note pinned or resolved
 * shows without waiting for the next read. The reads every few seconds stay,
 * for a browser without EventSource or a stream that gave up.
 *
 * The speech controls (mic, engine status) are TalkPane's; the stage draws
 * none. It reads only the routes: GET visuals and one visual, PATCH a note's
 * state, POST a mock-up frame's approval, GET the pending read-back, POST go or not-that, and the events the
 * card list hands it. Nothing here spends.
 */

import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from 'react'

import {
  answerReadback,
  approveVoiceMockup,
  loadPendingReadback,
  loadVoiceVisual,
  loadVoiceVisuals,
  normVoiceVisual,
  resolveVoiceNote,
  type VoiceApproval,
  type VoiceNote,
  type VoiceReadback,
  type VoiceVisual,
  type VoiceVisualDetail,
} from '../api'
import { Button, TOOLTIP_DELAY_MS } from '../components'
import '../styles/talk-stage.css'
import { STAGE_EVENTS, type StreamEvent } from './CardList'

/** How often a live session's visuals are read again. */
export const STAGE_REFRESH_MS = 5_000
/** How often a live session asks for a held read-back: its whole window is 55 s. */
export const READBACK_REFRESH_MS = 2_000
/** A read-back's window, s: ten to ask and 45 to answer (swarm_api/voice/readback.py). */
const READBACK_WINDOW_S = 55
/** How long an answered card shows its answer before it leaves. */
const ANSWERED_MS = 1_600

export { STAGE_EVENTS }

/** How the card list's stage events reach the stage: the card list emits, the stage listens. */
export interface StageFeed {
  emit: (ev: StreamEvent) => void
  listen: (fn: (ev: StreamEvent) => void) => () => void
}

/** A feed for one Talk pane: every event emitted reaches every listener, in order, none held back. */
export function stageFeed(): StageFeed {
  const listeners = new Set<(ev: StreamEvent) => void>()
  return {
    emit: (ev) => {
      for (const fn of [...listeners]) fn(ev)
    },
    listen: (fn) => {
      listeners.add(fn)
      return () => {
        listeners.delete(fn)
      }
    },
  }
}

/** Where the planner points (`speak`'s `point_at`): the visual, the lines of its anchor, and its caption. */
interface Focus {
  visual_id: string
  lines: { from: number; to: number } | null
  caption: string | null
}

/** What marks a list read as new: a visual drawn, or drawn again, since the last one. */
const markOf = (v: VoiceVisual | undefined): string | null => (v === undefined ? null : `${v.visual_id}:${v.version}:${v.updated_at ?? ''}`)

function focusOf(data: Record<string, unknown>): Focus | null {
  if (typeof data.visual_id !== 'string' || data.visual_id === '') return null
  const a = (data.anchor ?? null) as Record<string, unknown> | null
  const lined = a !== null && typeof a === 'object' && a.type === 'lines' && typeof a.from === 'number' && typeof a.to === 'number'
  return {
    visual_id: data.visual_id,
    lines: lined ? { from: a.from as number, to: a.to as number } : null,
    caption: typeof data.caption === 'string' && data.caption !== '' ? data.caption : null,
  }
}

const KIND_WORD: Record<VoiceVisual['kind'], string> = { code: 'Code', log: 'Log', mockup: 'Mock-up', diagram: 'Diagram', spec: 'Spec' }

function KindGlyph({ kind }: { kind: VoiceVisual['kind'] }) {
  const d =
    kind === 'code'
      ? 'M9 7l-5 5 5 5M15 7l5 5-5 5'
      : kind === 'log'
        ? 'M5 6h14M5 10h10M5 14h14M5 18h8'
        : kind === 'spec'
          ? 'M7 4h7l4 4v12H7zM14 4v4h4'
          : 'M4 5h16v14H4zM4 9h16'
  return (
    <svg className="vts-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d={d} />
    </svg>
  )
}

/** Where a visual came from, one line: `path @ ref`, `owner/repo #pull` or the task's step. */
function sourceLine(v: VoiceVisual): string | null {
  const s = v.source
  if (s === null) return null
  const text = (k: string): string | null => (typeof s[k] === 'string' && s[k] !== '' ? (s[k] as string) : null)
  if (typeof s.pull === 'number') return `${text('repo') ?? ''} #${s.pull}`.trim()
  if (text('path') !== null) return `${text('path')} @ ${text('ref') ?? ''}`.trim()
  if (text('task_id') !== null) return text('step') === null ? text('task_id') : `${text('task_id')} · ${text('step')}`
  return null
}

/** The body's first line number: the visual's own range, else 1. */
const firstLine = (d: VoiceVisualDetail): number => d.visual.lines?.from ?? 1

/** Whether a note is drawn on the body's lines; any other note is a pin under the visual. */
function onLines(note: VoiceNote, d: VoiceVisualDetail): boolean {
  if (d.visual.kind === 'mockup' || note.anchor?.type !== 'lines') return false
  const first = firstLine(d)
  const last = first + d.body.split('\n').length - 1
  return note.anchor.from >= first && note.anchor.to <= last
}

const openNotes = (notes: readonly VoiceNote[]): VoiceNote[] => notes.filter((n) => n.state === 'open')

/** What a mock-up frame may do: fetch nothing, run nothing (voice-it.md §4); `sandbox=""` already runs no script. */
export const FRAME_POLICY = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:"

interface Frame {
  id: string
  label: string
  html: string
}

/** A mock-up's frames (`{frames: [{id, label, html}]}`); a body drawn before frames is one frame. */
export function mockupFrames(d: VoiceVisualDetail): Frame[] {
  try {
    const frames = (JSON.parse(d.body) as { frames?: unknown } | null)?.frames
    if (Array.isArray(frames)) {
      const out = frames.flatMap((f) => {
        const r = (f ?? {}) as Record<string, unknown>
        return typeof r.id === 'string' && typeof r.html === 'string' ? [{ id: r.id, label: typeof r.label === 'string' ? r.label : r.id, html: r.html }] : []
      })
      if (out.length > 0) return out
    }
  } catch {
    // Not frames: the whole body is the one document.
  }
  return [{ id: 'frame-1', label: d.visual.title, html: d.body }]
}

/** A frame's document under the content policy, the policy first in its head. */
function framed(html: string): string {
  const meta = `<meta http-equiv="Content-Security-Policy" content="${FRAME_POLICY}">`
  const doctype = /^\s*<!doctype[^>]*>/i.exec(html)
  return doctype === null ? meta + html : doctype[0] + meta + html.slice(doctype[0].length)
}

const onFrame = (note: VoiceNote, frameId: string): boolean => note.anchor?.type === 'part' && note.anchor.part_id === frameId

function approvedTip(a: VoiceApproval): string {
  const at = a.at === null ? null : new Date(a.at)
  const when = at === null || Number.isNaN(at.getTime()) ? '' : `, ${at.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })}`
  return `Approved by ${a.by || 'the person'}${a.via === 'voice' ? ' by voice' : ''}${when}`
}

/** The approved frame's tick; its tip says who approved it, how and when. */
function ApprovedTick({ approval }: { approval: VoiceApproval }) {
  return (
    <TalkTip tip={approvedTip(approval)} end>
      <span className="vts-approved" role="img" aria-label="Approved" tabIndex={0}>
        <svg className="vts-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <path d="M5 12.5l4.5 4.5L19 7.5" />
        </svg>
      </span>
    </TalkTip>
  )
}

/**
 * Voice it's tooltip: the components' Tooltip (hover and focus, after its
 * delay, gone on Escape; open on a tap too, since Safari never focuses a
 * tapped button), with `data-help-description` on the tip so its sentence
 * stays out of the words the prose ratchet counts (proseapp.tsx), as People's
 * InfoTip and Profile's AboutTip are kept. The read-back's clock and the Talk
 * pane's (i) use it. `end` opens it leftwards, for a tip at the right of a
 * row (the approved tick ends a frame's header, and the frame clips past it).
 */
export function TalkTip({ tip, end = false, children }: { tip: string; end?: boolean; children: ReactNode }) {
  const id = useId()
  const [open, setOpen] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const show = (now: boolean) => {
    if (timer.current !== undefined) clearTimeout(timer.current)
    if (now) setOpen(true)
    else timer.current = setTimeout(() => setOpen(true), TOOLTIP_DELAY_MS)
  }
  const hide = () => {
    if (timer.current !== undefined) clearTimeout(timer.current)
    setOpen(false)
  }
  useEffect(() => () => hide(), [])
  return (
    <span
      className={`c-tip-host${end ? ' is-end' : ''}`}
      aria-describedby={id}
      onMouseEnter={() => show(false)}
      onMouseLeave={hide}
      onFocus={() => show(true)}
      onBlur={hide}
      onClick={() => show(true)}
      onKeyDown={(e) => {
        if (e.key === 'Escape') hide()
      }}
    >
      {children}
      <span role="tooltip" id={id} className="c-tip" data-help-description="" hidden={!open}>
        {tip}
      </span>
    </span>
  )
}

/** One note's pin: its number, and its words while open. */
function Pin({ n, note, open, onToggle }: { n: number; note: VoiceNote; open: boolean; onToggle: () => void }) {
  return (
    <button type="button" className={`vts-pin${open ? ' is-open' : ''}`} aria-expanded={open} aria-label={`Note ${n}`} title={note.text} onClick={onToggle}>
      {n}
    </button>
  )
}

function NoteText({ note, onResolve, busy }: { note: VoiceNote; onResolve: () => void; busy: boolean }) {
  return (
    <span className="vts-note" role="note">
      <span className="vts-note-text">{note.text}</span>
      <span className="vts-note-foot">
        <span className="vts-note-via">{note.via}</span>
        <Button kind="ghost" size="sm" onClick={onResolve} busy={busy}>
          Resolve
        </Button>
      </span>
    </span>
  )
}

/** A body drawn as numbered lines; a note's lines are banded and its pin sits on its first line. */
function Lines({
  detail,
  pins,
  open,
  setOpen,
  onResolve,
  resolving,
  focus,
}: {
  detail: VoiceVisualDetail
  focus: { from: number; to: number } | null
  pins: readonly VoiceNote[]
  open: string | null
  setOpen: (id: string | null) => void
  onResolve: (n: VoiceNote) => void
  resolving: string | null
}) {
  const first = firstLine(detail)
  const rows = detail.body.split('\n')
  const lined = pins.flatMap((note, i) => (note.anchor?.type === 'lines' && onLines(note, detail) ? [{ note, n: i + 1, from: note.anchor.from, to: note.anchor.to }] : []))
  const diff = detail.visual.kind === 'code' && typeof detail.visual.source?.pull === 'number'
  return (
    <ol className={`vts-lines is-${detail.visual.kind}`}>
      {rows.map((text, i) => {
        const at = first + i
        const banded = lined.filter((p) => at >= p.from && at <= p.to)
        const starts = lined.filter((p) => p.from === at)
        const ends = lined.filter((p) => p.to === at)
        const tone = diff ? (text.startsWith('+') ? ' is-add' : text.startsWith('-') ? ' is-del' : '') : ''
        const focused = focus !== null && at >= focus.from && at <= focus.to
        return (
          <li
            key={at}
            className={`vts-line${banded.length > 0 ? ' is-noted' : ''}${focused ? ' is-focus' : ''}${tone}`}
            data-line={at}
            aria-current={focused ? 'true' : undefined}
          >
            <span className="vts-n" aria-hidden="true">
              {at}
            </span>
            <code className="vts-code">{text === '' ? ' ' : text}</code>
            {starts.length > 0 && (
              <span className="vts-line-pins">
                {starts.map((p) => (
                  <Pin key={p.note.note_id} n={p.n} note={p.note} open={open === p.note.note_id} onToggle={() => setOpen(open === p.note.note_id ? null : p.note.note_id)} />
                ))}
              </span>
            )}
            {ends
              .filter((p) => open === p.note.note_id)
              .map((p) => (
                <NoteText key={p.note.note_id} note={p.note} onResolve={() => onResolve(p.note)} busy={resolving === p.note.note_id} />
              ))}
          </li>
        )
      })}
    </ol>
  )
}

/** The read-back the gate holds: heard, will, Go and Not that, and the clock. */
function HoldCard({ rb, now, busy, problem, onAnswer }: { rb: VoiceReadback; now: number; busy: boolean; problem: string | null; onAnswer: (go: boolean) => void }) {
  const expires = rb.expires_at === null ? NaN : Date.parse(rb.expires_at)
  const left = Number.isFinite(expires) ? Math.max(0, Math.ceil((expires - now) / 1000)) : null
  const out = rb.state === 'expired' || (rb.state === 'pending' && left === 0)
  const state = out ? 'expired' : rb.state
  return (
    <section className={`vts-hold is-${state}`} aria-label="Read-back">
      <dl className="vts-rb">
        <dt>Heard</dt>
        {/* What was heard and what it names are the person's words and the
            gate's, not the console's prose: the ratchet does not count them. */}
        <dd className="vts-rb-heard" data-prose-content="">
          “{rb.heard}”
        </dd>
        <dt>Will {rb.action}</dt>
        <dd className="vts-rb-target" data-prose-content="">
          {rb.target_name}
        </dd>
      </dl>
      {state === 'pending' && (
        <div className="vts-hold-acts">
          <Button kind="primary" size="sm" onClick={() => onAnswer(true)} disabled={busy}>
            Go
          </Button>
          <Button kind="secondary" size="sm" onClick={() => onAnswer(false)} disabled={busy}>
            Not that
          </Button>
          {left !== null && (
            <TalkTip tip={`Expires in ${left} s and nothing runs. Say “go” or “not that”; a bare “yes” is not a go.`}>
              <span className="vts-clock" tabIndex={0} style={{ ['--left' as string]: `${Math.min(100, (left / READBACK_WINDOW_S) * 100).toFixed(1)}%` }}>
                {left}s
              </span>
            </TalkTip>
          )}
        </div>
      )}
      {(state === 'confirmed' || state === 'used') && (
        <p className="vts-rb-done is-go" role="status">
          Go
        </p>
      )}
      {state === 'refused' && (
        <p className="vts-rb-done is-refused" role="status">
          Not that
        </p>
      )}
      {(state === 'expired' || state === 'superseded') && (
        <p className="vts-rb-done is-expired" role="status">
          <TalkTip tip="Not answered in time, so nothing ran.">
            <span className="vts-clock is-out" tabIndex={0}>
              0s
            </span>
          </TalkTip>
          <span>{state}</span>
        </p>
      )}
      {problem !== null && (
        <p className="vts-hold-problem" role="alert">
          {problem}
        </p>
      )}
    </section>
  )
}

/**
 * The stage of `projectId`'s visuals and `sessionId`'s held read-back. Draws
 * nothing until there is a visual or a hold: an empty stage is not drawn.
 * `feed` carries the session stream's stage events, from the card list.
 */
export function TalkStage({
  projectId,
  sessionId,
  live,
  feed,
}: {
  projectId: string | null
  sessionId: string | null
  live: boolean
  feed?: StageFeed
}) {
  const [visuals, setVisuals] = useState<VoiceVisual[]>([])
  const [picked, setPicked] = useState<string | null>(null)
  const [detail, setDetail] = useState<VoiceVisualDetail | null>(null)
  const [counts, setCounts] = useState<Record<string, number>>({})
  const [openPin, setOpenPin] = useState<string | null>(null)
  const [resolving, setResolving] = useState<string | null>(null)
  const [approving, setApproving] = useState<string | null>(null)
  const [hold, setHold] = useState<VoiceReadback | null>(null)
  const [holdBusy, setHoldBusy] = useState(false)
  const [holdProblem, setHoldProblem] = useState<string | null>(null)
  const [now, setNow] = useState(() => Date.now())
  const [focus, setFocus] = useState<Focus | null>(null)
  const mounted = useRef(true)
  const newest = useRef<string | null>(null)
  const listed = useRef<VoiceVisual[]>([])
  listed.current = visuals
  /** The list read of the current project; a focus on a visual it lacks asks it again. */
  const pullList = useRef<() => void>(() => undefined)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  // The project's visuals, again every few seconds while the session is live.
  useEffect(() => {
    setVisuals([])
    setPicked(null)
    setDetail(null)
    setFocus(null)
    newest.current = null
    if (projectId === null) return
    let stop = false
    const pull = () =>
      void loadVoiceVisuals(projectId).then((r) => {
        if (stop || (r.status !== 'ok' && r.status !== 'stale')) return
        setVisuals(r.data)
        // A visual drawn (or drawn again) since the last read takes the stage from the person's pick.
        const mark = markOf(r.data[0])
        if (mark !== newest.current) {
          newest.current = mark
          setPicked(null)
        }
      })
    pullList.current = pull
    pull()
    const t = live ? window.setInterval(pull, STAGE_REFRESH_MS) : undefined
    return () => {
      stop = true
      pullList.current = () => undefined
      if (t !== undefined) window.clearInterval(t)
    }
  }, [projectId, live])

  const shownId = picked ?? visuals[0]?.visual_id ?? null
  const shownVersion = visuals.find((v) => v.visual_id === shownId)?.version ?? null
  const shownRef = useRef(shownId)
  shownRef.current = shownId

  /** One visual read again: its open-note count always, and the stage's detail when it is the one shown. */
  const readVisual = useCallback(
    (visualId: string) => {
      if (projectId === null) return
      void loadVoiceVisual(projectId, visualId).then((r) => {
        if (!mounted.current) return
        if (r.status === 'ok' || r.status === 'stale') {
          if (r.data.visual.visual_id === shownRef.current) setDetail(r.data)
          setCounts((c) => ({ ...c, [r.data.visual.visual_id]: openNotes(r.data.notes).length }))
        }
      })
    },
    [projectId],
  )

  const loadShown = useCallback(() => {
    if (shownId !== null) readVisual(shownId)
  }, [readVisual, shownId])

  // The session's stage events, while it is live: a visual drawn, the planner's focus, a note pinned or resolved.
  useEffect(() => {
    if (feed === undefined || projectId === null || sessionId === null || !live) return
    let last = 0
    // The card list's stream, handed over: the stage opens none of its own.
    return feed.listen((ev) => {
      if (!(STAGE_EVENTS as readonly string[]).includes(ev.type)) return
      // A resumed stream repeats nothing it already sent; a repeat changes nothing here either.
      if (!Number.isInteger(ev.seq) || ev.seq <= last) return
      last = ev.seq
      if (ev.type === 'visual.upsert') {
        const v = normVoiceVisual(ev.data.visual)
        if (v === null) return
        const held = listed.current.find((x) => x.visual_id === v.visual_id)
        if (held !== undefined && held.version >= v.version) {
          // The same version approved: the tick moves, the stage stays where the person put it.
          if (held.version === v.version && held.approved?.frame_id !== v.approved?.frame_id) {
            setVisuals((vs) => vs.map((x) => (x.visual_id === v.visual_id ? v : x)))
            setDetail((d) => (d !== null && d.visual.visual_id === v.visual_id ? { ...d, visual: v } : d))
          }
          return
        }
        setVisuals((vs) => [v, ...vs.filter((x) => x.visual_id !== v.visual_id)])
        newest.current = markOf(v)
        setPicked(null)
      } else if (ev.type === 'focus') {
        const f = focusOf(ev.data)
        if (f === null) return
        setFocus(f)
        setPicked(f.visual_id)
        if (!listed.current.some((x) => x.visual_id === f.visual_id)) pullList.current()
      } else {
        const note = (ev.data.note ?? null) as { visual_id?: unknown } | null
        if (note !== null && typeof note.visual_id === 'string' && note.visual_id !== '') readVisual(note.visual_id)
      }
    })
  }, [feed, projectId, sessionId, live, readVisual])

  useEffect(() => {
    setOpenPin(null)
    loadShown()
  }, [loadShown, shownVersion])

  // The held read-back, asked for every two seconds while the session is live.
  useEffect(() => {
    if (sessionId === null || !live) {
      // An ended session holds nothing: its read-backs ended with it.
      setHold(null)
      return
    }
    let stop = false
    const ask = () =>
      void loadPendingReadback(sessionId).then((r) => {
        if (stop || r.status !== 'ok') return
        const found = r.data
        // A hold stays drawn once it is out (answered or expired) until another replaces it.
        setHold((was) => (found !== null ? found : was !== null && was.state === 'pending' && Date.parse(was.expires_at ?? '') > Date.now() ? null : was))
      })
    ask()
    const t = window.setInterval(ask, READBACK_REFRESH_MS)
    return () => {
      stop = true
      window.clearInterval(t)
    }
  }, [sessionId, live])

  // The hold's clock ticks while it is pending.
  useEffect(() => {
    if (hold?.state !== 'pending') return
    setNow(Date.now())
    const t = window.setInterval(() => setNow(Date.now()), 1_000)
    return () => window.clearInterval(t)
  }, [hold?.state, hold?.readback_id])

  const answer = async (go: boolean) => {
    if (hold === null || sessionId === null) return
    setHoldBusy(true)
    setHoldProblem(null)
    const r = await answerReadback(sessionId, hold.readback_id, go)
    if (!mounted.current) return
    setHoldBusy(false)
    if (r.status === 'error') {
      setHoldProblem(r.error.message)
      return
    }
    const after = r.status === 'ok' || r.status === 'stale' ? r.data : null
    const answered = after ?? { ...hold, state: go ? ('confirmed' as const) : ('refused' as const) }
    setHold(answered)
    window.setTimeout(() => mounted.current && setHold((h) => (h?.readback_id === answered.readback_id && h.state !== 'pending' ? null : h)), ANSWERED_MS)
  }

  const resolve = async (note: VoiceNote) => {
    if (projectId === null) return
    setResolving(note.note_id)
    const r = await resolveVoiceNote(projectId, note.visual_id, note.note_id, sessionId)
    if (!mounted.current) return
    setResolving(null)
    if (r.status !== 'error') {
      setOpenPin(null)
      loadShown()
    }
  }

  const approve = async (visualId: string, frameId: string) => {
    if (projectId === null) return
    setApproving(frameId)
    const r = await approveVoiceMockup(projectId, visualId, frameId, sessionId)
    if (!mounted.current) return
    setApproving(null)
    if ((r.status === 'ok' || r.status === 'stale') && r.data !== null) {
      const v = r.data
      setVisuals((vs) => vs.map((x) => (x.visual_id === v.visual_id ? v : x)))
      setDetail((d) => (d !== null && d.visual.visual_id === v.visual_id ? { ...d, visual: v } : d))
    }
  }

  if (visuals.length === 0 && hold === null) return null

  const shown = detail !== null && detail.visual.visual_id === shownId ? detail : null
  const pins = shown === null ? [] : openNotes(shown.notes)
  const frames = shown !== null && shown.visual.kind === 'mockup' ? mockupFrames(shown) : []
  const numbered = pins.map((note, i) => ({ note, n: i + 1 }))
  const loose = shown === null ? [] : numbered.filter((p) => !onLines(p.note, shown) && !frames.some((f) => onFrame(p.note, f.id)))
  const where = shown === null ? null : sourceLine(shown.visual)
  const range = shown?.visual.lines ?? null
  const pointed = shown !== null && focus !== null && focus.visual_id === shown.visual.visual_id ? focus : null

  return (
    <section className={`vts${hold !== null ? ' has-hold' : ''}`} aria-label="Stage">
      {visuals.length > 0 && (
        <nav className="vts-tabs" aria-label="Visuals">
          {visuals.map((v) => {
            const n = counts[v.visual_id] ?? 0
            return (
              <button key={v.visual_id} type="button" className="vts-tab" aria-pressed={v.visual_id === shownId} title={KIND_WORD[v.kind]} onClick={() => setPicked(v.visual_id)}>
                <KindGlyph kind={v.kind} />
                <span className="vts-tab-title">{v.title}</span>
                {n > 0 && (
                  <span className="vts-tab-pins" aria-label={`${n} notes`}>
                    {n}
                  </span>
                )}
              </button>
            )
          })}
        </nav>
      )}
      <div className="vts-main">
        {shown !== null && (
          // The figure is the file, diff, log or mock-up the planner drew,
          // not the console's prose: `data-prose-content` keeps it out of the
          // words the prose ratchet counts (proseapp.tsx).
          <figure className={`vts-frame is-${shown.visual.kind}`} aria-label={shown.visual.title} data-prose-content="">
            {(where !== null || range !== null || pointed?.caption) && (
              <figcaption className="vts-head">
                {where !== null && <code className="vts-where">{where}</code>}
                {range !== null && shown.visual.kind !== 'mockup' && (
                  <span className="vts-range">
                    L{range.from}–{range.to}
                  </span>
                )}
                {pointed?.caption && <span className="vts-focus-caption">{pointed.caption}</span>}
              </figcaption>
            )}
            {shown.visual.kind === 'mockup' ? (
              <div className="vts-mockups">
                {frames.map((f) => {
                  const approved = shown.visual.approved?.frame_id === f.id ? shown.visual.approved : null
                  return (
                    <div key={f.id} className={`vts-mockup-one${approved !== null ? ' is-approved' : ''}`} role="group" aria-label={f.label}>
                      <div className="vts-mockup-head">
                        <span className="vts-mockup-label">{f.label}</span>
                        {numbered
                          .filter((p) => onFrame(p.note, f.id))
                          .map((p) => (
                            <Pin key={p.note.note_id} n={p.n} note={p.note} open={openPin === p.note.note_id} onToggle={() => setOpenPin(openPin === p.note.note_id ? null : p.note.note_id)} />
                          ))}
                        {approved !== null ? (
                          <ApprovedTick approval={approved} />
                        ) : (
                          <Button kind="ghost" size="sm" onClick={() => void approve(shown.visual.visual_id, f.id)} busy={approving === f.id}>
                            Approve
                          </Button>
                        )}
                      </div>
                      {numbered
                        .filter((p) => onFrame(p.note, f.id) && openPin === p.note.note_id)
                        .map((p) => (
                          <NoteText key={p.note.note_id} note={p.note} onResolve={() => void resolve(p.note)} busy={resolving === p.note.note_id} />
                        ))}
                      <div className="vts-mockup">
                        {/* A frame is the planner's HTML: drawn only in a frame that allows nothing and fetches nothing (§8.1). */}
                        <iframe className="vts-mockup-frame" title={f.label} sandbox="" srcDoc={framed(f.html)} />
                      </div>
                    </div>
                  )
                })}
              </div>
            ) : (
              <Lines detail={shown} focus={pointed?.lines ?? null} pins={pins} open={openPin} setOpen={setOpenPin} onResolve={(n) => void resolve(n)} resolving={resolving} />
            )}
            {loose.length > 0 && (
              <div className="vts-loose">
                {loose.map((p) => (
                  <span key={p.note.note_id} className="vts-loose-one">
                    <Pin n={p.n} note={p.note} open={openPin === p.note.note_id} onToggle={() => setOpenPin(openPin === p.note.note_id ? null : p.note.note_id)} />
                    {openPin === p.note.note_id && <NoteText note={p.note} onResolve={() => void resolve(p.note)} busy={resolving === p.note.note_id} />}
                  </span>
                ))}
              </div>
            )}
          </figure>
        )}
        {hold !== null && (
          <aside className="vts-holds" aria-label="Held">
            <HoldCard rb={hold} now={now} busy={holdBusy} problem={holdProblem} onAnswer={(go) => void answer(go)} />
          </aside>
        )}
      </div>
    </section>
  )
}
