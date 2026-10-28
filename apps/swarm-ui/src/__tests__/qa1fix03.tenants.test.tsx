// VISUAL QA PASS 1, LANE QA1-FIX-03 (part of #2596): the Tenants identity,
// Activity.tsx `TenantIdent` and styles/admin.css.
//
//   admin-shell-02  at 390 the identity and its copy control ran past the
//                   card to x=461 and the page scrolled sideways
//   admin-shell-18  at 1440 the identity was cut twice, `sw…u-bogdan@ex…`
//
// Read through `cascade` (cssgate.ts), which weighs media conditions and
// specificity as a browser does; jsdom's own getComputedStyle does neither.

import { render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup } from '@testing-library/react'

import { TenantIdent } from '../Activity'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const SA = 'swarm-agent-worker-u-bogdan@example-project.iam.gserviceaccount.com'

afterEach(cleanup)

describe('admin-shell-02: at 390 the identity stays inside the card', () => {
  it('wraps the row, so the copy control takes the next line rather than the page edge', () => {
    render(<TenantIdent value={SA} noun="service account" tenant="u-bogdan" phone />)
    const row = document.querySelector('.ten-ident-row')!
    const ident = row.querySelector('.ten-ident')!
    // MUTATION: leave the row `inline-flex` with no wrap: its minimum is the
    // widest run plus the 44px control, 461px of page in 390.
    expect(painted(row, 'display', PHONE)).toBe('flex')
    expect(painted(row, ['flex-wrap', 'flex-flow'], PHONE)).toBe('wrap')
    expect(painted(row, 'max-width', PHONE)).toBe('100%')
    // The identity is never wider than the value column, so its runs break
    // inside it.
    expect(painted(ident, 'min-width', PHONE)).toBe('0')
    expect(painted(ident, 'max-width', PHONE)).toBe('100%')
  })
})

describe('admin-shell-18: at 1440 the identity is cut once, in its domain', () => {
  it('wraps the prefix and the key as whole runs and cuts only the domain', () => {
    render(<TenantIdent value={SA} noun="service account" tenant="u-bogdan" phone={false} />)
    const row = document.querySelector('.ten-ident-row')!
    const ident = row.querySelector('.ten-ident')!
    const head = ident.querySelector('.ten-ident-head')!
    const tail = ident.querySelector('.ten-ident-tail')!
    // MUTATION: `flex: 1 1 0` on the identity -- the copy control keeps its
    // place beside it and the identity has 16 characters.
    expect(painted(ident, 'flex', WIDE)).toBe('1 1 auto')
    // MUTATION: no wrap inside the identity: the prefix shrinks with the
    // domain, the second cut.
    expect(painted(ident, ['flex-wrap', 'flex-flow'], WIDE)).toBe('wrap')
    expect(painted(head, 'flex', WIDE)).toBe('0 1 auto')
    expect(painted(head, 'max-width', WIDE)).toBe('100%')
    // The domain is the one piece that gives way.
    expect(painted(tail, 'flex', WIDE)).toBe('1 1 0')
    expect(painted(tail, 'text-overflow', WIDE)).toBe('ellipsis')
    // And the whole value is on the identity's title and the control's name.
    expect(ident.getAttribute('title')).toBe(SA)
    expect(row.querySelector('button.ten-copy')?.getAttribute('aria-label')).toBe(`Copy service account ${SA}`)
  })
})
