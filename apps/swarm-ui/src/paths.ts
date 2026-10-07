/**
 * REAL ROUTES (rebrand, owner decision 2026-10-01; navigation.html §C).
 *
 * The console's addresses are History-API paths now -- `/agents/live`,
 * `/capacity/pools/profiles`, `/help/reading-a-figure#absent-vs-zero` -- and
 * the hash router is gone. Two spellings meet here:
 *
 *   an ADDRESS  the router's internal key, which is the hash the old router
 *               canonicalised to (`work/task/<id>/attempts`, `capacity/profiles`).
 *               `fromAddress` in App.tsx resolves it, aliases and all, and it is
 *               what `go()` and every in-app `href="#..."` still name.
 *   a PATH      what the address bar shows and what a person copies.
 *
 * `addressToPath` and `pathToAddress` are inverses over every canonical
 * address, and `paths.test.ts` holds them to it -- and holds every legacy hash
 * (SECTION_ALIASES, LEGACY, MOVED_PANES) to the path it must land on.
 *
 * THE SERVER MUST SERVE index.html FOR EVERY PATH. images/swarm-ui/nginx.conf
 * already does (`try_files $uri $uri/ /index.html`); `/v1/*` never reaches it.
 */
import { AGENT_TABS, RECENT_STATES, agentList, agentListQuery, listFlags, parseAgentList, type AgentList, type AgentTab, type RecentState } from './agentlist'
import { HELP, HELP_GROUPS, HELP_ROUTE, type TopicId } from './help'

/** The non-section addresses this file knows by name. */
export const SUBMIT_ADDRESS = 'submit'
export const REFERENCE_ADDRESS = 'reference'

/**
 * THE GITHUB APP'S CALLBACK (#780, OB3): the URL registered on the App,
 * https://swarm.saga.xyz/onboarding/github/callback. Not a section and not in
 * FIXED -- nothing links to it, and the not-found page must never offer it.
 * ITS QUERY IS NEVER PART OF THE ADDRESS: GitHub's `code` and `state` ride on
 * it, and an address is what the router writes back into the bar.
 */
export const GITHUB_CALLBACK_ADDRESS = 'onboarding/github/callback'
export const GITHUB_CALLBACK_PATH = '/onboarding/github/callback'

/** Address -> path for the panes that are one fixed path each. */
/** Exported for the not-found page, which offers the nearest of these (NotFound.tsx). */
export const FIXED: Readonly<Record<string, string>> = {
  'overview/now': '/overview',
  'work/running': '/agents',
  'work/workflows': '/workflows',
  'work/timeline': '/timeline',
  // Timeline's second page, today's outcome ledger (timeline.html pick A).
  'work/timeline/outcomes': '/timeline/outcomes',
  'work/new': '/submit/task',
  'work/new-workflow': '/submit/workflow',
  // intake-tenants.html 1A: the issue form, and Work › Runs (one run is
  // `/runs/<id>`, below).
  'work/new-issue': '/submit/issue',
  'work/runs': '/runs',
  'work/repositories': '/repositories',
  submit: '/submit',
  'capacity/pools': '/capacity/pools',
  'capacity/profiles': '/capacity/pools/profiles',
  'capacity/catalogue': '/capacity/runtimes',
  'capacity/holders': '/capacity/holders',
  'capacity/accounts': '/capacity/accounts',
  'capacity/quota': '/capacity/accounts/quota',
  'admin/limits': '/admin/limits',
  'admin/tenants': '/admin/tenants',
  'admin/counts': '/admin/counts',
  [HELP_ROUTE]: '/help',
  reference: '/api-reads',
}

const FIXED_BACK: Readonly<Record<string, string>> = Object.fromEntries(
  Object.entries(FIXED).map(([a, p]) => [p, a]),
)

/**
 * A SECTION'S ROOT OPENS ITS FIRST PAGE (owner QA R14, 2026-10-04): `/capacity`
 * and `/admin` said "No page at /capacity", though each is a section the spine
 * names. One way only -- the address goes back to the page's own path, so the
 * bar is rewritten to `/capacity/pools` (App's one-time redirect) and no page
 * has two spellings. `/agents` and `/overview` are pages already.
 */
export const SECTION_ROOTS: Readonly<Record<string, string>> = {
  '/work': 'work/running',
  '/capacity': 'capacity/pools',
  '/admin': 'admin/limits',
}

const GROUP_IDS: readonly string[] = HELP_GROUPS.map((g) => g.id)

/** The Help group a topic is drawn under, or null for a topic this build lacks. */
export function helpGroupOf(topic: string): string | null {
  const t = (HELP as Readonly<Record<string, { group: string } | undefined>>)[topic as TopicId]
  return t === undefined ? null : t.group
}

export const TASK_PANES = ['logs', 'children', 'attempts', 'artifacts', 'checkpoints'] as const

function isAgentTab(s: string | undefined): s is AgentTab {
  return (AGENT_TABS as readonly string[]).includes(s ?? '')
}

function isRecentState(s: string | null): s is RecentState {
  return (RECENT_STATES as readonly string[]).includes(s ?? '')
}

/**
 * A list's query on a path: `state=failed&group=wf&first=failed` (G2-22). The
 * state is the address's last segment and a query key on the path, as it
 * always was; the toggles are a query on both.
 *
 * EXPORTED FOR AN OPEN AGENT'S PATH, which carries the list it was opened
 * from: `/agents/recent/<id>?state=failed&group=wf`. Opening an agent from
 * `Recent · failed` wrote `/agents/recent/<id>`, so a reload or a shared link
 * landed beside the unfiltered list. `pathToAddress` reads it back as `list`.
 */
export function agentListSearch(list: AgentList): string {
  const q = new URLSearchParams()
  if (list.tab === 'recent' && list.state !== null) q.set('state', list.state)
  for (const [k, v] of new URLSearchParams(agentListQuery(list))) q.append(k, v)
  return q.toString()
}

/** The list a path's query names, with the tab the path carries. */
function listFromQuery(tab: AgentTab, query: string): AgentList {
  const state = new URLSearchParams(query).get('state')
  return agentList(tab, isRecentState(state) ? state : null, listFlags(query))
}


/** The panes of one workflow that are a path segment: `/workflows/<id>/<pane>`. */
export const WORKFLOW_PANES: readonly string[] = ['table', 'timeline']

/** A repository's tabs that are a path segment: `/repositories/<id>/<tab>`; Overview is the bare id. */
export const REPO_TABS: readonly string[] = ['graph', 'impact', 'test-map', 'hot-spots', 'index-runs', 'settings', 'used-by']
/**
 * The query a repository tab carries in the bar: the pull request the Impact
 * tab opens on, and the Graph view and symbol search a Test map row opens
 * (`view=tests&q=<directory>`, QA G4-09).
 */
const REPO_TAB_QUERY: readonly string[] = ['pr', 'view', 'q']

/**
 * The path for an address. `agentTab` is the list an open agent sits in
 * (`/agents/<tab>/<id>`), which the address does not carry; it defaults to
 * `live`, the list a pasted agent link is most often from.
 *
 * The list's state and toggles ride on an open agent's path as its query
 * (`agentListSearch`, G2-22), which `App` appends: this signature is the one
 * `swarm_api.codec.agent_console_url` is held to.
 */
export function addressToPath(address: string, agentTab: AgentTab = 'live'): string {
  const q = address.indexOf('?')
  const bare = q === -1 ? address : address.slice(0, q)
  const query = q === -1 ? '' : address.slice(q + 1)
  const seg = bare.split('/')

  // One agent: `work/task/<id>[/<pane>]` -> `/agents/<tab>/<id>[/<pane>]`.
  if (seg[0] === 'work' && seg[1] === 'task' && seg.length > 2) {
    return `/agents/${agentTab}/${seg.slice(2).join('/')}`
  }
  // The agent list: `work/running/recent/failed?group=wf` ->
  // `/agents/recent?state=failed&group=wf`.
  if (seg[0] === 'work' && seg[1] === 'running' && isAgentTab(seg[2])) {
    const list = parseAgentList(seg.slice(2), query)
    if (list === null) return seg[3] === undefined ? `/agents/${seg[2]}` : `/agents/${seg[2]}?state=${seg[3]}`
    const search = agentListSearch(list)
    return search === '' ? `/agents/${list.tab}` : `/agents/${list.tab}?${search}`
  }
  // One workflow rides on the list's query as `wf=<id>`; the rest is its filters.
  if (bare === 'work/workflows' && query !== '') {
    const params = new URLSearchParams(query)
    const wf = params.get('wf')
    params.delete('wf')
    // One workflow's Table or Timeline tab is a path segment of its own:
    // `/workflows/<id>/timeline` (workflows.html, section F). Only on a workflow.
    const tab = params.get('tab')
    const hasWf = wf !== null && wf !== ''
    const pane = hasWf && tab !== null && WORKFLOW_PANES.includes(tab) ? `/${tab}` : ''
    if (pane !== '') params.delete('tab')
    const rest = params.toString()
    const base = wf !== null && wf !== '' ? `/workflows/${encodeURIComponent(wf)}${pane}` : '/workflows'
    return rest === '' ? base : `${base}?${rest}`
  }
  // One issue run rides on the Runs list's query as `run=<id>`: `/runs/<id>`.
  if (bare === 'work/runs' && query !== '') {
    const run = new URLSearchParams(query).get('run')
    if (run !== null && run !== '') return `/runs/${encodeURIComponent(run)}`
  }
  // Work › Repositories (repositories.html): the page rides on the tab's query.
  if (bare === 'work/repositories' && query !== '') {
    const q = new URLSearchParams(query)
    const page = q.get('page')
    const repo = q.get('repo')
    if (page === 'register' || page === 'tokens') return `/repositories/${page}`
    if (page === 'permissions') return '/repositories/tokens/permissions'
    if (repo !== null && repo !== '') {
      const tab = q.get('tab')
      const base = `/repositories/${encodeURIComponent(repo)}`
      if (tab === null || !REPO_TABS.includes(tab)) return base
      const rest = new URLSearchParams()
      for (const k of REPO_TAB_QUERY) {
        const v = q.get(k)
        if (v !== null) rest.set(k, v)
      }
      return rest.toString() === '' ? `${base}/${tab}` : `${base}/${tab}?${rest.toString()}`
    }
  }
  // Help: a topic lands at its group's page, scrolled to it; a group is a page.
  if (seg[0] === HELP_ROUTE && seg.length > 1) {
    const tail = seg.slice(1).join('/')
    if (GROUP_IDS.includes(tail)) return `/help/${tail}`
    const group = helpGroupOf(tail)
    // An unknown topic keeps its name, so the page can say which was asked for.
    return group === null ? `/help/${tail}` : `/help/${group}#${tail}`
  }
  if (bare === GITHUB_CALLBACK_ADDRESS) return GITHUB_CALLBACK_PATH
  const fixed = FIXED[bare]
  if (fixed !== undefined) return query === '' ? fixed : `${fixed}?${query}`
  return '/overview'
}

export interface PathRoute {
  /** The address to resolve, in the router's internal spelling. */
  address: string
  /** For an agent path, the list it was opened from. */
  agentTab: AgentTab | null
  /**
   * For an agent path whose query names the list's state or toggles, that
   * whole list (G2-22). Absent when the query names neither, so the list is
   * `agentTab` alone.
   */
  list?: AgentList
}

/**
 * The address for a path, or null for a path this console does not serve
 * (the router then lands on Overview, and rewrites the bar to say so).
 */
export function pathToAddress(pathname: string, search = '', hash = ''): PathRoute | null {
  const path = pathname.replace(/\/+$/, '') || '/'
  const query = search.replace(/^\?/, '')
  const seg = path.split('/').slice(1)
  const plain = (address: string): PathRoute => ({ address, agentTab: null })

  if (path === '/') return plain('overview/now')

  if (seg[0] === 'agents') {
    const tab = seg[1]
    if (isAgentTab(tab)) {
      const rest = seg.slice(2)
      const list = listFromQuery(tab, query)
      if (rest.length > 0) {
        const named = list.state !== null || agentListQuery(list) !== ''
        return { address: `work/task/${rest.join('/')}`, agentTab: tab, ...(named ? { list } : {}) }
      }
      // The state is kept as written, as it always was: a misspelt one lands
      // on the plain list through `parseAgentList`, not here.
      const state = new URLSearchParams(query).get('state')
      const flags = agentListQuery(list)
      const at = state === null || tab !== 'recent' ? `work/running/${tab}` : `work/running/${tab}/${state}`
      return plain(flags === '' ? at : `${at}?${flags}`)
    }
    return seg.length === 1 ? plain('work/running') : null
  }

  if (seg[0] === 'workflows' && seg.length >= 2) {
    // `wf`, then the tab a trailing `/table` or `/timeline` names, then the
    // filters in the order the path carried them, which is the order
    // `addressToPath` took them off: the two stay exact inverses.
    const last = seg[seg.length - 1] ?? ''
    const pane = seg.length >= 3 && WORKFLOW_PANES.includes(last) ? last : null
    const idSegs = pane === null ? seg.slice(1) : seg.slice(1, -1)
    const params = new URLSearchParams({ wf: decodeURIComponent(idSegs.join('/')) })
    if (pane !== null) params.append('tab', pane)
    for (const [k, v] of new URLSearchParams(query)) if (k !== 'wf' && (pane === null || k !== 'tab')) params.append(k, v)
    return plain(`work/workflows?${params.toString()}`)
  }

  if (seg[0] === 'runs' && seg.length >= 2) {
    return plain(`work/runs?${new URLSearchParams({ run: decodeURIComponent(seg.slice(1).join('/')) }).toString()}`)
  }

  if (seg[0] === 'repositories' && seg.length >= 2) {
    const at = (q: Record<string, string>) => plain(`work/repositories?${new URLSearchParams(q).toString()}`)
    if (seg[1] === 'register' && seg.length === 2) return at({ page: 'register' })
    if (seg[1] === 'tokens') return seg[2] === 'permissions' ? at({ page: 'permissions' }) : at({ page: 'tokens' })
    const repo = decodeURIComponent(seg[1] ?? '')
    const tab = seg[2]
    if (tab === undefined || !REPO_TABS.includes(tab)) return at({ repo })
    const extra: Record<string, string> = {}
    const given = new URLSearchParams(query)
    for (const k of REPO_TAB_QUERY) {
      const v = given.get(k)
      if (v !== null) extra[k] = v
    }
    return at({ repo, tab, ...extra })
  }

  if (seg[0] === 'help' && seg.length >= 2) {
    const topic = hash.replace(/^#/, '')
    // `/help/<group>#<topic>` names the topic; `/help/<group>` the page.
    return plain(topic !== '' ? `${HELP_ROUTE}/${topic}` : `${HELP_ROUTE}/${seg.slice(1).join('/')}`)
  }

  // The callback's query (`code`, `state`, `error`) is dropped HERE, so the
  // route never holds it and the router's normalise writes a bare path.
  if (path === GITHUB_CALLBACK_PATH) return plain(GITHUB_CALLBACK_ADDRESS)

  const fixed = FIXED_BACK[path] ?? SECTION_ROOTS[path]
  if (fixed === undefined) return null
  return plain(query === '' ? fixed : `${fixed}?${query}`)
}

/**
 * Whether a `#...` fragment is an OLD ROUTE rather than an anchor on the page:
 * its head names a section, an alias, a legacy page, Help or API reads. On
 * `/help/<group>` a bare topic id is an anchor, and `help` is not one, so the
 * two never collide. Anything else is left to the browser.
 */
export function isLegacyHash(hash: string, heads: readonly string[], pathname = ''): boolean {
  const h = hash.replace(/^#/, '')
  if (h === '') return false
  // On `/help/...` a bare `#topic` is ALWAYS an in-page anchor. A topic id may
  // equal a section id (`capacity`, help.ts), and the section would win.
  if (pathname.startsWith('/help/') && !h.includes('/')) return false
  const head = h.split(/[/?]/)[0] ?? ''
  return heads.includes(head)
}
