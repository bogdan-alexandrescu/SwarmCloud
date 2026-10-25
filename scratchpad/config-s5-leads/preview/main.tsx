// CONFIG-S5-LEADS rendered preview: the real App, its reads answered by the fixtures below.
// ?p=wsadmin|member  ?at=/config/mcp-servers|/config/people  ?s=missing|done|unread|alone|waiting
// Built from scratchpad/config-panel-e/preview/main.tsx; the audit is the lane's AUDIT JS, verbatim.
import { createRoot } from 'react-dom/client'
import '@fontsource-variable/inter-tight'
import '@fontsource/dm-mono/400.css'
import '@fontsource/dm-mono/500.css'
import '../../../apps/swarm-ui/src/styles.css'

type Json = Record<string, unknown>
const q = new URLSearchParams(location.search)
const persona = q.get('p') ?? 'wsadmin'
const at = q.get('at') ?? '/config/mcp-servers'
const scenario = q.get('s') ?? 'missing'
const admin = persona !== 'member'

const email = 'operator@example.com'
const home = 'example-team'
const ME: Json = {
  tenant: { tenant_id: home, kind: 'group', display_name: 'Example Team' },
  principal: { email, domain: 'example.com', groups: [], is_admin: false },
  environment: 'dev',
  environment_declared: true,
}
const PERSONAL: Json = { tenant_id: 'u-operator', display_name: email, kind: 'user', via: 'personal', role: { name: 'admin', source: 'personal' } }
const MINE: Json[] = [PERSONAL, { tenant_id: home, display_name: 'Example Team', kind: 'group', via: 'member', role: { name: admin ? 'admin' : 'member', source: 'override' } }]

const step = (name: string, state: string, evidence: Json = {}): Json => ({ step: name, state, code: null, copy: null, checked_at: null, evidence, issues: [] })
const unreadMcp = 'the MCP servers and the secrets granted to them could not be read'
const unreadRoles = "this workspace's roles could not be read; retry shortly"
function secrets(): Json {
  const sources = (m: number | null): Json => ({
    mcp_servers: { needed: m, reason: m === null ? unreadMcp : null },
    qa_targets: { needed: 0, reason: null },
    provider_key: { needed: 0, reason: null },
  })
  if (scenario === 'unread') return { needed: null, done: null, items: [], sources: sources(null) }
  if (scenario === 'missing')
    return { needed: 1, done: false, items: [{ kind: 'mcp_server', name: 'kb', needed_by: 'mcp:kb', where: 'config/mcp-servers' }], sources: sources(1) }
  return { needed: 0, done: true, items: [], sources: sources(0) }
}
function people(): Json {
  if (scenario === 'unread') return { applies: true, done: null, holders: null, admins: null, pending: null, reason: unreadRoles, pending_reason: null }
  if (scenario === 'alone') return { applies: true, done: false, holders: 0, admins: 0, pending: null, reason: null, pending_reason: 'n' }
  if (scenario === 'waiting') return { applies: true, done: true, holders: 5, admins: 1, pending: 2, reason: null, pending_reason: null }
  return { applies: true, done: true, holders: 5, admins: 1, pending: null, reason: null, pending_reason: 'n' }
}
function setup(tenant: string): Json {
  const repos = ['acme/api', 'acme/web'].map((full_name) => ({ full_name }))
  return {
    tenant_id: tenant, user: email, user_hash: 'h', derived_at: '2026-10-25T09:00:00Z',
    steps: [step('github_connected', 'done', { login: 'operator' }), step('repos_chosen', 'done', { repositories: repos })],
    next_step: null, complete: false, source: 'derived', config_setup: { secrets: secrets(), people: people() },
  }
}

const server = (name: string, mode: string, used: string | null): Json => ({
  name, url: `https://${name}.app.example.com/mcp`, description: '', enabled: true, revision: 1,
  auth: { mode, header: 'Authorization', scheme: 'Bearer' }, allowed_profiles: mode === 'dispatch' ? ['claude-code'] : [], allowed_tools: [],
  consumer: `mcp:${name}`, updated_at: '2026-10-20T09:00:00Z', updated_by: 'dana@example.com', last_used_at: used,
})
const MCP: Json = { tenant_id: home, can_manage: admin, egress_addresses: ['34.1.2.3', '34.1.2.4'], servers: [server('kb', 'workspace_secret', null), server('docs-search', 'dispatch', '2026-10-25T07:00:00Z')] }

const person = (who: string, role: string, ago: string, reason = ''): Json => ({ email: who, role, source: 'override', granted_by: 'dana@example.com', granted_at: ago, granted_via: 'console', reason })
const ROSTER: Json[] =
  scenario === 'alone'
    ? [person(email, 'admin', '2026-10-22T09:00:00Z', 'workspace owner')]
    : [
        person('dana@example.com', 'admin', '2026-10-16T09:00:00Z', 'workspace owner'),
        person(email, admin ? 'admin' : 'member', '2026-10-19T09:00:00Z', 'runs agents'),
        person('ali@example.com', 'member', '2026-10-19T09:00:00Z', 'runs agents'),
        person('mo@example.com', 'member', '2026-10-21T09:00:00Z'),
        person('pat@example.com', 'viewer', '2026-10-23T09:00:00Z', 'reads results'),
        person('tess@example.com', 'viewer', '2026-10-24T09:00:00Z'),
      ]
const ROLES: Json = {
  tenant_id: home, kind: 'group', default_role: 'viewer', default_source: 'set',
  me: { email, role: admin ? 'admin' : 'member', source: 'override', via: 'member', granted_by: 'dana@example.com', granted_at: '2026-10-19T09:00:00Z' },
  people: ROSTER,
  roles: [
    { name: 'viewer', summary: 'Sees everything in this workspace; changes nothing.' },
    { name: 'member', summary: 'Submits and approves.' },
    { name: 'admin', summary: 'A member who also manages people’s roles.' },
  ],
  can_manage: admin,
  audit: [{ action: 'workspace_role_granted', by: 'dana@example.com', at: '2026-10-24T09:00:00Z', target_email: 'tess@example.com', detail: { from: 'member', to: 'viewer', via: 'console', reason: '' } }],
}

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = String(input)
  const headers = (init?.headers ?? {}) as Record<string, string>
  const tenant = headers['X-Swarm-Tenant'] ?? home
  if (url.includes('/v1/tenants/mine')) return Promise.resolve(json(MINE))
  if (url.includes('/v1/tenants/me')) return Promise.resolve(json(ME))
  if (url.includes('/v1/onboarding')) return Promise.resolve(json(setup(tenant)))
  if (url.includes('/v1/mcp-servers')) return Promise.resolve(json(MCP))
  if (url.includes('/v1/workspace/roles')) return Promise.resolve(json(ROLES))
  if (url.includes('/v1/approvals')) return Promise.resolve(json({ items: [], count: 0 }))
  return Promise.resolve(json({ code: 'not_found', message: 'Not served by this preview.' }, 404))
}) as typeof fetch

history.replaceState(null, '', at)
const { App } = await import('../../../apps/swarm-ui/src/App')
const { applyTheme, readTheme } = await import('../../../apps/swarm-ui/src/theme')
applyTheme(readTheme())
createRoot(document.getElementById('root')!).render(<App />)

;(window as unknown as Record<string, unknown>).__audit = () => {
  const out: unknown[] = []
  const sectionOf = (el: Element | null): string => { let n = el; while (n) { let p = n.previousElementSibling; while (p) { if (/^H2$/.test(p.tagName)) return (p.textContent ?? '').trim().slice(0, 60); const h = p.querySelector && p.querySelector('h2'); if (h) return (h.textContent ?? '').trim().slice(0, 60); p = p.previousElementSibling } n = n.parentElement } return '?' }
  const clipAncestor = (el: Element) => { let a = el.parentElement; while (a && a !== document.body) { const cs = getComputedStyle(a); if (/(hidden|clip)/.test(cs.overflowX) || /(hidden|clip)/.test(cs.overflow)) return a; a = a.parentElement } return null }
  const textOf = (el: Element) => (((el as HTMLElement).innerText || el.getAttribute('aria-label') || '') as string).trim().replace(/\s+/g, ' ').slice(0, 50)
  const seen = new Set<string>()
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el)
    if (cs.display === 'none' || cs.visibility === 'hidden') continue
    const r = el.getBoundingClientRect()
    if (r.width === 0 || r.height === 0) continue
    const hasOwnText = [...el.childNodes].some((n) => n.nodeType === 3 && (n.textContent ?? '').trim().length > 1)
    const isControl = /^(BUTTON|A|INPUT|SELECT)$/.test(el.tagName)
    if (!(hasOwnText || isControl)) continue
    const anc = clipAncestor(el)
    if (anc) { const ar = anc.getBoundingClientRect(); if (anc.scrollWidth > anc.clientWidth + 2 && getComputedStyle(anc).overflowX !== 'auto' && getComputedStyle(anc).overflowX !== 'scroll' && r.right > ar.right + 2) { const key = 'clip|' + textOf(el); if (!seen.has(key)) { seen.add(key); out.push(['clipped', sectionOf(el), textOf(el)]) } } }
    if (hasOwnText && r.width < 40 && r.height > 80 && textOf(el).length > 6) { const key = 'crush|' + textOf(el); if (!seen.has(key)) { seen.add(key); out.push(['crushed', sectionOf(el), textOf(el)]) } }
    const fs = parseFloat(cs.fontSize)
    if (hasOwnText && fs < 11.5) { const key = 'tiny|' + fs + '|' + sectionOf(el); if (!seen.has(key)) { seen.add(key); out.push(['tiny ' + fs + 'px', sectionOf(el), textOf(el)]) } }
  }
  return { audit: out, scrollWidth: document.documentElement.scrollWidth, lead: document.querySelector('.ws-lead')?.textContent ?? null }
}
