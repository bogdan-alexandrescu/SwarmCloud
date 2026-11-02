/**
 * ONE APPROVAL'S DECISION PAGE (#2417 A5, proposal B, with Reject in place of
 * B's Deny): `/automate/approvals/<id>`, opened from a row of the Approvals
 * list. It reads top to bottom -- the question, the evidence (numbered cards:
 * the pull request or the plan, why it is held, what you approve, what asked
 * for it), then the decision, pinned to the bottom of a desktop screen while
 * the evidence scrolls.
 *
 * WHO MAY DECIDE IS READ, NOT GUESSED. `GET /v1/approvals/{id}` serves
 * `may_decide {approve, reject, why, deciders}`, computed by the checks the
 * decision routes run. Only someone who may approve may reject (BGD
 * 2026-10-26), so a reader who may do neither sees no button at all (B's
 * A5f): the bar says who decides, from `deciders`, and why not them, from
 * `why`. An API that serves no `may_decide` (an older one) gets both verbs, as
 * before, and its refusal said in place. The server still decides every
 * click: a stale head or plan is refused (409), never approved unread.
 *
 * A decided item's bar is its outcome: who approved or rejected it and when,
 * the reason quoted in full; an expired one says nobody decided in time; a
 * superseded merge names the head it moved to and links the newer request
 * when `superseded_by` serves one.
 *
 * A waiting item holds no capacity (invariant 1): nothing here is running.
 */
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { approveApproval, loadApproval, loadSchedule, rejectApproval, type ApprovalDetail, type ApprovalItem } from './api'
import { Dash } from './components'
import { errorHeading, type ApiError, type Result } from './fetch'
import { PersonName } from './PersonName'
import { useRoleGate } from './Profile'
import { HeldButton, InApp, rememberScheduleNames, scheduleAddress, untilWords, useScheduleName } from './Schedules'
import { Screen, timeAgo } from './Shell'
import { useNow } from './useNow'
import './styles/schedules.css'
import './styles/automate-approvals.css'

/**
 * The page's address, and one item's (`/automate/approvals/<id>`, paths.ts). The
 * inbox's kind filter rides on both as `kind=` (`/automate/approvals?kind=merge`), so
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

/** `may_decide` (#2417 A4, A5f): what the reader may do, served beside each item. */
export interface MayDecide {
  approve: boolean
  reject: boolean
  why: string | null
  deciders: string[]
}

/** A hold as the approvals routes serve it, with `reason_files` where its reasons pair with files. */
export interface ServedHold {
  code?: string
  approvers?: string
  matched?: string[]
  reasons?: string[]
  reason_files?: Record<string, string[]>
}

/** One inbox row, with the fields the approvals routes serve beyond `ApprovalItem`. */
export type InboxItem = ApprovalItem & {
  approvers?: unknown
  hold?: ServedHold | null
  may_decide?: MayDecide | null
  decided_by?: string | null
  decided_by_name?: string | null
  decided_at?: string | null
  reason?: string | null
}

/** `GET /v1/approvals/{id}`, with the #2417 fields (PR 2501). */
export type Decision = Omit<ApprovalDetail, 'approval' | 'merge'> & {
  approval: Omit<ApprovalDetail['approval'], 'hold'> & {
    hold?: ServedHold | null
    may_decide?: MayDecide | null
    superseded_by?: { approval_id?: string; head_sha?: string | null; at?: string | null } | null
    head_sha_now?: string | null
  }
  merge?: NonNullable<ApprovalDetail['merge']> & {
    approval_hold?: ServedHold | null
    checks?: { passed?: number; failed?: number; pending?: number } | null
  }
}

/** The state an item is in, in the page's words: a pending item is waiting. */
export const STATE_WORD: Readonly<Record<string, string>> = {
  pending: 'waiting',
  approved: 'approved',
  rejected: 'rejected',
  expired: 'expired',
  superseded: 'superseded',
}

/** "a, b or c". */
function either(words: string[]): string {
  return words.length < 2 ? words.join('') : `${words.slice(0, -1).join(', ')} or ${words[words.length - 1]}`
}

/** `deciders`' rule words (`approvals.deciders`); anything else is an address. */
const DECIDER_WORDS: Readonly<Record<string, string>> = {
  owner_only: 'the platform owner',
  platform_admins: 'a platform admin',
  second_member: 'a second member',
  workspace_admins: 'a workspace admin',
  members: 'any member of this workspace',
}

/** Who may decide, from `may_decide.deciders`, or null when it is not served. */
export function decidersWords(d: MayDecide | null | undefined): string | null {
  const list = d?.deciders
  if (!Array.isArray(list) || list.length === 0) return null
  return either(list.map((x) => DECIDER_WORDS[x] ?? x))
}

/** Who a stored `approvers` value admits, in words. */
export function approversWords(a: unknown): string | null {
  if (a === undefined || a === null || a === '') return null
  if (a === 'members') return 'any member of this workspace'
  // #2259: the owner decides even as the schedule's last editor, audited.
  if (a === 'owner_only') return 'the platform owner alone, who may decide even as the schedule’s last editor; that decision is recorded in the audit as an owner waiver'
  if (a === 'second_member') return 'a second member: not the run’s creator, its last plan editor or its schedule’s last gate editor'
  if (Array.isArray(a)) return a.map(String).join(', ')
  return String(a)
}

export const HOLD_WORDS: Readonly<Record<string, string>> = {
  NEEDS_SECOND_MEMBER: 'held for a second member',
  NEEDS_OWNER: 'held for the platform owner',
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

/**
 * What each kind asked for, once it is decided (visual QA automate-07,
 * 2026-10-28): "What you approve" in the present tense sat above "Nothing is
 * left to decide." on a rejected plan.
 */
const ASKED_FOR: Readonly<Record<string, string>> = {
  run: 'It asked to create the work this firing was waiting to make.',
  plan: 'It asked to start the run on this plan.',
  hold: 'It asked to start the run on this held plan.',
  merge: 'It asked to merge this pull request at the head it was checked at.',
  proposal: 'It asked to file this proposal as an issue.',
  spec: 'It asked to approve this custom-prompt spec.',
}

/** A date and time, short: "Oct 29, 02:40". The full instant rides on the title. */
export function when(iso: string): string {
  const d = new Date(iso)
  return Number.isFinite(d.getTime()) ? d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : iso
}

/**
 * The head a merge approval is for: the digest's `head_sha` (§4.5 -- sent as
 * the object, or as `<head_sha>:<verdict>`), else the green head. It is what
 * Approve names, because a push after the request makes the approval stale.
 */
function mergeDigest(d: Decision): { head: string | null; verdict: string | null } {
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

/** One row of the merge checklist: met, unmet, or not read -- never left to plain text. */
type Check = { key: string; mark: 'met' | 'unmet' | 'unread'; label: ReactNode; sub?: ReactNode }

const MARK = {
  met: { glyph: '✓', word: 'met' },
  unmet: { glyph: '✗', word: 'unmet' },
  unread: { glyph: '–', word: 'not read' },
} as const

/**
 * What a merge is decided on: one row per check with its mark -- CI green at
 * the head, the review's verdict, each requirement, and the hard-stop gate on
 * the pull request's changed files. An unmet one is a ✗ row of its own.
 * Approve stays enabled whatever they say (owner decision 2026-10-11): the
 * server holds every rule and refuses what it must, in place.
 */
function mergeChecks(d: Decision): Check[] {
  const m = d.merge ?? {}
  const counted = checkCounts(d) !== null
  const { head, verdict } = mergeDigest(d)
  const out: Check[] = []
  const green = m.green_sha ?? null
  if (green === null) {
    out.push({ key: 'ci', mark: 'unmet', label: 'Required checks are not green yet', sub: 'No green CI reading at this head.' })
  } else if (head !== null && green !== head) {
    out.push({ key: 'ci', mark: 'unmet', label: 'Required checks green at an older head', sub: <>green at <span className="mono">{short(green)}</span>, the head is <span className="mono">{short(head)}</span></> })
  } else {
    out.push({
      key: 'ci',
      mark: 'met',
      label: 'Required checks green at the head',
      sub: (
        <>
          green at <span className="mono">{short(green)}</span>
          {!counted && '; per-check counts not served'}
        </>
      ),
    })
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
  const hold = m.approval_hold
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

function MergeChecks({ d }: { d: Decision }) {
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

/** `merge.checks` when every count is served, else null. */
function checkCounts(d: Decision): { passed: number; failed: number; pending: number } | null {
  const c = d.merge?.checks
  const ok = (n: unknown): n is number => typeof n === 'number' && Number.isFinite(n) && n >= 0
  return c === null || c === undefined || !ok(c.passed) || !ok(c.failed) || !ok(c.pending) ? null : { passed: c.passed, failed: c.failed, pending: c.pending }
}

/**
 * `merge.checks {passed, failed, pending}` at the head the approval names, as
 * a bar and its counts. Not served (no reading, or one of another head), it
 * is a dash with its reason, never "0 passed" -- unless the head is green,
 * when the checklist's green row says the counts are not served, on the one
 * line (visual QA automate-23: a dash line sat directly above "✓ Required
 * checks green at the head").
 */
function CheckCounts({ d, head }: { d: Decision; head: string | null }) {
  const c = checkCounts(d)
  if (c === null) {
    if (mergeChecks(d).some((k) => k.key === 'ci' && k.mark === 'met')) return null
    return (
      <p className="ad-counts" data-testid="ad-check-counts">
        Checks at this head: <Dash why="not served" /> <span className="ad-sub-i">no check counts are served for the head this approval names</span>
      </p>
    )
  }
  const total = c.passed + c.failed + c.pending
  const said = `${c.passed} passed, ${c.failed} failed, ${c.pending} pending`
  return (
    <>
      {total > 0 && (
        <div className="ad-cbar" role="img" aria-label={said}>
          {c.passed > 0 && <i className="is-passed" style={{ flexGrow: c.passed }} />}
          {c.failed > 0 && <i className="is-failed" style={{ flexGrow: c.failed }} />}
          {c.pending > 0 && <i className="is-pending" style={{ flexGrow: c.pending }} />}
        </div>
      )}
      <p className="ad-counts" data-testid="ad-check-counts">
        <b className={c.failed > 0 ? 'is-bad' : c.pending > 0 ? '' : 'is-ok'}>{c.passed} passed</b> · {c.failed} failed · {c.pending} pending
        {head !== null && (
          <>
            {' '}
            at <span className="mono">{short(head)}</span>
          </>
        )}
      </p>
    </>
  )
}

/** One numbered evidence card. */
function Card({ n, title, children }: { n: number; title: string; children: ReactNode }) {
  return (
    <section className="ad-card" aria-label={title} data-n={n}>
      <h3 className="ad-card-h">
        {n} · {title}
      </h3>
      {children}
    </section>
  )
}

function Kv({ name, children }: { name: string; children: ReactNode }) {
  return (
    <>
      <dt>{name}</dt>
      <dd>{children}</dd>
    </>
  )
}

/** The hold's lead sentence in words: "Held for the platform owner."; a code with no words is said as served. */
function holdLead(code: string | undefined): string {
  const words = code === undefined ? undefined : HOLD_WORDS[code]
  if (words !== undefined) return `${words.charAt(0).toUpperCase()}${words.slice(1)}.`
  return code === undefined || code === '' ? 'Held.' : `Held: ${code}.`
}

/** Why it is held: the hold's words, its reasons, and the files each reason matched. */
function HoldBody({ hold, itemApprovers }: { hold: ServedHold; itemApprovers: unknown }) {
  const reasons = hold.reasons ?? []
  const pairs = hold.reason_files ?? null
  const matched = hold.matched ?? []
  return (
    <div className="ad-hold" role="note">
      {/* The words lead, the code only as a title (visual QA automate2-05):
          "Held NEEDS_OWNER: held for the platform owner." said it twice. */}
      <p className="ad-p">
        <b title={hold.code}>{holdLead(hold.code)}</b>
      </p>
      {reasons.length > 0 && (
        <p className="ad-tags" aria-label="Reasons">
          {reasons.map((r) => (
            <span key={r} className="ad-chip is-warn">
              {r}
            </span>
          ))}
        </p>
      )}
      {pairs !== null && Object.keys(pairs).length > 0 ? (
        <dl className="ad-kv" aria-label="Files each reason matched">
          {Object.entries(pairs).map(([r, files]) => (
            <Kv key={r} name={r}>
              {files.map((f) => (
                <code key={f} className="ad-file">
                  {f}
                </code>
              ))}
            </Kv>
          ))}
        </dl>
      ) : (
        matched.length > 0 && (
          <>
            <p className="ad-p">Matched {matched.join(', ')}</p>
          </>
        )
      )}
      {/* "Who decides" above already says it when the item's approvers are the hold's, as a hold item's are. */}
      {approversWords(hold.approvers) !== approversWords(itemApprovers) && (
        <p className="ad-sub">Approved by {approversWords(hold.approvers) ?? 'the hold’s approvers'}.</p>
      )}
    </div>
  )
}

/**
 * THE SCHEDULE BY ITS NAME, its id beside it (QA2 automate2-15): "Its
 * schedule sch_a" named it by id though the list had its name. A name the
 * Schedules list read this session is used at once; else the schedule is read
 * once for it, and "Its schedule" stands until the name arrives (or if the
 * read fails).
 */
function ScheduleNamed({ id, go }: { id: string; go: (to: string) => void }) {
  const name = useScheduleName(id)
  useEffect(() => {
    if (name !== null) return
    let live = true
    void loadSchedule(id).then((r) => {
      if (live && (r.status === 'ok' || r.status === 'stale')) rememberScheduleNames([r.data.schedule])
    })
    return () => {
      live = false
    }
  }, [id, name])
  return (
    <>
      <InApp go={go} to={scheduleAddress(id)}>
        {name ?? 'Its schedule'}
      </InApp>{' '}
      <span className="mono ad-sub-i">{id}</span>
    </>
  )
}

/** Where the item came from: its schedule, firing, run, issue and pull request, as served. */
function AskedBy({ d, go }: { d: Decision; go: (to: string) => void }) {
  const s = d.approval.subject
  const rows: ReactNode[] = []
  if (typeof s.schedule_id === 'string' && s.schedule_id !== '') {
    rows.push(
      <Kv key="schedule" name="Schedule">
        <ScheduleNamed id={s.schedule_id} go={go} />
      </Kv>,
    )
  }
  if (typeof s.firing_id === 'string' && s.firing_id !== '') {
    rows.push(
      <Kv key="firing" name="Firing">
        <span className="mono">{s.firing_id}</span>
      </Kv>,
    )
  }
  if (typeof s.run_id === 'string' && s.run_id !== '') {
    rows.push(
      <Kv key="run" name="Run">
        <InApp go={go} to={runAddress(s.run_id)}>
          <span className="mono">{s.run_id}</span>
        </InApp>
      </Kv>,
    )
  }
  if (typeof s.issue === 'string' && s.issue !== '') {
    rows.push(
      <Kv key="issue" name="Issue">
        {s.issue}
      </Kv>,
    )
  }
  const pr = d.merge?.pull_request
  if (pr?.number !== undefined) {
    rows.push(
      <Kv key="pr" name="Pull request">
        {pr.url ? (
          <a href={pr.url} target="_blank" rel="noreferrer">
            #{pr.number}
          </a>
        ) : (
          `#${pr.number}`
        )}
      </Kv>,
    )
  }
  return rows.length > 0 ? <dl className="ad-kv">{rows}</dl> : <p className="ad-p">The item names nothing that asked for it.</p>
}

function runAddress(runId: string): string {
  return `work/runs?${new URLSearchParams({ run: runId }).toString()}`
}

/**
 * A plan's steps, as served (`issueruns.PlanStep`: a title and a prompt): the
 * reader approves "Start the run on this plan?" on them, so they are drawn
 * under the summary (visual QA automate-06). A step with no title is named by
 * its prompt; one with neither is not drawn.
 */
function planSteps(raw: unknown[] | undefined): { key: string; title: string; prompt: string | null }[] {
  const text = (v: unknown) => (typeof v === 'string' && v.trim() !== '' ? v : null)
  return (raw ?? []).flatMap((st, i) => {
    if (typeof st === 'string') return text(st) === null ? [] : [{ key: `s${i}`, title: st, prompt: null }]
    if (st === null || typeof st !== 'object') return []
    const o = st as { step_id?: unknown; title?: unknown; prompt?: unknown }
    const title = text(o.title) ?? text(o.prompt)
    if (title === null) return []
    return [{ key: text(o.step_id) ?? `s${i}`, title, prompt: text(o.title) === null ? null : text(o.prompt) }]
  })
}

/** The evidence, as numbered cards: what it is, why it is held, what you approve, what asked for it. */
function Evidence({ d, go }: { d: Decision; go: (to: string) => void }) {
  const a = d.approval
  const plan = d.run?.plan ?? null
  const files = plan?.files ?? []
  const steps = planSteps(plan?.steps)
  const pending = a.state === 'pending'
  const hold = a.hold ?? d.merge?.approval_hold ?? null
  const { head, verdict } = a.kind === 'merge' ? mergeDigest(d) : { head: null, verdict: null }
  const now = a.head_sha_now ?? null
  const cards: { title: string; body: ReactNode; side: 'l' | 'r' }[] = []
  if (d.merge !== undefined) {
    const pr = d.merge.pull_request
    cards.push({
      title: 'The pull request',
      side: 'l',
      body: (
        <>
          <dl className="ad-kv">
            <Kv name="Pull request">
              {pr?.url ? (
                <a href={pr.url} target="_blank" rel="noreferrer">
                  #{pr.number ?? '?'}
                </a>
              ) : pr?.number !== undefined ? (
                `#${pr.number}`
              ) : (
                <Dash why="the pull request is not served" />
              )}
            </Kv>
            <Kv name="Head">
              {head === null ? <Dash why="the head is not served" /> : <code>{short(head)}</code>}
              {now !== null && head !== null && now !== head && (
                <>
                  {' '}
                  <span className="ad-chip is-warn" data-testid="ad-head-now">
                    now {short(now)}
                  </span>
                </>
              )}
            </Kv>
          </dl>
          <CheckCounts d={d} head={head} />
          <MergeChecks d={d} />
        </>
      ),
    })
  }
  if (plan !== null) {
    cards.push({
      title: 'The plan',
      side: 'l',
      body: (
        <>
          {plan.summary ? <p className="ad-p">{plan.summary}</p> : <p className="ad-p">The plan carries no summary.</p>}
          {steps.length > 0 ? (
            <ol className="ad-files ad-steps" aria-label="The plan’s steps">
              {steps.map((st) => (
                <li key={st.key}>
                  <b>{st.title}</b>
                  {st.prompt !== null && <span className="ad-sub ad-step-p">{st.prompt}</span>}
                </li>
              ))}
            </ol>
          ) : (
            <p className="ad-p">The plan serves no steps.</p>
          )}
          {files.length > 0 ? (
            <ul className="ad-files" aria-label="Files the plan names">
              {files.map((f) => (
                <li key={f} className="mono">
                  {f}
                </li>
              ))}
            </ul>
          ) : (
            <p className="ad-p">The plan names no file. Its files are advisory: a hold is checked again at merge.</p>
          )}
        </>
      ),
    })
  }
  // Why it is held reads before what you approve, in the left column as the
  // mock-up draws it, so the DOM, the Tab order and the numbers agree
  // (visual QA automate2-04: the right column put 2 after 3 in the DOM).
  if (hold !== null) cards.push({ title: 'Why it is held', side: 'l', body: <HoldBody hold={hold} itemApprovers={a.approvers} /> })
  // Once decided, the card says what the item asked for, in the past tense,
  // and no longer what a decision would send (automate-07).
  cards.push({
    title: pending ? 'What you approve' : 'What it asked for',
    side: 'l',
    body: (
      <>
        <p className="ad-p">{pending ? (APPROVE_DOES[a.kind] ?? 'Approving lets this go ahead.') : (ASKED_FOR[a.kind] ?? 'It asked to let this go ahead.')}</p>
        {a.kind === 'merge' ? (
          <>
            {pending && <p className="ad-p">A decision is sent with this digest. If the head moves before it arrives, the server refuses it.</p>}
            {/* A digest with no verdict draws no Verdict row (visual QA automate2-20). */}
            <dl className="ad-kv">
              {verdict !== null && <Kv name="Verdict">{verdict}</Kv>}
              <Kv name="At head">{head === null ? <Dash why="the head is not served" /> : <code>{short(head)}</code>}</Kv>
            </dl>
          </>
        ) : (
          pending && a.digest !== undefined && a.digest !== null && <p className="ad-p">Approve sends back what this page read: a plan or a firing changed since is refused, never approved unread.</p>
        )}
      </>
    ),
  })
  cards.push({ title: 'What asked for it', side: 'r', body: <AskedBy d={d} go={go} /> })
  const numbered = cards.map((c, i) => ({ ...c, n: i + 1 }))
  const col = (side: 'l' | 'r') =>
    numbered
      .filter((c) => c.side === side)
      .map((c) => (
        <Card key={c.title} n={c.n} title={c.title}>
          {c.body}
        </Card>
      ))
  return (
    <div className="ad-ev">
      <div className="ad-col">{col('l')}</div>
      <div className="ad-col">{col('r')}</div>
    </div>
  )
}

/** What each kind's item is, as a decided headline names it: "This plan was rejected." */
const KIND_NOUN: Readonly<Record<string, string>> = {
  run: 'This firing',
  plan: 'This plan',
  hold: 'This held plan',
  proposal: 'This proposal',
  spec: 'This spec',
}

/**
 * What became of a decided item, in one sentence (visual QA automate-07): the
 * question "Start the run on this plan?" stayed the headline of a rejected,
 * expired or superseded plan, above "Nothing is left to decide."
 */
function outcomeSentence(d: Decision): ReactNode {
  const a = d.approval
  const n = d.merge?.pull_request?.number
  const noun: ReactNode =
    a.kind === 'merge' ? <>The merge of {n === undefined ? 'this pull request' : <>pull request <em>#{n}</em></>}</> : (KIND_NOUN[a.kind] ?? 'This request')
  const happened = a.state === 'expired' ? 'expired' : `was ${STATE_WORD[a.state] ?? a.state}`
  return (
    <>
      {noun} {happened}.
    </>
  )
}

/** The question the item asks, in one sentence; once decided, what became of it. */
function question(d: Decision): ReactNode {
  const a = d.approval
  if (a.state !== 'pending') return outcomeSentence(d)
  switch (a.kind) {
    case 'merge': {
      const n = d.merge?.pull_request?.number
      const { head } = mergeDigest(d)
      return (
        <>
          Merge {n === undefined ? 'this pull request' : <>pull request <em>#{n}</em></>}
          {head !== null && (
            <>
              {' '}
              at <em className="mono">{short(head)}</em>
            </>
          )}
          ?
        </>
      )
    }
    case 'plan':
      return 'Start the run on this plan?'
    case 'hold':
      return 'Start the run on this held plan?'
    case 'run':
      return 'Let this firing make its work?'
    case 'proposal':
      return 'File this proposal as an issue?'
    case 'spec':
      return 'Approve this custom-prompt spec?'
    default:
      return 'Let this go ahead?'
  }
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
    case 'reject_approver_required':
      return `Only someone who may approve this may reject it: ${e.message}`
    // #2259: `second_person_required` takes the default. The server's
    // message already says the last editor may not, and names who may
    // decide instead; a prefix here only restated its first clause.
    default:
      return `Not decided: ${e.message}`
  }
}

/** The words a one-person tenant types to approve its own hold (SD11), from a 403's detail. */
function confirmationOf(e: ApiError): string | null {
  const d = e.detail as { confirm?: unknown } | undefined
  return e.code === 'hold_approver_required' && typeof d?.confirm === 'string' && d.confirm !== '' ? d.confirm : null
}

/** When it expires, said for the bar: "Expires Oct 29, 02:40, in 2d." */
function expiryLine(a: Decision['approval'], now: number): string {
  if (a.expires_at === null) return 'It never expires: a run made by hand waits as it always has.'
  return `Expires ${when(a.expires_at)}, ${untilWords(a.expires_at, now)}.`
}

/** The decision bar of a decided, expired or superseded item: its outcome. */
function Outcome({ d, kind, go }: { d: Decision; kind: string | null; go: (to: string) => void }) {
  const a = d.approval
  const runId = typeof a.subject.run_id === 'string' && a.subject.run_id !== '' ? a.subject.run_id : null
  // Who decided, through `PersonName` (user-profile.md §4.4): their name, when
  // they set one, always beside the email that decided.
  const by = a.decided_by ? (
    <>
      {' by '}
      <PersonName email={a.decided_by} name={a.decided_by_name} />
    </>
  ) : null
  const at = a.decided_at ? <span title={a.decided_at}>, {when(a.decided_at)}</span> : null
  const openRun =
    runId === null ? null : (
      <InApp go={go} to={runAddress(runId)} className="c-btn">
        Open the run ›
      </InApp>
    )
  let tone = 'is-done'
  let head: ReactNode
  let body: ReactNode = null
  let act: ReactNode = openRun
  switch (a.state) {
    case 'approved':
      tone = 'is-ok'
      head = <>Approved{by}{at}.</>
      body = a.kind === 'merge' ? 'The merge step merges it next. Read it on the run.' : 'What it let go ahead is on the run.'
      break
    case 'rejected':
      tone = 'is-bad'
      head = <>Rejected{by}{at}.</>
      body = (
        <>
          {a.reason ? <q className="ad-quote">{a.reason}</q> : <span className="ad-sub">The rejection carries no reason.</span>}
          {a.kind === 'merge' && <span className="ad-sub">The pull request stays open and unmerged.</span>}
        </>
      )
      break
    case 'expired':
      head = a.expires_at ? <>Expired <span title={a.expires_at}>{when(a.expires_at)}</span>: nobody decided it in time.</> : 'Expired: nobody decided it in time.'
      body = a.kind === 'merge' ? 'So it did not merge. The pull request stays open.' : 'So it did not go ahead.'
      break
    case 'superseded': {
      tone = 'is-warn'
      const next = a.superseded_by ?? null
      const served = next !== null && !!next.approval_id
      // Only a merge has a head (visual QA automate-08): a plan is replaced
      // when the run's plan changes, a firing when a newer one asks.
      const isMerge = a.kind === 'merge'
      const isPlan = a.kind === 'plan' || a.kind === 'hold'
      const moved = isMerge ? (a.head_sha_now ?? next?.head_sha ?? null) : null
      const { head: was } = isMerge ? mergeDigest(d) : { head: null }
      head =
        moved !== null ? <>Superseded: the head moved to <span className="mono">{short(moved)}</span>.</> : isPlan ? 'Superseded: the run’s plan changed.' : 'Superseded: a newer request replaced it.'
      body = (
        <>
          {was !== null && (
            <span className="ad-sub">
              This approval was for <span className="mono">{short(was)}</span> and can no longer merge anything.
            </span>
          )}
          {isPlan && served && <span className="ad-sub">The new plan has its own request.</span>}
          {!served && (
            <span className="ad-sub">
              {isMerge ? 'No newer request is served for the new head yet.' : isPlan ? 'The new plan has its own request; none is served yet.' : 'No newer request is served for it yet.'}
            </span>
          )}
        </>
      )
      if (next !== null && next.approval_id) {
        act = (
          <InApp go={go} to={approvalAddress(next.approval_id, kind)} className="c-btn is-primary">
            Open the newer request ›
          </InApp>
        )
      }
      break
    }
    default:
      head = <>{a.state}{by}{at}.</>
  }
  return (
    <div className={`ad-bar ${tone}`} data-testid="ap-decided" data-state={a.state}>
      <div className="ad-bar-l">
        <b className="ad-bar-h">{head}</b>
        {body !== null && <span className="ad-sub">{body}</span>}
        <span className="ad-sub">Nothing is left to decide.</span>
      </div>
      {act !== null && <div className="ad-bar-acts">{act}</div>}
    </div>
  )
}

/** The bar a reader who may not decide sees (B's A5f): no button, who decides, and why not them. */
function NotYours({ md }: { md: MayDecide }) {
  const who = decidersWords(md)
  return (
    <div className="ad-bar is-info" data-testid="ap-no-decide">
      <div className="ad-bar-l">
        <b className="ad-bar-h">{who === null ? 'You may not decide this' : `Only ${who} decides this`}</b>
        {md.why && <span className="ad-sub">{md.why}</span>}
        <span className="ad-sub">You can read the evidence. When it is decided, the outcome shows here.</span>
      </div>
    </div>
  )
}

/** Approve and Reject, as `may_decide` allows, with the server's refusal said in place. */
function Decide({ d, onDecided }: { d: Decision; onDecided: (word: string) => void }) {
  const a = d.approval
  const now = useNow()
  const md = a.may_decide ?? null
  const [busy, setBusy] = useState<'approve' | 'reject' | null>(null)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const [confirm, setConfirm] = useState<{ expected: string; typed: string } | null>(null)
  const [reason, setReason] = useState('')
  // A VIEWER DECIDES NOTHING (docs/design/workspace-roles.md §4.8). With
  // `may_decide` served the server already says so (no button); an older API
  // draws both verbs disabled with the role sentence.
  const roleGate = useRoleGate('member', 'decide')
  // After a `hold_approver_required` refusal the typed confirmation is what
  // comes next, so focus lands there (visual QA V249).
  const confirmField = useRef<HTMLInputElement>(null)
  const asking = confirm !== null
  useEffect(() => {
    if (asking) confirmField.current?.focus()
  }, [asking])

  if (md !== null && !md.approve && !md.reject) return <NotYours md={md} />
  const offerApprove = md === null || md.approve
  const offerReject = md === null || md.reject
  const gate = md === null ? roleGate : null

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

  const confirmOk = confirm === null || confirm.typed.trim() === confirm.expected
  // A merge names its head on the button: that head is what is approved.
  const head = a.kind === 'merge' ? mergeDigest(d).head : null
  const approveWord = a.kind !== 'merge' ? 'Approve' : head === null ? 'Approve merge' : `Approve merge at ${short(head)}`
  const lead = md === null ? 'Decide this' : offerApprove ? 'You may decide this' : 'You may reject this, not approve it'
  return (
    <div className="ad-bar is-decide" data-testid="ad-decide">
      <div className="ad-bar-l">
        <b className="ad-bar-h">{lead}</b>
        <span className="ad-sub">{expiryLine(a, now)}</span>
        {md !== null && !md.approve && md.why && <span className="ad-sub">{md.why}</span>}
        {gate !== null && (
          <span className="ad-sub" data-role-gate="member">
            {gate}
          </span>
        )}
      </div>
      {offerReject && (
        <label className="c-field ad-why">
          <span className="c-lbl">Reason (needed to reject)</span>
          <textarea className="c-inp" rows={2} maxLength={1000} value={reason} placeholder="Say why it is rejected" onChange={(e) => setReason(e.target.value)} />
        </label>
      )}
      {confirm !== null && (
        <label className="c-field ad-why">
          <span className="c-lbl">
            You are the only person in this workspace, so no second member can approve this hold. Type <code>{confirm.expected}</code> to approve it yourself.
          </span>
          <input ref={confirmField} className="c-inp is-mono" value={confirm.typed} autoComplete="off" spellCheck={false} onChange={(e) => setConfirm({ ...confirm, typed: e.target.value })} />
        </label>
      )}
      <div className="ad-bar-acts">
        {offerReject && (
          // A pressed Reject keeps focus while busy (QA2 automate2-16): `HeldButton`, not `busy`'s disabled.
          <HeldButton kind="danger" busy={busy === 'reject' ? 'Rejecting…' : false} disabled={gate !== null} disabledReason={reason.trim() === '' ? 'Say why it is rejected.' : undefined} onClick={() => void reject()}>
            Reject
          </HeldButton>
        )}
        {offerApprove && (
          <HeldButton kind="primary" busy={busy === 'approve' ? 'Approving…' : false} disabled={gate !== null} disabledReason={confirmOk ? undefined : 'Type the words shown exactly.'} onClick={() => void approve()}>
            {approveWord}
          </HeldButton>
        )}
      </div>
      {refused !== null && !(confirm !== null && confirm.typed === '' && confirmationOf(refused) !== null) && (
        <p className="c-hint ad-refused" role="alert" data-code={refused.code ?? ''}>
          {refusalWords(refused)}
        </p>
      )}
    </div>
  )
}

/** The page: the way back, the question, the evidence, and the decision. */
export function ApprovalDecision({ d, kind, go, onDecided }: { d: Decision; kind: string | null; go: (to: string) => void; onDecided: (word: string) => void }) {
  const a = d.approval
  const now = useNow()
  const word = STATE_WORD[a.state] ?? a.state
  const who = decidersWords(a.may_decide) ?? approversWords(a.approvers)
  const tone = a.state === 'pending' ? 'is-warn' : a.state === 'approved' ? 'is-ok' : a.state === 'rejected' ? 'is-bad' : 'is-done'
  return (
    <section className="ap-detail ad-page" data-testid="ap-detail" data-state={a.state} aria-label={`${APPROVAL_KIND[a.kind] ?? a.kind}: ${a.summary}`}>
      <section className="ad-q">
        <p className="ad-tags">
          <span className="ad-chip is-kind">{(APPROVAL_KIND[a.kind] ?? a.kind).toLowerCase()} approval</span>
          <span className={`ad-chip ${tone}`} data-testid="ap-state">
            {word}
          </span>
          <span className="mono ad-id">{a.approval_id}</span>
        </p>
        <h2 className="ad-sent">{question(d)}</h2>
        <p className="ad-say">Its request reads: “{a.summary}”.</p>
        <dl className="ad-qfacts">
          <div>
            <dt>Asked</dt>
            <dd>{a.requested_at === null ? <Dash why="not served" /> : <span title={a.requested_at}>{when(a.requested_at)} · {timeAgo(a.requested_at, now)}</span>}</dd>
          </div>
          <div>
            <dt>{a.state === 'pending' ? 'Expires' : a.state === 'expired' ? 'Expired' : 'Was due to expire'}</dt>
            <dd>
              {a.expires_at === null ? (
                <span className="ad-sub-i">never: a run made by hand waits as it always has</span>
              ) : (
                <span title={a.expires_at}>
                  {when(a.expires_at)}
                  {a.state === 'pending' && <> · {untilWords(a.expires_at, now)}</>}
                </span>
              )}
            </dd>
          </div>
          <div>
            <dt>Who decides</dt>
            <dd data-testid="ad-who">{who ?? <Dash why="not served" />}</dd>
          </div>
        </dl>
      </section>
      <Evidence d={d} go={go} />
      {a.state === 'pending' ? <Decide d={d} onDecided={onDecided} /> : <Outcome d={d} kind={kind} go={go} />}
    </section>
  )
}

/** The way back to the list the item was opened from, its kind kept. */
export function BackToList({ kind, go }: { kind: string | null; go: (to: string) => void }) {
  return (
    <p className="ad-back">
      <InApp go={go} to={approvalsAddress(kind)}>
        ‹ Approvals{kind === null ? '' : `, ${APPROVAL_KIND[kind] ?? kind}`}
      </InApp>
    </p>
  )
}

/**
 * `/automate/approvals/<id>`: one item, read on its own (`GET
 * /v1/approvals/{id}`), so a link to an item someone else decided, or one that
 * expired, lands on what became of it (AUT-6) rather than on the list.
 */
export function ApprovalDecisionScreen({ itemId, kind, round, go, onDecided }: { itemId: string; kind: string | null; round: number; go: (to: string) => void; onDecided: (word: string) => void }) {
  return (
    <Screen
      key={`approval:${itemId}:${round}`}
      title="Approvals"
      // The read is wrapped so the page says a missing item in its own words
      // (the link names an item that does not exist here), not the Screen's.
      load={async () => {
        const r = await loadApproval(itemId)
        return { status: 'ok' as const, data: r as Result<Decision>, fetchedAt: Date.now() }
      }}
    >
      {(r) => (
        <>
          <BackToList kind={kind} go={go} />
          {r.status === 'ok' ? (
            <ApprovalDecision d={r.data} kind={kind} go={go} onDecided={onDecided} />
          ) : r.status === 'error' ? (
            <p className="c-hint" role="alert" data-testid="ap-detail">
              {r.error.kind === 'not_found'
                ? `No approval ${itemId} in this workspace: the link names an item that does not exist here, or a plan whose run has since moved on.`
                : `Not read: ${errorHeading(r.error)}`}
            </p>
          ) : (
            <p className="sb-note" data-testid="ap-detail">
              {r.status === 'loading' ? 'Reading the item…' : 'The item answered nothing.'}
            </p>
          )}
        </>
      )}
    </Screen>
  )
}
