/**
 * THE STATE MARKS: the brand vocabulary for a task's state, used everywhere a
 * state is drawn, workflow nodes included (rebrand, owner decision 2026-10-01;
 * brand.html, the mock-ups' common.py MARKS and STATE). It supersedes #405's
 * tints.
 *
 * Every state has EXACTLY ONE mark and ONE hue, and the mark alone separates
 * them -- the hue is the second channel, never the only one:
 *
 *   SUBMITTED / QUEUED     ring                 grey   in line, nothing decided
 *   READY                  ring with a dot      grey   eligible, waiting for room
 *   PARKED                 pause bars           violet holds nothing (invariant 1)
 *   LEASED / DISPATCHED /  half disc            teal   holds a slot, not yet running
 *     STARTING
 *   RUNNING                haloed disc          teal
 *   SUCCEEDED              check                grey
 *   CANCELLED              flat bar             grey
 *   FAILED                 solid diamond        red
 *   DEAD_LETTERED          outlined diamond     red
 *
 * The amber triangle is NOT a state. It is kept for warnings only (`WarnMark`).
 *
 * The four teal states are exactly the four that create infrastructure demand
 * (CONTRACT invariant 1), which is why they share one hue: the hue answers
 * "does this hold capacity", the mark answers "how far along is it".
 */
import type { TaskState } from './types'

export type MarkName =
  | 'queued'
  | 'ready'
  | 'parked'
  | 'starting'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'dead'
  | 'cancelled'

export type MarkHue = 'neu' | 'park' | 'live' | 'bad'

export const STATE_MARK: Readonly<Record<TaskState, { mark: MarkName; hue: MarkHue }>> = {
  // SUBMITTED is never stored on a task (types.ts); drawn as the ring, in line.
  SUBMITTED: { mark: 'queued', hue: 'neu' },
  QUEUED: { mark: 'queued', hue: 'neu' },
  READY: { mark: 'ready', hue: 'neu' },
  PARKED: { mark: 'parked', hue: 'park' },
  LEASED: { mark: 'starting', hue: 'live' },
  DISPATCHED: { mark: 'starting', hue: 'live' },
  STARTING: { mark: 'starting', hue: 'live' },
  RUNNING: { mark: 'running', hue: 'live' },
  SUCCEEDED: { mark: 'succeeded', hue: 'neu' },
  FAILED: { mark: 'failed', hue: 'bad' },
  DEAD_LETTERED: { mark: 'dead', hue: 'bad' },
  CANCELLED: { mark: 'cancelled', hue: 'neu' },
}

/** The 12-unit glyph for one mark. The geometry is the mock-ups' MARKS, verbatim. */
export function MarkGlyph({ mark }: { mark: MarkName | 'warn' }) {
  switch (mark) {
    case 'queued':
      return <circle cx="6" cy="6" r="4.3" fill="none" stroke="currentColor" strokeWidth="1.6" />
    case 'ready':
      return (
        <>
          <circle cx="6" cy="6" r="4.3" fill="none" stroke="currentColor" strokeWidth="1.6" />
          <circle cx="6" cy="6" r="1.7" fill="currentColor" />
        </>
      )
    case 'parked':
      return (
        <>
          <rect x="2.6" y="1.8" width="2.4" height="8.4" rx=".8" fill="currentColor" />
          <rect x="7" y="1.8" width="2.4" height="8.4" rx=".8" fill="currentColor" />
        </>
      )
    case 'starting':
      return (
        <>
          <circle cx="6" cy="6" r="4.3" fill="none" stroke="currentColor" strokeWidth="1.6" />
          <path d="M6 1.7a4.3 4.3 0 0 1 0 8.6Z" fill="currentColor" />
        </>
      )
    case 'running':
      return (
        <>
          <circle cx="6" cy="6" r="5.4" fill="none" stroke="currentColor" strokeWidth="1" opacity=".45" />
          <circle cx="6" cy="6" r="3.3" fill="currentColor" />
        </>
      )
    case 'succeeded':
      return (
        <path
          d="M2 6.4 4.8 9.1 10 3"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.9"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      )
    case 'failed':
      return <path d="M6 .8 11.2 6 6 11.2.8 6Z" fill="currentColor" />
    case 'dead':
      return (
        <>
          <path d="M6 1.2 10.8 6 6 10.8 1.2 6Z" fill="none" stroke="currentColor" strokeWidth="1.5" />
          <path d="m4.4 4.4 3.2 3.2M7.6 4.4 4.4 7.6" stroke="currentColor" strokeWidth="1.3" />
        </>
      )
    case 'cancelled':
      return <rect x="1.5" y="4.8" width="9" height="2.4" rx="1.2" fill="currentColor" />
    case 'warn':
      return <path d="M6 1.2 11.2 10.4H.8Z" fill="currentColor" />
  }
}

/**
 * One state, drawn: the mark in its hue and, unless `bare`, the state word
 * beside it in ink. `data-mark` and `data-hue` are what the tests read.
 */
export function StateMark({
  state,
  label,
  bare = false,
}: {
  state: TaskState
  label?: string
  bare?: boolean
}) {
  const { mark, hue } = STATE_MARK[state]
  const word = label ?? state.toLowerCase().replace('_', '-')
  return (
    <span className={`sk-st is-${hue}`} data-mark={mark} data-hue={hue} title={bare ? word : undefined}>
      <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
        <MarkGlyph mark={mark} />
      </svg>
      {bare ? <span className="sk-vh">{word}</span> : <span className="sk-st-w">{word}</span>}
    </span>
  )
}

/** The amber triangle: a warning, never a state. */
export function WarnMark({ label }: { label?: string }) {
  return (
    <span className="sk-st is-warn" data-mark="warn" data-hue="warn">
      <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
        <MarkGlyph mark="warn" />
      </svg>
      {label !== undefined && <span className="sk-st-w">{label}</span>}
    </span>
  )
}
