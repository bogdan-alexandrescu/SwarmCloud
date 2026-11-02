/**
 * VOICE IT'S RING: the agent's voice drawn as a luminous radial trace, the
 * presence direction's centrepiece (docs/design/voice-it.md §3.10, §11).
 *
 * THE ANALYSER IS A TAP, NOT A ROUTE. The agent's voice plays from the WebRTC
 * track on an <audio> element (Talk.tsx). Here the same stream also feeds an
 * `AnalyserNode` (fftSize 2048) that is connected to nothing: an analyser
 * reads its input without being connected to an output, so no sound goes
 * through Web Audio and the browser's echo cancellation, which covers WebRTC
 * remote audio, stays on for open mic with barge-in (MDN AnalyserNode,
 * `echoCancellation`). The mic gets a second tap, drawn as the small inner
 * trace while the person speaks.
 *
 * The trace is written into the SVG paths on each animation frame through
 * refs, not React state: sixty renders a second of the whole pane would be
 * the cost of a moving line. With `prefers-reduced-motion` the ring is still
 * and a level bar shows the voice instead.
 */

import { useEffect, useId, useRef, useState } from 'react'

export type ScopeState = 'idle' | 'connecting' | 'listening' | 'speaking'

const WORD: Record<ScopeState, string> = {
  idle: 'Not connected',
  connecting: 'Connecting',
  listening: 'Listening',
  speaking: 'Speaking',
}

const C = 200
const R = 120
const VOICE_POINTS = 180
const MIC_POINTS = 140
/** The analyser's window: 2048 samples, about 43 ms at 48 kHz (§3.10). */
export const FFT_SIZE = 2048

/** A moving average, so the ring blooms in syllables rather than bristling. */
function smooth(v: Float32Array | number[], w: number): number[] {
  const n = v.length
  const out = new Array<number>(n)
  for (let i = 0; i < n; i++) {
    let sum = 0
    for (let k = -w; k <= w; k++) sum += Math.abs(v[(i + k + n) % n] ?? 0)
    out[i] = sum / (2 * w + 1)
  }
  return out
}

/** `points` evenly spaced readings of a time-domain buffer. */
function pick(buf: Float32Array, points: number): number[] {
  const out = new Array<number>(points)
  const step = buf.length / points
  for (let i = 0; i < points; i++) out[i] = buf[Math.floor(i * step)] ?? 0
  return out
}

/** A closed path around the centre, radius `base + amp * v` at each sample. */
function radial(samples: number[], base: number, amp: number): string {
  const n = samples.length
  if (n === 0) return ''
  return (
    samples
      .map((v, i) => {
        const a = (i / n) * Math.PI * 2 - Math.PI / 2
        const r = base + amp * Math.min(1, v)
        return `${i === 0 ? 'M' : 'L'}${(C + r * Math.cos(a)).toFixed(1)},${(C + r * Math.sin(a)).toFixed(1)}`
      })
      .join(' ') + ' Z'
  )
}

function rms(buf: Float32Array): number {
  let sum = 0
  for (let i = 0; i < buf.length; i++) sum += (buf[i] ?? 0) * (buf[i] ?? 0)
  return Math.sqrt(sum / Math.max(1, buf.length))
}

function reducedMotion(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

type Ctx = typeof AudioContext

function audioContextClass(): Ctx | null {
  if (typeof window === 'undefined') return null
  const w = window as unknown as { AudioContext?: Ctx; webkitAudioContext?: Ctx }
  return w.AudioContext ?? w.webkitAudioContext ?? null
}

interface Tap {
  analyser: AnalyserNode
  buf: Float32Array<ArrayBuffer>
}

function tap(ctx: AudioContext, stream: MediaStream): Tap {
  const source = ctx.createMediaStreamSource(stream)
  const analyser = ctx.createAnalyser()
  analyser.fftSize = FFT_SIZE
  // Into the analyser and no further: never `analyser.connect(ctx.destination)`.
  source.connect(analyser)
  return { analyser, buf: new Float32Array(new ArrayBuffer(FFT_SIZE * 4)) }
}

/**
 * The ring. `voice` is the agent's remote stream, `mic` the person's; either
 * may be null (not connected yet, or a browser with no Web Audio, where the
 * ring stays still and says its state in words).
 */
export function Ring({ state, voice, mic }: { state: ScopeState; voice: MediaStream | null; mic: MediaStream | null }) {
  const gid = useId().replace(/:/g, '')
  const wave = useRef<SVGPathElement | null>(null)
  const glow = useRef<SVGPathElement | null>(null)
  const micPath = useRef<SVGPathElement | null>(null)
  const bar = useRef<HTMLSpanElement | null>(null)
  const [still] = useState(reducedMotion)
  const stateRef = useRef(state)
  stateRef.current = state

  useEffect(() => {
    const Klass = audioContextClass()
    if (Klass === null || (voice === null && mic === null)) return
    const ctx = new Klass()
    if (typeof ctx.resume === 'function' && ctx.state === 'suspended') void ctx.resume().catch(() => undefined)
    const v = voice === null ? null : tap(ctx, voice)
    const m = mic === null ? null : tap(ctx, mic)
    const raf = typeof window.requestAnimationFrame === 'function' ? window.requestAnimationFrame.bind(window) : null
    const caf = typeof window.cancelAnimationFrame === 'function' ? window.cancelAnimationFrame.bind(window) : null
    let frame: number | null = null
    const draw = () => {
      const speaking = stateRef.current === 'speaking'
      if (v !== null) {
        v.analyser.getFloatTimeDomainData(v.buf)
        if (still) {
          if (bar.current) bar.current.style.setProperty('--level', `${Math.min(100, Math.round(rms(v.buf) * 400))}%`)
        } else {
          const d = speaking ? radial(smooth(pick(v.buf, VOICE_POINTS), 3), R, 92) : ''
          wave.current?.setAttribute('d', d)
          glow.current?.setAttribute('d', d)
        }
      }
      if (m !== null && !still) {
        m.analyser.getFloatTimeDomainData(m.buf)
        micPath.current?.setAttribute('d', speaking ? '' : radial(smooth(pick(m.buf, MIC_POINTS), 1), R - 12, 60))
      }
      frame = raf === null ? null : raf(draw)
    }
    if (raf !== null) frame = raf(draw)
    return () => {
      if (frame !== null && caf !== null) caf(frame)
      void ctx.close().catch(() => undefined)
    }
  }, [voice, mic, still])

  const word = WORD[state]
  return (
    <figure className={`vt-ring is-${state}${still ? ' is-still' : ''}`} data-state={state}>
      <svg viewBox="0 0 400 400" role="img" aria-label={`Voice it: ${word}`}>
        <defs>
          <radialGradient id={`halo${gid}`} cx="0.5" cy="0.5" r="0.5">
            <stop offset="0.45" stopColor="currentColor" stopOpacity="0" />
            <stop offset="0.62" stopColor="currentColor" stopOpacity="0.16" />
            <stop offset="1" stopColor="currentColor" stopOpacity="0" />
          </radialGradient>
          <filter id={`glow${gid}`} x="-30%" y="-30%" width="160%" height="160%">
            <feGaussianBlur stdDeviation="7" />
          </filter>
        </defs>
        <circle className="vt-ring-halo" cx={C} cy={C} r={196} fill={`url(#halo${gid})`} />
        <circle className="vt-ring-base" cx={C} cy={C} r={R} />
        <circle className="vt-ring-still" cx={C} cy={C} r={R} />
        <path ref={glow} className="vt-ring-glow" d="" filter={`url(#glow${gid})`} />
        <path ref={wave} className="vt-ring-wave" d="" />
        <path ref={micPath} className="vt-ring-mic" d="" />
      </svg>
      <figcaption className="vt-ring-word">
        <span className="vt-ring-name">Voice it</span>
        <span className="vt-ring-state">{word}</span>
      </figcaption>
      {still && voice !== null && (
        <span className="vt-ring-level" aria-hidden="true">
          <span ref={bar} className="vt-ring-level-fill" />
        </span>
      )}
    </figure>
  )
}
