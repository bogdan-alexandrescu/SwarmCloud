// The states guard's overview slice: prosestates.tsx visits each state in both themes, the base is its ceiling.
import { afterEach } from 'vitest'

import { proseStateGroup } from './prosestates'
import { restoreOpenedStubs } from './prosestates.opened.mounts'
import { OVERVIEW } from './prosestates.overview'
import { restoreOverviewStubs } from './prosestates.overview.mounts'

afterEach(restoreOverviewStubs)

afterEach(restoreOpenedStubs)
proseStateGroup('overview', OVERVIEW)
