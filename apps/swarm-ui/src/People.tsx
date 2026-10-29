import { createContext, Fragment, useContext, useEffect, useId, useMemo, useRef, useState, type FormEvent, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import {
  approveWorkspace,
  declineLoanRequest,
  denyWorkspace,
  grantAdmin,
  loadPeople,
  removeAdmin,
  retryWorkspace,
  setWorkspaceCeiling,
  setWorkspaceLoan,
  USE_FIXTURES,
} from './api'
import { AdminLead } from './AdminSettings'
import { Banner, Button, buttonClass, Card, Chip, CIcon, Dash, Dialog, Segmented, ToneMark } from './components'
import { classifyFailure, route, TENANT_HEADER, write, type ApiError, type ErrorEnvelope, type Result } from './fetch'
import { trapTab } from './focus'
import { ApprovedWaiting, JobSteps, waitingOf } from './OnboardingWorkspace'
import { PersonName } from './PersonName'
import { groupWorkspaceName, useStoredRoles } from './Profile'
import { Mark } from './primitives'
import { UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import { PageHead, usePoll } from './Shell'
import { nameTenants, type NamedTenantChoice } from './Spine'
import { tableMode, usePhoneTables } from './capacityPoll'
import { PersonWorkspaceRoles, WorkspaceRolesCard } from './WorkspaceRolesAdmin'
import type {
  AdminAuditEntry,
  AdminHolder,
  LendableAccount,
  PeopleDoc,
  PeopleRoleCell,
  PeopleWorkspaceColumn,
  PersonClaudeAccount,
  PersonLentAccount,
  PersonRow,
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
 * PEOPLE AND ROLES (ADMIN-PEOPLE-ROLES; part of #2157, proposal E S5c, S5f):
 * "Every person against every workspace; the lead is who is waiting for an
 * admin." The lean table is the grid: the Teams column became one column per
 * group workspace the API served (`PeopleDoc.workspaces`), each cell the
 * person's role there, and Workspace became Own workspace. Approve, Deny,
 * Lend, Decline, the ceiling and the Admin switch stay where they were, on
 * Waiting for you and in each row's drawer. The page opens with a lead that
 * names the oldest open request, and its one button goes to it.
 *
 * A WORKSPACE ADMIN who is not a platform admin is answered a SCOPED read
 * (`scope: workspaces`): only the workspaces they administer, and of each
 * person only their cells and when they were last seen. That page draws the
 * grid and the lead and nothing else -- no Waiting for you (every request
 * here is a platform admin's to decide), no Manage, no Admins, no Recent
 * admin actions, no Workspace roles card -- because none of it was served
 * and none of its writes would be answered. The API is what holds the line
 * (`people_scope`, routes/people.py); this page only draws what it was given.
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

/**
 * AN ACCOUNT'S STATE IN PLAIN WORDS (visual QA pass 2, admin2-04,
 * 2026-10-29): the lend select read 'AVAILABLE', 'PAUSED' and
 * 'REAUTH_REQUIRED', the broker's enum. These are the words the Accounts
 * table's reasons use (`AccountsTable.tsx` `whyOf`: paused, draining, signed
 * out until someone signs in again). A state this console does not know is
 * its own word, lower case, as `AcctState` draws it.
 */
const ACCOUNT_STATE_WORD: Readonly<Record<string, string>> = {
  AVAILABLE: 'available',
  PAUSED: 'paused',
  DRAINING: 'draining',
  REAUTH_REQUIRED: 'needs signing in again',
}

export function accountStateWord(state: string): string {
  return ACCOUNT_STATE_WORD[state] ?? state.toLowerCase().replace(/_/g, ' ')
}

/**
 * WHO LENT IT, AS A WORKSPACE (admin2-04): the select and 'Now using' named
 * the lender by its tenant id, 'lent by u-bogdan'. A personal workspace is
 * named by its person, as the People table lists them ("Bogdan's
 * workspace"); a group workspace by its column's name, which
 * `withWorkspaceNames` already renamed by the switcher's rule. A tenant the
 * page cannot name stays its id.
 */
export function lenderNames(doc: Pick<PeopleDoc, 'people'> & { workspaces?: PeopleWorkspaceColumn[] | null }): (tenant: string) => string {
  const names = new Map<string, string>()
  for (const c of Array.isArray(doc.workspaces) ? doc.workspaces : []) {
    if (c.display_name !== '') names.set(c.tenant_id, c.display_name)
  }
  for (const r of Array.isArray(doc.people) ? doc.people : []) {
    const id = r.workspace.workspace_id
    if (typeof id === 'string' && id !== '' && !names.has(id)) names.set(id, `${r.display_name || r.email}\u2019s workspace`)
  }
  return (tenant) => names.get(tenant) ?? tenant
}

/** The page's lender namer; the bare id outside a People page. */
const LenderName = createContext<(tenant: string) => string>((tenant) => tenant)

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
function claudeText(c: PersonClaudeAccount, lender: (tenant: string) => string = (t) => t): ReactNode {
  if (c.source === 'own') return `own (${c.own})`
  if (c.source === 'lent' && c.lent_accounts !== undefined && c.lent_accounts.length > 0) {
    // Each id is one token (`ai-eng-4` broke as "ai-" over "eng-4" at 390,
    // rendered review of PR 2464): the line wraps between words, not in one.
    return c.lent_accounts.map((a, i) => (
      <Fragment key={`${a.owner_tenant}/${a.account_id}`}>
        {i > 0 ? '; ' : ''}
        <span className="pp-nobr">{a.account_id}</span>
        {accountWithId(a).slice(a.account_id.length)}, lent by <span className="pp-nobr">{a.owner_name ?? a.owner_tenant}</span>
      </Fragment>
    ))
  }
  // The lenders' tenant ids are tokens like the workspace ids beside them:
  // "lent by u-" over "bogdan" read as a lender called "u-" (rendered review).
  if (c.source === 'lent') return c.lent_by.length > 0 ? <>lent by <span className="pp-nobr">{c.lent_by.map(lender).join(', ')}</span></> : `lent (${c.lent})`
  if (c.source === 'provider_key') return 'provider key'
  return 'none'
}

/** The admin role document for this email, if the roles were read and one exists. */
function holderOf(doc: PeopleDoc, email: string): AdminHolder | undefined {
  return Array.isArray(doc.admins) ? doc.admins.find((a) => a.email === email) : undefined
}

/** One write's outcome, said where it was made from. */
type Said = { ok: true; text: string; command?: string } | { ok: false; error: ApiError }

function SaidLine({ said }: { said: Said | null }) {
  if (said === null) return null
  if (said.ok && said.command !== undefined) {
    // A join approved with platform memberships off: the one line that admits the person.
    return (
      <div className="pp-said-cmd" role="status">
        <p className="ur-hint">{said.text}</p>
        <pre className="pp-cmd">
          <code>{said.command}</code>
        </pre>
      </div>
    )
  }
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

/**
 * ESCAPE CANCELS A DENY REASON (visual QA pass 1, admin-shell-08,
 * 2026-10-28): only Cancel closed the inline form, so Escape left it open and
 * a keyboard user tabbed on and lost their place. Escape now does what Cancel
 * does, and the form's effect hands focus back to Deny, as the Restore and
 * Grant admin dialogs do. It stops there: inside the Manage drawer, Escape
 * closes the reason, not the drawer.
 */
function cancelOnEscape(e: ReactKeyboardEvent<HTMLElement>, cancel: () => void): void {
  if (e.key !== 'Escape') return
  e.preventDefault()
  e.stopPropagation()
  cancel()
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
    <span className="pp-deny" onKeyDown={(e) => cancelOnEscape(e, () => setOpen(false))}>
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
  const lender = useContext(LenderName)
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
  const name = chosen === null ? '' : `${named(chosen)} (${lender(chosen.owner_tenant)})`
  const idle = chosen !== null && chosen.state !== null && NOT_ASSIGNING.has(chosen.state)
  const verb = lend ? 'Lend' : 'Reclaim'
  const one = !lend && accounts.length === 1 ? accounts[0]! : null
  return (
    <div className="pp-loan">
      {one !== null ? (
        <span className="pp-loan-one" data-testid="pp-loan-one">
          Lent to them: <strong>{accountWithId(one)}</strong> · {lender(one.owner_tenant)}
          {one.state !== null ? ` · ${accountStateWord(one.state)}` : ''}
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
                {named(a)} · {lender(a.owner_tenant)}
                {a.state !== null ? ` · ${accountStateWord(a.state)}` : ''}
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
              ? `Their work may then run on an account ${lender(chosen.owner_tenant)} owns.`
              : `${lender(chosen.owner_tenant)} stops lending it to them; their new work is no longer offered it.`}
          </p>
          {lend && idle && (
            <p className="ur-hint" role="note" data-testid="pp-loan-idle">
              This account is {accountStateWord(chosen.state ?? '')}: it starts no new agent until its owner makes it available again.
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
  // WHAT APPROVING DOES, NOT WHAT IS STORED (visual QA pass 1, admin-shell-20,
  // 2026-10-28): "the record carries no limits" told the approver nothing.
  // With none asked for, Approve creates the workspace at `DEFAULT_CEILING`.
  if (parts.length === 0) return <span className="ur-hint">No ceiling was asked for: approving sets the default, {DEFAULT_CEILING} agents at once</span>
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

// ---------------------------------------------------------------------------
// JOIN REQUESTS (JOIN-UI-ADMIN; part of #2325; docs/design/join-requests.md
// §4.6, config-e.html S5f: "1 person is waiting to join Example Labs")
// ---------------------------------------------------------------------------
// A person asked to join a group workspace with its join link, and that
// workspace's admins and the platform admins decide it here. The People read
// carries the open requests its reader decides (`join_requests`, oldest last
// as served; drawn oldest first), and a requester's cell reads `requested`.
// These types live here, not in types.ts: the two join UI lanes never share
// a file (§6's note under the table).

/** One open request to join, as `People.join_rows` serves it: never a document id, never a MAC. */
export interface JoinRequestRow {
  /** The handle the decision routes take. Not a secret. */
  ref: string
  tenant_id: string
  /** The workspace's name, or its group when it has none. */
  display_name: string
  requester: string
  requester_name?: string | null
  requested_at: string
  expires_at: string
  /** Redacted when it was written. */
  message?: string | null
}

/** A grid cell with the join's word: `requested`, an open request and nothing else there. */
type JoinCell = PeopleRoleCell | 'requested'

/** What the People read adds for join requests; null with its code when they were not read; absent from an older API. */
interface JoinFields {
  join_requests?: JoinRequestRow[] | null
  join_requests_error?: string | null
}

/** One platform membership, `GET /v1/workspace/members`. */
export interface WorkspaceMember {
  email: string
  by: string
  at: string
  via?: string
  /** The workspace's group changed since: it admits nobody now. */
  group_changed: boolean
  /** A fresh check finds them in the Google group too; null when the directory did not answer. */
  still_in_group: boolean | null
}

interface WorkspaceMembersDoc {
  platform_memberships: boolean
  members: WorkspaceMember[]
}

/** `POST /v1/admin/join-requests/{ref}/approve`'s answer. */
interface JoinApproval {
  email: string
  tenant_id: string
  role: string
  /** False with platform memberships off: the group decides, and `group_change` says how. */
  admitted: boolean
  group_change: { group: string; command: string } | null
}

/** The open join requests, oldest first (an undated one last). */
export function openJoins(doc: JoinFields): JoinRequestRow[] {
  const rows = Array.isArray(doc.join_requests) ? doc.join_requests : []
  const when = (r: JoinRequestRow) => new Date(r.requested_at).getTime() || Number.POSITIVE_INFINITY
  return [...rows].sort((a, b) => when(a) - when(b))
}

/** fetch.ts READ_INIT's form: the init's credential mode, named once (a literal reads as a key to the publish scan). */
const MEMBERS_INIT = { sameOrigin: 'same-origin' } as const satisfies Record<string, RequestCredentials>

/**
 * ONE WORKSPACE'S PLATFORM MEMBERSHIPS: `GET /v1/workspace/members` with
 * `X-Swarm-Tenant` naming `tenantId`, so the grid reads every column's list
 * without switching the workspace in hand (as `loadWorkspaceSetup` reads each
 * workspace's setup). The header selects among the reader's verified
 * memberships and grants none, so a workspace they may not read is the API's
 * refusal, drawn as unread. Failures go through fetch.ts's one classifier.
 */
export async function loadWorkspaceMembers(tenantId: string, signal?: AbortSignal): Promise<Result<WorkspaceMembersDoc>> {
  // The fixture build serves no members list, and a mark is never a guess.
  if (USE_FIXTURES) return { status: 'error', error: { kind: 'not_found', httpStatus: null, code: null, message: 'The fixture build serves no members list.' } }
  const target = route('/v1/workspace/members')
  let res: Response
  try {
    // Behind IAP the browser holds the session cookie; nothing here handles a token.
    res = await globalThis.fetch(target.url, { headers: { accept: 'application/json', [TENANT_HEADER]: tenantId }, credentials: MEMBERS_INIT.sameOrigin, signal })
  } catch (err) {
    return { status: 'error', error: { kind: 'unreachable', httpStatus: null, code: null, message: err instanceof Error ? err.message : 'The request did not complete.' } }
  }
  if (!(res.headers.get('content-type') ?? '').includes('application/json')) {
    return { status: 'error', error: { kind: 'session_expired', httpStatus: res.status, code: null, message: 'The API answered with a page instead of data, which is how an expired sign-in arrives. Reload to sign in again.' } }
  }
  let body: unknown = null
  try {
    body = await res.json()
  } catch {
    body = null
  }
  const env = typeof body === 'object' && body !== null ? (body as Record<string, unknown>) : null
  if (!res.ok) {
    const envelope: ErrorEnvelope | null =
      env === null ? null : typeof env.message === 'string' ? (env as ErrorEnvelope) : { message: typeof env.detail === 'string' ? env.detail : undefined }
    return { status: 'error', error: classifyFailure(res.status, envelope, undefined) }
  }
  if (env === null || !Array.isArray(env.members)) {
    return { status: 'error', error: { kind: 'server_error', httpStatus: res.status, code: null, message: 'The members list was not readable.' } }
  }
  return { status: 'ok', data: body as WorkspaceMembersDoc, fetchedAt: Date.now() }
}

/** One column's members list: read (email -> membership), failed (with why), or still being read. */
type MembersRead = { ok: true; members: ReadonlyMap<string, WorkspaceMember> } | { ok: false; why: string }

/**
 * THE "JOINED BY REQUEST" MARK'S SOURCE (§4.6; BGD 2026-10-26): each
 * column's members list, read once per set of columns and again after a
 * decision (`version`). A column absent from the answer is still being read
 * or was not read: it draws no mark, never a guess.
 */
function useWorkspaceMembers(columns: readonly PeopleWorkspaceColumn[] | null, version: number): Readonly<Record<string, MembersRead>> {
  const ids = (columns ?? []).filter((c) => !c.unavailable).map((c) => c.tenant_id)
  const key = ids.join('\n')
  const [reads, setReads] = useState<Record<string, MembersRead>>({})
  useEffect(() => {
    const abort = new AbortController()
    for (const id of key === '' ? [] : key.split('\n')) {
      void loadWorkspaceMembers(id, abort.signal).then((r) => {
        if (abort.signal.aborted) return
        const read: MembersRead | null =
          r.status === 'ok' || r.status === 'stale'
            ? { ok: true, members: new Map(r.data.members.map((m) => [m.email.toLowerCase(), m])) }
            : r.status === 'empty'
              ? { ok: true, members: new Map() }
              : r.status === 'error'
                ? { ok: false, why: r.error.kind }
                : null
        if (read === null) return
        setReads((was) => ({ ...was, [id]: read }))
      })
    }
    return () => abort.abort()
  }, [key, version])
  return reads
}

/** The membership `email` holds in `tenant`, when that column's list was read and names them. */
function joinedIn(reads: Readonly<Record<string, MembersRead>>, tenant: string, email: string): WorkspaceMember | undefined {
  const read = reads[tenant]
  return read?.ok === true ? read.members.get(email.toLowerCase()) : undefined
}

/**
 * WHY A MEMBERS LIST WAS NOT READ, IN WORDS (visual QA pass 1, admin-shell-05,
 * 2026-10-28): the note printed the error's raw kind, "eng (not_found)". Only
 * the kinds a reader can act on get their own words; the rest "could not be
 * read". `plural` picks the verb for one list or several.
 */
export function membersUnreadWhy(kind: string, plural: boolean): string {
  const was = plural ? 'were' : 'was'
  if (kind === 'not_found') return `${was} not found`
  if (kind === 'forbidden' || kind === 'admin_required') return `${was} refused to you`
  return 'could not be read'
}

/**
 * Said once, above the grid: the columns whose members list was not read, so
 * no "joined by request" mark is drawn in them. THE ROLES IN THOSE COLUMNS ARE
 * A SEPARATE READ (admin-shell-05): the note said "could not be read" above a
 * column full of roles, and read as if the roles were unread too, so it says
 * which mark is missing and that the roles stand.
 */
function MembersUnread({ columns, reads }: { columns: readonly PeopleWorkspaceColumn[]; reads: Readonly<Record<string, MembersRead>> }) {
  const failed = columns.flatMap((c) => {
    const read = reads[c.tenant_id]
    return read !== undefined && !read.ok ? [{ name: c.display_name, why: read.why }] : []
  })
  const first = failed[0]
  if (first === undefined) return null
  const one = failed.length === 1
  const same = failed.every((f) => f.why === first.why)
  const lists = same
    ? `${one ? 'its members list' : 'their members lists'} ${membersUnreadWhy(first.why, !one)}`
    : listed(failed.map((f) => `${f.name}’s members list ${membersUnreadWhy(f.why, false)}`))
  return (
    <p className="ur-hint pp-joined-unread">
      No “joined by request” mark can be drawn in the {listed(failed.map((f) => f.name))} {one ? 'column' : 'columns'}: {lists}. The roles shown{' '}
      {one ? 'there' : 'in them'} are read separately and are not affected.
    </p>
  )
}

/** The mark beside a role (§4.6): this person is in by a platform membership, not by the Google group alone. */
function JoinedMark({ member }: { member: WorkspaceMember }) {
  const word = member.group_changed ? 'joined by request, group changed' : 'joined by request'
  const why = member.group_changed
    ? `Let in by ${member.by} ${timeAgo(member.at)}. The workspace's group changed since, so this membership admits nobody.`
    : `Let in by ${member.by} ${timeAgo(member.at)}${member.still_in_group === true ? '; also in the Google group' : ''}.`
  return (
    <span className={`pp-joined${member.group_changed ? ' is-changed' : ''}`} title={why}>
      {word}
    </span>
  )
}

/** What an approval gives (Q4 (a)): the workspace's default, or viewer below it; never admin. */
function offeredRoles(defaultRole: string | null | undefined): string[] {
  const out = defaultRole === 'member' || defaultRole === 'viewer' ? [defaultRole] : []
  if (!out.includes('viewer')) out.push('viewer')
  return out
}

function joinWrite<T>(path: string, ref: string, body: unknown): Promise<Result<T>> {
  if (USE_FIXTURES) {
    return Promise.resolve({ status: 'error', error: { kind: 'not_found', httpStatus: null, code: null, message: 'The fixture build does not decide join requests.' } })
  }
  return write(route(path, { ref }), 'POST', body) as Promise<Result<T>>
}

/**
 * APPROVE A JOIN. The role is the workspace's default, preselected; the one
 * below it, viewer, is the only other choice, and a default sends no role at
 * all, so the API gives whatever the default is when it decides. With
 * platform memberships off the API admits nobody (`admitted: false`) and
 * answers the group command, which the card says with the command in full.
 */
function JoinApprove({ join, defaultRole, reload, onSaid }: { join: JoinRequestRow; defaultRole: string | null | undefined; reload: () => void; onSaid: (s: Said) => void }) {
  const roles = offeredRoles(defaultRole)
  const known = defaultRole === 'member' || defaultRole === 'viewer'
  const [role, setRole] = useState(roles[0] ?? 'viewer')
  const [busy, setBusy] = useState(false)
  const go = async () => {
    if (busy) return
    setBusy(true)
    const r = await joinWrite<JoinApproval>('/v1/admin/join-requests/{ref}/approve', join.ref, known && role === defaultRole ? {} : { role })
    setBusy(false)
    if (r.status === 'error') onSaid({ ok: false, error: r.error })
    else if (r.status === 'ok' || r.status === 'stale') {
      const d = r.data
      onSaid(
        d.admitted || d.group_change === null
          ? { ok: true, text: `Approved: ${d.email} joined ${join.display_name} as ${d.role}.` }
          : {
              ok: true,
              text: `Approved, waiting for the group change: platform memberships are off here, so ${d.email} is not in ${join.display_name} until they are added to ${d.group_change.group}. Run:`,
              command: d.group_change.command,
            },
      )
    } else onSaid({ ok: true, text: `Approved ${join.requester}.` })
    reload()
  }
  return (
    <span className="pp-join-approve">
      {roles.length > 1 && (
        <select className="c-inp pp-join-role" aria-label={`Role for ${join.requester}`} value={role} disabled={busy} onChange={(e) => setRole(e.target.value)}>
          {roles.map((r) => (
            <option key={r} value={r}>
              {r === defaultRole ? `${r} (the default)` : r}
            </option>
          ))}
        </select>
      )}
      <Button size="sm" kind="primary" disabled={busy} onClick={() => void go()}>
        {busy ? 'Approving…' : 'Approve'}
      </Button>
    </span>
  )
}

/** DENY A JOIN: a reason is required and shown to the person (§4.3), so a blank one is never sent. */
function JoinDeny({ join, reload, onSaid }: { join: JoinRequestRow; reload: () => void; onSaid: (s: Said) => void }) {
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const blank = reason.trim() === ''
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
    if (busy || blank) return
    setBusy(true)
    const r = await joinWrite('/v1/admin/join-requests/{ref}/deny', join.ref, { reason: reason.trim() })
    setBusy(false)
    if (r.status !== 'error') {
      setOpen(false)
      setReason('')
    }
    onSaid(outcome(r, () => `Denied ${join.requester}’s request to join ${join.display_name}. They see the reason.`))
    reload()
  }
  if (!open) {
    return (
      <Button size="sm" id={denyId} onClick={() => setOpen(true)}>
        Deny
      </Button>
    )
  }
  return (
    <span className="pp-deny" onKeyDown={(e) => cancelOnEscape(e, () => setOpen(false))}>
      <label className="c-field">
        <span className="c-lbl">Reason, shown to {join.requester}</span>
        <textarea ref={reasonRef} className="c-inp" value={reason} maxLength={JOIN_REASON_MAX} rows={2} onChange={(e) => setReason(e.target.value)} />
      </label>
      <span className="ob-acts">
        <Button size="sm" onClick={() => setOpen(false)}>
          Cancel
        </Button>
        <Button size="sm" kind="primary" disabled={blank || busy} onClick={() => void go()}>
          {busy ? 'Denying…' : 'Deny'}
        </Button>
      </span>
    </span>
  )
}

/** `joinrequests.REASON_MAX`: the API refuses a longer reason, so the field stops there. */
const JOIN_REASON_MAX = 200

/** One open join on Waiting for you: who, to join which workspace, when, their message, and the decision. */
function JoinReq({ join, columns, reload, onSaid }: { join: JoinRequestRow; columns: readonly PeopleWorkspaceColumn[] | null; reload: () => void; onSaid: (s: Said) => void }) {
  const defaultRole = columns?.find((c) => c.tenant_id === join.tenant_id)?.default_role
  return (
    <li className="pp-req" data-kind="join" data-ref={join.ref} data-email={join.requester}>
      <div className="pp-req-who">
        <Avatar email={join.requester} />
        <span className="pp-who" title={join.requester}>
          <PersonName email={join.requester} name={join.requester_name ?? null} variant="full" />
        </span>
      </div>
      <div className="pp-req-what">
        <span>
          to join <b>{join.display_name}</b> · <Requested at={join.requested_at} />
        </span>
        {typeof join.message === 'string' && join.message.trim() !== '' && <q className="pp-join-msg">{join.message}</q>}
      </div>
      <div className="pp-req-acts">
        <JoinApprove join={join} defaultRole={defaultRole} reload={reload} onSaid={onSaid} />
        <JoinDeny join={join} reload={reload} onSaid={onSaid} />
      </div>
    </li>
  )
}

/** The join requests were not read: the rest of the page is served, and this says why nobody may appear to wait. */
function JoinsUnread({ code }: { code: string }) {
  return (
    <Banner tone="warn" title="The requests to join could not be read">
      Who is waiting to join a workspace is not shown ({code}). Read again shortly.
    </Banner>
  )
}

/** Bring a Waiting for you row into view and focus its first decision. */
function reviewRow(match: (li: HTMLElement) => boolean): void {
  const li = [...document.querySelectorAll<HTMLElement>('.pp-waiting li.pp-req')].find(match)
  if (li === undefined) return
  li.scrollIntoView?.({ block: 'center' })
  ;(li.querySelector<HTMLElement>('button, select, input') ?? li).focus()
}

/**
 * WAITING FOR YOU (#1206 part 1). Every open request, oldest first, each with
 * its decision beside it. Not drawn when nothing is open. Once a decision is
 * made the request leaves the list on the next read, so its outcome is said
 * under the list, and the card stays for that sentence.
 */
function WaitingCard({ doc, rows, reload }: { doc: PeopleDoc & JoinFields; rows: PersonRow[]; reload: () => void }) {
  const [said, setSaid] = useState<Said | null>(null)
  const items = waitingRequests(rows)
  // JOIN-UI-ADMIN: a platform admin decides every join request too (§4.6).
  const joins = openJoins(doc)
  const total = items.length + joins.length
  if (total === 0 && said === null) return null
  const oldest = oldestAt([...items.map((i) => i.at), ...joins.map((j) => j.requested_at)])
  return (
    <Card
      className="pp-waiting"
      title="Waiting for you"
      action={
        total > 0 ? (
          <Chip>
            {total} waiting{oldest !== null ? ` · oldest ${timeAgo(oldest)}` : ''}
          </Chip>
        ) : undefined
      }
    >
      {total === 0 ? (
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
                    {/* WHAT IS ASKED FOR, BY KIND AND PERSON (visual QA pass 2,
                        admin2-22, 2026-10-29): 'Workspace w-c4r01e' named the
                        request by a generated id an approver cannot place. A
                        person's row asks for their own workspace, so the line
                        says so; the id it will have is the secondary line. */}
                    <span>
                      A personal workspace for {row.email} · <Requested at={at} />
                    </span>
                    {row.workspace.workspace_id != null && row.workspace.workspace_id !== '' && (
                      <small className="pp-req-id">
                        id <code className="pp-id">{row.workspace.workspace_id}</code>
                      </small>
                    )}
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
          {joins.map((j) => (
            <JoinReq key={`join:${j.ref}`} join={j} columns={gridColumns(doc)} reload={reload} onSaid={setSaid} />
          ))}
        </ul>
      )}
      <SaidLine said={said} />
    </Card>
  )
}

/**
 * WAITING FOR YOU, A WORKSPACE ADMIN'S (S5f): the requests to join the
 * workspaces they administer, and nothing else -- a personal workspace and a
 * loan are a platform admin's. Not drawn when nothing is open.
 */
function JoinWaitingCard({ joins, columns, reload }: { joins: JoinRequestRow[]; columns: readonly PeopleWorkspaceColumn[]; reload: () => void }) {
  const [said, setSaid] = useState<Said | null>(null)
  if (joins.length === 0 && said === null) return null
  const oldest = oldestAt(joins.map((j) => j.requested_at))
  return (
    <Card
      className="pp-waiting"
      title="Waiting for you"
      action={
        joins.length > 0 ? (
          <Chip>
            {joins.length} waiting{oldest !== null ? ` · oldest ${timeAgo(oldest)}` : ''}
          </Chip>
        ) : undefined
      }
    >
      {joins.length === 0 ? (
        <p className="ur-hint">Nothing else is waiting.</p>
      ) : (
        <ul className="pp-reqs" aria-label="Open requests">
          {joins.map((j) => (
            <JoinReq key={j.ref} join={j} columns={columns} reload={reload} onSaid={setSaid} />
          ))}
        </ul>
      )}
      <SaidLine said={said} />
    </Card>
  )
}

/** The earliest of these times, or null when none is dated. */
function oldestAt(times: readonly (string | null)[]): string | null {
  let best: string | null = null
  for (const t of times) {
    if (t === null || !Number.isFinite(new Date(t).getTime())) continue
    if (best === null || new Date(t).getTime() < new Date(best).getTime()) best = t
  }
  return best
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
      {/* The grid says the platform role in a column of its own (`PlatformRoleCell`); an older API's table keeps the owner's chip. */}
      {gridColumns(doc) === null && holderOf(doc, row.email)?.role === 'owner' && <Chip className="pp-owner">platform owner</Chip>}
    </span>
  )
}

/**
 * THE GRID'S PLATFORM ROLE COLUMN (S5c, visual QA VQ-051): "platform owner",
 * "platform admin", or a dash for no platform role. It was a chip beside the
 * name, which took the room the name needed ("Bogdan Alexand…" at 1440,
 * VQ-052). When `admins` was not read, the dash says that instead: an unread
 * list is not "nobody holds one". A member of an ADMIN_GROUPS group holds no
 * document and reads as none here, as it does in Admins.
 */
function PlatformRoleCell({ row, doc }: { row: PersonRow; doc: PeopleDoc }) {
  const role = holderOf(doc, row.email)?.role
  return (
    <td role="cell" data-label="Platform role" className="pp-prole">
      {!Array.isArray(doc.admins) ? (
        <Dash why="The platform roles were not read" />
      ) : role === 'owner' || role === 'admin' ? (
        <span className={`pp-role-w is-${role}`}>platform {role}</span>
      ) : (
        <Dash why="No platform role" />
      )}
    </td>
  )
}

/** The grid's columns, or null for an older API's doc, which draws the Teams column instead. */
function gridColumns(doc: { workspaces?: PeopleWorkspaceColumn[] | null }): PeopleWorkspaceColumn[] | null {
  return Array.isArray(doc.workspaces) ? doc.workspaces : null
}

/**
 * ONE CELL OF THE GRID: the person's role in one workspace, as the API said it
 * (`people.role_cell`). `none` is a word, not a dash: it is a measurement --
 * not seen there and no role of their own. A dash is only for a workspace
 * whose roles were not read, and says why.
 *
 * A PLATFORM ADMIN'S CELL IS THEIR EFFECTIVE ROLE, "admin (platform admin)"
 * (BGD 2026-10-26, VQ-ROLES; part of #2391): `RoleStore.resolve` makes them
 * admin in every group workspace and reads no grant for them, so Config ›
 * People and roles, This workspace and Profile all say admin, and the grid
 * saying their stored `member` was the odd answer out (VQ-044). The rule is
 * `people.role_cell`'s too, and the API marks the row it applied it to,
 * `platform_admin: true` (`people.mark_platform_admin`). The suffix is drawn
 * from that marker, not from the admins list: a workspace admin's scoped read
 * serves no admins list, and every reader is to see where the cell came from
 * (BGD 2026-10-26, "Mark the source"; ROLE-MARK). The drawer still edits the
 * stored grant, which stays inert while they are one.
 */
function RoleCell({
  cell,
  column,
  email,
  platformAdmin,
  join,
  joined,
}: {
  cell: JoinCell | undefined
  column: PeopleWorkspaceColumn
  email: string
  platformAdmin: boolean
  /** Their open request to join this workspace, when `cell` is `requested` and the request was served. */
  join?: JoinRequestRow
  /** Their platform membership here, when this column's members list was read and names them. */
  joined?: WorkspaceMember
}) {
  const role = platformAdmin ? 'admin' : cell === undefined || cell === null || column.unavailable ? 'unread' : cell
  return (
    <td role="cell" data-label={column.display_name} data-tenant={column.tenant_id} data-role={role} data-source={platformAdmin ? 'platform_admin' : undefined} className="pp-role">
      {platformAdmin ? (
        // Two lines in one box (`.pp-role-pa`): the cell is nowrap, and on one
        // line it widened the grid from 1009px to 1102px at 1440, pushing
        // Manage behind the scroll; with every column marked even the
        // two-line form measured 1018px (rendered reviews, 2026-10-26/27), so
        // the suffix is the meta size and may break at its space. One box, so
        // the phone's grid-laid card keeps the two lines together. The space
        // keeps the text one phrase.
        <span className="pp-role-pa">
          <span className="pp-role-w is-admin">admin</span>{' '}
          <span className="pp-role-src">(platform admin)</span>
        </span>
      ) : role === 'unread' ? (
        <Dash why={`${column.display_name}’s roles could not be read`} />
      ) : role === 'none' ? (
        <span className="pp-role-none">none</span>
      ) : role === 'requested' ? (
        // JOIN-UI-ADMIN (§4.6): an open request to join, and nothing else here.
        <span className="pp-role-w is-requested" title={`${email} asked to join ${column.display_name} ${join !== undefined ? timeAgo(join.requested_at) : 'at an unknown time'}`}>
          requested
        </span>
      ) : (
        <span className={`pp-role-w is-${role}`}>{role}</span>
      )}
      {role !== 'unread' && joined !== undefined && (
        <>
          {' '}
          <JoinedMark member={joined} />
        </>
      )}
    </td>
  )
}

/** The props a row's cells need for join requests: the open requests, and each column's members list. */
interface JoinGrid {
  joins: readonly JoinRequestRow[]
  members: Readonly<Record<string, MembersRead>>
}

/**
 * A row as the grid reads it. `platform_admin`: the API applied the
 * platform-admin rule to this row's cells (`people.mark_platform_admin`), in
 * either read; absent on everyone else's row and from an older API.
 */
type MarkedRow = { email: string; roles?: Record<string, PeopleRoleCell>; platform_admin?: boolean }

/** Each column's cell; a marked row is drawn admin in every column (`RoleCell`). */
function roleCells(row: MarkedRow, columns: readonly PeopleWorkspaceColumn[], grid: JoinGrid): ReactNode[] {
  const platformAdmin = row.platform_admin === true
  return columns.map((c) => (
    <RoleCell
      key={c.tenant_id}
      cell={row.roles?.[c.tenant_id]}
      column={c}
      email={row.email}
      platformAdmin={platformAdmin}
      join={grid.joins.find((j) => j.tenant_id === c.tenant_id && j.requester === row.email)}
      joined={joinedIn(grid.members, c.tenant_id, row.email)}
    />
  ))
}

/**
 * A column's head is the workspace's NAME (`withWorkspaceNames`, VQ-056), its
 * group email the title. It was the email, which had to break before its `@`
 * because two of them whole pushed Manage behind the table's scroll at 1440
 * (rendered review, 2026-10-25); a name or an id is shorter than either.
 */
function ColumnHead({ column }: { column: PeopleWorkspaceColumn }) {
  return (
    <th role="columnheader" scope="col" data-tenant={column.tenant_id} title={column.group} className="pp-wscol">
      {column.display_name}
    </th>
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

/** Who lent an account: the owner tenant's display name, else its id. */
function lenderOf(a: PersonLentAccount): string {
  return a.owner_name ?? a.owner_tenant
}

/**
 * THE ROW'S CLAUDE ACCOUNT IS ONE SHORT LINE (S5c, visual QA VQ-052): seven
 * loans joined with '; ' ran eight lines, said "lent by Engineering" seven
 * times and made the row about 200px tall. Two or more loans are a count and
 * their lenders, each named once, with the accounts behind a disclosure; a
 * lender is repeated in it only when there is more than one. One loan, and
 * every other source, is already one line. The Manage drawer draws the same
 * line (`ClaudeSection`, rendered review of PR 2464): a phone's grid has no
 * Claude column, so the drawer is the only place a phone draws loans.
 */
function ClaudeSummary({ c }: { c: PersonClaudeAccount }) {
  const accounts = c.source === 'lent' ? (c.lent_accounts ?? []) : []
  const lender = useContext(LenderName)
  if (accounts.length < 2) return <>{claudeText(c, lender)}</>
  const lenders = [...new Set(accounts.map(lenderOf))]
  return (
    <details className="pp-loans">
      <summary className="pp-loan-sum">
        <span className="pp-nobr">{accounts.length} accounts ·</span> lent by{' '}
        {lenders.map((l, i) => (
          <Fragment key={l}>
            {i > 0 ? ', ' : ''}
            <span className="pp-nobr">{l}</span>
          </Fragment>
        ))}
      </summary>
      <ul className="pp-loan-list">
        {accounts.map((a) => (
          <li key={`${a.owner_tenant}/${a.account_id}`}>
            <span className="pp-nobr">{a.account_id}</span>
            {accountWithId(a).slice(a.account_id.length)}
            {lenders.length > 1 && (
              <>
                , lent by <span className="pp-nobr">{lenderOf(a)}</span>
              </>
            )}
          </li>
        ))}
      </ul>
    </details>
  )
}

function ClaudeCell({ row }: { row: PersonRow }) {
  return (
    <span className="pp-claude">
      <ClaudeSummary c={row.claude_account} />
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
  grid,
  onManage,
}: {
  row: PersonRow
  doc: PeopleDoc
  grid: JoinGrid
  /** The grid's columns as narrowed by the workspace filter; null for an older API's doc. */
  columns: PeopleWorkspaceColumn[] | null
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
      {columns !== null && <PlatformRoleCell row={row} doc={doc} />}
      {columns !== null
        ? roleCells(row, columns, grid)
        : !phone && (
            <td role="cell" data-label="Teams">
              {row.teams.length > 0 ? row.teams.join(', ') : <Dash why="Resolves to no team" />}
            </td>
          )}
      <td role="cell" data-label={columns !== null ? 'Own workspace' : 'Workspace'}>
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
      {/* THE GRID'S ONE LINE, NOT THE SEMICOLON PARAGRAPH (rendered review of
          PR 2464): seven loans printed "lent by Engineering" seven times, and
          on a phone this is the only place loans are drawn. A div, because a
          disclosure is not phrasing content a <p> may hold. */}
      <div className="pp-now">
        Now using{' '}
        <div className="pp-now-what">
          <ClaudeSummary c={row.claude_account} />
        </div>
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
      </div>
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
      ? 'Platform owner: set in the deployment’s configuration, so it is not switched here.'
      : on
        ? `Granted by ${holder.granted_by ?? 'unknown'}${holder.granted_at !== null ? ` ${timeAgo(holder.granted_at)}` : ''}. The last admin cannot be removed; the API says so when it refuses.`
        : 'Off. An admin through a group is an admin but has no role recorded here, so they show as off.'
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
              ? 'set in the deployment’s configuration'
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

/** What a grant gives, in both places Grant admin asks before it grants. */
const GRANT_GIVES = 'An admin can approve workspaces, change every ceiling and pool limit, lend accounts and grant admin to others.'

function granted(d: { email: string; changed: boolean }): string {
  return d.changed ? `${d.email} is now an admin.` : `${d.email} was already an admin.`
}

/**
 * GRANT ADMIN FROM THE GRID'S HEAD (S5c draws it there): the Admins card's
 * grant, as a dialog -- one email address, checked by the field's own
 * validity, then the same question before anything is sent (live QA ADM-4).
 */
function GrantDialog({ reload, onClose, onSaid }: { reload: () => void; onClose: () => void; onSaid: (s: Said) => void }) {
  const formId = useId()
  const [email, setEmail] = useState('')
  const [valid, setValid] = useState(false)
  const [asking, setAsking] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const typed = email.trim()
  const ask = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    if (typed === '' || !e.currentTarget.checkValidity()) return
    setAsking(typed)
  }
  const grant = async () => {
    if (busy || asking === null) return
    setBusy(true)
    const r = await grantAdmin(asking)
    setBusy(false)
    onSaid(outcome(r, granted))
    reload()
    onClose()
  }
  return (
    <Dialog
      title="Grant admin"
      onClose={onClose}
      actions={
        <>
          <Button onClick={onClose}>Cancel</Button>
          {asking === null ? (
            <Button kind="primary" type="submit" form={formId} disabled={!valid}>
              Grant admin
            </Button>
          ) : (
            <Button kind="primary" disabled={busy} onClick={() => void grant()}>
              {busy ? 'Granting…' : 'Grant admin'}
            </Button>
          )}
        </>
      }
    >
      {asking === null ? (
        <form id={formId} className="pp-grant-form" onSubmit={ask} noValidate>
          <label className="c-field">
            <span className="c-lbl">Email</span>
            <input
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
          {typed !== '' && !valid && <p className="ur-hint">Grant admin takes one email address.</p>}
        </form>
      ) : (
        <>
          <p>Make {asking} a platform admin?</p>
          <p className="ur-hint">{GRANT_GIVES}</p>
        </>
      )}
    </Dialog>
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
    setSaid(outcome(r, granted))
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
        be removed; the API says so when it refuses. An admin through a group has no role recorded here and is not
        listed.
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
          <p className="ur-hint">{GRANT_GIVES}</p>
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

/**
 * WHAT EACH ADMIN ACTION IS CALLED (visual QA VQ-052): Recent admin actions
 * printed the recorded kind, "qa_target_credential_set × 12". ONE TABLE, the
 * kinds the API's `admin_audit` writers record (people.py, admins.py,
 * purge.py, qatargets.py, repositories.py, roles.py, joinrequests.py and the
 * rest), each as what happened. A kind not in it -- a writer added later --
 * still reads as words, by `auditWords`' fallback, never as a raw token; the
 * kind itself stays in the name's `title`.
 */
export const AUDIT_ACTION_WORDS: Readonly<Record<string, string>> = {
  // People and roles: workspaces, loans, platform admins (people.py, admins.py).
  approve: 'Workspace approved',
  approve_own_workspace_auto: 'Own workspace approved automatically',
  request_workspace: 'Workspace requested',
  request_workspace_approved: 'Workspace requested and approved',
  deny: 'Workspace denied',
  retry: 'Workspace build retried',
  limits: 'Workspace ceiling set',
  lend: 'Claude account lent',
  reclaim: 'Claude account reclaimed',
  lend_pending: 'Claude account loan queued',
  reclaim_pending: 'Claude account reclaim queued',
  lend_pending_dropped: 'Queued loan dropped',
  loan_declined: 'Claude account loan declined',
  grant: 'Platform admin granted',
  remove: 'Platform admin removed',
  seed_owner: 'Platform owner recorded',
  owner_changed: 'Platform owner changed',
  history_purged: 'History purged',
  history_purge_failed: 'History purge failed',
  // Workspace roles, members and join requests.
  workspace_role_granted: 'Workspace role granted',
  workspace_role_cleared: 'Workspace role cleared',
  workspace_role_grant_invalid: 'Workspace role grant refused',
  workspace_default_role_set: 'Workspace default role set',
  workspace_member_added: 'Workspace member added',
  workspace_member_removed: 'Workspace member removed',
  join_requested: 'Join requested',
  join_approved: 'Join request approved',
  join_denied: 'Join request denied',
  join_withdrawn: 'Join request withdrawn',
  join_link_changed: 'Join link changed',
  platform_admin_acted: 'Platform admin acted in a workspace',
  platform_admin_viewed: 'Platform admin viewed a workspace',
  workspace_renamed: 'Workspace renamed',
  display_name_changed: 'Display name changed',
  tenant_enabled: 'Workspace enabled',
  tenant_disabled: 'Workspace disabled',
  // Secrets and keys.
  workspace_secret_created: 'Workspace secret created',
  workspace_secret_rotated: 'Workspace secret rotated',
  workspace_secret_deleted: 'Workspace secret deleted',
  workspace_secret_grant_enabled: 'Workspace secret grant enabled',
  workspace_secret_grant_disabled: 'Workspace secret grant disabled',
  workspace_secret_grant_attested: 'Workspace secret grant attested',
  workspace_secret_use_in_set: 'Workspace secret use set',
  workspace_secret_version_disabled: 'Workspace secret version disabled',
  workspace_secret_repository_grants_disabled: 'Workspace secret repository grants disabled',
  actions_secret_set: 'GitHub Actions secret set',
  actions_secret_deleted: 'GitHub Actions secret deleted',
  provider_key_set: 'Provider key set',
  provider_key_deleted: 'Provider key deleted',
  // Repositories, merge settings, code intelligence.
  repository_platform_set: 'Repository platform set',
  repository_platform_cleared: 'Repository platform cleared',
  repository_merge_policy_set: 'Repository merge policy set',
  repository_readable_by_tasks_set: 'Repository task access set',
  repository_workflow_files_set: 'Repository workflow files set',
  repository_context_set: 'Repository context set',
  agents_may_file_issues_set: 'Agent issue filing set',
  workspace_merge_settings_set: 'Workspace merge settings set',
  workspace_code_intelligence_set: 'Code intelligence set',
  judgment_backend_set: 'Judgment backend set',
  // QA targets and identities, MCP servers.
  qa_target_created: 'QA target created',
  qa_target_changed: 'QA target changed',
  qa_target_deleted: 'QA target deleted',
  qa_target_host_approved: 'QA target host approved',
  qa_target_credential_set: 'QA target credentials set',
  qa_target_challenge_bypass_set: 'QA target challenge bypass set',
  qa_target_migrated: 'QA target migrated',
  qa_target_issued: 'QA target issued',
  qa_target_route_dismissed: 'QA target route dismissed',
  qa_target_route_restored: 'QA target route restored',
  qa_identity_granted: 'QA identity granted',
  qa_identity_revoked: 'QA identity revoked',
  qa_identity_issued: 'QA identity issued',
  mcp_server_registered: 'MCP server registered',
  mcp_server_updated: 'MCP server updated',
  mcp_server_disabled: 'MCP server disabled',
  mcp_server_enabled: 'MCP server enabled',
}

/** Short words a fallback name keeps in capitals. */
const AUDIT_ACRONYMS: ReadonlySet<string> = new Set(['qa', 'mcp', 'ci', 'pr', 'id', 'url', 'iam', 'gcs', 'api'])

/**
 * An admin action in words: the table's, else the kind's own words --
 * `frobnicated_mcp_widget` reads "Frobnicated MCP widget".
 */
export function auditWords(action: string): string {
  const named = Object.prototype.hasOwnProperty.call(AUDIT_ACTION_WORDS, action) ? AUDIT_ACTION_WORDS[action] : undefined
  if (named !== undefined) return named
  const words = action
    .split(/[_\s.:-]+/)
    .filter((w) => w !== '')
    .map((w) => (AUDIT_ACRONYMS.has(w.toLowerCase()) ? w.toUpperCase() : w.toLowerCase()))
  if (words.length === 0) return action
  const [head, ...rest] = words
  return [head!.charAt(0).toUpperCase() + head!.slice(1), ...rest].join(' ')
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

/** `History purged × 48`, per housekeeping action in words, in the order they first appear. */
export function housekeepingSummary(entries: readonly AuditEntry[]): string {
  const counts = new Map<string, number>()
  for (const e of entries) if (HOUSEKEEPING.has(e.action)) counts.set(e.action, (counts.get(e.action) ?? 0) + 1)
  return [...counts].map(([a, n]) => `${auditWords(a)} × ${n}`).join(', ')
}

const WEEK_MS = 7 * 24 * 60 * 60 * 1000

/**
 * THE FOLDED HEAD'S COUNT (S5c, "12 this week"): the decisions -- not the
 * housekeeping the card folds away too -- recorded in the last seven days.
 * The read holds at most AUDIT_SHOWN entries, so when it is full and even its
 * oldest entry is from this week, the count is a floor and says so ("50+").
 */
export function thisWeek(entries: readonly AuditEntry[], now: number = Date.now()): string {
  const from = now - WEEK_MS
  const at = (e: AuditEntry) => (e.at === null ? Number.NaN : Date.parse(e.at))
  const n = entries.filter((e) => !HOUSEKEEPING.has(e.action) && at(e) >= from).length
  const oldest = entries.at(-1)
  const floor = entries.length >= AUDIT_SHOWN && oldest !== undefined && at(oldest) >= from
  return `${n}${floor ? '+' : ''} this week`
}

/**
 * RECENT ADMIN ACTIONS OPENS FOLDED (S5c): the page is for what waits and who
 * holds which role; the record is read when it is wanted. The head is the one
 * control and carries this week's count, so the folded card still says
 * whether anything happened.
 */
function Audit({ entries }: { entries: AuditEntry[] }) {
  const [open, setOpen] = useState(false)
  const [showHousekeeping, setShowHousekeeping] = useState(false)
  const bodyId = useId()
  const decisions = entries.filter((e) => !HOUSEKEEPING.has(e.action))
  const folded = entries.length - decisions.length
  const runs = auditRuns(showHousekeeping ? entries : decisions)
  const scope = entries.length >= AUDIT_SHOWN ? `the latest ${entries.length} entries` : `the ${entries.length} recorded ${entries.length === 1 ? 'entry' : 'entries'}`
  return (
    <Card
      className={`pp-audit${open ? ' is-open' : ''}`}
      title={
        <button type="button" className="pp-fold" aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen((o) => !o)}>
          <CIcon name="chevron" className="c-ic pp-fold-ic" />
          Recent admin actions
        </button>
      }
      action={<span className="ur-hint pp-week">{thisWeek(entries)}</span>}
    >
      <div id={bodyId} hidden={!open}>
        {!open ? null : entries.length === 0 ? (
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
                      {r.count > 1 && r.last.at !== null && r.first.at !== null && timeAgo(r.last.at) !== timeAgo(r.first.at) ? ` to ${timeAgo(r.last.at)}` : ''} ·{' '}
                      <b title={r.first.action}>{auditWords(r.first.action)}</b>
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
      </div>
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

/** What a waiting request asks for, in the lead's sentence. */
function askedFor(r: WaitingRequest): string {
  return r.kind === 'workspace' ? 'a workspace' : 'to borrow a Claude account'
}

/** "N people", "1 workspace": a count and its noun. */
function count(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`
}

/**
 * THE LEAD (S5c): who is waiting for an admin. Each open request on Waiting
 * for you, counted, and the OLDEST named -- the one that has waited longest
 * is the one to answer first -- with one button that takes the reader to it:
 * its row is scrolled into view and its first decision focused. Green when
 * nothing is open, and then no button.
 */
function PeopleLead({ rows, joins, joinsError }: { rows: readonly PersonRow[]; joins: readonly JoinRequestRow[]; joinsError: string | null }) {
  const items = waitingRequests(rows)
  // JOIN-UI-ADMIN: a join request counts with the others (§4.6), and the
  // oldest of all of them is the one named.
  const first = items[0]
  const join = joins[0]
  const named = join !== undefined && (first === undefined || first.at === null || new Date(join.requested_at).getTime() < new Date(first.at).getTime()) ? join : null
  if (first === undefined && join === undefined) {
    if (joinsError !== null) {
      // Nobody else waits, but the joins were not read: "nobody" would be a guess.
      return <AdminLead tone="unread" kicker="Not read" title="—, who is waiting to join a workspace could not be read" body={`No workspace or loan request is open; the join requests were not read (${joinsError}).`} />
    }
    return <AdminLead tone="ok" kicker="Nothing waiting" title="Nobody is waiting for an admin" body="A new workspace request, a request to join a workspace or a request to borrow a Claude account appears here first." />
  }
  const open = () =>
    named !== null
      ? reviewRow((el) => el.dataset.kind === 'join' && el.dataset.ref === named.ref)
      : first !== undefined && reviewRow((el) => el.dataset.kind === first.kind && el.dataset.email === first.row.email)
  const oldest =
    named !== null
      ? `${named.requester} asked to join ${named.display_name} ${timeAgo(named.requested_at)}`
      : first !== undefined
        ? `${first.row.email} asked for ${askedFor(first)} ${first.at !== null ? timeAgo(first.at) : 'at an unknown time'}`
        : ''
  return (
    <AdminLead
      tone="warn"
      kicker="Needs an admin"
      title={`${count(items.length + joins.length, 'request is', 'requests are')} waiting for an admin`}
      body={`The oldest: ${oldest}.`}
      action={
        <Button kind="primary" onClick={open}>
          Review the oldest
        </Button>
      }
    />
  )
}

/** The grid's columns were not read: the list is still served, and this says why the workspace columns are missing. */
function ColumnsUnread({ code }: { code: string }) {
  return (
    <Banner tone="warn" title="The workspaces’ roles could not be read">
      Each person’s own workspace and account are below; the workspace columns are not ({code}). Read again shortly.
    </Banner>
  )
}

/** The names a sentence lists: "A", "A and B", "A, B and C". */
function listed(names: readonly string[]): string {
  return names.length <= 1 ? (names[0] ?? '') : `${names.slice(0, -1).join(', ')} and ${names.at(-1)}`
}

/**
 * THE GRID'S WORKSPACE IS THIS DEVICE'S CHOICE, AND THE PAGE IS RIGHT WITHOUT
 * IT: storage throws in a private window and in previews, and the grid then
 * opens on every workspace. So does a remembered workspace this read no
 * longer serves -- removed, or not one this reader administers.
 */
export const PEOPLE_WORKSPACE_KEY = 'swarm.people.workspace'

/** Every workspace: never a tenant id, which is a slug. */
const EVERY = '*'

/** A person is IN a workspace when they hold a role there; `none` and an unread cell are not one. */
const HOLDS_ROLE: ReadonlySet<PeopleRoleCell> = new Set(['admin', 'member', 'viewer'])

function rememberedWorkspace(): string {
  try {
    return window.localStorage.getItem(PEOPLE_WORKSPACE_KEY) ?? EVERY
  } catch {
    return EVERY
  }
}

function rememberWorkspace(key: string): void {
  try {
    if (key === EVERY) window.localStorage.removeItem(PEOPLE_WORKSPACE_KEY)
    else window.localStorage.setItem(PEOPLE_WORKSPACE_KEY, key)
  } catch {
    // Nothing to do: this visit still shows the choice.
  }
}

type GridRow = { email: string; display_name?: string | null; roles?: Record<string, PeopleRoleCell> }

interface GridView<R> {
  /** The columns to draw: the chosen one, or every one; null for an older API's doc. */
  columns: PeopleWorkspaceColumn[] | null
  /** The chosen workspace, or null for every workspace. */
  column: PeopleWorkspaceColumn | null
  rows: R[]
  /** How many rows the chosen workspace holds before Find a person narrows them; every row when none is chosen. */
  inWorkspace: number
  typed: string
  setTyped: (v: string) => void
  choose: (key: string) => void
}

/**
 * THE GRID, NARROWED (S5c, S5f): to the chosen workspace's column and the
 * people who hold a role in it, then to the people whose name or email holds
 * what was typed. Both read the list already loaded; neither asks the API.
 */
function useGridView<R extends GridRow>(rows: readonly R[], columns: PeopleWorkspaceColumn[] | null): GridView<R> {
  const [chosen, setChosen] = useState(rememberedWorkspace)
  const [typed, setTyped] = useState('')
  const column = columns?.find((c) => c.tenant_id === chosen) ?? null
  const q = typed.trim().toLowerCase()
  const matches = (r: R) => q === '' || r.email.toLowerCase().includes(q) || (r.display_name ?? '').toLowerCase().includes(q)
  const held = rows.filter((r) => column === null || HOLDS_ROLE.has(r.roles?.[column.tenant_id] ?? null))
  return {
    columns: column === null ? columns : [column],
    column,
    rows: held.filter(matches),
    inWorkspace: held.length,
    typed,
    setTyped,
    choose: (key) => {
      setChosen(key)
      rememberWorkspace(key)
    },
  }
}

/**
 * THE WORKSPACE FILTER, IN THE GRID CARD'S HEAD: a platform admin's "Every
 * workspace" and one choice per workspace (S5c); a workspace admin's only the
 * workspaces they administer, "Both" when there are two (S5f). One workspace
 * leaves nothing to choose, so no filter is drawn.
 */
function WorkspaceFilter({ view, all, scoped }: { view: GridView<GridRow>; all: readonly PeopleWorkspaceColumn[]; scoped: boolean }) {
  if (all.length < 2) return null
  return (
    <span className="pp-filter">
      <span className="c-lbl" aria-hidden="true">
        Workspace
      </span>
      <Segmented<string>
        small
        label="Workspace"
        value={view.column?.tenant_id ?? EVERY}
        onChange={view.choose}
        options={[
          { key: EVERY, label: scoped && all.length === 2 ? 'Both' : 'Every workspace' },
          ...all.map((c) => ({ key: c.tenant_id, label: c.display_name })),
        ]}
      />
    </span>
  )
}

/**
 * THE HEAD'S COUNT, NARROWED WITH THE GRID (review of ADMIN-PEOPLE-FILTER).
 * The choice is remembered, so a reader who reloads sees a narrowed grid; a
 * head still saying the whole platform's total above it disagreed with the
 * rows drawn. With a workspace chosen it says how many of the total are in it.
 */
function narrowedCount(view: GridView<GridRow>, total: number): string | null {
  if (view.column === null) return null
  return `${view.inWorkspace} of ${count(total, 'person', 'people')} in ${view.column.display_name}`
}

/** "Find a person", over the loaded grid, and what it found: a count, or that nobody matches. */
function FindPerson({ view }: { view: GridView<GridRow> }) {
  const q = view.typed.trim()
  const n = view.rows.length
  const where = view.column !== null ? ` in ${view.column.display_name}` : ''
  return (
    <div className="pp-find">
      <input
        type="search"
        className="ur-search"
        aria-label="Find a person"
        placeholder="Find a person"
        value={view.typed}
        maxLength={200}
        onChange={(e) => view.setTyped(e.target.value)}
      />
      <span className="ur-hint" role="status">
        {q === '' ? '' : `${n === 0 ? 'Nobody' : count(n, 'person', 'people')}${where} ${n > 1 ? 'match' : 'matches'} “${q}”`}
      </span>
    </div>
  )
}

/** What the grid draws when its narrowing left nobody: why, in the reader's terms. */
function NobodyShown({ view }: { view: GridView<GridRow> }) {
  const c = view.column
  if (view.typed.trim() !== '') return <p className="ur-hint pp-nobody">Find a person reads the names and email addresses on this page.</p>
  if (c === null) return null
  return (
    <p className="ur-hint pp-nobody">
      {c.unavailable ? `${c.display_name}’s roles could not be read, so who is in it is not known.` : `Nobody holds a role in ${c.display_name}.`}
    </p>
  )
}

/**
 * A WORKSPACE ADMIN'S PAGE (S5f): the lead, then the grid of their workspaces
 * and nothing else. Nobody waits for a workspace admin here: a personal
 * workspace request and a loan are a platform admin's, so the lead says that
 * rather than drawing decisions this reader cannot make.
 */
function ScopedBody({ doc, reload }: { doc: ScopedPeopleDoc & JoinFields; reload: () => void }) {
  const columns = Array.isArray(doc.workspaces) ? doc.workspaces : []
  const rows: ScopedPersonRow[] = Array.isArray(doc.people) ? doc.people : []
  const phone = usePhoneTables()
  const names = listed(columns.map((c) => c.display_name))
  const view = useGridView(rows, columns)
  const shown = view.columns ?? columns
  // JOIN-UI-ADMIN (S5f): the requests to join these workspaces are this reader's to decide.
  const joins = openJoins(doc)
  const joinsError = doc.join_requests_error ?? null
  const [version, setVersion] = useState(0)
  const members = useWorkspaceMembers(columns, version)
  const decided = () => {
    reload()
    setVersion((v) => v + 1)
  }
  const grid: JoinGrid = { joins, members }
  const body = `Showing ${names}, the ${columns.length === 1 ? 'workspace' : 'workspaces'} you administer. A request for a personal workspace or a Claude account is a platform admin’s to decide.`
  const asking = new Set(joins.map((j) => j.requester)).size
  const into = listed([...new Set(joins.map((j) => j.display_name))])
  return (
    <>
      {joins.length > 0 ? (
        <AdminLead
          tone="warn"
          kicker="Your workspaces"
          title={`${count(asking, 'person is', 'people are')} waiting to join ${into}`}
          body={body}
          action={
            <Button kind="primary" onClick={() => reviewRow((el) => el.dataset.kind === 'join' && el.dataset.ref === joins[0]?.ref)}>
              {joins.length === 1 ? 'Review it' : `Review the ${joins.length}`}
            </Button>
          }
        />
      ) : joinsError !== null ? (
        <AdminLead tone="unread" kicker="Your workspaces" title={`—, who is waiting to join ${names} could not be read`} body={`${body} (${joinsError})`} />
      ) : (
        <AdminLead tone="ok" kicker="Your workspaces" title={`Nobody is waiting for you in ${names}`} body={body} />
      )}
      {joinsError !== null && <JoinsUnread code={joinsError} />}
      <JoinWaitingCard joins={joins} columns={columns} reload={decided} />
      <Card
        className="pp-people"
        title="People and their roles"
        action={
          <span className="pp-head-acts">
            <WorkspaceFilter view={view} all={columns} scoped />
            <Chip>{narrowedCount(view, doc.count) ?? count(doc.count, 'person', 'people')}</Chip>
          </span>
        }
      >
        {rows.length > 0 && <FindPerson view={view} />}
        <MembersUnread columns={columns} reads={members} />
        {rows.length === 0 ? (
          <p className="ur-hint">Nobody has been seen in {names} yet, and nobody holds a role there.</p>
        ) : view.rows.length === 0 ? (
          <NobodyShown view={view} />
        ) : (
          <div className={`table-wrap ${tableMode(phone)} pp-wrap`}>
            <table className="pools pp-table pp-grid" role="table">
              <thead role="rowgroup">
                <tr role="row">
                  <th role="columnheader" scope="col">Person</th>
                  {shown.map((c) => (
                    <ColumnHead key={c.tenant_id} column={c} />
                  ))}
                  <th role="columnheader" scope="col">Last active</th>
                </tr>
              </thead>
              <tbody role="rowgroup">
                {view.rows.map((row) => (
                  <tr key={row.email} role="row" data-email={row.email}>
                    <th role="rowheader" scope="row">
                      <span className="pp-person">
                        <Avatar email={row.email} />
                        <span className="pp-who" title={row.email}>
                          <PersonName email={row.email} name={row.display_name ?? null} variant="full" />
                        </span>
                      </span>
                    </th>
                    {roleCells(row, shown, grid)}
                    <td role="cell" data-label="Last active">
                      {row.last_active !== null ? timeAgo(row.last_active) : <Dash why="Not seen since roles were recorded" />}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  )
}

function isScoped(doc: PeopleDoc | ScopedPeopleDoc): doc is ScopedPeopleDoc {
  return doc.scope === 'workspaces'
}

/**
 * EACH WORKSPACE BY ITS NAME, NOT ITS GROUP EMAIL (visual QA VQ-056, BGD
 * 2026-10-26; part of #2391). The API names a column, and a join request's
 * workspace, by the group's address; the switcher calls the same workspace
 * 'Engineering', or by its id when it has no name. Every place the page draws
 * a workspace -- column heads, cells' labels, the filter, the lead, Waiting
 * for you -- reads `display_name`, so it is renamed here once, by the
 * switcher's rule (`groupWorkspaceName`). The address stays the head's title.
 */
function withWorkspaceNames<D extends { workspaces?: PeopleWorkspaceColumn[] | null } & JoinFields>(doc: D, named: readonly NamedTenantChoice[]): D {
  return {
    ...doc,
    workspaces: Array.isArray(doc.workspaces)
      ? doc.workspaces.map((c) => ({ ...c, display_name: groupWorkspaceName(c.tenant_id, c.display_name, named) }))
      : doc.workspaces,
    join_requests: Array.isArray(doc.join_requests)
      ? doc.join_requests.map((j) => ({ ...j, display_name: groupWorkspaceName(j.tenant_id, j.display_name, named) }))
      : doc.join_requests,
  }
}

function PeopleBody({ doc, reload }: { doc: PeopleDoc & JoinFields; reload: () => void }) {
  const rows = pendingFirst(Array.isArray(doc.people) ? doc.people : [])
  const phone = usePhoneTables()
  // The drawer follows the person, not a snapshot of their row: each read
  // redraws it from the list, so a decision made in it shows there.
  const [managing, setManaging] = useState<string | null>(null)
  const managed = managing === null ? null : (rows.find((r) => r.email === managing) ?? null)
  const all = gridColumns(doc)
  const view = useGridView(rows, all)
  const columns = view.columns
  const [granting, setGranting] = useState(false)
  const [said, setSaid] = useState<Said | null>(null)
  // JOIN-UI-ADMIN: the open join requests, and each column's members list for the mark.
  const joins = openJoins(doc)
  const joinsError = doc.join_requests_error ?? null
  const [version, setVersion] = useState(0)
  const members = useWorkspaceMembers(all, version)
  const decided = () => {
    reload()
    setVersion((v) => v + 1)
  }
  const grid: JoinGrid = { joins, members }
  const lender = useMemo(() => lenderNames(doc), [doc])
  return (
    <LenderName.Provider value={lender}>
      <PeopleLead rows={rows} joins={joins} joinsError={joinsError} />
      <BuildingOff doc={doc} />
      {joinsError !== null && <JoinsUnread code={joinsError} />}
      <WaitingCard doc={doc} rows={rows} reload={decided} />
      {doc.workspaces_error != null && <ColumnsUnread code={doc.workspaces_error} />}
      <Card
        className="pp-people"
        // Not the page's own heading again under it (visual QA V082).
        title="People and their roles"
        action={
          <span className="pp-head-acts">
            {all !== null && <WorkspaceFilter view={view} all={all} scoped={false} />}
            {/* The pending figure is the platform's; Waiting for you carries it while a workspace is chosen. */}
            <Chip>
              {narrowedCount(view, doc.count) ?? (
                <>
                  {doc.count} {doc.count === 1 ? 'person' : 'people'} ·{' '}
                  {doc.pending === 0 ? <span aria-label="none pending (measured)">none pending</span> : <>{doc.pending} pending</>}
                </>
              )}
            </Chip>
            <Button size="sm" onClick={() => setGranting(true)}>
              Grant admin
            </Button>
          </span>
        }
      >
        <SaidLine said={said} />
        {rows.length > 0 && <FindPerson view={view} />}
        {all !== null && <MembersUnread columns={all} reads={members} />}
        {rows.length === 0 ? (
          <p className="ur-hint">
            <span aria-label="no one has signed in (measured)">no one has signed in</span>
          </p>
        ) : view.rows.length === 0 ? (
          <NobodyShown view={view} />
        ) : (
          /* THE TENANTS PATTERN (Activity.tsx, design-system.md §7.3): the
              table has its own scroll container inside the card; below 560px
              it is a record per person. NOT `.ten-table`: that is Tenants'
              fixed layout, which split this table into equal columns and broke
              a workspace id at its hyphen. */
          <div className={`table-wrap ${tableMode(phone)} pp-wrap`}>
            <table className={`pools pp-table${columns !== null ? ' pp-grid' : ''}`} role="table">
              <thead role="rowgroup">
                <tr role="row">
                  <th role="columnheader" scope="col">Person</th>
                  {columns !== null && (
                    <th role="columnheader" scope="col">
                      Platform role
                    </th>
                  )}
                  {columns !== null
                    ? columns.map((c) => <ColumnHead key={c.tenant_id} column={c} />)
                    : !phone && <th role="columnheader" scope="col">Teams</th>}
                  <th role="columnheader" scope="col">{columns !== null ? 'Own workspace' : 'Workspace'}</th>
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
                {view.rows.map((row) => (
                  <Row key={row.email} row={row} doc={doc} columns={columns} phone={phone} grid={grid} onManage={setManaging} />
                ))}
              </tbody>
            </table>
          </div>
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
      {granting && <GrantDialog reload={reload} onClose={() => setGranting(false)} onSaid={setSaid} />}
    </LenderName.Provider>
  )
}

/** Admin › People and roles. */
export function PeopleScreen() {
  const people = useUrRead(loadPeople, 'people')
  const data = people.state.status === 'ok' || people.state.status === 'stale' ? people.state.data : null
  const scoped = data !== null && isScoped(data)
  // A row waiting on something the platform will not do by itself is not moving.
  const moving =
    data !== null &&
    !isScoped(data) &&
    Array.isArray(data.people) &&
    data.people.some((r) => IN_FLIGHT.has(r.workspace.state) && waitingOf(r.workspace) !== 'publishing_off')
  usePoll(PEOPLE_POLL_MS, people.reload, !moving)
  const choices = useStoredRoles()
  const named = useMemo(() => nameTenants(choices, null), [choices])
  return (
    <div className="ur-page pp-page">
      <PageHead title="People and roles">
        <UrRefresh reads={[people]} />
      </PageHead>
      <p className="ur-sub">
        {scoped
          ? 'The people in the workspaces you administer, and their role in each.'
          : 'Every person against every workspace, what they are waiting for, and who is an admin.'}
      </p>
      <UrRegion state={people.state} route="GET /v1/admin/people" what="People" plural onRetry={people.reload} lines={6}>
        {(d) =>
          isScoped(d) ? (
            <ScopedBody doc={withWorkspaceNames(d, named)} reload={people.reload} />
          ) : (
            <PeopleBody doc={withWorkspaceNames(d, named)} reload={people.reload} />
          )
        }
      </UrRegion>
    </div>
  )
}
