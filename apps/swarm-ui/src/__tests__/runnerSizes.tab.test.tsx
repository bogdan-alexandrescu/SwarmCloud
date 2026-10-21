// ADMIN › RUNNER SIZES, THE TAB (SIZES-UI; part of #1689,
// docs/design/runner-sizes.md §4.6 and §6).
//
// WHAT EACH CASE HOLDS:
//   * the tab is in the Admin section, beside Pool limits, marked admin;
//   * the Admin panel draws it for a platform admin and NOT for someone who
//     administers only a group workspace (`adminPagesFor`): sizes are a
//     platform setting, and a workspace admin may not change them (owner Q4:
//     "any platform admin");
//   * it has an address of its own, /admin/runner-sizes.
//
// MUTATIONS, each of which turns a case red: drop the tab from SECTIONS, or
// its `admin: true`; drop its row from PANEL_PAGES.admin; let `adminPagesFor`
// hand a workspace admin every Admin row; drop its entry from paths.ts FIXED.

import { describe, expect, it } from 'vitest'

import { SECTIONS } from '../App'
import { FIXED } from '../paths'
import { PANEL_PAGES, adminPagesFor } from '../Spine'

describe('Admin › Runner sizes is a platform admin page', () => {
  it('is an admin tab of the Admin section, right after Pool limits', () => {
    const admin = SECTIONS.find((s) => s.id === 'admin')!
    const ids = admin.tabs.map((t) => t.id)
    // MUTATION: drop the tab, or move it away from Pool limits.
    expect(ids.indexOf('runner-sizes')).toBe(ids.indexOf('limits') + 1)
    // MUTATION: drop `admin: true` and a member is offered the tab.
    expect(admin.tabs.find((t) => t.id === 'runner-sizes')).toEqual({ id: 'runner-sizes', label: 'Runner sizes', admin: true })
  })

  it('is drawn in the Admin panel for a platform admin and not for a workspace admin', () => {
    // MUTATION: drop the row from PANEL_PAGES.admin.
    expect(adminPagesFor(true, false).map((p) => p.key)).toContain('runner-sizes')
    expect(PANEL_PAGES.admin.find((p) => p.key === 'runner-sizes')?.to).toBe('admin/runner-sizes')
    // MUTATION: return every Admin row to a workspace admin.
    expect(adminPagesFor(false, true).map((p) => p.key)).not.toContain('runner-sizes')
    expect(adminPagesFor(false, false)).toEqual([])
  })

  it('has its own address', () => {
    // MUTATION: drop `admin/runner-sizes` from FIXED.
    expect(FIXED['admin/runner-sizes']).toBe('/admin/runner-sizes')
  })
})
