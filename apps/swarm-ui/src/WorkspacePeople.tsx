import { Fragment, useContext, useEffect, useId, useState, type FormEvent, type ReactNode } from 'react'
import {
  clearWorkspacePersonRole, loadAccessMembers, loadJoinedMembers, loadJoinLink, loadMe, loadWorkspaceRoles,
  removeJoinedMember, setJoinLink, setWorkspaceDefaultRole, setWorkspacePersonRole,
  type ApiError, type Result,
} from './api'
import { Banner, Button, ButtonLink, Card, Chip, Dash, EmptyState, Segmented, Tabs, ToneMark, type TabDef } from './components'
import { SetupContext } from './ConfigSetup'
import { addressToPath } from './paths'
import { UrCopy, UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import { DEFAULT_ROLES, ROLES, sourceWords, type RoleName } from './roles'
import { FailedPanel, PageHead } from './Shell'
import {
  timeAgo, type AccessMember, type JoinedMember, type JoinedMembers, type JoinLinkState, type WorkspaceRoleEvent,
  type WorkspaceRoles,
} from './types'
import { WORKSPACE_NEW_ADDRESS, WORKSPACE_SETTINGS_ADDRESS } from './WorkspaceSettings'
import './styles/keys.css'
import './styles/onboarding.css'
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
 * ONLY OVERRIDES, AND THE CALLER, ARE LISTED. Everyone else holds the
 * default, and the platform cannot list a group's members
 * (`searchTransitiveGroups` is 403 here; §4.3), so the page says "everyone
 * else" rather than drawing a roster it cannot have. The caller's own row is
 * drawn whatever they hold (S5b's lone admin, CONFIG-PEOPLE-S5).
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
 * "Where" list is unchanged (§4.8). (Since CONFIG-NAV it is Config › People
 * and roles, and since GA-MOVE-VIEWS it also holds Members' GitHub, below.)
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

/** The lead: a kicker, one title, the mock-up's body sentence under it (S5b, S5g), and at most one action. */
function Lead({ kicker, title, body, tone, action }: { kicker: string; title: string; body?: string; tone: 'next' | 'ok'; action?: ReactNode }) {
  return (
    <section className={`ws-lead is-${tone}`} aria-label={kicker}>
      <div className="ws-lead-t">
        <span className="ws-lead-k">{kicker}</span>
        <b>{title}</b>
        {body !== undefined && body !== '' && <span className="ws-lead-s">{body}</span>}
      </div>
      {action !== undefined && <div className="ws-lead-a">{action}</div>}
    </section>
  )
}

/** What everyone without a role of their own holds, as the lead's body ends (S5b, S5g); '' when unread. */
function everyoneElse(data: WorkspaceRoles, name: string): string {
  return data.default_role === null || data.default_role === '' ? '' : `Everyone else signed in to ${name} holds ${data.default_role}, the default.`
}

/** The caller's own role in the lead's body (S5g): "You hold member, given by dana@… 6 d ago." */
function youHold(me: WorkspaceRoles['me']): string {
  if (me.role === '') return ''
  const by = me.granted_by !== undefined && me.granted_by !== '' ? `, given by ${me.granted_by}` : me.source !== '' ? `, ${sourceWords(me.source)}` : ''
  const when = me.granted_at !== undefined && me.granted_at !== '' ? ` ${timeAgo(me.granted_at)}` : ''
  return `You hold ${me.role}${by}${when}.`
}

function sentences(...parts: string[]): string {
  return parts.filter((p) => p !== '').join(' ')
}

/** The page's lead: one title, the mock-up's body sentence, at most one button (proposal E). */
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
    return <Lead tone="ok" kicker="Read only for you" title={title} body={sentences(youHold(data.me), everyoneElse(data, name))} />
  }
  const step = peopleStepOf(doc)
  if (step === null) return null
  if (!step.read) return <Lead tone="next" kicker="Step 4 · People invited" title={`— ${step.reason}.`} />
  if (step.pending !== null && step.pending > 0) {
    return (
      <Lead tone="next" kicker="Needs an admin"
        title={`${step.pending} ${step.pending === 1 ? 'person is' : 'people are'} waiting for an admin to answer their request.`}
        body={everyoneElse(data, name)} />
    )
  }
  if (step.holders === 0) {
    return (
      <Lead tone="next" kicker="Step 4 · this page" title={`Nobody else in ${name} holds a role of their own yet`}
        body={sentences('Give each person who will run agents their own role.', everyoneElse(data, name))}
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
      title={`${step.holders} other ${step.holders === 1 ? 'person holds' : 'people hold'} a role in ${name}; nothing here is waiting for you.`}
      body={everyoneElse(data, name)} />
  )
}

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

/**
 * The tab an address names. App passes the ROUTED query (`view`), because an
 * old hash bookmark (`#work/access?view=members`) is rewritten to this path
 * only after the first render, so the bar read at mount names no tab (GA-MOVE-VIEWS
 * review). Mounted without App, the page reads the bar.
 */
function peopleViewOf(query: string | null | undefined): PeopleView {
  const q = query !== undefined ? query ?? '' : typeof window === 'undefined' ? '' : window.location.search
  return new URLSearchParams(q).get('view') === 'members' ? 'members' : 'roles'
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

export function WorkspacePeopleScreen({ view: routed }: { view?: string | null } = {}) {
  const named = peopleViewOf(routed)
  const [view, setViewState] = useState<PeopleView>(named)
  // A new routed address names its tab; a tab picked here is written in place
  // and leaves the route as it was, so it is not undone.
  const [arrived, setArrived] = useState(named)
  if (arrived !== named) {
    setArrived(named)
    setViewState(named)
  }
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
      {view === 'members' ? <Members /> : <RolesPage />}
    </div>
  )
}

/** Where a write's outcome is said: beside the control that made it. */
type NoteAt = 'roster' | 'person' | 'default'
type Note = { at: NoteAt; text: string; bad: boolean }

/**
 * THE ROLES TAB AS S5b'S CARDS (CONFIG-PEOPLE-S5; part of #2391, BGD
 * 2026-10-26: apply config-e.html S5b, S5e, S5g, S5h). The lead; then
 * "Roles in this workspace", which holds the roster table (the caller's own
 * row included) and what S5b's caption calls "as built" -- what each role
 * permits, Set the default, and each row's Return to default; then, for an
 * admin, a row of two separate cards, "Give a person their own role" and
 * "Role events", which `.ws-grid` stacks into one column under 900 px (S5e).
 * A member or viewer gets S5g: "read only for members" in the Roles card's
 * head and Role events as its own card, with no row because there is no form.
 * PR 2390's join-link and Joined by request cards (`JoinCards`,
 * docs/design/join-requests.md §4.6) are an admin's cards after the row.
 *
 * Every card reads the one roles read. Each write's outcome is said in the
 * card that made it (`Note`), and every write re-reads.
 */
function RolesPage() {
  const roles = useUrRead(loadWorkspaceRoles, 'workspace-roles')
  const inviteId = useId()
  const [note, setNote] = useState<Note | null>(null)
  const state = roles.state
  const data = state.status === 'ok' || state.status === 'stale' ? state.data : null
  const group = data !== null && data.kind === 'group'
  const readOnly = group && !data.can_manage

  /** One write: its outcome in words, or the API's refusal, and a fresh read on success. */
  async function act<T>(at: NoteAt, run: () => Promise<Result<T>>, said: (answer: T) => string, notDone: string): Promise<boolean> {
    setNote(null)
    const r = await run()
    if (r.status === 'error') {
      setNote({ at, text: `${notDone}: ${refusal(r.error)}`, bad: true })
      return false
    }
    if (r.status === 'ok' || r.status === 'stale') setNote({ at, text: said(r.data), bad: false })
    roles.reload()
    return true
  }
  const said = (at: NoteAt) => (note !== null && note.at === at ? <Said note={note} /> : null)

  const events = data !== null && group ? (
    <Card title="Role events">
      <Events events={data.audit} />
    </Card>
  ) : null

  return (
    <>
      <PeopleLead state={state} inviteId={inviteId} />
      <Card title="Roles in this workspace"
        action={
          <span className="wp-card-a">
            {/* S5g: a member's or viewer's page says so in the card's head. */}
            {readOnly && <Chip>read only for members</Chip>}
            <UrRefresh reads={[roles]} />
          </span>
        }>
        <UrRegion state={state} route={ROUTE} what="This workspace's roles" plural onRetry={roles.reload}>
          {(d) => (
            <Roster data={d}
              said={said}
              onClear={(email) => act('roster', () => clearWorkspacePersonRole(email),
                (a) => `${a.email} holds the default again (${a.role}).`, `${email} was not returned to the default`)}
              onDefault={(role) => act('default', () => setWorkspaceDefaultRole(role),
                (a) => `The default is now ${a.default_role} (was ${a.previous_default}).`, 'The default was not changed')} />
          )}
        </UrRegion>
      </Card>
      {data !== null && group && data.can_manage && (
        <div className="ws-grid wp-row">
          <Card title="Give a person their own role">
            <PersonForm emailId={inviteId}
              onSet={(email, role, reason) => act('person', () => setWorkspacePersonRole(email, role, reason), personSaid, `${email.trim()}'s role was not changed`)} />
            {said('person')}
          </Card>
          {events}
        </div>
      )}
      {readOnly && events}
      {data !== null && group && data.can_manage && (
        <JoinCards name={data.tenant_id || 'this workspace'} />
      )}
    </>
  )
}

/** A write's outcome, in the card that made it. */
function Said({ note }: { note: Note }) {
  return note.bad ? <Banner tone="bad" title={note.text} role="alert" /> : <p className="pk-saved" role="status">{note.text}</p>
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
        <UrRegion state={members.state} route="GET /v1/access/members" what="The workspace's members" onRetry={members.reload} plural lines={4}>
          {(d) =>
            d.members.length === 0 ? (
              <EmptyState kind="empty" heading="No member has started">
                Nobody in workspace {d.tenant_id} has connected GitHub or chosen a repository yet.
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

/**
 * A PERSONAL WORKSPACE'S PAGE HAS A WAY ON (visual QA pass 1, config-29; part of #2596). Reached by URL, the page
 * was one sentence with no way back but the panel, which leaves People and roles out here. It now offers Workspace
 * Settings, and for a platform admin, who is the one person who may add one (Workspace Settings' `part=new`), Add a
 * new workspace: people and roles belong to a group workspace.
 */
function PersonalOnly() {
  const admin = usePlatformAdmin()
  return (
    <>
      <p className="pk-lead">You are this workspace&rsquo;s admin; a personal workspace has only you.</p>
      <p className="ur-hint">
        {/* WHO CREATES WHAT, IN PLAIN WORDS (visual QA pass 2, config2-21): "a platform admin adds one" read as
            adding a person as well as a workspace. */}
        {admin === true
          ? 'Only a group workspace has people and roles; as a platform admin, you may create one.'
          : 'Only a group workspace has people and roles; a platform admin creates group workspaces.'}
      </p>
      <div className="ws-rename-a" data-testid="wp-personal-actions">
        <ButtonLink size="sm" href={addressToPath(WORKSPACE_SETTINGS_ADDRESS)}>Open Workspace Settings</ButtonLink>
        {admin === true && (
          <ButtonLink kind="primary" size="sm" href={addressToPath(WORKSPACE_NEW_ADDRESS)}>Add a new workspace</ButtonLink>
        )}
      </div>
    </>
  )
}

/** The Roles in this workspace card's body: who you are, the default, the roster, and S5b's "as built" rest. */
function Roster({ data, said, onClear, onDefault }: {
  data: WorkspaceRoles
  said: (at: NoteAt) => ReactNode
  onClear: (email: string) => Promise<boolean>
  onDefault: (role: string) => Promise<boolean>
}) {
  const name = data.tenant_id || 'this workspace'
  // People other than the caller who hold a role of their own: what the lead's step 4 counts as holders.
  const others = data.people.filter((p) => p.email.toLowerCase() !== (data.me.email || '').toLowerCase()).length

  if (data.kind === 'user') return <PersonalOnly />

  return (
    <>
      <p className="pk-lead" aria-label="Your role">
        You hold <b>{data.me.role || 'no readable role'}</b> in {name}
        {data.me.source !== '' && <>, {sourceWords(data.me.source)}</>}
        {data.me.granted_by !== undefined && <> by {data.me.granted_by}</>}
        {data.me.granted_at !== undefined && <span title={data.me.granted_at}> {timeAgo(data.me.granted_at)}</span>}.
      </p>
      {/* ONE STATEMENT OF WHO HOLDS WHAT, IN ONE TYPE SIZE (visual QA VQ-047): the lead said "Only you hold
          a role", a second line said "Nobody in eng holds a role of their own", and the caller's role and the
          default were set in two sizes. The default's sentence now says whether anyone else holds their own,
          in the lead's words, and every line of it is `pk-lead`. */}
      {data.me.via === 'platform_admin' && (
        <p className="pk-lead">
          You are not in {name}&rsquo;s group: you reach it as a platform admin, and every action you take here is
          recorded in its audit log.
        </p>
      )}
      <p className="pk-lead" aria-label="Default role">
        Everyone else in {name} holds <b>{data.default_role ?? 'no readable default'}</b>, the workspace&rsquo;s
        default{others === 0 ? ': nobody else holds a role of their own yet' : ', unless they are listed below with a role of their own'}.
        {data.default_source === 'migration' && ' Nobody has set the default yet, so it is what every member could do before roles.'}
      </p>

      <People data={data} onClear={onClear} />
      {said('roster')}

      {data.can_manage ? null : (
        <p className="ur-small">Only this workspace&rsquo;s admins change roles. You can read them here.</p>
      )}

      <RoleTable summaries={data.roles} />

      {data.can_manage && <DefaultForm current={data.default_role} onSet={onDefault} />}
      {said('default')}
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

/** Who gave the caller the role they hold, when they hold none of their own (S5b's lone admin: "a platform admin"). */
function myGiver(me: WorkspaceRoles['me']): string {
  if (me.granted_by !== undefined && me.granted_by !== '') return me.granted_by
  if (me.source === 'platform_admin') return 'a platform admin'
  if (me.source === 'default') return 'the default'
  return ''
}

/**
 * THE ROSTER IS A TABLE EVEN FOR A LONE ADMIN (S5b), WITH THE CALLER'S OWN ROW.
 * Every person with a role of their own, and the caller, marked "you": from
 * their own grant when they hold one, else from `me` (the default, or a
 * platform admin's reach), with nothing to return. Everyone else holds the
 * default and is not listed, because the platform cannot list a group's
 * members (§4.3).
 */
function People({ data, onClear }: { data: WorkspaceRoles; onClear: (email: string) => Promise<boolean> }) {
  const [busy, setBusy] = useState<string | null>(null)
  const canManage = data.can_manage
  const mine = (data.me.email || '').toLowerCase()
  const isMe = (email: string) => mine !== '' && email.toLowerCase() === mine
  const held = data.people.some((p) => isMe(p.email))
  const you = <small className="wp-you"> you</small>
  return (
    // keys.css's stacking table (#1680): at 390 px this roster was 589 px wide
    // in the content scroller (CONFIG-S5-LEADS review), so below 720 px each
    // person is a card, each value under its column's `data-label`.
    <table role="table" className="mcp-servers" aria-label={`Who holds which role in ${data.tenant_id || 'this workspace'}`}>
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
        {!held && (
          <tr data-you="true">
            <th scope="row" data-label="Person">{data.me.email || 'You'}{data.me.email !== '' && you}</th>
            <td data-label="Role">{data.me.role || <Dash why="Your role could not be read" />}</td>
            <td data-label="Given by">{myGiver(data.me)}</td>
            <td data-label="When" title={data.me.granted_at}>{data.me.granted_at === undefined || data.me.granted_at === '' ? '' : timeAgo(data.me.granted_at)}</td>
            <td data-label="Reason" />
            {canManage && <td data-label="Actions" />}
          </tr>
        )}
        {data.people.map((p) => (
          <tr key={p.email} data-you={isMe(p.email) ? 'true' : undefined}>
            <th scope="row" data-label="Person">{p.email}{isMe(p.email) && you}</th>
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
  const [role, setRole] = useState<RoleName>('member')
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

  // The card's own head names the form (S5b): "Give a person their own role".
  return (
    <form className="pk-form" autoComplete="off" aria-label="Give a person their own role" onSubmit={(e) => void save(e)}>
      <p className="ur-small">
        Google owns who is in the workspace: a role given to someone not in its group yet applies when they join.
      </p>
      <div className="pk-form-row">
        <label htmlFor={emailId}>Email</label>
        <input id={emailId} className="ur-search pk-input" type="email" value={email} required maxLength={254}
          spellCheck={false} onChange={(e) => setEmail(e.target.value)} />
      </div>
      {/* S5b's viewer | member | admin, the console's segmented control in place of a <select>.
          Its buttons are `type="button"`, so a pick never submits the form. */}
      <div className="pk-form-row">
        <span className="wp-label" aria-hidden="true">Role</span>
        <Segmented<RoleName> options={ROLES.map((r) => ({ key: r, label: r }))} value={role} onChange={setRole} label="Role" small />
      </div>
      <label htmlFor={`${id}-reason`}>Reason (optional, shown in the events)</label>
      <input id={`${id}-reason`} className="ur-search pk-input" value={reason} maxLength={280}
        onChange={(e) => setReason(e.target.value)} />
      <div className="pk-form-row">
        <Button kind="primary" type="submit" busy={busy}>Set role</Button>
      </div>
    </form>
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

/** The reason a role event carries, shown after its age as S5b's rows do ("3 d ago · workspace owner"). */
function eventReason(e: WorkspaceRoleEvent): string {
  const r = e.detail.reason
  return typeof r === 'string' ? r.trim() : ''
}

/** Role events, the card's body: each event's sentence over its age and reason (S5b's `.stp` rows). */
function Events({ events }: { events: WorkspaceRoleEvent[] }) {
  if (events.length === 0) {
    return <p className="ur-small">No role has been given or changed here, and no platform admin has acted here.</p>
  }
  return (
    <ul className="ws-steps wp-events" aria-label="Role events">
      {events.map((e, i) => {
        const meta = [e.at === '' ? '' : timeAgo(e.at), eventReason(e)].filter((x) => x !== '').join(' · ')
        return (
          <li key={`${e.at}-${i}`} className="ws-step">
            <span className="ws-step-t">
              <b>{eventWords(e)}</b>
              {meta !== '' && <small title={e.at}>{meta}</small>}
            </span>
          </li>
        )
      })}
    </ul>
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
