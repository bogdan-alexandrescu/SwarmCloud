// The states guard's admin slice: prosestates.tsx visits each state in both themes, the base is its ceiling.
import { afterEach } from 'vitest'

import { proseStateGroup } from './prosestates'
import { ADMIN } from './prosestates.admin'
import { restoreAdminStubs } from './prosestates.admin.mounts'
import { restoreOpenedStubs } from './prosestates.opened.mounts'

afterEach(restoreAdminStubs)
afterEach(restoreOpenedStubs)

proseStateGroup('admin', ADMIN)
