import { useContext, useId, useState, type FormEvent, type ReactNode } from 'react'
import {
  clearWorkspacePersonRole, loadJoinedMembers, loadJoinLink, loadWorkspaceRoles, removeJoinedMember, setJoinLink,
  setWorkspaceDefaultRole, setWorkspacePersonRole,
  type ApiError, type Result,
} from './api'
import { Banner, Button, Card } from './components'
import { SetupContext } from './ConfigSetup'
import { UrCopy, UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import { DEFAULT_ROLES, ROLES, sourceWords } from './roles'
import { PageHead } from './Shell'
import {
  timeAgo, type JoinedMember, type JoinedMembers, type JoinLinkState, type WorkspaceRoleEvent, type WorkspaceRoleOverride,
  type WorkspaceRoles,
} from './types'
import './styles/keys.css'
import './styles/workspace-settings.css'

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
 * THE JOIN LINK AND JOINED BY REQUEST (lane JOIN-UI-PERSON;
 * docs/design/join-requests.md §4.2, §4.6; part of #2325), for its admins
 * only (`JoinCards` below): the link a person asks to join with, and the
 * people an approved request let in.
 *
 * Not a SECTIONS tab: a Settings route beside Profile, so the issue forms'
 * "Where" list is unchanged (§4.8).
 *
 * THE LEAD (CONFIG-S5-LEADS; part of #2157, proposal E S5b, S5e, S5g, S5h)
 * is one sentence over the body as built. An admin's is the setup strip's
 * step 4, People invited, as the setup read serves it
 * (`config_setup.people`, CONFIG-SETUP-READ): nobody else holds a role, so
 * one button to the role form below; people waiting for an admin, counted;
 * otherwise green. A member's or viewer's names who administers the
 * workspace, from this page's own roster, and offers nothing. A source not
 * read, or not served, is "—" and why: never green.
 */

const ROUTE = 'GET /v1/workspace/roles'

/** Step 4 as the setup read serves it (`swarm_api.config_setup.people_step`), or why it cannot be said. */
export type PeopleStep =
  | { read: false; reason: string }
  | { read: true; holders: number; pending: number | null }

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

/** `config_setup.people` off the setup read; null while it has not landed. */
export function peopleStepOf(doc: Result<unknown>): PeopleStep | null {
  if (doc.status === 'loading') return null
  if (doc.status === 'error' || doc.status === 'empty') return { read: false, reason: 'the setup read could not be read' }
  const setup = isRecord(doc.data) ? doc.data.config_setup : undefined
  const step = isRecord(setup) ? setup.people : undefined
  if (!isRecord(step)) return { read: false, reason: 'this API does not serve who else holds a role here' }
  if (typeof step.holders !== 'number') {
    return { read: false, reason: typeof step.reason === 'string' && step.reason !== '' ? step.reason : 'who else holds a role here was not measured' }
  }
  return { read: true, holders: step.holders, pending: typeof step.pending === 'number' ? step.pending : null }
}

function joined(names: readonly string[]): string {
  return names.length < 2 ? names.join('') : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
}

function Lead({ kicker, title, tone, action }: { kicker: string; title: string; tone: 'next' | 'ok'; action?: ReactNode }) {
  return (
    <section className={`ws-lead is-${tone}`} aria-label={kicker}>
      <div className="ws-lead-t">
        <span className="ws-lead-k">{kicker}</span>
        <b>{title}</b>
      </div>
      {action !== undefined && <div className="ws-lead-a">{action}</div>}
    </section>
  )
}

/** The page's lead: one sentence, at most one button (proposal E). */
function PeopleLead({ state, inviteId }: { state: Result<WorkspaceRoles>; inviteId: string }) {
  const { doc } = useContext(SetupContext)
  if (state.status === 'loading') return null
  if (state.status === 'error' || state.status === 'empty') {
    return <Lead tone="next" kicker="People and roles" title="— this workspace’s roles could not be read, so who holds one is not known." />
  }
  const data = state.data
  // A personal workspace has nobody to invite: the strip draws no step 4 there (S1e).
  if (data.kind === 'user') return null
  const name = data.tenant_id || 'this workspace'
  if (!data.can_manage) {
    const admins = data.people.filter((p) => p.role === 'admin').map((p) => p.email)
    const title = admins.length === 0
      ? `Nobody in ${name} holds admin yet: a platform admin names a group workspace’s first admin.`
      : `${joined(admins)} ${admins.length === 1 ? 'administers' : 'administer'} ${name}, and only an admin changes roles here.`
    return <Lead tone="ok" kicker="Read only for you" title={title} />
  }
  const step = peopleStepOf(doc)
  if (step === null) return null
  if (!step.read) return <Lead tone="next" kicker="Step 4 · People invited" title={`— ${step.reason}.`} />
  if (step.pending !== null && step.pending > 0) {
    return (
      <Lead tone="next" kicker="Needs an admin"
        title={`${step.pending} ${step.pending === 1 ? 'person is' : 'people are'} waiting for an admin to answer their request.`} />
    )
  }
  if (step.holders === 0) {
    return (
      <Lead tone="next" kicker="Step 4 · this page" title={`Only you hold a role in ${name} so far: give each person who will run agents their own.`}
        action={
          <Button kind="primary" size="sm" onClick={() => {
            const field = document.getElementById(inviteId)
            field?.scrollIntoView?.({ block: 'center' })
            field?.focus({ preventScroll: true })
          }}>
            Give a person a role
          </Button>
        } />
    )
  }
  return (
    <Lead tone="ok" kicker="Step 4 · done"
      title={`${step.holders} other ${step.holders === 1 ? 'person holds' : 'people hold'} a role in ${name}; nothing here is waiting for you.`} />
  )
}

export function WorkspacePeopleScreen() {
  const roles = useUrRead(loadWorkspaceRoles, 'workspace-roles')
  const inviteId = useId()
  return (
    <div className="pk-page wp-page">
      <PageHead title="People and roles" />
      <PeopleLead state={roles.state} inviteId={inviteId} />
      <Card title="Roles in this workspace" action={<UrRefresh reads={[roles]} />}>
        <UrRegion state={roles.state} route={ROUTE} what="This workspace's roles" plural onRetry={roles.reload}>
          {(d) => <Roles data={d} onChange={roles.reload} inviteId={inviteId} />}
        </UrRegion>
      </Card>
      {(roles.state.status === 'ok' || roles.state.status === 'stale') && roles.state.data.can_manage && roles.state.data.kind === 'group' && (
        <JoinCards name={roles.state.data.tenant_id || 'this workspace'} />
      )}
    </div>
  )
}

function Roles({ data, onChange, inviteId }: { data: WorkspaceRoles; onChange: () => void; inviteId: string }) {
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
          <PersonForm emailId={inviteId} onSet={(email, role, reason) => act(() => setWorkspacePersonRole(email, role, reason), personSaid, `${email.trim()}'s role was not changed`)} />
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
    // keys.css's stacking table (#1680): at 390 px this roster was 589 px wide
    // in the content scroller (CONFIG-S5-LEADS review), so below 720 px each
    // person is a card, each value under its column's `data-label`.
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

function PersonForm({ emailId, onSet }: { emailId: string; onSet: (email: string, role: string, reason: string) => Promise<boolean> }) {
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
          <label htmlFor={emailId}>Email</label>
          <input id={emailId} className="ur-search pk-input" type="email" value={email} required maxLength={254}
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

/*
 * THE JOIN LINK AND JOINED BY REQUEST (lane JOIN-UI-PERSON;
 * docs/design/join-requests.md §4.2, §4.4, §4.6, §4.10; part of #2325).
 *
 * Drawn for the workspace's admins only (`can_manage`, which a platform admin
 * holds in every group workspace): both routes answer anyone else 403, so the
 * page reads neither for them. A personal workspace takes no join requests.
 *
 * THE LINK IS SHOWN ONCE (§4.2). The API stores only the code's hash, and
 * answers it in exactly one place: the `PUT` that made a new code. It is held
 * in this card's state from that answer and in nothing else -- never a read,
 * a route, storage or a log -- so leaving the page, or turning the link off,
 * is the last of it. Sharing it again means making a new one, which stops
 * the old one working.
 */

/**
 * The link as a person can open it. The API builds it on `console_url`,
 * which is empty unless SWARM_CONSOLE_URL is set, and then answers a bare
 * `/join#<code>`: this page is the console, so its own origin completes it.
 */
export function shareableLink(link: string, origin: string = window.location.origin): string {
  return link.startsWith('/') ? `${origin}${link}` : link
}

function JoinCards({ name }: { name: string }) {
  return (
    <>
      <JoinLinkCard name={name} />
      <JoinedByRequestCard name={name} />
    </>
  )
}

function JoinLinkCard({ name }: { name: string }) {
  const link = useUrRead(loadJoinLink, 'workspace-join-link')
  const fieldId = useId()
  // The link from the answer that made it: the one time it exists in this client.
  const [made, setMade] = useState<string | null>(null)
  const [busy, setBusy] = useState<'on' | 'off' | 'rotate' | null>(null)
  const [done, setDone] = useState<string | null>(null)
  const [failed, setFailed] = useState<string | null>(null)

  async function change(kind: 'on' | 'off' | 'rotate') {
    setBusy(kind)
    setDone(null)
    setFailed(null)
    const r = await setJoinLink(kind !== 'off', kind === 'rotate')
    setBusy(null)
    if (r.status === 'error') {
      setFailed(`The join link was not changed: ${refusal(r.error)}`)
      return
    }
    const answer: JoinLinkState | null = r.status === 'ok' || r.status === 'stale' ? r.data : null
    setMade(answer?.link === undefined ? null : shareableLink(answer.link))
    if (kind === 'off') setDone(`The join link is off: nobody can ask to join ${name} with it.`)
    else if (answer?.link === undefined) {
      // On again with the code it had (`JoinStore.set_link` keeps it): the
      // link shared before works again, and there is nothing new to show.
      setDone('The join link is on again: the link shared before it was switched off works again. Make a new link to get one to share.')
    }
    link.reload()
  }

  return (
    <Card title="Join link" action={<UrRefresh reads={[link]} />}>
      <UrRegion state={link.state} route="GET /v1/workspace/join-link" what="The join link" onRetry={link.reload}>
        {(d) => (
          <section className="pk-row" aria-label="Join link">
            {d.on ? (
              <p className="pk-lead">
                The join link is <b>on</b>: anyone holding it can ask to join {name}
                {d.set_by !== '' && <>, turned on by {d.set_by}</>}
                {d.set_at !== '' && <span title={d.set_at}> {timeAgo(d.set_at)}</span>}.
              </p>
            ) : (
              <p className="pk-lead">
                The join link is <b>off</b>: nobody can ask to join {name}. Turn it on to share a link a person asks to
                join with.
              </p>
            )}
            <p className="ur-small">
              Nobody gets in by holding the link: each request waits in Admin &rsaquo; People and roles until one of
              {' '}{name}&rsquo;s admins approves or denies it.
            </p>
            {made !== null && (
              <div className="pk-form" aria-label="The new join link">
                <Banner tone="warn" title="Copy the link now: it is shown this once." role="status">
                  Only its fingerprint is kept, so it cannot be shown again. To share it later, make a new link, which
                  stops this one working.
                </Banner>
                <div className="pk-form-row">
                  <label htmlFor={`${fieldId}-link`}>Link to share</label>
                  <input id={`${fieldId}-link`} className="ur-search pk-input" readOnly value={made} spellCheck={false}
                    onFocus={(e) => e.currentTarget.select()} />
                  <UrCopy text={made} label="Copy the join link" />
                </div>
              </div>
            )}
            {done !== null && <p className="pk-saved" role="status">{done}</p>}
            {failed !== null && <Banner tone="bad" title={failed} role="alert" />}
            <div className="pk-form-row">
              {d.on ? (
                <>
                  <Button kind="secondary" size="sm" busy={busy === 'rotate'} disabled={busy !== null && busy !== 'rotate'}
                    onClick={() => void change('rotate')}>
                    Make a new link
                  </Button>
                  <Button kind="danger" size="sm" busy={busy === 'off'} disabled={busy !== null && busy !== 'off'}
                    onClick={() => void change('off')}>
                    Turn the link off
                  </Button>
                </>
              ) : (
                <Button kind="primary" size="sm" busy={busy === 'on'} disabled={busy !== null && busy !== 'on'}
                  onClick={() => void change('on')}>
                  Turn the link on
                </Button>
              )}
            </div>
          </section>
        )}
      </UrRegion>
    </Card>
  )
}

/** Where a joined person's access comes from, beside the platform membership. */
function sourceOf(m: JoinedMember): string {
  if (m.group_changed) return 'Joined by request, group changed: this admits nobody now'
  if (m.still_in_group === true) return 'Joined by request, and in the Google group too'
  if (m.still_in_group === false) return 'Joined by request only'
  return 'Joined by request; the Google group could not be checked'
}

function removedSaid(email: string, name: string, stillInGroup: boolean | null | undefined): string {
  if (stillInGroup === true) {
    return `${email}'s membership by request is ended, but they are still in ${name}'s Google group, which the platform does not change: remove them there too.`
  }
  if (stillInGroup === false) return `${email} no longer reaches ${name}.`
  return `${email}'s membership by request is ended; the Google group could not be checked just now.`
}

function JoinedByRequestCard({ name }: { name: string }) {
  const members = useUrRead(loadJoinedMembers, 'workspace-joined-members')
  const [busy, setBusy] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)
  const [failed, setFailed] = useState<string | null>(null)

  async function remove(email: string) {
    setBusy(email)
    setDone(null)
    setFailed(null)
    const r = await removeJoinedMember(email)
    setBusy(null)
    if (r.status === 'error') {
      setFailed(`${email} was not removed: ${refusal(r.error)}`)
      return
    }
    setDone(removedSaid(email, name, r.status === 'ok' || r.status === 'stale' ? r.data?.still_in_group : null))
    members.reload()
  }

  return (
    <Card title="Joined by request" action={<UrRefresh reads={[members]} />}>
      <UrRegion state={members.state} route="GET /v1/workspace/members" what="The people who joined by request" plural
        onRetry={members.reload}>
        {(d: JoinedMembers) => (
          <section className="pk-row" aria-label="Joined by request">
            {!d.platform_memberships && (
              <p className="ur-small">
                Approving a request does not let anyone into {name} yet: platform memberships are off, so an approved
                person is added to its Google group by hand, and the approval names the command.
              </p>
            )}
            {done !== null && <p className="pk-saved" role="status">{done}</p>}
            {failed !== null && <Banner tone="bad" title={failed} role="alert" />}
            {d.members.length === 0 ? (
              <p className="ur-small">Nobody holds a membership of {name} by request: everyone here is in its Google group.</p>
            ) : (
              <table role="table" className="mcp-servers" aria-label="People who joined by request">
                <thead>
                  <tr>
                    <th scope="col">Person</th>
                    <th scope="col">Let in by</th>
                    <th scope="col">When</th>
                    <th scope="col">Access</th>
                    <th scope="col" aria-label="Actions" />
                  </tr>
                </thead>
                <tbody>
                  {d.members.map((m) => (
                    <tr key={m.email}>
                      <th scope="row" data-label="Person">{m.email}</th>
                      <td data-label="Let in by">{m.by}{m.via === 'platform_admin' ? ' (platform admin)' : ''}</td>
                      <td data-label="When" title={m.at}>{m.at === '' ? '' : timeAgo(m.at)}</td>
                      <td data-label="Access">{sourceOf(m)}</td>
                      <td data-label="Actions">
                        <Button size="sm" kind="danger" aria-label={`Remove ${m.email}'s membership by request`}
                          busy={busy === m.email} disabled={busy !== null && busy !== m.email}
                          onClick={() => void remove(m.email)}>
                          Remove
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        )}
      </UrRegion>
    </Card>
  )
}
