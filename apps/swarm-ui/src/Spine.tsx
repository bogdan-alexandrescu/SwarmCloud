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
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { AGENT_TABS, type AgentTab } from './agentlist'
import { loadCapacity, loadMe } from './api'
import { classifyEnvironment, envTreatment, servedEnvironment, SwarmMark } from './Brand'
import type { Result } from './fetch'
import { HELP_GROUPS, HELP, TOPIC_IDS } from './help'
import { MarkGlyph, STATE_MARK } from './marks'
import type { Capacity, Me, TaskState } from './types'

export type SpineSection = 'overview' | 'work' | 'capacity' | 'admin' | 'help' | 'api' | null

/** One page in the panel: its label, the address `go()` takes, and its children. */
interface PanelPage {
  key: string
  label: string
  to: string
  kids?: { key: string; label: string; to: string }[]
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
      to: 'work/running',
      kids: AGENT_TABS.map((t) => ({ key: t, label: AGENT_LABEL[t], to: `work/running/${t}` })),
    },
    { key: 'workflows', label: 'Workflows', to: 'work/workflows' },
    { key: 'timeline', label: 'Timeline', to: 'work/timeline' },
  ],
  capacity: [
    {
      key: 'pools',
      label: 'Pools',
      to: 'capacity/pools',
      kids: [
        { key: 'pools', label: 'Ceilings', to: 'capacity/pools' },
        { key: 'profiles', label: 'By runner profile', to: 'capacity/profiles' },
      ],
    },
    { key: 'catalogue', label: 'Runtimes', to: 'capacity/catalogue' },
    { key: 'holders', label: 'Holders', to: 'capacity/holders' },
    {
      key: 'accounts',
      label: 'Accounts',
      to: 'capacity/accounts',
      kids: [
        { key: 'accounts', label: 'Subscription accounts', to: 'capacity/accounts' },
        { key: 'quota', label: 'Provider quota', to: 'capacity/quota' },
      ],
    },
  ],
  admin: [
    { key: 'limits', label: 'Pool limits', to: 'admin/limits' },
    { key: 'tenants', label: 'Tenants', to: 'admin/tenants' },
    { key: 'counts', label: 'Platform counts', to: 'admin/counts' },
  ],
}

/** Which panel row a route's tab lights: a child's tab lights its parent. */
function rowFor(section: 'work' | 'capacity' | 'admin', tab: string): string {
  if (section === 'work' && (tab === 'running' || tab === '')) return 'agents'
  if (section === 'capacity' && tab === 'profiles') return 'pools'
  if (section === 'capacity' && tab === 'quota') return 'accounts'
  return tab
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
 * One remembered workflow: its id and the state it was last read in (null when
 * that read derived none), so the switcher draws its mark without a read of
 * its own. Remembered PER BROWSER: there is no per-person store in the API yet
 * (workflows.html F, "still open").
 */
export interface RecentWorkflow {
  readonly id: string
  readonly state: TaskState | null
}

function isTaskState(v: unknown): v is TaskState {
  return typeof v === 'string' && Object.prototype.hasOwnProperty.call(STATE_MARK, v)
}

export function recentWorkflows(): RecentWorkflow[] {
  const raw = readPref(RECENT_WORKFLOWS_KEY)
  if (raw === null) return []
  try {
    const v: unknown = JSON.parse(raw)
    if (!Array.isArray(v)) return []
    const out: RecentWorkflow[] = []
    for (const x of v) {
      // The first form of this store held bare ids; they still open, unmarked.
      if (typeof x === 'string') out.push({ id: x, state: null })
      else if (typeof x === 'object' && x !== null && typeof (x as { id?: unknown }).id === 'string') {
        const st = (x as { state?: unknown }).state
        out.push({ id: (x as { id: string }).id, state: isTaskState(st) ? st : null })
      }
    }
    return out.slice(0, 5)
  } catch {
    return []
  }
}

/** Put a workflow first in Recent (5), with the state it was read in. */
export function rememberWorkflow(id: string, state: TaskState | null = null): void {
  const before = recentWorkflows()
  const next = [{ id, state }, ...before.filter((x) => x.id !== id)].slice(0, 5)
  if (JSON.stringify(next) === JSON.stringify(before)) return
  writePref(RECENT_WORKFLOWS_KEY, JSON.stringify(next))
  try {
    globalThis.dispatchEvent?.(new Event(RECENT_WORKFLOWS_EVENT))
  } catch {
    /* no event target here; the panel reads the store on its next render */
  }
}

// ---------------------------------------------------------------------------
// The frame's two reads: who you are, and the global pool for the meter.
// Both are marked as the FRAME's, so the page head's "newest read" never
// shows their age beside a screen still loading (CH-2).
// ---------------------------------------------------------------------------

function useFrameRead<T>(load: () => Promise<Result<T>>, everyMs: number | null): Result<T> {
  const [r, setR] = useState<Result<T>>({ status: 'loading', since: Date.now() })
  useEffect(() => {
    let live = true
    let timer: ReturnType<typeof setTimeout> | undefined
    const tick = () => {
      // Paused in a hidden tab; picked up again on the next tick.
      if (typeof document !== 'undefined' && document.hidden && everyMs !== null) {
        timer = setTimeout(tick, everyMs)
        return
      }
      load().then((next) => {
        if (!live) return
        setR(next)
        if (everyMs !== null) timer = setTimeout(tick, everyMs)
      })
    }
    tick()
    return () => {
      live = false
      if (timer !== undefined) clearTimeout(timer)
    }
  }, [load, everyMs])
  return r
}

const loadFrameMe = () => loadMe({ frame: true })
const loadFrameCapacity = () => loadCapacity({ frame: true })

function dataOf<T>(r: Result<T>): T | null {
  return r.status === 'ok' || r.status === 'stale' ? r.data : null
}

/** The global pool for the meter, and the first pool that is full or paused. */
export function meterOf(c: Capacity | null): {
  active: number
  limit: number
  warn: string | null
} | null {
  if (c === null) return null
  const g = c.pools.find((p) => p.name === 'global')
  if (g === undefined) return null
  const hot = c.pools.find((p) => p.enabled === false || (p.effective_limit > 0 && p.available <= 0))
  const warn = hot === undefined ? null : `${hot.name} ${hot.enabled === false ? 'paused' : 'full'}`
  return { active: g.active, limit: g.effective_limit, warn }
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
  children: ReactNode
}

export function SkyShell({
  section,
  tab,
  title,
  go,
  helpGroup = null,
  apiFailuresOnly = false,
  onApiFilter,
  foot,
  children,
}: ShellProps) {
  const me = useFrameRead(loadFrameMe, null)
  const cap = useFrameRead(loadFrameCapacity, 30_000)
  const who = dataOf(me)
  const admin = who?.principal.is_admin === true
  const env = classifyEnvironment(
    import.meta.env.VITE_SWARM_ENV,
    typeof window === 'undefined' ? '' : window.location.hostname,
    who === null ? null : servedEnvironment(who),
  )
  const t = envTreatment(env)
  const meter = meterOf(dataOf(cap))

  const [collapsed, setCollapsed] = useState(() => readPref(COLLAPSED_KEY) === '1')
  const [drawer, setDrawer] = useState(false)
  const [fly, setFly] = useState<Exclude<SpineSection, null> | null>(null)
  const flyTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)

  const toggle = useCallback(() => {
    setCollapsed((c) => {
      writePref(COLLAPSED_KEY, c ? null : '1')
      return !c
    })
  }, [])

  // A navigation closes the phone drawer and any flyout.
  const nav = useCallback(
    (to: string) => {
      setDrawer(false)
      setFly(null)
      go(to)
    },
    [go],
  )

  // N opens Submit, from anywhere but a field.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'n' && e.key !== 'N') return
      if (e.metaKey || e.ctrlKey || e.altKey) return
      const el = e.target as HTMLElement | null
      if (el !== null && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))) return
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

  const spine = (
    <nav className="sk-spine" aria-label="Sections">
      <a className="sk-hive" href="/overview" onClick={(e) => (e.preventDefault(), nav('overview/now'))}>
        <SwarmMark size={44} paint="sky" title="SwarmCloud" />
      </a>
      <button type="button" className="sk-ri sk-cta" title="Submit (N)" aria-current={section === null ? 'page' : undefined} onClick={() => nav('submit')}>
        <Icon name="submit" />
        <small>Submit</small>
      </button>
      <span className="sk-rsep" aria-hidden />
      {SPINE.map((s) => {
        const on = section === s.key
        const locked = s.key === 'admin' && who !== null && !admin
        return (
          <button
            key={s.key}
            type="button"
            className={`sk-ri${on ? ' is-on' : ''}`}
            aria-current={on ? 'page' : undefined}
            title={locked ? `${s.label} (admins only)` : s.label}
            onClick={() => {
              // Collapsed, a click on a section opens the panel again.
              if (collapsed) toggle()
              if (!(collapsed && on)) nav(s.to)
            }}
            onMouseEnter={() => hover(s.key)}
            onMouseLeave={() => hover(null)}
            onFocus={() => hover(s.key)}
          >
            <Icon name={s.key} />
            <small>{s.label}</small>
            {s.key === 'capacity' && meter?.warn != null && <i className="sk-dot" title={meter.warn} />}
            {locked && <Icon name="lock" className="sk-ic sk-lkd" />}
          </button>
        )
      })}
      <span className="sk-grow" />
      {foot}
      <span className="sk-av" title={who?.principal.email ?? 'not read'} aria-hidden>
        {initials(who?.principal.email ?? '')}
      </span>
    </nav>
  )

  const pages = (
    <PanelPages
      section={section}
      tab={tab}
      admin={admin}
      known={who !== null}
      nav={nav}
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
      <TenantBlock me={me} />
      <div className="sk-pscroll">{pages}</div>
      <Meter meter={meter} unread={cap.status === 'error'} />
      <div className="sk-pfoot">
        <span className="sk-pfrow">
          <b>{who === null ? (me.status === 'loading' ? 'reading…' : 'not read') : who.principal.email.split('@')[0]}</b>
          {admin && <span className="sk-adm">admin</span>}
        </span>
        {who !== null && <small>{who.principal.email}</small>}
      </div>
    </nav>
  )

  return (
    <div className={`sk-app${collapsed ? ' is-collapsed' : ''}${drawer ? ' has-drawer' : ''}`} data-env={env.kind}>
      {t.bar && <div className="sk-prodbar" aria-hidden />}
      <header className="sk-pbar">
        <button type="button" className="sk-ibtn" aria-label="Open the menu" aria-expanded={drawer} onClick={() => setDrawer(true)}>
          <Icon name="menu" />
        </button>
        <SwarmMark size={22} paint="sky" />
        <b>{title}</b>
        {who !== null && <span className="sk-tn">{who.tenant.display_name ?? who.tenant.tenant_id}</span>}
        <EnvPill label={t.label} prod={t.bar} title={t.explain} mini />
      </header>
      {drawer && <div className="sk-scrim" onClick={() => setDrawer(false)} aria-hidden />}
      <div className="sk-side">
        {spine}
        {!collapsed || drawer ? panel : null}
      </div>
      {collapsed && fly !== null && !drawer && (
        <Flyout section={fly} tab={section === fly ? tab : ''} nav={nav} onEnter={() => hover(fly)} onLeave={() => hover(null)} />
      )}
      <main className="sk-main">{children}</main>
    </div>
  )
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

/**
 * The tenant block: a static label with copy-id. The SWITCHER the mock-ups
 * draw for people in more than one tenant group is not built: `/v1/tenants/me`
 * serves one tenant and there is no route that lists or switches the others.
 */
function TenantBlock({ me }: { me: Result<Me> }) {
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
  return (
    <div className="sk-tenant">
      <span className="sk-tg" aria-hidden>
        {name.charAt(0).toUpperCase()}
      </span>
      <span className="sk-tt">
        <b title={id}>{name}</b>
        <small>
          tenant{' '}
          <button type="button" className="sk-cp" aria-label={`Copy tenant id ${id}`} title={id} onClick={copy}>
            copy id
          </button>
          <span className="sk-said" role="status">
            {said}
          </span>
        </small>
      </span>
    </div>
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
  const pct = meter.limit > 0 ? Math.min(100, (meter.active / meter.limit) * 100) : 0
  return (
    <div className="sk-meter" title="Slots leased against the global pool's effective ceiling">
      <div className="sk-mh">
        <span>Global pool</span>
        <b>
          {meter.active} / {meter.limit}
        </b>
      </div>
      <div className="sk-bar" role="meter" aria-valuenow={meter.active} aria-valuemin={0} aria-valuemax={meter.limit} aria-label="Global pool leased">
        <i style={{ width: `${pct}%` }} />
      </div>
      {meter.warn !== null && <div className="sk-mw">{meter.warn}</div>}
    </div>
  )
}

function PanelPages({
  section,
  tab,
  admin,
  known,
  nav,
  helpGroup,
  apiFailuresOnly,
  onApiFilter,
}: {
  section: SpineSection
  tab: string
  admin: boolean
  known: boolean
  nav: (to: string) => void
  helpGroup: string | null
  apiFailuresOnly: boolean
  onApiFilter?: (failuresOnly: boolean) => void
}) {
  if (section === 'overview') {
    return (
      <>
        <div className="sk-pt">On this page</div>
        {OVERVIEW_JUMPS.map((j) => (
          <button
            key={j.id}
            type="button"
            className="sk-pk"
            onClick={() => {
              document.getElementById(j.id)?.scrollIntoView?.({ block: 'start', behavior: 'smooth' })
            }}
          >
            <span className="sk-pl">{j.label}</span>
          </button>
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
            <button key={g.id} type="button" className={`sk-pk${on ? ' is-on' : ''}`} aria-current={on ? 'page' : undefined} onClick={() => nav(`help/${g.id}`)}>
              <span className="sk-pl">{g.title}</span>
              <span className="sk-cnt">{n}</span>
            </button>
          )
        })}
      </>
    )
  }
  if (section === 'api') {
    return (
      <>
        <div className="sk-pt">API reads</div>
        <button type="button" className={`sk-pk${apiFailuresOnly ? '' : ' is-on'}`} onClick={() => onApiFilter?.(false)}>
          <span className="sk-pl">Every read this tab made</span>
        </button>
        <button type="button" className={`sk-pk${apiFailuresOnly ? ' is-on' : ''}`} onClick={() => onApiFilter?.(true)}>
          <span className="sk-pl">Failures only</span>
        </button>
      </>
    )
  }
  // Submit is work: its pages are Work's, with nothing lit.
  const sec = section ?? 'work'
  const locked = sec === 'admin' && known && !admin
  const title = sec === 'work' ? 'Work' : sec === 'capacity' ? 'Capacity' : 'Admin'
  const lit = section === null ? '' : rowFor(sec, tab)
  return (
    <>
      <div className="sk-pt">
        {title}
        {locked && <Icon name="lock" className="sk-ic sk-lk" />}
      </div>
      {PANEL_PAGES[sec].map((p) => {
        const on = p.key === lit
        return (
          <div key={p.key}>
            <button
              type="button"
              className={`sk-pk${on ? ' is-on' : ''}${locked ? ' is-dis' : ''}`}
              aria-current={on && (p.kids === undefined || p.key === tab) ? 'page' : undefined}
              disabled={locked}
              onClick={() => nav(p.to)}
            >
              <span className="sk-pl">{p.label}</span>
              {locked && <Icon name="lock" className="sk-ic sk-lk" />}
            </button>
            {on && p.kids !== undefined && (
              <div className="sk-kids">
                {p.kids.map((k) => (
                  <button key={k.key} type="button" className={`sk-kid${k.key === tab ? ' is-on' : ''}`} onClick={() => nav(k.to)}>
                    {k.label}
                  </button>
                ))}
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
 * workflows.html C): the five workflows last opened in this browser, each with
 * its state mark, the open one highlighted, then "All workflows →". It costs
 * no read: the marks are the states the workflows were read in when opened.
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
            onClick={() => nav(`work/workflows?wf=${encodeURIComponent(w.id)}`)}
          >
            <span className="sk-recent-row">
              <span
                className={`sk-st is-${look === null ? 'neu' : look.hue}`}
                data-mark={look === null ? 'none' : look.mark}
                title={word}
              >
                <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
                  {look !== null && <MarkGlyph mark={look.mark} />}
                </svg>
                <span className="sk-vh">{word}</span>
              </span>
              <span className="sk-recent-id">{w.id}</span>
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
  nav,
  onEnter,
  onLeave,
}: {
  section: Exclude<SpineSection, null>
  tab: string
  nav: (to: string) => void
  onEnter: () => void
  onLeave: () => void
}) {
  if (section !== 'work' && section !== 'capacity' && section !== 'admin') return null
  const lit = rowFor(section, tab)
  return (
    <div className={`sk-flyout is-${section}`} onMouseEnter={onEnter} onMouseLeave={onLeave} role="menu">
      <b>{section === 'work' ? 'Work' : section === 'capacity' ? 'Capacity' : 'Admin'}</b>
      {PANEL_PAGES[section].map((p) => (
        <button key={p.key} type="button" role="menuitem" className={p.key === lit ? 'is-on' : ''} onClick={() => nav(p.to)}>
          {p.label}
        </button>
      ))}
      <em>Click the section to open the panel again</em>
    </div>
  )
}
