import { useId, useState, type FormEvent } from 'react'
import {
  clearAdminWorkspacePersonRole,
  loadAdminPersonRoles,
  loadAdminWorkspaceRoles,
  loadAdminWorkspacesRoles,
  setAdminWorkspaceDefaultRole,
  setAdminWorkspacePersonRole,
  type ApiError,
  type Result,
} from './api'
import { Button, Card, Dash } from './components'
import { UrRefresh, useUrRead, type UrRead } from './RepositoriesParts'
import { DEFAULT_ROLES } from './roles'
import {
  timeAgo,
  type AdminWorkspaceRoles,
  type AdminWorkspaceRolesPage,
  type DefaultRoleChange,
  type PersonRoleChange,
  type WorkspaceRoleOverride,
  type WorkspaceRoster,
} from './types'
import './styles/roles.css'

/**
 * ADMIN › PEOPLE'S WORKSPACE ROLES (lanes ROLES-UI-ADMIN, ROLES-ADMIN-LIST;
 * docs/design/workspace-roles.md §4.3, §4.8; owner answers Q2, Q4, Q5; part
 * of #1518).
 *
 * THE CARD lists each registered group workspace with its default role, its
 * admins and how many people hold a role of their own, and offers "Name an
 * admin", "Set default" and "Return to default" per person. It is how a group
 * workspace gets its FIRST admin (Q2): the default is never `admin` (§4.6), so
 * until a platform admin names one, a group workspace has none. Every write
 * goes through the admin routes (`/v1/admin/workspaces/{id}/roles…`), which
 * act without switching workspace and are recorded `via: platform_admin`.
 *
 * WHICH WORKSPACES. `GET /v1/admin/workspaces/roles` lists every registered
 * group workspace -- the Terraform groups, then the `ready` groups the
 * workspace job made -- with each one's default and overrides, a page at a
 * time: ONE read per page, never one per workspace. "Show more workspaces"
 * reads the next page; a write re-reads the page its workspace is on. Before
 * that route the card listed the admin's own `GET /v1/tenants/mine` and read
 * each roster alone, which missed every group outside the admin's reach and
 * cost a read per workspace. An entry the list could not serve (its page held
 * more grants than one read looks at, its grants did not verify, its id
 * collides with another principal's) says why, as the API said it, and
 * offers that workspace's own roster read.
 *
 * NEVER A PERSONAL WORKSPACE (§4.2.1, §4.5): its only admin is its person,
 * and the API lists none of them.
 *
 * ONLY OVERRIDES ARE LISTED: everyone else holds the default, and the
 * platform cannot enumerate a Google group (`searchTransitiveGroups` is 403
 * here, §4.3). A role given to someone not in the group is stored and applies
 * when they join (Q3); the write's answer says which.
 *
 * THE DRAWER's "Roles in workspaces" (`PersonWorkspaceRoles`) reads
 * `GET /v1/admin/roles/person?email=`: every override one person holds.
 */

const LIST_ROUTE = 'GET /v1/admin/workspaces/roles'

// "Read again", never "Retry": the drawer this sits in already offers a
// workspace's Retry, and the two must not share a name.
function refusal(e: ApiError): string {
  return e.code === null ? e.message : `${e.message} (${e.code})`
}

function dataOf<T>(r: Result<T>): T | null {
  return r.status === 'ok' || r.status === 'stale' ? r.data : null
}

/** Admin › People's Workspace roles card. */
export function WorkspaceRolesCard() {
  const first = useUrRead(() => loadAdminWorkspacesRoles(null), 'workspace-roles-admin:list')
  // The tokens of the pages read after the first, in order.
  const [more, setMore] = useState<string[]>([])
  const next = (token: string) => setMore((was) => (was.includes(token) ? was : [...was, token]))
  return (
    <Card className="wra-card" title="Workspace roles" action={<UrRefresh reads={[first]} />}>
      <p className="ur-hint">
        Each group workspace&rsquo;s default role, its admins and the people with a role of their own. A group workspace has no admin until
        one is named here; a personal workspace&rsquo;s only admin is its person.
      </p>
      <ListPage read={first} last={more.length === 0} onMore={next} />
      {more.map((token, i) => (
        <LaterPage key={token} token={token} last={i === more.length - 1} onMore={next} />
      ))}
    </Card>
  )
}

function LaterPage({ token, last, onMore }: { token: string; last: boolean; onMore: (token: string) => void }) {
  const read = useUrRead(() => loadAdminWorkspacesRoles(token), `workspace-roles-admin:list:${token}`)
  return <ListPage read={read} last={last} onMore={onMore} />
}

/** One page of the list: its rows, and "Show more workspaces" when it is the last page read and the API holds more. */
function ListPage({ read, last, onMore }: { read: UrRead<AdminWorkspaceRolesPage>; last: boolean; onMore: (token: string) => void }) {
  const s = read.state
  const data = dataOf(s)
  return (
    <>
      {s.status === 'loading' && <p className="ur-hint">Reading the group workspaces…</p>}
      {s.status === 'error' && (
        <p className="ur-hint" role="alert">
          The workspaces were not read ({LIST_ROUTE}): {refusal(s.error)}{' '}
          <Button size="sm" onClick={read.reload}>
            Read again
          </Button>
        </p>
      )}
      {s.status === 'empty' && <p className="ur-hint">No group workspace is registered.</p>}
      {data !== null && data.workspaces.length > 0 && (
        <ul className="wra-list" aria-label="Group workspaces">
          {data.workspaces.map((w) => (
            <ListedRow key={w.tenant_id} entry={w} reload={read.reload} />
          ))}
        </ul>
      )}
      {last && data !== null && data.next_page_token !== null && (
        <Button size="sm" onClick={() => onMore(data.next_page_token!)}>
          Show more workspaces
        </Button>
      )}
    </>
  )
}

function Head({ id, group, registeredBy }: { id: string; group: string; registeredBy: string }) {
  return (
    <div className="wra-head">
      <b>{id}</b>
      {group !== '' && group !== id && <span className="ur-hint"> · {group}</span>}
      {registeredBy === 'workspace_job' && <span className="ur-hint"> · made by the workspace job</span>}
    </div>
  )
}

/** A listed workspace: its roster from the page, or why the page did not carry it and a way to read it alone. */
function ListedRow({ entry, reload }: { entry: AdminWorkspaceRoles; reload: () => void }) {
  const [alone, setAlone] = useState(false)
  if (entry.roster === null && alone) return <WorkspaceRow id={entry.tenant_id} group={entry.group} registeredBy={entry.registered_by} />
  return (
    <li className="wra-ws" data-tenant={entry.tenant_id}>
      <Head id={entry.tenant_id} group={entry.group} registeredBy={entry.registered_by} />
      {entry.roster !== null ? (
        <RosterActions id={entry.tenant_id} data={entry.roster} reload={reload} />
      ) : (
        <p className="ur-hint" role="alert">
          Not listed: {entry.unavailable?.message || 'the API served no roles for it'}
          {entry.unavailable?.code ? ` (${entry.unavailable.code})` : ''}{' '}
          <Button size="sm" onClick={() => setAlone(true)}>
            Read this workspace alone
          </Button>
        </p>
      )}
    </li>
  )
}

/** One workspace read by itself (`GET /v1/admin/workspaces/{id}/roles`): an entry the list could not serve. */
function WorkspaceRow({ id, group, registeredBy }: { id: string; group: string; registeredBy: string }) {
  const roster = useUrRead(() => loadAdminWorkspaceRoles(id), `workspace-roles-admin:${id}`)
  const s = roster.state
  const data = dataOf(s)
  return (
    <li className="wra-ws" data-tenant={id}>
      <Head id={id} group={group} registeredBy={registeredBy} />
      {s.status === 'loading' && <p className="ur-hint">reading…</p>}
      {s.status === 'empty' && <p className="ur-hint">The roster came back empty.</p>}
      {s.status === 'error' && (
        <p className="ur-hint" role="alert">
          Not read: {refusal(s.error)}{' '}
          <Button size="sm" onClick={roster.reload}>
            Read again
          </Button>
        </p>
      )}
      {data !== null && <RosterActions id={id} data={data} reload={roster.reload} />}
    </li>
  )
}

/** A roster and its three writes; a write that lands re-reads whatever the roster came from. */
function RosterActions({ id, data, reload }: { id: string; data: WorkspaceRoster; reload: () => void }) {
  const [said, setSaid] = useState<{ ok: boolean; text: string } | null>(null)

  /** One write: its outcome in words or the API's refusal, and a fresh read on success. */
  async function act<T>(run: () => Promise<Result<T>>, done: (answer: T) => string, notDone: string): Promise<boolean> {
    setSaid(null)
    const r = await run()
    if (r.status === 'error') {
      setSaid({ ok: false, text: `${notDone}: ${refusal(r.error)}` })
      return false
    }
    const answer = dataOf(r)
    setSaid({ ok: true, text: answer === null ? 'Saved.' : done(answer) })
    reload()
    return true
  }

  return (
    <>
      <Roster
        id={id}
        data={data}
        onName={(email) =>
          act(
            () => setAdminWorkspacePersonRole(id, email, 'admin'),
            (a: PersonRoleChange) => personSaid(id, a),
            `${email} was not made an admin`,
          )
        }
        onDefault={(role) =>
          act(
            () => setAdminWorkspaceDefaultRole(id, role),
            (a: DefaultRoleChange) => `The default in ${id} is now ${a.default_role || role}${a.previous_default ? ` (was ${a.previous_default})` : ''}.`,
            'The default was not changed',
          )
        }
        onClear={(email) =>
          act(
            () => clearAdminWorkspacePersonRole(id, email),
            (a: PersonRoleChange) => `${a.email || email} is back on the default in ${id}${a.role ? ` (${a.role})` : ''}.`,
            `${email} was not returned to the default`,
          )
        }
      />
      {said !== null && (
        <p className={said.ok ? 'ur-hint' : 'ur-hint is-bad'} role={said.ok ? 'status' : 'alert'}>
          {said.text}
        </p>
      )}
    </>
  )
}

function personSaid(id: string, a: PersonRoleChange): string {
  const joined =
    a.member === false
      ? ' They are not in its group yet: the role applies when they join.'
      : a.member === null
        ? ' The group could not be checked just now: the role applies whenever they are in it.'
        : ''
  return `${a.email} is now ${a.role} in ${id}${a.previous_role ? ` (was ${a.previous_role})` : ''}.${joined}`
}

function Roster({
  id,
  data,
  onName,
  onDefault,
  onClear,
}: {
  id: string
  data: WorkspaceRoster
  onName: (email: string) => Promise<boolean>
  onDefault: (role: string) => Promise<boolean>
  onClear: (email: string) => Promise<boolean>
}) {
  const fid = useId()
  const admins = data.people.filter((p) => p.role === 'admin')
  const n = data.people.length
  return (
    <div className="wra-roster">
      <dl className="wra-facts">
        <dt>Default</dt>
        <dd data-default={data.default_role ?? ''}>
          {data.default_role ?? <Dash why="The API named no default" />}
          {data.default_source === 'migration' && <span className="ur-hint"> · as before roles, until an admin sets it</span>}
        </dd>
        <dt>Admins</dt>
        <dd className="wra-admins">{admins.length === 0 ? 'no admin yet' : admins.map((p) => p.email).join(', ')}</dd>
        <dt>Own roles</dt>
        <dd className="wra-count">
          {n} {n === 1 ? 'override' : 'overrides'}
        </dd>
      </dl>
      {n > 0 && (
        <ul className="wra-people" aria-label={`People with their own role in ${id}`}>
          {data.people.map((p) => (
            <Override key={p.email} p={p} onClear={onClear} />
          ))}
        </ul>
      )}
      <NameAdmin id={id} fid={fid} onName={onName} />
      <SetDefault id={id} fid={fid} current={data.default_role} onDefault={onDefault} />
    </div>
  )
}

function Override({ p, onClear }: { p: WorkspaceRoleOverride; onClear: (email: string) => Promise<boolean> }) {
  const [busy, setBusy] = useState(false)
  return (
    <li data-email={p.email}>
      <b>{p.email}</b> · {p.role}
      <span className="ur-hint">
        {' '}
        · given by {p.granted_by || 'unknown'}
        {p.granted_via === 'platform_admin' ? ' (platform admin)' : ''}
        {p.granted_at !== '' ? `, ${timeAgo(p.granted_at)}` : ''}
      </span>{' '}
      <Button
        size="sm"
        busy={busy}
        onClick={() => {
          setBusy(true)
          void onClear(p.email).finally(() => setBusy(false))
        }}
      >
        Return to default
      </Button>
    </li>
  )
}

function NameAdmin({ id, fid, onName }: { id: string; fid: string; onName: (email: string) => Promise<boolean> }) {
  const [email, setEmail] = useState('')
  const [busy, setBusy] = useState(false)
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    const v = email.trim()
    if (busy || v === '') return
    setBusy(true)
    const ok = await onName(v)
    setBusy(false)
    if (ok) setEmail('')
  }
  return (
    <form className="wra-form" autoComplete="off" onSubmit={(e) => void submit(e)}>
      <label htmlFor={`${fid}-admin`} className="vh">
        Email of the admin to name in {id}
      </label>
      <input
        id={`${fid}-admin`}
        className="ur-search"
        type="email"
        placeholder="person@example.com"
        value={email}
        maxLength={254}
        spellCheck={false}
        onChange={(e) => setEmail(e.target.value)}
      />
      <Button size="sm" type="submit" busy={busy}>
        Name an admin
      </Button>
    </form>
  )
}

function SetDefault({ id, fid, current, onDefault }: { id: string; fid: string; current: string | null; onDefault: (role: string) => Promise<boolean> }) {
  // `admin` is never a default (§4.6): the selector does not offer the one value the server answers 409.
  const [role, setRole] = useState<string>(current !== null && DEFAULT_ROLES.includes(current as never) ? current : 'member')
  const [busy, setBusy] = useState(false)
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    await onDefault(role)
    setBusy(false)
  }
  return (
    <form className="wra-form" onSubmit={(e) => void submit(e)}>
      <label htmlFor={`${fid}-default`}>Default role in {id}</label>
      <select id={`${fid}-default`} value={role} onChange={(e) => setRole(e.target.value)}>
        {DEFAULT_ROLES.map((r) => (
          <option key={r} value={r}>
            {r}
          </option>
        ))}
      </select>
      <Button size="sm" type="submit" busy={busy} disabledReason={role === current ? `The default is already ${role}` : undefined}>
        Set default
      </Button>
    </form>
  )
}

/**
 * The Manage drawer's "Roles in workspaces": every override `email` holds,
 * across the group workspaces (`GET /v1/admin/roles/person`). A platform
 * admin's stored roles are inert while they are one (§4.2.1), so the section
 * says so rather than reading as their real reach.
 */
export function PersonWorkspaceRoles({ email, platformAdmin }: { email: string; platformAdmin: boolean }) {
  const read = useUrRead(() => loadAdminPersonRoles(email), `person-roles:${email}`)
  const s = read.state
  const data = dataOf(s)
  return (
    <section className="pp-dsec" data-section="workspace-roles">
      <h3>Roles in workspaces</h3>
      {platformAdmin && (
        <p className="ur-hint">A platform admin is admin in every group workspace; a role below applies only if they stop being one.</p>
      )}
      {s.status === 'loading' && <p className="ur-hint">reading…</p>}
      {s.status === 'error' && (
        <p className="ur-hint" role="alert">
          Not read: {refusal(s.error)}{' '}
          <Button size="sm" onClick={read.reload}>
            Read again
          </Button>
        </p>
      )}
      {(s.status === 'empty' || (data !== null && data.workspaces.length === 0)) && (
        <p className="ur-hint">No role of their own in any group workspace: they hold each one&rsquo;s default.</p>
      )}
      {data !== null && data.workspaces.length > 0 && (
        <ul className="wra-person" aria-label={`Roles ${email} holds`}>
          {data.workspaces.map((w) => (
            <li key={w.tenant_id} data-tenant={w.tenant_id}>
              <b>{w.tenant_id}</b>
              {w.unavailable === true ? (
                <span className="ur-hint"> · not read: the grant could not be checked</span>
              ) : (
                <>
                  {' '}
                  · {w.role}
                  <span className="ur-hint">
                    {' '}
                    · given by {w.granted_by || 'unknown'}
                    {w.granted_via === 'platform_admin' ? ' (platform admin)' : ''}
                    {w.granted_at ? `, ${timeAgo(w.granted_at)}` : ''}
                  </span>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
