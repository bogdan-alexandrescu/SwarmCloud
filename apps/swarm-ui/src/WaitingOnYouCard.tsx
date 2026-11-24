// Overview's "Waiting on you" (§6.1), apart from Schedules.tsx so the
// overview draws it without loading the schedules page.
import { useEffect, useState } from 'react'
import { loadApprovals, type ApprovalItem } from './api'
import { InApp } from './InApp'
import { scheduleAddress } from './schedulePaths'
import { timeAgo } from './Shell'
import { useNow } from './useNow'
import './styles/schedules.css'

/** How often the card re-reads the inbox while Overview is open. */
const WAITING_POLL_MS = 60_000

const KIND_WORD: Readonly<Record<string, string>> = {
  plan: 'Plan',
  hold: 'Held plan',
  merge: 'Merge',
  run: 'Run',
  proposal: 'Proposal',
  spec: 'Spec',
}

/** Where an item is decided from today: its run's page, or its schedule's. */
function subjectAddress(item: ApprovalItem): string | null {
  const s = item.subject
  if (typeof s.run_id === 'string' && s.run_id !== '') return `work/runs?${new URLSearchParams({ run: s.run_id }).toString()}`
  if (typeof s.schedule_id === 'string' && s.schedule_id !== '') return scheduleAddress(s.schedule_id)
  return null
}

/**
 * WAITING ON YOU: drawn ONLY when the inbox holds something (§6.1). Nothing
 * while reading, nothing for a measured zero, and nothing for a failed read
 * -- the spine's Automate badge carries that failure as a dash with its
 * reason, so a card that would say only "could not read" is not repeated
 * here. A waiting item holds no capacity (invariant 1), and the card says so.
 */
export function WaitingOnYouCard({ go }: { go: (to: string) => void }) {
  const [items, setItems] = useState<ApprovalItem[] | null>(null)
  useEffect(() => {
    let live = true
    const read = () =>
      void loadApprovals().then((r) => {
        if (!live) return
        if (r.status === 'ok' || r.status === 'stale') setItems(r.data.approvals)
        else if (r.status === 'empty') setItems([])
        // A failure keeps what the last read showed: the badge says it failed.
      })
    read()
    const timer = setInterval(read, WAITING_POLL_MS)
    return () => {
      live = false
      clearInterval(timer)
    }
  }, [])
  const now = useNow()
  if (items === null || items.length === 0) return null
  const shown = items.slice(0, 5)
  return (
    <section className="ctl-card ov-card sc-waiting" aria-labelledby="sc-waiting-h">
      <div className="sc-waiting-h">
        <h2 id="sc-waiting-h">Waiting on you</h2>
        <span className="sk-badge sc-count" aria-label={`${items.length} waiting`}>{items.length}</span>
      </div>
      <ul className="sc-waiting-list">
        {shown.map((item) => {
          const to = subjectAddress(item)
          const word = KIND_WORD[item.kind] ?? item.kind
          return (
            <li key={item.approval_id} data-approval={item.approval_id}>
              <span className="sc-kind">{word}</span>
              {to === null ? (
                <span className="sc-cut" title={item.summary}>{item.summary}</span>
              ) : (
                <InApp go={go} to={to} className="sc-cut">{item.summary}</InApp>
              )}
              {item.requested_at !== null && <span className="sc-sub" title={item.requested_at}>{timeAgo(item.requested_at, now)}</span>}
            </li>
          )
        })}
      </ul>
      <p className="sb-note">
        {items.length > shown.length ? `The oldest ${shown.length} of ${items.length}. ` : ''}Waiting items hold no capacity.
      </p>
    </section>
  )
}
