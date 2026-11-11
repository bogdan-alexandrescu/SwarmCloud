/**
 * VOICE IT, THE BUDGET METER beside the Talk pane (docs/design/voice-it.md
 * §3.8; section 10, phase three's ledger row).
 *
 * Two bars, the session against $100 and the day against $300, each the
 * total of what is SPENT (talk, planner, finished work at its measured cost,
 * follow-ups) and what is RESERVED (running work at its step budgets times
 * 1.2). The figures are swarm-api's ledger, which computes them on every read
 * (swarm_api/voice/ledger.py); this component sums nothing of its own.
 *
 * It reads once when a session is shown and again every LEDGER_REFRESH_MS
 * while the session is live, because talk is billed by the second. What the
 * figures leave out (pod compute, CI minutes, deploys) is said under them.
 */

import { useEffect, useState } from 'react'

import { UsageBar } from '../components'
import { read, route } from '../fetch'

export const VOICE_LEDGER = '/v1/voice/sessions/{id}/ledger'

/** How often a live session's meter is read again. */
export const LEDGER_REFRESH_MS = 30_000

/** The ledger's words for its lines, in the order it serves them. */
export const LINE_WORDS: Record<string, string> = { // MOCK-HOOK (exported for CostPill.tsx)
  talk: 'Talk',
  planner: 'Planner',
  speech_machine: 'Speech machine',
  work_running: 'Work running',
  work_finished: 'Work finished',
  followups: 'Follow-ups',
}

export interface LedgerLine {
  line: string
  spent_usd: number
  reserved_usd: number
}

export interface LedgerCap {
  cap_usd: number
  spent_usd: number
  reserved_usd: number
  total_usd: number
  remaining_usd: number
  lines: LedgerLine[]
}

export interface Ledger {
  session_id: string
  date: string
  time_zone: string
  session: LedgerCap
  day: LedgerCap
  not_counted: string[]
}

function isCap(v: unknown): v is LedgerCap {
  const c = v as LedgerCap | null
  return (
    typeof c === 'object' &&
    c !== null &&
    typeof c.cap_usd === 'number' &&
    typeof c.spent_usd === 'number' &&
    typeof c.reserved_usd === 'number' &&
    typeof c.total_usd === 'number' &&
    Array.isArray(c.lines)
  )
}

/** The served ledger, or null when it is not one. */
export function asLedger(v: unknown): Ledger | null {
  const l = v as Ledger | null
  if (typeof l !== 'object' || l === null || !isCap(l.session) || !isCap(l.day)) return null
  return { ...l, not_counted: Array.isArray(l.not_counted) ? l.not_counted : [] }
}

export function usd(n: number): string {
  return `$${n.toFixed(2)}`
}

function CapBar({ label, cap }: { label: string; cap: LedgerCap }) {
  const lines = cap.lines.filter((l) => l.spent_usd > 0 || l.reserved_usd > 0)
  return (
    <div className="vt-meter-cap" aria-label={label} role="group">
      <UsageBar
        label={label}
        used={Number(cap.total_usd.toFixed(2))}
        ceiling={cap.cap_usd}
        unit="USD"
        note={`${usd(cap.spent_usd)} spent, ${usd(cap.reserved_usd)} reserved`}
      />
      {lines.length > 0 && (
        <ul className="vt-meter-lines">
          {lines.map((l) => (
            <li key={l.line}>
              {LINE_WORDS[l.line] ?? l.line}: {usd(l.spent_usd)} spent
              {l.reserved_usd > 0 && `, ${usd(l.reserved_usd)} reserved`}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export function BudgetMeter({ sessionId, live }: { sessionId: string; live: boolean }) {
  const [ledger, setLedger] = useState<Ledger | null>(null)
  const [problem, setProblem] = useState<string | null>(null)

  useEffect(() => {
    let current = true
    const load = () => {
      void read<unknown>(route(VOICE_LEDGER, { id: sessionId }), () => false).then((r) => {
        if (!current) return
        if (r.status === 'ok' || r.status === 'stale') {
          const l = asLedger(r.data)
          if (l === null) setProblem("swarm-api's answer was not a ledger.")
          else {
            setLedger(l)
            setProblem(null)
          }
        } else if (r.status === 'error') {
          setProblem(r.error.message)
        }
      })
    }
    load()
    const timer = live ? window.setInterval(load, LEDGER_REFRESH_MS) : null
    return () => {
      current = false
      if (timer !== null) window.clearInterval(timer)
    }
  }, [sessionId, live])

  return (
    <section className="vt-meter" aria-label="Budget">
      <p className="vt-cards-h">Budget</p>
      {ledger === null && problem === null && <p className="vt-meter-note">Reading the ledger…</p>}
      {problem !== null && (
        <p className="vt-meter-note" role="alert">
          The budget could not be read: {problem}
        </p>
      )}
      {ledger !== null && (
        <>
          <CapBar label="This session" cap={ledger.session} />
          <CapBar label={`Today (${ledger.date}, ${ledger.time_zone})`} cap={ledger.day} />
          {ledger.not_counted.length > 0 && (
            <p className="vt-meter-note">Not counted: {ledger.not_counted.join(', ')}.</p>
          )}
        </>
      )}
    </section>
  )
}
