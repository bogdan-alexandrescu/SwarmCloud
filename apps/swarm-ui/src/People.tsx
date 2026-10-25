import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import {
  approveWorkspace,
  clearPeopleWorkspaceRole,
  declineLoanRequest,
  denyWorkspace,
  grantAdmin,
  loadPeopleAndRoles,
  removeAdmin,
  retryWorkspace,
  setPeopleWorkspaceRole,
  setWorkspaceCeiling,
  setWorkspaceLoan,
} from './api'
import { AdminLead } from './AdminSettings'
import { Banner, Button, buttonClass, Card, Chip, Dash, Dialog, ToneMark } from './components'
import type { ApiError, Result } from './fetch'
import { trapTab } from './focus'
import { ApprovedWaiting, JobSteps, waitingOf } from './OnboardingWorkspace'
import { PersonName } from './PersonName'
import { Mark } from './primitives'
import { UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import { PageHead, usePoll } from './Shell'
import { tableMode, usePhoneTables } from './capacityPoll'
import { PersonWorkspaceRoles, WorkspaceRolesCard } from './WorkspaceRolesAdmin'
import type {
  AdminAuditEntry,
  AdminHolder,
  LendableAccount,
  PeopleDoc,
  PeopleWorkspace,
  PersonClaudeAccount,
  PersonRow,
  RoleCell,
  ScopedPeopleDoc,
  ScopedPersonRow,
  WorkspaceState,
} from './types'
import { timeAgo } from './types'
import { accountName } from './accountName'
import './styles/repositories.css'
import './styles/onboarding.css'
import './styles/people.css'

/**
 * ADMIN › PEOPLE (#847, lane W8; docs/workspaces.md §6.4, the admin half of
 * §6.5), REDRAWN AS PROPOSAL A (#1206, lane PEOPLE-A; picked by the owner
 * 2026-10-12 17:40 PDT). Admins only: every route here is `/v1/admin/*`
 * behind `admin_auth`, and a non-admin's read is answered 403, which the
 * region draws as the admin gate with nothing under it.
 *
 * WHY IT WAS REDRAWN. Measured 2026-10-12 at 1260px: the seven-column table's
 * Actions column took 653px, so `devops@example.com` wrapped as `devops@example.
 * xyz` and "connected" as "connecte d"; each row crammed Approve, Deny, a
 * ceiling field and Save, Lend, Reclaim and an Account select that wrapped
 * differently per row; and a pending request was one small green dot, so
 * w-7f9285 sat unseen from 15:31 PDT. The page is now four parts:
 *
 *   1. WAITING FOR YOU, an amber card on top: one row per open request. A
 *      workspace request shows the limits it would get, with Approve (one
 *      click: the card already says what the old confirmation said) and Deny
 *      (a reason, shown to the person). A loan request shows the pool and a
 *      picker, with Lend (which still asks: live QA ADM-1). Absent when
 *      nothing is open.
 *   2. A LEAN TABLE that holds no control but Manage, so no column is
 *      squeezed into breaking a word.
 *   3. THE MANAGE DRAWER: Facts, the ceiling, the Claude account (Lend OR
 *      Reclaim, never both, and a reason when neither) and the Admin switch.
 *   4. Admins and Recent admin actions side by side, housekeeping folded.
 *
 * WORKSPACE ROLES (lane ROLES-UI-ADMIN; docs/design/workspace-roles.md §4.8):
 * a Workspace roles card between the table and the side cards, and "Roles in
 * workspaces" in the drawer, both in WorkspaceRolesAdmin.tsx. Role events and
 * `platform_admin_*` rows are `admin_audit` rows, so Recent admin actions
 * shows them as it shows every other.
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
 * draw no live step rows, and are not polled for, since nothing moves them. *
 * PEOPLE AND ROLES (ADMIN-PEOPLE-ROLES; part of #2157; config-e.html S5c,
 * S5f, picked by BGD): renamed from People, and "every person against every
 * workspace; the lead is who is waiting for an admin". The page opens with
 * that lead (`PeopleLead`) and one button to the oldest request; the table's
 * Teams column became one ROLE COLUMN PER GROUP WORKSPACE, each cell the
 * person's role there (`RoleCell`). Approve, Deny, the ceiling, Lend and
 * Decline stay where they were: Waiting for you and each row's Manage.
 *
 * A WORKSPACE ADMIN READS IT TOO, scoped by the API (`routes/people.py
 * people_reader`): only the workspaces they administer and the people in
 * them, `scope: 'workspace_admin'`. They are drawn `ScopedPeopleBody`: the
 * lead, and a grid whose cells are role selects, because a role in their own
 * workspace is the one thing on this page that is theirs to change. Nothing
 * of the platform's is drawn for them -- no requests, ceiling, Claude account,
 * Admins, audit or Workspace roles card -- because none of it is served.
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

function hasRecord(row: PersonRow): boolean {
  return typeof row.workspace.workspace_id === 'string' && row.workspace.workspace_id !== ''
}

/** One open request an admin can decide: a workspace to approve or deny, or a Claude account to lend. */
export interface WaitingRequest {
  kind: 'workspace' | 'loan'
  row: PersonRow
  at: string | null
}

/**
 * The requests waiting on an admin, oldest first (an undated one last). A
 * HELD request is not one: it waits for the platform owner's migration
 * (§3.3) and no admin can approve it, so listing it here would ask for a
 * decision nobody on this page can make.
 */
export function waitingRequests(rows: readonly PersonRow[]): WaitingRequest[] {
  const out: WaitingRequest[] = []
  for (const row of rows) {
    if (!hasRecord(row)) continue
    const w = row.workspace
    if (w.state === 'requested' && w.held == null) out.push({ kind: 'workspace', row, at: w.requested_at ?? null })
    if (row.loan_request?.state === 'requested') out.push({ kind: 'loan', row, at: row.loan_request.requested_at })
  }
  const when = (r: WaitingRequest) => (r.at === null ? Number.POSITIVE_INFINITY : new Date(r.at).getTime() || Number.POSITIVE_INFINITY)
  return out.sort((a, b) => when(a) - when(b))
}

/**
 * An account as the console names it (`accountName`: its Name, else its
 * label), or its id when it has neither. The id is not the Name: a lent
 * account without a label is served `label: null`.
 */
function named(a: { account_id: string; label: string | null; display_name?: string | null }): string {
  return accountName({ label: a.label ?? '', display_name: a.display_name }) || a.account_id
}

/** An account with its id: the id, then how the console names it when that is something else. */
function accountWithId(a: { account_id: string; label: string | null; display_name?: string | null }): string {
  const name = named(a)
  return name !== a.account_id ? `${a.account_id} (${name})` : a.account_id
}

/**
 * WHICH ACCOUNT, THEN WHO LENT IT (owner, 2026-10-13): "lent by eng" named
 * the owner tenant and never the account. An older API without
 * `lent_accounts` still says its owners.
 */
function claudeText(c: PersonClaudeAccount): ReactNode {
  if (c.source === 'own') return `own (${c.own})`
  if (c.source === 'lent' && c.lent_accounts !== undefined && c.lent_accounts.length > 0) {
    return c.lent_accounts.map((a) => `${accountWithId(a)}, lent by ${a.owner_name ?? a.owner_tenant}`).join('; ')
  }
  if (c.source === 'lent') return c.lent_by.length > 0 ? `lent by ${c.lent_by.join(', ')}` : `lent (${c.lent})`
  if (c.source === 'provider_key') return 'provider key'
  return 'none'
}

/** The admin role document for this email, if the roles were read and one exists. */
function holderOf(doc: PeopleDoc, email: string): AdminHolder | undefined {
  return Array.isArray(doc.admins) ? doc.admins.find((a) => a.email === email) : undefined
}

/** One write's outcome, said where it was made from. */
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

/**
 * Where a control says its outcome. In the drawer it says it in place; in the
 * Waiting card the request's row is gone once it is decided, so the card
 * takes the sentence and keeps it under its list.
 */
function useSaid(onSaid?: (s: Said) => void): [Said | null, (s: Said) => void] {
  const [said, setSaid] = useState<Said | null>(null)
  return [onSaid === undefined ? said : null, onSaid ?? setSaid]
}

/**
 * Approve. `confirm` asks first, in §6.4's words: the drawer's Approve of a
 * denied record does, since nothing beside it says what will be created. The
 * Waiting card's does not: its row already names the person, the workspace
 * and every limit the approval gives, which is all the question said.
 */
function Approve({ row, reload, confirm, onSaid }: { row: PersonRow; reload: () => void; confirm: boolean; onSaid?: (s: Said) => void }) {
  const id = row.workspace.workspace_id ?? ''
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [said, say] = useSaid(onSaid)
  const ceiling = row.workspace.limits?.max_active ?? DEFAULT_CEILING
  const go = async () => {
    if (busy) return
    setBusy(true)
    const r = await approveWorkspace(id)
    setBusy(false)
    setOpen(false)
    say(
      outcome(r, (d) =>
        // The API's own sentence when it sends one: it is the one that knows
        // whether anything will build this workspace.
        typeof d.message === 'string' && d.message !== ''
          ? d.message
          : d.dispatch?.published === false
            ? `Approved. The job was not started yet (${d.dispatch.reason ?? 'not published'}); the dispatch sweep retries it.`
            : `Approved ${id}. The job is starting.`,
      ),
    )
    reload()
  }
  return (
    <>
      <Button size="sm" kind="primary" disabled={busy} onClick={() => (confirm ? setOpen(true) : void go())}>
        {busy && !confirm ? 'Approving…' : 'Approve'}
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

function Deny({ row, reload, onSaid }: { row: PersonRow; reload: () => void; onSaid?: (s: Said) => void }) {
  const id = row.workspace.workspace_id ?? ''
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [said, say] = useSaid(onSaid)
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
    say(outcome(r, () => `Denied ${id}. The person sees the reason.`))
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

/**
 * DECLINE A LOAN REQUEST (LOAN-DECLINE). The reason is optional and shown to
 * the person, so a blank one sends none; the confirm is small and inline,
 * like Deny's, and nothing is sent until it is confirmed. A declined request
 * leaves the card on the next read, so the outcome is said under the card.
 */
function DeclineLoan({ row, reload, onSaid }: { row: PersonRow; reload: () => void; onSaid?: (s: Said) => void }) {
  const id = row.workspace.workspace_id ?? ''
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [said, say] = useSaid(onSaid)
  const go = async () => {
    if (busy) return
    setBusy(true)
    const r = await declineLoanRequest(id, reason)
    setBusy(false)
    if (r.status !== 'error') {
      setOpen(false)
      setReason('')
    }
    say(outcome(r, () => `Declined ${row.email}'s request to borrow a Claude account. They are told.`))
    reload()
  }
  if (!open) {
    return (
      <>
        <Button size="sm" onClick={() => setOpen(true)}>
          Decline
        </Button>
        <SaidLine said={said} />
      </>
    )
  }
  return (
    <span className="pp-deny">
      <label className="c-field">
        <span className="c-lbl">Reason, shown to {row.email} (optional)</span>
        <input className="c-inp" value={reason} maxLength={200} autoFocus onChange={(e) => setReason(e.target.value)} />
      </label>
      <span className="ob-acts">
        <Button size="sm" onClick={() => setOpen(false)}>
          Cancel
        </Button>
        <Button size="sm" kind="primary" disabled={busy} onClick={() => void go()}>
          {busy ? 'Declining…' : 'Decline'}
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

/** The ceiling as a stepper: − and + move it by one, the field takes any whole number, Save asks first. */
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
  const step = (by: number) => setValue(String(Math.max(1, (whole ? n : held) + by)))
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
    <div className="pp-ceiling">
      <div className="pp-stepper">
        <Button size="sm" aria-label="One less" disabled={busy || (whole ? n : held) <= 1} onClick={() => step(-1)}>
          −
        </Button>
        <label className="c-field">
          <span className="c-lbl">Active agents at once</span>
          <input className="c-inp" type="number" min={1} step={1} value={value} onChange={(e) => setValue(e.target.value)} aria-label={`Ceiling for ${row.email}`} />
        </label>
        <Button size="sm" aria-label="One more" disabled={busy} onClick={() => step(1)}>
          +
        </Button>
        <Button size="sm" kind="primary" disabled={busy || !changed} onClick={() => setOpen(true)}>
          Save
        </Button>
      </div>
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
    </div>
  )
}

/** Account states in which a lent account starts no new agent (the broker's `AccountState`). */
const NOT_ASSIGNING: ReadonlySet<string> = new Set(['PAUSED', 'DRAINING', 'REAUTH_REQUIRED'])

/**
 * Whether `account` is lent to this person, as far as the API lets the
 * console see. The admin view leaves the person's tenant id out, so the
 * account's `lend_to` cannot be matched against it here. `lent_accounts`
 * names the accounts themselves; an older API has only `lent_by`, the owner
 * tenants whose accounts name this person's tenant in `lend_to`, which
 * offers every account such an owner holds.
 */
export function lentToRow(account: LendableAccount, row: PersonRow): boolean {
  const c = row.claude_account
  if (Array.isArray(c.lent_accounts)) return c.lent_accounts.some((a) => a.account_id === account.account_id)
  return Array.isArray(c.lent_by) && c.lent_by.includes(account.owner_tenant)
}

/**
 * ONE ACCOUNT, ONE VERB. `lend` picks from the pool and lends; otherwise it
 * picks from what is lent to them and reclaims. NEITHER WRITES ON THE FIRST
 * CLICK (live QA ADM-1, 2026-10-11): lending hands one tenant's Claude
 * account to another person's workspace, and reclaiming takes it back from
 * under their runs. Each asks first, naming the account, its owner tenant and
 * the person. A single account to reclaim is said as text beside Reclaim
 * (owner, 2026-10-13: an empty "choose…" hid what was lent); a select only
 * when there is a choice to make.
 */
function LoanPicker({
  row,
  accounts,
  lend,
  reload,
  onSaid,
}: {
  row: PersonRow
  accounts: LendableAccount[]
  lend: boolean
  reload: () => void
  onSaid?: (s: Said) => void
}) {
  const [account, setAccount] = useState(!lend && accounts.length === 1 ? accounts[0]!.account_id : '')
  const [asking, setAsking] = useState(false)
  const [busy, setBusy] = useState(false)
  const [said, say] = useSaid(onSaid)
  const chosen = accounts.find((a) => a.account_id === account) ?? null
  const go = async () => {
    if (busy || chosen === null) return
    setBusy(true)
    const r = await setWorkspaceLoan(row.workspace.workspace_id ?? '', chosen.account_id, lend)
    setBusy(false)
    setAsking(false)
    say(
      outcome(r, (d) =>
        d.changed ? (d.lent ? `Lent ${d.account_id}.` : `Reclaimed ${d.account_id}.`) : d.lent ? `${d.account_id} was already lent.` : `${d.account_id} was not lent.`,
      ),
    )
    reload()
  }
  const name = chosen === null ? '' : `${named(chosen)} (${chosen.owner_tenant})`
  const idle = chosen !== null && chosen.state !== null && NOT_ASSIGNING.has(chosen.state)
  const verb = lend ? 'Lend' : 'Reclaim'
  const one = !lend && accounts.length === 1 ? accounts[0]! : null
  return (
    <div className="pp-loan">
      {one !== null ? (
        <span className="pp-loan-one" data-testid="pp-loan-one">
          Lent to them: <strong>{accountWithId(one)}</strong> · {one.owner_tenant}
          {one.state !== null ? ` · ${one.state}` : ''}
        </span>
      ) : (
        <label className="c-field">
          <span className="c-lbl">{lend ? 'Account from the pool' : 'Account lent to them'}</span>
          <select
            className="c-inp"
            value={account}
            onChange={(e) => setAccount(e.target.value)}
            aria-label={lend ? `Account to lend to ${row.email}` : `Account to reclaim from ${row.email}`}
          >
            <option value="">choose…</option>
            {accounts.map((a) => (
              <option key={a.account_id} value={a.account_id}>
                {named(a)} · {a.owner_tenant}
                {a.state !== null ? ` · ${a.state}` : ''}
                {a.state !== null && NOT_ASSIGNING.has(a.state) ? ' (starts no new agent)' : ''}
              </option>
            ))}
          </select>
        </label>
      )}
      <Button size="sm" kind="primary" disabled={busy || chosen === null} onClick={() => setAsking(true)}>
        {verb}
      </Button>
      {asking && chosen !== null && (
        <Dialog
          title={lend ? 'Lend this account' : 'Reclaim this account'}
          onClose={() => setAsking(false)}
          actions={
            <>
              <Button onClick={() => setAsking(false)}>Cancel</Button>
              <Button kind="primary" disabled={busy} onClick={() => void go()}>
                {busy ? (lend ? 'Lending…' : 'Reclaiming…') : verb}
              </Button>
            </>
          }
        >
          <p>{lend ? `Lend ${name} to ${row.email}?` : `Reclaim ${name} from ${row.email}?`}</p>
          <p className="ur-hint">
            {lend
              ? `Their work may then run on an account ${chosen.owner_tenant} owns.`
              : `${chosen.owner_tenant} stops lending it to them; their new work is no longer offered it.`}
          </p>
          {lend && idle && (
            <p className="ur-hint" role="note" data-testid="pp-loan-idle">
              This account is {chosen.state}: it starts no new agent until its owner makes it available again.
            </p>
          )}
        </Dialog>
      )}
      <SaidLine said={said} />
    </div>
  )
}

/** The pool's lendable list as a sentence, or as not read: never an unread list drawn as an empty one. */
function PoolLine({ doc }: { doc: PeopleDoc }) {
  if (doc.lendable_accounts === null) {
    return (
      <span className="ur-hint">
        <Mark kind="unread" say="The accounts this admin may lend were not read" /> {doc.lendable_error ?? 'the broker did not answer'}
      </span>
    )
  }
  const n = doc.lendable_accounts.length
  // A ZERO IN A SENTENCE READS AS WORDS (owner, 2026-10-09); that it was
  // measured stays in its accessible name.
  if (n === 0) return <span className="ur-hint" aria-label="no lendable accounts in the pool (measured)">no lendable accounts in the pool</span>
  return (
    <span className="ur-hint">
      {n} lendable {n === 1 ? 'account' : 'accounts'} in the pool
    </span>
  )
}

/** The limits a workspace request would get, as chips: §1.1's keys, only those the record carries. */
function LimitChips({ limits }: { limits: Record<string, number> | undefined }) {
  const parts: string[] = []
  const l = limits ?? {}
  if (typeof l.max_active === 'number') parts.push(`${l.max_active} agents at once`)
  if (typeof l.quota_cpu === 'number') parts.push(`${l.quota_cpu} vCPU`)
  if (typeof l.quota_pods === 'number') parts.push(`${l.quota_pods} pods`)
  if (typeof l.capacity_units === 'number') parts.push(`${l.capacity_units} capacity units`)
  if (parts.length === 0) return <span className="ur-hint">the record carries no limits</span>
  return (
    <span className="pp-limits" aria-label="Limits it would get">
      {parts.map((p) => (
        <Chip key={p}>{p}</Chip>
      ))}
    </span>
  )
}

function Requested({ at }: { at: string | null }) {
  return <span className="ur-hint">{at !== null ? `asked ${timeAgo(at)}` : 'asked at an unknown time'}</span>
}

/** Whether the read is a workspace admin's, scoped to the workspaces they administer. */
export function isScoped(doc: PeopleDoc | ScopedPeopleDoc): doc is ScopedPeopleDoc {
  return (doc as ScopedPeopleDoc).scope === 'workspace_admin'
}

/** The grid's columns, or none from an older API. */
function columnsOf(doc: PeopleDoc | ScopedPeopleDoc): PeopleWorkspace[] {
  return Array.isArray(doc.workspaces) ? doc.workspaces : []
}

/** A column's heading: the workspace id, whole group address in `title`. */
function ColumnHead({ ws }: { ws: PeopleWorkspace }) {
  return (
    <th role="columnheader" scope="col" className="pp-rolecol" title={ws.display_name}>
      {ws.tenant_id}
    </th>
  )
}

/** One read-only cell: the role, `none` dimmed, a dash with its reason when the column was not read. */
function RoleText({ cell }: { cell: RoleCell | null | undefined }) {
  if (cell == null) return <Dash why="This workspace’s roles could not be read" />
  return (
    <span className={`pp-role is-${cell}`} data-role={cell}>
      {cell}
    </span>
  )
}

/** What the lead says a request is for. */
function requestFor(r: WaitingRequest): string {
  return r.kind === 'workspace' ? 'a personal workspace' : 'a Claude account'
}

/**
 * Bring the request into view and put focus on its first decision, so the
 * lead's one button lands where the decision is made.
 */
function openRequest(r: WaitingRequest): void {
  const item = Array.from(document.querySelectorAll<HTMLElement>('.pp-req')).find((el) => el.dataset.kind === r.kind && el.dataset.email === r.row.email)
  if (item === undefined) return
  item.scrollIntoView?.({ block: 'center' })
  item.querySelector<HTMLElement>('.pp-req-acts button, .pp-req-acts select')?.focus()
}

/**
 * THE LEAD (S5c): who is waiting for an admin, the oldest named, and one
 * button to it. Green when nothing is. Counted from the same
 * `waitingRequests` Waiting for you lists, so the two cannot disagree.
 */
export function PeopleLead({ rows }: { rows: readonly PersonRow[] }) {
  const items = waitingRequests(rows)
  if (items.length === 0) {
    // A request held for the platform owner's migration is `requested` and
    // counted pending, but no admin here can decide it: said, so the lead
    // and the table's "N pending" never read as a contradiction.
    const held = rows.filter((r) => r.workspace.state === 'requested').length
    return (
      <AdminLead
        tone="ok"
        kicker="Nothing waiting"
        title="No one is waiting for an admin"
        body={
          held === 0
            ? 'Every workspace and Claude account request has been answered.'
            : `${held} workspace ${held === 1 ? 'request waits' : 'requests wait'} for the platform owner, not for an approval here.`
        }
      />
    )
  }
  const oldest = items[0]!
  const n = items.length
  const who = oldest.row.display_name ?? oldest.row.email
  return (
    <AdminLead
      tone="warn"
      kicker="Needs an admin"
      title={`${n} ${n === 1 ? 'request is' : 'requests are'} waiting for an admin`}
      body={
        <>
          The oldest: <strong>{who}</strong> asked for {requestFor(oldest)} {oldest.at !== null ? timeAgo(oldest.at) : 'at an unknown time'}. Approve or deny each; a denial says why to the person.
        </>
      }
      action={
        <Button kind="primary" onClick={() => openRequest(oldest)}>
          Review the oldest
        </Button>
      }
    />
  )
}

/**
 * WAITING FOR YOU (#1206 part 1). Every open request, oldest first, each with
 * its decision beside it. Not drawn when nothing is open. Once a decision is
 * made the request leaves the list on the next read, so its outcome is said
 * under the list, and the card stays for that sentence.
 */
function WaitingCard({ doc, rows, reload }: { doc: PeopleDoc; rows: PersonRow[]; reload: () => void }) {
  const [said, setSaid] = useState<Said | null>(null)
  const items = waitingRequests(rows)
  if (items.length === 0 && said === null) return null
  const oldest = items.find((i) => i.at !== null)?.at ?? null
  return (
    <Card
      className="pp-waiting"
      title="Waiting for you"
      action={
        items.length > 0 ? (
          <Chip>
            {items.length} waiting{oldest !== null ? ` · oldest ${timeAgo(oldest)}` : ''}
          </Chip>
        ) : undefined
      }
    >
      {items.length === 0 ? (
        <p className="ur-hint">Nothing else is waiting.</p>
      ) : (
        <ul className="pp-reqs" aria-label="Open requests">
          {items.map(({ kind, row, at }) => (
            <li key={`${kind}:${row.email}`} className="pp-req" data-kind={kind} data-email={row.email}>
              <div className="pp-req-who">
                <Avatar email={row.email} />
                <span className="pp-who" title={row.email}>
                  <PersonName email={row.email} name={row.display_name ?? null} variant="full" />
                </span>
              </div>
              {kind === 'workspace' ? (
                <>
                  <div className="pp-req-what">
                    <span>
                      Workspace <code className="pp-id">{row.workspace.workspace_id}</code> · <Requested at={at} />
                    </span>
                    <LimitChips limits={row.workspace.limits} />
                  </div>
                  <div className="pp-req-acts">
                    <Approve row={row} reload={reload} confirm={false} onSaid={setSaid} />
                    <Deny row={row} reload={reload} onSaid={setSaid} />
                  </div>
                </>
              ) : (
                <>
                  <div className="pp-req-what">
                    <span>
                      Borrow a Claude account · <Requested at={at} />
                    </span>
                    <PoolLine doc={doc} />
                  </div>
                  <div className="pp-req-acts">
                    {doc.lendable_accounts !== null && doc.lendable_accounts.length > 0 && (
                      <LoanPicker row={row} accounts={doc.lendable_accounts} lend reload={reload} onSaid={setSaid} />
                    )}
                    <DeclineLoan row={row} reload={reload} onSaid={setSaid} />
                  </div>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
      <SaidLine said={said} />
    </Card>
  )
}

function Avatar({ email }: { email: string }) {
  return (
    <span className="pp-avatar" aria-hidden="true">
      {(email.trim()[0] ?? '?').toUpperCase()}
    </span>
  )
}

/** GitHub as a mark, not a word: a connected or expired link only; none draws nothing. */
function GitHubMark({ github }: { github: PersonRow['github'] }) {
  if (github !== 'connected' && github !== 'expired') return null
  const say = github === 'connected' ? 'GitHub connected' : 'GitHub needs re-auth'
  return (
    <span className={`pp-gh${github === 'expired' ? ' is-expired' : ''}`} role="img" aria-label={say} title={say}>
      <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false">
        <path
          fill="currentColor"
          d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"
        />
      </svg>
    </span>
  )
}

function PersonCell({ row, doc }: { row: PersonRow; doc: PeopleDoc }) {
  return (
    <span className="pp-person">
      <Avatar email={row.email} />
      <span className="pp-who" title={row.email}>
        <PersonName email={row.email} name={row.display_name ?? null} variant="full" />
      </span>
      <GitHubMark github={row.github} />
      {holderOf(doc, row.email)?.role === 'owner' && <Chip className="pp-owner">platform owner</Chip>}
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
          {/* Whole in `title`: people.css cuts an id longer than its room (V021, V161). */}
          <code className="pp-id" title={w.workspace_id}>
            {w.workspace_id}
          </code>
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

function ClaudeCell({ row }: { row: PersonRow }) {
  return (
    <span className="pp-claude">
      {claudeText(row.claude_account)}
      {row.loan_request?.state === 'requested' && <Chip className="pp-borrow">borrow requested</Chip>}
      {row.loan_request?.state === 'declined' && <Chip className="pp-borrow">loan declined</Chip>}
    </span>
  )
}

function isWaiting(row: PersonRow): boolean {
  return hasRecord(row) && ((row.workspace.state === 'requested' && row.workspace.held == null) || row.loan_request?.state === 'requested')
}

/**
 * A LEAN ROW (#1206 part 2): facts only, and one Manage button. On a phone the
 * row is the person, their workspace and Manage; the rest is in the drawer,
 * which opens as a full sheet.
 */
function Row({
  row,
  doc,
  columns,
  phone,
  onManage,
}: {
  row: PersonRow
  doc: PeopleDoc
  columns: readonly PeopleWorkspace[]
  phone: boolean
  onManage: (email: string) => void
}) {
  const w = row.workspace
  const manage = (
    <td role="cell" data-label="Manage" className="pp-manage-cell">
      <Button size="sm" onClick={() => onManage(row.email)}>
        Manage
      </Button>
    </td>
  )
  return (
    <tr role="row" data-email={row.email} data-workspace-state={w.state} className={isWaiting(row) ? 'is-waiting' : undefined}>
      <th role="rowheader" scope="row">
        <PersonCell row={row} doc={doc} />
      </th>
      {/* ADMIN-PEOPLE-ROLES: a column per group workspace, where Teams was. */}
      {columns.map((ws) => (
        <td key={ws.tenant_id} role="cell" data-label={ws.tenant_id} data-col={ws.tenant_id} className="pp-rolecell">
          <RoleText cell={row.roles?.[ws.tenant_id]} />
        </td>
      ))}
      <td role="cell" data-label="Personal workspace">
        <WorkspaceCell row={row} />
      </td>
      {!phone && (
        <>
          <td role="cell" data-label="Claude account">
            <ClaudeCell row={row} />
          </td>
          <td role="cell" data-label="Ceiling" className="n">
            {hasRecord(row) ? (row.workspace.limits?.max_active ?? DEFAULT_CEILING) : <Dash why="No workspace, so no ceiling" />}
          </td>
          <td role="cell" data-label="Last active">
            {row.last_active !== null ? timeAgo(row.last_active) : <Dash why="Not seen active" />}
          </td>
        </>
      )}
      {manage}
    </tr>
  )
}

/** The drawer's Claude account: what they use now, then Lend OR Reclaim, or one line saying why neither. */
function ClaudeSection({ row, doc, reload }: { row: PersonRow; doc: PeopleDoc; reload: () => void }) {
  const lendable = doc.lendable_accounts
  const lent = lendable === null ? [] : lendable.filter((a) => lentToRow(a, row))
  let control: ReactNode
  if (!hasRecord(row)) {
    control = (
      <p className="ur-hint" data-testid="pp-loan-why">
        No workspace yet: an account is lent to a workspace.
      </p>
    )
  } else if (lendable === null) {
    control = (
      <p className="ur-hint" data-testid="pp-loan-why">
        <Mark kind="unread" say="The accounts this admin may lend were not read" /> {doc.lendable_error ?? 'the broker did not answer'}
      </p>
    )
  } else if (lent.length > 0) {
    // RECLAIM ONLY WHAT IS LENT (ADM-2), AND THEN ONLY RECLAIM (#1206): an
    // account of the pool is already theirs, so Lend is not offered beside it.
    control = <LoanPicker key="reclaim" row={row} accounts={lent} lend={false} reload={reload} />
  } else if (lendable.length === 0) {
    control = (
      <p className="ur-hint" data-testid="pp-loan-why">
        Nothing to lend or reclaim: no accounts in the pool you may lend, and none of them is lent to {row.email}.
      </p>
    )
  } else {
    control = <LoanPicker key="lend" row={row} accounts={lendable} lend reload={reload} />
  }
  return (
    <section className="pp-dsec" data-section="claude">
      <h3>Claude account</h3>
      <p>
        Now using {claudeText(row.claude_account)}
        {row.loan_request?.state === 'requested' && (
          <>
            {' '}
            <Chip className="pp-borrow">
              borrow requested{row.loan_request.requested_at !== null ? ` ${timeAgo(row.loan_request.requested_at)}` : ''}
            </Chip>
          </>
        )}
        {/* Until they ask again (LOAN-DECLINE): a new request re-opens it. */}
        {row.loan_request?.state === 'declined' && (
          <>
            {' '}
            <Chip className="pp-borrow">
              loan declined{row.loan_request.decided_at != null ? ` ${timeAgo(row.loan_request.decided_at)}` : ''}
            </Chip>
          </>
        )}
      </p>
      {row.claude_account.source === 'lent' && lent.length === 0 && lendable !== null && (
        <p className="ur-hint">Lent by an owner whose accounts you may not lend, so it is not yours to reclaim.</p>
      )}
      {control}
    </section>
  )
}

/** The drawer's Admin switch: on is a role document. Locked, with the reason, for the owner and while the roles are unread. */
function AdminSwitch({ row, doc, reload }: { row: PersonRow; doc: PeopleDoc; reload: () => void }) {
  const [asking, setAsking] = useState(false)
  const [busy, setBusy] = useState(false)
  const [said, setSaid] = useState<Said | null>(null)
  const read = Array.isArray(doc.admins)
  const holder = holderOf(doc, row.email)
  const on = holder !== undefined
  const locked = !read || holder?.role === 'owner'
  const why = !read
    ? `The admin roles were not read (${doc.admins_error ?? 'not served'}), so this cannot be switched here.`
    : holder?.role === 'owner'
      ? 'Platform owner: set by PLATFORM_OWNER in configuration, so it is not switched here.'
      : on
        ? `Granted by ${holder.granted_by ?? 'unknown'}${holder.granted_at !== null ? ` ${timeAgo(holder.granted_at)}` : ''}. The last admin cannot be removed; the API says so when it refuses.`
        : 'Off. An admin through an ADMIN_GROUPS group holds no role document and shows as off here.'
  const go = async () => {
    if (busy) return
    setBusy(true)
    const said = on
      ? outcome(await removeAdmin(row.email), (d) => `${d.email} is no longer an admin.`)
      : outcome(await grantAdmin(row.email), (d) => (d.changed ? `${d.email} is now an admin.` : `${d.email} was already an admin.`))
    setBusy(false)
    setAsking(false)
    setSaid(said)
    reload()
  }
  return (
    <section className="pp-dsec" data-section="admin">
      <h3>Admin</h3>
      <div className="pp-switch-row">
        <button
          type="button"
          role="switch"
          aria-checked={on}
          aria-label="Admin"
          className={`pp-switch${on ? ' is-on' : ''}`}
          disabled={locked || busy}
          onClick={() => setAsking(true)}
        >
          <span className="pp-switch-knob" aria-hidden="true" />
        </button>
        <span className="ur-hint">{why}</span>
      </div>
      {asking && (
        <Dialog
          title={on ? 'Remove admin' : 'Grant admin'}
          onClose={() => setAsking(false)}
          actions={
            <>
              <Button onClick={() => setAsking(false)}>Cancel</Button>
              <Button kind="primary" disabled={busy} onClick={() => void go()}>
                {on ? 'Remove' : 'Grant admin'}
              </Button>
            </>
          }
        >
          <p>{on ? `Remove admin from ${row.email}?` : `Make ${row.email} a platform admin?`}</p>
          {!on && (
            <p className="ur-hint">
              An admin can approve workspaces, change every ceiling and pool limit, lend accounts and grant admin to others.
            </p>
          )}
        </Dialog>
      )}
      <SaidLine said={said} />
    </section>
  )
}

const GITHUB_WORD: Readonly<Record<string, string>> = { connected: 'connected', expired: 'needs re-auth', none: 'not connected' }

/**
 * THE MANAGE DRAWER (#1206 part 3), from the right; a full sheet on a phone.
 * Escape, the scrim and Close shut it, and focus goes back to the Manage
 * button that opened it. Escape inside a confirmation shuts the confirmation
 * only: the drawer leaves it to the dialog on top.
 */
function Drawer({ row, doc, reload, onClose }: { row: PersonRow; doc: PeopleDoc; reload: () => void; onClose: () => void }) {
  const titleId = useId()
  const closeRef = useRef(onClose)
  closeRef.current = onClose
  const closeBtn = useRef<HTMLButtonElement>(null)
  const sheet = useRef<HTMLElement>(null)
  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    closeBtn.current?.focus()
    // THE PAGE BEHIND IS INERT WHILE THE DRAWER IS OPEN (visual QA V159). It
    // said `aria-modal="true"` and trapped nothing: after its last control Tab
    // walked into the skip link, the menu, the bell and the rail -- on a phone,
    // controls the full-sheet drawer hides. Every other child of <body> is made
    // inert (the drawer is portalled there), and only the ones this set, so a
    // confirmation opened later from inside the drawer, portalled after it, is
    // not. Tab also wraps at the drawer's ends (focus.ts `trapTab`).
    const scrim = sheet.current?.parentElement ?? null
    const benched = Array.from(document.body.children).filter(
      (el): el is HTMLElement => el instanceof HTMLElement && el !== scrim && !el.hasAttribute('inert'),
    )
    for (const el of benched) el.setAttribute('inert', '')
    const onKey = (e: KeyboardEvent) => {
      // A confirmation on top owns Escape and Tab.
      if (document.querySelector('.c-dlg') !== null) return
      if (e.key === 'Escape') {
        e.preventDefault()
        closeRef.current()
        return
      }
      trapTab(e, sheet.current)
    }
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      for (const el of benched) el.removeAttribute('inert')
      opener?.focus()
    }
  }, [])
  const w = row.workspace
  const record = hasRecord(row)
  const body = (
    <div
      className="pp-scrim"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) closeRef.current()
      }}
    >
      <aside className="pp-drawer" role="dialog" aria-modal="true" aria-labelledby={titleId} ref={sheet}>
        <header className="pp-drawer-h">
          <h2 id={titleId}>
            <span className="vh">Manage </span>
            <PersonName email={row.email} name={row.display_name ?? null} variant="full" />
          </h2>
          <button type="button" ref={closeBtn} className={buttonClass({ size: 'sm' })} onClick={() => closeRef.current()}>
            Close
          </button>
        </header>
        <section className="pp-dsec" data-section="facts">
          <h3>Facts</h3>
          <dl className="pp-facts">
            <dt>Teams</dt>
            <dd>{row.teams.length > 0 ? row.teams.join(', ') : <Dash why="Resolves to no team" />}</dd>
            <dt>GitHub</dt>
            <dd>{GITHUB_WORD[row.github] ?? row.github}</dd>
            <dt>Workspace</dt>
            <dd>
              <WorkspaceCell row={row} />
              {record && w.state === 'requested' && w.held == null && <p className="ur-hint">Approve or deny it under Waiting for you.</p>}
              {/* An admin may approve a denied record at any time, inside the
                  person's 24-hour wait too (§1.3, confirmed by the owner). */}
              {record && w.state === 'denied' && (
                <span className="ob-acts pp-ws-acts">
                  <Approve row={row} reload={reload} confirm />
                </span>
              )}
              {record && w.state === 'failed' && (
                <span className="ob-acts pp-ws-acts">
                  <Retry row={row} reload={reload} />
                  <Deny row={row} reload={reload} />
                </span>
              )}
            </dd>
            <dt>Last active</dt>
            <dd>{row.last_active !== null ? timeAgo(row.last_active) : <Dash why="Not seen active" />}</dd>
          </dl>
        </section>
        <section className="pp-dsec" data-section="ceiling">
          <h3>Ceiling</h3>
          {record ? <Ceiling row={row} reload={reload} /> : <p className="ur-hint">No workspace yet: a ceiling is set on a workspace.</p>}
        </section>
        <ClaudeSection row={row} doc={doc} reload={reload} />
        <AdminSwitch row={row} doc={doc} reload={reload} />
        {/* ROLES-UI-ADMIN (workspace-roles.md §4.8): every override this person holds. */}
        <PersonWorkspaceRoles email={row.email} platformAdmin={holderOf(doc, row.email) !== undefined} />
      </aside>
    </div>
  )
  return typeof document === 'undefined' ? body : createPortal(body, document.body)
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

/**
 * HOUSEKEEPING IS NOT A DECISION (#1206 part 4). Measured 2026-10-12: Recent
 * admin actions was mostly `history_purged`, an entry per task a purge sweep
 * removed, which buried the approvals, denials, loans and grants an admin
 * reads this card for. These actions fold into one line with a toggle.
 */
export const HOUSEKEEPING: ReadonlySet<string> = new Set(['history_purged'])

/** `history_purged × 48`, per housekeeping action, in the order they first appear. */
export function housekeepingSummary(entries: readonly AuditEntry[]): string {
  const counts = new Map<string, number>()
  for (const e of entries) if (HOUSEKEEPING.has(e.action)) counts.set(e.action, (counts.get(e.action) ?? 0) + 1)
  return [...counts].map(([a, n]) => `${a} × ${n}`).join(', ')
}

function Audit({ entries }: { entries: AuditEntry[] }) {
  const [showHousekeeping, setShowHousekeeping] = useState(false)
  const decisions = entries.filter((e) => !HOUSEKEEPING.has(e.action))
  const folded = entries.length - decisions.length
  const runs = auditRuns(showHousekeeping ? entries : decisions)
  const scope = entries.length >= AUDIT_SHOWN ? `the latest ${entries.length} entries` : `the ${entries.length} recorded ${entries.length === 1 ? 'entry' : 'entries'}`
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
          {folded > 0 && (
            <p className="ur-hint pp-housekeeping" data-testid="pp-housekeeping">
              <span>
                {housekeepingSummary(entries)} housekeeping {showHousekeeping ? 'shown' : 'hidden'}
              </span>{' '}
              <Button size="sm" aria-pressed={showHousekeeping} onClick={() => setShowHousekeeping((v) => !v)}>
                {showHousekeeping ? 'Hide housekeeping' : 'Show housekeeping'}
              </Button>
            </p>
          )}
          {runs.length === 0 ? (
            <p className="ur-hint">No approvals, denials, loans or admin grants in {scope}.</p>
          ) : (
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
          )}
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
  const columns = columnsOf(doc)
  const phone = usePhoneTables()
  // The drawer follows the person, not a snapshot of their row: each read
  // redraws it from the list, so a decision made in it shows there.
  const [managing, setManaging] = useState<string | null>(null)
  const managed = managing === null ? null : (rows.find((r) => r.email === managing) ?? null)
  return (
    <>
      <PeopleLead rows={rows} />
      <BuildingOff doc={doc} />
      <WaitingCard doc={doc} rows={rows} reload={reload} />
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
              table has its own scroll container inside the card; below 560px
              it is a record per person. NOT `.ten-table`: that is Tenants'
              fixed layout, which split this table into equal columns and broke
              a workspace id at its hyphen. */
          <div className={`table-wrap ${tableMode(phone)} pp-wrap`}>
            <table className="pools pp-table" role="table">
              <thead role="rowgroup">
                <tr role="row">
                  <th role="columnheader" scope="col">Person</th>
                  {columns.map((ws) => (
                    <ColumnHead key={ws.tenant_id} ws={ws} />
                  ))}
                  <th role="columnheader" scope="col">Personal workspace</th>
                  {!phone && (
                    <>
                      <th role="columnheader" scope="col">Claude account</th>
                      <th role="columnheader" scope="col" className="n">Ceiling</th>
                      <th role="columnheader" scope="col">Last active</th>
                    </>
                  )}
                  <th role="columnheader" scope="col">
                    <span className="vh">Manage</span>
                  </th>
                </tr>
              </thead>
              <tbody role="rowgroup">
                {rows.map((row) => (
                  <Row key={row.email} row={row} doc={doc} columns={columns} phone={phone} onManage={setManaging} />
                ))}
              </tbody>
            </table>
          </div>
        )}
        {doc.workspaces_error != null && (
          <p className="ur-hint">
            <Dash why="The registered workspaces could not be read" /> No workspace columns: the registered workspaces could not be read ({doc.workspaces_error}).
          </p>
        )}
      </Card>
      {/* ROLES-UI-ADMIN: each group workspace's default and admins, and how
          its first admin is named (workspace-roles.md §4.8, Q2). */}
      <WorkspaceRolesCard />
      <div className="pp-side">
        <Admins doc={doc} reload={reload} />
        <Audit entries={Array.isArray(doc.audit) ? doc.audit : []} />
      </div>
      {managed !== null && <Drawer key={managed.email} row={managed} doc={doc} reload={reload} onClose={() => setManaging(null)} />}
    </>
  )
}

/**
 * ONE CELL OF A WORKSPACE ADMIN'S GRID: the person's role in a workspace they
 * administer, as a select. "Workspace default" clears the person's own role
 * (`DELETE`); `none` is shown but cannot be chosen -- it is the absence of a
 * role, not one. A column that was not read is a dash, never a select.
 */
function RoleSelect({ ws, row, reload, onSaid }: { ws: PeopleWorkspace; row: ScopedPersonRow; reload: () => void; onSaid: (s: Said) => void }) {
  const id = useId()
  const [busy, setBusy] = useState(false)
  const cell = row.roles[ws.tenant_id]
  if (cell == null) return <Dash why="This workspace’s roles could not be read" />
  const go = async (to: string) => {
    setBusy(true)
    const r = to === 'default' ? await clearPeopleWorkspaceRole(ws.tenant_id, row.email) : await setPeopleWorkspaceRole(ws.tenant_id, row.email, to)
    setBusy(false)
    onSaid(outcome(r, (d) => `${row.email} is now ${d.role} in ${ws.tenant_id}${to === 'default' ? ' (the workspace default)' : ''}.`))
    reload()
  }
  return (
    <span className="pp-roleset">
      <label className="vh" htmlFor={id}>
        {`Role of ${row.email} in ${ws.tenant_id}`}
      </label>
      <select id={id} className={`pp-roleselect is-${cell}`} value={cell} disabled={busy} data-role={cell} onChange={(e) => void go(e.target.value)}>
        <option value="admin">admin</option>
        <option value="member">member</option>
        <option value="viewer">viewer</option>
        {cell === 'none' && (
          <option value="none" disabled>
            none
          </option>
        )}
        <option value="default">{`workspace default${ws.default_role !== null ? ` (${ws.default_role})` : ''}`}</option>
      </select>
    </span>
  )
}

/** A list of names as a sentence: "eng", "eng and research", "eng, ops and research". */
function andList(names: readonly string[]): string {
  if (names.length <= 1) return names.join('')
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
}

/**
 * A WORKSPACE ADMIN'S PAGE (S5f): the lead says what is theirs -- the
 * workspaces they administer -- and that nothing here waits on them, because
 * the requests this page answers are the platform's; then the grid, a select
 * per cell.
 */
export function ScopedPeopleBody({ doc, reload }: { doc: ScopedPeopleDoc; reload: () => void }) {
  const [said, setSaid] = useState<Said | null>(null)
  const phone = usePhoneTables()
  const columns = columnsOf(doc)
  const rows = Array.isArray(doc.people) ? doc.people : []
  const names = columns.map((w) => w.tenant_id)
  const one = columns.length === 1
  return (
    <>
      <AdminLead
        tone="ok"
        kicker="Your workspaces"
        title="No one is waiting for you"
        body={`Showing ${andList(names)}, the ${one ? 'workspace' : 'workspaces'} you administer. A request for a personal workspace is a platform admin’s, and who joins a group workspace is decided by its group.`}
      />
      <Card
        className="pp-people pp-scoped"
        title={one ? 'People in your workspace' : 'People in your workspaces'}
        action={
          <Chip>
            {doc.count} {doc.count === 1 ? 'person' : 'people'}
          </Chip>
        }
      >
        {rows.length === 0 ? (
          <p className="ur-hint">
            <span aria-label="no one is in these workspaces yet (measured)">no one is in {one ? 'this workspace' : 'these workspaces'} yet</span>
          </p>
        ) : (
          <div className={`table-wrap ${tableMode(phone)} pp-wrap`}>
            <table className="pools pp-table pp-grid" role="table">
              <thead role="rowgroup">
                <tr role="row">
                  <th role="columnheader" scope="col">Person</th>
                  {columns.map((ws) => (
                    <ColumnHead key={ws.tenant_id} ws={ws} />
                  ))}
                  <th role="columnheader" scope="col">Last active</th>
                </tr>
              </thead>
              <tbody role="rowgroup">
                {rows.map((row) => (
                  <tr key={row.email} role="row" data-email={row.email}>
                    <th role="rowheader" scope="row">
                      <span className="pp-person">
                        <Avatar email={row.email} />
                        <span className="pp-who" title={row.email}>
                          <PersonName email={row.email} name={row.display_name ?? null} variant="full" />
                        </span>
                      </span>
                    </th>
                    {columns.map((ws) => (
                      <td key={ws.tenant_id} role="cell" data-label={ws.tenant_id} data-col={ws.tenant_id} className="pp-rolecell">
                        <RoleSelect ws={ws} row={row} reload={reload} onSaid={setSaid} />
                      </td>
                    ))}
                    <td role="cell" data-label="Last active">
                      {row.last_active !== null ? timeAgo(row.last_active) : <Dash why="Not seen active" />}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <SaidLine said={said} />
      </Card>
    </>
  )
}

/** Admin › People and roles. */
export function PeopleScreen() {
  const people = useUrRead(loadPeopleAndRoles, 'people')
  const data = people.state.status === 'ok' || people.state.status === 'stale' ? people.state.data : null
  const scoped = data !== null && isScoped(data)
  // A row waiting on something the platform will not do by itself is not moving.
  const moving =
    data !== null &&
    !isScoped(data) &&
    Array.isArray(data.people) &&
    data.people.some((r) => IN_FLIGHT.has(r.workspace.state) && waitingOf(r.workspace) !== 'publishing_off')
  usePoll(PEOPLE_POLL_MS, people.reload, !moving)
  return (
    <div className="ur-page pp-page">
      <PageHead title="People and roles">
        <UrRefresh reads={[people]} />
      </PageHead>
      <p className="ur-sub">
        {scoped
          ? 'The people in the workspaces you administer, and their role in each.'
          : 'Everyone who has signed in, their role in each workspace, what they are waiting for, and who is an admin.'}
      </p>
      <UrRegion state={people.state} route="GET /v1/admin/people" what="People" plural onRetry={people.reload} lines={6}>
        {(d) => (isScoped(d) ? <ScopedPeopleBody doc={d} reload={people.reload} /> : <PeopleBody doc={d} reload={people.reload} />)}
      </UrRegion>
    </div>
  )
}
