import { useState, type ReactNode } from 'react'
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
import { Button, Card, Chip, Dash, Dialog, ToneMark } from './components'
import type { ApiError, Result } from './fetch'
import { JobSteps } from './OnboardingWorkspace'
import { Mark } from './primitives'
import { UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import { PageHead, usePoll } from './Shell'
import type { AdminAuditEntry, LendableAccount, PeopleDoc, PersonClaudeAccount, PersonRow, WorkspaceState } from './types'
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
 * Under the table, the last 50 admin_audit entries.
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
        d.dispatch?.published === false
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
        <Button size="sm" onClick={() => setOpen(true)}>
          Deny
        </Button>
        <SaidLine said={said} />
      </>
    )
  }
  return (
    <span className="pp-deny">
      <label>
        Reason, shown to {row.email}
        <textarea value={reason} maxLength={500} rows={2} onChange={(e) => setReason(e.target.value)} />
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

function Ceiling({ row, reload }: { row: PersonRow; reload: () => void }) {
  const held = row.workspace.limits?.max_active ?? DEFAULT_CEILING
  const [value, setValue] = useState(String(held))
  const [busy, setBusy] = useState(false)
  const [said, setSaid] = useState<Said | null>(null)
  const n = Number(value)
  const whole = value.trim() !== '' && Number.isInteger(n)
  const go = async () => {
    if (busy || !whole) return
    setBusy(true)
    const r = await setWorkspaceCeiling(row.workspace.workspace_id ?? '', n)
    setBusy(false)
    setSaid(outcome(r, (d) => `Ceiling ${d.workspace.limits?.max_active ?? n} saved.`))
    reload()
  }
  return (
    <span className="pp-ceiling">
      <label>
        Ceiling
        <input type="number" min={1} step={1} value={value} onChange={(e) => setValue(e.target.value)} aria-label={`Ceiling for ${row.email}`} />
      </label>
      <Button size="sm" disabled={busy || !whole} onClick={() => void go()}>
        {busy ? 'Saving…' : 'Save'}
      </Button>
      <SaidLine said={said} />
    </span>
  )
}

function Loan({ row, lendable, lendableError, reload }: { row: PersonRow; lendable: LendableAccount[] | null; lendableError: string | null; reload: () => void }) {
  const [account, setAccount] = useState('')
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
    return (
      <p className="ur-hint">
        <Mark kind="zero" say="No account a group or you own can be lent" /> accounts to lend
      </p>
    )
  }
  const go = async (lend: boolean) => {
    if (busy || account === '') return
    setBusy(true)
    const r = await setWorkspaceLoan(row.workspace.workspace_id ?? '', account, lend)
    setBusy(false)
    setSaid(
      outcome(r, (d) =>
        d.changed ? (d.lent ? `Lent ${d.account_id}.` : `Reclaimed ${d.account_id}.`) : d.lent ? `${d.account_id} was already lent.` : `${d.account_id} was not lent.`,
      ),
    )
    reload()
  }
  return (
    <span className="pp-loan">
      {row.loan_request?.state === 'requested' && (
        <Chip>loan requested{row.loan_request.requested_at !== null ? ` ${timeAgo(row.loan_request.requested_at)}` : ''}</Chip>
      )}
      <label>
        Account
        <select value={account} onChange={(e) => setAccount(e.target.value)} aria-label={`Account to lend to ${row.email}`}>
          <option value="">choose…</option>
          {lendable.map((a) => (
            <option key={a.account_id} value={a.account_id}>
              {a.label ?? a.account_id} · {a.owner_tenant}
              {a.state !== null ? ` · ${a.state}` : ''}
            </option>
          ))}
        </select>
      </label>
      <Button size="sm" disabled={busy || account === ''} onClick={() => void go(true)}>
        Lend
      </Button>
      <Button size="sm" kind="ghost" disabled={busy || account === ''} onClick={() => void go(false)}>
        Reclaim
      </Button>
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
          <code>{w.workspace_id}</code>
        </>
      )}
      {w.state === 'failed' && failedAt !== null && <> ({failedAt})</>}
      {w.state === 'needs_owner' && (
        <p className="ur-hint">
          Waiting for the platform owner{heldAt !== null ? ` · stopped at ${heldAt}` : ''}
        </p>
      )}
      {w.state === 'denied' && w.decision?.reason != null && <p className="ur-hint">“{w.decision.reason}”</p>}
      {(IN_FLIGHT.has(w.state) || w.state === 'needs_owner') && <JobSteps record={w} />}
    </div>
  )
}

function Row({ row, doc, reload }: { row: PersonRow; doc: PeopleDoc; reload: () => void }) {
  const w = row.workspace
  const hasRecord = typeof w.workspace_id === 'string' && w.workspace_id !== ''
  return (
    <tr data-email={row.email} data-workspace-state={w.state}>
      <td>{row.email}</td>
      <td>{row.teams.length > 0 ? row.teams.join(', ') : <Dash why="Resolves to no team" />}</td>
      <td>{row.github}</td>
      <td>
        <WorkspaceCell row={row} />
      </td>
      <td>
        {claudeText(row.claude_account)}
        {row.loan_request?.state === 'requested' && <p className="ur-hint">loan requested</p>}
      </td>
      <td>{row.last_active !== null ? timeAgo(row.last_active) : <Dash why="Not seen active" />}</td>
      <td className="pp-acts">
        {hasRecord && w.state === 'requested' && (
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
      </td>
    </tr>
  )
}

function Admins({ reload }: { reload: () => void }) {
  const [email, setEmail] = useState('')
  const [busy, setBusy] = useState(false)
  const [confirm, setConfirm] = useState(false)
  const [said, setSaid] = useState<Said | null>(null)
  const typed = email.trim()
  const grant = async () => {
    if (busy || typed === '') return
    setBusy(true)
    const r = await grantAdmin(typed)
    setBusy(false)
    setSaid(outcome(r, (d) => (d.changed ? `${d.email} is now an admin.` : `${d.email} was already an admin.`)))
    reload()
  }
  const remove = async () => {
    if (busy || typed === '') return
    setBusy(true)
    const r = await removeAdmin(typed)
    setBusy(false)
    setConfirm(false)
    setSaid(outcome(r, (d) => `${d.email} is no longer an admin.`))
    reload()
  }
  return (
    <Card className="pp-admins" title="Admins">
      <p className="ur-hint">
        Any admin may grant or remove admin. The platform owner can be changed only by the owner, and the last admin cannot
        be removed; the API says so when it refuses.
      </p>
      <span className="ob-acts">
        <label>
          Email
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
        <Button size="sm" disabled={busy || typed === ''} onClick={() => void grant()}>
          Grant admin
        </Button>
        <Button size="sm" kind="ghost" disabled={busy || typed === ''} onClick={() => setConfirm(true)}>
          Remove admin
        </Button>
      </span>
      {confirm && (
        <Dialog
          title="Remove admin"
          onClose={() => setConfirm(false)}
          actions={
            <>
              <Button onClick={() => setConfirm(false)}>Cancel</Button>
              <Button kind="primary" disabled={busy} onClick={() => void remove()}>
                Remove
              </Button>
            </>
          }
        >
          <p>Remove admin from {typed}?</p>
        </Dialog>
      )}
      <SaidLine said={said} />
    </Card>
  )
}

function Audit({ entries }: { entries: AdminAuditEntry[] }) {
  return (
    <Card className="pp-audit" title="Recent admin actions">
      {entries.length === 0 ? (
        <p className="ur-hint">
          <Mark kind="zero" say="No admin action is recorded" /> admin actions recorded
        </p>
      ) : (
        <ol className="pp-audit-list" aria-label="Admin audit">
          {entries.map((e, i) => (
            <li key={`${e.at ?? ''}:${i}`}>
              {e.at !== null ? timeAgo(e.at) : <Dash why="The entry carries no time" />} · <b>{e.action}</b>{' '}
              <code>{e.target_workspace_id ?? e.target_email ?? '?'}</code> by {e.by}
            </li>
          ))}
        </ol>
      )}
    </Card>
  )
}

function PeopleBody({ doc, reload }: { doc: PeopleDoc; reload: () => void }) {
  const rows = pendingFirst(Array.isArray(doc.people) ? doc.people : [])
  return (
    <>
      <Card
        className="pp-people"
        title="People"
        action={
          <Chip>
            {doc.count} {doc.count === 1 ? 'person' : 'people'} ·{' '}
            {doc.pending === 0 ? <Mark kind="zero" say="No request is waiting for approval" /> : doc.pending} pending
          </Chip>
        }
      >
        {rows.length === 0 ? (
          <p className="ur-hint">
            <Mark kind="zero" say="No one has signed in" /> people have signed in
          </p>
        ) : (
          <table className="pools ten-table pp-table">
            <thead>
              <tr>
                <th>Person</th>
                <th>Teams</th>
                <th>GitHub</th>
                <th>Workspace</th>
                <th>Claude account</th>
                <th>Last active</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <Row key={row.email} row={row} doc={doc} reload={reload} />
              ))}
            </tbody>
          </table>
        )}
      </Card>
      <Admins reload={reload} />
      <Audit entries={Array.isArray(doc.audit) ? doc.audit : []} />
    </>
  )
}

/** Admin › People. */
export function PeopleScreen() {
  const people = useUrRead(loadPeople, 'people')
  const data = people.state.status === 'ok' || people.state.status === 'stale' ? people.state.data : null
  const moving = data !== null && Array.isArray(data.people) && data.people.some((r) => IN_FLIGHT.has(r.workspace.state))
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
