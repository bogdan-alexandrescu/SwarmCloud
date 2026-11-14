/**
 * AUTOMATE › APPROVALS (docs/schedules.md §4.5, §6.1; #2417 A4, proposal A):
 * the tenant's inbox, at `/automate/approvals`. One list holds every kind -- a
 * firing waiting to run, an issue run's plan (projected from the run, not
 * copied), a held plan, a green pull request waiting to merge, a proposal, a
 * spec, a workflow step waiting for an answer -- as `GET /v1/approvals`
 * serves it.
 *
 * THE LIST AS CARDS, THEN ONE ITEM ON ITS OWN PAGE (the Automate redesign's
 * cards direction, the owner's pick of 12 November 2026). A kind filter, then
 * Waiting, oldest first, each card with its expiry and its approvers; then
 * Decided, Expired and Superseded, drawn only from items the read serves in
 * those states. A card opens the item's decision page,
 * `/automate/approvals/<id>` (`ApprovalDecision.tsx`): read before you
 * decide, on a page of its own.
 *
 * THE SERVER HOLDS EVERY RULE. Who may approve (§4.6), a hold's stricter
 * approver (§4.4), the one-person tenant's typed confirmation (SD11), the
 * rule that only someone who may approve may reject (owner 2026-10-26) and the
 * race between two approvers are all decided in `routes/approvals.py`; this
 * list reads `may_decide` out of each row and never widens it.
 *
 * A waiting item holds no capacity (invariant 1): nothing here is running.
 */
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { loadApprovals } from './api'
import type { Result } from './fetch'
import { Button, Dash, Tag } from './components'
import { InfoTip } from './People'
import { PersonName } from './PersonName'
import { InApp, untilWords, useScheduleName } from './Schedules'
import { Screen, timeAgo } from './Shell'
import { pluralise } from './types'
import { useNow } from './useNow'
import {
  APPROVAL_KIND,
  ApprovalDecisionScreen,
  BackToList,
  approvalAddress,
  approvalsAddress,
  decidersWords,
  when,
  type InboxItem,
} from './ApprovalDecision'
import './styles/schedules.css'
import './styles/automate-approvals.css'

export { APPROVAL_KIND, APPROVALS_ADDRESS, approvalAddress, approvalsAddress } from './ApprovalDecision'

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

/**
 * EVERY KIND, IN THE CHIPS' OWN WORDS (visual QA QA-048, 2026-10-20). The
 * empty inbox said "No firing, plan, merge, proposal or spec": a word no
 * chip carries, and three chips it left out. It is read off the same table
 * as the filter, so a kind added there is named here.
 */
const EVERY_KIND = (() => {
  const words = Object.values(APPROVAL_KIND).map((w) => w.toLowerCase())
  return words.length < 2 ? words.join('') : `${words.slice(0, -1).join(', ')} or ${words[words.length - 1]}`
})()

/** A row the read serves as waiting. A row with no state is one, as every row was before the read served others. */
const isWaiting = (i: InboxItem) => i.state === 'pending' || i.state === undefined || i.state === null
/** Approved, rejected, or an await-input step a member answered: the route's decided states. */
const isDecided = (i: InboxItem) => i.state === 'approved' || i.state === 'rejected' || i.state === 'answered'

/** A stored `approvers` value, short, for a row: the decision page says it in full. */
function approversShort(a: unknown): string | null {
  if (a === undefined || a === null || a === '') return null
  if (a === 'members') return 'any member of this workspace'
  if (a === 'owner_only') return 'the platform owner'
  if (a === 'second_member') return 'a second member'
  if (Array.isArray(a)) return a.map(String).join(', ')
  return String(a)
}

/** Who may decide an item, in words: `may_decide`, else the stored approvers. */
function approversOf(item: InboxItem): string | null {
  if (item.kind === 'input') return 'any member of this workspace'
  return decidersWords(item.may_decide ?? null) ?? approversShort(item.approvers) ?? (item.kind === 'contract' ? 'the step’s owner' : null)
}

/** A served time, as the page says it, the instant in its title; a dash with its reason when unserved. */
function At({ iso }: { iso: string | null | undefined }) {
  return iso ? <span title={iso}>{when(iso)}</span> : <Dash why="not served" />
}

/** Where an item came from: its schedule by name, else its run, else a dash. */
function FromWords({ item }: { item: InboxItem }) {
  const s = item.subject ?? {}
  const scheduleId = typeof s.schedule_id === 'string' && s.schedule_id !== '' ? s.schedule_id : null
  const name = useScheduleName(scheduleId)
  if (scheduleId !== null) return <span title={scheduleId}>{name ?? scheduleId}</span>
  if (typeof s.run_id === 'string' && s.run_id !== '') return <span className="apc-mono">{s.run_id}</span>
  return <Dash why="what asked for it is not served" />
}

const DECIDED_BY: Readonly<Record<string, string>> = { approved: 'Approved by', rejected: 'Rejected by', answered: 'Answered by' }

/**
 * ONE ITEM AS A CARD (the cards mock-up). Its kind, what it asks, where it
 * came from and when; then what its state asks for: a waiting item its
 * expiry, a decided one who decided and when, an ended one when it ended.
 * Its approvers are on every card. One action opens the item's page.
 */
function ApprovalCard({ item, kind, go }: { item: InboxItem; kind: string | null; go: (to: string) => void }) {
  const now = useNow()
  const waiting = isWaiting(item)
  const st = item.state ?? 'pending'
  const who = approversOf(item)
  const md = item.may_decide ?? null
  return (
    <section className={`apc-card${waiting ? ' is-info' : ''}`} data-approval={item.approval_id} aria-label={item.summary}>
      <div className="apc-card-h">
        <Tag>{APPROVAL_KIND[item.kind] ?? item.kind}</Tag>
        <h3>{item.summary}</h3>
      </div>
      <dl className="apc-facts">
        <dt>From</dt>
        <dd>
          <FromWords item={item} />
        </dd>
        <dt>Asked</dt>
        <dd>
          <At iso={item.requested_at} />
        </dd>
        {waiting && (
          <>
            <dt>Expires</dt>
            <dd className="apc-strong">
              {item.expires_at === null ? (
                <>
                  never <InfoTip about="never expires" tip="A run made by hand waits as it always has." />
                </>
              ) : (
                <span title={`${when(item.expires_at)} (${item.expires_at})`}>{untilWords(item.expires_at, now)}</span>
              )}
            </dd>
          </>
        )}
        {st === 'expired' && (
          <>
            <dt>Expired</dt>
            <dd>
              <At iso={item.decided_at ?? item.expires_at} />
            </dd>
          </>
        )}
        {isDecided(item) && (
          <>
            <dt>{DECIDED_BY[st] ?? 'Decided by'}</dt>
            <dd>{item.decided_by ? <PersonName email={item.decided_by} name={item.decided_by_name} /> : <Dash why="who decided is not served" />}</dd>
            <dt>Decided</dt>
            <dd>
              <At iso={item.decided_at} />
            </dd>
          </>
        )}
        {st === 'superseded' && (
          <>
            <dt>Superseded</dt>
            <dd>
              <At iso={item.decided_at} /> · {item.kind === 'merge' ? 'head moved' : 'replaced'}
            </dd>
          </>
        )}
        <dt>Approvers</dt>
        <dd>
          {who ?? <Dash why="who may decide is not served" />}
          {waiting && md !== null && !md.approve && <span className="apc-dim"> · {md.reject ? 'you may reject' : 'not you'}</span>}
        </dd>
      </dl>
      <div className="apc-card-f">
        <InApp go={go} to={approvalAddress(item.approval_id, kind)} className={`c-btn is-sm${waiting ? ' is-primary' : ''}`}>
          {waiting ? 'Review' : 'Open'}
        </InApp>
      </div>
    </section>
  )
}

/** A titled panel of cards: its heading, a count or note beside it, and an (i) where it needs one. */
function Panel({ title, note, tip, testId, children }: { title: string; note: string | null; tip?: string; testId: string; children: ReactNode }) {
  return (
    <section className="apc-panel" aria-label={title} data-testid={testId}>
      <div className="apc-panel-h">
        <h2>{title}</h2>
        {note !== null && <span className="apc-dim">{note}</span>}
        {tip !== undefined && <InfoTip about={title} tip={tip} />}
      </div>
      {children}
    </section>
  )
}

/**
 * The kind filter: every kind, or one (`?kind=`, the route's own filter). A
 * chip carries its count where a read measured it: every chip on the whole
 * inbox, which a filtered page reads beside its own (`every`), so choosing a
 * kind does not strip the other chips of their numbers (visual QA
 * automate-05, 2026-10-28). Only the chosen chip has no figure when that
 * second read failed.
 *
 * A KIND WITH NOTHING WAITING HAS NO CHIP (automate-22): eight chips, five at
 * 0, took two rows above the list on a phone. The chosen kind keeps its chip
 * at 0, so the filter in force is always shown; a kind whose count was not
 * read keeps its chip, since nobody measured it empty.
 */
function KindFilter({ kind, items, every, go }: { kind: string | null; items: InboxItem[]; every: InboxItem[] | null; go: (to: string) => void }) {
  const kinds: [string | null, string][] = [[null, 'Every kind'], ...Object.entries(APPROVAL_KIND)]
  const waiting = (kind === null ? items : (every ?? [])).filter(isWaiting)
  const count = (k: string | null): number | null => {
    if (kind === null || every !== null) return k === null ? waiting.length : waiting.filter((i) => i.kind === k).length
    return k === kind ? items.filter(isWaiting).length : null
  }
  return (
    <div className="apl-kinds" role="group" aria-label="Kind">
      {kinds.map(([k, word]) => {
        const n = count(k)
        if (n === 0 && k !== null && k !== kind) return null
        return (
          <Button key={k ?? 'all'} size="sm" kind={k === kind ? 'primary' : 'secondary'} aria-pressed={k === kind} onClick={() => go(approvalsAddress(k))}>
            {word}
            {n !== null && <span className="apl-n"> · {n}</span>}
          </Button>
        )
      })}
    </div>
  )
}

/** Oldest first by `requested_at`; an item with no time goes last. */
const byAsked = (a: InboxItem, b: InboxItem) => (Date.parse(a.requested_at ?? '') || Infinity) - (Date.parse(b.requested_at ?? '') || Infinity)

/**
 * THE LIST, AS CARDS (the cards mock-up). Waiting, oldest first; then what
 * the read serves in other states, each in its own panel: Decided (newest
 * first), Expired and Superseded. A panel with nothing served in it is not
 * drawn, rather than drawn empty, which would read as "nothing was decided".
 * An answered await-input step is decided (QA2B-E review).
 */
function ApprovalsList({ items, every, kind, go }: { items: InboxItem[]; every: InboxItem[] | null; kind: string | null; go: (to: string) => void }) {
  const waiting = items.filter(isWaiting).sort(byAsked)
  const decided = items
    .filter(isDecided)
    .sort((a, b) => (Date.parse(b.decided_at ?? '') || 0) - (Date.parse(a.decided_at ?? '') || 0))
  const expired = items.filter((i) => i.state === 'expired')
  const superseded = items.filter((i) => !isWaiting(i) && !isDecided(i) && i.state !== 'expired')
  const cards = (rows: InboxItem[]) => rows.map((i) => <ApprovalCard key={i.approval_id} item={i} kind={kind} go={go} />)
  return (
    <div className="apc-page">
      <div className="apc-bar-row">
        <KindFilter kind={kind} items={items} every={every} go={go} />
      </div>
      <Panel title="Waiting" note={waiting.length === 0 ? null : `${waiting.length}, oldest first`} testId="apl-waiting">
        {waiting.length === 0 ? (
          <EmptyInbox kind={kind} elsewhere={every === null ? null : every.filter(isWaiting).length} go={go} />
        ) : (
          <div className="apc-grid is-3">{cards(waiting)}</div>
        )}
      </Panel>
      {decided.length + expired.length + superseded.length > 0 && (
        <div className="apc-lapsed" data-testid="apl-lapsed">
          {decided.length > 0 && (
            <Panel title="Decided" note={`${decided.length}, newest first`} testId="apl-decided">
              <div className="apc-grid is-2">{cards(decided)}</div>
            </Panel>
          )}
          {expired.length > 0 && (
            <Panel title="Expired" note={String(expired.length)} tip="Nobody decided in time, so it did not go ahead. A merge that expires leaves its pull request open." testId="apl-expired">
              <div className="apc-stack">{cards(expired)}</div>
            </Panel>
          )}
          {superseded.length > 0 && (
            <Panel title="Superseded" note={String(superseded.length)} tip="A newer request replaced it, so nobody decides it; the newer one asks again." testId="apl-superseded">
              <div className="apc-stack">{cards(superseded)}</div>
            </Panel>
          )}
        </div>
      )}
    </div>
  )
}

/**
 * A measured empty inbox (A6d), the mock-up's one line: "Nothing waiting".
 * What was read is its tooltip, the kinds named from the chips' table
 * (QA-048). An empty kind says how many wait under the other kinds, from the
 * whole inbox's read, and links to Every kind (automate-05): "Nothing
 * waiting" alone, beside a spine badge of 3, read as an empty inbox.
 */
function EmptyInbox({ kind, elsewhere, go }: { kind: string | null; elsewhere: number | null; go: (to: string) => void }) {
  const tip = kind === null ? `The inbox was read: no ${EVERY_KIND} waits for a person.` : `The inbox was read: no ${APPROVAL_KIND[kind] ?? kind} item waits for a person.`
  return (
    <p className="apc-nothing" data-testid="apl-nothing">
      <span className="apc-dot is-ok" aria-hidden />
      Nothing waiting
      <InfoTip about="Nothing waiting" tip={tip} />
      {kind !== null && elsewhere !== null && elsewhere > 0 && (
        <span className="apc-dim" data-testid="apl-elsewhere">
          {pluralise(elsewhere, 'item')} under other kinds ·{' '}
          <InApp go={go} to={approvalsAddress(null)}>
            Every kind ›
          </InApp>
        </span>
      )}
    </p>
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
function InputItemDetail({ item, go }: { item: InboxItem; go: (to: string) => void }) {
  const now = useNow()
  const s = item.subject
  const workflowId = typeof s.workflow_id === 'string' && s.workflow_id !== '' ? s.workflow_id : null
  const form = typeof s.form === 'string' ? s.form : null
  return (
    <section className="ap-detail ad-page" data-testid="ap-detail" data-kind="input" aria-label={`${APPROVAL_KIND.input}: ${item.summary}`}>
      <section className="ad-q">
        <p className="ad-tags">
          <span className="ad-chip is-kind">{(APPROVAL_KIND.input ?? 'Answer').toLowerCase()}</span>
          <span className="ad-chip is-warn">waiting</span>
        </p>
        <h2 className="ad-sent">{item.summary}</h2>
        <dl className="ad-qfacts">
          <div>
            <dt>Asked</dt>
            <dd>{item.requested_at === null ? <Dash why="not served" /> : <span title={item.requested_at}>{timeAgo(item.requested_at, now)}</span>}</dd>
          </div>
          <div>
            <dt>Expires</dt>
            <dd>{item.expires_at === null ? <Dash why="not served" /> : <span title={item.expires_at}>{untilWords(item.expires_at, now)}</span>}</dd>
          </div>
          <div>
            <dt>Who may answer</dt>
            <dd>any member of this workspace, checked when the answer arrives</dd>
          </div>
          {form !== null && (
            <div>
              <dt>Form</dt>
              <dd>{form}</dd>
            </div>
          )}
        </dl>
      </section>
      <div className="ad-bar is-info">
        <div className="ad-bar-l">
          <b className="ad-bar-h">The answer is given on the step’s card, never here</b>
          <span className="ad-sub">
            {workflowId === null ? 'The item names no workflow, so its step cannot be opened from here.' : 'The inbox only lists the steps waiting for one.'}
          </span>
        </div>
        {workflowId !== null && (
          <div className="ad-bar-acts">
            <InApp go={go} to={stepAddress(workflowId)} className="c-btn is-primary">
              Answer ›
            </InApp>
          </div>
        )}
      </div>
    </section>
  )
}

/**
 * A step's page, read off the inbox: the step is projected, so there is no
 * record to read on its own. One no longer listed was answered, expired or
 * cancelled, and the page says so rather than reading anything.
 */
function InputScreen({ itemId, kind, round, go }: { itemId: string; kind: string | null; round: number; go: (to: string) => void }) {
  return (
    <Screen
      key={`approval:${itemId}:${round}`}
      title="Approvals"
      load={async () => {
        // `all` (QA2-FIX-07, automate2-01): the route defaults to pending. The
        // history it adds holds answered and expired steps, which this page
        // never draws as waiting: only a waiting row is found below.
        const r = await loadApprovals(kind === null ? { state: 'all' } : { kind, state: 'all' })
        return r.status === 'empty' ? { status: 'ok' as const, data: { approvals: [] }, fetchedAt: r.fetchedAt } : r
      }}
    >
      {(d) => {
        const item = (d.approvals as InboxItem[]).find((i) => i.approval_id === itemId && isWaiting(i))
        return (
          <>
            <BackToList kind={kind} go={go} />
            {item !== undefined ? (
              <InputItemDetail item={item} go={go} />
            ) : (
              <section className="ad-linked" data-testid="ap-linked" aria-label="The item the link names">
                <p className="sb-note" data-testid="ap-detail">
                  This step is no longer waiting for an answer: it was answered, it expired, or it was cancelled. Its workflow’s page says which, and who answered.
                </p>
              </section>
            )}
          </>
        )
      }}
    </Screen>
  )
}

/** The list's read: the inbox as filtered, and on a filtered page the whole inbox beside it, or null when unread. */
type ListRead = { approvals: InboxItem[]; every: InboxItem[] | null }

async function readList(kind: string | null): Promise<Result<ListRead>> {
  // `all` on the list read (automate2-01): the route's default is the inbox only.
  const [r, all] = await Promise.all([loadApprovals(kind === null ? { state: 'all' } : { kind, state: 'all' }), kind === null ? null : loadApprovals()])
  const every = all === null ? null : all.status === 'ok' ? (all.data.approvals as InboxItem[]) : all.status === 'empty' ? [] : null
  if (r.status === 'empty') return { status: 'ok', data: { approvals: [], every }, fetchedAt: r.fetchedAt }
  if (r.status === 'ok' || r.status === 'stale') return { ...r, data: { approvals: r.data.approvals as InboxItem[], every } }
  return r
}

export function ApprovalsScreen({ view, go }: { view: string | null; go: (to: string) => void }) {
  const selected = approvalOf(view)
  const kind = kindOf(view)
  const [round, setRound] = useState(0)
  const [said, setSaid] = useState<string | null>(null)
  // WHAT A DECISION SAID TAKES FOCUS (QA1 automate-15): Reject's button is
  // disabled while busy and gone once the item is read again, so focus fell
  // to <body> and a keyboard reader started again from the top of the page.
  const saidLine = useRef<HTMLParagraphElement | null>(null)
  useEffect(() => {
    if (said !== null) saidLine.current?.focus()
  }, [said])
  const decided = (word: string) => {
    setSaid(`${word}. The inbox was read again.`)
    go(approvalsAddress(kind))
    setRound((n) => n + 1)
  }
  return (
    <>
      {said !== null && (
        <p ref={saidLine} tabIndex={-1} className="sb-note" role="status">
          {said}
        </p>
      )}
      {selected !== null && selected.startsWith(INPUT_PREFIX) ? (
        <InputScreen itemId={selected} kind={kind} round={round} go={go} />
      ) : selected !== null ? (
        <ApprovalDecisionScreen itemId={selected} kind={kind} round={round} go={go} onDecided={decided} />
      ) : (
        <Screen
          key={`approvals:${kind ?? ''}:${round}`}
          title="Approvals"
          // An empty inbox is drawn by the body (A6d), under the kind filter,
          // so the filter does not vanish on a kind with nothing waiting. A
          // filtered page also reads the whole inbox, for the other chips'
          // counts and what waits elsewhere (automate-05); that read failing
          // costs only those figures, never the page.
          load={() => readList(kind)}
        >
          {(d) => <ApprovalsList items={d.approvals} every={d.every} kind={kind} go={go} />}
        </Screen>
      )}
    </>
  )
}
