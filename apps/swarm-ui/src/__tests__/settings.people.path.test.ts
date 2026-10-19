// SETTINGS › PEOPLE AND ROLES' ADDRESS (lane ROLES-UI-ADMIN, for
// ROLES-UI-PEOPLE; part of #1518; docs/design/workspace-roles.md §4.8).
//
// MUTATION: drop the `settings/people` entry from paths.ts FIXED and both
// directions go red -- the address falls to `/overview`, the path to null.

import { describe, expect, it } from 'vitest'
import { addressToPath, pathToAddress } from '../paths'

describe('Settings › People and roles', () => {
  it('is /settings/people, both ways', () => {
    expect(addressToPath('settings/people')).toBe('/settings/people')
    expect(pathToAddress('/settings/people')?.address).toBe('settings/people')
  })
})
