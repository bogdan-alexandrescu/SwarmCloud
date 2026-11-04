// PEOPLE AND ROLES' ADDRESS (lane ROLES-UI-ADMIN, for ROLES-UI-PEOPLE; part
// of #1518; docs/design/workspace-roles.md §4.8). Settings › People and roles
// until CONFIG-NAV (part of #2157); Config › People and roles until the admin
// routes lane (part of #2688), and since then the selected workspace's own
// page, People tab: every one of its old paths and addresses lands there.
//
// MUTATION: drop the `config/people` branch from paths.ts
// `movedToWorkspacePage` and every row goes red -- each lands on /config/people.
// MUTATION: drop `/settings/people` from OLD_CONFIG_PATHS and the old path is null.

import { describe, expect, it } from 'vitest'
import { addressToPath, pathToAddress } from '../paths'

describe('Config › People and roles, now the workspace page\'s People tab', () => {
  it('opens /config/people on the selected workspace\'s People tab, both ways', () => {
    expect(addressToPath('config/people')).toBe('/admin/workspaces?tab=people')
    expect(pathToAddress('/config/people')?.address).toBe('admin/workspaces?tab=people')
  })

  it('lands /settings/people and settings/people there', () => {
    expect(addressToPath('settings/people')).toBe('/admin/workspaces?tab=people')
    expect(pathToAddress('/settings/people')?.address).toBe('admin/workspaces?tab=people')
  })
})
