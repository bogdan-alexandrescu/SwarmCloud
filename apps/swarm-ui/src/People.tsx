import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from 'react'
import {
  approveWorkspace,
  denyWorkspace,
  grantAdmin,
  loadPeople,
  removeAdmin,
  retryWorkspace,
  setWorkspaceCeiling,
  setWorkspaceLoan,
} from './api'
import { Banner, Button, Card, Chip, Dash, Dialog, ToneMark } from './components'
import type { ApiError, Result } from './fetch'
import { ApprovedWaiting, JobSteps, waitingOf } from './OnboardingWorkspace'
import { Mark } from './primitives'
import { UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import { PageHead, usePoll } from './Shell'
import { tableMode, usePhoneTables } from './capacityPoll'
import type { AdminAuditEntry, AdminHolder, LendableAccount, PeopleDoc, PersonClaudeAccount, PersonRow, WorkspaceState } from './types'
import { timeAgo } from './types'
import './styles/repositories.css'
import './styles/onboarding.css'

/**
 * ADMIN › PEOPLE (#847, lane W8; docs/workspaces.md §6.4, the admin half of
 * §6.5). Admins only: every route here is `/v1/admin/*` behind `admin_auth`,
 * and a non-admin's read is answered 403, which the region draws as the admin
 * gate with nothing under it. The four parts the owner decided for v1:
 *
 *   1. everyone who has signed in, one row each: teams, GitHub, the
 *      workspace's state and id, where their Claude account comes from, and
 *      when they were last active;
 *   2. pending requests first, with Approve (one confirmation, then the job's
 *      steps live in the row) and Deny (a reason, shown to the person); a
 *      `needs_owner` row says it waits for the platform owner and at which
 *      step; a `failed` row has Retry;
 *   3. each person's ceiling (`{max_active}` and nothing else: the API turns
 *      it into the quota by §8's ratio) and lending or reclaiming a Claude
 *      account from the ones the API says this admin may lend;
 *   4. granting and removing admin, with the API's refusal for the owner and
 *      the last admin shown as it came.
 *
 * Under the table, the last 50 admin_audit entries, a run of one action as one line.
 *
 * A PERSON IS ADDRESSED BY WORKSPACE ID in every URL this screen requests,
 * never by an email or a tenant id, so a URL in a history or a proxy log
 * names nobody. The admins routes are the one exception, because the API
 * defines them by email (§6.5).
 *
 * EVERY FIGURE IS THE API'S. The list's order is the server's (pending first,
 * then most recently active), kept by a stable sort that only lifts
 * `requested` rows. While any row is approved or applying, the list is read
 * again every 5 s so its steps move; at rest it is read once per visit.
 *
 * AN APPROVAL NOTHING WILL BUILD IS NOT IN FLIGHT (2026-10-10, w-752763). With
 * workspace building off in the deployment, one banner says how many approved
 * requests wait on it and where it is switched on; their rows say they wait,
 * draw no live step rows, and are not polled for, since nothing moves them.
 */

/** While a run is under way, how often the list is read (as the setup checklist reads a person's record). */
export const PEOPLE_POLL_MS = 5_000

/** §6.4's default ceiling, for a row whose record carries no limits. */
const DEFAULT_CEILING = 8

const IN_FLIGHT: ReadonlySet<WorkspaceState> = new Set(['approved', 'applying'])

const STATE_TONE: Readonly<Record<WorkspaceState, string>> = {
  none: 'unknown',
  requested: 'live',
  approved: 'live',
  applying: 'live',
  needs_owner: 'warn',
  ready: 'ok',
  denied: 'info',
  failed: 'bad',
}

const STATE_WORD: Readonly<Record<WorkspaceState, string>> = {
  none: 'not requested',
  requested: 'requested',
  approved: 'approved',
  applying: 'setting up',
  needs_owner: 'waiting for the platform owner',
  ready: 'ready',
  denied: 'denied',
  failed: 'failed',
}

/** §4.2's console labels, for the step a guard stopped at or a failure names. */
const STEP_LABEL: Readonly<Record<string, string>> = {
  A1: 'Approved',
  A2: 'Checking the name is free',
  A3: 'Identity',
  A4: 'Identity',
  A5: 'Access',
  A6: 'Access',
  A7: 'Namespace',
  A8: 'Limits',
  A9: 'Final check',
}

function stepName(id: string | null | undefined): string | null {
  if (typeof id !== 'string' || id === '') return null
  return STEP_LABEL[id] ?? id
}

/** Requested rows first, the server's order otherwise. `Array.prototype.sort` is stable. */
export function pendingFirst(rows: readonly PersonRow[]): PersonRow[] {
  return [...rows].sort((a, b) => Number(b.workspace.state === 'requested') - Number(a.workspace.state === 'requested'))
}

function claudeText(c: PersonClaudeAccount): ReactNode {
  if (c.source === 'own') return `own (${c.own})`
  if (c.source === 'lent') return c.lent_by.length > 0 ? `lent by ${c.lent_by.join(', ')}` : `lent (${c.lent})`
  if (c.source === 'provider_key') return 'provider key'
  return 'none'
}

/** One write's outcome, said in the row it was made from. */
type Said = { ok: true; text: string } | { ok: false; error: ApiError }

function SaidLine({ said }: { said: Said | null }) {
  if (said === null) return null
  if (said.ok) return <p className="ur-hint" role="status">{said.text}</p>
  return (
    <p className="ur-small ob-issue" role="alert" data-code={said.error.code ?? undefined}>
      {said.error.code !== null && <b className="ur-bad">{said.error.code}</b>} {said.error.message}
    </p>
  )
}

function outcome<T>(r: Result<T>, text: (data: T) => string): Said {
  if (r.status === 'error') return { ok: false, error: r.error }
  if (r.status === 'ok' || r.status === 'stale') return { ok: true, text: text(r.data) }
  return { ok: true, text: 'Done.' }
}

function Approve({ row, reload }: { row: PersonRow; reload: () => void }) {
  const id = row.workspace.workspace_id ?? ''
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [said, setSaid] = useState<Said | null>(null)
  const ceiling = row.workspace.limits?.max_active ?? DEFAULT_CEILING
  const go = async () => {
    if (busy) return
    setBusy(true)
    const r = await approveWorkspace(id)
    setBusy(false)
    setOpen(false)
    setSaid(
      outcome(r, (d) =>
        // The API's own sentence when it sends one: it is the one that knows
        // whether anything will build this workspace.
        typeof d.message === 'string' && d.message !== ''
          ? d.message
          : d.dispatch?.published === false
            ? `Approved. The job was not started yet (${d.dispatch.reason ?? 'not published'}); the dispatch sweep retries it.`
            : 'Approved. The job is starting.',
      ),
    )
    reload()
  }
  return (
    <>
      <Button size="sm" kind="primary" onClick={() => setOpen(true)}>
        Approve
      </Button>
      {open && (
        <Dialog
          title="Approve this workspace"
          onClose={() => setOpen(false)}
          actions={
            <>
              <Button onClick={() => setOpen(false)}>Cancel</Button>
              <Button kind="primary" disabled={busy} onClick={() => void go()}>
                {busy ? 'Approving…' : 'Approve'}
              </Button>
            </>
          }
        >
          <p>
            Create a workspace for {row.email} with {ceiling} agents? A CI job will create its identity and namespace.
          </p>
        </Dialog>
      )}
      <SaidLine said={said} />
    </>
  )
}

function Deny({ row, reload }: { row: PersonRow; reload: () => void }) {
  const id = row.workspace.workspace_id ?? ''
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [said, setSaid] = useState<Said | null>(null)
  const blank = reason.trim() === ''
  // FOCUS FOLLOWS THE FORM (visual QA V028): opening Deny puts the caret in
  // the reason, and Cancel (or a sent denial) hands focus back to Deny, so a
  // keyboard user is never dropped on the page's body. Not on first mount.
  const denyId = useId()
  const reasonRef = useRef<HTMLTextAreaElement>(null)
  const wasOpen = useRef(open)
  useEffect(() => {
    if (wasOpen.current === open) return
    wasOpen.current = open
    if (open) reasonRef.current?.focus()
    else document.getElementById(denyId)?.focus()
  }, [open])
  const go = async () => {
    // A reason is required and shown to the person (§1.3): a blank one is not sent.
    if (busy || blank) return
    setBusy(true)
    const r = await denyWorkspace(id, reason.trim())
    setBusy(false)
    if (r.status !== 'error') {
      setOpen(false)
      setReason('')
    }
    setSaid(outcome(r, () => 'Denied. The person sees the reason.'))
    reload()
  }
  if (!open) {
    return (
      <>
        <Button size="sm" id={denyId} onClick={() => setOpen(true)}>
          Deny
        </Button>
        <SaidLine said={said} />
      </>
    )
  }
  return (
    <span className="pp-deny">
      <label className="c-field">
        <span className="c-lbl">Reason, shown to {row.email}</span>
        <textarea ref={reasonRef} className="c-inp" value={reason} maxLength={500} rows={2} onChange={(e) => setReason(e.target.value)} />
      </label>
      <span className="ob-acts">
        <Button size="sm" onClick={() => setOpen(false)}>
          Cancel
        </Button>
        <Button size="sm" kind="primary" disabled={blank || busy} onClick={() => void go()}>
          {busy ? 'Denying…' : 'Deny'}
        </Button>
      </span>
      <SaidLine said={said} />
    </span>
  )
}

function Retry({ row, reload }: { row: PersonRow; reload: () => void }) {
  const [busy, setBusy] = useState(false)
  const [said, setSaid] = useState<Said | null>(null)
  const go = async () => {
    if (busy) return
    setBusy(true)
    const r = await retryWorkspace(row.workspace.workspace_id ?? '')
    setBusy(false)
    setSaid(outcome(r, () => 'Retried. The job is starting again.'))
    reload()
  }
  return (
    <>
      <Button size="sm" disabled={busy} onClick={() => void go()}>
        {busy ? 'Retrying…' : 'Retry'}
      </Button>
      <SaidLine said={said} />
    </>
  )
}

/** `-12%`, `+50%`: the change a ceiling edit makes, as Pool limits says it. */
export function percentChange(from: number, to: number): string {
  if (from === 0) return to === 0 ? '0%' : 'from 0'
  const p = Math.round(((to - from) / from) * 100)
  return `${p > 0 ? '+' : p < 0 ? '−' : ''}${Math.abs(p)}%`
}

function Ceiling({ row, reload }: { row: PersonRow; reload: () => void }) {
  const held = row.workspace.limits?.max_active ?? DEFAULT_CEILING
  const [value, setValue] = useState(String(held))
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [said, setSaid] = useState<Said | null>(null)
  const n = Number(value)
  const whole = value.trim() !== '' && Number.isInteger(n) && n >= 1
  // ONE VALUE, ONE KIND OF EDIT (live QA ADM-3, 2026-10-11): a person's
  // ceiling is the same act as editing their tenant's pool on Pool limits,
  // which shows old → new before it writes. So Save asks first, with both
  // values and the change, and an unchanged value has nothing to save.
  const changed = whole && n !== held
  const go = async () => {
    if (busy || !changed) return
    setBusy(true)
    const r = await setWorkspaceCeiling(row.workspace.workspace_id ?? '', n)
    setBusy(false)
    setOpen(false)
    setSaid(outcome(r, (d) => `Ceiling ${d.workspace.limits?.max_active ?? n} saved.`))
    reload()
  }
  return (
    <span className="pp-ceiling">
      <label className="c-field">
        <span className="c-lbl">Ceiling</span>
        <input className="c-inp" type="number" min={1} step={1} value={value} onChange={(e) => setValue(e.target.value)} aria-label={`Ceiling for ${row.email}`} />
      </label>
      <Button size="sm" disabled={busy || !changed} onClick={() => setOpen(true)}>
        Save
      </Button>
      {open && (
        <Dialog
          title="Change this ceiling"
          onClose={() => setOpen(false)}
          actions={
            <>
              <Button onClick={() => setOpen(false)}>Keep {held}</Button>
              <Button kind="primary" disabled={busy} onClick={() => void go()}>
                {busy ? 'Saving…' : `Set to ${n}`}
              </Button>
            </>
          }
        >
          <p>
            Change {row.email}&apos;s ceiling from {held} to {n} active agents?
          </p>
          <p className="ur-hint" data-testid="pp-ceiling-change">
            <b>
              {held} → {n}
            </b>{' '}
            · {percentChange(held, n)}
          </p>
          <p className="ur-hint">
            {n < held
              ? 'A lower ceiling admits less of their work at once; what is already admitted is not stopped.'
              : 'A higher ceiling admits more of their work at once, inside the pools above it.'}{' '}
            The API turns the ceiling into the workspace&apos;s quota.
          </p>
        </Dialog>
      )}
      <SaidLine said={said} />
    </span>
  )
}

/** Account states in which a lent account starts no new agent (the broker's `AccountState`). */
const NOT_ASSIGNING: ReadonlySet<string> = new Set(['PAUSED', 'DRAINING', 'REAUTH_REQUIRED'])

/**
 * Whether `account` is lent to this person, as far as the API lets the
 * console see. The admin view leaves the person's tenant id out, so the
 * account's `lend_to` cannot be matched against it here; `lent_by` is the
 * API's list of the owner tenants whose accounts name this person's tenant
 * in `lend_to`, so an account whose owner is not in it is not lent to them.
 */
export function lentToRow(account: LendableAccount, row: PersonRow): boolean {
  return Array.isArray(row.claude_account.lent_by) && row.claude_account.lent_by.includes(account.owner_tenant)
}

function Loan({ row, lendable, lendableError, reload }: { row: PersonRow; lendable: LendableAccount[] | null; lendableError: string | null; reload: () => void }) {
  const [account, setAccount] = useState('')
  // true: asking to lend; false: asking to reclaim; null: not asking.
  const [asking, setAsking] = useState<boolean | null>(null)
  const [busy, setBusy] = useState(false)
  const [said, setSaid] = useState<Said | null>(null)
  if (lendable === null) {
    return (
      <p className="ur-hint">
        <Mark kind="unread" say="The accounts this admin may lend were not read" /> {lendableError ?? 'the broker did not answer'}
      </p>
    )
  }
  if (lendable.length === 0) {
    // A ZERO IN A SENTENCE READS AS WORDS (owner, 2026-10-09): `[real zero]
    // accounts to lend` was a table mark set mid-prose. The same rule writes
    // this screen's other empty lines and the `none pending` chip; that the
    // zero was measured stays in each one's accessible name.
    return (
      <p className="ur-hint">
        <span aria-label="no accounts to lend (measured)">no accounts to lend</span>
      </p>
    )
  }
  const chosen = lendable.find((a) => a.account_id === account) ?? null
  // NEITHER WRITES ON THE FIRST CLICK (live QA ADM-1, 2026-10-11): lending
  // hands one tenant's Claude account to another person's workspace, and
  // reclaiming takes it back from under their runs. Each asks first, naming
  // the account, its owner tenant and the person.
  // RECLAIM ONLY WHAT IS LENT (ADM-2): an account not lent to this person
  // has nothing to reclaim, so Reclaim stays off for it.
  const reclaimable = chosen !== null && lentToRow(chosen, row)
  const go = async (lend: boolean) => {
    if (busy || chosen === null) return
    setBusy(true)
    const r = await setWorkspaceLoan(row.workspace.workspace_id ?? '', chosen.account_id, lend)
    setBusy(false)
    setAsking(null)
    setSaid(
      outcome(r, (d) =>
        d.changed ? (d.lent ? `Lent ${d.account_id}.` : `Reclaimed ${d.account_id}.`) : d.lent ? `${d.account_id} was already lent.` : `${d.account_id} was not lent.`,
      ),
    )
    reload()
  }
  const name = chosen === null ? '' : `${chosen.label ?? chosen.account_id} (${chosen.owner_tenant})`
  const idle = chosen !== null && chosen.state !== null && NOT_ASSIGNING.has(chosen.state)
  return (
    <span className="pp-loan">
      {row.loan_request?.state === 'requested' && (
        <Chip>loan requested{row.loan_request.requested_at !== null ? ` ${timeAgo(row.loan_request.requested_at)}` : ''}</Chip>
      )}
      <label className="c-field">
        <span className="c-lbl">Account</span>
        <select className="c-inp" value={account} onChange={(e) => setAccount(e.target.value)} aria-label={`Account to lend to ${row.email}`}>
          <option value="">choose…</option>
          {lendable.map((a) => (
            <option key={a.account_id} value={a.account_id}>
              {a.label ?? a.account_id} · {a.owner_tenant}
              {a.state !== null ? ` · ${a.state}` : ''}
              {a.state !== null && NOT_ASSIGNING.has(a.state) ? ' (starts no new agent)' : ''}
              {lentToRow(a, row) ? ' · lent to them' : ''}
            </option>
          ))}
        </select>
      </label>
      <Button size="sm" disabled={busy || chosen === null} onClick={() => setAsking(true)}>
        Lend
      </Button>
      {/* Lend and Reclaim are a pair: the same kind, so a disabled pair
          looks alike (visual QA V080: a ghost Reclaim lost its border). */}
      <Button
        size="sm"
        disabled={busy || !reclaimable}
        title={chosen !== null && !reclaimable ? `${name} is not lent to ${row.email}` : undefined}
        onClick={() => setAsking(false)}
      >
        Reclaim
      </Button>
      {asking !== null && chosen !== null && (
        <Dialog
          title={asking ? 'Lend this account' : 'Reclaim this account'}
          onClose={() => setAsking(null)}
          actions={
            <>
              <Button onClick={() => setAsking(null)}>Cancel</Button>
              <Button kind="primary" disabled={busy} onClick={() => void go(asking)}>
                {busy ? (asking ? 'Lending…' : 'Reclaiming…') : asking ? 'Lend' : 'Reclaim'}
              </Button>
            </>
          }
        >
          <p>{asking ? `Lend ${name} to ${row.email}?` : `Reclaim ${name} from ${row.email}?`}</p>
          <p className="ur-hint">
            {asking
              ? `Their work may then run on an account ${chosen.owner_tenant} owns.`
              : `${chosen.owner_tenant} stops lending it to them; their new work is no longer offered it.`}
          </p>
          {asking && idle && (
            <p className="ur-hint" role="note" data-testid="pp-loan-idle">
              This account is {chosen.state}: it starts no new agent until its owner makes it available again.
            </p>
          )}
        </Dialog>
      )}
      <SaidLine said={said} />
    </span>
  )
}

function WorkspaceCell({ row }: { row: PersonRow }) {
  const w = row.workspace
  const failedAt = stepName(w.failure?.step)
  const heldAt = stepName(w.needs_owner_step)
  return (
    <div className="pp-ws" data-workspace-state={w.state}>
      <ToneMark tone={STATE_TONE[w.state] ?? 'unknown'}>{STATE_WORD[w.state] ?? w.state}</ToneMark>
      {w.workspace_id != null && w.workspace_id !== '' && (
        <>
          {' '}
          <code className="pp-id">{w.workspace_id}</code>
        </>
      )}
      {w.state === 'failed' && failedAt !== null && <> ({failedAt})</>}
      {w.state === 'needs_owner' && (
        <p className="ur-hint">
          Waiting for the platform owner{heldAt !== null ? ` · stopped at ${heldAt}` : ''}
        </p>
      )}
      {w.state === 'denied' && w.decision?.reason != null && <p className="ur-hint">“{w.decision.reason}”</p>}
      {w.state === 'requested' && w.held != null && (
        <p className="ur-hint">Being migrated by the platform owner; not approved here</p>
      )}
      {w.state !== 'denied' && w.decision?.auto === true && <p className="ur-hint">approved automatically (admin)</p>}
      {waitingOf(w) !== null ? (
        <ApprovedWaiting record={w} />
      ) : (
        (IN_FLIGHT.has(w.state) || w.state === 'needs_owner') && <JobSteps record={w} />
      )}
    </div>
  )
}

function Row({ row, doc, reload }: { row: PersonRow; doc: PeopleDoc; reload: () => void }) {
  const w = row.workspace
  const hasRecord = typeof w.workspace_id === 'string' && w.workspace_id !== ''
  return (
    <tr role="row" data-email={row.email} data-workspace-state={w.state}>
      <th role="rowheader" scope="row">
        <span className="pp-who">{row.email}</span>
      </th>
      <td role="cell" data-label="Teams">
        {row.teams.length > 0 ? row.teams.join(', ') : <Dash why="Resolves to no team" />}
      </td>
      <td role="cell" data-label="GitHub">
        <span className="pp-who">{row.github}</span>
      </td>
      <td role="cell" data-label="Workspace">
        <WorkspaceCell row={row} />
      </td>
      <td role="cell" data-label="Claude account">
        <div>
          {claudeText(row.claude_account)}
          {row.loan_request?.state === 'requested' && <p className="ur-hint">loan requested</p>}
        </div>
      </td>
      <td role="cell" data-label="Last active">
        {row.last_active !== null ? timeAgo(row.last_active) : <Dash why="Not seen active" />}
      </td>
      <td role="cell" data-label="Actions" className="pp-acts-cell">
        {/* THE ACTIONS FLOW IN THEIR CELL (visual QA V080, after 2026-10-09's
            stack made every row 160-200px tall): the groups sit side by side
            and wrap only when the cell is too narrow, no control wider than
            the cell, so Deny and the Account select stay inside the card. */}
        <div className="pp-acts">
          {/* A held record predates self-service setup (§3.3): its job would
              fail IDENTITY_NOT_OURS (WORKSPACE_MIGRATING once that refusal is
              switched on), so no approval is offered. */}
          {hasRecord && w.state === 'requested' && w.held == null && (
            <span className="ob-acts">
              <Approve row={row} reload={reload} />
              <Deny row={row} reload={reload} />
            </span>
          )}
          {/* An admin may approve a denied record at any time, inside the
              person's 24-hour wait too (§1.3, confirmed by the owner). */}
          {hasRecord && w.state === 'denied' && (
            <span className="ob-acts">
              <Approve row={row} reload={reload} />
            </span>
          )}
          {hasRecord && w.state === 'failed' && (
            <span className="ob-acts">
              <Retry row={row} reload={reload} />
              <Deny row={row} reload={reload} />
            </span>
          )}
          {hasRecord && <Ceiling row={row} reload={reload} />}
          {hasRecord && <Loan row={row} lendable={doc.lendable_accounts} lendableError={doc.lendable_error} reload={reload} />}
        </div>
      </td>
    </tr>
  )
}

function AdminList({ admins, onRemove }: { admins: AdminHolder[]; onRemove: (email: string) => void }) {
  if (admins.length === 0) {
    return (
      <p className="ur-hint">
        <span aria-label="no admin role documents (measured)">no admin role documents</span>
      </p>
    )
  }
  return (
    <ul className="pp-admin-list" aria-label="Admins">
      {admins.map((a) => (
        <li key={a.email} data-email={a.email} data-role={a.role}>
          <span className="pp-admin-who">
            <b>{a.email}</b>
            {a.role === 'owner' && <Chip className="pp-owner">platform owner</Chip>}
          </span>
          <span className="ur-hint">
            {a.role === 'owner'
              ? 'set by PLATFORM_OWNER in configuration'
              : `granted by ${a.granted_by ?? 'unknown'}${a.granted_at !== null ? ` ${timeAgo(a.granted_at)}` : ''}`}
          </span>
          {/* The owner is not removable here: the API refuses it
              (OWNER_FROM_CONFIG), so no control offers it. */}
          {a.role !== 'owner' && (
            <Button size="sm" onClick={() => onRemove(a.email)} title={`Remove admin from ${a.email}`}>
              Remove admin
            </Button>
          )}
        </li>
      ))}
    </ul>
  )
}

function Admins({ doc, reload }: { doc: PeopleDoc; reload: () => void }) {
  const [email, setEmail] = useState('')
  const [busy, setBusy] = useState(false)
  const [target, setTarget] = useState<string | null>(null)
  const [granting, setGranting] = useState<string | null>(null)
  const [said, setSaid] = useState<Said | null>(null)
  const typed = email.trim()
  // A GRANT IS A FORM, CHECKED AND CONFIRMED (live QA ADM-4, 2026-10-11):
  // `not-an-email` was sent as a grant, Enter did nothing, and a valid
  // address became a platform admin on one click. The field's own email
  // validity gates the button and the submit, and the grant asks first, as
  // Remove does.
  const fieldRef = useRef<HTMLInputElement>(null)
  const [valid, setValid] = useState(false)
  const ask = (e: FormEvent) => {
    e.preventDefault()
    if (busy || typed === '' || fieldRef.current?.checkValidity() === false) return
    setGranting(typed)
  }
  // An API that predates the list sends no `admins` at all; it is drawn as not
  // read, and the typed Remove stays, so the action is never lost with it.
  const admins = Array.isArray(doc.admins) ? doc.admins : null
  const grant = async () => {
    if (busy || granting === null) return
    setBusy(true)
    const r = await grantAdmin(granting)
    setBusy(false)
    setGranting(null)
    setSaid(outcome(r, (d) => (d.changed ? `${d.email} is now an admin.` : `${d.email} was already an admin.`)))
    reload()
  }
  const remove = async () => {
    if (busy || target === null) return
    setBusy(true)
    const r = await removeAdmin(target)
    setBusy(false)
    setTarget(null)
    setSaid(outcome(r, (d) => `${d.email} is no longer an admin.`))
    reload()
  }
  return (
    <Card className="pp-admins" title="Admins">
      <p className="ur-hint">
        Any admin may grant or remove admin. The platform owner can be changed only by the owner, and the last admin cannot
        be removed; the API says so when it refuses. An admin through an ADMIN_GROUPS group holds no role document and is
        not listed.
      </p>
      {admins !== null ? (
        <AdminList admins={admins} onRemove={setTarget} />
      ) : (
        <p className="ur-hint">
          <Mark kind="unread" say="The admin roles were not read" /> {doc.admins_error ?? 'the admin roles were not served'}
        </p>
      )}
      <form className="ob-acts pp-grant" onSubmit={ask} noValidate>
        <label className="c-field">
          <span className="c-lbl">Email</span>
          <input
            ref={fieldRef}
            className="c-inp"
            type="email"
            required
            value={email}
            onChange={(e) => {
              setEmail(e.target.value)
              setValid(e.target.value.trim() !== '' && e.target.checkValidity())
            }}
          />
        </label>
        <Button size="sm" type="submit" disabled={busy || !valid}>
          Grant admin
        </Button>
        {admins === null && (
          // Bordered like Grant admin beside it (visual QA V082): two actions
          // on one email are one pair, not an action and a stray word.
          <Button size="sm" disabled={busy || typed === ''} onClick={() => setTarget(typed)}>
            Remove admin
          </Button>
        )}
      </form>
      {typed !== '' && !valid && <p className="ur-hint">Grant admin takes one email address.</p>}
      {granting !== null && (
        <Dialog
          title="Grant admin"
          onClose={() => setGranting(null)}
          actions={
            <>
              <Button onClick={() => setGranting(null)}>Cancel</Button>
              <Button kind="primary" disabled={busy} onClick={() => void grant()}>
                {busy ? 'Granting…' : 'Grant admin'}
              </Button>
            </>
          }
        >
          <p>Make {granting} a platform admin?</p>
          <p className="ur-hint">
            An admin can approve workspaces, change every ceiling and pool limit, lend accounts and grant admin to others.
          </p>
        </Dialog>
      )}
      {target !== null && (
        <Dialog
          title="Remove admin"
          onClose={() => setTarget(null)}
          actions={
            <>
              <Button onClick={() => setTarget(null)}>Cancel</Button>
              <Button kind="primary" disabled={busy} onClick={() => void remove()}>
                Remove
              </Button>
            </>
          }
        >
          <p>Remove admin from {target}?</p>
        </Dialog>
      )}
      <SaidLine said={said} />
    </Card>
  )
}

/** The audit fields a task-scoped entry carries (`purge.py`'s `history_purged`), beside the typed ones. */
type AuditEntry = AdminAuditEntry & { target_kind?: string | null; target_id?: string | null }

/** What an entry acted on: a workspace id, an email, or another target's own id. */
export function auditTarget(e: AuditEntry): string | null {
  for (const t of [e.target_workspace_id, e.target_email, e.target_id]) {
    if (typeof t === 'string' && t !== '') return t
  }
  return null
}

function auditTenant(e: AuditEntry): string | null {
  const t = e.detail?.tenant_id
  return typeof t === 'string' && t !== '' ? t : null
}

/** A run of consecutive entries with the same action, admin and tenant. */
export interface AuditRun {
  first: AuditEntry
  last: AuditEntry
  count: number
}

/**
 * RUNS OF ONE ACTION ARE ONE LINE (live QA ADM-5, 2026-10-11): a purge
 * writes an entry per task, and fifty identical `history_purged` lines hid
 * every other action. Consecutive entries with the same action, admin and
 * tenant collapse into one, with how many and over what time.
 */
export function auditRuns(entries: readonly AuditEntry[]): AuditRun[] {
  const out: AuditRun[] = []
  for (const e of entries) {
    const prev = out[out.length - 1]
    if (prev !== undefined && prev.first.action === e.action && prev.first.by === e.by && auditTenant(prev.first) === auditTenant(e)) {
      prev.last = e
      prev.count += 1
    } else out.push({ first: e, last: e, count: 1 })
  }
  return out
}

/** `people.AUDIT_SHOWN`: the API serves at most this many entries. */
export const AUDIT_SHOWN = 50

function Audit({ entries }: { entries: AuditEntry[] }) {
  const runs = auditRuns(entries)
  return (
    <Card className="pp-audit" title="Recent admin actions">
      {entries.length === 0 ? (
        <p className="ur-hint">
          <span aria-label="no admin actions recorded (measured)">no admin actions recorded</span>
        </p>
      ) : (
        <>
          <p className="ur-hint" data-testid="pp-audit-shown">
            {entries.length >= AUDIT_SHOWN
              ? `The latest ${entries.length} entries; older ones are not shown here.`
              : `All ${entries.length} recorded ${entries.length === 1 ? 'entry' : 'entries'}.`}
          </p>
          <ol className="pp-audit-list" aria-label="Admin audit">
            {runs.map((r, i) => {
              const tenant = auditTenant(r.first)
              const target = auditTarget(r.first)
              return (
                <li key={`${r.first.at ?? ''}:${i}`} data-count={r.count}>
                  {r.first.at !== null ? timeAgo(r.first.at) : <Dash why="The entry carries no time" />}
                  {r.count > 1 && r.last.at !== null && r.last.at !== r.first.at ? ` to ${timeAgo(r.last.at)}` : ''} ·{' '}
                  <b>{r.first.action}</b>
                  {r.count > 1 ? (
                    <> × {r.count}</>
                  ) : target !== null ? (
                    <>
                      {' '}
                      <code>{target}</code>
                    </>
                  ) : (
                    <>
                      {' '}
                      <Dash why="The entry names no target" />
                    </>
                  )}
                  {tenant !== null && <> · {tenant}</>} by {r.first.by}
                </li>
              )
            })}
          </ol>
        </>
      )}
    </Card>
  )
}

/** One banner while this deployment builds no workspaces: an approval here is kept, and nothing builds it. */
export function BuildingOff({ doc }: { doc: PeopleDoc }) {
  const p = doc.provisioning
  if (p === undefined || p.available !== false) return null
  const n = p.approved_waiting
  return (
    <Banner tone="warn" title={`Workspace building is off in this deployment: ${n} approved request${n === 1 ? '' : 's'} ${n === 1 ? 'is' : 'are'} waiting`}>
      An approval is kept and sent for building once it is switched on. Where to switch it on:{' '}
      <code>docs/workspaces.md</code> §10.
    </Banner>
  )
}

function PeopleBody({ doc, reload }: { doc: PeopleDoc; reload: () => void }) {
  const rows = pendingFirst(Array.isArray(doc.people) ? doc.people : [])
  const phone = usePhoneTables()
  return (
    <>
      <BuildingOff doc={doc} />
      <Card
        className="pp-people"
        // Not "People" again under the page's own "People" (visual QA V082).
        title="Everyone who has signed in"
        action={
          <Chip>
            {doc.count} {doc.count === 1 ? 'person' : 'people'} ·{' '}
            {doc.pending === 0 ? <span aria-label="none pending (measured)">none pending</span> : <>{doc.pending} pending</>}
          </Chip>
        }
      >
        {rows.length === 0 ? (
          <p className="ur-hint">
            <span aria-label="no one has signed in (measured)">no one has signed in</span>
          </p>
        ) : (
          /* THE TENANTS PATTERN (Activity.tsx, design-system.md §7.3): the
              table has its own scroll container inside the card, so nothing
              in a row can run past the card's edge; below 560px it is a
              record per person. NOT `.ten-table`: that is Tenants' fixed
              layout, which without Tenants' colgroup split this table into
              seven equal columns, pushed Deny and the Account select out of
              the Actions column, and broke a workspace id at its hyphen. */
          <div className={`table-wrap ${tableMode(phone)} pp-wrap`}>
            <table className="pools pp-table" role="table">
              <thead role="rowgroup">
                <tr role="row">
                  <th role="columnheader" scope="col">Person</th>
                  <th role="columnheader" scope="col">Teams</th>
                  <th role="columnheader" scope="col">GitHub</th>
                  <th role="columnheader" scope="col">Workspace</th>
                  <th role="columnheader" scope="col">Claude account</th>
                  <th role="columnheader" scope="col">Last active</th>
                  <th role="columnheader" scope="col">Actions</th>
                </tr>
              </thead>
              <tbody role="rowgroup">
                {rows.map((row) => (
                  <Row key={row.email} row={row} doc={doc} reload={reload} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      <Admins doc={doc} reload={reload} />
      <Audit entries={Array.isArray(doc.audit) ? doc.audit : []} />
    </>
  )
}

/** Admin › People. */
export function PeopleScreen() {
  const people = useUrRead(loadPeople, 'people')
  const data = people.state.status === 'ok' || people.state.status === 'stale' ? people.state.data : null
  // A row waiting on something the platform will not do by itself is not moving.
  const moving =
    data !== null &&
    Array.isArray(data.people) &&
    data.people.some((r) => IN_FLIGHT.has(r.workspace.state) && waitingOf(r.workspace) !== 'publishing_off')
  usePoll(PEOPLE_POLL_MS, people.reload, !moving)
  return (
    <div className="ur-page pp-page">
      <PageHead title="People">
        <UrRefresh reads={[people]} />
      </PageHead>
      <p className="ur-sub">
        Everyone who has signed in, their workspace requests to approve or deny, each person&apos;s ceiling and Claude
        account, and who is an admin. A person is named by workspace id in every address this page requests.
      </p>
      <UrRegion state={people.state} route="GET /v1/admin/people" what="People" plural onRetry={people.reload} lines={6}>
        {(d) => <PeopleBody doc={d} reload={people.reload} />}
      </UrRegion>
    </div>
  )
}
