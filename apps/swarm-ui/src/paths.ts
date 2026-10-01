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
import { AGENT_TABS, type AgentTab } from './agentlist'
import { HELP, HELP_GROUPS, HELP_ROUTE, type TopicId } from './help'

/** The non-section addresses this file knows by name. */
export const SUBMIT_ADDRESS = 'submit'
export const REFERENCE_ADDRESS = 'reference'

/** Address -> path for the panes that are one fixed path each. */
const FIXED: Readonly<Record<string, string>> = {
  'overview/now': '/overview',
  'work/running': '/agents',
  'work/workflows': '/workflows',
  'work/timeline': '/timeline',
  'work/new': '/submit/task',
  'work/new-workflow': '/submit/workflow',
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

const GROUP_IDS: readonly string[] = HELP_GROUPS.map((g) => g.id)

/** The Help group a topic is drawn under, or null for a topic this build lacks. */
export function helpGroupOf(topic: string): string | null {
  const t = (HELP as Readonly<Record<string, { group: string } | undefined>>)[topic as TopicId]
  return t === undefined ? null : t.group
}

export const TASK_PANES = ['attempts', 'artifacts', 'checkpoints'] as const

function isAgentTab(s: string | undefined): s is AgentTab {
  return (AGENT_TABS as readonly string[]).includes(s ?? '')
}

/**
 * The path for an address. `agentTab` is the list an open agent sits in
 * (`/agents/<tab>/<id>`), which the address does not carry; it defaults to
 * `live`, the list a pasted agent link is most often from.
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
  // The agent list: `work/running/recent/failed` -> `/agents/recent?state=failed`.
  if (seg[0] === 'work' && seg[1] === 'running' && isAgentTab(seg[2])) {
    return seg[3] === undefined ? `/agents/${seg[2]}` : `/agents/${seg[2]}?state=${seg[3]}`
  }
  // One workflow rides on the list's query as `wf=<id>`; the rest is its filters.
  if (bare === 'work/workflows' && query !== '') {
    const params = new URLSearchParams(query)
    const wf = params.get('wf')
    params.delete('wf')
    const rest = params.toString()
    const base = wf === null || wf === '' ? '/workflows' : `/workflows/${encodeURIComponent(wf)}`
    return rest === '' ? base : `${base}?${rest}`
  }
  // Help: a topic lands at its group's page, scrolled to it; a group is a page.
  if (seg[0] === HELP_ROUTE && seg.length > 1) {
    const tail = seg.slice(1).join('/')
    if (GROUP_IDS.includes(tail)) return `/help/${tail}`
    const group = helpGroupOf(tail)
    // An unknown topic keeps its name, so the page can say which was asked for.
    return group === null ? `/help/${tail}` : `/help/${group}#${tail}`
  }
  const fixed = FIXED[bare]
  if (fixed !== undefined) return query === '' ? fixed : `${fixed}?${query}`
  return '/overview'
}

export interface PathRoute {
  /** The address to resolve, in the router's internal spelling. */
  address: string
  /** For an agent path, the list it was opened from. */
  agentTab: AgentTab | null
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
      if (rest.length > 0) return { address: `work/task/${rest.join('/')}`, agentTab: tab }
      const state = new URLSearchParams(query).get('state')
      return plain(state === null || tab !== 'recent' ? `work/running/${tab}` : `work/running/${tab}/${state}`)
    }
    return seg.length === 1 ? plain('work/running') : null
  }

  if (seg[0] === 'workflows' && seg.length >= 2) {
    const params = new URLSearchParams(query)
    params.set('wf', decodeURIComponent(seg.slice(1).join('/')))
    return plain(`work/workflows?${params.toString()}`)
  }

  if (seg[0] === 'help' && seg.length >= 2) {
    const topic = hash.replace(/^#/, '')
    // `/help/<group>#<topic>` names the topic; `/help/<group>` the page.
    return plain(topic !== '' ? `${HELP_ROUTE}/${topic}` : `${HELP_ROUTE}/${seg.slice(1).join('/')}`)
  }

  const fixed = FIXED_BACK[path]
  if (fixed === undefined) return null
  return plain(query === '' ? fixed : `${fixed}?${query}`)
}

/**
 * Whether a `#...` fragment is an OLD ROUTE rather than an anchor on the page:
 * its head names a section, an alias, a legacy page, Help or API reads. On
 * `/help/<group>` a bare topic id is an anchor, and `help` is not one, so the
 * two never collide. Anything else is left to the browser.
 */
export function isLegacyHash(hash: string, heads: readonly string[]): boolean {
  const h = hash.replace(/^#/, '')
  if (h === '') return false
  const head = h.split(/[/?]/)[0] ?? ''
  return heads.includes(head)
}
