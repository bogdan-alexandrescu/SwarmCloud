/**
 * AUTOMATE › APPROVALS (docs/schedules.md §4.5, §6.1; #2417 A4, proposal A):
 * the tenant's inbox, at `/automate/approvals`. One list holds every kind -- a
 * firing waiting to run, an issue run's plan (projected from the run, not
 * copied), a held plan, a green pull request waiting to merge, a proposal, a
 * spec, a workflow step waiting for an answer -- as `GET /v1/approvals`
 * serves it.
 *
 * THE LIST, THEN ONE ITEM ON ITS OWN PAGE. The lead says how many items wait
 * (every inbox item, not only the reader's) and when the oldest expires; then
 * Waiting, oldest first, each with its expiry and who may decide; then
 * Decided and Expired or superseded, drawn only from items the read serves
 * in those states. A row opens the item's decision page,
 * `/automate/approvals/<id>` (`ApprovalDecision.tsx`, proposal B's A5): read
 * before you decide, on a page of its own.
 *
 * THE SERVER HOLDS EVERY RULE. Who may approve (§4.6), a hold's stricter
 * approver (§4.4), the one-person tenant's typed confirmation (SD11), the
 * rule that only someone who may approve may reject (BGD 2026-10-26) and the
 * race between two approvers are all decided in `routes/approvals.py`; this
 * list reads `may_decide` out of each row and never widens it.
 *
 * A waiting item holds no capacity (invariant 1): nothing here is running.
 */
import { useEffect, useRef, useState, type ReactNode } from 'react'
import { loadApprovals } from './api'
import type { Result } from './fetch'
import { Button, Dash } from './components'
import { PersonName } from './PersonName'
import { Absent as AbsentState } from './primitives'
import { InApp, untilWords } from './Schedules'
import { Screen, timeAgo } from './Shell'
import { pluralise } from './types'
import { useNow } from './useNow'
import {
  APPROVAL_KIND,
  ApprovalDecisionScreen,
  BackToList,
  HOLD_WORDS,
  STATE_WORD,
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

/** A stored `approvers` value, short, for a row: the decision page says it in full. */
function approversShort(a: unknown): string | null {
  if (a === undefined || a === null || a === '') return null
  if (a === 'members') return 'any member of this workspace'
  if (a === 'owner_only') return 'the platform owner'
  if (a === 'second_member') return 'a second member'
  if (Array.isArray(a)) return a.map(String).join(', ')
  return String(a)
}

/** Who may decide a row, and whether the reader is one: `may_decide`, else the stored approvers. */
function whoCell(item: InboxItem): ReactNode {
  if (item.kind === 'input') {
    return (
      <>
        any member of this workspace<small>answered on its step</small>
      </>
    )
  }
  const md = item.may_decide ?? null
  const who = decidersWords(md) ?? approversShort(item.approvers)
  const you = md === null ? null : md.approve ? 'you may decide' : md.reject ? 'you may reject, not approve' : 'not you'
  return (
    <>
      {who ?? <Dash why="who may decide is not served" />}
      {you !== null && <small data-you={md?.approve ? 'yes' : 'no'}>{you}</small>}
    </>
  )
}

/** A row's "What": the summary, which opens the item's page, and its hold. */
function WhatCell({ item, kind, go }: { item: InboxItem; kind: string | null; go: (to: string) => void }) {
  const held = item.hold?.code !== undefined ? HOLD_WORDS[item.hold.code] : undefined
  return (
    <>
      <InApp go={go} to={approvalAddress(item.approval_id, kind)} className="apl-open">
        {item.summary}
      </InApp>
      {held !== undefined && <small>{held}</small>}
    </>
  )
}

const kindCell = (item: InboxItem) => <span className="apl-kind">{APPROVAL_KIND[item.kind] ?? item.kind}</span>

/** A small table: the column heads, then a row per item. Each cell names its column for the phone's stacked rows. */
function Table({ label, heads, rows }: { label: string; heads: string[]; rows: { id: string; cells: ReactNode[] }[] }) {
  return (
    <table className="apl-t" aria-label={label}>
      <thead>
        <tr>
          {heads.map((h) => (
            <th key={h} scope="col">
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.id} data-approval={r.id}>
            {r.cells.map((c, i) => (
              <td key={heads[i]} data-l={heads[i]}>
                {c}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function Card({ title, note, children, testId }: { title: string; note: string; children: ReactNode; testId: string }) {
  return (
    <section className="apl-card" aria-label={title} data-testid={testId}>
      <div className="apl-card-h">
        <h2>{title}</h2>
        <span>{note}</span>
      </div>
      {children}
    </section>
  )
}

/** The lead: how many wait, and when the oldest expires. */
function Lead({ waiting, kind, go }: { waiting: InboxItem[]; kind: string | null; go: (to: string) => void }) {
  const now = useNow()
  const oldest = waiting[0]!
  const expiry =
    oldest.expires_at === null ? (
      <>never expires: a run made by hand waits as it always has.</>
    ) : (
      <>
        expires <b title={oldest.expires_at}>{untilWords(oldest.expires_at, now)}</b> ({when(oldest.expires_at)}).
      </>
    )
  return (
    <section className="apl-lead" data-testid="apl-lead">
      <div className="apl-lead-t">
        <span className="apl-lead-k">Waiting</span>
        <b data-testid="apl-count">
          {pluralise(waiting.length, 'item')} {waiting.length === 1 ? 'waits' : 'wait'} for a decision.
        </b>
        <span data-testid="apl-oldest">
          The oldest, {oldest.summary}, {expiry}
        </span>
        <span className="apl-lead-s">Waiting items hold no capacity. An item nobody decides expires; a merge that expires leaves its pull request open.</span>
      </div>
      <div className="apl-lead-a">
        <InApp go={go} to={approvalAddress(oldest.approval_id, kind)} className="c-btn is-primary">
          Open the oldest
        </InApp>
      </div>
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

/**
 * THE LIST. The read serves waiting items; an item it serves in another state
 * is drawn in the Decided or the Expired-or-superseded card, and a card with
 * nothing served in it is not drawn at all, rather than drawn as an empty
 * table that reads as "nothing was decided".
 */
function ApprovalsList({ items, every, kind, go }: { items: InboxItem[]; every: InboxItem[] | null; kind: string | null; go: (to: string) => void }) {
  const now = useNow()
  const waiting = items.filter(isWaiting)
  // NEWEST FIRST, AND SAID SO (visual QA pass 2, automate2-08). The note read
  // `as the read served them`, how the rows arrived rather than their order;
  // the route serves the inbox by `requested_at`, oldest first, so the card
  // orders its own rows by when they were decided. One with no decision time
  // goes last.
  const decided = items
    .filter((i) => i.state === 'approved' || i.state === 'rejected')
    .sort((a, b) => (Date.parse(b.decided_at ?? '') || 0) - (Date.parse(a.decided_at ?? '') || 0))
  const ended = items.filter((i) => !isWaiting(i) && i.state !== 'approved' && i.state !== 'rejected')
  const at = (iso: string | null | undefined) => (iso ? <span title={iso}>{when(iso)}</span> : <Dash why="not served" />)
  return (
    <>
      {waiting.length > 0 && <Lead waiting={waiting} kind={kind} go={go} />}
      <KindFilter kind={kind} items={items} every={every} go={go} />
      {waiting.length === 0 ? (
        <EmptyInbox kind={kind} elsewhere={every === null ? null : every.filter(isWaiting).length} go={go} />
      ) : (
        <Card title={`Waiting · ${waiting.length}`} note="oldest first" testId="apl-waiting">
          <Table
            label="Waiting, oldest first"
            heads={['Kind', 'What', 'Asked', 'Expires in', 'Who may decide']}
            rows={waiting.map((i) => ({
              id: i.approval_id,
              cells: [
                kindCell(i),
                <WhatCell key="what" item={i} kind={kind} go={go} />,
                i.requested_at === null ? <Dash why="not served" /> : <span title={i.requested_at}>{timeAgo(i.requested_at, now)}</span>,
                i.expires_at === null ? (
                  <>
                    never<small>a run made by hand waits</small>
                  </>
                ) : (
                  <>
                    <b title={i.expires_at}>{untilWords(i.expires_at, now)}</b>
                    <small>{when(i.expires_at)}</small>
                  </>
                ),
                whoCell(i),
              ],
            }))}
          />
        </Card>
      )}
      {decided.length > 0 && (
        <Card title={`Decided · ${decided.length}`} note="newest first" testId="apl-decided">
          <Table
            label="Decided"
            heads={['Kind', 'What', 'Decision', 'By', 'When', 'Reason']}
            rows={decided.map((i) => ({
              id: i.approval_id,
              cells: [
                kindCell(i),
                <WhatCell key="what" item={i} kind={kind} go={go} />,
                <span key="d" className={`apl-state is-${i.state}`}>
                  {STATE_WORD[i.state] ?? i.state}
                </span>,
                i.decided_by ? <PersonName email={i.decided_by} name={i.decided_by_name} /> : <Dash why="who decided is not served" />,
                at(i.decided_at),
                i.reason ? <q>{i.reason}</q> : <span className="apl-dim">{i.state === 'approved' ? 'an approval carries no reason' : 'no reason served'}</span>,
              ],
            }))}
          />
        </Card>
      )}
      {ended.length > 0 && (
        <Card title={`Expired or superseded · ${ended.length}`} note="nobody decided these" testId="apl-ended">
          <Table
            label="Expired or superseded"
            heads={['Kind', 'What', 'Ended', 'When', 'Why']}
            rows={ended.map((i) => ({
              id: i.approval_id,
              cells: [
                kindCell(i),
                <WhatCell key="what" item={i} kind={kind} go={go} />,
                <span key="e" className={`apl-state is-${i.state}`}>
                  {STATE_WORD[i.state] ?? i.state}
                </span>,
                at(i.decided_at ?? (i.state === 'expired' ? i.expires_at : null)),
                i.state === 'expired'
                  ? i.kind === 'merge'
                    ? 'nobody decided in time; the pull request stays open'
                    : 'nobody decided in time, so it did not go ahead'
                  : i.state === 'superseded'
                    ? 'a newer request replaced it'
                    : (i.reason ?? <Dash why="not served" />),
              ],
            }))}
          />
        </Card>
      )}
    </>
  )
}

/**
 * A measured empty inbox (A6d): said, never an empty table. An empty kind
 * says how many wait under the other kinds, from the whole inbox's read
 * (`elsewhere`), and links to Every kind (automate-05): "Nothing waiting"
 * alone, beside a spine badge of 3, read as an empty inbox.
 */
function EmptyInbox({ kind, elsewhere, go }: { kind: string | null; elsewhere: number | null; go: (to: string) => void }) {
  return (
    <AbsentState
      kind="zero"
      heading="Nothing waiting"
      say={kind === null ? 'The inbox was read and nothing is waiting' : `The inbox was read and no ${APPROVAL_KIND[kind] ?? kind} item is waiting`}
    >
      {kind === null ? (
        `No ${EVERY_KIND} in this workspace is waiting for a person: the inbox was read and is empty.`
      ) : elsewhere !== null && elsewhere > 0 ? (
        <span data-testid="apl-elsewhere">
          Nothing of this kind is waiting for a person. {pluralise(elsewhere, 'item')} {elsewhere === 1 ? 'waits' : 'wait'} under other kinds.{' '}
          <InApp go={go} to={approvalsAddress(null)}>
            Read every kind ›
          </InApp>
        </span>
      ) : elsewhere === 0 ? (
        'Nothing of any kind is waiting for a person: the whole inbox was read and is empty.'
      ) : (
        'Nothing of this kind is waiting for a person. Choose Every kind to read the whole inbox.'
      )}
    </AbsentState>
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
        const r = await loadApprovals(kind === null ? {} : { kind })
        return r.status === 'empty' ? { status: 'ok' as const, data: { approvals: [] }, fetchedAt: r.fetchedAt } : r
      }}
    >
      {(d) => {
        const item = (d.approvals as InboxItem[]).find((i) => i.approval_id === itemId)
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
  const [r, all] = await Promise.all([loadApprovals(kind === null ? {} : { kind }), kind === null ? null : loadApprovals()])
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
