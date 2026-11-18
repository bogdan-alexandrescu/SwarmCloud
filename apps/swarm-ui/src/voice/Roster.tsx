/**
 * THE AGENT ROSTER, DRAWN AS THE CREW STRIP (docs/design/voice-it.md §3.12.6;
 * the owner's pick of 12 November 2026, "Crew strip").
 *
 * One chip per row: the planner's delegations and every agent, workflow and
 * issue run the project set going, with its follow-ups, each with its kind,
 * title, state and progress (a workflow's steps done of total, a run's lanes
 * merged of total). The rows come from the card list's state: the roster
 * route read once, then the stream's `roster.update` events (CardList.tsx).
 * Pressing or hovering a chip shows its line and, for what has a console
 * page, Open, which is a click through the console's own `go`.
 */

import { useEffect, useId, useState, type ReactNode } from 'react'

import { Button } from '../components'
import '../styles/voice-activity.css'
import type { RosterKind, RosterRow, RosterState } from './CardList'
import { objectPath } from './cards/NavigateCard'

const KIND_WORD: Readonly<Record<RosterKind, string>> = { delegation: 'Planner', agent: 'Agent', workflow: 'Workflow', run: 'Issue run' }

const STATE_WORD: Readonly<Record<RosterState, string>> = {
  queued: 'Queued',
  running: 'Working',
  waiting: 'Waiting on you',
  done: 'Done',
  failed: 'Failed',
  cancelled: 'Cancelled',
}

const TONE: Readonly<Record<RosterState, string>> = { queued: 'idle', running: 'live', waiting: 'paused', done: 'ok', failed: 'bad', cancelled: 'idle' }

/** A row's console page, or null for a delegation (it has none). */
export function rowHref(row: Pick<RosterRow, 'kind' | 'id'>): string | null {
  return row.kind === 'delegation' ? null : objectPath(row.kind, row.id)
}

function KindIcon({ kind }: { kind: RosterKind }) {
  const d: Record<Exclude<RosterKind, 'run'>, ReactNode> = {
    delegation: (
      <>
        <circle cx="6" cy="6" r="2.2" />
        <circle cx="18" cy="6" r="2.2" />
        <circle cx="12" cy="18" r="2.2" />
        <path d="M6 8.2v2.3a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V8.2M12 12.5v3.3" />
      </>
    ),
    agent: <path d="M9 7l-5 5 5 5M15 7l5 5-5 5M13.5 5l-3 14" />,
    workflow: (
      <>
        <rect x="3" y="4" width="6" height="5" rx="1.2" />
        <rect x="15" y="15" width="6" height="5" rx="1.2" />
        <path d="M6 9v3.5a2 2 0 0 0 2 2h6M12 12l2.5 2.5L12 17" />
      </>
    ),
  }
  return (
    <svg className="vtc-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      {d[kind === 'run' ? 'workflow' : kind]}
    </svg>
  )
}

/** "3 of 5", or null when the row carries no total. */
function progressOf(row: RosterRow): { line: string; pct: number } | null {
  if (row.total === null || row.total === 0) return null
  const done = Math.min(row.done ?? 0, row.total)
  return { line: `${done} of ${row.total}`, pct: Math.round((done / row.total) * 100) }
}

function Chip({ row, open, onOpen, navigate }: { row: RosterRow; open: boolean; onOpen: (key: string | null) => void; navigate: (to: string) => void }) {
  const id = useId()
  const [hover, setHover] = useState(false)
  const key = `${row.kind}:${row.id}`
  const name = row.title ?? row.id
  const progress = progressOf(row)
  const href = rowHref(row)
  const shown = open || hover
  return (
    <li className={`vtc-chip is-${TONE[row.state]} is-${row.kind}`} data-row={key} onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}>
      <button
        type="button"
        className="vtc-chip-btn"
        aria-expanded={shown}
        aria-controls={id}
        aria-label={`${name}, ${KIND_WORD[row.kind]}, ${STATE_WORD[row.state]}`}
        onClick={() => onOpen(open ? null : key)}
        onKeyDown={(e) => {
          if (e.key === 'Escape') onOpen(null)
        }}
      >
        <span className="vtc-chip-ic">
          <KindIcon kind={row.kind} />
          {row.state === 'running' && <span className="vtc-chip-live" aria-hidden="true" />}
        </span>
        <span className="vtc-chip-name">{name}</span>
        <span className="vtc-chip-run">{STATE_WORD[row.state]}</span>
        <span className="vtc-chip-line">{progress?.line ?? KIND_WORD[row.kind]}</span>
        {progress !== null && (
          <span className="vtc-bar" role="progressbar" aria-label={`${name} progress`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress.pct}>
            <span style={{ width: `${progress.pct}%` }} />
          </span>
        )}
      </button>
      <div id={id} className="vtc-pop" role="dialog" aria-label={name} hidden={!shown}>
        <p className="vtc-pop-head">
          <KindIcon kind={row.kind} />
          <b>{name}</b>
          <span className="vtc-pop-kind">{KIND_WORD[row.kind]}</span>
        </p>
        <p className="vtc-pop-meta">
          <span>{STATE_WORD[row.state]}</span>
          {progress !== null && <span>{progress.line}</span>}
          {href !== null && (
            <Button size="sm" onClick={() => navigate(href)}>
              Open
            </Button>
          )}
        </p>
      </div>
    </li>
  )
}

/** The crew strip, or nothing while the roster has no row. */
export function Roster({ rows, navigate }: { rows: readonly RosterRow[]; navigate: (to: string) => void }) {
  const [open, setOpen] = useState<string | null>(null)
  useEffect(() => {
    if (open === null) return
    const close = (e: MouseEvent) => {
      if (!(e.target instanceof Element) || e.target.closest('.vtc-chip') === null) setOpen(null)
    }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [open])
  if (rows.length === 0) return null
  const working = rows.filter((r) => r.state === 'running').length
  return (
    <section className="vtc-crew" aria-label="Agents">
      <ol className="vtc-chips">
        {rows.map((r) => (
          <Chip key={`${r.kind}:${r.id}`} row={r} open={open === `${r.kind}:${r.id}`} onOpen={setOpen} navigate={navigate} />
        ))}
      </ol>
      {working > 0 && <span className="vtc-count">{working} working</span>}
    </section>
  )
}
