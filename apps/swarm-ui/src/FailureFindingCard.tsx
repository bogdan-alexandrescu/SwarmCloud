/**
 * "THE FAILURE REVIEW FOUND THE CAUSE IN YOUR REPOSITORY": a failed agent's
 * finding (docs/design/failure-review.md §1; lane FR-4 FINDINGS, part of
 * #1933).
 *
 * The `failure-review` schedule root-causes every failed agent. A cause in a
 * tenant's OWN repository -- its test that fails, a build its code breaks --
 * is not this platform's to fix, so the review writes a finding into that
 * tenant instead of filing it anywhere. The bell's "needs you" item
 * (`notifications.failure_findings`, `failure_review:<finding id>`) lands
 * here, the failed agent's Details at `#failure-findings`: the cause
 * sentence, and what the platform recorded about the failure.
 *
 * READ FROM `GET /v1/failure-findings?task_id=<id>`, the caller's tenant
 * only. The API serves an allow-list (`routes/failurefindings.tenant_view`):
 * no fingerprint, firing, schedule or proposed fix of the platform tenant's
 * review. The card draws only the fields it names below, so a field the API
 * someday sends by mistake is still not drawn.
 *
 * OPENING IT READS THE NOTIFICATION, as the questions card does: each drawn
 * finding's bell item is marked read once, only while the feed lists it unread.
 *
 * NOTHING IS DRAWN FOR AN AGENT WITH NO FINDING: an empty answer is no card,
 * and a read that failed says so in one line rather than hiding a finding.
 */
import { useEffect, useRef, useState, useSyncExternalStore } from 'react'

import { USE_FIXTURES } from './api'
import { Card } from './components'
import { errorHeading, read, route, type Result } from './fetch'
import { feedData, markRead, notificationsSnapshot, subscribeNotifications } from './notificationFeed'
import { timeAgo } from './types'

/** What the platform recorded about the failure: the tenant's own, nothing else. */
export interface FindingEvidence {
  state?: string | null
  profile?: string | null
  end_cause?: string | null
  last_error?: string | null
}

/** One finding as the API serves it to its tenant. */
export interface FailureFinding {
  finding_id: string
  kind: string | null
  task_id: string | null
  state: string | null
  cause: string | null
  evidence: FindingEvidence
  created_at: string | null
}

const LIST = '/v1/failure-findings'

const str = (v: unknown): string | null => (typeof v === 'string' && v !== '' ? v : null)

/** The named fields and nothing else, whatever the row carried. */
function asFinding(raw: unknown): FailureFinding {
  const r = (raw ?? {}) as Record<string, unknown>
  const e = (typeof r.evidence === 'object' && r.evidence !== null ? r.evidence : {}) as Record<string, unknown>
  return {
    finding_id: str(r.finding_id) ?? '',
    kind: str(r.kind),
    task_id: str(r.task_id),
    state: str(r.state),
    cause: str(r.cause),
    evidence: { state: str(e.state), profile: str(e.profile), end_cause: str(e.end_cause), last_error: str(e.last_error) },
    created_at: str(r.created_at),
  }
}

/** This agent's findings. */
export async function loadFailureFindings(taskId: string): Promise<Result<FailureFinding[]>> {
  // The development fixture runs no failure review, so it has no finding.
  if (USE_FIXTURES) return { status: 'empty', fetchedAt: Date.now() }
  const r = await read<{ findings: unknown[] }>(
    route(LIST, {}, new URLSearchParams({ task_id: taskId })),
    (d) => !Array.isArray(d?.findings) || d.findings.length === 0,
  )
  if (r.status === 'ok' || r.status === 'stale') {
    return { ...r, data: (Array.isArray(r.data.findings) ? r.data.findings : []).map(asFinding).filter((f) => f.finding_id !== '') }
  }
  return r
}

/** The bell's item for one finding (`notifications.failure_findings`). */
export function findingNotificationId(findingId: string): string {
  return `failure_review:${findingId}`
}

/** Mark the drawn findings' bell items read once, while the feed lists them unread. */
function useReadsNotifications(ids: readonly string[]): void {
  const feed = useSyncExternalStore(subscribeNotifications, notificationsSnapshot, notificationsSnapshot)
  const done = useRef(new Set<string>())
  const unread = (feedData(feed)?.items ?? [])
    .filter((i) => !i.read && ids.includes(i.id) && !done.current.has(i.id))
    .map((i) => i.id)
  const key = unread.join(',')
  useEffect(() => {
    if (key === '') return
    const ids = key.split(',')
    ids.forEach((id) => done.current.add(id))
    void markRead(ids)
  }, [key])
}

function Evidence({ e }: { e: FindingEvidence }) {
  const rows: [string, string | null | undefined][] = [
    ['State', e.state],
    ['Runner profile', e.profile],
    ['End cause', e.end_cause],
    ['Last error', e.last_error],
  ]
  const shown = rows.filter(([, v]) => v)
  if (shown.length === 0) return null
  return (
    <dl className="ff-evidence">
      {shown.map(([k, v]) => (
        <div key={k}>
          <dt>{k}</dt>
          <dd>{k === 'Last error' ? <code>{v}</code> : v}</dd>
        </div>
      ))}
    </dl>
  )
}

/** The failed agent's findings, or nothing when it has none. */
export function FailureFindingCard({ taskId, now = Date.now() }: { taskId: string; now?: number }) {
  const [held, setHeld] = useState<{ task: string; r: Result<FailureFinding[]> }>({
    task: taskId,
    r: { status: 'loading', since: now },
  })
  useEffect(() => {
    let live = true
    void loadFailureFindings(taskId).then((r) => {
      if (live) setHeld({ task: taskId, r })
    })
    return () => {
      live = false
    }
  }, [taskId])
  const r = held.task === taskId ? held.r : ({ status: 'loading', since: now } as const)
  const findings = r.status === 'ok' || r.status === 'stale' ? r.data : []
  useReadsNotifications(findings.map((f) => findingNotificationId(f.finding_id)))

  if (r.status === 'loading' || r.status === 'empty') return null
  if (r.status === 'error') {
    return (
      <p className="dt-note ff-unread" id="failure-findings" role="status">
        The failure review&rsquo;s findings for this agent could not be read: {errorHeading(r.error)}.
      </p>
    )
  }
  if (findings.length === 0) return null
  return (
    <Card title="The failure review found the cause in your repository" id="failure-findings" className="ff-card">
      <p className="dt-note">
        SwarmCloud&rsquo;s failure review traced this agent&rsquo;s failure to your repository&rsquo;s own code, which the
        platform does not change. It is yours to fix; nothing was filed anywhere.
      </p>
      <ul className="ff-list">
        {findings.map((f) => (
          <li key={f.finding_id} data-finding={f.finding_id} data-state={f.state ?? ''}>
            <p className="ff-cause">{f.cause ?? 'The review recorded no cause sentence.'}</p>
            <Evidence e={f.evidence} />
            {f.created_at && <p className="ff-when">Found {timeAgo(new Date(f.created_at).getTime(), now)}</p>}
          </li>
        ))}
      </ul>
    </Card>
  )
}
