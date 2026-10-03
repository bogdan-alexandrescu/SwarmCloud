/**
 * THE SKY SPINE SHELL (rebrand, owner's pick 2026-10-01; navigation.html
 * Variant 2, spine.py).
 *
 *   SPINE   84px, deep blue (#0b3a7a -> #06101e), THE SAME IN BOTH THEMES. The
 *           Hive in its 44px full cut (white -> #7dd3fc), Submit (the sky
 *           button), Overview, Work, Capacity, Admin; at its foot Help, API
 *           reads and the avatar.
 *   PANEL   236px. The wordmark and the environment pill, the tenant block,
 *           the open section's pages, the capacity meter and the user footer.
 *
 * Collapsing hides the panel and keeps the spine; hovering a spine item then
 * shows a flyout of that section's pages. Below 760px a 44px phone header (in
 * the spine's colour, with the compact seven-dot Hive) opens a drawer that
 * holds both columns.
 *
 * Admin is SHOWN to a non-admin, with a lock: the rows are disabled and one
 * line says admins only. A hidden page is indistinguishable from one that does
 * not exist.
 *
 * Production draws a red pill and a 3px red bar across the top; the pill shows
 * only what was measured (`classifyEnvironment`), never a hardcoded word.
 */
import { Fragment, useCallback, useEffect, useRef, useState, useSyncExternalStore, type FocusEvent as ReactFocusEvent, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent, type ReactNode, type RefObject } from 'react'
import { AGENT_TABS, type AgentTab } from './agentlist'
import { loadCapacity, loadMe, loadMyTenants, loadStats, type TenantChoice } from './api'
import { classifyEnvironment, envTreatment, servedEnvironment, SwarmMark } from './Brand'
import { Banner, Button, NamedMark, Toaster, acknowledge, routedClick } from './components'
import { chooseTenant, chosenTenant, clearTenantSwitch, errorHeading, noteTenantSwitch, probeSnapshot, subscribeProbes, subscribeTenant, subscribeTenantSwitch, tenantSwitchSnapshot, type Result } from './fetch'
import { HELP_GROUPS, HELP, TOPIC_IDS } from './help'
import { STATE_MARK } from './marks'
import { addressToPath } from './paths'
import type { Capacity, Me, Stats, TaskState } from './types'
import { ThemeToggle } from './ThemeToggle'
import { AppTakeover, OfflineBanner, wholeAppFault, useOnline } from './AppStates'

export type SpineSection = 'overview' | 'work' | 'capacity' | 'admin' | 'help' | 'api' | null

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

/**
 * The panel's pages, per spine section (common.py NAV). The `to` values are
 * router addresses, so they resolve exactly as a click on the old rail did.
 */
export const PANEL_PAGES: Readonly<Record<'work' | 'capacity' | 'admin', PanelPage[]>> = {
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
        { key: 'quota', label: 'Provider quota', to: 'capacity/quota', admin: true },
      ],
    },
  ],
  admin: [
    { key: 'limits', label: 'Pool limits', icon: 'admin', to: 'admin/limits' },
    { key: 'tenants', label: 'Tenants', icon: 'tenants', to: 'admin/tenants' },
    { key: 'counts', label: 'Platform counts', icon: 'counts', to: 'admin/counts' },
  ],
}

/** Which panel row a route's tab belongs to: a child's tab belongs to its parent. */
function rowFor(section: 'work' | 'capacity' | 'admin', tab: string): string {
  if (section === 'work' && (tab === 'running' || tab === '')) return 'agents'
  if (section === 'capacity' && tab === 'profiles') return 'pools'
  if (section === 'capacity' && tab === 'quota') return 'accounts'
  return tab
}

/**
 * THE PAGE THE PANEL LIGHTS IS THE PAGE ITSELF (#503): on /agents/live it is
 * Live, not Agents. A row with children is a group, and a group is never the
 * page -- one of its children is. `agentTab` is the Agents list's own tab,
 * which the route's tab (`running`) does not carry.
 */
export function litPage(section: 'work' | 'capacity' | 'admin', tab: string, agentTab: AgentTab): { row: string; kid: string | null } {
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

/** The Overview page's regions, as jump links (overview.html O1). */
export const OVERVIEW_JUMPS: readonly { id: string; label: string }[] = [
  { id: 'ov-needs', label: 'Needs a look' },
  { id: 'ov-band', label: 'Waiting, working, done' },
  { id: 'ov-running', label: 'Running now' },
  { id: 'ov-waiting', label: 'Waiting, and why' },
  { id: 'ov-headroom', label: 'Headroom' },
  { id: 'ov-failures', label: 'Recent failures' },
]

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

/** The workflows opened in this browser, newest first: what `rememberWorkflow` keeps. */
function openedWorkflows(): RecentWorkflow[] {
  return readRecent(RECENT_WORKFLOWS_KEY)
}

/**
 * RECENT (5) AS THE SWITCHER DRAWS IT (workflows.html C; #503): the workflows
 * opened in this browser first, then the list read's newest to fill five, so
 * the list page shows the switcher before anything was opened. Each carries
 * the freshest state and name either store holds for it.
 */
export function recentWorkflows(): RecentWorkflow[] {
  const newest = readRecent(NEWEST_WORKFLOWS_KEY)
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

/** Put a workflow first in Recent (5), with the state and name it was read with. */
export function rememberWorkflow(id: string, state: TaskState | null = null, name: string | null = null): void {
  const before = openedWorkflows()
  const next = [{ id, state, name }, ...before.filter((x) => x.id !== id)].slice(0, 5)
  if (JSON.stringify(next) === JSON.stringify(before)) return
  writePref(RECENT_WORKFLOWS_KEY, JSON.stringify(next))
  announce()
}

/**
 * THE LIST READ'S NEWEST FIVE, given newest first: what fills Recent (5)
 * behind the opened ones. It costs no read -- the list already made it -- and
 * it refreshes the state and name of an opened workflow the read holds.
 */
export function offerNewestWorkflows(newest: readonly RecentWorkflow[]): void {
  const next = newest.slice(0, 5).map((w) => ({ id: w.id, state: w.state, name: w.name }))
  if (readPref(NEWEST_WORKFLOWS_KEY) === JSON.stringify(next)) return
  writePref(NEWEST_WORKFLOWS_KEY, JSON.stringify(next))
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

function useFrameRead<T>(load: () => Promise<Result<T>>, everyMs: number | null, rereadOn = '', retry = false): [Result<T>, () => void] {
  const [r, setR] = useState<Result<T>>({ status: 'loading', since: Date.now() })
  const [nonce, setNonce] = useState(0)
  useEffect(() => {
    let live = true
    let timer: ReturnType<typeof setTimeout> | undefined
    let failures = 0
    const tick = () => {
      // Paused in a hidden tab; picked up again on the next tick.
      if (typeof document !== 'undefined' && document.hidden && everyMs !== null) {
        timer = setTimeout(tick, everyMs)
        return
      }
      load().then((next) => {
        if (!live) return
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
  return [r, again]
}

const loadFrameMe = () => loadMe({ frame: true })
const loadFrameTenants = () => loadMyTenants()

/**
 * WHAT SITS OVER THE PAGE AND OWNS THE KEYBOARD WHILE IT IS OPEN, for the N
 * key. `aside.adm-side` is Pool limits' side editor (AdminSettings.tsx), which
 * is not a dialog but holds a draft and a typed confirmation N must not drop.
 */
const OVER_THE_PAGE = '[role="dialog"], [aria-modal="true"], aside.adm-side'
const loadFrameCapacity = () => loadCapacity({ frame: true })
const loadFrameStats = () => loadStats({ frame: true })

function dataOf<T>(r: Result<T>): T | null {
  return r.status === 'ok' || r.status === 'stale' ? r.data : null
}

/** The global pool for the meter, and the first pool that is full or paused. */
export function meterOf(c: Capacity | null): {
  active: number
  /** null: the global pool has no limit set (#374), which is not 0. */
  limit: number | null
  warn: string | null
} | null {
  if (c === null) return null
  const g = c.pools.find((p) => p.name === 'global')
  if (g === undefined) return null
  const hot = c.pools.find((p) => p.enabled === false || (p.effective_limit !== null && p.effective_limit > 0 && p.available !== null && p.available <= 0))
  const warn = hot === undefined ? null : `${hot.name} ${hot.enabled === false ? 'paused' : 'full'}`
  return { active: g.active, limit: g.effective_limit, warn }
}

/** One count beside a panel row: a measurement, or a dash with its reason. */
export interface PanelCount {
  /** null is UNKNOWN, drawn as a dash -- never a 0. */
  n: number | null
  why?: string
  /** An amber count: something on that page needs a look. */
  alert?: boolean
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
 *   Workflows      NOT SERVED: no route counts a tenant's workflows (the list
 *                  read pages 100 with a rollup budget, far too heavy for the
 *                  frame), so the row carries a dash and says so.
 *   Pools          the amber count of pools full or paused, derived from the
 *                  capacity read the meter already made; nothing when none is.
 *
 * A failed read is a dash with the failure as its reason, never a 0.
 */
export function panelCounts(stats: Result<Stats>, cap: Result<Capacity>): Readonly<Record<string, PanelCount>> {
  const out: Record<string, PanelCount> = {}
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
  out.workflows = { n: null, why: 'not served: no route counts this tenant’s workflows' }
  const c = dataOf(cap)
  if (c === null) {
    if (cap.status === 'error') out.pools = { n: null, why: `not read: ${errorHeading(cap.error)}` }
  } else {
    const hot = c.pools.filter((p) => p.enabled === false || (p.effective_limit !== null && p.effective_limit > 0 && p.available !== null && p.available <= 0)).length
    if (hot > 0) out.pools = { n: hot, alert: true, why: `${hot} pool${hot === 1 ? '' : 's'} full or paused` }
  }
  return out
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
    <span className={`sk-cnt${c.alert === true ? ' is-alert' : ''}`} title={c.why}>
      {c.n}
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
  admin: (
    <>
      <path d="M4 7h10M18 7h2M4 17h4M12 17h8" />
      <circle cx="16" cy="7" r="2" />
      <circle cx="10" cy="17" r="2" />
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
  swap: <path d="m7 9 5-5 5 5M7 15l5 5 5-5" />,
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

const SPINE: readonly { key: Exclude<SpineSection, null>; label: string; to: string }[] = [
  { key: 'overview', label: 'Overview', to: 'overview/now' },
  { key: 'work', label: 'Work', to: 'work/running' },
  { key: 'capacity', label: 'Capacity', to: 'capacity/pools' },
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
  go: (to: string) => void
  /** Help's search, owned by the Help page through the route. */
  helpGroup?: string | null
  /** API reads' filter. */
  apiFailuresOnly?: boolean
  onApiFilter?: (failuresOnly: boolean) => void
  /** The spine's foot: Help and API reads, drawn by App (see `ctl-nav-util`). */
  foot: ReactNode
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
  foot,
  dock,
  keepOnSwitch = false,
  children,
}: ShellProps) {
  // THE CHOSEN TENANT (fetch.ts), null for the default. Every read sends it;
  // a change re-reads the frame and remounts the page below so its reads run
  // again under the new tenant rather than showing the old one's rows -- all
  // but an unsent form (`keepOnSwitch`), which is kept.
  const tenant = useSyncExternalStore(subscribeTenant, chosenTenant, chosenTenant)
  const [me, rereadMe] = useFrameRead(loadFrameMe, null, tenant ?? '', true)
  const [cap] = useFrameRead(loadFrameCapacity, 30_000, tenant ?? '')
  const [stats] = useFrameRead(loadFrameStats, 30_000, tenant ?? '')
  const [mine, rereadMine] = useFrameRead(loadFrameTenants, null)
  const who = dataOf(me)
  const admin = who?.principal.is_admin === true
  const env = classifyEnvironment(
    import.meta.env.VITE_SWARM_ENV,
    typeof window === 'undefined' ? '' : window.location.hostname,
    who === null ? null : servedEnvironment(who),
  )
  const t = envTreatment(env)
  const meter = meterOf(dataOf(cap))
  const counts = panelCounts(stats, cap)
  const online = useOnline()
  const probes = useSyncExternalStore(subscribeProbes, probeSnapshot, probeSnapshot)
  const fault = wholeAppFault(me, probes)
  // A retry from the takeover re-reads the frame AND the page under it.
  const [attempt, setAttempt] = useState(0)

  const [collapsed, setCollapsed] = useState(() => readPref(COLLAPSED_KEY) === '1')
  const [drawer, setDrawer] = useState(false)
  const [fly, setFly] = useState<Exclude<SpineSection, null> | null>(null)
  /** Where the tenant list is open: beside the panel block, beside the spine's tile, or the phone's sheet. */
  const [picker, setPicker] = useState<TenantPickerAt | null>(null)
  const flyTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const sideRef = useRef<HTMLDivElement | null>(null)
  const flyRef = useRef<HTMLDivElement>(null)
  const openerRef = useRef<HTMLButtonElement | null>(null)
  const appRef = useRef<HTMLDivElement | null>(null)
  const scrollerRef = useRef<HTMLDivElement | null>(null)
  const [scroll, setScroll] = useState<PageScroll>('top')

  // THE PHONE HEADER GIVES BACK ITS HEIGHT ON SCROLL, AND A LONG PAGE GETS A
  // WAY BACK UP (#139). The scroller is the content column's `.ctl-scroll`:
  // the spine and the panel sit beside it, the dock under it. Read on every
  // scroll event, which is a comparison and a state write that React drops
  // when the band has not changed.
  useEffect(() => {
    const scroller = scrollerRef.current
    if (scroller === null) return
    const onScroll = () => setScroll(scrollBand(scroller.scrollTop, scroller.clientHeight))
    onScroll()
    scroller.addEventListener('scroll', onScroll, { passive: true })
    return () => scroller.removeEventListener('scroll', onScroll)
  }, [])
  const toTop = () => {
    const scroller = scrollerRef.current
    if (scroller === null) return
    scroller.scrollTop = 0
    setScroll('top')
  }

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
      openerRef.current?.focus()
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
    if (refocus && was !== null) sideRef.current?.querySelector<HTMLElement>(`[data-sec="${was}"]`)?.focus()
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

  const toggle = useCallback(() => {
    setCollapsed((c) => {
      writePref(COLLAPSED_KEY, c ? null : '1')
      return !c
    })
  }, [])

  // A navigation closes the phone drawer, any flyout and the tenant list.
  const nav = useCallback(
    (to: string) => {
      setDrawer(false)
      setFly(null)
      setPicker(null)
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
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'n' && e.key !== 'N') return
      if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return
      const el = e.target instanceof Element ? e.target : null
      if (el !== null && el.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"])') !== null) return
      if (el instanceof HTMLElement && el.isContentEditable) return
      // Nor while something sits over the page: a modal, an open drawer or
      // pinned help card (`role=dialog`), or Pool limits' side editor. It owns
      // the keyboard, and N there navigating away drops what was being edited.
      if (document.querySelector(OVER_THE_PAGE) !== null) return
      e.preventDefault()
      nav('submit')
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
  const choices = dataOf(mine) ?? []
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
      const from = { id: who.tenant.tenant_id, name: who.tenant.display_name ?? who.tenant.tenant_id }
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
    [who, keepOnSwitch],
  )

  const spine = (
    <nav className="sk-spine" aria-label="Sections" onBlur={onFlyBlur} onKeyDown={onFlyKey}>
      <a className="sk-hive" {...routed('overview/now', () => nav('overview/now'))}>
        <SwarmMark size={44} paint="sky" title="SwarmCloud" />
      </a>
      {/* THE COLLAPSED SPINE KEEPS THE TENANT IN SIGHT (2A): a tile under the
          Hive, which opens the same list beside the spine. */}
      {collapsed && !drawer && <TenantTile me={me} switchable={switchable} open={picker === 'tile'} onOpen={() => setPicker(picker === 'tile' ? null : 'tile')} />}
      <a className="sk-ri sk-cta" title="Submit (N)" aria-current={section === null ? 'page' : undefined} {...routed('submit', () => nav('submit'))}>
        <Icon name="submit" />
        <small>Submit</small>
      </a>
      <span className="sk-rsep" aria-hidden />
      {SPINE.map((s) => {
        const on = section === s.key
        // LOCKED UNTIL THE READ SAYS ADMIN. Drawn unlocked while `who` was
        // unread, a non-admin saw Admin open and then saw the lock appear.
        const locked = s.key === 'admin' && !admin
        const checking = locked && who === null
        // A LINK, NOT A BUTTON (#503): a section opens in a new tab and copies
        // as a link, like every other place in the console.
        return (
          <a
            key={s.key}
            data-sec={s.key}
            className={`sk-ri${on ? ' is-on' : ''}`}
            aria-current={on ? 'page' : undefined}
            title={checking ? `${s.label} (checking access)` : locked ? `${s.label} (admins only)` : s.label}
            {...routed(s.to, () => {
              // Collapsed, a click on a section opens the panel again.
              if (collapsed) toggle()
              if (!(collapsed && on)) nav(s.to)
            })}
            onMouseEnter={() => hover(s.key)}
            onMouseLeave={() => hover(null)}
            onFocus={() => hover(s.key)}
          >
            <Icon name={s.key} />
            <small>{s.label}</small>
            {s.key === 'capacity' && meter?.warn != null && <i className="sk-dot" title={meter.warn} />}
            {locked && <Icon name="lock" className="sk-ic sk-lkd" />}
          </a>
        )
      })}
      <span className="sk-grow" />
      {foot}
      <span className="sk-av" title={who?.principal.email ?? 'not read'} aria-hidden>
        {initials(who?.principal.email ?? '')}
        {fault !== null && <i className="sk-av-dot" />}
      </span>
    </nav>
  )

  const pages = (
    <PanelPages
      section={section}
      tab={tab}
      agentTab={agentTab}
      admin={admin}
      known={who !== null}
      nav={nav}
      counts={counts}
      helpGroup={helpGroup}
      apiFailuresOnly={apiFailuresOnly}
      onApiFilter={onApiFilter}
    />
  )

  const panel = (
    <nav className="sk-panel" aria-label="Pages">
      <div className="sk-ph">
        <span className="sk-wm">
          Swarm<span>Cloud</span>
        </span>
        <EnvPill label={t.label} prod={t.bar} title={t.explain} />
        <button type="button" className="sk-ibtn sk-collapse" aria-label="Collapse the panel" title="Collapse the panel" onClick={toggle}>
          <Icon name="collapse" />
        </button>
      </div>
      <TenantBlock me={me} mine={mine} onRetryList={rereadMine} open={picker === 'panel'} onOpen={() => setPicker(picker === 'panel' ? null : 'panel')} />
      <div className="sk-pscroll">{pages}</div>
      <Meter meter={meter} unread={cap.status === 'error' || fault !== null} />
      <div className="sk-pfoot">
        <span className="sk-pfrow">
          <b>{who === null ? (me.status === 'loading' ? 'reading…' : 'not read') : who.principal.email.split('@')[0]}</b>
          {admin && <span className="sk-adm">admin</span>}
        </span>
        {who !== null && <small>{who.principal.email}</small>}
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

  return (
    <div
      ref={appRef}
      className={`sk-app${collapsed ? ' is-collapsed' : ''}${drawer ? ' has-drawer' : ''}${scroll === 'top' ? '' : ' is-scrolled'}`}
      data-env={env.kind}
    >
      {t.bar && <div className="sk-prodbar" aria-hidden />}
      <header className="sk-pbar">
        <button type="button" className="sk-ibtn" aria-label="Open the menu" aria-expanded={drawer} ref={openerRef} onClick={() => setDrawer(true)}>
          <Icon name="menu" />
        </button>
        <SwarmMark size={22} paint="sky" />
        <b>{title}</b>
        {/* THE PHONE'S TENANT IS A CHIP that opens a bottom sheet (2A), for
            someone who can switch; a plain label for everyone else. */}
        {who !== null &&
          (switchable ? (
            <button type="button" className="sk-tchip" aria-haspopup="dialog" aria-expanded={picker === 'sheet'} onClick={() => setPicker('sheet')}>
              {who.tenant.display_name ?? who.tenant.tenant_id}
              <Icon name="swap" />
            </button>
          ) : (
            <span className="sk-tn">{who.tenant.display_name ?? who.tenant.tenant_id}</span>
          ))}
        <EnvPill label={t.label} prod={t.bar} title={t.explain} mini />
      </header>
      {drawer && <div className="sk-scrim" onClick={() => setDrawer(false)} aria-hidden />}
      <div className="sk-side" ref={sideRef}>
        {spine}
        {!collapsed || drawer ? panel : null}
      </div>
      {collapsed && fly !== null && !drawer && (
        <Flyout section={fly} tab={section === fly ? tab : ''} agentTab={agentTab} nav={nav} onEnter={() => hover(fly)} onLeave={() => hover(null)} flyRef={flyRef} onBlur={onFlyBlur} onKeyDown={onFlyKey} />
      )}
      {picker !== null && who !== null && switchable && (
        <TenantPicker at={picker} current={who.tenant.tenant_id} choices={choices} onChoose={choose} onClose={() => setPicker(null)} />
      )}
      <div className="sk-main">
        <div className="ctl-scroll" ref={scrollerRef}>
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
              {keptBanner}
              {/* Keyed on the chosen tenant: a switch is a fresh screen, every
                  read of it made again with the new X-Swarm-Tenant -- except
                  a kept form, which re-reads in place. */}
              <Fragment key={`${keepOnSwitch ? 'kept' : (tenant ?? '')}:${attempt}`}>{children}</Fragment>
            </>
          )}
          {/* A WAY BACK UP, once the page is more than a screen long and the
              reader is past the first screen of it (#139). Drawn below 760px
              only (styles.css): above it the spine never scrolls away. */}
          {scroll === 'far' && (
            <button type="button" className="sk-top" onClick={toTop}>
              &#8593; Top
            </button>
          )}
        </div>
        {dock}
      </div>
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
}: {
  me: Result<Me>
  mine: Result<TenantChoice[]>
  onRetryList: () => void
  open: boolean
  onOpen: () => void
}) {
  const [said, setSaid] = useState('')
  const who = dataOf(me)
  if (who === null) {
    return (
      <div className="sk-tenant" role="status">
        <span className="sk-tg">·</span>
        <span className="sk-tt">
          <b>{me.status === 'loading' ? 'reading…' : 'not read'}</b>
          <small>tenant</small>
        </span>
      </div>
    )
  }
  const id = who.tenant.tenant_id
  const copy = () => {
    const c = typeof navigator === 'undefined' ? undefined : navigator.clipboard
    if (c === undefined) return setSaid('copy refused; select the id instead')
    c.writeText(id).then(
      () => setSaid('tenant id copied'),
      () => setSaid('copy refused; select the id instead'),
    )
  }
  const name = who.tenant.display_name ?? id
  const choices = dataOf(mine) ?? []
  const copyId = (
    <>
      <button type="button" className="sk-cp" aria-label={`Copy tenant id ${id}`} title={id} onClick={copy}>
        copy id
      </button>
      <span className="sk-said" role="status">
        {said}
      </span>
    </>
  )
  if (choices.length > 1) {
    const at = choices.findIndex((c) => c.tenant_id === id)
    return (
      <div className={`sk-tenant is-sw${open ? ' is-open' : ''}`}>
        <button type="button" className="sk-tsw" aria-haspopup="dialog" aria-expanded={open} title={id} onClick={onOpen}>
          <span className="sk-tg" aria-hidden>
            {initialOf(name)}
          </span>
          <span className="sk-tt">
            <b>{name}</b>
            <small>tenant · {at === -1 ? 'not in the list' : `${at + 1} of ${choices.length}`}</small>
          </span>
          <Icon name="swap" className="sk-ic sk-car" />
        </button>
        <span className="sk-tcopy">{copyId}</span>
      </div>
    )
  }
  return (
    <div className="sk-tenant">
      <span className="sk-tg" aria-hidden>
        {initialOf(name)}
      </span>
      <span className="sk-tt">
        <b title={id}>{name}</b>
        <small>
          {mine.status === 'error' ? (
            <>
              tenant · others not read{' '}
              <button type="button" className="sk-tretry" onClick={onRetryList}>
                retry
              </button>
            </>
          ) : (
            <>tenant </>
          )}
          {copyId}
        </small>
      </span>
    </div>
  )
}

/** The collapsed spine's tenant tile: the initial and the name, opening the list beside the spine. */
function TenantTile({ me, switchable, open, onOpen }: { me: Result<Me>; switchable: boolean; open: boolean; onOpen: () => void }) {
  const who = dataOf(me)
  const name = who === null ? (me.status === 'loading' ? 'reading…' : 'not read') : (who.tenant.display_name ?? who.tenant.tenant_id)
  const body = (
    <>
      {who === null ? '·' : initialOf(name)}
      <small>{name}</small>
      {switchable && <Icon name="swap" className="sk-ic sk-tswap" />}
    </>
  )
  if (!switchable) {
    return (
      <span className="sk-ttile" title={who === null ? name : `tenant ${who.tenant.tenant_id}`}>
        {body}
      </span>
    )
  }
  return (
    <button type="button" className="sk-ttile" aria-haspopup="dialog" aria-expanded={open} aria-label={`Tenant ${name}: switch`} onClick={onOpen}>
      {body}
    </button>
  )
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
  choices: readonly TenantChoice[]
  onChoose: (c: TenantChoice) => void
  onClose: () => void
}) {
  const box = useRef<HTMLDivElement | null>(null)
  const close = useRef(onClose)
  close.current = onClose
  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    box.current?.querySelector<HTMLElement>('button[aria-current="true"], button')?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
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
    document.addEventListener('keydown', onKey)
    document.addEventListener('pointerdown', onDown)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('pointerdown', onDown)
      opener?.focus()
    }
  }, [])
  const list = (
    <div className={`sk-tpop is-${at}`} role="dialog" aria-modal={at === 'sheet' ? true : undefined} aria-label="Act as" ref={box}>
      {at === 'sheet' && <span className="sk-grab" aria-hidden />}
      <span className="sk-th">
        Act as · {choices.length} tenants you are a member of
      </span>
      {choices.map((c) => {
        const on = c.tenant_id === current
        return (
          <button key={c.tenant_id} type="button" className={`sk-to${on ? ' is-on' : ''}`} aria-current={on ? 'true' : undefined} onClick={() => onChoose(c)}>
            <span className="sk-tg" aria-hidden>
              {initialOf(c.display_name)}
            </span>
            <b>{c.display_name}</b>
            {on && <span className="sk-ck">current</span>}
            <small>{c.tenant_id}</small>
          </button>
        )
      })}
      <span className="sk-tf">Remembered in this browser. Every request names it, and the API checks your membership each time; switching reloads this page&rsquo;s reads.</span>
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

function Meter({ meter, unread }: { meter: ReturnType<typeof meterOf>; unread: boolean }) {
  if (meter === null) {
    return (
      <div className="sk-meter">
        <div className="sk-mh">
          <span>Global pool</span>
          <b>{unread ? 'not read' : '—'}</b>
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
      {meter.warn !== null && <div className="sk-mw">{meter.warn}</div>}
    </div>
  )
}

function PanelPages({
  section,
  tab,
  agentTab,
  admin,
  known,
  nav,
  counts,
  helpGroup,
  apiFailuresOnly,
  onApiFilter,
}: {
  section: SpineSection
  tab: string
  agentTab: AgentTab
  admin: boolean
  known: boolean
  nav: (to: string) => void
  counts: Readonly<Record<string, PanelCount>>
  helpGroup: string | null
  apiFailuresOnly: boolean
  onApiFilter?: (failuresOnly: boolean) => void
}) {
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
              document.getElementById(j.id)?.scrollIntoView?.({ block: 'start', behavior: 'smooth' })
            }}
          >
            <span className="sk-pl">{j.label}</span>
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
  if (section === 'api') {
    // Two filters of one page, not two pages: buttons, pressed or not.
    return (
      <>
        <div className="sk-pt">API reads</div>
        <button type="button" className={`sk-pk${apiFailuresOnly ? '' : ' is-on'}`} aria-pressed={!apiFailuresOnly} onClick={() => onApiFilter?.(false)}>
          <span className="sk-pl">Every read this tab made</span>
        </button>
        <button type="button" className={`sk-pk${apiFailuresOnly ? ' is-on' : ''}`} aria-pressed={apiFailuresOnly} onClick={() => onApiFilter?.(true)}>
          <span className="sk-pl">Failures only</span>
        </button>
      </>
    )
  }
  // Submit is work: its pages are Work's, with nothing lit.
  const sec = section ?? 'work'
  // The lock is drawn until the session read says admin (as on the spine);
  // the rows are disabled, and "admins only" said, only once it says not.
  const shut = sec === 'admin' && !admin
  const locked = shut && known
  const title = sec === 'work' ? 'Work' : sec === 'capacity' ? 'Capacity' : 'Admin'
  const lit = section === null ? { row: '', kid: null } : litPage(sec, tab, agentTab)
  return (
    <>
      <div className="sk-pt">
        {title}
        {shut && <Icon name="lock" className="sk-ic sk-lk" />}
      </div>
      {PANEL_PAGES[sec].map((p) => {
        const group = p.key === lit.row
        // THE PAGE ITSELF IS LIT (#503). A row with children is a group: it
        // opens its children, and one of them is the page.
        const on = group && p.kids === undefined
        const row = (
          <>
            <Icon name={p.icon} />
            <span className="sk-pl">{p.label}</span>
            {p.admin === true && <span className="sk-adm">admin</span>}
            {p.kids === undefined && <CountMark c={counts[p.key]} />}
            {p.kids !== undefined && p.key === 'pools' && <CountMark c={counts.pools} />}
            {shut && <Icon name="lock" className="sk-ic sk-lk" />}
          </>
        )
        return (
          <div key={p.key}>
            {locked ? (
              // A disabled page is not a link anywhere: it says why below.
              <span className="sk-pk is-dis" aria-disabled="true">
                {row}
              </span>
            ) : (
              <a className={`sk-pk${on ? ' is-on' : ''}${group ? ' is-group' : ''}`} aria-current={on ? 'page' : undefined} {...routed(p.to, () => nav(p.to))}>
                {row}
              </a>
            )}
            {group && p.kids !== undefined && (
              <div className="sk-kids">
                {p.kids.map((k) => {
                  const kidOn = k.key === lit.kid
                  return (
                    <a key={k.key} className={`sk-kid${kidOn ? ' is-on' : ''}`} aria-current={kidOn ? 'page' : undefined} {...routed(k.to, () => nav(k.to))}>
                      <span>{k.label}</span>
                      {k.admin === true && <span className="sk-adm">admin</span>}
                      {p.key === 'agents' && <CountMark c={counts[k.key]} />}
                    </a>
                  )
                })}
              </div>
            )}
            {p.key === 'workflows' && sec === 'work' && <RecentWorkflows nav={nav} />}
          </div>
        )
      })}
      {locked && (
        <p className="sk-pnote">
          <Icon name="lock" className="sk-ic sk-lk" />
          <span>Admins only. You can see these pages exist; changing a ceiling or a tenant needs the platform admin group.</span>
        </p>
      )}
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
  useEffect(() => {
    const again = () => setItems(recentWorkflows())
    globalThis.addEventListener?.(RECENT_WORKFLOWS_EVENT, again)
    return () => globalThis.removeEventListener?.(RECENT_WORKFLOWS_EVENT, again)
  }, [])
  if (items.length === 0) return null
  const open = openWorkflowId()
  return (
    <div className="sk-kids sk-recent" role="group" aria-label="Recent workflows">
      <span className="sk-recent-h">Recent</span>
      {items.map((w) => {
        const look = w.state === null ? null : STATE_MARK[w.state]
        const word = w.state === null ? 'state not recorded' : w.state.toLowerCase().replace('_', '-')
        return (
          <button
            key={w.id}
            type="button"
            className={`sk-kid sk-recent-kid${w.id === open ? ' is-on' : ''}`}
            aria-current={w.id === open ? 'page' : undefined}
            title={w.id}
            onClick={() => nav(`work/workflows?wf=${encodeURIComponent(w.id)}`)}
          >
            <span className="sk-recent-row">
              <NamedMark mark={look === null ? null : look.mark} hue={look === null ? 'neu' : look.hue} word={word} bare />
              <span className={RECENT_NAME_CLASS}>{w.name ?? w.id}</span>
            </span>
          </button>
        )
      })}
      <button type="button" className="sk-kid sk-recent-all" onClick={() => nav('work/workflows')}>
        All workflows →
      </button>
    </div>
  )
}

/**
 * The Recent row's name class, BUILT FROM PARTS: written whole, `sk-` and the
 * name after it read as a provider key to the worker's publish scan, which
 * then refuses the diff (owner rule, 2026-10-02).
 */
const RECENT_NAME_CLASS = ['sk', 'recent', 'id'].join('-')

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
  nav,
  onEnter,
  onLeave,
  flyRef,
  onBlur,
  onKeyDown,
}: {
  section: Exclude<SpineSection, null>
  tab: string
  agentTab: AgentTab
  nav: (to: string) => void
  onEnter: () => void
  onLeave: () => void
  flyRef: RefObject<HTMLDivElement>
  onBlur: (e: ReactFocusEvent) => void
  onKeyDown: (e: ReactKeyboardEvent) => void
}) {
  if (section !== 'work' && section !== 'capacity' && section !== 'admin') return null
  const lit = tab === '' ? { row: '', kid: null } : litPage(section, tab, agentTab)
  const name = section === 'work' ? 'Work' : section === 'capacity' ? 'Capacity' : 'Admin'
  // A plain labelled group of links, not role="menu": a menu promises arrow
  // keys and roving focus, which this does not have. Tab walks it.
  return (
    <div className={`sk-flyout is-${section}`} ref={flyRef} onMouseEnter={onEnter} onMouseLeave={onLeave} onBlur={onBlur} onKeyDown={onKeyDown} role="group" aria-label={`${name} pages`}>
      <b>{name}</b>
      {PANEL_PAGES[section].map((p) => {
        const on = p.key === lit.row && p.kids === undefined
        return (
          <a key={p.key} className={on ? 'is-on' : p.key === lit.row ? 'is-group' : ''} aria-current={on ? 'page' : undefined} {...routed(p.to, () => nav(p.to))}>
            {p.label}
          </a>
        )
      })}
      <em>Click the section to open the panel again</em>
    </div>
  )
}
