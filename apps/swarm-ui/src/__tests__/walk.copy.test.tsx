/**
 * WALKTHROUGH E (owner, 2026-10-03, live console at 1440x900): implementation
 * explanations do not belong on a user's screen.
 *
 * Measured: Submit's "Start from a recent one" said "This list needs the API
 * to keep a short per-person list of what you submitted… It does not today";
 * runner reasons said "POST /v1/runs takes no runner" and "disabled: the
 * merge chain (#295) is disabled for every tenant until signed step specs
 * (#342)…"; Overview's lines said "Decided counts from GET /v1/outcomes…".
 *
 * THE SWEEP: every section page and every object page the fixtures serve is
 * rendered, and its VISIBLE text -- not titles, not screen-reader-only words,
 * not a file's own contents -- may carry no API path, no issue or contract
 * number and no code identifier. Ids that ARE the user's data (`task_…`,
 * `wf_…`) and the platform's own tokens a screen shows verbatim (a state, a
 * park reason) are not identifiers in this sense. The why moved into the `?`
 * cards and the Help section, where it is still one click away.
 *
 * The API reads page and the reads strip under every page are exempt: what
 * they show IS the route each read called, as data.
 *
 * MUTATION: put "POST /v1/runs takes no runner" back in IssueSubmit's runner
 * reason, or "#295" back in a runner's line -- the sweep names the route.
 */
import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { App, SECTIONS } from '../App'
import { PARK_REASONS, REAL_STATES } from '../types'

const WAIT = { timeout: 8000 }

afterEach(() => {
  window.location.hash = ''
  window.history.replaceState(null, '', '/')
})

/**
 * The platform's own tokens a screen prints verbatim: they are data, not
 * code. A state, a park reason, and the reason an account's refresh failed as
 * the API wrote it on the account (`reauth_required`).
 */
const TOKENS = new Set<string>([
  ...PARK_REASONS.map((r) => String(r).toLowerCase()),
  ...REAL_STATES.map((s) => s.toLowerCase()),
  'reauth_required',
])

const RULES: ReadonlyArray<readonly [string, RegExp]> = [
  ['an API path', /\/v1\/[a-z]/i],
  ['an HTTP verb and path', /\b(?:GET|POST|PUT|PATCH|DELETE) \//],
  ['an issue or contract number', /(?:^|[\s(,])#\d{2,4}\b/],
  ['a contract invariant by number', /\binvariant \d+\b/i],
  ['a code identifier', /\b[a-z]{2,}(?:_[a-z]{2,})+\b/],
]

/** The visible words under `root`: no titles, no screen-reader-only text, no file contents, no reads strip. */
function visibleText(root: Element): string {
  const copy = root.cloneNode(true) as Element
  for (const el of copy.querySelectorAll('.sk-vh, .sr-only, [aria-hidden="true"] .sk-vh, pre, .ag-logtext, .dock, .ctl-dock, [data-reads-strip]')) el.remove()
  // A storage location is the user's data, underscores and all: a log's
  // `gs://…/agent_stdout.log` is what they copy, not an identifier we chose.
  // Text node by text node, so two neighbouring elements' words do not run
  // together into one token ("reauth_required" + "weekly").
  const words: string[] = []
  const walk = document.createTreeWalker(copy, NodeFilter.SHOW_TEXT)
  for (let n = walk.nextNode(); n !== null; n = walk.nextNode()) {
    const t = n.textContent ?? ''
    // An id drawn as an id -- one token alone in a code, mono or id element,
    // like the step a workflow integrates through -- is the user's data.
    if (n.parentElement?.matches('code, .mono, .id') && /^\s*[\w.-]+\s*$/.test(t)) continue
    words.push(t)
  }
  return words.join(' ').replace(/gs:\/\/\S+/g, ' ').replace(/\s+/g, ' ')
}

function findings(text: string): string[] {
  const out: string[] = []
  for (const [what, re] of RULES) {
    const g = new RegExp(re.source, re.flags.includes('g') ? re.flags : `${re.flags}g`)
    for (const m of text.matchAll(g)) {
      const hit = m[0].trim()
      if (what === 'a code identifier' && TOKENS.has(hit)) continue
      const at = m.index ?? 0
      out.push(`${what}: "${hit}" in "…${text.slice(Math.max(0, at - 50), at + 60).trim()}…"`)
    }
  }
  return out
}

const PAGES: readonly string[] = [
  ...SECTIONS.filter((s) => s.id !== 'api').flatMap((s) => s.tabs.map((t) => `/#${s.id}/${t.id}`)),
  '/submit',
  '/submit/task',
  '/submit/workflow',
  '/submit/issue',
  '/workflows/wf_5e5ad3b6f7da4299a839',
  '/workflows/wf_5e5ad3b6f7da4299a839/table',
  '/#work/task/task_a073aff5',
  '/#work/task/task_a3881bec',
]

describe('E: no implementation explanation in visible copy', () => {
  it('prints no API path, issue number or code identifier on any page', async () => {
    const report: string[] = []
    let visited = 0
    for (const path of PAGES) {
      window.history.replaceState(null, '', path)
      const { unmount } = render(<App />)
      // Every fixture read has landed (the slowest simulated latency is
      // well under this), so the page's loaded copy is what is swept.
      await new Promise((r) => setTimeout(r, 900))
      await waitFor(() => expect(document.querySelector('main')).not.toBeNull(), WAIT)
      for (const f of findings(visibleText(document.body))) report.push(`${path}: ${f}`)
      visited++
      unmount()
    }
    expect(visited, 'the sweep did not visit every page').toBe(PAGES.length)
    expect(report).toEqual([])
  }, 240_000)
})
