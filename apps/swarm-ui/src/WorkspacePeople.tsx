import { useId, useState, type FormEvent } from 'react'
import {
  clearWorkspacePersonRole, loadWorkspaceRoles, setWorkspaceDefaultRole, setWorkspacePersonRole,
  type ApiError, type Result,
} from './api'
import { Banner, Button, Card } from './components'
import { UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import { DEFAULT_ROLES, ROLES, sourceWords } from './roles'
import { PageHead } from './Shell'
import { timeAgo, type WorkspaceRoleEvent, type WorkspaceRoleOverride, type WorkspaceRoles } from './types'
import './styles/keys.css'

/**
 * SETTINGS › PEOPLE AND ROLES, A WORKSPACE'S OWN ROLES PAGE (lane
 * ROLES-UI-PEOPLE; docs/design/workspace-roles.md §4.3, §4.8; part of #1518).
 *
 * WHO SEES WHAT. Every member of the selected workspace, of any role, reads
 * the default, the overrides, what each role permits and the last role and
 * platform-admin events (owner answer Q7). Only its `admin` -- the API's
 * `can_manage`: a workspace admin, or a platform admin, who is admin in every
 * group workspace (Q5) -- is offered the three controls: set a person's role,
 * return a person to the default, set the default. The API is the gate; the
 * page only stops offering what would be refused.
 *
 * THE DEFAULT IS `viewer` OR `member`, NEVER `admin` (§4.6): an admin is named
 * person by person, so the selector does not offer the one value the server
 * answers 409.
 *
 * ONLY OVERRIDES ARE LISTED. Everyone else holds the default, and the
 * platform cannot list a group's members (`searchTransitiveGroups` is 403
 * here; §4.3), so the page says "everyone else" rather than drawing a roster
 * it cannot have.
 *
 * A PERSONAL WORKSPACE has only its person, who is its fixed admin (§4.5):
 * the page says so and offers nothing, because every write there is 409.
 *
 * Membership is Google's (Q3): a role given to someone not in the group is
 * stored and applies when they join, and the write's answer says which.
 *
 * Not a SECTIONS tab: a Settings route beside Profile, so the issue forms'
 * "Where" list is unchanged (§4.8).
 */

const ROUTE = 'GET /v1/workspace/roles'

export function WorkspacePeopleScreen() {
  const roles = useUrRead(loadWorkspaceRoles, 'workspace-roles')
  return (
    <div className="pk-page wp-page">
      <PageHead title="People and roles" help="workspace-roles">
        <UrRefresh reads={[roles]} />
      </PageHead>
      <Card title="Roles in this workspace">
        <UrRegion state={roles.state} route={ROUTE} what="This workspace's roles" plural onRetry={roles.reload}>
          {(d) => <Roles data={d} onChange={roles.reload} />}
        </UrRegion>
      </Card>
    </div>
  )
}

function Roles({ data, onChange }: { data: WorkspaceRoles; onChange: () => void }) {
  const [done, setDone] = useState<string | null>(null)
  const [failed, setFailed] = useState<string | null>(null)
  const name = data.tenant_id || 'this workspace'

  if (data.kind === 'user') {
    return <p className="pk-lead">You are this workspace&rsquo;s admin; a personal workspace has only you.</p>
  }

  /** One write: its outcome in words, or the API's refusal, and a fresh read on success. */
  async function act<T>(run: () => Promise<Result<T>>, said: (answer: T) => string, notDone: string): Promise<boolean> {
    setDone(null)
    setFailed(null)
    const r = await run()
    if (r.status === 'error') {
      setFailed(`${notDone}: ${refusal(r.error)}`)
      return false
    }
    if (r.status === 'ok' || r.status === 'stale') setDone(said(r.data))
    onChange()
    return true
  }

  return (
    <>
      <p className="pk-lead" aria-label="Your role">
        You hold <b>{data.me.role || 'no readable role'}</b> in {name}
        {data.me.source !== '' && <>, {sourceWords(data.me.source)}</>}
        {data.me.granted_by !== undefined && <> by {data.me.granted_by}</>}
        {data.me.granted_at !== undefined && <span title={data.me.granted_at}> {timeAgo(data.me.granted_at)}</span>}.
      </p>
      {data.me.via === 'platform_admin' && (
        <p className="ur-small">
          You are not in {name}&rsquo;s group: you reach it as a platform admin, and every action you take here is
          recorded in its audit log.
        </p>
      )}
      <p className="ur-small" aria-label="Default role">
        Everyone else in {name} holds <b>{data.default_role ?? 'no readable default'}</b>, the workspace&rsquo;s
        default{data.default_source === 'migration' ? ': nobody has set it yet, so it is what every member could do before roles' : ''}.
      </p>

      <RoleTable summaries={data.roles} />

      {data.people.length === 0 ? (
        <p className="ur-small">Nobody in {name} holds a role of their own: everyone holds the default.</p>
      ) : (
        <Overrides people={data.people} canManage={data.can_manage}
          onClear={(email) => act(() => clearWorkspacePersonRole(email),
            (a) => `${a.email} holds the default again (${a.role}).`, `${email} was not returned to the default`)} />
      )}

      {done !== null && <p className="pk-saved" role="status">{done}</p>}
      {failed !== null && <Banner tone="bad" title={failed} role="alert" />}

      {data.can_manage ? (
        <>
          <PersonForm onSet={(email, role, reason) => act(() => setWorkspacePersonRole(email, role, reason), personSaid, `${email.trim()}'s role was not changed`)} />
          <DefaultForm current={data.default_role} onSet={(role) => act(() => setWorkspaceDefaultRole(role),
            (a) => `The default is now ${a.default_role} (was ${a.previous_default}).`, 'The default was not changed')} />
        </>
      ) : (
        <p className="ur-small">Only this workspace&rsquo;s admins change roles. You can read them here.</p>
      )}

      <Events events={data.audit} />
    </>
  )
}

/** A refusal in the API's words: its message, and its code when it has one. */
function refusal(e: ApiError): string {
  return e.code === null ? e.message : `${e.message} (${e.code})`
}

function personSaid(a: { email: string; role: string; previous_role: string; member?: boolean | null }): string {
  const joined = a.member === false
    ? ' They are not in the workspace’s group yet: the role applies when they join.'
    : a.member === null
      ? ' The group could not be checked just now: the role applies whenever they are in it.'
      : ''
  return `${a.email} is now ${a.role} (was ${a.previous_role}).${joined}`
}

function RoleTable({ summaries }: { summaries: WorkspaceRoles['roles'] }) {
  const id = useId()
  if (summaries.length === 0) return null
  return (
    <section className="pk-row" aria-labelledby={`${id}-h`}>
      <div className="pk-row-head">
        <h3 id={`${id}-h`}>What each role permits</h3>
      </div>
      <dl>
        {summaries.map((r) => (
          <div key={r.name}>
            <dt><b>{r.name}</b></dt>
            <dd>{r.summary}</dd>
          </div>
        ))}
      </dl>
    </section>
  )
}

function Overrides({ people, canManage, onClear }: {
  people: WorkspaceRoleOverride[]
  canManage: boolean
  onClear: (email: string) => Promise<boolean>
}) {
  const [busy, setBusy] = useState<string | null>(null)
  return (
    <table role="table" aria-label="People with their own role">
      <thead>
        <tr>
          <th scope="col">Person</th>
          <th scope="col">Role</th>
          <th scope="col">Given by</th>
          <th scope="col">When</th>
          <th scope="col">Reason</th>
          {canManage && <th scope="col" aria-label="Actions" />}
        </tr>
      </thead>
      <tbody>
        {people.map((p) => (
          <tr key={p.email}>
            <th scope="row">{p.email}</th>
            <td>{p.role}</td>
            <td>{p.granted_by}{p.granted_via === 'platform_admin' ? ' (platform admin)' : ''}</td>
            <td title={p.granted_at}>{p.granted_at === '' ? '' : timeAgo(p.granted_at)}</td>
            <td>{p.reason}</td>
            {canManage && (
              <td>
                <Button size="sm" aria-label={`Return ${p.email} to the default`} busy={busy === p.email}
                  disabled={busy !== null && busy !== p.email}
                  onClick={() => {
                    setBusy(p.email)
                    void onClear(p.email).finally(() => setBusy(null))
                  }}>
                  Return to default
                </Button>
              </td>
            )}
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function PersonForm({ onSet }: { onSet: (email: string, role: string, reason: string) => Promise<boolean> }) {
  const id = useId()
  const [email, setEmail] = useState('')
  const [role, setRole] = useState<string>('member')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)

  async function save(e: FormEvent) {
    e.preventDefault()
    if (busy || email.trim() === '') return
    setBusy(true)
    const ok = await onSet(email, role, reason)
    setBusy(false)
    if (ok) {
      setEmail('')
      setReason('')
    }
  }

  return (
    <section className="pk-row" aria-labelledby={`${id}-h`}>
      <div className="pk-row-head">
        <h3 id={`${id}-h`}>Give a person their own role</h3>
      </div>
      <form className="pk-form" autoComplete="off" aria-labelledby={`${id}-h`} onSubmit={(e) => void save(e)}>
        <p className="ur-small">
          Google owns who is in the workspace: a role given to someone not in its group yet applies when they join.
        </p>
        <div className="pk-form-row">
          <label htmlFor={`${id}-email`}>Email</label>
          <input id={`${id}-email`} className="ur-search pk-input" type="email" value={email} required maxLength={254}
            spellCheck={false} onChange={(e) => setEmail(e.target.value)} />
          <label htmlFor={`${id}-role`}>Role</label>
          <select id={`${id}-role`} value={role} onChange={(e) => setRole(e.target.value)}>
            {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
        </div>
        <label htmlFor={`${id}-reason`}>Reason (optional, shown in the events)</label>
        <input id={`${id}-reason`} className="ur-search pk-input" value={reason} maxLength={280}
          onChange={(e) => setReason(e.target.value)} />
        <div className="pk-form-row">
          <Button kind="primary" type="submit" busy={busy}>Set role</Button>
        </div>
      </form>
    </section>
  )
}

function DefaultForm({ current, onSet }: { current: string | null; onSet: (role: string) => Promise<boolean> }) {
  const id = useId()
  const [role, setRole] = useState<string>(current !== null && (DEFAULT_ROLES as readonly string[]).includes(current) ? current : 'member')
  const [busy, setBusy] = useState(false)

  async function save(e: FormEvent) {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    await onSet(role)
    setBusy(false)
  }

  return (
    <section className="pk-row" aria-labelledby={`${id}-h`}>
      <div className="pk-row-head">
        <h3 id={`${id}-h`}>Set the default</h3>
      </div>
      <form className="pk-form" autoComplete="off" aria-labelledby={`${id}-h`} onSubmit={(e) => void save(e)}>
        <p className="ur-small">
          The role everyone without their own holds. Viewer or member: an admin is named person by person.
        </p>
        <div className="pk-form-row">
          <label htmlFor={`${id}-role`}>New default role</label>
          <select id={`${id}-role`} value={role} onChange={(e) => setRole(e.target.value)}>
            {DEFAULT_ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
          <Button kind="secondary" type="submit" busy={busy}>Set default</Button>
        </div>
      </form>
    </section>
  )
}

/** One role or platform-admin event, in words. The route of a platform admin's action, never its body. */
function eventWords(e: WorkspaceRoleEvent): string {
  const d = e.detail
  const word = (k: string) => (typeof d[k] === 'string' ? (d[k] as string) : '?')
  const asAdmin = d.via === 'platform_admin' ? ', as a platform admin' : ''
  const who = e.target_email ?? 'someone'
  switch (e.action) {
    case 'workspace_role_granted':
      return `${e.by} gave ${who} the role ${word('to')} (was ${word('from')})${asAdmin}`
    case 'workspace_role_cleared':
      return `${e.by} returned ${who} to the default, ${word('to')} (was ${word('from')})${asAdmin}`
    case 'workspace_default_role_set':
      return `${e.by} set the default to ${word('to')} (was ${word('from')})${asAdmin}`
    case 'workspace_role_grant_invalid':
      return `A stored role for ${who} failed its integrity check and is ignored`
    case 'platform_admin_acted':
      return `${e.by} acted here as a platform admin: ${word('method')} ${word('route')}`
    case 'platform_admin_viewed':
      return `${e.by} read this workspace as a platform admin`
    default:
      return `${e.by}: ${e.action}`
  }
}

function Events({ events }: { events: WorkspaceRoleEvent[] }) {
  const id = useId()
  return (
    <section className="pk-row" aria-labelledby={`${id}-h`}>
      <div className="pk-row-head">
        <h3 id={`${id}-h`}>Role events</h3>
      </div>
      {events.length === 0 ? (
        <p className="ur-small">No role has been given or changed here, and no platform admin has acted here.</p>
      ) : (
        <ul className="pk-uses" aria-label="Role events">
          {events.map((e, i) => (
            <li key={`${e.at}-${i}`}>
              {eventWords(e)}
              {e.at !== '' && <span className="ur-small" title={e.at}> · {timeAgo(e.at)}</span>}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
