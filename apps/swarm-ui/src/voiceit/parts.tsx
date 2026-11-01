// MOCK-HOOK FILE
/**
 * VOICE IT, THE SHARED PIECES (mock-ups only). Each is one part of the page a
 * direction arranges: the oscilloscope, the captions, the mic controls, the
 * budget meter, the project header, what needs Bogdan, and his standing
 * preferences. They draw from the console's own components and tokens; none
 * keeps state a layout would need to read back.
 */
import { useId, useLayoutEffect, useRef, type ReactNode } from 'react'

import { Button, Chip, Segmented, ToneMark, UsageTrack } from '../components'
import {
  ENGINES,
  scopeSamples,
  type Budget,
  type CaptionLine,
  type MicState,
  type Project,
  type Scope,
  type Waiting,
} from './fixtures'

export const money = (n: number): string => (Number.isInteger(n) ? `$${n}` : `$${n.toFixed(2)}`)

// ---------------------------------------------------------------------------
// Oscilloscope
// ---------------------------------------------------------------------------

const SCOPE_WORD: Record<Scope['state'], string> = {
  speaking: 'Voice it is speaking',
  listening: 'Listening to you',
  thinking: 'Thinking',
  idle: 'Idle',
}

/**
 * The agent's voice, as inline SVG (a canvas would be blank in a static
 * frame). `height` sets the drawing's height; it always fills its width.
 * `compact` drops the word under it, for a header strip.
 */
export function Oscilloscope({
  scope,
  micLevel = null,
  height = 96,
  compact = false,
  className,
}: {
  scope: Scope
  /** Bogdan's mic level, drawn while listening. */
  micLevel?: number | null
  height?: number
  compact?: boolean
  className?: string
}) {
  const gid = useId().replace(/:/g, '')
  const W = 600
  const H = 100
  const mid = H / 2
  const samples = scopeSamples(scope.state, scope.seed, 160, micLevel ?? 0.5)
  const step = W / (samples.length - 1)
  const pts = samples.map((v, i) => `${(i * step).toFixed(1)},${(mid - v * (H * 0.44)).toFixed(1)}`)
  const line = `M${pts.join(' L')}`
  // A filled envelope for speaking: the wave's mirror, faintly.
  const env =
    scope.state === 'speaking'
      ? `M0,${mid} ` + samples.map((v, i) => `L${(i * step).toFixed(1)},${(mid - Math.abs(v) * H * 0.44).toFixed(1)}`).join(' ') +
        ` L${W},${mid} ` + [...samples].reverse().map((v, i) => `L${(W - i * step).toFixed(1)},${(mid + Math.abs(v) * H * 0.44).toFixed(1)}`).join(' ') + ' Z'
      : null
  const word = scope.state === 'thinking' && scope.label ? `Thinking: ${scope.label}` : SCOPE_WORD[scope.state]
  return (
    <figure className={`vi-scope is-${scope.state}${compact ? ' is-compact' : ''}${className ? ` ${className}` : ''}`} data-state={scope.state}>
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" style={{ height }} role="img" aria-label={word}>
        <defs>
          <linearGradient id={`sh${gid}`} x1="0" x2="1" y1="0" y2="0">
            <stop offset="0" stopColor="currentColor" stopOpacity="0.15" />
            <stop offset="0.5" stopColor="currentColor" stopOpacity="0.9" />
            <stop offset="1" stopColor="currentColor" stopOpacity="0.15" />
          </linearGradient>
        </defs>
        <line className="vi-scope-axis" x1="0" x2={W} y1={mid} y2={mid} />
        {env !== null && <path className="vi-scope-env" d={env} />}
        <path
          className="vi-scope-line"
          d={line}
          stroke={scope.state === 'thinking' ? `url(#sh${gid})` : undefined}
          vectorEffect="non-scaling-stroke"
        />
      </svg>
      {!compact && <figcaption className="vi-scope-word">{word}</figcaption>}
    </figure>
  )
}

// ---------------------------------------------------------------------------
// Captions
// ---------------------------------------------------------------------------

const WHO: Record<CaptionLine['who'], string> = { bogdan: 'Bogdan', agent: 'Voice it', note: 'Console' }

/**
 * Both sides, speaker-labelled, newest last. The partial line is muted and
 * marked as still being spoken. `last` keeps only the newest lines (a
 * strip); omitted, every line is drawn and the box scrolls.
 */
export function Captions({ lines, last, className }: { lines: CaptionLine[]; last?: number; className?: string }) {
  const shown = last === undefined ? lines : lines.slice(-last)
  // The newest line is the one in view, as in any live caption box.
  const box = useRef<HTMLDivElement | null>(null)
  useLayoutEffect(() => {
    if (box.current) box.current.scrollTop = box.current.scrollHeight
  }, [shown.length])
  return (
    <div ref={box} className={`vi-captions${className ? ` ${className}` : ''}`} role="log" aria-live="polite" aria-label="Live captions">
      {shown.length === 0 && <p className="vi-cap-empty">Nothing said yet.</p>}
      {shown.map((l) => (
        <p key={l.id} className={`vi-cap is-${l.who}${l.partial ? ' is-partial' : ''}`}>
          <span className="vi-cap-who">{WHO[l.who]}</span>
          <time className="vi-cap-at">{l.at}</time>
          <span className="vi-cap-text">
            {l.text}
            {l.partial && <span className="vi-cap-more" aria-label="still speaking">…</span>}
          </span>
        </p>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Mic controls
// ---------------------------------------------------------------------------

/** Mic on/off, Mute, and Open mic or Push to talk. Static in the mock-ups. */
export function MicControls({ mic, onChange, compact = false }: { mic: MicState; onChange?: (m: MicState) => void; compact?: boolean }) {
  const set = (m: Partial<MicState>) => onChange?.({ ...mic, ...m })
  return (
    <div className={`vi-mic${compact ? ' is-compact' : ''}`} role="group" aria-label="Microphone">
      <Button kind={mic.on ? 'primary' : 'secondary'} aria-pressed={mic.on} icon={<MicGlyph off={!mic.on} />} onClick={() => set({ on: !mic.on })}>
        {mic.on ? 'Mic on' : 'Mic off'}
      </Button>
      <Button kind="secondary" aria-pressed={mic.muted} onClick={() => set({ muted: !mic.muted })} disabled={!mic.on}>
        {mic.muted ? 'Unmute' : 'Mute'}
      </Button>
      <Segmented
        small
        label="How the mic listens"
        value={mic.pushToTalk ? 'ptt' : 'open'}
        options={[
          { key: 'open', label: 'Open mic', title: 'Always listening; talk over Voice it to interrupt' },
          { key: 'ptt', label: 'Push to talk', title: 'Listens only while you hold Space' },
        ]}
        onChange={(k) => set({ pushToTalk: k === 'ptt' })}
      />
      {!compact && (
        <span className="vi-mic-state">
          {!mic.on ? 'mic off' : mic.muted ? 'muted: Voice it cannot hear you' : mic.pushToTalk ? 'hold Space to talk' : 'listening; you can interrupt'}
        </span>
      )}
    </div>
  )
}

export function MicGlyph({ off = false }: { off?: boolean }) {
  return (
    <svg className="c-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false" width="16" height="16">
      <rect x="9" y="3" width="6" height="11" rx="3" fill="none" stroke="currentColor" strokeWidth="1.8" />
      <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      {off && <path d="M4 4l16 16" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />}
    </svg>
  )
}

// ---------------------------------------------------------------------------
// Budget meter
// ---------------------------------------------------------------------------

function trackTone(spent: number, cap: number): 'is-warn' | 'is-bad' | undefined {
  if (spent >= cap) return 'is-bad'
  return spent / cap >= 0.9 ? 'is-warn' : undefined
}

function BudgetRow({ label, spent, cap }: { label: string; spent: number | null; cap: number }) {
  const pct = spent === null ? null : (spent / cap) * 100
  return (
    <div className="vi-budget-row">
      <span className="vi-budget-fig">
        {spent === null ? <span className="vi-absent">not read</span> : <b>{money(spent)}</b>} of {money(cap)} {label}
      </span>
      <UsageTrack pct={pct} tone={spent === null ? undefined : trackTone(spent, cap)} meter={{ label: `${label}: ${spent === null ? 'not read' : money(spent)} of ${money(cap)}`, now: spent ?? 0, max: cap }} />
    </div>
  )
}

/** "$23.40 of $100 this session, $61 of $300 today". `inline` is one line, no tracks. */
export function BudgetMeter({ budget, inline = false }: { budget: Budget; inline?: boolean }) {
  if (inline) {
    const f = (n: number | null) => (n === null ? 'not read' : money(n))
    const near = budget.session !== null && budget.session / budget.sessionCap >= 0.9
    return (
      <span className={`vi-budget is-inline${near ? ' is-near' : ''}`} aria-label="Budget">
        {near && <ToneMark tone="warn" />}
        {f(budget.session)} of {money(budget.sessionCap)} this session, {f(budget.day)} of {money(budget.dayCap)} today
      </span>
    )
  }
  return (
    <section className="vi-budget" aria-label="Budget">
      <BudgetRow label="this session" spent={budget.session} cap={budget.sessionCap} />
      <BudgetRow label="today" spent={budget.day} cap={budget.dayCap} />
    </section>
  )
}

// ---------------------------------------------------------------------------
// Project header
// ---------------------------------------------------------------------------

/** The quiet engines line: shown once, never a headline. */
export function EnginesLine() {
  return (
    <span className="vi-engines">
      Voice: {ENGINES.voice}, planner: {ENGINES.planner}
    </span>
  )
}

/** The project's name, its length, what opened it, and Resume / Projects. */
export function ProjectHeader({ project, actions, engines = true }: { project: Project; actions?: ReactNode; engines?: boolean }) {
  return (
    <header className="vi-project">
      <div className="vi-project-main">
        <h1 className="vi-project-name">{project.name}</h1>
        <span className="vi-project-meta">
          <span>{project.kind}</span>
          <span>started {project.startedAt}</span>
          <span>{project.length}</span>
          {project.resumable && <span>saved</span>}
        </span>
        {project.openedFrom && (
          <span className="vi-project-from">
            Opened from {project.openedFrom.what.toLowerCase()} <b>{project.openedFrom.name}</b>
            {' '}
            <ToneMark tone={project.openedFrom.state === 'failed' ? 'bad' : 'unknown'}>{project.openedFrom.state}</ToneMark>
            {project.openedFrom.detail && <span className="vi-project-detail">{project.openedFrom.detail}</span>}
          </span>
        )}
        {engines && <EnginesLine />}
      </div>
      <div className="vi-project-acts">
        {actions ?? (
          <>
            <Button kind="ghost" size="sm">Projects</Button>
            <Button kind="secondary" size="sm">{project.resumable ? 'Resume later' : 'End'}</Button>
          </>
        )}
      </div>
    </header>
  )
}

// ---------------------------------------------------------------------------
// What needs Bogdan
// ---------------------------------------------------------------------------

/**
 * Everything waiting on Bogdan, in one place: a card on screen (a link to it)
 * or an inbox item not opened yet. Empty says so, plainly.
 */
export function NeedsYou({ waiting, title = 'Needs you', className }: { waiting: Waiting[]; title?: string; className?: string }) {
  const hid = useId()
  return (
    <section className={`vi-needs${waiting.length > 0 ? ' has-items' : ''}${className ? ` ${className}` : ''}`} aria-labelledby={hid}>
      <h2 id={hid} className="vi-needs-h">
        {title} <span className="vi-needs-n">{waiting.length}</span>
      </h2>
      {waiting.length === 0 ? (
        <p className="vi-needs-none">Nothing is waiting on you.</p>
      ) : (
        <ul className="vi-needs-list">
          {waiting.map((w) => (
            <li key={w.id}>
              <ToneMark tone="warn" />
              {w.cardId ? (
                <button type="button" className="vi-needs-go" onClick={() => document.getElementById(`card-${w.cardId}`)?.scrollIntoView({ block: 'nearest' })}>
                  {w.text}
                </button>
              ) : (
                <span>{w.text}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

// ---------------------------------------------------------------------------
// Standing preferences
// ---------------------------------------------------------------------------

/** Bogdan's standing preferences, kept across projects; each can be removed. */
export function Preferences({ items }: { items: string[] }) {
  return (
    <section className="vi-prefs" aria-label="What Voice it remembers about you">
      <h2 className="vi-prefs-h">What I remember about you</h2>
      <div className="vi-prefs-list">
        {items.map((p) => (
          <Chip key={p} onRemove={() => undefined} removeLabel={`Forget: ${p}`}>
            {p}
          </Chip>
        ))}
      </div>
      <Button kind="ghost" size="sm">Add a preference</Button>
    </section>
  )
}
