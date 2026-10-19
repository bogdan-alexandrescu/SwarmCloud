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
import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { approveApproval, loadApproval, loadApprovals, rejectApproval, type ApprovalDetail, type ApprovalItem } from './api'
import { usePhoneTables } from './capacityPoll'
import { Button } from './components'
import { errorHeading, type ApiError, type Result } from './fetch'
import { Absent } from './primitives'
import { PersonName } from './PersonName'
import { InApp, scheduleAddress, untilWords } from './Schedules'
import { Screen, timeAgo } from './Shell'
import { pluralise } from './types'
import { useNow } from './useNow'
import './styles/schedules.css'

/**
 * The page's address, and one item's (`/approvals/<id>`, paths.ts). The
 * inbox's kind filter rides on both as `kind=` (`/approvals?kind=merge`), so
 * opening an item keeps the list it was opened from (AUT-15).
 */
export const APPROVALS_ADDRESS = 'automate/approvals'
export function approvalAddress(id: string, kind: string | null = null): string {
  const q = new URLSearchParams({ approval: id })
  if (kind !== null) q.set('kind', kind)
  return `${APPROVALS_ADDRESS}?${q.toString()}`
}
export function approvalsAddress(kind: string | null = null): string {
  return kind === null ? APPROVALS_ADDRESS : `${APPROVALS_ADDRESS}?${new URLSearchParams({ kind }).toString()}`
}

/** The item an Approvals address names (`approval=<id>`), or null for none. */
function approvalOf(view: string | null): string | null {
  if (view === null || view === '') return null
  const id = new URLSearchParams(view).get('approval')
  return id === null || id === '' ? null : id
}

/** The kind an address filters to, or null for every kind; a kind the route does not know is none. */
function kindOf(view: string | null): string | null {
  if (view === null || view === '') return null
  const kind = new URLSearchParams(view).get('kind')
  return kind !== null && Object.prototype.hasOwnProperty.call(APPROVAL_KIND, kind) ? kind : null
}

/** §4.5's kinds, as the person reads them. */
export const APPROVAL_KIND: Readonly<Record<string, string>> = {
  run: 'Run',
  plan: 'Plan',
  hold: 'Held plan',
  merge: 'Merge',
  proposal: 'Proposal',
  spec: 'Spec',
  // A workflow step waiting for a member's answer (human-steps.md §4.5),
  // projected from its task. The inbox lists it and never takes the answer.
  input: 'Answer',
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

/**
 * What became of an item that is no longer waiting (AUT-6): a link from a
 * notification to an item someone else decided, or one that expired, says so.
 */
function decidedWords(a: ApprovalDetail['approval']): ReactNode {
  // Who decided, through `PersonName` (user-profile.md §4.4): their name, when
  // they set one, always beside the email that decided.
  const by = a.decided_by ? (
    <>
      {' by '}
      <PersonName email={a.decided_by} name={a.decided_by_name} />
    </>
  ) : null
  const at = a.decided_at ? ` ${new Date(a.decided_at).toLocaleString()}` : ''
  switch (a.state) {
    case 'approved':
      return <>Approved{by}{at}.</>
    case 'rejected':
      return <>Rejected{by}{at}{a.reason ? `: ${a.reason}` : ''}.</>
    case 'expired':
      return `Expired${a.expires_at ? ` ${new Date(a.expires_at).toLocaleString()}` : ''}: nobody decided it in time, so it did not go ahead.`
    case 'superseded':
      return 'Superseded: a newer request replaced it.'
    default:
      return <>{a.state}{by}{a.reason ? `: ${a.reason}` : ''}.</>
  }
}

/** Approve and reject, with the server's refusal said in place. */
function Decide({ d, onDecided }: { d: ApprovalDetail; onDecided: (word: string) => void }) {
  const a = d.approval
  const [busy, setBusy] = useState<'approve' | 'reject' | null>(null)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const [confirm, setConfirm] = useState<{ expected: string; typed: string } | null>(null)
  const [rejecting, setRejecting] = useState(false)
  const [reason, setReason] = useState('')
  // WHERE FOCUS GOES (visual QA V249). Reject… opened the form with focus on
  // "Keep it waiting" instead of the reason it requires; Escape did nothing;
  // and a `hold_approver_required` refusal disabled Approve under the cursor,
  // dropping focus to <body>. Each now lands on the control that comes next.
  const decide = useRef<HTMLDivElement>(null)
  const reasonField = useRef<HTMLTextAreaElement>(null)
  const confirmField = useRef<HTMLInputElement>(null)
  const back = useRef(false)
  const asking = confirm !== null
  useEffect(() => {
    if (rejecting) reasonField.current?.focus()
    else if (back.current) {
      back.current = false
      decide.current?.querySelector<HTMLButtonElement>('[data-ap-reject-open]')?.focus()
    }
  }, [rejecting])
  useEffect(() => {
    if (asking) confirmField.current?.focus()
  }, [asking])
  const stopRejecting = () => {
    back.current = true
    setRejecting(false)
  }
  const onKey = (ev: KeyboardEvent) => {
    if (ev.key === 'Escape' && rejecting) {
      ev.preventDefault()
      ev.stopPropagation()
      stopRejecting()
    }
  }

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
      <p className="sb-note" data-testid="ap-decided" data-state={a.state}>
        {decidedWords(a)} Nothing is left to decide.
      </p>
    )
  }
  const confirmOk = confirm === null || confirm.typed.trim() === confirm.expected
  // A merge names its head on the button: that head is what is approved.
  const head = a.kind === 'merge' ? mergeDigest(d).head : null
  const approveWord = a.kind !== 'merge' ? 'Approve' : head === null ? 'Approve merge' : `Approve merge at ${short(head)}`
  return (
    <div className="ap-decide" ref={decide} onKeyDown={onKey}>
      <p className="sb-note">{APPROVE_DOES[a.kind] ?? 'Approving lets this go ahead.'}</p>
      {confirm !== null && (
        <label className="c-field">
          <span className="c-lbl">
            You are the only person in this tenant, so no second member can approve this hold. Type <code>{confirm.expected}</code> to approve it yourself.
          </span>
          <input ref={confirmField} className="c-inp is-mono" value={confirm.typed} autoComplete="off" spellCheck={false} onChange={(e) => setConfirm({ ...confirm, typed: e.target.value })} />
        </label>
      )}
      {rejecting && (
        <label className="c-field">
          <span className="c-lbl">Why it is rejected</span>
          <textarea ref={reasonField} className="c-inp" rows={2} maxLength={1000} value={reason} onChange={(e) => setReason(e.target.value)} />
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
            <Button kind="ghost" onClick={stopRejecting}>
              Keep it waiting
            </Button>
            <Button kind="danger" busy={busy === 'reject' ? 'Rejecting…' : false} disabledReason={reason.trim() === '' ? 'Say why it is rejected.' : undefined} onClick={() => void reject()}>
              Reject
            </Button>
          </>
        ) : (
          <>
            <Button data-ap-reject-open onClick={() => { setRejecting(true); setRefused(null) }}>Reject…</Button>
            <Button kind="primary" busy={busy === 'approve' ? 'Approving…' : false} disabledReason={confirmOk ? undefined : 'Type the words shown exactly.'} onClick={() => void approve()}>
              {approveWord}
            </Button>
          </>
        )}
      </div>
    </div>
  )
}

/** The inbox id of a projected await-input step (`approvals.INPUT_PREFIX`). */
const INPUT_PREFIX = 'input:'

/** The workflow page that opens the step's card, where the answer is given. */
function stepAddress(workflowId: string): string {
  return `work/workflows?${new URLSearchParams({ wf: workflowId }).toString()}`
}

/**
 * A WORKFLOW STEP WAITING FOR AN ANSWER (human-steps.md §4.5). It is drawn
 * from the inbox row itself: the item is projected from the step's task, so
 * `GET /v1/approvals/{id}` has no record of it, and the inbox has no decide
 * path for it. Answer opens the step's card on its workflow, so the step has
 * one writer, the step's own route.
 */
function InputItemDetail({ item, go }: { item: ApprovalItem; go: (to: string) => void }) {
  const now = useNow()
  const s = item.subject
  const workflowId = typeof s.workflow_id === 'string' && s.workflow_id !== '' ? s.workflow_id : null
  const form = typeof s.form === 'string' ? s.form : null
  return (
    <section className="ap-detail" data-testid="ap-detail" data-kind="input" aria-label={`${APPROVAL_KIND.input}: ${item.summary}`}>
      <h2 className="sc-h2">
        <span className="sc-kind">{APPROVAL_KIND.input}</span> {item.summary}
      </h2>
      <dl className="sc-facts">
        <Fact name="Asked">{item.requested_at === null ? <i className="ctl-em">&mdash; not served</i> : <span title={item.requested_at}>{timeAgo(item.requested_at, now)}</span>}</Fact>
        <Fact name="Expires">{item.expires_at === null ? <i className="ctl-em">&mdash; not served</i> : <span title={item.expires_at}>{untilWords(item.expires_at, now)}</span>}</Fact>
        <Fact name="Who may answer">any member of this tenant, checked when the answer arrives</Fact>
        {form !== null && <Fact name="Form">{form}</Fact>}
      </dl>
      <p className="sb-note">The answer is given on the step’s card, never here: the inbox only lists the steps waiting for one.</p>
      {workflowId === null ? (
        <p className="c-hint">The item names no workflow, so its step cannot be opened from here.</p>
      ) : (
        <div className="c-acts">
          <InApp go={go} to={stepAddress(workflowId)} className="c-btn is-primary">
            Answer ›
          </InApp>
        </div>
      )}
    </section>
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
        {r.error.kind === 'not_found'
          ? `No approval ${itemId} in this tenant: the link names an item that does not exist here, or a plan whose run has since moved on.`
          : `Not read: ${errorHeading(r.error)}`}
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
        <Fact name="State">
          <span data-testid="ap-state">{a.state}</span>
        </Fact>
        {a.state === 'pending' && (
          <Fact name="Expires">
            {a.expires_at === null ? <i className="ctl-em">&mdash; never: a run made by hand waits as it always has</i> : <span title={a.expires_at}>{untilWords(a.expires_at, now)}</span>}
          </Fact>
        )}
        <Fact name="Who may approve">{approversWords(a.approvers) ?? <i className="ctl-em">&mdash; not served</i>}</Fact>
        {links.length > 0 && <Fact name="Read in full">{links.reduce<ReactNode[]>((acc, l, i) => (i === 0 ? [l] : [...acc, ' · ', l]), [])}</Fact>}
      </dl>
      <Evidence d={d} />
      <Decide d={d} onDecided={onDecided} />
    </section>
  )
}

function Inbox({
  items,
  selected,
  kind,
  go,
  onDecided,
}: {
  items: ApprovalItem[]
  selected: string | null
  kind: string | null
  go: (to: string) => void
  onDecided: (word: string) => void
}) {
  const now = useNow()
  const phone = usePhoneTables()
  const chosen = selected === null ? undefined : items.find((i) => i.approval_id === selected)
  const detail =
    selected === null ? null : chosen !== undefined && chosen.kind === 'input' ? (
      <InputItemDetail key={selected} item={chosen} go={go} />
    ) : (
      <ItemDetail key={selected} itemId={selected} go={go} onDecided={onDecided} />
    )
  return (
    <div className={phone ? 'ap-inbox is-phone' : 'ap-inbox is-split'}>
      <ul className="ap-list" aria-label="Waiting for approval, oldest first">
        {items.map((item) => {
          const on = item.approval_id === selected
          return (
            <li key={item.approval_id} className={on ? 'ap-item is-on' : 'ap-item'} data-approval={item.approval_id}>
              <InApp go={go} to={on ? approvalsAddress(kind) : approvalAddress(item.approval_id, kind)} className="ap-pick" current={on}>
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

/** The inbox's kind filter: every kind, or one (`?kind=`, the route's own filter). */
function KindFilter({ kind, go }: { kind: string | null; go: (to: string) => void }) {
  const kinds: [string | null, string][] = [[null, 'Every kind'], ...Object.entries(APPROVAL_KIND)]
  return (
    <div className="ap-filter" role="group" aria-label="Kind">
      {kinds.map(([k, word]) => (
        <Button key={k ?? 'all'} size="sm" kind={k === kind ? 'primary' : 'secondary'} aria-pressed={k === kind} onClick={() => go(approvalsAddress(k))}>
          {word}
        </Button>
      ))}
    </div>
  )
}

/**
 * THE INBOX, AND THE ITEM A LINK NAMED (AUT-6). An item the address names
 * that is not in the list read -- decided by someone else, expired, or not
 * of the kind filtered to -- is read on its own (`GET /v1/approvals/{id}`)
 * and drawn above the inbox with what became of it, so a link from a
 * notification never lands on what looks like an empty inbox.
 */
function ApprovalsBody({
  items,
  selected,
  kind,
  go,
  onDecided,
}: {
  items: ApprovalItem[]
  selected: string | null
  kind: string | null
  go: (to: string) => void
  onDecided: (word: string) => void
}) {
  const listed = selected !== null && items.some((i) => i.approval_id === selected)
  return (
    <>
      <KindFilter kind={kind} go={go} />
      {selected !== null && !listed && (
        <section className="ap-linked" data-testid="ap-linked" aria-label="The item the link names">
          {selected.startsWith(INPUT_PREFIX) ? (
            // A projected step that is not listed is no longer waiting: it
            // was answered, expired or cancelled. There is no record to read.
            <p className="sb-note" data-testid="ap-detail">
              This step is no longer waiting for an answer: it was answered, it expired, or it was cancelled. Its workflow’s page says which, and who answered.
            </p>
          ) : (
            <ItemDetail key={selected} itemId={selected} go={go} onDecided={onDecided} />
          )}
        </section>
      )}
      {items.length === 0 ? (
        <Absent
          kind="zero"
          heading="Nothing waiting"
          say={kind === null ? 'The inbox was read and nothing is waiting' : `The inbox was read and no ${APPROVAL_KIND[kind] ?? kind} item is waiting`}
        >
          {kind === null
            ? 'No firing, plan, merge, proposal or spec in this tenant is waiting for a person: the inbox was read and is empty.'
            : `Nothing of this kind is waiting for a person. Choose Every kind to read the whole inbox.`}
        </Absent>
      ) : (
        <Inbox items={items} selected={listed ? selected : null} kind={kind} go={go} onDecided={onDecided} />
      )}
    </>
  )
}

export function ApprovalsScreen({ view, go }: { view: string | null; go: (to: string) => void }) {
  const selected = approvalOf(view)
  const kind = kindOf(view)
  const [round, setRound] = useState(0)
  const [said, setSaid] = useState<string | null>(null)
  const decided = (word: string) => {
    setSaid(`${word}. The inbox was read again.`)
    go(approvalsAddress(kind))
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
        key={`approvals:${kind ?? ''}:${round}`}
        title="Approvals"
        // An empty inbox is drawn by the body, not the Screen, so the item a
        // link names is still read and shown beside it (AUT-6).
        load={async () => {
          const r = await loadApprovals(kind === null ? {} : { kind })
          return r.status === 'empty' ? { status: 'ok' as const, data: { approvals: [] }, fetchedAt: r.fetchedAt } : r
        }}
        summary={(d) => `${pluralise(d.approvals.length, 'item')} waiting, oldest first · waiting items hold no capacity`}
      >
        {(d) => <ApprovalsBody items={d.approvals} selected={selected} kind={kind} go={go} onDecided={decided} />}
      </Screen>
    </>
  )
}
