// THE ROLE LADDER, AS THE CONSOLE READS IT (lane ROLES-UI-PEOPLE;
// docs/design/workspace-roles.md §4.1, §4.12; part of #1518).
//
// viewer < member < admin, and a role permits everything below it. A name
// this build does not know permits nothing: an unread or newer role is never
// read as a wider one (the server's rule for `role_unavailable`).
//
// MUTATIONS, each of which turns a case red: rank an unknown name as member;
// let `atLeast(undefined, 'viewer')` pass; offer `admin` as a default.

import { describe, expect, it } from 'vitest'
import { DEFAULT_ROLES, ROLES, atLeast, roleOf } from '../roles'

describe('the role ladder', () => {
  it('is viewer < member < admin, each permitting what is below it', () => {
    expect(ROLES).toEqual(['viewer', 'member', 'admin'])
    expect(atLeast('admin', 'member')).toBe(true)
    expect(atLeast('admin', 'admin')).toBe(true)
    expect(atLeast('member', 'viewer')).toBe(true)
    expect(atLeast('member', 'admin')).toBe(false)
    expect(atLeast('viewer', 'member')).toBe(false)
  })

  it('permits nothing to a role it cannot read', () => {
    expect(atLeast(undefined, 'viewer')).toBe(false)
    expect(atLeast(null, 'viewer')).toBe(false)
    expect(atLeast('owner', 'viewer')).toBe(false)
    expect(atLeast('', 'viewer')).toBe(false)
  })

  it('offers viewer and member, never admin, as a default', () => {
    expect([...DEFAULT_ROLES]).toEqual(['viewer', 'member'])
  })

  it("reads a TenantChoice's role when served, and says so when it is not", () => {
    expect(roleOf({ tenant_id: 'eng', display_name: 'eng@example.com', role: { name: 'viewer', source: 'override' } })).toBe('viewer')
    expect(roleOf({ tenant_id: 'eng', display_name: 'eng@example.com' })).toBeNull()
  })
})
