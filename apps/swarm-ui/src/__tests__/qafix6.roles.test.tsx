// ADMIN › PEOPLE'S WORKSPACE ROLES, VISUAL QA RUN 6 (lane QAFIX6-ROLES; part
// of #1680). One case per item, each named by its run-6 finding and its #1680
// box:
//
//   a. 6:admin-004 (box 6343787071) -- the card's `wra-*` classes matched no
//      rule anywhere, so the roster drew as the browser's default lists: a
//      bullet per workspace, a hollow bullet per override, and three facts
//      as six indented lines (~300 px);
//   b. 6:admin-004, 6:admin-011 (box 6343788697) -- 'Name an admin' sat flush
//      under its 'person@example.com' field instead of beside it, 'Default
//      role in eng' touched its select, and the select and 'Set default' were
//      two heights.
//
// Layout is read off the shipped sheets with `painted` (the cascade at a
// stated width, since jsdom applies no `@media`). Fixtures only; no network.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, waitFor } from '@testing-library/react'
import { serve } from './repofixture'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { ALL_CSS } from './sheets'
import { lengthPx } from './tablefit'

type Json = Record<string, unknown>

const WAIT = { timeout: 4000 }
const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

const ROLES = [
  { name: 'viewer', summary: 'Sees everything in this workspace; changes nothing.' },
  { name: 'member', summary: 'Submits, approves, answers, cancels.' },
  { name: 'admin', summary: 'A member who also manages secrets and roles.' },
]

function override(email: string, role: string): Json {
  return { email, role, source: 'override', granted_by: 'owner@example.com', granted_at: '2026-10-22T10:00:00Z', granted_via: 'platform_admin' }
}

const PAGE: Json = {
  workspaces: [
    {
      tenant_id: 'eng', kind: 'group', group: 'eng@example.com', registered_by: 'terraform', default_role: 'member', default_source: 'grant',
      people: [override('bob@example.com', 'admin'), override('carol@example.com', 'viewer')],
    },
    { tenant_id: 'ops', kind: 'group', group: 'ops@example.com', registered_by: 'workspace_job', default_role: 'member', default_source: 'migration', people: [] },
  ],
  roles: ROLES,
  next_page_token: null,
}

async function mount(): Promise<HTMLElement> {
  serve((m, url) => {
    if (m === 'GET' && url.startsWith('/v1/admin/workspaces/roles')) return { status: 200, body: PAGE }
    if (m === 'GET' && url.startsWith('/v1/admin/roles/person?')) {
      return { status: 200, body: { email: 'bob@example.com', workspaces: [{ ...override('bob@example.com', 'admin'), tenant_id: 'eng', group: 'eng@example.com' }] } }
    }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { WorkspaceRolesCard, PersonWorkspaceRoles } = await import('../WorkspaceRolesAdmin')
  const { container } = render(
    <div className="app">
      <WorkspaceRolesCard />
      <PersonWorkspaceRoles email="bob@example.com" platformAdmin={false} />
    </div>,
  )
  await waitFor(() => {
    expect(container.querySelector('.wra-card li[data-tenant="eng"] .wra-people'), 'the eng roster never drew').not.toBeNull()
    expect(container.querySelector('.wra-person'), 'the drawer section never drew').not.toBeNull()
  }, WAIT)
  return container
}

/** Every `wra-*` class on the page. */
function wraClasses(root: Element): string[] {
  const seen = new Set<string>()
  for (const el of root.querySelectorAll('[class*="wra-"]')) for (const c of el.classList) if (c.startsWith('wra-')) seen.add(c)
  return [...seen].sort()
}

function px(el: Element, prop: string | string[], env: CascadeEnv = WIDE): number | null {
  return lengthPx(painted(el, prop, env), 0)
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.restoreAllMocks()
})

describe('QAFIX6-ROLES a: the roster is laid out by rules, not by the browser (6:admin-004, box 6343787071)', () => {
  it('gives every wra-* class the card renders a rule, lists without bullets and its facts as a label-value grid', async () => {
    const root = await mount()
    const classes = wraClasses(root)
    // The card, its list, a row, its head, the roster and its facts, the
    // override list, the forms, and the drawer's list.
    expect(classes.length).toBeGreaterThanOrEqual(10)
    // MUTATION: delete the `.wra-count` rule from styles/roles.css -- this names it.
    const unruled = classes.filter((c) => !new RegExp(`\\.${c}(?![\\w-])`).test(ALL_CSS))
    expect(unruled, 'wra-* classes with no rule in any shipped sheet').toEqual([])

    // MUTATION: drop `list-style: none` from `.wra-people` -- the override line gets its hollow bullet back.
    for (const sel of ['.wra-list', '.wra-people', '.wra-person']) {
      const list = root.querySelector(sel)!
      expect(painted(list, ['list-style', 'list-style-type'], WIDE), `${sel} draws browser bullets`).toMatch(/^none\b/)
      expect(px(list, ['padding', 'padding-left', 'padding-inline-start']) ?? 0, `${sel} keeps the browser's 40px indent`).toBe(0)
    }

    // Three facts take three lines: each label beside its value.
    const facts = root.querySelector('.wra-facts')!
    // MUTATION: make `.wra-facts` `display: block` -- the six-line default list comes back.
    expect(painted(facts, 'display', WIDE)).toBe('grid')
    expect(painted(facts, 'grid-template-columns', WIDE)?.trim().split(/\s+(?![^(]*\))/)).toHaveLength(2)
    for (const dd of facts.querySelectorAll('dd')) expect(px(dd, ['margin', 'margin-left', 'margin-inline-start']) ?? 0).toBe(0)
    expect(px(facts, ['margin', 'margin-top', 'margin-bottom']) ?? 0).toBe(0)
  })
})

describe('QAFIX6-ROLES b: the controls (6:admin-004, 6:admin-011, box 6343788697)', () => {
  it('puts the email field and Name an admin on one row, 8px between Default role and its select, and one height for select and Set default', async () => {
    const root = await mount()
    const row = root.querySelector('li[data-tenant="eng"]')!
    const forms = [...row.querySelectorAll('form.wra-form')]
    expect(forms).toHaveLength(2)
    const [nameForm, defaultForm] = forms as [HTMLFormElement, HTMLFormElement]
    const input = nameForm.querySelector('input[type="email"]')!
    const name = [...nameForm.querySelectorAll('button')].find((b) => /Name an admin/.test(b.textContent ?? ''))!
    const select = defaultForm.querySelector('select')!
    const set = [...defaultForm.querySelectorAll('button')].find((b) => /Set default/.test(b.textContent ?? ''))!

    for (const env of [WIDE, PHONE]) {
      for (const f of forms) {
        // MUTATION: drop `display: flex` from `.wra-form` -- the button falls under the field.
        expect(painted(f, 'display', env)).toBe('flex')
        expect(painted(f, 'flex-direction', env) ?? 'row').toBe('row')
        expect(painted(f, 'align-items', env)).toBe('center')
        // MUTATION: set `.wra-form`'s gap to 0 -- 'Default role in eng' touches its select again.
        expect(px(f, ['gap', 'column-gap'], env), 'the gap between a label, its control and its button').toBe(8)
      }
      // The field does not claim the row: `.ur-search` is `width: 100%`.
      expect(painted(input, 'width', env)).not.toBe('100%')
      const flex = painted(input, ['flex', 'flex-basis'], env)
      const basis = flex === null ? null : lengthPx(flex.trim().split(/\s+/).pop()!, 0)
      expect(basis, 'the field has a basis to shrink from').not.toBeNull()
      // At 390 the card's content is ~330px: the field's basis, the gap and the
      // button (~110px) fit on it.
      expect(basis! + 8 + 110).toBeLessThanOrEqual(330)

      // MUTATION: give `.wra-form select` `height: var(--h-ctl)` -- it stands 4px taller than Set default.
      const sh = px(select, ['height', 'block-size'], env)
      const bh = px(set, ['height', 'block-size'], env)
      expect(sh, 'the select has a stated height').not.toBeNull()
      expect(sh).toBe(bh)
      expect(px(input, ['height', 'block-size'], env)).toBe(px(name, ['height', 'block-size'], env))
    }
  })
})
