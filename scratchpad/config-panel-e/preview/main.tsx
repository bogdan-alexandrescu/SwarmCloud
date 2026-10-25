// CONFIG-PANEL-E rendered preview: the real App, its reads answered by the fixtures below.
// ?p=platform|wsadmin|member|personal  ?at=/config/secrets  ?s=part|unread|done
import { createRoot } from 'react-dom/client'
import '@fontsource-variable/inter-tight'
import '@fontsource/dm-mono/400.css'
import '@fontsource/dm-mono/500.css'
import '../../../apps/swarm-ui/src/styles.css'

type Json = Record<string, unknown>
const q = new URLSearchParams(location.search)
const persona = q.get('p') ?? 'platform'
const at = q.get('at') ?? '/config/secrets'
const scenario = q.get('s') ?? 'part'

const email = 'operator@example.com'
const personal = persona === 'personal'
const home = personal ? 'u-operator' : 'example-team'
const ME: Json = {
  tenant: { tenant_id: home, kind: personal ? 'user' : 'group', display_name: personal ? email : 'Example Team' },
  principal: { email, domain: 'example.com', groups: [], is_admin: persona === 'platform' || personal },
  environment: 'dev',
  environment_declared: true,
}
const PERSONAL: Json = { tenant_id: 'u-operator', display_name: email, kind: 'user', via: 'personal', role: { name: 'admin', source: 'personal' } }
const grp = (id: string, name: string, role: string): Json => ({ tenant_id: id, display_name: name, kind: 'group', via: 'member', role: { name: role, source: 'override' } })
const MINE: Json[] =
  persona === 'member' ? [PERSONAL, grp('example-team', 'Example Team', 'member')] : [PERSONAL, grp('example-team', 'Example Team', 'admin'), grp('example-labs', 'Example Labs', 'admin')]

const step = (name: string, state: string, evidence: Json = {}): Json => ({ step: name, state, code: null, copy: null, checked_at: null, evidence, issues: [] })
const item = (kind: string, name: string, extra: Json = {}): Json => ({ kind, name, needed_by: `${name}`, where: 'config/secrets', ...extra })
const src = (m: number | null, qa: number | null, k: number | null): Json => ({
  mcp_servers: { needed: m, reason: m === null ? 'the MCP servers and the secrets granted to them could not be read' : null },
  qa_targets: { needed: qa, reason: qa === null ? 'the QA targets could not be read' : null },
  provider_key: { needed: k, reason: null },
})
const people = (done: boolean | null, holders: number | null): Json =>
  personal
    ? { applies: false, done: null, holders: null, admins: null, pending: null, reason: 'a personal workspace has nobody to invite', pending_reason: null }
    : { applies: true, done, holders, admins: 0, pending: null, reason: done === null ? "this workspace's roles could not be read; retry shortly" : null, pending_reason: 'n' }
function setup(tenant: string): Json {
  const repos = ['acme/api', 'acme/web', 'acme/docs', 'acme/infra'].map((full_name) => ({ full_name }))
  if (tenant === 'example-labs')
    return { tenant_id: tenant, steps: [step('github_connected', 'done', { login: 'operator' }), step('repos_chosen', 'todo')], config_setup: { secrets: { needed: 0, done: true, items: [], sources: src(0, 0, 0) }, people: people(false, 0) } }
  const secrets =
    scenario === 'unread'
      ? { needed: null, done: null, items: [item('mcp_server', 'linear')], sources: src(1, null, 0) }
      : scenario === 'done'
        ? { needed: 0, done: true, items: [], sources: src(0, 0, 0) }
        : { needed: 3, done: false, items: [item('mcp_server', 'linear'), item('qa_target', 'shop', { environment: 'staging' }), item('provider_key', 'typesafe')], sources: src(1, 1, 1) }
  const ppl = scenario === 'unread' ? people(null, null) : persona === 'member' || persona === 'wsadmin' || scenario === 'done' ? people(true, 5) : people(false, 0)
  return { tenant_id: tenant, user: email, user_hash: 'h', derived_at: '2026-10-25T09:00:00Z', steps: [step('github_connected', 'done', { login: 'operator' }), step('repos_chosen', 'done', { repositories: repos })], next_step: null, complete: false, source: 'derived', config_setup: { secrets, people: ppl } }
}
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = String(input)
  const headers = (init?.headers ?? {}) as Record<string, string>
  const tenant = headers['X-Swarm-Tenant'] ?? home
  if (url.includes('/v1/tenants/mine')) return Promise.resolve(json(MINE))
  if (url.includes('/v1/tenants/me')) return Promise.resolve(json(ME))
  if (url.includes('/v1/onboarding')) return Promise.resolve(json(setup(tenant)))
  if (url.includes('/v1/approvals')) return Promise.resolve(json({ items: [], count: 3 }))
  return Promise.resolve(json({ code: 'not_found', message: 'Not served by this preview.' }, 404))
}) as typeof fetch

history.replaceState(null, '', at)
const { App } = await import('../../../apps/swarm-ui/src/App')
const { applyTheme, readTheme } = await import('../../../apps/swarm-ui/src/theme')
applyTheme(readTheme())
createRoot(document.getElementById('root')!).render(<App />)

// The lane's AUDIT JS, verbatim, for `() => window.__audit()`.
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
  return { audit: out, scrollWidth: document.documentElement.scrollWidth }
}
