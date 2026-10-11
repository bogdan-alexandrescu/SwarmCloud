/**
 * AUTOMATE › APPROVALS (docs/schedules.md §4.5, §6.1-§6.2, lane S7): the
 * tenant's inbox, at `/approvals`. One list holds every kind -- a firing
 * waiting to run, an issue run's plan (projected from the run, not copied),
 * a held plan, a green pull request waiting to merge, a proposal, a spec --
 * oldest first, as `GET /v1/approvals` serves it.
 *
 * READ BEFORE YOU DECIDE. A plan or a merge is approved on what it says, so
 * the screen is a split list and detail on a desktop (§6.2 variant B) and one
 * list with the chosen item opened in place on a phone (variant A). The
 * detail reads `GET /v1/approvals/{id}`, and Approve sends back the digest
 * that read showed: a plan edited or a branch pushed since is refused by the
 * server (409), never approved on a stale reading.
 *
 * THE SERVER HOLDS EVERY RULE. Who may approve (§4.6), a hold's stricter
 * approver (§4.4), the one-person tenant's typed confirmation (SD11) and the
 * race between two approvers are all decided in `routes/approvals.py`; the
 * screen offers both verbs to everyone and says which rule refused. Anyone
 * in the tenant may reject, with a reason (§4.5).
 *
 * A waiting item holds no capacity (invariant 1): nothing here is running.
 */
import { useEffect, useState, type ReactNode } from 'react'
import { approveApproval, loadApproval, loadApprovals, rejectApproval, type ApprovalDetail, type ApprovalItem } from './api'
import { usePhoneTables } from './capacityPoll'
import { Button } from './components'
import { errorHeading, type ApiError, type Result } from './fetch'
import { InApp, scheduleAddress, untilWords } from './Schedules'
import { Screen, timeAgo } from './Shell'
import { pluralise } from './types'
import { useNow } from './useNow'
import './styles/schedules.css'

/** The page's address, and one item's (`/approvals/<id>`, paths.ts). */
export const APPROVALS_ADDRESS = 'automate/approvals'
export function approvalAddress(id: string): string {
  return `${APPROVALS_ADDRESS}?${new URLSearchParams({ approval: id }).toString()}`
}

/** The item an Approvals address names (`approval=<id>`), or null for none. */
function approvalOf(view: string | null): string | null {
  if (view === null || view === '') return null
  const id = new URLSearchParams(view).get('approval')
  return id === null || id === '' ? null : id
}

/** §4.5's kinds, as the person reads them. */
export const APPROVAL_KIND: Readonly<Record<string, string>> = {
  run: 'Run',
  plan: 'Plan',
  hold: 'Held plan',
  merge: 'Merge',
  proposal: 'Proposal',
  spec: 'Spec',
}

/** What approving each kind does (§4.5 step 3). */
const APPROVE_DOES: Readonly<Record<string, string>> = {
  run: 'Approving creates the work this firing is waiting to make.',
  plan: 'Approving starts the run on this plan.',
  hold: 'Approving starts the run on this plan. The hold names who may approve it.',
  merge: 'Approving submits the merge of this pull request at the head it was checked at.',
  proposal: 'Approving files this proposal as an issue.',
  spec: 'A custom-prompt spec is approved by a platform admin.',
}

/** Who a stored `approvers` value admits, in words. */
function approversWords(a: unknown): string | null {
  if (a === undefined || a === null || a === '') return null
  if (a === 'members') return 'any member of this tenant'
  if (a === 'owner_only') return 'the platform owner only'
  if (a === 'second_member') return 'a second member: not the run’s creator, its last plan editor or its schedule’s last gate editor'
  if (Array.isArray(a)) return a.map(String).join(', ')
  return String(a)
}

/** Where an item's subject is read in full: its run's page, or its schedule's. */
function subjectLinks(item: ApprovalItem, go: (to: string) => void): ReactNode[] {
  const out: ReactNode[] = []
  const s = item.subject
  if (typeof s.run_id === 'string' && s.run_id !== '') {
    out.push(
      <InApp key="run" go={go} to={`work/runs?${new URLSearchParams({ run: s.run_id }).toString()}`}>
        Run {s.run_id}
      </InApp>,
    )
  }
  if (typeof s.schedule_id === 'string' && s.schedule_id !== '') {
    out.push(
      <InApp key="schedule" go={go} to={scheduleAddress(s.schedule_id)}>
        Its schedule
      </InApp>,
    )
  }
  return out
}

/** The refusal in the server's words, with the rule's code. */
function refusalWords(e: ApiError): string {
  switch (e.code) {
    case 'already_decided':
      return `Someone decided this first: ${e.message}`
    case 'merge_changed':
    case 'plan_changed':
    case 'approval_changed':
      return `It changed after you read it, so nothing was approved: ${e.message}`
    case 'hold_approver_required':
      return `The hold does not admit you: ${e.message}`
    case 'approver_not_allowed':
      return `You are not one of this item’s approvers: ${e.message}`
    default:
      return `Not decided: ${e.message}`
  }
}

/** The words a one-person tenant types to approve its own hold (SD11), from a 403's detail. */
function confirmationOf(e: ApiError): string | null {
  const d = e.detail as { confirm?: unknown } | undefined
  return e.code === 'hold_approver_required' && typeof d?.confirm === 'string' && d.confirm !== '' ? d.confirm : null
}

function Fact({ name, children }: { name: string; children: ReactNode }) {
  return (
    <div className="sc-fact">
      <dt>{name}</dt>
      <dd>{children}</dd>
    </div>
  )
}

/**
 * The head a merge approval is for: the digest's `head_sha` (§4.5 -- sent as
 * the object, or as `<head_sha>:<verdict>`), else the green head. It is what
 * Approve names, because a push after the request makes the approval stale.
 */
function mergeDigest(d: ApprovalDetail): { head: string | null; verdict: string | null } {
  const raw = d.approval.digest
  if (raw !== null && typeof raw === 'object') {
    const o = raw as { head_sha?: unknown; verdict?: unknown }
    return {
      head: typeof o.head_sha === 'string' && o.head_sha !== '' ? o.head_sha : (d.merge?.green_sha ?? null),
      verdict: typeof o.verdict === 'string' && o.verdict !== '' ? o.verdict : null,
    }
  }
  if (typeof raw === 'string' && raw.includes(':')) {
    const [head, verdict] = raw.split(':', 2)
    return { head: head || (d.merge?.green_sha ?? null), verdict: verdict || null }
  }
  return { head: d.merge?.green_sha ?? null, verdict: null }
}

const short = (sha: string) => sha.slice(0, 7)

/** The run's hold as the merge read serves it (`routes/approvals.py`: `merge.approval_hold`). */
type MergeHold = { code?: string; approvers?: string; matched?: string[]; reasons?: string[] } | null | undefined

const HOLD_WORDS: Readonly<Record<string, string>> = {
  NEEDS_SECOND_MEMBER: 'held for a second member',
  NEEDS_OWNER: 'held for the platform owner',
}

/** One row of the merge checklist: met, unmet, or not read -- never left to plain text. */
type Check = { key: string; mark: 'met' | 'unmet' | 'unread'; label: ReactNode; sub?: ReactNode }

const MARK = {
  met: { glyph: '✓', word: 'met' },
  unmet: { glyph: '✗', word: 'unmet' },
  unread: { glyph: '–', word: 'not read' },
} as const

/**
 * What a merge is decided on, as the mockup's inbox B draws it: one row per
 * check with its mark -- CI green at the head, the review's verdict, each
 * requirement, and the hard-stop gate on the pull request's changed files.
 * An unmet one is a ✗ row of its own. Approve stays enabled whatever they
 * say (owner decision 2026-10-11): the server holds every rule and refuses
 * what it must, in place.
 */
function mergeChecks(d: ApprovalDetail): Check[] {
  const m = d.merge ?? {}
  const { head, verdict } = mergeDigest(d)
  const out: Check[] = []
  const green = m.green_sha ?? null
  if (green === null) {
    out.push({ key: 'ci', mark: 'unmet', label: 'Required checks are not green yet', sub: 'No green CI reading at this head.' })
  } else if (head !== null && green !== head) {
    out.push({ key: 'ci', mark: 'unmet', label: 'Required checks green at an older head', sub: <>green at <span className="mono">{short(green)}</span>, the head is <span className="mono">{short(head)}</span></> })
  } else {
    out.push({ key: 'ci', mark: 'met', label: 'Required checks green at the head', sub: <>green at <span className="mono">{short(green)}</span></> })
  }
  if (verdict !== null) {
    out.push({ key: 'review', mark: verdict === 'MERGE' ? 'met' : 'unmet', label: <>Review verdict <b>{verdict}</b></> })
  }
  const unmet = m.requirements_unmet ?? []
  if (unmet.length > 0) {
    for (const [i, req] of unmet.entries()) out.push({ key: `requirement-${i}`, mark: 'unmet', label: <>Requirement unmet: {req}</> })
  } else if (m.requirements_met === true) {
    out.push({ key: 'requirements', mark: 'met', label: 'Every requirement met' })
  } else if (m.requirements_met === false) {
    out.push({ key: 'requirements', mark: 'unmet', label: 'Requirements not met' })
  } else {
    out.push({ key: 'requirements', mark: 'unread', label: 'Requirements not read yet' })
  }
  const hold = (m as { approval_hold?: MergeHold }).approval_hold
  const checked = m.files_checked_head ?? null
  if (hold !== null && hold !== undefined) {
    const matched = [...(hold.reasons ?? []), ...(hold.matched ?? [])]
    out.push({
      key: 'gate',
      mark: 'unmet',
      label: <>A hard stop matched: {HOLD_WORDS[hold.code ?? ''] ?? 'held'}</>,
      sub: matched.length > 0 ? matched.join(', ') : undefined,
    })
  } else if (checked === null) {
    out.push({ key: 'gate', mark: 'unread', label: 'Protected paths not checked yet', sub: 'They are checked on the pull request’s files before any merge.' })
  } else if (head !== null && checked !== head) {
    out.push({ key: 'gate', mark: 'unread', label: 'Protected paths checked at an older head', sub: <>checked at <span className="mono">{short(checked)}</span></> })
  } else {
    out.push({ key: 'gate', mark: 'met', label: 'No protected path changed', sub: <>checked at <span className="mono">{short(checked)}</span></> })
  }
  return out
}

function MergeChecks({ d }: { d: ApprovalDetail }) {
  return (
    <ul className="ap-checks" aria-label="What the merge is checked on">
      {mergeChecks(d).map((c) => (
        <li key={c.key} className={`ap-check is-${c.mark}`} data-check={c.key} data-mark={c.mark}>
          <span className="ap-mark" role="img" aria-label={MARK[c.mark].word}>
            {MARK[c.mark].glyph}
          </span>
          <span className="ap-check-l">{c.label}</span>
          {c.sub !== undefined && <span className="sc-sub">{c.sub}</span>}
        </li>
      ))}
    </ul>
  )
}

/** What the item is decided on: a plan's files, a merge's head and its checklist. */
function Evidence({ d }: { d: ApprovalDetail }) {
  const a = d.approval
  const plan = d.run?.plan ?? null
  const files = plan?.files ?? []
  return (
    <>
      {a.hold !== undefined && a.hold !== null && (
        <div className="ap-hold" role="note">
          <b>Held {a.hold.code ?? ''}</b>
          {(a.hold.matched ?? []).length > 0 && <span> · matched {(a.hold.matched ?? []).join(', ')}</span>}
          <span className="sc-sub">Approved only by {approversWords(a.hold.approvers) ?? 'the hold’s approvers'}.</span>
        </div>
      )}
      {plan !== null && (
        <>
          <h3 className="sc-h3">The plan</h3>
          {plan.summary ? <p className="ap-text">{plan.summary}</p> : <p className="sb-note">The plan carries no summary.</p>}
          {files.length > 0 ? (
            <ul className="ap-files" aria-label="Files the plan names">
              {files.map((f) => (
                <li key={f} className="mono">{f}</li>
              ))}
            </ul>
          ) : (
            <p className="sb-note">The plan names no file. Its files are advisory: a hold is checked again at merge.</p>
          )}
        </>
      )}
      {d.merge !== undefined && (
        <>
          <h3 className="sc-h3">The merge</h3>
          <dl className="sc-facts">
            <Fact name="Pull request">
              {d.merge.pull_request?.url ? (
                <a href={d.merge.pull_request.url} target="_blank" rel="noreferrer">
                  #{d.merge.pull_request.number ?? '?'}
                </a>
              ) : (
                <i className="ctl-em">&mdash; not served</i>
              )}
            </Fact>
          </dl>
          <MergeChecks d={d} />
        </>
      )}
    </>
  )
}

/** Approve and reject, with the server's refusal said in place. */
function Decide({ d, onDecided }: { d: ApprovalDetail; onDecided: (word: string) => void }) {
  const a = d.approval
  const [busy, setBusy] = useState<'approve' | 'reject' | null>(null)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const [confirm, setConfirm] = useState<{ expected: string; typed: string } | null>(null)
  const [rejecting, setRejecting] = useState(false)
  const [reason, setReason] = useState('')

  const approve = async () => {
    setBusy('approve')
    setRefused(null)
    const r = await approveApproval(a.approval_id, { digest: a.digest ?? null, ...(confirm === null ? {} : { confirm: confirm.typed.trim() }) })
    setBusy(null)
    if (r.status === 'ok' || r.status === 'empty') return onDecided('Approved')
    if (r.status !== 'error') return
    const expected = confirmationOf(r.error)
    if (expected !== null && confirm === null) setConfirm({ expected, typed: '' })
    setRefused(r.error)
  }
  const reject = async () => {
    setBusy('reject')
    setRefused(null)
    const r = await rejectApproval(a.approval_id, { reason: reason.trim() })
    setBusy(null)
    if (r.status === 'ok' || r.status === 'empty') return onDecided('Rejected')
    if (r.status === 'error') setRefused(r.error)
  }

  if (a.state !== 'pending') {
    return (
      <p className="sb-note" data-testid="ap-decided">
        {a.state}
        {a.decided_by ? ` by ${a.decided_by}` : ''}
        {a.reason ? `: ${a.reason}` : ''}. Nothing is left to decide.
      </p>
    )
  }
  const confirmOk = confirm === null || confirm.typed.trim() === confirm.expected
  // A merge names its head on the button: that head is what is approved.
  const head = a.kind === 'merge' ? mergeDigest(d).head : null
  const approveWord = a.kind !== 'merge' ? 'Approve' : head === null ? 'Approve merge' : `Approve merge at ${short(head)}`
  return (
    <div className="ap-decide">
      <p className="sb-note">{APPROVE_DOES[a.kind] ?? 'Approving lets this go ahead.'}</p>
      {confirm !== null && (
        <label className="c-field">
          <span className="c-lbl">
            You are the only person in this tenant, so no second member can approve this hold. Type <code>{confirm.expected}</code> to approve it yourself.
          </span>
          <input className="c-inp is-mono" value={confirm.typed} autoComplete="off" spellCheck={false} onChange={(e) => setConfirm({ ...confirm, typed: e.target.value })} />
        </label>
      )}
      {rejecting && (
        <label className="c-field">
          <span className="c-lbl">Why it is rejected</span>
          <textarea className="c-inp" rows={2} maxLength={1000} value={reason} onChange={(e) => setReason(e.target.value)} />
        </label>
      )}
      {refused !== null && !(confirm !== null && confirm.typed === '' && confirmationOf(refused) !== null) && (
        <p className="c-hint" role="alert" data-code={refused.code ?? ''}>
          {refusalWords(refused)}
        </p>
      )}
      <div className="c-acts">
        {rejecting ? (
          <>
            <Button kind="ghost" onClick={() => setRejecting(false)}>
              Keep it waiting
            </Button>
            <Button kind="danger" busy={busy === 'reject' ? 'Rejecting…' : false} disabledReason={reason.trim() === '' ? 'Say why it is rejected.' : undefined} onClick={() => void reject()}>
              Reject
            </Button>
          </>
        ) : (
          <>
            <Button onClick={() => { setRejecting(true); setRefused(null) }}>Reject…</Button>
            <Button kind="primary" busy={busy === 'approve' ? 'Approving…' : false} disabledReason={confirmOk ? undefined : 'Type the words shown exactly.'} onClick={() => void approve()}>
              {approveWord}
            </Button>
          </>
        )}
      </div>
    </div>
  )
}

/** One item read in full: what it is, what it is decided on, and the two verbs. */
function ItemDetail({ itemId, go, onDecided }: { itemId: string; go: (to: string) => void; onDecided: (word: string) => void }) {
  const [r, setR] = useState<Result<ApprovalDetail>>({ status: 'loading', since: Date.now() })
  const now = useNow()
  useEffect(() => {
    let live = true
    setR({ status: 'loading', since: Date.now() })
    void loadApproval(itemId).then((next) => {
      if (live) setR(next)
    })
    return () => {
      live = false
    }
  }, [itemId])
  if (r.status === 'loading') return <p className="sb-note" data-testid="ap-detail">Reading the item…</p>
  if (r.status === 'error') {
    return (
      <p className="c-hint" role="alert" data-testid="ap-detail">
        {r.error.kind === 'not_found' ? 'This item is no longer in the inbox: it was decided, expired or superseded.' : `Not read: ${errorHeading(r.error)}`}
      </p>
    )
  }
  if (r.status === 'empty') return <p className="sb-note" data-testid="ap-detail">The item answered nothing.</p>
  const d = r.data
  const a = d.approval
  const links = subjectLinks(a, go)
  return (
    <section className="ap-detail" data-testid="ap-detail" aria-label={`${APPROVAL_KIND[a.kind] ?? a.kind}: ${a.summary}`}>
      <h2 className="sc-h2">
        <span className="sc-kind">{APPROVAL_KIND[a.kind] ?? a.kind}</span> {a.summary}
      </h2>
      <dl className="sc-facts">
        <Fact name="Asked">{a.requested_at === null ? <i className="ctl-em">&mdash; not served</i> : <span title={a.requested_at}>{timeAgo(a.requested_at, now)}</span>}</Fact>
        <Fact name="Expires">
          {a.expires_at === null ? <i className="ctl-em">&mdash; never: a run made by hand waits as it always has</i> : <span title={a.expires_at}>{untilWords(a.expires_at, now)}</span>}
        </Fact>
        <Fact name="Who may approve">{approversWords(a.approvers) ?? <i className="ctl-em">&mdash; not served</i>}</Fact>
        {links.length > 0 && <Fact name="Read in full">{links.reduce<ReactNode[]>((acc, l, i) => (i === 0 ? [l] : [...acc, ' · ', l]), [])}</Fact>}
      </dl>
      <Evidence d={d} />
      <Decide d={d} onDecided={onDecided} />
    </section>
  )
}

function Inbox({ items, selected, go, onDecided }: { items: ApprovalItem[]; selected: string | null; go: (to: string) => void; onDecided: (word: string) => void }) {
  const now = useNow()
  const phone = usePhoneTables()
  const detail = selected === null ? null : <ItemDetail key={selected} itemId={selected} go={go} onDecided={onDecided} />
  return (
    <div className={phone ? 'ap-inbox is-phone' : 'ap-inbox is-split'}>
      <ul className="ap-list" aria-label="Waiting for approval, oldest first">
        {items.map((item) => {
          const on = item.approval_id === selected
          return (
            <li key={item.approval_id} className={on ? 'ap-item is-on' : 'ap-item'} data-approval={item.approval_id}>
              <InApp go={go} to={on ? APPROVALS_ADDRESS : approvalAddress(item.approval_id)} className="ap-pick">
                <span className="sc-kind">{APPROVAL_KIND[item.kind] ?? item.kind}</span>
                <span className="sc-cut" title={item.summary}>{item.summary}</span>
                {item.requested_at !== null && <span className="sc-sub" title={item.requested_at}>{timeAgo(item.requested_at, now)}</span>}
              </InApp>
              {/* Variant A on a phone: the chosen item opens in place. */}
              {phone && on && detail}
            </li>
          )
        })}
      </ul>
      {!phone && (
        <div className="ap-pane">
          {detail ?? <p className="sb-note">Choose an item to read it before deciding.</p>}
        </div>
      )}
    </div>
  )
}

export function ApprovalsScreen({ view, go }: { view: string | null; go: (to: string) => void }) {
  const selected = approvalOf(view)
  const [round, setRound] = useState(0)
  const [said, setSaid] = useState<string | null>(null)
  const decided = (word: string) => {
    setSaid(`${word}. The inbox was read again.`)
    go(APPROVALS_ADDRESS)
    setRound((n) => n + 1)
  }
  return (
    <>
      {said !== null && (
        <p className="sb-note" role="status">
          {said}
        </p>
      )}
      <Screen
        key={`approvals:${round}`}
        title="Approvals"
        load={() => loadApprovals()}
        summary={(d) => `${pluralise(d.approvals.length, 'item')} waiting, oldest first · waiting items hold no capacity`}
        empty={{
          heading: 'Nothing waiting',
          body: 'No firing, plan, merge, proposal or spec in this tenant is waiting for a person: the inbox was read and is empty.',
          say: 'The inbox was read and nothing is waiting',
        }}
      >
        {(d) => <Inbox items={d.approvals} selected={selected} go={go} onDecided={decided} />}
      </Screen>
    </>
  )
}
