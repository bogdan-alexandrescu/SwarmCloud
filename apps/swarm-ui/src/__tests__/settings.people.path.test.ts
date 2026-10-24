// PEOPLE AND ROLES' ADDRESS (lane ROLES-UI-ADMIN, for ROLES-UI-PEOPLE; part
// of #1518; docs/design/workspace-roles.md §4.8). Settings › People and roles
// until CONFIG-NAV (part of #2157); Config › People and roles since, and the
// old path and address land there.
//
// MUTATION: drop the `config/people` entry from paths.ts FIXED and both
// directions go red -- the address falls to `/overview`, the path to null.
// MUTATION: drop `/settings/people` from OLD_CONFIG_PATHS and the old path is null.

import { describe, expect, it } from 'vitest'
import { addressToPath, pathToAddress } from '../paths'

describe('Config › People and roles', () => {
  it('is /config/people, both ways', () => {
    expect(addressToPath('config/people')).toBe('/config/people')
    expect(pathToAddress('/config/people')?.address).toBe('config/people')
  })

  it('lands /settings/people and settings/people there', () => {
    expect(addressToPath('settings/people')).toBe('/config/people')
    expect(pathToAddress('/settings/people')?.address).toBe('config/people')
  })
})
