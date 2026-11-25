// The states guard's overview slice: prosestates.tsx visits each state in both themes, the base is its ceiling.
import { afterEach } from 'vitest'

import { proseStateGroup } from './prosestates'
import { OVERVIEW } from './prosestates.overview'
import { restoreOverviewStubs } from './prosestates.overview.mounts'

afterEach(restoreOverviewStubs)

proseStateGroup('overview', OVERVIEW)
