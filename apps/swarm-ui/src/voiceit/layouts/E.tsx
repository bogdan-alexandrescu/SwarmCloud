// MOCK-HOOK FILE
/**
 * VOICE IT, DIRECTION E: A PRESENCE (mock-ups only, ?vi=e).
 *
 * The agent is a luminous ring, a radial waveform drawn as inline SVG, at the
 * centre of a quiet stage. What it makes floats beside it as translucent glass
 * panels; the one being talked about comes forward, larger and lit, and the
 * rest recede (smaller, dimmer, blurred behind glass). Status runs as a quiet
 * heads-up line along the top edge (budget, lanes, what needs Bogdan), the
 * captions run as a subtitle band under the stage, and the mic sits on the
 * bottom edge. Flat 2D: depth comes only from blur, glow and scale, all of it
 * built from the console's tokens.
 *
 * WHAT NEEDS BOGDAN is in the heads-up line at the top of every frame, and
 * every receded panel that needs him keeps a warm edge, so it is findable
 * whichever panel is forward.
 */
import { useId, useState, type MouseEvent } from 'react'

import { Button, ToneMark } from '../../components'
import { KIND_WORD, TONE, VoiceCardView, duration } from '../cards'
import { SAVED_PROJECTS, scopeSamples, type Frame, type Lane, type Scope, type VoiceCard } from '../fixtures'
import { BudgetMeter, Captions, MicControls, NeedsYou, Preferences, ProjectHeader, money } from '../parts'
import type { LayoutProps } from '../VoiceIt'
import './E.css'

// ---------------------------------------------------------------------------
// The ring
// ---------------------------------------------------------------------------

const RING_WORD: Record<Scope['state'], string> = {
  speaking: 'Speaking',
  listening: 'Listening',
  thinking: 'Thinking',
  idle: 'Waiting for you',
}

const C = 200
const R = 120

/** A moving average, so the ring blooms in syllables rather than bristling. */
function smooth(v: number[], w: number): number[] {
  const n = v.length
  return v.map((_, i) => {
    let sum = 0
    for (let k = -w; k <= w; k++) sum += Math.abs(v[(i + k + n) % n] ?? 0)
    return sum / (2 * w + 1)
  })
}

/** A closed path around the centre, radius `R + amp * f(v)` at each sample. */
function radial(samples: number[], base: number, amp: number, f: (v: number) => number = Math.abs): string {
  const n = samples.length
  return (
    samples
      .map((v, i) => {
        const a = (i / n) * Math.PI * 2 - Math.PI / 2
        const r = base + amp * f(v)
        return `${i === 0 ? 'M' : 'L'}${(C + r * Math.cos(a)).toFixed(1)},${(C + r * Math.sin(a)).toFixed(1)}`
      })
      .join(' ') + ' Z'
  )
}

/**
 * The voice as a ring: speaking blooms outward with the waveform, listening
 * draws Bogdan's mic level as a small inner ring, thinking is a calm shimmer
 * with what the planner is doing, idle is a faint still circle.
 */
export function Ring({ scope, micLevel, size }: { scope: Scope; micLevel: number | null; size: 'large' | 'medium' | 'small' }) {
  const gid = useId().replace(/:/g, '')
  const word = RING_WORD[scope.state]
  const label = scope.state === 'thinking' && scope.label ? `${word}: ${scope.label}` : word
  const raw = scope.state === 'speaking' ? scopeSamples('speaking', scope.seed, 180) : null
  const voice = raw === null ? null : smooth(raw, 3)
  const mic = scope.state === 'listening' ? scopeSamples('listening', scope.seed, 140, micLevel ?? 0.4) : null
  return (
    <figure className={`vi-e-ring is-${scope.state} is-${size}`} data-state={scope.state}>
      <svg viewBox="0 0 400 400" role="img" aria-label={`Voice it: ${label}`}>
        <defs>
          <radialGradient id={`halo${gid}`} cx="0.5" cy="0.5" r="0.5">
            <stop offset="0.45" stopColor="currentColor" stopOpacity="0" />
            <stop offset="0.62" stopColor="currentColor" stopOpacity="0.16" />
            <stop offset="1" stopColor="currentColor" stopOpacity="0" />
          </radialGradient>
          <linearGradient id={`shim${gid}`} x1="0" x2="1" y1="0" y2="1">
            <stop offset="0" stopColor="currentColor" stopOpacity="0.1" />
            <stop offset="0.5" stopColor="currentColor" stopOpacity="0.95" />
            <stop offset="1" stopColor="currentColor" stopOpacity="0.1" />
          </linearGradient>
          <filter id={`glow${gid}`} x="-30%" y="-30%" width="160%" height="160%">
            <feGaussianBlur stdDeviation="7" />
          </filter>
        </defs>
        <circle className="vi-e-ring-halo" cx={C} cy={C} r={196} fill={`url(#halo${gid})`} />
        <circle className="vi-e-ring-base" cx={C} cy={C} r={R} />
        {voice !== null && (
          <>
            <path className="vi-e-ring-glow" d={radial(voice, R, 92)} filter={`url(#glow${gid})`} />
            <path className="vi-e-ring-fill" d={radial(voice, R, 92)} />
            <path className="vi-e-ring-wave" d={radial(voice, R, 92)} />
            <path className="vi-e-ring-inner" d={radial(raw ?? [], R - 4, -16)} />
          </>
        )}
        {mic !== null && (
          <>
            <circle className="vi-e-ring-still" cx={C} cy={C} r={R} />
            <path className="vi-e-ring-mic" d={radial(smooth(mic, 1), R - 12, 60)} />
          </>
        )}
        {scope.state === 'thinking' && (
          <g className="vi-e-ring-spin">
            <circle className="vi-e-ring-shim" cx={C} cy={C} r={R} stroke={`url(#shim${gid})`} filter={`url(#glow${gid})`} />
            <circle className="vi-e-ring-shim" cx={C} cy={C} r={R} stroke={`url(#shim${gid})`} />
            <circle className="vi-e-ring-shim2" cx={C} cy={C} r={R - 14} />
          </g>
        )}
        {scope.state === 'idle' && <circle className="vi-e-ring-still" cx={C} cy={C} r={R} />}
      </svg>
      <figcaption className="vi-e-ring-word">
        <span className="vi-e-ring-name">Voice it</span>
        <span className="vi-e-ring-state">{word}</span>
        {scope.state === 'thinking' && scope.label && <span className="vi-e-ring-label">{scope.label}</span>}
      </figcaption>
    </figure>
  )
}

// ---------------------------------------------------------------------------
// Receded panels
// ---------------------------------------------------------------------------

/** One plain line that says what a receded panel holds, without opening it. */
function gist(c: VoiceCard): string {
  switch (c.kind) {
    case 'task': return c.when ?? 'not scheduled'
    case 'running': {
      const run = c.lanes.filter((l) => l.state === 'running').length
      return `${c.lanes.length} lanes, ${run} running`
    }
    case 'review': return `${c.repo} #${c.number}, ${c.files} files`
    case 'design': return c.picked ? `Picked ${c.picked}` : `${c.options.map((o) => o.key).join(' or ')}, I suggest ${c.recommended}`
    case 'diagram': return `${c.diagram.kind}, ${c.diagram.nodes.length} parts`
    case 'mockup': return c.frames.map((f) => f.label).join(', ')
    case 'plan': {
      const lanes = c.phases.reduce((n, p) => n + p.lanes.length, 0)
      return `${c.phases.length} phases, ${lanes} lanes, about ${money(c.totalCost)} and ${duration(c.totalMinutes)}`
    }
    case 'decision': return c.question
    case 'input': return c.value ?? c.question
    case 'credential': return c.entered ? 'Stored in the secure form' : 'Opens the secure form'
    case 'failure': return c.where
    case 'shipped': return c.cost === null ? `${c.url}, cost not known yet` : `${c.url}, ${money(c.cost)}`
    case 'cap': return `${money(c.spent)} of ${money(c.cap)} ${c.which === 'session' ? 'this session' : 'today'}`
  }
}

const LANE_TONE: Record<Lane['state'], string> = { done: 'ok', failed: 'bad', 'waiting on you': 'warn', queued: 'info', running: 'live' }

/** A card that is not the one being discussed: small, dim, behind glass; click to bring it forward. */
function Glance({ card, onForward }: { card: VoiceCard; onForward: () => void }) {
  const needs = card.needs !== undefined
  return (
    <article id={`card-${card.id}`} className={`vi-e-glance is-${card.kind}${needs ? ' is-needs' : ''}`} aria-label={`${KIND_WORD[card.kind]}: ${card.title}`}>
      <p className="vi-e-glance-h">
        <span className="vi-card-kind">{KIND_WORD[card.kind]}</span>
        <span className="vi-e-glance-state">
          <ToneMark tone={TONE[card.tone]}>{card.state}</ToneMark>
          <time className="vi-card-at">{card.at}</time>
        </span>
      </p>
      <p className="vi-e-glance-title">{card.title}</p>
      <p className="vi-e-glance-gist">{gist(card)}</p>
      {card.kind === 'running' && (
        <span className="vi-e-glance-lanes" aria-hidden="true">
          {card.lanes.map((l) => (
            <span key={l.name} className={`vi-e-lanebit is-${LANE_TONE[l.state]}`} style={{ ['--p' as string]: `${l.pct ?? 0}%` }} />
          ))}
        </span>
      )}
      <Button kind="ghost" size="sm" onClick={onForward} className="vi-e-glance-go">
        Bring forward
      </Button>
    </article>
  )
}

// ---------------------------------------------------------------------------
// Which card is forward
// ---------------------------------------------------------------------------

/**
 * The card the conversation is on at each moment, as the captions tell it;
 * any other frame puts forward the newest card that needs Bogdan, else the
 * newest card.
 */
const DISCUSSED: Record<string, string> = {
  'new-app/cards': 'rb-arch',
  'new-app/decide': 'rb-plan',
  'new-app/result': 'rb-ship',
  'rescue/cards': 'rs-fail',
  'rescue/decide': 'rs-fix',
  'rescue/result': 'rs-ship',
  'feature/cards': 'nw-model',
  'feature/decide': 'nw-key',
  'feature/result': 'nw-ship',
  'flex/cards': 'fx-mods',
  'flex/decide': 'fx-plan',
  'flex/result': 'fx-run',
}

function discussed(frame: Frame): string | null {
  const id = DISCUSSED[`${frame.scenario}/${frame.stage}`]
  if (id !== undefined && frame.cards.some((c) => c.id === id)) return id
  const needs = frame.cards.filter((c) => c.needs !== undefined)
  const pick = needs.length > 0 ? needs[0] : frame.cards[frame.cards.length - 1]
  return pick?.id ?? null
}

// ---------------------------------------------------------------------------
// Heads-up line
// ---------------------------------------------------------------------------

function lanesWord(frame: Frame): { text: string; live: boolean } {
  const lanes = frame.cards.flatMap((c) => (c.kind === 'running' ? c.lanes : []))
  if (lanes.length === 0) return { text: 'No lanes running', live: false }
  const run = lanes.filter((l) => l.state === 'running').length
  const wait = lanes.filter((l) => l.state === 'waiting on you').length
  const done = lanes.filter((l) => l.state === 'done').length
  const parts = [run === 0 ? 'No lanes running' : `${run} ${run === 1 ? 'lane' : 'lanes'} running`]
  if (wait > 0) parts.push(`${wait} waiting on you`)
  if (done > 0) parts.push(`${done} done`)
  return { text: parts.join(', '), live: run > 0 }
}

// ---------------------------------------------------------------------------
// The layout
// ---------------------------------------------------------------------------

function Presence({ frame }: LayoutProps) {
  const [forward, setForward] = useState<string | null>(() => discussed(frame))
  const focus = frame.cards.find((c) => c.id === forward) ?? null
  const rest = frame.cards.filter((c) => c !== focus)
  const lanes = lanesWord(frame)
  const open = focus === null
  const bring = (id: string) => setForward(id)
  // The heads-up list's links bring their card forward, not only into view.
  const linked = frame.waiting.filter((w) => w.cardId !== undefined)
  const onNeeds = (e: MouseEvent<HTMLDivElement>) => {
    const btn = (e.target as HTMLElement).closest('.vi-needs-go')
    if (btn === null) return
    const all = Array.from(e.currentTarget.querySelectorAll('.vi-needs-go'))
    const w = linked[all.indexOf(btn)]
    if (w?.cardId) setForward(w.cardId)
  }
  return (
    <div className={`vi-e${open ? ' is-open' : ' has-focus'}`} data-scope={frame.scope.state}>
      <div className="vi-e-sky" aria-hidden="true" />

      <div className="vi-e-hud">
        <ProjectHeader
          project={frame.project}
          actions={
            <>
              <Button kind="ghost" size="sm">Projects</Button>
              {frame.stage !== 'rest' && <Button kind="ghost" size="sm">What I remember ({frame.preferences.length})</Button>}
              <Button kind="secondary" size="sm">{frame.project.resumable ? 'Resume later' : 'End'}</Button>
            </>
          }
        />
        <div className="vi-e-status" role="group" aria-label="Status">
          <span className="vi-e-stat">
            <BudgetMeter budget={frame.budget} inline />
          </span>
          <span className={`vi-e-stat${lanes.live ? ' is-live' : ''}`}>
            <ToneMark tone={lanes.live ? 'live' : 'unknown'} />
            {lanes.text}
          </span>
          <div className={`vi-e-needs-wrap${frame.waiting.length > 0 ? ' has-items' : ''}`} onClickCapture={onNeeds}>
            <NeedsYou waiting={frame.waiting} className="vi-e-needs" />
          </div>
        </div>
      </div>

      {/* The presence: the ring, its words and the mic. On a wide screen its
          parts sit in the page's grid; on a phone with a card forward they
          dock together along the bottom edge. */}
      <div className="vi-e-presence" aria-label="Voice it">
        <div className="vi-e-core">
          <Ring scope={frame.scope} micLevel={frame.mic.level} size={open ? 'large' : 'medium'} />
        </div>
        <div className="vi-e-subs-band">
          <Captions lines={frame.captions} last={3} className="vi-e-subs" />
          {frame.captions.length > 3 && (
            <Button kind="ghost" size="sm" className="vi-e-subs-all">
              Whole transcript, {frame.captions.length} lines
            </Button>
          )}
        </div>
        <div className="vi-e-mic">
          <MicControls mic={frame.mic} />
        </div>
      </div>

      {focus !== null && (
        <div className="vi-e-focus" aria-label="Being discussed">
          <VoiceCardView key={focus.id} card={focus} />
        </div>
      )}

      {rest.length > 0 && (
        <div className="vi-e-orbit" aria-label="Also on the table">
          <p className="vi-e-orbit-h">Also on the table</p>
          {rest.map((c) => (
            <Glance key={c.id} card={c} onForward={() => bring(c.id)} />
          ))}
        </div>
      )}

      {open && frame.stage === 'rest' && (
        <>
          <section className="vi-e-float is-left" aria-label="Saved projects">
            <h2 className="vi-prefs-h">Saved projects</h2>
            <ul className="vi-e-projects">
              {SAVED_PROJECTS.map((p) => (
                <li key={p.id}>
                  <span className="vi-e-proj-name">{p.name}</span>
                  <span className="vi-e-proj-meta">
                    {p.when}, {p.state}
                  </span>
                  <Button kind="ghost" size="sm">Resume</Button>
                </li>
              ))}
            </ul>
          </section>
          <section className="vi-e-float is-right">
            <Preferences items={frame.preferences} />
          </section>
        </>
      )}
    </div>
  )
}

/** Direction E. Keyed by the frame, so a new moment starts from what it discusses. */
export function PresenceLayout({ frame }: LayoutProps) {
  return <Presence key={`${frame.scenario}/${frame.stage}`} frame={frame} />
}
