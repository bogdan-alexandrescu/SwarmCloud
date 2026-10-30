/**
 * THE SKY SPINE SHELL (rebrand, owner's pick 2026-10-01; navigation.html
 * Variant 2, spine.py).
 *
 *   SPINE   84px, deep blue (#0b3a7a -> #06101e), THE SAME IN BOTH THEMES. The
 *           Hive in its 44px full cut (white -> #7dd3fc), Submit (the sky
 *           button), Overview, Work, Automate, Capacity, Config (CONFIG-NAV,
 *           part of #2157), Admin; at its foot
 *           Help, then Profile (the person's avatar), the last item
 *           (NAV-FOOT, #1672). API reads is Capacity's last page.
 *   PANEL   236px. The wordmark and the environment pill, the tenant block,
 *           the open section's pages, the capacity meter and the user footer.
 *
 * Collapsing hides the panel and keeps the spine; hovering a spine item then
 * shows a flyout of that section's pages. Below 760px a 44px phone header (in
 * the spine's colour, with the compact seven-dot Hive) opens a drawer that
 * holds both columns.
 *
 * ADMIN IS DRAWN ONLY FOR SOMEONE WHO ADMINISTERS SOMETHING (amended
 * 2026-10-19, NAV-ADMIN-ROLE; part of #1633, owner decision "All under
 * Profile; Admin by role"): a platform admin (`/v1/tenants/me`), or a person
 * whose stored role is `admin` in at least one GROUP workspace
 * (`/v1/tenants/mine`, `adminWorkspaces`). Everyone else sees no Admin item,
 * flyout, drawer entry or panel -- no lock, no disabled rows -- and nothing is
 * drawn while either read is in flight. It reverses the rule written here
 * until then, "Admin is SHOWN to a non-admin, with a lock": a person who
 * administers nothing was shown four pages they could never use. The routes
 * stay, but since CONFIG-NAV (part of #2157, BGD 2026-10-23: "Admin is shown
 * only to admins") a direct link from someone who administers nothing goes
 * to Overview once both reads say so (`onAdminRedirect`), and since
 * ADMIN-REDIRECT (part of #2157, BGD 2026-10-24: "Redirect, in a later lane")
 * a workspace admin who is not a platform admin, on a platform Admin page, goes
 * to Workspaces you admin. Since ADMIN-PEOPLE-ROLES (part of #2157) People and
 * roles is theirs too, scoped by the API to the workspaces they administer, so
 * /admin/people is no longer sent away. Since visual QA pass 1 (admin-shell-23)
 * someone who administers nothing, and since pass 2 (admin2-01) a workspace
 * admin on a platform page, is refused in place instead; only a workspace
 * admin's bare /admin still goes to Workspaces you admin. A platform admin is never redirected. The API's own refusal of every admin route is unchanged.
 *
 * Production draws a red pill and a 3px red bar across the top; the pill shows
 * only what was measured (`classifyEnvironment`), never a hardcoded word.
 */
import { Fragment, useCallback, useEffect, useId, useMemo, useRef, useState, useSyncExternalStore, type FocusEvent as ReactFocusEvent, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent, type ReactNode, type RefObject } from 'react'
import { AGENT_TABS, type AgentTab } from './agentlist'
import { USE_FIXTURES, WORKSPACE_RENAMED_EVENT, loadApprovals, loadCapacity, loadMe, loadMyTenants, loadOnboarding, loadStats, loadWorkflowsById, type NotificationFeed, type ResourceClasses, type TenantChoice } from './api'
import { classifyEnvironment, envTreatment, servedEnvironment, SwarmMark } from './Brand'
import { Banner, Button, ButtonLink, NamedMark, Toaster, acknowledge, routedClick } from './components'
import { chooseTenant, chosenTenant, clearTenantSwitch, errorHeading, noteTenantSwitch, preferencesUnavailableSnapshot, probeSnapshot, read, route, subscribePreferencesUnavailable, subscribeProbes, subscribeTenant, subscribeTenantSwitch, tenantSwitchSnapshot, type Result } from './fetch'
import { HELP_GROUPS, HELP, TOPIC_IDS, helpAnchor } from './help'
import { STATE_MARK, type MarkHue, type MarkName } from './marks'
import { addressToPath } from './paths'
import { PageHead } from './Shell'
import { AdminRoleRefusal } from './WorkspaceRefusal'
import { shortcut, typedIntoField } from './shortcuts'
import { poolLabelAmong, type Capacity, type Me, type Stats, type TaskState } from './types'
import { ThemeToggle } from './ThemeToggle'
import { derivedStateOf } from './workflowlist'
import { useResourceClasses } from './Blockers'
import { needsALook } from './Capacity'
import { SignOutButton, SignedBackIn } from './SignOut'
import { iapFronted, signingOut } from './signoutState'
import { AppTakeover, OfflineBanner, wholeAppFault, useOnline } from './AppStates'
import { feedData, notificationsSnapshot, subscribeNotifications } from './notificationFeed'
import { firstName } from './PersonName'
import { NEAR_CEILING } from './AdminSettings'
import { roleOf } from './roles'
import { ConfigBadgeMark, SetupContext, SetupMeter, setupSteps, setupUnfinished, useConfigBadges, type SetupRead } from './ConfigSetup'

export type SpineSection = 'profile' | 'overview' | 'work' | 'automate' | 'capacity' | 'config' | 'admin' | 'help' | null

/** The spine sections that draw a panel of pages. */
type PanelSection = 'profile' | 'work' | 'automate' | 'capacity' | 'config' | 'admin'

/** One page in the panel: its label, its icon, the address `go()` takes, and its children. */
interface PanelPage {
  key: string
  label: string
  /** The page's icon (navigation.html V2 draws one per page). */
  icon: string
  to: string
  /**
   * An admin page among pages that are not (#136): drawn with the `admin`
   * mark. Never set in the Admin section, whose heading already says it.
   * `spine.labels.test.tsx` holds these to App.tsx `SECTIONS`' admin flags.
   */
  admin?: boolean
  kids?: { key: string; label: string; to: string; admin?: boolean }[]
}

const AGENT_LABEL: Readonly<Record<AgentTab, string>> = { live: 'Live', waiting: 'Waiting', recent: 'Recent' }

/** Capacity › API reads' tab (NAV-FOOT, #1672): App.tsx SECTIONS' id. */
const API_READS_KEY = 'api-reads'

/** Admin › Workspaces you admin's tab (NAV-ADMIN-ROLE; part of #1633). */
export const WORKSPACES_YOU_ADMIN_TAB = 'workspaces'

/**
 * The panel's pages, per spine section (common.py NAV). The `to` values are
 * router addresses, so they resolve exactly as a click on the old rail did.
 */
export const PANEL_PAGES: Readonly<Record<PanelSection, PanelPage[]>> = {
  // EVERY SETTINGS PAGE UNDER PROFILE (NAV-PROFILE; part of #1633, owner
  // decision 2026-10-19 "All under Profile; Admin by role"). They were four
  // "Settings ›" links stacked in the panel foot, and People and roles had no
  // link at all. The person's own pages first, then the selected workspace's.
  // Not SECTIONS entries (App.tsx): the addresses stay `settings/*`, and the
  // issue forms already name each page as Settings › <page>. THE PERSON'S OWN
  // PAGES ONLY since CONFIG-NAV (part of #2157, BGD 2026-10-23): Workspace,
  // MCP servers and People and roles are Config's.
  // API keys left this list for Config › Secrets' Keys for agents and MCP
  // (SSC-UI, part of #2158; BGD 2026-10-23 23:57Z, proposal E S1: Settings
  // keeps only Profile). `/settings/keys` still lands there (paths.ts).
  profile: [
    { key: 'profile', label: 'Profile', icon: 'people', to: 'settings/profile' },
  ],
  work: [
    {
      key: 'agents',
      label: 'Agents',
      icon: 'work',
      to: 'work/running',
      kids: AGENT_TABS.map((t) => ({ key: t, label: AGENT_LABEL[t], to: `work/running/${t}` })),
    },
    { key: 'workflows', label: 'Workflows', icon: 'workflows', to: 'work/workflows' },
    // ISSUE RUNS (intake-tenants.html 1A): a route and a SECTIONS tab since
    // #523, and missing here, so /runs was reachable only from a submit's
    // redirect (visual QA Q7, 2026-10-02).
    { key: 'runs', label: 'Runs', icon: 'runs', to: 'work/runs' },
    { key: 'timeline', label: 'Timeline', icon: 'timeline', to: 'work/timeline' },
    // Repositories, Setup and Access are Config's since CONFIG-NAV.
  ],
  // docs/schedules.md §6.1 (SD1): the section's two pages. Approvals carries
  // the inbox count, as the section does on the spine.
  automate: [
    { key: 'schedules', label: 'Schedules', icon: 'schedules', to: 'automate/schedules' },
    { key: 'approvals', label: 'Approvals', icon: 'approvals', to: 'automate/approvals' },
  ],
  capacity: [
    {
      key: 'pools',
      label: 'Pools',
      icon: 'capacity',
      to: 'capacity/pools',
      kids: [
        { key: 'pools', label: 'Ceilings', to: 'capacity/pools' },
        { key: 'profiles', label: 'By runner profile', to: 'capacity/profiles' },
      ],
    },
    { key: 'catalogue', label: 'Runtimes', icon: 'runtimes', to: 'capacity/catalogue' },
    { key: 'holders', label: 'Holders', icon: 'holders', to: 'capacity/holders' },
    {
      key: 'accounts',
      label: 'Accounts',
      icon: 'accounts',
      to: 'capacity/accounts',
      kids: [
        { key: 'accounts', label: 'Subscription accounts', to: 'capacity/accounts' },
        // ACCOUNT-HEALTH-PAGE (part of #2302): what went wrong on each account, after the list.
        { key: 'account-health', label: 'Account health', to: 'capacity/account-health' },
        { key: 'quota', label: 'Provider quota', to: 'capacity/quota', admin: true },
      ],
    },
    // NAV-FOOT (#1672, owner 2026-10-20): API reads left the spine's foot to
    // be Capacity's last page. Its "Every read / Failures only" pair is drawn
    // under it while it is the open page (`PanelPages`).
    { key: API_READS_KEY, label: 'API reads', icon: 'api', to: `capacity/${API_READS_KEY}` },
  ],
  // CONFIG (CONFIG-NAV; part of #2157, proposal E S1a): App.tsx SECTIONS'
  // six tabs in BGD's order. People and roles is left out of a personal
  // workspace (S1e, `configPagesFor`). Workspace setup is a part of Workspace
  // Settings, which lights for both its addresses.
  config: [
    { key: 'github', label: 'GitHub access', icon: 'github', to: 'config/github' },
    { key: 'repositories', label: 'Repositories', icon: 'repo', to: 'config/repositories' },
    { key: 'secrets', label: 'Secrets', icon: 'key', to: 'config/secrets' },
    { key: 'mcp-servers', label: 'MCP servers', icon: 'plug', to: 'config/mcp-servers' },
    { key: 'people', label: 'People and roles', icon: 'accounts', to: 'config/people' },
    { key: 'workspace', label: 'Workspace Settings', icon: 'gear', to: 'config/workspace' },
  ],
  admin: [
    { key: 'limits', label: 'Pool limits', icon: 'admin', to: 'admin/limits' },
    // SIZES-UI (part of #1689): a platform admin's alone, so `adminPagesFor`
    // never hands it to someone who administers only a group workspace.
    { key: 'runner-sizes', label: 'Runner sizes', icon: 'runtimes', to: 'admin/runner-sizes' },
    { key: 'tenants', label: 'Tenants', icon: 'tenants', to: 'admin/tenants' },
    { key: 'counts', label: 'Platform counts', icon: 'counts', to: 'admin/counts' },
    // SSC-UI (part of #2158; docs/design/secrets-scopes.md §5, proposal E S7):
    // status only, a platform admin's alone, so under Platform and never in
    // a workspace admin's `adminPagesFor`.
    { key: 'platform-secrets', label: 'Platform secrets', icon: 'key', to: 'admin/platform-secrets' },
    // DAYONE-SETUP-UI-3 (part of #2688; day-one-plan §5.2): Setup and its
    // Health page, a platform admin's alone, so never in a workspace admin's
    // `adminPagesFor` and under Platform in the panel.
    { key: 'setup', label: 'Setup', icon: 'admin', to: 'admin/setup' },
    { key: 'health', label: 'Health', icon: 'admin', to: 'admin/health' },
    // #847 W8 (docs/workspaces.md §6.4): a SECTIONS tab and a route, and
    // missing here, so an admin reached it only by typing /admin/people --
    // the Runs defect visual QA Q7 caught, again. SECTIONS' label, People
    // and roles, since ADMIN-PEOPLE-ROLES (part of #2157).
    { key: 'people', label: 'People and roles', icon: 'people', to: 'admin/people' },
    // NAV-ADMIN-ROLE (part of #1633): the group workspaces the person
    // administers, last. The only Admin page a workspace admin who is not a
    // platform admin is shown (`adminPagesFor`).
    { key: WORKSPACES_YOU_ADMIN_TAB, label: 'Workspaces you admin', icon: 'tenants', to: `admin/${WORKSPACES_YOU_ADMIN_TAB}` },
    // No Schedules here since SCHED-ONE-PAGE (owner decision 2026-10-13):
    // every tenant's schedules are Automate › Schedules, All tenants.
  ],
}

/**
 * Submit's pages (CH-17): the chooser and its three forms, by the route tab
 * each has (`''` is the chooser at /submit). Their labels are the forms'
 * own headings on the chooser.
 */
const SUBMIT_PAGES: readonly { tab: string; label: string; icon: string; to: string }[] = [
  { tab: '', label: 'Choose what to submit', icon: 'submit', to: 'submit' },
  { tab: 'new', label: 'An agent', icon: 'work', to: 'work/new' },
  { tab: 'new-workflow', label: 'A workflow', icon: 'workflows', to: 'work/new-workflow' },
  { tab: 'new-issue', label: 'From a GitHub issue', icon: 'runs', to: 'work/new-issue' },
]

/**
 * THE GROUP WORKSPACES A PERSON ADMINISTERS (NAV-ADMIN-ROLE; part of #1633):
 * the `/v1/tenants/mine` entries whose stored role is `admin` and whose kind
 * is `group`. A personal workspace never counts -- everyone administers their
 * own, and its settings are under Profile -- and neither does a platform
 * admin's reach entry (`isReach`): that is a workspace they can reach, not
 * one they were made admin of.
 */
export function adminWorkspaces(choices: readonly TenantChoice[]): TenantChoice[] {
  return choices.filter((c) => c.kind === 'group' && roleOf(c) === 'admin' && !isReach(c))
}

/**
 * The Admin pages a person is shown: all of them for a platform admin, People
 * and roles and Workspaces you admin for a workspace admin who is not one, and
 * none for anyone else -- which is how the spine knows to draw no Admin item.
 *
 * THIS IS ADMIN-REDIRECT'S LIST: an Admin page that is not here refuses a
 * workspace admin in place, with a link to Workspaces you admin (admin2-01). People and roles joined it with
 * ADMIN-PEOPLE-ROLES (part of #2157, BGD "Both, by scope"): its read
 * (`GET /v1/admin/people`) answers a workspace admin with only the workspaces
 * they administer, so /admin/people is a page of theirs, not one to send them
 * away from.
 */
export const WORKSPACE_ADMIN_PAGE_KEYS: ReadonlySet<string> = new Set(['people', WORKSPACES_YOU_ADMIN_TAB])

export function adminPagesFor(platformAdmin: boolean, workspaceAdmin: boolean): readonly PanelPage[] {
  if (platformAdmin) return PANEL_PAGES.admin
  return workspaceAdmin ? PANEL_PAGES.admin.filter((p) => WORKSPACE_ADMIN_PAGE_KEYS.has(p.key)) : []
}

/**
 * Where Admin lands: a platform admin's first page, and a workspace admin's
 * Workspaces you admin -- the page ADMIN-REDIRECT sends them to, and the
 * spine's Admin item opens, as before People and roles was theirs too.
 */
export function adminLanding(platformAdmin: boolean, pages: readonly PanelPage[]): PanelPage | null {
  return (platformAdmin ? undefined : pages.find((p) => p.key === WORKSPACES_YOU_ADMIN_TAB)) ?? pages[0] ?? null
}

/**
 * THE ADMIN PANEL IN GROUPS (CONFIG-PANEL-E; part of #2157, proposal E S1b,
 * S1c). First the workspaces the person administers -- People and
 * Workspaces you admin -- then, for a platform admin only, the platform's own
 * pages. A workspace admin is drawn only the first group: no platform page,
 * no lock, no disabled row. The group a page falls in is its key's, so a page
 * added to PANEL_PAGES.admin lands under Platform unless it is named here.
 */
export const ADMIN_WORKSPACE_KEYS: ReadonlySet<string> = new Set(['people', WORKSPACES_YOU_ADMIN_TAB])

export interface AdminGroup {
  key: 'workspaces' | 'platform'
  title: string
  rows: readonly PanelPage[]
}

/** `pages` (`adminPagesFor`'s) in their groups; a group with no page is not drawn. */
export function adminGroups(pages: readonly PanelPage[]): AdminGroup[] {
  const platform = pages.filter((p) => !ADMIN_WORKSPACE_KEYS.has(p.key))
  const groups: AdminGroup[] = [
    // A platform admin's first group holds every person too (Admin › People), so its heading says both.
    { key: 'workspaces', title: platform.length > 0 ? 'People and workspaces' : 'Your workspaces', rows: pages.filter((p) => ADMIN_WORKSPACE_KEYS.has(p.key)) },
    { key: 'platform', title: 'Platform', rows: platform },
  ]
  return groups.filter((g) => g.rows.length > 0)
}

/**
 * "IN AUTOMATE": LINKS, NOT ADMIN PAGES (BGD, 2026-10-23T23:57Z, answer 1).
 * Every workspace's schedules and approvals stay in Automate; Admin only links
 * there (S1b, S1c). A platform admin's Schedules link opens the All
 * workspaces scope (Schedules.tsx ALL_TENANTS_SCHEDULES; the address
 * /admin/schedules has redirected to it since SCHED-ONE-PAGE).
 */
export function inAutomateLinks(platformAdmin: boolean): readonly PanelPage[] {
  return [
    { key: 'schedules', label: 'Schedules', icon: 'schedules', to: platformAdmin ? 'automate/schedules?scope=all' : 'automate/schedules' },
    { key: 'approvals', label: 'Approvals', icon: 'approvals', to: 'automate/approvals' },
  ]
}

/**
 * Config's pages for the workspace in hand: all six in a group workspace, and
 * no People and roles in the person's own, which has nobody else in it (S1e).
 * `personal` null is unread: all six, rather than a page that comes and goes.
 */
export function configPagesFor(personal: boolean | null): readonly PanelPage[] {
  return personal === true ? PANEL_PAGES.config.filter((p) => p.key !== 'people') : PANEL_PAGES.config
}

/** Each panel's heading: its spine section's label. */
export const PANEL_TITLE: Readonly<Record<PanelSection, string>> = { profile: 'Profile', work: 'Work', automate: 'Automate', capacity: 'Capacity', config: 'Config', admin: 'Admin' }

/** Which panel row a route's tab belongs to: a child's tab belongs to its parent. */
function rowFor(section: PanelSection, tab: string): string {
  if (section === 'work' && (tab === 'running' || tab === '')) return 'agents'
  if (section === 'capacity' && tab === 'profiles') return 'pools'
  if (section === 'capacity' && tab === 'quota') return 'accounts'
  if (section === 'capacity' && tab === 'account-health') return 'accounts'
  // The one-time key link (paths.ts KEY_LINK_ADDRESS) opens API keys.
  if (section === 'profile' && tab.startsWith('keys/')) return 'keys'
  return tab
}

/**
 * THE PAGE THE PANEL LIGHTS IS THE PAGE ITSELF (#503): on /agents/live it is
 * Live, not Agents. A row with children is a group, and a group is never the
 * page -- one of its children is. `agentTab` is the Agents list's own tab,
 * which the route's tab (`running`) does not carry.
 */
export function litPage(section: PanelSection, tab: string, agentTab: AgentTab): { row: string; kid: string | null } {
  const row = rowFor(section, tab)
  const page = PANEL_PAGES[section].find((p) => p.key === row)
  if (page?.kids === undefined) return { row, kid: null }
  if (section === 'work' && row === 'agents') return { row, kid: agentTab }
  return { row, kid: tab === '' ? page.kids[0]!.key : tab }
}

/** The path an address is written as, for a link's `href`. */
export function hrefOf(to: string): string {
  return addressToPath(to)
}

/**
 * The Overview page's regions, as jump links (overview.html O1), in the order
 * the page draws them. Cost so far and Pools were on the page and not here
 * (G1-14, QA pass 2026-10-07); `qa.g1.jumps.test.tsx` reads Overview's
 * sections and holds this list to them.
 */
export const OVERVIEW_JUMPS: readonly { id: string; label: string }[] = [
  { id: 'ov-needs', label: 'Needs a look' },
  { id: 'ov-band', label: 'Waiting, working, done' },
  { id: 'ov-running', label: 'Running now' },
  { id: 'ov-waiting', label: 'Waiting, and why' },
  { id: 'ov-spend', label: 'Recent token cost' },
  { id: 'ov-headroom', label: 'Headroom' },
  // A full-width row under the two columns since visual QA VQ-006 (2026-10-26).
  { id: 'ov-failures', label: 'Recent failures' },
  { id: 'ov-pools', label: 'Pools' },
]

/**
 * Jump to one of Overview's regions, and put it on the address (G1-14).
 *
 * The link prevents its own navigation -- the router would treat
 * `/overview#…` as a route change -- so the hash was never written and a
 * section could not be linked. `replaceState`, not a push: a jump is a place
 * on the page, and Back should leave the page rather than walk its sections.
 * A hidden tab does not animate a smooth scroll (measured by the QA pass: the
 * section stayed put), so it jumps there instead.
 */
function jumpTo(id: string): void {
  window.history.replaceState(window.history.state, '', `#${id}`)
  const behavior = document.visibilityState === 'hidden' ? 'auto' : 'smooth'
  document.getElementById(id)?.scrollIntoView?.({ block: 'start', behavior })
}

// ---------------------------------------------------------------------------
// Per-browser memory, every access inside try/catch: storage throws in a
// private window, and losing the shell to a preference is no trade.
// ---------------------------------------------------------------------------

export function readPref(key: string): string | null {
  try {
    return globalThis.localStorage?.getItem(key) ?? null
  } catch {
    return null
  }
}

export function writePref(key: string, value: string | null): void {
  try {
    if (value === null) globalThis.localStorage?.removeItem(key)
    else globalThis.localStorage?.setItem(key, value)
  } catch {
    /* a refused write keeps the in-memory value; nothing else to do */
  }
}

const COLLAPSED_KEY = 'swarm.shell.collapsed'

/**
 * THE TABLET BAND STARTS COLLAPSED (live QA CH-1, 2026-10-11). Between the
 * phone layout (below 760px) and about 1100px the open panel left the page
 * NARROWER than the phone layout does: 500px of content at 820, 440 at 760,
 * against 720 at 720 -- and tables only stack at the phone breakpoint, so
 * Tenants, Workflows, Runs and Pools scrolled sideways inside their cards.
 * In this band the shell is the spine and its flyouts on every load, and
 * `Expand the panel` opens the panel for this visit only: the stored choice
 * (`COLLAPSED_KEY`) is the desktop's, and a tablet's expand does not change it.
 */
export const TABLET_QUERY = '(min-width: 760px) and (max-width: 1099px)'

function subscribeTablet(fn: () => void): () => void {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return () => {}
  const query = window.matchMedia(TABLET_QUERY)
  query.addEventListener?.('change', fn)
  return () => query.removeEventListener?.('change', fn)
}

/** False where there is no `matchMedia` (jsdom), which is the desktop's answer. */
function tabletNow(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia(TABLET_QUERY).matches
}

/** The phone band, where the spine becomes the 44px phone header (the same width as Notifications.tsx's). */
const PHONE_QUERY = '(max-width: 759px)'

function subscribePhone(fn: () => void): () => void {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return () => {}
  const query = window.matchMedia(PHONE_QUERY)
  query.addEventListener?.('change', fn)
  return () => query.removeEventListener?.('change', fn)
}

function phoneNow(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia(PHONE_QUERY).matches
}

/** The page scroller's id: the skip link's target (CH-15). */
const CONTENT_ID = 'swarm-page'

/** The address a scroll position is kept under: the path and query, not the in-page hash. */
function addressNow(): string {
  return typeof window === 'undefined' ? '' : window.location.pathname + window.location.search
}

/**
 * The page's own heading, else the scroller itself: where focus goes when the
 * page changes, the skip link is used or `Top` is pressed (CH-5, CH-13,
 * CH-15). A heading is made focusable without entering the tab order.
 */
function focusPage(scroller: HTMLElement | null): void {
  if (scroller === null) return
  const h = scroller.querySelector<HTMLElement>('h1')
  if (h !== null && !h.hasAttribute('tabindex')) h.setAttribute('tabindex', '-1')
  ;(h ?? scroller).focus({ preventScroll: true })
}

/** Whether focus sits in the shell's own chrome (spine, panel, flyout, phone bar, tenant list) or nowhere. */
function focusInChrome(): boolean {
  const a = document.activeElement
  if (a === null || a === document.body) return true
  return a.closest('.sk-side, .sk-flyout, .sk-pbar, .sk-tpop, .sk-skip') !== null
}

/**
 * THE TENANT NAMES `/v1/tenants/me` HAS GIVEN IN THIS BROWSER, by tenant id
 * (QA G1-06, 2026-10-07). `/v1/tenants/mine` named each tenant by its
 * registered group's email, so the shell called one tenant "Engineering" in
 * the panel and "eng@example.com" in the list under it. The list draws the
 * name `me` gave where one is known: the current tenant's always, another's
 * once it has been acted as here. Since MINE-NAMES (part of #2157) `/mine`
 * serves a group workspace's stored name itself, marked by `group`, and that
 * wins over this store, which could be a rename behind (`nameTenants`); the
 * store is the fallback for an entry `/mine` could not name.
 */
export const TENANT_NAMES_STORE = 'swarm.tenants.names'

function knownTenantNames(): Record<string, string> {
  const raw = readPref(TENANT_NAMES_STORE)
  if (raw === null) return {}
  try {
    const v: unknown = JSON.parse(raw)
    if (typeof v !== 'object' || v === null || Array.isArray(v)) return {}
    const out: Record<string, string> = {}
    for (const [k, n] of Object.entries(v)) if (typeof n === 'string' && n !== '') out[k] = n
    return out
  } catch {
    return {}
  }
}

function learnTenantName(id: string, name: string): void {
  const known = knownTenantNames()
  if (known[id] === name) return
  writePref(TENANT_NAMES_STORE, JSON.stringify({ ...known, [id]: name }))
}

/** A tenant the list offers, named as the panel names it, with the group it was registered by. */
export interface NamedTenantChoice extends TenantChoice {
  /** The registered group's email: `/v1/tenants/mine`'s `group`, or its `display_name` when it served none. */
  group: string
  /** The person's default workspace (§4.5), as `/v1/tenants/mine` marks it. */
  is_default: boolean
}

/** What the switcher calls the caller's own personal workspace (#1200), whatever its tenant document says. */
export const OWN_WORKSPACE_LABEL = 'Your workspace'

/**
 * THE ONE NAME THE SHELL DRAWS FOR THE WORKSPACE IN FORCE, at every width
 * (visual QA 5b:QA-059 and run 6's 6:admin-015, box 6343790327, part of
 * #1680). The panel's switcher said `Your workspace` while the phone header's
 * pill said `Bogdan`, the personal tenant document's own name: one workspace,
 * two names, depending on the window. The caller's own personal workspace
 * (`kind: user` in `/v1/tenants/mine`) is `OWN_WORKSPACE_LABEL` everywhere;
 * any other is its `/v1/tenants/me` name, or its id.
 *
 * A PERSONAL WORKSPACE IN FORCE IS THE CALLER'S OWN, LISTED OR NOT (visual QA
 * pass 2, admin-shell-21; part of #2694). Someone in no group is served an
 * empty `/v1/tenants/mine`, so the test above found nothing and the member's
 * switcher and Admin refusal drew the tenant document's own name (`Bogdan`)
 * where the platform admin's drew `Your workspace`. `/v1/tenants/me` only
 * answers `kind: user` for the caller's own (a platform admin's reach is
 * group workspaces only, §4.2.2), so that kind is the own workspace too.
 */
export function workspaceName(tenant: { tenant_id: string; display_name?: string | null; kind?: string }, choices: readonly TenantChoice[]): string {
  const own = tenant.kind === 'user' || choices.some((c) => c.kind === 'user' && c.tenant_id === tenant.tenant_id)
  if (own) return OWN_WORKSPACE_LABEL
  return displayNameOf(tenant.display_name) ?? tenant.tenant_id
}

/**
 * A NAME THAT IS AN ADDRESS IS NOT A DISPLAY NAME (visual QA VQ-056, part of
 * #2391). A workspace with no stored name was served its registered group's
 * email as `display_name`, so `smoke` showed as `smoke@…` in the switcher
 * while every other place called it `smoke`. Such a workspace is named by its
 * id, as everywhere else; the email stays the list row's `registered as` title.
 */
export function displayNameOf(name: string | null | undefined): string | null {
  if (name === null || name === undefined) return null
  const n = name.trim()
  return n === '' || /^[^\s@]+@[^\s@]+$/.test(n) ? null : n
}

/**
 * THE WORKSPACE'S SHORT KEY, for the phone header (visual QA VQ-001, part of
 * #2391): a group workspace's id (`eng`), which the switcher's list prints
 * under each name; the person's own workspace keeps its one name, which a
 * personal id (`u-…`) is not.
 */
export function workspaceKey(tenant: { tenant_id: string; display_name?: string | null; kind?: string }, choices: readonly TenantChoice[]): string {
  const own = tenant.kind === 'user' || choices.some((c) => c.kind === 'user' && c.tenant_id === tenant.tenant_id)
  return own ? workspaceName(tenant, choices) : tenant.tenant_id
}

/**
 * Settings › Profile (#1505, user-profile.md §4.7): the spine's last item and
 * the signed-in name's link (NAV-FOOT, #1672). The path is written out because
 * `paths.ts` gained the address in its own lane (PROFILE-NAV).
 */
export const PROFILE_ADDRESS = 'settings/profile'
export const PROFILE_PATH = '/settings/profile'

/**
 * The one name the shell draws for a tenant: the current tenant's from `me`;
 * another's as `/v1/tenants/mine` served it when it read the stored name
 * (`group` present: BGD, 2026-10-25, a rename shows in everyone's switcher at
 * once), else as `me` last gave it in this browser, and the group email only
 * for a tenant never acted as here. The caller's own personal workspace
 * (`kind: user`) is always "Your workspace". The id is always the secondary line.
 */
export function nameTenants(choices: readonly TenantChoice[], me: Me | null): NamedTenantChoice[] {
  const known = knownTenantNames()
  return choices.map((c) => {
    const is_default = defaultChoice(c)
    if (c.kind === 'user') return { tenant_id: c.tenant_id, display_name: OWN_WORKSPACE_LABEL, group: OWN_WORKSPACE_LABEL, is_default }
    const current = me !== null && me.tenant.tenant_id === c.tenant_id ? me.tenant.display_name || null : null
    // `via` rides along: the list draws a platform admin's reach entries under
    // their own heading, and the shell its banner (`isReach`).
    const via = c.via === undefined ? {} : { via: c.via }
    const group = typeof c.group === 'string' && c.group !== '' ? c.group : null
    const served = group === null ? null : displayNameOf(c.display_name)
    const name = displayNameOf(current) ?? served ?? displayNameOf(known[c.tenant_id]) ?? displayNameOf(c.display_name) ?? c.tenant_id
    return { tenant_id: c.tenant_id, display_name: name, group: group ?? c.display_name, is_default, ...via }
  })
}

/**
 * A PLATFORM ADMIN'S REACH (docs/design/workspace-roles.md §4.2.2 step 5,
 * §4.8; owner answer Q5; lane ROLES-UI-ADMIN). `/v1/tenants/mine` lists a
 * platform admin's memberships, then each registered group workspace they
 * reach only as a platform admin, marked `via: "platform_admin"`. Only the
 * server's mark makes an entry one: a group workspace the caller is a member
 * of is never one, whatever their platform role, and anyone who is not a
 * platform admin is served no such entry, so their list is drawn as before.
 */
export function isReach(c: TenantChoice): boolean {
  return c.via === 'platform_admin'
}

/** The heading the switcher draws a platform admin's reach entries under (§4.8). */
export const REACH_HEADING = 'Other workspaces (platform admin)'
/** The Workflows panel's Recent (5) switcher, per browser. */
export const RECENT_WORKFLOWS_KEY = 'swarm.workflows.recent'

/** The event `rememberWorkflow` raises, so the panel redraws without a reload. */
export const RECENT_WORKFLOWS_EVENT = 'swarm:recent-workflows'

/**
 * One workflow in the switcher: its id, the state it was last read in (null
 * when that read derived none), and its NAME -- the spec's label
 * (`workflowLabel`), null when it has none -- so the switcher draws both
 * without a read of its own. Remembered PER BROWSER: there is no per-person
 * store in the API yet (workflows.html F, "still open").
 */
export interface RecentWorkflow {
  readonly id: string
  readonly state: TaskState | null
  readonly name: string | null
}

/** The list read's five newest workflows, which fill Recent (5) behind the ones you opened. */
export const NEWEST_WORKFLOWS_KEY = 'swarm.workflows.newest'

function isTaskState(v: unknown): v is TaskState {
  return typeof v === 'string' && Object.prototype.hasOwnProperty.call(STATE_MARK, v)
}

function readRecent(key: string): RecentWorkflow[] {
  const raw = readPref(key)
  if (raw === null) return []
  try {
    const v: unknown = JSON.parse(raw)
    if (!Array.isArray(v)) return []
    const out: RecentWorkflow[] = []
    for (const x of v) {
      // The first form of this store held bare ids; they still open, unmarked.
      if (typeof x === 'string') out.push({ id: x, state: null, name: null })
      else if (typeof x === 'object' && x !== null && typeof (x as { id?: unknown }).id === 'string') {
        const st = (x as { state?: unknown }).state
        const name = (x as { name?: unknown }).name
        out.push({
          id: (x as { id: string }).id,
          state: isTaskState(st) ? st : null,
          name: typeof name === 'string' && name !== '' ? name : null,
        })
      }
    }
    return out.slice(0, 5)
  } catch {
    return []
  }
}

/**
 * THE TENANT `me` NAMED WHILE NO TENANT WAS CHOSEN: the caller's default
 * (live QA CH-3). Learned by the shell, so the stores below can tell the
 * default tenant's list from another's.
 */
export const DEFAULT_TENANT_KEY = 'swarm.tenant.default'

/** Record the tenant the API acts as when no tenant is chosen. */
export function learnDefaultTenant(id: string): void {
  if (readPref(DEFAULT_TENANT_KEY) === id) return
  writePref(DEFAULT_TENANT_KEY, id)
  announce()
}

/**
 * RECENT IS KEPT PER TENANT (live QA CH-3, 2026-10-11). It was one list for
 * the browser, so after a switch to `smoke` the panel still offered eng's
 * five workflows, and a workflow page opened from it was headed with eng's
 * name above smoke's 404. The default tenant keeps the bare key -- the list
 * every browser already holds was written under it -- and any other tenant
 * has its own, suffixed with its id.
 */
export function recentKey(key: string): string {
  const t = chosenTenant()
  return t === null || t === readPref(DEFAULT_TENANT_KEY) ? key : `${key}:${t}`
}

/** The workflows opened in this browser, newest first: what `rememberWorkflow` keeps. */
function openedWorkflows(): RecentWorkflow[] {
  return readRecent(recentKey(RECENT_WORKFLOWS_KEY))
}

/**
 * RECENT (5) AS THE SWITCHER DRAWS IT (workflows.html C; #503): the workflows
 * opened in this browser first, then the list read's newest to fill five, so
 * the list page shows the switcher before anything was opened. Each carries
 * the freshest state and name either store holds for it.
 */
export function recentWorkflows(): RecentWorkflow[] {
  const newest = readRecent(recentKey(NEWEST_WORKFLOWS_KEY))
  const fresh = new Map(newest.map((w) => [w.id, w] as const))
  const opened = openedWorkflows().map((w) => {
    const f = fresh.get(w.id)
    return f === undefined ? w : { id: w.id, state: f.state ?? w.state, name: f.name ?? w.name }
  })
  const ids = new Set(opened.map((w) => w.id))
  return [...opened, ...newest.filter((w) => !ids.has(w.id))].slice(0, 5)
}

function announce(): void {
  try {
    globalThis.dispatchEvent?.(new Event(RECENT_WORKFLOWS_EVENT))
  } catch {
    /* no event target here; the panel reads the store on its next render */
  }
}

/**
 * THE WORKFLOWS A READ REFRESHED SINCE THIS PAGE LOADED (QA G3-04,
 * 2026-10-07). The stores above outlive the tab, so a state in them may be
 * days old: Recent drew `running` beside workflows the list read had as
 * succeeded, and beside some no read had seen at all. Only an id in here
 * carries its state mark; any other is drawn as not re-read.
 */
const readThisLoad = new Set<string>()

/** Whether a read in this page load gave this workflow's state. */
export function workflowReadThisLoad(id: string): boolean {
  return readThisLoad.has(id)
}

/** Put a workflow first in Recent (5), with the state and name it was read with. */
export function rememberWorkflow(id: string, state: TaskState | null = null, name: string | null = null): void {
  const fresh = !readThisLoad.has(id)
  readThisLoad.add(id)
  const before = openedWorkflows()
  const next = [{ id, state, name }, ...before.filter((x) => x.id !== id)].slice(0, 5)
  if (JSON.stringify(next) === JSON.stringify(before)) {
    if (fresh) announce()
    return
  }
  writePref(recentKey(RECENT_WORKFLOWS_KEY), JSON.stringify(next))
  announce()
}

/**
 * THE LIST'S WHOLE READ, given newest first. Its newest five fill Recent (5)
 * behind the opened ones, and EVERY workflow it holds refreshes the state and
 * name of an opened one with that id -- not only the newest five, which left
 * an opened workflow outside them marked with whatever it was last opened in
 * (QA G3-04). It costs no read: the list already made it.
 */
export function offerNewestWorkflows(board: readonly RecentWorkflow[]): void {
  let changed = false
  for (const w of board) {
    if (readThisLoad.has(w.id)) continue
    readThisLoad.add(w.id)
    changed = true
  }
  const read = new Map(board.map((w) => [w.id, w] as const))
  const before = openedWorkflows()
  const opened = before.map((w) => {
    const f = read.get(w.id)
    return f === undefined ? w : { id: w.id, state: f.state, name: f.name ?? w.name }
  })
  if (JSON.stringify(opened) !== JSON.stringify(before)) {
    writePref(recentKey(RECENT_WORKFLOWS_KEY), JSON.stringify(opened))
    changed = true
  }
  const next = board.slice(0, 5).map((w) => ({ id: w.id, state: w.state, name: w.name }))
  if (readPref(recentKey(NEWEST_WORKFLOWS_KEY)) !== JSON.stringify(next)) {
    writePref(recentKey(NEWEST_WORKFLOWS_KEY), JSON.stringify(next))
    changed = true
  }
  if (changed) announce()
}

/** The ids Recent asked its own read for in this page load: each is asked once. */
const askedThisLoad = new Set<string>()

/**
 * RECENT'S OWN READ, GIVEN BACK (QA1 overview-agents-21). Only the Workflows
 * list offered states, so on every other page each Recent workflow carried
 * the dashed "not re-read" ring while the Workflows pages drew the same ones
 * running or failed. Recent now reads the ids no read of this load gave
 * (`loadWorkflowsById`, one batched read) and hands the states here: each one
 * read refreshes that id in both stores, and only those ids. The newest list
 * keeps its members; that is the board read's to decide.
 */
export function refreshRecentWorkflows(got: readonly RecentWorkflow[]): void {
  if (got.length === 0) return
  const by = new Map(got.map((w) => [w.id, w] as const))
  for (const id of by.keys()) readThisLoad.add(id)
  for (const key of [RECENT_WORKFLOWS_KEY, NEWEST_WORKFLOWS_KEY]) {
    const before = readRecent(recentKey(key))
    const after = before.map((w) => {
      const f = by.get(w.id)
      return f === undefined ? w : { id: w.id, state: f.state, name: f.name ?? w.name }
    })
    if (JSON.stringify(after) !== JSON.stringify(before)) writePref(recentKey(key), JSON.stringify(after))
  }
  announce()
}

/** The name Recent holds for a workflow, or null: what a page can be titled by before its read lands. */
export function recentName(id: string): string | null {
  return recentWorkflows().find((w) => w.id === id)?.name ?? null
}

// ---------------------------------------------------------------------------
// The frame's reads: who you are, the global pool for the meter, and which
// tenants you may switch to. All are marked as the FRAME's, so the page head's
// "newest read" never shows their age beside a screen still loading (CH-2).
//
// `rereadOn` re-runs a read when it changes: the chosen tenant, so "who you are"
// and the meter are re-read under the tenant just picked.
// ---------------------------------------------------------------------------

/**
 * The kinds a frame read retries on its own, with back-off: a failure that can
 * clear without a person (a blip, a tenant-resolution retry the API asks for,
 * a rate limit). Anything else -- an expired session, a domain or a tenant
 * that is refused -- waits for a person, as `Screen` does (NEEDS_A_PERSON).
 */
const FRAME_RETRIES: ReadonlySet<string> = new Set(['unreachable', 'tenant_unresolved', 'upstream_degraded', 'server_error', 'rate_limited'])

/** The first wait before a frame read is asked again, doubling to five minutes. */
export const FRAME_RETRY_MS = 20_000
const FRAME_RETRY_MAX_MS = 5 * 60_000

function useFrameRead<T>(load: () => Promise<Result<T>>, everyMs: number | null, rereadOn = '', retry = false): [Result<T>, () => void, () => void] {
  const [r, setR] = useState<Result<T>>({ status: 'loading', since: Date.now() })
  const [nonce, setNonce] = useState(0)
  useEffect(() => {
    let live = true
    let timer: ReturnType<typeof setTimeout> | undefined
    let failures = 0
    let landed = false
    const tick = () => {
      // A REPEAT is paused in a hidden tab and picked up on the next tick; the
      // FIRST read never is (QA G1-01, 2026-10-07). A tab opened in the
      // background skipped it, so the meter and the panel counts sat at
      // "loading" for the whole session -- and a meter that has never read is
      // not a meter that read nothing.
      if (landed && typeof document !== 'undefined' && document.hidden && everyMs !== null) {
        timer = setTimeout(tick, everyMs)
        return
      }
      load().then((next) => {
        if (!live) return
        landed = true
        setR(next)
        if (next.status === 'error' && retry && FRAME_RETRIES.has(next.error.kind)) {
          failures += 1
          const after = next.error.retryAfterSeconds === undefined ? 0 : next.error.retryAfterSeconds * 1000
          timer = setTimeout(tick, Math.max(after, Math.min(FRAME_RETRY_MS * 2 ** (failures - 1), FRAME_RETRY_MAX_MS)))
          return
        }
        failures = 0
        if (everyMs !== null) timer = setTimeout(tick, everyMs)
      })
    }
    tick()
    return () => {
      live = false
      if (timer !== undefined) clearTimeout(timer)
    }
  }, [load, everyMs, rereadOn, retry, nonce])
  const again = useCallback(() => {
    setR({ status: 'loading', since: Date.now() })
    setNonce((n) => n + 1)
  }, [])
  // Read again KEEPING what is drawn until the answer lands: a rename changes
  // one name, and a shell that blanked to loading for it would flash.
  const refresh = useCallback(() => setNonce((n) => n + 1), [])
  return [r, again, refresh]
}

/*
 * THE SIGNED-IN LINE'S NAME (PROFILE-FIRSTNAME, owner 2026-10-19,
 * user-profile.md §7.1 amendment): the caller's saved FIRST name, else the
 * local part of their email as before. `/v1/tenants/me` does not carry the
 * name, so `GET /v1/profile` is read for it ONCE per session per signed-in
 * email -- not once per page, and not again when the workspace changes,
 * because the name is the person's, not the workspace's. A read that fails
 * leaves the local part, which is what the line said before names existed.
 *
 * Profile's own save calls `noteOwnName`, so a name saved there reaches the
 * panel without a second read. Memory only, like the frame's other reads.
 */
let ownName: { email: string; name: string | null } | null = null
let ownNameAskedFor: string | null = null
const ownNameListeners = new Set<() => void>()

/** Record the signed-in person's saved name (null: none saved). */
export function noteOwnName(email: string, name: string | null): void {
  const kept = typeof name === 'string' && name.trim() !== '' ? name : null
  if (ownName !== null && ownName.email === email && ownName.name === kept) return
  ownName = { email, name: kept }
  for (const fn of ownNameListeners) fn()
}

function ownNameSnapshot(): { email: string; name: string | null } | null {
  return ownName
}

function subscribeOwnName(fn: () => void): () => void {
  ownNameListeners.add(fn)
  return () => ownNameListeners.delete(fn)
}

/** Ask `/v1/profile` for `email`'s saved name, the first time only. */
function askOwnName(email: string): void {
  if (USE_FIXTURES || ownNameAskedFor === email) return
  ownNameAskedFor = email
  void read<{ identity?: { email?: unknown; display_name?: unknown } }>(route('/v1/profile'), () => false, { frame: true }).then((r) => {
    if (r.status !== 'ok' && r.status !== 'stale') return
    const id = r.data?.identity
    // The answer is about whoever the API saw; a different address is not this line's.
    if (typeof id?.email === 'string' && id.email !== email) return
    noteOwnName(email, typeof id?.display_name === 'string' ? id.display_name : null)
  })
}

/** The panel's signed-in line: the saved first name, else the email's local part. */
export function signedInLabel(email: string, saved: { email: string; name: string | null } | null): { text: string; title: string } {
  const local = email.split('@')[0] ?? email
  if (saved === null || saved.email !== email || saved.name === null) return { text: local, title: local }
  return { text: firstName(saved.name), title: saved.name }
}

const loadFrameMe = () => loadMe({ frame: true })
const loadFrameTenants = () => loadMyTenants()
const loadFrameOnboarding = () => loadOnboarding()

/**
 * WHAT SITS OVER THE PAGE AND OWNS THE KEYBOARD WHILE IT IS OPEN, for the N
 * key. `aside.adm-side` is Pool limits' side editor (AdminSettings.tsx), which
 * is not a dialog but holds a draft and a typed confirmation N must not drop.
 */
const OVER_THE_PAGE = '[role="dialog"], [aria-modal="true"], aside.adm-side'
/** N and `?`, as the shortcut table spells them (G1-21). */
const SUBMIT_SHORTCUT = shortcut('anywhere', 'n')
const HELP_SHORTCUT = shortcut('anywhere', '?')
const loadFrameCapacity = () => loadCapacity({ frame: true })
/**
 * The panel's Live and Waiting counts, polled every 30 s: the caller's tenant
 * only. Never the platform scope (#2149) -- that is twelve count()s over every
 * tenant, which Platform counts runs behind its priced button and nothing
 * re-reads on a timer (owner ruling 2026-10-07, #138).
 */
export const loadFrameStats = () => loadStats({ frame: true })
const loadFrameApprovals = () => loadApprovals({ frame: true })

/**
 * THE AUTOMATE BADGE (docs/schedules.md §6.1): how many items wait in the
 * approvals inbox, plans of issue runs included. Nothing while the read is in
 * flight or when nothing waits -- a measured zero is no badge, the way a pool
 * that is not full draws no dot. A FAILED read is a dash with its reason,
 * never a 0: "nothing waits" and "could not ask" are opposite facts.
 */
export function approvalsBadge(r: Result<{ approvals: unknown[] }>): PanelCount | null {
  if (r.status === 'error') return { n: null, why: `Approvals not read: ${errorHeading(r.error)}` }
  const d = dataOf(r)
  if (d === null || d.approvals.length === 0) return null
  const n = d.approvals.length
  return { n, alert: true, why: `${n} waiting for approval` }
}

function dataOf<T>(r: Result<T>): T | null {
  return r.status === 'ok' || r.status === 'stale' ? r.data : null
}

/**
 * The global pool for the meter, and the first pool that is full or paused.
 *
 * ANOTHER POOL IS SAID TO BE ANOTHER POOL (QA-040, part of #1680). The line
 * sits under the global meter, so `provider:anthropic:tenant:u-bogdan full`
 * under a quarter-full `5 / 20` read as the global pool being full, in a raw
 * key. It now says whether it is this pool or another, names another by its
 * label (`poolLabelAmong`, so `resource:browser` and `runner:browser` stay
 * two), and hands the raw key over as `warnPool` for the line's title.
 * `a pool`, not `another pool` (visual QA pass 2, admin2-15; part of #2694):
 * `another` named no first pool -- the meter above it is a count, not a pool
 * the reader had been told about.
 */
export function meterOf(c: Capacity | null): {
  active: number
  /** null: the global pool has no limit set (#374), which is not 0. */
  limit: number | null
  warn: string | null
  /** The full or paused pool's exact name, null when none is. */
  warnPool: string | null
  /** Another pool's label, the tail of `warn`, drawn whole (admin-007); null for none or for this pool. */
  warnLabel: string | null
} | null {
  if (c === null) return null
  const g = c.pools.find((p) => p.name === 'global')
  if (g === undefined) return null
  const hot = c.pools.find((p) => p.enabled === false || (p.effective_limit !== null && p.effective_limit > 0 && p.available !== null && p.available <= 0))
  if (hot === undefined) return { active: g.active, limit: g.effective_limit, warn: null, warnPool: null, warnLabel: null }
  const state = hot.enabled === false ? 'paused' : 'full'
  if (hot.name === 'global') return { active: g.active, limit: g.effective_limit, warn: `this pool is ${state}`, warnPool: hot.name, warnLabel: null }
  const label = poolLabelAmong(
    hot.name,
    c.pools.map((p) => p.name),
  )
  return { active: g.active, limit: g.effective_limit, warn: `a pool is ${state}: ${label}`, warnPool: hot.name, warnLabel: label }
}

/** One count beside a panel row: a measurement, or a dash with its reason. */
export interface PanelCount {
  /** null is UNKNOWN, drawn as a dash -- never a 0. */
  n: number | null
  why?: string
  /** An amber count: something on that page needs a look. */
  alert?: boolean
  /** Drawn instead of the bare number, "1 near ceiling" (S1b); `n` stays the count. */
  text?: string
}

/**
 * THE AGENTS LIST'S OWN COUNTS, while it is on screen (U10a D27, owner QA
 * 2026-10-04: the panel said Live 14 / Waiting 1 beside tabs that said Live 8
 * / Waiting 2 at the same moment). The panel read `/v1/stats` every 30s and
 * the list read its page every 5s, so the two lagged each other by up to 30s.
 * While the list is mounted and its page is the WHOLE population (fewer rows
 * than it asked for, no next page), the panel draws the list's numbers: one
 * read, one moment. A capped page is not a count of everything, so then the
 * panel keeps `/v1/stats` -- and re-reads it with each list read, so the two
 * at least come from the same refresh.
 */
export interface ListCounts {
  live: number
  waiting: number
  /** When the list's read landed. */
  at: number
  /** Whether the page held every row there is. */
  whole: boolean
}
let listCounts: ListCounts | null = null
const listWatchers = new Set<() => void>()
export function publishListCounts(next: ListCounts | null): void {
  listCounts = next
  for (const w of listWatchers) w()
}
function subscribeListCounts(w: () => void): () => void {
  listWatchers.add(w)
  return () => listWatchers.delete(w)
}
function listCountsSnapshot(): ListCounts | null {
  return listCounts
}

/**
 * OVERVIEW'S OWN COUNTS, BESIDE ITS JUMPS (visual QA VQ-007, part of #2391;
 * overview.html O1's panel: `Needs a look 3`, `Running now 12`, `Waiting,
 * and why 37`, `Recent failures 6`). The panel's "On this page" list had
 * none. The counts are the page's -- the same reads and the same sums its
 * cards draw (`OverviewCounts`, mounted by Overview.tsx) -- not a second
 * read, so the panel and the card beside it cannot disagree (U10a D27). A
 * section with no count yet (its read in flight) draws no mark; a count read
 * from part of the population is drawn `≥N`, as its card says it.
 */
let overviewCounts: Readonly<Record<string, PanelCount>> | null = null
const overviewWatchers = new Set<() => void>()
export function publishOverviewCounts(next: Readonly<Record<string, PanelCount>> | null): void {
  overviewCounts = next
  for (const w of overviewWatchers) w()
}
function subscribeOverviewCounts(w: () => void): () => void {
  overviewWatchers.add(w)
  return () => overviewWatchers.delete(w)
}
function overviewCountsSnapshot(): Readonly<Record<string, PanelCount>> | null {
  return overviewCounts
}

/** Publishes Overview's section counts for the panel while the page is mounted, by jump id. */
export function OverviewCounts({ counts }: { counts: Readonly<Record<string, PanelCount>> }) {
  const key = JSON.stringify(counts)
  const latest = useRef(counts)
  latest.current = counts
  useEffect(() => {
    publishOverviewCounts(latest.current)
  }, [key])
  useEffect(() => () => publishOverviewCounts(null), [])
  return null
}

/** CONTRACT invariant 1: the four states that hold capacity, counted from LEASED. */
const LIVE_STATES = ['LEASED', 'DISPATCHED', 'STARTING', 'RUNNING'] as const
/** Waiting, and free: QUEUED, READY and PARKED hold nothing. */
const WAITING_STATES = ['QUEUED', 'READY', 'PARKED'] as const

/**
 * THE PANEL'S COUNTS (navigation.html V2, #503 "no icons and no counts").
 *
 *   Live, Waiting  from `/v1/stats` -- one count() per state for this tenant,
 *                  re-read every 30s with the meter. Live is LEASED through
 *                  RUNNING, so it agrees with how concurrency is counted
 *                  (invariant 3), not with RUNNING alone.
 *   Workflows,     NO COUNT AT ALL (visual QA V150, 2026-10-11). No route
 *   Recent         counts a tenant's workflows (the list read pages 100 with
 *                  a rollup budget, far too heavy for the frame), and Recent
 *                  is a window of finished agents, not a population. The
 *                  Workflows row carried a dash whose reason lived only in a
 *                  tooltip, beside a Recent row that carried nothing: two
 *                  ways of not counting. A dash is for a count that WAS
 *                  asked for and could not be read (Live and Waiting, below);
 *                  a row nobody counts draws no mark, never a 0.
 *   Pools          the amber count of the pools Pools' "Needs a look" group
 *                  holds (`needsALook`, Capacity.tsx), derived from the
 *                  capacity read the meter already made; nothing when none
 *                  is. It counted only full or paused pools, so a pool
 *                  lowered to 0 -- no room, nothing can start -- was on the
 *                  page's "Needs a look · 4" and not in the badge's 2 (QA1
 *                  capacity-11). `classes` is the shared class catalogue read,
 *                  which weighs a pool too small for one task of its class.
 *
 * A failed read is a dash with the failure as its reason, never a 0.
 */
export function panelCounts(
  stats: Result<Stats>,
  cap: Result<Capacity>,
  list: ListCounts | null = null,
  classes: ResourceClasses | null = null,
): Readonly<Record<string, PanelCount>> {
  const out: Record<string, PanelCount> = {}
  if (list !== null && list.whole) {
    const why = `counted from the Agents list read at ${new Date(list.at).toLocaleTimeString()}, as its tabs are`
    out.live = { n: list.live, why }
    out.waiting = { n: list.waiting, why }
    return { ...panelCounts(stats, cap, null, classes), ...out }
  }
  const s = dataOf(stats)
  // A reply with no per-state table is not a table of zeros.
  if (s !== null && (typeof s.tasks_by_state !== 'object' || s.tasks_by_state === null)) {
    out.live = { n: null, why: 'not read: the reply carried no counts by state' }
    out.waiting = { n: null, why: 'not read: the reply carried no counts by state' }
  } else if (s === null) {
    // Still reading: nothing is drawn yet (a dash would flash on every load).
    // A failed read is a dash, with the failure as its reason.
    if (stats.status === 'error') {
      const why = `not read: ${errorHeading(stats.error)}`
      out.live = { n: null, why }
      out.waiting = { n: null, why }
    }
  } else {
    // count_tasks_by_state writes a key for every state, so a missing key is
    // a reply this client does not understand: unknown, not 0.
    const by = s.tasks_by_state
    const sum = (keys: readonly string[]): number | null => (keys.every((k) => typeof by[k] === 'number') ? keys.reduce((n, k) => n + by[k]!, 0) : null)
    const live = sum(LIVE_STATES)
    const waiting = sum(WAITING_STATES)
    out.live = live === null ? { n: null, why: 'not read: a state is missing from the counts' } : { n: live }
    out.waiting = waiting === null ? { n: null, why: 'not read: a state is missing from the counts' } : { n: waiting }
  }
  const c = dataOf(cap)
  if (c === null) {
    if (cap.status === 'error') out.pools = { n: null, why: `not read: ${errorHeading(cap.error)}` }
  } else {
    const hot = c.pools.filter((p) => needsALook(p, classes)).length
    if (hot > 0) out.pools = { n: hot, alert: true, why: `${hot} pool${hot === 1 ? ' needs' : 's need'} a look: full, lowered, paused or near the ceiling` }
  }
  return out
}

/** A count the feed serves as a number, or null when it is absent (an older API) or not one. */
function feedCount(feed: NotificationFeed | null, key: 'admin_pending' | 'tenants_waiting' | 'platform_secrets_unset'): number | null {
  const v = feed?.[key]
  return typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : null
}

/**
 * A WORKSPACE ADMIN'S PEOPLE AND ROLES BADGE (S1c, visual QA VQ-051): the
 * open requests to join a workspace they administer, as the feed serves them
 * -- one `admin:join:<ref>` item each, derived for exactly the workspaces
 * whose requests they may decide (notifications.py
 * `join_requests_to_decide`). `admin_pending` is 0 for them by design, so the
 * items are the only count the API serves. Null for no feed. A muted Admin
 * topic leaves the items out of the feed, and so out of this count.
 */
export function joinsToDecide(feed: NotificationFeed | null): number | null {
  if (feed === null || !Array.isArray(feed.items)) return null
  return feed.items.filter((i) => i.group === 'admin' && i.id.startsWith('admin:join:')).length
}

/**
 * How many pools are near their ceiling: Pool limits' own rule (AdminSettings
 * `poolLead`, `NEAR_CEILING`). A paused pool, one with no ceiling set, and a
 * ceiling of 0 with nothing in use are not ranked there and not counted here.
 */
export function nearCeilingCount(pools: Capacity['pools']): number {
  return pools.filter((p) => {
    if (p.enabled === false || p.effective_limit === null) return false
    if (p.effective_limit <= 0) return p.active > 0
    return p.active / p.effective_limit >= NEAR_CEILING
  }).length
}

/**
 * How many of those are FULL: at or past their ceiling (visual QA pass 2,
 * admin2-15; part of #2694). One pool at 5 of 5 was `1 near ceiling` on the
 * panel's badge, `full` under the meter and `at 100%` in the banner; the
 * badge now says `full` for it, and `near ceiling` only for a pool under it.
 */
export function fullCount(pools: Capacity['pools']): number {
  return pools.filter((p) => {
    if (p.enabled === false || p.effective_limit === null) return false
    if (p.effective_limit <= 0) return p.active > 0
    return p.active >= p.effective_limit
  }).length
}

/**
 * THE ADMIN PANEL'S BADGES (CONFIG-ADMIN-POLISH; part of #2157, proposal E
 * S1b, S1c), each from a read the shell already makes -- never a poll of
 * the page's own:
 *
 *   People and roles      the feed's `admin_pending`, the requests waiting
 *                         on a platform admin (amber)
 *   Workspaces you admin  how many group workspaces `/v1/tenants/mine`
 *                         names the person admin of (dim: a count, not a task)
 *   Pool limits           "N near ceiling", from the capacity read the
 *                         meter makes, by Pool limits' own rule
 *   Tenants               "N waiting", the feed's `tenants_waiting`
 *   Platform secrets      "N", the feed's `platform_secrets_unset`
 *
 * A COUNT THAT WAS NOT READ DRAWS NO BADGE, NEVER 0: a read in flight or
 * failed, or a feed from an API that does not serve the count (null or
 * absent). A measured zero of something waiting is no badge either, the way
 * Approvals' is (`approvalsBadge`). A WORKSPACE ADMIN WHO IS NOT A PLATFORM
 * ADMIN IS COUNTED ONLY WHAT IS THEIRS (S1c): `admin_pending` is the
 * platform's requests, so it is no badge of theirs; their People and roles
 * counts the join requests the feed serves them (`joinsToDecide`), and the
 * platform pages are not in their panel at all.
 */
export function adminPanelCounts(
  platformAdmin: boolean,
  feed: NotificationFeed | null,
  cap: Result<Capacity>,
  mine: Result<TenantChoice[]>,
): Readonly<Record<string, PanelCount>> {
  const out: Record<string, PanelCount> = {}
  const choices = dataOf(mine) ?? (mine.status === 'empty' ? [] : null)
  if (choices !== null) {
    const n = adminWorkspaces(choices).length
    out[WORKSPACES_YOU_ADMIN_TAB] = { n, why: `${n} group workspace${n === 1 ? '' : 's'} you administer` }
  }
  if (!platformAdmin) {
    const joins = joinsToDecide(feed)
    if (joins !== null && joins > 0) out.people = { n: joins, alert: true, why: `${joins} request${joins === 1 ? '' : 's'} to join a workspace you administer` }
    return out
  }
  const pending = feedCount(feed, 'admin_pending')
  if (pending !== null && pending > 0) out.people = { n: pending, alert: true, why: `${pending} request${pending === 1 ? '' : 's'} waiting on an admin` }
  const c = dataOf(cap)
  const near = c === null ? 0 : nearCeilingCount(c.pools)
  const full = c === null ? 0 : fullCount(c.pools)
  const under = near - full
  // Full first: it is the state that blocks a lease; the title counts both.
  const why = [
    full > 0 ? `${full} pool${full === 1 ? ' is' : 's are'} full` : null,
    under > 0 ? `${under} pool${under === 1 ? ' is' : 's are'} at ${Math.round(NEAR_CEILING * 100)}% of the ceiling or more` : null,
  ].filter((w) => w !== null).join('; ')
  if (near > 0) out.limits = { n: near, alert: true, text: full > 0 ? `${full} full` : `${under} near ceiling`, why }
  const waiting = feedCount(feed, 'tenants_waiting')
  if (waiting !== null && waiting > 0) out.tenants = { n: waiting, alert: true, text: `${waiting} waiting`, why: `${waiting} workspace${waiting === 1 ? '' : 's'} waiting to be created` }
  const unset = feedCount(feed, 'platform_secrets_unset')
  if (unset !== null && unset > 0) out['platform-secrets'] = { n: unset, alert: true, why: `${unset} platform secret${unset === 1 ? ' has' : 's have'} no enabled version` }
  return out
}

/**
 * THE PANEL FOOT'S ROLE LINE (S1a): who the person is here, in place of the
 * email (which stays in its title). "Platform admin", else "Workspace admin"
 * when `/v1/tenants/mine` names them admin of a group workspace, else
 * "Viewer" when the group workspace in hand holds them as one -- "Member"
 * there would claim a wider role than the server stores -- "Workspace admin"
 * again when the workspace in hand is the person's own personal one -- else
 * "Member". Null until both reads have answered: an unread list is not "not an admin".
 */
export function roleLine(platformAdmin: boolean, mine: Result<TenantChoice[]>, tenantId: string | null, personal = false): string | null {
  if (platformAdmin) return 'Platform admin'
  const choices = dataOf(mine) ?? (mine.status === 'empty' ? [] : null)
  if (choices === null) return null
  if (adminWorkspaces(choices).length > 0) return 'Workspace admin'
  const here = choices.find((c) => c.tenant_id === tenantId)
  // THE OWNER OF A PERSONAL WORKSPACE IS ITS ADMIN (QA1 config-28): the foot
  // said "Member" while People and roles said "You are this workspace's
  // admin" and This workspace "Your role: admin". `personal` is `me`'s tenant
  // kind, as the People page reads it: `/v1/tenants/mine` answers empty for
  // someone in no group, so `here` alone missed the finding's own case.
  if (personal || (here !== undefined && here.kind === 'user')) return 'Workspace admin'
  return here !== undefined && here.kind === 'group' && roleOf(here) === 'viewer' ? 'Viewer' : 'Member'
}

/**
 * The role an Admin page's refusal names (admin-shell-23, QA1B-04 review): as
 * `roleLine` reads it, the owner of a personal workspace is its admin -- its
 * `/v1/tenants/mine` entry may carry no stored role, and the refusal said the
 * role "was not read" beside a foot reading "Workspace admin". A group gives
 * its stored role; no entry is null, never a role guessed.
 */
export function refusedRole(here: TenantChoice | undefined, personal: boolean): string | null {
  if (personal || (here !== undefined && here.kind === 'user')) return 'admin'
  return here === undefined ? null : roleOf(here)
}

function CountMark({ c }: { c: PanelCount | undefined }) {
  if (c === undefined) return null
  if (c.n === null) {
    return (
      <span className="sk-cnt is-dash" title={c.why} aria-label={c.why}>
        &mdash;
      </span>
    )
  }
  return (
    <span className={`sk-cnt${c.alert === true ? ' is-alert' : ''}${c.text !== undefined ? ' is-chip' : ''}`} title={c.why}>
      {c.text ?? c.n}
    </span>
  )
}

// ---------------------------------------------------------------------------
// Icons (24px stroke, common.py ICONS)
// ---------------------------------------------------------------------------

const ICONS: Readonly<Record<string, ReactNode>> = {
  overview: (
    <>
      <rect x="3.5" y="3.5" width="7" height="7" rx="1.5" />
      <rect x="13.5" y="3.5" width="7" height="4.5" rx="1.5" />
      <rect x="13.5" y="11" width="7" height="9.5" rx="1.5" />
      <rect x="3.5" y="13.5" width="7" height="7" rx="1.5" />
    </>
  ),
  work: (
    <>
      <rect x="5" y="7.5" width="14" height="11" rx="3" />
      <path d="M12 4v3.5M9 12.5v1M15 12.5v1M2.5 12v3M21.5 12v3" />
    </>
  ),
  capacity: (
    <>
      <path d="M12 3.5 3.5 8 12 12.5 20.5 8Z" />
      <path d="m3.5 12 8.5 4.5 8.5-4.5" />
      <path d="m3.5 16 8.5 4.5 8.5-4.5" />
    </>
  ),
  // CONFIG-NAV (S1): Config takes the sliders, and Admin a shield.
  config: (
    <>
      <path d="M4 7h10M18 7h2M4 17h4M12 17h8" />
      <circle cx="16" cy="7" r="2" />
      <circle cx="10" cy="17" r="2" />
    </>
  ),
  admin: (
    <>
      <path d="M12 3.5 5 6.2v5.3c0 4.3 2.9 7.6 7 9 4.1-1.4 7-4.7 7-9V6.2z" />
      <path d="m9.2 12 2 2 3.8-3.8" />
    </>
  ),
  github: (
    <>
      <path d="M9 19.5c-4 1.2-4-2-5.6-2.4M14.6 21v-3.1a2.7 2.7 0 0 0-.8-2.1c2.6-.3 5.3-1.3 5.3-5.8a4.5 4.5 0 0 0-1.2-3.1 4.2 4.2 0 0 0-.1-3.1s-1-.3-3.2 1.2a11 11 0 0 0-5.8 0C6.6 3.5 5.6 3.8 5.6 3.8a4.2 4.2 0 0 0-.1 3.1 4.5 4.5 0 0 0-1.2 3.1c0 4.5 2.7 5.5 5.3 5.8a2.7 2.7 0 0 0-.8 2.1V21" />
    </>
  ),
  gear: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21M5.6 5.6l1.8 1.8M16.6 16.6l1.8 1.8M5.6 18.4l1.8-1.8M16.6 7.4l1.8-1.8" />
    </>
  ),
  help: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M9.6 9.5a2.5 2.5 0 0 1 4.8 1c0 1.7-2.4 2-2.4 3.5" />
      <path d="M12 17.2v.1" />
    </>
  ),
  api: <path d="M3.5 12h3l2.5-6 4 12 2.5-6h5" />,
  submit: <path d="M12 5v14M5 12h14" />,
  lock: (
    <>
      <rect x="5" y="10.5" width="14" height="9.5" rx="2" />
      <path d="M8 10.5V8a4 4 0 0 1 8 0v2.5" />
    </>
  ),
  collapse: (
    <>
      <rect x="3.5" y="4.5" width="17" height="15" rx="2.5" />
      <path d="M9.5 4.5v15M15.5 10l-2 2 2 2" />
    </>
  ),
  expand: (
    <>
      <rect x="3.5" y="4.5" width="17" height="15" rx="2.5" />
      <path d="M9.5 4.5v15M13.5 10l2 2-2 2" />
    </>
  ),
  menu: <path d="M4 7h16M4 12h16M4 17h16" />,
  // The panel's page icons (navigation.html V2's symbol sheet).
  workflows: (
    <>
      <circle cx="6" cy="6" r="2.2" />
      <circle cx="6" cy="18" r="2.2" />
      <circle cx="18" cy="12" r="2.2" />
      <path d="M8.2 6h3a3 3 0 0 1 3 3v.8M8.2 18h3a3 3 0 0 0 3-3v-.8" />
    </>
  ),
  runs: (
    <>
      <circle cx="6" cy="6" r="1.8" />
      <circle cx="6" cy="12" r="1.8" />
      <circle cx="6" cy="18" r="1.8" />
      <path d="M10 6h9M10 12h9M10 18h9" />
    </>
  ),
  timeline: (
    <>
      <path d="M4 19.5h16" />
      <path d="M6.5 16v-4M10.5 16V8M14.5 16v-6M18.5 16V5" />
    </>
  ),
  // repositories.html's i-repo, as drawn.
  repo: (
    <>
      <path d="M5.5 4.5h11a2 2 0 0 1 2 2v13h-11a2 2 0 0 1-2-2Z" />
      <path d="M5.5 17.5a2 2 0 0 1 2-2h11M9 8.5h6" />
    </>
  ),
  // #780 OB8: a checklist for Setup, a key for Access.
  setup: (
    <>
      <path d="M4 6.5l1.6 1.6L8.5 5M4 12.5l1.6 1.6 2.9-3.1M4 18.5l1.6 1.6 2.9-3.1" />
      <path d="M11.5 7h8.5M11.5 13h8.5M11.5 19h8.5" />
    </>
  ),
  access: (
    <>
      <circle cx="8" cy="12" r="3.5" />
      <path d="M11.5 12H20M17 12v3M20 12v2.5" />
    </>
  ),
  runtimes: (
    <>
      <rect x="4" y="4" width="16" height="16" rx="2.5" />
      <path d="M9 4v16M4 9h5M4 14h5" />
    </>
  ),
  holders: (
    <>
      <rect x="5" y="10.5" width="14" height="9.5" rx="2" />
      <path d="M8 10.5V8a4 4 0 0 1 8 0v2.5" />
    </>
  ),
  accounts: (
    <>
      <circle cx="9" cy="8.5" r="3.2" />
      <path d="M3.5 19.5a5.5 5.5 0 0 1 11 0" />
      <path d="M16 5.5a3 3 0 0 1 0 6M18.5 19.5a5 5 0 0 0-2.5-4.3" />
    </>
  ),
  tenants: (
    <>
      <path d="M4 20V6.5L12 3.5l8 3V20" />
      <path d="M9 20v-4.5h6V20M8.5 9.5h1M14.5 9.5h1M8.5 12.5h1M14.5 12.5h1" />
    </>
  ),
  counts: <path d="M9 4 7 20M17 4l-2 16M4.5 9h16M3.5 15h16" />,
  // docs/schedules.md §6.1: a loop for the section, a clock for its Schedules page.
  automate: (
    <>
      <path d="M19.5 12a7.5 7.5 0 0 1-13.2 4.9M4.5 12a7.5 7.5 0 0 1 13.2-4.9" />
      <path d="M17.7 3.5v3.6h-3.6M6.3 20.5v-3.6h3.6" />
    </>
  ),
  schedules: (
    <>
      <circle cx="12" cy="12" r="8" />
      <path d="M12 7.5V12l3 2" />
    </>
  ),
  // §6.1: a tray with a tick, for the inbox of things waiting on a person.
  approvals: (
    <>
      <path d="M4 13.5h4.5l1.5 2.5h4l1.5-2.5H20" />
      <path d="M4 13.5 6.5 5h11l2.5 8.5V19H4z" />
      <path d="m9.5 9.5 2 2 3.5-3.5" />
    </>
  ),
  people: (
    <>
      <circle cx="12" cy="7.5" r="3.5" />
      <path d="M5 20a7 7 0 0 1 14 0" />
    </>
  ),
  swap: <path d="m7 9 5-5 5 5M7 15l5 5 5-5" />,
  // NAV-PROFILE: API keys and MCP servers, in the Profile panel.
  key: (
    <>
      <circle cx="8" cy="15" r="4" />
      <path d="m11 12 8.5-8.5M16.5 6.5l2.5 2.5M14 9l2 2" />
    </>
  ),
  plug: (
    <>
      <path d="M9 3.5V8M15 3.5V8M6.5 8h11v3a5.5 5.5 0 0 1-11 0z" />
      <path d="M12 16.5v4" />
    </>
  ),
  // #1206: the header bar's bell, and the tick its empty state and a finished update draw.
  bell: (
    <>
      <path d="M6 16.5V11a6 6 0 0 1 12 0v5.5l1.5 2h-15z" />
      <path d="M10 20.5a2 2 0 0 0 4 0" />
    </>
  ),
  check: <path d="m5 12.5 4.5 4.5L19 7.5" />,
  search: (
    <>
      <circle cx="11" cy="11" r="6.5" />
      <path d="m20 20-4.2-4.2" />
    </>
  ),
}

export function Icon({ name, className = 'sk-ic' }: { name: string; className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      {ICONS[name]}
    </svg>
  )
}

// ---------------------------------------------------------------------------
// The shell
// ---------------------------------------------------------------------------

/**
 * PROFILE, THE SPINE'S LAST ITEM (NAV-FOOT, #1672, owner 2026-10-20): at the
 * foot, below Help, drawn as the person's avatar. NAV-PROFILE (#1633) had put
 * it under the Hive, above Submit; the owner moved it back to where people
 * look for themselves, and kept its panel of five settings pages. It is the
 * one place the person is drawn on the spine.
 */
const PROFILE_ITEM: { key: 'profile'; label: string; to: string } = { key: 'profile', label: 'Profile', to: PROFILE_ADDRESS }

const SPINE: readonly { key: Exclude<SpineSection, null>; label: string; to: string }[] = [
  { key: 'overview', label: 'Overview', to: 'overview/now' },
  // WORK STILL OPENS AGENTS (QA-112, part of #1680, left as it is): the same
  // click opens the Work panel, where Workflows is the row after Agents, so
  // a workflow is one more click away and never hidden. Opening Workflows
  // instead would change where every Work link in the console lands. That
  // is an IA decision for the owner, asked in the PR rather than made here.
  // spine.qafix.test.tsx holds the panel row.
  { key: 'work', label: 'Work', to: 'work/running' },
  { key: 'automate', label: 'Automate', to: 'automate/schedules' },
  { key: 'capacity', label: 'Capacity', to: 'capacity/pools' },
  // CONFIG-NAV (part of #2157, S1): between Capacity and Admin.
  { key: 'config', label: 'Config', to: 'config/github' },
  { key: 'admin', label: 'Admin', to: 'admin/limits' },
]

export interface ShellProps {
  /** The spine section the route belongs to; null for Submit. */
  section: SpineSection
  /** The route's tab, which lights a panel row. */
  tab: string
  /** The Agents list's own tab, which lights Live, Waiting or Recent. */
  agentTab?: AgentTab
  /** The page title, for the phone header. */
  title: string
  /**
   * The address has no page (NotFound.tsx). The spine still lights the
   * nearest section, but the panel draws none of that page's own anchors:
   * Overview's "On this page" jumps to cards a 404 does not have (QA G1-11).
   */
  missing?: boolean
  go: (to: string) => void
  /** Help's search, owned by the Help page through the route. */
  helpGroup?: string | null
  /** API reads' filter. */
  apiFailuresOnly?: boolean
  onApiFilter?: (failuresOnly: boolean) => void
  /** The spine's foot: Help, drawn by App (see `ctl-nav-util`); Profile follows it. */
  foot: ReactNode
  /**
   * THE NOTIFICATION BELL (#1206), drawn by App -- Notifications.tsx imports
   * from this file, so it is handed in rather than imported. Placed ONCE, in
   * the shell's own chrome (visual QA V178, 2026-10-16): at the spine's foot
   * from 760px up, in the phone header below it. It used to be the only thing
   * in a 48px header bar over every section page -- an empty band above the
   * title at 1440, a second sticky band under the phone header at 390.
   */
  bell?: ReactNode
  /**
   * The API-reads strip, drawn as the last row of the CONTENT column -- not a
   * full-width bar under the spine and the panel (#503: it cut the spine's
   * avatar off at 1440x900; the V2 frames have no bottom bar).
   */
  dock?: ReactNode
  /**
   * A FORM WHOSE UNSENT DRAFT SURVIVES A TENANT SWITCH (intake-tenants.html
   * 2A): Submit's task and workflow forms. The page is not remounted on a
   * switch; its reads run again under the new tenant (`Screen`), and the
   * form says which tenant it will now submit as.
   */
  keepOnSwitch?: boolean
  /**
   * The route's address. A change is a new page: focus moves to it when it was
   * in the chrome (CH-15), and Back restores the scroll it was left at (CH-5).
   */
  place?: string
  /**
   * The open page is an Admin page this person is not shown, once both reads
   * have landed and never while either is in flight: `''` (Overview) for
   * someone who administers nothing (CONFIG-NAV), and `admin/workspaces` for
   * a workspace admin on a platform page (ADMIN-REDIRECT). App rewrites the
   * bar in place.
   */
  onAdminRedirect?: (to: string) => void
  children: ReactNode
}

/** A link a plain click routes and any other click leaves to the browser (a new tab, a copy). */
function routed(to: string, onPlain: () => void): { href: string; onClick: (e: ReactMouseEvent) => void } {
  return {
    href: hrefOf(to),
    onClick: (e) => {
      if (!routedClick(e)) return
      e.preventDefault()
      onPlain()
    },
  }
}

export function SkyShell({
  section,
  tab,
  agentTab = 'live',
  title,
  go,
  helpGroup = null,
  apiFailuresOnly = false,
  onApiFilter,
  missing = false,
  foot,
  bell = null,
  dock,
  keepOnSwitch = false,
  place = '',
  onAdminRedirect,
  children,
}: ShellProps) {
  // THE CHOSEN TENANT (fetch.ts), null for the default. Every read sends it;
  // a change re-reads the frame and remounts the page below so its reads run
  // again under the new tenant rather than showing the old one's rows -- all
  // but an unsent form (`keepOnSwitch`), which is kept.
  const tenant = useSyncExternalStore(subscribeTenant, chosenTenant, chosenTenant)
  const [me, rereadMe, refreshMe] = useFrameRead(loadFrameMe, null, tenant ?? '', true)
  const [cap] = useFrameRead(loadFrameCapacity, 30_000, tenant ?? '')
  const fromList = useSyncExternalStore(subscribeListCounts, listCountsSnapshot, listCountsSnapshot)
  // A capped list re-reads the stats with each of its own reads (D27).
  const [stats] = useFrameRead(loadFrameStats, 30_000, `${tenant ?? ''}:${fromList !== null && !fromList.whole ? fromList.at : ''}`)
  const [mine, rereadMine, refreshMine] = useFrameRead(loadFrameTenants, null)
  // A WORKSPACE RENAMED on Config › Workspace Settings (WORKSPACE-ADMIN-FIELDS,
  // part of #2157): the name the switcher and every header draw is `me`'s, so
  // both reads are asked again and the new name lands without a reload.
  useEffect(() => {
    const again = () => {
      refreshMe()
      refreshMine()
    }
    globalThis.addEventListener?.(WORKSPACE_RENAMED_EVENT, again)
    return () => globalThis.removeEventListener?.(WORKSPACE_RENAMED_EVENT, again)
  }, [refreshMe, refreshMine])
  // A headerless request refused because the default is unknown (Q9): said
  // beside the switcher, where naming a workspace is the next action.
  const prefsDown = useSyncExternalStore(subscribePreferencesUnavailable, preferencesUnavailableSnapshot, preferencesUnavailableSnapshot)
  // Once a minute: the inbox route syncs the tenant's held firings as it reads.
  const [inbox] = useFrameRead(loadFrameApprovals, 60_000, tenant ?? '')
  const waiting = approvalsBadge(inbox)
  // #1206: the notification feed's admin count, from the store the header's
  // bell reads too (notificationFeed.ts), so the two never disagree.
  const notes = useSyncExternalStore(subscribeNotifications, notificationsSnapshot, notificationsSnapshot)
  const adminPending = feedData(notes)?.admin_pending ?? 0
  const who = dataOf(me)
  const admin = who?.principal.is_admin === true
  // WHO SEES ADMIN (NAV-ADMIN-ROLE; part of #1633): a platform admin, or the
  // stored admin of a group workspace. Both reads unread is no Admin at all,
  // never a locked one that then disappears.
  const adminPages = adminPagesFor(admin, adminWorkspaces(dataOf(mine) ?? []).length > 0)
  // Their names, for the note a workspace admin's Admin panel carries (S1c).
  const adminOf = useMemo(() => nameTenants(adminWorkspaces(dataOf(mine) ?? []), null).map((w) => w.display_name), [mine])
  const landing = adminLanding(admin, adminPages)
  // THE BADGE'S ITEM OPENS WHERE THE BADGE'S REQUESTS ARE (visual QA pass 2,
  // admin2-07; part of #2694): the rail's `N waiting for an admin` sat on an
  // item that opened Pool limits, and the requests are on People and roles.
  // The badge is drawn for a platform admin only (`sectionLink`), so only
  // their item moves; with nothing waiting it opens Pool limits as before.
  const pendingAt = admin && adminPending > 0 ? adminPages.find((p) => p.key === 'people') : undefined
  const adminTo = pendingAt?.to ?? landing?.to
  const spineItems = SPINE.flatMap((s) => (s.key !== 'admin' ? [s] : adminTo === undefined ? [] : [{ ...s, to: adminTo }]))
  // ADMIN'S ADDRESSES SEND A NON-ADMIN TO OVERVIEW (CONFIG-NAV), and only
  // once both reads have answered: an unread session is not "not an admin".
  const adminDecided = who !== null && (mine.status === 'ok' || mine.status === 'stale' || mine.status === 'empty')
  // A WORKSPACE ADMIN'S PLATFORM ADDRESS SENDS THEM TO WORKSPACES YOU ADMIN
  // (ADMIN-REDIRECT): an Admin page that is not among theirs. Only a page the
  // panel has: an address that is no page at all keeps its 404.
  // SOMEONE WHO ADMINISTERS NOTHING IS TOLD SO IN PLACE (visual QA pass 1,
  // admin-shell-23; part of #2596; BGD 2026-10-28, reversing CONFIG-NAV's
  // redirect for this case): Overview in its place was the link silently
  // becoming another page. The page's own address stays; the refusal names
  // the page, the role and the workspace, and links Overview.
  // A WORKSPACE ADMIN ON A PLATFORM PAGE IS REFUSED IN PLACE TOO (visual QA
  // pass 2, admin2-01; part of #2694; BGD 2026-10-28's rule for admin-shell-23,
  // "a link must not silently become another page"): ADMIN-REDIRECT swapped
  // /admin/limits for Workspaces you admin with no word said. The refusal
  // names the page and links Workspaces you admin. Only the bare /admin --
  // Admin itself, which App reads as its first page -- still lands on their
  // page, and only on the visit it was opened at (`openedAtAdmin`).
  const [openedAtAdmin, setOpenedAtAdmin] = useState(() => /^\/admin\/?$/.test(globalThis.location?.pathname ?? '') || /^#\/?admin\/?$/.test(globalThis.location?.hash ?? ''))
  const platformPage = section === 'admin' && adminDecided && landing !== null && !missing
    && PANEL_PAGES.admin.some((p) => p.to === `admin/${tab}`) && !adminPages.some((p) => p.to === `admin/${tab}`)
  const adminRefused = section === 'admin' && adminDecided && !missing && (landing === null || (platformPage && !openedAtAdmin))
  const refusedEntry = PANEL_PAGES.admin.find((p) => p.to === `admin/${tab}`)
  const refusedPage = refusedEntry?.label ?? 'Admin'
  const refusedHere = (dataOf(mine) ?? []).find((c) => c.tenant_id === who?.tenant.tenant_id)
  const adminRedirect = platformPage && openedAtAdmin && landing !== null ? landing.to : null
  useEffect(() => {
    if (adminRedirect !== null) onAdminRedirect?.(adminRedirect)
    if (adminRedirect !== null || section !== 'admin') setOpenedAtAdmin(false)
  }, [adminRedirect, onAdminRedirect, section])
  // THE WORKSPACE IN HAND IS THE PERSON'S OWN (S1e): no People and roles, and
  // a three-step strip. Read off `me`'s tenant kind, which is what the People
  // page itself reads: `/v1/tenants/mine` answers empty for someone in no
  // group, which left a personal workspace drawn as a group one (CONFIG-NAV
  // review, phone People and roles). Unread is null, the group shape.
  const personal = who === null ? null : who.tenant.kind === 'user'
  const configPages = configPagesFor(personal)
  // THE SETUP READ (proposal E): `GET /v1/onboarding`, once per workspace and
  // again on each Config page, so the strip a page draws is that visit's. The
  // spine's dot on Config and the panel's meter read the same answer.
  const [onboarding] = useFrameRead(loadFrameOnboarding, null, `${tenant ?? ''}:${section === 'config' ? place : ''}`)
  const setupRead = useMemo<SetupRead>(() => ({ doc: onboarding, personal }), [onboarding, personal])
  const setupLeft = setupUnfinished(setupSteps(dataOf(onboarding), personal === true))
  const savedName = useSyncExternalStore(subscribeOwnName, ownNameSnapshot, ownNameSnapshot)
  const whoEmail = who?.principal.email ?? null
  useEffect(() => {
    if (whoEmail !== null) askOwnName(whoEmail)
  }, [whoEmail])
  const signedIn = who === null ? null : signedInLabel(who.principal.email, savedName)
  const env = classifyEnvironment(
    import.meta.env.VITE_SWARM_ENV,
    typeof window === 'undefined' ? '' : window.location.hostname,
    who === null ? null : servedEnvironment(who),
  )
  const t = envTreatment(env)
  // SIGN OUT (owner 2026-10-14) only where there is an IAP session to end:
  // never on `npm run dev` (signoutState.ts `iapFronted`), and never before the
  // identity it would sign out has been read.
  const canSignOut = who !== null && iapFronted(typeof window === 'undefined' ? '' : window.location.hostname)
  const meter = meterOf(dataOf(cap))
  const classes = useResourceClasses()
  // The Approvals page carries the inbox count the Automate section does (§6.1).
  const counts = waiting === null ? panelCounts(stats, cap, fromList, classes) : { ...panelCounts(stats, cap, fromList, classes), approvals: waiting }
  const adminCounts = adminPanelCounts(admin, feedData(notes), cap, mine)
  const jumpCounts = useSyncExternalStore(subscribeOverviewCounts, overviewCountsSnapshot, overviewCountsSnapshot)
  const role = who === null ? null : roleLine(admin, mine, who.tenant.tenant_id, personal === true)
  const online = useOnline()
  const probes = useSyncExternalStore(subscribeProbes, probeSnapshot, probeSnapshot)
  const fault = wholeAppFault(me, probes)
  // A retry from the takeover re-reads the frame AND the page under it.
  const [attempt, setAttempt] = useState(0)

  const tablet = useSyncExternalStore(subscribeTablet, tabletNow, () => false)
  const phone = useSyncExternalStore(subscribePhone, phoneNow, () => false)
  const menuNoteId = useId()
  const [pinned, setPinned] = useState(() => readPref(COLLAPSED_KEY) === '1')
  // The tablet's expand, for this visit and this band only (CH-1).
  const [tabletOpen, setTabletOpen] = useState(false)
  useEffect(() => setTabletOpen(false), [tablet])
  const collapsed = tablet ? !tabletOpen : pinned
  const [drawer, setDrawer] = useState(false)
  const [fly, setFly] = useState<Exclude<SpineSection, null> | null>(null)
  /** Where the tenant list is open: beside the panel block, beside the spine's tile, or the phone's sheet. */
  const [picker, setPicker] = useState<TenantPickerAt | null>(null)
  const flyTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const sideRef = useRef<HTMLDivElement | null>(null)
  const flyRef = useRef<HTMLDivElement>(null)
  const openerRef = useRef<HTMLButtonElement | null>(null)
  const appRef = useRef<HTMLDivElement | null>(null)
  const jumpedTo = useRef<HTMLElement | null>(null)
  const placed = useRef(false)
  const titleNow = useRef(title)
  titleNow.current = title
  const [arrived, setArrived] = useState('')
  /** Which collapse control to focus once the panel has been swapped (CH-9). */
  const refocusToggle = useRef<'expand' | 'collapse' | null>(null)
  const scrollerRef = useRef<HTMLDivElement | null>(null)
  const [scroll, setScroll] = useState<PageScroll>('top')

  // THE PHONE HEADER GIVES BACK ITS HEIGHT ON SCROLL, AND A LONG PAGE GETS A
  // WAY BACK UP (#139). The scroller is the content column's `.ctl-scroll`:
  // the spine and the panel sit beside it, the dock under it. Read on every
  // scroll event, which is a comparison and a state write that React drops
  // when the band has not changed.
  //
  // EACH ADDRESS KEEPS THE SCROLL IT WAS LEFT AT (live QA CH-5, 2026-10-11).
  // The page scrolls in this element, not the document, so the browser's own
  // restoration never applied: scrolled to 4000, a trip to Capacity and Back
  // came back at 0. Recorded per address on every scroll, read when `popstate`
  // says the address is a history entry, and put back once the page is tall
  // enough to hold it -- its reads land after the route does.
  const scrolledAt = useRef(new Map<string, number>())
  const restoreTo = useRef<number | null>(null)
  /** The page (section and tab) the scroll belongs to, and whether the chrome's own `nav` moved it. */
  const pageNow = useRef('')
  pageNow.current = `${section}|${tab}`
  const pageAt = useRef(pageNow.current)
  const navigated = useRef(false)
  useEffect(() => {
    const scroller = scrollerRef.current
    if (scroller === null) return
    const onScroll = () => {
      setScroll(scrollBand(scroller.scrollTop, scroller.clientHeight))
      scrolledAt.current.set(addressNow(), scroller.scrollTop)
    }
    onScroll()
    const onPop = () => {
      restoreTo.current = scrolledAt.current.get(addressNow()) ?? 0
    }
    scroller.addEventListener('scroll', onScroll, { passive: true })
    window.addEventListener('popstate', onPop)
    return () => {
      scroller.removeEventListener('scroll', onScroll)
      window.removeEventListener('popstate', onPop)
    }
  }, [])
  // `Top` MOVES FOCUS AS WELL AS THE PAGE (CH-13): it unmounts itself once
  // the page is at the top, which dropped focus to <body>; and the jump's
  // hash went too, or a reload went straight back down to the section.
  const toTop = () => {
    const scroller = scrollerRef.current
    if (scroller === null) return
    scroller.scrollTop = 0
    setScroll('top')
    if (window.location.hash !== '') window.history.replaceState(window.history.state, '', addressNow())
    focusPage(scroller)
  }

  // A NEW PAGE TAKES FOCUS FROM THE CHROME (live QA CH-15, CH-5). Focus stayed
  // on the rail link that was clicked, so the next Tab walked the rest of the
  // rail, and after a load focus sat on <body>, where PageDown, Space and End
  // scroll the document -- which does not scroll; `.ctl-scroll` does. When
  // focus is in the chrome or nowhere, it moves to the page's heading (else
  // the scroller), inside the scroller, so the keys scroll the page and a
  // screen reader reads where it landed. Focus inside the page -- a tab, a
  // filter, a form -- is left where it is. On a Back or Forward the scroll
  // the address was left at is put back (CH-5).
  useEffect(() => {
    const scroller = scrollerRef.current
    if (scroller === null) return
    if (focusInChrome()) focusPage(scroller)
    // SAID, as well as focused (CH-15): a page with no heading yet takes
    // focus on its scroller, which has no name to read. Not on the first
    // load, which the browser announces by the document title.
    const first = !placed.current
    if (!first) setArrived(titleNow.current)
    placed.current = true
    const newPage = navigated.current || pageAt.current !== pageNow.current
    navigated.current = false
    pageAt.current = pageNow.current
    const want = restoreTo.current
    restoreTo.current = null
    if (want === null) {
      // A NEW PAGE OPENS AT ITS TOP (visual QA VQ-053, part of #2391). The
      // page scrolls in `.ctl-scroll`, which outlives the route, so a page
      // opened from the panel kept the last page's offset: scrolled down
      // Admin › Tenants, Platform counts opened halfway down with its head
      // and Run buttons off-screen. A new page -- another section or tab, or
      // any address the chrome itself went to -- starts at 0. An agent or a
      // run opened from its list is the same page (the list stays where it
      // was beside it), and an in-page jump (`#…`) is left where it went.
      if (!first && newPage && window.location.hash === '') {
        scroller.scrollTop = 0
        setScroll('top')
      }
      return
    }
    let tries = 0
    let timer: ReturnType<typeof setTimeout> | undefined
    const stop = () => {
      if (timer !== undefined) clearTimeout(timer)
      timer = undefined
    }
    const step = () => {
      scroller.scrollTop = want
      if (Math.abs(scroller.scrollTop - want) < 2 || ++tries > 30) return stop()
      timer = setTimeout(step, 100)
    }
    step()
    // The reader's own scroll wins over a restore still waiting for the page.
    scroller.addEventListener('wheel', stop, { passive: true, once: true })
    scroller.addEventListener('touchstart', stop, { passive: true, once: true })
    window.addEventListener('keydown', stop, { once: true })
    return () => {
      stop()
      scroller.removeEventListener('wheel', stop)
      scroller.removeEventListener('touchstart', stop)
      window.removeEventListener('keydown', stop)
    }
  }, [place])
  // A `nav` that went nowhere new (the row already lit) is spent on the
  // render it caused, so it cannot send a later in-page open to the top.
  useEffect(() => {
    navigated.current = false
  })

  // THE PHONE DRAWER IS A DIALOG IN BEHAVIOUR: focus moves into it on open, Tab
  // wraps inside it, Escape closes it, and focus returns to the button that
  // opened it (the cleanup, so every way of closing -- Escape, the scrim, a
  // navigation -- restores it the same way).
  useEffect(() => {
    if (!drawer) return
    const side = sideRef.current
    const focusables = () =>
      side === null
        ? []
        : Array.from(side.querySelectorAll<HTMLElement>('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'))
    focusables()[0]?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        // THE INNERMOST FIRST (CH-11): the tenant sheet over the drawer
        // takes the key on its own capture listener and marks it used.
        if (e.defaultPrevented) return
        e.preventDefault()
        setDrawer(false)
        return
      }
      if (e.key !== 'Tab') return
      const items = focusables()
      if (items.length === 0) return
      const first = items[0]!
      const last = items[items.length - 1]!
      const active = document.activeElement
      if (e.shiftKey && (active === first || !(side?.contains(active) ?? false))) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && (active === last || !(side?.contains(active) ?? false))) {
        e.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      // An "On this page" jump hands focus to the section it went to (CH-12);
      // every other way of closing returns it to the menu button.
      const to = jumpedTo.current
      jumpedTo.current = null
      if (to !== null) to.focus({ preventScroll: true })
      else openerRef.current?.focus()
    }
  }, [drawer])

  // THE COLLAPSED SPINE'S FLYOUT CLOSES WHEN FOCUS LEAVES THE SPINE AND THE
  // FLYOUT, and on Escape, which hands focus back to the section link that
  // opened it. Opening it on focus without these left it up over the page for
  // a keyboard user, with no way to dismiss it short of moving the mouse.
  const closeFly = (refocus: boolean) => {
    if (flyTimer.current !== undefined) clearTimeout(flyTimer.current)
    const was = fly
    setFly(null)
    if (refocus && was !== null) {
      // Handed back, the section does not open its flyout again on that focus.
      quietFocus.current = true
      sideRef.current?.querySelector<HTMLElement>(`[data-sec="${was}"]`)?.focus()
      quietFocus.current = false
    }
  }
  const onFlyBlur = (e: ReactFocusEvent) => {
    const to = e.relatedTarget instanceof Node ? e.relatedTarget : null
    if (to !== null && ((sideRef.current?.contains(to) ?? false) || (flyRef.current?.contains(to) ?? false))) return
    closeFly(false)
  }
  const onFlyKey = (e: ReactKeyboardEvent) => {
    if (e.key !== 'Escape' || fly === null) return
    e.preventDefault()
    e.stopPropagation()
    closeFly(true)
  }
  // THE FLYOUT IS IN THE TAB ORDER, RIGHT AFTER ITS SECTION (live QA CH-8).
  // It is drawn outside the spine, so Tab went from Work straight to
  // Automate and its pages were mouse-only. From an open flyout's section,
  // Tab or ArrowRight enters it; inside it, the arrows move, Tab past its
  // last control goes on to the next spine item, Shift+Tab past its first
  // and ArrowLeft go back to the section.
  const flyItems = () => Array.from(flyRef.current?.querySelectorAll<HTMLElement>('a[href], button') ?? [])
  const enterFly = useRef(false)
  const quietFocus = useRef(false)
  useEffect(() => {
    if (fly === null || !enterFly.current) return
    enterFly.current = false
    flyItems()[0]?.focus()
  }, [fly])
  const onSectionKey = (e: ReactKeyboardEvent, key: Exclude<SpineSection, null>) => {
    if (!collapsed || drawer || !(key in PANEL_PAGES)) return
    const forward = (e.key === 'Tab' && !e.shiftKey) || e.key === 'ArrowRight'
    if (!forward || e.altKey || e.ctrlKey || e.metaKey) return
    e.preventDefault()
    if (flyTimer.current !== undefined) clearTimeout(flyTimer.current)
    if (fly === key && flyItems().length > 0) {
      flyItems()[0]!.focus()
      return
    }
    enterFly.current = true
    setFly(key)
  }
  const onFlyoutKey = (e: ReactKeyboardEvent) => {
    onFlyKey(e)
    if (e.defaultPrevented || fly === null) return
    const items = flyItems()
    const at = items.indexOf(document.activeElement as HTMLElement)
    const section = sideRef.current?.querySelector<HTMLElement>(`[data-sec="${fly}"]`) ?? null
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      const n = items.length
      items[(at + (e.key === 'ArrowDown' ? 1 : n - 1) + n) % n]?.focus()
    } else if (e.key === 'ArrowLeft' || (e.key === 'Tab' && e.shiftKey && at === 0)) {
      e.preventDefault()
      closeFly(true)
    } else if (e.key === 'Tab' && !e.shiftKey && at === items.length - 1 && section !== null) {
      const rail = Array.from(sideRef.current?.querySelectorAll<HTMLElement>('.sk-spine a[href], .sk-spine button:not([disabled])') ?? [])
      const next = rail[rail.indexOf(section) + 1]
      if (next === undefined) return
      e.preventDefault()
      next.focus()
    }
  }

  const toggle = useCallback(() => {
    if (tablet) {
      setTabletOpen((o) => !o)
      return
    }
    setPinned((c) => {
      writePref(COLLAPSED_KEY, c ? null : '1')
      return !c
    })
  }, [tablet])
  // THE PRESSED CONTROL IS UNMOUNTED BY ITS OWN PRESS (live QA CH-9): focus
  // fell to <body>. Its counterpart takes it once the swap has drawn.
  const flip = useCallback(() => {
    refocusToggle.current = collapsed ? 'collapse' : 'expand'
    toggle()
  }, [collapsed, toggle])
  useEffect(() => {
    const which = refocusToggle.current
    refocusToggle.current = null
    if (which !== null) sideRef.current?.querySelector<HTMLElement>(`.sk-${which}`)?.focus()
  }, [collapsed])

  // AN "ON THIS PAGE" JUMP MOVES FOCUS TO THE SECTION (live QA CH-12): it was
  // left on the panel link -- or, on a phone, on the menu button the closing
  // drawer handed it to -- so a keyboard or screen-reader user stayed at the
  // top of a page that had scrolled away from them.
  const onJump = useCallback(
    (id: string) => {
      jumpTo(id)
      const target = document.getElementById(id)
      if (target === null) return
      if (!target.hasAttribute('tabindex')) target.setAttribute('tabindex', '-1')
      if (drawer) {
        jumpedTo.current = target
        setDrawer(false)
      } else target.focus({ preventScroll: true })
    },
    [drawer],
  )

  // A navigation closes the phone drawer, any flyout and the tenant list.
  const nav = useCallback(
    (to: string) => {
      setDrawer(false)
      setFly(null)
      setPicker(null)
      navigated.current = true
      go(to)
    },
    [go],
  )

  // N opens Submit -- the promise the spine's "Submit (N)" makes -- from
  // anywhere but a field. Ignored: a key typed into an input, a textarea, a
  // select or anything contenteditable (it is a letter there); any modifier,
  // Shift included (the browser's and the OS's); and a key another handler
  // already consumed, such as the diff view's next-file `n`. `closest`, not
  // `isContentEditable`: the target is often a span inside the editable
  // element, and jsdom does not implement `isContentEditable` at all.
  //
  // `?` OPENS THE KEYBOARD TOPIC (G1-21), under the same guards but one: it
  // is Shift and `/` on most layouts, so Shift is allowed for it alone. Both
  // keys, and N's destination, are read from `SHORTCUTS` (shortcuts.ts), the
  // table the Help topic is generated from.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const key = e.key.length === 1 ? e.key.toLowerCase() : e.key
      if (key !== SUBMIT_SHORTCUT.key && key !== HELP_SHORTCUT.key) return
      if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return
      if (e.shiftKey && key !== HELP_SHORTCUT.key) return
      if (typedIntoField(e.target)) return
      // Nor while something sits over the page: a modal, an open drawer or
      // pinned help card (`role=dialog`), or Pool limits' side editor. It owns
      // the keyboard, and N there navigating away drops what was being edited.
      if (document.querySelector(OVER_THE_PAGE) !== null) return
      e.preventDefault()
      nav(key === HELP_SHORTCUT.key ? helpAnchor('keyboard') : SUBMIT_SHORTCUT.to!)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [nav])

  const hover = (k: Exclude<SpineSection, null> | null) => {
    if (!collapsed) return
    if (flyTimer.current !== undefined) clearTimeout(flyTimer.current)
    flyTimer.current = setTimeout(() => setFly(k), k === null ? 200 : 80)
  }

  // ---- the tenant switch (intake-tenants.html 2A) --------------------------
  const choices = useMemo(() => nameTenants(dataOf(mine) ?? [], who), [mine, who])
  useEffect(() => {
    // Not after Sign out: its clear is what made `tenant` null, and the old `me`
    // would write both stores straight back (signoutState.ts `signingOut`).
    if (signingOut()) return
    if (who !== null && who.tenant.display_name) learnTenantName(who.tenant.tenant_id, who.tenant.display_name)
    // With no tenant chosen, `me` names the default: Recent keeps its list under the bare key (CH-3).
    if (who !== null && tenant === null) learnDefaultTenant(who.tenant.tenant_id)
  }, [who, tenant])
  const switchable = who !== null && choices.length > 1
  // THE SWITCH ON A KEPT FORM, for the banner over it and its button's words.
  const switched = useSyncExternalStore(subscribeTenantSwitch, tenantSwitchSnapshot, tenantSwitchSnapshot)
  // A page change ends what the banner and the button say: the switch was
  // about the form that was open when it happened.
  useEffect(() => {
    clearTenantSwitch()
  }, [title, keepOnSwitch])
  const choose = useCallback(
    (to: TenantChoice) => {
      setPicker(null)
      if (who === null || to.tenant_id === who.tenant.tenant_id) return
      const named = choices.find((c) => c.tenant_id === who.tenant.tenant_id)
      const from = { id: who.tenant.tenant_id, name: named?.display_name ?? displayNameOf(who.tenant.display_name) ?? who.tenant.tenant_id }
      noteTenantSwitch({ from, to: { id: to.tenant_id, name: to.display_name }, kept: keepOnSwitch })
      chooseTenant(to.tenant_id)
      // A TOAST, because it acknowledges what the viewer just did -- and it
      // carries the way back (components.html A: toasts are for this only).
      acknowledge(
        <>
          Now acting as <b>{to.display_name}</b>. {keepOnSwitch ? 'Your form is kept.' : 'This page’s reads are reloading.'}
        </>,
        {
          label: `Back to ${from.name}`,
          onClick: () => {
            noteTenantSwitch({ from: { id: to.tenant_id, name: to.display_name }, to: from, kept: keepOnSwitch })
            chooseTenant(from.id)
          },
        },
      )
    },
    [who, keepOnSwitch, choices],
  )

  // ONE SPINE SECTION: the five after Submit, and Profile at the foot.
  const sectionLink = (s: { key: Exclude<SpineSection, null>; label: string; to: string }) => {
    // A 404 IS NEVER LIT AS OVERVIEW (visual QA pass 1, admin-shell-12):
    // `nearestPath` falls back to /overview for a path near nothing, and
    // Overview's panel has nothing for a 404 (`SectionPanel` draws none), so
    // the rail marked a section the reader is not in over an empty panel.
    const on = section === s.key && !(missing && s.key === 'overview')
    // A LINK, NOT A BUTTON (#503): a section opens in a new tab and copies
    // as a link, like every other place in the console.
    return (
      <a
        key={s.key}
        data-sec={s.key}
        className={`sk-ri${on ? ' is-on' : ''}`}
        // A 404 LIGHTS THE NEAREST SECTION BUT IS ON NO PAGE (live QA
        // CH-14): the section is drawn lit as the way back, never
        // announced as where the reader is.
        aria-current={on && !missing ? 'page' : undefined}
        title={s.label}
        {...routed(s.to, () => {
          // COLLAPSED, A CLICK ON THE SECTION YOU ARE IN OPENS ITS PAGES
          // (visual QA V083, 2026-10-11). It expanded the panel, so with
          // no hover -- a touch screen -- the only way between the four
          // Admin pages was to undo the collapse. The flyout opens at
          // once and stays until a choice, Escape or a click elsewhere;
          // `Open the panel` in it, and the spine's own expand, still
          // undo the collapse. Overview has no pages, so it expands.
          if (collapsed && on && s.key !== 'overview' && !drawer) {
            if (flyTimer.current !== undefined) clearTimeout(flyTimer.current)
            setFly(s.key)
            return
          }
          // Collapsed, a click on another section opens the panel again --
          // but not in the tablet band, which is collapsed for room (CH-1).
          if (collapsed && !tablet) toggle()
          if (!(collapsed && on)) nav(s.to)
        })}
        onMouseEnter={() => hover(s.key)}
        onMouseLeave={() => hover(null)}
        onFocus={() => {
          // Focus opens the flyout at once, so the Tab that follows can enter it (CH-8).
          if (!collapsed || drawer || quietFocus.current) return
          if (flyTimer.current !== undefined) clearTimeout(flyTimer.current)
          setFly(s.key)
        }}
        onKeyDown={(e) => onSectionKey(e, s.key)}
      >
        {/* PROFILE IS DRAWN AS THE PERSON (NAV-PROFILE): their initials, the
            email in the title, and a whole-app state's dot (states.html C). */}
        {s.key === 'profile' ? (
          <span className="sk-av" title={who?.principal.email ?? 'not read'} aria-hidden>
            {initials(who?.principal.email ?? '')}
            {fault !== null && <i className="sk-av-dot" />}
          </span>
        ) : (
          <Icon name={s.key} />
        )}
        <small>{s.label}</small>
        {s.key === 'capacity' && meter?.warn != null && <i className="sk-dot" title={meter.warn} />}
        {/* S1: an amber dot while the setup read says a step is left. */}
        {s.key === 'config' && setupLeft && <i className="sk-dot" title="Workspace setup has a step left" />}
        {s.key === 'automate' && waiting !== null && (
          <i className={waiting.n === null ? 'sk-badge is-unread' : 'sk-badge'} title={waiting.why} aria-label={waiting.why}>
            {waiting.n === null ? '—' : waiting.n}
          </i>
        )}
        {/* #1206: requests waiting on ANY admin, as a pill at the item's
            top-right. Gated on the session read as well as the count: a
            workspace admin's Admin carries no platform count. */}
        {s.key === 'admin' && admin && adminPending > 0 && (
          <i className="sk-badge sk-apill" title={`${adminPending} waiting for an admin`} aria-label={`${adminPending} waiting for an admin`}>
            {adminPending}
          </i>
        )}
      </a>
    )
  }

  const spine = (
    <nav className="sk-spine" aria-label="Sections" onBlur={onFlyBlur} onKeyDown={onFlyKey}>
      <a className="sk-hive" {...routed('overview/now', () => nav('overview/now'))}>
        <SwarmMark size={44} paint="sky" title="SwarmCloud" />
      </a>
      {/* THE COLLAPSED SPINE KEEPS THE TENANT IN SIGHT (2A): a tile under the
          Hive, which opens the same list beside the spine. */}
      {collapsed && !drawer && (
        <TenantTile me={me} choices={dataOf(mine) ?? []} switchable={switchable} open={picker === 'tile'} onOpen={() => setPicker(picker === 'tile' ? null : 'tile')} />
      )}
      <a className="sk-ri sk-cta" title="Submit (N)" aria-current={section === null ? 'page' : undefined} {...routed('submit', () => nav('submit'))}>
        <Icon name="submit" />
        <small>Submit</small>
      </a>
      <span className="sk-rsep" aria-hidden />
      {spineItems.map(sectionLink)}
      <span className="sk-grow" />
      {/* THE WAY BACK TO THE PANEL (U10a D26): collapsed, the only way to
          bring it back was to click a section. The panel's own collapse
          control, mirrored, at the spine's foot. */}
      {collapsed && !drawer && (
        <button type="button" className="sk-ibtn sk-expand" aria-label="Expand the panel" title="Expand the panel" onClick={flip}>
          <Icon name="expand" />
        </button>
      )}
      {/* THE BELL AT THE FOOT, above Help, from 760px up (V178): always on
          screen, as it was in the sticky bar, and no band over the page. Its
          drop-down opens beside the spine (styles/notifications.css). */}
      {!phone && bell !== null && <div className="sk-bell">{bell}</div>}
      {foot}
      {/* Collapsed, the panel's Sign out is an icon at the spine's foot. */}
      {collapsed && !drawer && canSignOut && <SignOutButton email={who.principal.email} compact />}
      {/* PROFILE, LAST (NAV-FOOT, #1672): the bottom-most item at every
          width -- the open spine, the collapsed one (its flyout opens beside
          the foot, styles.css `.sk-flyout.is-profile`) and the phone drawer,
          which holds this same spine. */}
      {sectionLink(PROFILE_ITEM)}
    </nav>
  )

  // The Config panel's meter reads the setup read through this provider.
  const pages = (
    <SetupContext.Provider value={setupRead}>
      <PanelPages
        section={section}
        tab={tab}
        agentTab={agentTab}
        adminPages={adminPages}
        adminOf={adminOf}
        configPages={configPages}
        personal={personal === true}
        nav={nav}
        counts={counts}
        adminCounts={adminCounts}
        jumpCounts={jumpCounts}
        helpGroup={helpGroup}
        apiFailuresOnly={apiFailuresOnly}
        onApiFilter={onApiFilter}
        missing={missing}
        onJump={onJump}
        sections={spineItems}
      />
    </SetupContext.Provider>
  )

  const panel = (
    <nav className="sk-panel" aria-label="Pages">
      <div className="sk-ph">
        <span className="sk-wm">
          Swarm<span>Cloud</span>
        </span>
        <EnvPill label={t.label} prod={t.bar} title={t.explain} />
        <button type="button" className={COLLAPSE_CLASS} aria-label="Collapse the panel" title="Collapse the panel" onClick={flip}>
          <Icon name="collapse" />
        </button>
        {/* THE DRAWER'S OWN CLOSE (live QA CH-12): the menu button that
            opened it sits under it. Escape and the scrim still close it. */}
        {drawer && (
          <button type="button" className="sk-ibtn sk-dclose" aria-label="Close the menu" title="Close the menu" onClick={() => setDrawer(false)}>
            <span aria-hidden>&times;</span>
          </button>
        )}
      </div>
      <TenantBlock
        me={me}
        mine={mine}
        onRetryList={rereadMine}
        open={picker === 'panel'}
        onOpen={() => setPicker(picker === 'panel' ? null : 'panel')}
        chosen={tenant}
        onBackToDefault={() => {
          setPicker(null)
          chooseTenant(null)
        }}
        prefsDown={prefsDown}
      />
      <div className="sk-pscroll">{pages}</div>
      <Meter meter={meter} read={cap.status === 'error' || fault !== null ? 'unread' : cap.status === 'loading' ? 'loading' : 'landed'} />
      <div className="sk-pfoot">
        <span className="sk-pfrow">
          {/* THE NAME OPENS PROFILE (NAV-FOOT, #1672): a link once the name is
              read, named for where it goes -- the visible first name first,
              so a voice user can say what they see. Unread, it is plain text:
              "reading…" is no one's profile. The chip, Sign out and the email
              beside it are unchanged. */}
          <b title={signedIn?.title}>
            {signedIn === null ? (
              me.status === 'loading' ? 'reading…' : 'not read'
            ) : (
              <a className="sk-me" aria-label={`${signedIn.text}, open your profile`} {...routed(PROFILE_ADDRESS, () => nav(PROFILE_ADDRESS))}>
                {signedIn.text}
              </a>
            )}
          </b>
          {admin && <span className="sk-adm">admin</span>}
          {canSignOut && <SignOutButton email={who.principal.email} />}
        </span>
        {/* THE ROLE, NOT THE EMAIL (S1a): the address is in its title. One
            line each, cut, whole in the title (styles.css `.sk-pfoot`, V054). */}
        {who !== null && (
          <small className="sk-role" title={who.principal.email}>
            {role ?? 'reading role…'}
          </small>
        )}
        {/* No Settings links here since NAV-PROFILE (part of #1633): the
            four that stacked under the name are the Profile panel's pages. */}
        <ThemeToggle />
      </div>
    </nav>
  )

  // The kept form's banner: what it will now submit as, and the way back.
  const keptBanner =
    keepOnSwitch && switched !== null && switched.kept ? (
      <div className="sk-kept">
        <Banner
          tone="info"
          title={`You switched to ${switched.to.name} with an unsent form open.`}
          actions={
            <Button
              size="sm"
              onClick={() => {
                noteTenantSwitch({ from: switched.to, to: switched.from, kept: true })
                chooseTenant(switched.from.id)
              }}
            >
              Switch back to {switched.from.name}
            </Button>
          }
        >
          The form is kept, and it will now submit as {switched.to.name}: its runner list, capacity and repository access are {switched.to.name}&rsquo;s.
        </Banner>
      </div>
    ) : null

  // ACTING THROUGH THE REACH IS SAID ON EVERY SCREEN (§4.8, Q12): a platform
  // admin in a group workspace they are not a member of is recorded on every
  // write and hourly on reads, and the banner is how they know it. Drawn from
  // the server's `via` for the tenant `/v1/tenants/me` says is in force, so it
  // never claims a reach the API did not answer as.
  const reachName =
    who !== null && choices.some((c) => c.tenant_id === who.tenant.tenant_id && isReach(c)) ? displayNameOf(who.tenant.display_name) ?? who.tenant.tenant_id : null
  const reachBanner =
    reachName === null ? null : (
      <div className="sk-kept" data-banner="platform-admin">
        <Banner tone="warn" title={`You are acting in ${reachName} as a platform admin.`}>
          Every action is recorded in its audit log.
        </Banner>
      </div>
    )

  return (
    <div
      ref={appRef}
      className={`sk-app${collapsed ? ' is-collapsed' : ''}${drawer ? ' has-drawer' : ''}${scroll === 'top' ? '' : ' is-scrolled'}`}
      data-env={env.kind}
    >
      {/* THE FIRST TAB GOES TO THE PAGE (live QA CH-15): before this, to the
          logo, Submit, seven spine links, the tenant card and every panel
          row. Hidden until it has focus. */}
      <a
        className="sk-skip"
        // An element's fragment, not a route: built from the id it targets.
        href={`#${CONTENT_ID}`}
        onClick={(e) => {
          e.preventDefault()
          focusPage(scrollerRef.current)
        }}
      >
        Skip to the page
      </a>
      {t.bar && <div className="sk-prodbar" aria-hidden />}
      {/* `has-chip` WHEN THE WORKSPACE IS A SWITCHABLE CHIP (visual QA run 6,
          6:admin-007): the title then gives way before the chip does
          (styles.css), so the pill keeps the workspace's name. */}
      <header className={switchable ? 'sk-pbar has-chip' : 'sk-pbar'}>
        {/* APPROVALS WAITING SHOW ON THE MENU BUTTON (visual QA V237,
            2026-10-16): the spine's Automate badge is inside the drawer, so
            on a phone nothing said they waited until the drawer was open.
            The same count, on the button that opens it; its name stays
            "Open the menu" and the count is its description. */}
        <button
          type="button"
          className="sk-ibtn"
          aria-label="Open the menu"
          aria-describedby={waiting !== null ? menuNoteId : undefined}
          aria-expanded={drawer}
          ref={openerRef}
          onClick={() => setDrawer(true)}
        >
          <Icon name="menu" />
          {waiting !== null && (
            <>
              <i className={waiting.n === null ? 'sk-badge sk-mbadge is-unread' : 'sk-badge sk-mbadge'} aria-hidden="true">
                {waiting.n === null ? '—' : waiting.n}
              </i>
              <span className="sk-vh" id={menuNoteId}>
                {waiting.why}
              </span>
            </>
          )}
        </button>
        <SwarmMark size={22} paint="sky" />
        <b>{title}</b>
        {/* THE PHONE'S TENANT IS A CHIP that opens a bottom sheet (2A), for
            someone who can switch; a plain label for everyone else. */}
        {/* THE WORKSPACE BY ITS SHORT KEY (visual QA VQ-001, part of
            #2391; overview.html's phone frame: `≡ Hive Overview  eng DEV`).
            Its full name squeezed the page name to `O…` at 390; the key
            (`eng`) is what the switcher's list prints under each name, and
            the whole name is the control's title and accessible name. */}
        {who !== null &&
          (switchable ? (
            <button
              type="button"
              className="sk-tchip"
              aria-haspopup="dialog"
              aria-expanded={picker === 'sheet'}
              aria-label={`Workspace: ${workspaceName(who.tenant, dataOf(mine) ?? [])}. Switch workspace`}
              title={workspaceName(who.tenant, dataOf(mine) ?? [])}
              onClick={() => setPicker('sheet')}
            >
              <span className="sk-tcn">{workspaceKey(who.tenant, dataOf(mine) ?? [])}</span>
              <Icon name="swap" />
            </button>
          ) : (
            // Cut with an ellipsis on a narrow phone (styles.css), so the whole name is its title.
            <span className="sk-tn" title={workspaceName(who.tenant, dataOf(mine) ?? [])}>
              {workspaceKey(who.tenant, dataOf(mine) ?? [])}
            </span>
          ))}
        <EnvPill label={t.label} prod={t.bar} title={t.explain} mini />
        {/* NO ROLE TAG IN THE PHONE HEADER (visual QA VQ-001, part of #2391;
            navigation.html V2: "on a phone both live in the drawer"). The
            `admin` tag here was one more fixed-width item the page name gave
            way to; it is in the drawer's foot, beside the name, as on the
            desktop panel. */}
        {/* THE BELL JOINS THE PHONE HEADER (V178): one 44px band, not a
            second sticky one under it. After the tags, before `Top`. */}
        {phone && bell !== null && <span className="sk-pbell">{bell}</span>}
        {/* A WAY BACK UP, once the page is more than a screen long and the
            reader is past the first screen of it (#139). Drawn below 760px
            only (styles.css): above it the spine never scrolls away.

            IN THE HEADER, NOT OVER THE PAGE (visual QA V009, 2026-10-11). It
            was a pill floating at the foot of the scrollport, and at 400px
            it sat on whatever scrolled under it -- a stacked record's value,
            a refusal's last lines, Overview's pool figures. No room the
            scroller reserves can stop that mid-page; the header is already
            on screen whenever `Top` is (compacted to 36px by then), so here
            it covers nothing and costs no chrome. Last in the row, after the
            tags; the title gives way first.

            ITS PLACE IS HELD FROM THE FIRST FRAME (visual QA run 3,
            work-b-007, part of #1680). Mounted only once the page was far
            down, it pushed the tags beside it about 60px left as it arrived.
            The same button is always in the row now, `is-idle` until then:
            `visibility: hidden` keeps its width and takes it out of sight,
            out of the tab order and out of the accessibility tree. */}
        <button
          type="button"
          className={scroll === 'far' ? 'sk-top' : 'sk-top is-idle'}
          onClick={toTop}
          aria-label="Back to the top of the page"
          aria-hidden={scroll === 'far' ? undefined : true}
          tabIndex={scroll === 'far' ? undefined : -1}
        >
          {/* THE ARROW ALONE (VQ-001): its slot is held in the row from the
              first frame, so `↑ Top`'s word was width the page name never
              had. Its name is the aria-label; its title says it on hover. */}
          <span aria-hidden="true" title="Top">
            &#8593;
          </span>
        </button>
      </header>
      {drawer && <div className="sk-scrim" onClick={() => setDrawer(false)} aria-hidden />}
      {/* THE DRAWER IS A DIALOG TO ASSISTIVE TECHNOLOGY TOO (CH-12), as it
          already was to the keyboard: focus moves in, Tab stays in it. */}
      <div
        className="sk-side"
        ref={sideRef}
        role={drawer ? 'dialog' : undefined}
        aria-modal={drawer ? true : undefined}
        aria-label={drawer ? 'Menu' : undefined}
      >
        {spine}
        {!collapsed || drawer ? panel : null}
      </div>
      {collapsed && fly !== null && !drawer && (
        <Flyout
          section={fly}
          tab={section === fly ? tab : ''}
          agentTab={agentTab}
          adminPages={adminPages}
          configPages={configPages}
          nav={nav}
          onExpand={() => {
            setFly(null)
            refocusToggle.current = 'collapse'
            toggle()
          }}
          onEnter={() => hover(fly)}
          onLeave={() => hover(null)}
          flyRef={flyRef}
          onBlur={onFlyBlur}
          onKeyDown={onFlyoutKey}
        />
      )}
      {picker !== null && who !== null && switchable && (
        <TenantPicker at={picker} current={who.tenant.tenant_id} choices={choices} onChoose={choose} onClose={() => setPicker(null)} />
      )}
      <div className="sk-main">
        {/* Focusable, never in the tab order: where focus goes when the page
            has no heading yet, so the keyboard scrolls it (CH-5). */}
        <div className="ctl-scroll" ref={scrollerRef} id={CONTENT_ID} tabIndex={-1}>
          {!online && <OfflineBanner />}
          {fault !== null ? (
            // A WHOLE-APP STATE TAKES OVER THE CONTENT AREA ONLY (states.html
            // C): the spine, the panel and Submit stay usable.
            <AppTakeover
              error={fault}
              online={online}
              onRetry={() => {
                rereadMe()
                setAttempt((n) => n + 1)
              }}
            />
          ) : (
            <>
              {reachBanner}
              {keptBanner}
              {who !== null && <SignedBackIn key={who.principal.email} email={who.principal.email} />}
              {phone && section === 'overview' && !missing && <PhoneJumps onJump={onJump} />}
              {/* Keyed on the chosen tenant: a switch is a fresh screen, every
                  read of it made again with the new X-Swarm-Tenant -- except
                  a kept form, which re-reads in place. */}
              <Fragment key={`${keepOnSwitch ? 'kept' : (tenant ?? '')}:${attempt}`}>
                <SetupContext.Provider value={setupRead}>
                  {adminRefused && who !== null ? (
                    // IN THE PAGE'S FRAME (QA1B-04 review): the gutter and
                    // the head every page has (`.app`, `main.work`,
                    // `PageHead`), not drawn flush against the panel.
                    <div className="app">
                      <main className="work">
                        <PageHead title={refusedPage} />
                        {landing === null ? (
                          <AdminRoleRefusal
                            page={refusedPage}
                            role={refusedRole(refusedHere, personal === true)}
                            workspace={inSentence(workspaceName(who.tenant, dataOf(mine) ?? []))}
                            // A workspace admin's own pages are refused as such (admin2-02).
                            audience={refusedEntry !== undefined && WORKSPACE_ADMIN_PAGE_KEYS.has(refusedEntry.key) ? 'workspace' : 'platform'}
                            personal={personal === true || refusedHere?.kind === 'user'}
                          />
                        ) : (
                          // The member's refusal (AdminRoleRefusal's words),
                          // and the way to the Admin page that is theirs
                          // (admin2-01): the workspaces they administer, by name.
                          <div className="ws-refusal" data-refused="admin">
                            <Banner
                              tone="info"
                              role="status"
                              title={`${refusedPage} is for platform admins`}
                              actions={
                                <>
                                  <ButtonLink kind="primary" size="sm" {...routed(landing.to, () => nav(landing.to))}>
                                    Open {landing.label}
                                  </ButtonLink>
                                  <ButtonLink kind="secondary" size="sm" {...routed('overview/now', () => nav('overview/now'))}>
                                    Open Overview
                                  </ButtonLink>
                                </>
                              }
                            >
                              {adminOf.length > 0 ? `You are not a platform admin. You administer ${listed(adminOf)}.` : 'You are not a platform admin.'}
                            </Banner>
                          </div>
                        )}
                      </main>
                    </div>
                  ) : (
                    children
                  )}
                </SetupContext.Provider>
              </Fragment>
            </>
          )}
        </div>
        {dock}
      </div>
      <p className="sk-vh" role="status" aria-live="polite">
        {arrived}
      </p>
      <Toaster />
    </div>
  )
}

/** How far down the page the reader is, in the three bands the phone chrome uses. */
export type PageScroll = 'top' | 'down' | 'far'

/**
 * Past the phone header's own height (44px) the header compacts to 36px:
 * by then the page's title row has gone under it, and what the reader needs
 * from it is the way to the menu, not 8px of padding (#139, §6.15's 25%
 * chrome budget). Past one scrollport -- floored at 600px, so a short phone
 * in landscape is not offered `Top` after one swipe -- the `Top` control
 * appears.
 */
export const COMPACT_AFTER_PX = 44
export const TOP_AFTER_MIN_PX = 600

export function scrollBand(scrollTop: number, viewport: number): PageScroll {
  if (scrollTop > Math.max(viewport, TOP_AFTER_MIN_PX)) return 'far'
  if (scrollTop > COMPACT_AFTER_PX) return 'down'
  return 'top'
}

function initials(email: string): string {
  const local = email.split('@')[0] ?? ''
  const parts = local.split(/[._-]/).filter(Boolean)
  return ((parts[0]?.[0] ?? '') + (parts[1]?.[0] ?? '')).toUpperCase() || '·'
}

function EnvPill({ label, prod, title, mini = false }: { label: string; prod: boolean; title: string; mini?: boolean }) {
  return (
    <span className={`sk-pill${prod ? ' is-prod' : ''}${mini ? ' is-mini' : ''}`} title={title}>
      {label}
    </span>
  )
}

/** Where the tenant list opens: under the panel's block, beside the collapsed spine's tile, or as the phone's bottom sheet. */
type TenantPickerAt = 'panel' | 'tile' | 'sheet'

/** The block's initial: the first letter of the name the API answered with. */
function initialOf(name: string): string {
  return name.charAt(0).toUpperCase() || '·'
}

/**
 * The tenant block: a static label with copy-id, and a SWITCHER when
 * `/v1/tenants/mine` lists more than one tenant (owner decision 2026-10-01;
 * intake-tenants.html 2A, picked 2026-10-02).
 *
 * The switcher SELECTS among the caller's verified memberships; it grants
 * nothing. Its options are exactly what the API confirmed, and the API refuses
 * any other `X-Swarm-Tenant` anyway. The value shown is the tenant `/v1/tenants/me`
 * says is in force, not the stored pick, so the block never claims a tenant the
 * API did not answer as.
 *
 * WHEN THE LIST COULD NOT BE READ, the block says so ("others not read", and
 * a retry) rather than hiding the chance of others: a list we could not read
 * is not a list of one.
 */
function TenantBlock({
  me,
  mine,
  onRetryList,
  open,
  onOpen,
  chosen = null,
  onBackToDefault = () => chooseTenant(null),
  prefsDown = null,
}: {
  me: Result<Me>
  mine: Result<TenantChoice[]>
  onRetryList: () => void
  open: boolean
  onOpen: () => void
  /** This tab's switch (`chosenTenant`), null when the default is in force. */
  chosen?: string | null
  /** "Back to default": forget this tab's switch. */
  onBackToDefault?: () => void
  /** The API's `preferences_unavailable` message, or null (Q9). */
  prefsDown?: string | null
}) {
  const [said, setSaid] = useState('')
  const who = dataOf(me)
  if (who === null) {
    return (
      <div className="sk-tenant" role="status">
        <span className="sk-tg">·</span>
        <span className="sk-tt">
          <b>{me.status === 'loading' ? 'reading…' : 'not read'}</b>
          <small>workspace</small>
        </span>
      </div>
    )
  }
  const id = who.tenant.tenant_id
  const copy = () => {
    const c = typeof navigator === 'undefined' ? undefined : navigator.clipboard
    if (c === undefined) return setSaid('copy refused; select the id instead')
    c.writeText(id).then(
      () => setSaid('workspace id copied'),
      () => setSaid('copy refused; select the id instead'),
    )
  }
  const choices = dataOf(mine) ?? []
  const name = workspaceName(who.tenant, choices)
  // THE DEFAULT (§4.5): the workspace a request naming none acts in, the
  // stored one or else today's rule, as `/v1/tenants/mine` marks it.
  const isDefault = choices.some((c) => c.tenant_id === id && defaultChoice(c))
  const back =
    chosen === null ? null : (
      <button type="button" className="sk-tretry sk-tback" onClick={onBackToDefault}>
        Back to default
      </button>
    )
  // THE NAME STARTS WITH THE WORDS ON THE BUTTON (QA-028, part of #1680):
  // `Copy tenant id u-bogdan` did not contain the visible `copy id`, so a
  // speech user saying "click copy id" matched nothing (axe
  // label-content-name-mismatch). The tenant still follows, for a reader.
  const copyId = (
    <>
      <button type="button" className="sk-cp" aria-label={`copy id: workspace ${id}`} title={id} onClick={copy}>
        copy id
      </button>
      <span className="sk-said" role="status">
        {said}
      </span>
    </>
  )
  // A WORKSPACE, IN EVERY WORD A PERSON READS (visual QA VQ-056, part of
  // #2391; BGD on #2157): the tile said `tenant · 1 of 3` and its list `3
  // tenants you are a member of` beside Config's Workspace Settings.
  if (choices.length > 1) {
    const at = choices.findIndex((c) => c.tenant_id === id)
    return withPrefs(
      <div className={`sk-tenant is-sw${open ? ' is-open' : ''}`}>
        <button type="button" className="sk-tsw" aria-haspopup="dialog" aria-expanded={open} title={id} onClick={onOpen}>
          <span className="sk-tg" aria-hidden>
            {initialOf(name)}
          </span>
          <span className="sk-tt">
            <b>{name}</b>
            <small>
              {/* ITS KIND, AS WORKSPACE SETTINGS SAYS IT (QA1 admin-shell-21):
                  the person's own workspace is `personal` there. */}
              {choices[at]?.kind === 'user' ? 'personal' : 'workspace'} · {at === -1 ? 'not in the list' : `${at + 1} of ${choices.length}`}
              {isDefault ? ' · default' : ''}
            </small>
          </span>
          <Icon name="swap" className="sk-ic sk-car" />
        </button>
        <span className="sk-tcopy">
          {copyId}
          {back}
        </span>
      </div>,
      prefsDown,
    )
  }
  return withPrefs(
    <div className="sk-tenant">
      <span className="sk-tg" aria-hidden>
        {initialOf(name)}
      </span>
      <span className="sk-tt">
        <b title={id}>{name}</b>
        <small>
          {mine.status === 'error' ? (
            <>
              workspace · others not read{' '}
              <button type="button" className="sk-tretry" onClick={onRetryList}>
                retry
              </button>
            </>
          ) : (
            // Its kind as the switcher's list says it (admin-shell-21): the person's own is `personal`.
            <>{who.tenant.kind === 'user' ? 'personal' : 'workspace'} {isDefault ? '· default ' : ''}</>
          )}
          {copyId}
          {back}
        </small>
      </span>
    </div>,
    prefsDown,
  )
}

/** `is_default` on a `/v1/tenants/mine` entry (PROFILE-DEFAULT); absent from an older API. */
function defaultChoice(c: TenantChoice): boolean {
  return (c as TenantChoice & { is_default?: unknown }).is_default === true
}

/**
 * The block, and under it the API's own `preferences_unavailable` message: a
 * request that named no workspace was refused, so nothing was done, and the
 * way on is the switcher right above it (§4.5, Q9).
 */
function withPrefs(block: ReactNode, prefsDown: string | null) {
  return (
    <>
      {block}
      {prefsDown !== null && (
        <div className="sk-tprefs">
          <Banner tone="warn" role="alert" title="Your default workspace is not known">
            {prefsDown}
          </Banner>
        </div>
      )}
    </>
  )
}

/**
 * The collapsed spine's tenant tile: THE INITIAL ONLY, the name in its title
 * (U10a D26, owner QA 2026-10-04: `E Engin…` -- a 44px tile has no room for a
 * name, and a cut one reads as a different tenant). It opens the list beside
 * the spine for someone who can switch.
 */
function TenantTile({
  me,
  choices,
  switchable,
  open,
  onOpen,
}: {
  me: Result<Me>
  choices: readonly TenantChoice[]
  switchable: boolean
  open: boolean
  onOpen: () => void
}) {
  const who = dataOf(me)
  const name = who === null ? (me.status === 'loading' ? 'reading…' : 'not read') : workspaceName(who.tenant, choices)
  const say = who === null ? `Tenant ${name}` : `Tenant ${name} (${who.tenant.tenant_id})`
  const body = (
    <>
      <span aria-hidden>{who === null ? '·' : initialOf(name)}</span>
      {switchable && <Icon name="swap" className="sk-ic sk-tswap" />}
    </>
  )
  if (!switchable) {
    return (
      <span className="sk-ttile" title={say} role="img" aria-label={say}>
        {body}
      </span>
    )
  }
  return (
    <button type="button" className="sk-ttile" title={`${say}: switch`} aria-haspopup="dialog" aria-expanded={open} aria-label={`${say}: switch`} onClick={onOpen}>
      {body}
    </button>
  )
}

/** The control each place opens the list from, which takes focus back when it closes. */
const PICKER_TRIGGER: Readonly<Record<TenantPickerAt, string>> = { panel: '.sk-panel .sk-tsw', tile: 'button.sk-ttile', sheet: '.sk-pbar .sk-tchip' }

const TABBABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

/** The next control after `from` in the document's order, outside `skip`. */
function tabbableAfter(from: HTMLElement, skip: HTMLElement | null): HTMLElement | null {
  const all = Array.from(document.querySelectorAll<HTMLElement>(TABBABLE)).filter((el) => skip === null || !skip.contains(el))
  return all[all.indexOf(from) + 1] ?? null
}

/**
 * THE LIST OF TENANTS YOU MAY ACT AS: beside the panel's block, beside the
 * collapsed spine's tile, or, on a phone, a bottom sheet with 44px rows. A
 * dialog in behaviour: focus moves in, Escape and a press outside close it,
 * and focus goes back to what opened it.
 */
function TenantPicker({
  at,
  current,
  choices,
  onChoose,
  onClose,
}: {
  at: TenantPickerAt
  current: string
  choices: readonly NamedTenantChoice[]
  onChoose: (c: NamedTenantChoice) => void
  onClose: () => void
}) {
  const box = useRef<HTMLDivElement | null>(null)
  const close = useRef(onClose)
  close.current = onClose
  /** False once focus left by Tab: it stays where the reader sent it. */
  const giveBack = useRef(true)
  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    box.current?.querySelector<HTMLElement>('button[aria-current="true"], button')?.focus()
    // CAPTURE, so the innermost layer takes Escape first (live QA CH-11): the
    // phone drawer under the sheet listens on the same document, and one
    // Escape closed both.
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !e.defaultPrevented) {
        e.preventDefault()
        close.current()
      }
    }
    const onDown = (e: PointerEvent) => {
      const t = e.target
      if (!(t instanceof Element)) return
      if (box.current?.contains(t) === true || t.closest('.sk-tsw, .sk-ttile, .sk-tchip') !== null) return
      close.current()
    }
    // FOCUS ARRIVING OUTSIDE CLOSES IT TOO (QA1 admin-shell-07): `onBlur`
    // below reads `relatedTarget`, and a focusout that does not carry one
    // returned early, so Tab walked into the page with the list still open and
    // a later Escape gave focus back to nothing. Where focus LANDS is read here.
    const onIn = (e: FocusEvent) => {
      const t = e.target
      if (!(t instanceof Element) || box.current?.contains(t) === true) return
      if (t.closest('.sk-tsw, .sk-ttile, .sk-tchip') !== null) return
      giveBack.current = false
      close.current()
    }
    document.addEventListener('keydown', onKey, true)
    document.addEventListener('pointerdown', onDown)
    document.addEventListener('focusin', onIn)
    return () => {
      document.removeEventListener('keydown', onKey, true)
      document.removeEventListener('pointerdown', onDown)
      document.removeEventListener('focusin', onIn)
      // BACK TO THE CONTROL THAT OPENED IT, EVEN WHEN A TAP DID NOT FOCUS IT
      // (visual QA pass 2, admin2-06; part of #2694): a tap on the phone's
      // workspace chip leaves focus on <body>, so the sheet's Escape handed
      // focus back to <body> and a keyboard user restarted from `Skip to
      // the page`. Whatever held focus is kept only if it is a control.
      if (!giveBack.current) return
      const held = opener !== null && opener !== document.body && opener.isConnected ? opener : null
      ;(held ?? document.querySelector<HTMLElement>(PICKER_TRIGGER[at]))?.focus()
    }
  }, [])
  // FOCUS LEAVING CLOSES IT, AND THE ARROWS MOVE IN IT (live QA CH-7). Tab
  // walked out into the page with the list still open (`aria-expanded`
  // true), where Escape no longer reached it; and a list of choices offered
  // no ArrowUp or ArrowDown.
  const onBlur = (e: ReactFocusEvent) => {
    const to = e.relatedTarget
    if (!(to instanceof Element) || box.current?.contains(to) === true) return
    // Its own trigger toggles it on the click that follows.
    if (to.closest('.sk-tsw, .sk-ttile, .sk-tchip') !== null) return
    giveBack.current = false
    close.current()
  }
  const onKeyDown = (e: ReactKeyboardEvent) => {
    // TAB OUT OF THE POPOVER CONTINUES FROM ITS TRIGGER (visual QA pass 2,
    // admin2-11; part of #2694). The popover is drawn after the spine and
    // the panel, so Tab past its last option went on into the page and
    // skipped the panel's `copy id`, `Back to default` and every section
    // link. Tab goes to the control after the trigger, Shift+Tab from the
    // first option back to the trigger. The phone sheet is modal and keeps
    // the browser's order.
    if (e.key === 'Tab' && at !== 'sheet') {
      const items = Array.from(box.current?.querySelectorAll<HTMLElement>('.sk-to') ?? [])
      const edge = e.shiftKey ? items[0] : items[items.length - 1]
      const trigger = document.querySelector<HTMLElement>(PICKER_TRIGGER[at])
      if (edge === undefined || document.activeElement !== edge || trigger === null) return
      e.preventDefault()
      giveBack.current = false
      close.current()
      ;(e.shiftKey ? trigger : tabbableAfter(trigger, box.current) ?? trigger).focus()
      return
    }
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp' && e.key !== 'Home' && e.key !== 'End') return
    const items = Array.from(box.current?.querySelectorAll<HTMLElement>('.sk-to') ?? [])
    if (items.length === 0) return
    e.preventDefault()
    const from = items.indexOf(document.activeElement as HTMLElement)
    const n = items.length
    const next = e.key === 'Home' ? 0 : e.key === 'End' ? n - 1 : (from + (e.key === 'ArrowDown' ? 1 : n - 1) + n) % n
    items[next]?.focus()
  }
  // A platform admin's reach entries under their own heading, after the
  // memberships (§4.8); for anyone else `reach` is empty and the list is as before.
  const members = choices.filter((c) => !isReach(c))
  const reach = choices.filter(isReach)
  const row = (c: NamedTenantChoice) => {
    const on = c.tenant_id === current
    // ONE LABEL, IN THE GRID'S THIRD COLUMN (PICKER-LABELS): two `.sk-ck`
    // spans wrapped the second into the next grid row's 24px column, over
    // the tenant id -- "defaultu-bogdan" (live, 2026-10-19).
    const marks = [on ? 'current' : null, c.is_default ? 'default' : null].filter((m) => m !== null)
    return (
      <button
        key={c.tenant_id}
        type="button"
        className={`sk-to${on ? ' is-on' : ''}`}
        aria-current={on ? 'true' : undefined}
        title={c.group === c.display_name ? undefined : `registered as ${c.group}`}
        onClick={() => onChoose(c)}
      >
        <span className="sk-tg" aria-hidden>
          {initialOf(c.display_name)}
        </span>
        <b>{c.display_name}</b>
        {marks.length > 0 && <span className="sk-ck">{marks.join(' · ')}</span>}
        <small>{c.tenant_id}</small>
      </button>
    )
  }
  const list = (
    <div className={`sk-tpop is-${at}`} role="dialog" aria-modal={at === 'sheet' ? true : undefined} aria-label="Act as" ref={box} onBlur={onBlur} onKeyDown={onKeyDown}>
      {at === 'sheet' && <span className="sk-grab" aria-hidden />}
      <span className="sk-th">
        Act as · {members.length} {members.length === 1 ? 'workspace' : 'workspaces'} you are a member of
      </span>
      {members.map(row)}
      {reach.length > 0 && (
        // `display: contents`: the group names the rows for a screen reader
        // and leaves them in the list's own column, spaced as the others.
        <div role="group" aria-label={REACH_HEADING} className="sk-treach" style={{ display: 'contents' }}>
          <span className="sk-th">{REACH_HEADING}</span>
          {reach.map(row)}
        </div>
      )}
      <span className="sk-tf">Kept for this tab&rsquo;s session; a new tab opens in your default. Every request names it, and the API checks your membership each time; switching reloads this page&rsquo;s reads.</span>
    </div>
  )
  if (at !== 'sheet') return list
  return (
    <>
      <div className="sk-scrim is-sheet" aria-hidden onClick={onClose} />
      {list}
    </>
  )
}

/**
 * OVERVIEW'S "ON THIS PAGE", ON THE PAGE, ON A PHONE (visual QA run 2,
 * FF-P1, part of #1680). Below 760px the panel that lists Overview's sections
 * is behind the menu, so nothing on the page said they were there and Recent
 * failures was reached by scrolling past every card. The same jumps, as one
 * scrollable row of chips at the top of the page (styles.css `.sk-otp`).
 * Phone only: the desktop's open panel already lists them, and a second list
 * there would be the same eight links twice. A tap is the panel's jump
 * (`onJump`): the hash goes on the address and focus moves to the section.
 */
function PhoneJumps({ onJump }: { onJump: (id: string) => void }) {
  // THE ROW SAYS IT SCROLLS (visual QA run 3, ffp-001, part of #1680). Its
  // scrollbar is hidden, so a row cut at the glass edge read as complete and
  // Recent failures, fifth of eight, was never seen. Each side that has more
  // chips past it is marked and fades (`.sk-otp.is-more-end`, styles.css),
  // read on every scroll and resize; a row that fits is marked on neither.
  const rowRef = useRef<HTMLElement | null>(null)
  const [more, setMore] = useState({ start: false, end: false })
  useEffect(() => {
    const row = rowRef.current
    if (row === null) return
    // THE GUTTER IS NOT A CHIP PAST THE EDGE (visual QA run 4, N2,
    // overview-automate-005, part of #1680). The row starts at the page's
    // 16px gutter as its padding (`.sk-otp`), so the first chip lines up with
    // the cards and the row still scrolls out to the glass edge. A chip is
    // past an edge only once the scroll has gone through that side's padding:
    // a row nudged inside its gutter is still at its start, and fades nothing.
    const read = () => {
      const pad = getComputedStyle(row)
      const inStart = parseFloat(pad.paddingLeft) || 0
      const inEnd = parseFloat(pad.paddingRight) || 0
      const start = row.scrollLeft > inStart + 1
      const end = row.scrollLeft + row.clientWidth < row.scrollWidth - inEnd - 1
      setMore((m) => (m.start === start && m.end === end ? m : { start, end }))
    }
    read()
    row.addEventListener('scroll', read, { passive: true })
    window.addEventListener('resize', read)
    return () => {
      row.removeEventListener('scroll', read)
      window.removeEventListener('resize', read)
    }
  }, [])
  const cls = `sk-otp${more.start ? ' is-more-start' : ''}${more.end ? ' is-more-end' : ''}`
  return (
    <nav className={cls} aria-label="On this page" ref={rowRef}>
      {OVERVIEW_JUMPS.map((j) => (
        <a
          key={j.id}
          className="sk-oj"
          href={`/overview#${j.id}`}
          onClick={(e) => {
            if (!routedClick(e)) return
            e.preventDefault()
            onJump(j.id)
          }}
        >
          {j.label}
        </a>
      ))}
    </nav>
  )
}

/**
 * THE RAIL'S GLOBAL POOL. Three ways to have no figure, and each says which
 * (QA G1-01): `reading…` while the first read is out, `not read` when it
 * failed, and the dash only for a read that landed without a `global` pool --
 * the house legend's "nothing recorded". Loading drew the dash too, so a tab
 * whose read never ran looked like a platform with no pool.
 */
function Meter({ meter, read }: { meter: ReturnType<typeof meterOf>; read: 'loading' | 'unread' | 'landed' }) {
  if (meter === null) {
    return (
      <div className="sk-meter">
        <div className="sk-mh">
          <span>Global pool</span>
          {read === 'loading' ? (
            <b>reading…</b>
          ) : read === 'unread' ? (
            <b>not read</b>
          ) : (
            <b title="The capacity read landed without a global pool.">—</b>
          )}
        </div>
      </div>
    )
  }
  const limit = meter.limit
  const pct = limit !== null && limit > 0 ? Math.min(100, (meter.active / limit) * 100) : 0
  return (
    <div className="sk-meter" title="Slots leased against the global pool's effective ceiling">
      <div className="sk-mh">
        <span>Global pool</span>
        <b>
          {meter.active} / {limit === null ? 'no limit set' : limit}
        </b>
      </div>
      <div className="sk-bar" role="meter" aria-valuenow={meter.active} aria-valuemin={0} aria-valuemax={limit ?? 0} aria-label="Global pool leased">
        <i style={{ width: `${pct}%` }} />
      </div>
      {/* THE POOL'S LABEL IS NEVER BROKEN INSIDE A NAME (visual QA run 2,
          admin-007, part of #1680): `anthropic · u-bogdan` wrapped at the
          tenant id's hyphen, `u-` ending one line and `bogdan` alone on the
          next. Each part between the `·` is one unbreakable span
          (styles.css `.sk-mws`), so the line breaks after `full:` or at the
          dot, never in a provider or tenant id. */}
      {meter.warn !== null && (
        <div className="sk-mw" title={meter.warnPool ?? undefined}>
          {meter.warnLabel === null ? (
            meter.warn
          ) : (
            <>
              {meter.warn.slice(0, meter.warn.length - meter.warnLabel.length)}
              <span className="sk-mwn">
                {meter.warnLabel.split(' · ').map((part, i) => (
                  <Fragment key={i}>
                    {i > 0 && ' · '}
                    <span className="sk-mws">{part}</span>
                  </Fragment>
                ))}
              </span>
            </>
          )}
        </div>
      )}
    </div>
  )
}

function PanelPages({
  section,
  tab,
  agentTab,
  adminPages,
  adminOf = [],
  configPages,
  personal = false,
  nav,
  counts,
  adminCounts = {},
  jumpCounts = null,
  helpGroup,
  apiFailuresOnly,
  onApiFilter,
  missing,
  onJump,
  sections = [],
}: {
  section: SpineSection
  tab: string
  agentTab: AgentTab
  /** The Admin pages this person is shown (`adminPagesFor`); none draws no Admin panel. */
  adminPages: readonly PanelPage[]
  /** The names of the group workspaces the person administers, for a workspace admin's note (S1c). */
  adminOf?: readonly string[]
  /** Config's pages for the workspace in hand (`configPagesFor`). */
  configPages: readonly PanelPage[]
  /** The workspace in hand is the person's own (S1e): Config says why People and roles is missing. */
  personal?: boolean
  nav: (to: string) => void
  counts: Readonly<Record<string, PanelCount>>
  /** The Admin rows' badges (`adminPanelCounts`, S1b, S1c). */
  adminCounts?: Readonly<Record<string, PanelCount>>
  /** Overview's own section counts, by jump id (`OverviewCounts`, VQ-007). */
  jumpCounts?: Readonly<Record<string, PanelCount>> | null
  helpGroup: string | null
  apiFailuresOnly: boolean
  onApiFilter?: (failuresOnly: boolean) => void
  missing: boolean
  /** An Overview jump: scrolls to the section, closes the phone drawer and moves focus there. */
  onJump?: (id: string) => void
  /** The rail's sections (`spineItems`), for a 404 that is in none of them. */
  sections?: readonly { key: Exclude<SpineSection, null>; label: string; to: string }[]
}) {
  // A 404 IN NO SECTION IS OFFERED THE SECTIONS (visual QA pass 2,
  // admin-shell-12; part of #2694). It is lit as no section (`sectionLink`),
  // and the panel under the switcher was empty: no heading, no link, no word.
  // The rail's own list, headed `Go to`, none of it marked current.
  if (section === 'overview' && missing) {
    return (
      <>
        <div className="sk-pt">Go to</div>
        <p className="sk-pnote">This page is not in any section.</p>
        {sections.map((s) => (
          <a key={s.key} className="sk-pk" {...routed(s.to, () => nav(s.to))}>
            <Icon name={s.key} />
            <span className="sk-pl">{s.label}</span>
          </a>
        ))}
      </>
    )
  }
  if (section === 'overview') {
    return (
      <>
        <div className="sk-pt">On this page</div>
        {OVERVIEW_JUMPS.map((j) => (
          <a
            key={j.id}
            className="sk-pk"
            href={`/overview#${j.id}`}
            onClick={(e) => {
              if (!routedClick(e)) return
              e.preventDefault()
              // THE PHONE DRAWER CLOSES ON A JUMP (live QA OV-5, 2026-10-11):
              // the page scrolled behind it and the drawer still covered the
              // card, so the tap looked like it did nothing. A navigation
              // closes it (`nav`); a jump is one too.
              if (onJump === undefined) jumpTo(j.id)
              else onJump(j.id)
            }}
          >
            <span className="sk-pl">{j.label}</span>
            <CountMark c={jumpCounts?.[j.id]} />
          </a>
        ))}
      </>
    )
  }
  if (section === 'help') {
    return (
      <>
        <div className="sk-pt">Help · {TOPIC_IDS.length} topics</div>
        {HELP_GROUPS.map((g) => {
          const n = TOPIC_IDS.filter((id) => HELP[id].group === g.id).length
          const on = helpGroup === g.id
          return (
            <a key={g.id} className={`sk-pk${on ? ' is-on' : ''}`} aria-current={on ? 'page' : undefined} {...routed(`help/${g.id}`, () => nav(`help/${g.id}`))}>
              <span className="sk-pl">{g.title}</span>
              <span className="sk-cnt">{n}</span>
            </a>
          )
        })}
      </>
    )
  }
  // SUBMIT DRAWS ITS OWN PAGES (live QA CH-17): it drew Work's, headed
  // "Work" with nothing lit, while the spine marked Submit -- another
  // section's navigation under the place you are. The chooser and its three
  // forms, the one you are on lit.
  if (section === null) {
    return (
      <>
        <div className="sk-pt">Submit</div>
        {SUBMIT_PAGES.map((p) => {
          const on = !missing && p.tab === tab
          return (
            <a key={p.to} className={`sk-pk${on ? ' is-on' : ''}`} aria-current={on ? 'page' : undefined} {...routed(p.to, () => nav(p.to))}>
              <Icon name={p.icon} />
              <span className="sk-pl">{p.label}</span>
            </a>
          )
        })}
      </>
    )
  }
  const sec = section
  // NO LOCKED ADMIN PANEL (NAV-ADMIN-ROLE): someone who administers nothing,
  // on a direct link to an Admin page, gets the page's own refusal and no
  // list of pages they cannot use.
  const rows = sec === 'admin' ? adminPages : sec === 'config' ? configPages : PANEL_PAGES[sec]
  if (rows.length === 0) return null
  const title = PANEL_TITLE[sec]
  // A 404 is on no page, though its section is drawn (CH-14).
  const lit = missing ? { row: '', kid: null } : litPage(sec, tab, agentTab)
  // The Admin panel's groups (S1b, S1c); every other panel is one group with no heading.
  const groups: { key: string; title: string | null; rows: readonly PanelPage[] }[] = sec === 'admin' ? adminGroups(rows) : [{ key: sec, title: null, rows }]
  const platformAdmin = groups.some((g) => g.key === 'platform')
  return (
    <>
      <div className="sk-pt">{title}</div>
      {/* S1a: the setup strip's count, as a meter under Config's title. */}
      {sec === 'config' && <SetupMeter />}
      {groups.map((g) => (
        <div key={g.key} className="sk-pgrp" role={g.title === null ? undefined : 'group'} aria-label={g.title ?? undefined}>
          {g.title !== null && <div className="sk-pg">{g.title}</div>}
          <PanelRows sec={sec} rows={g.rows} lit={lit} nav={nav} counts={sec === 'admin' ? adminCounts : counts} apiFailuresOnly={apiFailuresOnly} onApiFilter={onApiFilter} />
        </div>
      ))}
      {/* S1e: why a personal workspace's Config has no People and roles. */}
      {sec === 'config' && personal && <p className="sk-pnote">A personal workspace has no People and roles: nobody else is in it.</p>}
      {/* A WORKSPACE ADMIN IS TOLD WHOSE PAGES THESE ARE (S1c), and that the
          platform's are not hidden behind a lock but simply not theirs. */}
      {sec === 'admin' && !platformAdmin && (
        <p className="sk-pnote">
          {/* One workspace is one (visual QA pass 2, admin2-18; part of #2694). */}
          {adminOf.length > 0 ? `Showing ${listed(adminOf)}, the workspace${adminOf.length === 1 ? '' : 's'} you administer. ` : ''}The platform’s own pages are shown to platform admins only.
        </p>
      )}
      {sec === 'admin' && (
        <div className="sk-pgrp" role="group" aria-label="In Automate">
          <div className="sk-pg">In Automate</div>
          {inAutomateLinks(platformAdmin).map((p) => (
            <a key={p.key} className="sk-pk sk-pk-out" {...routed(p.to, () => nav(p.to))}>
              <Icon name={p.icon} />
              <span className="sk-pl">
                {p.label}
                <span aria-hidden="true"> ↗</span>
              </span>
              {p.key === 'schedules' && platformAdmin && <small className="sk-pk-sub">all workspaces</small>}
              {p.key === 'approvals' && <CountMark c={counts.approvals} />}
            </a>
          ))}
        </div>
      )}
    </>
  )
}

/**
 * The own workspace's one name inside a sentence (admin-shell-21): "You are
 * an admin of your workspace.", not "of Your workspace.".
 */
function inSentence(name: string): string {
  return name === OWN_WORKSPACE_LABEL ? 'your workspace' : name
}

/** "a", "a and b", "a, b and c". */
function listed(names: readonly string[]): string {
  return names.length <= 1 ? (names[0] ?? '') : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
}

/** One group's rows in the panel: each page, its children when it is open, and its count or setup badge. */
function PanelRows({
  sec,
  rows,
  lit,
  nav,
  counts,
  apiFailuresOnly,
  onApiFilter,
}: {
  sec: PanelSection
  rows: readonly PanelPage[]
  lit: { row: string; kid: string | null }
  nav: (to: string) => void
  counts: Readonly<Record<string, PanelCount>>
  apiFailuresOnly: boolean
  onApiFilter?: (failuresOnly: boolean) => void
}) {
  // S1a: each Config row's state, from the read the strip draws.
  const badges = useConfigBadges()
  return (
    <>
      {rows.map((p) => {
        const group = p.key === lit.row
        // THE PAGE ITSELF IS LIT (#503). A row with children is a group: it
        // opens its children, and one of them is the page.
        const on = group && p.kids === undefined
        const row = (
          <>
            <Icon name={p.icon} />
            <span className="sk-pl">{p.label}</span>
            {p.admin === true && <span className="sk-adm">admin</span>}
            {sec === 'config' ? <ConfigBadgeMark badge={badges[p.key]} /> : p.kids === undefined && <CountMark c={counts[p.key]} />}
            {p.kids !== undefined && p.key === 'pools' && <CountMark c={counts.pools} />}
          </>
        )
        return (
          <div key={p.key}>
            <a className={`sk-pk${on ? ' is-on' : ''}${group ? ' is-group' : ''}`} aria-current={on ? 'page' : undefined} {...routed(p.to, () => nav(p.to))}>
              {row}
            </a>
            {group && p.kids !== undefined && (
              <div className="sk-kids">
                {p.kids.map((k) => {
                  const kidOn = k.key === lit.kid
                  return (
                    <a
                      key={k.key}
                      className={`sk-kid${kidOn ? ' is-on' : ''}`}
                      aria-current={kidOn ? 'page' : undefined}
                      // Why Recent has no number beside Live and Waiting (V150).
                      title={p.key === 'agents' && k.key === 'recent' ? 'Finished agents, newest first. The list’s tab counts the rows it read; nothing totals every finished agent, so no number is drawn here' : undefined}
                      {...routed(k.to, () => nav(k.to))}
                    >
                      <span>{k.label}</span>
                      {/* AND IN WORDS (visual QA V150, 2026-10-16): beside a tab
                          that says "Recent 17", a bare row read as a count
                          missing. The tab counts the rows read; nothing
                          totals the finished. Hidden from a screen reader,
                          which has the title. SAYS WHAT RECENT IS, NOT WHAT
                          IT LACKS (QA-078, part of #1680): "not totalled"
                          beside a tab saying "Recent 17" read as one of the
                          two being wrong. */}
                      {p.key === 'agents' && k.key === 'recent' && (
                        <small className="sk-knote" aria-hidden="true">
                          newest first
                        </small>
                      )}
                      {k.admin === true && <span className="sk-adm">admin</span>}
                      {p.key === 'agents' && <CountMark c={counts[k.key]} />}
                    </a>
                  )
                })}
              </div>
            )}
            {p.key === 'workflows' && sec === 'work' && <RecentWorkflows nav={nav} />}
            {/* API READS' TWO FILTERS, under the page while it is open
                (NAV-FOOT, #1672): the rows its own panel drew while it was a
                spine item. Two filters of one page, not two pages: buttons,
                pressed or not. */}
            {on && sec === 'capacity' && p.key === API_READS_KEY && (
              <div className="sk-kids" role="group" aria-label="API reads rows">
                <button type="button" className={`sk-kid${apiFailuresOnly ? '' : ' is-on'}`} aria-pressed={!apiFailuresOnly} onClick={() => onApiFilter?.(false)}>
                  <span>Every read this tab made</span>
                </button>
                <button type="button" className={`sk-kid${apiFailuresOnly ? ' is-on' : ''}`} aria-pressed={apiFailuresOnly} onClick={() => onApiFilter?.(true)}>
                  <span>Failures only</span>
                </button>
              </div>
            )}
          </div>
        )
      })}
    </>
  )
}

/**
 * THE WORKFLOWS ROW'S RECENT (5) SWITCHER (owner's pick, 2026-10-01;
 * workflows.html C): the five workflows last opened in this browser, filled
 * up with the list read's newest (#503), each BY NAME with its state mark --
 * the id is its title -- the open one highlighted, then "All workflows →". It
 * costs no read: the marks are the states the workflows were last read in.
 */
function RecentWorkflows({ nav }: { nav: (to: string) => void }) {
  const [items, setItems] = useState<RecentWorkflow[]>(recentWorkflows)
  // A tenant switch is a different list (CH-3): read the new tenant's at once.
  const tenant = useSyncExternalStore(subscribeTenant, chosenTenant, chosenTenant)
  useEffect(() => {
    const again = () => setItems(recentWorkflows())
    again()
    globalThis.addEventListener?.(RECENT_WORKFLOWS_EVENT, again)
    return () => globalThis.removeEventListener?.(RECENT_WORKFLOWS_EVENT, again)
  }, [tenant])
  // THE STATES ARE READ HERE, NOT LEFT TO THE PAGE (QA1 overview-agents-21):
  // the ids no read of this load gave, once each, in one batched read.
  const unread = items.filter((w) => !workflowReadThisLoad(w.id) && !askedThisLoad.has(w.id)).map((w) => w.id)
  const unreadKey = unread.join(' ')
  useEffect(() => {
    if (unreadKey === '') return
    const ids = unreadKey.split(' ')
    for (const id of ids) askedThisLoad.add(id)
    void loadWorkflowsById(ids).then((r) => {
      if (r.status !== 'ok' && r.status !== 'stale') return
      refreshRecentWorkflows([...r.data.reads.values()].map(({ workflow: w }) => ({ id: w.workflow_id, state: derivedStateOf(w), name: null })))
    })
  }, [unreadKey])
  if (items.length === 0) return null
  const open = openWorkflowId()
  return (
    <div className="sk-kids sk-recent" role="group" aria-label="Recent workflows">
      <span className="sk-recent-h">Recent</span>
      {items.map((w) => {
        // A STATE NO READ OF THIS PAGE LOAD GAVE is not drawn as a state
        // (QA G3-04): it is the one the workflow was last opened in, which
        // may be long over. The word says so; the mark is withheld.
        // A NULL STATE IS ONE THE READ DID NOT DERIVE (QA-096, part of
        // #1680): said in the Workflows table's words, `state not derived`,
        // with its amber ring -- `state not recorded` and no mark disagreed
        // with the table about the same row. A withheld mark still keeps its
        // slot (`.sk-recent .sk-st`, styles.css), so every name starts in line.
        // AND THE SLOT IS DRAWN (BT-015, #2423): with no glyph it was 0px
        // tall, so the row's state reached a screen reader only. A withheld
        // mark is a dashed neutral ring (`.is-withheld`, styles.css) -- not a
        // state's glyph -- and its title is the `last seen …` word.
        const current = workflowReadThisLoad(w.id)
        const look: { mark: MarkName | null; hue: MarkHue | 'warn' } | null =
          w.state === null ? (current ? { mark: 'queued', hue: 'warn' } : null) : current ? STATE_MARK[w.state] : null
        const said = w.state === null ? null : w.state.toLowerCase().replace('_', '-')
        const word =
          said === null
            ? current
              ? 'state not derived'
              : 'state not derived when last read; not re-read since this page loaded'
            : current
              ? said
              : `last seen ${said}; not re-read since this page loaded`
        // A LINK, LIKE EVERY OTHER PANEL ROW (#503): a recent workflow opens
        // in a new tab and copies as its own address.
        const to = `work/workflows?wf=${encodeURIComponent(w.id)}`
        return (
          <a
            key={w.id}
            className={`sk-kid sk-recent-kid${w.id === open ? ' is-on' : ''}`}
            aria-current={w.id === open ? 'page' : undefined}
            title={w.name !== null ? `${w.name} · ${w.id}` : w.id}
            {...routed(to, () => nav(to))}
          >
            <span className="sk-recent-row">
              <NamedMark
                mark={look === null ? null : look.mark}
                hue={look === null ? 'neu' : look.hue}
                word={word}
                dataMark={!current ? (said !== null ? 'stale' : undefined) : said === null ? 'unknown' : undefined}
                className={look === null ? 'is-withheld' : undefined}
                bare
              />
              {/* THE WHOLE NAME AND ID ON THE SPAN THAT ELLIPSES (U11a D14):
                  the ellipsis is drawn on this span, so the hover that
                  explains it is too, not only on the link around it. */}
              <span className={RECENT_NAME_CLASS} title={w.name !== null ? `${w.name} · ${w.id}` : w.id}>
                {w.name ?? w.id}
              </span>
            </span>
          </a>
        )
      })}
      <a className={`sk-kid ${RECENT_ALL_CLASS}`} {...routed('work/workflows', () => nav('work/workflows'))}>
        All workflows →
      </a>
    </div>
  )
}

/**
 * The Recent row's name class, BUILT FROM PARTS: written whole, `sk-` and the
 * name after it read as a provider key to the worker's publish scan, which
 * then refuses the diff (owner rule, 2026-10-02).
 */
const RECENT_NAME_CLASS = ['sk', 'recent', 'id'].join('-')
/** The panel's collapse control's classes, built from parts for the same reason. */
const COLLAPSE_CLASS = ['sk-ibtn', ['sk', 'collapse'].join('-')].join(' ')
/** "All workflows →"'s class, built from parts for the same reason. */
const RECENT_ALL_CLASS = ['sk', 'recent', 'all'].join('-')

/** The workflow the address names (`/workflows/<id>[/<pane>]`), or null. */
function openWorkflowId(): string | null {
  const m = /^\/workflows\/([^/?#]+)/.exec(globalThis.location?.pathname ?? '')
  if (m === null || m[1] === undefined) return null
  try {
    return decodeURIComponent(m[1])
  } catch {
    return m[1]
  }
}

function Flyout({
  section,
  tab,
  agentTab,
  adminPages,
  configPages,
  nav,
  onExpand,
  onEnter,
  onLeave,
  flyRef,
  onBlur,
  onKeyDown,
}: {
  section: Exclude<SpineSection, null>
  tab: string
  agentTab: AgentTab
  adminPages: readonly PanelPage[]
  configPages: readonly PanelPage[]
  nav: (to: string) => void
  /** Undo the collapse: the panel comes back and the flyout goes. */
  onExpand: () => void
  onEnter: () => void
  onLeave: () => void
  flyRef: RefObject<HTMLDivElement>
  onBlur: (e: ReactFocusEvent) => void
  onKeyDown: (e: ReactKeyboardEvent) => void
}) {
  if (section !== 'profile' && section !== 'work' && section !== 'automate' && section !== 'capacity' && section !== 'config' && section !== 'admin') return null
  const lit = tab === '' ? { row: '', kid: null } : litPage(section, tab, agentTab)
  const name = PANEL_TITLE[section]
  // The Admin rows in the panel's group order (S1b), so the two never list them differently.
  const rows = section === 'admin' ? adminGroups(adminPages).flatMap((g) => g.rows) : section === 'config' ? configPages : PANEL_PAGES[section]
  if (rows.length === 0) return null
  // A plain labelled group of links, not role="menu": a menu promises arrow
  // keys and roving focus, which this does not have. Tab walks it.
  return (
    <div className={`sk-flyout is-${section}`} ref={flyRef} onMouseEnter={onEnter} onMouseLeave={onLeave} onBlur={onBlur} onKeyDown={onKeyDown} role="group" aria-label={`${name} pages`}>
      <b>{name}</b>
      {rows.map((p) => {
        const on = p.key === lit.row && p.kids === undefined
        return (
          <a key={p.key} className={on ? 'is-on' : p.key === lit.row ? 'is-group' : ''} aria-current={on ? 'page' : undefined} {...routed(p.to, () => nav(p.to))}>
            {p.label}
          </a>
        )
      })}
      {/* A CONTROL, NOT AN INSTRUCTION (V083): "Click the section to open
          the panel again" stopped being true when a click on the section
          became the way to open this list without a pointer that hovers. */}
      <button type="button" className="sk-unfold" onClick={onExpand}>
        Open the panel
      </button>
    </div>
  )
}
