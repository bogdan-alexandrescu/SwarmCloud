/**
 * ADMIN › WORKSPACES (HOME-ADMIN, part of #2688; the owner picked a page per
 * workspace for administration, the 'place' mock-up's Workspaces list,
 * 2026-11-03). It was Workspaces you admin (NAV-ADMIN-ROLE; part of #1633,
 * docs/design/workspace-roles.md §4.8).
 *
 * WHICH WORKSPACES IT LISTS, BY WHO READS IT (`listedWorkspaces`): every group
 * workspace for a platform admin -- the ones they are in and the ones they
 * reach only as a platform admin (`via: "platform_admin"`) alike; the ones a
 * workspace admin administers (`adminWorkspaces`); and for a member, the group
 * workspaces they are in, read only. The caller's own personal workspace is
 * the first row, as the mock-up draws it; other people's never are. Every
 * entry comes from the one read the page made before, `GET /v1/tenants/mine`,
 * and who is a platform admin from `GET /v1/tenants/me`, which the frame reads
 * on every page.
 *
 * FURTHEST BEHIND ON SETUP FIRST (`orderWorkspaces`): each workspace's setup
 * is read for that workspace (`loadWorkspaceSetup`, which names it in
 * `X-Swarm-Tenant` without switching the one in hand, CONFIG-PANEL-E), and the
 * rows are ordered by steps done, fewest first; a workspace whose setup is not
 * read yet, or could not be, is not known to be behind or ahead, so it goes
 * after every measured one. "A to Z" orders by name. A read that failed draws
 * "not read", never done, and a count that needs a read the page lacks says
 * "not known", never 0.
 *
 * EACH ROW: the workspace's name, linking to its own page
 * (`admin/workspaces/<tenant>`); its setup progress (the Config strip's own
 * `SetupProgress`); what waits in it (a failed step, a step to re-check, the
 * next step, or nothing); the caller's role there; and, for a workspace they
 * administer, today's four Config links. EACH CONFIG LINK SELECTS ITS
 * WORKSPACE FIRST, then navigates (`chooseTenant`, the tenant block's switch),
 * so the page it lands on reads that workspace. A member's row has no Config
 * links: what they may change is not here.
 *
 * THE LEAD names the administered workspace furthest behind, its next step,
 * and one button, "Open <X>'s Config", which selects it and opens that step's
 * Config page (CONFIG-ADMIN-POLISH; part of #2157, S10a).
 *
 * THE ROLE IS THE ONE THE API ACTS ON (VQ-044; part of #2391): each entry's
 * `role` is `RoleStore.resolve`'s answer, which is `admin` for a platform admin
 * in every group workspace, whatever their stored grant says. So the page
 * never calls it a stored role, and marks such a row "admin as a platform
 * admin", the words People and roles uses for the same answer.
 */
import { useEffect, useState, useSyncExternalStore, type MouseEvent as ReactMouseEvent } from 'react'
import { loadMe, loadMyTenants, type TenantChoice } from './api'
import { SetupProgress, behindOf, setupCount, setupNext, setupSteps, type SetupStep } from './ConfigSetup'
import { loadWorkspaceSetup } from './configSetupApi'
import type { Result } from './fetch'
import type { OnboardingDoc } from './types'
import { ButtonLink, Chip, EmptyState, Segmented, routedClick } from './components'
import { chooseTenant } from './fetch'
import { AdminLead } from './AdminSettings'
import { feedData, notificationsSnapshot, subscribeNotifications } from './notificationFeed'
import { CONFIG_SECRETS_ADDRESS, addressToPath } from './paths'
import { UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import { WORKSPACE_PEOPLE_ADDRESS, roleOf } from './roles'
import { PageHead } from './Shell'
import { adminWorkspaces, isReach, joinsToDecide, nameTenants } from './Spine'
import { WORKSPACE_NEW_ADDRESS } from './WorkspaceSettings'

/** The four Config pages a workspace admin changes, by their tab names (S10a). */
export const WORKSPACE_ADMIN_LINKS: readonly { label: string; to: string }[] = [
  { label: 'Workspace Settings', to: 'config/workspace' },
  { label: 'MCP servers', to: 'config/mcp-servers' },
  { label: 'People and roles', to: WORKSPACE_PEOPLE_ADDRESS },
  // Workspace secrets and the workspace key (Keys for agents and MCP).
  { label: 'Secrets', to: CONFIG_SECRETS_ADDRESS },
]

/** Where "Open <X>'s Config" lands when no step is next: Config's first page, as `/config` does. */
const CONFIG_LANDING = 'config/github'

/** One workspace's own page (the admin routes lane draws it). */
export const workspacePageAddress = (tenantId: string): string => `admin/workspaces/${tenantId}`

/**
 * Its path, written out: until the router knows the address, `addressToPath`
 * maps it to Overview, so the link carries the path itself and is followed as
 * a plain link; once the router knows it, a click routes in place.
 */
export const workspacePagePath = (tenantId: string): string => `/admin/workspaces/${encodeURIComponent(tenantId)}`

/** Whose list this is: a platform admin's, a workspace admin's, or a member's (read only). */
export type WorkspacesView = 'platform' | 'admin' | 'member'

/**
 * The workspaces the list draws, and as whom. A platform admin is the frame's
 * `is_admin`, or -- with `/me` unread -- any entry the server marks as theirs
 * (a reach entry, or a role resolved from the platform role).
 */
export function listedWorkspaces(
  choices: readonly TenantChoice[],
  platformAdmin: boolean,
): { view: WorkspacesView; listed: TenantChoice[]; personal: TenantChoice | null } {
  const personal = choices.find((c) => c.kind === 'user') ?? null
  const groups = choices.filter((c) => c.kind === 'group')
  if (platformAdmin || choices.some((c) => isReach(c) || c.role?.source === 'platform_admin')) return { view: 'platform', listed: groups, personal }
  const administered = adminWorkspaces(choices)
  if (administered.length > 0) return { view: 'admin', listed: administered, personal }
  return { view: 'member', listed: groups.filter((c) => !isReach(c)), personal }
}

export type WorkspacesOrder = 'behind' | 'name'

/**
 * Furthest behind first: fewest setup steps done first, ties by name; a
 * workspace whose setup is not read (`steps` null) after every measured one,
 * by name. `name` is A to Z.
 */
export function orderWorkspaces<T extends { name: string; steps: readonly SetupStep[] | null }>(rows: readonly T[], order: WorkspacesOrder): T[] {
  const byName = (a: T, b: T) => a.name.localeCompare(b.name)
  if (order === 'name') return rows.slice().sort(byName)
  const done = (r: T) => (r.steps === null ? Number.POSITIVE_INFINITY : setupCount(r.steps).done)
  return rows.slice().sort((a, b) => done(a) - done(b) || byName(a, b))
}

/** What waits in a workspace, from its setup: a failed step, one to re-check, the next one, or nothing. */
export function setupWaits(steps: readonly SetupStep[] | null): { text: string; tone: 'bad' | 'park' | null }[] {
  if (steps === null) return [{ text: 'not known', tone: null }]
  const held = steps.filter((s) => s.state === 'failed' || s.state === 'stale')
  if (held.length > 0) return held.map((s) => ({ text: `${s.label}: ${s.state === 'failed' ? 'failed' : 'needs a re-check'}`, tone: s.state === 'failed' ? 'bad' : 'park' }))
  const next = setupNext(steps)
  if (next !== null) return [{ text: `${next.label}: waits`, tone: 'park' }]
  const { done, of } = setupCount(steps)
  return [{ text: done === of ? 'nothing waits' : 'not known', tone: null }]
}

const SUB: Readonly<Record<WorkspacesView, string>> = {
  platform: 'Every group workspace; you are admin in each as a platform admin. A Config link selects that workspace, then opens the page.',
  admin: 'The group workspaces you administer, and your own. A Config link selects that workspace, then opens the page.',
  member: 'The group workspaces you are in, and your own, read only: their admins change them.',
}

function EmptyList({ view }: { view: WorkspacesView | null }) {
  if (view === 'platform') {
    return (
      <EmptyState kind="empty" heading="No group workspace yet">
        No group workspace is registered. Add one with Add a new workspace; your personal workspace is under Profile.
      </EmptyState>
    )
  }
  if (view === 'member') {
    return (
      <EmptyState kind="empty" heading="You are in no group workspace">
        No group workspace lists you. Your personal workspace is yours to change, under Profile.
      </EmptyState>
    )
  }
  return (
    <EmptyState kind="empty" heading="You administer no group workspace">
      No group workspace names you as its admin. Your personal workspace is yours to change, under Profile.
    </EmptyState>
  )
}

/** Each listed workspace's setup read, by tenant id; absent while in flight. */
function useWorkspaceSetups(ids: readonly string[]): Readonly<Record<string, Result<OnboardingDoc>>> {
  const [reads, setReads] = useState<Record<string, Result<OnboardingDoc>>>({})
  const key = ids.join('\n')
  useEffect(() => {
    const controller = new AbortController()
    setReads({})
    for (const id of key === '' ? [] : key.split('\n')) {
      void loadWorkspaceSetup(id, controller.signal).then((r) => {
        if (!controller.signal.aborted) setReads((was) => ({ ...was, [id]: r }))
      })
    }
    return () => controller.abort()
  }, [key])
  return reads
}

const docOf = (r: Result<OnboardingDoc> | undefined): OnboardingDoc | null => (r !== undefined && (r.status === 'ok' || r.status === 'stale') ? r.data : null)

type Open = (e: ReactMouseEvent, tenantId: string, to: string) => void
type Go = (e: ReactMouseEvent, to: string) => void
/** Opens one workspace's page: routed once the router knows its address, followed as a plain link until then. */
type OpenPage = (e: ReactMouseEvent, tenantId: string) => void

/** The counts over the list, and the requests the feed serves: a count the page cannot know says so. */
function Counts({ view, total, setUp, behind }: { view: WorkspacesView; total: number; setUp: number | null; behind: number | null }) {
  const feed = feedData(useSyncExternalStore(subscribeNotifications, notificationsSnapshot, notificationsSnapshot))
  const requests = view === 'platform' ? (typeof feed?.admin_pending === 'number' ? feed.admin_pending : null) : view === 'admin' ? joinsToDecide(feed) : undefined
  const n = (v: number | null) => (v === null ? 'not known' : String(v))
  return (
    <div className="wya-links wya-counts" role="list" aria-label="Workspaces, counted">
      <span role="listitem"><Chip><b>{total}</b> group {total === 1 ? 'workspace' : 'workspaces'}</Chip></span>
      <span role="listitem"><Chip><b>{n(setUp)}</b> set up</Chip></span>
      <span role="listitem"><Chip tone={behind !== null && behind > 0 ? 'park' : undefined}><b>{n(behind)}</b> behind</Chip></span>
      {requests !== undefined && (
        <span role="listitem">
          <Chip href={addressToPath('admin/people')} tone={requests !== null && requests > 0 ? 'park' : undefined}>
            <b>{n(requests)}</b> {requests === 1 ? 'request waits' : 'requests wait'} in {view === 'platform' ? 'Platform people' : 'People and roles'}
          </Chip>
        </span>
      )}
    </div>
  )
}

function WorkspaceList({ choices, platformAdmin, open, openPage }: { choices: readonly TenantChoice[]; platformAdmin: boolean; open: Open; openPage: OpenPage }) {
  const [order, setOrder] = useState<WorkspacesOrder>('behind')
  const { view, listed, personal } = listedWorkspaces(choices, platformAdmin)
  const admins = new Set(listed.filter((c) => view === 'platform' || roleOf(c) === 'admin').map((c) => c.tenant_id))
  // `nameTenants` keeps no role, so whose admin role is a platform admin's is read before naming.
  const asPlatformAdmin = new Set(listed.filter((c) => view === 'platform' || c.role?.source === 'platform_admin').map((c) => c.tenant_id))
  const roles = new Map(listed.map((c) => [c.tenant_id, roleOf(c)]))
  const named = nameTenants(listed, null)
  const setups = useWorkspaceSetups(named.map((w) => w.tenant_id))
  if (named.length === 0) return <EmptyList view={view} />
  // Every listed workspace is a group one, so each has four steps.
  const rows = named.map((w) => {
    const read = setups[w.tenant_id]
    const doc = docOf(read)
    return { w, name: w.display_name, read, steps: doc === null ? null : setupSteps(doc, false) }
  })
  const measured = rows.filter((r): r is (typeof rows)[number] & { steps: SetupStep[] } => r.steps !== null)
  const allRead = measured.length === rows.length
  const setUp = allRead ? measured.filter((r) => { const c = setupCount(r.steps); return c.done === c.of }).length : null
  const behind = behindOf(measured.filter((r) => admins.has(r.w.tenant_id)))
  const behindNext = behind === null ? null : setupNext(behind.steps)
  return (
    <>
      <Counts view={view} total={rows.length} setUp={setUp} behind={setUp === null ? null : rows.length - setUp} />
      {behind !== null && (
        <AdminLead
          tone="next"
          kicker="Your workspaces"
          title={`${behind.name} is furthest behind`}
          body={behindNext !== null ? `Next: ${behindNext.label.toLowerCase()} (${behindNext.line}).` : undefined}
          action={
            <ButtonLink kind="primary" href={addressToPath(behindNext?.to ?? CONFIG_LANDING)} onClick={(e) => open(e, behind.w.tenant_id, behindNext?.to ?? CONFIG_LANDING)}>
              Open {behind.name}’s Config
            </ButtonLink>
          }
        />
      )}
      <Segmented
        small
        label="Order"
        value={order}
        onChange={setOrder}
        options={[
          { key: 'behind', label: 'Furthest behind first' },
          { key: 'name', label: 'A to Z' },
        ]}
      />
      <ul className="wya-list" data-view={view}>
        {personal !== null && (
          <li className="wya-row is-personal" data-tenant={personal.tenant_id}>
            <span className="wya-name">
              <a href={workspacePagePath(personal.tenant_id)} onClick={(e) => openPage(e, personal.tenant_id)}>
                <b>Your workspace</b>
              </a>
              <small>{personal.tenant_id}</small>
            </span>
            <small className="wya-reading">personal · you are its fixed admin; its settings are under Profile</small>
          </li>
        )}
        {orderWorkspaces(rows, order).map(({ w, read, steps }) => {
          const admin = admins.has(w.tenant_id)
          const role = roles.get(w.tenant_id) ?? null
          return (
            <li key={w.tenant_id} className="wya-row" data-tenant={w.tenant_id} data-admin={admin ? 'yes' : 'no'}>
              <span className="wya-name">
                <a href={workspacePagePath(w.tenant_id)} onClick={(e) => openPage(e, w.tenant_id)}>
                  <b>{w.display_name}</b>
                </a>
                <small>{w.group !== w.display_name ? w.group : w.tenant_id}</small>
                {asPlatformAdmin.has(w.tenant_id) ? (
                  <small className="wya-role" data-via="platform_admin">admin as a platform admin</small>
                ) : (
                  <small className="wya-role" data-role={role ?? 'unknown'}>{role === null ? 'your role: not known' : `your role: ${role}`}</small>
                )}
              </span>
              <span className="wya-waits" aria-label={`What waits in ${w.display_name}`}>
                {setupWaits(steps).map((t) => (
                  <Chip key={t.text} tone={t.tone ?? undefined} faint={t.tone === null}>
                    {t.text}
                  </Chip>
                ))}
              </span>
              {admin && (
                <nav className="wya-links" aria-label={`${w.display_name} settings`}>
                  {WORKSPACE_ADMIN_LINKS.map((l) => (
                    <a key={l.to} href={addressToPath(l.to)} onClick={(e) => open(e, w.tenant_id, l.to)}>
                      {l.label}
                    </a>
                  ))}
                </nav>
              )}
              <div className="wya-setup">
                {read === undefined ? (
                  <small className="wya-reading">Reading setup…</small>
                ) : read.status === 'error' ? (
                  <small className="wya-reading">Setup not read: {read.error.message}</small>
                ) : (
                  <SetupProgress name={w.display_name} steps={steps ?? setupSteps(null, false)} onNext={(s, e) => open(e, w.tenant_id, s.to)} />
                )}
              </div>
            </li>
          )
        })}
      </ul>
    </>
  )
}

export function WorkspacesYouAdminScreen({ go }: { go: (to: string) => void }) {
  const mine = useUrRead(loadMyTenants, 'mine')
  const me = useUrRead(loadMe, 'me')
  const who = me.state.status === 'ok' || me.state.status === 'stale' ? me.state.data : null
  const platformAdmin = who?.principal.is_admin === true
  const open: Open = (e, tenantId, to) => {
    if (!routedClick(e)) return
    e.preventDefault()
    chooseTenant(tenantId)
    go(to)
  }
  const goTo: Go = (e, to) => {
    if (!routedClick(e)) return
    e.preventDefault()
    go(to)
  }
  const openPage: OpenPage = (e, tenantId) => {
    if (addressToPath(workspacePageAddress(tenantId)) !== workspacePagePath(tenantId)) return
    goTo(e, workspacePageAddress(tenantId))
  }
  const choices = mine.state.status === 'ok' || mine.state.status === 'stale' ? mine.state.data : []
  const view = mine.state.status === 'ok' || mine.state.status === 'stale' ? listedWorkspaces(choices, platformAdmin).view : platformAdmin ? 'platform' : null
  return (
    <div className="ur-page wya-page">
      <PageHead title="Workspaces">
        {view === 'platform' && (
          <ButtonLink kind="primary" href={addressToPath(WORKSPACE_NEW_ADDRESS)} onClick={(e) => goTo(e, WORKSPACE_NEW_ADDRESS)}>
            Add a new workspace
          </ButtonLink>
        )}
        <UrRefresh reads={[mine, me]} ageOf={[mine]} />
      </PageHead>
      <p className="ur-sub">{SUB[view ?? 'admin']}</p>
      <UrRegion state={mine.state} route="GET /v1/tenants/mine" what="Your workspaces" plural onRetry={mine.reload} empty={<EmptyList view={view} />}>
        {(data) => <WorkspaceList choices={data} platformAdmin={platformAdmin} open={open} openPage={openPage} />}
      </UrRegion>
    </div>
  )
}
