/**
 * THE WORKING CARD (docs/design/voice-it.md §3.12.5; the owner's pick of
 * 12 November 2026, "Crew strip"): from a delegation's start until its next
 * output, one card in the caption band, between the last reply and the next.
 *
 * Collapsed (the default) it is one line: a live dot (still under
 * `prefers-reduced-motion`), what the agent is doing now and the time since
 * the delegation started. Expanded, the delegation's steps, newest last, at
 * most `STEPS_KEPT`, the older folded into "and N earlier". With two
 * delegations in flight it follows the oldest and says "and 1 more". The
 * delegation's end removes it, steps and all (CardList.tsx `applyWork`).
 *
 * A STEP IS ITS PHRASE AND ITS IDS. swarm-api writes the phrase from its fixed
 * table and the ids its path named (workfeed.py); nothing else of a step,
 * no query, no body, no text, is drawn.
 */

import { useEffect, useId, useState } from 'react'

import '../styles/voice-activity.css'
import type { Delegation, WorkStep } from './CardList'

/** m:ss, for the elapsed time and a step's offset. */
export function clock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

/** A step's line: its phrase with the ids its path named put in their places. */
export function stepLine(step: Pick<WorkStep, 'phrase' | 'ids'>): string {
  return step.phrase.replace(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g, (_, name: string) => step.ids[name] ?? '…')
}

const ms = (iso: string | null): number | null => {
  const t = iso === null ? NaN : Date.parse(iso)
  return Number.isFinite(t) ? t : null
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg className={`vtc-ic vtc-chev${open ? ' is-open' : ''}`} viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M7 10l5 5 5-5" />
    </svg>
  )
}

/** The working card for `work` (the delegations in flight, oldest first), or nothing when none is. */
export function WorkingCard({ work }: { work: readonly Delegation[] }) {
  const id = useId()
  const [open, setOpen] = useState(false)
  const [now, setNow] = useState(() => Date.now())
  const d = work[0]
  const live = d !== undefined
  useEffect(() => {
    if (!live) return
    const t = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(t)
  }, [live])
  if (d === undefined) return null
  const started = ms(d.at)
  const newest = d.steps[d.steps.length - 1]
  const more = work.length - 1
  return (
    <div className={`vtc-work${open ? ' is-open' : ''}`} data-delegation={d.delegation_id}>
      <button type="button" className="vtc-work-head" aria-expanded={open} aria-controls={id} onClick={() => setOpen((o) => !o)}>
        <span className="vtc-work-live" aria-hidden="true" />
        <span className="vtc-work-now">{newest === undefined ? 'Working' : stepLine(newest)}</span>
        {more > 0 && <span className="vtc-work-more">and {more} more</span>}
        {started !== null && <span className="vtc-work-time">{clock((now - started) / 1000)}</span>}
        <Chevron open={open} />
      </button>
      {open && (
        <ol id={id} className="vtc-steps" aria-label="Steps">
          {d.folded > 0 && <li className="vtc-step is-folded">and {d.folded} earlier</li>}
          {d.steps.map((s, i) => {
            const at = ms(s.at)
            return (
              <li key={s.seq} className={`vtc-step${i === d.steps.length - 1 ? ' is-newest' : ''}`}>
                <span className="vtc-step-at">{started !== null && at !== null ? clock((at - started) / 1000) : ''}</span>
                <span className="vtc-step-dot" aria-hidden="true" />
                <span className="vtc-step-text">{stepLine(s)}</span>
              </li>
            )
          })}
        </ol>
      )}
    </div>
  )
}
