import { Fragment, useEffect, useId, useState, type FormEvent } from 'react'
import {
  clearWorkspacePersonRole, loadAccessMembers, loadMe, loadWorkspaceRoles, setWorkspaceDefaultRole, setWorkspacePersonRole,
  type ApiError, type Result,
} from './api'
import { Banner, Button, Card, Dash, EmptyState, Tabs, ToneMark, type TabDef } from './components'
import { UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import { DEFAULT_ROLES, ROLES, sourceWords } from './roles'
import { FailedPanel, PageHead } from './Shell'
import { timeAgo, type AccessMember, type WorkspaceRoleEvent, type WorkspaceRoleOverride, type WorkspaceRoles } from './types'
import './styles/keys.css'
import './styles/onboarding.css'

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
 * "Where" list is unchanged (§4.8). (Since CONFIG-NAV it is Config › People
 * and roles, and since GA-MOVE-VIEWS it also holds Members' GitHub, below.)
 */

const ROUTE = 'GET /v1/workspace/roles'

/**
 * MEMBERS' GITHUB, THE PAGE'S SECOND TAB (GA-MOVE-VIEWS; part of #2157, BGD
 * 2026-10-24: "Members to Config › People and roles"). It was GitHub access'
 * Members view: every member's GitHub connection state, the owners they
 * enabled and how many repositories they chose, moved whole. Its address is
 * `?view=members`, which `/config/github?view=members` and `/access?view=members`
 * land on (paths.ts `MOVED_VIEWS`).
 *
 * DRAWN FOR WHOEVER THE API SERVES IT TO. `GET /v1/access/members` answers
 * `auth.is_admin` -- a platform admin -- and refuses everyone else, a
 * workspace admin included, so the tab is offered on `principal.is_admin`
 * from the frame's `GET /v1/tenants/me` and the read is made only when the
 * tab is open. Someone else who follows an old link still sees the admin
 * gate there, as GitHub access drew it.
 */
type PeopleView = 'roles' | 'members'

const PEOPLE_TABS: readonly TabDef[] = [
  { key: 'roles', label: 'Roles' },
  { key: 'members', label: "Members' GitHub" },
]

function peopleViewFromUrl(): PeopleView {
  if (typeof window === 'undefined') return 'roles'
  return new URLSearchParams(window.location.search).get('view') === 'members' ? 'members' : 'roles'
}

/** The tab into the bar in place, as GitHub access writes its own: Roles is the bare path. */
function writePeopleView(v: PeopleView): void {
  const url = new URL(window.location.href)
  if (v === 'members') url.searchParams.set('view', v)
  else url.searchParams.delete('view')
  window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`)
}

/** Whether the session is a platform admin; null until the frame's read answers. Unknown is not an admin. */
function usePlatformAdmin(): boolean | null {
  const [admin, setAdmin] = useState<boolean | null>(null)
  useEffect(() => {
    let live = true
    void loadMe({ frame: true }).then(
      (r) => {
        if (live) setAdmin((r.status === 'ok' || r.status === 'stale') && r.data.principal?.is_admin === true)
      },
      () => {
        if (live) setAdmin(false)
      },
    )
    return () => {
      live = false
    }
  }, [])
  return admin
}

export function WorkspacePeopleScreen() {
  const [view, setViewState] = useState<PeopleView>(peopleViewFromUrl)
  const admin = usePlatformAdmin()
  function setView(v: PeopleView) {
    setViewState(v)
    writePeopleView(v)
  }
  return (
    <div className="pk-page wp-page">
      <PageHead title="People and roles" />
      {/* `cfg-parts` (config.css): the Config pages' tab strip, as GitHub access draws it. */}
      {(admin === true || view === 'members') && (
        <Tabs tabs={PEOPLE_TABS} current={view} label="People and roles" className="cfg-parts" onSelect={(k) => setView(k === 'members' ? 'members' : 'roles')} />
      )}
      {view === 'members' ? <Members /> : <RolesCard />}
    </div>
  )
}

function RolesCard() {
  const roles = useUrRead(loadWorkspaceRoles, 'workspace-roles')
  return (
    <Card title="Roles in this workspace" action={<UrRefresh reads={[roles]} />}>
      <UrRegion state={roles.state} route={ROUTE} what="This workspace's roles" plural onRetry={roles.reload}>
        {(d) => <Roles data={d} onChange={roles.reload} />}
      </UrRegion>
    </Card>
  )
}

function MemberRow({ m }: { m: AccessMember }) {
  const c = m.connection
  const writes = m.grants.filter((g) => g.mode === 'write').length
  const state = c === null ? 'not started' : (c.state ?? 'unknown')
  return (
    <li className="ac-member" data-member={m.user}>
      <div className="ac-row-name">
        <b>{m.user}</b>
        <small className="ur-mu">{c?.forge_login != null ? `@${c.forge_login}` : 'no GitHub account'}</small>
      </div>
      <span className="ac-cell">
        <span className="ac-cell-l">Connection</span>
        <ToneMark tone={state === 'active' ? 'ok' : c === null ? 'unknown' : 'bad'}>{c?.failure ?? state}</ToneMark>
      </span>
      <span className="ac-cell">
        <span className="ac-cell-l">Owners</span>
        {m.orgs.length === 0 ? <Dash why="No owner enabled" /> : m.orgs.map((o) => o.owner).join(', ')}
      </span>
      <span className="ac-cell">
        <span className="ac-cell-l">Repositories</span>
        {m.grants.length} · {writes} write
      </span>
    </li>
  )
}

/**
 * A 403 on the members read is the admin gate, not a failure (visual QA V047).
 * The API refuses a non-admin with its generic `forbidden` code ("the
 * tenant's members view is for admins"), which the client classifies as an
 * unrecognised refusal and the region drew red, with a Try again that can
 * never succeed. On this one admin-only route a refusal means exactly that,
 * so it is drawn as the admin gate. A tenant or domain refusal keeps its own.
 */
function membersGated(state: Result<unknown>): ApiError | null {
  if (state.status !== 'error') return null
  const e = state.error
  if (e.kind === 'admin_required') return e
  if (e.kind === 'forbidden' && e.httpStatus === 403) return { ...e, kind: 'admin_required' }
  return null
}

function Members() {
  const members = useUrRead(loadAccessMembers, 'access-members')
  const gate = membersGated(members.state)
  return (
    <Card className="ac-members" title="Members' GitHub connections" action={gate === null ? <UrRefresh reads={[members]} /> : undefined}>
      {gate !== null ? (
        <FailedPanel error={gate} onRetry={members.reload} />
      ) : (
        <UrRegion state={members.state} route="GET /v1/access/members" what="The tenant's members" onRetry={members.reload} plural lines={4}>
          {(d) =>
            d.members.length === 0 ? (
              <EmptyState kind="empty" heading="No member has started">
                Nobody in tenant {d.tenant_id} has connected GitHub or chosen a repository yet.
              </EmptyState>
            ) : (
              <>
                <p className="pk-lead">
                  Every member of {d.tenant_id}: whether they connected GitHub, the owners they enabled and how many
                  repositories they chose.
                </p>
                {/* The rows' column names (visual QA V230); a phone's records carry their own (`.ac-cell-l`). */}
                <div className="ac-members-h" aria-hidden="true">
                  <span>Member</span>
                  <span>Connection</span>
                  <span>Owners</span>
                  <span>Repositories</span>
                </div>
                <ul className="ac-grid" aria-label={`Members of ${d.tenant_id}`}>
                  {d.members.map((m) => (
                    <MemberRow key={m.user} m={m} />
                  ))}
                </ul>
              </>
            )
          }
        </UrRegion>
      )}
      <p className="ur-hint">
        Each member connects as themselves. An admin sees states and names, never a value, and cannot reconnect for
        someone else.
      </p>
    </Card>
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
      {/* Profile's label-value grid (keys.css `.pf-facts`): the browser's
          definition list drew 16px text indented 40px (rendered review,
          GA-MOVE-VIEWS). Its rules read `dt` and `dd` as direct children. */}
      <dl className="pf-facts">
        {summaries.map((r) => (
          <Fragment key={r.name}>
            <dt>{r.name}</dt>
            <dd>{r.summary}</dd>
          </Fragment>
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
    // The Secrets and MCP servers table (keys.css `.mcp-servers`): left-aligned,
    // padded rows at 1440 where the browser centred them, and a card per
    // person on a phone, each value under its `data-label`.
    <table role="table" className="mcp-servers" aria-label="People with their own role">
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
            <th scope="row" data-label="Person">{p.email}</th>
            <td data-label="Role">{p.role}</td>
            <td data-label="Given by">{p.granted_by}{p.granted_via === 'platform_admin' ? ' (platform admin)' : ''}</td>
            <td data-label="When" title={p.granted_at}>{p.granted_at === '' ? '' : timeAgo(p.granted_at)}</td>
            <td data-label="Reason">{p.reason}</td>
            {canManage && (
              <td data-label="Actions">
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
