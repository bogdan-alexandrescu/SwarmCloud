// CONSOLE VISUAL QA PASS 1, LANE QA1-FIX-18 (part of #2596): the GitHub
// permission cards on Config › Workspace Settings › Workspace setup.
//
//   * config-12: the count chip ("7/7 granted", "1 missing") sat in the card
//     head's action, right-aligned on a row of its own under the summary, in
//     the chip's monospace, with a lone Details beside it. It is on the title
//     row now, in the card's own face, and the fold button is the only action.
//   * config-13: at 390 the grants table (about 426px) scrolled sideways in a
//     326px card with nothing saying so, its For column cut at the edge. Under
//     600px it stacks like the MCP servers table: each cell carries its
//     column's name (`data-label`) and explicit roles, so the stacked blocks
//     keep their table semantics.
//   * config-14: "Manage on GitHub" was a ghost button, grey body text. It is
//     a `c-link`, as the page's other GitHub links are.
//
// Fixtures only; no network. Nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { serve, visible } from './repofixture'

const WAIT = { timeout: 4000 }
const REQUIRED = [
  { permission: 'contents', access: 'write', needed_for: "runs can't push their branch, and merges can't land", capabilities: ['push', 'merge'] },
  { permission: 'statuses', access: 'read', needed_for: "merges can't see CI, so they fail", capabilities: ['merge', 'ci'] },
]
const GRANTED = { contents: 'write', statuses: 'read' }

function answer(gap: boolean) {
  return {
    tenant_id: 'eng',
    checked_at: new Date().toISOString(),
    required: REQUIRED,
    workflow_files_required: null,
    app: { configured: true, slug: 'swarmcloud-example', granted: GRANTED, requested: GRANTED, missing: [], settings_url: null, error: null },
    orgs: [
      {
        org: 'example-org',
        account_type: 'Organization',
        installation_id: 42,
        granted: gap ? { contents: 'write' } : GRANTED,
        missing: gap ? [{ ...REQUIRED[1], granted: null }] : [],
        pending_update: gap,
        pending: [],
        review_url: 'https://github.com/organizations/example-org/settings/installations/42/permissions/update',
        error: null,
      },
    ],
    repositories: [],
    cached: false,
  }
}

async function mount(gap: boolean) {
  serve((m, url) => {
    if (m !== 'GET') return null
    if (url === '/v1/tenants/me') return { status: 200, body: { tenant: { tenant_id: 'eng' }, principal: { email: 'dev@example.com', domain: 'example.com', groups: [], is_admin: false } } }
    if (url.startsWith('/v1/forge/permissions')) return { status: 200, body: answer(gap) }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { GitHubPermissions } = await import('../PermissionCards')
  render(<GitHubPermissions />)
  await waitFor(() => expect(card()).toBeDefined(), WAIT)
  return card()!
}

const card = () => Array.from(document.querySelectorAll<HTMLElement>('.fp-card')).find((c) => visible(c.querySelector('h3 b')) === 'example-org')

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('config-12: the count chip is on the card’s title row, in the card’s face', () => {
  it.each([
    [false, '2/2 granted'],
    [true, '1 missing'],
  ])('gap=%s: the chip "%s" is in the title, not the head’s action', async (gap, words) => {
    const c = await mount(gap)
    const chip = c.querySelector<HTMLElement>('.c-chip')!
    expect(visible(chip)).toBe(words)
    // MUTATION: draw the chip back in the head's action (`.fp-acts`), and it wraps onto its own row.
    expect(chip.closest('.fp-title'), 'the chip is not on the title row').not.toBeNull()
    expect(chip.closest('.fp-acts')).toBeNull()
    // It comes before the summary, which takes a line of its own under the title row.
    const title = c.querySelector('.fp-title')!
    const kids = Array.from(title.children)
    expect(kids.indexOf(chip)).toBeLessThan(kids.indexOf(title.querySelector('.fp-summary')!))
    expect(chip.classList.contains('fp-count')).toBe(true)
  })

  it('sets the count chip in the proportional face and keeps the fold button on the title row', () => {
    const css = readFileSync(join(__dirname, '..', 'styles', 'forgeperms.css'), 'utf8')
    // MUTATION: drop the face rule, and the chip draws in components.css' mono.
    expect(css).toMatch(/\.fp-card \.c-chip\.fp-count\s*\{[^}]*font-family:\s*var\(--font\)/)
    // The head does not wrap above the phone width, so Details stays beside the title.
    expect(css).toMatch(/\.fp-card > \.c-card-h\s*\{[^}]*flex-wrap:\s*nowrap/)
  })
})

describe('config-13: the grants table stacks at phone width', () => {
  it('names every cell’s column (data-label) and states the roles a stacked table loses', async () => {
    const c = await mount(false)
    fireEvent.click(c.querySelector('button[aria-expanded]')!)
    const table = c.querySelector<HTMLElement>('table.fp-grants')!
    expect(table.getAttribute('role')).toBe('table')
    const rows = Array.from(table.querySelectorAll<HTMLElement>('tbody tr'))
    expect(rows).toHaveLength(2)
    for (const r of rows) {
      expect(r.getAttribute('role')).toBe('row')
      // MUTATION: drop a data-label, and that stacked value has no key.
      expect(Array.from(r.children).map((td) => td.getAttribute('data-label'))).toEqual(['GitHub permission', 'Needs', 'Granted', 'Enough', 'For'])
      expect(Array.from(r.children).every((td) => td.getAttribute('role') === 'cell')).toBe(true)
    }
    expect(Array.from(table.querySelectorAll('thead th')).every((th) => th.getAttribute('role') === 'columnheader')).toBe(true)
  })

  it('stacks under 600px, each row a record and the head row visually hidden', () => {
    const css = readFileSync(join(__dirname, '..', 'styles', 'forgeperms.css'), 'utf8')
    const at = css.indexOf('@media (max-width: 599px)')
    // MUTATION: delete the phone block, and the table scrolls sideways again.
    expect(at, 'no phone block for the grants table').toBeGreaterThan(-1)
    const block = css.slice(at)
    expect(block).toMatch(/table\.fp-grants > tbody > tr > td\s*\{[^}]*display:\s*block/)
    expect(block).toMatch(/table\.fp-grants > thead\s*\{[^}]*clip-path:\s*inset\(50%\)/)
    expect(block).toMatch(/td\[data-label\]::before\s*\{[^}]*content:\s*attr\(data-label\)/)
    // The cell rule above outranks `.fp-for`, so the For cell restates its column.
    // MUTATION: drop the td.fp-for rule, and "merges can't land" runs into "Push, Merge" on one line.
    expect(block).toMatch(/table\.fp-grants > tbody > tr > td\.fp-for\s*\{[^}]*display:\s*flex;[^}]*flex-direction:\s*column/)
  })
})

describe('config-14: Manage on GitHub is drawn as a link', () => {
  it('is a c-link to the installation, in a new tab', async () => {
    const c = await mount(false)
    fireEvent.click(c.querySelector('button[aria-expanded]')!)
    const manage = Array.from(c.querySelectorAll('a')).find((a) => visible(a) === 'Manage on GitHub')!
    // MUTATION: draw it as the ghost ButtonLink again (`c-btn is-ghost`, grey body text).
    expect(manage.classList.contains('c-link')).toBe(true)
    expect(manage.classList.contains('c-btn')).toBe(false)
    expect(manage.getAttribute('href')).toBe('https://github.com/organizations/example-org/settings/installations/42')
    expect(manage.getAttribute('target')).toBe('_blank')
  })
})
