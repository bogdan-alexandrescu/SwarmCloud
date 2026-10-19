import { useId, useState, type FormEvent } from 'react'
import {
  clearAdminWorkspacePersonRole,
  loadAdminPersonRoles,
  loadAdminWorkspaceRoles,
  loadMyTenants,
  setAdminWorkspaceDefaultRole,
  setAdminWorkspacePersonRole,
  type ApiError,
  type Result,
  type TenantChoice,
} from './api'
import { Button, Card, Dash } from './components'
import { UrRefresh, useUrRead } from './RepositoriesParts'
import { DEFAULT_ROLES } from './roles'
import { timeAgo, type DefaultRoleChange, type PersonRoleChange, type WorkspaceRoleOverride, type WorkspaceRoster } from './types'

/**
 * ADMIN › PEOPLE'S WORKSPACE ROLES (lane ROLES-UI-ADMIN; docs/design/
 * workspace-roles.md §4.3, §4.8; owner answers Q2, Q4, Q5; part of #1518).
 *
 * THE CARD lists each registered group workspace with its default role, its
 * admins and how many people hold a role of their own, and offers "Name an
 * admin", "Set default" and "Return to default" per person. It is how a group
 * workspace gets its FIRST admin (Q2): the default is never `admin` (§4.6), so
 * until a platform admin names one, a group workspace has none. Every write
 * goes through the admin routes (`/v1/admin/workspaces/{id}/roles…`), which
 * act without switching workspace and are recorded `via: platform_admin`.
 *
 * WHICH WORKSPACES. There is no admin route that lists the registered group
 * workspaces, so the card lists the group entries of the platform admin's own
 * `GET /v1/tenants/mine`: their memberships, then, with the reach on
 * (`PLATFORM_ADMIN_REACH`, §4.2.2 step 5), every other registered group
 * workspace. With the reach off a group they are not in is not listed, so the
 * card also opens any workspace by its id; the admin route answers 404 for an
 * id that is not a registered group workspace, and that refusal is shown as
 * the API said it, never as an empty roster.
 *
 * NEVER A PERSONAL WORKSPACE (§4.2.1, §4.5): its only admin is its person, the
 * API reaches none of them, and an id in the `u-` namespace is not offered.
 *
 * ONLY OVERRIDES ARE LISTED: everyone else holds the default, and the
 * platform cannot enumerate a Google group (`searchTransitiveGroups` is 403
 * here, §4.3). A role given to someone not in the group is stored and applies
 * when they join (Q3); the write's answer says which.
 *
 * THE DRAWER's "Roles in workspaces" (`PersonWorkspaceRoles`) reads
 * `GET /v1/admin/roles/person?email=`: every override one person holds.
 */

const MINE_ROUTE = 'GET /v1/tenants/mine'

/** An id the card may open: a group workspace's, never one in the personal `u-` namespace. */
export function isGroupId(id: string): boolean {
  return id !== '' && !id.startsWith('u-')
}

/** The group workspaces a `/v1/tenants/mine` list names, in its order, each once. */
export function groupWorkspaces(choices: readonly TenantChoice[]): TenantChoice[] {
  const seen = new Set<string>()
  return choices.filter((c) => {
    if (c.kind === 'user' || !isGroupId(c.tenant_id) || seen.has(c.tenant_id)) return false
    seen.add(c.tenant_id)
    return true
  })
}

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
  const mine = useUrRead(loadMyTenants, 'workspace-roles-admin:mine')
  const [opened, setOpened] = useState<string[]>([])
  const listed = groupWorkspaces(dataOf(mine.state) ?? [])
  const ids = new Set(listed.map((c) => c.tenant_id))
  const extra = opened.filter((id) => !ids.has(id))
  return (
    <Card className="wra-card" title="Workspace roles" action={<UrRefresh reads={[mine]} />}>
      <p className="ur-hint">
        Each group workspace&rsquo;s default role, its admins and the people with a role of their own. A group workspace has no admin until
        one is named here; a personal workspace&rsquo;s only admin is its person.
      </p>
      {mine.state.status === 'loading' && <p className="ur-hint">Reading the workspaces you reach…</p>}
      {mine.state.status === 'error' && (
        <p className="ur-hint" role="alert">
          The workspaces were not read ({MINE_ROUTE}): {refusal(mine.state.error)}{' '}
          <Button size="sm" onClick={mine.reload}>
            Read again
          </Button>
        </p>
      )}
      {mine.state.status === 'empty' && extra.length === 0 && <p className="ur-hint">You reach no group workspace; open one by its id below.</p>}
      {listed.length + extra.length > 0 && (
        <ul className="wra-list" aria-label="Group workspaces">
          {listed.map((c) => (
            <WorkspaceRow key={c.tenant_id} id={c.tenant_id} group={c.display_name} reach={c.via === 'platform_admin'} />
          ))}
          {extra.map((id) => (
            <WorkspaceRow key={id} id={id} group={null} reach={false} />
          ))}
        </ul>
      )}
      <OpenById onOpen={(id) => setOpened((was) => (was.includes(id) ? was : [...was, id]))} />
    </Card>
  )
}

function OpenById({ onOpen }: { onOpen: (id: string) => void }) {
  const id = useId()
  const [value, setValue] = useState('')
  const [said, setSaid] = useState<string | null>(null)
  const open = (e: FormEvent) => {
    e.preventDefault()
    const v = value.trim()
    if (v === '') return
    if (!isGroupId(v)) {
      setSaid('A personal workspace has only its person as its admin; open a group workspace.')
      return
    }
    setSaid(null)
    setValue('')
    onOpen(v)
  }
  return (
    <form className="wra-open" autoComplete="off" onSubmit={open}>
      <label htmlFor={`${id}-ws`}>Workspace id</label>
      <input id={`${id}-ws`} className="ur-search" value={value} maxLength={128} spellCheck={false} onChange={(e) => setValue(e.target.value)} />
      <Button size="sm" type="submit">
        Open
      </Button>
      {said !== null && (
        <span className="ur-hint" role="status">
          {said}
        </span>
      )}
    </form>
  )
}

function WorkspaceRow({ id, group, reach }: { id: string; group: string | null; reach: boolean }) {
  const roster = useUrRead(() => loadAdminWorkspaceRoles(id), `workspace-roles-admin:${id}`)
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
    roster.reload()
    return true
  }

  const s = roster.state
  const data = dataOf(s)
  return (
    <li className="wra-ws" data-tenant={id}>
      <div className="wra-head">
        <b>{id}</b>
        {group !== null && group !== id && <span className="ur-hint"> · {group}</span>}
        {reach && <span className="ur-hint"> · you reach it as a platform admin</span>}
      </div>
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
      {data !== null && (
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
      )}
      {said !== null && (
        <p className={said.ok ? 'ur-hint' : 'ur-hint is-bad'} role={said.ok ? 'status' : 'alert'}>
          {said.text}
        </p>
      )}
    </li>
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
