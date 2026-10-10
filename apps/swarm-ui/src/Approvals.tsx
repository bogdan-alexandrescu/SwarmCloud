/**
 * AUTOMATE › APPROVALS (docs/schedules.md §4.5, §6.1-§6.2, lane S7).
 *
 * ONE INBOX. `approvals/` records -- a firing waiting to create work, a merge,
 * a proposal, a spec, a hold -- and every PLANNED issue run, projected into it
 * by the API, so a plan approved here, in Work › Runs or from `sc plan
 * approve` is one approval with one source of truth. Items cover issue runs
 * as well as schedules, which is why this is a page of its own (§6.1).
 *
 * Variant B on a desktop, split list and detail, because a plan or a merge is
 * read before it is approved; variant A on a phone, one column, the detail
 * under the list. Approve sends THE DIGEST SHOWN: a subject changed since it
 * was read is refused (`merge_changed`, `plan_changed`) and said in place.
 *
 * A PENDING APPROVAL HOLDS NOTHING (§4.7, invariant 1): no task, lease or slot
 * exists for it, so nothing here is capacity and nothing here is drawn as such.
 */
import { useEffect, useState } from 'react'
import { approveItem, loadApproval, loadApprovals, rejectItem, type ApprovalItem } from './api'
import { Banner, Button, Card, Segmented, TypedConfirm } from './components'
import type { ApiError, Result } from './fetch'
import { addressToPath } from './paths'
import { Absent } from './primitives'
import { RoutedLink, scheduleAddress } from './Schedules'
import { Screen } from './Shell'
import { pluralise, timeAgo } from './types'
import { useNow } from './useNow'
import './styles/automate.css'

export const APPROVALS = 'automate/approvals'

const KIND_WORDS: Readonly<Record<string, string>> = {
  run: 'Run',
  plan: 'Plan',
  merge: 'Merge',
  proposal: 'Proposal',
  spec: 'Spec',
  hold: 'Hold',
}

const FILTERS = ['all', 'run', 'plan', 'merge', 'hold', 'proposal', 'spec'] as const
type Filter = (typeof FILTERS)[number]

function itemOf(view: string | null): string | null {
  const id = new URLSearchParams(view ?? '').get('item')
  return id === null || id === '' ? null : id
}

function itemAddress(id: string): string {
  return `${APPROVALS}?${new URLSearchParams({ item: id }).toString()}`
}

function pendingOf(items: readonly ApprovalItem[]): ApprovalItem[] {
  return items.filter((a) => a.state === 'pending')
}

export function ApprovalsScreen({ view, go }: { view: string | null; go: (to: string) => void }) {
  const [reloads, setReloads] = useState(0)
  return (
    <Screen
      key={`approvals:${reloads}`}
      title="Approvals"
      load={async () => {
        const r = await loadApprovals()
        return r.status === 'empty' ? { status: 'ok' as const, fetchedAt: r.fetchedAt, data: { approvals: [], tenant_id: '' } } : r
      }}
      pollMs={60_000}
      summary={(d) => `${pluralise(pendingOf(d.approvals).length, 'item')} waiting`}
    >
      {(d) => <Inbox items={pendingOf(d.approvals)} open={itemOf(view)} go={go} reread={() => setReloads((n) => n + 1)} />}
    </Screen>
  )
}

function Inbox({ items, open, go, reread }: { items: ApprovalItem[]; open: string | null; go: (to: string) => void; reread: () => void }) {
  const now = useNow()
  const [which, setWhich] = useState<Filter>('all')
  if (items.length === 0) {
    return (
      <Absent kind="zero" heading="Nothing is waiting on you" say="No pending approval: a real zero, read from the API">
        A schedule whose gate asks first, a plan waiting for approval and a merge waiting for a person all arrive here.
      </Absent>
    )
  }
  const counts = Object.fromEntries(FILTERS.map((f) => [f, f === 'all' ? items.length : items.filter((a) => a.kind === f).length])) as Record<Filter, number>
  const shown = items.filter((a) => which === 'all' || a.kind === which)
  const current = items.find((a) => a.approval_id === open) ?? null
  return (
    <div className={`au-inbox${current === null ? '' : ' has-open'}`}>
      <div className="au-inbox-list">
        <Segmented
          label="Which items"
          value={which}
          options={FILTERS.filter((f) => f === 'all' || counts[f] > 0).map((f) => ({ key: f, label: f === 'all' ? 'all' : KIND_WORDS[f]!.toLowerCase(), count: counts[f] }))}
          onChange={setWhich}
        />
        <ul className="au-items">
          {shown.map((a) => (
            <li key={a.approval_id} className={`au-item${a.approval_id === open ? ' is-on' : ''}`} data-item={a.approval_id}>
              <a
                href={addressToPath(itemAddress(a.approval_id))}
                aria-current={a.approval_id === open ? 'true' : undefined}
                onClick={(e) => {
                  if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
                  e.preventDefault()
                  go(itemAddress(a.approval_id))
                }}
              >
                <span className={`au-kind is-${a.kind}`}>{KIND_WORDS[a.kind] ?? a.kind}</span>
                <span className="au-item-s">{a.summary || a.approval_id}</span>
                <span className="au-dim">{a.requested_at === null ? 'requested at an unknown time' : `requested ${timeAgo(a.requested_at, now)}`}</span>
              </a>
            </li>
          ))}
        </ul>
      </div>
      <div className="au-inbox-detail">
        {current === null ? (
          <p className="au-dim">Choose an item to read it before you decide.</p>
        ) : (
          <ItemDetail key={current.approval_id} item={current} go={go} reread={reread} />
        )}
      </div>
    </div>
  )
}

function refusal(e: ApiError): string {
  return e.code === null ? e.message : `${e.message} (${e.code})`
}

function ItemDetail({ item, go, reread }: { item: ApprovalItem; go: (to: string) => void; reread: () => void }) {
  const now = useNow()
  const [more, setMore] = useState<Result<Record<string, unknown>> | null>(null)
  const [busy, setBusy] = useState<'approve' | 'reject' | null>(null)
  const [reason, setReason] = useState('')
  const [error, setError] = useState<ApiError | null>(null)
  /** The text the API asks a one-person workspace to type, when it has asked. */
  const [confirming, setConfirming] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    void loadApproval(item.approval_id).then((r) => {
      if (live) setMore(r)
    })
    return () => {
      live = false
    }
  }, [item.approval_id])

  async function approve(confirm?: string) {
    setBusy('approve')
    setError(null)
    const r = await approveItem(item.approval_id, item.digest, confirm)
    setBusy(null)
    if (r.status === 'error') {
      // A one-person tenant approves its own hold after typing the matched
      // paths (§4.6, SD11): the 403 names the text, and only a first ask
      // opens the confirm -- a typed text refused again is said as refused.
      const asked = confirmAsked(r.error)
      if (asked !== null && confirm === undefined) {
        setConfirming(asked)
        return
      }
      setConfirming(null)
      setError(r.error)
      return
    }
    setConfirming(null)
    reread()
  }

  async function reject() {
    if (reason.trim() === '') return
    setBusy('reject')
    setError(null)
    const r = await rejectItem(item.approval_id, reason.trim())
    setBusy(null)
    if (r.status === 'error') {
      setError(r.error)
      return
    }
    reread()
  }

  const sub = item.subject ?? {}
  const merge = more !== null && (more.status === 'ok' || more.status === 'stale') ? more.data : null
  return (
    <Card title={`${KIND_WORDS[item.kind] ?? item.kind}: ${item.summary || item.approval_id}`} className="au-decide">
      <dl className="au-dl">
        {sub.schedule_id && (
          <>
            <dt>Schedule</dt>
            <dd>
              <RoutedLink to={scheduleAddress(sub.schedule_id)} go={go}>
                {sub.schedule_id}
              </RoutedLink>
            </dd>
          </>
        )}
        {sub.run_id && (
          <>
            <dt>Issue run</dt>
            <dd>
              <a className="ctl-link mono" href={`/runs/${encodeURIComponent(sub.run_id)}`}>
                {sub.issue ?? sub.run_id}
              </a>
            </dd>
          </>
        )}
        {typeof sub.pr === 'number' && (
          <>
            <dt>Pull request</dt>
            <dd>#{sub.pr}</dd>
          </>
        )}
        <dt>Requested</dt>
        <dd>{item.requested_at === null ? '—' : timeAgo(item.requested_at, now)}</dd>
        <dt>Expires</dt>
        <dd>{item.expires_at === null ? 'never: it is not a schedule’s, so it waits as a run always has' : new Date(item.expires_at).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' })}</dd>
        <dt>Who may approve</dt>
        <dd>{approverWords(item.approvers)}</dd>
        {item.hold && (
          <>
            <dt>Hold</dt>
            <dd>
              {item.hold.code ?? 'held'}
              {item.hold.approvers ? ` · ${item.hold.approvers === 'owner_only' ? 'the platform owner approves' : 'a second member approves'}` : ''}
            </dd>
          </>
        )}
      </dl>
      {item.kind === 'merge' && merge !== null && <MergeFacts data={merge} />}
      {error !== null && (
        <Banner tone="bad" title="Not decided">
          {refusal(error)}
        </Banner>
      )}
      <div className="au-acts">
        <Button kind="primary" size="sm" busy={busy === 'approve'} onClick={() => void approve()}>
          Approve
        </Button>
      </div>
      <label className="c-field">
        <span className="c-lbl">Reason, to reject</span>
        <textarea className="c-inp" rows={2} value={reason} maxLength={1000} onChange={(e) => setReason(e.target.value)} />
      </label>
      <div className="au-acts">
        <Button kind="danger" size="sm" busy={busy === 'reject'} disabledReason={reason.trim() === '' ? 'A rejection says why.' : undefined} onClick={() => void reject()}>
          Reject
        </Button>
      </div>
      {confirming !== null && (
        <TypedConfirm title="Approve your own hold" name={confirming} verb="Approve" keep="Leave it" busy={busy === 'approve'} onConfirm={() => void approve(confirming)} onClose={() => setConfirming(null)}>
          You are the only member here, so no second person can approve it. Typing the matched paths says you read them; the audit records that you did.
        </TypedConfirm>
      )}
    </Card>
  )
}

/** `hold_approver_required` with the text to type in its detail (approvals.py `_hold_refusal`), or null. */
export function confirmAsked(e: ApiError): string | null {
  if (e.code !== 'hold_approver_required' && e.code !== 'confirmation_required') return null
  const d = e.detail
  const text = typeof d === 'object' && d !== null ? (d as Record<string, unknown>).confirm : undefined
  return typeof text === 'string' && text !== '' ? text : null
}

function approverWords(a: unknown): string {
  if (Array.isArray(a)) return a.join(', ')
  if (a === 'owner_only') return 'the platform owner only'
  if (a === 'second_member') return 'a member other than its author'
  return 'any member of this tenant'
}

/** What a merge needs to be decided (§7.1): the green sha, the verdict, the protected files. */
function MergeFacts({ data }: { data: Record<string, unknown> }) {
  const green = typeof data.green_sha === 'string' ? data.green_sha : null
  const verdict = typeof data.verdict === 'string' ? data.verdict : null
  const files = Array.isArray(data.protected_files) ? (data.protected_files as unknown[]).filter((f): f is string => typeof f === 'string') : null
  return (
    <dl className="au-dl">
      <dt>Green at</dt>
      <dd className="mono">{green === null ? '—' : green.slice(0, 12)}</dd>
      <dt>Review</dt>
      <dd>{verdict ?? '—'}</dd>
      {files !== null && (
        <>
          <dt>Protected paths touched</dt>
          <dd>{files.length === 0 ? 'none' : files.join(', ')}</dd>
        </>
      )}
    </dl>
  )
}

/**
 * OVERVIEW'S "WAITING ON YOU" CARD (§6.1): only when something is waiting.
 * Nothing pending, a read in flight or a failed read all draw nothing here --
 * the Approvals page says which, and Overview's own checks say what is broken.
 */
export function WaitingOnYouCard() {
  const [items, setItems] = useState<ApprovalItem[] | null>(null)
  useEffect(() => {
    let live = true
    void loadApprovals().then((r) => {
      if (live && (r.status === 'ok' || r.status === 'stale')) setItems(pendingOf(r.data.approvals))
    })
    return () => {
      live = false
    }
  }, [])
  if (items === null || items.length === 0) return null
  return (
    <Card
      className="au-waiting"
      title="Waiting on you"
      action={
        <a className="ctl-link" href={addressToPath(APPROVALS)}>
          Open approvals
        </a>
      }
    >
      <p>{pluralise(items.length, 'item')} waiting for a decision. Nothing waiting holds capacity.</p>
      <ul className="au-items">
        {items.slice(0, 3).map((a) => (
          <li key={a.approval_id} className="au-item">
            <a href={addressToPath(itemAddress(a.approval_id))}>
              <span className={`au-kind is-${a.kind}`}>{KIND_WORDS[a.kind] ?? a.kind}</span>
              <span className="au-item-s">{a.summary || a.approval_id}</span>
            </a>
          </li>
        ))}
      </ul>
    </Card>
  )
}
