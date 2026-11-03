import { useState, type FormEvent, type MouseEvent, type ReactNode } from 'react'
import { loadProfile, loadWorkspaceRoles, setWorkspaceDefaultRole, type ApiError } from './api'
import { Breadcrumb, Button, Card, Chip, Dash, ProgressBar, Segmented, Tabs, routedClick, type TabDef } from './components'
import { setupCount, useSetupSteps, type SetupStep } from './ConfigSetup'
import { ACCESS as GITHUB_ACCESS_ADDRESS, REPOSITORIES as REPOSITORIES_ADDRESS } from './Onboarding'
import { CONFIG_SECRETS_ADDRESS, addressToPath } from './paths'
import { UrRefresh, UrRegion, useUrRead, type UrRead } from './RepositoriesParts'
import { DEFAULT_ROLES, ROLES } from './roles'
import { OWN_WORKSPACE_LABEL } from './Spine'
import { timeAgo, type Profile, type WorkspaceRoleEvent, type WorkspaceRoles } from './types'
import { WorkspacePeopleScreen } from './WorkspacePeople'
import { WorkspaceSettingsScreen, effectiveRole } from './WorkspaceSettings'
import './styles/workspace-page.css'

/**
 * ONE WORKSPACE'S PAGE (lane workspace-admin-page; part of #2688). The owner
 * picked administration by workspace (the 'place' mock-up, 2026-11-02):
 * Admin › Workspaces lists the workspaces a person may see, and each one has
 * this page, with tabs People, Roles, Settings and Setup. This component is
 * the page; the admin routes lane mounts it.
 *
 * WHICH WORKSPACE. The acting one: every read here is the one Config › People
 * and roles and Workspace Settings make today (`GET /v1/profile`,
 * `GET /v1/workspace/roles`, and what the embedded screens read), so the page
 * needs no new route. Whoever mounts it selects the workspace first, as the
 * workspace switcher does.
 *
 * WHO SEES WHAT. The roles read's `can_manage` decides (a workspace admin, or
 * a platform admin, who is admin in every group workspace): a manager gets
 * every control the embedded screens offer; anyone else reads the same tabs
 * with nothing to change, and the header says "read only for you". A platform
 * admin's header says "as a platform admin", and only they are offered Add a
 * new workspace on the Setup tab (the API is the gate: POST
 * /v1/admin/workspaces refuses anyone else). A personal workspace has its
 * person as its fixed admin and no default role.
 *
 * TODAY'S SCREENS, EMBEDDED. People is Config › People and roles
 * (`WorkspacePeopleScreen`), with Members' GitHub under it for a platform
 * admin as its tab was; Settings and Setup are Workspace Settings' parts
 * (`WorkspaceSettingsScreen`). Each is drawn `embedded`: without its own
 * heading or Config tab strip, which this page's header and tabs replace.
 * Roles is the page's own card set over the same roles read: what each role
 * may do, the default with Set default, and the role events.
 *
 * A COUNT THE CONSOLE CANNOT KNOW reads "not known", never 0.
 */

export type WorkspaceTab = 'people' | 'roles' | 'settings' | 'setup'

export const WORKSPACE_TABS: readonly WorkspaceTab[] = ['people', 'roles', 'settings', 'setup']

/** The tab a query names (`tab=roles`); anything else is People. */
export function workspaceTabOf(view: string | null | undefined): WorkspaceTab {
  const t = new URLSearchParams(view ?? '').get('tab')
  return (WORKSPACE_TABS as readonly string[]).includes(t ?? '') ? (t as WorkspaceTab) : 'people'
}

/** The Config pages the rest of a workspace's setup is done on, each named once. */
const SETUP_PAGES: readonly { key: SetupStep['key'] | 'mcp'; label: string; to: string; what: string }[] = [
  { key: 'github', label: 'GitHub access', to: GITHUB_ACCESS_ADDRESS, what: 'Connect GitHub, install the App, enable orgs.' },
  { key: 'repositories', label: 'Repositories', to: REPOSITORIES_ADDRESS, what: 'The repositories agents may read or write.' },
  { key: 'secrets', label: 'Secrets', to: CONFIG_SECRETS_ADDRESS, what: "The workspace's keys and who may use them." },
  { key: 'mcp', label: 'MCP servers', to: 'config/mcp-servers', what: 'The tool servers agents may reach.' },
]

const STATE_WORD: Readonly<Record<string, string>> = { done: 'done', in_progress: 'in progress', failed: 'failed', stale: 'needs a re-check', todo: 'to do', unknown: 'not known' }

function routed(to: string, go?: (to: string) => void) {
  return (e: MouseEvent) => {
    if (go === undefined || !routedClick(e)) return
    e.preventDefault()
    go(to)
  }
}

function dataOf<T>(r: UrRead<T>['state']): T | null {
  return r.status === 'ok' || r.status === 'stale' ? r.data : null
}

function refusal(e: ApiError): string {
  return e.code === null ? e.message : `${e.message} (${e.code})`
}

function RoleChip({ role, def = false }: { role: string; def?: boolean }) {
  return <span className={`wpg-role is-${role}${def ? ' is-def' : ''}`}>{role}</span>
}

/** The header's setup chip: the Config setup strip's count, and its next step; opens the Setup tab. */
function SetupChip({ onOpen }: { onOpen: () => void }) {
  const steps = useSetupSteps()
  const { done, of, unknown } = setupCount(steps)
  const failed = steps.find((s) => s.state === 'failed') ?? null
  const known = of > 0 && unknown < of
  const words = known ? `setup ${done} of ${of}` : 'setup not known'
  return (
    <button type="button" className={`wpg-setup-chip${known && done < of ? ' is-warn' : ''}`} onClick={onOpen} title="The setup checklist: the Setup tab">
      <ProgressBar done={done} total={known ? of : 0} unread={unknown} tone={known && done === of ? 'live' : 'park'} label={known ? `Setup ${done} of ${of} steps` : 'Setup not known'} />
      {words}
      {failed !== null && (
        <>
          {' · '}
          <b className="is-bad">{failed.label} failed</b>
        </>
      )}
    </button>
  )
}

export function WorkspacePage({
  tab: routedTab,
  onTab,
  go,
  back,
}: {
  /** The tab to open; held here once picked unless `onTab` takes it. */
  tab?: WorkspaceTab
  /** Told the tab picked, for whoever mounts the page to write it into the address. */
  onTab?: (tab: WorkspaceTab) => void
  go?: (to: string) => void
  /** The list this page is reached from (Admin › Workspaces), as the breadcrumb's first segment. */
  back?: { label: string; href: string }
}) {
  const [picked, setPicked] = useState<WorkspaceTab>(routedTab ?? 'people')
  const [arrived, setArrived] = useState(routedTab)
  if (arrived !== routedTab) {
    setArrived(routedTab)
    if (routedTab !== undefined) setPicked(routedTab)
  }
  const tab = picked
  const select = (t: WorkspaceTab) => {
    setPicked(t)
    onTab?.(t)
  }
  const profile = useUrRead(loadProfile, 'profile')
  const roles = useUrRead(loadWorkspaceRoles, 'workspace-roles')
  const p = dataOf(profile.state)
  const r = dataOf(roles.state)
  const ws = p?.workspaces.find((w) => w.is_acting) ?? null
  const platformAdmin = p !== null && p.identity.is_admin === true
  const personal = ws?.kind === 'user' || r?.kind === 'user'
  const canManage = r !== null ? r.can_manage : personal || platformAdmin
  const readOnly = !personal && r !== null && !r.can_manage
  const name = ws === null ? (r?.tenant_id ?? '') : personal ? OWN_WORKSPACE_LABEL : ws.display_name || ws.tenant_id
  const role = p !== null && ws !== null ? effectiveRole(p, ws, roles) : null
  const peopleCount: TabDef['count'] =
    roles.state.status === 'error' || roles.state.status === 'empty' ? 'not known' : r === null ? undefined : personal ? undefined : r.people.length

  const tabs: TabDef[] = [
    { key: 'people', label: 'People', count: peopleCount, why: peopleCount === 'not known' ? 'The roles read did not answer' : 'People with a role of their own' },
    { key: 'roles', label: 'Roles' },
    { key: 'settings', label: 'Settings' },
    { key: 'setup', label: 'Setup' },
  ]

  return (
    <div className="wpg-page">
      {back !== undefined && <Breadcrumb onGo={go} crumbs={[{ key: 'all', label: back.label, href: back.href }, { key: 'this', label: name || 'This workspace', href: null }]} />}
      <div className="wpg-head">
        <div className="wpg-head-t">
          <h1>{name || (profile.state.status === 'loading' ? 'Reading the workspace…' : 'This workspace')}</h1>
          <p className="wpg-chips" aria-label="About this workspace">
            {ws !== null && <Chip title="The workspace's id">{ws.tenant_id}</Chip>}
            {ws !== null && personal && <span className="wpg-dim">personal</span>}
            {!personal && (
              <span className="wpg-dim">
                default {r === null ? <Dash why="The roles read has not answered" /> : r.default_role === null ? <Dash why="The roles read names no default role" /> : <RoleChip role={r.default_role} def />}
              </span>
            )}
            <span className="wpg-dim" data-testid="wpg-you">
              you {role === null || role.name === '' ? <Dash why="Your role is not known yet" /> : <RoleChip role={role.name} />}
            </span>
            <SetupChip onOpen={() => select('setup')} />
            {platformAdmin && !personal && <Chip>as a platform admin</Chip>}
            {readOnly && <span className="wpg-tag">read only for you</span>}
          </p>
        </div>
        <UrRefresh reads={[profile, roles]} />
      </div>
      <Tabs tabs={tabs} current={tab} label={name || 'This workspace'} className="wpg-tabs" onSelect={(k) => select(k as WorkspaceTab)} />
      <div className="wpg-body" role="tabpanel" aria-label={tabs.find((t) => t.key === tab)?.label}>
        {tab === 'people' && <PeopleTab platformAdmin={platformAdmin} />}
        {tab === 'roles' && <RolesTab roles={roles} canManage={canManage && !personal} />}
        {tab === 'settings' && <WorkspaceSettingsScreen part="settings" go={go} embedded />}
        {tab === 'setup' && <SetupTab platformAdmin={platformAdmin} profile={p} go={go} />}
      </div>
    </div>
  )
}

/**
 * PEOPLE: Config › People and roles, embedded, and under it Members' GitHub,
 * which was that page's second tab and is offered as it was, to a platform
 * admin (its route answers anyone else 403).
 */
function PeopleTab({ platformAdmin }: { platformAdmin: boolean }) {
  return (
    <div className="wpg-embed">
      <WorkspacePeopleScreen embedded view="view=roles" />
      {platformAdmin && (
        <details className="wpg-fold">
          <summary>Members&rsquo; GitHub</summary>
          <WorkspacePeopleScreen embedded view="view=members" />
        </details>
      )}
    </div>
  )
}

function eventWords(e: WorkspaceRoleEvent): ReactNode {
  const d = e.detail
  const to = typeof d.to === 'string' ? d.to : null
  const asAdmin = d.via === 'platform_admin' ? ', as a platform admin' : ''
  const who = e.target_email ?? 'someone'
  switch (e.action) {
    case 'workspace_role_granted':
      return <>{e.by} gave {who} {to === null ? 'a role' : <RoleChip role={to} />}{asAdmin}</>
    case 'workspace_role_cleared':
      return <>{e.by} returned {who} to the default{asAdmin}</>
    case 'workspace_default_role_set':
      return <>{e.by} set the default to {to === null ? '?' : <RoleChip role={to} />}{asAdmin}</>
    case 'workspace_role_grant_invalid':
      return <>A stored role for {who} failed its integrity check and is ignored</>
    case 'platform_admin_acted':
      return <>{e.by} acted here as a platform admin</>
    case 'platform_admin_viewed':
      return <>{e.by} read this workspace as a platform admin</>
    default:
      return <>{e.by}: {e.action}</>
  }
}

/** ROLES: what each role may do with how many hold it, the default and Set default, and the role events. */
function RolesTab({ roles, canManage }: { roles: UrRead<WorkspaceRoles>; canManage: boolean }) {
  return (
    <UrRegion state={roles.state} route="GET /v1/workspace/roles" what="This workspace's roles" plural onRetry={roles.reload}>
      {(d) => <RolesCards data={d} canManage={canManage} reload={roles.reload} />}
    </UrRegion>
  )
}

function RolesCards({ data, canManage, reload }: { data: WorkspaceRoles; canManage: boolean; reload: () => void }) {
  const personal = data.kind === 'user'
  const [def, setDef] = useState<string>(data.default_role ?? 'viewer')
  const [said, setSaid] = useState<{ text: string; bad: boolean } | null>(null)
  const [busy, setBusy] = useState(false)
  const summary = (name: string) => data.roles.find((s) => s.name === name)?.summary ?? null
  const held = (name: string) => {
    if (personal) return name === 'admin' ? 'you' : ''
    const n = data.people.filter((x) => x.role === name).length
    return name === data.default_role ? `default + ${n} named` : `${n} named`
  }
  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setSaid(null)
    const r = await setWorkspaceDefaultRole(def)
    setBusy(false)
    if (r.status === 'error') {
      setSaid({ text: `The default was not changed: ${refusal(r.error)}`, bad: true })
      return
    }
    if (r.status === 'ok' || r.status === 'stale') setSaid({ text: `The default is now ${r.data.default_role} (was ${r.data.previous_default}).`, bad: false })
    reload()
  }
  return (
    <div className="wpg-two">
      <div className="wpg-col">
        <Card title="What each role may do">
          <ul className="wpg-rolelist" aria-label="What each role may do">
            {ROLES.map((name) => (
              <li key={name}>
                <RoleChip role={name} />
                <span>{summary(name) ?? <Dash why="The roles read did not say what this role permits" />}</span>
                <small className="wpg-num">{held(name)}</small>
              </li>
            ))}
          </ul>
          {!personal && (
            <details className="wpg-why">
              <summary>Why the default is never admin</summary>
              <p>
                Admin is given person by person, so nobody gains it by being added to the Google group. A platform admin is admin in every group
                workspace; a personal workspace&rsquo;s person is its fixed admin.
              </p>
            </details>
          )}
        </Card>
        {personal ? (
          <Card title="Default role">
            <p className="wpg-small">A personal workspace has only its person, its fixed admin, so it has no default role.</p>
          </Card>
        ) : (
          <Card title="Default role">
            {canManage ? (
              <form className="wpg-give" onSubmit={(e) => void submit(e)}>
                <Segmented small label="Default role" value={def} onChange={(k) => setDef(k)} options={DEFAULT_ROLES.map((k) => ({ key: k, label: k }))} />
                <Button kind="secondary" type="submit" disabled={busy || def === data.default_role}>
                  Set default
                </Button>
                <small className="wpg-dim">Everyone in the workspace&rsquo;s group without a role of their own.</small>
              </form>
            ) : (
              <p className="wpg-small">
                Everyone in the workspace&rsquo;s group without a role of their own holds{' '}
                {data.default_role === null ? <Dash why="The roles read names no default role" /> : <RoleChip role={data.default_role} def />}.
              </p>
            )}
            {said !== null && (
              <p className={`wpg-said${said.bad ? ' is-bad' : ''}`} role={said.bad ? 'alert' : 'status'}>
                {said.text}
              </p>
            )}
          </Card>
        )}
      </div>
      <div className="wpg-col is-side">
        <Card title="Role events">
          {data.audit.length === 0 ? (
            <p className="wpg-dim wpg-small">No role has been given or changed here, and no platform admin has acted here.</p>
          ) : (
            <ul className="wpg-events" aria-label="Role events">
              {data.audit.map((e, i) => (
                <li key={`${e.at}-${i}`}>
                  <small title={e.at}>{e.at === '' ? '' : timeAgo(e.at)}</small>
                  <span>{eventWords(e)}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </div>
  )
}

/**
 * SETUP: where the rest of setup is done (the Config pages for GitHub access,
 * Repositories, Secrets and MCP servers, each with the setup strip's state
 * where it measures one), then Workspace Settings' setup part embedded, and,
 * for a platform admin only, Add a new workspace, drawn in place.
 */
function SetupTab({ platformAdmin, profile, go }: { platformAdmin: boolean; profile: Profile | null; go?: (to: string) => void }) {
  const steps = useSetupSteps()
  const [adding, setAdding] = useState(false)
  return (
    <div className="wpg-embed">
      <Card title="Where the rest of setup is done">
        <ul className="wpg-links" aria-label="Setup pages">
          {SETUP_PAGES.map((page) => {
            const step = steps.find((s) => s.key === page.key) ?? null
            return (
              <li key={page.key}>
                <a className="ctl-link" href={addressToPath(page.to)} onClick={routed(page.to, go)}>
                  {page.label}
                </a>
                <small className="wpg-dim">{page.what}</small>
                {step !== null && <span className={`wpg-state is-${step.state}`}>{STATE_WORD[step.state] ?? step.state}</span>}
              </li>
            )
          })}
        </ul>
      </Card>
      <WorkspaceSettingsScreen part="setup" go={go} embedded />
      {platformAdmin && (
        <Card
          title="Add a new workspace"
          action={
            adding ? (
              <Button size="sm" kind="ghost" onClick={() => setAdding(false)}>
                Close
              </Button>
            ) : (
              <Button size="sm" kind="primary" onClick={() => setAdding(true)}>
                Add a new workspace
              </Button>
            )
          }
        >
          <p className="wpg-small">
            {profile?.identity.email ?? 'You'}, as a platform admin, may add a personal, group or service workspace. It is filed with{' '}
            <code>POST /v1/admin/workspaces</code>, as Admin › Tenants files it.
          </p>
        </Card>
      )}
      {platformAdmin && adding && <WorkspaceSettingsScreen part="new" go={go} embedded />}
    </div>
  )
}
