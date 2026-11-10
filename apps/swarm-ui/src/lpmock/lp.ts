// MOCK-HOOK FILE
// LLM Providers mock-up, direction "table" (LANE LLM-PROVIDERS-MOCKUPS): on at ?lp=table, kept for the tab.
let seen = false
export function lpOn(): boolean {
  if (typeof window === 'undefined') return false
  const q = new URLSearchParams(window.location.search).get('lp')
  if (seen && q !== 'off') return true
  if (q === 'table') {
    seen = true
    try { window.sessionStorage.setItem('lp', 'table') } catch { /* private mode */ }
    return true
  }
  if (q === 'off') {
    try { window.sessionStorage.removeItem('lp') } catch { /* private mode */ }
    return false
  }
  try { return window.sessionStorage.getItem('lp') === 'table' } catch { return false }
}

export function lpParam(name: string): string | null {
  return typeof window === 'undefined' ? null : new URLSearchParams(window.location.search).get(name)
}

export type LpSub = 'plans' | 'api-keys'

export function lpSub(): LpSub {
  return typeof window !== 'undefined' && /\/llm-providers\/api-keys/.test(window.location.pathname) ? 'api-keys' : 'plans'
}

export function lpGo(sub: LpSub) {
  const q = new URLSearchParams(window.location.search)
  for (const k of ['sheet', 'id', 'vendor']) q.delete(k)
  const s = q.toString()
  window.history.pushState(window.history.state, '', `/capacity/llm-providers/${sub}${s ? `?${s}` : ''}`)
  window.dispatchEvent(new PopStateEvent('popstate'))
}

/** A parameter of the address the tab opened on, read once at load: the router may rewrite the address before a lazy screen reads it. */
const FIRST = typeof window === 'undefined' ? new URLSearchParams() : new URLSearchParams(window.location.search)
export function lpFirst(name: string): string | null {
  return lpParam(name) ?? FIRST.get(name)
}
